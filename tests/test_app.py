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


# ---------------------------------------------------------------- auto clips (v0.2)
def load(at, path):
    at.text_input(key="local_path").set_value(str(path)).run()
    [b for b in at.button if b.label == "Use this video"][0].click().run()
    assert not at.exception
    at.segmented_control(key="mode").set_value("✨ Several clips (auto)").run()
    assert not at.exception, at.exception


def click(at, text):
    [b for b in at.button if text in b.label][0].click().run()
    assert not at.exception, at.exception


def test_auto_even_clips_with_look_export(media, tmp_path):
    out = tmp_path / "ChiefCut_Output"
    at = make_app()
    load(at, media["landscape"])
    at.radio(key="auto_method").set_value("even").run()
    at.segmented_control(key="auto_dur_preset").set_value("20s").run()
    at.number_input(key="auto_count").set_value(2).run()
    at.selectbox(key="auto_size_v").set_value("720x1280").run()
    at.selectbox(key="auto_quality").set_value("fast").run()
    at.selectbox(key="auto_look").set_value("comic").run()
    click(at, "Find clips")
    assert any("Review 2 suggested" in m.value for m in at.markdown)
    at.checkbox(key="auto_inc_1").uncheck().run()          # export only the first suggestion
    click(at, "Export 1 clip")
    clips = sorted(out.glob("*.mp4"))
    assert len(clips) == 1 and "comic" in clips[0].name
    info = probe(clips[0])
    assert (info.display_width, info.display_height) == (720, 1280)
    assert info.duration == pytest.approx(20, abs=0.15)


def test_auto_smart_highlights_finds_loud_part(media2, tmp_path):
    at = make_app()
    load(at, media2["energy"])
    at.radio(key="auto_method").set_value("smart").run()
    at.segmented_control(key="auto_dur_preset").set_value("Custom").run()
    at.number_input(key="auto_dur_custom").set_value(10.0).run()
    at.number_input(key="auto_count").set_value(2).run()
    click(at, "Find clips")
    assert not at.error, [e.value for e in at.error]
    labels = [c.label for c in at.checkbox if c.key and c.key.startswith("auto_inc_")]
    assert len(labels) == 2
    best = [lbl for lbl in labels if "score 100/100" in lbl]
    assert best and "· 1:0" in best[0]       # the top clip starts around 1:01-1:05 (the loud section)


def test_auto_too_many_clips_capped(media):
    at = make_app()
    load(at, media["silent"])                  # 12s video
    at.segmented_control(key="auto_dur_preset").set_value("Custom").run()
    at.number_input(key="auto_dur_custom").set_value(5.0).run()
    assert at.number_input(key="auto_count").max == 2


def test_auto_speech_disabled_without_audio(media):
    at = make_app()
    load(at, media["silent"])
    at.radio(key="auto_method").set_value("speech").run()
    assert any("no audio track" in e.value for e in at.error)
    assert [b for b in at.button if "Find clips" in b.label][0].disabled


def test_settings_api_key_save_and_delete():
    at = make_app()
    at.sidebar.radio[0].set_value("⚙️  Settings").run()
    at.text_input(key="api_key_input").set_value("not a key").run()
    click(at, "Save key")
    assert any("doesn't look like" in e.value for e in at.error)
    at.text_input(key="api_key_input").set_value("sk-ant-api03-" + "y" * 40).run()
    click(at, "Save key")
    assert any("saved in ChiefCut" in s.value for s in at.success)
    click(at, "Delete saved key")
    assert not any("Key set" in s.value for s in at.success)
