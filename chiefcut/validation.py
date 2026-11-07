"""Validation of user input: timestamps, clip ranges, filenames and paths.

Everything here is pure Python (no FFmpeg) so it is fast and easy to test.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Containers we accept. FFprobe still has the final say on whether the file
# can actually be decoded.
ALLOWED_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")

MIN_CLIP_SECONDS = 1.0
MAX_CLIP_SECONDS = 600.0  # 10 minutes is plenty for short-form clips
# Containers/encoders can make durations differ by a few hundredths of a second.
DURATION_TOLERANCE = 0.05

_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
_INTEGER = re.compile(r"^\d+$")


class ValidationError(ValueError):
    """Raised when user input is invalid. The message is shown to the user."""


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------
def parse_timestamp(text: str | float | int | None) -> float:
    """Convert user input into seconds.

    Accepted forms:
        "75"         -> 75 seconds
        "75.5"       -> 75.5 seconds
        "1:15"       -> MM:SS  = 75 seconds
        "0:01:15"    -> HH:MM:SS = 75 seconds
        "0:01:15.25" -> fractional seconds allowed on the last part
    """
    if text is None:
        raise ValidationError("Please enter a start time, for example 0, 75 or 1:15.")
    if isinstance(text, bool):
        raise ValidationError("Start time must be a number of seconds or a timestamp.")
    if isinstance(text, (int, float)):
        value = float(text)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValidationError("Start time must be a real number.")
        if value < 0:
            raise ValidationError("Start time cannot be negative.")
        return value

    s = str(text).strip()
    if not s:
        raise ValidationError("Please enter a start time, for example 0, 75 or 1:15.")
    if len(s) > 20:
        raise ValidationError("That start time is too long. Use seconds (75) or a timestamp (1:15).")
    if s.startswith("-"):
        raise ValidationError("Start time cannot be negative.")

    parts = s.split(":")
    if len(parts) > 3:
        raise ValidationError(
            f'"{s}" is not a valid timestamp. Use seconds (75), MM:SS (1:15) or HH:MM:SS (0:01:15).'
        )

    # Only the final part may have decimals; earlier parts must be whole numbers.
    for i, part in enumerate(parts):
        pattern = _NUMBER if i == len(parts) - 1 else _INTEGER
        if not pattern.match(part):
            raise ValidationError(
                f'"{s}" is not a valid timestamp. Use seconds (75), MM:SS (1:15) or HH:MM:SS (0:01:15).'
            )

    if len(parts) == 1:
        return float(parts[0])

    secs = float(parts[-1])
    if secs >= 60:
        raise ValidationError(f'In "{s}" the seconds part must be below 60.')

    if len(parts) == 2:
        minutes = int(parts[0])
        return minutes * 60 + secs

    hours, minutes = int(parts[0]), int(parts[1])
    if minutes >= 60:
        raise ValidationError(f'In "{s}" the minutes part must be below 60.')
    return hours * 3600 + minutes * 60 + secs


def format_timestamp(seconds: float, precise: bool = False) -> str:
    """Render seconds as M:SS or H:MM:SS (optionally with tenths)."""
    if seconds is None or seconds < 0:
        seconds = 0.0
    if precise:
        whole = int(seconds)
        tenths = int(round((seconds - whole) * 10))
        if tenths == 10:
            whole, tenths = whole + 1, 0
    else:
        whole, tenths = int(round(seconds)), 0
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{base}.{tenths}" if precise else base


# --------------------------------------------------------------------------
# Clip range
# --------------------------------------------------------------------------
def validate_duration(duration: float) -> float:
    try:
        d = float(duration)
    except (TypeError, ValueError):
        raise ValidationError("Clip duration must be a number of seconds.") from None
    if d != d:
        raise ValidationError("Clip duration must be a number of seconds.")
    if d < MIN_CLIP_SECONDS:
        raise ValidationError(f"Clip duration must be at least {MIN_CLIP_SECONDS:g} second.")
    if d > MAX_CLIP_SECONDS:
        raise ValidationError(
            f"Clip duration can be at most {MAX_CLIP_SECONDS:g} seconds ({format_timestamp(MAX_CLIP_SECONDS)})."
        )
    return d


def validate_clip_range(start: float, duration: float, source_duration: float) -> None:
    """Make sure [start, start + duration] fits inside the source video."""
    duration = validate_duration(duration)
    if source_duration is None or source_duration <= 0:
        raise ValidationError("The length of this video could not be determined, so it cannot be clipped safely.")
    if start < 0:
        raise ValidationError("Start time cannot be negative.")

    src = format_timestamp(source_duration, precise=True)

    if duration > source_duration + DURATION_TOLERANCE:
        raise ValidationError(
            f"This video is only {src} long ({source_duration:.1f}s), so a {duration:g}s clip will not fit. "
            f"Choose a duration of {int(source_duration)}s or less."
        )
    if start >= source_duration:
        raise ValidationError(
            f"Start time {format_timestamp(start, precise=True)} is at or after the end of the video ({src})."
        )
    end = start + duration
    if end > source_duration + DURATION_TOLERANCE:
        latest_start = max(0.0, source_duration - duration)
        remaining = source_duration - start
        raise ValidationError(
            f"A {duration:g}s clip starting at {format_timestamp(start, precise=True)} would end at "
            f"{format_timestamp(end, precise=True)}, but the video is only {src} long. "
            f"Start at {format_timestamp(latest_start, precise=True)} or earlier, "
            f"or choose a duration of {int(remaining)}s or less."
        )


# --------------------------------------------------------------------------
# Filenames and paths
# --------------------------------------------------------------------------
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_stem(name: str, max_length: int = 50, fallback: str = "clip") -> str:
    """Turn an arbitrary (possibly hostile) filename into a safe filename stem.

    Directory components are discarded, so "..\\..\\evil" can never escape the
    output folder. Only letters, digits, '-' and '_' survive.
    """
    if not name:
        return fallback
    # Drop any directory part, for both Windows and POSIX separators.
    base = re.split(r"[\\/]", str(name))[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_-")
    stem = stem[:max_length].rstrip("_-")
    if not stem or stem.upper() in _WINDOWS_RESERVED:
        return fallback
    return stem


def check_extension(filename: str) -> str:
    """Return the lowercase extension, or raise a friendly error."""
    ext = Path(str(filename)).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(e.lstrip(".").upper() for e in ALLOWED_EXTENSIONS)
        shown = ext.lstrip(".").upper() or "no extension"
        raise ValidationError(f"Files of type {shown} are not supported. Supported formats: {allowed}.")
    return ext


def clean_user_path(text: str) -> Path:
    """Normalise a path the user typed or pasted (e.g. from Explorer's 'Copy as path')."""
    if text is None:
        raise ValidationError("Please enter a path.")
    s = str(text).strip().strip('"').strip("'").strip()
    if not s:
        raise ValidationError("Please enter a path.")
    if "\x00" in s:
        raise ValidationError("That path contains invalid characters.")
    p = Path(s).expanduser()
    if not p.is_absolute():
        raise ValidationError(
            r"Please enter a full path, for example C:\Users\you\Videos\holiday.mp4"
        )
    return p


def resolve_local_video(text: str) -> Path:
    """Validate a path to an existing local video file (read-only use)."""
    p = clean_user_path(text)
    if not p.exists():
        raise ValidationError("No file was found at that location. Check the path and try again.")
    if p.is_dir():
        raise ValidationError("That is a folder, not a video file.")
    if not p.is_file():
        raise ValidationError("That path is not a regular file.")
    check_extension(p.name)
    return p.resolve()
