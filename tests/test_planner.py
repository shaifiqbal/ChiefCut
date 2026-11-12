import pytest

from chiefcut.planner import Candidate, max_clips_that_fit, pick_non_overlapping, plan_even, plan_random
from chiefcut.validation import ValidationError


def no_overlap(cands):
    s = sorted(cands, key=lambda c: c.start)
    return all(a.end <= b.start + 1e-6 for a, b in zip(s, s[1:]))


def test_even_spread_covers_start_and_end():
    c = plan_even(300, 30, 5)
    assert [x.start for x in c] == [0, 67.5, 135, 202.5, 270]
    assert all(x.duration == pytest.approx(30) for x in c)
    assert c[-1].end == 300 and no_overlap(c)


def test_even_single_clip_is_centred():
    (c,) = plan_even(100, 20, 1)
    assert c.start == 40 and c.end == 60


def test_even_exact_fit():
    c = plan_even(90, 30, 3)
    assert [(x.start, x.end) for x in c] == [(0, 30), (30, 60), (60, 90)]


@pytest.mark.parametrize("seed", [1, 2, 3, 42, 999])
def test_random_valid_and_reproducible(seed):
    a = plan_random(600, 30, 8, seed)
    b = plan_random(600, 30, 8, seed)
    assert [(x.start, x.end) for x in a] == [(x.start, x.end) for x in b]
    assert no_overlap(a)
    assert all(0 <= x.start and x.end <= 600 + 1e-6 for x in a)
    assert all(x.duration == pytest.approx(30, abs=0.02) for x in a)


def test_random_different_seeds_differ():
    assert [x.start for x in plan_random(600, 30, 5, 1)] != [x.start for x in plan_random(600, 30, 5, 2)]


def test_too_many_clips_explains_limit():
    assert max_clips_that_fit(100, 30) == 3
    with pytest.raises(ValidationError, match="Only 3 non-overlapping"):
        plan_even(100, 30, 4)
    with pytest.raises(ValidationError, match="shorter than one"):
        plan_random(10, 20, 1)
    with pytest.raises(ValidationError):
        plan_even(100, 30, 0)


def test_pick_non_overlapping_prefers_best_score():
    cands = [Candidate(0, 30, 0.5), Candidate(10, 40, 0.9), Candidate(41, 71, 0.4), Candidate(35, 65, 0.8)]
    picked = pick_non_overlapping(cands, 2)
    assert [(c.start, c.score) for c in picked] == [(10, 0.9), (41, 0.4)]
    assert no_overlap(picked)
