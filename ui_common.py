"""Streamlit helpers shared by the ChiefCut pages."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

from chiefcut.probe import probe
from chiefcut.processing import AspectMode, frame_preview
from chiefcut.storage import default_output_dir, load_settings
from chiefcut.styles import LOOKS

ss = st.session_state
DOWNLOAD_BUTTON_LIMIT = 400 * 1024 * 1024


def get_settings() -> dict:
    if "settings" not in ss:
        ss.settings = load_settings()
    return ss.settings


def output_dir_setting() -> Path:
    s = get_settings()
    return Path(s["output_dir"]) if s.get("output_dir") else default_output_dir()


def meta_grid(items: list[tuple[str, str]]) -> None:
    cells = "".join(f"<div><b>{k}</b><span>{v}</span></div>" for k, v in items)
    st.markdown(f'<div class="cc-meta">{cells}</div>', unsafe_allow_html=True)


def section(num: int | str, title: str) -> None:
    st.markdown(f'<div class="cc-section"><span class="cc-step">{num}</span>{title}</div>', unsafe_allow_html=True)


def open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 - local folder chosen by the user
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def can_open_folders() -> bool:
    return os.name == "nt" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY"))


@st.cache_data(show_spinner=False, max_entries=96)
def thumbnail(path: str, mtime: float, at: float, aspect: str, look: str = "none", height: int = 360) -> bytes | None:
    info = probe(path)
    return frame_preview(Path(path), info, at, AspectMode(aspect), height=height, look=look)


def output_settings(prefix: str = "") -> tuple[AspectMode, str, str, str]:
    """Format, size, encoding and look pickers. Returns (aspect, size, quality, look)."""
    settings = get_settings()
    c1, c2 = st.columns([1.2, 1])
    with c1:
        aspect_label = st.radio(
            "Format", ["Vertical 9:16 (Shorts, Reels, TikTok)", "Original aspect ratio"], key=f"{prefix}aspect",
        )
    aspect = AspectMode.VERTICAL if aspect_label.startswith("Vertical") else AspectMode.ORIGINAL
    with c2:
        look = st.selectbox(
            "Look", list(LOOKS), format_func=lambda k: LOOKS[k].label, key=f"{prefix}look",
            help="Stylised filters applied while exporting: comic book, pencil sketch, pop-art, vintage film…",
        )
        if look != "none":
            st.caption(LOOKS[look].description + (f" ({LOOKS[look].speed_note})" if LOOKS[look].speed_note else ""))
    c4, c5 = st.columns(2)
    with c4:
        if aspect == AspectMode.VERTICAL:
            size = st.selectbox(
                "Output size", ["1080x1920", "720x1280"],
                format_func=lambda s: {"1080x1920": "1080 × 1920 (Full HD, recommended)",
                                       "720x1280": "720 × 1280 (faster, good for testing)"}[s],
                key=f"{prefix}size_v",
            )
        else:
            size = st.selectbox(
                "Output size", ["source", "1080p", "720p"],
                format_func=lambda s: {"source": "Same as source",
                                       "1080p": "Up to 1080p", "720p": "Up to 720p (faster)"}[s],
                key=f"{prefix}size_o",
            )
    with c5:
        quality = st.selectbox(
            "Encoding", ["standard", "high", "fast"],
            index=["standard", "high", "fast"].index(settings.get("quality", "standard")),
            format_func=lambda q: {"standard": "Standard (balanced)", "high": "High quality (slower)",
                                   "fast": "Fast draft (larger, lower quality)"}[q],
            key=f"{prefix}quality",
        )
    return aspect, size, quality, look


def duration_picker(info_duration: float, prefix: str = "", default: str = "30s") -> float:
    preset = st.segmented_control(
        "Clip duration", ["20s", "30s", "60s", "Custom"], default=default, key=f"{prefix}dur_preset",
    ) or default
    if preset == "Custom":
        from chiefcut.validation import MAX_CLIP_SECONDS
        return float(st.number_input(
            "Custom duration (seconds)", min_value=1.0, max_value=float(MAX_CLIP_SECONDS),
            value=min(15.0, max(1.0, float(int(info_duration)))), step=0.5, key=f"{prefix}dur_custom",
        ))
    return float(preset.rstrip("s"))
