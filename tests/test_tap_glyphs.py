"""Tap events carry every element that appears with a note; text glyphs Qt cannot draw get stand-ins."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lxml import etree                                  # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

from sheet_music_animator import engraver, tapmode      # noqa: E402
from sheet_music_animator.build import build_score      # noqa: E402
from test_effects_alignment import make_project         # noqa: E402

_app = QApplication.instance() or QApplication([])
SVG = engraver.SVG_NS


class TestTap(unittest.TestCase):
    def test_a_tap_retimes_everything_that_appears_with_the_note(self):
        score = build_score(make_project(Path(tempfile.mkdtemp())))
        events = tapmode.tap_events(score, "notes")
        kinds = {u.kind for _, units in events for u in units}
        self.assertIn("note", kinds)
        self.assertTrue(kinds - {"note", "chord"}, kinds)          # ledger lines, accents, dynamics, ... come along
        every = {u.uid for _, units in events for u in units}
        timed = [u for u in score.units if not u.static]
        self.assertGreaterEqual(len(every), 0.9 * len(timed))      # nothing timed is left behind


class TestGlyphs(unittest.TestCase):
    def fix(self, xml):
        el = etree.fromstring(f'<g xmlns="{SVG}"><text>{xml}</text></g>')
        engraver._fix_text_glyphs(el)
        return el

    def test_dynamics_in_text_become_letters(self):
        el = self.fix('<tspan font-family="Leipzig" font-size="720px">&#58658;</tspan><tspan>&#160;&#160;risoluto</tspan>')
        t = list(el.iter(f"{{{SVG}}}tspan"))
        self.assertEqual(t[0].text, "f ")    # the no-break spaces survive as a gap Qt draws (an en space)
        self.assertNotIn("font-family", t[0].attrib)
        self.assertEqual(t[1].text, "risoluto")

    def test_metronome_notes_become_symbols_and_nothing_is_left_to_show_as_a_box(self):
        el = self.fix('<tspan font-family="Leipzig">&#60579;</tspan><tspan font-family="Leipzig">&#57344;</tspan>')
        t = list(el.iter(f"{{{SVG}}}tspan"))
        self.assertEqual(t[0].text, "\U0001D15E")                  # a half note
        self.assertEqual(t[0].get("font-family"), engraver.SYMBOL_TOKEN.decode())
        self.assertEqual(t[1].text, "")                            # an unknown private-use glyph is dropped


if __name__ == "__main__":
    unittest.main()
