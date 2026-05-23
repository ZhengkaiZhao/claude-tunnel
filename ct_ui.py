"""Rich terminal UI wrapper for claude-tunnel."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Generator, List, Optional, Tuple

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.status import Status
    from rich.rule import Rule
    from rich.columns import Columns
    from rich.markup import escape
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class UI:
    def __init__(self):
        if HAS_RICH:
            self.console = Console()
        else:
            self.console = None

    def banner(self) -> None:
        if not HAS_RICH:
            print("\n  claude-tunnel v0.1.0\n")
            return
        banner_text = Text()
        banner_text.append("claude-tunnel", style="bold cyan")
        banner_text.append(" v0.1.0", style="dim")
        panel = Panel(
            banner_text,
            border_style="cyan",
            padding=(0, 2),
        )
        self.console.print(panel)

    def step(self, tag: str, msg: str) -> None:
        if not HAS_RICH:
            print(f"[{tag}] {msg}")
            return
        self.console.print(f"  [bold blue][{tag}][/] {escape(msg)}")

    def success(self, msg: str) -> None:
        if not HAS_RICH:
            print(f"[ok] {msg}")
            return
        self.console.print(f"  [bold green][ok][/] {escape(msg)}")

    def warn(self, msg: str) -> None:
        if not HAS_RICH:
            print(f"[!] {msg}")
            return
        self.console.print(f"  [bold yellow][!][/]  {escape(msg)}")

    def error(self, msg: str) -> None:
        if not HAS_RICH:
            print(f"[X] {msg}")
            return
        self.console.print(f"  [bold red][X][/]  {escape(msg)}")

    def info(self, msg: str) -> None:
        if not HAS_RICH:
            print(f"    {msg}")
            return
        self.console.print(f"      {escape(msg)}", style="dim")

    def rule(self, title: str = "") -> None:
        if not HAS_RICH:
            print(f"\n{'─' * 50} {title}")
            return
        self.console.print(Rule(title, style="dim"))

    @contextmanager
    def spinner(self, msg: str) -> Generator[None, None, None]:
        if not HAS_RICH:
            print(f"  ... {msg}")
            yield
            return
        with self.console.status(f"  {msg}", spinner="dots") as status:
            yield

    def env_table(self, checks: List[Tuple[str, str, str]]) -> None:
        """Display environment check results as a table.
        checks: list of (name, status, detail) where status is 'ok'/'warn'/'fail'
        """
        if not HAS_RICH:
            for name, status, detail in checks:
                icon = {"ok": "[ok]", "warn": "[--]", "fail": "[X] "}.get(status, "[??]")
                print(f"  {icon} {name:16s} {detail}")
            return

        table = Table(show_header=True, header_style="bold", border_style="dim",
                      padding=(0, 1), show_edge=False)
        table.add_column("", width=3)
        table.add_column("Component", style="bold", min_width=14)
        table.add_column("Status")

        for name, status, detail in checks:
            if status == "ok":
                icon = "[green]●[/]"
                detail_style = "green"
            elif status == "warn":
                icon = "[yellow]●[/]"
                detail_style = "yellow"
            else:
                icon = "[red]●[/]"
                detail_style = "red"
            table.add_row(icon, name, f"[{detail_style}]{escape(detail)}[/]")

        self.console.print()
        self.console.print(table)
        self.console.print()

    def tunnel_panel(self, local_port: int, gw_token: str, model: str,
                     is_windows: bool = False, has_wsl: bool = False) -> None:
        """Display tunnel-only mode connection instructions in a rich panel."""
        lines = []

        if is_windows:
            lines.append("[bold cyan]PowerShell:[/]")
            lines.append(f'  [green]$env:ANTHROPIC_BASE_URL[/] = [yellow]"http://127.0.0.1:{local_port}"[/]')
            lines.append(f'  [green]$env:ANTHROPIC_AUTH_TOKEN[/] = [yellow]"{gw_token}"[/]')
            lines.append(f"  claude --model {model}")
            lines.append("")
            lines.append("[bold cyan]CMD:[/]")
            lines.append(f"  [green]set[/] ANTHROPIC_BASE_URL=[yellow]http://127.0.0.1:{local_port}[/]")
            lines.append(f"  [green]set[/] ANTHROPIC_AUTH_TOKEN=[yellow]{gw_token}[/]")
            lines.append(f"  claude --model {model}")
        else:
            lines.append("[bold cyan]Bash / Zsh:[/]")
            lines.append(f"  [green]export[/] ANTHROPIC_BASE_URL=[yellow]http://127.0.0.1:{local_port}[/]")
            lines.append(f"  [green]export[/] ANTHROPIC_AUTH_TOKEN=[yellow]{gw_token}[/]")
            lines.append(f"  claude --model {model}")

        if is_windows and has_wsl:
            lines.append("")
            lines.append("[bold cyan]WSL (recommended):[/]")
            lines.append("  [green]wsl[/]")
            host_ip = "$(ip route show default | awk '{print $3}')"
            lines.append(f"  [green]export[/] ANTHROPIC_BASE_URL=[yellow]http://{host_ip}:{local_port}[/]")
            lines.append(f"  [green]export[/] ANTHROPIC_AUTH_TOKEN=[yellow]{gw_token}[/]")
            lines.append(f"  claude --model {model}")

        if not HAS_RICH:
            print("\n" + "=" * 60)
            print("  TUNNEL-ONLY MODE")
            print("=" * 60)
            for line in lines:
                # Strip rich markup for plain output
                import re
                clean = re.sub(r'\[/?[^\]]*\]', '', line)
                print(f"  {clean}")
            print("\n  Press Ctrl+C to stop the tunnel.")
            print("=" * 60 + "\n")
            return

        content = "\n".join(lines)
        content += "\n\n[dim]Press Ctrl+C to stop the tunnel.[/]"
        panel = Panel(
            content,
            title="[bold]TUNNEL-ONLY MODE[/]",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
        self.console.print()
        self.console.print(panel)
        self.console.print()

    def warnings_panel(self, warnings: List[str]) -> None:
        """Display warnings in a yellow panel."""
        if not warnings:
            return
        if not HAS_RICH:
            print("\n  Warnings:")
            for w in warnings:
                print(f"    [!] {w}")
            print()
            return

        content = "\n".join(f"[yellow]•[/] {escape(w)}" for w in warnings)
        panel = Panel(content, title="[bold yellow]Warnings[/]", border_style="yellow", padding=(0, 1))
        self.console.print(panel)
        self.console.print()


# Singleton
ui = UI()
