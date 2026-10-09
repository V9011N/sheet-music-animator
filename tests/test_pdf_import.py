"""Opening a PDF: the check that it is engraved music throughout, the hand-over to an OMR engine, and the
Open drop-down.

Run:  python -m unittest discover tests
The PDFs are made on the fly (a score engraved by Verovio, text, empty staves, tablature, a crooked scan); the
OMR engines are stand-ins that answer like Audiveris and homr on the command line."""
import os
import sys
from collections import Counter
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np                                    # noqa: E402
import verovio                                        # noqa: E402
from lxml import etree                                # noqa: E402
from PySide6.QtCore import QByteArray, QMarginsF, QRectF   # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPageSize, QPainter, QPdfWriter, QPen   # noqa: E402
from PySide6.QtSvg import QSvgRenderer                # noqa: E402
from PySide6.QtWidgets import QApplication            # noqa: E402
from scipy import ndimage                             # noqa: E402

from sheet_music_animator import pdfimport            # noqa: E402
from test_effects_alignment import make_score_xml     # noqa: E402

_app = QApplication.instance() or QApplication([])
SVG = "{http://www.w3.org/2000/svg}"


def music_svg() -> bytes:
    """A page of music engraved by Verovio, as one flat SVG (Qt skips nested ones)."""
    tk = verovio.toolkit()
    tk.setOptions({"pageWidth": 2100, "pageHeight": 2970, "scale": 40})
    tk.loadData(make_score_xml(16))
    root = etree.fromstring(tk.renderToSVG(1).encode())
    inner = next(e for e in root if e.tag == SVG + "svg")
    for d in root.findall(SVG + "defs"):
        inner.insert(0, d)
    inner.set("width", root.get("width"))
    inner.set("height", root.get("height"))
    return etree.tostring(inner)


def make_pdf(path: Path, pages) -> Path:
    w = QPdfWriter(str(path))
    w.setPageSize(QPageSize(QPageSize.A4))
    w.setResolution(150)
    w.setPageMargins(QMarginsF(0, 0, 0, 0))
    p = QPainter(w)
    W, H = w.width(), w.height()
    for i, kind in enumerate(pages):
        if i:
            w.newPage()
        if kind == "music":
            QSvgRenderer(QByteArray(music_svg())).render(p, QRectF(0, 0, W, H))
        elif kind == "text":
            p.setFont(QFont("Sans", 12))
            for k in range(40):
                p.drawText(80, 80 + 28 * k, "Lorem ipsum dolor sit amet, consectetur adipiscing elit " * 2)
        elif kind in ("empty", "tab"):
            p.setPen(QPen(QColor("black"), 1.5))
            n = 6 if kind == "tab" else 5
            for st in range(10):
                for ln in range(n):
                    y = 120 + st * 150 + ln * 12
                    p.drawLine(100, y, W - 100, y)
                if kind == "tab":
                    p.setFont(QFont("Sans", 9))
                    for x in range(150, W - 120, 40):
                        p.drawText(x, 120 + st * 150 + 12 * (x // 40 % 6) + 4, str(x % 10))
        elif kind == "crooked":       # a scan of a music page, put on the glass 1.5 degrees off
            a = pdfimport.render_page(pdfimport._open(make_pdf(path.with_name("straight.pdf"), ["music"])), 0)
            b = np.ascontiguousarray(ndimage.rotate(a, 1.5, reshape=False, order=1, cval=255).astype(np.uint8))
            p.drawImage(QRectF(0, 0, W, H), QImage(b.data, b.shape[1], b.shape[0], b.shape[1], QImage.Format_Grayscale8))
    p.end()
    return path


def musicxml(measures=2, notes=True) -> str:
    note = ('<note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>'
            if notes else '<note><rest/><duration>4</duration><type>whole</type></note>')
    body = "".join(f'<measure number="{i + 1}">'
                   + ('<attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time>'
                      '<clef><sign>G</sign><line>2</line></clef></attributes>' if i == 0 else "")
                   + note + '</measure>' for i in range(measures))
    return ('<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1"><part-list><score-part id="P1">'
            f'<part-name>Piano</part-name></score-part></part-list><part id="P1">{body}</part></score-partwise>')


def with_lines(xml: str, lines) -> str:
    """`xml` with direction lines added: (measure, kind, type, number, staff)."""
    root = etree.fromstring(xml.encode())
    measures = root.find("part").findall("measure")
    for m, kind, typ, number, staff in lines:
        d = etree.SubElement(measures[m - 1], "direction")
        etree.SubElement(etree.SubElement(d, "direction-type"), kind, type=typ, number=number)
        etree.SubElement(d, "staff").text = staff
    return etree.tostring(root).decode()


def pitches(root) -> Counter:
    step = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    return Counter(12 * (int(p.findtext("octave")) + 1) + step[p.findtext("step")] + int(float(p.findtext("alter") or 0))
                   for p in root.iter("pitch"))


def fake_engine(folder: Path, name: str, body: str) -> Path:
    """An executable script standing in for an OMR program."""
    exe = folder / name
    exe.write_text(f"#!{sys.executable}\nimport sys, pathlib\nargs = sys.argv[1:]\n" + textwrap.dedent(body))
    exe.chmod(0o755)
    return exe


def audiveris(xml: str) -> str:
    """A stand-in Audiveris: writes `xml` as the score it read, and the sheets it was asked for next to itself."""
    return f'''
out = pathlib.Path(args[args.index("-output") + 1])
sheets = args[args.index("-sheets") + 1:args.index("--")] if "-sheets" in args else []
(pathlib.Path(sys.argv[0]).parent / "sheets.txt").write_text(" ".join(sheets))
import zipfile
with zipfile.ZipFile(out / "score.mxl", "w") as z:
    z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="score.xml"/></rootfiles></container>')
    z.writestr("score.xml", {xml!r})
'''


class TestPdfCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())

    def pdf(self, name, pages) -> Path:
        return make_pdf(self.dir / name, pages)

    def test_an_engraved_score_is_music_on_every_page(self):
        check = pdfimport.check_pdf(self.pdf("score.pdf", ["music", "music"]))
        self.assertEqual(check.music_pages, [0, 1])
        self.assertEqual(check.report(), "")
        self.assertGreaterEqual(len(check.pages[0].music), 4)

    def test_pages_without_music_are_listed(self):
        check = pdfimport.check_pdf(self.pdf("mixed.pdf", ["text", "music", "blank", "music"]))
        self.assertEqual(check.music_pages, [1, 3])
        self.assertEqual([p.index for p in check.other_pages], [0, 2])
        self.assertIn("Page 1: no staves", check.report())
        self.assertIn("Page 3: blank", check.report())

    def test_files_without_music_are_refused_with_the_reason(self):
        for pages, reason in ((["text", "text"], "No page of"), (["empty"], "empty staves"),
                              (["tab"], "tablature")):
            with self.subTest(pages=pages):
                with self.assertRaisesRegex(pdfimport.PdfError, reason):
                    pdfimport.check_pdf(self.pdf(f"{pages[0]}.pdf", pages))

    def test_a_crooked_scan_is_straightened(self):
        check = pdfimport.check_pdf(self.pdf("crooked.pdf", ["crooked"]))
        self.assertEqual(check.music_pages, [0])
        self.assertAlmostEqual(abs(check.pages[0].skew), 1.5, delta=0.3)

    def test_files_that_cannot_be_read(self):
        good = self.pdf("good.pdf", ["music"])
        (self.dir / "not.pdf").write_text("hello")
        (self.dir / "cut.pdf").write_bytes(good.read_bytes()[:2000])
        for name, reason in (("not.pdf", "not a PDF"), ("cut.pdf", "damaged"), ("missing.pdf", "no file")):
            with self.subTest(name=name):
                with self.assertRaisesRegex(pdfimport.PdfError, reason):
                    pdfimport.check_pdf(self.dir / name)

    def test_beams_lying_on_staff_lines_are_not_a_sixth_line(self):
        page = np.full((400, 1000), 255, np.uint8)
        for ln in range(5):
            page[100 + 10 * ln, 50:950] = 0
        page[140:145, 50:950] = 0           # a beam along the bottom line, across the page
        page[150:155, 600:900] = 0          # and one below the staff, where a sixth line would be
        page[60:100, 100:900:20] = 0        # stems: something written on the staff
        staves = pdfimport.find_staves(page)
        self.assertEqual([(s.lines, round(s.spacing)) for s in staves], [(5, 10)])


class TestRecognition(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.pdf = make_pdf(self.dir / "score.pdf", ["music"])
        self.out = self.dir / "score (from PDF).musicxml"

    def with_engine(self, audiveris=None, homr=None):
        return mock.patch.multiple(pdfimport, _audiveris=lambda: audiveris, _homr=lambda: homr)

    def test_no_engine_says_what_to_install(self):
        with self.with_engine():
            self.assertEqual(pdfimport.engines(), [])
            with self.assertRaisesRegex(pdfimport.OmrError, "Audiveris"):
                pdfimport.recognize(self.pdf, [0], self.out)

    def test_audiveris_reads_the_music_pages(self):
        exe = fake_engine(self.dir, "audiveris", audiveris(musicxml(2)))
        with self.with_engine(audiveris=str(exe)):
            path = pdfimport.recognize(self.pdf, [0, 2], self.out)
        self.assertEqual((self.dir / "sheets.txt").read_text(), "1 3")    # pages are numbered from 1
        self.assertEqual(pdfimport.score_stats(pdfimport.read_musicxml(path)), (1, 2, 2))

    def test_an_engine_that_finds_no_notes_is_an_error(self):
        exe = fake_engine(self.dir, "audiveris", audiveris(musicxml(2, notes=False)))
        with self.with_engine(audiveris=str(exe)), self.assertRaisesRegex(pdfimport.OmrError, "no notes"):
            pdfimport.recognize(self.pdf, [0], self.out)
        self.assertFalse(self.out.exists())

    def test_an_engine_that_fails_shows_its_messages(self):
        exe = fake_engine(self.dir, "audiveris", 'print("Sheet 1: no staff found"); sys.exit(3)\n')
        with self.with_engine(audiveris=str(exe)), self.assertRaisesRegex(pdfimport.OmrError, "(?s)exit code 3.*no staff"):
            pdfimport.recognize(self.pdf, [0], self.out)

    def test_homr_reads_page_by_page_and_the_pages_are_joined(self):
        exe = fake_engine(self.dir, "homr", '''
            img = pathlib.Path(args[0])
            img.with_suffix(".musicxml").write_text(%r)
        ''' % musicxml(3))
        pdf = make_pdf(self.dir / "two.pdf", ["music", "music"])
        with self.with_engine(homr=[str(exe)]):
            root = pdfimport.read_musicxml(pdfimport.recognize(pdf, [0, 1], self.out))
        measures = root.find("part").findall("measure")
        self.assertEqual([m.get("number") for m in measures], [str(i) for i in range(1, 7)])
        self.assertEqual(measures[3].find("print").get("new-page"), "yes")

    def test_homr_failing_on_a_page_names_it(self):
        exe = fake_engine(self.dir, "homr", '''
            img = pathlib.Path(args[0])
            if "002" not in img.name:
                img.with_suffix(".musicxml").write_text(%r)
        ''' % musicxml(1))
        pdf = make_pdf(self.dir / "two.pdf", ["music", "music"])
        with self.with_engine(homr=[str(exe)]), self.assertRaisesRegex(pdfimport.OmrError, "page 2"):
            pdfimport.recognize(pdf, [0, 1], self.out)

    def test_lines_an_engine_never_closes_are_taken_out(self):
        """Audiveris read two 15ma lines into the Heroic Polonaise that never end; Verovio crashes on them."""
        root = etree.fromstring(with_lines(musicxml(4), [
            (1, "octave-shift", "down", "1", "1"),                      # never stopped: out
            (2, "wedge", "crescendo", "1", "2"), (3, "wedge", "stop", "2", "2"),   # numbered loosely: kept
            (2, "octave-shift", "up", "1", "2"), (3, "octave-shift", "stop", "1", "2"),
            (4, "dashes", "stop", "1", "1"),                           # a stop without a start: out
        ]).encode())
        self.assertEqual(pdfimport.tidy(root), 2)
        left = [(e.tag, e.get("type")) for e in root.iter("octave-shift", "wedge", "dashes")]
        self.assertEqual(left, [("wedge", "crescendo"), ("octave-shift", "up"), ("wedge", "stop"),
                                ("octave-shift", "stop")])

    def test_beams_on_every_note_of_a_chord_and_pedals_never_pressed_are_taken_out(self):
        """Audiveris writes both; Verovio then drops chords (4,500 of the Polonaise's 5,600 notes engraved) and
        its MIDI fails (a pedal let go before the first note)."""
        chord = ('<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type>'
                 '<beam number="1">begin</beam></note><note><chord/><pitch><step>E</step><octave>4</octave></pitch>'
                 '<duration>1</duration><type>quarter</type><beam number="1">begin</beam></note>')
        pedal = '<direction><direction-type><pedal type="{}"/></direction-type></direction>'
        xml = musicxml(2).replace('<note>', pedal.format("stop") + chord + pedal.format("start") + '<note>', 1)
        xml = xml.replace('</measure><measure number="2">', pedal.format("stop") + '</measure><measure number="2">')
        root = etree.fromstring(xml.encode())
        self.assertEqual(pdfimport.tidy(root), 2)
        notes = root.find("part/measure").findall("note")
        self.assertEqual([len(n.findall("beam")) for n in notes[:2]], [1, 0])
        self.assertEqual([p.get("type") for p in root.iter("pedal")], ["start", "stop"])

    def test_a_clef_change_written_at_the_start_of_the_measure_goes_back_where_it_is_printed(self):
        """Heroic Polonaise m. 1 as Audiveris 5.11 wrote it: the right hand's change to the bass clef, printed
        before the sixteenths, was put next to the treble clef the measure starts with, so the whole opening was
        drawn in the treble clef, on ledger lines."""
        def note(step, octave, dur, chord=False, rest=False):
            pitch = "<rest/>" if rest else f"<pitch><step>{step}</step><octave>{octave}</octave></pitch>"
            return f'<note>{"<chord/>" if chord else ""}{pitch}<duration>{dur}</duration><staff>1</staff></note>'
        clef = '<clef number="1"><sign>{}</sign><line>{}</line></clef>'
        body = (note("E", 3, 4) + note("E", 4, 4, chord=True) + note("", 0, 2, rest=True)
                + "".join(note(st, 3, 1) for st in "EFFGGA"))
        xml = ('<score-partwise><part-list><score-part id="P1"/></part-list><part id="P1"><measure number="1">'
               f'<attributes><divisions>4</divisions>{clef.format("G", 2)}{clef.format("F", 4)}<staff-details/>'
               f'</attributes>{body}</measure><measure number="2"><attributes>{clef.format("F", 4)}</attributes>'
               f'{note("", 0, 12, rest=True)}</measure></part></score-partwise>')
        root = etree.fromstring(xml.encode())
        self.assertEqual(pdfimport.place_clefs(root), 1)
        m = root.find("part/measure")
        self.assertEqual([c.findtext("sign") for c in m.find("attributes").findall("clef")], ["G"])
        self.assertEqual([e.tag for e in m][3:6], ["note", "attributes", "note"])   # after the rest, before the run
        self.assertEqual(m.findall("attributes")[1].findtext("clef/sign"), "F")

        lone = etree.fromstring(xml.replace(body, note("E", 4, 12)).replace(
            f'<attributes>{clef.format("F", 4)}</attributes>', "").encode())
        pdfimport.place_clefs(lone)                       # nothing to go after: it takes effect in the next measure
        self.assertEqual(lone.findall("part/measure")[1].findtext("attributes/clef/sign"), "F")

    def test_measures_audiveris_could_not_export_are_reported(self):
        log = self.dir / "omr.log"
        log.write_text("WARN  Error visiting Measure{#8} in {Page#3.1}\nWARN  Error visiting Measure{#4} in "
                       "{Page#3.1}\nWARN  Error visiting Measure{#2} in {Page#6.1}\nINFO  fine\n")
        self.assertEqual(pdfimport.dropped_measures(log), {3: [4, 8], 6: [2]})
        exe = fake_engine(self.dir, "audiveris", audiveris(musicxml(2)) +
                          'print("WARN  Error visiting Measure{#3} in {Page#1.1}")\n')
        lost = []
        with self.with_engine(audiveris=str(exe)):
            pdfimport.recognize(self.pdf, [0], self.out, notes=lost)
        self.assertEqual(len(lost), 1)
        self.assertIn("page 1: measure 3", lost[0])

    def test_an_unclosed_8va_from_the_engine_is_tidied_and_opens(self):
        exe = fake_engine(self.dir, "audiveris", audiveris(with_lines(musicxml(3), [(2, "octave-shift", "down", "1", "1")])))
        with self.with_engine(audiveris=str(exe)):
            root = pdfimport.read_musicxml(pdfimport.recognize(self.pdf, [0], self.out))
        self.assertIsNone(root.find(".//octave-shift"))

    def test_a_score_the_engraver_cannot_draw_is_reported_instead_of_crashing(self):
        exe = fake_engine(self.dir, "audiveris", audiveris(with_lines(musicxml(3), [(2, "octave-shift", "down", "1", "1")])))
        with self.with_engine(audiveris=str(exe)), mock.patch.object(pdfimport, "tidy", lambda root: 0), \
                self.assertRaisesRegex(pdfimport.OmrError, "cannot draw"):
            pdfimport.recognize(self.pdf, [0], self.out)       # an unclosed 8va: Verovio crashes, in a child process
        self.assertTrue(self.out.exists())                      # kept, to be fixed in a notation program

    @unittest.skipUnless(pdfimport._audiveris(), "Audiveris is not installed")
    def test_audiveris_reads_an_engraved_page(self):
        """The real thing: a page engraved by Verovio, read back by Audiveris."""
        source = etree.fromstring(make_score_xml(16).encode())
        root = pdfimport.read_musicxml(pdfimport.recognize(self.pdf, [0], self.out))
        a, b = pitches(source), pitches(root)
        self.assertGreater(2 * sum((a & b).values()) / (sum(a.values()) + sum(b.values())), 0.8)

    def test_the_result_is_kept_next_to_the_pdf_without_overwriting(self):
        first = pdfimport.output_path(self.pdf)
        self.assertEqual(first, self.dir / "score (from PDF).musicxml")
        first.write_text("taken")
        self.assertEqual(pdfimport.output_path(self.pdf), self.dir / "score (from PDF 2).musicxml")


class TestOpenMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sheet_music_animator.app import MainWindow
        cls.win = MainWindow()

    def test_open_is_a_drop_down_with_xml_and_pdf(self):
        button = self.win._open_button()
        self.assertEqual(button.text(), "Open")
        self.assertEqual([a.text() for a in self.win.a_open.menu().actions()], ["Open XML…", "Open PDF (EXPERIMENTAL!)"])

    def test_a_pdf_without_music_is_refused_with_a_message(self):
        pdf = make_pdf(Path(tempfile.mkdtemp()) / "letter.pdf", ["text"])
        from sheet_music_animator import app
        with mock.patch.object(app.QMessageBox, "critical") as critical:
            self.assertFalse(self.win.open_pdf_path(str(pdf)))
        title, text = critical.call_args[0][1:3]
        self.assertIn("does not look like sheet music", text)

    def test_without_an_engine_the_user_is_told_what_to_install(self):
        pdf = make_pdf(Path(tempfile.mkdtemp()) / "score.pdf", ["music"])
        from sheet_music_animator import app
        with mock.patch.multiple(pdfimport, _audiveris=lambda: None, _homr=lambda: None), \
                mock.patch.object(app.QMessageBox, "information") as info:
            self.assertFalse(self.win.open_pdf_path(str(pdf)))
        self.assertIn("pip install homr", info.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
