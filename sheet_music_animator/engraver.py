"""MusicXML -> timed vector layers.

Verovio engraves the score to a single SVG.  This module splits that SVG into

* a *static* layer per system (staff lines, clefs, key/time signatures, barlines, ...)
* a list of *units*: the things that appear while the music plays (notes, chords,
  rests, beams, ties, slurs, dynamics, ledger lines, ...).  Each unit is a tiny
  self-contained SVG plus its bounding box and the time (seconds) at which it is
  first heard, taken from Verovio's timemap.

All coordinates are in Verovio's SVG space (one staff space is ~200 units), with the
page margin already applied ("page space").
"""
from __future__ import annotations

import copy
import json
import math
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path as FsPath

import verovio
from lxml import etree
from svgelements import Path as SvgPath

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
_G, _SVG, _USE = (f"{{{SVG_NS}}}{t}" for t in ("g", "svg", "use"))
_HREF = f"{{{XLINK_NS}}}href"
_NUM = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?", re.I)

NOTE_KINDS = {"note", "chord"}
REST_KINDS = {"rest", "mRest", "multiRest", "mRpt", "mRpt2", "beatRpt", "halfmRpt"}
BUNDLES = {"beam", "tuplet", "fTrem", "bTrem", "graceGrp", "ligature"}
STRUCTURE = {"mdiv", "score", "system", "measure", "staff", "layer", "section"}
STATIC = {"clef", "keySig", "meterSig", "barLine", "mNum", "space", "grpSym", "label", "labelAbbr",
          "sb", "pb", "pageMilestone", "systemMilestone", "scoreDef", "staffDef", "ending"}
# Units that grow from left to right over their span instead of just fading in.
WIPE_KINDS = {"beam", "tuplet", "slur", "tie", "hairpin", "pedal", "octave", "bracketSpan",
              "beamSpan", "gliss", "lv", "fTrem", "bTrem"}
DRAWABLE = {"path", "use", "polygon", "polyline", "rect", "ellipse", "text", "line"}
CSS = "ellipse,path,polygon,polyline,rect{stroke:currentColor}"
POSITION_TOLERANCE = 160.0  # how far left of a control event a note may sit and still "start" it

LAYOUTS = {
    "pages": {"breaks": "auto", "pageWidth": 2100, "pageHeight": 60000, "adjustPageHeight": True},
    "horizontal": {"breaks": "none", "pageWidth": 100000, "pageHeight": 2600,
                   "adjustPageWidth": True, "adjustPageHeight": True},
}


# --------------------------------------------------------------------------- data
@dataclass
class Unit:
    uid: int
    kind: str
    svg: bytes
    rect: tuple          # x, y, w, h (page space)
    time: float          # first heard
    end: float           # notes: stops sounding; spanning shapes: last note they cover
    system: int
    wipe: bool = False
    # Beams, tuplets, tremolos: (member uid, member time, fraction of rect width to show once that
    # member has appeared) so the shape grows note by note instead of all at once.
    steps: tuple = ()


@dataclass
class StaticLayer:
    svg: bytes
    rect: tuple


@dataclass
class System:
    rect: tuple          # x, y, w, h
    start: float
    end: float


@dataclass
class Score:
    width: float = 0
    height: float = 0
    title: str = ""
    layers: list[StaticLayer] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)
    systems: list[System] = field(default_factory=list)
    measures: list[tuple] = field(default_factory=list)   # (time, number)
    notes: list[tuple] = field(default_factory=list)      # (midi pitch, start, end, velocity)
    duration: float = 0.0
    _now_cache: dict = field(default_factory=dict, repr=False)

    def now_x(self, system: int, t: float) -> float:
        """Horizontal position of 'now' in a system, linear between note onsets."""
        cache = self._now_cache
        if system not in cache:
            cache[system] = sorted({(u.time, u.rect[0] + u.rect[2] / 2) for u in self.units
                                    if u.system == system and u.kind in NOTE_KINDS})
        pts = cache[system]
        if not pts:
            return self.systems[system].rect[0]
        ts = [p[0] for p in pts]
        i = bisect_right(ts, t)
        if i == 0:
            return pts[0][1]
        if i == len(pts):
            return pts[-1][1]
        (t0, x0), (t1, x1) = pts[i - 1], pts[i]
        return x0 if t1 == t0 else x0 + (x1 - x0) * (t - t0) / (t1 - t0)


# --------------------------------------------------------------------------- geometry
def _classes(el) -> set[str]:
    return set((el.get("class") or "").split())


def _tag(el) -> str:
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(a, b):
    """Affine matrices (a, b, c, d, e, f); returns a*b (b is applied first)."""
    return (a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
            a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
            a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5])


def _parse_transform(s):
    m = _IDENT
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in _NUM.findall(args)]
        if name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "matrix":
            t = tuple(v[:6])
        elif name == "rotate":
            a = math.radians(v[0])
            c, s = math.cos(a), math.sin(a)
            t = (c, s, -s, c, 0, 0)
            if len(v) == 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        else:
            continue
        m = _mul(m, t)
    return m


def _xf_box(box, m):
    x0, y0, x1, y1 = box
    pts = [(m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])
           for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _union(a, b):
    if a is None or b is None:
        return a or b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _pad(box, p):
    return (box[0] - p, box[1] - p, box[2] + p, box[3] + p)


class BoxCalculator:
    """Bounding boxes of Verovio SVG fragments (paths, glyph <use>s, polygons, text)."""

    def __init__(self, defs):
        self.defs = defs
        self._glyph = {}

    def glyph_box(self, gid):
        if gid not in self._glyph:
            el = self.defs.get(gid)
            self._glyph[gid] = None if el is None else self.box(el)
        return self._glyph[gid]

    def box(self, el):
        tag = _tag(el)
        m = _parse_transform(el.get("transform"))
        if tag in ("g", "svg", "defs"):
            b = None
            for ch in el:
                b = _union(b, self.box(ch))
            return None if b is None else _xf_box(b, m)
        b = None
        if tag == "path":
            try:
                bb = SvgPath(el.get("d") or "").bbox()
                b = None if bb is None else tuple(bb)
            except Exception:
                b = None
        elif tag in ("polygon", "polyline"):
            v = [float(x) for x in _NUM.findall(el.get("points") or "")]
            if len(v) >= 4:
                xs, ys = v[0::2], v[1::2]
                b = (min(xs), min(ys), max(xs), max(ys))
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            b = (x, y, x + float(el.get("width", 0)), y + float(el.get("height", 0)))
        elif tag == "ellipse":
            cx, cy, rx, ry = (float(el.get(k, 0)) for k in ("cx", "cy", "rx", "ry"))
            b = (cx - rx, cy - ry, cx + rx, cy + ry)
        elif tag == "use":
            b = self.glyph_box((el.get(_HREF) or el.get("href") or "").lstrip("#"))
        elif tag == "text":
            b = self._text_box(el)
        if b is None:
            return None
        sw = float(el.get("stroke-width") or 0) / 2 + 1
        return _xf_box((b[0] - sw, b[1] - sw, b[2] + sw, b[3] + sw), m)

    @staticmethod
    def _text_box(el):
        x, y = float(el.get("x", 0)), float(el.get("y", 0))
        sizes = [float(e.get("font-size", "0").rstrip("px")) for e in el.iter()]
        fs = max([s for s in sizes if s > 0] or [400.0])
        w = fs * 0.62 * max(1, len(re.sub(r"\s+", " ", "".join(el.itertext())).strip()))
        anchor = el.get("text-anchor", "start")
        x0 = x - w if anchor == "end" else x - w / 2 if anchor == "middle" else x
        return (x0, y - fs, x0 + w, y + fs * 0.35)


# --------------------------------------------------------------------------- engraving
def engrave(path, layout: str = "pages", ink: str = "#000000", progress=None) -> Score:
    say = progress or (lambda *_: None)
    say("Engraving with Verovio…")
    tk = verovio.toolkit()
    opts = {"scale": 40, "svgViewBox": True, "header": "none", "footer": "none",
            "pageMarginLeft": 40, "pageMarginRight": 40, "pageMarginTop": 60, "pageMarginBottom": 60}
    opts.update(LAYOUTS[layout])
    tk.setOptions(opts)
    if not tk.loadFile(str(path)):
        raise ValueError(f"Verovio could not read {path}")
    svg = tk.renderToSVG(1)
    timemap = tk.renderToTimemap({"includeRests": True, "includeMeasures": True})
    timemap = json.loads(timemap) if isinstance(timemap, str) else timemap

    on, off, measures = {}, {}, []
    for ev in timemap:
        t = ev["tstamp"] / 1000.0
        for k in ("on", "restsOn"):
            on.update({i: t for i in ev.get(k, [])})
        for k in ("off", "restsOff"):
            off.update({i: t for i in ev.get(k, [])})
        if "measureOn" in ev:
            measures.append(t)

    say("Splitting layers…")
    root = etree.fromstring(svg.encode("utf8"), etree.XMLParser(remove_blank_text=True))
    inner = next(e for e in root if e.tag == _SVG)
    vb = [float(x) for x in inner.get("viewBox").split()]
    defs = {e.get("id"): e for d in root.iter(f"{{{SVG_NS}}}defs") for e in d if e.get("id")}
    margin = next(e for e in inner.iter(_G) if "page-margin" in _classes(e))

    b = _Builder(defs, ink, margin.get("transform"))
    score = b.run(margin, on, off, tk)
    score.width, score.height = vb[2], vb[3]
    score.title = FsPath(path).stem
    score.measures = [(t, i + 1) for i, t in enumerate(measures)]
    score.duration = max([off.get(k, 0) for k in off] + [u.end for u in score.units] + [0])
    return score


class _Builder:
    def __init__(self, defs, ink, margin_transform):
        self.calc = BoxCalculator(defs)
        self.defs_xml = {k: etree.tostring(v, with_tail=False) for k, v in defs.items()}
        self.ink = ink
        self.margin_tf = margin_transform
        self.m = _parse_transform(margin_transform)
        self.recs: list[dict] = []

    def run(self, margin, on, off, tk) -> Score:
        self.on, self.off = on, off
        systems = [e for e in margin if _tag(e) == "g" and "system" in _classes(e)]
        for si, system in enumerate(systems):
            self._walk(system, "system", si)
        self._resolve_times(max(len(systems), 1))
        score = Score()
        score.units = self._make_units()
        self._make_static(systems, score)
        score.notes = self._audio_notes(tk)
        return score

    # -- discovery ---------------------------------------------------------------
    @staticmethod
    def _kind(c):
        return next(k for k in ("chord", "note", "rest", "mRest", "multiRest", "mRpt", "mRpt2",
                                "beatRpt", "halfmRpt") if k in c)

    def _drawable(self, el):
        return any(_tag(e) in DRAWABLE for e in el.iter())

    def _walk(self, el, parent, si):
        """Register the units below `el`; returns the note/rest records found (for bundles)."""
        found = []
        for ch in list(el):
            if _tag(ch) != "g":
                continue
            c = _classes(ch)
            if c & (NOTE_KINDS | REST_KINDS):
                found.append(self._unit(ch, self._kind(c), si))
            elif c & BUNDLES:
                found += self._bundle(ch, si, next(iter(c & BUNDLES)))
            elif "ledgerLines" in c:
                for p in list(ch):
                    if _tag(p) == "path":
                        self._unit(p, "ledger", si)
            elif c & STATIC:
                continue
            elif c & STRUCTURE:
                found += self._walk(ch, next(iter(c & STRUCTURE)), si)
            elif parent in ("measure", "layer") and self._drawable(ch):
                kind = next((k for k in (ch.get("class") or "g").split() if k != "autogenerated"), "g")
                self._unit(ch, kind, si)
        return found

    def _bundle(self, el, si, kind):
        members, loose = [], []
        for ch in list(el):
            c = _classes(ch) if _tag(ch) == "g" else set()
            if c & (NOTE_KINDS | REST_KINDS):
                members.append(self._unit(ch, self._kind(c), si))
            elif c & BUNDLES:
                members += self._bundle(ch, si, next(iter(c & BUNDLES)))
            elif _tag(ch) in DRAWABLE or (_tag(ch) == "g" and self._drawable(ch)):
                loose.append(ch)
        if loose:  # beam polygons, tuplet brackets ... become one unit of their own
            wrap = etree.Element(_G)
            wrap.set("class", kind)
            el.insert(list(el).index(loose[0]), wrap)
            for e in loose:
                wrap.append(e)
            self._unit(wrap, kind, si)["members"] = members
        return members

    def _unit(self, el, kind, si):
        el.set("data-unit", str(len(self.recs)))
        if kind in NOTE_KINDS:
            ids = [e.get("id") for e in el.iter(_G) if "note" in _classes(e)] if kind == "chord" \
                else [el.get("id")]
        elif kind in REST_KINDS:
            ids = [el.get("id")]
        else:
            ids = []
        rec = {"n": len(self.recs), "el": el, "kind": kind, "box": self.calc.box(el), "system": si,
               "ids": ids, "members": [], "time": None, "end": None}
        self.recs.append(rec)
        return rec

    # -- timing ----------------------------------------------------------------------
    def _anchor_x(self, rec):
        for head in rec["el"].iter(_G):
            if "notehead" in _classes(head):
                b = self.calc.box(head)
                if b:
                    return (b[0] + b[2]) / 2
        b = rec["box"]
        return None if b is None else (b[0] + b[2]) / 2

    def _resolve_times(self, nsys):
        anchors = [[] for _ in range(nsys)]  # per system: (x, time), sorted by x
        for r in self.recs:
            ts = [self.on[i] for i in r["ids"] if i in self.on]
            if ts:
                r["time"] = min(ts)
                offs = [self.off[i] for i in r["ids"] if i in self.off]
                r["end"] = max(offs) if offs else r["time"]
                x = self._anchor_x(r)
                if x is not None and r["kind"] in ("note", "chord", "rest"):
                    anchors[r["system"]].append((x, r["time"]))
        for a in anchors:
            a.sort()

        for r in self.recs:  # everything else is timed by where it sits next to the notes
            if r["time"] is not None or r["members"] or r["box"] is None:
                continue
            a = anchors[r["system"]]
            if not a:
                r["time"] = r["end"] = 0.0
                continue
            xs = [p[0] for p in a]
            if r["kind"] in NOTE_KINDS | REST_KINDS | {"ledger"}:
                x = self._anchor_x(r)
                i = bisect_right(xs, x)
                j = min((j for j in (i - 1, i) if 0 <= j < len(a)), key=lambda j: (abs(xs[j] - x), a[j][1]))
                r["time"] = r["end"] = a[j][1]
            else:
                b = r["box"]
                i0 = max(bisect_right(xs, b[0] + POSITION_TOLERANCE) - 1, 0)
                i1 = max(bisect_right(xs, b[2] - POSITION_TOLERANCE) - 1, i0)
                r["time"], r["end"] = a[i0][1], max(a[i1][1], a[i0][1])

        for r in self.recs:  # beams & tuplets: from first to last member note
            if r["members"]:
                ts = [m["time"] for m in r["members"] if m["time"] is not None]
                r["time"], r["end"] = (min(ts), max(ts)) if ts else (0.0, 0.0)

    # -- svg documents -------------------------------------------------------------------
    def _doc(self, el, rect):
        hrefs = {(u.get(_HREF) or "").lstrip("#") for u in el.iter(_USE)}
        defs = b"".join(self.defs_xml[h] for h in hrefs if h in self.defs_xml)
        x, y, w, h = rect
        tf = f' transform="{self.margin_tf}"' if self.margin_tf else ""
        return (f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}" '
                f'width="{w:.2f}" height="{h:.2f}"><style type="text/css">{CSS}</style><defs>').encode() \
            + defs + f'</defs><g color="{self.ink}" fill="{self.ink}" font-family="Times, serif">' \
                     f'<g{tf}>'.encode() + etree.tostring(el, with_tail=False) + b"</g></g></svg>"

    def _page_rect(self, box, pad):
        x0, y0, x1, y1 = _pad(_xf_box(box, self.m), pad)
        return (x0, y0, x1 - x0, y1 - y0)

    def _stem_x(self, rec):
        """Page x where a bundle (beam...) reaches this member: its stem, else the right edge."""
        for e in rec["el"].iter(_G):
            if "stem" in _classes(e):
                b = self.calc.box(e)
                if b:
                    return _xf_box(b, self.m)[2]
        b = rec["box"]
        return None if b is None else _xf_box(b, self.m)[2]

    def _steps(self, r, rect):
        pts = sorted((m["time"], x, m["n"]) for m in r["members"]
                     if m["time"] is not None and (x := self._stem_x(m)) is not None)
        if len(pts) < 2:
            return ()
        steps, reach = [], rect[0]
        for i, (t, x, uid) in enumerate(pts):
            reach = max(reach, x)
            frac = 1.0 if i == len(pts) - 1 else min(max((reach - rect[0]) / max(rect[2], 1e-6), 0.0), 1.0)
            steps.append((uid, t, frac))
        return tuple(steps)

    def _make_units(self):
        out = []
        for r in self.recs:
            if r["box"] is None or r["time"] is None:
                continue
            rect = self._page_rect(r["box"], 30)
            span = r["end"] - r["time"]
            out.append(Unit(uid=r["n"], kind=r["kind"], svg=self._doc(r["el"], rect), rect=rect,
                            time=r["time"], end=r["end"], system=r["system"],
                            wipe=r["kind"] in WIPE_KINDS and span > 0.12,
                            steps=self._steps(r, rect) if r["members"] else ()))
        return out

    def _make_static(self, systems, score):
        for si, system in enumerate(systems):
            cp = copy.deepcopy(system)
            for e in list(cp.iter()):  # drop everything that is a unit
                if isinstance(e.tag, str) and e.get("data-unit") is not None and e.getparent() is not None:
                    e.getparent().remove(e)
            # one layer per measure (so the renderer can skip off-screen ones) + one for the rest
            parts = [m for m in cp if _tag(m) == "g" and "measure" in _classes(m)]
            for m in parts:
                cp.remove(m)
            extent = None  # everything this system draws, notes included
            for part in parts + [cp]:
                box = self.calc.box(part)
                if box is not None:
                    rect = self._page_rect(box, 60)
                    score.layers.append(StaticLayer(self._doc(part, rect), rect))
                    extent = _union(extent, (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]))
            mine = [u for u in score.units if u.system == si]
            for u in mine:
                extent = _union(extent, (u.rect[0], u.rect[1], u.rect[0] + u.rect[2], u.rect[1] + u.rect[3]))
            if extent is None:
                continue
            ts = [u.time for u in mine]
            ends = [u.end for u in mine if u.kind in NOTE_KINDS]
            score.systems.append(System((extent[0], extent[1], extent[2] - extent[0], extent[3] - extent[1]),
                                        min(ts, default=0.0), max(ends + ts, default=0.0)))

    # -- audio ------------------------------------------------------------------------------
    def _audio_notes(self, tk):
        notes = []
        for r in self.recs:
            if r["kind"] in NOTE_KINDS:
                for i in r["ids"]:
                    if i in self.on:
                        pitch = tk.getMIDIValuesForElement(i).get("pitch")
                        if pitch is not None:
                            notes.append([pitch, self.on[i], self.off.get(i, self.on[i] + 0.25), 80])
        notes.sort(key=lambda n: (n[0], n[1]))
        merged = []  # fuse ties: same pitch where the next note starts as the previous ends
        for n in notes:
            if merged and merged[-1][0] == n[0] and abs(merged[-1][2] - n[1]) < 0.003:
                merged[-1][2] = n[2]
            else:
                merged.append(n)
        return sorted((tuple(n) for n in merged), key=lambda n: n[1])
