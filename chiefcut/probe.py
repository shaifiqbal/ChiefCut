"""Reading video metadata with FFprobe."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .tools import find_tool, run

# FFprobe reports still images as "video", so reject image demuxers explicitly.
_IMAGE_FORMATS = ("image2", "png_pipe", "jpeg_pipe", "bmp_pipe", "gif", "webp_pipe", "tiff_pipe")


class ProbeError(RuntimeError):
    """The file could not be read as a video. The message is user-facing."""


@dataclass
class VideoInfo:
    duration: float                # seconds
    width: int                     # stored (coded) width
    height: int                    # stored (coded) height
    rotation: int                  # 0, 90, 180 or 270 (clockwise, normalised)
    fps: float
    video_codec: str
    has_audio: bool
    audio_codec: str | None
    container: str
    size_bytes: int
    bitrate: int | None = None
    sample_aspect: float = 1.0
    extra: dict = field(default_factory=dict)

    @property
    def display_width(self) -> int:
        """Width as it should be shown (after rotation and pixel aspect)."""
        w, h = self._display()
        return w

    @property
    def display_height(self) -> int:
        w, h = self._display()
        return h

    def _display(self) -> tuple[int, int]:
        w = int(round(self.width * self.sample_aspect))
        h = self.height
        if self.rotation in (90, 270):
            w, h = h, w
        return w, h

    @property
    def aspect_label(self) -> str:
        w, h = self.display_width, self.display_height
        if not w or not h:
            return "unknown"
        ratio = w / h
        for label, value in (("16:9", 16 / 9), ("9:16", 9 / 16), ("4:3", 4 / 3), ("3:4", 3 / 4), ("1:1", 1.0), ("21:9", 21 / 9)):
            if abs(ratio - value) < 0.02:
                return label
        return f"{ratio:.2f}:1"

    @property
    def orientation(self) -> str:
        w, h = self.display_width, self.display_height
        if w == h:
            return "Square"
        return "Landscape" if w > h else "Portrait"


def _parse_fraction(text: str | None) -> float:
    if not text or text in ("0/0", "N/A"):
        return 0.0
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            d = float(den)
            return float(num) / d if d else 0.0
        return float(text)
    except ValueError:
        return 0.0


def _rotation(stream: dict) -> int:
    rot = 0.0
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rot = float(sd["rotation"])
            except (TypeError, ValueError):
                pass
    tag = (stream.get("tags") or {}).get("rotate")
    if tag and not rot:
        try:
            rot = -float(tag)  # legacy tag is clockwise; display matrix is counter-clockwise
        except ValueError:
            pass
    # Normalise to clockwise degrees in {0, 90, 180, 270}.
    return int(round(-rot / 90.0)) * 90 % 360


def probe(path: str | Path, timeout: float = 60) -> VideoInfo:
    path = Path(path)
    if not path.is_file():
        raise ProbeError("The video file could not be found.")
    args = [
        find_tool("ffprobe"), "-v", "error", "-hide_banner",
        "-print_format", "json", "-show_format", "-show_streams",
        str(path),
    ]
    try:
        result = run(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError("Reading the video took too long. The file may be damaged or on a slow drive.") from None
    if result.returncode != 0:
        raise ProbeError(
            "This file could not be read as a video. It may be damaged, incomplete, "
            "or use a format FFmpeg cannot decode."
        )
    try:
        data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise ProbeError("FFprobe returned unreadable information for this file.") from None

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    container = fmt.get("format_name", "") or ""
    if any(container.startswith(f) for f in _IMAGE_FORMATS):
        raise ProbeError("This looks like an image, not a video.")

    video = next(
        (s for s in streams
         if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise ProbeError("This file has no video track (it may be audio-only).")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _parse_fraction(fmt.get("duration")) or _parse_fraction(video.get("duration"))
    if duration <= 0:
        raise ProbeError("The length of this video could not be determined.")

    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ProbeError("The video's resolution could not be determined.")

    sar = _parse_fraction(video.get("sample_aspect_ratio")) or 1.0
    fps = _parse_fraction(video.get("avg_frame_rate")) or _parse_fraction(video.get("r_frame_rate"))
    try:
        bitrate = int(fmt["bit_rate"]) if fmt.get("bit_rate") else None
    except ValueError:
        bitrate = None

    return VideoInfo(
        duration=duration,
        width=width,
        height=height,
        rotation=_rotation(video),
        fps=round(fps, 3),
        video_codec=video.get("codec_name", "unknown"),
        has_audio=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
        container=container,
        size_bytes=int(fmt.get("size") or path.stat().st_size),
        bitrate=bitrate,
        sample_aspect=sar if 0.1 < sar < 10 else 1.0,
        extra={
            "pix_fmt": video.get("pix_fmt"),
            "audio_channels": audio.get("channels") if audio else None,
            "audio_sample_rate": audio.get("sample_rate") if audio else None,
        },
    )
