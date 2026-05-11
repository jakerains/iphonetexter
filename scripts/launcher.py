#!/usr/bin/env python3
"""Interactive control panel for iphonetexter.

Invoked by start.sh after bootstrap (Swift CLI built, venv ready, deps installed).
Provides a rich-based status panel and arrow-key menu for ongoing operations.

Run directly only if the venv is already set up. Otherwise use start.sh.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import questionary
    from rich.console import Console, Group
    from rich.live import Live
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.prompt import Confirm
    from rich.table import Table
    from rich.text import Text
except ImportError as exc:  # pragma: no cover - imports come from venv
    sys.stderr.write(
        f"launcher.py requires 'rich' and 'questionary' in the active Python env.\n"
        f"  missing: {exc.name}\n"
        f"  fix: rerun ./start.sh so the venv installs scripts/web_ui/requirements.txt\n"
    )
    raise SystemExit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
IMSG_BIN = REPO_ROOT / "bin" / "imsg"
WEB_DIR = REPO_ROOT / "scripts" / "web_ui"
LOG_DIR = REPO_ROOT / ".logs"
WEB_UI_LOG = LOG_DIR / "web-ui.log"
HOST = os.environ.get("IMSG_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("IMSG_WEB_PORT", "8765"))
URL = f"http://{HOST}:{PORT}/"

console = Console()


# ---------------------------------------------------------------------------
# Environment probes — cheap, called for the status panel and diagnostics


def chat_db_has_access() -> bool:
    db = Path.home() / "Library" / "Messages" / "chat.db"
    if not db.exists():
        return False
    try:
        with open(db, "rb") as f:
            f.read(1)
        return True
    except (PermissionError, OSError):
        return False


def imsg_version() -> str:
    if not IMSG_BIN.exists():
        return "not built"
    try:
        out = subprocess.run(
            [str(IMSG_BIN), "--version"],
            capture_output=True, text=True, timeout=5,
        )
        text = (out.stdout or out.stderr).strip()
        return text.splitlines()[0] if text else "unknown"
    except Exception:
        return "unknown"


def git_info() -> tuple[str, str, bool, int]:
    """Returns (short_sha, latest_tag, is_dirty, commits_behind_origin)."""
    def run(*args: str) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(REPO_ROOT), *args],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip()
        except Exception:
            return ""

    sha = run("rev-parse", "--short", "HEAD") or "?"
    tag = run("describe", "--tags", "--abbrev=0") or "none"
    dirty = bool(run("status", "--porcelain"))
    behind_str = run("rev-list", "--count", "HEAD..@{u}")
    try:
        behind = int(behind_str) if behind_str else 0
    except ValueError:
        behind = 0
    return sha, tag, dirty, behind


def server_running() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        s.connect((HOST, PORT))
        return True
    except (OSError, socket.error):
        return False
    finally:
        with contextlib.suppress(Exception):
            s.close()


# ---------------------------------------------------------------------------
# Status panel rendering


def status_panel() -> Panel:
    sha, tag, dirty, behind = git_info()
    fda_ok = chat_db_has_access()

    fda_text = Text("granted", style="bold green") if fda_ok else Text("missing", style="bold red")
    server_text = (
        Text("running", style="bold green") if server_running()
        else Text("stopped", style="dim")
    )
    repo_text = Text(f"{tag} @ {sha}")
    if dirty:
        repo_text.append("  (uncommitted changes)", style="yellow")
    if behind > 0:
        repo_text.append(f"  ({behind} behind origin)", style="yellow")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right", no_wrap=True)
    grid.add_column()
    grid.add_row("Web UI", Text(URL, style="cyan"))
    grid.add_row("imsg", Text(f"bin/imsg  ({imsg_version()})"))
    grid.add_row("Repo", repo_text)
    grid.add_row("FDA", fda_text)
    grid.add_row("Server", server_text)

    return Panel(
        Padding(grid, (0, 1)),
        title="[bold cyan]iphonetexter[/bold cyan]",
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Actions

@dataclass
class ActionResult:
    ok: bool
    message: str = ""


def action_start_web_ui() -> ActionResult:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not chat_db_has_access():
        console.print(
            "[yellow]Warning:[/yellow] Full Disk Access is missing. The web UI will start "
            "but /chats and /inbound will be empty.\n"
        )
    if server_running():
        console.print(f"[yellow]Server already running at {URL}.[/yellow]")
        if Confirm.ask("Open it in your browser?", default=True):
            subprocess.run(["open", URL])
        return ActionResult(ok=True)

    env = os.environ.copy()
    env["IMSG_BIN"] = str(IMSG_BIN)
    env["IMSG_WEB_HOST"] = HOST
    env["IMSG_WEB_PORT"] = str(PORT)

    console.print(
        Panel.fit(
            Text.from_markup(
                f"[bold]Starting web UI[/bold] at [cyan]{URL}[/cyan]\n"
                "[dim]Logs:[/dim] " + str(WEB_UI_LOG.relative_to(REPO_ROOT)) + "\n"
                "[dim]Press Ctrl+C to stop and return to the menu.[/dim]"
            ),
            border_style="green",
        )
    )

    # Open browser once server is responsive.
    open_after_ready()

    # tee uvicorn output to the log file AND the terminal so the user sees liveness
    # and "View recent logs" has something to show.
    with WEB_UI_LOG.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            [sys.executable, str(WEB_DIR / "server.py")],
            cwd=str(WEB_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                logf.write(line)
                logf.flush()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            console.print("\n[dim]Server stopped.[/dim]")
        else:
            proc.wait()
    return ActionResult(ok=True)


def open_after_ready() -> None:
    """Background-ish: poll once a second for ~20s, then open browser."""
    import threading

    def _poll():
        for _ in range(40):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            try:
                s.connect((HOST, PORT))
                s.close()
                subprocess.run(["open", URL])
                return
            except OSError:
                pass
            finally:
                with contextlib.suppress(Exception):
                    s.close()
            time.sleep(0.5)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()


def action_update() -> ActionResult:
    sha_before, _, dirty, behind = git_info()
    if dirty:
        console.print(
            "[yellow]Uncommitted changes detected.[/yellow] "
            "Commit or stash them before updating."
        )
        return ActionResult(ok=False, message="dirty tree")
    if behind == 0:
        console.print("[green]Already up to date with origin.[/green]")
        return ActionResult(ok=True)

    console.print(f"Fetching and pulling {behind} commit(s) from origin...")
    with Progress(
        SpinnerColumn(), TextColumn("{task.description}"),
        TimeElapsedColumn(), console=console, transient=True,
    ) as progress:
        task = progress.add_task("git pull", total=None)
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--ff-only"],
            capture_output=True, text=True,
        )
        progress.update(task, completed=1)
    if r.returncode != 0:
        console.print(f"[red]git pull failed:[/red]\n{r.stderr.strip()}")
        return ActionResult(ok=False)
    console.print(r.stdout.strip() or "(no output)")

    # Re-run pip install in case requirements.txt changed.
    console.print("\nUpdating Python dependencies...")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as p:
        p.add_task("pip install -r scripts/web_ui/requirements.txt", total=None)
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             "-r", str(WEB_DIR / "requirements.txt")],
            capture_output=True, text=True,
        )
    if pip.returncode != 0:
        console.print(f"[red]pip install failed:[/red]\n{pip.stderr.strip()}")
        return ActionResult(ok=False)

    # Rebuild Swift CLI if Sources/ or Package.swift changed.
    changed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--name-only", sha_before, "HEAD"],
        capture_output=True, text=True,
    ).stdout
    if any(p.startswith(("Sources/", "Package.swift", "Package.resolved")) for p in changed.splitlines()):
        console.print("\nSwift sources changed — rebuilding imsg CLI...")
        rebuild = run_with_tail(["make", "build"], "make build")
        if rebuild != 0:
            console.print("[red]Swift build failed.[/red]")
            return ActionResult(ok=False)
    else:
        console.print("\n[dim]No Swift changes — skipping rebuild.[/dim]")

    sha_after, _, _, _ = git_info()
    console.print(
        Panel.fit(
            Text.from_markup(f"[bold green]Updated[/bold green]  {sha_before} → {sha_after}"),
            border_style="green",
        )
    )
    return ActionResult(ok=True)


def action_reinstall() -> ActionResult:
    console.print(
        Panel.fit(
            Text.from_markup(
                "[bold yellow]Reinstall from scratch[/bold yellow] will:\n"
                "  • delete [cyan].venv/[/cyan]\n"
                "  • delete [cyan]bin/imsg[/cyan]\n"
                "  • re-run ./start.sh from zero (~1-2 min for Swift build)"
            ),
            border_style="yellow",
        )
    )
    if not Confirm.ask("Continue?", default=False):
        return ActionResult(ok=False, message="cancelled")

    venv_dir = REPO_ROOT / ".venv"
    bin_dir = REPO_ROOT / "bin"
    if venv_dir.exists():
        console.print(f"[dim]Removing {venv_dir.relative_to(REPO_ROOT)}/...[/dim]")
        shutil.rmtree(venv_dir)
    if bin_dir.exists():
        console.print(f"[dim]Removing {bin_dir.relative_to(REPO_ROOT)}/...[/dim]")
        shutil.rmtree(bin_dir)

    console.print("\n[bold]Re-executing ./start.sh...[/bold]\n")
    # Re-exec start.sh; this process is replaced.
    os.execv(str(REPO_ROOT / "start.sh"), [str(REPO_ROOT / "start.sh")])
    return ActionResult(ok=True)  # unreachable


def action_diagnose() -> ActionResult:
    console.print(Panel.fit(Text("Permission diagnostics", style="bold"), border_style="cyan"))

    rows: list[tuple[str, bool, str]] = []

    # 1) chat.db readable (FDA)
    fda_ok = chat_db_has_access()
    rows.append((
        "Full Disk Access (chat.db readable)",
        fda_ok,
        "OK" if fda_ok else "Open System Settings -> Privacy & Security -> Full Disk Access "
                            "and add your terminal app.",
    ))

    # 2) imsg binary runs
    imsg_ok = IMSG_BIN.exists() and os.access(str(IMSG_BIN), os.X_OK)
    rows.append((
        "imsg binary built",
        imsg_ok,
        "OK" if imsg_ok else "Pick 'Reinstall from scratch' to rebuild.",
    ))

    # 3) imsg can list chats (combined FDA + binary + Messages.app)
    chats_ok = False
    chats_detail = "n/a (skipped because earlier checks failed)"
    if imsg_ok and fda_ok:
        try:
            r = subprocess.run(
                [str(IMSG_BIN), "chats", "--limit", "1", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            chats_ok = r.returncode == 0
            chats_detail = "OK" if chats_ok else (r.stderr.strip()[:200] or "imsg chats returned non-zero")
        except subprocess.TimeoutExpired:
            chats_detail = "imsg chats timed out after 10s"
        except Exception as exc:  # noqa: BLE001
            chats_detail = f"imsg chats raised: {exc}"

    rows.append(("imsg chats --limit 1", chats_ok, chats_detail))

    # 4) Automation permission (probe — will auto-prompt if first time)
    if Confirm.ask(
        "\nProbe Messages.app Automation permission now? "
        "(macOS will pop a one-time prompt if you haven't granted it)",
        default=False,
    ):
        try:
            r = subprocess.run(
                ["osascript", "-e", 'tell application "Messages" to get name'],
                capture_output=True, text=True, timeout=15,
            )
            auto_ok = r.returncode == 0 and "Messages" in r.stdout
            rows.append((
                "Messages.app Automation",
                auto_ok,
                "OK" if auto_ok else (r.stderr.strip()[:200] or "AppleScript probe failed"),
            ))
        except Exception as exc:  # noqa: BLE001
            rows.append(("Messages.app Automation", False, f"probe error: {exc}"))

    # Render results
    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("Check")
    table.add_column("Status", width=8)
    table.add_column("Detail")
    for name, ok, detail in rows:
        status = Text("PASS", style="bold green") if ok else Text("FAIL", style="bold red")
        table.add_row(name, status, detail)
    console.print(table)
    return ActionResult(ok=True)


def action_view_logs() -> ActionResult:
    if not WEB_UI_LOG.exists():
        console.print("[dim]No web UI session has been started yet (no log file).[/dim]")
        return ActionResult(ok=True)
    text = WEB_UI_LOG.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tail = lines[-80:] if len(lines) > 80 else lines
    body = "\n".join(tail) or "[dim](log file is empty)[/dim]"
    console.print(
        Panel(
            body,
            title=f"[bold]{WEB_UI_LOG.relative_to(REPO_ROOT)}[/bold]  (last {len(tail)} lines)",
            title_align="left",
            border_style="dim",
        )
    )
    return ActionResult(ok=True)


# ---------------------------------------------------------------------------
# Subprocess helper: run a command and tail its last output line in a Live status


def run_with_tail(cmd: list[str], label: str) -> int:
    """Run a command; show last stdout line in a rich Status. Returns exit code."""
    proc = subprocess.Popen(
        cmd, cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    last_line = ""
    width = max(40, console.size.width - 8)

    def render() -> Text:
        line = last_line[:width] if last_line else "(starting...)"
        return Text.from_markup(f"[bold]{label}[/bold]  [dim]{line}[/dim]")

    with Live(render(), console=console, refresh_per_second=10, transient=True) as live:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if line:
                last_line = line
                live.update(render())
        proc.wait()

    status = "[bold green]done[/bold green]" if proc.returncode == 0 else f"[bold red]failed (exit {proc.returncode})[/bold red]"
    console.print(f"  {label} — {status}")
    return proc.returncode


# ---------------------------------------------------------------------------
# Main menu loop


MENU = [
    ("start",   "Start web UI",            action_start_web_ui),
    ("update",  "Update from origin",      action_update),
    ("install", "Reinstall from scratch",  action_reinstall),
    ("diag",    "Diagnose permissions",    action_diagnose),
    ("logs",    "View recent logs",        action_view_logs),
    ("quit",    "Quit",                    None),
]


def render_header() -> None:
    console.clear()
    console.print(status_panel())
    console.print()


def menu_loop(initial: Optional[str] = None) -> None:
    first = True
    while True:
        if first and initial:
            choice_key = initial
            first = False
        else:
            render_header()
            choice = questionary.select(
                "What would you like to do?",
                choices=[questionary.Choice(label, value=key) for key, label, _ in MENU],
                default="start",
                use_shortcuts=True,
                qmark="",
                instruction="(arrows + Enter, or press the highlighted key)",
            ).ask()
            if choice is None:  # Ctrl+C / Esc
                console.print("[dim]Bye.[/dim]")
                return
            choice_key = choice

        if choice_key == "quit":
            console.print("[dim]Bye.[/dim]")
            return

        action = next((a for key, _, a in MENU if key == choice_key), None)
        if action is None:
            continue

        console.print()
        try:
            action()
        except KeyboardInterrupt:
            console.print("\n[dim]Cancelled.[/dim]")

        if choice_key not in ("quit", "install"):
            console.print()
            try:
                input("Press Enter to return to the menu... ")
            except (EOFError, KeyboardInterrupt):
                return


def parse_initial_action(argv: list[str]) -> Optional[str]:
    if len(argv) <= 1:
        return None
    arg = argv[1]
    valid = {key for key, _, _ in MENU}
    if arg in valid:
        return arg
    sys.stderr.write(f"unknown action: {arg!r}\nvalid: {sorted(valid)}\n")
    raise SystemExit(2)


def main() -> None:
    initial = parse_initial_action(sys.argv)
    try:
        menu_loop(initial=initial)
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/dim]")


if __name__ == "__main__":
    main()
