"""Things Verovio draws wrong from (old MuseScore) MusicXML exports, found in the Heroic Polonaise, and their fixes.

Run:  python -m unittest discover tests
Each test builds a small score on the fly."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lxml import etree                                 # noqa: E402

from sheet_music_animator import engraver              # noqa: E402

M = "{http://www.music-encoding.org/ns/mei}"
DUR = {"eighth": 1, "quarter": 2, "half": 4, "whole": 8}     # divisions = 2


def note(step, octave, staff=1, voice=1, typ="quarter", alter=None, chord=False, extra="", inner="", hidden=False):
    """A MusicXML note.  `extra` goes after <type> (accidental, stem, beams), `inner` inside <notations>."""
    return (f'<note{" print-object=\"no\"" if hidden else ""}>{"<chord/>" if chord else ""}<pitch><step>{step}</step>'
            f'{f"<alter>{alter}</alter>" if alter else ""}<octave>{octave}</octave></pitch><duration>{DUR[typ]}</duration>'
            f'<voice>{voice}</voice><type>{typ}</type>{extra}<staff>{staff}</staff>'
            f'{f"<notations>{inner}</notations>" if inner else ""}</note>')


def score(measures) -> Path:
    """A piano score (4/4) from the contents of its measures."""
    attrs = ('<attributes><divisions>2</divisions><key><fifths>0</fifths></key><time><beats>4</beats>'
             '<beat-type>4</beat-type></time><staves>2</staves><clef number="1"><sign>G</sign><line>2</line></clef>'
             '<clef number="2"><sign>F</sign><line>4</line></clef></attributes>')
    body = "".join(f'<measure number="{i + 1}">{attrs if i == 0 else ""}{m}</measure>' for i, m in enumerate(measures))
    p = Path(tempfile.mkdtemp()) / "s.musicxml"
    p.write_text('<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1"><part-list><score-part id="P1">'
                 f'<part-name>Piano</part-name></score-part></part-list><part id="P1">{body}</part></score-partwise>',
                 encoding="utf8")
    return p


BACK = '<backup><duration>8</duration></backup>'
LEFT = BACK + "".join(note(s, 3, 2, 5) for s in "CDEF")
BEAMS = ("begin", "continue", "continue", "end")


class TestEngravingFixes(unittest.TestCase):
    def test_a_beamed_run_marked_stemless_gets_its_stems_back(self):
        """MuseScore 1 writes <stem>none</stem> on runs it draws with stems: Verovio drew the beam floating alone."""
        run = "".join(note(s, o, typ="eighth", extra=f'<stem>none</stem><beam number="1">{b}</beam>')
                      for (s, o), b in zip((("C", 4), ("D", 4), ("E", 4), ("F", 4)), BEAMS))
        rest = "".join(note(s, o, typ="eighth", extra=f'<beam number="1">{b}</beam>')
                       for (s, o), b in zip((("G", 4), ("A", 4), ("B", 4), ("C", 5)), BEAMS))
        s = engraver.engrave(score([run + rest + LEFT]), measures_per_line=4)
        right_hand = [u for u in s.units if u.kind == "note" and u.measure == 0 and u.heads and u.heads[0][4] == 0]
        self.assertEqual(len(right_hand), 8)
        self.assertTrue(all(b'class="stem"' in u.svg for u in right_hand))

    def test_the_accidental_of_a_hidden_note_is_not_drawn(self):
        """A trill written out in hidden notes, one of them a G flat: its flat must not appear."""
        right = (note("F", 4, alter=1, extra="<accidental>sharp</accidental>") + note("G", 4) + note("A", 4) + note("B", 4) +
                 BACK + note("G", 4, voice=2, typ="whole", alter=-1, extra="<accidental>flat</accidental>", hidden=True))
        s = engraver.engrave(score([right + LEFT]), measures_per_line=4)
        self.assertEqual(sum(1 for u in s.units if u.kind == "accid"), 1)       # the sharp only

    def test_arpeggios_of_both_hands_are_drawn_per_staff(self):
        """Without a `number` saying they belong together, the arpeggios of the two hands are separate."""
        arp = "<arpeggiate/>"
        right = note("C", 5, inner=arp) + note("E", 5, chord=True, inner=arp) + note("D", 5, typ="half") + note("E", 5)
        left = BACK + note("C", 3, 2, 5, inner=arp) + note("G", 3, 2, 5, chord=True, inner=arp) + \
            note("D", 3, 2, 5, typ="half") + note("E", 3, 2, 5)
        s = engraver.engrave(score([right + left]), measures_per_line=4)
        arps = [u for u in s.units if u.kind == "arpeg"]
        self.assertEqual(len(arps), 2)
        gap = s.measure_infos[0].rect[3]                     # both staves of the measure
        self.assertTrue(all(u.rect[3] < 0.5 * gap for u in arps))

    def test_two_dynamics_at_one_moment_become_one_marking(self):
        d = '<direction placement="below"><direction-type><dynamics><{}/></dynamics></direction-type><staff>2</staff></direction>'
        right = "".join(note(s, 5) for s in "CDEF")
        s = engraver.engrave(score([right + BACK + d.format("p") + d.format("sf") + "".join(note(x, 3, 2, 5) for x in "CDEF")]),
                             measures_per_line=4)
        self.assertEqual([u.label for u in s.units if u.kind == "dynam"], ["p sf"])

    def test_clefs_at_the_start_of_lines_stay_visible_with_cross_staff_notes(self):
        """The right hand reaching into the bass staff in the first measure of a line made the cross-staff clef fix
        put an invisible clef there, and Verovio then left out the clef at the start of that line."""
        measures = [note("C", 5) + note("D", 5) + (note("G", 3, staff=2) if i == 4 else note("E", 5)) + note("F", 5) + LEFT
                    for i in range(12)]
        s = engraver.engrave(score(measures), measures_per_line=4)
        self.assertEqual(len(s.systems), 3)
        self.assertEqual(sum(1 for u in s.units if u.kind == "clef" and u.static), 6)

    def test_a_tie_paired_with_a_far_note_is_reattached(self):
        mei = etree.fromstring(b'''<mei xmlns="http://www.music-encoding.org/ns/mei"><music><body><mdiv><score><section>
            <measure n="1"><staff n="1"><layer n="1"><note xml:id="a" pname="g" oct="3"/><note xml:id="b" pname="g" oct="3"/>
              </layer></staff><tie xml:id="t" startid="#a" endid="#z"/></measure>
            <measure n="2"><staff n="1"><layer n="1"><note xml:id="c" pname="c" oct="4"/></layer></staff></measure>
            <measure n="3"><staff n="1"><layer n="1"><note xml:id="z" pname="g" oct="3"/></layer></staff></measure>
            </section></score></mdiv></body></music></mei>''')
        self.assertTrue(engraver._fix_long_ties(mei))
        self.assertEqual(next(mei.iter(M + "tie")).get("endid"), "#b")

    def test_a_slur_pushed_into_a_loop_is_found(self):
        svg = etree.fromstring(
            '<svg xmlns="http://www.w3.org/2000/svg"><g id="ok" class="slur"><path d="M0,0 C300,-300 1200,-300 1500,0"/></g>'
            '<g id="loop" class="slur"><path d="M4709,60013 C4821,63418 6222,63689 6329,60528"/></g></svg>')
        self.assertEqual(engraver._looping_slurs(svg), {"loop": "below"})


if __name__ == "__main__":
    unittest.main()
