"""Smart highlights without AI models: loudness + scene changes, measured by FFmpeg.

Loudness: FFmpeg decodes the audio to 8 kHz mono and we measure the volume of
every half-second. Audio decoding is very fast, even for long videos.

Scene changes: FFmpeg's scene detector runs on a tiny, low-frame-rate copy of
the picture. This needs the whole video decoded, so it is slower and optional.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .planner import Candidate, pick_non_overlapping
from .probe import VideoInfo
from .tools import CREATIONFLAGS, find_tool
from .validation import format_timestamp

BIN = 0.5            # seconds per loudness measurement
_SR = 8000           # analysis sample rate
SILENCE_DB = -50.0

Progress = Callable[[float, str], None] | None


class AnalysisError(RuntimeError):
    pass


@dataclass
class Analysis:
    duration: float
    loudness: np.ndarray | None      # dBFS per BIN seconds
    scene_times: list[float] | None  # seconds


# --------------------------------------------------------------------------
# Measurements
# --------------------------------------------------------------------------
def audio_loudness(path: Path, info: VideoInfo, on_progress: Progress = None,
                   cancel: threading.Event | None = None) -> np.ndarray | None:
    """Loudness in dBFS for every BIN seconds, or None if there is no audio."""
    if not info.has_audio:
        return None
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", str(_SR), "-f", "s16le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, shell=False, creationflags=CREATIONFLAGS)
    bin_bytes = int(BIN * _SR) * 2
    expected = max(1, int(info.duration / BIN))
    values: list[float] = []
    leftover = b""
    try:
        assert proc.stdout is not None
        while True:
            if cancel is not None and cancel.is_set():
                raise AnalysisError("Analysis cancelled.")
            chunk = proc.stdout.read(bin_bytes * 240)  # ~2 minutes of audio at a time
            if not chunk:
                break
            data = leftover + chunk
            usable = len(data) - (len(data) % bin_bytes)
            leftover = data[usable:]
            if usable:
                samples = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0
                rms = np.sqrt(np.mean(samples.reshape(-1, bin_bytes // 2) ** 2, axis=1))
                values.extend((20 * np.log10(np.maximum(rms, 1e-5))).tolist())
            if on_progress:
                on_progress(min(1.0, len(values) / expected), "Measuring loudness…")
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if not values:
        return None
    return np.array(values, dtype=np.float32)


_PTS = re.compile(r"pts_time:([0-9.]+)")


def scene_changes(path: Path, info: VideoInfo, threshold: float = 0.32, on_progress: Progress = None,
                  cancel: threading.Event | None = None) -> list[float]:
    """Times (seconds) where the picture changes sharply, e.g. camera cuts."""
    vf = f"fps=5,scale=160:-2,select='gt(scene,{threshold:.2f})',showinfo"
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "info",
           "-threads", "0", "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
           "-vf", vf, "-f", "null", "-progress", "pipe:1", "-nostats", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, shell=False, creationflags=CREATIONFLAGS)
    times: list[float] = []

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace")
            if "Parsed_showinfo" in line:
                m = _PTS.search(line)
                if m:
                    times.append(float(m.group(1)))

    t = threading.Thread(target=_read_stderr, daemon=True)
    t.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if cancel is not None and cancel.is_set():
                raise AnalysisError("Analysis cancelled.")
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us=") and on_progress:
                try:
                    secs = int(line.split("=", 1)[1]) / 1e6
                    on_progress(min(1.0, secs / info.duration), "Detecting scene changes…")
                except ValueError:
                    pass
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t.join(timeout=5)
    return sorted(times)


def analyse(path: Path, info: VideoInfo, scenes: bool = True, on_progress: Progress = None,
            cancel: threading.Event | None = None) -> Analysis:
    """Run the measurements. Progress is split between the two passes."""
    loud_share = 0.25 if scenes else 1.0

    def p_loud(f: float, msg: str) -> None:
        if on_progress:
            on_progress(f * loud_share, msg)

    def p_scene(f: float, msg: str) -> None:
        if on_progress:
            on_progress(loud_share + f * (1 - loud_share), msg)

    loud = audio_loudness(path, info, p_loud, cancel)
    times = scene_changes(path, info, on_progress=p_scene, cancel=cancel) if scenes else None
    if on_progress:
        on_progress(1.0, "Analysis complete")
    return Analysis(info.duration, loud, times)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def _normalise(db: np.ndarray) -> np.ndarray:
    """Map loudness to a robust 'how energetic' scale: 0 = typical, 1 = loud."""
    if np.sum(db > SILENCE_DB) < 4:
        return np.zeros_like(db)
    floored = np.maximum(db, -60.0)          # treat digital silence as very quiet, not -100 dB
    med = float(np.median(floored))          # the video's typical level
    p95 = float(np.percentile(floored, 95))  # its loud moments
    spread = max(p95 - med, 3.0)
    z = (db - med) / spread
    z[db <= SILENCE_DB] = -2.0
    return np.clip(z, -2.0, 2.0)


def _quietest_before(z: np.ndarray, start_bin: int, look_back: int) -> int:
    lo = max(0, start_bin - look_back)
    if lo >= start_bin:
        return start_bin
    window = z[lo:start_bin + 1]
    return lo + int(np.argmin(window))


def score_highlights(an: Analysis, clip_duration: float, count: int) -> list[Candidate]:
    """Score every possible window and pick the best non-overlapping ones."""
    dur = an.duration
    if clip_duration > dur:
        return []
    n_bins = int(np.ceil(dur / BIN))
    win = max(1, int(round(clip_duration / BIN)))
    has_audio = an.loudness is not None and an.loudness.size > 0
    has_scenes = an.scene_times is not None

    z = np.zeros(n_bins, dtype=np.float32)
    if has_audio:
        norm = _normalise(an.loudness)
        z[: min(n_bins, norm.size)] = norm[:n_bins]

    cuts = np.zeros(n_bins, dtype=np.float32)
    if has_scenes:
        for t in an.scene_times:
            i = int(t / BIN)
            if 0 <= i < n_bins:
                cuts[i] += 1

    hook_bins = max(1, int(3 / BIN))
    step = max(1, int(1 / BIN))
    look_back = int(1.5 / BIN)
    cands: list[Candidate] = []
    last_start = n_bins - win
    for s in range(0, max(1, last_start + 1), step):
        seg = z[s:s + win]
        if seg.size == 0:
            continue
        top = np.sort(seg)[-max(1, int(seg.size * 0.4)):]
        energy = float(np.mean(top))                       # sustained loud moments
        peak = float(np.max(seg))
        hook = float(np.mean(seg[:hook_bins]))             # strong first 3 seconds
        active = float(np.mean(seg > -1.0))                # little dead air
        n_cuts = float(np.sum(cuts[s:s + win]))
        scene_score = min(n_cuts, 8.0) / 8.0

        if has_audio and has_scenes:
            score = 0.40 * energy + 0.15 * peak / 2 + 0.15 * hook + 0.15 * active + 0.15 * scene_score
        elif has_audio:
            score = 0.45 * energy + 0.20 * peak / 2 + 0.15 * hook + 0.20 * active
        elif has_scenes:
            score = scene_score
        else:
            score = 0.0

        # Start on a quiet moment just before, to avoid cutting into a word.
        start_bin = _quietest_before(z, s, look_back) if has_audio else s
        start = start_bin * BIN
        end = min(dur, start + clip_duration)
        start = max(0.0, end - clip_duration)

        reasons = []
        if has_audio and peak >= 0.8:
            pk = s + int(np.argmax(seg))
            reasons.append(f"Loud, energetic moment around {format_timestamp(pk * BIN)}")
        if has_audio and hook > 0.4:
            reasons.append("Strong opening")
        if has_audio and active > 0.9:
            reasons.append("Very little silence")
        if has_scenes and n_cuts >= 2:
            reasons.append(f"{int(n_cuts)} scene changes")
        cands.append(Candidate(round(start, 2), round(end, 2), score, reasons, source="smart"))

    picked = pick_non_overlapping(cands, count)
    if picked:
        lo = min(c.score for c in cands)
        hi = max(c.score for c in cands)
        for c in picked:
            c.score = round(100 * (c.score - lo) / (hi - lo), 0) if hi > lo else 50.0
            if not c.reasons:
                c.reasons = ["Above-average energy"]
    return picked
