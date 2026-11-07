"""ChiefCut — local video clipping studio (Streamlit interface).

Launch with:  streamlit run app.py      (or double-click run_chiefcut.bat)
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

from chiefcut import __version__, history
from chiefcut.probe import ProbeError, probe
from chiefcut.processing import (
    AspectMode,
    ClipRequest,
    ProcessingError,
    create_clip,
    frame_preview,
    output_dimensions,
)
from chiefcut.storage import (
    cleanup_stale_temp_dirs,
    create_temp_dir,
    default_output_dir,
    ensure_writable_dir,
    free_space_bytes,
    human_size,
    load_settings,
    remove_temp_dir,
    save_settings,
    save_stream_to_temp,
    validate_output_dir_input,
    videos_dir,
    build_output_name,
)
from chiefcut.tools import ToolNotFoundError, ffmpeg_version, find_tool
from chiefcut.validation import (
    ALLOWED_EXTENSIONS,
    MAX_CLIP_SECONDS,
    ValidationError,
    check_extension,
    clean_user_path,
    format_timestamp,
    parse_timestamp,
    resolve_local_video,
    validate_clip_range,
)

UPLOAD_LIMIT_MB = 500          # keep in sync with .streamlit/config.toml
INLINE_PREVIEW_LIMIT = 250 * 1024 * 1024   # larger sources show a still frame only
DOWNLOAD_BUTTON_LIMIT = 400 * 1024 * 1024
LARGE_FILE_WARNING = 2 * 1024 ** 3

st.set_page_config(page_title="ChiefCut", page_icon="✂️", layout="wide", initial_sidebar_state="expanded")


# ==========================================================================
# Styling
# ==========================================================================
st.markdown(
    """
    <style>
    :root { --cc-accent:#FF5A36; --cc-accent2:#FFB347; --cc-card:#161A23; --cc-line:#262C3A; --cc-muted:#8B93A7; }
    .block-container { padding-top: 1.6rem; max-width: 1280px; }
    [data-testid="stSidebar"] { background: linear-gradient(180deg,#10131A 0%,#0B0D12 100%); border-right:1px solid var(--cc-line); }
    .cc-brand { display:flex; align-items:center; gap:.6rem; margin:.2rem 0 1.2rem 0; }
    .cc-logo { width:38px; height:38px; border-radius:10px; display:flex; align-items:center; justify-content:center;
               background:linear-gradient(135deg,var(--cc-accent),var(--cc-accent2)); font-size:20px; box-shadow:0 6px 18px rgba(255,90,54,.35);}
    .cc-name { font-size:1.35rem; font-weight:800; letter-spacing:-.02em; }
    .cc-name span { color: var(--cc-accent); }
    .cc-tag { color:var(--cc-muted); font-size:.75rem; margin-top:-.25rem; }
    .cc-hero h1 { font-size:1.9rem; font-weight:800; letter-spacing:-.03em; margin:0 0 .15rem 0; }
    .cc-hero p { color:var(--cc-muted); margin:0 0 1rem 0; }
    .cc-step { display:inline-flex; align-items:center; justify-content:center; width:26px; height:26px; border-radius:50%;
               background:rgba(255,90,54,.15); color:var(--cc-accent); font-weight:700; font-size:.85rem; margin-right:.5rem; }
    .cc-section { font-weight:700; font-size:1.05rem; margin:.1rem 0 .6rem 0; display:flex; align-items:center; }
    .cc-meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:.5rem; margin:.4rem 0 .2rem 0; }
    .cc-meta div { background:#0F1219; border:1px solid var(--cc-line); border-radius:10px; padding:.55rem .7rem; }
    .cc-meta b { display:block; font-size:.68rem; text-transform:uppercase; letter-spacing:.06em; color:var(--cc-muted); font-weight:600; }
    .cc-meta span { font-size:.98rem; font-weight:600; }
    .cc-pill { display:inline-block; padding:.12rem .55rem; border-radius:999px; font-size:.72rem; font-weight:600;
               background:rgba(46,204,113,.13); color:#4ADE80; border:1px solid rgba(46,204,113,.25); }
    .cc-pill.warn { background:rgba(255,179,71,.12); color:#FFB347; border-color:rgba(255,179,71,.3); }
    .cc-path { font-family: ui-monospace, Consolas, monospace; font-size:.8rem; color:#C9D1E3; background:#0F1219;
               border:1px solid var(--cc-line); border-radius:8px; padding:.45rem .6rem; word-break:break-all; }
    .cc-foot { color:var(--cc-muted); font-size:.72rem; }
    div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:14px; }
    .stButton>button[kind="primary"], .stDownloadButton>button[kind="primary"] {
        background:linear-gradient(135deg,var(--cc-accent),#FF7A3D); border:0; font-weight:700; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==========================================================================
# One-time process setup
# ==========================================================================
@st.cache_resource
def _process_setup() -> dict:
    """Runs once per server process: tidy stale temp folders, register cleanup."""
    registry: dict = {"temp_dirs": set(), "stale_removed": cleanup_stale_temp_dirs()}

    def _cleanup() -> None:
        for d in list(registry["temp_dirs"]):
            remove_temp_dir(d)

    atexit.register(_cleanup)
    return registry


REGISTRY = _process_setup()


@st.cache_data(show_spinner=False)
def _tool_status() -> tuple[bool, str]:
    try:
        find_tool("ffmpeg")
        find_tool("ffprobe")
        return True, ffmpeg_version()
    except ToolNotFoundError as exc:
        return False, str(exc)


@st.cache_data(show_spinner=False, max_entries=64)
def _thumbnail(path: str, mtime: float, at: float, aspect: str) -> bytes | None:
    info = probe(path)
    return frame_preview(Path(path), info, at, AspectMode(aspect))


# ==========================================================================
# Session helpers
# ==========================================================================
ss = st.session_state
ss.setdefault("source", None)       # dict: path, name, is_temp, key, info
ss.setdefault("temp_dir", None)
ss.setdefault("last_result", None)
ss.setdefault("local_path", "")


def get_settings() -> dict:
    if "settings" not in ss:
        ss.settings = load_settings()
    return ss.settings


def output_dir_setting() -> Path:
    s = get_settings()
    return Path(s["output_dir"]) if s.get("output_dir") else default_output_dir()


def session_temp_dir() -> Path:
    d = ss.temp_dir
    if d is None or not Path(d).is_dir():
        d = create_temp_dir()
        ss.temp_dir = d
        REGISTRY["temp_dirs"].add(d)
    return Path(d)


def drop_source() -> None:
    """Forget the current source; delete it only if it is OUR temp copy."""
    src = ss.source
    if src and src.get("is_temp"):
        try:
            p = Path(src["path"])
            if ss.temp_dir and p.parent == Path(ss.temp_dir):
                p.unlink(missing_ok=True)
        except OSError:
            pass
    ss.source = None


def set_source(path: Path, name: str, is_temp: bool, key: str) -> None:
    try:
        info = probe(path)
    except ProbeError as exc:
        if is_temp:
            path.unlink(missing_ok=True)
        st.error(f"**This file can't be used.** {exc}")
        return
    drop_source()
    ss.source = {"path": str(path), "name": name, "is_temp": is_temp, "key": key, "info": info}
    ss.last_result = None


def open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 - local folder chosen by the user
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def can_open_folders() -> bool:
    return os.name == "nt" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY"))


def browse_for_file() -> str | None:
    """Native file picker in a helper process (works because the server is this PC)."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "chiefcut.file_dialog", str(videos_dir())],
            capture_output=True, timeout=600, shell=False,
            cwd=str(Path(__file__).parent),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0 and out.stdout:
        return out.stdout.decode("utf-8", "replace").strip()
    return None


def meta_grid(items: list[tuple[str, str]]) -> None:
    cells = "".join(f"<div><b>{k}</b><span>{v}</span></div>" for k, v in items)
    st.markdown(f'<div class="cc-meta">{cells}</div>', unsafe_allow_html=True)


def section(num: int | str, title: str) -> None:
    st.markdown(f'<div class="cc-section"><span class="cc-step">{num}</span>{title}</div>', unsafe_allow_html=True)


# ==========================================================================
# Sidebar
# ==========================================================================
with st.sidebar:
    st.markdown(
        '<div class="cc-brand"><div class="cc-logo">✂️</div><div>'
        '<div class="cc-name">Chief<span>Cut</span></div>'
        '<div class="cc-tag">Local video clipping studio</div></div></div>',
        unsafe_allow_html=True,
    )
    page = st.radio(
        "Navigation",
        ["🎬  Create clip", "🕘  Recent clips", "⚙️  Settings", "🔒  About & privacy"],
        label_visibility="collapsed",
    )
    st.divider()
    tools_ok, tools_msg = _tool_status()
    if tools_ok:
        st.markdown('<span class="cc-pill">● FFmpeg ready</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="cc-pill warn">● FFmpeg missing</span>', unsafe_allow_html=True)
    st.markdown(
        '<p class="cc-foot" style="margin-top:.6rem">Runs only on this computer. '
        f"Videos never leave your PC.<br>v{__version__}</p>",
        unsafe_allow_html=True,
    )

if not tools_ok:
    st.error(f"**FFmpeg is required.** {tools_msg}")
    st.stop()


# ==========================================================================
# Page: Create clip
# ==========================================================================
def page_create() -> None:
    st.markdown(
        '<div class="cc-hero"><h1>Create a clip</h1>'
        "<p>Import a video, choose the moment and the format, and export a share-ready MP4.</p></div>",
        unsafe_allow_html=True,
    )

    # ---------------- 1. Import ----------------
    with st.container(border=True):
        section(1, "Import a video")
        tab_upload, tab_local = st.tabs(["⬆️  Upload", "📁  Open from this PC (large files)"])
        with tab_upload:
            exts = [e.lstrip(".") for e in ALLOWED_EXTENSIONS]
            up = st.file_uploader(
                f"MP4, MOV, WebM, MKV, M4V or AVI — up to {UPLOAD_LIMIT_MB} MB",
                type=exts, accept_multiple_files=False, key="uploader",
            )
            st.caption(
                "Uploading copies the video into a temporary folder on this PC. "
                "For files over a few hundred MB, use **Open from this PC** instead — it reads "
                "the file in place and uses far less memory."
            )
            if up is not None:
                key = f"upload:{getattr(up, 'file_id', '')}:{up.name}:{up.size}"
                if not ss.source or ss.source.get("key") != key:
                    try:
                        check_extension(up.name)
                        with st.spinner("Saving and inspecting video…"):
                            path = save_stream_to_temp(up, up.name, session_temp_dir())
                            set_source(path, up.name, True, key)
                    except ValidationError as exc:
                        st.error(str(exc))
                    except OSError as exc:
                        st.error(f"Could not store the upload temporarily ({exc.strerror or exc}).")
            elif ss.source and ss.source.get("is_temp"):
                drop_source()  # user removed the upload

        with tab_local:
            st.caption(
                "The original file is only **read** — ChiefCut never modifies, moves or deletes it."
            )
            if ss.get("_picked_path"):
                ss.local_path = ss.pop("_picked_path")  # must be set before the widget is drawn
            c1, c2 = st.columns([5, 1.3], vertical_alignment="bottom")
            with c1:
                st.text_input(
                    "Video file path",
                    key="local_path",
                    placeholder=r'C:\Users\you\Videos\my-video.mp4   (tip: Shift + right-click → "Copy as path")',
                )
            with c2:
                if os.name == "nt" and st.button("Browse…", use_container_width=True):
                    chosen = browse_for_file()
                    if chosen:
                        ss._picked_path = chosen
                        st.rerun()
            if st.button("Use this video", type="secondary", disabled=not ss.local_path.strip()):
                try:
                    p = resolve_local_video(ss.local_path)
                    with st.spinner("Inspecting video…"):
                        set_source(p, p.name, False, f"local:{p}:{p.stat().st_mtime}")
                    if ss.source and ss.source["key"].startswith("local:"):
                        st.toast("Video loaded", icon="✅")
                except ValidationError as exc:
                    st.error(str(exc))

    src = ss.source
    if not src:
        st.info("👆 Import a video to get started. Need a test file? See **README → Make a test video**.")
        render_last_result()
        return

    info = src["info"]
    src_path = Path(src["path"])
    if not src_path.is_file():
        st.error("The source video is no longer available. Please import it again.")
        ss.source = None
        return

    # ---------------- 2. Source info ----------------
    with st.container(border=True):
        section(2, "Source video")
        top_l, top_r = st.columns([1.35, 1])
        with top_l:
            st.markdown(f"**{src['name']}**")
            meta_grid([
                ("Duration", format_timestamp(info.duration, precise=True)),
                ("Resolution", f"{info.display_width}×{info.display_height}"),
                ("Aspect", f"{info.aspect_label} · {info.orientation}"),
                ("Frame rate", f"{info.fps:g} fps" if info.fps else "—"),
                ("Video", info.video_codec.upper()),
                ("Audio", (info.audio_codec or "").upper() if info.has_audio else "None"),
                ("Rotation", f"{info.rotation}°" if info.rotation else "None"),
                ("File size", human_size(info.size_bytes)),
            ])
            if info.size_bytes > LARGE_FILE_WARNING:
                st.warning(
                    "This is a large file. Clipping still works, but expect slower processing on an "
                    "8 GB machine — close other heavy apps, and try the 720×1280 size first."
                )
            if not info.has_audio:
                st.caption("ℹ️ This video has no audio track, so the clip will be silent.")
        with top_r:
            if info.size_bytes <= INLINE_PREVIEW_LIMIT:
                st.video(str(src_path))
            else:
                st.caption("Preview player disabled for very large files to save memory — see the framing preview below.")

    # ---------------- 3. Clip settings ----------------
    settings = get_settings()
    with st.container(border=True):
        section(3, "Clip settings")
        c1, c2, c3 = st.columns([1.1, 1, 1.2])
        with c1:
            preset = st.segmented_control(
                "Clip duration", ["20s", "30s", "60s", "Custom"], default="30s", key="dur_preset",
            ) or "30s"
            if preset == "Custom":
                duration = st.number_input(
                    "Custom duration (seconds)", min_value=1.0, max_value=float(MAX_CLIP_SECONDS),
                    value=min(15.0, max(1.0, float(int(info.duration)))), step=0.5, key="dur_custom",
                )
            else:
                duration = float(preset.rstrip("s"))
        with c2:
            start_text = st.text_input(
                "Start time", value="0", key="start_text",
                help="Seconds (75), MM:SS (1:15) or HH:MM:SS (0:01:15). Decimals allowed, e.g. 1:15.5",
            )
        with c3:
            aspect_label = st.radio(
                "Format", ["Vertical 9:16 (Shorts, Reels, TikTok)", "Original aspect ratio"], key="aspect",
            )
        aspect = AspectMode.VERTICAL if aspect_label.startswith("Vertical") else AspectMode.ORIGINAL

        c4, c5 = st.columns(2)
        with c4:
            if aspect == AspectMode.VERTICAL:
                size = st.selectbox(
                    "Output size", ["1080x1920", "720x1280"],
                    format_func=lambda s: {"1080x1920": "1080 × 1920 (Full HD, recommended)",
                                           "720x1280": "720 × 1280 (faster, good for testing)"}[s],
                    key="size_v",
                )
            else:
                size = st.selectbox(
                    "Output size", ["source", "1080p", "720p"],
                    format_func=lambda s: {"source": "Same as source",
                                           "1080p": "Up to 1080p", "720p": "Up to 720p (faster)"}[s],
                    key="size_o",
                )
        with c5:
            quality = st.selectbox(
                "Encoding", ["standard", "high", "fast"],
                index=["standard", "high", "fast"].index(settings.get("quality", "standard")),
                format_func=lambda q: {"standard": "Standard (balanced)", "high": "High quality (slower)",
                                       "fast": "Fast draft (larger, lower quality)"}[q],
                key="quality",
            )

        # Validate live.
        error = None
        start = 0.0
        try:
            start = parse_timestamp(start_text)
            validate_clip_range(start, duration, info.duration)
        except ValidationError as exc:
            error = str(exc)

        if error:
            st.error(f"**Please adjust the clip:** {error}")
        else:
            ow, oh = output_dimensions(info, aspect, size)
            st.markdown(
                f"Clip: **{format_timestamp(start, True)} → {format_timestamp(start + duration, True)}** "
                f"({duration:g}s) · Output **{ow}×{oh}** MP4 (H.264{' + AAC audio' if info.has_audio else ', no audio'})"
            )

        # Framing preview
        if not error:
            with st.expander("🖼️ Framing preview at start time", expanded=aspect == AspectMode.VERTICAL):
                thumb = _thumbnail(str(src_path), src_path.stat().st_mtime, round(start, 1), aspect.value)
                if thumb:
                    cap = ("Highlighted area = what the 9:16 centre crop keeps." if aspect == AspectMode.VERTICAL
                           else "Full frame is kept.")
                    st.image(thumb, caption=cap, width=520)
                else:
                    st.caption("A preview frame could not be extracted at this time.")

    # ---------------- 4. Export ----------------
    with st.container(border=True):
        section(4, "Export")
        out_dir = output_dir_setting()
        try:
            out_dir = ensure_writable_dir(out_dir)
            out_ok = True
        except ValidationError as exc:
            out_ok = False
            st.error(f"**Output folder problem:** {exc} Open **⚙️ Settings** to choose another folder.")
        if out_ok:
            free = free_space_bytes(out_dir)
            st.markdown(
                f'Saving to <div class="cc-path">{out_dir}</div>'
                f'<span class="cc-foot">Free space: {human_size(free)}</span>',
                unsafe_allow_html=True,
            )

        go = st.button("✂️  Create clip", type="primary", disabled=bool(error) or not out_ok,
                       use_container_width=True)
        if go:
            req = ClipRequest(source=src_path, start=start, duration=duration, aspect=aspect,
                              size=size, quality=quality)
            tag = f"{'vertical' if aspect == AspectMode.VERTICAL else 'original'}_{duration:g}s".replace(".", "-")
            name = build_output_name(src["name"], tag)
            bar = st.progress(0.0, text="Starting FFmpeg…")

            def _progress(frac: float) -> None:
                bar.progress(frac, text=f"Encoding… {frac * 100:.0f}%")

            try:
                result = create_clip(req, out_dir, name, info=info, on_progress=_progress)
            except (ValidationError, ProbeError) as exc:
                bar.empty()
                st.error(str(exc))
            except ProcessingError as exc:
                bar.empty()
                st.error(f"**Export failed.** {exc}")
                if exc.details:
                    with st.expander("Technical details"):
                        st.code(exc.details, language="text")
            except OSError as exc:
                bar.empty()
                st.error(f"**Export failed** because of a file system error: {exc.strerror or exc}")
            else:
                bar.progress(1.0, text=f"Done in {result.elapsed:.1f}s")
                entry = {
                    "path": str(result.path), "name": result.path.name, "source_name": src["name"],
                    "start": start, "duration": round(result.duration, 2),
                    "width": result.width, "height": result.height,
                    "aspect": aspect.value, "has_audio": result.has_audio, "size_bytes": result.size_bytes,
                }
                history.add(entry)
                ss.last_result = entry
                st.toast("Clip exported!", icon="🎉")

    render_last_result()


def render_last_result() -> None:
    res = ss.last_result
    if not res:
        return
    p = Path(res["path"])
    if not p.is_file():
        return
    with st.container(border=True):
        section("✓", "Your clip is ready")
        l, r = st.columns([1, 1.3])
        with l:
            st.video(str(p))
        with r:
            st.markdown(f"**{p.name}**")
            meta_grid([
                ("Length", f"{res['duration']:.1f}s"),
                ("Size", f"{res['width']}×{res['height']}"),
                ("Audio", "Yes" if res["has_audio"] else "No"),
                ("File", human_size(res["size_bytes"])),
            ])
            st.markdown(f'<div class="cc-path">{p.parent}</div>', unsafe_allow_html=True)
            st.write("")
            b1, b2 = st.columns(2)
            with b1:
                if res["size_bytes"] <= DOWNLOAD_BUTTON_LIMIT:
                    st.download_button("⬇️  Download MP4", data=p.read_bytes(), file_name=p.name,
                                       mime="video/mp4", type="primary", use_container_width=True)
            with b2:
                if can_open_folders() and st.button("📂  Open folder", use_container_width=True):
                    open_folder(p.parent)
            st.caption("The clip is already saved in the folder above — downloading just makes another copy.")


# ==========================================================================
# Page: Recent clips
# ==========================================================================
def page_history() -> None:
    st.markdown('<div class="cc-hero"><h1>Recent clips</h1><p>Clips you have exported with ChiefCut.</p></div>',
                unsafe_allow_html=True)
    entries = history.load()
    if not entries:
        st.info("No clips yet. Head to **🎬 Create clip** to make your first one.")
        return
    labels = [f"{e['name']}  ·  {e.get('created', '')[:16].replace('T', ' ')}" for e in entries]
    idx = st.selectbox("Preview a clip", range(len(entries)), format_func=lambda i: labels[i])
    e = entries[idx]
    p = Path(e["path"])
    with st.container(border=True):
        l, r = st.columns([1, 1.3])
        with l:
            st.video(str(p))
        with r:
            st.markdown(f"**{e['name']}**")
            meta_grid([
                ("From", e.get("source_name", "—")),
                ("Start", format_timestamp(e.get("start", 0), True)),
                ("Length", f"{e.get('duration', 0):.1f}s"),
                ("Size", f"{e.get('width')}×{e.get('height')}"),
                ("Format", "9:16" if e.get("aspect") == "vertical" else "Original"),
                ("File", human_size(e.get("size_bytes"))),
            ])
            st.markdown(f'<div class="cc-path">{p.parent}</div>', unsafe_allow_html=True)
            st.write("")
            b1, b2, b3 = st.columns(3)
            with b1:
                if (e.get("size_bytes") or 0) <= DOWNLOAD_BUTTON_LIMIT:
                    st.download_button("⬇️ Download", data=p.read_bytes(), file_name=p.name, mime="video/mp4",
                                       use_container_width=True, key=f"dl_{idx}")
            with b2:
                if can_open_folders() and st.button("📂 Open folder", use_container_width=True, key=f"of_{idx}"):
                    open_folder(p.parent)
            with b3:
                if st.button("Remove from list", use_container_width=True, key=f"rm_{idx}",
                             help="Removes it from this list only — the video file is kept."):
                    history.remove(e["path"])
                    st.rerun()

    st.markdown("#### All clips")
    st.dataframe(
        [{"Clip": x["name"], "Created": x.get("created", "")[:16].replace("T", " "),
          "Length (s)": x.get("duration"), "Size": f"{x.get('width')}×{x.get('height')}",
          "Source": x.get("source_name", "")} for x in entries],
        hide_index=True, use_container_width=True,
    )


# ==========================================================================
# Page: Settings
# ==========================================================================
def page_settings() -> None:
    st.markdown('<div class="cc-hero"><h1>Settings</h1><p>Where clips are saved and default options.</p></div>',
                unsafe_allow_html=True)
    s = get_settings()
    with st.container(border=True):
        st.markdown("**Output folder**")
        current = output_dir_setting()
        st.markdown(f'<div class="cc-path">{current}</div>', unsafe_allow_html=True)
        new = st.text_input("Change output folder", value="", placeholder=r"D:\Clips\ChiefCut")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save folder", type="primary", disabled=not new.strip(), use_container_width=True):
                try:
                    p = validate_output_dir_input(new)
                    s["output_dir"] = str(p)
                    save_settings(s)
                    st.success("Output folder saved.")
                    st.rerun()
                except ValidationError as exc:
                    st.error(str(exc))
        with c2:
            if st.button("Reset to default", use_container_width=True):
                s.pop("output_dir", None)
                save_settings(s)
                st.rerun()
        st.caption(f"Default: {default_output_dir()}")

    with st.container(border=True):
        st.markdown("**Default encoding**")
        q = st.selectbox("Encoding preset", ["standard", "high", "fast"],
                         index=["standard", "high", "fast"].index(s.get("quality", "standard")))
        if q != s.get("quality", "standard"):
            s["quality"] = q
            save_settings(s)
            st.success("Saved.")

    with st.container(border=True):
        st.markdown("**System**")
        meta_grid([("FFmpeg", tools_msg.split(" Copyright")[0].replace("ffmpeg version ", "")),
                   ("Python", sys.version.split()[0]),
                   ("Streamlit", st.__version__)])
        if REGISTRY.get("stale_removed"):
            st.caption(f"Cleaned up {REGISTRY['stale_removed']} leftover temporary folder(s) at startup.")


# ==========================================================================
# Page: About
# ==========================================================================
def page_about() -> None:
    st.markdown('<div class="cc-hero"><h1>About & privacy</h1><p>How ChiefCut handles your videos.</p></div>',
                unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown(
            """
**ChiefCut runs entirely on this computer.**

- Videos are processed locally by FFmpeg. Nothing is uploaded to the internet — the "Upload" box only
  copies the file into this PC's temporary folder.
- The server listens on `127.0.0.1` only, so other devices on your network cannot open it.
- No accounts, analytics or tracking. Streamlit's usage statistics are switched off.
- Original videos are never modified, moved or deleted. Temporary copies are removed when you
  replace the video, when ChiefCut shuts down, and (if a crash left any behind) on the next start.
- Exported clips have camera/GPS metadata stripped.

**Version 1 features:** upload or open local videos · 20/30/60s or custom clip length · start time in
seconds or timestamps · 9:16 centre crop (1080×1920 / 720×1280) or original aspect ratio · H.264/AAC
MP4 export · recent clips.

**Not included yet:** automatic captions, AI highlight detection, subject tracking, batch export,
URL import.
            """
        )


{
    "🎬  Create clip": page_create,
    "🕘  Recent clips": page_history,
    "⚙️  Settings": page_settings,
    "🔒  About & privacy": page_about,
}[page]()
