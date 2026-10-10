"""PDF page vs. the app's MuseScore engraving of the same page (rendered through the app's own scene).

usage: python ms_compare.py <score file (.mscz / .mxl)> <pdf> [out dir] [--pages a-b]
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402
from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sheet_music_animator import msengraver  # noqa: E402
from sheet_music_animator.project import Project  # noqa: E402
from sheet_music_animator.scene import SheetScene  # noqa: E402

app = QApplication.instance() or QApplication([])


def main():
    src, pdf = Path(sys.argv[1]), Path(sys.argv[2])
    rest = [a for a in sys.argv[3:] if not a.startswith("--") and "-" not in a[:1]]
    out = Path(rest[0]) if rest and "--pages" not in sys.argv[3:4] else Path(__file__).parent / "out" / "musescore"
    rng = None
    if "--pages" in sys.argv:
        a, b = sys.argv[sys.argv.index("--pages") + 1].split("-")
        rng = (int(a), int(b))
    t0 = time.time()
    score = msengraver.engrave_musescore(src, ink="#000000")
    print(f"{src.name}: {len(score.systems)} lines, {len(score.units)} units, {len(score.layers)} layers, "
          f"{time.time() - t0:.1f}s; duration {score.duration:.1f}s, {len(score.notes)} notes")
    scene = SheetScene(score, Project(xml_path=str(src)))
    scene.set_cache(False)
    scene.rendering = True
    scene.apply_time(1e9, force=True)
    folder = msengraver.export(src)
    pages = msengraver._pages(folder)
    sizes = []
    for p in pages:
        from lxml import etree
        vb = [float(v) for v in etree.parse(str(p)).getroot().get("viewBox").split()]
        sizes.append((vb[2], vb[3]))
    k = score.width / max(w for w, _ in sizes)
    doc = pymupdf.open(pdf)
    out.mkdir(parents=True, exist_ok=True)
    y = 0.0
    for i, (w, h) in enumerate(sizes):
        if rng is None or rng[0] <= i + 1 <= rng[1]:
            pix = doc[i].get_pixmap(dpi=110) if i < len(doc) else None
            W = pix.width if pix else 900
            H = int(W * h / w)
            mine = QImage(W, H, QImage.Format_RGB32)
            mine.fill(QColor("white"))
            p = QPainter(mine)
            p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
            scene.render(p, QRectF(0, 0, W, H), QRectF(0, y * k, w * k, h * k), Qt.IgnoreAspectRatio)
            p.end()
            comp = QImage(W * 2 + 20, H, QImage.Format_RGB32)
            comp.fill(QColor("#888"))
            p = QPainter(comp)
            if pix:
                p.drawImage(0, 0, QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy())
            p.drawImage(W + 20, 0, mine)
            p.end()
            comp.save(str(out / f"{src.stem[:24]}_p{i + 1:02d}.png"))
        y += h


if __name__ == "__main__":
    main()
