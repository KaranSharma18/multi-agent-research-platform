"""`mara dev`: run every service as a child process, with prefixed, coloured logs.

This is the no-Docker way to run the platform, and it works the same on
Windows, macOS and Linux. Services start in stages; each stage waits until the
previous one answers its health check. Ctrl+C stops everything.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from mara_core.paths import repo_root
from rich.console import Console

console = Console(highlight=False)
IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class Service:
    name: str
    module: str
    port: int | None
    stage: int
    colour: str

    @property
    def health(self) -> str | None:
        return f"http://127.0.0.1:{self.port}/healthz" if self.port else None


SERVICES = [
    Service("token-service", "mara_token_service", 8090, 0, "bright_magenta"),
    Service("mock-apis", "mara_mock_apis", 8300, 0, "bright_black"),
    Service("mcp-knowledge", "mara_mcp_knowledge", 8101, 1, "cyan"),
    Service("mcp-warehouse", "mara_mcp_warehouse", 8102, 1, "blue"),
    Service("mcp-live-data", "mara_mcp_live_data", 8103, 1, "green"),
    Service("mcp-actions", "mara_mcp_actions", 8104, 1, "red"),
    Service("research-agent", "mara_research_agent", 8200, 1, "yellow"),
    Service("orchestrator", "mara_orchestrator", 8000, 2, "bright_white"),
    Service("executor", "mara_executor", None, 3, "bright_red"),
]


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class DevRunner:
    def __init__(self, only: list[str] | None = None, ui: bool = False):
        self.services = [s for s in SERVICES if not only or s.name in only]
        self.ui = ui
        self.procs: dict[str, subprocess.Popen] = {}
        self.stopping = False

    def _pump(self, name: str, colour: str, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.strip():
                console.print(f"[{colour}]{name:>15}[/] │ {line.rstrip()}", markup=True, soft_wrap=True)
        code = proc.wait()
        if not self.stopping:
            console.print(f"[{colour}]{name:>15}[/] │ [bold red]exited with code {code}[/]")

    def _spawn(self, name: str, colour: str, cmd: list[str], env: dict[str, str], cwd: Path | None = None) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        self.procs[name] = proc
        threading.Thread(target=self._pump, args=(name, colour, proc), daemon=True).start()

    def _wait_healthy(self, service: Service, timeout: float = 90.0) -> bool:
        if not service.health:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = self.procs.get(service.name)
            if proc is not None and proc.poll() is not None:
                return False
            try:
                if httpx.get(service.health, timeout=1.0).status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.4)
        return False

    def start(self) -> None:
        root = repo_root()
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "MARA_ROOT": str(root)}
        busy = [s for s in self.services if s.port and port_in_use(s.port)]
        if busy:
            for s in busy:
                console.print(f"[red]port {s.port} ({s.name}) is already in use; stop the other process first[/]")
            raise SystemExit(1)
        for stage in sorted({s.stage for s in self.services}):
            group = [s for s in self.services if s.stage == stage]
            for s in group:
                child_env = {**env, "PORT": str(s.port)} if s.port else env
                self._spawn(s.name, s.colour, [sys.executable, "-m", s.module], child_env)
            for s in group:
                if not self._wait_healthy(s):
                    console.print(f"[bold red]{s.name} did not become healthy; see its log above[/]")
                    self.stop()
                    raise SystemExit(1)
            console.print(f"[dim]stage {stage} ready: {', '.join(s.name for s in group)}[/]")
        if self.ui:
            npm = shutil.which("npm")
            if npm is None:
                console.print(
                    "[yellow]npm not found: install Node.js 20+ to run the UI, or open the API docs at "
                    "http://localhost:8000/docs[/]"
                )
            else:
                self._spawn("ui", "bright_cyan", [npm, "run", "dev"], env, cwd=root / "ui")
        console.print(
            "\n[bold green]MARA is up.[/] Orchestrator: http://localhost:8000/docs"
            + ("   UI: http://localhost:5173" if self.ui else "")
            + '\nTry: [bold]uv run mara ask "What caused last week\'s payments outage?"[/]   (Ctrl+C stops)\n'
        )

    def stop(self) -> None:
        self.stopping = True
        for proc in reversed(list(self.procs.values())):
            if proc.poll() is None:
                try:
                    if IS_WINDOWS:
                        proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                    else:
                        proc.terminate()
                except OSError:
                    pass
        deadline = time.monotonic() + 8
        for proc in self.procs.values():
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                proc.kill()

    def run(self) -> None:
        try:
            self.start()
            while any(p.poll() is None for p in self.procs.values()):
                time.sleep(0.5)
        except KeyboardInterrupt:
            console.print("\n[dim]stopping...[/]")
        finally:
            self.stop()
