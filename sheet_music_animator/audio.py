"""A tiny built-in piano-ish synth so the project has something to play without a soundfont."""
from __future__ import annotations

import wave

import numpy as np

SR = 44100
PARTIALS = (1.0, 0.55, 0.32, 0.2, 0.12, 0.07, 0.04)


def synthesize(notes, duration: float, tail: float = 3.0) -> np.ndarray:
    """notes: iterable of (midi pitch, start, end, velocity).  Returns mono float32."""
    out = np.zeros(int((duration + tail) * SR) + SR, dtype=np.float32)
    for pitch, start, end, vel in notes:
        f0 = 440.0 * 2 ** ((pitch - 69) / 12)
        hold = max(end - start, 0.05)
        ring = 0.35 + 0.9 * max(0.0, (84 - pitch)) / 60          # low notes ring longer
        i = int(start * SR)
        n = min(int((hold + ring * 2.2) * SR), len(out) - i - 1)
        if n <= 0:
            continue
        t = np.arange(n, dtype=np.float32) / SR
        tau = 0.35 + ring                                         # decay time constant
        env = np.exp(-t / tau)
        # key release: after `hold` fade quickly (as if a damper came down) but not instantly
        rel = np.clip(1 - (t - hold) / 0.45, 0, 1) ** 2
        env *= np.where(t > hold, rel, 1.0)
        env *= np.minimum(t / 0.004, 1.0)                         # click-free attack
        tone = np.zeros(n, dtype=np.float32)
        for k, a in enumerate(PARTIALS, start=1):
            f = f0 * k * (1 + 0.0004 * k * k)                     # slight string inharmonicity
            if f > SR / 2.2:
                break
            tone += a * np.exp(-t * (k - 1) * 1.6 / tau) * np.sin(2 * np.pi * f * t)
        out[i:i + n] += (vel / 127.0) * 0.35 * env * tone
    peak = float(np.max(np.abs(out))) or 1.0
    out = np.tanh(out / peak * 1.4) / np.tanh(1.4) * 0.9
    return out


def write_wav(path, samples: np.ndarray):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
