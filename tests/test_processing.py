"""End-to-end FFmpeg tests: these really encode video files."""

import threading

import pytest

from chiefcut.probe import probe
from chiefcut.processing import (
    AspectMode,
    ClipRequest,
    ProcessingError,
    build_command,
    create_clip,
    frame_preview,
    output_dimensions,
    vertical_crop_box,
)
from chiefcut.tools import find_tool, run
from chiefcut.validation import ValidationError

from .conftest import sha256


def decodes_cleanly(path) -> bool:
    """Fully decode every frame; any error means the file would not play properly."""
    r = run([find_tool("ffmpeg"), "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], timeout=120)
    return r.returncode == 0 and not r.stderr.strip()


def no_partials(directory) -> bool:
    return not list(directory.glob("*.part.mp4"))


# ---------------------------------------------------------------- geometry
@pytest.mark.parametrize("w,h,expected", [
    (1920, 1080, (656, 0, 608, 1080)),
    (1080, 1920, (0, 0, 1080, 1920)),
    (1000, 1000, (219, 0, 562, 1000)),
    (720, 1600, (0, 160, 720, 1280)),   # taller than 9:16 -> crop top/bottom
])
def test_vertical_crop_box(w, h, expected):
    x, y, cw, ch = vertical_crop_box(w, h)
    assert (x, y, cw, ch) == expected
    assert abs(cw / ch - 9 / 16) < 0.01


def test_command_is_argument_list_without_shell(media, out_dir):
    info = probe(media["landscape"])
    cmd = build_command(ClipRequest(media["landscape"], 1, 20), info, out_dir / "x.mp4")
    assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)
    assert "-n" in cmd                     # never overwrite
    assert str(media["landscape"]) in cmd  # path with spaces passed as ONE argument


# ---------------------------------------------------------------- exports
def test_20s_vertical_1080x1920_with_audio(media, out_dir):
    src = media["landscape"]
    before = sha256(src)
    progress = []
    res = create_clip(ClipRequest(src, 10, 20, AspectMode.VERTICAL, "1080x1920", "fast"),
                      out_dir, "test_vertical.mp4", on_progress=progress.append)
    out = probe(res.path)
    assert res.path.suffix == ".mp4" and res.path.parent == out_dir
    assert (out.display_width, out.display_height) == (1080, 1920)
    assert out.duration == pytest.approx(20, abs=0.15)
    assert out.video_codec == "h264" and out.extra["pix_fmt"] == "yuv420p"
    assert out.has_audio and out.audio_codec == "aac"
    assert progress and progress[-1] == 1.0 and all(0 <= p <= 1 for p in progress)
    assert decodes_cleanly(res.path)
    assert sha256(src) == before          # source untouched
    assert no_partials(out_dir)


def test_original_aspect_export(media, out_dir):
    res = create_clip(ClipRequest(media["landscape"], 0, 5, AspectMode.ORIGINAL, "source", "fast"),
                      out_dir, "orig.mp4")
    out = probe(res.path)
    assert (out.display_width, out.display_height) == (1920, 1080)
    assert out.duration == pytest.approx(5, abs=0.15)
    assert decodes_cleanly(res.path)


def test_original_aspect_downscaled_720p(media, out_dir):
    res = create_clip(ClipRequest(media["landscape"], 0, 3, AspectMode.ORIGINAL, "720p", "fast"),
                      out_dir, "orig720.mp4")
    assert (res.width, res.height) == (1280, 720)


def test_vertical_720_option(media, out_dir):
    res = create_clip(ClipRequest(media["landscape"], 0, 3, AspectMode.VERTICAL, "720x1280", "fast"),
                      out_dir, "v720.mp4")
    assert (res.width, res.height) == (720, 1280)


def test_video_without_audio(media, out_dir):
    res = create_clip(ClipRequest(media["silent"], 2, 5, AspectMode.VERTICAL, "720x1280", "fast"),
                      out_dir, "silent.mp4")
    out = probe(res.path)
    assert out.has_audio is False
    assert out.duration == pytest.approx(5, abs=0.15)
    assert decodes_cleanly(res.path)


def test_rotated_source_comes_out_upright(media, out_dir):
    res = create_clip(ClipRequest(media["rotated"], 0, 4, AspectMode.ORIGINAL, "source", "fast"),
                      out_dir, "rot.mp4")
    out = probe(res.path)
    assert (out.display_width, out.display_height) == (720, 1280)
    assert out.rotation == 0   # pixels physically rotated, no flag left to confuse players
    assert out.has_audio


@pytest.mark.parametrize("key", ["webm", "mov"])
def test_webm_and_mov_sources(media, out_dir, key):
    res = create_clip(ClipRequest(media[key], 0, 3, AspectMode.VERTICAL, "720x1280", "fast"),
                      out_dir, f"{key}.mp4")
    assert probe(res.path).video_codec == "h264"


def test_existing_outputs_are_never_overwritten(media, out_dir):
    req = ClipRequest(media["silent"], 0, 2, AspectMode.VERTICAL, "720x1280", "fast")
    existing = out_dir / "same.mp4"
    existing.write_bytes(b"precious user file")
    first = create_clip(req, out_dir, "same.mp4")
    second = create_clip(req, out_dir, "same.mp4")
    assert existing.read_bytes() == b"precious user file"
    assert len({existing, first.path, second.path}) == 3
    assert first.path.name == "same_2.mp4" and second.path.name == "same_3.mp4"


def test_clip_past_end_rejected_before_encoding(media, out_dir):
    with pytest.raises(ValidationError, match="would end at"):
        create_clip(ClipRequest(media["landscape"], 40, 20), out_dir, "nope.mp4")
    assert list(out_dir.iterdir()) == []


def test_cancel_removes_partial_file(media, out_dir):
    ev = threading.Event()
    ev.set()
    with pytest.raises(ProcessingError, match="cancelled"):
        create_clip(ClipRequest(media["landscape"], 0, 20, quality="standard"), out_dir, "c.mp4", cancel_event=ev)
    assert list(out_dir.iterdir()) == []


def test_missing_source(tmp_path, out_dir):
    with pytest.raises(ValidationError, match="no longer available"):
        create_clip(ClipRequest(tmp_path / "gone.mp4", 0, 5), out_dir, "x.mp4")


def test_output_dimensions_are_even(media):
    info = probe(media["mov"])
    for size in ("source", "1080p", "720p"):
        w, h = output_dimensions(info, AspectMode.ORIGINAL, size)
        assert w % 2 == 0 and h % 2 == 0


def test_frame_preview_jpeg(media):
    info = probe(media["landscape"])
    for mode in AspectMode:
        jpg = frame_preview(media["landscape"], info, 5, mode)
        assert jpg and jpg[:2] == b"\xff\xd8"
