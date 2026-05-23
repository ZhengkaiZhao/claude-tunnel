#!/usr/bin/env python3
"""claude-tunnel: One-command Claude Code remote tunnel tool.

Usage:
    python3 claude_tunnel.py init        # Interactive setup
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
import textwrap
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__version__ = "0.1.0"

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
    "claude": {"local_port": 50000, "model": "claude-sonnet-4-6", "project_dir": ""},
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


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or default


def cmd_init_interactive() -> dict[str, Any]:
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
        cfg["gateway"]["port"] = int(ask("Local gateway port", "8787"))
        cfg["gateway"]["token"] = ask("Gateway auth token", "change-me")
        cfg["gateway"]["upstream_base_url"] = ask("Upstream API base URL", "https://api.anthropic.com")
        cfg["gateway"]["upstream_auth_token"] = ask("Upstream API key/token")
    else:
        print("\n  --- Claude Code (A-side) ---")
        cfg["claude"]["local_port"] = int(ask("Local port for Claude", "50000"))
        cfg["claude"]["model"] = ask("Model", "claude-sonnet-4-6")
        cfg["claude"]["project_dir"] = ask("Project directory", str(Path.cwd()))
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


def ssh_tunnel_bg(cfg: dict[str, Any], tunnel_flag: str, mapping: str) -> subprocess.Popen:
    password = cfg["server"].get("password", "")
    if IS_WINDOWS and _has_plink():
        args = _plink_base_args(cfg)
        # plink uses -L/-R differently: insert before host
        args_insert = ["-N", tunnel_flag, mapping]
        args = args[:-1] + args_insert + [args[-1]]
    else:
        args = _ssh_base_args(cfg)
        args_insert = ["-o", "ExitOnForwardFailure=yes", "-N", tunnel_flag, mapping]
        args = args[:1] + args_insert + args[1:]
    env = (_ssh_env(password) if password else None) if not IS_WINDOWS else None
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
    if not gw.get("upstream_base_url") or not gw.get("upstream_auth_token"):
        print("[error] gateway.upstream_base_url and upstream_auth_token required in config")
        return 1

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

    if platform.system() == "Windows":
        claude_cmd = ["claude.cmd", "--model", model]
    else:
        claude_cmd = ["claude", "--model", model]

    print(f"[claude] starting in {project_dir} with model={model}")
    print(f"[claude] ANTHROPIC_BASE_URL=http://127.0.0.1:{local_port}")

    try:
        result = subprocess.run(claude_cmd, cwd=project_dir, env=env)
    except FileNotFoundError:
        print("[error] 'claude' command not found. Is Claude Code installed?")
        proc.terminate()
        return 1
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
    print(f"[web] http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), WebHandler).serve_forever()
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════════════════════════════


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
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    global CONFIG_PATH
    # On Windows with password auth, paramiko is required for reliable SSH
    if IS_WINDOWS and not HAS_PARAMIKO:
        print("[!] Windows detected: install paramiko for password-based SSH support:")
        print("    pip install paramiko")
        print("    (SSH key auth works without paramiko)")
        print()

    parser = argparse.ArgumentParser(
        prog="claude-tunnel",
        description="One-command Claude Code remote tunnel tool.",
    )
    parser.add_argument("command", nargs="?", default="up",
                        choices=["init", "deploy", "c-start", "a-start", "up", "down", "status", "web"],
                        help="Subcommand (default: up)")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Config file path")
    parser.add_argument("--web-port", type=int, default=8765, help="Web UI port")
    args = parser.parse_args()

    CONFIG_PATH = args.config

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
    sys.exit(main())
