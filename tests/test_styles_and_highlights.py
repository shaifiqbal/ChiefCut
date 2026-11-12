"""Style looks (real encodes) and smart highlights (real FFmpeg analysis)."""

import numpy as np
import pytest

from chiefcut.analysis import Analysis, analyse, audio_loudness, score_highlights
from chiefcut.probe import probe
from chiefcut.processing import AspectMode, ClipRequest, create_clip, frame_preview
from chiefcut.styles import LOOKS, get_look
from chiefcut.tools import find_tool, run

from .test_processing import decodes_cleanly


def frame_rgb(path, at=1.0):
    r = run([find_tool("ffmpeg"), "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
             "-vf", "scale=64:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, 3).astype(int)


@pytest.mark.parametrize("look", [k for k in LOOKS if k != "none"])
def test_every_look_exports_a_valid_clip(media, out_dir, look):
    res = create_clip(ClipRequest(media["landscape"], 2, 2, AspectMode.VERTICAL, "720x1280", "fast", look),
                      out_dir, f"{look}.mp4")
    info = probe(res.path)
    assert (info.display_width, info.display_height) == (720, 1280)
    assert info.duration == pytest.approx(2, abs=0.15) and info.has_audio
    assert decodes_cleanly(res.path)


def test_looks_actually_change_the_picture(media, out_dir):
    plain = create_clip(ClipRequest(media["landscape"], 2, 2, AspectMode.ORIGINAL, "720p", "fast"), out_dir, "p.mp4")
    sketch = create_clip(ClipRequest(media["landscape"], 2, 2, AspectMode.ORIGINAL, "720p", "fast", "sketch"),
                         out_dir, "s.mp4")
    noir = create_clip(ClipRequest(media["landscape"], 2, 2, AspectMode.ORIGINAL, "720p", "fast", "noir"),
                       out_dir, "n.mp4")
    a, s, n = frame_rgb(plain.path), frame_rgb(sketch.path), frame_rgb(noir.path)
    assert np.abs(a - s).mean() > 20                      # visibly different
    for img in (s, n):                                     # black & white looks have no colour
        assert np.abs(img[:, 0] - img[:, 1]).mean() < 4 and np.abs(img[:, 1] - img[:, 2]).mean() < 4
    assert np.abs(a[:, 0] - a[:, 1]).mean() > 20           # the original is colourful


@pytest.mark.parametrize("look", ["comic", "popart", "vivid", "vintage"])
def test_colour_looks_keep_colour(media, out_dir, look):
    """Regression: FFmpeg's split once forced the comic look's colour branch to grey."""
    res = create_clip(ClipRequest(media["landscape"], 2, 2, AspectMode.VERTICAL, "720x1280", "fast", look),
                      out_dir, f"{look}_c.mp4")
    img = frame_rgb(res.path)
    assert np.abs(img[:, 0] - img[:, 1]).mean() + np.abs(img[:, 1] - img[:, 2]).mean() > 30


def test_unknown_look_falls_back_to_none():
    assert get_look("does-not-exist").key == "none" and get_look(None).key == "none"


def test_framing_preview_with_look(media):
    info = probe(media["landscape"])
    jpg = frame_preview(media["landscape"], info, 3, AspectMode.VERTICAL, look="comic")
    assert jpg and jpg[:2] == b"\xff\xd8"


# ---------------------------------------------------------------- smart highlights
def test_scene_changes_and_loud_section_found(media2):
    info = probe(media2["energy"])
    steps = []
    an = analyse(media2["energy"], info, scenes=True, on_progress=lambda f, m: steps.append(f))
    assert an.scene_times is not None
    assert any(abs(t - 30) < 0.5 for t in an.scene_times) and any(abs(t - 60) < 0.5 for t in an.scene_times)
    assert len(an.scene_times) <= 4
    assert an.loudness.size == pytest.approx(180, abs=2)
    assert steps and steps[-1] == 1.0
    best = max(score_highlights(an, 10, 3), key=lambda c: c.score)
    assert 58 <= best.start <= 66 and best.score == 100      # the loud burst at 62-75s
    assert any("Loud" in r for r in best.reasons)


def test_highlights_do_not_overlap_and_fit(media2):
    info = probe(media2["energy"])
    an = analyse(media2["energy"], info, scenes=False)
    picks = score_highlights(an, 20, 4)
    assert 1 <= len(picks) <= 4
    for c in picks:
        assert 0 <= c.start and c.end <= info.duration + 0.01 and c.duration == pytest.approx(20, abs=0.01)
    s = sorted(picks, key=lambda c: c.start)
    assert all(a.end <= b.start for a, b in zip(s, s[1:]))


def test_no_audio_returns_none(media):
    assert audio_loudness(media["silent"], probe(media["silent"])) is None


def test_scene_only_scoring():
    an = Analysis(100.0, None, [10, 11, 12, 13, 70])
    best = score_highlights(an, 10, 1)[0]
    assert best.start <= 10 and best.end >= 13
