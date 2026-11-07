import pytest

from chiefcut.validation import (
    ValidationError,
    check_extension,
    clean_user_path,
    format_timestamp,
    parse_timestamp,
    resolve_local_video,
    safe_stem,
    validate_clip_range,
    validate_duration,
)


@pytest.mark.parametrize("text,expected", [
    ("0", 0.0), ("75", 75.0), ("75.5", 75.5), (" 12 ", 12.0),
    ("1:15", 75.0), ("01:15", 75.0), ("0:01:15", 75.0), ("1:00:00", 3600.0),
    ("0:00:10.25", 10.25), ("10:00", 600.0), ("90:00", 5400.0), ("1.5", 1.5),
    (30, 30.0), (12.5, 12.5),
])
def test_parse_timestamp_valid(text, expected):
    assert parse_timestamp(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "", "   ", None, "-5", "abc", "1:2:3:4", "1:75", "1:60:00", "1:05:61", "1.5:30",
    "1e3", "12s", "1;30", "0x10", "1:", ":30", "nan", "inf", "x" * 30, "; rm -rf /",
])
def test_parse_timestamp_invalid_gives_message(text):
    with pytest.raises(ValidationError) as exc:
        parse_timestamp(text)
    assert len(str(exc.value)) > 10  # a useful, human-readable message


def test_parse_timestamp_rejects_bool_and_negative_numbers():
    with pytest.raises(ValidationError):
        parse_timestamp(True)
    with pytest.raises(ValidationError):
        parse_timestamp(-1.0)
    with pytest.raises(ValidationError):
        parse_timestamp(float("nan"))


def test_format_timestamp():
    assert format_timestamp(75) == "1:15"
    assert format_timestamp(3725) == "1:02:05"
    assert format_timestamp(10.4, precise=True) == "0:10.4"
    assert format_timestamp(59.96, precise=True) == "1:00.0"


def test_clip_range_ok():
    validate_clip_range(0, 20, 45)
    validate_clip_range(25, 20, 45)          # ends exactly at the end
    validate_clip_range(25.02, 20, 45)       # within encoder tolerance


def test_clip_range_past_end_explains_fix():
    with pytest.raises(ValidationError) as exc:
        validate_clip_range(30, 20, 45)
    msg = str(exc.value)
    assert "0:45" in msg and "0:25" in msg and "15s or less" in msg


def test_clip_range_start_after_end():
    with pytest.raises(ValidationError, match="at or after the end"):
        validate_clip_range(50, 20, 45)


def test_clip_longer_than_source():
    with pytest.raises(ValidationError, match="only"):
        validate_clip_range(0, 60, 45)


@pytest.mark.parametrize("d", [0, 0.5, -3, 601, "abc", None, float("nan")])
def test_bad_durations(d):
    with pytest.raises(ValidationError):
        validate_duration(d)


@pytest.mark.parametrize("name,expected", [
    ("My Holiday Video.mp4", "My_Holiday_Video"),
    ("..\\..\\Windows\\System32\\evil.mp4", "evil"),
    ("../../etc/passwd", "passwd"),
    ("CON.mp4", "clip"),
    ("café déjà vu.mov", "cafe_deja_vu"),
    ("<script>alert(1)</script>.mp4", "script"),
    ("", "clip"),
    ("....mp4", "clip"),
    ("a" * 200 + ".mp4", "a" * 50),
])
def test_safe_stem(name, expected):
    assert safe_stem(name) == expected


@pytest.mark.parametrize("name", ["a.mp4", "B.MOV", "c.webm", "d.mkv", "e.m4v", "f.avi"])
def test_allowed_extensions(name):
    check_extension(name)


@pytest.mark.parametrize("name", ["a.exe", "b.png", "c", "d.mp4.bat", "e.txt"])
def test_rejected_extensions(name):
    with pytest.raises(ValidationError, match="not supported"):
        check_extension(name)


def test_clean_user_path_strips_quotes(tmp_path):
    assert clean_user_path(f'  "{tmp_path}"  ') == tmp_path


def test_clean_user_path_rejects_relative_and_nul():
    with pytest.raises(ValidationError, match="full path"):
        clean_user_path("videos/clip.mp4")
    with pytest.raises(ValidationError):
        clean_user_path("/tmp/a\x00b.mp4")


def test_resolve_local_video(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"0")
    assert resolve_local_video(str(f)) == f.resolve()
    with pytest.raises(ValidationError, match="No file"):
        resolve_local_video(str(tmp_path / "missing.mp4"))
    with pytest.raises(ValidationError, match="folder"):
        resolve_local_video(str(tmp_path))
    bad = tmp_path / "x.exe"
    bad.write_bytes(b"0")
    with pytest.raises(ValidationError, match="not supported"):
        resolve_local_video(str(bad))
