import pytest

from chiefcut.probe import ProbeError, probe


def test_landscape_metadata(media):
    info = probe(media["landscape"])
    assert info.duration == pytest.approx(45, abs=0.1)
    assert (info.width, info.height) == (1920, 1080)
    assert (info.display_width, info.display_height) == (1920, 1080)
    assert info.rotation == 0
    assert info.fps == pytest.approx(30)
    assert info.video_codec == "h264"
    assert info.has_audio and info.audio_codec == "aac"
    assert info.aspect_label == "16:9" and info.orientation == "Landscape"
    assert info.size_bytes > 0


def test_silent_video(media):
    info = probe(media["silent"])
    assert info.has_audio is False and info.audio_codec is None
    assert info.duration == pytest.approx(12, abs=0.1)


def test_rotated_video_reports_display_size(media):
    info = probe(media["rotated"])
    assert (info.width, info.height) == (1280, 720)       # stored
    assert info.rotation in (90, 270)
    assert (info.display_width, info.display_height) == (720, 1280)  # as shown
    assert info.orientation == "Portrait"


@pytest.mark.parametrize("key,codec", [("webm", "vp8"), ("mov", "h264")])
def test_other_containers(media, key, codec):
    info = probe(media[key])
    assert info.video_codec == codec
    assert info.duration > 1


def test_image_rejected(media):
    with pytest.raises(ProbeError, match="image"):
        probe(media["image"])


def test_garbage_rejected(media):
    with pytest.raises(ProbeError):
        probe(media["garbage"])


def test_missing_file(tmp_path):
    with pytest.raises(ProbeError, match="could not be found"):
        probe(tmp_path / "nope.mp4")
