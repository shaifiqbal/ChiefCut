"""On-device speech transcription (faster-whisper) and sentence-aware clip picking.

Privacy: audio is transcribed on this PC. The only network access is the
one-time download of the speech model from Hugging Face (no audio is sent).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .planner import Candidate, pick_non_overlapping
from .probe import VideoInfo
from .storage import app_data_dir
from .tools import CREATIONFLAGS, find_tool

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # harmless Windows warning

MODELS = {
    # key: (label, approx download size)
    "tiny": ("Tiny — fastest, least accurate", "75 MB"),
    "base": ("Base — good balance (recommended)", "145 MB"),
    "small": ("Small — most accurate, ~3x slower", "480 MB"),
}
CHUNK_SECONDS = 600     # decode/transcribe 10 minutes at a time (keeps RAM low)
_SR = 16000
TRANSCRIPT_VERSION = 1

Progress = Callable[[float, str], None] | None


class TranscriptionError(RuntimeError):
    pass


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: int = 0


@dataclass
class Transcript:
    language: str
    duration: float
    model: str
    words: list[Word] = field(default_factory=list)

    def sentences(self) -> list[Sentence]:
        return split_sentences(self.words)

    def to_json(self) -> str:
        return json.dumps({"v": TRANSCRIPT_VERSION, "language": self.language, "duration": self.duration,
                           "model": self.model, "words": [asdict(w) for w in self.words]})

    @classmethod
    def from_json(cls, text: str) -> "Transcript":
        d = json.loads(text)
        if d.get("v") != TRANSCRIPT_VERSION:
            raise ValueError("old transcript format")
        return cls(d["language"], d["duration"], d["model"], [Word(**w) for w in d["words"]])


# --------------------------------------------------------------------------
# Availability / model
# --------------------------------------------------------------------------
def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def models_dir() -> Path:
    override = os.environ.get("CHIEFCUT_MODELS_DIR")
    d = Path(override) if override else app_data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_is_downloaded(size: str) -> bool:
    d = models_dir()
    return any(d.glob(f"models--Systran--faster-whisper-{size}/snapshots/*/model.bin"))


_model_cache: dict[str, object] = {}
_model_lock = threading.Lock()


def load_model(size: str):
    if size not in MODELS:
        raise TranscriptionError(f"Unknown model size: {size}")
    with _model_lock:
        if size in _model_cache:
            return _model_cache[size]
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # pragma: no cover - depends on install
            raise TranscriptionError(
                "The speech AI package is not installed. Close ChiefCut and run run_chiefcut.bat again "
                "to install it."
            ) from exc
        threads = max(1, min(4, os.cpu_count() or 2))
        try:
            model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=threads,
                                 download_root=str(models_dir()))
        except Exception as exc:
            raise TranscriptionError(
                "The speech model could not be loaded or downloaded. The first use needs an internet "
                f"connection to download it ({MODELS[size][1]}). Details: {exc}"
            ) from exc
        _model_cache.clear()        # keep only one model in RAM
        _model_cache[size] = model
        return model


# --------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------
def _cache_file(path: Path, size: str) -> Path:
    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{size}".encode()).hexdigest()[:24]
    d = app_data_dir() / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def cached_transcript(path: Path, size: str) -> Transcript | None:
    f = _cache_file(path, size)
    try:
        return Transcript.from_json(f.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _decode_chunk(path: Path, start: float, length: float) -> np.ndarray:
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path),
           "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(_SR), "-f", "f32le", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, shell=False, stdin=subprocess.DEVNULL,
                         creationflags=CREATIONFLAGS, timeout=1800)
    if out.returncode != 0:
        raise TranscriptionError("FFmpeg could not read the audio track for transcription.")
    return np.frombuffer(out.stdout, dtype=np.float32)


def transcribe(path: Path, info: VideoInfo, size: str = "base", language: str | None = None,
               on_progress: Progress = None, cancel: threading.Event | None = None,
               use_cache: bool = True) -> Transcript:
    if not info.has_audio:
        raise TranscriptionError("This video has no audio track, so there is no speech to transcribe.")
    path = Path(path)
    if use_cache:
        hit = cached_transcript(path, size)
        if hit is not None:
            if on_progress:
                on_progress(1.0, "Loaded saved transcript")
            return hit

    if on_progress:
        msg = "Loading speech model…" if model_is_downloaded(size) else \
              f"Downloading speech model ({MODELS[size][1]}, first time only)…"
        on_progress(0.0, msg)
    model = load_model(size)

    words: list[Word] = []
    detected = language or ""
    total = info.duration
    t0 = 0.0
    while t0 < total - 0.05:
        if cancel is not None and cancel.is_set():
            raise TranscriptionError("Transcription cancelled.")
        length = min(CHUNK_SECONDS, total - t0)
        audio = _decode_chunk(path, t0, length)
        if audio.size < _SR // 2:
            t0 += length
            continue
        segments, tinfo = model.transcribe(
            audio, language=detected or None, word_timestamps=True, vad_filter=True,
            beam_size=1, condition_on_previous_text=False,
        )
        if not detected:
            detected = tinfo.language
        for seg in segments:
            if cancel is not None and cancel.is_set():
                raise TranscriptionError("Transcription cancelled.")
            for w in seg.words or []:
                text = w.word.strip()
                if text:
                    words.append(Word(round(t0 + w.start, 2), round(t0 + w.end, 2), text))
            if on_progress:
                on_progress(min(0.99, (t0 + seg.end) / total), "Transcribing speech…")
        del audio
        t0 += length

    tr = Transcript(detected or "unknown", total, size, words)
    try:
        _cache_file(path, size).write_text(tr.to_json(), encoding="utf-8")
    except OSError:
        pass
    if on_progress:
        on_progress(1.0, "Transcription complete")
    return tr


# --------------------------------------------------------------------------
# Sentences & speech-aware clip picking
# --------------------------------------------------------------------------
_END = re.compile(r"[.!?…]['\")\]]*$")


def split_sentences(words: list[Word], max_gap: float = 1.2, max_words: int = 40) -> list[Sentence]:
    """Group words into sentences using punctuation, long pauses and a length cap."""
    out: list[Sentence] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        if cur and (w.start - cur[-1].end > max_gap):
            out.append(_mk(cur))
            cur = []
        cur.append(w)
        if _END.search(w.text) or len(cur) >= max_words:
            out.append(_mk(cur))
            cur = []
    if cur:
        out.append(_mk(cur))
    return out


def _mk(ws: list[Word]) -> Sentence:
    return Sentence(ws[0].start, ws[-1].end, " ".join(w.text for w in ws).strip(), len(ws))


_HOOK_WORDS = {
    "you", "your", "secret", "never", "always", "why", "how", "what", "mistake", "best", "worst",
    "truth", "stop", "need", "nobody", "everyone", "money", "free", "first", "biggest", "simple",
    "crazy", "insane", "important", "actually", "story", "tip", "hack", "wrong", "here's",
}
_WEAK_START = {"and", "but", "so", "because", "or", "then", "also", "which", "that", "um", "uh"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def speech_candidates(tr: Transcript, target: float, count: int,
                      loudness: np.ndarray | None = None, loud_bin: float = 0.5) -> list[Candidate]:
    """Clips that start and end on sentence boundaries, scored for hooks and density."""
    sents = tr.sentences()
    if not sents:
        return []
    lo_len, hi_len = target * 0.75, target * 1.3
    dur = tr.duration
    cands: list[Candidate] = []
    for i, first in enumerate(sents):
        j = i
        while j + 1 < len(sents) and sents[j + 1].end - first.start <= hi_len:
            j += 1
            if sents[j].end - first.start >= target:
                break
        length = sents[j].end - first.start
        if length < lo_len or length > hi_len:
            continue
        chosen = sents[i:j + 1]
        text = " ".join(s.text for s in chosen)
        first_toks = _tokens(first.text)
        n_words = sum(s.words for s in chosen)
        speech_time = sum(s.end - s.start for s in chosen)
        wps = n_words / max(length, 1e-6)
        gaps = [chosen[k + 1].start - chosen[k].end for k in range(len(chosen) - 1)]
        long_pauses = sum(1 for g in gaps if g > 1.5)

        density = min(wps / 3.0, 1.0)
        coverage = min(speech_time / length, 1.0)
        hook = 0.0
        reasons: list[str] = []
        if first.text.rstrip().endswith("?"):
            hook += 0.5
            reasons.append("Opens with a question")
        if any(t in _HOOK_WORDS for t in first_toks[:12]):
            hook += 0.3
            reasons.append("Hook words in the first line")
        if first_toks and first_toks[0].isdigit():
            hook += 0.2
        if first.words <= 12:
            hook += 0.15
        if first_toks and first_toks[0] in _WEAK_START:
            hook -= 0.5          # starts mid-thought
        complete = bool(_END.search(chosen[-1].text))
        excitement = min(text.count("!") * 0.15 + text.count("?") * 0.08, 0.4)
        energy = 0.0
        if loudness is not None and loudness.size:
            a, b = int(first.start / loud_bin), int(chosen[-1].end / loud_bin)
            seg = loudness[a:max(a + 1, b)]
            if seg.size:
                med = float(np.median(loudness[loudness > -50])) if np.any(loudness > -50) else -30.0
                energy = float(np.clip((np.percentile(seg, 80) - med) / 10.0, -0.5, 0.5))

        score = (0.30 * hook + 0.25 * density + 0.15 * coverage + 0.10 * (1 if complete else 0)
                 + 0.10 * excitement + 0.10 * energy - 0.15 * long_pauses)
        if complete:
            reasons.append("Ends on a complete sentence")
        if density > 0.8:
            reasons.append("Fast, dense speech")
        if energy > 0.25:
            reasons.append("Lively delivery")

        start = max(0.0, first.start - 0.2)
        if i > 0:
            start = max(start, sents[i - 1].end)          # never include the previous word
        end = min(dur, chosen[-1].end + 0.35)
        if j + 1 < len(sents):
            end = min(end, sents[j + 1].start)
        excerpt = text if len(text) <= 280 else text[:277].rstrip() + "…"
        cands.append(Candidate(round(start, 2), round(end, 2), score, reasons, excerpt, source="speech"))

    picked = pick_non_overlapping(cands, count, min_gap=0.0)
    if picked:
        lo, hi = min(c.score for c in cands), max(c.score for c in cands)
        for c in picked:
            c.score = round(100 * (c.score - lo) / (hi - lo), 0) if hi > lo else 50.0
    return picked


def transcript_for_prompt(tr: Transcript) -> str:
    """Compact, timestamped sentence list for an AI judge."""
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in tr.sentences())
