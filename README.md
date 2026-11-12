# ✂️ ChiefCut — Local Video Clipping Studio

Turn long videos into short clips for TikTok, YouTube Shorts and Instagram Reels — entirely on your own PC.
ChiefCut runs in your web browser at `http://localhost:8501`, but **nothing is uploaded to the internet**:
FFmpeg does all the work locally.

## Features

- Import a video by **upload** (up to 500 MB) or **open from this PC** (any size, read in place, never modified)
- MP4, MOV, WebM, MKV, M4V, AVI — anything FFmpeg can decode
- Shows duration, resolution, aspect ratio, frame rate, codecs, rotation and file size
- Clip length: **20 s, 30 s, 60 s or custom** (1–600 s)
- Start time as seconds (`75`), `MM:SS` (`1:15`) or `HH:MM:SS` (`0:01:15`) with clear error messages
- **Vertical 9:16** centre crop (1080×1920 or 720×1280) — fills the frame, never stretches — or **original aspect ratio**
- Framing preview showing exactly what the 9:16 crop keeps
- Phone videos with rotation metadata come out the right way up
- H.264 + AAC MP4 with fast-start, playable everywhere; silent videos supported
- Progress bar, in-app playback, download button, "open folder" button
- Never overwrites files (`clip.mp4`, `clip_2.mp4`, …); camera/GPS metadata stripped from exports
- Recent clips history and a Settings page to change the output folder

### New in version 0.2

- **Several clips at once** (*Create clip → ✨ Several clips (auto)*), with four ways to choose them:
  - **📏 Evenly spaced** — one clip from each equal part of the video (instant)
  - **🎲 Random** — random non-overlapping moments; *Shuffle* for a new set (instant)
  - **⚡ Smart highlights** — finds the loudest, most energetic moments and busy scenes with lots of
    cuts, using FFmpeg measurements (fast, no AI model)
  - **🗣️ Speech-aware AI** — transcribes speech **on your PC** with faster-whisper and picks clips that
    open with a hook (a question, "you", "the secret"…) and start and end on whole sentences
  - **☁️ Optional Claude judge** — sends only the *text transcript* (after you tick a consent box) to
    Claude, which picks the best moments and writes a catchy title for each. Needs your own Anthropic
    API key (Settings) and costs a few US cents per video.
- Review the suggestions (thumbnail, reasons, transcript excerpt), untick any you don't want, export all.
- **Looks**: Comic book, Pencil sketch, Pop-art, Vintage film, Noir (black & white), Vivid — applied by
  FFmpeg during export, with a live preview.

**Speed on an 8 GB i5:** evenly spaced / random are instant; smart highlights take seconds (plus a few
minutes for scene detection on long videos); speech-aware AI takes roughly 5–15 minutes per hour of video
with the *Base* model. The first speech-aware run downloads the model once (Tiny 75 MB, Base 145 MB,
Small 480 MB) into `%APPDATA%\ChiefCut\models`. Transcripts are saved, so re-runs are instant.

Clips are saved to `%USERPROFILE%\Documents\ChiefCut_Output` by default (OneDrive-redirected Documents
folders are detected automatically). You can change this in **⚙️ Settings**.

---

## 1. One-time setup (Windows 11)

Open **PowerShell** (press Start, type `PowerShell`, press Enter) and check what you have:

```powershell
python --version
ffmpeg -version
git --version
```

Install anything that says *"not recognized"*:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Gyan.FFmpeg
winget install -e --id Git.Git
```

**Close and reopen PowerShell** after installing so the new commands are found.

> If `python` opens the Microsoft Store instead of printing a version, go to
> *Settings → Apps → Advanced app settings → App execution aliases* and turn **off** the two
> "python.exe / python3.exe" App Installer entries, then reopen PowerShell.

## 2. Get the code

```powershell
cd $HOME\Documents
git clone https://github.com/shaifiqbal/ChiefCut.git
cd ChiefCut
```

## 3. Launch ChiefCut

**Easiest:** double-click **`run_chiefcut.bat`** in the ChiefCut folder.

The first launch creates a private Python environment (`.venv`) and installs packages (1–3 minutes).
It then checks FFmpeg and opens ChiefCut in your browser. Keep the black window open while you work;
close it to stop ChiefCut.

**Manual alternative** (PowerShell, inside the ChiefCut folder):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m chiefcut.doctor
.venv\Scripts\python -m streamlit run app.py
```

Then open <http://localhost:8501>.

## 4. Make a test video (optional)

No suitable video handy? This creates a 70-second test clip with a tone in your Videos folder:

```powershell
ffmpeg -f lavfi -i testsrc2=size=1920x1080:rate=30:duration=70 -f lavfi -i sine=frequency=440:duration=70 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$HOME\Videos\chiefcut_test.mp4"
```

## 5. Try it

1. **Import** — drag `chiefcut_test.mp4` onto the Upload box (or use *Open from this PC → Browse…*).
2. Check the **Source video** card shows `1:10.0` and `1920×1080`.
3. **Clip settings** — choose `20s`, start time `0:15`, *Vertical 9:16*, `1080 × 1920`.
   Try an invalid start like `1:75` or `1:00` to see the error messages.
4. Click **✂️ Create clip**, watch the progress bar, then play the result and click **Download** or **Open folder**.
5. Open **🕘 Recent clips** to see your history.

## 6. Run the automated tests

```powershell
.venv\Scripts\python -m pytest
```

The tests generate their own short synthetic videos with FFmpeg (landscape with audio, silent, rotated
phone video, WebM, MOV, an image and a fake file) and really encode clips, so they take ~30–60 seconds.

---

## Privacy & security

- The server binds to **127.0.0.1** only (`.streamlit/config.toml`) — other devices on your network can't reach it.
- Streamlit usage statistics are **disabled**. No accounts, analytics, cloud storage or paid APIs.
- FFmpeg runs with argument lists (`shell=False`) — user input is never turned into a shell command.
- Uploaded filenames are sanitised; output names can't escape the output folder.
- Speech transcription runs on this PC; only the speech model itself is downloaded (once).
- Claude is never contacted unless you saved an API key **and** ticked the consent box for that video,
  and then receives only the transcript text. The key is stored in `%APPDATA%\ChiefCut\secrets.json`.
- Original videos are only read. Upload copies live in `%TEMP%\chiefcut_*` and are deleted when replaced,
  when ChiefCut stops, or (after a crash) on the next start.

## Project layout

```
app.py                  Streamlit interface (pages, layout, state)
ui_auto.py              "Several clips (auto)" screen
ui_common.py            shared interface helpers
chiefcut/
  validation.py         timestamps, clip range checks, safe filenames and paths
  probe.py              FFprobe metadata (duration, size, rotation, audio)
  processing.py         FFmpeg trim/crop/scale/encode, progress, framing preview
  styles.py             looks (comic, sketch, pop-art, vintage, noir, vivid) as FFmpeg filters
  planner.py            several-clip planning (even, random) + non-overlap selection
  analysis.py           smart highlights: loudness + scene changes
  transcribe.py         on-PC speech transcription (faster-whisper) + sentence-aware picks
  ai_judge.py           optional Claude picks and titles (transcript text only)
  storage.py            output folder, unique names, temp files, settings
  history.py            recent clips list (JSON in %APPDATA%\ChiefCut)
  tools.py              finds ffmpeg/ffprobe (incl. winget locations), safe subprocess
  file_dialog.py        native "Open file" dialog (Windows)
  doctor.py             environment check: python -m chiefcut.doctor
tests/                  pytest suite with synthetic media
run_chiefcut.bat        double-click launcher
```

## Troubleshooting

| Problem | Fix |
|---|---|
| *"ffmpeg was not found"* | `winget install -e --id Gyan.FFmpeg`, then **reopen** PowerShell / re-run the .bat |
| Port 8501 already in use | Another ChiefCut window is open — close it, or run `streamlit run app.py --server.port 8502` |
| Upload over 500 MB refused | Use **Open from this PC** — it reads big files without copying them into memory |
| Export is slow | Choose `720 × 1280` and *Fast draft* encoding while testing |
| Output folder error | Pick another folder in **⚙️ Settings** |
| Speech model download fails | It needs internet once. Try again, or pick the *Tiny* model |
| Speech-aware is slow | Use the *Tiny* model, or make clips with *Smart highlights* instead |
| Claude: "rejected the API key" / "no credit" | Check the key in Settings, or add credit at console.anthropic.com |

## Updating ChiefCut

Replace the files with the new version (keep nothing else). Next time you double-click
`run_chiefcut.bat` it notices the changed `requirements.txt` and installs the new packages
automatically (version 0.2 adds faster-whisper and the Anthropic SDK — a few minutes, once).
Your clips, history, settings, API key and downloaded models are stored outside the ChiefCut folder
and are kept.

## Roadmap (not built yet)

Animated word-by-word captions (the transcript is already there) · smart reframing / subject tracking ·
batch processing of many videos · platform presets · hosted SaaS version.
