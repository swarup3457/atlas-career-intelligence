"""Windows daily scheduling installer/generator (Phase 1E/F §11).

Generates a Windows Task Scheduler (``schtasks``) command that runs the daily
Atlas graph headless in the background. SAFETY: scheduler creation is DRY-RUN by
default; enabling requires an explicit flag. A scheduled run NEVER opens an auth
window — expired auth yields WAITING_FOR_HUMAN and an exact manual command
(surfaced by the daily runner), it does not prompt. Paths are always quoted.

The actual ``schtasks`` invocation is injectable so the generator + argument
quoting can be unit-tested without touching the real Task Scheduler.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

TASK_NAME = "AtlasDailyRun"
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

CommandRunner = Callable[[Sequence[str]], Any]


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True)  # noqa: S603


def _quote(value: str) -> str:
    return value if (value and " " not in value and '"' not in value) else f'"{value}"'


def display_command(argv: Sequence[str]) -> str:
    return " ".join(_quote(str(a)) for a in argv)


@dataclass
class SchedulerAction:
    action: str            # install | disable | remove
    task: str
    dry_run: bool
    enabled: bool
    argv: tuple[str, ...]
    command: str
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "task": self.task, "dry_run": self.dry_run,
            "enabled": self.enabled, "argv": list(self.argv), "command": self.command,
            "returncode": self.returncode, "stdout": self.stdout, "stderr": self.stderr,
        }


class WindowsDailyScheduler:
    def __init__(
        self,
        *,
        python_exe: Optional[str] = None,
        task_name: str = TASK_NAME,
        runner: CommandRunner = _default_runner,
    ) -> None:
        self.python_exe = python_exe or sys.executable
        self.task_name = task_name
        self._run = runner

    @staticmethod
    def validate_time(time_hhmm: str) -> None:
        if not _TIME_RE.match(time_hhmm or ""):
            raise ValueError(f"invalid time {time_hhmm!r}; expected HH:mm (00:00-23:59)")

    def task_run_command(self) -> str:
        """The command the scheduled task executes: the daily graph, live +
        headless/background. It never opens an interactive auth window."""
        return f'"{self.python_exe}" -m atlas.cli daily run --live --background'

    def create_argv(self, time_hhmm: str) -> tuple[str, ...]:
        return (
            "schtasks", "/Create", "/SC", "DAILY", "/ST", time_hhmm,
            "/TN", self.task_name, "/TR", self.task_run_command(),
            "/RL", "LIMITED", "/F",
        )

    def install(self, time_hhmm: str, *, enable: bool = False) -> SchedulerAction:
        self.validate_time(time_hhmm)
        argv = self.create_argv(time_hhmm)
        command = display_command(argv)
        if not enable:
            # DRY-RUN default: generate the command, never touch Task Scheduler.
            return SchedulerAction(
                action="install", task=self.task_name, dry_run=True, enabled=False,
                argv=argv, command=command,
            )
        result = self._run(argv)
        rc = getattr(result, "returncode", 1)
        return SchedulerAction(
            action="install", task=self.task_name, dry_run=False, enabled=(rc == 0),
            argv=argv, command=command, returncode=rc,
            stdout=getattr(result, "stdout", "") or "", stderr=getattr(result, "stderr", "") or "",
        )

    def disable(self) -> SchedulerAction:
        argv = ("schtasks", "/Change", "/TN", self.task_name, "/DISABLE")
        result = self._run(argv)
        rc = getattr(result, "returncode", 1)
        return SchedulerAction(
            action="disable", task=self.task_name, dry_run=False, enabled=False,
            argv=argv, command=display_command(argv), returncode=rc,
            stdout=getattr(result, "stdout", "") or "", stderr=getattr(result, "stderr", "") or "",
        )

    def remove(self) -> SchedulerAction:
        argv = ("schtasks", "/Delete", "/TN", self.task_name, "/F")
        result = self._run(argv)
        rc = getattr(result, "returncode", 1)
        return SchedulerAction(
            action="remove", task=self.task_name, dry_run=False, enabled=False,
            argv=argv, command=display_command(argv), returncode=rc,
            stdout=getattr(result, "stdout", "") or "", stderr=getattr(result, "stderr", "") or "",
        )


__all__ = ["TASK_NAME", "SchedulerAction", "WindowsDailyScheduler", "display_command"]
