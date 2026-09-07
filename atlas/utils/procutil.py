"""Cross-platform (Windows-first) process-liveness helpers.

Used by the browser-profile lock and the run lock to distinguish a
crashed/stale lock (safe to reclaim) from a lock genuinely held by a
live process (must never be reclaimed). Does not touch or modify any
other process — read-only liveness probing only.
"""

from __future__ import annotations

import os
import sys


def is_pid_running(pid: int) -> bool:
    """Return True if a process with `pid` currently exists.

    On Windows, uses OpenProcess (read-only query) via ctypes - no
    external dependency (e.g. psutil) required. On POSIX, uses the
    standard os.kill(pid, 0) probe.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            STILL_ACTIVE = 259
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user - still "running".
        return True
    except OSError:
        return False
    return True


def current_pid() -> int:
    return os.getpid()
