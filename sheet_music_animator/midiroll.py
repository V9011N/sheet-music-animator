"""The MIDI editor: the score's notes as a piano roll, each where it is played (the fit to the recording and every
per-element nudge included), under the recording's waveform.

Only timing is edited, and only in a way the animation can show: a note belongs to an engraved element (a note or
chord of the score) and moving it moves that element -- every note of the chord with it -- by changing the
element's timing nudge (`Project.overrides`), the same value Tap to Keyframe and the Selection tab set.  Notes are
never added or deleted.
"""
from __future__ import annotations

import bisect
import struct
import tempfile
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollBar, QSizePolicy,
                               QVBoxLayout, QWidget)

from .engraver import NOTE_KINDS

LINK_TIME = 0.15        # a note belongs to an element first heard at most this long before it (a rolled chord)...
LINK_PITCH = 2          # ...with a notehead written within this many semitones (accidentals are not in the heads)
LOW, HIGH = 21, 108     # the piano
SNAP_PX = 8             # a dragged note snaps to an attack or another note this close (pixels)
NUDGE = 0.01            # Ctrl+Left/Right moves the selection by this much (s)


# ================================================================================================ the data
def link_notes(score) -> list[tuple[int | None, int]]:
    """(uid of the engraved element, staff from 1) of every note of `score.notes`: the note or chord, heard at
    most LINK_TIME before the note, with the notehead nearest its pitch.  (None, 0) when there is none."""
    units = sorted((u for u in score.units if u.kind in NOTE_KINDS and u.heads), key=lambda u: u.time)
    times = [u.time for u in units]
    out = []
    for pitch, start, *_ in score.notes:
        best, best_cost = None, None
        for u in units[bisect.bisect_left(times, start - LINK_TIME):bisect.bisect_right(times, start + 0.02)]:
            for h in u.heads:
                # written under an 8va or 15ma line, a note sounds an octave or two from where it is drawn
                d = min(abs(h[6] + k - pitch) + (0.5 if k else 0.0) for k in (0, 12, -12, 24, -24))
                if d <= LINK_PITCH + 0.5:
                    cost = (d, abs(u.time - start), h[5])          # untied heads first: a tie starts at its first
                    if best_cost is None or cost < best_cost:
                        best, best_cost = (u.uid, int(h[4]) + 1), cost
        out.append(best or (None, 0))
    return out


def edited_notes(project, score, links) -> list[tuple]:
    """The notes of the score where they are played now: each moved by the nudge of its element."""
    ov = project.overrides
    return [(p, s + ov.get(uid, 0.0), e + ov.get(uid, 0.0), *rest)
            for (p, s, e, *rest), (uid, _) in zip(score.notes, links)]


def move_units(project, uids, dt: float) -> None:
    """Play the elements `uids` `dt` seconds later (earlier when negative)."""
    for uid in uids:
        v = project.overrides.get(uid, 0.0) + dt
        if abs(v) < 1e-4:
            project.overrides.pop(uid, None)
        else:
            project.overrides[uid] = v


def write_midi(path, notes, ppq: int = 480, bpm: float = 120.0) -> None:
    """A standard MIDI file (format 0, piano) of (pitch, start, end, velocity) notes in seconds."""
    per_s = ppq * bpm / 60.0
    events = []
    for p, s, e, v, *_ in notes:
        a = int(round(max(s, 0.0) * per_s))
        b = max(int(round(max(e, 0.0) * per_s)), a + 1)
        events.append((a, 1, 0x90, int(p), max(1, min(int(v), 127))))
        events.append((b, 0, 0x80, int(p), 0))                     # offs before ons at the same tick
    events.sort()

    def vlq(n):
        out = [n & 0x7F]
        while n > 0x7F:
            n >>= 7
            out.append(0x80 | (n & 0x7F))
        return bytes(reversed(out))
    track = bytearray(b"\x00\xff\x51\x03" + int(60e6 / bpm).to_bytes(3, "big") + b"\x00\xc0\x00")
    now = 0
    for tick, _, status, pitch, vel in events:
        track += vlq(tick - now) + bytes((status, pitch, vel))
        now = tick
    track += b"\x00\xff\x2f\x00"
    Path(path).write_bytes(b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq) + b"MTrk" + struct.pack(">I", len(track))
                           + bytes(track))


class RollModel:
    """The notes of the roll as arrays (start, end, pitch, staff, element) in the order of `score.notes`."""

    def __init__(self, project, score):
        self.project, self.score = project, score
        self.links = link_notes(score) if score is not None else []
        n = len(self.links)
        self.pitch = np.array([nt[0] for nt in score.notes], int) if n else np.zeros(0, int)
        self.base_s = np.array([nt[1] for nt in score.notes], float) if n else np.zeros(0)
        self.base_e = np.array([nt[2] for nt in score.notes], float) if n else np.zeros(0)
        self.staff = np.array([st for _, st in self.links], int) if n else np.zeros(0, int)
        self.uid = np.array([-1 if u is None else u for u, _ in self.links], int) if n else np.zeros(0, int)
        self.groups: dict[int, np.ndarray] = {}
        for i, u in enumerate(self.uid):
            if u >= 0:
                self.groups.setdefault(int(u), []).append(i)
        self.groups = {u: np.array(v) for u, v in self.groups.items()}
        self.refresh()

    def refresh(self):
        """Take the project's nudges again (after an edit anywhere)."""
        ov = self.project.overrides if self.project is not None else {}
        shift = np.array([ov.get(int(u), 0.0) if u >= 0 else 0.0 for u in self.uid]) if len(self.uid) else np.zeros(0)
        self.start, self.end = self.base_s + shift, self.base_e + shift
        self.order = np.argsort(self.start, kind="stable")
        self.sorted_start = self.start[self.order]
        self.max_len = float((self.end - self.start).max()) if len(self.start) else 0.0

    def visible(self, t0: float, t1: float) -> np.ndarray:
        """Indices of the notes that sound between t0 and t1."""
        lo = bisect.bisect_left(self.sorted_start, t0 - self.max_len)
        hi = bisect.bisect_right(self.sorted_start, t1)
        idx = self.order[lo:hi]
        return idx[self.end[idx] >= t0]

    def group_of(self, i: int) -> np.ndarray:
        u = int(self.uid[i])
        return self.groups.get(u, np.array([i])) if u >= 0 else np.array([i])


# ================================================================================================ the widgets
KEY_W = 46              # the keyboard
RULER_H = 22            # measure numbers and time
WAVE_H = 64             # the waveform
BLACK = {1, 3, 6, 8, 10}
NAMES = ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"]
STAFF_COLOURS = {1: QColor("#5aa9e6"), 2: QColor("#7bc67b"), 0: QColor("#9a9a9a")}
SELECTED = QColor("#ff9f1c")
PLAYHEAD = QColor("#ff4d4d")


class _View:
    """What the roll shows: seconds `t0` at its left edge, `pps` pixels per second; pitch `top` at its top edge,
    `rh` pixels per semitone."""

    def __init__(self):
        self.t0, self.pps = 0.0, 80.0
        self.top, self.rh = 96.0, 12.0

    def x(self, t):
        return (t - self.t0) * self.pps

    def t(self, x):
        return self.t0 + x / self.pps

    def y(self, pitch):
        return (self.top - pitch) * self.rh

    def pitch(self, y):
        return self.top - y / self.rh


class _TimeBar(QWidget):
    """Measure numbers, time and the recording's waveform, on the roll's time scale.  Click or drag to seek."""

    def __init__(self, editor: "MidiEditor"):
        super().__init__(editor)
        self.ed = editor
        self.setMinimumHeight(RULER_H + WAVE_H)
        self.setMaximumHeight(RULER_H + WAVE_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, _):
        ed, v = self.ed, self.ed.view
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#202024"))
        w = self.width()
        t0, t1 = v.t(0), v.t(w)
        # the waveform
        if ed.wave is not None:
            rms, rate = ed.wave
            peak = float(np.percentile(rms, 99.5)) or 1.0
            mid, half = RULER_H + WAVE_H / 2, WAVE_H / 2 - 3
            xs = np.arange(0, w, 1.0)
            ts = v.t(xs)
            i0 = np.clip((ts * rate).astype(int), 0, len(rms) - 1)
            i1 = np.clip(((ts + 1 / v.pps) * rate).astype(int) + 1, 1, len(rms))
            amp = np.array([rms[a:max(b, a + 1)].max() for a, b in zip(i0, i1)]) / peak
            amp[(ts < 0) | (ts > len(rms) / rate)] = 0
            p.setPen(QPen(QColor("#6f8fb3"), 1))
            for x, a in zip(xs, np.minimum(amp, 1.0)):
                h = a * half
                if h >= 0.5:
                    p.drawLine(QPointF(x, mid - h), QPointF(x, mid + h))
        else:
            p.setPen(QColor("#77777d"))
            p.drawText(QRectF(0, RULER_H, w, WAVE_H), Qt.AlignCenter, "No recording loaded")
        # the ruler: measures, and seconds when zoomed in far enough
        p.fillRect(QRectF(0, 0, w, RULER_H), QColor("#2a2a30"))
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        if ed.score is not None:
            every = max(1, int(np.ceil(40 / max(self._measure_px(), 1e-6))))
            for t, n in ed.score.measures:
                if t0 - 5 <= t <= t1 and (n - 1) % every == 0:
                    x = v.x(t)
                    p.setPen(QColor("#8b8b95"))
                    p.drawLine(QPointF(x, 0), QPointF(x, RULER_H))
                    p.setPen(QColor("#d6d6dc"))
                    p.drawText(QPointF(x + 3, 14), str(n))
        p.setPen(QColor("#55555d"))
        step = next(s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 1e9) if s * v.pps >= 60)
        t = np.floor(t0 / step) * step
        while t <= t1:
            x = v.x(t)
            p.drawLine(QPointF(x, RULER_H - 5), QPointF(x, RULER_H))
            p.drawText(QPointF(x + 2, RULER_H - 2), f"{int(t // 60)}:{t % 60:04.1f}" if step < 60 else "")
            t += step
        # the playhead
        x = v.x(ed.t)
        p.setPen(QPen(PLAYHEAD, 2))
        p.drawLine(QPointF(x, 0), QPointF(x, self.height()))

    def _measure_px(self):
        m = self.ed.score.measures if self.ed.score is not None else []
        if len(m) < 2:
            return 1e9
        return (m[-1][0] - m[0][0]) / (len(m) - 1) * self.ed.view.pps

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.ed.seeked.emit(max(self.ed.view.t(e.position().x()), 0.0))

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self.ed.seeked.emit(max(self.ed.view.t(e.position().x()), 0.0))

    def wheelEvent(self, e):
        self.ed.roll.wheelEvent(e)


class _Keys(QWidget):
    """The piano keyboard at the left of the roll; click a key to hear it."""

    def __init__(self, editor: "MidiEditor"):
        super().__init__(editor)
        self.ed = editor
        self.setFixedWidth(KEY_W)

    def paintEvent(self, _):
        v = self.ed.view
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1b1b1f"))
        f = QFont(self.font())
        f.setPointSizeF(7)
        p.setFont(f)
        lo, hi = int(np.floor(v.pitch(self.height()))), int(np.ceil(v.pitch(0)))
        for pitch in range(max(lo, LOW), min(hi, HIGH) + 1):
            y = v.y(pitch + 0.5)
            black = pitch % 12 in BLACK
            p.fillRect(QRectF(0, y, KEY_W - 1, v.rh), QColor("#2b2b2b") if black else QColor("#ececec"))
            p.setPen(QColor("#888"))
            p.drawLine(QPointF(0, y + v.rh), QPointF(KEY_W, y + v.rh))
            if pitch % 12 == 0 or v.rh >= 13:
                p.setPen(QColor("#d0d0d0") if black else QColor("#333"))
                p.drawText(QRectF(2, y, KEY_W - 4, v.rh), Qt.AlignVCenter | Qt.AlignRight,
                           f"{NAMES[pitch % 12]}{pitch // 12 - 1}")

    def mousePressEvent(self, e):
        pitch = int(round(self.ed.view.pitch(e.position().y())))
        if LOW <= pitch <= HIGH:
            self.ed.audition.emit(pitch)

    def wheelEvent(self, e):
        self.ed.roll.wheelEvent(e)


class _Roll(QWidget):
    """The notes.  Click, Ctrl+click and Shift+click select (a note selects its whole chord: they move
    together); drag on empty space to select in a rectangle; drag selected notes left or right to retime them."""

    def __init__(self, editor: "MidiEditor"):
        super().__init__(editor)
        self.ed = editor
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self._press = None        # (x, y, note index or None, modifiers)
        self._drag_dt = 0.0
        self._band = None         # QRectF while selecting in a rectangle
        self._snap_at = None      # time of the attack or note snapped to, while dragging
        self._pan = None
        self._cache = None        # the notes drawn for the current view

    # ---- drawing ------------------------------------------------------------------------------------------------
    def invalidate(self):
        """The notes, the view or the selection changed: draw the notes again (the playhead alone does not)."""
        self._cache = None
        self.update()

    def paintEvent(self, _):
        ed, v, m = self.ed, self.ed.view, self.ed.model
        if self._cache is None or self._cache.size() != self.size() * self.devicePixelRatioF():
            self._cache = self._draw_notes()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        h = self.height()
        if m is not None:                                           # the notes sounding now, lit up
            now = ed.t
            for i in m.visible(now, now):
                if m.start[i] <= now < m.end[i] and i not in ed.selected:
                    p.fillRect(self._rect(i, 0.0), STAFF_COLOURS.get(int(m.staff[i]), STAFF_COLOURS[0]).lighter(160))
        if self._snap_at is not None:
            p.setPen(QPen(QColor("#ffe066"), 1, Qt.DashLine))
            x = v.x(self._snap_at)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
        if self._band is not None:
            p.setPen(QPen(QColor("#ffffff"), 1, Qt.DashLine))
            p.fillRect(self._band, QColor(255, 255, 255, 30))
            p.drawRect(self._band)
        x = v.x(ed.t)
        p.setPen(QPen(PLAYHEAD, 2))
        p.drawLine(QPointF(x, 0), QPointF(x, h))

    def _rect(self, i, dt) -> QRectF:
        m, v = self.ed.model, self.ed.view
        s, e = m.start[i] + dt, m.end[i] + dt
        return QRectF(v.x(s), v.y(m.pitch[i] + 0.5) + 1, max((e - s) * v.pps, 3.0), max(v.rh - 2, 2.0))

    def _draw_notes(self) -> QPixmap:
        ed, v, m = self.ed, self.ed.view, self.ed.model
        ratio = self.devicePixelRatioF()
        pm = QPixmap(self.size() * ratio)
        pm.setDevicePixelRatio(ratio)
        p = QPainter(pm)
        w, h = self.width(), self.height()
        p.fillRect(QRectF(0, 0, w, h), QColor("#26262b"))
        lo, hi = int(np.floor(v.pitch(h))), int(np.ceil(v.pitch(0)))
        for pitch in range(max(lo, LOW), min(hi, HIGH) + 1):          # the black keys' rows a little darker
            y = v.y(pitch + 0.5)
            if pitch % 12 in BLACK:
                p.fillRect(QRectF(0, y, w, v.rh), QColor("#202024"))
            if pitch % 12 == 0:
                p.setPen(QColor("#3c3c44"))
                p.drawLine(QPointF(0, y + v.rh), QPointF(w, y + v.rh))
        t0, t1 = v.t(0), v.t(w)
        if ed.score is not None:                                        # bar lines
            p.setPen(QColor("#393941"))
            for t, _ in ed.score.measures:
                if t0 <= t <= t1:
                    p.drawLine(QPointF(v.x(t), 0), QPointF(v.x(t), h))
        if m is not None:
            idx = m.visible(t0 - abs(self._drag_dt), t1 + abs(self._drag_dt))
            idx = idx[(m.pitch[idx] >= lo - 1) & (m.pitch[idx] <= hi + 1)]
            outline = v.rh >= 6
            for i in idx:
                chosen = i in ed.selected
                r = self._rect(i, self._drag_dt if chosen else 0.0)
                p.fillRect(r, SELECTED if chosen else STAFF_COLOURS.get(int(m.staff[i]), STAFF_COLOURS[0]))
                if outline:
                    p.setPen(QColor(0, 0, 0, 110))
                    p.drawRect(r)
        p.end()
        return pm

    # ---- picking ------------------------------------------------------------------------------------------------
    def note_at(self, pos) -> int | None:
        m, v = self.ed.model, self.ed.view
        if m is None:
            return None
        t = v.t(pos.x())
        pitch = int(round(v.pitch(pos.y())))
        slack = 3 / v.pps                                            # very short notes are drawn 3 px wide
        idx = m.visible(t - slack, t + slack)
        idx = idx[(m.pitch[idx] == pitch) & (m.start[idx] <= t + slack)]
        if not len(idx):
            return None
        return int(idx[np.argmax(m.start[idx])])                    # the latest starting: the one drawn on top

    def notes_in(self, r: QRectF) -> np.ndarray:
        m, v = self.ed.model, self.ed.view
        t0, t1 = v.t(r.left()), v.t(r.right())
        idx = m.visible(t0, t1)
        p_hi, p_lo = v.pitch(r.top()) + 0.5, v.pitch(r.bottom()) - 0.5
        return idx[(m.pitch[idx] <= p_hi) & (m.pitch[idx] >= p_lo)]

    # ---- mouse --------------------------------------------------------------------------------------------------
    def mousePressEvent(self, e):
        self.setFocus()
        pos, mods = e.position(), e.modifiers()
        if e.button() == Qt.MiddleButton:
            self._pan = (pos, self.ed.view.t0, self.ed.view.top)
            return
        if e.button() != Qt.LeftButton or self.ed.model is None:
            return
        i = self.note_at(pos)
        ed = self.ed
        if i is None:
            if not mods & (Qt.ControlModifier | Qt.ShiftModifier):
                ed.set_selection(set())
            self._band = QRectF(pos, pos)
            self._press = (pos, None, mods, set(ed.selected))
            self.update()
            return
        ed.audition.emit(int(ed.model.pitch[i]))
        group = set(int(k) for k in ed.model.group_of(i))
        if mods & Qt.ControlModifier:
            ed.set_selection(ed.selected ^ group if group <= ed.selected else ed.selected | group)
            ed.anchor = i
            self._press = None
            return
        if mods & Qt.ShiftModifier and ed.anchor is not None:
            a, b = sorted((ed.model.start[ed.anchor], ed.model.start[i]))
            span = np.nonzero((ed.model.start >= a - 1e-6) & (ed.model.start <= b + 1e-6))[0]
            chosen = set()
            for k in span:
                chosen |= set(int(g) for g in ed.model.group_of(int(k)))
            ed.set_selection(chosen)
            self._press = None
            return
        if i not in ed.selected:
            ed.set_selection(group)
        ed.anchor = i
        self._press = (pos, i, mods, None)
        self._drag_dt = 0.0

    def mouseMoveEvent(self, e):
        pos, ed, v = e.position(), self.ed, self.ed.view
        if self._pan is not None:
            start, t0, top = self._pan
            v.t0 = max(t0 - (pos.x() - start.x()) / v.pps, -1.0)
            v.top = top + (pos.y() - start.y()) / v.rh
            ed.view_changed()
            return
        if self._press is None:
            i = self.note_at(pos)
            self.setToolTip(self._tip(i) if i is not None else "")
            return
        start, i, mods, before = self._press
        if i is None:                                               # selecting in a rectangle
            self._band = QRectF(start, pos).normalized()
            inside = set()
            for k in self.notes_in(self._band):
                inside |= set(int(g) for g in ed.model.group_of(int(k)))
            ed.set_selection(before ^ inside if mods & Qt.ControlModifier else before | inside)
            return
        if abs(pos.x() - start.x()) < 3 and self._drag_dt == 0.0:
            return
        dt = (pos.x() - start.x()) / v.pps
        self._snap_at = None
        if ed.snap and not (e.modifiers() & Qt.AltModifier):
            dt, self._snap_at = ed.snapped(i, dt)
        self._drag_dt = dt
        ed.dragging(dt)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MiddleButton:
            self._pan = None
            return
        if self._press is None:
            return
        start, i, mods, _ = self._press
        self._press, self._band, self._snap_at = None, None, None
        dt, self._drag_dt = self._drag_dt, 0.0
        if i is not None and abs(dt) > 1e-6:
            self.ed.move_selection(dt)
        elif i is not None and not mods & (Qt.ControlModifier | Qt.ShiftModifier):
            self.ed.set_selection(set(int(k) for k in self.ed.model.group_of(i)))   # a click on a selected note
        self.ed.dragging(0.0)

    def wheelEvent(self, e):
        ed, v = self.ed, self.ed.view
        d = e.angleDelta()
        step = (d.y() or d.x()) / 120.0
        mods = e.modifiers()
        pos = e.position()
        if mods & Qt.ControlModifier:                               # zoom in time, about the mouse
            t = v.t(pos.x())
            v.pps = float(np.clip(v.pps * 1.25 ** step, ed.min_pps(), 4000.0))
            v.t0 = t - pos.x() / v.pps
        elif mods & Qt.AltModifier:                                 # zoom in pitch, about the mouse
            pitch = v.pitch(pos.y()) if self.underMouse() else v.pitch(self.height() / 2)
            v.rh = float(np.clip(v.rh * 1.2 ** step, 3.0, 40.0))
            v.top = pitch + (pos.y() if self.underMouse() else self.height() / 2) / v.rh
        elif mods & Qt.ShiftModifier:                               # scroll in time
            v.t0 -= step * 80 / v.pps
        else:                                                       # scroll in pitch
            v.top += step * 3
        ed.view_changed()

    def keyPressEvent(self, e):
        ed = self.ed
        if e.key() in (Qt.Key_Left, Qt.Key_Right) and e.modifiers() & Qt.ControlModifier and ed.selected:
            ed.move_selection(NUDGE * (1 if e.key() == Qt.Key_Right else -1))
        elif e.key() == Qt.Key_Escape:
            ed.set_selection(set())
        elif e.key() == Qt.Key_A and e.modifiers() & Qt.ControlModifier and ed.model is not None:
            ed.set_selection(set(int(k) for k in np.nonzero(ed.model.uid >= 0)[0]))
        else:
            super().keyPressEvent(e)

    def _tip(self, i) -> str:
        m = self.ed.model
        p = int(m.pitch[i])
        s = float(m.start[i])
        nudge = self.ed.project.overrides.get(int(m.uid[i]), 0.0) if m.uid[i] >= 0 else 0.0
        where = f"{int(s // 60)}:{s % 60:05.2f}"
        if m.uid[i] < 0:
            return f"{NAMES[p % 12]}{p // 12 - 1} at {where} — not linked to an engraving, cannot be moved"
        return f"{NAMES[p % 12]}{p // 12 - 1} at {where}" + (f" (moved {nudge * 1000:+.0f} ms)" if nudge else "")


class MidiEditor(QWidget):
    """The MIDI editor tab: tools, the time bar with the waveform, the keyboard and the roll, with scroll bars."""

    seeked = Signal(float)
    edited = Signal()                 # notes were moved: the project's nudges changed
    audition = Signal(int)            # a note or key was clicked: play this pitch
    exportRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = self.score = self.model = None
        self.wave = None              # (rms, values per second) of the recording, or None
        self.attack_times = np.zeros(0)
        self.selected: set[int] = set()
        self.anchor = None
        self.t = 0.0
        self.snap = True
        self.duration = 10.0
        self.view = _View()
        self._dirty = True
        self._needs_fit = True        # fit the piece in once the roll has its real size

        tools = QHBoxLayout()
        tools.setContentsMargins(6, 4, 6, 4)
        self.lbl = QLabel("Open a score to see its notes.")
        self.lbl.setStyleSheet("color:#bbb;")
        tools.addWidget(self.lbl, 1)
        self.chk_snap = QCheckBox("Snap")
        self.chk_snap.setChecked(True)
        self.chk_snap.setToolTip("Dragged notes snap to the attacks in the recording and to other notes "
                                 "(hold Alt to drag freely)")
        self.chk_snap.toggled.connect(lambda on: setattr(self, "snap", on))
        tools.addWidget(self.chk_snap)
        for text, tip, fn in (("−", "Zoom out in time (Ctrl+wheel)", lambda: self.zoom_time(1 / 1.5)),
                              ("+", "Zoom in in time (Ctrl+wheel)", lambda: self.zoom_time(1.5)),
                              ("↕−", "Smaller rows (Alt+wheel)", lambda: self.zoom_pitch(1 / 1.3)),
                              ("↕+", "Taller rows (Alt+wheel)", lambda: self.zoom_pitch(1.3)),
                              ("Fit", "Show the whole piece and all its notes", self.fit),
                              ("Export MIDI…", "Save the notes, as they are timed now, as a .mid file",
                               self.exportRequested.emit)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            b.setFocusPolicy(Qt.NoFocus)
            tools.addWidget(b)

        self.bar = _TimeBar(self)
        self.keys = _Keys(self)
        self.roll = _Roll(self)
        self.hscroll = QScrollBar(Qt.Horizontal)
        self.vscroll = QScrollBar(Qt.Vertical)
        self.hscroll.valueChanged.connect(self._hscrolled)
        self.vscroll.valueChanged.connect(self._vscrolled)
        corner = QWidget()
        corner.setFixedWidth(KEY_W)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        grid.addWidget(corner, 0, 0)
        grid.addWidget(self.bar, 0, 1)
        grid.addWidget(self.keys, 1, 0)
        grid.addWidget(self.roll, 1, 1)
        grid.addWidget(self.vscroll, 1, 2)
        grid.addWidget(self.hscroll, 2, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(1, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(tools)
        lay.addLayout(grid, 1)

    # ---- data ---------------------------------------------------------------------------------------------------
    def set_data(self, project, score, duration: float):
        """A newly loaded score (or the project replaced): rebuild everything."""
        self.project, self.score = project, score
        self.duration = max(duration, 1.0)
        self.model = RollModel(project, score) if score is not None else None
        self.selected, self.anchor = set(), None
        self._dirty = False
        self._update_label()
        self._needs_fit = True
        if self.isVisible():
            QTimer.singleShot(0, self.fit)

    def set_audio(self, wave, attack_times=None):
        """The waveform (rms, values per second) to show, or None; the attacks to snap to."""
        self.wave = wave
        self.attack_times = np.asarray(attack_times if attack_times is not None else [], float)
        self.bar.update()

    def project_changed(self):
        """The project's timing changed somewhere (undo, the Selection tab, Tap to Keyframe): show it."""
        if self.model is None:
            return
        if not self.isVisible():
            self._dirty = True
            return
        self.model.refresh()
        self._dirty = False
        self.roll.invalidate()

    def showEvent(self, e):
        if self._needs_fit:
            QTimer.singleShot(0, self.fit)
        if self._dirty and self.model is not None:
            self.model.refresh()
            self._dirty = False
            self.roll.invalidate()
        super().showEvent(e)

    def set_time(self, t: float, follow: bool = False):
        self.t = t
        if not self.isVisible():
            return
        w = self.roll.width()
        if follow and not (self.view.t0 <= t <= self.view.t(w * 0.95)):
            self.view.t0 = t - 0.05 * w / self.view.pps         # a page on, like a score's page turn
            self.view_changed()
            return
        self.bar.update()
        self.roll.update()

    # ---- the view ---------------------------------------------------------------------------------------------
    def min_pps(self) -> float:
        return max(self.roll.width(), 100) / (self.duration + 2.0)

    def fit(self):
        self._needs_fit = False
        v = self.view
        v.pps = self.min_pps()
        v.t0 = -4.0 / v.pps
        if self.model is not None and len(self.model.pitch):
            lo, hi = int(self.model.pitch.min()) - 2, int(self.model.pitch.max()) + 2
            v.rh = float(np.clip(max(self.roll.height(), 200) / (hi - lo + 1), 3.0, 40.0))
            v.top = hi + 0.5
        self.view_changed()

    def zoom_time(self, f: float):
        v, w = self.view, self.roll.width()
        mid = v.t(w / 2)
        v.pps = float(np.clip(v.pps * f, self.min_pps(), 4000.0))
        v.t0 = mid - w / 2 / v.pps
        self.view_changed()

    def zoom_pitch(self, f: float):
        v, h = self.view, self.roll.height()
        mid = v.pitch(h / 2)
        v.rh = float(np.clip(v.rh * f, 3.0, 40.0))
        v.top = mid + h / 2 / v.rh
        self.view_changed()

    def view_changed(self):
        v, w, h = self.view, self.roll.width(), self.roll.height()
        span = w / v.pps
        v.t0 = float(np.clip(v.t0, -1.0, max(self.duration + 1.0 - span, -1.0)))
        rows = h / v.rh
        v.top = float(np.clip(v.top, LOW - 0.5 + rows, max(HIGH + 0.5, LOW - 0.5 + rows)))
        self.hscroll.blockSignals(True)
        self.hscroll.setRange(-100, max(int((self.duration + 1.0 - span) * 100), -100))
        self.hscroll.setPageStep(max(int(span * 100), 1))
        self.hscroll.setValue(int(v.t0 * 100))
        self.hscroll.blockSignals(False)
        self.vscroll.blockSignals(True)
        self.vscroll.setRange(0, max(int((HIGH - LOW + 1 - rows) * 10), 0))
        self.vscroll.setPageStep(max(int(rows * 10), 1))
        self.vscroll.setValue(int((HIGH + 0.5 - v.top) * 10))
        self.vscroll.blockSignals(False)
        self.bar.update()
        self.keys.update()
        self.roll.invalidate()

    def _hscrolled(self, value):
        self.view.t0 = value / 100.0
        self.view_changed()

    def _vscrolled(self, value):
        self.view.top = HIGH + 0.5 - value / 10.0
        self.view_changed()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self.view_changed)

    def sizeHint(self):
        return QSize(1000, 600)

    # ---- selection and editing ----------------------------------------------------------------------------------
    def set_selection(self, chosen: set):
        self.selected = set(chosen)
        self._update_label()
        self.roll.invalidate()

    def _update_label(self):
        if self.model is None:
            self.lbl.setText("Open a score to see its notes.")
            return
        n = len(self.selected)
        chords = len({int(self.model.uid[i]) for i in self.selected if self.model.uid[i] >= 0})
        if n:
            self.lbl.setText(f"{n} note{'s' if n > 1 else ''} selected ({chords} chord{'s' if chords != 1 else ''}"
                             f" / note{'s' if chords != 1 else ''} of the score). Drag left or right to retime "
                             f"them; Ctrl+←/→ nudges by 10 ms.")
        else:
            self.lbl.setText(f"{len(self.model.pitch)} notes. Click, Ctrl+click, Shift+click or drag a rectangle to "
                             f"select; a note moves with its chord. Ctrl+wheel / Alt+wheel zoom, Shift+wheel and "
                             f"middle-drag pan.")

    def selected_units(self) -> list[int]:
        m = self.model
        return sorted({int(m.uid[i]) for i in self.selected if m.uid[i] >= 0})

    def snapped(self, grabbed: int, dt: float) -> tuple[float, float | None]:
        """`dt` moved so the grabbed note lands on an attack of the recording or on another note's start, when
        one is within SNAP_PX; and where it snapped (or None)."""
        m = self.model
        want = m.start[grabbed] + dt
        reach = SNAP_PX / self.view.pps
        others = m.start[[i for i in m.visible(want - reach, want + reach) if i not in self.selected]]
        cands = np.concatenate([self.attack_times[np.abs(self.attack_times - want) <= reach], others])
        cands = cands[np.abs(cands - want) <= reach]
        if not len(cands):
            return dt, None
        best = float(cands[np.argmin(np.abs(cands - want))])
        return best - m.start[grabbed], best

    def dragging(self, dt: float):
        self.roll.invalidate()

    def move_selection(self, dt: float):
        uids = self.selected_units()
        if not uids:
            return
        move_units(self.project, uids, dt)
        self.model.refresh()
        self.roll.invalidate()
        self.edited.emit()


class NotePlayer:
    """Plays a pitch when a note or key is clicked (short synthesised tones, made the first time each is needed)."""

    def __init__(self):
        self._effects = {}
        self._dir = Path(tempfile.gettempdir()) / "sheet_music_animator" / "keys"
        try:
            from PySide6.QtMultimedia import QSoundEffect
            self._cls = QSoundEffect
        except ImportError:   # pragma: no cover - no multimedia: clicks are silent
            self._cls = None

    def play(self, pitch: int):
        if self._cls is None:
            return
        fx = self._effects.get(pitch)
        if fx is None:
            from . import audio
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{pitch}.wav"
            if not path.exists():
                v = audio._voice(pitch, 0.35)
                audio.write_wav(path, v / (np.max(np.abs(v)) or 1.0) * 0.6)
            fx = self._cls()
            fx.setSource(QUrl.fromLocalFile(str(path)))
            fx.setVolume(0.6)
            self._effects[pitch] = fx
        fx.play()
