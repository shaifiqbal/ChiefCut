"""Output folders, unique filenames, temporary storage and settings."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from .validation import ValidationError, clean_user_path, safe_stem

OUTPUT_FOLDER_NAME = "ChiefCut_Output"
TEMP_PREFIX = "chiefcut_"
TEMP_MARKER = ".chiefcut-temp"
COPY_CHUNK = 4 * 1024 * 1024  # 4 MB


# --------------------------------------------------------------------------
# Well-known folders
# --------------------------------------------------------------------------
def _windows_known_folder(guid: str) -> Path | None:
    """Ask Windows where a known folder really is (handles OneDrive/redirection)."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        fid = uuid.UUID(guid)
        buf = (ctypes.c_char * 16).from_buffer_copy(fid.bytes_le)  # GUID memory layout
        out = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(buf), 0, None, ctypes.byref(out))
        if hr != 0 or not out.value:
            return None
        value = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        return Path(value)
    except Exception:
        return None


def documents_dir() -> Path | None:
    p = _windows_known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7")  # FOLDERID_Documents
    if p and p.is_dir():
        return p
    home = os.environ.get("USERPROFILE") or str(Path.home())
    p = Path(home) / "Documents"
    return p if p.is_dir() else None


def videos_dir() -> Path:
    p = _windows_known_folder("18989B1D-99B5-455B-841C-AB7C74E4DDFC")  # FOLDERID_Videos
    if p and p.is_dir():
        return p
    for name in ("Videos", "Movies"):
        cand = Path.home() / name
        if cand.is_dir():
            return cand
    return Path.home()


def default_output_dir() -> Path:
    """%USERPROFILE%\\Documents\\ChiefCut_Output, with sensible fallbacks."""
    override = os.environ.get("CHIEFCUT_OUTPUT_DIR")
    if override:
        return Path(override)
    docs = documents_dir()
    if docs is not None:
        return docs / OUTPUT_FOLDER_NAME
    return Path.home() / OUTPUT_FOLDER_NAME


def app_data_dir() -> Path:
    """Where small settings/history files live (never inside the output folder)."""
    override = os.environ.get("CHIEFCUT_DATA_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / "ChiefCut"
    else:
        base = Path.home() / ".config" / "chiefcut"
    base.mkdir(parents=True, exist_ok=True)
    return base


def ensure_writable_dir(path: str | Path) -> Path:
    """Create the folder if needed and confirm we can write to it."""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValidationError(f"ChiefCut could not create the output folder ({exc.strerror or exc}).") from None
    if not p.is_dir():
        raise ValidationError("The output location exists but is not a folder.")
    probe_file = p / f".chiefcut-write-test-{uuid.uuid4().hex}"
    try:
        with open(probe_file, "xb") as fh:
            fh.write(b"ok")
    except OSError:
        raise ValidationError("ChiefCut cannot write to that folder. Choose a different output folder.") from None
    finally:
        try:
            probe_file.unlink()
        except OSError:
            pass
    return p.resolve()


def validate_output_dir_input(text: str) -> Path:
    return ensure_writable_dir(clean_user_path(text))


def free_space_bytes(path: str | Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


# --------------------------------------------------------------------------
# Output filenames
# --------------------------------------------------------------------------
def build_output_name(source_name: str, tag: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    stem = safe_stem(source_name, max_length=40, fallback="video")
    tag = safe_stem(tag, max_length=30, fallback="clip")
    return f"{stem}_{tag}_{now:%Y%m%d-%H%M%S}.mp4"


def unique_path(directory: Path, filename: str) -> Path:
    """Return directory/filename, or directory/filename_2 ... if taken."""
    base = Path(filename).stem
    suffix = Path(filename).suffix or ".mp4"
    candidate = directory / f"{base}{suffix}"
    n = 2
    while candidate.exists() or (directory / f"{candidate.name}.part.mp4").exists():
        candidate = directory / f"{base}_{n}{suffix}"
        n += 1
        if n > 10_000:
            raise RuntimeError("Could not find a free output filename.")
    return candidate


def finalize_no_overwrite(temp_file: Path, wanted: Path) -> Path:
    """Move a finished file into place without ever replacing an existing file.

    Uses a hard link (atomic, fails if the target exists) and falls back to a
    careful rename on drives that do not support links (e.g. FAT/exFAT USB).
    """
    target = wanted
    for _ in range(1000):
        try:
            os.link(temp_file, target)
        except FileExistsError:
            target = unique_path(target.parent, wanted.name)
            if target.exists():
                target = target.with_name(f"{target.stem}_{uuid.uuid4().hex[:6]}{target.suffix}")
            continue
        except OSError:
            if target.exists():
                target = unique_path(target.parent, wanted.name)
                continue
            # On Windows os.rename refuses to replace an existing file.
            os.rename(temp_file, target)
            return target
        temp_file.unlink()
        return target
    raise RuntimeError("Could not save the output file without overwriting.")


# --------------------------------------------------------------------------
# Temporary storage for uploads
# --------------------------------------------------------------------------
def temp_root() -> Path:
    return Path(tempfile.gettempdir())


def create_temp_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=temp_root()))
    (d / TEMP_MARKER).write_text(str(time.time()), encoding="utf-8")
    return d


def is_chiefcut_temp_dir(path: Path) -> bool:
    try:
        return (
            path.is_dir()
            and path.name.startswith(TEMP_PREFIX)
            and (path / TEMP_MARKER).is_file()
            and path.resolve().parent == temp_root().resolve()
        )
    except OSError:
        return False


def remove_temp_dir(path: Path | None) -> bool:
    """Delete a ChiefCut temp dir. Refuses to touch anything else."""
    if path is None:
        return False
    path = Path(path)
    if not is_chiefcut_temp_dir(path):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def cleanup_stale_temp_dirs(max_age_hours: float = 12) -> int:
    """Remove ChiefCut temp folders left behind by a crash or forced shutdown."""
    removed = 0
    cutoff = time.time() - max_age_hours * 3600
    try:
        entries = list(temp_root().glob(f"{TEMP_PREFIX}*"))
    except OSError:
        return 0
    for d in entries:
        try:
            if is_chiefcut_temp_dir(d) and d.stat().st_mtime < cutoff:
                if remove_temp_dir(d):
                    removed += 1
        except OSError:
            continue
    return removed


def save_stream_to_temp(stream: BinaryIO, original_name: str, temp_dir: Path) -> Path:
    """Copy an uploaded file to temp storage in chunks, using a safe name."""
    ext = Path(original_name).suffix.lower()
    dest = Path(temp_dir) / f"source_{safe_stem(original_name, 40, 'video')}{ext}"
    with open(dest, "xb") as out:
        shutil.copyfileobj(stream, out, length=COPY_CHUNK)
    return dest


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
_SETTINGS_FILE = "settings.json"


def load_settings() -> dict:
    p = app_data_dir() / _SETTINGS_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict) -> None:
    p = app_data_dir() / _SETTINGS_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def human_size(num: float | None) -> str:
    if num is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"
