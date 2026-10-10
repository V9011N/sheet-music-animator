"""What the notation programs export that Verovio would draw differently from them (or crash on), and its clean-up
(xmlfix.py, and the engraver's own repairs).  Found by engraving a set of real scores next to their PDFs.

Run:  python -m unittest discover tests
Each test builds a small score on the fly."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lxml import etree                                 # noqa: E402

from sheet_music_animator import engraver, xmlfix      # noqa: E402
from test_engraving_fixes import BACK, LEFT, note, score   # noqa: E402

RIGHT = "".join(note(s, 5) for s in "CDEF")


def direction(inner, staff=1, placement="below", sound=""):
    return (f'<direction placement="{placement}"><direction-type>{inner}</direction-type><staff>{staff}</staff>'
            f'{sound}</direction>')


def cleaned(path):
    root = engraver._read_musicxml(path)
    xmlfix.clean(root)
    return root


class TestLines(unittest.TestCase):
    def test_an_8va_stopped_before_it_is_started_is_put_in_order(self):
        """Etude-tableau op. 39/5 m. 69: the stop of an 8va line is written (in the first voice) before its start
        (in the second): Verovio's MIDI export crashed the program."""
        m = (RIGHT + direction('<octave-shift type="stop" size="8"/>', placement="above") + BACK +
             direction('<octave-shift type="down" size="8"/>', placement="above") +
             "".join(note(s, 4, voice=2) for s in "GABC") + LEFT)
        path = score([m, RIGHT + LEFT])
        root = cleaned(path)
        kinds = [o.get("type") for o in root.iter("octave-shift")]
        self.assertEqual(kinds, ["down", "stop"])                  # the stop now follows its start
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from sheet_music_animator.engraver import engrave; "
                "s = engrave(sys.argv[2], measures_per_line=4); print(sum(u.kind == 'octave' for u in s.units))")
        out = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]), str(path)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        self.assertEqual(out.stdout.strip().splitlines()[-1], "1")

    def test_a_stop_that_ends_nothing_is_taken_out(self):
        m = RIGHT + direction('<wedge type="stop"/>') + LEFT
        root = cleaned(score([m]))
        self.assertEqual(len(list(root.iter("wedge"))), 0)


class TestPedals(unittest.TestCase):
    def test_musescore_4_pedal_marks_are_drawn_as_ped_with_a_line(self):
        """MuseScore 4 writes its "Ped. ____" marks as `resume` / `discontinue`, which Verovio drops."""
        m = (direction('<pedal type="resume" line="yes" sign="yes"/>', 2) + RIGHT + BACK + "".join(note(s, 3, 2, 5) for s in "CDE") +
             direction('<pedal type="discontinue" line="yes" sign="no"/>', 2) + note("F", 3, 2, 5))
        s = engraver.engrave(score([m, RIGHT + LEFT]), measures_per_line=4)
        pedals = [u for u in s.units if u.kind == "pedal"]
        self.assertEqual(len(pedals), 1)

    def test_pedal_marks_go_below_the_lowest_staff(self):
        root = cleaned(score([direction('<pedal type="start"/>', 1) + RIGHT + direction('<pedal type="stop"/>', 1) + LEFT]))
        self.assertEqual({d.findtext("staff") for d in root.iter("direction") if d.find("direction-type/pedal") is not None}, {"2"})

    def test_a_pedal_pressed_while_it_is_down_is_a_change(self):
        """A held pedal pressed again (a second, shorter pedal for the playback) was drawn as brackets stacked on top
        of each other; a missing release then made every later pedal look nested."""
        p = lambda t: direction(f'<pedal type="{t}" line="yes"/>', 2)                           # noqa: E731
        m = p("start") + note("C", 5) + p("start") + note("D", 5) + note("E", 5) + p("stop") + note("F", 5) + LEFT
        root = cleaned(score([m, p("start") + RIGHT + p("stop") + LEFT]))
        self.assertEqual([x.get("type") for x in root.iter("pedal")], ["start", "change", "stop", "start", "stop"])

    def test_pedalling_written_twice_is_drawn_once(self):
        """MuseScore 1 exports its "Ped." signs and its hidden pedal lines both, at the same moments."""
        sign = lambda t: direction(f'<pedal type="{t}"/>', 1)                                   # noqa: E731
        line = lambda t: direction(f'<pedal type="{t}" line="yes"/>', 2)                        # noqa: E731
        m = sign("start") + line("start") + RIGHT + sign("stop") + line("stop") + LEFT
        root = cleaned(score([m]))
        self.assertEqual([x.get("line") for x in root.iter("pedal")], [None, None])

    def test_musescore_3_pedal_lines_are_its_ped_signs(self):
        path = score([direction('<pedal type="start" line="yes"/>', 2) + RIGHT + direction('<pedal type="stop" line="yes"/>', 2) + LEFT])
        text = path.read_text(encoding="utf8").replace(
            "<part-list>", "<identification><encoding><software>MuseScore 3.6.2</software></encoding></identification><part-list>")
        path.write_text(text, encoding="utf8")
        self.assertEqual([x.get("line") for x in cleaned(path).iter("pedal")], [None, None])


class TestHiddenMarks(unittest.TestCase):
    def test_hidden_words_are_not_drawn_but_their_tempo_stays(self):
        slow = direction('<words print-object="no">rit.</words>', placement="above", sound='<sound tempo="30"/>')
        s = engraver.engrave(score([RIGHT + LEFT, slow + RIGHT + LEFT]), measures_per_line=4)
        self.assertFalse(any(b"rit." in u.svg for u in s.units))
        self.assertAlmostEqual(s.measures[1][0], 2.0, places=2)   # the first measure at the default 120
        self.assertGreater(s.duration - s.measures[1][0], 7.0)    # four quarters at 30

    def test_a_crowd_of_metronome_marks_is_a_rubato_for_the_playback(self):
        """A written-out rubato: a metronome mark every beat or bar (Polonaise-fantaisie); none is printed."""
        mm = lambda bpm: direction(f'<metronome><beat-unit>quarter</beat-unit><per-minute>{bpm}</per-minute></metronome>',
                                   placement="above", sound=f'<sound tempo="{bpm}"/>')                # noqa: E731
        s = engraver.engrave(score([mm(60) + RIGHT + LEFT, mm(80) + RIGHT + LEFT, mm(100) + RIGHT + LEFT]),
                             measures_per_line=4)
        self.assertEqual([u for u in s.units if u.kind == "tempo" and b"= " in u.svg], [])
        self.assertAlmostEqual(s.measures[1][0], 4.0, places=2)   # 60 bpm: the tempo still plays

    def test_a_single_metronome_mark_is_printed(self):
        mm = direction('<metronome><beat-unit>quarter</beat-unit><per-minute>126</per-minute></metronome>',
                       placement="above", sound='<sound tempo="126"/>')
        root = cleaned(score([mm + RIGHT + LEFT, RIGHT + LEFT, RIGHT + LEFT]))
        self.assertEqual(len(list(root.iter("metronome"))), 1)

    def test_a_bare_number_is_a_tempo_for_the_playback(self):
        root = cleaned(score([direction("<words>84</words>", placement="above", sound='<sound tempo="84"/>') + RIGHT + LEFT]))
        self.assertEqual(len(list(root.iter("words"))), 0)
        self.assertEqual([s.get("tempo") for s in root.iter("sound")], ["84"])

    def test_placeholder_dynamics_go_and_smufl_names_become_letters(self):
        d = lambda inner: direction(f"<dynamics><other-dynamics>{inner}</other-dynamics></dynamics>")   # noqa: E731
        root = cleaned(score([d("other-dynamics") + d("<sym>dynamicMezzo</sym><sym>dynamicPiano</sym>") + RIGHT + LEFT]))
        self.assertEqual([e.text for e in root.iter("other-dynamics")], ["mp"])

    def test_one_instrument_is_not_named_again_on_every_line(self):
        path = score([RIGHT + LEFT])
        path.write_text(path.read_text(encoding="utf8").replace(
            "<part-name>Piano</part-name>", "<part-name>Piano</part-name><part-abbreviation>Pno.</part-abbreviation>"),
            encoding="utf8")
        self.assertIsNone(cleaned(path).find("part-list/score-part/part-abbreviation"))


class TestDynamics(unittest.TestCase):
    def test_which_of_the_dynamics_at_one_moment_are_printed(self):
        """What the PDFs of the exports print: a level an accent implies is hidden (f + fz), a softer one is not
        (p + sf), and of two levels the last written."""
        m = engraver._merged_dynamic
        self.assertEqual(m(["f", "fz"]), "fz")
        self.assertEqual(m(["fff", "fz"]), "fz")
        self.assertEqual(m(["p", "sf"]), "p sf")
        self.assertEqual(m(["mp", "mf"]), "mf")

    def test_spaces_qt_would_drop_and_music_font_text(self):
        svg = etree.fromstring(
            '<svg xmlns="http://www.w3.org/2000/svg"><text><tspan font-family="Leipzig"></tspan>'
            '<tspan>cresc.</tspan></text><text><tspan></tspan></text>'
            '<text><tspan></tspan><tspan> dolciss.</tspan></text></svg>')
        engraver._fix_text_glyphs(svg)
        texts = ["|".join(t.text for t in tx) for tx in svg]
        self.assertEqual(texts, ["pp |cresc.", "3", "pp |dolciss."])


class TestLayout(unittest.TestCase):
    def test_an_empty_third_staff_is_left_out_of_its_lines(self):
        """Jeux d'eau, Scriabin's 5th sonata: a third staff used a few times; the lines where it only rests show the
        usual two staves."""
        path = score([RIGHT + LEFT + BACK + note("C", 2, 3, 9, typ="whole") if i == 4 else
                      RIGHT + LEFT + '<backup><duration>8</duration></backup><note><rest/><duration>8</duration>'
                      '<voice>9</voice><staff>3</staff></note>' for i in range(8)])
        text = path.read_text(encoding="utf8").replace("<staves>2</staves>", "<staves>3</staves>").replace(
            '<line>4</line></clef></attributes>', '<line>4</line></clef><clef number="3"><sign>F</sign><line>4</line></clef></attributes>')
        path.write_text(text, encoding="utf8")
        s = engraver.engrave(path, measures_per_line=4)
        self.assertEqual(len(s.systems), 2)
        heights = [m.rect[3] for m in s.measure_infos]
        self.assertLess(heights[0], 0.8 * heights[4])             # the first line: two staves; the second: three

    def test_a_resting_upper_staff_is_left_out_and_the_line_keeps_its_brace(self):
        """Ondine: the piano on three staves, the top one silent for most lines.  Those lines show the two staves
        that play, close together under a brace (an invisible staff kept its room, and Verovio drew no brace)."""
        rest3 = '<note><rest/><duration>8</duration><voice>1</voice><staff>1</staff></note>'
        mid = "".join(note(s, 4, 2, 5) for s in "CDEF")
        low = '<backup><duration>8</duration></backup>' + "".join(note(s, 3, 3, 9) for s in "CDEF")
        path = score([(RIGHT if i >= 4 else rest3) + BACK + mid + low for i in range(8)])
        text = path.read_text(encoding="utf8").replace("<staves>2</staves>", "<staves>3</staves>").replace(
            '<clef number="2"><sign>F</sign><line>4</line></clef>',
            '<clef number="2"><sign>G</sign><line>2</line></clef><clef number="3"><sign>F</sign><line>4</line></clef>')
        path.write_text(text, encoding="utf8")
        s = engraver.engrave(path, measures_per_line=4)
        heights = [m.rect[3] for m in s.measure_infos]
        self.assertLess(heights[0], 0.8 * heights[4])             # line 1: two staves, line 2: three
        braces = [u for u in s.layers if b'class="grpSym"' in u.svg] + [u for u in s.units if u.kind == "grpSym"]
        self.assertEqual(len(braces), 2)                           # one brace on each line

    def test_a_slur_flying_off_the_page_is_found(self):
        svg = etree.fromstring(
            '<svg xmlns="http://www.w3.org/2000/svg"><g id="ok" class="slur"><path d="M0,0 C300,-300 1200,-300 1500,0"/></g>'
            '<g id="wild" class="tie"><path d="M10,10 C-165096884,930752657 331636216,1020623867 900,20"/></g></svg>')
        self.assertEqual(engraver._wild_curves(svg, 20000, 50000), {"wild"})


if __name__ == "__main__":
    unittest.main()
