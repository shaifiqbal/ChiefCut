"""Locating FFmpeg / FFprobe and running them safely."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path


class ToolNotFoundError(RuntimeError):
    """FFmpeg or FFprobe is not installed / not on PATH."""


# Hide the black console window that would otherwise flash up on Windows.
CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    custom = os.environ.get("CHIEFCUT_FFMPEG_DIR")
    if custom:
        dirs.append(Path(custom))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            # `winget install Gyan.FFmpeg` puts shortcuts here; PATH only
            # refreshes in new terminals, so look explicitly.
            dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
            pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
            if pkgs.is_dir():
                dirs.extend(sorted(pkgs.glob("Gyan.FFmpeg*/*/bin")))
        dirs += [Path(r"C:\ffmpeg\bin"), Path(r"C:\Program Files\ffmpeg\bin")]
    return dirs


@functools.lru_cache(maxsize=None)
def find_tool(name: str) -> str:
    """Return the full path of `ffmpeg` or `ffprobe`."""
    if name not in ("ffmpeg", "ffprobe"):
        raise ValueError(name)
    exe = name + (".exe" if os.name == "nt" else "")
    for d in _candidate_dirs():
        p = d / exe
        if p.is_file():
            return str(p)
    found = shutil.which(name)
    if found:
        return found
    hint = (
        "Install it by opening PowerShell and running:  winget install -e --id Gyan.FFmpeg\n"
        "Then close and reopen PowerShell and start ChiefCut again."
        if os.name == "nt"
        else "Install FFmpeg with your package manager and make sure it is on PATH."
    )
    raise ToolNotFoundError(f"{name} was not found on this computer. {hint}")


def run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    """Run a command from an argument list (never through a shell)."""
    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        creationflags=CREATIONFLAGS,
    )


@functools.lru_cache(maxsize=None)
def ffmpeg_version() -> str:
    out = run([find_tool("ffmpeg"), "-hide_banner", "-version"], timeout=20)
    first = out.stdout.decode("utf-8", "replace").splitlines()
    return first[0] if first else "unknown"


@functools.lru_cache(maxsize=None)
def available_encoders() -> frozenset[str]:
    out = run([find_tool("ffmpeg"), "-hide_banner", "-encoders"], timeout=20)
    names = set()
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        # Lines look like: " V....D libx264   libx264 H.264 / AVC ..."
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def python_info() -> str:
    return f"Python {sys.version.split()[0]}"
