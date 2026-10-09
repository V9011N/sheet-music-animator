"""A tiny built-in piano-ish synth so the project has something to play without a soundfont."""
from __future__ import annotations

import wave

import numpy as np

SR = 44100
PARTIALS = (1.0, 0.55, 0.32, 0.2, 0.12, 0.07, 0.04)


def _voice(pitch: int, hold: float) -> np.ndarray:
    """One note (unit velocity) that is held for `hold` seconds, then released."""
    f0 = 440.0 * 2 ** ((pitch - 69) / 12)
    ring = 0.35 + 0.9 * max(0.0, (84 - pitch)) / 60              # low notes ring longer
    n = int((hold + ring * 2.2) * SR)
    t = np.arange(n, dtype=np.float32) / SR
    tau = 0.35 + ring                                             # decay time constant
    env = np.exp(-t / tau)
    # key release: after `hold` fade quickly (as if a damper came down) but not instantly
    rel = np.clip(1 - (t - hold) / 0.45, 0, 1) ** 2
    env *= np.where(t > hold, rel, 1.0)
    env *= np.minimum(t / 0.004, 1.0)                             # click-free attack
    tone = np.zeros(n, dtype=np.float32)
    for k, a in enumerate(PARTIALS, start=1):
        f = f0 * k * (1 + 0.0004 * k * k)                         # slight string inharmonicity
        if f > SR / 2.2:
            break
        tone += a * np.exp(-t * (k - 1) * 1.6 / tau) * np.sin(2 * np.pi * f * t)
    return 0.35 * env * tone


def synthesize(notes, duration: float, tail: float = 3.0) -> np.ndarray:
    """notes: iterable of (midi pitch, start, end, velocity).  Returns mono float32."""
    return Synth(notes, duration, tail).render()


def _hold_key(pitch, start, end) -> tuple:
    hold = max(end - start, 0.05)
    step = 0.02 if hold < 1.0 else 0.1                            # coarser rounding for long notes
    return int(pitch), round(hold / step) * step


class Synth:
    """The mix of a list of notes, kept so that moving a few of them does not mean synthesising the whole
    piece again (which takes seconds): each moved note is taken out where it was and mixed in where it now is.

    Notes with the same pitch and (rounded) length sound identical apart from their loudness, so each such voice
    is computed once and mixed in at every position it is needed -- long pieces repeat the same pitch/length
    combinations thousands of times."""

    def __init__(self, notes, duration: float, tail: float = 3.0):
        self.notes = [tuple(n[:4]) for n in notes]
        self._voices: dict[tuple, np.ndarray] = {}
        self.mix = np.zeros(int((duration + tail) * SR) + SR, dtype=np.float32)
        groups: dict[tuple, list] = {}
        for pitch, start, end, vel in self.notes:
            groups.setdefault(_hold_key(pitch, start, end), []).append((start, vel))
        for key, hits in groups.items():
            voice = self._voice(key)
            for start, vel in hits:
                self._add(voice, start, vel / 127.0)
        self.peak = float(np.max(np.abs(self.mix))) or 1.0          # kept: an edit does not change the loudness

    def _voice(self, key) -> np.ndarray:
        if key not in self._voices:
            self._voices[key] = _voice(*key)
        return self._voices[key]

    def _add(self, voice, start: float, gain: float):
        i = int(max(start, 0.0) * SR)
        if i + len(voice) + 1 > len(self.mix):                      # moved past the end
            self.mix = np.concatenate([self.mix, np.zeros(i + len(voice) + 1 - len(self.mix), np.float32)])
        self.mix[i:i + len(voice)] += gain * voice

    def move(self, notes) -> int:
        """Change the notes to `notes` (the same notes, some at other times).  Returns how many moved."""
        moved = 0
        for k, (old, new) in enumerate(zip(self.notes, notes)):
            new = tuple(new[:4])
            if abs(old[1] - new[1]) < 1e-6 and abs(old[2] - new[2]) < 1e-6:
                continue
            self._add(self._voice(_hold_key(*old[:3])), old[1], -old[3] / 127.0)
            self._add(self._voice(_hold_key(*new[:3])), new[1], new[3] / 127.0)
            self.notes[k] = new
            moved += 1
        return moved

    def render(self) -> np.ndarray:
        return np.tanh(self.mix / self.peak * 1.4) / np.tanh(1.4) * 0.9


def write_wav(path, samples: np.ndarray):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
