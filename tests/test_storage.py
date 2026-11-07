import io
from datetime import datetime

import pytest

from chiefcut import history
from chiefcut.storage import (
    build_output_name,
    cleanup_stale_temp_dirs,
    create_temp_dir,
    default_output_dir,
    ensure_writable_dir,
    finalize_no_overwrite,
    load_settings,
    remove_temp_dir,
    save_settings,
    save_stream_to_temp,
    unique_path,
)


def test_output_name_is_safe_and_unique_looking():
    name = build_output_name(r"..\..\My Video!.mov", "vertical_20s", datetime(2026, 1, 2, 3, 4, 5))
    assert name == "My_Video_vertical_20s_20260102-030405.mp4"


def test_unique_path(tmp_path):
    assert unique_path(tmp_path, "a.mp4") == tmp_path / "a.mp4"
    (tmp_path / "a.mp4").write_bytes(b"1")
    (tmp_path / "a_2.mp4").write_bytes(b"2")
    assert unique_path(tmp_path, "a.mp4") == tmp_path / "a_3.mp4"


def test_finalize_never_overwrites(tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"old")
    part = tmp_path / "clip.mp4.part.mp4"
    part.write_bytes(b"new")
    saved = finalize_no_overwrite(part, target)
    assert target.read_bytes() == b"old"
    assert saved != target and saved.read_bytes() == b"new"
    assert not part.exists()


def test_default_output_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEFCUT_OUTPUT_DIR", str(tmp_path / "x"))
    assert default_output_dir() == tmp_path / "x"


def test_default_output_dir_is_in_documents(monkeypatch, tmp_path):
    monkeypatch.delenv("CHIEFCUT_OUTPUT_DIR", raising=False)
    (tmp_path / "Documents").mkdir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert default_output_dir() == tmp_path / "Documents" / "ChiefCut_Output"


def test_default_output_dir_without_documents(monkeypatch, tmp_path):
    """If Documents is missing/redirected away, fall back to the home folder."""
    monkeypatch.delenv("CHIEFCUT_OUTPUT_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nobody"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_output_dir().name == "ChiefCut_Output"


def test_ensure_writable_dir_creates(tmp_path):
    p = ensure_writable_dir(tmp_path / "a" / "b")
    assert p.is_dir() and list(p.iterdir()) == []   # test file cleaned up


def test_temp_dir_lifecycle(tmp_path):
    d = create_temp_dir()
    f = save_stream_to_temp(io.BytesIO(b"x" * 10_000_000), "../../evil name.mp4", d)
    assert f.parent == d and f.name == "source_evil_name.mp4" and f.stat().st_size == 10_000_000
    assert remove_temp_dir(d) and not d.exists()


def test_remove_temp_dir_refuses_other_folders(tmp_path):
    victim = tmp_path / "chiefcut_important"
    victim.mkdir()
    (victim / "keep.txt").write_text("hi")
    assert remove_temp_dir(victim) is False
    assert (victim / "keep.txt").exists()


def test_stale_cleanup_only_old_dirs():
    import os
    import time
    fresh, old = create_temp_dir(), create_temp_dir()
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))
    assert cleanup_stale_temp_dirs(12) >= 1
    assert fresh.exists() and not old.exists()
    remove_temp_dir(fresh)


def test_settings_roundtrip():
    assert load_settings() == {}
    save_settings({"output_dir": "D:/Clips", "quality": "high"})
    assert load_settings()["quality"] == "high"


def test_history_skips_missing_files(tmp_path):
    real = tmp_path / "a.mp4"
    real.write_bytes(b"1")
    history.add({"path": str(real), "name": "a.mp4"})
    history.add({"path": str(tmp_path / "deleted.mp4"), "name": "deleted.mp4"})
    names = [e["name"] for e in history.load()]
    assert names == ["a.mp4"]
    history.remove(str(real))
    assert history.load() == [] and real.exists()  # removing from history keeps the file
