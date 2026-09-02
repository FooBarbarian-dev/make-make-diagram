"""Opening reports and URLs in the user's browser, on every platform
pipeview runs on — including inside WSL, where the browser lives on the
Windows side.

`webbrowser.open` is the right tool on Windows, macOS, and a Linux
desktop. Inside a WSL distro it is usually useless: there is no Linux
browser, `xdg-open` is often missing, and when it *is* present it may
report success while opening nothing. WSL interop, on the other hand,
lets us exec Windows programs directly, so here we go through `wslview`
(from wslu, preinstalled on the Ubuntu images) or, failing that,
PowerShell's `Start-Process` / `cmd.exe start` with the path converted
by `wslpath -w`.

Every entry point returns a bool and never raises: callers decide how
to tell the user when nothing could be opened (the report path is
always printed or shown, so a manual open is one click away).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import webbrowser
from pathlib import Path

_LAUNCH_TIMEOUT = 15


def is_wsl() -> bool:
    """Running inside Windows Subsystem for Linux?"""
    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in platform.uname().release.lower()
    except Exception:  # exotic platforms: never let detection raise
        return False


def is_url(target: str) -> bool:
    return target.startswith(("http://", "https://", "file://"))


def to_uri(target: str) -> str:
    """A URL as given; a local path as a well-formed file:// URI (on
    Windows that means file:///C:/..., not file://C:\\...)."""
    if is_url(target):
        return target
    return Path(target).resolve().as_uri()


def open_in_browser(target: str) -> bool:
    """Open `target` — a local file path or an http(s)/file URL — in the
    default browser. True if a launch was handed off successfully."""
    try:
        url = to_uri(target)
    except (OSError, ValueError):
        return False
    # An explicit $BROWSER is the user's word, on WSL too.
    if is_wsl() and not os.environ.get("BROWSER"):
        if _open_from_wsl(target, url):
            return True
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# WSL: hand the target to the Windows side
# ---------------------------------------------------------------------------

def _run_quiet(argv: list[str]) -> bool:
    """Run a launcher with its output discarded (browsers chatter on
    stdout, and for the LSP stdout is the protocol channel)."""
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=_LAUNCH_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _windows_path(target: str) -> str | None:
    """`wslpath -w` for a Linux path: \\\\wsl.localhost\\<distro>\\... or the
    /mnt/c drive path, whichever Windows can open."""
    try:
        proc = subprocess.run(
            ["wslpath", "-w", target], capture_output=True, text=True,
            timeout=_LAUNCH_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _open_from_wsl(target: str, url: str) -> bool:
    # wslview takes Linux paths and URLs alike and does the conversion.
    if shutil.which("wslview") and _run_quiet(["wslview", target]):
        return True
    if is_url(target):
        win_target = target
    else:
        win_target = _windows_path(target)
        if win_target is None:
            return False
    # Windows tools on PATH through interop (they are on a stock distro;
    # spell out the usual mount in case PATH interop is disabled).
    powershell = shutil.which("powershell.exe") or \
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    cmd = shutil.which("cmd.exe") or "/mnt/c/Windows/System32/cmd.exe"
    quoted = win_target.replace("'", "''")
    if _run_quiet([powershell, "-NoProfile", "-NonInteractive", "-Command",
                   f"Start-Process -FilePath '{quoted}'"]):
        return True
    # `start` needs the empty title argument when the target is quoted.
    return _run_quiet([cmd, "/c", "start", "", win_target])
