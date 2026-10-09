"""Sheet music from a PDF.

Two steps, both of which can fail in ways the user has to hear about:

* `check_pdf` makes sure the file is engraved music throughout.  Every page is rendered (Qt's own PDF engine) and
  searched for staves: five evenly spaced, thin, long horizontal lines with something written on them.  It
  reports files that cannot be read, pages without music (a title page, text, a blank page, a scan too faint or
  crooked to read), blank staff paper and tablature.
* `recognize` has an optical music recognition (OMR) engine installed on the computer read the music pages
  into MusicXML: Audiveris (https://audiveris.github.io, best for engraved scores) or homr
  (`pip install homr`, one page at a time).  Its result is checked again: a file without notes is an error.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from lxml import etree
from scipy import ndimage

RENDER_DPI = 150          # pages are searched for staves at this resolution...
OMR_DPI = 300             # ...and handed to an engine that reads images at this one
LINE_MIN = 0.25           # a staff line runs across at least this fraction of the page
LINE_GAP = 5              # pixels of a staff line that may be missing (a scan) and still count as one line
SPACING_MIN = 3.0         # pixels between staff lines, at RENDER_DPI: anything closer is too small to read
SPACING_SLACK = 0.2       # the gaps between the lines of a staff differ by at most this fraction
THICK_MAX = 0.35          # a sixth staff line is at most this fraction of the spacing thick (a beam is thicker)
INK_MIN = 0.06            # a staff with something written on it: this fraction of its columns has ink off the lines
BLANK_INK = 0.002         # a page with less ink than this is blank
SKEW_MAX = 3.0            # degrees: a crooked scan is straightened by up to this much
SKEW_STEP = 0.25


class PdfError(Exception):
    """The PDF cannot be used; the message is meant for the user."""


class OmrError(Exception):
    """The music could not be read from the PDF; the message is meant for the user."""


# ------------------------------------------------------------------------------------------------ the check
@dataclass
class Staff:
    top: float
    bottom: float
    spacing: float
    x0: int
    x1: int
    lines: int = 5            # 6: tablature
    ink: float = 0.0          # fraction of its columns with something written on them


@dataclass
class PageCheck:
    index: int                                   # 0-based
    staves: list[Staff] = field(default_factory=list)
    skew: float = 0.0                            # degrees the page had to be straightened
    ink: float = 1.0                             # fraction of the page that is not paper

    @property
    def music(self) -> list[Staff]:
        return [s for s in self.staves if s.lines == 5 and s.ink >= INK_MIN]

    @property
    def empty(self) -> list[Staff]:
        return [s for s in self.staves if s.lines == 5 and s.ink < INK_MIN]

    @property
    def tab(self) -> list[Staff]:
        return [s for s in self.staves if s.lines == 6]

    def problem(self) -> str:
        """Why the page is not engraved music ("" when it is)."""
        if self.music:
            return ""
        if self.tab:
            return "tablature only (six-line staves)"
        if self.empty:
            return "empty staves, nothing written on them"
        if self.ink < BLANK_INK:
            return "blank"
        return "no staves (a title or text page, a picture, or a scan too faint or crooked to read)"


@dataclass
class PdfCheck:
    path: str
    pages: list[PageCheck]

    @property
    def music_pages(self) -> list[int]:
        return [p.index for p in self.pages if not p.problem()]

    @property
    def other_pages(self) -> list[PageCheck]:
        return [p for p in self.pages if p.problem()]

    def report(self) -> str:
        """The pages without music, one line each."""
        return "\n".join(f"Page {p.index + 1}: {p.problem()}" for p in self.other_pages)


def _open(path):
    from PySide6.QtPdf import QPdfDocument
    p = Path(path)
    if not p.is_file():
        raise PdfError(f"There is no file at {p}.")
    with open(p, "rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        raise PdfError(f"{p.name} is not a PDF file.")
    doc = QPdfDocument()
    err = doc.load(str(p))
    E = QPdfDocument.Error
    if err in (E.IncorrectPassword, E.UnsupportedSecurityScheme):
        raise PdfError(f"{p.name} is protected by a password. Save an unprotected copy and open that.")
    if err != E.None_ or doc.status() != QPdfDocument.Status.Ready:
        raise PdfError(f"{p.name} is damaged or not a PDF the reader understands.")
    if doc.pageCount() < 1:
        raise PdfError(f"{p.name} has no pages.")
    return doc


def render_page(doc, i: int, dpi: int = RENDER_DPI) -> np.ndarray:
    """Page i as a grey image (0 = black), on white."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage, QPainter
    pt = doc.pagePointSize(i)
    w, h = max(int(pt.width() * dpi / 72), 1), max(int(pt.height() * dpi / 72), 1)
    img = doc.render(i, QSize(w, h))
    out = QImage(w, h, QImage.Format_Grayscale8)
    out.fill(255)
    painter = QPainter(out)
    painter.drawImage(0, 0, img)
    painter.end()
    return np.frombuffer(out.constBits(), np.uint8).reshape(h, out.bytesPerLine())[:, :w].copy()


def _threshold(gray: np.ndarray) -> int:
    """Otsu's threshold between ink (at or below it) and paper, never above the middle grey of a clean print."""
    hist = np.bincount(gray.ravel(), minlength=256).astype(float)
    p = hist / hist.sum()
    w = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    between = (mu[-1] * w - mu) ** 2 / np.maximum(w * (1 - w), 1e-12)
    return int(min(np.argmax(between), 160))


def _lines(dark: np.ndarray):
    """Long horizontal lines: (centre row, thickness, x0, x1)."""
    h, w = dark.shape
    closed = ndimage.binary_closing(dark, structure=np.ones((1, LINE_GAP), bool))
    pad = np.zeros((h, 1), bool)
    d = np.diff(np.hstack([pad, closed, pad]).astype(np.int8), axis=1)
    run, x0 = np.zeros(h, int), np.zeros(h, int)
    for r in np.flatnonzero(closed.sum(axis=1) >= LINE_MIN * w):
        s, e = np.flatnonzero(d[r] == 1), np.flatnonzero(d[r] == -1)
        k = int(np.argmax(e - s))
        run[r], x0[r] = e[k] - s[k], s[k]
    rows = np.flatnonzero(run >= LINE_MIN * w)
    out = []
    for grp in np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1):
        if len(grp):
            k = grp[np.argmax(run[grp])]
            out.append((float(grp.mean()), len(grp), int(x0[k]), int(x0[k] + run[k])))
    return out


def find_staves(gray: np.ndarray) -> list[Staff]:
    """The staves on a page image."""
    h = gray.shape[0]
    dark = gray <= _threshold(gray)
    lines = _lines(dark)
    staves = []
    i = 0
    while i + 5 <= len(lines):
        group = lines[i:i + 5]
        c = np.array([ln[0] for ln in group])
        gaps = np.diff(c)
        s = float(np.median(gaps))
        xa, xb = max(ln[2] for ln in group), min(ln[3] for ln in group)
        thick = np.array([ln[1] for ln in group])
        slack = max(1.5, SPACING_SLACK * s) + (thick[:-1] + thick[1:] - 2) / 2   # a beam lying on a line moves its centre
        ok = (SPACING_MIN <= s <= 0.05 * h and np.all(np.abs(gaps - s) <= slack)
              and xb - xa > 0.8 * min(ln[3] - ln[2] for ln in group))
        if not ok:
            i += 1
            continue
        n = 5
        if i + 5 < len(lines):      # a sixth line like the others: tablature
            nx = lines[i + 5]
            if (abs(nx[0] - c[-1] - s) <= max(1.5, SPACING_SLACK * s) and nx[1] <= max(3, THICK_MAX * s)
                    and min(nx[3], xb) - max(nx[2], xa) > 0.9 * (xb - xa)):
                n = 6
        top, bottom = c[0], lines[i + n - 1][0]
        band = dark[int(top):int(np.ceil(bottom)) + 1, xa:xb].copy()
        for ln in lines[i:i + n]:
            r0 = int(round(ln[0] - top - ln[1] / 2)) - 1
            band[max(r0, 0):r0 + ln[1] + 3] = False
        ink = float(band.any(axis=0).mean()) if band.size else 0.0
        staves.append(Staff(float(top), float(bottom), s, xa, xb, n, ink))
        i += n
    return staves


def _straightened(gray: np.ndarray):
    """The page turned by the angle that makes its rows sharpest (a crooked scan), and that angle."""
    small = gray[::2, ::2] <= _threshold(gray)
    best, best_a = -1.0, 0.0
    for a in np.arange(-SKEW_MAX, SKEW_MAX + 1e-9, SKEW_STEP):
        rows = ndimage.rotate(small.astype(np.float32), a, reshape=False, order=0).sum(axis=1)
        sharp = float(np.var(np.diff(rows)))
        if sharp > best:
            best, best_a = sharp, float(a)
    if best_a == 0.0:
        return gray, 0.0
    return ndimage.rotate(gray, best_a, reshape=False, order=1, cval=255), best_a


def check_page(gray: np.ndarray, index: int = 0) -> PageCheck:
    ink = float((gray <= _threshold(gray)).mean())
    staves = find_staves(gray)
    if not staves and ink >= BLANK_INK:
        turned, angle = _straightened(gray)
        if angle:
            staves = find_staves(turned)
            return PageCheck(index, staves, angle if staves else 0.0, ink)
    return PageCheck(index, staves, 0.0, ink)


def check_pdf(path, progress=None) -> PdfCheck:
    """Check every page.  Raises PdfError when the file cannot be read or holds no music at all; pages
    without music are listed in the result (`other_pages`, `report`) for the caller to decide about."""
    say = progress or (lambda f, s="": True)
    doc = _open(path)
    n = doc.pageCount()
    pages = []
    for i in range(n):
        if say(i / n, f"Looking for music on page {i + 1} of {n}…") is False:
            raise PdfError("Cancelled.")
        pages.append(check_page(render_page(doc, i), i))
    say(1.0, "")
    check = PdfCheck(str(path), pages)
    if not check.music_pages:
        name = Path(path).name
        if n == 1:
            raise PdfError(f"{name} does not look like sheet music: {pages[0].problem()}.")
        raise PdfError(f"No page of {name} looks like sheet music:\n\n{check.report()}")
    return check


# ------------------------------------------------------------------------------------------------ recognition
def _audiveris() -> str | None:
    env = os.environ.get("AUDIVERIS")
    if env and Path(env).is_file():
        return env
    for name in ("audiveris", "Audiveris"):
        found = shutil.which(name)
        if found:
            return found
    for p in ("/opt/audiveris/bin/Audiveris", "/usr/bin/Audiveris",
              "/Applications/Audiveris.app/Contents/MacOS/Audiveris",
              r"C:\Program Files\Audiveris\Audiveris.exe", r"C:\Program Files\Audiveris\bin\Audiveris.bat"):
        if Path(p).is_file():
            return p
    return None


def _homr() -> list[str] | None:
    found = shutil.which("homr")
    if found:
        return [found]
    try:
        import importlib.util
        if importlib.util.find_spec("homr") is not None:
            return [sys.executable, "-c", "import sys; from homr.main import main; sys.exit(main())"]
    except (ImportError, ValueError):
        pass
    return None


def engines() -> list[str]:
    """The OMR engines found on this computer, best first."""
    return [name for name, found in (("Audiveris", _audiveris()), ("homr", _homr())) if found]


NO_ENGINE = ("Reading music from a PDF needs an optical music recognition program, and none was found on this "
             "computer.\n\nInstall one of:\n"
             "• Audiveris (free, best for engraved scores): https://audiveris.github.io — or set the AUDIVERIS "
             "environment variable to its program file.\n"
             "• homr: pip install homr\n\nThen open the PDF again.")


def _run(cmd, say, frac0, frac1, text, log: Path, env=None) -> int:
    """Run an engine, keeping the progress dialog alive; returns its exit code."""
    with open(log, "ab") as out:
        proc = subprocess.Popen([str(c) for c in cmd], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env={**os.environ, **env} if env else None)
        t0 = time.monotonic()
        while proc.poll() is None:
            f = frac0 + (frac1 - frac0) * (1 - np.exp(-(time.monotonic() - t0) / 60.0))
            if say(f, text) is False:
                proc.kill()
                proc.wait()
                raise OmrError("Cancelled.")
            time.sleep(0.05)
    return proc.returncode


def _tail(log: Path, n: int = 12) -> str:
    try:
        lines = log.read_text("utf8", "replace").strip().splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def read_musicxml(path) -> etree._Element:
    """The score element of a .musicxml/.xml or compressed .mxl file."""
    path = Path(path)
    data = path.read_bytes()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = None
            if "META-INF/container.xml" in z.namelist():
                c = etree.fromstring(z.read("META-INF/container.xml"))
                root = next((e for e in c.iter() if e.tag.endswith("rootfile")), None)
                name = root.get("full-path") if root is not None else None
            if name is None:
                name = next(n for n in z.namelist() if n.endswith((".xml", ".musicxml")) and not n.startswith("META"))
            data = z.read(name)
    return etree.fromstring(data, etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True))


def score_stats(root) -> tuple[int, int, int]:
    """(parts, measures of the first part, notes that are not rests)."""
    parts = root.findall("part")
    measures = len(parts[0].findall("measure")) if parts else 0
    notes = sum(1 for n in root.iter("note") if n.find("rest") is None)
    return len(parts), measures, notes


def merge_scores(roots: list) -> etree._Element:
    """One score from several read separately (one per page): the measures of every part, one after the other,
    numbered anew."""
    base = roots[0]
    parts = base.findall("part")
    for k, other in enumerate(roots[1:], start=2):
        more = other.findall("part")
        if len(more) != len(parts):
            raise OmrError(f"Page {k} was read with {len(more)} instrument(s) and the first page with "
                           f"{len(parts)}; the pages could not be put together.")
        for dst, src in zip(parts, more):
            first = True
            for m in src.findall("measure"):
                if first:      # a new page: a new line
                    pr = m.find("print")
                    if pr is None:
                        pr = etree.Element("print")
                        m.insert(0, pr)
                    pr.set("new-page", "yes")
                    first = False
                dst.append(m)
    for p in parts:
        for i, m in enumerate(p.findall("measure"), start=1):
            m.set("number", str(i))
    return base


SPANNERS = ("octave-shift", "wedge", "dashes", "bracket")


def tidy(root) -> int:
    """Take out what an OMR engine writes that the engraver cannot take: lines it starts and never stops (8va,
    hairpins, dashes, brackets: misreads, and an unclosed 8va line crashes the engraver), pedals let go that were
    never pressed (Verovio's MIDI then fails), and beams repeated on every note of a chord (Audiveris; Verovio
    then drops the chord).  Returns how many were taken out."""
    gone = []
    for part in root.findall("part"):
        open_ = {}                                      # (kind, number, staff) -> the element that started it
        for d in part.iter("direction"):
            staff = d.findtext("staff") or "1"
            for e in [e for dt in d.findall("direction-type") for e in dt if e.tag in SPANNERS]:
                key, kind = (e.tag, e.get("number", "1"), staff), e.get("type")
                if kind == "continue":
                    continue
                if kind == "stop":
                    match = key if key in open_ else next(      # numbered inconsistently: the oldest of its kind
                        (k for k in open_ if k[0] == key[0] and k[2] == staff),
                        next((k for k in open_ if k[0] == key[0]), None))
                    if match is None:
                        gone.append(e)                  # a stop without a start
                    else:
                        del open_[match]
                    continue
                if key in open_:
                    gone.append(open_[key])             # started again before it stopped
                open_[key] = e
        gone += open_.values()
    for part in root.findall("part"):    # a pedal let go that was never pressed: Verovio's MIDI goes back in time
        down = False
        for e in part.iter("pedal"):
            kind = e.get("type")
            if kind == "start":
                down = True
            elif kind in ("stop", "change", "continue") and not down:
                gone.append(e)
            elif kind == "stop":
                down = False
    for note in root.iter("note"):   # beams repeated on every note of a chord: Verovio then loses the chord's notes
        if note.find("chord") is not None:
            gone += note.findall("beam")
    for e in gone:
        if e.tag == "beam":
            e.getparent().remove(e)
            continue
        dt = e.getparent()
        dt.remove(e)
        if len(dt) == 0:
            d = dt.getparent()
            d.remove(dt)
            if d.find("direction-type") is None:
                d.getparent().remove(d)
    return len(gone)


ENGRAVE_CHECK = ("import sys; sys.path.insert(0, sys.argv[1]); from sheet_music_animator.engraver import engrave; "
                 "engrave(sys.argv[2])")


def _engraves(path: Path, say, log: Path) -> bool:
    """Whether the program's engraver can draw the score.  Tried in a separate process: a score the engraver
    cannot take can crash it, and in this process that would close the program."""
    code = _run([sys.executable, "-c", ENGRAVE_CHECK, Path(__file__).resolve().parents[1], path], say, 0.96, 0.99,
                "Checking that the score can be engraved…", log, {"QT_QPA_PLATFORM": "offscreen"})
    return code == 0


def _check_result(root, name: str) -> tuple[int, int, int]:
    parts, measures, notes = score_stats(root)
    if not parts or not measures:
        raise OmrError(f"{name} could not read any measures from the PDF.")
    if notes == 0:
        raise OmrError(f"{name} found staves but no notes in the PDF.")
    return parts, measures, notes


def recognize(pdf, pages: list[int], out_path, progress=None, engine: str | None = None) -> Path:
    """Read the music on `pages` (0-based) of `pdf` into a MusicXML file at `out_path` (returned).  Raises
    OmrError with a message for the user."""
    say = progress or (lambda f, s="": True)
    found = engines()
    if not found:
        raise OmrError(NO_ENGINE)
    engine = engine if engine in found else found[0]
    out_path = Path(out_path)
    work = Path(tempfile.mkdtemp(prefix="sma-omr-"))
    log = work / "omr.log"
    try:
        if engine == "Audiveris":
            root = _with_audiveris(Path(pdf), pages, work, log, say)
        else:
            root = _with_homr(Path(pdf), pages, work, log, say)
        _check_result(root, engine)
        tidy(root)
        out_path.write_bytes(etree.tostring(root.getroottree(), xml_declaration=True, encoding="UTF-8"))
        if not _engraves(out_path, say, work / "engrave.log"):
            raise OmrError(f"{engine} read the music, but this program cannot draw the result (it is saved as "
                           f"{out_path}). Open that file in a notation program such as MuseScore, save it again as "
                           f"MusicXML and open it with Open XML…")
        say(1.0, "")
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _with_audiveris(pdf: Path, pages, work: Path, log: Path, say):
    out = work / "out"
    out.mkdir()
    src = work / "score.pdf"          # a plain name: Audiveris names its output after the input
    shutil.copyfile(pdf, src)
    cmd = [_audiveris(), "-batch", "-export", "-output", out]
    if pages:
        cmd += ["-sheets", *[str(p + 1) for p in pages]]
    cmd += ["--", src]
    code = _run(cmd, say, 0.02, 0.95, "Audiveris is reading the music (this can take a few minutes)…", log)
    files = sorted(f for f in out.rglob("*") if f.suffix.lower() in (".mxl", ".musicxml") and f.is_file())
    if not files:
        why = _tail(log)
        raise OmrError("Audiveris could not read music from the PDF" + (f" (exit code {code})" if code else "")
                       + "." + (f"\n\nIts last messages:\n{why}" if why else ""))
    roots = [read_musicxml(f) for f in files]           # several movements come as several files
    return merge_scores(roots) if len(roots) > 1 else roots[0]


def _with_homr(pdf: Path, pages, work: Path, log: Path, say):
    doc = _open(pdf)
    cmd = _homr()
    roots, failed = [], []
    for k, i in enumerate(pages):
        f0, f1 = 0.02 + 0.93 * k / len(pages), 0.02 + 0.93 * (k + 1) / len(pages)
        say(f0, f"Reading page {i + 1} ({k + 1} of {len(pages)})…")
        img = work / f"page-{i + 1:03d}.png"
        _save_png(render_page(doc, i, OMR_DPI), img)
        _run(cmd + [img], say, f0, f1, f"homr is reading page {i + 1} ({k + 1} of {len(pages)})…", log)
        xml = img.with_suffix(".musicxml")
        root = read_musicxml(xml) if xml.is_file() else None
        if root is None or score_stats(root)[2] == 0:
            failed.append(i + 1)
        else:
            roots.append(root)
    if not roots:
        why = _tail(log)
        raise OmrError("homr could not read music from any page." + (f"\n\nIts last messages:\n{why}" if why else ""))
    if failed:
        raise OmrError(f"homr could not read the music on page{'s' if len(failed) > 1 else ''} "
                       f"{', '.join(map(str, failed))}. The score would have holes, so it was not opened.")
    return merge_scores(roots) if len(roots) > 1 else roots[0]


def _save_png(gray: np.ndarray, path: Path) -> None:
    from PySide6.QtGui import QImage
    h, w = gray.shape
    img = QImage(np.ascontiguousarray(gray).data, w, h, w, QImage.Format_Grayscale8)
    if not img.save(str(path)):
        raise OmrError(f"Could not write {path}.")


def output_path(pdf) -> Path:
    """Where the score read from `pdf` is kept: next to it (to be opened again, or corrected in a notation
    program), else in the temporary folder."""
    pdf = Path(pdf)
    for folder in (pdf.parent, Path(tempfile.gettempdir())):
        for k in range(1, 100):
            p = folder / (f"{pdf.stem} (from PDF).musicxml" if k == 1 else f"{pdf.stem} (from PDF {k}).musicxml")
            if not p.exists():
                try:
                    p.touch()
                    p.unlink()
                    return p
                except OSError:
                    break
    raise OmrError("There is nowhere to save the score read from the PDF.")
