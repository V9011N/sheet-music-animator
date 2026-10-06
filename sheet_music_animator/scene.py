"""Qt scene (the sheet music with timed notes) and the views onto it."""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPicture, QPixmap, QPixmapCache
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsScene, QGraphicsView, QStyle, QWidget)

from .engraver import NOTE_KINDS, REST_KINDS, Score
from .project import Project

SELECTABLE = NOTE_KINDS | REST_KINDS


class SvgItem(QGraphicsItem):
    """Draws a small standalone SVG 1:1 into its own bounding rect (page space)."""

    LOWRES_BELOW = 0.03   # device pixels per page unit under which `lowres` items are drawn from a small bitmap

    def __init__(self, svg: bytes, rect: tuple, lowres: bool = False):
        super().__init__()
        self._rect = QRectF(*rect)
        self._svg = svg
        self._picture = None    # recorded on first paint: most items of a long piece are never drawn
        self._lowres = lowres
        self._bitmaps: dict[float, QPixmap] = {}
        self.wipe = 1.0
        self.alpha = 1.0     # opacity the scene gave this item (also set with setOpacity)
        self.ghost = 0.0     # opacity of the part that has not been revealed yet
        self.setAcceptedMouseButtons(Qt.NoButton)

    def boundingRect(self):
        return self._rect

    def _draw(self, painter):
        if self._picture is None:
            # Replaying recorded paint commands is ~2x faster than interpreting the SVG every frame,
            # and the SVG DOM can be dropped right away.
            self._picture = QPicture()
            p = QPainter(self._picture)
            QSvgRenderer(QByteArray(self._svg)).render(p, self._rect)
            p.end()
        self._picture.play(painter)

    def _draw_lowres(self, painter, r, lod):
        """Zoomed far out (the whole-page editor view) re-rendering big vector layers is the slow part."""
        scale = 2.0 ** math.ceil(math.log2(max(lod, 1e-6)))
        pix = self._bitmaps.get(scale)
        if pix is None:
            if len(self._bitmaps) >= 2:
                self._bitmaps.pop(next(iter(self._bitmaps)))
            pix = QPixmap(max(int(r.width() * scale), 1), max(int(r.height() * scale), 1))
            pix.fill(Qt.transparent)
            p = QPainter(pix)
            p.setRenderHints(QPainter.Antialiasing)
            p.scale(pix.width() / r.width(), pix.height() / r.height())
            p.translate(-r.topLeft())
            self._draw(p)
            p.end()
            self._bitmaps[scale] = pix
        painter.drawPixmap(r, pix, QRectF(pix.rect()))

    def paint(self, painter, option, widget=None):
        r = self._rect
        if self.wipe < 1.0:  # left-to-right reveal
            if self.ghost > 0:   # the unrevealed rest stays faintly visible
                painter.save()
                painter.setOpacity(painter.opacity() * min(self.ghost / max(self.alpha, 1e-6), 1.0))
                self._draw(painter)
                painter.restore()
            painter.save()
            painter.setClipRect(QRectF(r.left(), r.top(), r.width() * self.wipe, r.height()))
            self._draw(painter)
            painter.restore()
        else:
            lod = option.levelOfDetailFromTransform(painter.worldTransform())
            if self._lowres and lod < self.LOWRES_BELOW:
                self._draw_lowres(painter, r, lod)
            else:
                self._draw(painter)
        if option.state & QStyle.State_Selected:
            painter.setPen(QPen(QColor("#2f7bff"), 0, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(r.adjusted(10, 10, -10, -10))


class SheetScene(QGraphicsScene):
    BUCKET = 1.0   # seconds per bucket of the time index

    def __init__(self, score: Score, project: Project, parent=None):
        super().__init__(parent)
        # Qt's default 10 MB pixmap cache is far too small for the thousands of cached items of a long
        # piece: they evict each other and everything is re-rendered on every repaint.
        QPixmapCache.setCacheLimit(max(QPixmapCache.cacheLimit(), 256 * 1024))
        self.score, self.project = score, project
        m = 2500  # breathing room so the camera can sit at the edge of the page
        self.page = QRectF(0, 0, score.width, score.height)
        self.setSceneRect(self.page.adjusted(-m, -m, m, m))
        self.setItemIndexMethod(QGraphicsScene.BspTreeIndex)
        self.setBackgroundBrush(QBrush(QColor(project.settings.paper)))
        for layer in score.layers:
            it = SvgItem(layer.svg, layer.rect, lowres=True)
            it.setZValue(0)
            self.addItem(it)
        self.items_by_uid: dict[int, SvgItem] = {}
        self._items: list[SvgItem] = []          # parallel to score.units
        for u in score.units:
            it = SvgItem(u.svg, u.rect)
            # Beams, slurs, dynamics... have big bounding boxes: keep them below the notes so that a
            # click on a note selects the note, while every engraved element can be selected.
            it.setZValue(2 if u.kind in SELECTABLE else 1)
            it.setVisible(False)
            it.unit = u
            it.setOpacity(0.0)
            it.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
            it.setFlag(QGraphicsItem.ItemIsSelectable, True)
            it.setAcceptedMouseButtons(Qt.LeftButton)
            self.addItem(it)
            self.items_by_uid[u.uid] = it
            self._items.append(it)
        self._state: list[tuple | None] = [None] * len(self._items)
        self._measure_times = [t for t, _ in score.measures]
        self._applied_t: float | None = None     # time the item states were last brought up to date for
        self._applied_end = 0.0
        self.time = 0.0
        self.reindex()

    # -- time index -----------------------------------------------------------------------------
    def reindex(self):
        """Work out when each unit starts and finishes changing, so that moving the playhead only
        has to touch the few units around it (a long piece has thousands)."""
        proj, s = self.project, self.project.settings
        fade = 0.0 if s.reveal == "instant" else s.fade
        self._starts, self._buckets = [], {}
        for i, u in enumerate(self.score.units):
            if proj.always_visible(u):   # never changes with time: set once, no need to index
                self._starts.append(-math.inf)
                continue
            start = proj.start_of(u)
            if u.steps:
                finish = max(m + s.offset + proj.overrides.get(uid, 0.0) for uid, m, _ in u.steps) + fade
            elif u.wipe and s.reveal != "instant":
                finish = start + max(u.end - u.time, 0.05)
            else:
                finish = start
            finish = max(finish, start + fade)
            self._starts.append(start)
            for b in range(math.floor(start / self.BUCKET), math.floor(finish / self.BUCKET) + 1):
                self._buckets.setdefault(b, []).append(i)
        self._order = sorted(range(len(self._starts)), key=self._starts.__getitem__)
        self._sorted_starts = [self._starts[i] for i in self._order]
        self._meas = [t + s.offset for t in self._measure_times]

    def _lookahead_end(self, t: float) -> float:
        """Unplayed units that start later than this are not loaded (not even as ghosts)."""
        s = self.project.settings
        if s.ghost <= 0 or s.lookahead <= 0 or not self._meas:
            return math.inf
        k = bisect_right(self._meas, t) - 1 + s.lookahead + 1
        return self._meas[k] if k < len(self._meas) else math.inf

    def apply_time(self, t: float | None = None, force: bool = False):
        if t is not None:
            self.time = t
        t = self.time
        end = self._lookahead_end(t)
        if force or self._applied_t is None:
            if force:
                self.reindex()
            changed = range(len(self._items))
        else:
            lo, hi = sorted((self._applied_t, t))
            changed = set()
            for b in range(math.floor(lo / self.BUCKET), math.floor(hi / self.BUCKET) + 1):
                changed.update(self._buckets.get(b, ()))
            e0, e1 = sorted((self._applied_end, end))   # units entering or leaving the lookahead window
            if e0 < e1:
                changed.update(self._order[bisect_left(self._sorted_starts, e0):bisect_left(self._sorted_starts, e1)])
        self._applied_t, self._applied_end = t, end
        proj, ghost = self.project, self.project.settings.ghost
        units, items, states, starts = self.score.units, self._items, self._state, self._starts
        for i in changed:
            alpha, wipe = proj.reveal(units[i], t)
            visible = bool(alpha > 0.002 and (wipe > 0 or (ghost > 0 and starts[i] < end)))
            state = (alpha, wipe, visible, ghost)
            if not force and states[i] == state:
                continue
            states[i] = state
            it = items[i]
            it.wipe, it.alpha, it.ghost = wipe, alpha, ghost
            if it.cacheMode() != QGraphicsItem.NoCache:
                # The editor and the preview both draw these items; Qt keeps one pixmap per view and
                # can leave a stale one behind (missing stems/beams/slurs), so drop them all on change.
                it.setCacheMode(QGraphicsItem.NoCache)
                it.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
            it.setVisible(visible)
            it.setOpacity(alpha)
            it.update()

    def set_cache(self, on: bool):
        """Pixmap caching keeps the editor fast; turn it off while rendering for pure vector output."""
        mode = QGraphicsItem.DeviceCoordinateCache if on else QGraphicsItem.NoCache
        for it in self._items:
            it.setCacheMode(mode)

    def refresh(self):
        self.setBackgroundBrush(QBrush(QColor(self.project.settings.paper)))
        self.apply_time(force=True)

    def selected_units(self):
        return [i.unit for i in self.selectedItems() if hasattr(i, "unit")]


def _hq(painter):
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)


class EditorView(QGraphicsView):
    """The whole sheet, with a draggable/resizable camera window on top."""

    cameraEdited = Signal(float, float, float)   # cx, cy, w
    cameraEditFinished = Signal()

    HANDLE = 9  # px

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cam: QRectF | None = None
        self.aspect = 16 / 9
        self._drag = None
        self._panning = None
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setMouseTracking(True)

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, QColor("#2b2b2e"))
        if self.scene():
            painter.fillRect(self.scene().page, self.scene().backgroundBrush())

    # -- camera overlay -----------------------------------------------------------------------
    def set_camera(self, rect: QRectF | None, aspect: float):
        self.cam, self.aspect = rect, aspect
        self.viewport().update()

    def _corners(self):
        c = self.cam
        return {"tl": c.topLeft(), "tr": c.topRight(), "bl": c.bottomLeft(), "br": c.bottomRight()}

    def _hit(self, pos: QPointF):
        if self.cam is None:
            return None
        for name, p in self._corners().items():
            vp = self.mapFromScene(p)
            if abs(vp.x() - pos.x()) <= self.HANDLE and abs(vp.y() - pos.y()) <= self.HANDLE:
                return name
        return "move" if self.cam.contains(self.mapToScene(pos.toPoint())) else None

    def drawForeground(self, painter, rect):
        if self.cam is None:
            return
        _hq(painter)
        shade = QColor(0, 0, 0, 110)
        painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), self.cam.top() - rect.top()), shade)
        painter.fillRect(QRectF(rect.left(), self.cam.bottom(), rect.width(), rect.bottom() - self.cam.bottom()), shade)
        painter.fillRect(QRectF(rect.left(), self.cam.top(), self.cam.left() - rect.left(), self.cam.height()), shade)
        painter.fillRect(QRectF(self.cam.right(), self.cam.top(), rect.right() - self.cam.right(), self.cam.height()), shade)
        pen = QPen(QColor("#ff9f1a"), 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self.cam)
        k = 1 / max(self.transform().m11(), 1e-9)
        painter.setBrush(QColor("#ff9f1a"))
        hs = self.HANDLE * k * 0.6
        for p in self._corners().values():
            painter.drawRect(QRectF(p.x() - hs, p.y() - hs, 2 * hs, 2 * hs))

    # -- mouse ---------------------------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() in (Qt.MiddleButton, Qt.RightButton) or (e.button() == Qt.LeftButton and e.modifiers() & Qt.AltModifier):
            self._panning = e.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if e.button() == Qt.LeftButton:
            hit = self._hit(e.position())
            if hit and self.cam is not None:
                self._drag = {"mode": hit, "start": self.mapToScene(e.position().toPoint()), "rect": QRectF(self.cam)}
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._panning is not None:
            d = e.position() - self._panning
            self._panning = e.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            return
        if self._drag:
            self._do_drag(self.mapToScene(e.position().toPoint()))
            return
        hit = self._hit(e.position())
        self.setCursor({"move": Qt.SizeAllCursor, "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
                        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor}.get(hit, Qt.ArrowCursor))
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._panning is not None:
            self._panning = None
            self.setCursor(Qt.ArrowCursor)
            return
        if self._drag:
            self._drag = None
            self.cameraEditFinished.emit()
            return
        super().mouseReleaseEvent(e)

    def _do_drag(self, p: QPointF):
        d, r0 = self._drag, self._drag["rect"]
        if d["mode"] == "move":
            delta = p - d["start"]
            cx, cy = r0.center().x() + delta.x(), r0.center().y() + delta.y()
            w = r0.width()
        else:  # resize from a corner, opposite corner stays put, aspect locked
            anchor = {"tl": r0.bottomRight(), "tr": r0.bottomLeft(),
                      "bl": r0.topRight(), "br": r0.topLeft()}[d["mode"]]
            w = max(abs(p.x() - anchor.x()), abs(p.y() - anchor.y()) * self.aspect, 50.0)
            h = w / self.aspect
            sx = 1 if p.x() >= anchor.x() else -1
            sy = 1 if p.y() >= anchor.y() else -1
            cx, cy = anchor.x() + sx * w / 2, anchor.y() + sy * h / 2
        self.cameraEdited.emit(cx, cy, w)

    def wheelEvent(self, e):
        f = 1.0015 ** e.angleDelta().y()
        self.scale(f, f)



class PreviewView(QGraphicsView):
    """Exactly what the camera sees (kept at the output aspect ratio by PreviewWidget)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cam: QRectF | None = None
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setInteractive(False)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)

    def set_camera(self, rect: QRectF | None):
        self.cam = rect
        if rect is not None:
            self.fitInView(rect, Qt.IgnoreAspectRatio)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.cam is not None:
            self.fitInView(self.cam, Qt.IgnoreAspectRatio)

    def wheelEvent(self, e):
        e.ignore()


class PreviewWidget(QWidget):
    """Letterboxes a PreviewView to the output aspect ratio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = PreviewView(self)
        self.aspect = 16 / 9
        self.setMinimumSize(240, 135)
        self.setStyleSheet("background:#111;")

    def set_aspect(self, aspect: float):
        self.aspect = aspect
        self._layout()

    def resizeEvent(self, e):
        self._layout()

    def _layout(self):
        w, h = self.width(), self.height()
        vw = min(w, int(h * self.aspect))
        vh = int(vw / self.aspect)
        self.view.setGeometry((w - vw) // 2, (h - vh) // 2, vw, vh)
        if self.view.cam is not None:
            self.view.fitInView(self.view.cam, Qt.IgnoreAspectRatio)
