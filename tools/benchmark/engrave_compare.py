"""Side-by-side: PDF page vs. the app's engraving of the same systems (line/page breaks from the MusicXML).

usage: python engrave_compare.py <piece dir name substring> [out dir] [--pages a-b]
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TRAIN = ROOT / "01_XMLs_PDFs_MP3s_for_training"

import pymupdf  # noqa: E402
from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sheet_music_animator.engraver import _read_musicxml, engrave  # noqa: E402
from sheet_music_animator.project import Project  # noqa: E402
from sheet_music_animator.scene import SheetScene  # noqa: E402

app = QApplication.instance() or QApplication([])


def breaks(xml):
    """(line starts, page of every line) from <print new-system/new-page>."""
    root = _read_musicxml(xml)
    part = root.find("part")
    starts, pages, page = [0], [0], 0
    for i, m in enumerate(part.findall("measure")):
        if i == 0:
            continue
        pr = m.findall("print")
        np_ = any(p.get("new-page") == "yes" for p in pr)
        ns = any(p.get("new-system") == "yes" for p in pr)
        if np_ or ns:
            page += np_
            starts.append(i)
            pages.append(page)
    return starts, pages


def piece_files(sub):
    d = next(p for p in TRAIN.iterdir() if sub.lower() in p.name.lower())
    xmls = sorted(d.glob("*.mxl"))
    return d, xmls


def render_systems(xml, starts, px_per_unit):
    t0 = time.time()
    score = engrave(str(xml), line_starts=starts)
    t1 = time.time()
    proj = Project(xml_path=str(xml))
    proj.line_starts = starts
    scene = SheetScene(score, proj)
    scene.set_cache(False)
    scene.rendering = True
    scene.apply_time(1e9, force=True)
    imgs = []
    for sy in score.systems:
        x, y, w, h = sy.rect
        x, y, w, h = x - 100, y - 100, w + 200, h + 200
        img = QImage(int(w * px_per_unit), int(h * px_per_unit), QImage.Format_RGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        scene.render(p, QRectF(0, 0, img.width(), img.height()), QRectF(x, y, w, h), Qt.IgnoreAspectRatio)
        p.end()
        imgs.append(img)
    return score, imgs, t1 - t0


def stack(imgs, width):
    hs = [int(i.height() * width / i.width()) for i in imgs]
    out = QImage(width, max(sum(hs), 1), QImage.Format_RGB32)
    out.fill(QColor("white"))
    p = QPainter(out)
    p.setRenderHints(QPainter.SmoothPixmapTransform)
    y = 0
    for i, h in zip(imgs, hs):
        p.drawImage(QRectF(0, y, width, h), i)
        p.setPen(QColor("#e05050"))
        p.drawLine(0, y, width, y)
        y += h
    p.end()
    return out


def main():
    sub = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else Path(__file__).parent / "out" / "engraving"
    rng = None
    if "--pages" in sys.argv:
        a, b = sys.argv[sys.argv.index("--pages") + 1].split("-")
        rng = (int(a), int(b))
    d, xmls = piece_files(sub)
    for xml in xmls:
        pdf = xml.with_suffix(".pdf")
        starts, pages = breaks(xml)
        score, imgs, secs = render_systems(xml, starts, 0.06)
        print(f"{xml.name}: {len(starts)} lines in xml, {len(score.systems)} systems engraved, "
              f"{len(score.units)} units, {secs:.1f}s engrave")
        doc = pymupdf.open(pdf)
        tag = xml.stem[:24]
        out.mkdir(parents=True, exist_ok=True)
        for pg in range(len(doc)):
            if rng and not rng[0] <= pg + 1 <= rng[1]:
                continue
            pix = doc[pg].get_pixmap(dpi=110)
            pimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
            mine = [im for im, p in zip(imgs, pages) if p == pg]
            if not mine:
                continue
            right = stack(mine, pimg.width())
            W = pimg.width() * 2 + 20
            comp = QImage(W, max(pimg.height(), right.height()), QImage.Format_RGB32)
            comp.fill(QColor("#888"))
            p = QPainter(comp)
            p.drawImage(0, 0, pimg)
            p.drawImage(pimg.width() + 20, 0, right)
            p.end()
            comp.save(str(out / f"{tag}_p{pg + 1:02d}.png"))


if __name__ == "__main__":
    main()
