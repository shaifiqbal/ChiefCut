"""Environment check:  python -m chiefcut.doctor"""

from __future__ import annotations

import sys

from .storage import default_output_dir, ensure_writable_dir
from .tools import ToolNotFoundError, available_encoders, ffmpeg_version, find_tool


def main() -> int:
    ok = True
    print(f"Python        : {sys.version.split()[0]}", "(OK)" if sys.version_info >= (3, 10) else "(need 3.10+)")
    ok &= sys.version_info >= (3, 10)
    try:
        import streamlit

        print(f"Streamlit     : {streamlit.__version__} (OK)")
    except ImportError:
        print("Streamlit     : MISSING - run: pip install -r requirements.txt")
        ok = False
    for tool in ("ffmpeg", "ffprobe"):
        try:
            find_tool(tool)
            print(f"{tool:<14}: found (OK)")
        except ToolNotFoundError as exc:
            print(f"{tool:<14}: MISSING\n  {exc}")
            ok = False
    if ok:
        print(f"FFmpeg build  : {ffmpeg_version()}")
        enc = available_encoders()
        h264 = "libx264" in enc
        print(f"H.264 encoder : {'libx264 (OK)' if h264 else 'libx264 missing - a fallback encoder will be used'}")
        print(f"AAC encoder   : {'OK' if 'aac' in enc else 'MISSING'}")
    try:
        out = ensure_writable_dir(default_output_dir())
        print(f"Output folder : {out} (writable)")
    except Exception as exc:  # noqa: BLE001
        print(f"Output folder : problem - {exc}")
        ok = False
    print("\nAll checks passed." if ok else "\nSome checks failed - see above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
