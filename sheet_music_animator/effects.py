"""Draws a stack of effect layers (see layers.py) into finished video frames.

Nothing here needs the editor: `Compositor` takes the project, the score, the loudness of the recording and,
for each frame, the score's ink as an alpha mask drawn by Qt.  The frame is built as three float planes
(red, green, blue; 0..255) by drawing the layers bottom to top with the usual blend modes.
"""
from __future__ import annotations

import bisect
import colorsys
import math
import os
import re
import subprocess
from dataclasses import dataclass

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter

from .layers import LAYER_TYPES, schema
from .signals import CameraMods, Signals

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def rgb(hex_color) -> np.ndarray:
    h = str(hex_color).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32)
    except ValueError:
        return np.zeros(3, np.float32)


def lerp(a, b, t):
    return a * (1 - t) + b * t


def smooth(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


def ramp(x, a, w):
    return np.clip((x - a) / max(w, 1e-6), 0, 1)


def view_matrix(pose, W, H):
    """2x3 matrix taking page coordinates to pixels for a camera pose (cx, cy, w, h, rot)."""
    cx, cy, w, h, rot = pose
    s = W / w
    a = math.radians(rot)
    c, si = math.cos(-a) * s, math.sin(-a) * s
    return np.array([[c, -si, W / 2 - (c * cx - si * cy)],
                     [si, c, H / 2 - (si * cx + c * cy)]], np.float32)


@dataclass
class Flash:
    t: float
    rect: tuple      # page space x0, y0, x1, y1
    staff: int
    pitch: int
    measure: int


def build_flashes(project, score) -> list[Flash]:
    """Every note that is struck (not merely tied over), sorted by time."""
    out = []
    for u in score.units:
        if not u.heads:
            continue
        t = project.start_of(u)
        for x0, y0, x1, y1, staff, tied, pitch in u.heads:
            if not tied:
                pad = (y1 - y0) * 0.12
                out.append(Flash(t, (x0 - 6, y0 - pad, x1 + 6, y1 + pad), min(staff, 1), pitch, u.measure))
    out.sort(key=lambda f: f.t)
    return out


# ====================================================================================== canvas
class Canvas:
    """Three float planes (R, G, B; 0..255) and the blend modes."""

    def __init__(self, W, H):
        self.W, self.H = W, H
        self.P = [np.zeros((H, W), np.float32) for _ in range(3)]
        self._a = np.empty((H, W), np.float32)
        self._t = np.empty((H, W), np.float32)
        self.up = [np.empty((H, W), np.float32) for _ in range(3)]
        self.dither = np.repeat(np.random.default_rng(5).random((H, W, 1), dtype=np.float32) - 0.5, 3, axis=2)

    def clear(self):
        for p in self.P:
            p[:] = 0

    def upscale(self, small):
        """Three small planes -> three full-size planes (a reused buffer)."""
        for c in range(3):
            cv2.resize(small[c], (self.W, self.H), dst=self.up[c], interpolation=cv2.INTER_LINEAR)
        return self.up

    def blend(self, src, alpha=None, mode="normal", opacity=1.0):
        """Draw `src` (a colour: array of 3, or a list of three planes) with `alpha` (None, a number, or a plane)."""
        planes = isinstance(src, list)
        if isinstance(alpha, np.ndarray):
            A = alpha
            if opacity != 1.0:
                A = np.multiply(alpha, opacity, out=self._a)
            s = None
        else:
            s = (1.0 if alpha is None else float(alpha)) * opacity
            if s <= 0.0005:
                return
            A = None
        t = self._t
        for c in range(3):
            P = self.P[c]
            sc = src[c] if planes else float(src[c])
            if mode == "add":
                if A is None:
                    if planes:
                        cv2.scaleAdd(sc, s, P, dst=P)
                    else:
                        P += sc * s
                elif planes:
                    cv2.multiply(sc, A, dst=t)
                    cv2.add(P, t, dst=P)
                else:
                    cv2.scaleAdd(A, sc, P, dst=P)
            elif mode == "multiply":
                if planes:
                    np.multiply(sc, 1 / 255.0, out=t)
                    t -= 1
                    t *= A if A is not None else s
                    t += 1
                elif A is None:
                    P *= 1 + s * (sc / 255.0 - 1)
                    continue
                else:
                    np.multiply(A, sc / 255.0 - 1, out=t)
                    t += 1
                cv2.multiply(P, t, dst=P)
            elif mode == "screen":       # P + S - P*S/255 with S = src * alpha
                if planes:
                    cv2.multiply(sc, A, dst=t) if A is not None else np.multiply(sc, s, out=t)
                    P += t - P * t / 255.0
                elif A is None:
                    P *= 1 - sc * s / 255.0
                    P += sc * s
                else:
                    np.multiply(A, sc / 255.0, out=t)
                    np.subtract(1.0, t, out=t)
                    cv2.multiply(P, t, dst=P)
                    cv2.scaleAdd(A, sc, P, dst=P)
            else:                        # normal
                if A is None:
                    if planes:
                        cv2.addWeighted(P, 1 - s, sc, s, 0, dst=P)
                    elif s >= 0.999:
                        P[:] = sc
                    else:
                        P *= 1 - s
                        P += sc * s
                else:
                    np.subtract(1.0, A, out=t)
                    cv2.multiply(P, t, dst=P)
                    if planes:
                        cv2.multiply(sc, A, dst=t)
                        cv2.add(P, t, dst=P)
                    else:
                        cv2.scaleAdd(A, sc, P, dst=P)

    def finish(self) -> np.ndarray:
        out = cv2.merge(self.P)
        cv2.add(out, self.dither, dst=out)
        np.clip(out, 0, 255, out=out)
        return out.astype(np.uint8)


# ====================================================================================== layer runtime
class LayerRT:
    """A layer with its settings resolved for every frame (bindings evaluated, timing envelope made)."""

    def __init__(self, layer, comp):
        self.layer, self.comp = layer, comp
        self.arr = {}
        for p in schema(layer.type):
            bs = layer.bindings.get(p.name)
            if p.kind == "float" and bs:
                self.arr[p.name] = comp.sig.evaluate(layer.get(p.name), bs, p.lo, p.hi)
        n, TT, dur = comp.n, comp.sig.TT, comp.sig.duration
        self.env = np.ones(n, np.float32)
        if LAYER_TYPES[layer.type].common:
            start, end = float(layer.get("start")), float(layer.get("end"))
            start = dur + start if start < 0 else start
            end = (dur + end if end < 0 else end) if end != 0 else 0.0
            fi, fo = float(layer.get("fade_in")), float(layer.get("fade_out"))
            e = smooth((TT - start) / fi) if fi > 0 else (TT >= start).astype(np.float64)
            if end:
                e = e * ((1 - smooth((TT - end) / fo)) if fo > 0 else (TT < end))
            self.env = e.astype(np.float32)
        self.drawer = None
        self._ints = {}

    def v(self, name, k):
        a = self.arr.get(name)
        return float(a[k]) if a is not None else float(self.layer.get(name))

    def const(self, name):
        return self.layer.get(name)

    def opacity(self, k):
        return (self.v("opacity", k) if LAYER_TYPES[self.layer.type].common else 1.0) * float(self.env[k])

    def integral(self, name):
        """Running total of a setting over time (e.g. distance travelled at a changing speed), per frame."""
        if name not in self._ints:
            a = self.arr.get(name)
            a = np.full(self.comp.n, float(self.layer.get(name)), np.float32) if a is None else a
            self._ints[name] = np.cumsum(a.astype(np.float64)) / self.comp.fps
        return self._ints[name]


class Drawer:
    def __init__(self, rt: LayerRT, comp: "Compositor"):
        self.rt, self.comp, self.L = rt, comp, rt.layer

    def draw(self, k: int, op: float):
        raise NotImplementedError

    def close(self):
        pass


# ====================================================================================== backdrops
class SolidDrawer(Drawer):
    def draw(self, k, op):
        mix = self.rt.v("mix", k)
        self.comp.cv.blend(lerp(rgb(self.L.get("color")), rgb(self.L.get("color_b")), mix), None, self.L.blend, op)


class GradientDrawer(Drawer):
    def __init__(self, rt, comp):
        super().__init__(rt, comp)
        w, h = comp.small
        g = self.L.get
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        x, y = xs / w, ys / h                                  # both 0..1
        cx, cy = g("center_x"), g("center_y")
        if g("style") == "radial":
            t = np.sqrt(((x - cx) * comp.W / comp.H) ** 2 + (y - cy) ** 2) / max(g("radius"), 1e-3)
        else:
            a = math.radians(g("angle"))
            dx, dy = math.sin(a), math.cos(a)           # 0 deg = top to bottom
            t = ((x - 0.5) * dx * comp.W / comp.H + (y - 0.5) * dy) / (abs(dx) * comp.W / comp.H + abs(dy)) + 0.5
        self.t = np.clip(t, 0, 1).astype(np.float32)

    def draw(self, k, op):
        g, mix = self.L.get, self.rt.v("mix", k)
        a = lerp(rgb(g("top")), rgb(g("top_b")), mix)
        b = lerp(rgb(g("bottom")), rgb(g("bottom_b")), mix)
        small = [(a[c] * (1 - self.t) + b[c] * self.t).astype(np.float32) for c in range(3)]
        self.comp.cv.blend(self.comp.cv.upscale(small), None, self.L.blend, op)


class NoiseDrawer(Drawer):
    TILE = 256

    def __init__(self, rt, comp):
        super().__init__(rt, comp)
        g = self.L.get
        self.det = int(g("detail"))
        rng = np.random.default_rng(int(g("seed")))
        n = self.det * 2 + 2

        def tile():
            t = gaussian_filter(rng.standard_normal((self.TILE, self.TILE)), 8, mode="wrap")
            return ((t - t.mean()) / (t.std() + 1e-9)).astype(np.float32)
        self.tiles = [tile() for _ in range(n)]
        self.S = rt.integral("speed")
        lut = np.zeros((256, 3), np.float32)
        d, m, b = rgb(g("color_dark")), rgb(g("color_mid")), rgb(g("color_bright"))
        t = np.linspace(0, 1, 256)[:, None]
        lut[:] = np.where(t < 0.5, lerp(d, m, t * 2), lerp(m, b, (t - 0.5) * 2))
        self.lut = lut
        div = 2 if g("style") == "ridged" else 4            # thin sharp lines need more pixels than soft clouds
        w, h = max(comp.W // div, 2), max(comp.H // div, 2)
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        self.u0, self.v0 = xs / w, ys / w      # isotropic: both in units of the frame width
        self.vgrad = (1 - ys / h) ** 1.2        # for flames: strong at the bottom

    def _sample(self, tile, u, v):
        T = self.TILE
        return cv2.remap(tile, (u * T).astype(np.float32), (v * T).astype(np.float32), cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_WRAP)

    def draw(self, k, op):
        g = self.L.get
        scale, stretch, t = float(g("scale")), float(g("stretch")), self.comp.t
        th = math.radians(float(g("direction")))
        S = float(self.S[k]) * scale
        u = self.u0 * scale - math.cos(th) * S
        v = self.v0 * scale * stretch - math.sin(th) * S * stretch
        warp = float(g("warp"))
        if warp > 0:
            ph = float(g("evolve")) * t * 0.5
            wu = self._sample(self.tiles[-1], u * 0.5, v * 0.5) * math.cos(ph) + self._sample(self.tiles[-2], u * 0.5, v * 0.5) * math.sin(ph)
            wv = self._sample(self.tiles[-2], u * 0.5 + 0.37, v * 0.5) * math.cos(ph) - self._sample(self.tiles[-1], u * 0.5 + 0.37, v * 0.5) * math.sin(ph)
            u = u + warp * 0.2 * wu
            v = v + warp * 0.2 * wv
        f = np.zeros_like(u)
        var = 0.0
        for o in range(self.det):
            fr, amp = 2 ** o, 0.55 ** o
            ph = float(g("evolve")) * t * (1 + 0.5 * o)
            a = self._sample(self.tiles[2 * o], u * fr, v * fr)
            b = self._sample(self.tiles[2 * o + 1], u * fr, v * fr)
            f += amp * (a * math.cos(ph) + b * math.sin(ph))
            var += amp * amp
        z = f / math.sqrt(var) * float(g("contrast"))
        style = g("style")
        if style == "ridged":
            val = (1 - np.abs(np.tanh(z))) ** 3
        elif style == "billow":
            val = np.abs(np.tanh(z))
        elif style == "flame":
            val = (0.5 + 0.5 * np.tanh(z)) * self.vgrad * 1.6
        else:
            val = 0.5 + 0.5 * np.tanh(z * 0.9)
        val = np.clip(val + float(g("brightness")) * 0.5, 0, 1)
        col = self.lut[(val * 255).astype(np.uint8)]
        half = [np.ascontiguousarray(col[..., c]) for c in range(3)]
        self.comp.cv.blend(self.comp.cv.upscale(half), None, self.L.blend, op)


class VideoSource:
    """Frames of a video file, decoded by ffmpeg, scaled to the frame; sequential access is fast."""

    def __init__(self, path, W, H, fps, speed, loop, fit):
        import imageio_ffmpeg
        self.exe = imageio_ffmpeg.get_ffmpeg_exe()
        self.path, self.W, self.H, self.fps, self.speed, self.loop, self.fit = path, W, H, fps, speed, loop, fit
        self.proc, self.next_k, self.last = None, -1, None
        r = subprocess.run([self.exe, "-i", path], stderr=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=_NO_WINDOW)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr.decode(errors="replace"))
        self.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0

    def _start(self, k):
        self.close()
        t = k / self.fps * self.speed
        if self.duration > 0:
            t = t % self.duration if self.loop else min(t, max(self.duration - 0.1, 0))
        W, H = self.W, self.H
        scale = {"stretch": f"scale={W}:{H}", "contain": f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"}.get(
            self.fit, f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}")
        cmd = [self.exe, "-v", "error"] + (["-stream_loop", "-1"] if self.loop else []) + ["-ss", f"{t:.3f}", "-i", self.path,
               "-vf", f"setpts=PTS/{self.speed:.4f},fps={self.fps},{scale}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        self.next_k = k

    def frame(self, k):
        if self.proc is None or k != self.next_k:
            if self.proc is not None and k == self.next_k - 1 and self.last is not None:
                return self.last
            self._start(k)
        buf = self.proc.stdout.read(self.W * self.H * 3)
        if len(buf) < self.W * self.H * 3:
            return self.last if self.last is not None else np.zeros((self.H, self.W, 3), np.uint8)
        self.next_k = k + 1
        self.last = np.frombuffer(buf, np.uint8).reshape(self.H, self.W, 3)
        return self.last

    def close(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc = None


class MediaDrawer(Drawer):
    def __init__(self, rt, comp):
        super().__init__(rt, comp)
        path = str(self.L.get("file"))
        self.img, self.video = None, None
        W, H = comp.W, comp.H
        if path and os.path.exists(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"):
                data = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
                if data is not None:
                    ih, iw = data.shape[:2]
                    fit = self.L.get("fit")
                    s = (max(W / iw, H / ih) if fit == "cover" else min(W / iw, H / ih)) if fit != "stretch" else None
                    if s is None:
                        self.img = cv2.resize(data, (W, H), interpolation=cv2.INTER_AREA)
                    else:
                        base = cv2.resize(data, (max(int(iw * s), 1), max(int(ih * s), 1)), interpolation=cv2.INTER_AREA)
                        canvas = np.zeros((H, W, 3), np.uint8)
                        y0, x0 = (H - base.shape[0]) // 2, (W - base.shape[1]) // 2
                        sy, sx = max(-y0, 0), max(-x0, 0)
                        h2, w2 = min(base.shape[0] - sy, H - max(y0, 0)), min(base.shape[1] - sx, W - max(x0, 0))
                        canvas[max(y0, 0):max(y0, 0) + h2, max(x0, 0):max(x0, 0) + w2] = base[sy:sy + h2, sx:sx + w2]
                        self.img = canvas
                    self.img = cv2.cvtColor(self.img, cv2.COLOR_BGR2RGB)
            else:
                self.video = VideoSource(path, W, H, comp.fps, float(self.L.get("speed")), bool(self.L.get("loop")),
                                         self.L.get("fit"))

    def close(self):
        if self.video:
            self.video.close()

    def draw(self, k, op):
        g, comp = self.L.get, self.comp
        frame = self.img if self.img is not None else (self.video.frame(k) if self.video else None)
        if frame is None:
            return
        W, H = comp.W, comp.H
        p = k / max(comp.n - 1, 1)
        z = lerp(float(g("zoom")), float(g("end_zoom")), p)
        dx, dy = float(g("drift_x")) * p * W * 0.1, float(g("drift_y")) * p * H * 0.1
        if abs(z - 1) > 1e-3 or dx or dy:
            M = np.float32([[z, 0, W / 2 - z * W / 2 - dx], [0, z, H / 2 - z * H / 2 - dy]])
            frame = cv2.warpAffine(frame, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        blur = float(g("blur")) * comp.S
        if blur > 0.3:
            frame = cv2.GaussianBlur(frame, (0, 0), blur)
        planes = [frame[..., c].astype(np.float32) for c in range(3)]
        sat, br = float(g("saturation")), float(g("brightness"))
        if abs(sat - 1) > 1e-3:
            lum = planes[0] * 0.299 + planes[1] * 0.587 + planes[2] * 0.114
            planes = [lum + (p_ - lum) * sat for p_ in planes]
        if br:
            planes = [p_ * (1 + br) if br < 0 else p_ + br * 255 for p_ in planes]
        comp.cv.blend(planes, None, self.L.blend, op)


# ====================================================================================== particles
class ParticleDrawer(Drawer):
    COLOR_BINS = 4

    def __init__(self, rt, comp):
        super().__init__(rt, comp)
        g = self.L.get
        self.rng = np.random.default_rng(int(g("seed")))
        self.on_notes = g("emit") == "on notes"
        self.img = np.zeros((comp.H, comp.W, 3), np.uint8)
        self.S = rt.integral("speed")
        self.ca, self.cb = rgb(g("color_a")), rgb(g("color_b"))
        D = int(g("depth"))
        self.layers = []
        total = int(g("count"))
        for d in range(D):
            t = d / (D - 1) if D > 1 else 0.5
            share = (1.0 - 0.6 * t)
            self.layers.append(dict(sf=0.5 + 0.9 * t, szf=0.6 + 0.9 * t, af=0.75 + 0.5 * t, share=share))
        tot = sum(L["share"] for L in self.layers)
        for L in self.layers:
            n = max(int(round(total * L["share"] / tot)), 1) if not self.on_notes else total
            L.update(n=n, x0=self.rng.uniform(0, comp.W, n), y0=self.rng.uniform(0, comp.H, n), ph=self.rng.uniform(0, 6.28, n),
                     spd=self.rng.uniform(0.7, 1.3, n), ang=(self.rng.random(n) - 0.5), sz=1 + (self.rng.random(n) - 0.5) * 2 * float(g("size_var")),
                     ct=self.rng.random(n))
            if self.on_notes:
                break
        self.flash = [f for f in comp.flashes] if self.on_notes else []
        self.flash_t = [f.t for f in self.flash]

    # --- drawing helpers: particles grouped by colour/brightness bin so that few cv2 calls draw them all
    def _draw_group(self, img, pts_a, pts_b, color, thick, shape):
        if len(pts_a) == 0:
            return
        col = tuple(int(max(0, min(255, c))) for c in color)
        if shape in ("streak", "dot", "spark"):
            seg = np.stack([pts_a, pts_b], axis=1).astype(np.int32)
            cv2.polylines(img, list(seg), False, col, thick, cv2.LINE_AA)
            if shape == "spark":
                d = max(thick * 3, 3)
                for p in pts_a.astype(np.int32):
                    cv2.line(img, (p[0] - d, p[1]), (p[0] + d, p[1]), col, 1, cv2.LINE_AA)
                    cv2.line(img, (p[0], p[1] - d), (p[0], p[1] + d), col, 1, cv2.LINE_AA)
        else:   # ring
            for p in pts_a.astype(np.int32):
                cv2.circle(img, (int(p[0]), int(p[1])), max(thick * 2, 2), col, 1, cv2.LINE_AA)

    def draw(self, k, op):
        g, comp = self.L.get, self.comp
        S, W, H = comp.S, comp.W, comp.H
        img = self.img
        img[:] = 0
        shape = g("shape")
        size = float(g("size")) * S
        alpha = float(g("alpha"))
        nb = self.COLOR_BINS
        if not self.on_notes:
            t = comp.t
            ang0 = math.radians(float(g("angle")))
            spread = math.radians(float(g("spread")))
            speed_now = self.rt.v("speed", k)
            for L in self.layers:
                ang = ang0 + L["ang"] * spread
                dist = float(self.S[k]) * L["sf"] * L["spd"] * S
                x = L["x0"] + np.cos(ang) * dist + float(g("wobble")) * S * np.sin(t * 1.3 + L["ph"])
                y = L["y0"] + np.sin(ang) * dist + float(g("gravity")) * S * t * L["sf"]
                x = (x % (W + 80)) - 40
                y = (y % (H + 40)) - 20
                vx = np.cos(ang) * speed_now * L["sf"] * L["spd"] * S
                vy = np.sin(ang) * speed_now * L["sf"] * L["spd"] * S + float(g("gravity")) * S * L["sf"]
                ln = float(g("streak")) if shape == "streak" else 0.0
                a = np.stack([x, y], 1)
                b = np.stack([x - vx * ln, y - vy * ln], 1)
                thick = max(int(round(size * L["szf"])), 1)
                bin_ = np.minimum((L["ct"] * nb).astype(int), nb - 1)
                for j in range(nb):
                    m = bin_ == j
                    if m.any():
                        color = lerp(self.ca, self.cb, (j + 0.5) / nb) * alpha * L["af"]
                        self._draw_group(img, a[m], b[m], color, thick, shape)
        else:
            self._draw_notes(k, img, shape, size, alpha)
        soft = float(g("softness")) * S
        if soft > 0.2:
            cv2.GaussianBlur(img, (0, 0), soft, dst=img)
        planes = [img[..., c].astype(np.float32) for c in range(3)]
        if self.L.blend == "normal":        # over the picture: the brightest channel is the coverage
            cover = np.clip(np.maximum(np.maximum(planes[0], planes[1]), planes[2]) / 255.0, 0, 1)
            comp.cv.blend(planes, cover, "normal", op)
        else:
            comp.cv.blend(planes, None, self.L.blend, op)

    def _draw_notes(self, k, img, shape, size, alpha):
        g, comp = self.L.get, self.comp
        t, S = comp.t, comp.S
        life = float(g("life"))
        i0 = bisect.bisect_left(self.flash_t, t - life)
        L = self.layers[0]
        spread = math.radians(float(g("spread")))
        ang0 = math.radians(float(g("angle")))
        by_staff = g("color_by") == "staff"
        speed = self.rt.v("speed", k) * S
        grav = float(g("gravity")) * S
        ln = float(g("streak")) if shape == "streak" else 0.0
        thick = max(int(round(size)), 1)
        bins: dict = {}
        for fi in range(i0, len(self.flash)):
            f = self.flash[fi]
            if f.t > t:
                break
            u = (t - f.t) / life
            cx, cy = (f.rect[0] + f.rect[2]) / 2, (f.rect[1] + f.rect[3]) / 2
            ox = comp.M[0, 0] * cx + comp.M[0, 1] * cy + comp.M[0, 2]
            oy = comp.M[1, 0] * cx + comp.M[1, 1] * cy + comp.M[1, 2]
            rot = fi * 2.399
            ang = ang0 + L["ang"] * spread + (rot if spread >= 6.2 else 0.0)
            dist = speed * L["spd"] * life * (1 - math.exp(-4 * u)) / 4
            x = ox + np.cos(ang) * dist
            y = oy + np.sin(ang) * dist + 0.5 * grav * (u * life) ** 2
            vx, vy = np.cos(ang) * speed * L["spd"] * math.exp(-4 * u), np.sin(ang) * speed * L["spd"] * math.exp(-4 * u)
            a = np.stack([x, y], 1)
            b = np.stack([x - vx * ln, y - vy * ln], 1)
            fade = (1 - u) ** 1.5
            ib = min(int(fade * 3.999), 3)
            cbin = (0 if f.staff == 0 else 1) if by_staff else None
            for j in range(self.COLOR_BINS if cbin is None else 1):
                m = (np.minimum((L["ct"] * self.COLOR_BINS).astype(int), self.COLOR_BINS - 1) == j) if cbin is None else slice(None)
                key = (cbin if cbin is not None else 10 + j, ib)
                bins.setdefault(key, []).append((a[m], b[m]))
        for (cb, ib), items in bins.items():
            if cb < 10:
                base = self.ca if cb == 0 else self.cb
            else:
                base = lerp(self.ca, self.cb, (cb - 10 + 0.5) / self.COLOR_BINS)
            color = base * alpha * (ib + 0.5) / 4.0
            A = np.concatenate([x for x, _ in items])
            Bp = np.concatenate([y for _, y in items])
            self._draw_group(img, A, Bp, color, thick, shape)


# ====================================================================================== notation
class ScoreDrawer(Drawer):
    def draw(self, k, op):
        comp, g = self.comp, self.L.get
        color = lerp(rgb(g("color")), rgb(g("color_b")), self.rt.v("mix", k))
        comp.ink_color = color
        a = comp.ink(float(g("edge_fade")))
        comp.last_ink = a
        comp.cv.blend(color, a, self.L.blend, op)
        glow = float(g("glow"))
        if glow > 0:
            q = (comp.W // 4, comp.H // 4)
            a4 = cv2.resize(a, q, interpolation=cv2.INTER_AREA)
            a4 = cv2.GaussianBlur(a4, (0, 0), max(float(g("glow_radius")) * comp.S / 4, 0.5))
            gl = cv2.resize(a4, (comp.W, comp.H), interpolation=cv2.INTER_LINEAR)
            comp.cv.blend(color, gl, "add", glow * op)


class HighlightDrawer(Drawer):
    def __init__(self, rt, comp):
        super().__init__(rt, comp)
        g = self.L.get
        which = g("staves")
        self.fl = [f for f in comp.flashes if which == "all" or (which == "upper") == (f.staff == 0)]
        self.t = [f.t for f in self.fl]
        a, b, mode = rgb(g("color_a")), rgb(g("color_b")), g("color_mode")
        cols = []
        for f in self.fl:
            if mode == "pitch":
                cols.append(np.array(colorsys.hsv_to_rgb((f.pitch % 12) / 12.0, 0.85, 1.0), np.float32) * 255)
            elif mode == "register":
                cols.append(lerp(a, b, float(np.clip((f.pitch - 36) / 60.0, 0, 1))))
            elif mode == "single":
                cols.append(a)
            else:
                cols.append(a if f.staff == 0 else b)
        self.cols = cols
        h2, w2 = comp.H // 2, comp.W // 2
        self.hl = np.zeros((h2, w2, 3), np.float32)
        self.m = np.zeros((h2, w2), np.float32)
        self.box = None

    def _active(self, comp, dur):
        i0 = bisect.bisect_left(self.t, comp.t - dur)
        out = []
        for i in range(i0, len(self.fl)):
            f = self.fl[i]
            if f.t > comp.t:
                break
            out.append((i, f))
        return out

    def draw(self, k, op):
        comp, g = self.comp, self.L.get
        dur = float(g("duration"))
        act = self._active(comp, dur)
        shape = g("shape")
        W, H = comp.W, comp.H
        inten = self.rt.v("intensity", k)
        if shape == "glow":
            self._glow(act, dur, inten, op)
            return
        h2, w2 = H // 2, W // 2
        lc = np.zeros((h2, w2, 3), np.float32)
        size = float(g("size"))
        for i, f in act:
            u = (comp.t - f.t) / dur
            fade = (1 - u) ** 1.6 * inten
            cx, cy = (f.rect[0] + f.rect[2]) / 2, (f.rect[1] + f.rect[3]) / 2
            px = (comp.M[0, 0] * cx + comp.M[0, 1] * cy + comp.M[0, 2]) / 2
            py = (comp.M[1, 0] * cx + comp.M[1, 1] * cy + comp.M[1, 2]) / 2
            hw = abs(comp.M[0, 0]) * (f.rect[2] - f.rect[0]) / 2 / 2          # half the head width in half-res pixels
            col = tuple(float(c * fade) for c in self.cols[i])
            if px < -200 or px > w2 + 200 or py < -200 or py > h2 + 200:
                continue
            if shape == "ring":
                r = hw * (0.8 + 5.0 * size * (1 - (1 - u) ** 2))
                cv2.circle(lc, (int(px), int(py)), max(int(r), 1), col, max(int(hw * 0.35), 1), cv2.LINE_AA)
            elif shape == "flare":
                L_ = hw * 7 * size * (1 - u * 0.5)
                for dx, dy, f_ in ((1, 0, 1.0), (0, 1, 0.7), (0.7, 0.7, 0.45), (0.7, -0.7, 0.45)):
                    cv2.line(lc, (int(px - dx * L_ * f_), int(py - dy * L_ * f_)), (int(px + dx * L_ * f_), int(py + dy * L_ * f_)),
                             col, max(int(hw * 0.2), 1), cv2.LINE_AA)
                cv2.circle(lc, (int(px), int(py)), max(int(hw * 0.9), 1), col, -1, cv2.LINE_AA)
            else:   # column
                wcol = max(int(hw * 0.8 * size), 1)
                cv2.rectangle(lc, (int(px - wcol), 0), (int(px + wcol), h2), col, -1)
        if not act:
            return
        bl = max(float(g("bloom")), 0.0)
        lc = cv2.GaussianBlur(lc, (0, 0), max(hw * 0.5 if act else 2, 1.5) * (0.6 + 0.2 * bl))
        planes = [cv2.resize(np.ascontiguousarray(lc[..., c]), (W, H), interpolation=cv2.INTER_LINEAR) for c in range(3)]
        comp.cv.blend(planes, None, self.L.blend, op * (1 + 0.5 * bl))

    def _glow(self, act, dur, inten, op):
        comp, g = self.comp, self.L.get
        W, H = comp.W, comp.H
        if self.box is not None:      # clear what was drawn last frame
            x0, y0, x1, y1 = self.box
            self.hl[y0:y1, x0:x1] = 0
            self.m[y0:y1, x0:x1] = 0
            self.box = None
        if not act:
            return
        size = float(g("size"))
        lo, hi = [W, H], [0, 0]
        for i, f in act:
            kk = (1 - (comp.t - f.t) / dur) ** 1.6 * inten
            x0, y0, x1, y1 = f.rect
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            x0, x1, y0, y1 = cx + (x0 - cx) * size, cx + (x1 - cx) * size, cy + (y0 - cy) * size, cy + (y1 - cy) * size
            px = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32) @ comp.M[:, :2].T + comp.M[:, 2]
            mn, mx = px.min(axis=0), px.max(axis=0)
            if mx[0] < 0 or mn[0] > W or mx[1] < 0 or mn[1] > H:
                continue
            pts = np.round(px / 2).astype(np.int32)
            cv2.fillConvexPoly(self.hl, pts, tuple(float(c * kk) for c in self.cols[i]))
            cv2.fillConvexPoly(self.m, pts, float(kk))
            lo = [min(lo[0], mn[0]), min(lo[1], mn[1])]
            hi = [max(hi[0], mx[0]), max(hi[1], mx[1])]
        if hi[0] <= lo[0]:
            return
        x0, y0 = max(int(lo[0] / 2) - 2, 0), max(int(lo[1] / 2) - 2, 0)
        x1, y1 = min(int(hi[0] / 2) + 3, W // 2), min(int(hi[1] / 2) + 3, H // 2)
        self.box = (x0, y0, x1, y1)
        a = comp.last_ink if comp.last_ink is not None else comp.ink(0.0)
        if g("tint_ink"):
            rw, rh = (x1 - x0) * 2, (y1 - y0) * 2
            hl = cv2.resize(self.hl[y0:y1, x0:x1], (rw, rh), interpolation=cv2.INTER_LINEAR)
            m = cv2.resize(self.m[y0:y1, x0:x1], (rw, rh), interpolation=cv2.INTER_LINEAR)
            ar = a[y0 * 2:y0 * 2 + rh, x0 * 2:x0 * 2 + rw]
            for c in range(3):
                comp.cv.P[c][y0 * 2:y0 * 2 + rh, x0 * 2:x0 * 2 + rw] += op * ar * (hl[..., c] - m * float(comp.ink_color[c]))
        bloom = float(g("bloom"))
        if bloom > 0:
            q = (W // 4, H // 4)
            a4 = cv2.resize(a, q, interpolation=cv2.INTER_AREA)
            hq = cv2.resize(self.hl, q, interpolation=cv2.INTER_AREA)
            gl = cv2.GaussianBlur(hq * a4[..., None], (0, 0), 3.5 * comp.S)
            if gl.max() > 0:
                planes = [cv2.resize(np.ascontiguousarray(gl[..., c]), (W, H), interpolation=cv2.INTER_LINEAR) for c in range(3)]
                comp.cv.blend(planes, None, "add", bloom * op)


class SpotlightDrawer(Drawer):
    """Does not draw: the notation and highlight layers ask `Compositor.spot_x` for its window."""

    def draw(self, k, op):
        pass


# ====================================================================================== finishing
class BloomDrawer(Drawer):
    def draw(self, k, op):
        comp, g = self.comp, self.L.get
        cv = comp.cv
        q = (comp.W // 4, comp.H // 4)
        small = [cv2.resize(p, q, interpolation=cv2.INTER_AREA) for p in cv.P]
        lum = (small[0] + small[1] + small[2]) / 3.0
        thr = float(g("threshold")) * 255
        w = np.clip((lum - thr) / max(255 - thr, 1.0), 0, 1)
        bl = [cv2.GaussianBlur(s * w, (0, 0), max(float(g("radius")) * comp.S / 4, 0.5)) for s in small]
        planes = [cv2.resize(b, (comp.W, comp.H), interpolation=cv2.INTER_LINEAR) for b in bl]
        cv.blend(planes, None, "add", self.rt.v("strength", k) * op)


class VignetteDrawer(Drawer):
    def __init__(self, rt, comp):
        super().__init__(rt, comp)
        g = self.L.get
        W, H = comp.W, comp.H
        yy, xx = np.mgrid[0:H // 2, 0:W // 2].astype(np.float32)
        rr = np.sqrt(((xx - W / 4) / (W / 4)) ** 2 + ((yy - H / 4) / (H / 4)) ** 2)
        m = np.clip((rr - float(g("size"))) / max(float(g("softness")), 0.05), 0, 1) ** 1.5
        self.mask = cv2.resize(m.astype(np.float32), (W, H), interpolation=cv2.INTER_LINEAR)

    def draw(self, k, op):
        s = self.rt.v("strength", k)
        if s > 0.001:
            self.comp.cv.blend(rgb(self.L.get("color")), self.mask * s, "normal", op)


class GradeDrawer(Drawer):
    def draw(self, k, op):
        g = self.rt.v
        P = self.comp.cv.P
        br, ct, st = g("brightness", k), g("contrast", k), g("saturation", k)
        hue = math.radians(g("hue", k))
        ta = g("tint_amount", k)
        if abs(br) < 1e-4 and abs(ct - 1) < 1e-4 and abs(st - 1) < 1e-4 and abs(hue) < 1e-4 and ta < 1e-4:
            return
        lum = np.array([0.213, 0.715, 0.072], np.float32)
        sat = np.array([[0.213 + 0.787 * st, 0.715 - 0.715 * st, 0.072 - 0.072 * st],
                        [0.213 - 0.213 * st, 0.715 + 0.285 * st, 0.072 - 0.072 * st],
                        [0.213 - 0.213 * st, 0.715 - 0.715 * st, 0.072 + 0.928 * st]], np.float32)
        c, s = math.cos(hue), math.sin(hue)
        hm = np.array([[lum[0] + c * 0.787 - s * 0.213, lum[1] - c * 0.715 - s * 0.715, lum[2] - c * 0.072 + s * 0.928],
                       [lum[0] - c * 0.213 + s * 0.143, lum[1] + c * 0.285 + s * 0.140, lum[2] - c * 0.072 - s * 0.283],
                       [lum[0] - c * 0.213 - s * 0.787, lum[1] - c * 0.715 + s * 0.715, lum[2] + c * 0.928 + s * 0.072]], np.float32)
        tint = lerp(np.ones(3, np.float32), rgb(self.L.get("tint")) / 255.0, ta)
        M = (np.diag(tint) @ hm @ sat * ct).astype(np.float32)
        bias = (128 * (1 - ct) * tint + br * 255).astype(np.float32)
        new = []
        for i in range(3):
            acc = np.multiply(P[0], float(M[i, 0]))
            cv2.scaleAdd(P[1], float(M[i, 1]), acc, dst=acc)
            cv2.scaleAdd(P[2], float(M[i, 2]), acc, dst=acc)
            acc += float(bias[i])
            new.append(acc)
        for i in range(3):
            P[i][:] = P[i] * (1 - op) + new[i] * op if op < 0.999 else new[i]


class BlurDrawer(Drawer):
    def draw(self, k, op):
        r = self.rt.v("radius", k) * self.comp.S
        if r > 0.3:
            for p in self.comp.cv.P:
                b = cv2.GaussianBlur(p, (0, 0), r)
                p[:] = p * (1 - op) + b * op if op < 0.999 else b


class AberrationDrawer(Drawer):
    def draw(self, k, op):
        a = self.rt.v("amount", k) * self.comp.S * op
        if a > 0.2:
            P, (W, H) = self.comp.cv.P, (self.comp.W, self.comp.H)
            for i, d in ((0, a), (2, -a)):
                M = np.float32([[1, 0, d], [0, 1, 0]])
                P[i][:] = cv2.warpAffine(P[i], M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


class GrainDrawer(Drawer):
    def __init__(self, rt, comp):
        super().__init__(rt, comp)
        s = max(int(self.L.get("size")), 1)
        rng = np.random.default_rng(7)
        h, w = comp.H // s + 1, comp.W // s + 1
        self.pool = [cv2.resize(rng.standard_normal((h, w)).astype(np.float32), (comp.W, comp.H), interpolation=cv2.INTER_LINEAR)
                     for _ in range(4)]

    def draw(self, k, op):
        a = self.rt.v("amount", k) * op
        if a > 0.05:
            n = self.pool[k % len(self.pool)]
            for p in self.comp.cv.P:
                cv2.scaleAdd(n, a, p, dst=p)


# ====================================================================================== text
class TextDrawer(Drawer):
    def __init__(self, rt, comp):
        super().__init__(rt, comp)
        self.mask = self._render()

    def _render(self):
        g = self.L.get
        text = str(g("text"))
        if not text.strip():
            return None
        from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter
        W, H = self.comp.W, self.comp.H
        if g("uppercase"):
            text = text.upper()
        f = QFont(str(g("font")))
        px = max(int(H * float(g("size"))), 6)
        f.setPixelSize(px)
        f.setItalic(bool(g("italic")))
        f.setBold(bool(g("bold")))
        if float(g("spacing")) > 0:
            f.setLetterSpacing(QFont.AbsoluteSpacing, px * float(g("spacing")))
        fm = QFontMetricsF(f)
        lines = text.split("\n")
        widths = [fm.horizontalAdvance(l_) for l_ in lines]
        wmax = int(max(widths)) + px * 2
        lh = int(fm.height() * 1.15)
        hh = lh * len(lines) + px
        img = QImage(wmax, hh, QImage.Format_Grayscale8)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255))
        for i, l_ in enumerate(lines):
            p.drawText(int((wmax - widths[i]) / 2) if g("align") == "center" else (px if g("align") == "left" else int(wmax - widths[i] - px)),
                       int(px * 0.6 + fm.ascent() + i * lh), l_)
        p.end()
        arr = np.frombuffer(img.constBits(), np.uint8).reshape(hh, img.bytesPerLine())[:, :wmax].astype(np.float32) / 255.0
        x, y = float(g("x")) * W, float(g("y")) * H - px * 0.6 - fm.ascent()
        align = g("align")
        x0 = int(x - wmax / 2) if align == "center" else (int(x - px) if align == "left" else int(x - wmax + px))
        y0 = int(y)
        glow = float(g("glow"))
        if glow > 0:
            pad = int(px)
            big = np.zeros((hh + 2 * pad, wmax + 2 * pad), np.float32)
            big[pad:pad + hh, pad:pad + wmax] = arr
            arr = np.clip(arr.copy(), 0, 1)
            gl = cv2.GaussianBlur(big, (0, 0), px * 0.25)
            return dict(arr=arr, glow=gl, gx=x0 - pad, gy=y0 - pad, x=x0, y=y0)
        return dict(arr=arr, glow=None, x=x0, y=y0)

    def _crop(self, arr, x, y):
        W, H = self.comp.W, self.comp.H
        h, w = arr.shape
        sx, sy = max(-x, 0), max(-y, 0)
        ex, ey = min(w, W - x), min(h, H - y)
        if ex <= sx or ey <= sy:
            return None
        return arr[sy:ey, sx:ex], x + sx, y + sy

    def draw(self, k, op):
        if self.mask is None:
            return
        comp, m = self.comp, self.mask
        col = rgb(self.L.get("color"))
        for key, strength in (("glow", float(self.L.get("glow"))), ("arr", 1.0)):
            arr = m.get(key)
            if arr is None:
                continue
            x, y = (m["gx"], m["gy"]) if key == "glow" else (m["x"], m["y"])
            cr = self._crop(arr, x, y)
            if cr is None:
                continue
            sub, x0, y0 = cr
            h, w = sub.shape
            a = np.clip(sub * (strength if key == "glow" else 1.0), 0, 1) * op
            for c in range(3):
                P = comp.cv.P[c][y0:y0 + h, x0:x0 + w]
                if self.L.blend == "add" or key == "glow":
                    P += a * float(col[c])
                else:
                    P *= 1 - a
                    P += a * float(col[c])


DRAWERS = {"solid": SolidDrawer, "gradient": GradientDrawer, "noise": NoiseDrawer, "media": MediaDrawer,
           "particles": ParticleDrawer, "score": ScoreDrawer, "highlight": HighlightDrawer,
           "spotlight": SpotlightDrawer, "bloom": BloomDrawer, "vignette": VignetteDrawer, "grade": GradeDrawer,
           "blur": BlurDrawer, "aberration": AberrationDrawer, "grain": GrainDrawer, "text": TextDrawer}
CAMERA_TYPES = ("shake", "zoom", "sway")


# ====================================================================================== the compositor
class Compositor:
    def __init__(self, project, score, W: int, H: int, fps: int, duration: float, loudness=None):
        self.project, self.score = project, score
        self.fx = project.effects
        self.W, self.H, self.fps = W, H, fps
        self.S = H / 1080.0
        self.sig = Signals(project, score, fps, duration, loudness)
        self.n = self.sig.n
        self.small = (max(W // 4, 2), max(H // 4, 2))
        self.cv = Canvas(W, H)
        self.flashes = build_flashes(project, score)
        self.xcol = np.arange(W, dtype=np.float32)
        self.rts, self.spots = [], []
        camera_layers = []
        for lay in self.fx.layers:
            if not lay.enabled:
                continue
            if lay.type in CAMERA_TYPES:
                camera_layers.append(lay)
                continue
            rt = LayerRT(lay, self)
            self.rts.append(rt)
            if lay.type == "spotlight":
                self.spots.append(rt)
        self.camera = CameraMods(camera_layers, self.sig, lambda lay, name: self._camera_param(lay, name))
        self.k = 0
        self.t = 0.0
        self.M = np.eye(2, 3, dtype=np.float32)
        self.alpha8 = None
        self.spot_px = {}
        self.ink_color = np.full(3, 255, np.float32)
        self.last_ink = None
        self._ink_cache = {}
        for rt in self.rts:                     # drawers need the compositor's state, so they come last
            cls = DRAWERS.get(rt.layer.type)
            rt.drawer = cls(rt, self) if cls else None

    def _camera_param(self, lay, name):
        p = lay.spec(name)
        bs = lay.bindings.get(name)
        return self.sig.evaluate(lay.get(name), bs, p.lo, p.hi) if bs else np.full(self.n, float(lay.get(name)), np.float32)

    def close(self):
        for rt in self.rts:
            if rt.drawer:
                rt.drawer.close()

    def camera_pose(self, k: int, pose: tuple) -> tuple:
        return self.camera.apply(k, pose, self.W, self.H)

    # ---- helpers for the drawers --------------------------------------------------------------
    def spot_x(self) -> np.ndarray:
        """How visible the ink is at every column (1 everywhere unless a spotlight is active)."""
        mult = np.ones(self.W, np.float32)
        for rt in self.spots:
            g = rt.layer.get
            if not rt.layer.enabled:
                continue
            t = self.t
            f = float(smooth((t - (float(g("at")) - 0.12)) / max(float(g("ramp")), 0.01)))
            if float(g("until")) > 0:
                f *= 1 - float(smooth((t - float(g("until"))) / 0.3))
            win = self.spot_px.get(rt.layer.id)
            if f > 0 and win is not None:
                lo, hi = win
                w = ramp(self.xcol, lo - 30, 60) * ramp(hi + 30 - self.xcol, 0, 60)
                vis = float(g("outside")) + (1 - float(g("outside"))) * w
                mult = mult * ((1 - f) + f * vis)
        return mult

    def ink(self, edge_fade: float) -> np.ndarray:
        """The notation's coverage (float 0..1) with the side fade and spotlights applied."""
        key = round(edge_fade, 4)
        if key not in self._ink_cache:
            edge = np.ones(self.W, np.float32)
            if edge_fade > 0:
                e = edge_fade * self.W
                edge = ramp(self.xcol, 0, e) * ramp(self.W - self.xcol, 0, e)
            self._ink_cache[key] = self.alpha8 * (edge * self.spot_x() / 255.0).astype(np.float32)[None, :]
        return self._ink_cache[key]

    # ---- the frame ------------------------------------------------------------------------------------
    def frame(self, k: int, alpha8: np.ndarray, pose: tuple, spot_px: dict | None = None) -> np.ndarray:
        """alpha8: uint8 HxW coverage of the notation drawn for `pose`; returns uint8 HxWx3 (RGB)."""
        self.k = k = min(max(k, 0), self.n - 1)
        self.t = k / self.fps
        self.alpha8, self.M, self.spot_px = alpha8, view_matrix(pose, self.W, self.H), spot_px or {}
        self._ink_cache.clear()
        self.last_ink = None
        self.cv.clear()
        for rt in self.rts:
            if rt.drawer is None:
                continue
            op = rt.opacity(k)
            if op > 0.002:
                rt.drawer.draw(k, op)
        return self.cv.finish()
