"""Clean-ups of a MusicXML score before Verovio reads it.

The notation programs export things that Verovio draws differently from them, or cannot take at all (a line
that is stopped before it is started makes its MIDI export crash).  `clean` mends them in place on the parsed
score; every fix is general (it looks at what the file says, not at which program wrote it):

* marks the program hid (`print-object="no"`) are not drawn; what they do to the sound (tempo, loudness) stays;
* tempo marks that only steer the playback (a bare number, or a crowd of metronome marks a few beats apart that
  make a written-out rubato) are hidden the same way;
* dynamics the program exported as placeholders (`<other-dynamics>other-dynamics</…>`, blanks) are dropped, and
  SMuFL names written as text (`<sym>dynamicMezzo</sym>`) become letters;
* pedal marks: the MusicXML 4 types `resume` / `discontinue` become `start` / `stop`, every pedal mark of a part
  sits below its lowest staff (piano pedalling is written under the left hand), when the pedalling is written
  twice (signs and lines at the same moments) one of the two is kept, a pedal pressed while it is down is a
  pedal change, and MuseScore's "Ped. ... *" marks (exported as lines before MuseScore 4) are drawn as signs;
* lines that run along the music (8va, hairpins, dashes, brackets, pedals) are paired by musical time: a stop
  written before its start (they are in different voices) is moved after it, and a stop that ends nothing or a
  line that never ends is taken out;
* a score for one instrument does not repeat its abbreviated name on every line.
"""
from __future__ import annotations

import re

from lxml import etree

SPANNERS = ("octave-shift", "wedge", "dashes", "bracket", "pedal")
PLAYBACK_TEMPO_REACH = 1      # metronome marks this many measures apart or closer are a written-out rubato
DYNAMIC_SYMS = {"dynamicPiano": "p", "dynamicMezzo": "m", "dynamicForte": "f", "dynamicRinforzando": "r",
                "dynamicSforzando": "s", "dynamicZ": "z", "dynamicNiente": "n"}


def clean(root) -> bool:
    """Apply every clean-up to a <score-partwise> element; True when something changed."""
    if root is None or root.tag != "score-partwise":
        return False
    changed = False
    for fix in (_hidden_marks, _playback_tempos, _placeholder_dynamics, _pedal_types, _duplicate_pedals,
                _pedal_signs, _pedals_below, _spanners, _single_part_labels):
        changed = bool(fix(root)) or changed
    return changed


# ------------------------------------------------------------------------------------------- positions
def _events(part, starts=None):
    """(element, measure index, onset in quarter notes from the start of the part) of every direction and note of
    a part, in document order.  Positions follow <backup>, <forward>, chords and grace notes.  `starts`, a list,
    receives the time every measure starts at."""
    out = []
    start, divisions = 0.0, 1
    for mi, measure in enumerate(part.findall("measure")):
        pos = longest = 0
        last = 0
        for e in measure:
            if e.tag == "attributes":
                d = e.findtext("divisions")
                if d and d.strip().isdigit() and int(d) > 0:
                    divisions = int(d)
            elif e.tag == "backup":
                pos -= int(e.findtext("duration") or 0)
            elif e.tag == "forward":
                pos += int(e.findtext("duration") or 0)
            elif e.tag == "note":
                chord = e.find("chord") is not None
                at = last if chord else pos
                out.append((e, mi, start + at / divisions))
                if not chord and e.find("grace") is None:
                    last, pos = pos, pos + int(e.findtext("duration") or 0)
            elif e.tag == "direction":
                off = e.findtext("offset")
                out.append((e, mi, start + (pos + (int(off) if off and re.fullmatch(r"-?\d+", off.strip()) else 0)) / divisions))
            longest = max(longest, pos)
        if starts is not None:
            starts.append((start, divisions))
        start += longest / divisions
    return out


def _remove_direction_child(child) -> None:
    """Take a child out of its <direction-type>, and the direction-type out of its direction when it is empty.
    A direction left without anything to show keeps what it does to the sound (an empty direction: Verovio keeps
    its tempo and draws nothing), or goes."""
    dt = child.getparent()
    dt.remove(child)
    if len(dt):
        return
    d = dt.getparent()
    d.remove(dt)
    if d.find("direction-type") is None and d.find("sound") is None and d.getparent() is not None:
        d.getparent().remove(d)


# ------------------------------------------------------------------------------------------- hidden marks
def _hidden_marks(root) -> int:
    """Marks the notation program hid.  Verovio only leaves out a hidden metronome mark; hidden words, dynamics and
    hairpins it draws.  Hidden tuplet numbers and brackets keep their rhythm."""
    n = 0
    for dt in list(root.iter("direction-type")):
        for e in [e for e in dt if e.get("print-object") == "no"]:
            _remove_direction_child(e)
            n += 1
    for nt in root.iter("notations"):
        if nt.get("print-object") != "no":
            continue
        for t in nt.iter("tuplet"):
            t.set("bracket", "no")
            t.set("show-number", "none")
            n += 1
        for kind in ("articulations", "ornaments", "fermata", "technical", "dynamics"):
            for e in nt.findall(kind):
                nt.remove(e)
                n += 1
    return n


_NUMBER = re.compile(r"\s*\(?\s*(?:\d+(?:[.,]\d*)?|[.,]\d+)\s*\)?\s*")   # "84", "(84)", ".5" (of "60.5")


def _tempo_marks(d) -> list:
    """The parts of a direction that print a tempo number (a metronome mark, or a bare number such as "84" or the
    ".5" of "60.5"), when the direction sets a tempo (<sound tempo>)."""
    s = d.find("sound")
    if s is None or s.get("tempo") is None:
        return []
    return [e for dt in d.findall("direction-type") for e in dt
            if e.tag == "metronome" or (e.tag == "words" and _NUMBER.fullmatch(e.text or ""))]


def _playback_tempos(root) -> int:
    """Tempo marks that only steer the playback.  A bare number ("84") is never how a tempo is printed; a crowd of
    metronome marks within a measure of each other is a rubato written out for the playback (the printed score
    shows none of them).  Their numbers are hidden (words with them, "rapido", stay); their tempo stays."""
    n = 0
    for part in root.findall("part"):
        marks = [(mi, kids) for e, mi, _ in _events(part) if e.tag == "direction" and (kids := _tempo_marks(e))]
        for k, (mi, kids) in enumerate(marks):
            bare = all(e.tag == "words" for e in kids)
            crowd = any(j != k and abs(mj - mi) <= PLAYBACK_TEMPO_REACH for j, (mj, _) in enumerate(marks))
            if bare or crowd:
                for e in kids:
                    _remove_direction_child(e)
                n += 1
    return n


# ------------------------------------------------------------------------------------------- dynamics
def _placeholder_dynamics(root) -> int:
    n = 0
    for e in list(root.iter("other-dynamics")):
        syms = e.findall("sym")
        if syms:     # MuseScore 4 writes the SMuFL glyphs by name
            letters = "".join(DYNAMIC_SYMS.get((s.text or "").strip(), "") for s in syms)
            for s in syms:
                e.remove(s)
            e.text = ((e.text or "") + letters).strip()
            n += 1
        text = (e.text or "").strip()
        if not text or text == "other-dynamics":
            dyn = e.getparent()
            dyn.remove(e)
            if len(dyn) == 0:
                _remove_direction_child(dyn)
            n += 1
    return n


# ------------------------------------------------------------------------------------------- pedals
def _pedal_types(root) -> int:
    """MusicXML 4's `resume` (a pedal line that goes on after a break) and `discontinue` (one that ends without a
    hook), which MuseScore 4 writes for ordinary pedal marks, are a start and a stop to Verovio (it drops them)."""
    n = 0
    for p in root.iter("pedal"):
        t = {"resume": "start", "discontinue": "stop"}.get(p.get("type"))
        if t:
            p.set("type", t)
            n += 1
    return n


def _pedal_signs(root) -> int:
    """MuseScore before version 4 exports every pedal mark as a line (`line="yes"`, no `sign`), also its usual
    "Ped. ... *" marks, and that is what its scores show; MuseScore 4 says which it is (`sign`).  Such marks are
    drawn as signs."""
    m = re.search(r"MuseScore\s+(\d+)", " ".join(root.xpath("//identification/encoding/software/text()")))
    if not m or int(m.group(1)) >= 4:
        return 0
    n = 0
    for p in root.iter("pedal"):
        if p.get("line") == "yes" and p.get("sign") is None:
            del p.attrib["line"]
            n += 1
    return n


def _staves(part) -> int:
    return max([int(t) for t in part.xpath(".//attributes/staves/text()") if t.strip().isdigit()] or [1])


def _pedals_below(root) -> int:
    """Piano pedalling is written below the lowest staff of the instrument."""
    n = 0
    for part in root.findall("part"):
        low = _staves(part)
        if low < 2:
            continue
        for d in part.iter("direction"):
            if d.find("direction-type/pedal") is None:
                continue
            st = d.find("staff")
            if st is None:
                st = etree.SubElement(d, "staff")
            if st.text != str(low) or d.get("placement") == "above":
                st.text = str(low)
                d.set("placement", "below")
                n += 1
            _order_direction(d)
    return n


_DIRECTION_ORDER = ("direction-type", "offset", "footnote", "level", "voice", "staff", "sound", "listening")


def _order_direction(d) -> None:
    kids = sorted(d, key=lambda e: _DIRECTION_ORDER.index(e.tag) if e.tag in _DIRECTION_ORDER else 99)
    for e in kids:
        d.append(e)


def _duplicate_pedals(root) -> int:
    """Pedalling written twice: a "Ped." sign and a pedal line that go down at the same moment (an export that
    writes the program's pedal symbols and its hidden pedal lines both).  The sign is kept, and the copy (the line
    from that start to its stop) goes; lines that no sign doubles stay."""
    n = 0
    for part in root.findall("part"):
        marks = [(e, t) for e, _, t in _events(part) if e.tag == "direction" and e.find("direction-type/pedal") is not None]
        kind = lambda d: d.find("direction-type/pedal").get("type")              # noqa: E731
        lines = sorted(((d, t) for d, t in marks if d.find("direction-type/pedal").get("line") == "yes"), key=lambda x: x[1])
        downs = [t for d, t in marks if d.find("direction-type/pedal").get("line") != "yes" and kind(d) == "start"]
        if not lines or not downs:
            continue
        for i, (d, t) in enumerate(lines):
            if kind(d) != "start" or not any(abs(t - u) <= 1.0 for u in downs):
                continue
            stop = next((s for s, _ in lines[i + 1:] if kind(s) in ("stop", "start")), None)
            for e in (d, stop if stop is not None and kind(stop) == "stop" else None):
                if e is not None and e.getparent() is not None:
                    e.getparent().remove(e)
                    n += 1
    return n


# ------------------------------------------------------------------------------------------- lines along the music
def _key(e, d):
    if e.tag == "pedal":
        return ("pedal",)
    return (e.tag, e.get("number", "1"), d.findtext("staff") or "1")


def _measure_slots(measure):
    """(document index, position in divisions) of every point between the children of a measure."""
    out, pos, last = [(0, 0)], 0, 0
    for i, e in enumerate(measure):
        if e.tag == "backup":
            pos -= int(e.findtext("duration") or 0)
        elif e.tag == "forward":
            pos += int(e.findtext("duration") or 0)
        elif e.tag == "note" and e.find("chord") is None and e.find("grace") is None:
            last, pos = pos, pos + int(e.findtext("duration") or 0)
        out.append((i + 1, pos))
    return out


def _spanners(root) -> int:
    """Pair the starts and stops of the lines along the music by when they happen, not by where they are written.
    Verovio pairs them in document order: a stop written (in another voice) before its start ends nothing and
    leaves the line open, and an open 8va line or a pedal let go that was never pressed makes its MIDI crash or
    run backwards."""
    n = 0
    for part in root.findall("part"):
        measures = part.findall("measure")
        starts: list = []
        events = _events(part, starts)
        order = {id(e): i for i, (e, _, _) in enumerate(events)}
        items = []                       # (time, rank, doc order, key, type, element, direction, measure)
        for d, mi, t in events:
            if d.tag != "direction":
                continue
            for dt in d.findall("direction-type"):
                for e in dt:
                    if e.tag not in SPANNERS:
                        continue
                    kind = e.get("type")
                    if kind == "continue":
                        continue
                    if e.tag == "octave-shift":
                        kind = "stop" if kind == "stop" else "start"
                    if e.tag == "wedge":
                        kind = "stop" if kind == "stop" else "start"
                    if e.tag == "pedal" and kind not in ("start", "stop", "change", "sostenuto"):
                        continue
                    if kind == "sostenuto":
                        continue
                    rank = 0 if kind == "stop" else 1      # at one moment a line ends before the next begins
                    items.append((t, rank, order[id(d)], _key(e, d), kind, e, d, mi))
        items.sort(key=lambda x: (x[0], x[1], x[2]))
        open_: dict = {}                 # key -> (element, direction, doc order, time) of the start not yet ended
        gone, moves = [], []
        for t, _, doc, key, kind, e, d, mi in items:
            if kind == "change":
                if key not in open_:
                    e.set("type", "start")         # a pedal changed that was never down: it goes down here
                    open_[key] = (e, d, doc, t)
                    n += 1
                continue
            if kind == "start":
                if key in open_:
                    if key[0] == "pedal":          # a pedal pressed while it is down: it is changed there
                        e.set("type", "change")
                        n += 1
                        continue
                    gone.append(open_[key][0])     # a line started again before it ended
                open_[key] = (e, d, doc, t)
                continue
            if key not in open_:
                gone.append(e)                     # a stop that ends nothing (a pedal let go while it is up)
                continue
            se, sd, sdoc, st = open_.pop(key)
            if doc < sdoc:                         # written before its start: move it after the start
                moves.append((e, d, se, sd, t, mi))
        gone += [v[0] for k, v in open_.items() if k[0] != "pedal"]    # never ended (a held pedal may end the piece)
        for e, d, se, sd, t, mi in moves:
            if not _move_after(d, sd, measures[mi], round((t - starts[mi][0]) * starts[mi][1])):
                gone += [e, se]                    # nowhere to put it: the line goes
        for e in gone:
            if e.getparent() is not None:
                _remove_direction_child(e)
                n += 1
        n += len(moves)
    return n


def _move_after(d, start_dir, measure, target: int) -> bool:
    """Move direction `d` to the first point of `measure` after `start_dir` (in document order) that lies at
    `target` divisions into the measure.  False when there is no such point."""
    if d.getparent() is not measure:
        return False
    kids = list(measure)
    after = kids.index(start_dir) + 1 if start_dir.getparent() is measure else 0
    for idx, pos in _measure_slots(measure):
        if idx >= after and pos == target:
            anchor = kids[idx] if idx < len(kids) else None
            if anchor is d:
                return True
            measure.remove(d)
            if anchor is None:
                measure.append(d)
            else:
                anchor.addprevious(d)
            return True
    return False


def pedal_forms(root) -> list[list[str | None]]:
    """For every measure (in order), the MEI form of each of its pedal marks in document order: "pedline" for a
    pedal line that starts with the "Ped." sign (`line="yes" sign="yes"`, and the end of such a line), else None.
    Verovio's import does not read `sign` and draws every pedal line as a bracket."""
    rows: list[list] = []
    for part in root.findall("part"):
        for mi, measure in enumerate(part.findall("measure")):
            while len(rows) <= mi:
                rows.append([])
            for p in measure.iter("pedal"):
                rows[mi].append(p)
    out, sign = [], False
    for ps in rows:
        forms = []
        for p in ps:
            kind = p.get("type")
            if kind == "start":
                sign = p.get("line") == "yes" and p.get("sign") == "yes"
            forms.append("pedline" if sign and p.get("line") == "yes" else None)
        out.append(forms)
    return out


# ------------------------------------------------------------------------------------------- labels
def _single_part_labels(root) -> int:
    """A score for one instrument names it on the first line only (the modern default of the notation programs)."""
    parts = root.findall("part-list/score-part")
    if len(parts) != 1:
        return 0
    n = 0
    for tag in ("part-abbreviation", "part-abbreviation-display"):
        for e in parts[0].findall(tag):
            parts[0].remove(e)
            n += 1
    return n
