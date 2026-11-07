"""A small 'recent clips' list stored as JSON in the app data folder."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from .storage import app_data_dir

MAX_ENTRIES = 100
_lock = threading.Lock()


def _file() -> Path:
    return app_data_dir() / "history.json"


def load(include_missing: bool = False) -> list[dict]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    entries = [e for e in data if isinstance(e, dict) and "path" in e]
    if not include_missing:
        entries = [e for e in entries if Path(e["path"]).is_file()]
    return entries


def _write(entries: list[dict]) -> None:
    f = _file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries[:MAX_ENTRIES], indent=2), encoding="utf-8")
    os.replace(tmp, f)


def add(entry: dict) -> None:
    entry = {**entry, "created": entry.get("created") or datetime.now().isoformat(timespec="seconds")}
    with _lock:
        entries = load(include_missing=True)
        entries.insert(0, entry)
        _write(entries)


def remove(path: str) -> None:
    """Forget a clip. The video file itself is NOT deleted."""
    with _lock:
        _write([e for e in load(include_missing=True) if e.get("path") != path])
