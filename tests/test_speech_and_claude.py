"""Sentence-aware picking, real on-PC transcription (if the model is present), and the Claude judge
with a fake client (no network, no API key, no cost)."""

import json
from types import SimpleNamespace

import pytest

from chiefcut import ai_judge
from chiefcut import transcribe as T
from chiefcut.probe import probe


def words_from(sentences, start=0.0, wps=2.5, gap=0.4):
    """Build fake word timings from a list of sentences."""
    out, t = [], start
    for sent in sentences:
        for w in sent.split():
            out.append(T.Word(round(t, 2), round(t + 1 / wps - 0.05, 2), w))
            t += 1 / wps
        t += gap
    return out


STORY = [
    "So anyway that was the weather.",                      # weak start
    "Why do most people fail at saving money?",             # hook question
    "They never make a plan.",
    "Here is the simple fix that changed everything for me.",
    "Write down every single expense for one month.",
    "You will be shocked where the money really goes.",
    "And then we went to the shop and bought some bread and milk and eggs.",
    "It was a normal day really nothing much happened at all honestly.",
]


def test_split_sentences_uses_punctuation_and_pauses():
    ws = words_from(["Hello there.", "How are you?"]) + [T.Word(30.0, 30.4, "later")]
    s = T.split_sentences(ws)
    assert [x.text for x in s] == ["Hello there.", "How are you?", "later"]
    assert s[0].start == 0.0 and s[-1].start == 30.0


def test_speech_candidates_start_on_hook_and_align_to_sentences():
    tr = T.Transcript("en", 60.0, "base", words_from(STORY))
    picks = T.speech_candidates(tr, 15, 2)
    assert picks
    best = max(picks, key=lambda c: c.score)
    assert best.text.startswith("Why do most people")
    assert "Opens with a question" in best.reasons
    sents = tr.sentences()
    starts, ends = [s.start for s in sents], [s.end for s in sents]
    for c in picks:
        assert any(abs(c.start - (s - 0.2)) < 0.25 for s in starts)    # begins at a sentence start
        assert any(abs(c.end - (e + 0.35)) < 0.4 for e in ends)        # ends after a sentence end
        assert 15 * 0.75 - 0.6 <= c.duration <= 15 * 1.3 + 0.6
    s = sorted(picks, key=lambda c: c.start)
    assert all(a.end <= b.start for a, b in zip(s, s[1:]))


def test_transcript_cache_roundtrip():
    tr = T.Transcript("en", 10, "base", words_from(["One two three."]))
    assert T.Transcript.from_json(tr.to_json()).words == tr.words


def test_no_audio_rejected(media):
    with pytest.raises(T.TranscriptionError, match="no audio"):
        T.transcribe(media["silent"], probe(media["silent"]))


@pytest.mark.skipif(not (T.is_available() and T.model_is_downloaded("base")),
                    reason="speech model not downloaded on this machine (it downloads on first use in the app)")
def test_real_transcription_on_this_pc(media2):
    info = probe(media2["talk"])
    tr = T.transcribe(media2["talk"], info, "base", "en", use_cache=False)
    text = " ".join(w.text for w in tr.words).lower()
    assert tr.language == "en" and len(tr.words) > 80
    for phrase in ("welcome to the show", "why do most", "thanks for watching"):
        assert phrase in text
    picks = T.speech_candidates(tr, 15, 2)
    assert picks and all(c.text for c in picks)
    assert T.cached_transcript(media2["talk"], "base") is not None


# ---------------------------------------------------------------- Claude judge
class FakeClient:
    def __init__(self, payload=None, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        content = [SimpleNamespace(type="thinking", thinking=""),
                   SimpleNamespace(type="text", text=json.dumps(self.payload))]
        return SimpleNamespace(stop_reason=self.stop_reason, content=content)


def story_transcript():
    return T.Transcript("en", 60.0, "base", words_from(STORY))


def test_judge_sends_only_transcript_text_and_correct_settings():
    tr = story_transcript()
    s = tr.sentences()
    fake = FakeClient({"clips": [{"start": s[1].start + 0.3, "end": s[4].end - 0.2, "title": "Why you're broke",
                                  "reason": "Strong question hook.", "score": 91}]})
    picks = ai_judge.judge(tr, 12, 1, client=fake)
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    user = call["messages"][0]["content"]
    assert isinstance(user, str) and "<transcript>" in user and "Why do most people fail" in user
    (p,) = picks
    assert p.title == "Why you're broke" and p.score == 91 and p.source == "claude"
    assert p.start == pytest.approx(s[1].start - 0.2, abs=0.01)      # snapped to sentence boundary
    assert p.end == pytest.approx(s[4].end + 0.35, abs=0.01)


def test_judge_drops_bad_and_overlapping_clips():
    tr = story_transcript()
    s = tr.sentences()
    fake = FakeClient({"clips": [
        {"start": s[1].start, "end": s[4].end, "title": "A", "reason": "r", "score": 80},
        {"start": s[2].start, "end": s[5].end, "title": "overlaps A", "reason": "r", "score": 70},
        {"start": 500, "end": 520, "title": "out of range", "reason": "r", "score": 99},
        {"start": s[0].start, "end": s[0].end, "title": "too short", "reason": "r", "score": 99},
    ]})
    picks = ai_judge.judge(tr, 12, 3, client=fake)
    assert [p.title for p in picks] == ["A"]


def test_judge_refusal_and_garbage():
    tr = story_transcript()
    with pytest.raises(ai_judge.JudgeError, match="declined"):
        ai_judge.judge(tr, 12, 1, client=FakeClient({"clips": []}, stop_reason="refusal"))
    with pytest.raises(ai_judge.JudgeError, match="usable"):
        ai_judge.judge(tr, 12, 1, client=FakeClient({"clips": []}))


def test_judge_without_key_gives_clear_error():
    with pytest.raises(ai_judge.JudgeError, match="No Anthropic API key"):
        ai_judge.judge(story_transcript(), 12, 1)


def test_api_key_storage():
    with pytest.raises(ai_judge.JudgeError):
        ai_judge.save_api_key("not-a-key")
    key = "sk-ant-api03-" + "x" * 40
    ai_judge.save_api_key("  " + key + "  ")
    assert ai_judge.get_api_key() == key and ai_judge.key_source() == "saved in ChiefCut"
    assert key not in ai_judge.mask(key)
    ai_judge.delete_api_key()
    assert ai_judge.get_api_key() is None
