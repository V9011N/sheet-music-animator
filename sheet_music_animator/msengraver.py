"""Engraving with MuseScore: the score laid out by the notation program itself, exactly as it prints it.

MuseScore (installed on this computer) exports, from one layout pass:
* every page as SVG, in which every engraved element (notehead, stem, beam, slur, dynamic ...) is its own path
  with its kind as its class,
* the position of every measure (.mpos) and of every note segment (.spos) with the time it is played at,
* the MIDI of its playback (on the same clock) and the score as MusicXML.

`engrave_musescore` turns that into the same `Score` the Verovio engraver makes: every element becomes a unit
of its own, timed by the note segment it stands in (the segments are columns across the line, so x decides),
and the staff lines, barlines, clefs and key signatures at the start of a line are the static layers.  Text is
drawn as glyph outlines, so the score looks exactly as MuseScore prints it.

A .mscz is used as it is (its own style, line breaks and hidden staves).  A MusicXML file is tidied first
(xmlfix.clean: the playback-only tempo marks, doubled pedals ... a .mscz never has) and converted to a .mscz;
then an instrument written on three staves or more hides the staves that only rest for a line, as printed
piano music does.  The lines are broken as the score breaks them, unless measures per line (or explicit line
starts) are asked for; MuseScore still breaks a line that does not fit the page.

The exports are cached per score and layout (MuseScore takes a few seconds per score).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from bisect import bisect_right
from pathlib import Path

from lxml import etree

from . import xmlfix
from .engraver import (LINE_KINDS, NOTE_KINDS, REST_KINDS, WIPE_KINDS, BoxCalculator, MeasureInfo, Score,
                       StaticLayer, System, Unit, _read_musicxml, read_midi)

VERSION = 11                 # bump when the cached exports change meaning
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
POS_SCALE = 12.0            # .mpos/.spos coordinates are 12 times the SVG's
STAFF_SPACE = 184.0         # page units per staff space (the Verovio engraver's size: the camera, padding ... fit)
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# MuseScore element class -> the unit kind the rest of the program knows (the Verovio/MEI names)
KINDS = {
    "Note": "note", "Rest": "rest", "Stem": "stem", "Hook": "flag", "StemSlash": "stem", "Beam": "beam",
    "LedgerLine": "ledger", "Accidental": "accid", "NoteDot": "dots", "Articulation": "artic",
    "Fingering": "fing", "Dynamic": "dynam", "Expression": "dir", "StaffText": "dir", "SystemText": "dir",
    "Tempo": "tempo", "RehearsalMark": "reh", "TextLineSegment": "dir", "HairpinSegment": "hairpin",
    "SlurSegment": "slur", "TieSegment": "tie", "LaissezVibSegment": "lv", "OttavaSegment": "octave",
    "PedalSegment": "pedal", "Arpeggio": "arpeg", "TrillSegment": "trill", "Ornament": "ornam",
    "Fermata": "fermata", "Tuplet": "tuplet", "TremoloTwoChord": "fTrem", "TremoloSingleChord": "bTrem",
    "GlissandoSegment": "gliss", "Breath": "breath", "ChordLine": "dir", "Lyrics": "dir", "Harmony": "dir",
    "Clef": "clef", "KeySig": "keySig", "TimeSig": "meterSig", "BarLine": "barLine", "Bracket": "grpSym",
    "BracketItem": "grpSym", "InstrumentName": "label", "MeasureNumber": "mNum", "StaffLines": "staffLines",
    "Text": "text", "Image": "text", "VoltaSegment": "ending", "Spacer": None, "": None,
}
ALWAYS_STATIC = {"staffLines", "barLine", "grpSym", "label", "mNum", "ending"}
LINE_START = {"clef", "keySig", "meterSig"}        # static at the start of a line, timed inside it
SPANNING = {"beam", "slur", "tie", "lv", "hairpin", "pedal", "octave", "trill", "dir", "tuplet", "fTrem", "gliss"}
BEFORE = {"accid", "arpeg"}                        # drawn before the note they belong to
# Classes drawn as several paths (a word, a beam group, "Ped." and its line): consecutive paths of one class
# this close (staff spaces) are one element.
MERGE = {"Dynamic", "Expression", "StaffText", "SystemText", "Tempo", "Text", "TextLineSegment", "Fingering",
         "InstrumentName", "MeasureNumber", "RehearsalMark", "Tuplet", "PedalSegment", "TrillSegment",
         "OttavaSegment", "HairpinSegment", "Beam", "Arpeggio", "Lyrics", "Harmony", "VoltaSegment", "Fermata"}
MERGE_GAP = 1.2


# ------------------------------------------------------------------------------------------- MuseScore
def find_musescore() -> str | None:
    """The MuseScore executable: the MUSESCORE environment variable, the PATH, or its usual install folders."""
    env = os.environ.get("MUSESCORE")
    if env and Path(env).exists():
        return env
    for name in ("MuseScore4", "mscore4portable", "mscore", "musescore", "MuseScore3", "mscore3"):
        p = shutil.which(name)
        if p:
            return p
    candidates = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        if base:
            candidates += [Path(base) / "MuseScore 4" / "bin" / "MuseScore4.exe",
                           Path(base) / "MuseScore Studio 4" / "bin" / "MuseScore4.exe",
                           Path(base) / "MuseScore 3" / "bin" / "MuseScore3.exe"]
    candidates += [Path("/Applications/MuseScore 4.app/Contents/MacOS/mscore"),
                   Path("/Applications/MuseScore Studio 4.app/Contents/MacOS/mscore")]
    return next((str(c) for c in candidates if c.exists()), None)


def can_engrave(path) -> bool:
    return find_musescore() is not None and Path(path).suffix.lower() in (".mscz", ".mscx", ".mxl", ".musicxml", ".xml")


class _OneAtATime:
    """A lock between processes: two MuseScores running at once make one of them fail (it exits at once, or
    crashes) -- the editor and a render or the command line may engrave at the same time."""

    def __init__(self, wait=900.0, stale=900.0):
        self.path = _cache_dir() / "running.lock"
        self.wait, self.stale = wait, stale
        self.held = False

    def __enter__(self):
        t0 = time.time()
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:     # left behind by a crash
                        self.path.unlink()
                        continue
                except OSError:
                    continue
                if time.time() - t0 > self.wait:
                    return self               # give up waiting: run anyway (the retries below still help)
                time.sleep(0.25)

    def __exit__(self, *exc):
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def _run(exe, args, timeout=240, done=None, tries=4):
    """Run MuseScore (one at a time); with `done` (a check that its output is there) up to `tries` times: a
    MuseScore started while another one is still closing now and then exits at once without doing anything, or
    hangs (it is stopped after `timeout` seconds; the longest scores take well under a minute)."""
    # MuseScore is a Qt program: the Qt settings of this program (the render workers and the command line run Qt
    # "offscreen") are not passed on -- with QT_QPA_PLATFORM=offscreen MuseScore hangs on Windows.  Only a Linux
    # machine without a display needs it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("QT_")}
    if sys.platform.startswith("linux") and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["QT_QPA_PLATFORM"] = "offscreen"
    code, err = -1, ""
    with _OneAtATime():
        for attempt in range(tries if done else 1):
            if attempt:
                time.sleep(2.0 * attempt)
            try:
                p = subprocess.run([exe] + args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW,
                                   env=env)
                code, err = p.returncode, (p.stderr or b"").decode(errors="replace")[-600:]
            except subprocess.TimeoutExpired:
                code, err = -1, f"MuseScore did not finish in {timeout} s."
            if done is None or done():
                break
    return code, err


def _cache_dir() -> Path:
    d = Path.home() / ".sheet_music_animator" / "musescore"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------------------------------- the score file
def _score_files(z: zipfile.ZipFile):
    names = z.namelist()
    mscx = next((n for n in names if n.endswith(".mscx")), None)
    style = next((n for n in names if n.endswith(".mss")), None)
    return mscx, style


def _first_staff_measures(score_el):
    """The <Measure> elements of the first staff of the music (line breaks live there)."""
    staff = next((s for s in score_el.findall("Staff") if s.find("Measure") is not None), None)
    return [] if staff is None else staff.findall("Measure")


def _set_breaks(score_el, starts, one_line: bool, kind: str = "line") -> None:
    """Replace the line and page breaks: a break after the measure before every line start.  `kind` "section"
    ends each line as a section does (MuseScore does not stretch the last line of a section to the page width),
    without a pause or new bar numbers."""
    measures = _first_staff_measures(score_el)
    for staff in score_el.findall("Staff"):
        for m in staff.findall("Measure"):
            for lb in m.findall("LayoutBreak"):
                if (lb.findtext("subtype") or "") in ("line", "page"):
                    m.remove(lb)
    if one_line:
        return
    for i in starts:
        if 0 < i <= len(measures):
            m = measures[i - 1]
            lb = etree.Element("LayoutBreak")
            etree.SubElement(lb, "subtype").text = kind
            if kind == "section":
                for tag, v in (("pause", "0"), ("startWithLongNames", "0"), ("startWithMeasureOne", "0"),
                               ("firstSystemIndentation", "0")):
                    etree.SubElement(lb, tag).text = v
            eid = m.find("eid")
            (eid.addnext(lb) if eid is not None else m.insert(0, lb))


def _style_root(z, style_name, mscx_root):
    """(the element holding the style values, the document it belongs to or None when it is in the score)."""
    if style_name:
        doc = etree.fromstring(z.read(style_name))
        st = doc.find("Style")
        return st, doc
    sc = mscx_root.find("Score")
    st = sc.find("Style")
    if st is None:
        st = etree.Element("Style")
        sc.insert(0, st)
    return st, None


def _set_style(style, name, value) -> None:
    e = style.find(name)
    if e is None:
        e = etree.SubElement(style, name)
    e.text = str(value)


def _prepare(src: Path, work: Path, exe: str, starts, one_line: bool, from_xml: bool, spacers=None,
             first_hides: bool = False) -> Path:
    """The .mscz to lay out: the score with the asked line breaks (and, for a score read from MusicXML, empty
    staves of three-staff instruments hidden)."""
    if starts is None and not one_line and not from_xml and not spacers and not first_hides:
        return src                            # the score as its author laid it out
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n) for n in z.namelist()}
        mscx_name, style_name = _score_files(z)
        mscx = etree.fromstring(files[mscx_name], etree.XMLParser(huge_tree=True))
        style, style_doc = _style_root(z, style_name, mscx)
    score_el = mscx.find("Score")
    if starts is not None or one_line:
        # one line made of several systems: each ends as a section, so that it keeps its natural width
        _set_breaks(score_el, starts or [], one_line and not starts, "section" if one_line and starts else "line")
    if one_line:      # frames inside the music (text, pictures) start a new line or leave a gap in it: one line has
        for staff in score_el.findall("Staff"):           # none (those before the first measure, the title, stay)
            started = False
            for child in list(staff):
                if child.tag == "Measure":
                    started = True
                elif started and child.tag in ("VBox", "HBox", "TBox", "FBox"):
                    staff.remove(child)
    if one_line:      # a page wide enough for the whole score on one line, and the line not stretched to fill it
        _set_style(style, "pageWidth", ONE_LINE_PAGE)    # (MuseScore stretches a line but a section's last, and
        _set_style(style, "pagePrintableWidth", ONE_LINE_PAGE - 1)    # that one too when it fills more than
        _set_style(style, "lastSystemFillLimit", 1)      # lastSystemFillLimit of the page: a long piece does)
    if one_line and starts:   # systems put side by side: the standard distance between staves, not spread to a page
        _set_style(style, "enableVerticalSpread", 0)
    if first_hides or (one_line and starts):     # every system hides its empty staves, the first one too (each
        _set_style(style, "dontHideStavesInFirstSystem", 0)   # system side by side starts a section of its own)
    if spacers:               # {(measure index, staff number): staff spaces below that staff}
        staves = score_el.findall("Staff")
        for (mi, sn), space in spacers.items():
            if 1 <= sn <= len(staves):
                ms = staves[sn - 1].findall("Measure")
                if 0 <= mi < len(ms):
                    sp_el = etree.Element("vspacerFixed")   # exactly this far to the next staff
                    sp_el.text = f"{space:.2f}"
                    eid = ms[mi].find("eid")
                    (eid.addnext(sp_el) if eid is not None else ms[mi].insert(0, sp_el))
    if from_xml:
        hide = False
        for part in score_el.findall("Part"):
            if len(part.findall("Staff")) >= 3:
                e = part.find("hideStavesWhenIndividuallyEmpty")
                if e is None:
                    e = etree.Element("hideStavesWhenIndividuallyEmpty")
                    part.insert(0, e)
                e.text = "1"
                hide = True
        if hide or len(score_el.findall("Part")) > 1:     # parts of one instrument: a part that rests goes
            _set_style(style, "hideEmptyStaves", 1)
            _set_style(style, "dontHideStavesInFirstSystem", 0)
    files[mscx_name] = etree.tostring(mscx, xml_declaration=True, encoding="UTF-8")
    if style_doc is not None:
        files[style_name] = etree.tostring(style_doc, xml_declaration=True, encoding="UTF-8")
    out = work / "score.mscz"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return out


ONE_LINE_PAGE = 20000     # inches: the page a whole score is laid out on in one line

OUTPUTS = ["score.svg", "score.mpos", "score.spos", "score.mid", "score.musicxml"]


def _xml_layout(root, starts, one_line: bool) -> None:
    """Line breaks (and, for one line, a page wide enough) written into a MusicXML score."""
    if starts is None and not one_line:
        return
    wanted = set(starts or []) if starts else set()     # one line made of several systems keeps its breaks
    for part in root.findall("part"):
        for i, m in enumerate(part.findall("measure")):
            for p in m.findall("print"):
                for att in ("new-system", "new-page"):
                    p.attrib.pop(att, None)
            if i in wanted and i:
                p = m.find("print")
                if p is None:
                    p = etree.Element("print")
                    m.insert(0, p)
                p.set("new-system", "yes")
    if one_line:
        d = root.find("defaults")
        if d is None:
            d = etree.Element("defaults")
            root.insert(1, d)
        pl = d.find("page-layout")
        if pl is None:
            pl = etree.SubElement(d, "page-layout")
            etree.SubElement(pl, "page-height").text = "1683"
        w = pl.find("page-width")
        if w is None:
            w = etree.SubElement(pl, "page-width")
        w.text = "200000"


def _many_staves(root) -> bool:
    """An instrument on three staves or more (one part, or parts of the same name): staves that only rest for a
    line are left out of it."""
    parts = root.findall("part")
    staves = [max([int(t) for t in p.xpath(".//attributes/staves/text()") if t.strip().isdigit()] or [1]) for p in parts]
    names = {(sp.findtext("part-name") or "").strip().lower() for sp in root.findall("part-list/score-part")}
    return max(staves or [1]) >= 3 or (len(parts) > 1 and len(names) == 1 and sum(staves) >= 3)


def _lay_out(exe, score: Path, out: Path):
    job = out.parent / (out.name + ".job.json")
    job.write_text(json.dumps([{"in": str(score), "out": [str(out / n) for n in OUTPUTS]}]), encoding="utf8")
    ok = lambda: bool(list(out.glob("score*.svg"))) and (out / "score.spos").exists()          # noqa: E731
    code, err = _run(exe, ["-f", "-j", str(job)], done=ok)        # -f: a score MuseScore finds "corrupted" (a
                                                                    # measure that does not add up) is still laid out
    job.unlink(missing_ok=True)
    return ok(), err


def export(path, starts=None, one_line=False, progress=None, spacers=None, first_hides=False) -> Path:
    """Lay the score out with MuseScore and export it; returns the folder holding score-<page>.svg, score.mpos,
    score.spos, score.mid and score.musicxml (cached).

    A .mscz/.mscx is laid out as it is (with the asked line breaks written into it).  A MusicXML file is tidied
    (xmlfix.clean), its line breaks written into it, and laid out directly; an instrument on three staves or more
    goes through a .mscz first, to hide the staves that only rest for a line (MusicXML cannot say that) -- and if
    MuseScore cannot open the score it made, the MusicXML is laid out as it is."""
    say = progress or (lambda *_: None)
    exe = find_musescore()
    if exe is None:
        raise ValueError("MuseScore is not installed (or not found: set the MUSESCORE environment variable to it).")
    path = Path(path)
    key = hashlib.sha1(path.read_bytes() + json.dumps([VERSION, starts, one_line, exe, os.path.getmtime(exe),
                                                       sorted((list(k), v) for k, v in (spacers or {}).items()),
                                                       first_hides]).encode()).hexdigest()[:20]
    out = _cache_dir() / key
    if (out / "done").exists():
        return out
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix="sma_ms_"))
    try:
        suffix = path.suffix.lower()
        ok, err = False, ""
        if suffix in (".mscz", ".mscx"):
            src = path
            if suffix == ".mscx":
                src = work / "in.mscz"
                with zipfile.ZipFile(src, "w") as z:
                    z.writestr("score.mscx", path.read_bytes())
                    z.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
                               '<rootfile full-path="score.mscx"/></rootfiles></container>')
            say("Laying the score out with MuseScore…")
            ok, err = _lay_out(exe, _prepare(src, work, exe, starts, one_line, False, spacers, first_hides), out)
        else:
            root = _read_musicxml(path)
            if root is None:
                raise ValueError(f"Could not read {path}")
            xmlfix.clean(root)
            _xml_layout(root, starts, one_line)
            xml = work / "in.musicxml"
            xml.write_bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8"))
            if _many_staves(root) or one_line:     # (styles are only set in a .mscz)
                say("Reading the MusicXML with MuseScore…")
                src = work / "in.mscz"
                _run(exe, ["-f", "-o", str(src), str(xml)], done=src.exists)
                if src.exists():
                    say("Laying the score out with MuseScore…")
                    ok, err = _lay_out(exe, _prepare(src, work, exe, starts if one_line else None, one_line,
                                                     True, spacers, first_hides), out)
            if not ok:
                say("Laying the score out with MuseScore…")
                for f in out.iterdir():
                    f.unlink()
                ok, err = _lay_out(exe, xml, out)
        if not ok:
            raise ValueError(f"MuseScore could not lay out {path.name}.\n{err}")
        (out / "done").write_text("ok")
        return out
    except Exception:
        shutil.rmtree(out, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _pages(folder: Path) -> list[Path]:
    pages = list(folder.glob("score-*.svg")) or list(folder.glob("score.svg"))
    return sorted(pages, key=lambda p: int(re.findall(r"(\d+)\.svg$", p.name)[0]) if re.findall(r"-(\d+)\.svg$", p.name) else 0)


# ------------------------------------------------------------------------------------------- positions
def _positions(path: Path):
    """({id: (page, x0, y0, x1, y1)} in SVG units, [(ms, id)] sorted)."""
    root = etree.parse(str(path)).getroot()
    boxes = {}
    for e in root.iter("element"):
        x, y, sx, sy = (float(e.get(k)) / POS_SCALE for k in ("x", "y", "sx", "sy"))
        boxes[e.get("id")] = (int(e.get("page", 0)), x, y, x + sx, y + sy)
    events = sorted((int(ev.get("position")), ev.get("elid")) for ev in root.iter("event"))
    return boxes, events


def _first_times(events) -> dict:
    out = {}
    for ms, eid in events:
        out.setdefault(eid, ms / 1000.0)
    return out


# ------------------------------------------------------------------------------------------- building
class _El:
    __slots__ = ("cls", "kind", "xml", "box", "page", "system", "measure", "time", "end", "static", "mid",
                 "steps", "heads", "label", "grace")

    def __init__(self, cls, xml, box, page):
        self.cls, self.kind, self.xml, self.box, self.page = cls, KINDS.get(cls, cls.lower() or None), xml, box, page
        self.system = self.measure = -1
        self.time = self.end = None
        self.static = self.mid = self.grace = False
        self.steps, self.heads, self.label = (), (), ""


def _read_pages(pages):
    """The elements of every page (in drawing order) with their boxes in SVG units; consecutive paths of one class
    that belong together are merged."""
    calc = BoxCalculator({})
    out, sizes = [], []
    for pi, p in enumerate(pages):
        root = etree.parse(str(p), etree.XMLParser(huge_tree=True)).getroot()
        vb = [float(v) for v in (root.get("viewBox") or "0 0 0 0").split()]
        sizes.append((vb[2], vb[3]))
        for e in root:
            if not isinstance(e.tag, str) or e.tag.split("}")[-1] in ("title", "desc", "defs"):
                continue
            cls = e.get("class") or ""
            if cls == "" and (e.get("fill") or "").lower() in ("#ffffff", "white"):
                continue                       # the page itself
            box = calc.box(e)
            if box is None:
                continue
            out.append(_El(cls, e, box, pi))
    return out, sizes


def _merge(els, sp):
    """Consecutive paths of a class in MERGE that touch (within MERGE_GAP staff spaces) become one element."""
    out = []
    gap = MERGE_GAP * sp
    for e in els:
        last = out[-1] if out else None
        if (last is not None and e.cls in MERGE and last.cls == e.cls and last.page == e.page
                and e.box[0] <= last.box[2] + gap and last.box[0] <= e.box[2] + gap
                and e.box[1] <= last.box[3] + gap and last.box[1] <= e.box[3] + gap):
            last.xml = last.xml if isinstance(last.xml, list) else [last.xml]
            last.xml.append(e.xml)
            last.box = (min(last.box[0], e.box[0]), min(last.box[1], e.box[1]),
                        max(last.box[2], e.box[2]), max(last.box[3], e.box[3]))
            continue
        out.append(e)
    return out


def _staff_space(els) -> float:
    ys = sorted({round(float(e.xml.get("points", "0,0").split()[0].split(",")[1]), 2)
                 for e in els if e.cls == "StaffLines" and e.page == 0 and e.xml.get("points")})
    gaps = sorted(b - a for a, b in zip(ys, ys[1:]) if b - a > 1)
    return gaps[len(gaps) // 4] if gaps else 83.3          # the small gaps: between the lines of a staff


def _staves(els):
    """{(page, system): [(top, bottom) of every staff]} from the staff lines."""
    lines: dict = {}
    for e in els:
        if e.cls == "StaffLines":
            lines.setdefault((e.page, e.system), []).append((e.box[1] + e.box[3]) / 2)
    out = {}
    for k, ys in lines.items():
        ys.sort()
        staves = [ys[i:i + 5] for i in range(0, len(ys), 5)]
        out[k] = [(s[0], s[-1]) for s in staves]
    return out


def engrave_musescore(path, ink: str = "#000000", progress=None, measures_per_line: int | None = None,
                      line_starts: list[int] | None = None) -> Score:
    """Engrave a score (.mscz, .mscx or MusicXML) with MuseScore.  Without measures_per_line / line_starts the
    score's own line breaks are kept; 0 puts the whole score on one line."""
    say = progress or (lambda *_: None)
    one_line = (line_starts is not None and len(line_starts) <= 1) or measures_per_line == 0
    starts = None
    if not one_line:
        if line_starts is not None:
            starts = sorted(set(line_starts))
        elif measures_per_line and measures_per_line > 0:
            starts = "every"
    if starts == "every":
        n = _count_measures(path)
        starts = list(range(0, n, measures_per_line))
    runs = _visibility_runs(path, say) if one_line else None
    if runs:          # one line of several systems: a new one wherever the printed score shows other staves
        folder = export(path, runs, True, say)
        spacers = _even_staves(folder)
        if spacers:   # ...and the same distance between two staves in every one of them
            folder = export(path, runs, True, say, spacers)
        say("Splitting layers…")
        rows = _system_rows(folder / "score.mpos")
        shown = {r[3][0]: sh for r, sh in zip(rows, _shown_staves(folder, rows))}
        return _build(folder, ink, Path(path).stem, side_by_side=True, shown=shown)
    folder = export(path, starts, one_line, say)
    say("Splitting layers…")
    return _build(folder, ink, Path(path).stem)


def _system_rows(mpos: Path):
    """The lines of a layout: [(page, top, bottom, [measure indices])] in the order they are played."""
    boxes, events = _positions(mpos)
    times = _first_times(events)
    measures = sorted(((b, times[i]) for i, b in boxes.items() if i in times), key=lambda m: m[1])
    rows: dict = {}
    for idx, (b, _) in enumerate(measures):
        rows.setdefault((b[0], round(b[2], 1), round(b[4], 1)), []).append(idx)
    return sorted(((p, t, bt, ms) for (p, t, bt), ms in rows.items()), key=lambda r: r[3][0])


def _shown_staves(folder: Path, rows) -> list:
    """For every line of a layout, the numbers (1 = top) of the staves MuseScore draws -- it drew so many; which ones
    follows its rule: the staves with written notes in the line's measures, then (when it drew more) the other staves
    of the parts that play (a part hides only whole unless told otherwise), then the first ones (a line of rests keeps
    a staff).  None for a line with more staves playing than it drew."""
    root = _read_musicxml(folder / "score.musicxml")
    if root is None:
        return [None] * len(rows)
    written: dict = {}                                   # measure index -> staves with notes
    part_of: dict = {}                                   # staff number -> part index
    parts_seen: list = []
    total, base = 0, 0
    for part in root.findall("part"):
        n = max([int(t) for t in part.xpath(".//attributes/staves/text()") if t.strip().isdigit()] or [1])
        part_of.update({base + i: len(parts_seen) for i in range(1, n + 1)})
        parts_seen.append(part)
        for mi, m in enumerate(part.findall("measure")):
            for note in m.iter("note"):
                if note.find("rest") is None and note.get("print-object") != "no":
                    written.setdefault(mi, set()).add(base + int(note.findtext("staff") or 1))
        base += n
    total = base
    lines: dict = {}                                     # (page, row) -> staff line count
    for pi, page in enumerate(_pages(folder)):
        for m in re.finditer(r'class="StaffLines"[^>]*points="[\d.e+-]+,([\d.e+-]+)', page.read_text(encoding="utf8")):
            y = float(m.group(1))
            for ri, (p, top, bottom, _) in enumerate(rows):
                if p == pi and top - 2 <= y <= bottom + 2:
                    lines[ri] = lines.get(ri, 0) + 1
    out = []
    for ri, (_, _, _, ms) in enumerate(rows):
        drawn = lines.get(ri, 0) // 5
        used = set().union(*[written.get(m, set()) for m in ms])
        if drawn == total:
            out.append(tuple(range(1, total + 1)))
        elif len(used) <= drawn:
            playing = {part_of.get(n) for n in used}
            rest = sorted(set(range(1, total + 1)) - used, key=lambda n: (part_of.get(n) not in playing, n))
            out.append(tuple(sorted(used | set(rest[:drawn - len(used)]))))
        else:
            out.append(None)
    return out


def _staff_spans(folder: Path, rows) -> list:
    """For every line of a layout, (top, bottom) of each staff it draws, top to bottom (SVG units)."""
    out = [[] for _ in rows]
    ys = [[] for _ in rows]
    for pi, page in enumerate(_pages(folder)):
        for m in re.finditer(r'class="StaffLines"[^>]*points="[\d.e+-]+,([\d.e+-]+)', page.read_text(encoding="utf8")):
            y = float(m.group(1))
            for ri, (p, top, bottom, _) in enumerate(rows):
                if p == pi and top - 2 <= y <= bottom + 2:
                    ys[ri].append(y)
    for ri, v in enumerate(ys):
        v.sort()
        out[ri] = [(v[i], v[i + 4]) for i in range(0, len(v) - 4, 5)]
    return out


def _even_staves(folder: Path) -> dict:
    """Fixed spacers that put every staff at one height in all the lines of a layout: two neighbouring staves
    get the largest distance MuseScore needed between them in any line (so nothing collides), and two staves with
    hidden ones between them the room those would take.  {(first measure of a line, staff number): staff spaces
    from that staff down to the next one shown}."""
    rows = _system_rows(folder / "score.mpos")
    shown = _shown_staves(folder, rows)
    spans = _staff_spans(folder, rows)
    sp = height = None
    widest: dict = {}                                     # staff n -> largest gap down to staff n + 1
    pairs = []                                            # (row, upper staff, lower staff, gap)
    for ri, (sh, st) in enumerate(zip(shown, spans)):
        if sh is None or len(sh) != len(st) or not st:
            continue
        if sp is None:
            height = st[0][1] - st[0][0]
            sp = height / 4
        for (a, sa), (b, sb) in zip(zip(sh, st), zip(sh[1:], st[1:])):
            pairs.append((ri, a, b, sb[0] - sa[1]))
            if b == a + 1:
                widest[a] = max(widest.get(a, 0.0), sb[0] - sa[1])
    if not sp:
        return {}
    out, uneven = {}, False
    for ri, a, b, g in pairs:
        if any(n not in widest for n in range(a, b)):
            continue
        target = sum(widest[n] for n in range(a, b)) + (b - a - 1) * height    # through the hidden staves
        uneven = uneven or abs(target - g) > 0.05 * sp
        out[(rows[ri][3][0], a)] = round(target / sp, 2)
    return out if uneven else {}


def _visibility_runs(path, say):
    """Where a score laid out on one line has to start a new system: the first measure of every stretch of the
    printed score (its own lines) that shows a different set of staves.  None when the staves never change."""
    printed = export(path, None, False, say, first_hides=True)     # (the rule the systems side by side follow)
    rows = _system_rows(printed / "score.mpos")
    shown = _shown_staves(printed, rows)
    starts, last = [], object()
    for (_, _, _, ms), sh in zip(rows, shown):
        key = sh if sh is not None else ("?", len(starts))
        if key != last:
            starts.append(ms[0])
            last = key
    return starts if len(starts) > 1 else None


def _count_measures(path) -> int:
    p = Path(path)
    if p.suffix.lower() in (".mscz", ".mscx"):
        data = p.read_bytes()
        if p.suffix.lower() == ".mscz":
            with zipfile.ZipFile(p) as z:
                data = z.read(_score_files(z)[0])
        root = etree.fromstring(data, etree.XMLParser(huge_tree=True))
        return len(_first_staff_measures(root.find("Score")))
    root = _read_musicxml(p)
    return len(root.find("part").findall("measure")) if root is not None else 0


def _build(folder: Path, ink: str, title: str, side_by_side: bool = False, shown: dict | None = None) -> Score:
    """The Score of an export.  With `side_by_side` its lines are put one after the other on one line, their
    staves level (`shown`: the staff numbers each line shows, by its first measure): a line of several systems."""
    pages = _pages(folder)
    els, sizes = _read_pages(pages)
    sp = _staff_space(els)
    k = STAFF_SPACE / sp                                     # SVG units -> page units
    offsets, y = [], 0.0                                     # pages stacked top to bottom
    for w, h in sizes:
        offsets.append(y)
        y += h
    els = _merge(els, sp)
    mboxes, mevents = _positions(folder / "score.mpos")
    sboxes, sevents = _positions(folder / "score.spos")
    mtime, stime = _first_times(mevents), _first_times(sevents)

    # lines: the rows of measure boxes
    measures = sorted(((b, mtime.get(i)) for i, b in mboxes.items() if i in mtime), key=lambda m: m[1])
    rows = sorted({(b[0], round(b[2], 1), round(b[4], 1)) for b, _ in measures})
    sysidx = {r: i for i, r in enumerate(rows)}
    msys = [sysidx[(b[0], round(b[2], 1), round(b[4], 1))] for b, _ in measures]
    systems = [(r[0], r[1], r[2]) for r in rows]             # (page, top, bottom) in SVG units
    # segments of every line: (x0, x1, time)
    segs: dict = {}
    for i, b in sboxes.items():
        if i not in stime:
            continue
        si = min(range(len(systems)), key=lambda s: (systems[s][0] != b[0], abs(systems[s][1] - b[2])))
        segs.setdefault(si, []).append((b[1], b[3], stime[i]))
    for v in segs.values():
        v.sort()

    def system_of(e):
        cy = (e.box[1] + e.box[3]) / 2
        cands = [s for s in range(len(systems)) if systems[s][0] == e.page]
        if not cands:
            return -1, 1e9
        s = min(cands, key=lambda s: max(systems[s][1] - cy, cy - systems[s][2], 0))
        return s, max(systems[s][1] - cy, cy - systems[s][2], 0)

    for e in els:
        e.system, dist = system_of(e)
        if e.kind is None or e.system < 0 or (e.kind in ("text", "label") and dist > 6 * sp) or dist > 14 * sp:
            e.static = True                                  # titles, page numbers, credits: part of no line
            e.kind = e.kind or "text"
            e.system = -1
            continue
        if e.kind in ALWAYS_STATIC or e.kind == "text" and dist > 6 * sp:
            e.static = True
        s = segs.get(e.system)
        first = s[0][0] if s else 1e18
        last = s[-1][1] if s else -1e18
        if e.kind in LINE_START:
            cx = (e.box[0] + e.box[2]) / 2
            if cx < first or cx > last:
                e.static = True                              # at the start of the line, or a courtesy sign at its end
            else:
                e.mid = True

    # measures: their boxes, times and lines
    infos = []
    for idx, ((b, t), si) in enumerate(zip(measures, msys)):
        x0, y0, x1, y1 = b[1] * k, b[2] * k + offsets[b[0]] * k, b[3] * k, b[4] * k + offsets[b[0]] * k
        nxt = measures[idx + 1][1] if idx + 1 < len(measures) else None
        infos.append(MeasureInfo(idx, si, (x0, y0, x1 - x0, y1 - y0), t, t if nxt is None else max(nxt, t)))
    by_sys_meas: dict = {}
    for m, (b, _) in zip(infos, measures):
        by_sys_meas.setdefault(m.system, []).append((b[1], b[3], m.index))

    def measure_of(e):
        ms = by_sys_meas.get(e.system, [])
        if not ms:
            return -1
        cx = (e.box[0] + e.box[2]) / 2
        return min(ms, key=lambda m: 0 if m[0] <= cx <= m[1] else min(abs(cx - m[0]), abs(cx - m[1])))[2]

    def time_at(si, x, before=False):
        """Time of the segment of line si at x: the one it stands in (or the last one left of it); `before`: the
        next one to the right (an accidental, an arpeggio, a grace note stands before its note)."""
        s = segs.get(si)
        if not s:
            return None
        x0s = [g[0] for g in s]
        i = bisect_right(x0s, x) - 1
        if before:
            if i < 0 or not (s[i][0] <= x <= s[i][1]):
                i = min(i + 1, len(s) - 1)
        return s[max(i, 0)][2]

    notes = [e for e in els if e.cls == "Note"]
    scales = sorted(abs(_scale_of(e.xml)) for e in notes) or [1.0]
    normal = scales[len(scales) // 2]
    for e in els:
        e.measure = measure_of(e)
        if e.static:
            continue
        x0, _, x1, _ = e.box
        cx = (x0 + x1) / 2
        if e.kind in ("note", "rest"):
            e.grace = e.kind == "note" and abs(_scale_of(e.xml)) < 0.85 * normal
            e.time = time_at(e.system, cx, before=e.grace)
            e.end = e.time
        elif e.kind in BEFORE or e.mid:
            e.time = e.end = time_at(e.system, x1 if e.kind in BEFORE else cx, before=True)
        elif e.kind in SPANNING or e.kind in WIPE_KINDS:
            t0 = time_at(e.system, x0 + 0.6 * sp, before=True)
            t1 = time_at(e.system, x1 - 0.6 * sp)
            e.time, e.end = t0, max(t1 if t1 is not None else t0, t0) if t0 is not None else None
            if e.time is not None and e.end - e.time > 0.12:
                e.steps = _steps(segs.get(e.system, []), x0, x1)
        elif e.kind in ("dynam", "tempo", "reh", "text"):
            e.time = e.end = time_at(e.system, min(x0 + 0.8 * sp, cx), before=True)
        else:
            e.time = e.end = time_at(e.system, cx)
        if e.time is None:
            e.static = True

    # where every element goes on the canvas: (dx, dy) in page units for each (page, line)
    k_page = [o * k for o in offsets]
    shift = {}
    if side_by_side:
        staves = _staves(els)
        order = sorted(range(len(systems)), key=lambda si: min(m.index for m in infos if m.system == si))
        ext = {}
        for si in order:
            mine = [e for e in els if e.system == si and not (e.static and e.kind == "text")]
            page = systems[si][0]
            st = staves.get((page, si)) or [(systems[si][1], systems[si][2])]
            ms = [m for m in by_sys_meas.get(si, [])]
            first = min(m[2] for m in ms)
            sh = (shown or {}).get(first)
            ext[si] = (min(e.box[0] for e in mine), max(m[1] for m in ms), min(e.box[1] for e in mine),
                       max(e.box[3] for e in mine), st[-1][1], page,
                       sh if sh is not None and len(sh) == len(st) else None, st)
        # Every staff has one height on the line: the staves a system shows sit where they sit in the others (its
        # distance to the staff below is the same everywhere: `_even_staves`).  below[n]: how far the bottom of
        # staff n lies above the bottom of the score's lowest staff.
        last = max((x[6][-1] for x in ext.values() if x[6]), default=None)
        gap: dict = {}
        for x in ext.values():
            if x[6]:
                for (a, sa), (b, sb) in zip(zip(x[6], x[7]), zip(x[6][1:], x[7][1:])):
                    if b == a + 1:
                        gap[a] = max(gap.get(a, 0.0), sb[1] - sa[1])
        below = {}
        if last is not None:
            below[last] = 0.0
            for n in range(last - 1, 0, -1):
                below[n] = below[n + 1] + gap.get(n, 0.0)

        def raised(x):             # how far above the line's lowest staves this system's lowest staff sits (SVG)
            return below.get(x[6][-1], 0.0) if x[6] else 0.0
        level = max((x[4] - x[2] + raised(x)) * k for x in ext.values()) + 6 * STAFF_SPACE
        cursor = 4 * STAFF_SPACE
        for si in order:
            x0, x1, top, bottom, low, page, _, _ = ext[si]
            shift[si] = (cursor - x0 * k, level - (low + raised(ext[si])) * k)
            cursor += (x1 - x0) * k + 2 * STAFF_SPACE        # the next system starts right after the barline
    first_si = min(range(len(systems)), key=lambda si: min(m.index for m in infos if m.system == si)) if systems else 0
    first_page = systems[first_si][0] if systems else 0

    def where(page, si):
        if side_by_side:      # (the title above the first system goes with it)
            return shift[si] if si in shift else shift.get(first_si, (0.0, 0.0))
        return 0.0, k_page[page]
    if side_by_side:          # the page around the music (titles, page numbers) does not belong to one line
        els = [e for e in els if e.system >= 0 or e.page == first_page]
        for e in els:
            if e.system < 0:
                e.static = True
        infos = [MeasureInfo(m.index, m.system, (m.rect[0] + where(0, m.system)[0],
                                                 m.rect[1] - k_page[measures[m.index][0][0]] + where(0, m.system)[1],
                                                 m.rect[2], m.rect[3]), m.time, m.end) for m in infos]

    data = (folder / "score.mid").read_bytes()
    midi = read_midi(data, velocity=True, restrike=True) or []        # the sound: one key per pitch
    _pitches(els, read_midi(data) or [], _staves(els))                # every voice's note, for its notehead
    _labels(els, folder / "score.musicxml", infos)

    score = Score()
    score.width = max(w for w, _ in sizes) * k
    score.height = y * k
    score.title = title
    units = []
    for e in els:
        if e.static:
            continue
        dx, dy = where(e.page, e.system)
        rect = _rect(e.box, k, dx, dy, 30)
        wipe = e.kind in WIPE_KINDS and e.end - e.time > 0.12
        steps = tuple((-1, t, (min(max((x * k + dx - rect[0]) / max(rect[2], 1e-6), 0.0), 1.0) if f < 1 else 1.0))
                      for t, x, f in e.steps) if e.steps and (e.kind in LINE_KINDS or e.kind in ("beam", "tuplet", "fTrem")) else ()
        heads = tuple((h[0] * k + dx, h[1] * k + dy, h[2] * k + dx, h[3] * k + dy, h[4], h[5], h[6])
                      for h in e.heads)
        units.append(Unit(uid=len(units), kind=e.kind, svg=_doc(e.xml, rect, k, dx, dy, ink), rect=rect,
                          time=e.time, end=e.end, system=0 if side_by_side else e.system, wipe=wipe, static=False,
                          measure=e.measure, steps=steps, heads=heads, label=e.label))
    score.units = units
    # static layers: every line (and what lies outside the lines: titles, credits) per page
    groups: dict = {}
    for e in els:
        if e.static:
            groups.setdefault((e.page, e.system if e.system >= 0 else -1), []).append(e)
    for (page, si), group in sorted(groups.items(), key=lambda g: (g[0][0], g[0][1])):
        box = (min(e.box[0] for e in group), min(e.box[1] for e in group),
               max(e.box[2] for e in group), max(e.box[3] for e in group))
        dx, dy = where(page, si)
        rect = _rect(box, k, dx, dy, 60)
        score.layers.append(StaticLayer(_doc([x for e in group for x in (e.xml if isinstance(e.xml, list) else [e.xml])],
                                             rect, k, dx, dy, ink), rect))
    # the canvas ends where the music does (a page made wide for one line is mostly empty)
    right = max([m.rect[0] + m.rect[2] for m in infos] + [u.rect[0] + u.rect[2] for u in units] or [score.width])
    score.width = min(score.width, right + 4 * STAFF_SPACE)
    if side_by_side:          # one line: every system of it is part of line 0
        score.width = right + 4 * STAFF_SPACE
        score.height = max([layer.rect[1] + layer.rect[3] for layer in score.layers] +
                           [u.rect[1] + u.rect[3] for u in units]) + 4 * STAFF_SPACE
        infos = [MeasureInfo(m.index, 0, m.rect, m.time, m.end) for m in infos]
    score.measure_infos = infos
    score.measures = [(m.time, i + 1) for i, m in enumerate(infos)]
    score.line_starts = [next(m.index for m in infos if m.system == si) for si in sorted({m.system for m in infos})]
    for si in ([0] if side_by_side else range(len(systems))):
        mine = [u for u in units if u.system == si]
        ms = [m for m in infos if m.system == si]
        rects = [u.rect for u in mine] + [m.rect for m in ms] + \
                [layer.rect for (p, s), layer in zip(sorted(groups, key=lambda g: (g[0], g[1])), score.layers)
                 if s == si or (side_by_side and s >= 0)]
        if not rects:
            continue
        x0 = min(r[0] for r in rects)
        y0 = min(r[1] for r in rects)
        x1 = max(r[0] + r[2] for r in rects)
        y1 = max(r[1] + r[3] for r in rects)
        ts = [u.time for u in mine] or [ms[0].time if ms else 0.0]
        ends = [u.end for u in mine if u.kind in NOTE_KINDS]
        score.systems.append(System((x0, y0, x1 - x0, y1 - y0), min(ts), max(ends + ts)))
    full = sorted((tuple(n) for n in midi), key=lambda n: n[1])
    score.rolls = _rolls(els, full)
    score.notes = _written(full, units, score.rolls)
    score.duration = max([n[2] for n in score.notes] + [u.end for u in units] + [0.0])
    return score


def _scale_of(el) -> float:
    m = re.match(r"\s*matrix\(([-\d.e]+)", (el if not isinstance(el, list) else el[0]).get("transform") or "")
    return float(m.group(1)) if m else 1.0


def _steps(segs, x0, x1):
    """(time, x, fraction flag) of the segments under a line: it grows to each as it is played."""
    out = [(t, s1, 0.0) for s0, s1, t in segs if x0 - 1 <= s0 <= x1]
    out = sorted(set(out))
    if len(out) < 2:
        return ()
    out[-1] = (out[-1][0], out[-1][1], 1.0)
    return out


def _rect(box, k, dx, dy, pad):
    x0, y0, x1, y1 = box
    return (x0 * k + dx - pad, y0 * k + dy - pad, (x1 - x0) * k + 2 * pad, (y1 - y0) * k + 2 * pad)


_BLACK = re.compile(rb'(fill|stroke)="#000000"')


def _doc(xml, rect, k, dx, dy, ink) -> bytes:
    """A small SVG of the element(s), in page units, black drawn in the ink colour."""
    parts = xml if isinstance(xml, list) else [xml]
    body = b"".join(_BLACK.sub(rb'\1="currentColor"', etree.tostring(p, with_tail=False)) for p in parts)
    x, y, w, h = rect
    return (f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}" '
            f'width="{w:.2f}" height="{h:.2f}"><g color="{ink}" fill="{ink}">'
            f'<g transform="translate({dx:.2f},{dy:.2f}) scale({k:.6f})">').encode() + body + b"</g></g></svg>"


# ------------------------------------------------------------------------------------------- pitches, labels
def _pitches(els, midi, staves) -> None:
    """Every notehead gets its staff (index within its line) and a pitch: the notes the MIDI starts at its time,
    matched top to bottom with the heads of that moment (a head with no note starting is a tied one)."""
    onsets: dict = {}
    for p, s, *_ in midi:
        onsets.setdefault(round(s, 3), []).append(p)
    times = sorted(onsets)
    heads_at: dict = {}
    for e in els:
        if e.kind == "note" and e.time is not None:
            heads_at.setdefault((e.system, round(e.time, 3)), []).append(e)
    for (si, t), hs in heads_at.items():
        i = bisect_right(times, t + 0.004) - 1
        ps = sorted(onsets[times[i]], reverse=True) if i >= 0 and abs(times[i] - t) <= 0.004 else []
        hs.sort(key=lambda e: (e.box[1] + e.box[3]) / 2)
        st = staves.get((hs[0].page, si), [])
        for j, e in enumerate(hs):
            cy = (e.box[1] + e.box[3]) / 2
            staff = min(range(len(st)), key=lambda q: abs(cy - (st[q][0] + st[q][1]) / 2)) if st else 0
            tied = j >= len(ps)
            pitch = ps[min(j, len(ps) - 1)] if ps else 60
            e.heads = ((*e.box, staff, tied, pitch),)


_DYN = re.compile(r"^(p+|m?[pf]|f+|s?f+z?p?|sfp+|fp|rf?z?|n)$")


def _labels(els, xml_path: Path, infos) -> None:
    """What the dynamics say ("ff", "sfz"), from the MusicXML MuseScore exported with the layout: matched in order
    within each measure.  Accents are told by their shape."""
    try:
        root = _read_musicxml(xml_path)
    except Exception:  # noqa: BLE001
        root = None
    if root is not None:
        written: dict = {}
        for part in root.findall("part"):
            for mi, m in enumerate(part.findall("measure")):
                pos = 0
                for e in m:
                    if e.tag == "backup":
                        pos -= int(e.findtext("duration") or 0)
                    elif e.tag == "forward":
                        pos += int(e.findtext("duration") or 0)
                    elif e.tag == "note" and e.find("chord") is None and e.find("grace") is None:
                        pos += int(e.findtext("duration") or 0)
                    elif e.tag == "direction":
                        for d in e.iter("dynamics"):
                            text = "".join(c.tag if c.tag != "other-dynamics" else (c.text or "") for c in d).strip()
                            if text:
                                written.setdefault(mi, []).append((pos, int(e.findtext("staff") or 1), text))
        drawn: dict = {}
        for e in els:
            if e.kind == "dynam" and not e.static and e.measure >= 0:
                drawn.setdefault(e.measure, []).append(e)
        for mi, ds in drawn.items():
            ws = sorted(written.get(mi, []))
            ds.sort(key=lambda e: (e.time, e.box[1]))
            if len(ws) == len(ds):
                for e, w in zip(ds, ws):
                    e.label = w[2]
    sp = _staff_space(els)
    for e in els:
        if e.kind == "artic" and not e.static:
            w, h = (e.box[2] - e.box[0]) / sp, (e.box[3] - e.box[1]) / sp
            e.label = "acc" if w > 0.9 and 0.45 < h < 1.2 else "marc" if h >= 1.0 and w < 1.2 else \
                "stacc" if w < 0.5 and h < 0.5 else "ten" if h < 0.3 else ""


WRITTEN_BEFORE, WRITTEN_AFTER = 0.02, 0.25   # a played note belongs to a notehead starting this close (s)


def _written(midi, units, rolls=()) -> list[tuple]:
    """The notes of the playback that are written: a notehead of the same pitch starts with it (or just after it:
    a grace note is played before the beat).  MuseScore also plays what it does not show -- trills, tremolos and
    turns played out note by note, notes the score hides -- and no performer plays those the same way: the
    alignment snaps every note to an attack of its pitch, so it follows the written notes only.  The notes of a
    rolled chord (`rolls`) start one after the other from the chord's moment and are all written."""
    rolled = {t for r in rolls for t in r}
    heads: dict = {}
    for u in units:
        for h in u.heads:
            heads.setdefault(h[6], []).append(u.time)
    for v in heads.values():
        v.sort()
    out = []
    for n in midi:
        ts = heads.get(n[0], [])
        i = bisect_right(ts, n[1] - WRITTEN_BEFORE)
        if (i < len(ts) and ts[i] - n[1] <= WRITTEN_AFTER) or round(n[1], 4) in rolled:
            out.append(tuple(n))
    return sorted(out, key=lambda n: n[1]) or sorted((tuple(n) for n in midi), key=lambda n: n[1])


def _rolls(els, notes) -> list[tuple]:
    """The onset times of the notes of every rolled chord (an arpeggio sign): the notes starting a few hundredths
    apart from its moment on."""
    starts = sorted({round(n[1], 4) for n in notes})
    out = []
    for e in els:
        if e.kind != "arpeg" or e.static or e.time is None:
            continue
        i = bisect_right(starts, e.time - 0.006)
        group = []
        while i < len(starts) and starts[i] <= e.time + 0.5 and (not group or starts[i] - group[-1] <= 0.08):
            group.append(starts[i])
            i += 1
        if len(group) > 1:
            out.append(tuple(group))
    merged = []
    for g in sorted(set(out)):
        if merged and set(merged[-1]) & set(g):
            merged[-1] = tuple(sorted(set(merged[-1]) | set(g)))
        else:
            merged.append(g)
    return merged
