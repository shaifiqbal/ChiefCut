"""Opens a native 'Open file' dialog and prints the chosen path.

Run as a separate process (python -m chiefcut.file_dialog) because Tk must own
its own main thread, which Streamlit's script thread is not.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return 2
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        parent=root,
        title="ChiefCut - choose a video",
        initialdir=initial,
        filetypes=[
            ("Video files", "*.mp4 *.mov *.m4v *.webm *.mkv *.avi"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    if path:
        sys.stdout.write(path)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
