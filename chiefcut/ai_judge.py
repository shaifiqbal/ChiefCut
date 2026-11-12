"""Optional cloud step: ask Claude to pick the most engaging moments.

Only the *text transcript* (with timestamps) is sent — never the video or
audio — and only after the user has ticked a consent box in the interface.
The API key is stored in the user's app-data folder, never in the project.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .planner import Candidate, pick_non_overlapping
from .storage import app_data_dir
from .transcribe import Sentence, Transcript, transcript_for_prompt

MODEL = "claude-opus-5-5"
PRICE_IN, PRICE_OUT = 4.00, 20.00        # USD per million tokens for MODEL
_SECRETS = "secrets.json"


class JudgeError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# API key storage (local file in %APPDATA%\ChiefCut, or ANTHROPIC_API_KEY)
# --------------------------------------------------------------------------
def _secrets_path() -> Path:
    return app_data_dir() / _SECRETS


def get_api_key() -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env.strip()
    try:
        key = json.loads(_secrets_path().read_text(encoding="utf-8")).get("anthropic_api_key")
        return key.strip() if key else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def key_source() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "environment variable"
    return "saved in ChiefCut" if get_api_key() else None


def save_api_key(key: str) -> None:
    key = (key or "").strip()
    if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
        raise JudgeError("That doesn't look like an Anthropic API key (it should start with sk-ant-).")
    p = _secrets_path()
    p.write_text(json.dumps({"anthropic_api_key": key}), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def delete_api_key() -> None:
    try:
        _secrets_path().unlink()
    except FileNotFoundError:
        pass


def mask(key: str | None) -> str:
    return f"{key[:10]}…{key[-4:]}" if key and len(key) > 16 else "—"


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------
SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "score": {"type": "integer"},
                },
                "required": ["start", "end", "title", "reason", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are an expert short-form video editor who finds the most engaging, self-contained moments "
    "in long videos for TikTok, YouTube Shorts and Instagram Reels. You receive a transcript as lines "
    "of the form [start-end] sentence, with times in seconds. Choose moments that hook a viewer in "
    "the first seconds, make sense without the surrounding context, and end on a satisfying beat. "
    "Each clip's start must be the start time of a sentence and its end must be the end time of a "
    "sentence, copied exactly from the transcript. Clips must not overlap. Titles are short, catchy, "
    "under 70 characters, in the transcript's language, without hashtags. The reason explains in one "
    "sentence why the moment works. Score each clip 1-100 for viral potential."
)


def estimate_cost(tr: Transcript) -> float:
    in_tokens = len(transcript_for_prompt(tr)) / 3.5 + 600
    out_tokens = 3000          # answer + some thinking, a generous estimate
    return (in_tokens * PRICE_IN + out_tokens * PRICE_OUT) / 1_000_000


def _nearest(value: float, options: list[float]) -> float:
    return min(options, key=lambda o: abs(o - value)) if options else value


def _clean(raw: list[dict], sents: list[Sentence], target: float, count: int, duration: float) -> list[Candidate]:
    starts = [s.start for s in sents]
    ends = [s.end for s in sents]
    lo_len, hi_len = target * 0.6, min(target * 1.5, 600.0)
    cands: list[Candidate] = []
    for item in raw:
        try:
            st = _nearest(float(item["start"]), starts)
            en = _nearest(float(item["end"]), ends)
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= st < en <= duration + 0.5):
            continue
        if not (lo_len <= en - st <= hi_len):
            continue
        text = " ".join(s.text for s in sents if s.start >= st - 0.01 and s.end <= en + 0.01)
        cands.append(Candidate(
            start=round(max(0.0, st - 0.2), 2), end=round(min(duration, en + 0.35), 2),
            score=float(max(1, min(100, int(item.get("score", 50))))),
            reasons=[str(item.get("reason", "")).strip()[:300] or "Picked by Claude"],
            text=text if len(text) <= 280 else text[:277].rstrip() + "…",
            title=str(item.get("title", "")).strip()[:90], source="claude",
        ))
    return pick_non_overlapping(cands, count, min_gap=0.0)


def judge(tr: Transcript, target: float, count: int, client=None) -> list[Candidate]:
    """Ask Claude for the best clips. `client` can be injected for tests."""
    sents = tr.sentences()
    if not sents:
        raise JudgeError("The transcript is empty, so there is nothing for Claude to choose from.")
    import anthropic

    if client is None:
        key = get_api_key()
        if not key:
            raise JudgeError("No Anthropic API key is set. Add one in ⚙️ Settings.")
        client = anthropic.Anthropic(api_key=key, timeout=180.0, max_retries=2)

    prompt = (
        f"Pick the {count} best clips of about {target:g} seconds each "
        f"(each between {target * 0.75:.0f} and {target * 1.3:.0f} seconds). "
        f"The video is {tr.duration:.0f} seconds long.\n\n<transcript>\n{transcript_for_prompt(tr)}\n</transcript>"
    )
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        raise JudgeError("Claude rejected the API key. Check it in ⚙️ Settings.") from None
    except anthropic.PermissionDeniedError:
        raise JudgeError("This API key isn't allowed to use that model.") from None
    except anthropic.RateLimitError:
        raise JudgeError("Claude is rate-limiting requests right now. Wait a minute and try again.") from None
    except anthropic.BadRequestError as exc:
        msg = str(getattr(exc, "message", exc))
        if "credit" in msg.lower() or "billing" in msg.lower():
            raise JudgeError("Your Anthropic account has no credit left. Add credit at console.anthropic.com.") from None
        raise JudgeError(f"Claude could not process the request: {msg}") from None
    except anthropic.APIStatusError as exc:
        raise JudgeError(f"Claude's service returned an error ({exc.status_code}). Try again later.") from None
    except anthropic.APIConnectionError:
        raise JudgeError("Could not reach Claude. Check your internet connection.") from None

    if response.stop_reason == "refusal":
        raise JudgeError("Claude declined to analyse this transcript. Use the on-PC speech-aware picks instead.")
    if response.stop_reason == "max_tokens":
        raise JudgeError("Claude's answer was cut off. Try fewer clips.")
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
    try:
        raw = json.loads(text).get("clips", [])
    except (json.JSONDecodeError, AttributeError):
        raise JudgeError("Claude's answer could not be read. Please try again.") from None
    picked = _clean(raw if isinstance(raw, list) else [], sents, target, count, tr.duration)
    if not picked:
        raise JudgeError("Claude didn't return any usable clips for this video.")
    return picked
