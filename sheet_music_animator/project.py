"""Project state: settings, camera keyframes, per-note timing overrides."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .engraver import NOTE_KINDS, REST_KINDS, Score
from .layers import Layer, legacy_layers, schema

EASES = ("smooth", "linear", "hold")
CAMERA_CHANNELS = ("pos", "size", "rot")
LANE = "lane:"                      # automation lanes live in `channels` under "lane:<name>"
CHANNEL_LABELS = {"pos": "Position", "size": "Frame size", "rot": "Rotation"}


def is_lane(channel: str) -> bool:
    return channel.startswith(LANE)


def channel_label(channel: str) -> str:
    return CHANNEL_LABELS.get(channel) or channel[len(LANE):]


# Categories of engravings that can be switched off per measure: name -> SVG classes that make them up.
# A class that is a whole element (a fingering, a dynamic) hides that element; a class inside a
# note (an articulation, an accidental) is cut out of it.
CATEGORIES = {
    "Fingerings": ("fing",),
    "Tuplet numbers and brackets": ("tupletNum", "tupletBracket", "tuplet"),
    "Articulations": ("artic",),
    "Accidentals": ("accid",),
    "Dots": ("dots",),
    "Dynamics": ("dynam",),
    "Hairpins": ("hairpin",),
    "Directions and text": ("dir", "tempo"),
    "Slurs": ("slur",),
    "Ties": ("tie", "lv"),
    "Octave lines (8va)": ("octave",),
    "Pedal marks": ("pedal",),
    "Arpeggios": ("arpeg",),
    "Ornaments and fermatas": ("trill", "mordent", "turn", "ornam", "fermata"),
    "Measure numbers": ("mNum",),
}
# Engravings that cannot be moved or resized: noteheads and note tails (the note itself) and beams.
FIXED_KINDS = {"note", "chord", "beam", "beamSpan", "fTrem", "bTrem"}


@dataclass(eq=False)
class Key:
    """A keyframe on one camera channel."""
    t: float
    v: list                 # pos: [cx, cy]   size: [width in page units]   rot: [degrees, clockwise]
    ease: str = "smooth"    # how the value travels from this key to the next


@dataclass
class Effects:
    """The produced look of the video: a stack of effect layers (see layers.py) drawn bottom to top, the
    automation lanes they can follow, and how events are made from the music.  Unused unless `enabled`."""
    enabled: bool = False
    layers: list = field(default_factory=list)        # Layer objects, bottom to top
    lanes: dict = field(default_factory=dict)         # lane name -> its value where it has no keyframes
    use_dynamics: bool = True     # f, ff, fff, sfz ... markings make events
    use_accents: bool = True      # accents and marcatos make events
    impulses: list = field(default_factory=list)      # [{"t": seconds, "s": 0..1}]: events placed by hand
    hits: list = field(default_factory=list)          # [{"m0": measure, "m1": measure, "s": 0..1}]: every note is an event
    loud_floor_db: float = -40.0  # loudness 0 at this level...
    loud_range_db: float = 26.0   # ...and 1 this many dB above
    density_max: float = 14.0     # notes per second that count as "full density"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["layers"] = [lay.to_dict() for lay in self.layers]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Effects":
        d = dict(d)
        known = cls.__dataclass_fields__
        if "layers" not in d and ("bg_calm" in d or "flash_colors" in d):   # the first, fixed-feature version
            d["layers"] = legacy_layers(d)
            d["lanes"] = {"Mood": 1.0, "Hush": 0.0, "Snow lift": 0.0}
        layers = [Layer.from_dict(x) if isinstance(x, dict) else x for x in d.get("layers", [])]
        return cls(**{**{k: v for k, v in d.items() if k in known}, "layers": layers})

    def layer(self, layer_id: str):
        return next((x for x in self.layers if x.id == layer_id), None)


@dataclass
class Settings:
    layout: str = "pages"        # "pages" (lines stacked) or "horizontal" (one long line); see measures_per_line
    measures_per_line: int = 4   # measures in each line (0 = the whole score on one line)
    font: str = "Times New Roman"   # font of all text in the score
    reveal: str = "instant"      # "fade" (notes fade/wipe in) or "instant" (notes pop in at their time)
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
    preset: str = "medium"       # x264 speed/size trade-off (ultrafast ... veryslow); faster = bigger files
    follow_width: float = 14000.0  # camera width (page units) used by the automatic camera path
    follow_lead: float = 0.25    # where "now" sits in the frame (fraction from the left) when following the music
    align_audio: str = ""        # recording the score timing was fitted to ("" = the score's own timing)

    @property
    def aspect(self) -> float:
        return self.width / self.height


def norm_transform(v) -> list:
    """[dx, dy, sx, sy, rotation]; older projects stored [dx, dy, scale]."""
    v = [float(x) for x in v]
    if len(v) == 3:
        v = [v[0], v[1], v[2], v[2], 0.0]
    return (v + [1.0, 1.0, 0.0][len(v) - 2:])[:5] if len(v) < 5 else v[:5]


def _blank_channels():
    return {c: [] for c in CAMERA_CHANNELS}


def _interp(keys, t, kind):
    """Value list of a channel at time t (None for an empty channel)."""
    if not keys:
        return None
    if t <= keys[0].t:
        return list(keys[0].v)
    if t >= keys[-1].t:
        return list(keys[-1].v)
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if keys[mid].t <= t else (lo, mid)
    a, b = keys[lo], keys[hi]
    if a.ease == "hold":
        return list(a.v)
    u = (t - a.t) / max(b.t - a.t, 1e-9)
    if a.ease == "smooth":
        u = u * u * (3 - 2 * u)
    if kind == "size" and a.v[0] > 0 and b.v[0] > 0:     # zoom is geometric
        return [a.v[0] * (b.v[0] / a.v[0]) ** u]
    return [x + (y - x) * u for x, y in zip(a.v, b.v)]


@dataclass
class Project:
    xml_path: str = ""
    settings: Settings = field(default_factory=Settings)
    channels: dict[str, list[Key]] = field(default_factory=_blank_channels)
    overrides: dict[int, float] = field(default_factory=dict)   # unit uid -> extra seconds
    keys_edited: bool = False
    timed: set[int] = field(default_factory=set)   # clefs, barlines... (static units) that follow the music
    hidden: dict[int, set[str]] = field(default_factory=dict)   # measure index -> hidden CATEGORIES
    transforms: dict[int, list[float]] = field(default_factory=dict)   # unit uid -> [dx, dy, scale x, scale y, rotation (degrees)]
    deleted: set[int] = field(default_factory=set)                      # unit uids of engravings the user deleted
    line_starts: list[int] | None = None   # measure index that starts each line (None: every measures_per_line)
    effects: Effects = field(default_factory=Effects)
    sync_conf: list = field(default_factory=list)   # [[recording seconds, confidence 0..1], ...] of that alignment
    sync_overall: float = 0.0                       # the headline confidence (0..1) of that alignment
    time_map: list = field(default_factory=list)   # [[score seconds, recording seconds], ...] from aligning to audio

    # ---- camera ---------------------------------------------------------------------
    def has_keys(self) -> bool:
        return bool(self.channels["pos"] and self.channels["size"])

    def camera_at(self, t: float):
        """(cx, cy, w, rotation) of the camera at time t, or None while it has no keyframes."""
        pos, size = _interp(self.channels["pos"], t, "pos"), _interp(self.channels["size"], t, "size")
        if pos is None or size is None:
            return None
        rot = _interp(self.channels["rot"], t, "rot")
        return pos[0], pos[1], size[0], rot[0] if rot else 0.0

    def camera_pose(self, t: float):
        """(cx, cy, w, h, rotation in degrees) of the camera window at time t in page units."""
        c = self.camera_at(t)
        if c is None:
            return None
        return c[0], c[1], c[2], c[2] / self.settings.aspect, c[3]

    def camera_rect(self, t: float):
        """(x, y, w, h) of the camera window at time t, ignoring its rotation."""
        c = self.camera_pose(t)
        if c is None:
            return None
        cx, cy, w, h, _ = c
        return cx - w / 2, cy - h / 2, w, h

    def set_camera(self, t: float, cx: float, cy: float, w: float, rot: float = 0.0,
                   snap: float = 0.02, create: bool = True) -> list:
        """Store the camera pose at time t.  Only the channels whose value differs from what they
        evaluate to now get a keyframe (an existing key within `snap` seconds is updated), so moving the
        camera does not touch the size or rotation channels.  With create=False only existing keys are
        updated.  Returns the keys that were set."""
        cur = self.camera_at(t)
        wanted = {"pos": [cx, cy], "size": [w], "rot": [rot]}
        now = {"pos": None, "size": None, "rot": [0.0]}
        if cur is not None:
            now = {"pos": [cur[0], cur[1]], "size": [cur[2]], "rot": [cur[3]]}
        out = []
        for ch in CAMERA_CHANNELS:
            keys = self.channels[ch]
            if now[ch] is not None and all(abs(a - b) < 1e-6 for a, b in zip(now[ch], wanted[ch])):
                continue
            existing = next((k for k in keys if abs(k.t - t) <= snap), None)
            if existing is not None:
                existing.v = list(wanted[ch])
                out.append(existing)
            elif create:
                k = Key(t, list(wanted[ch]))
                keys.append(k)
                keys.sort(key=lambda q: q.t)
                out.append(k)
        if out:
            self.keys_edited = True
        return out

    def add_key(self, channel: str, t: float, snap: float = 0.02):
        """A keyframe on `channel` at t holding the value there now (or the existing key)."""
        keys = self.channels[channel]
        existing = next((k for k in keys if abs(k.t - t) <= snap), None)
        if existing is not None:
            return existing
        if is_lane(channel):
            k = Key(t, [self.lane_at(channel[len(LANE):], t)])
        else:
            cur = self.camera_at(t)
            if cur is None:
                return None
            k = Key(t, {"pos": [cur[0], cur[1]], "size": [cur[2]], "rot": [cur[3]]}[channel])
        keys.append(k)
        keys.sort(key=lambda q: q.t)
        if channel in CAMERA_CHANNELS:
            self.keys_edited = True
        return k

    # ---- automation lanes ---------------------------------------------------------------
    def lane_at(self, name: str, t: float) -> float:
        """Value of a lane at time t."""
        v = _interp(self.channels.get(LANE + name, []), t, "scalar")
        return float(self.effects.lanes.get(name, 0.0)) if v is None else float(v[0])

    def sample_lane(self, name: str, times):
        """lane_at for many times at once (numpy arrays)."""
        import numpy as np
        t = np.asarray(times, dtype=np.float64)
        keys = self.channels.get(LANE + name, [])
        if not keys:
            return np.full(len(t), float(self.effects.lanes.get(name, 0.0)), np.float32)
        out = np.empty(len(t), np.float64)
        out[:] = keys[-1].v[0]
        out[t < keys[0].t] = keys[0].v[0]
        for a, b in zip(keys, keys[1:]):
            m = (t >= a.t) & (t < b.t)
            if not m.any():
                continue
            if a.ease == "hold":
                out[m] = a.v[0]
                continue
            u = (t[m] - a.t) / max(b.t - a.t, 1e-9)
            if a.ease == "smooth":
                u = u * u * (3 - 2 * u)
            out[m] = a.v[0] + (b.v[0] - a.v[0]) * u
        return out.astype(np.float32)

    def add_lane(self, name: str, default: float = 0.0) -> str:
        name = name.strip() or "Lane"
        base, i = name, 2
        while name in self.effects.lanes:
            name, i = f"{base} {i}", i + 1
        self.effects.lanes[name] = default
        self.channels.setdefault(LANE + name, [])
        return name

    def remove_lane(self, name: str) -> None:
        self.effects.lanes.pop(name, None)
        self.channels.pop(LANE + name, None)
        for lay in self.effects.layers:
            for setting, bs in list(lay.bindings.items()):
                lay.bindings[setting] = [b for b in bs if b.get("src") != LANE + name]

    def rename_lane(self, old: str, new: str) -> str:
        if old not in self.effects.lanes or not new.strip() or new == old:
            return old
        new = new.strip()
        if new in self.effects.lanes:
            return old
        self.effects.lanes = {(new if k == old else k): v for k, v in self.effects.lanes.items()}
        self.channels = {(LANE + new if k == LANE + old else k): v for k, v in self.channels.items()}
        for lay in self.effects.layers:
            for bs in lay.bindings.values():
                for b in bs:
                    if b.get("src") == LANE + old:
                        b["src"] = LANE + new
        return new

    def retime(self, fn) -> None:
        """Move everything that is placed in time (keyframes, events, layer timings) with `fn`, e.g. when the
        score is aligned to a recording and its notes move."""
        for ch, keys in self.channels.items():
            for k in keys:
                k.t = max(float(fn(k.t)), 0.0)
            keys.sort(key=lambda q: q.t)
        fx = self.effects
        for im in fx.impulses:
            im["t"] = float(fn(im["t"]))
        for lay in fx.layers:
            for prm in schema(lay.type):
                v = lay.params.get(prm.name)
                if prm.kind == "time" and isinstance(v, (int, float)) and v > 0:
                    lay.params[prm.name] = float(fn(v))

    def all_keys(self):
        return [(ch, k) for ch, keys in self.channels.items() for k in keys]

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
    def to_dict(self) -> dict:
        return {"version": 4, "xml_path": self.xml_path, "settings": asdict(self.settings),
                "channels": {ch: [{"t": k.t, "v": k.v, "ease": k.ease} for k in keys] for ch, keys in self.channels.items()},
                "overrides": {str(k): v for k, v in sorted(self.overrides.items())},
                "keys_edited": self.keys_edited, "timed": sorted(self.timed),
                "hidden": {str(m): sorted(c) for m, c in sorted(self.hidden.items()) if c},
                "transforms": {str(u): list(v) for u, v in sorted(self.transforms.items())},
                "deleted": sorted(self.deleted),
                "line_starts": self.line_starts, "effects": self.effects.to_dict(), "time_map": self.time_map,
                "sync_conf": self.sync_conf, "sync_overall": self.sync_overall}

    def snapshot(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    def load_dict(self, d: dict) -> None:
        """Replace this project's contents (in place, so everything holding a reference stays valid)."""
        known = Settings.__dataclass_fields__
        st = {k: v for k, v in d.get("settings", {}).items() if k in known}
        if d.get("version", 1) < 2:   # older projects: Verovio chose the breaks of the stacked layout
            st["measures_per_line"] = 0 if st.get("layout") == "horizontal" else -1
        self.xml_path = d.get("xml_path", "")
        self.settings = Settings(**st)
        self.channels = _blank_channels()
        legacy = {"mood": LANE + "Mood", "hush": LANE + "Hush", "lift": LANE + "Snow lift"}   # the first version's fixed lanes
        if "channels" in d:
            for ch, keys in d["channels"].items():
                self.channels[legacy.get(ch, ch)] = [Key(k["t"], list(k["v"]), k.get("ease", "smooth")) for k in keys]
        else:   # version 1: one list of keys that carry position and size together
            for k in d.get("keys", []):
                self.channels["pos"].append(Key(k["t"], [k["cx"], k["cy"]], k.get("ease", "smooth")))
                self.channels["size"].append(Key(k["t"], [k["w"]], k.get("ease", "smooth")))
        self.overrides = {int(k): v for k, v in d.get("overrides", {}).items()}
        self.keys_edited = d.get("keys_edited", False)
        self.timed = {int(u) for u in d.get("timed", [])}
        self.hidden = {int(m): set(c) for m, c in d.get("hidden", {}).items()}
        self.transforms = {int(u): norm_transform(v) for u, v in d.get("transforms", {}).items()}
        self.deleted = {int(u) for u in d.get("deleted", [])}
        self.line_starts = d.get("line_starts")
        self.effects = Effects.from_dict(d.get("effects", {}))
        for ch in self.channels:             # a lane always has its entry in the effects
            if is_lane(ch):
                self.effects.lanes.setdefault(ch[len(LANE):], 0.0)
        for name in self.effects.lanes:
            self.channels.setdefault(LANE + name, [])
        self.time_map = [list(p) for p in d.get("time_map", [])]
        self.sync_conf = [list(p) for p in d.get("sync_conf", [])] if self.time_map else []
        self.sync_overall = float(d.get("sync_overall", 0.0)) if self.sync_conf else 0.0

    def restore(self, snapshot: str) -> None:
        self.load_dict(json.loads(snapshot))

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_dict(), indent=1), encoding="utf8")

    @classmethod
    def load(cls, path) -> "Project":
        p = cls()
        p.load_dict(json.loads(Path(path).read_text(encoding="utf8")))
        return p


def time_map_function(points):
    """f(t) for a time map [[score seconds, recording seconds], ...] (identity for an empty map)."""
    import numpy as np
    if not points or len(points) < 2:
        return lambda t: t
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    lo, hi = ys[0] - xs[0], ys[-1] - xs[-1]
    return lambda t: t + lo if t <= xs[0] else t + hi if t >= xs[-1] else float(np.interp(t, xs, ys))


def retimer(old_points, new_points, grid):
    """A function taking a time in the output of the old time map to the matching time of the new one.
    `grid`: score times (seconds) at which both maps are compared."""
    import numpy as np
    f_old, f_new = time_map_function(old_points), time_map_function(new_points)
    g = sorted(set(float(x) for x in grid))
    old = np.array([f_old(x) for x in g])
    new = np.array([f_new(x) for x in g])
    o = np.maximum.accumulate(old)
    return lambda t: float(new[0] + (t - o[0]) if t <= o[0] else new[-1] + (t - o[-1]) if t >= o[-1] else np.interp(t, o, new))


# --------------------------------------------------------------------------------------------
@dataclass
class CameraKey:
    """Used while building the automatic path: position and size of one step."""
    t: float
    cx: float
    cy: float
    w: float
    ease: str = "smooth"


def _auto_keys(score: Score, settings: Settings) -> list:
    """A sensible starting camera path that follows the music.

    The frame is `follow_width` wide and tracks "now" along each system (keeping the
    playing position ~25% from the left); between systems it glides to the next one.
    If the frame is as wide as a system it simply frames the whole system instead.
    """
    keys: list = []
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
            return min(max(score.now_x(i, t) + (0.5 - settings.follow_lead) * fw, lo), hi)

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


def auto_camera(score: Score, settings: Settings) -> dict:
    """The automatic camera path as keyframe channels (position changes often, frame size rarely).
    Only the camera channels: `project.channels.update(auto_camera(...))` leaves the effect channels alone."""
    out = {c: [] for c in CAMERA_CHANNELS}
    last_w = None
    for k in _auto_keys(score, settings):
        out["pos"].append(Key(k.t, [k.cx, k.cy], k.ease))
        if last_w is None or abs(k.w - last_w) > 1.0:
            out["size"].append(Key(k.t, [k.w], "linear" if last_w is not None else k.ease))
            last_w = k.w
    return out
