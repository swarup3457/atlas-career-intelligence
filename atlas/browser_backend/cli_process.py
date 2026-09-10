"""Launch one Copilot-CLI company process safely and capture everything.

Design rules (prompt s.5.3, s.12 PROCESS):

* Build a subprocess **argument list**, never a shell string. Untrusted company /
  query data only ever appears as a single argv element, never concatenated into
  shell syntax.
* On Windows the ``copilot`` shim is a ``.cmd`` / ``.ps1``; wrap it via
  ``cmd.exe /c`` (or ``powershell -File``) with args passed as list elements, so
  no untrusted data is interpolated into shell syntax.
* On timeout/interruption terminate the **whole child process tree by PID**
  (``taskkill /PID <pid> /T`` — never ``/IM`` name-based) so unrelated Copilot /
  Chrome / Edge / Excel processes are untouched.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from atlas.browser_backend.jsonl_parser import (
    count_tool_calls, final_assistant_text, load_events,
)
from atlas.browser_backend.models import CliProcessConfig, CompanyTask, ProcessCapture

_SHELL_METACHARS = set("&|<>^%!`$")


def resolve_copilot(explicit: Optional[str] = None) -> Optional[Path]:
    """Resolve the Copilot CLI entry point. Prefer a directly-executable form."""
    if explicit and Path(explicit).exists():
        return Path(explicit)
    for name in ("copilot.cmd", "copilot.exe", "copilot", "copilot.ps1"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def build_copilot_args(
    task: CompanyTask,
    cfg: CliProcessConfig,
    *,
    prompt: str,
    mcp_config_path: Path,
    usage_path: Path,
    log_dir: Path,
    agent: str = "",
    add_dir: Optional[Path] = None,
) -> list[str]:
    """Build the *logical* Copilot argument list (before any OS shell wrapper).

    Only Playwright MCP tools are made available and pre-approved; the built-in
    GitHub MCP is disabled; ask-user and remote export are disabled.
    """
    args: list[str] = [
        "--prompt", prompt,
        "--model", cfg.model,
        "--output-format", "json",
        "--allow-all-tools",            # required for non-interactive; scope limited below
        "--disable-builtin-mcps",       # no github MCP
        "--additional-mcp-config", f"@{mcp_config_path}",
        "--usage-output-file", str(usage_path),
        "--log-dir", str(log_dir),
        "--log-level", "default",
        "--name", task.session_name,
        "--max-ai-credits", str(cfg.max_ai_credits),
        "--max-autopilot-continues", str(cfg.max_continuations),
        "--mode", "autopilot",
        "--context", "long_context" if cfg.long_context else "default",
        "--no-color",
        "--no-auto-update",
    ]
    if cfg.no_ask_user:
        args.append("--no-ask-user")
    if cfg.no_remote_export:
        args.append("--no-remote-export")
    if cfg.reasoning_effort:
        args += ["--effort", cfg.reasoning_effort]
    if agent:
        args += ["--agent", agent]
    if add_dir is not None:
        args += ["--add-dir", str(add_dir)]
    return args


def finalize_argv(copilot_path: Path, logical_args: list[str]) -> list[str]:
    """Wrap the logical args for the OS without interpolating untrusted data."""
    suffix = copilot_path.suffix.lower()
    if suffix == ".cmd" or suffix == ".bat":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", str(copilot_path), *logical_args]
    if suffix == ".ps1":
        ps = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(copilot_path), *logical_args]
    return [str(copilot_path), *logical_args]


def prompt_is_shell_safe(prompt: str) -> bool:
    """A defensive guard: the prompt must not rely on cmd/PowerShell expansion
    metacharacters that survive inside double quotes (``%`` and ``!``)."""
    return "%" not in prompt and "!" not in prompt


def terminate_tree(proc: subprocess.Popen) -> None:
    """Kill the child process tree by PID only (never by image name)."""
    if proc.poll() is not None:
        return
    pid = proc.pid
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            time.sleep(1.0)
            if proc.poll() is None:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def run_company_process(
    task: CompanyTask,
    cfg: CliProcessConfig,
    *,
    prompt: str,
    mcp_config_path: Path,
    output_dir: Path,
    copilot_path: Optional[Path] = None,
    agent: str = "",
    add_dir: Optional[Path] = None,
) -> ProcessCapture:
    """Run one company process, capturing JSONL stdout, stderr, usage, timing."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = output_dir / "copilot-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / "copilot_events.jsonl"
    stderr_path = output_dir / "copilot_stderr.txt"
    usage_path = output_dir / "copilot_usage.json"

    cap = ProcessCapture(stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                         usage_path=str(usage_path), mcp_output_dir=str(output_dir / "mcp-output"))

    resolved = copilot_path or resolve_copilot()
    if copilot_path is not None and not Path(copilot_path).exists():
        resolved = None
    if resolved is None:
        cap.exit_code = 127
        cap.final_assistant_text = ""
        stderr_path.write_text("copilot CLI not found on PATH", encoding="utf-8")
        return cap
    if not prompt_is_shell_safe(prompt):
        cap.exit_code = 126
        stderr_path.write_text("prompt rejected: contains shell-expansion metacharacters", encoding="utf-8")
        return cap

    logical = build_copilot_args(task, cfg, prompt=prompt, mcp_config_path=mcp_config_path,
                                 usage_path=usage_path, log_dir=log_dir, agent=agent, add_dir=add_dir)
    argv = finalize_argv(resolved, logical)
    cap.argv = argv

    creationflags = 0
    preexec = None
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        preexec = os.setsid  # own process group => killpg the whole tree

    cap.started_at = time.time()
    with stdout_path.open("w", encoding="utf-8") as out_fh, stderr_path.open("w", encoding="utf-8") as err_fh:
        try:
            proc = subprocess.Popen(argv, stdout=out_fh, stderr=err_fh, cwd=str(output_dir),
                                    creationflags=creationflags, preexec_fn=preexec,
                                    env=dict(os.environ))
        except (OSError, ValueError) as exc:
            cap.ended_at = time.time()
            cap.exit_code = 1
            err_fh.write(f"failed to spawn copilot: {exc}")
            return cap
        try:
            cap.exit_code = proc.wait(timeout=cfg.wall_clock_timeout_s)
        except subprocess.TimeoutExpired:
            cap.timed_out = True
            terminate_tree(proc)
            try:
                cap.exit_code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                cap.exit_code = None
        except KeyboardInterrupt:  # pragma: no cover - interactive interruption
            cap.interrupted = True
            terminate_tree(proc)
            cap.exit_code = None
    cap.ended_at = time.time()

    cap.jsonl_events = load_events(stdout_path)
    cap.final_assistant_text = final_assistant_text(cap.jsonl_events)
    return cap


__all__ = [
    "resolve_copilot", "build_copilot_args", "finalize_argv", "prompt_is_shell_safe",
    "terminate_tree", "run_company_process",
]
