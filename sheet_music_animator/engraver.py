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

import base64
import copy
import json
import math
import re
import zipfile
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path as FsPath

import numpy as np
import verovio
from lxml import etree
from svgelements import Path as SvgPath

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
_G, _SVG, _USE = (f"{{{SVG_NS}}}{t}" for t in ("g", "svg", "use"))
_HREF = f"{{{XLINK_NS}}}href"
_NUM = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?", re.I)
_NOT_LINES = re.compile(r"[^eE\d\s.,+-]")   # what is left once absolute M/L commands are removed from plain-polyline paths

NOTE_KINDS = {"note", "chord"}
REST_KINDS = {"rest", "mRest", "multiRest", "mRpt", "mRpt2", "beatRpt", "halfmRpt"}
BUNDLES = {"beam", "tuplet", "fTrem", "bTrem", "graceGrp", "ligature"}
STRUCTURE = {"mdiv", "score", "system", "measure", "staff", "layer", "section"}
STATIC = {"clef", "keySig", "meterSig", "barLine", "mNum", "space", "grpSym", "label", "labelAbbr",
          "sb", "pb", "pageMilestone", "systemMilestone", "scoreDef", "staffDef", "ending"}
# Units that grow from left to right over their span instead of just fading in.
WIPE_KINDS = {"beam", "tuplet", "slur", "tie", "hairpin", "pedal", "octave", "bracketSpan",
              "beamSpan", "gliss", "lv", "fTrem", "bTrem", "dir", "dynam"}
# Lines that run along the music (8va, hairpins, "cresc. - - -") grow note by note, even when notes appear instantly;
# slurs and ties appear whole.
LINE_KINDS = {"hairpin", "pedal", "octave", "bracketSpan", "gliss", "dir", "dynam"}
DRAWABLE = {"path", "use", "polygon", "polyline", "rect", "ellipse", "text", "line"}
CSS = "ellipse,path,polygon,polyline,rect{stroke:currentColor}"
CROSS_STAFF_SPACING = 20   # Verovio's default is 12; cross-staff beams then run into the notes of the staff below
FONT_TOKEN = b"@@FONT@@"   # stands for the font family in every SVG; filled in when drawing
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
    static: bool = False  # clefs, barlines, key signatures...: always on show unless the project times them
    # Beams, tuplets, tremolos: (member uid, member time, fraction of rect width to show once that
    # member has appeared) so the shape grows note by note instead of all at once.
    steps: tuple = ()
    measure: int = -1     # index of the measure the element belongs to (in drawing order)
    # Notes and chords: (x0, y0, x1, y1, staff, tied, pitch) of every notehead in page space; `tied` marks a
    # note that only continues a tie; `pitch` is the written MIDI pitch (without accidentals).  For the light-up effect.
    heads: tuple = ()
    label: str = ""       # dynamics: what is written ("ff", "sfz"); articulations: "acc", "marc", ...


@dataclass
class MeasureInfo:
    index: int
    system: int
    rect: tuple           # x, y, w, h of the staves of the measure (page space)
    time: float           # first note in the measure
    end: float


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
    measure_infos: list[MeasureInfo] = field(default_factory=list)
    line_starts: list[int] = field(default_factory=list)  # index of the first measure of every line
    notes: list[tuple] = field(default_factory=list)      # (midi pitch, start, end, velocity)
    nominal_notes: list[tuple] = field(default_factory=list)   # the same before `warp` (the score's own timing)
    duration: float = 0.0
    _now_cache: dict = field(default_factory=dict, repr=False)

    def warp(self, points) -> None:
        """Re-time the whole score with a time map of (score seconds, recording seconds) points, e.g. the
        result of aligning it to a performance.  Times outside the map shift by the nearest end's offset."""
        if not points or len(points) < 2:
            return
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        lo_off, hi_off = ys[0] - xs[0], ys[-1] - xs[-1]

        def f(t):
            if t <= xs[0]:
                return t + lo_off
            if t >= xs[-1]:
                return t + hi_off
            return float(np.interp(t, xs, ys))
        for u in self.units:
            u.time, u.end = f(u.time), f(u.end)
            if u.steps:
                u.steps = tuple((uid, f(t), frac) for uid, t, frac in u.steps)
        self.measures = [(f(t), n) for t, n in self.measures]
        for m in self.measure_infos:
            m.time, m.end = f(m.time), f(m.end)
        for sy in self.systems:
            sy.start, sy.end = f(sy.start), f(sy.end)
        self.notes = [(p, f(a), f(b), *rest) for p, a, b, *rest in self.notes]
        self.duration = f(self.duration)
        self._now_cache.clear()

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
            d = el.get("d") or ""
            if d and not _NOT_LINES.search(d.replace("M", "").replace("L", "")):   # stems, ledger lines: skip the slow parser
                v = [float(x) for x in _NUM.findall(d)]
                b = (min(v[0::2]), min(v[1::2]), max(v[0::2]), max(v[1::2])) if len(v) >= 4 else None
            else:
                try:
                    bb = SvgPath(d).bbox()
                    b = None if bb is None else tuple(float(v) for v in bb)
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


# --------------------------------------------------------------------------- hidden staves
MEI_NS = "http://www.music-encoding.org/ns/mei"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


def _read_musicxml(path):
    """Root element of a (possibly compressed) MusicXML file, or None."""
    try:
        data = FsPath(path).read_bytes()
        if data[:2] == b"PK":
            with zipfile.ZipFile(FsPath(path)) as z:
                names = [n for n in z.namelist() if n.lower().endswith((".xml", ".musicxml")) and not n.startswith("META-INF")]
                if "META-INF/container.xml" in z.namelist():
                    full = etree.fromstring(z.read("META-INF/container.xml")).find(".//{*}rootfile")
                    if full is not None and full.get("full-path") in z.namelist():
                        names = [full.get("full-path")]
                data = z.read(names[0])
        return etree.fromstring(data)
    except Exception:
        return None


def hidden_staves(path) -> dict[str, set[int]]:
    """{measure number: staves that the notation program hid there}, read from the MusicXML.

    Staves are numbered the way Verovio/MEI numbers them (continuing across parts).  MuseScore hides an
    empty staff per system and writes `<staff-details print-object="no">` in the first measure of every
    system where it is hidden; other programs switch it off until the next staff-details.
    """
    root = _read_musicxml(path)
    if root is None or root.tag != "score-partwise":
        return {}
    musescore = "musescore" in " ".join(root.xpath("//software/text()")).lower()
    hidden: dict[str, set[int]] = {}
    base = 0
    for part in root.findall("part"):
        measures = part.findall("measure")
        nstaves = max([int(t) for t in part.xpath(".//attributes/staves/text()") if t.strip().isdigit()] or [1])
        off: set[int] = set()       # staves currently switched off (part-relative numbers)
        for i, m in enumerate(measures):
            new_system = i == 0 or any(p.get("new-system") == "yes" or p.get("new-page") == "yes" for p in m.findall("print"))
            if new_system and musescore:
                off = set()
            for d in m.findall("attributes/staff-details"):
                n = int(d.get("number") or 1)
                (off.add if d.get("print-object") == "no" else off.discard)(n)
            if off:
                hidden.setdefault(m.get("number"), set()).update(base + n for n in off)
        base += nstaves
    return hidden


def has_cross_staff(path) -> bool:
    """True when some voice of the MusicXML moves between staves of its part (cross-staff notes)."""
    root = _read_musicxml(path)
    if root is None or root.tag != "score-partwise":
        return False
    for m in root.iterfind("part/measure"):
        staves: dict[str, set[str]] = {}
        for n in m.findall("note"):
            staves.setdefault(n.findtext("voice") or "1", set()).add(n.findtext("staff") or "1")
        if any(len(v) > 1 for v in staves.values()):
            return True
    return False


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _layer_events(layer):
    """(time in ppq, element) of the notes, rests and clefs of a MEI layer, in order."""
    out, t = [], 0

    def walk(el):
        nonlocal t
        for c in el:
            tag = _local(c)
            if tag in ("beam", "tuplet", "bTrem", "fTrem", "graceGrp"):
                walk(c)
            elif tag == "chord":
                notes = [n for n in c if _local(n) == "note"]
                out.append((t, c))
                out.extend((t, n) for n in notes)
                t += int(c.get("dur.ppq") or (notes[0].get("dur.ppq") if notes else 0) or 0)
            elif tag in ("note", "rest", "space"):
                out.append((t, c))
                t += int(c.get("dur.ppq") or 0)
            elif tag == "clef":
                out.append((t, c))
    walk(layer)
    return out


_TEXT_STYLE = ("font-size", "font-family", "font-weight", "font-style", "fill")
_TEXT_POS = ("x", "y", "dx", "dy")
# Leipzig (Verovio's music font) glyphs that occur in text, with a plain-text stand-in
_TEXT_GLYPHS = {"\ueca5": "\u2669", "\ueca7": "\u266a"}   # SMuFL metronome marks: quarter, eighth


def _flatten_text(root) -> None:
    """Qt's SVG renderer drops text whose <tspan>s are nested (Verovio nests them 3 deep): rebuild
    every <text> as a flat list of <tspan>s that carry the inherited font attributes."""
    for text in list(root.iter(f"{{{SVG_NS}}}text")):
        if not any(_tag(c) == "tspan" and any(_tag(g) == "tspan" for g in c) for c in text):
            continue
        leaves = []

        def walk(el, style, pos):
            for ch in el:
                if _tag(ch) != "tspan":
                    continue
                st = dict(style, **{k: ch.get(k) for k in _TEXT_STYLE if ch.get(k)})
                ps = dict(pos, **{k: ch.get(k) for k in _TEXT_POS if ch.get(k)})
                if any(_tag(g) == "tspan" for g in ch):
                    walk(ch, st, ps)
                    pos.clear()
                else:
                    leaves.append((st, ps, ch.text or ""))
                    pos.clear()
        walk(text, {}, {})
        for i, (_, _, string) in enumerate(leaves):
            # Verovio writes "[♩ = 104]" as "[", "]", ♩, "= 104": put the bracket back at the end
            if string.strip() == "]" and i + 1 < len(leaves) and any("[" in l[2] for l in leaves[:i]):
                leaves.append(leaves.pop(i))
                break
        for ch in list(text):
            text.remove(ch)
        for st, ps, string in leaves:
            ts = etree.SubElement(text, f"{{{SVG_NS}}}tspan", **st, **ps)
            ts.text = "".join(_TEXT_GLYPHS.get(c, c) for c in string)
            if st.get("font-family") == "Leipzig" and string and string[0] in _TEXT_GLYPHS:
                ts.set("font-family", "serif")


def _clef_attrs(el):
    return {k: v for k, v in el.attrib.items() if k in ("shape", "line", "dis", "dis.place")}


def _fix_cross_staff_clefs(root) -> bool:
    """Verovio positions a cross-staff note with the clef that its target staff had at the start of the
    measure unless that staff has a layer with the same number as the note's own layer.  A note that
    crosses into a staff after a mid-measure clef change (right hand moving down into a bass staff that
    just changed back from treble) then lands on the wrong lines.  Give such staves an otherwise empty,
    invisible layer holding the clef that is really in effect."""
    ns = {"m": MEI_NS}
    state: dict[str, dict] = {}
    changed = False

    def take_scoredef(sd):
        for sdef in sd.iterfind(".//m:staffDef", ns):
            clef = sdef.find("m:clef", ns)
            if clef is not None:
                state[sdef.get("n")] = _clef_attrs(clef)

    for el in root.iter():
        tag = _local(el)
        if tag == "scoreDef":
            take_scoredef(el)
            continue
        if tag != "measure":
            continue
        staves = {st.get("n"): st for st in el.findall("m:staff", ns)}
        clefs: dict[str, list] = {n: [] for n in staves}      # (time, attrs) of the clef changes in each staff
        events = {}
        for n, st in staves.items():
            for lay in st.findall("m:layer", ns):
                ev = _layer_events(lay)
                events[(n, lay.get("n"))] = ev
                clefs[n] += [(t, _clef_attrs(c)) for t, c in ev if _local(c) == "clef"]
        wanted: dict[tuple, list] = {}                        # (target staff, layer) -> clefs needed
        for (n, lay_n), ev in events.items():
            for t, note in ev:
                tgt = note.get("staff")
                if _local(note) == "note" and tgt and tgt != n and tgt in staves:
                    if all(lay.get("n") != lay_n for lay in staves[tgt].findall("m:layer", ns)):
                        now = state.get(tgt, {})
                        for ct, attrs in sorted(clefs[tgt], key=lambda c: c[0]):
                            if ct <= t:
                                now = attrs
                        wanted.setdefault((tgt, lay_n), []).append(now)
        for (tgt, lay_n), nows in wanted.items():
            if not nows[0]:
                continue
            layer = etree.Element(f"{{{MEI_NS}}}layer", n=lay_n)
            etree.SubElement(layer, f"{{{MEI_NS}}}clef", visible="false", **nows[0])
            staves[tgt].insert(0, layer)
            changed = True
        for n in staves:
            if clefs[n]:
                state[n] = max(clefs[n], key=lambda c: c[0])[1]
    return changed


def _merge_slur_chains(root) -> bool:
    """MuseScore exports one long slur of a voice that moves between staves as a chain of short slurs
    (start 1, start 2, stop 1, stop 2, start 1 ...) and Verovio draws each of them.  Slurs that
    interleave within one voice (A starts, B starts, A ends, B ends) are merged into one, and so are slurs that
    continue such a chain on the very note where it stopped."""
    ns = {"m": MEI_NS}
    pos: dict[str, tuple] = {}
    voice: dict[str, tuple] = {}
    slurs = []
    for mi, measure in enumerate(root.iterfind(".//m:measure", ns)):
        for lay in measure.iterfind("m:staff/m:layer", ns):
            for t, el in _layer_events(lay):
                if el.get(XML_ID):
                    pos[el.get(XML_ID)] = (mi, t)
                    voice[el.get(XML_ID)] = (lay.get("n"), el.get("staff") or lay.getparent().get("n"))
        slurs += [s for s in measure.findall("m:slur", ns)]
    items = []
    for sl in slurs:
        a, b = (sl.get("startid") or "").lstrip("#"), (sl.get("endid") or "").lstrip("#")
        if a in pos and b in pos and pos[a] != pos[b]:
            if voice[a][0] == voice[b][0]:   # both ends in the same voice
                items.append({"el": sl, "a": pos[a], "b": pos[b], "aid": a, "bid": b, "grp": len(items),
                              "voice": voice[a][0]})
    if len(items) < 2:
        return False

    def union(i, j):
        gi, gj = items[i]["grp"], items[j]["grp"]
        for it in items:
            if it["grp"] == gj:
                it["grp"] = gi
    for i, A in enumerate(items):
        for j, B in enumerate(items):
            if i != j and A["voice"] == B["voice"] and A["a"] < B["a"] < A["b"] < B["b"]:
                union(i, j)
    merged = False
    again = True
    while again:
        again = False
        for grp in {it["grp"] for it in items}:
            members = [it for it in items if it["grp"] == grp]
            if len(members) < 2:
                continue
            last = max(members, key=lambda it: it["b"])
            for it in items:
                if it["grp"] != grp and it["aid"] == last["bid"] and it["b"] > last["b"] and it["voice"] == last["voice"]:
                    union(items.index(last), items.index(it))
                    again = True
                    break
            if again:
                break
    for grp in {it["grp"] for it in items}:
        members = sorted((it for it in items if it["grp"] == grp), key=lambda it: it["a"])
        if len(members) < 2:
            continue
        first, last = members[0], max(members, key=lambda it: it["b"])
        first["el"].set("endid", "#" + last["bid"])
        for it in members[1:]:
            it["el"].getparent().remove(it["el"])
        merged = True
    return merged


def _set_line_breaks(root, breaks) -> bool:
    """Replace the line and page breaks of the score: `breaks` is a number (a break before every n-th
    measure) or the sorted indices of the measures that start a line."""
    ns = {"m": MEI_NS}
    for el in list(root.iter(f"{{{MEI_NS}}}sb")) + list(root.iter(f"{{{MEI_NS}}}pb")):
        el.getparent().remove(el)
    for i, measure in enumerate(root.iterfind(".//m:measure", ns)):
        if i and (i in breaks if isinstance(breaks, (list, tuple, set)) else i % breaks == 0):
            measure.addprevious(etree.Element(f"{{{MEI_NS}}}sb"))
    return True


def _adjust_mei(tk, path, hidden, cross, breaks=None) -> set[str]:
    """Work around Verovio's MusicXML import by re-loading the score through MEI:
    * hidden staves (print-object="no"): marked invisible, their xml:ids returned so that
      `_remove_hidden_staves` can take them out of the SVG afterwards (only rest-only staves are touched);
    * cross-staff clefs (see `_fix_cross_staff_clefs`).
    Returns the ids of the hidden <staff> elements (and re-breaks the lines when `breaks` is given)."""
    ns = {"m": MEI_NS}
    root = etree.fromstring(tk.getMEI().encode("utf8"))
    changed = _fix_cross_staff_clefs(root) if cross else False
    if cross:
        changed = _merge_slur_chains(root) or changed
    ids: set[str] = set()
    if hidden:
        for measure in root.iterfind(".//m:measure", ns):
            gone = hidden.get(measure.get("n"), set())
            for staff in measure.findall("m:staff", ns):
                sid = staff.get(XML_ID)
                if sid and int(staff.get("n", 0)) in gone and not staff.xpath(".//m:note", namespaces=ns):
                    staff.set("visible", "false")
                    ids.add(sid)
                elif staff.get("visible") == "false":
                    del staff.attrib["visible"]   # Verovio's import switches off *every* staff of such a measure
        changed = changed or bool(ids)
    if breaks:
        changed = _set_line_breaks(root, breaks) or changed
    if not changed:
        return set()
    if not tk.loadData(etree.tostring(root, encoding="unicode")):
        tk.loadFile(str(path))   # could not be applied: keep the plain import
        return set()
    return ids


def _scale_y(d: str, top: float, k: float) -> str:
    """Scale the y coordinates of an absolute M/C/L path about `top`."""
    nums = _NUM.findall(d)
    out, i = [], 0
    for tok in re.split(r"(-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?)", d):
        if i < len(nums) and tok == nums[i]:
            v = float(tok)
            out.append(f"{(top + (v - top) * k) if i % 2 else v:.2f}")
            i += 1
        else:
            out.append(tok)
    return "".join(out)


def _remove_hidden_staves(svg_root, ids: set[str]):
    """Delete the hidden staves from the SVG and shrink each system's brace (and centre its labels) to
    the staves that are left, the way the notation program draws it."""
    cls = lambda e: set((e.get("class") or "").split())
    for system in [e for e in svg_root.iter(_G) if "system" in cls(e)]:
        measures = [e for e in system if e.tag == _G and "measure" in cls(e)]
        gone = [st for m in measures for st in m if st.tag == _G and st.get("id") in ids]
        if not gone or not measures:
            continue
        first_staves = [st for st in measures[0] if st.tag == _G and "staff" in cls(st)]
        keep = [st for st in first_staves if st.get("id") not in ids]
        mixed = not all(any(st.get("id") in ids for st in m if st.tag == _G) for m in measures)
        for st in gone:
            st.getparent().remove(st)
        if mixed:
            continue   # a system that mixes hidden and shown measures keeps its full brace
        lines = lambda st: [float(v) for p in st if p.tag.endswith("path") for v in _NUM.findall(p.get("d") or "")[1::2]]
        ys = [y for st in keep for y in lines(st)]
        old_ys = [y for st in first_staves for y in lines(st)]
        if not ys or not old_ys:
            continue
        top, old_bottom, new_bottom = min(old_ys), max(old_ys), max(ys)
        for grp in system:
            if grp.tag.endswith("path"):   # the line at the start of the system joins all the staves
                v = [float(x) for x in _NUM.findall(grp.get("d") or "")[1::2]]
                if len(v) >= 2 and max(v) > min(v):
                    lo, hi = min(v), max(v)
                    grp.set("d", _scale_y(grp.get("d"), lo, (hi - (old_bottom - new_bottom) - lo) / (hi - lo)))
            elif grp.tag == _G and "grpSym" in cls(grp):
                paths = [p for p in grp if p.tag.endswith("path")]
                pys = [float(v) for p in paths for v in _NUM.findall(p.get("d") or "")[1::2]]
                if pys and max(pys) - min(pys) > 1:
                    t, b = min(pys), max(pys)
                    k = (new_bottom + (b - old_bottom) - t) / (b - t)
                    for p in paths:
                        p.set("d", _scale_y(p.get("d"), t, k))
            elif grp.tag == _G and cls(grp) & {"label", "labelAbbr"}:   # centred on the whole group: move to the staves left
                dy = ((top + new_bottom) - (top + old_bottom)) / 2
                grp.set("transform", f"translate(0,{dy:g}) " + (grp.get("transform") or ""))


# --------------------------------------------------------------------------- engraving
ENCODED_BREAKS = {"breaks": "encoded", "pageWidth": 100000, "pageHeight": 60000,
                  "adjustPageWidth": True, "adjustPageHeight": True}


def engrave(path, layout: str = "pages", ink: str = "#000000", progress=None,
            measures_per_line: int | None = None, line_starts: list[int] | None = None) -> Score:
    """Engrave a MusicXML file.  `measures_per_line` (0 = the whole score on one line) or the explicit
    `line_starts` (index of the first measure of every line) decide where the lines break; without
    either, `layout` is used and Verovio breaks the lines itself."""
    say = progress or (lambda *_: None)
    say("Engraving with Verovio…")
    tk = verovio.toolkit()
    opts = {"scale": 40, "svgViewBox": True, "header": "none", "footer": "none",
            "svgAdditionalAttribute": ["tie@endid", "artic@artic", "note@pname", "note@oct"],
            "pageMarginLeft": 40, "pageMarginRight": 40, "pageMarginTop": 60, "pageMarginBottom": 60}
    breaks = None
    if line_starts is not None or measures_per_line is not None:
        one_line = len(line_starts) <= 1 if line_starts is not None else measures_per_line == 0
        opts.update(LAYOUTS["horizontal"] if one_line else ENCODED_BREAKS)
        if not one_line:
            breaks = sorted(set(line_starts)) if line_starts is not None else measures_per_line
    else:
        opts.update(LAYOUTS[layout])
    hidden, cross = hidden_staves(path), has_cross_staff(path)
    if cross:   # a beam that crosses between staves is not taken into account when Verovio spaces the staves
        opts["spacingStaff"] = CROSS_STAFF_SPACING
    tk.setOptions(opts)
    if not tk.loadFile(str(path)):
        raise ValueError(f"Verovio could not read {path}")
    hidden_ids = _adjust_mei(tk, path, hidden, cross, breaks) if hidden or cross or breaks else set()
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
    if hidden_ids:
        _remove_hidden_staves(root, hidden_ids)
    inner = next(e for e in root if e.tag == _SVG)
    vb = [float(x) for x in inner.get("viewBox").split()]
    defs = {e.get("id"): e for d in root.iter(f"{{{SVG_NS}}}defs") for e in d if e.get("id")}
    margin = next(e for e in inner.iter(_G) if "page-margin" in _classes(e))

    tied = {(e.get("data-endid") or "").lstrip("#") for e in inner.iter(_G) if "tie" in _classes(e)} - {""}
    b = _Builder(defs, ink, margin.get("transform"), tied)
    score = b.run(margin, on, off, tk)
    score.width, score.height = vb[2], vb[3]
    score.title = FsPath(path).stem
    score.measures = [(t, i + 1) for i, t in enumerate(measures)]
    score.duration = max([off.get(k, 0) for k in off] + [u.end for u in score.units] + [0])
    return score


class _Builder:
    def __init__(self, defs, ink, margin_transform, tied_ids=()):
        self.tied_ids = set(tied_ids)
        self.calc = BoxCalculator(defs)
        self.defs_xml = {k: etree.tostring(v, with_tail=False) for k, v in defs.items()}
        self.ink = ink
        self.margin_tf = margin_transform
        self.m = _parse_transform(margin_transform)
        self.recs: list[dict] = []
        self.measure_recs: list[dict] = []   # {"system", "rect"} of every measure in drawing order
        self.cur_measure = -1

    def run(self, margin, on, off, tk) -> Score:
        self.on, self.off = on, off
        systems = [e for e in margin if _tag(e) == "g" and "system" in _classes(e)]
        for si, system in enumerate(systems):
            self._walk(system, "system", si)
        self._resolve_times(max(len(systems), 1))
        score = Score()
        score.units = self._make_units()
        self._make_measures(score)
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
                if self._drawable(ch):
                    mid = parent == "layer"   # a clef/key/meter change inside the music, not at the start of the staff
                    self._unit(ch, next(iter(c & STATIC)), si, static=not mid, mid=mid)
            elif c & STRUCTURE:
                if "measure" in c:
                    self.cur_measure = self._measure(ch, si)
                found += self._walk(ch, next(iter(c & STRUCTURE)), si)
                if "measure" in c:
                    self.cur_measure = -1
            elif (parent in ("measure", "layer") or "spanning" in c) and self._drawable(ch):
                # "spanning": the part of a slur/8va/hairpin... that continues on the next system
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
            elif c & STATIC:
                if self._drawable(ch):   # a clef change inside a beam
                    self._unit(ch, next(iter(c & STATIC)), si, mid=True)
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

    def _measure(self, el, si) -> int:
        """Register a measure; its rectangle is the extent of the staff lines of its staves."""
        xs, ys, staves = [], [], []
        for st in el:
            if _tag(st) == "g" and "staff" in _classes(st):
                sy = []
                for p in st:
                    if _tag(p) == "path":
                        b = self.calc.box(p)
                        if b:
                            xs += [b[0], b[2]]
                            ys += [b[1], b[3]]
                            sy += [b[1], b[3]]
                if sy:
                    staves.append(_xf_box((0, min(sy), 0, max(sy)), self.m)[1::2])
        if xs:
            x0, y0, x1, y1 = _xf_box((min(xs), min(ys), max(xs), max(ys)), self.m)
            rect = (x0, y0, x1 - x0, y1 - y0)
        else:
            rect = (0.0, 0.0, 0.0, 0.0)
        self.measure_recs.append({"system": si, "rect": rect, "staves": staves})
        return len(self.measure_recs) - 1

    PART_CLASSES = {"accid", "artic", "dots"}   # parts of a note that can be moved on their own

    def _unit(self, el, kind, si, static=False, mid=False, parent=None):
        el.set("data-unit", str(len(self.recs)))
        parts = []
        if kind in NOTE_KINDS:
            ids = [e.get("id") for e in el.iter(_G) if "note" in _classes(e)] if kind == "chord" \
                else [el.get("id")]
            for e in list(el.iter(_G)):   # accidentals, articulations and dots become units of their own
                if e is not el and _classes(e) & self.PART_CLASSES and self._drawable(e):
                    e.getparent().remove(e)
                    parts.append(e)
        elif kind in REST_KINDS:
            ids = [el.get("id")]
        else:
            ids = []
        rec = {"n": len(self.recs), "el": el, "kind": kind, "box": self.calc.box(el), "system": si,
               "ids": ids, "members": [], "time": None, "end": None, "static": static, "mid": mid,
               "measure": self.cur_measure, "parent": parent}
        self.recs.append(rec)
        for e in parts:
            self._unit(e, next(iter(_classes(e) & self.PART_CLASSES)), si, parent=rec)
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
        for r in self.recs:   # accidentals, articulations... appear with the note they belong to
            p = r["parent"]
            if p is not None and p["time"] is not None:
                r["time"], r["end"] = p["time"], p["time"]
        for a in anchors:
            a.sort()
        self.anchors = anchors
        chords = [[] for _ in range(nsys)]   # per system: (x, y0, y1, time) of every note, for arpeggio signs
        for r in self.recs:
            if r["kind"] in NOTE_KINDS and r["time"] is not None and r["box"] is not None:
                x = self._anchor_x(r)
                if x is not None:
                    chords[r["system"]].append((x, r["box"][1], r["box"][3], r["time"]))

        for r in self.recs:  # everything else is timed by where it sits next to the notes
            if r["time"] is not None or r["members"] or r["box"] is None:
                continue
            a = anchors[r["system"]]
            if not a:
                r["time"] = r["end"] = 0.0
                continue
            xs = [p[0] for p in a]
            if r["mid"]:   # a change inside the music takes effect with the note that follows it
                i = bisect_left(xs, r["box"][0])
                r["time"] = r["end"] = a[min(i, len(a) - 1)][1]
                continue
            if r["kind"] == "arpeg":   # the wavy line sits just left of the chord it rolls: it appears with that chord
                b = r["box"]
                nxt = [c for c in chords[r["system"]] if c[0] >= b[0] and c[1] <= b[3] and b[1] <= c[2]]
                if nxt:
                    r["time"] = r["end"] = min(nxt, key=lambda c: (c[0], c[3]))[3]
                    continue
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
        _flatten_text(el)
        hrefs = {(u.get(_HREF) or "").lstrip("#") for u in el.iter(_USE)}
        defs = b"".join(self.defs_xml[h] for h in hrefs if h in self.defs_xml)
        x, y, w, h = rect
        tf = f' transform="{self.margin_tf}"' if self.margin_tf else ""
        return (f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}" '
                f'width="{w:.2f}" height="{h:.2f}"><style type="text/css">{CSS}</style><defs>').encode() \
            + defs + f'</defs><g color="{self.ink}" fill="{self.ink}" font-family="@@FONT@@">' \
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

    def _line_steps(self, r, rect):
        """Like `_steps`, for lines without member notes: reveal up to the latest note under the line."""
        a = self.anchors[r["system"]]
        if not a or r["box"] is None:
            return ()
        b = r["box"]
        xs = [p[0] for p in a]
        i0 = max(bisect_right(xs, b[0] + POSITION_TOLERANCE) - 1, 0)
        i1 = max(bisect_right(xs, b[2] - POSITION_TOLERANCE) - 1, i0)
        steps, last_t = [], None
        for x, t in a[i0:i1 + 1]:
            if t == last_t:
                continue
            last_t = t
            px = _xf_box((x, 0, x, 0), self.m)[0]
            steps.append([-1, t, min(max((px + 120 - rect[0]) / max(rect[2], 1e-6), 0.0), 1.0)])
        if len(steps) < 2:
            return ()
        steps[-1][2] = 1.0
        for i in range(1, len(steps)):
            steps[i][2] = max(steps[i][2], steps[i - 1][2])
        return tuple(tuple(st) for st in steps)

    DYNAMIC_GLYPHS = {"E520": "p", "E521": "m", "E522": "f", "E523": "r", "E524": "s", "E525": "z", "E526": "n",
                      "E527": "pppppp", "E528": "ppppp", "E529": "pppp", "E52A": "ppp", "E52B": "pp", "E52C": "mp",
                      "E52D": "mf", "E52E": "pf", "E52F": "ff", "E530": "fff", "E531": "ffff", "E532": "fffff",
                      "E533": "ffffff", "E534": "fp", "E535": "fz", "E536": "sf", "E537": "sfp", "E538": "sfpp",
                      "E539": "sfz", "E53A": "sffz", "E53B": "rf", "E53C": "rfz"}

    def _dynamic_label(self, el) -> str:
        """What a dynamic marking says ("ff", "sfz"), read from the SMuFL glyphs it is drawn with."""
        glyphs = [(u.get(_HREF) or "").lstrip("#").split("-")[0] for u in el.iter(_USE)]
        return "".join(self.DYNAMIC_GLYPHS.get(g, "") for g in glyphs)

    def _heads(self, r) -> tuple:
        """Notehead rectangles (page space) of a note or chord, with the staff each one sits on."""
        el = r["el"]
        notes = [e for e in el.iter(_G) if "note" in _classes(e)] if r["kind"] == "chord" else [el]
        staves = (self.measure_recs[r["measure"]]["staves"] if 0 <= r["measure"] < len(self.measure_recs) else [])
        out = []
        for n in notes:
            head = next((g for g in n.iter(_G) if "notehead" in _classes(g)), None)
            b = self.calc.box(head) if head is not None else None
            if b is None:
                continue
            x0, y0, x1, y1 = _xf_box(b, self.m)
            cy = (y0 + y1) / 2
            staff = min(range(len(staves)), key=lambda i: abs(cy - (staves[i][0] + staves[i][1]) / 2)) if staves else 0
            pname, octave = n.get("data-pname"), n.get("data-oct")
            pitch = (12 * (int(octave) + 1) + "cdefgab".index(pname) * 2 - (1 if "cdefgab".index(pname) > 2 else 0)
                     ) if pname and octave and pname in "cdefgab" else 60
            out.append((x0, y0, x1, y1, staff, (n.get("id") or "") in self.tied_ids, pitch))
        return tuple(out)

    def _make_units(self):
        out = []
        for r in self.recs:
            if r["box"] is None or r["time"] is None:
                continue
            rect = self._page_rect(r["box"], 30)
            span = r["end"] - r["time"]
            heads, label = (), ""
            if r["kind"] in NOTE_KINDS:
                heads = self._heads(r)
            elif r["kind"] == "dynam":
                label = self._dynamic_label(r["el"])
            elif r["kind"] == "artic":
                label = r["el"].get("data-artic") or ""
            out.append(Unit(uid=r["n"], kind=r["kind"], svg=self._doc(r["el"], rect), rect=rect,
                            time=r["time"], end=r["end"], system=r["system"],
                            wipe=r["kind"] in WIPE_KINDS and span > 0.12, static=r["static"], measure=r["measure"],
                            heads=heads, label=label,
                            steps=self._steps(r, rect) if r["members"] else
                            self._line_steps(r, rect) if r["kind"] in LINE_KINDS and span > 0.12 else ()))
        return out

    def _make_measures(self, score):
        """Measure rectangles with their times, and the measure of every element that sits outside a
        measure (the continuation of a slur on the next line, a label...)."""
        times: dict[int, list] = {}
        for u in score.units:
            if u.measure >= 0 and u.kind in NOTE_KINDS | REST_KINDS:
                times.setdefault(u.measure, []).append((u.time, u.end))
        infos = []
        for i, m in enumerate(self.measure_recs):
            ts = times.get(i)
            t0 = min(t for t, _ in ts) if ts else (infos[-1].end if infos else 0.0)
            t1 = max(e for _, e in ts) if ts else t0
            infos.append(MeasureInfo(i, m["system"], m["rect"], t0, t1))
        score.measure_infos = infos
        score.line_starts = [next(m.index for m in infos if m.system == si) for si in sorted({m.system for m in infos})]
        for u in score.units:
            if u.measure >= 0:
                continue
            cx = u.rect[0] + u.rect[2] / 2
            mine = [m for m in infos if m.system == u.system and m.rect[2] > 0]
            if mine:
                u.measure = min(mine, key=lambda m: 0 if m.rect[0] <= cx <= m.rect[0] + m.rect[2]
                                else min(abs(cx - m.rect[0]), abs(cx - m.rect[0] - m.rect[2]))).index

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
    @staticmethod
    def _midi_notes(tk):
        """(pitch, start, end, velocity) of every note, read from one MIDI rendering.

        Asking Verovio for each element's pitch (getMIDIValuesForElement) re-renders the MIDI
        every time, which took ~10 s for a long piece.  Returns None when the MIDI cannot be read.
        """
        try:
            data = base64.b64decode(tk.renderToMIDI())
            if data[:4] != b"MThd":
                return None
            ntracks, division = int.from_bytes(data[10:12], "big"), int.from_bytes(data[12:14], "big")
            if division & 0x8000:
                return None
            pos, events, tempos = 14, [], []
            for _ in range(ntracks):
                if data[pos:pos + 4] != b"MTrk":
                    return None
                end = pos + 8 + int.from_bytes(data[pos + 4:pos + 8], "big")
                pos, tick, status = pos + 8, 0, 0

                def varlen():
                    nonlocal pos
                    v = 0
                    while True:
                        b = data[pos]
                        pos += 1
                        v = (v << 7) | (b & 0x7F)
                        if not b & 0x80:
                            return v
                while pos < end:
                    tick += varlen()
                    if data[pos] & 0x80:
                        status, pos = data[pos], pos + 1
                    if status == 0xFF:
                        kind, size = data[pos], None
                        pos += 1
                        size = varlen()
                        if kind == 0x51 and size == 3:
                            tempos.append((tick, int.from_bytes(data[pos:pos + 3], "big")))
                        pos += size
                    elif status in (0xF0, 0xF7):
                        pos += varlen()
                    elif status >> 4 in (0xC, 0xD):
                        pos += 1
                    else:
                        if status >> 4 in (0x8, 0x9):
                            events.append((tick, status >> 4 == 0x9 and data[pos + 1] > 0, data[pos], data[pos + 1]))
                        pos += 2
                pos = end
        except (IndexError, ValueError):
            return None
        tempos = sorted(set(tempos)) or [(0, 500000)]
        marks, sec = [], 0.0   # (tick, seconds at that tick, microseconds per quarter)
        for i, (tk_, us) in enumerate(tempos):
            if marks:
                sec += (tk_ - marks[-1][0]) * marks[-1][2] / 1e6 / division
            marks.append((tk_, sec, us))
        ticks = [m[0] for m in marks]

        def seconds(t):
            m = marks[max(bisect_right(ticks, t) - 1, 0)]
            return m[1] + (t - m[0]) * m[2] / 1e6 / division
        notes, open_ = [], {}
        for tick, on, pitch, vel in sorted(events, key=lambda e: (e[0], e[1])):   # offs before ons
            if on:
                open_.setdefault(pitch, []).append((seconds(tick), vel))
            elif open_.get(pitch):
                start, v = open_[pitch].pop(0)
                notes.append([pitch, start, seconds(tick), 80])
        return notes or None

    def _audio_notes(self, tk):
        notes = self._midi_notes(tk)
        if notes is None:   # slow path: ask Verovio for every element's pitch
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
