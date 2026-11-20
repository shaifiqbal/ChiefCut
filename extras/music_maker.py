"""ChiefCut Music Maker - composes original, royalty-free background music.

Every sound is synthesised from maths: no samples, no loops from anywhere else,
no internet. Each --seed gives a different piece, so the music is original.

Usage (Windows PowerShell, inside the ChiefCut folder):
    .venv\\Scripts\\python extras\\music_maker.py --minutes 10 --style chill --seed 1 --out "$HOME\\Videos\\music1.wav"

Styles: chill (lo-fi), upbeat (pop), epic (cinematic), ambient (calm, no drums).
Needs only numpy (already installed with ChiefCut).
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

SR = 44100
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR = [0, 2, 4, 5, 7, 9, 11]
MINOR = [0, 2, 3, 5, 7, 8, 10]
PROGS = {
    "major": [[0, 4, 5, 3], [5, 3, 0, 4], [0, 5, 3, 4], [3, 4, 0, 0], [0, 3, 5, 4], [3, 0, 4, 5]],
    "minor": [[0, 5, 2, 6], [0, 3, 6, 2], [0, 6, 5, 6], [0, 3, 4, 0], [5, 6, 0, 0], [0, 5, 3, 4]],
}
STYLES = {
    "chill":   dict(bpm=(76, 86),   mode="minor", swing=0.14, drums="lofi", bright=0.55, seventh=True),
    "upbeat":  dict(bpm=(108, 122), mode="major", swing=0.0,  drums="pop",  bright=1.0,  seventh=False),
    "epic":    dict(bpm=(86, 96),   mode="minor", swing=0.0,  drums="epic", bright=0.8,  seventh=False),
    "ambient": dict(bpm=(60, 70),   mode="major", swing=0.0,  drums=None,   bright=0.4,  seventh=True),
}


def mtof(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


class Song:
    def __init__(self, minutes: float, style: str, seed: int):
        self.rng = np.random.default_rng(seed)
        self.st = STYLES[style]
        self.style = style
        lo, hi = self.st["bpm"]
        self.bpm = float(self.rng.integers(lo, hi + 1))
        self.beat = 60.0 / self.bpm
        self.bar = 4 * self.beat
        self.n_bars = max(16, int(round(minutes * 60 / self.bar)))
        self.length = self.n_bars * self.bar
        self.root = int(self.rng.integers(0, 12)) + 48            # C3..B3
        self.scale = MAJOR if self.st["mode"] == "major" else MINOR
        n = int((self.length + 4.0) * SR)                         # + tail for reverb
        self.L = np.zeros(n, np.float32)
        self.R = np.zeros(n, np.float32)
        self.send = np.zeros(n, np.float32)                       # reverb bus

    # ---------------------------------------------------------------- helpers
    def degree_to_midi(self, degree: int, octave: int = 0) -> int:
        o, d = divmod(degree, 7)
        return self.root + 12 * (o + octave) + self.scale[d]

    def chord(self, degree: int) -> list[int]:
        notes = [degree, degree + 2, degree + 4] + ([degree + 6] if self.st["seventh"] else [])
        return [self.degree_to_midi(d) for d in notes]

    def add(self, t: float, sig: np.ndarray, pan: float = 0.0, send: float = 0.0, right: np.ndarray | None = None):
        i = int(t * SR)
        if i < 0 or i >= len(self.L):
            return
        sig = sig[: len(self.L) - i]
        r = sig if right is None else right[: len(sig)]
        gl, gr = np.sqrt(0.5 * (1 - pan)), np.sqrt(0.5 * (1 + pan))
        self.L[i:i + len(sig)] += gl * sig
        self.R[i:i + len(r)] += gr * r
        if send:
            self.send[i:i + len(sig)] += send * 0.5 * (sig + r)

    @staticmethod
    def env(n: int, attack: float, release: float) -> np.ndarray:
        e = np.ones(n, np.float32)
        a, r = min(n, int(attack * SR)), min(n, int(release * SR))
        if a:
            e[:a] = np.linspace(0, 1, a, dtype=np.float32)
        if r:
            e[-r:] *= np.linspace(1, 0, r, dtype=np.float32)
        return e

    # ---------------------------------------------------------------- instruments
    def pad(self, t: float, notes: list[int], dur: float, vol: float = 0.05):
        n = int(dur * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        left = np.zeros(n, np.float32)
        right = np.zeros(n, np.float32)
        b = self.st["bright"]
        for m in notes:
            f = mtof(m)
            for h in range(1, 7):
                amp = (1 / h ** 1.6) * (b ** (h - 1))
                left += amp * np.sin(2 * np.pi * f * h * 0.9985 * tt + h)
                right += amp * np.sin(2 * np.pi * f * h * 1.0015 * tt + 2 * h)
        e = self.env(n, min(0.8, dur / 3), min(1.0, dur / 3)) * vol
        self.add(t, left * e, send=0.5, right=right * e)

    def bass(self, t: float, m: int, dur: float, vol: float = 0.32):
        n = int(min(dur, 1.5) * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        f = mtof(m - 12)
        sig = np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(4 * np.pi * f * tt) + 0.08 * np.sin(6 * np.pi * f * tt)
        e = self.env(n, 0.005, 0.04) * np.exp(-tt / 0.45)
        self.add(t, (sig * e * vol).astype(np.float32))

    def pluck(self, t: float, m: int, vol: float = 0.08, pan: float = 0.0):
        n = int(0.6 * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        f = mtof(m + 12)
        sig = np.zeros(n, np.float32)
        for h in range(1, 9):
            sig += (1 / h) * np.sin(2 * np.pi * f * h * tt) * np.exp(-tt * (4 + 3 * h))
        self.add(t, sig * self.env(n, 0.002, 0.05) * vol, pan=pan, send=0.35)

    def lead(self, t: float, m: int, dur: float, vol: float = 0.07):
        n = int(dur * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        f = mtof(m + 12)
        vib = 1 + 0.003 * np.sin(2 * np.pi * 5 * tt) * np.minimum(1, tt / 0.3)
        phase = 2 * np.pi * np.cumsum(f * vib) / SR
        sig = np.zeros(n, np.float32)
        for k, h in enumerate((1, 3, 5, 7)):
            sig += ((-1) ** k / h ** 2) * np.sin(h * phase)
        self.add(t, sig * self.env(n, 0.03, min(0.15, dur / 2)) * vol, pan=0.1, send=0.45)

    def kick(self, t: float, vol: float = 0.8):
        n = int(0.45 * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        f = 45 + 85 * np.exp(-tt / 0.035)
        sig = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt / 0.22)
        self.add(t, (sig * vol).astype(np.float32))

    def snare(self, t: float, vol: float = 0.35, verb: float = 0.2):
        n = int(0.3 * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        noise = np.diff(self.rng.standard_normal(n + 1).astype(np.float32))
        sig = 0.35 * noise * np.exp(-tt / 0.09) + 0.5 * np.sin(2 * np.pi * 185 * tt) * np.exp(-tt / 0.05)
        self.add(t, sig * vol, send=verb)

    def hat(self, t: float, vol: float = 0.06, open_: bool = False):
        n = int((0.25 if open_ else 0.06) * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        noise = np.diff(np.diff(self.rng.standard_normal(n + 2).astype(np.float32)))
        sig = noise * np.exp(-tt / (0.08 if open_ else 0.018))
        self.add(t, sig * vol, pan=0.35)

    def boom(self, t: float, vol: float = 0.7):
        n = int(1.6 * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        f = 38 + 50 * np.exp(-tt / 0.08)
        sig = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt / 0.6)
        self.add(t, (sig * vol).astype(np.float32), send=0.4)

    # ---------------------------------------------------------------- composition
    def plan_sections(self) -> list[str]:
        n_sec = max(2, self.n_bars // 8)
        body = []
        last = "intro"
        choices = ["groove", "full", "break", "full", "groove"]
        for _ in range(n_sec - 2):
            options = [c for c in choices if c != last] if last in ("break",) else choices
            last = str(self.rng.choice(options))
            body.append(last)
        return ["intro"] + body + ["outro"]

    def melody(self, prog: list[int]) -> list[tuple[float, int, float]]:
        """A 2-bar motif: (beat offset, scale degree offset, length in beats)."""
        notes, pos = [], 0.0
        deg = int(self.rng.choice([0, 2, 4]))
        while pos < 8:
            length = float(self.rng.choice([0.5, 0.5, 1.0, 1.0, 1.5, 2.0]))
            if self.rng.random() < 0.2:
                pos += 0.5                                   # a rest
                continue
            notes.append((pos, deg, min(length, 8 - pos)))
            deg = int(np.clip(deg + self.rng.choice([-2, -1, -1, 1, 1, 2, 3]), -2, 7))
            pos += length
        return notes

    def compose(self, progress=None):
        sections = self.plan_sections()
        prog = list(self.rng.choice(PROGS[self.st["mode"]]))
        motif = self.melody(prog)
        arp_pat = int(self.rng.integers(0, 3))
        bar_i = 0
        for si, kind in enumerate(sections):
            if si % 2 == 0 and si:
                prog = list(self.rng.choice(PROGS[self.st["mode"]]))
            if kind == "full" and self.rng.random() < 0.5:
                motif = self.melody(prog)
                arp_pat = int(self.rng.integers(0, 3))
            n_bars = 8 if si < len(sections) - 1 else self.n_bars - bar_i
            for b in range(n_bars):
                t0 = (bar_i + b) * self.bar
                chord_deg = prog[b % len(prog)]
                notes = self.chord(chord_deg)
                self.pad(t0, notes, self.bar * 1.05, vol=0.045 if kind != "break" else 0.06)
                if kind in ("groove", "full", "outro") or (kind == "intro" and b >= 4):
                    self.bass_line(t0, notes[0], kind)
                if kind in ("full", "break") or (kind == "groove" and b % 2 == 1):
                    self.arp(t0, notes, arp_pat, soft=kind == "break")
                if kind in ("full", "break"):
                    for off, deg, ln in motif:
                        if (b % 2) * 4 <= off < (b % 2) * 4 + 4:
                            m = self.degree_to_midi(chord_deg + deg)
                            self.lead(t0 + (off - (b % 2) * 4) * self.beat, m, ln * self.beat * 0.95)
                if self.st["drums"] and kind not in ("intro", "break"):
                    fill = b == n_bars - 1
                    self.drums(t0, kind, fill, fade=(kind == "outro" and b >= n_bars - 4))
            bar_i += n_bars
            if progress:
                progress(bar_i / self.n_bars)

    def bass_line(self, t0: float, m: int, kind: str):
        b = self.beat
        if self.st["drums"] == "pop":
            for k in range(8):
                self.bass(t0 + k * b / 2, m, b / 2, vol=0.26)
        elif self.st["drums"] == "epic":
            self.bass(t0, m, 2 * b, vol=0.34)
            self.bass(t0 + 2 * b, m, 2 * b, vol=0.3)
        else:
            self.bass(t0, m, 1.5 * b)
            self.bass(t0 + 2.5 * b, m, b, vol=0.25)
            if self.rng.random() < 0.4:
                self.bass(t0 + 3.5 * b, m + 7, 0.5 * b, vol=0.2)

    def arp(self, t0: float, notes: list[int], pattern: int, soft: bool = False):
        step = self.beat / (2 if self.style in ("chill", "ambient", "epic") else 4)
        seq = notes + [notes[0] + 12]
        if pattern == 1:
            seq = seq + seq[-2:0:-1]
        steps = int(round(self.bar / step))
        for k in range(steps):
            if pattern == 2:
                m = int(self.rng.choice(seq))
            else:
                m = seq[k % len(seq)]
            vol = (0.05 if soft else 0.07) * (0.8 + 0.2 * self.rng.random())
            self.pluck(t0 + k * step, m, vol=vol, pan=0.3 * np.sin(k))

    def drums(self, t0: float, kind: str, fill: bool, fade: bool):
        b, sw = self.beat, self.st["swing"]
        g = 0.5 if fade else 1.0
        style = self.st["drums"]
        hum = lambda: float(self.rng.normal(0, 0.004))  # noqa: E731
        if style == "epic":
            self.boom(t0, 0.6 * g)
            self.boom(t0 + 2 * b, 0.45 * g)
            if kind == "full":
                self.snare(t0 + 3 * b, 0.4 * g, verb=0.6)
            return
        kick_beats = [0, 2.5] if style == "lofi" else [0, 1, 2, 3]
        for kb in kick_beats:
            self.kick(t0 + kb * b + hum(), (0.65 if style == "lofi" else 0.75) * g)
        for sb in (1, 3):
            self.snare(t0 + sb * b + hum(), (0.28 if style == "lofi" else 0.33) * g)
        for k in range(8):
            off = (k * 0.5 + (sw if k % 2 else 0)) * b
            if kind == "groove" and k % 2:
                continue
            self.hat(t0 + off + hum(), (0.05 if k % 2 else 0.065) * g, open_=(style == "pop" and k == 7))
        if fill:
            for k in range(4):
                self.snare(t0 + (3 + k / 4) * b, 0.18 + 0.05 * k)

    # ---------------------------------------------------------------- mixing
    def reverb(self, seconds: float = 1.8):
        n = int(seconds * SR)
        tt = np.arange(n, dtype=np.float32) / SR
        decay = np.exp(-tt / (seconds / 5)).astype(np.float32)
        irs = [self.rng.standard_normal(n).astype(np.float32) * decay * 0.02 for _ in range(2)]
        block = 1 << 16
        fft_n = 1 << int(np.ceil(np.log2(block + n)))
        irf = [np.fft.rfft(ir, fft_n) for ir in irs]
        targets = (self.L, self.R)
        total = len(self.L)
        for i in range(0, len(self.send), block):
            seg = np.fft.rfft(self.send[i:i + block], fft_n)
            for c in range(2):
                y = np.fft.irfft(seg * irf[c], fft_n)[: block + n].astype(np.float32)
                stop = min(total, i + len(y))
                targets[c][i:stop] += y[: stop - i]   # add straight into the mix (saves memory)
        self.send = np.zeros(1, np.float32)        # free the reverb bus

    def master(self) -> np.ndarray:
        self.reverb()
        if self.style == "chill":                       # gentle vinyl crackle
            n = len(self.L)
            pos = self.rng.integers(0, n, size=int(self.length * 6))
            crackle = np.zeros(n, np.float32)
            crackle[pos] = self.rng.uniform(-0.03, 0.03, size=len(pos)).astype(np.float32)
            self.L += crackle
            self.R += np.roll(crackle, 37)
        end = int(self.length * SR) + int(1.5 * SR)
        mix = np.empty((end, 2), np.float32)
        mix[:, 0] = self.L[:end]
        mix[:, 1] = self.R[:end]
        self.L = self.R = None                           # free memory
        sq = sum(float(np.sum(mix[i:i + SR * 30].astype(np.float64) ** 2)) for i in range(0, end, SR * 30))
        rms = (sq / mix.size) ** 0.5 or 1.0
        mix *= 0.16 / rms                                # consistent loudness
        np.tanh(mix * 1.3, out=mix)                      # soft limiter
        fade_in, fade_out = int(2 * SR), int(6 * SR)
        mix[:fade_in] *= np.linspace(0, 1, fade_in, dtype=np.float32)[:, None]
        mix[-fade_out:] *= np.linspace(1, 0, fade_out, dtype=np.float32)[:, None]
        peak = float(np.max(np.abs(mix))) or 1.0
        mix *= 0.89 / peak
        return mix


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        for i in range(0, len(audio), SR * 10):
            w.writeframes((audio[i:i + SR * 10] * 32767).astype("<i2").tobytes())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compose original royalty-free background music.")
    ap.add_argument("--minutes", type=float, default=10.0, help="length in minutes (0.5 - 30)")
    ap.add_argument("--style", choices=sorted(STYLES), default="chill")
    ap.add_argument("--seed", type=int, default=1, help="any number; each gives a different song")
    ap.add_argument("--out", required=True, help="output .wav file")
    a = ap.parse_args(argv)
    if not 0.5 <= a.minutes <= 30:
        ap.error("--minutes must be between 0.5 and 30")
    out = Path(a.out).expanduser()
    if out.suffix.lower() != ".wav":
        ap.error("--out must end with .wav")
    if out.exists():
        ap.error(f"{out} already exists - choose another name (ChiefCut never overwrites files)")

    start = time.time()
    song = Song(a.minutes, a.style, a.seed)
    key = NOTE_NAMES[song.root % 12] + (" major" if song.st["mode"] == "major" else " minor")
    print(f"Composing a {a.style} piece in {key} at {song.bpm:.0f} BPM, {song.length / 60:.1f} minutes (seed {a.seed})...")

    def progress(f: float) -> None:
        sys.stdout.write(f"\r  composing {f * 100:5.1f}%")
        sys.stdout.flush()

    song.compose(progress)
    print("\n  mixing...")
    audio = song.master()
    write_wav(out, audio)
    print(f"Done in {time.time() - start:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
