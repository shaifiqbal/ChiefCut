"""The FFmpeg engine: trim, crop/resize and encode to a share-ready MP4."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .probe import ProbeError, VideoInfo, probe
from .storage import finalize_no_overwrite, free_space_bytes, unique_path
from .styles import get_look
from .tools import CREATIONFLAGS, available_encoders, find_tool, run
from .validation import ValidationError, validate_clip_range


class ProcessingError(RuntimeError):
    """Encoding failed. `details` holds FFmpeg's own messages for troubleshooting."""

    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.details = details


class AspectMode(str, Enum):
    VERTICAL = "vertical"   # 9:16 centre crop
    ORIGINAL = "original"   # keep source aspect ratio


# Named output sizes. (width, height) for vertical; max height for original.
VERTICAL_SIZES = {
    "1080x1920": (1080, 1920),
    "720x1280": (720, 1280),
}
ORIGINAL_SIZES = {
    "source": None,   # keep the source resolution
    "1080p": 1080,    # limit the short side
    "720p": 720,
}

QUALITY_PRESETS = {
    # name: (x264 preset, CRF). Lower CRF = better quality, bigger file.
    "standard": ("veryfast", 21),
    "high": ("medium", 18),
    "fast": ("ultrafast", 24),
}


@dataclass
class ClipRequest:
    source: Path
    start: float
    duration: float
    aspect: AspectMode = AspectMode.VERTICAL
    size: str = "1080x1920"
    quality: str = "standard"
    look: str = "none"


@dataclass
class ClipResult:
    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool
    size_bytes: int
    elapsed: float


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
def even(n: float) -> int:
    v = int(round(n))
    return max(2, v - (v % 2))


def vertical_crop_box(display_w: int, display_h: int) -> tuple[int, int, int, int]:
    """Largest centred 9:16 rectangle (x, y, w, h) inside a display_w x display_h frame."""
    target = 9 / 16
    if display_w / display_h > target:
        h = display_h
        w = h * target
    else:
        w = display_w
        h = w / target
    x = (display_w - w) / 2
    y = (display_h - h) / 2
    return int(round(x)), int(round(y)), int(round(w)), int(round(h))


def output_dimensions(info: VideoInfo, aspect: AspectMode, size: str) -> tuple[int, int]:
    if aspect == AspectMode.VERTICAL:
        if size not in VERTICAL_SIZES:
            raise ValidationError(f"Unknown vertical size: {size}")
        return VERTICAL_SIZES[size]
    if size not in ORIGINAL_SIZES:
        raise ValidationError(f"Unknown output size: {size}")
    w, h = info.display_width, info.display_height
    limit = ORIGINAL_SIZES[size]
    short = min(w, h)
    if limit and short > limit:
        scale = limit / short
        w, h = w * scale, h * scale
    return even(w), even(h)


def build_filter(info: VideoInfo, aspect: AspectMode, size: str, look: str = "none") -> str:
    """FFmpeg video filter chain. Rotation is handled by FFmpeg's autorotate,
    so the filter sees the upright (display) frame."""
    out_w, out_h = output_dimensions(info, aspect, size)
    chain = []
    if abs(info.sample_aspect - 1.0) > 0.01:
        chain.append("scale=trunc(iw*sar/2)*2:ih")  # make pixels square first
        chain.append("setsar=1")
    if aspect == AspectMode.VERTICAL:
        # Fill the 9:16 frame (scale up the short side), then centre-crop the rest.
        chain.append(f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase:flags=lanczos")
        chain.append(f"crop={out_w}:{out_h}")
    else:
        chain.append(f"scale={out_w}:{out_h}:flags=lanczos")
    style = get_look(look).filter
    if style:
        chain.append(style)  # applied after resizing, so it costs as little as possible
    chain += ["setsar=1", "format=yuv420p"]
    return ",".join(chain)


# --------------------------------------------------------------------------
# Command building
# --------------------------------------------------------------------------
def _video_codec_args(quality: str) -> list[str]:
    preset, crf = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["standard"])
    encoders = available_encoders()
    if "libx264" in encoders:
        return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-profile:v", "high", "-level:v", "4.1"]
    for alt in ("libopenh264", "h264_mf", "h264_qsv"):
        if alt in encoders:
            return ["-c:v", alt, "-b:v", "8M"]
    raise ProcessingError(
        "Your FFmpeg build has no H.264 encoder. Install the full build with:  winget install -e --id Gyan.FFmpeg"
    )


def build_command(req: ClipRequest, info: VideoInfo, output: Path) -> list[str]:
    """Build the FFmpeg argument list. No shell is ever involved."""
    args = [
        find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-nostats",
        "-loglevel", "error", "-progress", "pipe:1",
        "-n",                                   # never overwrite
        "-ss", f"{req.start:.3f}",              # fast + frame-accurate seek (we re-encode)
        "-i", str(req.source),
        "-t", f"{req.duration:.3f}",
        "-map", "0:v:0",
    ]
    if info.has_audio:
        args += ["-map", "0:a:0?"]
    args += ["-vf", build_filter(info, req.aspect, req.size, req.look)]
    args += _video_codec_args(req.quality)
    if info.has_audio:
        args += ["-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"]
    else:
        args += ["-an"]
    args += [
        "-sn", "-dn",
        "-map_metadata", "-1",                  # strip GPS/camera metadata for privacy
        "-map_chapters", "-1",
        "-movflags", "+faststart",              # plays before fully downloaded
        "-f", "mp4",
        str(output),
    ]
    return args


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
def _estimate_bytes(duration: float, w: int, h: int) -> int:
    # Rough upper estimate: ~0.25 bits per pixel per frame at 30fps + audio.
    return int(duration * (w * h * 30 * 0.25 / 8 + 24_000))


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join([ln for ln in text.splitlines() if ln.strip()][-lines:])


def create_clip(
    req: ClipRequest,
    output_dir: Path,
    output_name: str,
    info: VideoInfo | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ClipResult:
    """Render a clip. Never touches the source file; never overwrites outputs."""
    started = time.monotonic()
    source = Path(req.source)
    if not source.is_file():
        raise ValidationError("The source video is no longer available. Please import it again.")
    info = info or probe(source)
    validate_clip_range(req.start, req.duration, info.duration)

    out_w, out_h = output_dimensions(info, req.aspect, req.size)
    free = free_space_bytes(output_dir)
    needed = _estimate_bytes(req.duration, out_w, out_h) * 2 + 50 * 1024 * 1024
    if free is not None and free < needed:
        raise ProcessingError(
            f"Not enough free disk space in the output folder (about {needed // 2**20} MB needed)."
        )

    final = unique_path(output_dir, output_name)
    partial = final.with_name(final.name + ".part.mp4")
    cmd = build_command(req, info, partial)

    with tempfile.TemporaryFile() as err_file:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=err_file,
            stdin=subprocess.DEVNULL,
            shell=False,
            creationflags=CREATIONFLAGS,
        )
        cancelled = False
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    proc.kill()
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    if on_progress and req.duration > 0:
                        on_progress(max(0.0, min(0.99, us / 1_000_000 / req.duration)))
            proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            partial.unlink(missing_ok=True)
            raise
        err_file.seek(0)
        stderr = err_file.read().decode("utf-8", "replace")

    if cancelled:
        partial.unlink(missing_ok=True)
        raise ProcessingError("Export cancelled.")
    if proc.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise ProcessingError(_friendly_ffmpeg_error(stderr), details=_tail(stderr))

    # Verify the result before we call it a success.
    try:
        out_info = probe(partial)
    except ProbeError as exc:
        partial.unlink(missing_ok=True)
        raise ProcessingError("FFmpeg finished but the result is not a readable video.", str(exc)) from None
    if out_info.duration < min(req.duration, info.duration - req.start) - 0.5:
        partial.unlink(missing_ok=True)
        raise ProcessingError(
            f"The exported clip is shorter than expected ({out_info.duration:.1f}s). "
            "The source may be damaged near that point.",
            details=_tail(stderr),
        )

    saved = finalize_no_overwrite(partial, final)
    if on_progress:
        on_progress(1.0)
    return ClipResult(
        path=saved,
        duration=out_info.duration,
        width=out_info.display_width,
        height=out_info.display_height,
        has_audio=out_info.has_audio,
        size_bytes=saved.stat().st_size,
        elapsed=time.monotonic() - started,
    )


def _friendly_ffmpeg_error(stderr: str) -> str:
    s = stderr.lower()
    if "no space left" in s:
        return "The disk is full. Free up space or choose another output folder."
    if "permission denied" in s:
        return "Permission was denied while writing the clip. Choose another output folder."
    if "already exists" in s:
        return "An output file with that name already exists. Please try again."
    if "invalid data found" in s or "error while decoding" in s:
        return "The source video contains data FFmpeg cannot decode. The file may be damaged."
    if "cannot allocate memory" in s or "out of memory" in s:
        return "The computer ran out of memory. Try the 720x1280 size or close other programs."
    return "FFmpeg could not create the clip. See the technical details below."


# --------------------------------------------------------------------------
# Thumbnails (framing preview)
# --------------------------------------------------------------------------
def frame_preview(source: Path, info: VideoInfo, at: float, aspect: AspectMode, height: int = 360,
                  look: str = "none") -> bytes | None:
    """A JPEG of the frame at `at` seconds, with the 9:16 crop area outlined."""
    at = max(0.0, min(at, max(0.0, info.duration - 0.1)))
    dw, dh = info.display_width, info.display_height
    w = even(height * dw / dh)
    vf = f"scale={w}:{height}"
    style = get_look(look).filter
    if style:
        vf = f"{vf},{style},format=yuv420p"
    if aspect == AspectMode.VERTICAL:
        x, y, cw, ch = vertical_crop_box(w, height)
        # Dim everything, then redraw the kept area at full brightness, then outline it.
        vf = (
            f"{vf},split[a][b];[a]colorchannelmixer=rr=0.35:gg=0.35:bb=0.35[dim];"
            f"[b]crop={cw}:{ch}:{x}:{y}[keep];[dim][keep]overlay={x}:{y},"
            f"drawbox=x={x}:y={y}:w={cw}:h={ch}:color=0xFF5A36:t=3"
        )
    cmd = [
        find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{at:.3f}", "-i", str(source),
        "-frames:v", "1", "-filter_complex", vf,
        "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1",
    ]
    try:
        out = run(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    return out.stdout if out.returncode == 0 and out.stdout else None
