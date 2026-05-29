"""Interactive init wizard with back-navigation and validation for claude-tunnel."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ct_ui import ui

IS_WINDOWS = platform.system() == "Windows"


# ═══════════════════════════════════════════════════════════════════════════════
# Validators
# ═══════════════════════════════════════════════════════════════════════════════

def validate_host(value: str) -> Tuple[bool, str]:
    if not value.strip():
        return False, "Host cannot be empty"
    v = value.strip()
    ip_pattern = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
    if ip_pattern.match(v):
        parts = v.split(".")
        for p in parts:
            if int(p) > 255:
                return False, f"Invalid IP: octet {p} > 255"
        return True, ""
    host_pattern = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*$")
    if host_pattern.match(v) and len(v) <= 253:
        return True, ""
    return False, "Invalid hostname or IP address"


def validate_port(value: str) -> Tuple[bool, str]:
    try:
        p = int(value)
        if 1 <= p <= 65535:
            return True, ""
        return False, "Port must be 1-65535"
    except ValueError:
        return False, "Port must be a number"


def validate_nonempty(value: str) -> Tuple[bool, str]:
    if value.strip():
        return True, ""
    return False, "This field cannot be empty"


def validate_optional(value: str) -> Tuple[bool, str]:
    return True, ""


def validate_path_if_set(value: str) -> Tuple[bool, str]:
    if not value.strip():
        return True, ""
    p = Path(value.strip())
    if p.exists():
        return True, ""
    return False, f"File not found: {value}"


def validate_choice(choices: List[str]) -> Callable[[str], Tuple[bool, str]]:
    def _validate(value: str) -> Tuple[bool, str]:
        if value.strip() in choices:
            return True, ""
        return False, f"Must be one of: {', '.join(choices)}"
    return _validate


def _detect_local_oauth_token() -> Optional[str]:
    """Try to detect an existing OAuth token from the local Claude Code installation."""
    # Check CLAUDE_CODE_OAUTH_TOKEN env var
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if env_token:
        return env_token

    # Try reading ~/.claude/.credentials.json (Linux/Windows)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if config_dir:
        cred_path = Path(config_dir) / ".credentials.json"
    else:
        cred_path = Path.home() / ".claude" / ".credentials.json"

    if cred_path.exists():
        try:
            with cred_path.open("r", encoding="utf-8") as f:
                creds = json.load(f)
            token = creds.get("oauth_token") or creds.get("accessToken") or creds.get("token")
            if token:
                return token
        except Exception:
            pass

    # Try running 'claude auth status' to check if logged in
    claude_bin = shutil.which("claude")
    if claude_bin:
        try:
            result = subprocess.run(
                [claude_bin, "auth", "status"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data.get("loggedIn") and data.get("authMethod") == "oauth_token":
                    return "__OAUTH_LOGGED_IN__"
        except Exception:
            pass

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Step definitions
# ═══════════════════════════════════════════════════════════════════════════════

class Step:
    def __init__(self, key: str, prompt: str, default: str = "",
                 validate: Optional[Callable] = None, secret: bool = False,
                 section: str = "", condition: Optional[Callable] = None,
                 hint: str = "", choices: Optional[List[Tuple[str, str]]] = None):
        self.key = key
        self.prompt = prompt
        self.default = default
        self.validate = validate or validate_optional
        self.secret = secret
        self.section = section
        self.condition = condition
        self.hint = hint
        self.choices = choices


def _load_claude_settings() -> Dict[str, str]:
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return {}
    try:
        with settings_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("env", {})
    except Exception:
        return {}


ALL_STEPS = [
    Step("role", "Role (a=local dev, c=model server)", "c",
         validate=validate_choice(["a", "c"]),
         section="Basic", hint="a = A-side (dev machine), c = C-side (model server)"),

    # Server section
    Step("server.host", "Server host/IP", "",
         validate=validate_host, section="Public VPS (relay server)"),
    Step("server.port", "SSH port", "22", validate=validate_port),
    Step("server.user", "SSH user", "root", validate=validate_nonempty),
    Step("server.password", "SSH password (empty if using key)", "",
         validate=validate_optional, secret=True),
    Step("server.key_file", "SSH key file path (empty if using password)", "",
         validate=validate_path_if_set),

    # Tunnel section
    Step("tunnel.relay_port", "Relay HTTP port on server", "8088",
         validate=validate_port, section="Tunnel"),
    Step("tunnel.forward_port", "Forward port on server", "19001",
         validate=validate_port),
    Step("room.name", "Room name", "default", validate=validate_nonempty),
    Step("room.token", "Room token (shared secret)", "change-me"),

    # C-side gateway
    Step("gateway.port", "Local gateway port", "8787",
         validate=validate_port, section="Gateway (C-side)",
         condition=lambda v: v.get("role") == "c"),
    Step("gateway.token", "Gateway auth token", "change-me",
         condition=lambda v: v.get("role") == "c"),
    Step("gateway.auth_type", "Select authentication type:", "api_key",
         validate=validate_choice(["api_key", "oauth_token"]),
         condition=lambda v: v.get("role") == "c",
         choices=[("api_key", "API Key (sk-ant-xxx)"),
                  ("oauth_token", "OAuth Token (from 'claude setup-token', for Pro/Max subscribers)")]),
    Step("gateway.upstream_base_url", "Upstream API base URL", "https://api.anthropic.com",
         validate=validate_nonempty,
         condition=lambda v: v.get("role") == "c"),
    Step("gateway.upstream_auth_token", "Upstream API key/token", "",
         validate=validate_optional, secret=True,
         condition=lambda v: v.get("role") == "c",
         hint="Leave empty to use ~/.claude/settings.json"),

    # A-side claude
    Step("claude.local_port", "Local port for Claude", "50000",
         validate=validate_port, section="Claude Code (A-side)",
         condition=lambda v: v.get("role") == "a"),
    Step("claude.model", "Model", "claude-sonnet-4-6",
         condition=lambda v: v.get("role") == "a"),
    Step("claude.project_dir", "Project directory", str(Path.cwd()),
         condition=lambda v: v.get("role") == "a"),
    Step("claude.command", "Claude command (empty=auto-detect)", "",
         validate=validate_optional,
         condition=lambda v: v.get("role") == "a",
         hint="e.g. 'tunnel-only' or 'wsl -- claude'"),
    Step("gateway.token", "Gateway auth token (same as C-side)", "change-me",
         condition=lambda v: v.get("role") == "a"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Interactive wizard
# ═══════════════════════════════════════════════════════════════════════════════

def _prompt_choices(step: Step, step_num: int, total: int) -> Optional[str]:
    """Display numbered choice menu. Returns selected value or None for back."""
    if hasattr(ui, 'console') and ui.console:
        ui.console.print(f"  [bold cyan][{step_num}/{total}][/] {step.prompt}")
        for i, (value, label) in enumerate(step.choices, 1):
            marker = " *" if value == step.default else ""
            ui.console.print(f"    [bold]{i}[/]. {label}{marker}")
        ui.console.print(f"  [dim]Enter number (1-{len(step.choices)}), or 'b' to go back[/]")
    else:
        print(f"  [{step_num}/{total}] {step.prompt}")
        for i, (value, label) in enumerate(step.choices, 1):
            marker = " *" if value == step.default else ""
            print(f"    {i}. {label}{marker}")
        print(f"  Enter number (1-{len(step.choices)}), or 'b' to go back")

    max_retries = 5
    for attempt in range(max_retries):
        try:
            if hasattr(ui, 'console') and ui.console:
                from rich.prompt import Prompt
                raw = Prompt.ask(f"  [bold cyan]>[/]", default=None)
            else:
                raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if not raw:
            if step.default:
                return step.default
            continue

        if raw.lower() in ("b", "<", "back"):
            return None

        try:
            idx = int(raw)
            if 1 <= idx <= len(step.choices):
                return step.choices[idx - 1][0]
        except ValueError:
            pass

        ui.error(f"Please enter a number between 1 and {len(step.choices)}")
        if attempt >= max_retries - 1:
            ui.warn("Too many invalid attempts, going back.")
            return None

    return None


def _prompt_one(step: Step, step_num: int, total: int, values: Dict[str, str]) -> Optional[str]:
    """Prompt for one step. Returns value or None if user wants to go back."""
    # Section header
    if step.section:
        ui.rule(step.section)

    # Show hint
    if step.hint:
        if hasattr(ui, 'console') and ui.console:
            ui.console.print(f"  [dim italic]{step.hint}[/]")
        else:
            print(f"        {step.hint}")

    # Numbered choice menu
    if step.choices:
        return _prompt_choices(step, step_num, total)

    # Retry loop for validation (not recursive)
    max_retries = 5
    for attempt in range(max_retries):
        try:
            if hasattr(ui, 'console') and ui.console:
                from rich.prompt import Prompt
                display_default = step.default
                if display_default == "__LOCAL__":
                    display_default = "auto (local)"
                elif display_default == "__OAUTH_LOCAL__":
                    display_default = "auto (detected OAuth token)"
                elif display_default == "__OAUTH_GENERATE__":
                    display_default = "auto (run setup-token)"
                default_val = display_default if display_default else None
                raw = Prompt.ask(
                    f"  [bold cyan][{step_num}/{total}][/] {step.prompt}",
                    default=default_val,
                    password=step.secret
                )
                value = raw.strip() if raw else ""
                if value in ("auto (local)", "auto (detected OAuth token)", "auto (run setup-token)"):
                    value = step.default
            else:
                display_default = step.default
                if display_default == "__LOCAL__":
                    display_default = "auto (local)"
                elif display_default == "__OAUTH_LOCAL__":
                    display_default = "auto (detected OAuth token)"
                elif display_default == "__OAUTH_GENERATE__":
                    display_default = "auto (run setup-token)"
                default_display = "***" if step.secret and display_default else display_default
                suffix = f" [{default_display}]" if display_default else ""
                value = input(f"  [{step_num}/{total}] {step.prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        # Check for back command
        if value.lower() in ("b", "<", "back"):
            return None

        # Use default if empty
        if not value and step.default:
            value = step.default

        # Validate
        ok, err_msg = step.validate(value)
        if ok:
            return value

        ui.error(err_msg)
        if attempt >= max_retries - 1:
            ui.warn("Too many invalid attempts, going back.")
            return None

    return None


def _get_active_steps(values: Dict[str, str]) -> List[Step]:
    """Filter steps based on conditions (role-dependent steps)."""
    active = []
    for step in ALL_STEPS:
        if step.condition is None or step.condition(values):
            active.append(step)
    return active


def _set_nested(cfg: Dict[str, Any], key: str, value: str) -> None:
    """Set a nested config value like 'server.host'."""
    parts = key.split(".")
    d = cfg
    for p in parts[:-1]:
        if p not in d:
            d[p] = {}
        d = d[p]
    # Convert port values to int
    if "port" in parts[-1]:
        try:
            value = int(value)
        except (ValueError, TypeError):
            pass
    d[parts[-1]] = value


def cmd_init_interactive() -> Dict[str, Any]:
    """Run the interactive init wizard with back-navigation."""
    ui.banner()
    ui.rule("Configuration Wizard")

    if hasattr(ui, 'console') and ui.console:
        ui.console.print("  [dim]Type 'b' or '<' to go back to the previous step[/]")
        ui.console.print()

    values: Dict[str, str] = {}
    current = 0
    last_section = ""

    while True:
        # Get active steps based on current values
        active_steps = _get_active_steps(values)

        if current >= len(active_steps):
            break
        if current < 0:
            current = 0

        step = active_steps[current]
        total = len(active_steps)

        # Auto-fill defaults from claude settings for C-side
        if step.key == "gateway.upstream_base_url":
            env = _load_claude_settings()
            local_url = env.get("ANTHROPIC_BASE_URL", "")
            if local_url and not values.get("_upstream_url_set"):
                if hasattr(ui, 'console') and ui.console:
                    ui.console.print(f"  [dim]Detected in ~/.claude/settings.json:[/] [cyan]{local_url}[/]")
                    ui.console.print(f"  [dim]Press Enter to use it, or type a custom URL[/]")
                else:
                    print(f"        Detected: {local_url}")
                    print(f"        Press Enter to use it, or type a custom URL")
                step.default = local_url
            elif not step.default:
                step.default = "https://api.anthropic.com"

        if step.key == "gateway.upstream_auth_token":
            is_oauth = values.get("gateway.auth_type") == "oauth_token"
            if is_oauth:
                detected_token = _detect_local_oauth_token()
                if detected_token and detected_token != "__OAUTH_LOGGED_IN__":
                    masked = detected_token[:12] + "..." if len(detected_token) > 12 else "***"
                    if hasattr(ui, 'console') and ui.console:
                        ui.console.print(f"  [dim]Detected local OAuth token:[/] [cyan]{masked}[/]")
                        ui.console.print(f"  [dim]Press Enter to use it, or paste a different token[/]")
                    else:
                        print(f"        Detected local OAuth token: {masked}")
                        print(f"        Press Enter to use it, or paste a different token")
                    step.default = "__OAUTH_LOCAL__"
                    step._detected_oauth = detected_token
                elif detected_token == "__OAUTH_LOGGED_IN__":
                    if hasattr(ui, 'console') and ui.console:
                        ui.console.print(f"  [bold green]✓[/] [dim]Claude Code is logged in via OAuth on this machine.[/]")
                        ui.console.print(f"  [dim]Run [bold]claude setup-token[/bold] to generate a shareable token, then paste it below.[/]")
                        ui.console.print(f"  [dim]Or press Enter to run it automatically.[/]")
                    else:
                        print("        ✓ Claude Code is logged in via OAuth on this machine.")
                        print("        Run 'claude setup-token' to generate a shareable token, then paste it below.")
                        print("        Or press Enter to run it automatically.")
                    step.default = "__OAUTH_GENERATE__"
                else:
                    if hasattr(ui, 'console') and ui.console:
                        ui.console.print(f"  [dim]Run [bold]claude setup-token[/bold] on this machine to get a long-lived OAuth token.[/]")
                        ui.console.print(f"  [dim]Paste the token below:[/]")
                    else:
                        print("        Run 'claude setup-token' to get a long-lived OAuth token.")
                        print("        Paste the token below:")
            else:
                env = _load_claude_settings()
                local_token = env.get("ANTHROPIC_AUTH_TOKEN", "")
                if local_token and not values.get("_upstream_token_set"):
                    masked = local_token[:8] + "..." if len(local_token) > 8 else "***"
                    if hasattr(ui, 'console') and ui.console:
                        ui.console.print(f"  [dim]Detected in ~/.claude/settings.json:[/] [cyan]{masked}[/]")
                        ui.console.print(f"  [dim]Press Enter to use it, or type a custom key[/]")
                    else:
                        print(f"        Detected: {masked}")
                        print(f"        Press Enter to use it, or type a custom key")
                    step.default = "__LOCAL__"

        result = _prompt_one(step, current + 1, total, values)

        if result is None:
            if current == 0:
                ui.warn("Configuration cancelled.")
                return None
            if hasattr(ui, 'console') and ui.console:
                ui.console.print(f"  [dim]← back[/]")
                ui.console.print()
            else:
                print("  ← back\n")
            current -= 1
            continue

        values[step.key] = result
        current += 1

    # Build config dict
    from claude_tunnel import DEFAULT_CONFIG
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["role"] = values.get("role", "c")

    for key, value in values.items():
        _set_nested(cfg, key, value)

    # Handle special case: upstream_auth_token from local settings
    if cfg.get("gateway", {}).get("upstream_auth_token") == "__LOCAL__":
        env = _load_claude_settings()
        cfg["gateway"]["upstream_auth_token"] = env.get("ANTHROPIC_AUTH_TOKEN", "")

    # Handle OAuth token special values
    gw_token = cfg.get("gateway", {}).get("upstream_auth_token", "")
    if gw_token == "__OAUTH_LOCAL__":
        detected = _detect_local_oauth_token()
        if detected and detected != "__OAUTH_LOGGED_IN__":
            cfg["gateway"]["upstream_auth_token"] = detected
        else:
            cfg["gateway"]["upstream_auth_token"] = ""
    elif gw_token == "__OAUTH_GENERATE__":
        cfg["gateway"]["upstream_auth_token"] = _run_setup_token()

    return cfg


def _run_setup_token() -> str:
    """Run 'claude setup-token' and capture the generated token."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        ui.error("Claude Code CLI not found. Please run 'claude setup-token' manually.")
        return ""

    if hasattr(ui, 'console') and ui.console:
        ui.console.print(f"\n  [bold]Running 'claude setup-token'...[/]")
        ui.console.print(f"  [dim]This will open a browser for OAuth authorization.[/]\n")
    else:
        print("\n  Running 'claude setup-token'...")
        print("  This will open a browser for OAuth authorization.\n")

    try:
        result = subprocess.run(
            [claude_bin, "setup-token"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and result.stdout.strip():
            token = result.stdout.strip()
            masked = token[:12] + "..." if len(token) > 12 else "***"
            if hasattr(ui, 'console') and ui.console:
                ui.console.print(f"  [bold green]✓[/] Token generated: [cyan]{masked}[/]")
            else:
                print(f"  ✓ Token generated: {masked}")
            return token
        else:
            ui.error(f"setup-token failed: {result.stderr.strip() or 'no output'}")
            return ""
    except subprocess.TimeoutExpired:
        ui.error("setup-token timed out (120s). Please run it manually.")
        return ""
    except Exception as e:
        ui.error(f"Failed to run setup-token: {e}")
        return ""
