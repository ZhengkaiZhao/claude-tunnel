#!/usr/bin/env python3
"""claude-tunnel: One-command Claude Code remote tunnel tool.

Usage:
    python3 claude_tunnel.py init        # Interactive setup (with env check)
    python3 claude_tunnel.py check       # Check environment dependencies
    python3 claude_tunnel.py up          # One-key start (deploy + connect)
    python3 claude_tunnel.py down        # Disconnect + cleanup
    python3 claude_tunnel.py status      # Check connection state
    python3 claude_tunnel.py deploy      # Deploy relay to server only
    python3 claude_tunnel.py c-start     # C-side: gateway + reverse tunnel
    python3 claude_tunnel.py a-start     # A-side: local tunnel + Claude Code
    python3 claude_tunnel.py web         # Local Web UI (optional)
"""
from __future__ import annotations

import argparse
import atexit
import http.client
import json
import os
import platform
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__version__ = "0.1.0"

# Global stop event for clean Ctrl+C handling
_stop_event = threading.Event()

CONFIG_PATH = Path.home() / ".claude-tunnel.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "role": "",
    "server": {"host": "", "port": 22, "user": "root", "password": "", "key_file": None},
    "tunnel": {"relay_port": 8088, "forward_port": 19001},
    "gateway": {
        "host": "127.0.0.1",
        "port": 8787,
        "token": "change-me",
        "upstream_base_url": "",
        "upstream_auth_token": "",
    },
    "claude": {"local_port": 50000, "model": "claude-sonnet-4-6", "project_dir": "", "command": ""},
    "room": {"name": "default", "token": "change-me"},
}

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    if platform.system() != "Windows":
        CONFIG_PATH.chmod(0o600)
    print(f"[ok] config saved to {CONFIG_PATH}")


def ensure_config() -> dict[str, Any]:
    cfg = load_config()
    if not cfg or not cfg.get("server", {}).get("host"):
        print("[!] No config found. Running init...")
        cfg = cmd_init_interactive()
    return cfg


def _load_claude_settings() -> dict[str, str]:
    """Read ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN from ~/.claude/settings.json."""
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return {}
    try:
        with settings_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("env", {})
    except Exception:
        return {}


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or default


def cmd_init_interactive() -> dict[str, Any]:
    """Run interactive init — delegates to ct_init if rich is available."""
    try:
        from ct_init import cmd_init_interactive as _rich_init
        cfg = _rich_init()
        if cfg is None:
            print("  Init cancelled.")
            sys.exit(0)
        save_config(cfg)
        return cfg
    except ImportError:
        pass

    # Fallback: simple prompts
    print("\n=== claude-tunnel init ===\n")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    cfg["role"] = ask("Role (a=local dev, c=model server)", "c")

    print("\n  --- Public VPS (relay server) ---")
    cfg["server"]["host"] = ask("Server host/IP")
    cfg["server"]["port"] = int(ask("SSH port", "22"))
    cfg["server"]["user"] = ask("SSH user", "root")
    cfg["server"]["password"] = ask("SSH password (empty if using key)", "")
    key = ask("SSH key file path (empty if using password)", "")
    cfg["server"]["key_file"] = key or None

    print("\n  --- Tunnel ---")
    cfg["tunnel"]["relay_port"] = int(ask("Relay HTTP port on server", "8088"))
    cfg["tunnel"]["forward_port"] = int(ask("Forward port on server", "19001"))

    cfg["room"]["name"] = ask("Room name", "default")
    cfg["room"]["token"] = ask("Room token (shared secret)", "change-me")

    if cfg["role"] == "c":
        print("\n  --- Gateway (C-side) ---")
        claude_env = _load_claude_settings()
        auto_url = claude_env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        auto_token = claude_env.get("ANTHROPIC_AUTH_TOKEN", "")
        if auto_token:
            print(f"  [auto] found credentials in ~/.claude/settings.json")

        cfg["gateway"]["port"] = int(ask("Local gateway port", "8787"))
        cfg["gateway"]["token"] = ask("Gateway auth token", "change-me")
        cfg["gateway"]["upstream_base_url"] = ask("Upstream API base URL", auto_url)
        cfg["gateway"]["upstream_auth_token"] = ask("Upstream API key/token (Enter to use local claude settings)", auto_token)

    else:
        print("\n  --- Claude Code (A-side) ---")
        cfg["claude"]["local_port"] = int(ask("Local port for Claude", "50000"))
        cfg["claude"]["model"] = ask("Model", "claude-sonnet-4-6")
        cfg["claude"]["project_dir"] = ask("Project directory", str(Path.cwd()))
        cfg["claude"]["command"] = ask("Claude command (empty=auto-detect)", "")
        cfg["gateway"]["token"] = ask("Gateway auth token (same as C-side)", "change-me")

    save_config(cfg)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# SSH helpers
# ═══════════════════════════════════════════════════════════════════════════════

_SSH_PROCS: list[subprocess.Popen] = []
_ASKPASS_FILES: list[str] = []

IS_WINDOWS = platform.system() == "Windows"

try:
    import paramiko as _paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


def _make_askpass(password: str) -> str:
    """Write a temp askpass script that echoes the password.
    On Windows, SSH_ASKPASS must be an executable (.bat/.exe), not a .sh file.
    """
    import stat
    import tempfile
    if IS_WINDOWS:
        # Windows: .bat file, escape % and special chars
        safe = password.replace("%", "%%").replace('"', '""')
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bat", delete=False) as f:
            f.write(f"@echo off\necho {safe}\n")
            path = f.name
    else:
        safe = password.replace("'", "'\\''")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write(f"#!/bin/sh\necho '{safe}'\n")
            path = f.name
        import stat as _stat
        os.chmod(path, _stat.S_IRWXU)
    _ASKPASS_FILES.append(path)
    return path


def _ssh_env(password: str) -> dict[str, str]:
    """Return env vars that make SSH use our askpass script silently."""
    env = os.environ.copy()
    env["SSH_ASKPASS"] = _make_askpass(password)
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.pop("DISPLAY", None)
    return env


def _has_plink() -> bool:
    return shutil.which("plink") is not None


def _plink_base_args(cfg: dict[str, Any]) -> list[str]:
    """Build plink (PuTTY) args — used on Windows when plink is available."""
    srv = cfg["server"]
    args = ["plink", "-batch"]
    if srv.get("key_file"):
        args += ["-i", srv["key_file"]]
    elif srv.get("password"):
        args += ["-pw", srv["password"]]
    if srv.get("port") and srv["port"] != 22:
        args += ["-P", str(srv["port"])]
    args.append(f"{srv['user']}@{srv['host']}")
    return args


def _ssh_base_args(cfg: dict[str, Any]) -> list[str]:
    srv = cfg["server"]
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30"]
    if srv.get("key_file"):
        args += ["-i", srv["key_file"]]
    elif srv.get("password"):
        args += ["-o", "PubkeyAuthentication=no",
                 "-o", "PreferredAuthentications=keyboard-interactive,password"]
    if srv.get("port") and srv["port"] != 22:
        args += ["-p", str(srv["port"])]
    args.append(f"{srv['user']}@{srv['host']}")
    return args


def _paramiko_exec(cfg: dict[str, Any], remote_cmd: str) -> tuple[int, str, str]:
    """Run a remote command via paramiko (pure Python SSH, no external tools)."""
    srv = cfg["server"]
    client = _paramiko.SSHClient()
    client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = {
        "hostname": srv["host"],
        "port": srv.get("port", 22),
        "username": srv["user"],
        "timeout": 30,
    }
    if srv.get("key_file"):
        connect_kwargs["key_filename"] = srv["key_file"]
    elif srv.get("password"):
        connect_kwargs["password"] = srv["password"]
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"] = False
    client.connect(**connect_kwargs)
    _, stdout, stderr = client.exec_command(remote_cmd, timeout=60)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    client.close()
    return exit_code, out, err


def _paramiko_upload(cfg: dict[str, Any], local_path: str, remote_path: str) -> tuple[int, str]:
    """Upload a file via paramiko SFTP."""
    srv = cfg["server"]
    client = _paramiko.SSHClient()
    client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = {
        "hostname": srv["host"],
        "port": srv.get("port", 22),
        "username": srv["user"],
        "timeout": 30,
    }
    if srv.get("key_file"):
        connect_kwargs["key_filename"] = srv["key_file"]
    elif srv.get("password"):
        connect_kwargs["password"] = srv["password"]
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"] = False
    client.connect(**connect_kwargs)
    sftp = client.open_sftp()
    sftp.put(local_path, remote_path)
    sftp.close()
    client.close()
    return 0, ""


def ssh_exec(cfg: dict[str, Any], remote_cmd: str) -> tuple[int, str, str]:
    if HAS_PARAMIKO and cfg["server"].get("password"):
        try:
            return _paramiko_exec(cfg, remote_cmd)
        except Exception as e:
            return 1, "", str(e)
    password = cfg["server"].get("password", "")
    if IS_WINDOWS and _has_plink():
        args = _plink_base_args(cfg) + [remote_cmd]
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True)
    else:
        args = _ssh_base_args(cfg) + [remote_cmd]
        env = _ssh_env(password) if password else None
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, env=env)
    return proc.returncode, proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")


def scp_upload(cfg: dict[str, Any], local_path: str, remote_path: str) -> tuple[int, str]:
    """Upload a local file to the server via scp / sftp."""
    srv = cfg["server"]
    password = srv.get("password", "")

    if HAS_PARAMIKO and password:
        try:
            return _paramiko_upload(cfg, local_path, remote_path)
        except Exception as e:
            return 1, str(e)

    if IS_WINDOWS and shutil.which("pscp"):
        args = ["pscp", "-batch"]
        if srv.get("key_file"):
            args += ["-i", srv["key_file"]]
        elif password:
            args += ["-pw", password]
        if srv.get("port") and srv["port"] != 22:
            args += ["-P", str(srv["port"])]
        args += [local_path, f"{srv['user']}@{srv['host']}:{remote_path}"]
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True)
        return proc.returncode, proc.stderr.decode(errors="replace")

    args = ["scp", "-o", "StrictHostKeyChecking=accept-new"]
    if srv.get("key_file"):
        args += ["-i", srv["key_file"]]
    elif password:
        args += ["-o", "PubkeyAuthentication=no",
                 "-o", "PreferredAuthentications=keyboard-interactive,password"]
    if srv.get("port") and srv["port"] != 22:
        args += ["-P", str(srv["port"])]
    args += [local_path, f"{srv['user']}@{srv['host']}:{remote_path}"]
    env = _ssh_env(password) if password else None
    proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, env=env)
    return proc.returncode, proc.stderr.decode(errors="replace")


def _paramiko_tunnel_bg(cfg: dict[str, Any], tunnel_flag: str, mapping: str) -> threading.Thread:
    """Create an SSH tunnel using paramiko (works without terminal interaction)."""
    srv = cfg["server"]
    parts = mapping.split(":")
    if tunnel_flag == "-L":
        # -L local_host:local_port:remote_host:remote_port
        local_host, local_port_s, remote_host, remote_port_s = parts[0], parts[1], parts[2], parts[3]
        local_port = int(local_port_s)
        remote_port = int(remote_port_s)
    else:
        # -R remote_bind:remote_port:local_host:local_port
        _remote_bind, remote_port_s, local_host, local_port_s = parts[0], parts[1], parts[2], parts[3]
        local_port = int(local_port_s)
        remote_port = int(remote_port_s)

    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = {
        "hostname": srv["host"],
        "port": int(srv.get("port", 22)),
        "username": srv.get("user", "root"),
    }
    if srv.get("key_file"):
        connect_kwargs["key_filename"] = srv["key_file"]
    elif srv.get("password"):
        connect_kwargs["password"] = srv["password"]
    client.connect(**connect_kwargs)

    transport = client.get_transport()

    if tunnel_flag == "-L":
        import socketserver

        class LocalForwardHandler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    chan = transport.open_channel("direct-tcpip",
                                                 (remote_host, remote_port),
                                                 self.request.getpeername())
                except Exception:
                    return
                if chan is None:
                    return
                import select
                while True:
                    r, _, _ = select.select([self.request, chan], [], [], 1.0)
                    if self.request in r:
                        data = self.request.recv(4096)
                        if not data:
                            break
                        chan.sendall(data)
                    if chan in r:
                        data = chan.recv(4096)
                        if not data:
                            break
                        self.request.sendall(data)
                chan.close()
                self.request.close()

        class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        server = ThreadedTCPServer((local_host, local_port), LocalForwardHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        t._paramiko_client = client
        t._paramiko_server = server
        return t
    else:
        transport.request_port_forward("", remote_port)
        def _accept_loop():
            while transport.is_active():
                chan = transport.accept(timeout=1)
                if chan is None:
                    continue
                def _forward(ch):
                    try:
                        sock = socket.create_connection((local_host, local_port))
                    except Exception:
                        ch.close()
                        return
                    import select
                    while True:
                        r, _, _ = select.select([sock, ch], [], [], 1.0)
                        if sock in r:
                            data = sock.recv(4096)
                            if not data:
                                break
                            ch.sendall(data)
                        if ch in r:
                            data = ch.recv(4096)
                            if not data:
                                break
                            sock.sendall(data)
                    sock.close()
                    ch.close()
                threading.Thread(target=_forward, args=(chan,), daemon=True).start()
        t = threading.Thread(target=_accept_loop, daemon=True)
        t.start()
        t._paramiko_client = client
        return t


def ssh_tunnel_bg(cfg: dict[str, Any], tunnel_flag: str, mapping: str) -> subprocess.Popen:
    password = cfg["server"].get("password", "")
    if HAS_PARAMIKO and password:
        tunnel_thread = _paramiko_tunnel_bg(cfg, tunnel_flag, mapping)
        class _FakeProc:
            """Mimics subprocess.Popen interface for paramiko tunnel."""
            def __init__(self, thread):
                self._thread = thread
                self.returncode = None
            def poll(self):
                return None if self._thread.is_alive() else 0
            def terminate(self):
                client = getattr(self._thread, '_paramiko_client', None)
                server = getattr(self._thread, '_paramiko_server', None)
                if server:
                    server.shutdown()
                if client:
                    client.close()
            def kill(self):
                self.terminate()
            def wait(self, timeout=None):
                self._thread.join(timeout=timeout)
                return 0
        proc = _FakeProc(tunnel_thread)
        _SSH_PROCS.append(proc)
        return proc
    if IS_WINDOWS and _has_plink():
        args = _plink_base_args(cfg)
        args_insert = ["-N", tunnel_flag, mapping]
        args = args[:-1] + args_insert + [args[-1]]
    else:
        args = _ssh_base_args(cfg)
        args_insert = ["-o", "ExitOnForwardFailure=yes", "-N", tunnel_flag, mapping]
        args = args[:1] + args_insert + args[1:]
    if password:
        env = _ssh_env(password)
    else:
        env = None
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, env=env)
    _SSH_PROCS.append(proc)
    return proc


# ═══════════════════════════════════════════════════════════════════════════════
# Embedded relay (deployed to server automatically)
# ═══════════════════════════════════════════════════════════════════════════════

RELAY_SCRIPT = r'''#!/usr/bin/env python3
"""Minimal relay: room state + heartbeat + auto-exit."""
import json, signal, sys, threading, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOMS = {}
LOCK = threading.RLock()
IDLE_TIMEOUT = 300  # auto-exit after 5 min no heartbeat

class Room:
    def __init__(self, name, token):
        self.name = name
        self.token = token
        self.ts = time.time()
        self.a_alive = False
        self.c_alive = False
        self.switch_open = False
    def heartbeat(self, role):
        self.ts = time.time()
        if role == "a": self.a_alive = True
        if role == "c": self.c_alive = True
    def snapshot(self):
        return {"name": self.name, "a_alive": self.a_alive, "c_alive": self.c_alive,
                "switch_open": self.switch_open, "age": time.time() - self.ts}

def get_room(name, token):
    with LOCK:
        r = ROOMS.get(name)
        if r is None:
            r = Room(name, token)
            ROOMS[name] = r
        if r.token != token:
            return None
        return r

class H(BaseHTTPRequestHandler):
    server_version = "claude-tunnel-relay/0.1"
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/health":
            self._j(200, {"ok": True, "rooms": len(ROOMS)})
            return
        if self.path == "/rooms":
            with LOCK:
                data = {name: r.snapshot() for name, r in ROOMS.items()}
            self._j(200, data)
            return
        self._j(404, {"error": "not found"})
    def do_POST(self):
        ln = int(self.headers.get("Content-Length", "0") or "0")
        d = json.loads(self.rfile.read(ln)) if ln > 0 else {}
        room = get_room(d.get("room", ""), d.get("token", ""))
        if not room:
            self._j(401, {"error": "bad room/token"})
            return
        action = d.get("action", "heartbeat")
        role = d.get("role", "")
        room.heartbeat(role)
        if action == "open" and role == "c":
            room.switch_open = True
        elif action == "close":
            room.switch_open = False
        self._j(200, {"ok": True, "room": room.snapshot()})
    def _j(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def watchdog():
    while True:
        time.sleep(60)
        with LOCK:
            now = time.time()
            for r in list(ROOMS.values()):
                if now - r.ts > IDLE_TIMEOUT:
                    del ROOMS[r.name]
            if not ROOMS:
                print("relay: no active rooms, exiting", flush=True)
                sys.exit(0)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Thread(target=watchdog, daemon=True).start()
    print(f"relay listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


# ═══════════════════════════════════════════════════════════════════════════════
# Deploy relay to server
# ═══════════════════════════════════════════════════════════════════════════════

_RELAY_PID: int | None = None


def cmd_deploy(cfg: dict[str, Any]) -> int:
    global _RELAY_PID
    srv = cfg["server"]
    room = cfg["room"]["name"]
    relay_port = cfg["tunnel"]["relay_port"]
    remote_dir = f"/tmp/claude-tunnel-{room}"

    print(f"[deploy] uploading relay to {srv['user']}@{srv['host']}:{remote_dir}")

    # Write relay script to a local temp file, upload via scp, then start it.
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(RELAY_SCRIPT)
        local_relay = f.name

    try:
        # Create remote dir
        rc, _, stderr = ssh_exec(cfg, f"mkdir -p {remote_dir}")
        if rc != 0:
            print(f"[deploy] mkdir failed: {stderr.strip()}")
            return 1

        # Upload relay script
        rc, stderr = scp_upload(cfg, local_relay, f"{remote_dir}/relay.py")
        if rc != 0:
            print(f"[deploy] scp failed: {stderr.strip()}")
            return 1
    finally:
        os.unlink(local_relay)

    # Start relay on server
    start_cmd = (
        f"kill $(cat {remote_dir}/relay.pid 2>/dev/null) 2>/dev/null; "
        f"nohup python3 {remote_dir}/relay.py {relay_port} > {remote_dir}/relay.log 2>&1 & "
        f"echo $! | tee {remote_dir}/relay.pid"
    )
    rc, stdout, stderr = ssh_exec(cfg, start_cmd)
    if rc != 0:
        print(f"[deploy] start failed: {stderr.strip() or stdout.strip()}")
        return 1

    lines = [l.strip() for l in stdout.strip().split("\n") if l.strip() and l.strip().isdigit()]
    try:
        _RELAY_PID = int(lines[-1]) if lines else None
    except (ValueError, IndexError):
        _RELAY_PID = None
    print(f"[deploy] relay running on server, PID={_RELAY_PID}, port={relay_port}")
    return 0


def cmd_down(cfg: dict[str, Any]) -> int:
    room = cfg["room"]["name"]
    remote_dir = f"/tmp/claude-tunnel-{room}"
    print(f"[down] cleaning up relay on server...")
    ssh_exec(cfg, f"kill $(cat {remote_dir}/relay.pid 2>/dev/null) 2>/dev/null; rm -rf {remote_dir}")
    for proc in _SSH_PROCS:
        proc.terminate()
    print("[down] done")
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Gateway proxy (runs on C-side, forwards API requests to upstream)
# ═══════════════════════════════════════════════════════════════════════════════

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}

_GATEWAY_SERVER: ThreadingHTTPServer | None = None


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "claude-tunnel-gateway/0.1"

    def log_message(self, fmt, *args):
        print(f"  gateway: {fmt % args}", flush=True)

    def do_GET(self): self._forward()
    def do_POST(self): self._forward()
    def do_OPTIONS(self): self._forward()

    def _forward(self):
        gw_token = self.server.gw_token
        if gw_token:
            if self.headers.get("authorization") != f"Bearer {gw_token}":
                self._err(401, "unauthorized")
                return

        length = int(self.headers.get("content-length", "0") or "0")
        body = self.rfile.read(length) if length else None

        upstream = urlsplit(self.server.upstream_url)
        path = self.path if self.path.startswith("/") else "/" + self.path
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS | {"host", "authorization", "x-api-key", "content-length"}}
        headers["host"] = upstream.netloc
        headers["authorization"] = f"Bearer {self.server.upstream_token}"
        headers["x-api-key"] = self.server.upstream_token
        if body is not None:
            headers["content-length"] = str(len(body))

        conn_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        ctx = ssl.create_default_context() if upstream.scheme == "https" else None
        kw = {"timeout": 120}
        if ctx:
            kw["context"] = ctx
        conn = conn_cls(upstream.netloc, **kw)
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
        except Exception as exc:
            self._err(502, f"upstream error: {exc}")
            return
        finally:
            conn.close()

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_HEADERS | {"content-length"}:
                self.send_header(k, v)
        self.send_header("content-length", str(len(resp_body)))
        self.end_headers()
        if resp_body:
            self.wfile.write(resp_body)

    def _err(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_gateway(cfg: dict[str, Any]) -> None:
    global _GATEWAY_SERVER
    gw = cfg["gateway"]
    server = ThreadingHTTPServer((gw["host"], gw["port"]), GatewayHandler)
    server.gw_token = gw.get("token", "")
    server.upstream_url = gw["upstream_base_url"].rstrip("/")
    server.upstream_token = gw["upstream_auth_token"]
    _GATEWAY_SERVER = server
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[gateway] listening on {gw['host']}:{gw['port']} → {server.upstream_url}")


# ═══════════════════════════════════════════════════════════════════════════════
# Relay client (heartbeat)
# ═══════════════════════════════════════════════════════════════════════════════


def relay_post(cfg: dict[str, Any], action: str, role: str) -> dict[str, Any] | None:
    """Query the relay via SSH (relay listens on 127.0.0.1 on the server)."""
    relay_port = cfg["tunnel"]["relay_port"]
    body = json.dumps({
        "room": cfg["room"]["name"],
        "token": cfg["room"]["token"],
        "role": role,
        "action": action,
    })
    # Use curl on the server to avoid complex Python escaping
    escaped = body.replace("'", "'\\''")
    curl_cmd = f"curl -sf -X POST http://127.0.0.1:{relay_port}/api -H 'Content-Type: application/json' -d '{escaped}'"
    rc, stdout, stderr = ssh_exec(cfg, curl_cmd)
    if rc != 0 or not stdout.strip():
        return None
    try:
        return json.loads(stdout.strip())
    except Exception:
        return None


def start_heartbeat(cfg: dict[str, Any], role: str) -> None:
    def beat():
        while True:
            time.sleep(30)
            relay_post(cfg, "heartbeat", role)
    t = threading.Thread(target=beat, daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════════════════════════
# C-start: gateway + reverse tunnel
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_c_start(cfg: dict[str, Any]) -> int:
    gw = cfg["gateway"]

    # Fill upstream credentials from ~/.claude/settings.json if not in config
    if not gw.get("upstream_base_url") or not gw.get("upstream_auth_token"):
        claude_env = _load_claude_settings()
        if not gw.get("upstream_base_url"):
            gw["upstream_base_url"] = claude_env.get("ANTHROPIC_BASE_URL", "")
        if not gw.get("upstream_auth_token"):
            gw["upstream_auth_token"] = claude_env.get("ANTHROPIC_AUTH_TOKEN", "")

    if not gw.get("upstream_base_url") or not gw.get("upstream_auth_token"):
        print("[error] upstream API credentials not found.")
        print("  Set them in config or in ~/.claude/settings.json (env.ANTHROPIC_BASE_URL / env.ANTHROPIC_AUTH_TOKEN)")
        return 1

    print(f"[gateway] upstream: {gw['upstream_base_url']}")

    start_gateway(cfg)

    fwd_port = cfg["tunnel"]["forward_port"]
    mapping = f"127.0.0.1:{fwd_port}:{gw['host']}:{gw['port']}"
    print(f"[tunnel] SSH -R {mapping} → {cfg['server']['user']}@{cfg['server']['host']}")

    relay_post(cfg, "open", "c")
    start_heartbeat(cfg, "c")

    proc = ssh_tunnel_bg(cfg, "-R", mapping)
    print(f"[ok] C-side running. Ctrl+C to stop.")

    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        relay_post(cfg, "close", "c")
        proc.terminate()
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# A-start: local tunnel + Claude Code
# ═══════════════════════════════════════════════════════════════════════════════


def wait_for_port(host: str, port: int, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    return False


def _find_claude_js_entry() -> str | None:
    """Find the Claude Code JS entry point, bypassing the native binary."""
    # Look in npm global prefix
    npm_cmd = "npm.cmd" if IS_WINDOWS else "npm"
    if not shutil.which(npm_cmd):
        return None
    try:
        proc = subprocess.run([npm_cmd, "prefix", "-g"], capture_output=True, timeout=10)
        npm_prefix = proc.stdout.decode(errors="replace").strip()
    except Exception:
        return None
    if not npm_prefix:
        return None

    pkg_dir = Path(npm_prefix) / "node_modules" / "@anthropic-ai" / "claude-code"
    if not pkg_dir.exists():
        return None

    # Read package.json to find the real entry point
    pkg_json = pkg_dir / "package.json"
    if pkg_json.exists():
        try:
            with pkg_json.open("r", encoding="utf-8") as f:
                pkg = json.load(f)
            # Check for "main" or known JS entry files
            main = pkg.get("main", "")
            if main:
                entry = pkg_dir / main
                if entry.exists():
                    return str(entry)
        except Exception:
            pass

    # Try common entry point names
    for name in ("cli.js", "cli.mjs", "index.js", "dist/cli.js", "dist/index.js",
                 "src/cli.js", "bin/cli.js", "lib/cli.js"):
        entry = pkg_dir / name
        if entry.exists():
            return str(entry)

    return None


def _preflight_claude_cmd(cmd: list[str], env: dict[str, str]) -> bool:
    """Quick test if the claude command can actually execute (catches binary incompatibility)."""
    test_cmd = cmd[:1] + ["--version"] if "npx" not in cmd[0] else cmd[:3] + ["--version"]
    try:
        proc = subprocess.run(
            test_cmd, capture_output=True, timeout=15, env=env
        )
        output = (proc.stdout + proc.stderr).decode(errors="replace").lower()
        if "not compatible" in output or "is not recognized" in output:
            return False
        return proc.returncode == 0
    except (FileNotFoundError, OSError):
        return False
    except Exception:
        return True


def _check_wsl_claude() -> bool:
    """Check if Claude Code is available inside WSL."""
    if not IS_WINDOWS:
        return False
    if not shutil.which("wsl"):
        return False
    try:
        proc = subprocess.run(
            ["wsl", "--", "which", "claude"],
            capture_output=True, timeout=10
        )
        return proc.returncode == 0
    except Exception:
        return False


def _detect_claude_command(model: str) -> list[str]:
    """Auto-detect the best way to launch Claude Code on this platform."""
    system = platform.system()

    if system == "Windows":
        # On Windows, the native claude.exe may be incompatible.
        # Priority: JS entry > WSL > npx > claude.cmd
        js_entry = _find_claude_js_entry()
        if js_entry:
            return ["node", js_entry, "--model", model]
        if _check_wsl_claude():
            return ["wsl", "--", "claude", "--model", model]
        npx_cmd = "npx.cmd" if shutil.which("npx.cmd") else ("npx" if shutil.which("npx") else "")
        if npx_cmd:
            return [npx_cmd, "-y", "@anthropic-ai/claude-code", "--model", model]
        if shutil.which("claude.cmd"):
            return ["claude.cmd", "--model", model]
    elif system == "Darwin":
        if shutil.which("claude"):
            return ["claude", "--model", model]
        if shutil.which("npx"):
            return ["npx", "-y", "@anthropic-ai/claude-code", "--model", model]
    else:
        if shutil.which("claude"):
            return ["claude", "--model", model]
        if shutil.which("npx"):
            return ["npx", "-y", "@anthropic-ai/claude-code", "--model", model]

    return []


def _tunnel_only_mode(local_port: int, gw_token: str, model: str, proc: subprocess.Popen) -> int:
    """Keep tunnel alive and print instructions for manual connection."""
    try:
        from ct_ui import ui
        ui.tunnel_panel(local_port, gw_token, model,
                       is_windows=IS_WINDOWS, has_wsl=bool(shutil.which("wsl")))
    except ImportError:
        print("\n" + "=" * 60)
        print("  TUNNEL-ONLY MODE")
        print("=" * 60)
        if IS_WINDOWS:
            print(f'\n  PowerShell:')
            print(f'    $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:{local_port}"')
            print(f'    $env:ANTHROPIC_AUTH_TOKEN = "{gw_token}"')
            print(f'    claude --model {model}')
        else:
            print(f'\n  Bash/Zsh:')
            print(f'    export ANTHROPIC_BASE_URL=http://127.0.0.1:{local_port}')
            print(f'    export ANTHROPIC_AUTH_TOKEN={gw_token}')
            print(f'    claude --model {model}')
        print(f"\n  Press Ctrl+C to stop the tunnel.")
        print("=" * 60 + "\n")

    _stop_event.clear()
    old_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: _stop_event.set())

    try:
        while not _stop_event.is_set():
            if proc.poll() is not None:
                print("[!] SSH tunnel process exited unexpectedly.")
                return 1
            _stop_event.wait(timeout=1.0)
    finally:
        signal.signal(signal.SIGINT, old_handler)
        print("\n[down] stopping tunnel...")
        proc.terminate()

    return 0


def cmd_a_start(cfg: dict[str, Any]) -> int:
    local_port = cfg["claude"]["local_port"]
    fwd_port = cfg["tunnel"]["forward_port"]
    mapping = f"127.0.0.1:{local_port}:127.0.0.1:{fwd_port}"

    print(f"[tunnel] SSH -L {mapping} → {cfg['server']['user']}@{cfg['server']['host']}")

    relay_post(cfg, "heartbeat", "a")
    start_heartbeat(cfg, "a")

    proc = ssh_tunnel_bg(cfg, "-L", mapping)
    print(f"[tunnel] waiting for port {local_port}...")

    if not wait_for_port("127.0.0.1", local_port):
        print(f"[error] port {local_port} not available after 30s. SSH tunnel may have failed.")
        proc.terminate()
        return 1

    print(f"[ok] tunnel ready on localhost:{local_port}")

    gw_token = cfg["gateway"].get("token", "change-me")
    model = cfg["claude"].get("model", "claude-sonnet-4-6")
    project_dir = cfg["claude"].get("project_dir") or str(Path.cwd())

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{local_port}"
    env["ANTHROPIC_AUTH_TOKEN"] = gw_token

    # Use configured command, or auto-detect
    custom_cmd = cfg["claude"].get("command", "")
    if custom_cmd:
        if custom_cmd.strip().lower() == "tunnel-only":
            return _tunnel_only_mode(local_port, gw_token, model, proc)
        claude_cmd = shlex.split(custom_cmd) + ["--model", model]
    else:
        claude_cmd = _detect_claude_command(model)
        if not claude_cmd:
            print("[!] Claude Code not found, entering tunnel-only mode...")
            return _tunnel_only_mode(local_port, gw_token, model, proc)

    # Pre-flight check: verify the command actually works
    print(f"[claude] verifying command: {' '.join(claude_cmd[:3])}...")
    if not _preflight_claude_cmd(claude_cmd, env):
        print(f"[!] command failed pre-flight check: {claude_cmd[0]}")
        # Try fallback: node + JS entry
        if "node" not in claude_cmd[0]:
            js_entry = _find_claude_js_entry()
            if js_entry:
                claude_cmd = ["node", js_entry, "--model", model]
                print(f"[!] trying JS entry: node {js_entry}")
                if not _preflight_claude_cmd(claude_cmd, env):
                    js_entry = None
            if not js_entry:
                # Try WSL
                if IS_WINDOWS and _check_wsl_claude():
                    claude_cmd = ["wsl", "--", "claude", "--model", model]
                    print("[!] trying WSL claude...")
                    if not _preflight_claude_cmd(claude_cmd, env):
                        print("[!] WSL claude also failed, entering tunnel-only mode...")
                        return _tunnel_only_mode(local_port, gw_token, model, proc)
                else:
                    print("[!] No working Claude Code found, entering tunnel-only mode...")
                    return _tunnel_only_mode(local_port, gw_token, model, proc)

    print(f"[claude] starting in {project_dir} with model={model}")
    print(f"[claude] command: {' '.join(claude_cmd)}")
    print(f"[claude] ANTHROPIC_BASE_URL=http://127.0.0.1:{local_port}")

    try:
        result = subprocess.run(claude_cmd, cwd=project_dir, env=env)
    except FileNotFoundError:
        print(f"[error] command not found: {claude_cmd[0]}")
        print("[!] entering tunnel-only mode...")
        return _tunnel_only_mode(local_port, gw_token, model, proc)
    except OSError as e:
        print(f"[error] failed to launch: {e}")
        print("[!] entering tunnel-only mode...")
        return _tunnel_only_mode(local_port, gw_token, model, proc)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()

    return result.returncode if 'result' in dir() else 0


# ═══════════════════════════════════════════════════════════════════════════════
# Up: deploy + start (one command)
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_up(cfg: dict[str, Any]) -> int:
    role = cfg.get("role", "")
    if not role:
        role = ask("Role (a/c)", "c")
        cfg["role"] = role
        save_config(cfg)

    rc = cmd_deploy(cfg)
    if rc != 0:
        return rc

    time.sleep(1)

    if role == "c":
        return cmd_c_start(cfg)
    else:
        return cmd_a_start(cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# Status
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_status(cfg: dict[str, Any]) -> int:
    data = relay_post(cfg, "heartbeat", cfg.get("role", "a"))
    if data is None:
        print("[status] cannot reach relay. Is it deployed?")
        srv = cfg["server"]
        print(f"  server: {srv['user']}@{srv['host']}")
        print(f"  relay port: {cfg['tunnel']['relay_port']}")
        return 1
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Web UI (optional, local only)
# ═══════════════════════════════════════════════════════════════════════════════

WEB_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>claude-tunnel</title>
<style>
:root{font-family:system-ui,sans-serif;background:#f4f6f8;color:#20242a}
body{margin:0;min-height:100vh}
header{background:#15202b;color:#f5f8fb;padding:18px 22px;border-bottom:4px solid #0e7c7b}
header h1{margin:0;font-size:22px}
main{max-width:700px;margin:24px auto;padding:0 16px}
section{background:#fff;border:1px solid #d7dde4;border-radius:8px;padding:16px;margin-bottom:16px}
h2{margin:0 0 12px;font-size:17px}
.status{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:16px}
.pill{border:1px solid #d7dde4;border-radius:8px;padding:12px;text-align:center}
.pill strong{display:block;font-size:13px;color:#5d6874;margin-bottom:4px}
.ok{color:#08745f;font-weight:800} .bad{color:#9a3535;font-weight:800}
button{border:0;background:#0e7c7b;color:white;padding:10px 18px;border-radius:6px;font-weight:700;cursor:pointer;margin:4px}
button.danger{background:#a33a3a}
button:disabled{opacity:.5;cursor:wait}
pre{background:#111820;color:#edf4ff;padding:12px;border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere;min-height:60px}
@media(prefers-color-scheme:dark){:root{background:#15181c;color:#eceff3}section,.pill{background:#20252b;border-color:#3b444f}}
</style></head><body>
<header><h1>claude-tunnel</h1></header>
<main>
<div class="status">
  <div class="pill"><strong>Role</strong><span id="role">-</span></div>
  <div class="pill"><strong>Relay</strong><span id="relay">-</span></div>
  <div class="pill"><strong>Tunnel</strong><span id="tunnel">-</span></div>
</div>
<section>
  <h2>Controls</h2>
  <button id="btnUp" onclick="act('up')">Up (deploy + start)</button>
  <button id="btnDown" class="danger" onclick="act('down')">Down</button>
  <button onclick="act('status')">Status</button>
</section>
<section>
  <h2>Log</h2>
  <pre id="log">Ready.</pre>
</section>
</main>
<script>
async function act(cmd) {
  document.getElementById('log').textContent = 'Running ' + cmd + '...';
  try {
    const r = await fetch('/api/' + cmd, {method:'POST'});
    const d = await r.json();
    document.getElementById('log').textContent = JSON.stringify(d, null, 2);
    refresh();
  } catch(e) { document.getElementById('log').textContent = 'Error: ' + e.message; }
}
async function refresh() {
  try {
    const r = await fetch('/api/info');
    const d = await r.json();
    document.getElementById('role').textContent = d.role || '-';
    document.getElementById('relay').innerHTML = d.relay_ok ? '<span class="ok">online</span>' : '<span class="bad">offline</span>';
    document.getElementById('tunnel').innerHTML = d.tunnel_ok ? '<span class="ok">connected</span>' : '<span class="bad">disconnected</span>';
  } catch(e) {}
}
refresh(); setInterval(refresh, 3000);
</script></body></html>"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "claude-tunnel-web/0.1"

    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = WEB_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/info":
            cfg = load_config()
            relay_ok = relay_post(cfg, "heartbeat", cfg.get("role", "")) is not None
            tunnel_ok = False
            lp = cfg.get("claude", {}).get("local_port", 50000)
            try:
                s = socket.create_connection(("127.0.0.1", lp), timeout=1)
                s.close()
                tunnel_ok = True
            except Exception:
                pass
            self._json(200, {"role": cfg.get("role", ""), "relay_ok": relay_ok, "tunnel_ok": tunnel_ok})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        cfg = ensure_config()
        path = self.path
        if path == "/api/up":
            rc = cmd_up(cfg)
            self._json(200, {"ok": rc == 0, "returncode": rc})
        elif path == "/api/down":
            rc = cmd_down(cfg)
            self._json(200, {"ok": rc == 0})
        elif path == "/api/status":
            data = relay_post(cfg, "heartbeat", cfg.get("role", ""))
            self._json(200, data or {"error": "relay unreachable"})
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def cmd_web(cfg: dict[str, Any], port: int = 8765) -> int:
    """Start web management panel — uses ct_web if available."""
    try:
        from ct_web import cmd_web as _web
        return _web(port)
    except ImportError:
        server = None
        actual_port = port
        for p in range(port, port + 10):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", p), WebHandler)
                actual_port = p
                break
            except OSError:
                continue
        if server is None:
            print(f"[error] Cannot bind to ports {port}-{port+9}")
            return 1
        print(f"[web] http://127.0.0.1:{actual_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Environment check
# ═══════════════════════════════════════════════════════════════════════════════

IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


def _get_python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _get_ssh_version() -> tuple[str, tuple[int, int]]:
    """Return (version_string, (major, minor)) for the system SSH."""
    import re
    try:
        proc = subprocess.run(["ssh", "-V"], capture_output=True, timeout=5)
        ver_str = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    except Exception:
        return ("not found", (0, 0))
    m = re.search(r"OpenSSH_(\d+)\.(\d+)", ver_str)
    if m:
        return (ver_str, (int(m.group(1)), int(m.group(2))))
    return (ver_str or "unknown", (0, 0))


def _check_node() -> tuple[bool, str]:
    """Check if Node.js is available and return version."""
    try:
        proc = subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        if proc.returncode == 0:
            return (True, proc.stdout.decode(errors="replace").strip())
    except Exception:
        pass
    return (False, "")


def _check_npx() -> bool:
    return shutil.which("npx") is not None


def _check_claude_code() -> tuple[bool, str, str]:
    """Check Claude Code availability. Returns (available, method, detail)."""
    system = platform.system()

    if system == "Windows":
        # Check for JS entry (most reliable on Windows)
        js_entry = _find_claude_js_entry()
        if js_entry:
            return (True, "node", f"node {Path(js_entry).name} (JS entry)")
        if shutil.which("npx.cmd") or shutil.which("npx"):
            return (True, "npx", "npx @anthropic-ai/claude-code (binary may be incompatible)")
        if shutil.which("claude.cmd"):
            return (True, "native", "claude.cmd (binary may be incompatible)")
    elif system == "Darwin":
        if shutil.which("claude"):
            return (True, "native", shutil.which("claude"))
        if shutil.which("npx"):
            return (True, "npx", "npx @anthropic-ai/claude-code")
    else:
        if shutil.which("claude"):
            return (True, "native", shutil.which("claude"))
        if shutil.which("npx"):
            return (True, "npx", "npx @anthropic-ai/claude-code")

    return (False, "", "")


def _check_ssh_tool() -> tuple[bool, str]:
    """Check SSH client availability."""
    if IS_WINDOWS:
        if _has_plink():
            return (True, "plink (PuTTY)")
        if shutil.which("ssh"):
            return (True, "ssh (Windows OpenSSH)")
    else:
        if shutil.which("ssh"):
            return (True, "ssh")
    return (False, "")


def _check_scp_tool() -> tuple[bool, str]:
    """Check SCP/file transfer availability."""
    if HAS_PARAMIKO:
        return (True, "paramiko SFTP")
    if IS_WINDOWS and shutil.which("pscp"):
        return (True, "pscp (PuTTY)")
    if shutil.which("scp"):
        return (True, "scp")
    return (False, "")


def cmd_check_env(role: str = "") -> int:
    """Run full environment dependency check and print results."""
    try:
        from ct_ui import ui
        has_ui = True
    except ImportError:
        has_ui = False

    system = platform.system()
    checks: list[tuple[str, str, str]] = []
    all_ok = True
    warnings: list[str] = []

    if has_ui:
        ui.banner()
        ui.rule("Environment Check")
        ui.info(f"Platform: {system} ({platform.machine()})  Python: {_get_python_version()}")
    else:
        print(f"\n  === Environment Check ===")
        print(f"  Platform: {system} ({platform.machine()})  Python: {_get_python_version()}\n")

    # --- SSH ---
    ssh_ok, ssh_tool = _check_ssh_tool()
    checks.append(("SSH Client", "ok" if ssh_ok else "fail", ssh_tool or "NOT FOUND"))
    if not ssh_ok:
        all_ok = False
        warnings.append("Install OpenSSH or PuTTY for SSH access")

    ssh_ver_str, ssh_ver = _get_ssh_version()
    if ssh_ver != (0, 0):
        ver_display = ssh_ver_str.split(",")[0] if "," in ssh_ver_str else ssh_ver_str
        checks.append(("SSH Version", "ok", ver_display))
        if ssh_ver < (8, 4) and not HAS_PARAMIKO:
            warnings.append(f"OpenSSH {ssh_ver[0]}.{ssh_ver[1]} < 8.4: pip install paramiko for password auth")

    # --- SCP ---
    scp_ok, scp_tool = _check_scp_tool()
    checks.append(("File Transfer", "ok" if scp_ok else "fail", scp_tool or "NOT FOUND"))
    if not scp_ok:
        all_ok = False
        warnings.append("Install scp or: pip install paramiko")

    # --- paramiko ---
    checks.append(("paramiko", "ok" if HAS_PARAMIKO else "warn",
                   "installed" if HAS_PARAMIKO else "not installed"))
    if not HAS_PARAMIKO and IS_WINDOWS:
        warnings.append("pip install paramiko (recommended for Windows)")

    # --- Node.js ---
    node_ok, node_ver = _check_node()
    checks.append(("Node.js", "ok" if node_ok else "fail", node_ver or "NOT FOUND"))
    if not node_ok:
        all_ok = False
        warnings.append("Install Node.js: https://nodejs.org/")

    # --- npx ---
    npx_ok = _check_npx()
    checks.append(("npx", "ok" if npx_ok else "warn", "available" if npx_ok else "not found"))

    # --- Claude Code ---
    if role != "c":
        claude_ok, _, claude_detail = _check_claude_code()
        checks.append(("Claude Code", "ok" if claude_ok else "fail", claude_detail or "NOT FOUND"))
        if not claude_ok:
            all_ok = False
            warnings.append("npm install -g @anthropic-ai/claude-code")

    # --- WSL ---
    if IS_WINDOWS and role != "c":
        wsl_claude = _check_wsl_claude()
        if wsl_claude:
            checks.append(("WSL Claude", "ok", "available (fallback)"))
        elif shutil.which("wsl"):
            checks.append(("WSL Claude", "warn", "WSL exists, claude not installed"))
        else:
            checks.append(("WSL", "warn", "not available"))

        if not _find_claude_js_entry() and not wsl_claude:
            warnings.append("If binary fails: wsl -- npm install -g @anthropic-ai/claude-code")

    # Display
    if has_ui:
        ui.env_table(checks)
        ui.warnings_panel(warnings)
        if all_ok and not warnings:
            ui.success("All checks passed.")
        elif all_ok:
            ui.success("Core dependencies OK.")
        else:
            ui.error("Some required dependencies are missing.")
    else:
        for name, status, detail in checks:
            icon = {"ok": "[ok]", "warn": "[--]", "fail": "[X] "}.get(status, "[??]")
            print(f"  {icon} {name:16s} {detail}")
        if warnings:
            print("\n  Warnings:")
            for w in warnings:
                print(f"    [!] {w}")
        print()
        if all_ok:
            print("  All checks passed." if not warnings else "  Core dependencies OK.")
        else:
            print("  Some required dependencies are missing.")
        print()

    return 0 if all_ok else 1




def _cleanup():
    for proc in _SSH_PROCS:
        try:
            proc.terminate()
        except Exception:
            pass
    for path in _ASKPASS_FILES:
        try:
            os.unlink(path)
        except Exception:
            pass


atexit.register(_cleanup)
if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    global CONFIG_PATH

    parser = argparse.ArgumentParser(
        prog="claude-tunnel",
        description="One-command Claude Code remote tunnel tool.",
    )
    parser.add_argument("command", nargs="?", default="up",
                        choices=["init", "check", "deploy", "c-start", "a-start", "up", "down", "status", "web"],
                        help="Subcommand (default: up)")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Config file path")
    parser.add_argument("--web-port", type=int, default=8765, help="Web UI port")
    parser.add_argument("--skip-check", action="store_true", help="Skip environment check")
    args = parser.parse_args()

    CONFIG_PATH = args.config

    # Run environment check on init/up/check
    if args.command in ("init", "check", "up") and not args.skip_check:
        # Determine role from existing config if available
        existing_cfg = load_config()
        role = existing_cfg.get("role", "")

        rc = cmd_check_env(role)
        if args.command == "check":
            return rc
        if rc != 0:
            answer = input("  Continue anyway? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                return 1
            print()

    if args.command == "init":
        cmd_init_interactive()
        return 0

    cfg = ensure_config()

    if args.command == "deploy":
        return cmd_deploy(cfg)
    elif args.command == "c-start":
        return cmd_c_start(cfg)
    elif args.command == "a-start":
        return cmd_a_start(cfg)
    elif args.command == "up":
        return cmd_up(cfg)
    elif args.command == "down":
        return cmd_down(cfg)
    elif args.command == "status":
        return cmd_status(cfg)
    elif args.command == "web":
        return cmd_web(cfg, args.web_port)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(130)
