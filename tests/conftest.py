"""Shared fixtures: synthetic test videos generated with FFmpeg."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from chiefcut.tools import find_tool


DATA = Path(__file__).parent / "data"
# Reuse the user's real speech-model folder so tests don't re-download models.
REAL_MODELS_DIR = os.environ.get("CHIEFCUT_MODELS_DIR") or str(
    (Path(os.environ["APPDATA"]) / "ChiefCut" / "models") if os.name == "nt" and os.environ.get("APPDATA")
    else Path.home() / ".config" / "chiefcut" / "models"
)


def _ffmpeg(*args: str) -> None:
    subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, shell=False)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    m = {
        "landscape": d / "landscape 1080p (test).mp4",  # spaces & brackets on purpose
        "silent": d / "silent.mp4",
        "rotated": d / "phone_rotated.mp4",
        "webm": d / "short.webm",
        "mov": d / "clip.mov",
        "image": d / "picture.png",
        "garbage": d / "not_really.mp4",
    }
    # 45s 1920x1080 with a 440 Hz tone.
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=45",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=45",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(m["landscape"]))
    # 12s 1280x720, no audio.
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25:duration=12",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(m["silent"]))
    # Phone-style: stored 1280x720 landscape with a 90 degree rotation flag (displays portrait).
    tmp = d / "rot_src.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=25",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=25",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(tmp))
    _ffmpeg("-display_rotation", "90", "-i", str(tmp), "-c", "copy", str(m["rotated"]))
    # Small WebM (VP8 + Vorbis/Opus) and MOV.
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=8",
            "-f", "lavfi", "-i", "sine=duration=8",
            "-c:v", "libvpx", "-deadline", "realtime", "-b:v", "500k", "-c:a", "libopus",
            "-shortest", str(m["webm"]))
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x480:rate=30:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(m["mov"]))
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240", "-frames:v", "1", str(m["image"]))
    m["garbage"].write_bytes(b"this is definitely not a video file" * 100)
    return m


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    """Keep settings/history from tests away from the real user folders."""
    monkeypatch.setenv("CHIEFCUT_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("CHIEFCUT_OUTPUT_DIR", str(tmp_path / "ChiefCut_Output"))
    monkeypatch.setenv("CHIEFCUT_MODELS_DIR", REAL_MODELS_DIR)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


@pytest.fixture
def out_dir(tmp_path) -> Path:
    p = tmp_path / "out"
    p.mkdir()
    return p


@pytest.fixture(scope="session")
def media2(tmp_path_factory) -> dict[str, Path]:
    """Videos with known structure for the auto-clip features."""
    d = tmp_path_factory.mktemp("media2")
    m = {"energy": d / "energy.mp4", "talk": d / "talk.mp4"}
    # 90s: three different pictures (cuts at 30s and 60s), quiet tone, LOUD noise from 62s to 75s.
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=30",
            "-f", "lavfi", "-i", "smptebars=size=640x360:rate=25:duration=30",
            "-f", "lavfi", "-i", "mandelbrot=size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=90",
            "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.9:duration=13",
            "-filter_complex",
            "[0][1][2]concat=n=3:v=1:a=0,trim=duration=90[v];[3]volume=0.03[q];"
            "[4]adelay=62000|62000[n];[q][n]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "90", str(m["energy"]))
    # 75s talking video built from the bundled synthetic-speech recording.
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=75",
            "-i", str(DATA / "speech.opus"), "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-t", "75", str(m["talk"]))
    return m
