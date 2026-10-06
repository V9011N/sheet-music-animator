"""Project state: settings, camera keyframes, per-note timing overrides."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .engraver import NOTE_KINDS, REST_KINDS, Score

EASES = ("smooth", "linear", "hold")


@dataclass
class CameraKey:
    t: float
    cx: float
    cy: float
    w: float            # width in page units; height follows from the output aspect ratio
    ease: str = "smooth"  # how the camera travels from this key to the next


@dataclass
class Settings:
    layout: str = "pages"        # "pages" (systems stacked) or "horizontal" (one long line)
    reveal: str = "fade"         # "fade" (notes fade/wipe in) or "instant" (notes pop in at their time)
    fade: float = 0.25           # seconds a note takes to fade in
    ghost: float = 0.0           # opacity of notes that have not played yet (0 = hidden)
    lookahead: int = 4           # only this many measures after the playhead show ghost notes (0 = all)
    offset: float = 0.0          # shift every reveal by this many seconds (audio latency, etc.)
    tail: float = 2.0            # seconds of video after the last note
    width: int = 1920
    height: int = 1080
    fps: int = 30
    ink: str = "#1a1a1a"
    paper: str = "#ffffff"
    audio: str = "synth"         # "synth", "none", or a path to an audio file
    crf: int = 16
    follow_width: float = 14000.0  # camera width (page units) used by the automatic camera path

    @property
    def aspect(self) -> float:
        return self.width / self.height


@dataclass
class Project:
    xml_path: str = ""
    settings: Settings = field(default_factory=Settings)
    keys: list[CameraKey] = field(default_factory=list)
    overrides: dict[int, float] = field(default_factory=dict)   # unit uid -> extra seconds
    keys_edited: bool = False
    timed: set[int] = field(default_factory=set)   # clefs, barlines... (static units) that follow the music

    # ---- camera ---------------------------------------------------------------------
    def camera_at(self, t: float):
        """(cx, cy, w) of the camera at time t."""
        ks = self.keys
        if not ks:
            return None
        if t <= ks[0].t:
            return ks[0].cx, ks[0].cy, ks[0].w
        if t >= ks[-1].t:
            return ks[-1].cx, ks[-1].cy, ks[-1].w
        lo, hi = 0, len(ks) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (mid, hi) if ks[mid].t <= t else (lo, mid)
        a, b = ks[lo], ks[hi]
        if a.ease == "hold":
            return a.cx, a.cy, a.w
        u = (t - a.t) / max(b.t - a.t, 1e-9)
        if a.ease == "smooth":
            u = u * u * (3 - 2 * u)
        return (a.cx + (b.cx - a.cx) * u, a.cy + (b.cy - a.cy) * u,
                a.w * (b.w / a.w) ** u if a.w > 0 and b.w > 0 else a.w)

    def camera_rect(self, t: float):
        """(x, y, w, h) of the camera window at time t in page units."""
        c = self.camera_at(t)
        if c is None:
            return None
        cx, cy, w = c
        h = w / self.settings.aspect
        return cx - w / 2, cy - h / 2, w, h

    def set_key(self, t: float, cx: float, cy: float, w: float, snap: float = 0.02):
        """Create (or update the key within `snap` seconds of t)."""
        self.keys_edited = True
        for k in self.keys:
            if abs(k.t - t) <= snap:
                k.cx, k.cy, k.w = cx, cy, w
                return k
        k = CameraKey(t, cx, cy, w)
        self.keys.append(k)
        self.keys.sort(key=lambda k: k.t)
        return k

    # ---- note reveal -------------------------------------------------------------------
    def start_of(self, unit) -> float:
        return unit.time + self.settings.offset + self.overrides.get(unit.uid, 0.0)

    def _ease(self, dt: float) -> float:
        """0..1 progress of a note's appearance `dt` seconds after its start."""
        s = self.settings
        if s.reveal == "instant" or s.fade <= 0:
            return 1.0
        a = min(dt / s.fade, 1.0)
        return a * a * (3 - 2 * a)

    def _step_wipe(self, unit, t: float) -> float:
        """Beams/tuplets grow up to the latest member note that has been revealed."""
        wipe, prev = 0.0, 0.0
        for uid, mtime, frac in unit.steps:
            dm = t - (mtime + self.settings.offset + self.overrides.get(uid, 0.0))
            if dm >= 0:
                wipe = max(wipe, prev + (frac - prev) * self._ease(dm))
            prev = frac
        return wipe

    def always_visible(self, unit) -> bool:
        """Clefs, barlines, key signatures... are on show from the start until they are given a timing."""
        return unit.static and unit.uid not in self.timed

    def reveal(self, unit, t: float):
        """(opacity, wipe fraction) of a unit at time t."""
        if self.always_visible(unit):
            return 1.0, 1.0
        s = self.settings
        dt = t - self.start_of(unit)
        if dt < 0:
            return s.ghost, 0.0
        a = self._ease(dt)
        wipe = 1.0
        if unit.steps:
            wipe = self._step_wipe(unit, t)
        elif unit.wipe and s.reveal != "instant":   # slurs, ties... appear whole when instant
            wipe = min(dt / max(unit.end - unit.time, 0.05), 1.0)
        return s.ghost + (1 - s.ghost) * a, wipe

    # ---- persistence -----------------------------------------------------------------------
    def save(self, path):
        data = {"version": 1, "xml_path": self.xml_path, "settings": asdict(self.settings),
                "keys": [asdict(k) for k in self.keys],
                "overrides": {str(k): v for k, v in self.overrides.items()},
                "keys_edited": self.keys_edited, "timed": sorted(self.timed)}
        Path(path).write_text(json.dumps(data, indent=1), encoding="utf8")

    @classmethod
    def load(cls, path) -> "Project":
        d = json.loads(Path(path).read_text(encoding="utf8"))
        known = Settings.__dataclass_fields__
        p = cls(xml_path=d.get("xml_path", ""),
                settings=Settings(**{k: v for k, v in d.get("settings", {}).items() if k in known}),
                keys=[CameraKey(**k) for k in d.get("keys", [])],
                overrides={int(k): v for k, v in d.get("overrides", {}).items()},
                keys_edited=d.get("keys_edited", False),
                timed={int(u) for u in d.get("timed", [])})
        return p


# --------------------------------------------------------------------------------------------
def auto_camera(score: Score, settings: Settings) -> list[CameraKey]:
    """A sensible starting camera path that follows the music.

    The frame is `follow_width` wide and tracks "now" along each system (keeping the
    playing position ~25% from the left); between systems it glides to the next one.
    If the frame is as wide as a system it simply frames the whole system instead.
    """
    keys: list[CameraKey] = []
    if not score.systems:
        return keys
    # Never leave a system before its last note has been on screen for a moment (`hold`, or most of
    # the gap to the next system when that is shorter), even if that means the next system's first
    # notes arrive a little late.  Otherwise glide for up to `glide` s, arriving `lead` s ahead of
    # the next system; the glide shrinks to `min_glide` (practically a cut) when time is short.
    lead, glide, hold, min_glide = 0.1, 0.6, 0.4, 0.02
    paged = settings.layout == "pages"
    n = len(score.systems)
    last_on: dict[int, float] = {}
    for u in score.units:
        if u.kind in NOTE_KINDS | REST_KINDS:
            last_on[u.system] = max(last_on.get(u.system, 0.0), u.time)
    arrives, leaves = [0.0] * n, [0.0] * n
    for i, s in enumerate(score.systems):
        last = max(last_on.get(i, s.start), s.start)
        if i + 1 < n:
            nxt = score.systems[i + 1].start
            shown_until = last + min(hold, 0.8 * max(nxt - last, 0.0))
            arrives[i + 1] = max(nxt - lead, shown_until + min_glide)
            leaves[i] = max(shown_until, arrives[i + 1] - glide)
        else:
            leaves[i] = max(last + hold, arrives[i] + 0.02)
    for i, s in enumerate(score.systems):
        x, y, w, h = s.rect
        cy = y + h / 2
        fw = settings.follow_width
        nxt = score.systems[i + 1].start if i + 1 < n else score.duration
        arrive, leave = arrives[i], max(leaves[i], arrives[i] + 0.02)
        if fw >= w * 0.98:   # whole system fits
            fw = max(w * 1.03, h * settings.aspect * 1.05)
            keys.append(CameraKey(arrive, x + w / 2, cy, fw))
            keys.append(CameraKey(leave, x + w / 2, cy, fw))
            continue
        lo, hi = x + fw / 2 - 150, x + w - fw / 2 + 150

        def at(t, i=i, fw=fw, lo=lo, hi=hi):
            return min(max(score.now_x(i, t) + 0.25 * fw, lo), hi)

        keys.append(CameraKey(arrive, at(s.start), cy, fw, "linear"))
        for t, _ in score.measures:
            if arrive + 0.05 < t < (leave - 0.05 if paged else nxt):
                keys.append(CameraKey(t, at(t), cy, fw, "linear"))
        if paged:   # follow until the glide starts, then travel to the next system
            keys.append(CameraKey(leave, at(leave), cy, fw, "smooth"))
    if settings.layout == "horizontal":
        keys.append(CameraKey(score.duration, keys[-1].cx, keys[-1].cy, keys[-1].w))
    keys.sort(key=lambda k: k.t)
    return keys
