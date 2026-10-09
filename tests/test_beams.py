"""Beam groups that cut across tuplets (four sixteenths, then eight, over three-note tuplets) are drawn the
way the MusicXML groups them, without changing any timing."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheet_music_animator import engraver   # noqa: E402

PITCHES = [("A", 2), ("G", 3), ("E", 3), ("A", 3), ("G", 3), ("E", 4), ("A", 3), ("G", 4), ("G", 3), ("E", 4), ("A", 3), ("G", 4)]
B1 = ["begin", "continue", "continue", "end", "begin", "continue", "continue", "continue", "continue", "continue", "continue", "end"]


def make_xml(clef_change: bool = False) -> str:
    notes = []
    for i, ((step, octave), b1) in enumerate(zip(PITCHES, B1)):
        tup = '<tuplet type="start" bracket="no" show-number="none"/>' if i % 3 == 0 else \
              '<tuplet type="stop"/>' if i % 3 == 2 else ""
        stem = "down" if i < 4 else "up"
        if clef_change and i == 6:    # a clef change in the middle of the beam group
            notes.append('<attributes><clef><sign>G</sign><line>2</line></clef></attributes>')
        notes.append(f'<note><pitch><step>{step}</step><octave>{octave}</octave></pitch><duration>20</duration>'
                     f'<voice>5</voice><type>16th</type><time-modification><actual-notes>3</actual-notes>'
                     f'<normal-notes>2</normal-notes></time-modification><stem>{stem}</stem>'
                     f'<staff>1</staff><beam number="1">{b1}</beam><beam number="2">{b1}</beam>'
                     f'<notations>{tup}</notations></note>')
    return ('<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1"><part-list><score-part id="P1">'
            '<part-name>P</part-name></score-part></part-list><part id="P1"><measure number="1"><attributes>'
            '<divisions>120</divisions><time><beats>2</beats><beat-type>4</beat-type></time>'
            '<clef><sign>F</sign><line>4</line></clef></attributes>' + "".join(notes) + '</measure></part></score-partwise>')


class TestBeams(unittest.TestCase):
    def test_conflict_is_found(self):
        p = Path(tempfile.mkdtemp()) / "b.musicxml"
        p.write_text(make_xml(), encoding="utf8")
        found = engraver.beam_conflicts(p)
        self.assertEqual(list(found), [("1", 1, "5")])
        self.assertEqual(found[("1", 1, "5")], B1)

    def test_beam_groups_follow_the_xml_and_timing_is_unchanged(self):
        for clef_change in (False, True):
            self.check(clef_change)

    def check(self, clef_change):
        p = Path(tempfile.mkdtemp()) / "b.musicxml"
        p.write_text(make_xml(clef_change), encoding="utf8")
        fixed = engraver.engrave(p, measures_per_line=4)
        orig = engraver.beam_conflicts
        engraver.beam_conflicts = lambda path: {}
        try:
            plain = engraver.engrave(p, measures_per_line=4)
        finally:
            engraver.beam_conflicts = orig
        self.assertEqual(sorted(fixed.nominal_notes), sorted(plain.nominal_notes))      # same notes, same times
        beams = lambda s: sum(1 for u in s.units if u.kind == "beam")                   # noqa: E731
        self.assertEqual(beams(fixed), 2)                                               # four + eight, nothing nested
        import verovio
        from lxml import etree
        tk = verovio.toolkit()
        tk.loadFile(str(p))
        root = etree.fromstring(tk.getMEI().encode("utf8"))
        ns = {"m": engraver.MEI_NS}
        self.assertTrue(root.xpath("//m:beam//m:beam", namespaces=ns))                  # Verovio's own import nests them
        self.assertTrue(engraver._regroup_beams(root, engraver.beam_conflicts(p)))
        self.assertFalse(root.xpath("//m:beam//m:beam", namespaces=ns))                 # ...the rebuilt layer does not
        self.assertEqual(len(root.xpath("//m:beam", namespaces=ns)), 2)


if __name__ == "__main__":
    unittest.main()
