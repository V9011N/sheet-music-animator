"""Engraving with MuseScore (msengraver.py): the score laid out by the notation program, split into timed units.

Run:  python -m unittest discover tests
The end-to-end tests need MuseScore installed (they are skipped otherwise)."""
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lxml import etree                                 # noqa: E402

from sheet_music_animator import msengraver            # noqa: E402
from sheet_music_animator.build import build_score     # noqa: E402
from sheet_music_animator.project import Project       # noqa: E402
from test_engraving_fixes import BACK, LEFT, note, score   # noqa: E402

HAVE_MUSESCORE = msengraver.find_musescore() is not None
RIGHT = "".join(note(s, 5) for s in "CDEF")


class TestPieces(unittest.TestCase):
    def test_a_word_drawn_as_several_glyphs_is_one_element(self):
        mk = lambda cls, x: msengraver._El(cls, etree.Element("path"), (x, 0, x + 50, 60), 0)   # noqa: E731
        els = msengraver._merge([mk("Tempo", 0), mk("Tempo", 60), mk("Tempo", 120), mk("Note", 200), mk("Note", 260)], 83.3)
        self.assertEqual([e.cls for e in els], ["Tempo", "Note", "Note"])
        self.assertEqual(els[0].box, (0, 0, 170, 60))
        self.assertEqual(len(els[0].xml), 3)

    def test_line_breaks_are_written_after_the_measure_before_each_line(self):
        sc = etree.fromstring("<Score><Staff id='1'>" + "".join(
            f"<Measure><eid>{i}</eid>{'<LayoutBreak><subtype>line</subtype></LayoutBreak>' if i == 1 else ''}</Measure>"
            for i in range(6)) + "</Staff></Score>")
        msengraver._set_breaks(sc, [0, 2, 4], one_line=False)
        breaks = [i for i, m in enumerate(sc.find("Staff").findall("Measure")) if m.find("LayoutBreak") is not None]
        self.assertEqual(breaks, [1, 3])                       # lines start at measures 2 and 4 (0-based)

    def test_coordinates_past_a_million_are_read(self):
        """Mephisto on one line: MuseScore writes x = 1.06221e+06 -- read as 1.06221, its stems and beams went to
        the start of the line."""
        from sheet_music_animator.engraver import BoxCalculator
        el = etree.fromstring('<polyline points="1.06221e+06,7888.66 1.06238e+06,7888.66" stroke-width="2"/>')
        box = BoxCalculator({}).box(el)
        self.assertAlmostEqual(box[0], 1062210 - 2, places=0)
        self.assertAlmostEqual(box[2], 1062380 + 2, places=0)

    def test_positions_are_read_in_svg_units(self):
        f = Path(tempfile.mkdtemp()) / "s.spos"
        f.write_text('<score><elements><element id="0" x="1200" y="2400" sx="120" sy="1200" page="1"/></elements>'
                     '<events><event elid="0" position="1500"/><event elid="0" position="9000"/></events></score>')
        boxes, events = msengraver._positions(f)
        self.assertEqual(boxes["0"], (1, 100.0, 200.0, 110.0, 300.0))
        self.assertEqual(msengraver._first_times(events), {"0": 1.5})    # a repeat: the first time it is played


@unittest.skipUnless(HAVE_MUSESCORE, "MuseScore is not installed")
class TestEngraving(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dyn = '<direction placement="below"><direction-type><dynamics><ff/></dynamics></direction-type><staff>1</staff></direction>'
        measures = [(dyn if i == 2 else "") + RIGHT + LEFT for i in range(8)]
        cls.path = score(measures)
        p = Project(xml_path=str(cls.path))
        p.settings.engraver = "musescore"
        p.settings.measures_per_line = 4
        cls.project = p
        cls.score = build_score(p)

    def test_the_score_is_laid_out_by_musescore(self):
        self.assertEqual(self.project.settings.engraver, "musescore")
        self.assertEqual(len(self.score.systems), 2)            # four measures per line, as asked
        self.assertEqual(self.score.line_starts, [0, 4])

    def test_every_note_is_a_timed_unit_with_its_pitch(self):
        heads = [(u.time, h[6]) for u in self.score.units if u.kind == "note" for h in u.heads]
        self.assertEqual(len(heads), 8 * 8)
        right = sorted(t for t, p in heads if p >= 72)
        self.assertEqual(len(right), 32)
        self.assertAlmostEqual(right[1] - right[0], 0.5, places=2)      # quarter notes at 120
        self.assertEqual({p for _, p in heads if p >= 72}, {72, 74, 76, 77})

    def test_stems_appear_with_their_notes_and_the_dynamic_is_read(self):
        notes = sorted({round(u.time, 3) for u in self.score.units if u.kind == "note"})
        stems = {round(u.time, 3) for u in self.score.units if u.kind == "stem"}
        self.assertTrue(stems <= set(notes))
        dyn = [u for u in self.score.units if u.kind == "dynam"]
        self.assertEqual([u.label for u in dyn], ["ff"])
        self.assertAlmostEqual(dyn[0].time, 4.0, places=2)                 # the third measure

    def test_the_playback_is_musescores(self):
        self.assertEqual(len(self.score.notes), 64)
        self.assertAlmostEqual(self.score.duration, 16.0, delta=0.1)

    def test_as_printed_and_one_line(self):
        p = Project(xml_path=str(self.path))
        p.settings.engraver = "musescore"
        p.settings.measures_per_line = 0
        one = build_score(p)
        self.assertEqual(len(one.systems), 1)
        self.assertLess(one.width, 200000)                       # the music's width, not the page's

    def test_one_line_starts_a_new_system_where_the_printed_staves_change(self):
        """Ondine on one line: the top staff, hidden in the printed lines where it rests, was drawn with rests all
        along the line.  The line is now made of systems side by side, a new one (brace, clefs, key) wherever the
        printed score shows other staves, each staff at one height all along."""
        low = '<backup><duration>8</duration></backup>'
        rest3 = low + '<note><rest/><duration>8</duration><voice>9</voice><staff>3</staff></note>'
        play3 = low + note("C", 2, 3, 9, typ="whole")
        measures = [(('<print new-system="yes"/>' if i == 4 else "") + RIGHT + LEFT + (play3 if i >= 4 else rest3))
                    for i in range(8)]
        path = score(measures)
        text = path.read_text(encoding="utf8").replace("<staves>2</staves>", "<staves>3</staves>").replace(
            '<line>4</line></clef></attributes>',
            '<line>4</line></clef><clef number="3"><sign>F</sign><line>4</line></clef></attributes>')
        path.write_text(text, encoding="utf8")
        self.assertEqual(msengraver._visibility_runs(path, None), [0, 4])
        p = Project(xml_path=str(path))
        p.settings.engraver = "musescore"
        p.settings.measures_per_line = 0
        s = build_score(p)
        self.assertEqual(len(s.systems), 1)                        # one line for the camera...
        braces = sum(layer.svg.count(b'class="Bracket"') for layer in s.layers)
        self.assertEqual(braces, 2)                                # ...made of two systems
        tops = [round(m.rect[1]) for m in s.measure_infos]
        heights = [round(m.rect[3]) for m in s.measure_infos]
        self.assertEqual(len(set(tops)), 1)                        # the right hand at one height
        self.assertLess(heights[0], 0.8 * heights[4])              # two staves, then three
        self.assertTrue(all(a.rect[0] < b.rect[0] for a, b in zip(s.measure_infos, s.measure_infos[1:])))

    def test_one_line_is_not_stretched_by_a_frame_inside_the_music(self):
        """Scriabin's 5th sonata (.mscz): a text frame after m. 24 started a new line, and MuseScore stretched the
        line before it over the whole page made for one line -- millions of units, half the notes lost."""
        folder = Path(tempfile.mkdtemp())
        out = folder / "framed.mscz"
        msengraver._run(msengraver.find_musescore(), ["-f", "-o", str(out), str(self.path)], done=out.exists)
        with zipfile.ZipFile(out) as z:
            name = msengraver._score_files(z)[0]
            files = {n: z.read(n) for n in z.namelist()}
        sc = etree.fromstring(files[name])
        staff = next(s for s in sc.find("Score").findall("Staff") if s.find("Measure") is not None)
        frame = etree.fromstring("<VBox><height>10</height><Text><style>title</style><text>Epigraph</text></Text></VBox>")
        staff.findall("Measure")[3].addnext(frame)
        files[name] = etree.tostring(sc)
        with zipfile.ZipFile(out, "w") as z:
            for n, d in files.items():
                z.writestr(n, d)
        p = Project(xml_path=str(out))
        p.settings.measures_per_line = 0
        s = build_score(p)
        self.assertEqual(len(s.systems), 1)
        widths = [m.rect[2] for m in s.measure_infos]
        self.assertLess(max(widths), 3 * min(widths))              # every measure at its natural width
        self.assertEqual(len(s.notes), 64)

    def test_a_mscz_keeps_its_own_layout(self):
        """A score saved by MuseScore: the breaks it has are the lines."""
        folder = Path(tempfile.mkdtemp())
        out = folder / "s.mscz"
        code, err = msengraver._run(msengraver.find_musescore(), ["-o", str(out), str(self.path)])
        self.assertTrue(out.exists(), err)
        with zipfile.ZipFile(out) as z:
            name = msengraver._score_files(z)[0]
            sc = etree.fromstring(z.read(name))
            files = {n: z.read(n) for n in z.namelist()}
        msengraver._set_breaks(sc.find("Score"), [0, 3, 6], one_line=False)
        files[name] = etree.tostring(sc)
        with zipfile.ZipFile(out, "w") as z:
            for n, d in files.items():
                z.writestr(n, d)
        p = Project(xml_path=str(out))
        p.settings.measures_per_line = -1                         # as printed
        s = build_score(p)
        self.assertEqual(s.line_starts, [0, 3, 6])


if __name__ == "__main__":
    unittest.main()
