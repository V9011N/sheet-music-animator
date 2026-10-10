"""How exactly does the app draw what MuseScore draws?  For each score and layout, MuseScore's own SVG export and the
app's scene (everything revealed) are rendered at the same size and compared pixel by pixel.

usage: python ms_parity.py <score.mscz ...> [--layouts printed,4,one] [--out dir]
Prints, per page (or per system on one line), the share of ink that differs (1 px tolerance), and saves an image
(MuseScore | app | difference) of every page or system that differs by more than 1 %.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from lxml import etree  # noqa: E402
from PySide6.QtCore import QByteArray, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sheet_music_animator import msengraver as M  # noqa: E402
from sheet_music_animator.project import Project  # noqa: E402
from sheet_music_animator.scene import SheetScene  # noqa: E402

app = QApplication.instance() or QApplication([])
PX = 1400            # pixels across a page


def ink(img):
    g = img.convertToFormat(QImage.Format_Grayscale8)
    h = g.height()
    a = np.frombuffer(g.constBits(), np.uint8, count=g.bytesPerLine() * h).reshape(h, g.bytesPerLine())[:, :g.width()]
    return a < 160


def grow(a):
    b = a.copy()
    b[1:] |= a[:-1]
    b[:-1] |= a[1:]
    b[:, 1:] |= b[:, :-1]
    b[:, :-1] |= b[:, 1:]
    return b


def compare(raw, mine):
    a, b = ink(raw), ink(mine)
    miss = a & ~grow(b)           # MuseScore draws it, the app does not
    extra = b & ~grow(a)          # the app draws it, MuseScore does not
    return (miss.sum() + extra.sum()) / max(a.sum(), 1), miss, extra


def image(w, h):
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("white"))
    return img


def raw_region(svg_bytes, page_w, page_h, region, W, H):
    """MuseScore's page, the part `region` (x, y, w, h in SVG units) of it, rendered W x H."""
    r = QSvgRenderer(QByteArray(svg_bytes))
    img = image(W, H)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing)
    sx, sy = W / region[2], H / region[3]
    p.scale(sx, sy)
    p.translate(-region[0], -region[1])
    r.render(p, QRectF(0, 0, page_w, page_h))
    p.end()
    return img


def app_region(scene, rect, W, H):
    img = image(W, H)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing)
    scene.render(p, QRectF(0, 0, W, H), QRectF(*rect), Qt.IgnoreAspectRatio)
    p.end()
    return img


def save(out, name, raw, mine, miss, extra):
    W, H = raw.width(), raw.height()
    comp = image(W * 3 + 20, H)
    p = QPainter(comp)
    p.drawImage(0, 0, raw)
    p.drawImage(W + 10, 0, mine)
    diff = image(W, H)
    for (arr, color) in ((miss, 0xFFD02020), (extra, 0xFF2060FF)):
        ys, xs = np.nonzero(arr)
        for y, x in zip(ys[:200000], xs[:200000]):
            diff.setPixel(int(x), int(y), color)
    p.drawImage(2 * W + 20, 0, diff)
    p.end()
    comp.save(str(out / f"{name}.png"))


def run(path: Path, layout: str, out: Path):
    mpl = {"printed": None, "4": 4, "one": 0}[layout]
    score = M.engrave_musescore(path, measures_per_line=mpl)
    scene = SheetScene(score, Project(xml_path=str(path)))
    scene.set_cache(False)
    scene.rendering = True
    scene.apply_time(1e9, force=True)
    if layout == "one" and (runs := M._visibility_runs(path, None)):
        f0 = M.export(path, runs, True)
        folder = M.export(path, runs, True, None, M._even_staves(f0) or None)
    elif layout == "one":
        folder = M.export(path, None, True)
    else:
        n = M._count_measures(path)
        folder = M.export(path, None if mpl is None else list(range(0, n, mpl)), False)
    pages = M._pages(folder)
    els, sizes = M._read_pages(pages)
    k = M.STAFF_SPACE / M._staff_space(els)
    results = []
    if layout == "one" and len(score.measure_infos):
        rows = M._system_rows(folder / "score.mpos")
        boxes, ev = M._positions(folder / "score.mpos")
        times = M._first_times(ev)
        ms = sorted(((b, times[i]) for i, b in boxes.items() if i in times), key=lambda m: m[1])
        for ri, (page, top, bottom, mids) in enumerate(rows):
            b0 = ms[mids[0]][0]
            info = score.measure_infos[mids[0]]
            dx, dy = info.rect[0] - b0[1] * k, info.rect[1] - b0[2] * k
            x0 = min(ms[i][0][1] for i in mids) - 1200 / k
            x1 = max(ms[i][0][3] for i in mids) + 400 / k
            y0, y1 = top - 2500 / k, bottom + 2500 / k
            # long systems: compare in pieces as wide as a page
            step = sizes[0][1] / 1.4
            x = x0
            while x < x1:
                region = (x, y0, min(step, x1 - x), y1 - y0)
                W = PX
                H = max(int(W * region[3] / region[2]), 10)
                raw = raw_region(pages[page].read_bytes(), *sizes[page], region, W, H)
                mine = app_region(scene, (region[0] * k + dx, region[1] * k + dy, region[2] * k, region[3] * k), W, H)
                d, miss, extra = compare(raw, mine)
                results.append((f"system {ri + 1} x{int(x)}", d))
                if d > 0.01:
                    save(out, f"{path.stem[:20]}_{layout}_s{ri + 1}_{int(x)}", raw, mine, miss, extra)
                x += step
    else:
        y = 0.0
        for pi, (w, h) in enumerate(sizes):
            W, H = PX, int(PX * h / w)
            raw = raw_region(pages[pi].read_bytes(), w, h, (0, 0, w, h), W, H)
            mine = app_region(scene, (0, y * k, w * k, h * k), W, H)
            d, miss, extra = compare(raw, mine)
            results.append((f"page {pi + 1}", d))
            if d > 0.01:
                save(out, f"{path.stem[:20]}_{layout}_p{pi + 1}", raw, mine, miss, extra)
            y += h
    worst = sorted(results, key=lambda r: -r[1])[:3]
    print(f"{path.name[:40]:40s} {layout:7s} {len(results):3d} pieces, mean {np.mean([r[1] for r in results]) * 100:5.2f}% "
          f"worst " + ", ".join(f"{n} {d * 100:.1f}%" for n, d in worst), flush=True)


def main():
    args = sys.argv[1:]
    layouts = ["printed", "4", "one"]
    out = Path(__file__).parent / "out" / "parity"
    if "--layouts" in args:
        i = args.index("--layouts")
        layouts = args[i + 1].split(",")
        del args[i:i + 2]
    if "--out" in args:
        i = args.index("--out")
        out = Path(args[i + 1])
        del args[i:i + 2]
    out.mkdir(parents=True, exist_ok=True)
    for a in args:
        for layout in layouts:
            try:
                run(Path(a), layout, out)
            except Exception as e:  # noqa: BLE001
                import traceback
                print(f"{Path(a).name[:40]:40s} {layout:7s} FAILED: {e!r}"[:300])
                traceback.print_exc()


if __name__ == "__main__":
    main()
