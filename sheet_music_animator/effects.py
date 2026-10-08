"""The produced look of the video: animated backdrop, snow, note light-up, reactions to the music.

Nothing in here needs Qt (except `title_layer`): `EffectTracks` works out everything that varies with
time (wind, shake, mood, ...) from the project and the recording's loudness, and `Compositor` turns the
score's ink (an alpha mask drawn by the Qt scene) into the finished frame.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter, gaussian_filter1d

from .engraver import NOTE_KINDS
from .project import Effects, Project

DYNAMIC_IMPULSE = {"f": 0.35, "ff": 0.6, "fff": 0.95, "ffff": 1.0, "fz": 0.6, "sf": 0.6, "sfz": 0.6,
                   "sffz": 0.8, "fp": 0.3, "rf": 0.4, "rfz": 0.5, "sfp": 0.4}
ACCENT_IMPULSE = {"acc": 0.32, "marc": 0.55}
# (count, speed, radius, alpha) of the three parallax layers of snow; positions are in pixels of a 1080p frame
SNOW_LAYERS = [(300, 0.45, 1, 0.3), (140, 0.8, 2, 0.42), (40, 1.4, 3, 0.5)]


def rgb(hex_color: str) -> np.ndarray:
    h = hex_color.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


def smooth(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


def ramp(x, a, w):
    return np.clip((x - a) / max(w, 1e-6), 0, 1)


# ====================================================================================== time signals
@dataclass
class Flash:
    t: float
    rect: tuple      # page space x0, y0, x1, y1
    color: int       # 0 / 1: index into Effects.flash_colors


class EffectTracks:
    """Everything that changes with time, sampled once per video frame."""

    def __init__(self, project: Project, score, fps: int, duration: float, loudness=None):
        fx = project.effects
        self.fps = fps
        self.n = max(int(math.ceil(duration * fps)), 1)
        TT = self.TT = np.arange(self.n) / fps
        n = self.n

        self.storm, self.hush, self.lift = (project.sample_effect(c, TT) for c in ("mood", "hush", "lift"))

        # loudness of the recording (RMS, mapped from -40..-14 dB to 0..1) and how many notes are starting
        if loudness is not None:
            rms, rfps = loudness
            db = 20 * np.log10(np.interp(TT, np.arange(len(rms)) / rfps, gaussian_filter1d(rms, 0.25 * rfps)) + 1e-6)
            self.loud = np.clip((db + 40) / 26, 0, 1).astype(np.float32)
        else:
            self.loud = None
        starts = np.array(sorted(project.start_of(u) for u in score.units if u.kind in NOTE_KINDS))
        dens = np.histogram(starts, bins=np.append(TT, TT[-1] + 1 / fps))[0].astype(np.float64) * fps
        self.dens = np.clip(gaussian_filter1d(dens, 0.4 * fps) / 14, 0, 1).astype(np.float32)
        if self.loud is None:       # no recording: the amount of music stands in for the loudness
            self.loud = np.clip(0.25 + 0.75 * self.dens, 0, 1).astype(np.float32)
        activity = np.clip(fx.react * (0.55 * self.loud + 0.45 * self.dens), 0, 1)
        wind = self.storm * (0.18 + 0.82 * activity) + (1 - self.storm) * 0.04
        self.wind = gaussian_filter1d(wind, 0.15 * fps).astype(np.float32)

        # events: dynamics, accents, hand-placed ones
        imps = []
        for u in score.units:
            if fx.use_dynamics and u.kind == "dynam" and u.label in DYNAMIC_IMPULSE:
                imps.append((project.start_of(u), DYNAMIC_IMPULSE[u.label]))
            elif fx.use_accents and u.kind == "artic" and u.label in ACCENT_IMPULSE:
                imps.append((project.start_of(u), ACCENT_IMPULSE[u.label]))
        for im in fx.impulses:
            imps.append((float(im["t"]), float(im["s"])))
        for h in fx.hits:
            lo, hi = sorted((int(h["m0"]), int(h["m1"])))
            for u in score.units:
                if u.kind in NOTE_KINDS and lo <= u.measure <= hi:
                    imps.append((project.start_of(u), float(h["s"])))
        imps.sort()
        self.impulses = imps
        shake, punch, flash_light = np.zeros(n), np.zeros(n), np.zeros(n)
        span = int(2.5 * fps)
        for t0, s in imps:
            k0 = int(round(t0 * fps))
            if k0 >= n or k0 < 0:
                continue
            dt = TT[k0:k0 + span] - t0
            m = len(dt)
            shake[k0:k0 + m] = np.maximum(shake[k0:k0 + m], s * np.exp(-dt / 0.22))
            punch[k0:k0 + m] = np.maximum(punch[k0:k0 + m], s * np.exp(-dt / 0.35))
            if s >= 0.55:
                flash_light[k0:k0 + m] = np.maximum(flash_light[k0:k0 + m], (s ** 1.5) * np.exp(-dt / 0.13))
        shake = shake + self.storm * 0.12 * np.clip((self.loud - 0.6) / 0.4, 0, 1)    # constant tremor when loudest
        rng = np.random.default_rng(3)
        ph = rng.uniform(0, 6.28, 6)
        nx = (np.sin(TT * 53 + ph[0]) + 0.6 * np.sin(TT * 91 + ph[1]) + 0.4 * np.sin(TT * 137 + ph[2])) / 2
        ny = (np.sin(TT * 47 + ph[3]) + 0.6 * np.sin(TT * 83 + ph[4]) + 0.4 * np.sin(TT * 149 + ph[5])) / 2
        self.shake_x = (24 * fx.shake * shake * nx).astype(np.float32)     # pixels of a 1080p frame
        self.shake_y = (18 * fx.shake * shake * ny).astype(np.float32)
        self.flash_light = flash_light.astype(np.float32)
        self.zoom = (1 + self.storm * (0.05 * fx.breathe * self.loud + 0.06 * fx.punch * punch)).astype(np.float32)

        # spotlights, vignette, text, fades
        self.spot = [smooth((TT - (sp["t"] - 0.12)) / max(sp.get("ramp", 0.55), 0.01))
                     * ((1 - smooth((TT - sp["end"]) / 0.3)) if sp.get("end", 0) > 0 else 1.0)
                     for sp in fx.spotlights]
        vig = (1 - self.storm) * (0.55 + 0.25 * self.hush) + self.storm * (0.3 + 0.35 * self.loud)
        for fe in self.spot:
            vig = np.maximum(vig, 0.6 * fe)
        self.vig = (vig * fx.vignette).astype(np.float32)
        t_in, d_in = fx.title_in
        a = smooth((TT - t_in) / max(d_in, 1e-3))
        if fx.title_out[0] > 0:
            a = a * (1 - smooth((TT - fx.title_out[0]) / max(fx.title_out[1], 1e-3)))
        self.title_a = a.astype(np.float32)
        end = TT[-1] + 1 / fps
        self.endfade = smooth((TT - (end - fx.fade_out)) / max(fx.fade_out * 0.95, 1e-3)) if fx.fade_out > 0 \
            else np.zeros(n, np.float32)

    def camera(self, k: int, pose: tuple, W: int, H: int) -> tuple:
        """The camera pose for frame k with this frame's zoom reaction and shake applied."""
        cx, cy, w, h, rot = pose
        z = float(self.zoom[min(k, self.n - 1)])
        w2, h2 = w / z, h / z
        scl = W / w2
        k = min(k, self.n - 1)
        s = H / 1080.0
        return (cx - self.shake_x[k] * s / scl, cy - self.shake_y[k] * s / scl, w2, h2, rot)


def build_flashes(project: Project, score) -> list[Flash]:
    """Every note that is struck (not merely tied over), sorted by time."""
    out = []
    for u in score.units:
        if not u.heads:
            continue
        t = project.start_of(u)
        for x0, y0, x1, y1, staff, tied in u.heads:
            if tied:
                continue
            pad = (y1 - y0) * 0.12
            out.append(Flash(t, (x0 - 6, y0 - pad, x1 + 6, y1 + pad), min(staff, 1)))
    out.sort(key=lambda f: f.t)
    return out


# ====================================================================================== compositing
def view_matrix(pose, W, H):
    """2x3 matrix taking page coordinates to pixels for a camera pose (cx, cy, w, h, rot)."""
    cx, cy, w, h, rot = pose
    s = W / w
    a = math.radians(rot)
    c, si = math.cos(-a) * s, math.sin(-a) * s     # the picture turns the opposite way to the camera
    # pixel = R * (page - centre) * s + (W/2, H/2); R rotates by -rot
    return np.array([[c, -si, W / 2 - (c * cx - si * cy)],
                     [si, c, H / 2 - (si * cx + c * cy)]], np.float32)


def title_layer(fx: Effects, W: int, H: int):
    """Alpha mask (float32 HxW) of the title and subtitle, or None."""
    if not (fx.title or fx.subtitle):
        return None
    from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter
    img = QImage(W, H, QImage.Format_Grayscale8)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
    p.setPen(QColor(255, 255, 255))
    if fx.title:
        f = QFont(fx.title_font)
        f.setPixelSize(max(int(H * 0.062), 8))
        f.setLetterSpacing(QFont.AbsoluteSpacing, H * 0.016)
        p.setFont(f)
        r = QFontMetricsF(f).boundingRect(fx.title.upper())
        p.drawText(int((W - r.width()) / 2), int(H * 0.145), fx.title.upper())
    if fx.subtitle:
        f = QFont(fx.title_font)
        f.setPixelSize(max(int(H * 0.026), 6))
        f.setItalic(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, H * 0.002)
        p.setFont(f)
        r = QFontMetricsF(f).boundingRect(fx.subtitle)
        p.drawText(int((W - r.width()) / 2), int(H * 0.2), fx.subtitle)
    p.end()
    a = np.frombuffer(img.constBits(), np.uint8).reshape(H, img.bytesPerLine())[:, :W]
    return a.astype(np.float32) / 255.0


class Compositor:
    def __init__(self, project: Project, score, tracks: EffectTracks, W: int, H: int, fps: int):
        self.fx = fx = project.effects
        self.project, self.score, self.tr = project, score, tracks
        self.W, self.H, self.fps = W, H, fps
        self.S = H / 1080.0
        self.flashes = build_flashes(project, score) if fx.flash else []
        self.flash_t = [f.t for f in self.flashes]
        self.flash_rgb = [rgb(c) for c in fx.flash_colors]
        # ---- static buffers
        small = (max(W // 4, 2), max(H // 4, 2))
        self.small = small
        grad = (np.arange(small[1], dtype=np.float32) / small[1])[:, None, None]
        self.bg_calm = rgb(fx.bg_calm[0]) * (1 - grad) + rgb(fx.bg_calm[1]) * grad
        self.bg_storm = rgb(fx.bg_storm[0]) * (1 - grad) + rgb(fx.bg_storm[1]) * grad
        rng = np.random.default_rng(11)
        mw = 2 * small[0]
        noise = gaussian_filter(rng.standard_normal((max(small[1] // 2, 2), max(mw // 2, 4))), (5, 9), mode="wrap")
        noise = (noise - noise.min()) / (noise.max() - noise.min() + 1e-9)
        noise = cv2.resize(noise.astype(np.float32), (mw, small[1]), interpolation=cv2.INTER_CUBIC)
        self.mist = np.concatenate([noise, noise[:, :small[0]]], axis=1)
        self.mw = mw
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        rr = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
        self.vigm = (np.clip((rr - 0.3) / 0.95, 0, 1) ** 1.5).astype(np.float32)          # (H, W)
        self.flash_light_map = (1 - 0.5 * self.vigm).astype(np.float32)
        self.dither = np.repeat((rng.random((H, W, 1), dtype=np.float32) - 0.5), 3, axis=2)
        self._oma = np.empty((H, W), np.float32)         # work buffers (the frame is built in place)
        self._tmp = np.empty((H, W), np.float32)
        self._fm = [np.zeros((H, W), np.float32) for _ in range(2)]   # light-up masks, cleared where they were drawn
        self._fm_box = None
        self.xcol = np.arange(W, dtype=np.float32)
        self.title = title_layer(fx, W, H)
        self.title_rows = None
        if self.title is not None:
            rows = np.nonzero(self.title.max(axis=1) > 0)[0]
            self.title_rows = (int(rows[0]), int(rows[-1]) + 1) if len(rows) else None
        # ---- snow: a simulation that is stepped frame by frame (checkpoints make seeking cheap)
        self._snow_rng = np.random.default_rng(11)
        self._snow = self._new_snow()
        self._snow_k = -1
        self._snow_ckpt: dict[int, list] = {}
        self.mist_off = np.cumsum(tracks.wind * 14 * self.S * 60 / fps + 0.4 * self.S * 60 / fps) / 4
        self.lift_px = 7.0

    # ---------------------------------------------------------------------------- snow
    def _new_snow(self):
        rng = np.random.default_rng(11)
        layers = []
        for n, spd, rad, al in SNOW_LAYERS:
            n = max(int(n * self.fx.snow), 0)
            layers.append(dict(x=rng.uniform(0, self.W, n), y=rng.uniform(0, self.H, n), ph=rng.uniform(0, 6.28, n),
                               spd=spd * rng.uniform(0.7, 1.3, n), rad=rad, al=al, vx=np.zeros(n), vy=np.zeros(n)))
        return layers

    def _step_snow(self, k):
        w, t = float(self.tr.wind[min(k, self.tr.n - 1)]), k / self.fps
        up = float(self.tr.lift[min(k, self.tr.n - 1)]) * self.lift_px
        sc = self.S * 60.0 / self.fps
        for L in self._snow:
            L["vx"] = -(0.6 + 30 * w) * L["spd"] * sc
            L["vy"] = ((0.9 + 1.2 * (1 - w)) * L["spd"] - up * L["spd"] + 1.6 * w * np.sin(t * 2.3 + L["ph"]) * L["spd"]) * sc
            L["x"] = L["x"] + L["vx"]
            L["y"] = L["y"] + L["vy"]
            L["x"][L["x"] < -40] += self.W + 80
            L["x"][L["x"] > self.W + 40] -= self.W + 80
            L["y"][L["y"] > self.H + 20] -= self.H + 40
            L["y"][L["y"] < -20] += self.H + 40

    def seek_snow(self, k: int):
        """Bring the snow to frame k (stepping from the nearest earlier checkpoint)."""
        if k == self._snow_k:
            return
        if k < self._snow_k or self._snow_k < 0:
            base = max([c for c in self._snow_ckpt if c <= k], default=-1)
            if base >= 0:
                self._snow = [{key: (v.copy() if isinstance(v, np.ndarray) else v) for key, v in L.items()}
                              for L in self._snow_ckpt[base]]
                self._snow_k = base
            else:
                self._snow, self._snow_k = self._new_snow(), -1
        while self._snow_k < k:
            self._snow_k += 1
            self._step_snow(self._snow_k)
            if self._snow_k % 300 == 0:
                self._snow_ckpt[self._snow_k] = [{key: (v.copy() if isinstance(v, np.ndarray) else v) for key, v in L.items()}
                                                 for L in self._snow]

    def _draw_snow(self, layers):
        lay = np.zeros((self.H, self.W), np.uint8)
        for li in layers:
            if li >= len(self._snow):
                continue
            L = self._snow[li]
            x0, y0 = L["x"], L["y"]
            x1, y1 = x0 - L["vx"] * 1.4, y0 - L["vy"] * 1.4
            lv = int(255 * L["al"])
            th = max(int(round(L["rad"] * self.S)), 1)
            for a, b, c, d in zip(x0.astype(int), y0.astype(int), x1.astype(int), y1.astype(int)):
                cv2.line(lay, (a, b), (c, d), lv, th, cv2.LINE_AA)
        return cv2.GaussianBlur(lay, (0, 0), 1.0 * self.S).astype(np.float32) / 255.0

    # ---------------------------------------------------------------------------- flashes
    def _flash_masks(self, k, M):
        """Draw the light-up masks of the two colours (persistent buffers); returns the box (x0, y0, x1, y1) they
        occupy, or None when nothing is lit."""
        if self._fm_box is not None:       # clear what the previous frame drew
            x0, y0, x1, y1 = self._fm_box
            for m in self._fm:
                m[y0:y1, x0:x1] = 0
            self._fm_box = None
        t = k / self.fps
        fd = self.fx.flash_time
        i0 = bisect.bisect_left(self.flash_t, t - fd)
        lo = [self.W, self.H]
        hi = [0, 0]
        for f in self.flashes[i0:]:
            if f.t > t:
                break
            kk = (1 - (t - f.t) / fd) ** 1.6
            x0, y0, x1, y1 = f.rect
            px = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32) @ M[:, :2].T + M[:, 2]
            mn, mx = px.min(axis=0), px.max(axis=0)
            if mx[0] < 0 or mn[0] > self.W or mx[1] < 0 or mn[1] > self.H:
                continue
            cv2.fillConvexPoly(self._fm[f.color], np.round(px).astype(np.int32), float(kk))   # newer = brighter
            lo = [min(lo[0], mn[0]), min(lo[1], mn[1])]
            hi = [max(hi[0], mx[0]), max(hi[1], mx[1])]
        if hi[0] <= lo[0]:
            return None
        self._fm_box = (max(int(lo[0]) - 2, 0), max(int(lo[1]) - 2, 0),
                        min(int(hi[0]) + 3, self.W), min(int(hi[1]) + 3, self.H))
        return self._fm_box

    # ---------------------------------------------------------------------------- the frame
    def frame(self, k: int, alpha: np.ndarray, pose: tuple, spot_rects=None) -> np.ndarray:
        """alpha: uint8 HxW coverage of the score (0..255) rendered for `pose`; returns uint8 HxWx3.
        The frame is built as three float planes with in-place OpenCV arithmetic (much faster than numpy
        broadcasting over HxWx3 temporaries)."""
        fx, tr, W, H = self.fx, self.tr, self.W, self.H
        k = min(k, tr.n - 1)
        st = float(tr.storm[k])
        wind = float(tr.wind[k])
        hush = float(tr.hush[k])
        oma, tmp = self._oma, self._tmp
        # ---- backdrop, drawn small and enlarged (it is all soft)
        bg = self.bg_calm * (1 - st) + self.bg_storm * st
        if fx.mist > 0:
            off = int(self.mist_off[k]) % self.mw
            mis = self.mist[:, off:off + self.small[0]][..., None] * fx.mist
            bg = bg * (1 + (0.25 + 0.9 * wind) * mis) + (6 + 28 * wind) * mis * np.array([0.7, 0.85, 1.0], np.float32)
        bg = bg * (1 - 0.35 * hush)
        P = [cv2.resize(np.ascontiguousarray(c), (W, H), interpolation=cv2.INTER_LINEAR) for c in cv2.split(bg.astype(np.float32))]
        if fx.snow > 0:
            sn = self._draw_snow((0, 1))
            col = np.array([200, 215, 235], np.float32) * (0.35 + 0.65 * max(st, 0.4))
            for c in range(3):
                cv2.scaleAdd(sn, float(col[c]), P[c], dst=P[c])
        # ---- the score
        edge = np.ones(W, np.float32)
        if fx.edge_fade > 0:
            e = fx.edge_fade * W
            edge = ramp(self.xcol, 0, e) * ramp(W - self.xcol, 0, e)
        for si, fe in enumerate(tr.spot):
            f = float(fe[k])
            if f > 0 and spot_rects and spot_rects[si] is not None:
                l, r = spot_rects[si]
                win = ramp(self.xcol, l - 30, 60) * ramp(r + 30 - self.xcol, 0, 60)
                edge = edge * (1 - f) + np.minimum(edge, win) * f
        a = alpha * (edge / 255.0).astype(np.float32)[None, :]              # float32 coverage with the side fades
        inkc = rgb(fx.ink_calm) * (1 - st) + rgb(fx.ink_storm) * st
        np.subtract(1.0, a, out=oma)
        for c in range(3):
            cv2.multiply(P[c], oma, dst=P[c])
            cv2.scaleAdd(a, float(inkc[c]), P[c], dst=P[c])
        box = self._flash_masks(k, view_matrix(pose, W, H)) if self.flashes else None
        if box is not None:
            x0, y0, x1, y1 = box
            for m, col in zip(self._fm, self.flash_rgb):
                reg = (slice(y0, y1), slice(x0, x1))
                lit = a[reg] * m[reg]
                for c in range(3):
                    P[c][reg] += lit * float(col[c] - inkc[c])
            if fx.glow > 0:
                q = (W // 4, H // 4)
                a4 = cv2.resize(a, q, interpolation=cv2.INTER_AREA)
                g = np.dstack([cv2.resize(m, q, interpolation=cv2.INTER_AREA) * a4 for m in self._fm])
                if g.max() > 0:
                    g = cv2.GaussianBlur(g, (0, 0), 3.5 * self.S)
                    g0 = cv2.resize(np.ascontiguousarray(g[..., 0]), (W, H), interpolation=cv2.INTER_LINEAR)
                    g1 = cv2.resize(np.ascontiguousarray(g[..., 1]), (W, H), interpolation=cv2.INTER_LINEAR)
                    for c in range(3):
                        cv2.scaleAdd(g0, float(fx.glow * self.flash_rgb[0][c]), P[c], dst=P[c])
                        cv2.scaleAdd(g1, float(fx.glow * self.flash_rgb[1][c]), P[c], dst=P[c])
        if fx.snow > 0:
            sn2 = self._draw_snow((2,))
            col = np.array([215, 228, 245], np.float32) * 0.75 * max(st, 0.35)
            for c in range(3):
                cv2.scaleAdd(sn2, float(col[c]), P[c], dst=P[c])
        fl = float(tr.flash_light[k])
        if fl > 0.01:
            col = fl * 0.45 * np.array([190, 215, 255], np.float32)
            for c in range(3):
                cv2.scaleAdd(self.flash_light_map, float(col[c]), P[c], dst=P[c])
        v = float(tr.vig[k])
        if v > 0.001:
            np.multiply(self.vigm, v, out=tmp)                               # the dark-corner mask
            np.subtract(1.0, tmp, out=oma)
            vc = (2.0, 4.0, 10.0)
            for c in range(3):
                cv2.multiply(P[c], oma, dst=P[c])
                cv2.scaleAdd(tmp, vc[c], P[c], dst=P[c])
        ta = float(tr.title_a[k])
        if self.title is not None and self.title_rows and ta > 0.001:
            r0, r1 = self.title_rows
            tt = self.title[r0:r1] * ta
            tc = rgb(fx.title_color)
            for c in range(3):
                P[c][r0:r1] = P[c][r0:r1] * (1 - tt) + tt * float(tc[c])
        out = cv2.merge(P)
        ef = float(tr.endfade[k])
        if ef > 0:
            out *= 1 - ef
        cv2.add(out, self.dither, dst=out)
        np.clip(out, 0, 255, out=out)
        return out.astype(np.uint8)
