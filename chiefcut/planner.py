"""Planning several clips from one video.

All strategies (even, random, smart highlights, speech-aware AI, Claude) return
the same `Candidate` objects, so the interface and exporter don't care how a
clip was chosen.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .validation import ValidationError, validate_duration

MAX_CLIPS = 20


@dataclass
class Candidate:
    start: float
    end: float
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    text: str = ""          # transcript excerpt, when known
    title: str = ""         # suggested title (Claude), when known
    source: str = "even"    # which strategy produced it

    @property
    def duration(self) -> float:
        return self.end - self.start


def max_clips_that_fit(source_duration: float, clip_duration: float) -> int:
    if clip_duration <= 0:
        return 0
    return int((source_duration + 0.05) // clip_duration)


def _check(source_duration: float, clip_duration: float, count: int) -> int:
    validate_duration(clip_duration)
    if count < 1:
        raise ValidationError("Choose at least one clip.")
    if count > MAX_CLIPS:
        raise ValidationError(f"You can make at most {MAX_CLIPS} clips at a time.")
    fit = max_clips_that_fit(source_duration, clip_duration)
    if fit == 0:
        raise ValidationError(
            f"The video is only {source_duration:.1f}s long, which is shorter than one {clip_duration:g}s clip."
        )
    if count > fit:
        raise ValidationError(
            f"Only {fit} non-overlapping {clip_duration:g}s clips fit in this {source_duration:.0f}s video. "
            f"Choose {fit} clips or fewer, or a shorter clip length."
        )
    return count


def plan_even(source_duration: float, clip_duration: float, count: int) -> list[Candidate]:
    """Clips spread evenly from the start to the end of the video."""
    _check(source_duration, clip_duration, count)
    if count == 1:
        starts = [max(0.0, (source_duration - clip_duration) / 2)]
    else:
        step = (source_duration - clip_duration) / (count - 1)
        starts = [i * step for i in range(count)]
    return [Candidate(round(s, 2), round(s + clip_duration, 2), source="even",
                      reasons=[f"Part {i + 1} of {count}"]) for i, s in enumerate(starts)]


def plan_random(source_duration: float, clip_duration: float, count: int, seed: int | None = None) -> list[Candidate]:
    """Random, non-overlapping clips. The same seed always gives the same clips."""
    _check(source_duration, clip_duration, count)
    rng = random.Random(seed)
    slack = max(0.0, source_duration - count * clip_duration)
    # Split the spare time into count+1 random gaps.
    weights = [rng.random() for _ in range(count + 1)]
    total = sum(weights) or 1.0
    gaps = [slack * w / total for w in weights]
    out, t = [], 0.0
    for i in range(count):
        t += gaps[i]
        out.append(Candidate(round(t, 2), round(t + clip_duration, 2), source="random", reasons=["Random pick"]))
        t += clip_duration
    return out


def pick_non_overlapping(cands: list[Candidate], count: int, min_gap: float = 1.0) -> list[Candidate]:
    """Greedy: best score first, skipping anything that overlaps a chosen clip.
    Returned in timeline order."""
    chosen: list[Candidate] = []
    for c in sorted(cands, key=lambda c: c.score, reverse=True):
        if all(c.end + min_gap <= o.start or c.start >= o.end + min_gap for o in chosen):
            chosen.append(c)
            if len(chosen) >= count:
                break
    return sorted(chosen, key=lambda c: c.start)
