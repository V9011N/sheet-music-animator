"""The MIDI editor: notes linked to their engravings, retimed (only) by moving whole chords, the edits flowing into
the score animation, the synth and a MIDI file; and the editor itself driven with the mouse.

Run:  python -m unittest discover tests"""
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np                                     # noqa: E402
from PySide6.QtCore import QEvent, QPointF, Qt         # noqa: E402
from PySide6.QtGui import QMouseEvent                  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog    # noqa: E402

from sheet_music_animator import audio                 # noqa: E402
from sheet_music_animator.build import build_score     # noqa: E402
from sheet_music_animator.engraver import _Builder     # noqa: E402
from sheet_music_animator.midiroll import (RollModel, edited_notes, link_notes, move_units,   # noqa: E402
                                           write_midi)
from test_effects_alignment import make_project        # noqa: E402

_app = QApplication.instance() or QApplication([])


class TestNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = make_project(Path(tempfile.mkdtemp()))
        cls.score = build_score(cls.project)
        cls.links = link_notes(cls.score)

    def test_every_note_belongs_to_its_engraving(self):
        self.assertEqual(len(self.links), len(self.score.notes))
        self.assertTrue(all(uid is not None for uid, _ in self.links))
        units = {u.uid: u for u in self.score.units}
        for (pitch, start, *_), (uid, staff) in zip(self.score.notes, self.links):
            self.assertAlmostEqual(units[uid].time, start, delta=0.02)
            self.assertIn(staff, (1, 2))
        self.assertEqual({st for _, st in self.links}, {1, 2})          # melody above, bass below

    def test_moving_a_note_moves_its_chord_and_nothing_else(self):
        p = make_project(Path(tempfile.mkdtemp()))
        uid = self.links[0][0]
        move_units(p, [uid], 0.25)
        before, after = self.score.notes, edited_notes(p, self.score, self.links)
        for (b, a, (u, _)) in zip(before, after, self.links):
            self.assertAlmostEqual(a[1] - b[1], 0.25 if u == uid else 0.0)
            self.assertAlmostEqual(a[2] - b[2], a[1] - b[1])           # the length is kept
        unit = next(u for u in self.score.units if u.uid == uid)
        self.assertAlmostEqual(p.start_of(unit), unit.time + 0.25)      # the animation follows
        move_units(p, [uid], -0.25)
        self.assertNotIn(uid, p.overrides)                               # back where it was: no nudge left

    def test_the_roll_takes_nudges_set_anywhere(self):
        p = make_project(Path(tempfile.mkdtemp()))
        m = RollModel(p, self.score)
        uid = self.links[3][0]
        p.overrides[uid] = -0.1                    # e.g. Tap to Keyframe or the Selection tab
        m.refresh()
        self.assertAlmostEqual(m.start[3], self.score.notes[3][1] - 0.1)

    def test_a_midi_file_of_the_edited_notes(self):
        p = make_project(Path(tempfile.mkdtemp()))
        move_units(p, [self.links[5][0]], 0.3)
        notes = edited_notes(p, self.score, self.links)
        path = Path(tempfile.mkdtemp()) / "out.mid"
        write_midi(path, notes)

        class Toolkit:                             # read it back with the engraver's own MIDI reader
            def renderToMIDI(self):
                return base64.b64encode(path.read_bytes()).decode()
        back = sorted(_Builder._midi_notes(Toolkit()), key=lambda n: (n[1], n[0]))
        want = sorted(notes, key=lambda n: (n[1], n[0]))
        self.assertEqual(len(back), len(want))
        for (p1, s1, e1, _), (p2, s2, e2, _) in zip(back, want):
            self.assertEqual(p1, p2)
            self.assertAlmostEqual(s1, s2, delta=0.002)
            self.assertAlmostEqual(e1, e2, delta=0.002)

    def test_the_synth_moves_only_what_moved(self):
        notes = [(60 + i % 12, 0.3 * i, 0.3 * i + 0.4, 80) for i in range(60)]
        moved = [(p, s + 0.1, e + 0.1, v) if i % 7 == 0 else (p, s, e, v) for i, (p, s, e, v) in enumerate(notes)]
        synth = audio.Synth(notes, 20.0)
        self.assertEqual(synth.move(moved), 9)
        fresh = audio.Synth(moved, 20.0)
        self.assertLess(float(np.abs(synth.mix[:len(fresh.mix)] - fresh.mix).max()), 1e-5)


def press(widget, kind, pos, button=Qt.LeftButton, mods=Qt.NoModifier):
    buttons = Qt.NoButton if kind == QEvent.MouseButtonRelease else button
    e = QMouseEvent(kind, QPointF(pos), widget.mapToGlobal(QPointF(pos)), button, buttons, mods)
    {QEvent.MouseButtonPress: widget.mousePressEvent, QEvent.MouseMove: widget.mouseMoveEvent,
     QEvent.MouseButtonRelease: widget.mouseReleaseEvent}[kind](e)


class TestEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sheet_music_animator import app as appmod
        cls.appmod = appmod
        cls.win = appmod.MainWindow()
        cls.win.resize(1500, 850)
        cls.win.show()

        def accept(dlg):
            dlg.result_value = 4
            return QDialog.Accepted
        p = make_project(Path(tempfile.mkdtemp()))
        with mock.patch.object(appmod.LayoutDialog, "exec", accept):
            cls.win.open_xml_path(p.xml_path)
        cls.win.view_tabs.setCurrentIndex(1)
        _app.processEvents()
        cls.ed = cls.win.midi
        cls.ed.fit()
        _app.processEvents()

    def setUp(self):
        self.win.project.overrides.clear()
        self.win.commit()
        self.ed.set_selection(set())
        self.ed.snap = False

    def centre(self, i):
        return self.ed.roll._rect(i, 0.0).center()

    def test_the_tabs_switch_workspaces_on_one_project(self):
        self.assertEqual(self.win.stack.currentWidget(), self.ed)
        self.assertEqual([self.win.view_tabs.tabText(i) for i in range(2)], ["Score Animation", "MIDI Editor"])
        self.assertIs(self.ed.project, self.win.project)
        self.assertEqual(len(self.ed.model.pitch), len(self.win.score.notes))   # filled in when the score opened
        self.win.view_tabs.setCurrentIndex(0)
        self.assertNotEqual(self.win.stack.currentWidget(), self.ed)
        self.win.view_tabs.setCurrentIndex(1)

    def test_click_ctrl_click_shift_click_and_rectangle(self):
        m, roll = self.ed.model, self.ed.roll
        order = list(m.order)
        a, b = order[0], order[8]
        press(roll, QEvent.MouseButtonPress, self.centre(a))
        press(roll, QEvent.MouseButtonRelease, self.centre(a))
        self.assertEqual(self.ed.selected, set(int(k) for k in m.group_of(a)))      # a note selects its chord
        press(roll, QEvent.MouseButtonPress, self.centre(b), mods=Qt.ControlModifier)
        self.assertTrue(set(int(k) for k in m.group_of(b)) <= self.ed.selected)
        press(roll, QEvent.MouseButtonPress, self.centre(b), mods=Qt.ControlModifier)   # and again: off
        self.assertFalse(set(int(k) for k in m.group_of(b)) & self.ed.selected)
        press(roll, QEvent.MouseButtonPress, self.centre(a))
        press(roll, QEvent.MouseButtonRelease, self.centre(a))
        press(roll, QEvent.MouseButtonPress, self.centre(b), mods=Qt.ShiftModifier)     # a range in time
        lo, hi = sorted((m.start[a], m.start[b]))
        self.assertEqual(self.ed.selected, set(np.nonzero((m.start >= lo - 1e-6) & (m.start <= hi + 1e-6))[0].tolist()))
        # a rectangle over empty space at the very top and down across everything in the first second
        x1 = self.ed.view.x(1.0)
        press(roll, QEvent.MouseButtonPress, QPointF(1, 1))
        press(roll, QEvent.MouseMove, QPointF(x1, roll.height() - 2))
        press(roll, QEvent.MouseButtonRelease, QPointF(x1, roll.height() - 2))
        first = set(np.nonzero(m.start <= 1.0)[0].tolist())
        self.assertTrue(first <= self.ed.selected)

    def test_dragging_retimes_the_chord_in_the_animation_and_can_be_undone(self):
        m, roll, win = self.ed.model, self.ed.roll, self.win
        i = int(m.order[10])
        uid = int(m.uid[i])
        start = m.start[i]
        c = self.centre(i)
        dx = 0.2 * self.ed.view.pps
        press(roll, QEvent.MouseButtonPress, c)
        press(roll, QEvent.MouseMove, QPointF(c.x() + dx, c.y() + 40))        # up or down: ignored
        press(roll, QEvent.MouseButtonRelease, QPointF(c.x() + dx, c.y() + 40))
        self.assertAlmostEqual(win.project.overrides[uid], 0.2, places=3)
        self.assertAlmostEqual(m.start[i], start + 0.2, places=3)
        self.assertEqual(m.pitch[i], win.score.notes[i][0])                   # the pitch never changes
        for k in m.group_of(i):                                                # the chord went along
            self.assertAlmostEqual(m.start[k] - win.score.notes[k][1], 0.2, places=3)
        unit = next(u for u in win.score.units if u.uid == uid)
        self.assertAlmostEqual(win.project.start_of(unit) - unit.time - win.project.settings.offset, 0.2, places=3)
        win.undo()
        self.assertNotIn(uid, win.project.overrides)
        self.assertAlmostEqual(m.start[i], start, places=6)                    # the roll follows undo
        win.redo()
        self.assertAlmostEqual(m.start[i], start + 0.2, places=3)

    def test_a_drag_snaps_to_an_attack(self):
        m, roll = self.ed.model, self.ed.roll
        i = int(m.order[12])
        self.ed.snap = True
        self.ed.attack_times = np.array([m.start[i] + 0.31])
        c = self.centre(i)
        x = c.x() + 0.31 * self.ed.view.pps + 3                                 # 3 px past the attack
        press(roll, QEvent.MouseButtonPress, c)
        press(roll, QEvent.MouseMove, QPointF(x, c.y()))
        press(roll, QEvent.MouseButtonRelease, QPointF(x, c.y()))
        self.assertAlmostEqual(self.win.project.overrides[int(m.uid[i])], 0.31, places=4)
        self.ed.attack_times = np.zeros(0)

    def test_the_built_in_sound_follows_the_edits(self):
        win, m = self.win, self.ed.model
        if win.synth is None:
            self.skipTest("no audio output here")
        i = int(m.order[6])
        before = win.wav_path
        self.ed.set_selection(set(int(k) for k in m.group_of(i)))
        self.ed.move_selection(0.15)
        self.assertAlmostEqual(win.synth.notes[i][1], win.score.notes[i][1] + 0.15, places=6)
        import time
        deadline = time.time() + 20
        while win.wav_path == before and time.time() < deadline:     # written in the background, then swapped in
            _app.processEvents()
            time.sleep(0.02)
        self.assertNotEqual(win.wav_path, before)
        self.assertTrue(Path(win.wav_path).exists())

    def test_notes_cannot_be_added_or_deleted(self):
        n = len(self.ed.model.pitch)
        roll = self.ed.roll
        press(roll, QEvent.MouseButtonPress, QPointF(roll.width() - 3, 3))     # a click on empty space
        press(roll, QEvent.MouseButtonRelease, QPointF(roll.width() - 3, 3))
        self.ed.set_selection({0})
        from PySide6.QtGui import QKeyEvent
        roll.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
        self.assertEqual(len(self.ed.model.pitch), n)
        self.assertEqual(len(self.win.score.notes), n)


if __name__ == "__main__":
    unittest.main()
