"""'Several clips (auto)' mode: plan many clips, review them, export the chosen ones."""

from __future__ import annotations

import random
import threading
from dataclasses import asdict
from pathlib import Path

import streamlit as st

from chiefcut import ai_judge, history
from chiefcut import transcribe as T
from chiefcut.analysis import AnalysisError, analyse, score_highlights
from chiefcut.planner import MAX_CLIPS, Candidate, max_clips_that_fit, plan_even, plan_random
from chiefcut.probe import ProbeError, VideoInfo
from chiefcut.processing import ClipRequest, ProcessingError, create_clip, output_dimensions
from chiefcut.storage import build_output_name, ensure_writable_dir, free_space_bytes, human_size
from chiefcut.styles import LOOKS
from chiefcut.validation import ValidationError, format_timestamp, safe_stem
from ui_common import (
    DOWNLOAD_BUTTON_LIMIT,
    can_open_folders,
    duration_picker,
    open_folder,
    output_dir_setting,
    output_settings,
    section,
    thumbnail,
)

ss = st.session_state

METHODS = {
    "even": "📏 Evenly spaced",
    "random": "🎲 Random",
    "smart": "⚡ Smart highlights",
    "speech": "🗣️ Speech-aware AI",
}
METHOD_HELP = {
    "even": "Splits the video into equal parts and takes one clip from each. Instant.",
    "random": "Picks random, non-overlapping moments. Shuffle for a different set. Instant.",
    "smart": "Finds the loudest, most energetic moments (laughter, cheering, raised voices) and busy "
             "scenes with lots of cuts. Fast — no AI model needed.",
    "speech": "Transcribes the speech on this PC and picks moments that open with a hook and start and "
              "end on whole sentences. Slower: roughly 5–15 minutes per hour of video on an i5.",
}
LANGUAGES = {
    "": "Detect automatically", "en": "English", "ur": "Urdu", "hi": "Hindi", "ar": "Arabic",
    "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese", "tr": "Turkish",
}


def _store(cands: list[Candidate], meta: dict) -> None:
    ss.auto_cands = [asdict(c) for c in cands]
    ss.auto_meta = meta
    ss.auto_results = []
    for k in [k for k in ss if str(k).startswith("auto_inc_")]:
        del ss[k]


def _bar_callback(bar):
    def cb(frac: float, msg: str) -> None:
        bar.progress(max(0.0, min(1.0, frac)), text=f"{msg} {frac * 100:.0f}%")
    return cb


def render(src: dict, info: VideoInfo, src_path: Path) -> None:
    ss.setdefault("auto_cands", [])
    ss.setdefault("auto_meta", {})
    ss.setdefault("auto_results", [])
    if ss.auto_meta.get("source_key") not in (None, src["key"]):
        _store([], {})

    # ---------------- 3. How to pick ----------------
    with st.container(border=True):
        section(3, "Auto clip settings")
        method = st.radio(
            "How should ChiefCut choose the clips?", list(METHODS), format_func=METHODS.get,
            horizontal=True, key="auto_method",
        )
        st.caption(METHOD_HELP[method])

        c1, c2 = st.columns([1.2, 1])
        with c1:
            duration = duration_picker(info.duration, prefix="auto_")
        fit = max(1, min(MAX_CLIPS, max_clips_that_fit(info.duration, duration)))
        with c2:
            count = int(st.number_input("Number of clips", min_value=1, max_value=fit,
                                        value=min(5, fit), step=1, key="auto_count"))
            if max_clips_that_fit(info.duration, duration) == 0:
                st.error("This video is shorter than one clip — choose a shorter duration.")

        use_claude = False
        scenes = False
        model_size, language, seed = "base", "", 0
        if method == "random":
            ss.setdefault("auto_seed", random.randint(1, 99999))
            seed = int(ss.auto_seed)
        elif method == "smart":
            scenes = st.checkbox(
                "Also detect scene changes (slower: reads every frame of the video)",
                value=info.duration <= 30 * 60, key="auto_scenes",
                help="Loudness analysis alone is very fast. Scene detection helps for edited videos "
                     "with lots of cuts, but takes a few minutes for long videos.",
            )
            if not info.has_audio and not scenes:
                st.warning("This video has no audio, so smart highlights need scene detection turned on.")
        elif method == "speech":
            if not info.has_audio:
                st.error("This video has no audio track, so speech-aware picking can't be used.")
            if not T.is_available():
                st.error("The speech AI package isn't installed. Close ChiefCut and double-click "
                         "run_chiefcut.bat to install it.")
            m1, m2 = st.columns(2)
            with m1:
                model_size = st.selectbox(
                    "Speech model", list(T.MODELS), index=1, key="auto_model",
                    format_func=lambda k: f"{T.MODELS[k][0]}" + ("" if T.model_is_downloaded(k)
                                                                 else f" · {T.MODELS[k][1]} download"),
                )
            with m2:
                language = st.selectbox("Spoken language", list(LANGUAGES), format_func=LANGUAGES.get,
                                        key="auto_lang")
            if not T.model_is_downloaded(model_size):
                st.caption(f"ℹ️ The first time, ChiefCut downloads the {model_size} speech model "
                           f"({T.MODELS[model_size][1]}) from Hugging Face. Your audio never leaves this PC.")
            with st.expander("☁️ Optional: let Claude pick the best moments and write titles"):
                key_src = ai_judge.key_source()
                if not key_src:
                    st.caption("Add an Anthropic API key in **⚙️ Settings** to enable this.")
                st.markdown(
                    "Claude reads the **text transcript only** — never your video or audio — and picks the "
                    "moments most likely to work as Shorts, with a catchy title for each. This uses your "
                    f"Anthropic account (usually a few US cents per video, model `{ai_judge.MODEL}`)."
                )
                consent = st.checkbox(
                    "I agree to send this video's transcript text to Anthropic's Claude API",
                    key="auto_claude_consent", disabled=not key_src,
                )
                use_claude = bool(consent and key_src)

        aspect, size, quality, look = output_settings(prefix="auto_")

        disabled = (max_clips_that_fit(info.duration, duration) == 0
                    or (method == "speech" and (not info.has_audio or not T.is_available()))
                    or (method == "smart" and not info.has_audio and not scenes))
        b1, b2 = st.columns([3, 1])
        with b1:
            find = st.button("🔍  Find clips", type="primary", width="stretch", disabled=disabled)
        with b2:
            if method == "random" and st.button("🎲 Shuffle", width="stretch"):
                ss.auto_seed = random.randint(1, 99999)
                seed = ss.auto_seed
                find = True

    if find:
        _find(src, info, src_path, method, duration, count, seed, scenes, model_size, language, use_claude)

    if not ss.auto_cands:
        return
    meta = ss.auto_meta
    if meta.get("duration") != duration or meta.get("method") != method:
        st.info("You changed the settings — click **Find clips** again to update the suggestions.")

    _review_and_export(src, info, src_path, aspect, size, quality, look)


def _find(src, info, src_path, method, duration, count, seed, scenes, model_size, language, use_claude) -> None:
    bar = st.progress(0.0, text="Starting…")
    cb = _bar_callback(bar)
    cancel = threading.Event()
    note = ""
    try:
        if method == "even":
            cands = plan_even(info.duration, duration, count)
        elif method == "random":
            cands = plan_random(info.duration, duration, count, seed)
        elif method == "smart":
            an = analyse(src_path, info, scenes=scenes, on_progress=cb, cancel=cancel)
            cands = score_highlights(an, duration, count)
            if len(cands) < count:
                note = f"Only {len(cands)} distinct highlight(s) fit without overlapping."
        else:
            tr = T.transcribe(src_path, info, model_size, language or None, on_progress=cb, cancel=cancel)
            if not tr.words:
                raise T.TranscriptionError("No speech was detected in this video.")
            if use_claude:
                bar.progress(1.0, text="Asking Claude to choose the best moments…")
                with st.spinner("Claude is reading the transcript…"):
                    cands = ai_judge.judge(tr, duration, count)
            else:
                cands = T.speech_candidates(tr, duration, count)
            if not cands:
                raise T.TranscriptionError(
                    "Couldn't find sentence-aligned clips of that length. Try a shorter duration."
                )
            if len(cands) < count:
                note = f"Only {len(cands)} good sentence-aligned clip(s) were found."
            ss.auto_transcript_lang = tr.language
    except (ValidationError, AnalysisError, T.TranscriptionError, ai_judge.JudgeError, ProbeError) as exc:
        bar.empty()
        st.error(str(exc))
        return
    bar.empty()
    _store(cands, {"source_key": src["key"], "method": method, "duration": duration,
                   "claude": use_claude, "note": note})
    st.toast(f"Found {len(cands)} clip(s)", icon="🔍")


def _review_and_export(src, info, src_path, aspect, size, quality, look) -> None:
    cands = [Candidate(**c) for c in ss.auto_cands]
    meta = ss.auto_meta
    with st.container(border=True):
        label = METHODS.get(meta.get("method", ""), "")
        if meta.get("claude"):
            label += " + Claude"
        section(4, f"Review {len(cands)} suggested clip(s) · {label}")
        if meta.get("note"):
            st.caption("ℹ️ " + meta["note"])
        st.caption("Untick any clip you don't want, then export.")
        mtime = src_path.stat().st_mtime
        for i, c in enumerate(cands):
            with st.container(border=True):
                t_col, info_col = st.columns([1, 3.2])
                with t_col:
                    jpg = thumbnail(str(src_path), mtime, round(c.start + min(1.0, c.duration / 2), 1),
                                    aspect.value, look, 240)
                    if jpg:
                        st.image(jpg, width="stretch")
                with info_col:
                    head = f"**Clip {i + 1}** · {format_timestamp(c.start, True)} → {format_timestamp(c.end, True)}" \
                           f" · {c.duration:.1f}s"
                    if c.source not in ("even", "random"):
                        head += f" · score {c.score:.0f}/100"
                    st.checkbox(head, value=True, key=f"auto_inc_{i}")
                    if c.title:
                        st.markdown(f"🏷️ **{c.title}**")
                    if c.reasons:
                        st.caption(" · ".join(c.reasons))
                    if c.text:
                        st.markdown(f"> {c.text}")

    chosen = [c for i, c in enumerate(cands) if ss.get(f"auto_inc_{i}", True)]
    with st.container(border=True):
        section(5, "Export")
        out_dir = output_dir_setting()
        try:
            out_dir = ensure_writable_dir(out_dir)
        except ValidationError as exc:
            st.error(f"**Output folder problem:** {exc} Open **⚙️ Settings** to choose another folder.")
            return
        ow, oh = output_dimensions(info, aspect, size)
        st.markdown(
            f"{len(chosen)} clip(s) · **{ow}×{oh}** MP4 · look: **{LOOKS[look].label}**"
            f'<div class="cc-path">{out_dir}</div><span class="cc-foot">Free space: '
            f"{human_size(free_space_bytes(out_dir))}</span>",
            unsafe_allow_html=True,
        )
        go = st.button(f"✂️  Export {len(chosen)} clip(s)", type="primary", width="stretch",
                       disabled=not chosen, key="auto_export")
        if go:
            _export(src, info, src_path, chosen, out_dir, aspect, size, quality, look)

    _render_results()


def _export(src, info, src_path, chosen, out_dir, aspect, size, quality, look) -> None:
    overall = st.progress(0.0, text="Starting…")
    results, errors = [], []
    method = ss.auto_meta.get("method", "auto")
    for n, c in enumerate(chosen):
        def cb(frac: float, n=n) -> None:
            overall.progress((n + frac) / len(chosen), text=f"Exporting clip {n + 1} of {len(chosen)}… {frac * 100:.0f}%")

        tag = f"{method}_{n + 1:02d}_{c.duration:.0f}s"
        if c.title:
            tag = f"{n + 1:02d}_{safe_stem(c.title, 30, 'clip')}"
        if look != "none":
            tag += f"_{look}"
        req = ClipRequest(src_path, c.start, c.duration, aspect, size, quality, look)
        try:
            r = create_clip(req, out_dir, build_output_name(src["name"], tag), info=info, on_progress=cb)
        except (ValidationError, ProcessingError, ProbeError, OSError) as exc:
            errors.append(f"Clip {n + 1}: {exc}")
            continue
        entry = {
            "path": str(r.path), "name": r.path.name, "source_name": src["name"], "start": c.start,
            "duration": round(r.duration, 2), "width": r.width, "height": r.height, "aspect": aspect.value,
            "has_audio": r.has_audio, "size_bytes": r.size_bytes, "look": look, "title": c.title,
        }
        history.add(entry)
        results.append(entry)
    overall.progress(1.0, text=f"Exported {len(results)} of {len(chosen)} clip(s)")
    for e in errors:
        st.error(e)
    ss.auto_results = results
    if results:
        st.toast(f"{len(results)} clip(s) exported!", icon="🎉")


def _render_results() -> None:
    results = [r for r in ss.get("auto_results", []) if Path(r["path"]).is_file()]
    if not results:
        return
    with st.container(border=True):
        section("✓", f"{len(results)} clip(s) ready")
        folder = Path(results[0]["path"]).parent
        st.markdown(f'<div class="cc-path">{folder}</div>', unsafe_allow_html=True)
        if can_open_folders() and st.button("📂  Open folder", key="auto_open"):
            open_folder(folder)
        cols = st.columns(3)
        for i, r in enumerate(results):
            p = Path(r["path"])
            with cols[i % 3]:
                st.video(str(p))
                st.caption(f"**{r.get('title') or p.name}** · {r['duration']:.1f}s · {human_size(r['size_bytes'])}")
                if r["size_bytes"] <= DOWNLOAD_BUTTON_LIMIT:
                    st.download_button("⬇️ Download", data=p.read_bytes(), file_name=p.name, mime="video/mp4",
                                       key=f"auto_dl_{i}", width="stretch")
