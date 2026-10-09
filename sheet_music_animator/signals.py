"""Signals of the music (and your own automation lanes) that effect settings can follow.

Every signal is an array with one value per video frame.  A *binding* makes a setting follow one:
``value = setting + amount * curve(signal)`` (smoothed over `smooth` seconds); a setting may have several.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.ndimage import gaussian_filter1d

from .engraver import NOTE_KINDS

DYNAMIC_EVENT = {"f": 0.35, "ff": 0.6, "fff": 0.95, "ffff": 1.0, "fz": 0.6, "sf": 0.6, "sfz": 0.6,
                 "sffz": 0.8, "fp": 0.3, "rf": 0.4, "rfz": 0.5, "sfp": 0.4}
ACCENT_EVENT = {"acc": 0.32, "marc": 0.55}
LANE = "lane:"


def curve_of(x: np.ndarray, name: str | None) -> np.ndarray:
    if name == "squared":
        return x * x
    if name == "square root":
        return np.sqrt(np.clip(x, 0, None))
    if name == "inverted":
        return 1.0 - x
    return x


class Signals:
    def __init__(self, project, score, fps: int, duration: float, loudness=None):
        fx = project.effects
        self.fps = fps
        self.n = n = max(int(math.ceil(duration * fps)), 1)
        TT = self.TT = np.arange(n) / fps
        self.duration = n / fps
        s: dict[str, np.ndarray] = {}

        if loudness is not None:
            rms, rfps = loudness
            db = 20 * np.log10(np.interp(TT, np.arange(len(rms)) / rfps, gaussian_filter1d(rms, 0.25 * rfps)) + 1e-6)
            s["loudness"] = np.clip((db - fx.loud_floor_db) / max(fx.loud_range_db, 1.0), 0, 1)
        starts = np.array(sorted(project.start_of(u) for u in score.units if u.kind in NOTE_KINDS))
        dens = np.histogram(starts, bins=np.append(TT, TT[-1] + 1 / fps))[0].astype(np.float64) * fps
        s["density"] = np.clip(gaussian_filter1d(dens, 0.4 * fps) / max(fx.density_max, 1.0), 0, 1)
        if "loudness" not in s:        # no recording: the amount of music stands in for how loud it is
            s["loudness"] = np.clip(0.25 + 0.75 * s["density"], 0, 1)
        s["activity"] = np.clip(0.55 * s["loudness"] + 0.45 * s["density"], 0, 1)

        imps = []
        for u in score.units:
            if fx.use_dynamics and u.kind == "dynam" and u.label in DYNAMIC_EVENT:
                imps.append((project.start_of(u), DYNAMIC_EVENT[u.label]))
            elif fx.use_accents and u.kind == "artic" and u.label in ACCENT_EVENT:
                imps.append((project.start_of(u), ACCENT_EVENT[u.label]))
        for im in fx.impulses:
            imps.append((float(im["t"]), float(im["s"])))
        for h in fx.hits:
            lo, hi = sorted((int(h["m0"]), int(h["m1"])))
            imps += [(project.start_of(u), float(h["s"])) for u in score.units
                     if u.kind in NOTE_KINDS and lo <= u.measure <= hi]
        imps.sort()
        self.events_list = imps
        ev, slow, big, on = (np.zeros(n) for _ in range(4))
        span = int(2.5 * fps)

        def stamp(arr, t0, v, tau):
            k0 = int(round(t0 * fps))
            if 0 <= k0 < n:
                dt = TT[k0:k0 + span] - t0
                arr[k0:k0 + len(dt)] = np.maximum(arr[k0:k0 + len(dt)], v * np.exp(-dt / tau))
        for t0, v in imps:
            stamp(ev, t0, v, 0.22)
            stamp(slow, t0, v, 0.35)
            if v >= 0.55:
                stamp(big, t0, v ** 1.5, 0.13)
        for t0 in starts:
            stamp(on, float(t0), 1.0, 0.12)
        s.update(events=ev, events_slow=slow, impacts=big, onsets=on)

        # pitch height of what is being played (0 = low, 1 = high)
        num, den = np.zeros(n), np.zeros(n)
        for pitch, a, *_ in score.notes:
            k = int(round(a * fps))
            if 0 <= k < n:
                num[k] += (pitch - 36) / 60.0
                den[k] += 1
        num, den = gaussian_filter1d(num, 0.4 * fps), gaussian_filter1d(den, 0.4 * fps)
        s["pitch"] = np.clip(np.where(den > 1e-4, num / np.maximum(den, 1e-9), 0.5), 0, 1)
        s["progress"] = TT / max(self.duration, 1e-6)
        self.s = {k: v.astype(np.float32) for k, v in s.items()}
        for name in fx.lanes:
            self.s[LANE + name] = project.sample_lane(name, TT)

    def get(self, name: str):
        return self.s.get(name)

    def evaluate(self, base, bindings, lo=None, hi=None):
        """The value of a setting: a float without bindings, else an array with one value per frame."""
        if not bindings:
            return float(base)
        out = np.full(self.n, float(base), np.float64)
        for b in bindings:
            sig = self.s.get(b.get("src"))
            if sig is None:
                continue
            x = float(b.get("amount", 1.0)) * curve_of(sig.astype(np.float64), b.get("curve"))
            if b.get("smooth", 0) > 0:
                x = gaussian_filter1d(x, b["smooth"] * self.fps)
            out += x
        if lo is not None and hi is not None:
            out = np.clip(out, lo, hi)
        return out.astype(np.float32)


class CameraMods:
    """What the camera layers (shake, zoom, sway) do to the camera, per frame."""

    def __init__(self, layers, signals: Signals, params):
        n, TT = signals.n, signals.TT
        self.shake_x, self.shake_y = np.zeros(n, np.float32), np.zeros(n, np.float32)
        self.zoom, self.rot = np.zeros(n, np.float32), np.zeros(n, np.float32)
        rng = np.random.default_rng(3)
        ph = rng.uniform(0, 6.28, 6)
        for lay in layers:
            if not lay.enabled:
                continue
            if lay.type == "shake":
                amount = params(lay, "amount")
                sp = float(lay.get("speed"))
                T = TT * sp
                nx = (np.sin(T * 53 + ph[0]) + 0.6 * np.sin(T * 91 + ph[1]) + 0.4 * np.sin(T * 137 + ph[2])) / 2
                ny = (np.sin(T * 47 + ph[3]) + 0.6 * np.sin(T * 83 + ph[4]) + 0.4 * np.sin(T * 149 + ph[5])) / 2
                self.shake_x += (amount * nx).astype(np.float32)
                self.shake_y += (amount * 0.75 * ny).astype(np.float32)
            elif lay.type == "zoom":
                self.zoom += np.asarray(params(lay, "zoom"), np.float32)
            elif lay.type == "sway":
                deg = params(lay, "degrees")
                self.rot += (deg * np.sin(2 * np.pi * TT / max(float(lay.get("period")), 0.1))).astype(np.float32)

    def apply(self, k: int, pose: tuple, W: int, H: int) -> tuple:
        """The camera pose (cx, cy, w, h, rot) of frame k with the camera layers' motion on top."""
        cx, cy, w, h, rot = pose
        k = min(k, len(self.zoom) - 1)
        z = max(1.0 + float(self.zoom[k]), 0.05)
        w2, h2 = w / z, h / z
        scl = W / w2
        s = H / 1080.0
        return (cx - float(self.shake_x[k]) * s / scl, cy - float(self.shake_y[k]) * s / scl, w2, h2,
                rot + float(self.rot[k]))
