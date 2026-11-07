"""Drives the real Streamlit app script headlessly (no browser needed)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from chiefcut import history
from chiefcut.probe import probe

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def make_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    assert not at.exception, at.exception
    return at


def test_app_starts_and_all_pages_render():
    at = make_app()
    assert any("Create a clip" in m.value for m in at.markdown)
    for page in at.sidebar.radio[0].options:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, at.exception)


def test_full_workflow_local_file(media, tmp_path):
    out = Path(tmp_path / "ChiefCut_Output")   # set by the autouse fixture
    at = make_app()
    at.text_input(key="local_path").set_value(str(media["landscape"])).run()
    [b for b in at.button if b.label == "Use this video"][0].click().run()
    assert not at.exception
    assert any("0:45.0" in m.value for m in at.markdown)      # duration shown

    at.segmented_control(key="dur_preset").set_value("20s").run()
    at.text_input(key="start_text").set_value("0:10").run()
    at.selectbox(key="size_v").set_value("720x1280").run()
    at.selectbox(key="quality").set_value("fast").run()
    assert not at.error, [e.value for e in at.error]

    [b for b in at.button if "Create clip" in b.label][0].click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]

    clips = list(out.glob("*.mp4"))
    assert len(clips) == 1
    info = probe(clips[0])
    assert (info.display_width, info.display_height) == (720, 1280)
    assert info.duration == pytest.approx(20, abs=0.15)
    assert history.load()[0]["path"] == str(clips[0])
    assert any("Your clip is ready" in m.value for m in at.markdown)


def test_invalid_timestamp_shows_error_and_disables_export(media):
    at = make_app()
    at.text_input(key="local_path").set_value(str(media["silent"])).run()
    [b for b in at.button if b.label == "Use this video"][0].click().run()
    at.text_input(key="start_text").set_value("1:75").run()
    assert any("seconds part must be below 60" in e.value for e in at.error)
    at.text_input(key="start_text").set_value("0:05").run()      # 30s clip from 5s in a 12s video
    assert any("Please adjust the clip" in e.value for e in at.error)
    create = [b for b in at.button if "Create clip" in b.label][0]
    assert create.disabled


def test_unsupported_local_file_rejected(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    at = make_app()
    at.text_input(key="local_path").set_value(str(bad)).run()
    [b for b in at.button if b.label == "Use this video"][0].click().run()
    assert any("not supported" in e.value for e in at.error)


def test_fake_video_rejected(media):
    at = make_app()
    at.text_input(key="local_path").set_value(str(media["garbage"])).run()
    [b for b in at.button if b.label == "Use this video"][0].click().run()
    assert any("can't be used" in e.value for e in at.error)
