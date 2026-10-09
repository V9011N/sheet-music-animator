"""Qt scene (the sheet music with timed notes) and the views onto it."""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPicture, QMouseEvent, QPolygonF, QPixmap, QPixmapCache, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsScene, QGraphicsView, QLabel, QStyle, QWidget)

from .engraver import FONT_TOKEN, NOTE_KINDS, REST_KINDS, Score
from .project import CATEGORIES, FIXED_KINDS, Project

SELECTABLE = NOTE_KINDS | REST_KINDS


class SvgItem(QGraphicsItem):
    """Draws a small standalone SVG 1:1 into its own bounding rect (page space).  Elements that may be
    moved or resized carry an offset and a scale (see `set_geom`)."""

    LOWRES_BELOW = 0.03   # device pixels per page unit under which `lowres` items are drawn from a small bitmap
    HANDLE_PX = 6         # half the size of a resize handle on screen

    def __init__(self, svg: bytes, rect: tuple, lowres: bool = False, movable: bool = False):
        super().__init__()
        self._rect = QRectF(*rect)
        self._svg = svg
        self.font = "Times New Roman"
        self._picture = None    # recorded on first paint: most items of a long piece are never drawn
        self._lowres = lowres
        self._bitmaps: dict[float, QPixmap] = {}
        self.wipe = 1.0
        self.alpha = 1.0     # opacity the scene gave this item (also set with setOpacity)
        self.ghost = 0.0     # opacity of the part that has not been revealed yet
        self.movable = movable
        self._lod = 1.0
        self._silent = False         # set while the scene positions the item itself
        self._scaling = None         # (centre in the scene, start distance, start scale) while a handle is dragged
        self._moved = False
        self.scale_factor = 1.0
        self.setAcceptedMouseButtons(Qt.NoButton)
        if movable:
            self.setFlag(QGraphicsItem.ItemIsMovable, True)
            self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)

    def boundingRect(self):
        return self._rect

    # -- look ---------------------------------------------------------------------------------------
    def set_font(self, font: str):
        if font != self.font:
            self.font = font
            self._picture = None
            self._bitmaps.clear()
            self.update()

    def _draw(self, painter):
        if self._picture is None:
            # Replaying recorded paint commands is ~2x faster than interpreting the SVG every frame,
            # and the SVG DOM can be dropped right away.
            self._picture = QPicture()
            p = QPainter(self._picture)
            family = ("'%s', serif" % self.font.replace("'", "")).encode("utf8")
            QSvgRenderer(QByteArray(self._svg.replace(FONT_TOKEN, family))).render(p, self._rect)
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
        if option.state & QStyle.State_Selected and not getattr(self.scene(), "rendering", False):
            lod = max(option.levelOfDetailFromTransform(painter.worldTransform()), 1e-9)
            self._lod = lod
            box = r.adjusted(10, 10, -10, -10)
            painter.setPen(QPen(QColor("#2f7bff"), 0, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(box)
            if self.movable:   # resize handles
                h = self.HANDLE_PX / lod
                painter.setPen(QPen(QColor("#2f7bff"), 0))
                painter.setBrush(QColor("#ffffff"))
                for c in self._corners(box):
                    painter.drawRect(QRectF(c.x() - h / 2, c.y() - h / 2, h, h))

    @staticmethod
    def _corners(box):
        return (box.topLeft(), box.topRight(), box.bottomLeft(), box.bottomRight())

    # -- moving and resizing --------------------------------------------------------------------------
    def set_geom(self, dx: float, dy: float, s: float):
        """Place the item: offset (dx, dy) and uniform scale s about its centre."""
        self._silent = True
        self.scale_factor = s
        self.setPos(dx, dy)
        c = self._rect.center()
        self.setTransform(QTransform.fromTranslate(c.x(), c.y()).scale(s, s).translate(-c.x(), -c.y()))
        self._silent = False

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and not self._silent and self.scene() is not None:
            self._moved = True
            self.scene().item_geometry_changed(self)
        return super().itemChange(change, value)

    def _handle_hit(self, pos) -> bool:
        box = self._rect.adjusted(10, 10, -10, -10)
        tol = (self.HANDLE_PX + 3) / max(self._lod, 1e-9)
        return any(abs(c.x() - pos.x()) <= tol and abs(c.y() - pos.y()) <= tol for c in self._corners(box))

    def mousePressEvent(self, e):
        if self.movable and self.isSelected() and e.button() == Qt.LeftButton and self._handle_hit(e.pos()):
            centre = self.mapToScene(self._rect.center())
            d = math.hypot(e.scenePos().x() - centre.x(), e.scenePos().y() - centre.y())
            self._scaling = (centre, max(d, 1e-6), self.scale_factor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._scaling is not None:
            centre, d0, s0 = self._scaling
            d = math.hypot(e.scenePos().x() - centre.x(), e.scenePos().y() - centre.y())
            self.set_geom(self.pos().x(), self.pos().y(), min(max(s0 * d / d0, 0.1), 10.0))
            self._moved = True
            self.scene().item_geometry_changed(self)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        was = self._scaling is not None
        self._scaling = None
        super().mouseReleaseEvent(e)
        if (was or self._moved) and self.scene() is not None:
            self._moved = False
            self.scene().item_edit_finished()


class MeasureItem(QGraphicsItem):
    """The staves of one measure: clicking its white space selects the measure."""

    def __init__(self, index: int, rect: tuple):
        super().__init__()
        self.index = index
        self._rect = QRectF(*rect)
        self.setZValue(0.5)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def boundingRect(self):
        return self._rect.adjusted(-15, -15, 15, 15)

    def shape(self):
        path = QPainterPath()
        path.addRect(self._rect)
        return path

    def paint(self, painter, option, widget=None):
        if option.state & QStyle.State_Selected and not getattr(self.scene(), "rendering", False):
            painter.setPen(QPen(QColor("#2f7bff"), 0))
            painter.setBrush(QColor(47, 123, 255, 45))
            painter.drawRect(self._rect)


class SheetScene(QGraphicsScene):
    BUCKET = 1.0   # seconds per bucket of the time index
    geometryChanged = Signal()        # an element was moved or resized
    editFinished = Signal()           # ...and the mouse was released

    def __init__(self, score: Score, project: Project, parent=None):
        super().__init__(parent)
        # Qt's default 10 MB pixmap cache is far too small for the thousands of cached items of a long
        # piece: they evict each other and everything is re-rendered on every repaint.
        QPixmapCache.setCacheLimit(max(QPixmapCache.cacheLimit(), 256 * 1024))
        self.score, self.project = score, project
        m = 20000  # breathing room so the camera can sit (and rotate) at the edge of the page
        self.page = QRectF(0, 0, score.width, score.height)
        self.setSceneRect(self.page.adjusted(-m, -m, m, m))
        self.setItemIndexMethod(QGraphicsScene.BspTreeIndex)
        self.setBackgroundBrush(QBrush(QColor(project.settings.paper)))
        self._layers: list[SvgItem] = []
        for layer in score.layers:
            it = SvgItem(layer.svg, layer.rect, lowres=True)
            it.setZValue(0)
            self.addItem(it)
            self._layers.append(it)
        self.measure_items: list[MeasureItem] = []
        for mi in score.measure_infos:
            it = MeasureItem(mi.index, mi.rect)
            self.addItem(it)
            self.measure_items.append(it)
        self._measure_anchor: int | None = None
        self.items_by_uid: dict[int, SvgItem] = {}
        self._items: list[SvgItem] = []          # parallel to score.units
        for u in score.units:
            it = SvgItem(u.svg, u.rect, movable=u.kind not in FIXED_KINDS)
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
        self.rendering = False       # while a frame is rendered selection marks are not drawn
        self._state: list[tuple | None] = [None] * len(self._items)
        self._cat_hidden = [False] * len(self._items)   # hidden by a category of its measure
        self._measure_times = [t for t, _ in score.measures]
        self._applied_t: float | None = None     # time the item states were last brought up to date for
        self._applied_end = 0.0
        self.time = 0.0
        self._geom_ready = False
        self.set_font(project.settings.font)
        self.apply_categories()
        self.apply_geometry()
        self._geom_ready = True
        self.reindex()

    # -- fonts, categories, geometry --------------------------------------------------------------------
    def set_font(self, font: str):
        self.font = font
        for it in self._items + self._layers:
            it.set_font(font)

    def apply_categories(self):
        """Hide the engravings of the categories switched off in each measure (see Project.hidden)."""
        hidden = self.project.hidden
        classes = {m: {c for cat in cats for c in CATEGORIES.get(cat, ())} for m, cats in hidden.items() if cats}
        for i, u in enumerate(self.score.units):
            self._cat_hidden[i] = u.kind in classes.get(u.measure, ())

    def apply_geometry(self):
        """Position every element as recorded in Project.transforms."""
        tf = self.project.transforms
        for it in self._items:
            if not it.movable:
                continue
            dx, dy, s = tf.get(it.unit.uid, (0.0, 0.0, 1.0))
            if (it.pos().x(), it.pos().y(), it.scale_factor) != (dx, dy, s):
                it.set_geom(dx, dy, s)

    def item_geometry_changed(self, item: SvgItem):
        if not self._geom_ready:
            return
        uid = item.unit.uid
        g = [round(item.pos().x(), 2), round(item.pos().y(), 2), round(item.scale_factor, 4)]
        if g == [0.0, 0.0, 1.0]:
            self.project.transforms.pop(uid, None)
        else:
            self.project.transforms[uid] = g
        self.geometryChanged.emit()

    def item_edit_finished(self):
        self.editFinished.emit()

    # -- selection ---------------------------------------------------------------------------------------
    def selected_measures(self) -> list[int]:
        return sorted(i.index for i in self.selectedItems() if isinstance(i, MeasureItem))

    def mousePressEvent(self, e):
        top = self.itemAt(e.scenePos(), QTransform())
        if e.button() == Qt.LeftButton and isinstance(top, MeasureItem):
            if e.modifiers() & Qt.ShiftModifier and self._measure_anchor is not None:
                lo, hi = sorted((self._measure_anchor, top.index))
                if not e.modifiers() & Qt.ControlModifier:
                    self.clearSelection()
                for it in self.measure_items[lo:hi + 1]:
                    it.setSelected(True)
                e.accept()
                return
            self._measure_anchor = top.index
        super().mousePressEvent(e)

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
            visible = bool(alpha > 0.002 and (wipe > 0 or (ghost > 0 and starts[i] < end)) and not self._cat_hidden[i])
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
        if self.project.settings.font != self.font:
            self.set_font(self.project.settings.font)
        self.apply_categories()
        self._geom_ready = False
        self.apply_geometry()
        self._geom_ready = True
        self.apply_time(force=True)

    def selected_units(self):
        return [i.unit for i in self.selectedItems() if hasattr(i, "unit")]


def heat_color(c: float, alpha: int = 150) -> QColor:
    """Red (unsure) through amber to green (confident); most good fits sit in the upper half."""
    t = min(max((c - 0.45) / 0.5, 0.0), 1.0)
    return QColor.fromHsv(int(t * 118), 210, 240, alpha)


def _heat_gradient(rect: QRectF, stops) -> QLinearGradient:
    g = QLinearGradient(rect.left(), 0, rect.right(), 0)
    for pos, c in stops:
        g.setColorAt(min(max(pos, 0.0), 1.0), heat_color(c))
    return g


def _hq(painter):
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)


def _rot(x, y, deg):
    """(x, y) rotated clockwise by deg degrees (y points down)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


class EditorView(QGraphicsView):
    """The whole sheet, with a draggable/resizable/rotatable camera window on top."""

    cameraEdited = Signal(float, float, float, float)   # cx, cy, w, rotation
    cameraEditFinished = Signal()

    HANDLE = 9  # px

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cam: tuple | None = None       # (cx, cy, w, h, rotation) in scene units
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
    def set_camera(self, pose: tuple | None, aspect: float):
        self.cam, self.aspect = pose, aspect
        self.viewport().update()

    def _k(self):
        return 1 / max(self.transform().m11(), 1e-9)       # scene units per screen pixel

    def _to_scene(self, lx, ly):
        cx, cy, w, h, rot = self.cam
        x, y = _rot(lx, ly, rot)
        return QPointF(cx + x, cy + y)

    def _to_local(self, p: QPointF, cam=None):
        cx, cy, w, h, rot = cam or self.cam
        x, y = _rot(p.x() - cx, p.y() - cy, -rot)
        return x, y

    def _corners(self):
        cx, cy, w, h, rot = self.cam
        return {"tl": self._to_scene(-w / 2, -h / 2), "tr": self._to_scene(w / 2, -h / 2),
                "bl": self._to_scene(-w / 2, h / 2), "br": self._to_scene(w / 2, h / 2)}

    def _rot_handle(self):
        cx, cy, w, h, rot = self.cam
        return self._to_scene(0, -h / 2 - 28 * self._k())

    def _hit(self, pos: QPointF):
        if self.cam is None:
            return None
        pts = dict(self._corners())
        pts["rot"] = self._rot_handle()
        for name, p in pts.items():
            vp = self.mapFromScene(p)
            if abs(vp.x() - pos.x()) <= self.HANDLE and abs(vp.y() - pos.y()) <= self.HANDLE:
                return name
        lx, ly = self._to_local(self.mapToScene(pos.toPoint()))
        return "move" if abs(lx) <= self.cam[2] / 2 and abs(ly) <= self.cam[3] / 2 else None

    # -- sync heat map (editor only: it is never part of the video) ------------------------------------------
    def set_heat(self, strips):
        """`strips`: [(QRectF, [(position 0..1, confidence 0..1), ...])] or None to switch the overlay off."""
        self._heat = None if not strips else [(r, _heat_gradient(r, stops)) for r, stops in strips]
        self.viewport().update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if getattr(self, "_heat", None):
            p = QPainter(self.viewport())
            p.setRenderHint(QPainter.Antialiasing)
            box = QRectF(self.viewport().width() - 188, 10, 176, 40)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 20, 24, 215))
            p.drawRoundedRect(box, 6, 6)
            g = QLinearGradient(box.left() + 10, 0, box.right() - 10, 0)
            for c in (0.45, 0.7, 0.95):
                g.setColorAt((c - 0.45) / 0.5, heat_color(c, 255))
            p.setBrush(g)
            p.drawRoundedRect(QRectF(box.left() + 10, box.top() + 8, box.width() - 20, 8), 3, 3)
            p.setPen(QColor("#d8d8de"))
            f = p.font()
            f.setPointSizeF(max(f.pointSizeF() * 0.85, 7.0)) if f.pointSizeF() > 0 else None
            p.setFont(f)
            p.drawText(QRectF(box.left() + 8, box.top() + 19, 80, 18), Qt.AlignLeft | Qt.AlignVCenter, "unsure")
            p.drawText(QRectF(box.right() - 88, box.top() + 19, 80, 18), Qt.AlignRight | Qt.AlignVCenter, "confident")
            p.end()

    def drawForeground(self, painter, rect):
        for r, g in getattr(self, "_heat", None) or ():
            if r.intersects(rect):
                painter.save()
                painter.setCompositionMode(QPainter.CompositionMode_Multiply)
                painter.fillRect(r, g)
                painter.restore()
        if self.cam is None:
            return
        _hq(painter)
        cx, cy, w, h, rot = self.cam
        poly = QPolygonF([self._to_scene(-w / 2, -h / 2), self._to_scene(w / 2, -h / 2),
                          self._to_scene(w / 2, h / 2), self._to_scene(-w / 2, h / 2)])
        shade = QPainterPath()
        shade.setFillRule(Qt.OddEvenFill)
        shade.addRect(rect)
        shade.addPolygon(poly)
        shade.closeSubpath()
        painter.fillPath(shade, QColor(0, 0, 0, 110))
        pen = QPen(QColor("#ff9f1a"), 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPolygon(poly)
        k = self._k()
        top, handle = self._to_scene(0, -h / 2), self._rot_handle()
        painter.drawLine(top, handle)
        painter.setBrush(QColor("#ff9f1a"))
        hs = self.HANDLE * k * 0.6
        for p in self._corners().values():
            painter.drawRect(QRectF(p.x() - hs, p.y() - hs, 2 * hs, 2 * hs))
        painter.drawEllipse(handle, hs * 1.1, hs * 1.1)

    # -- mouse ---------------------------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() in (Qt.MiddleButton, Qt.RightButton) or (e.button() == Qt.LeftButton and e.modifiers() & Qt.AltModifier):
            self._panning = e.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if e.button() == Qt.LeftButton:
            hit = self._hit(e.position())
            if hit and self.cam is not None:
                if hit == "move" and any(isinstance(i, SvgItem) and i.isSelected()
                                         for i in self.items(e.position().toPoint())):
                    super().mousePressEvent(e)   # a selected element under the cursor: move or resize it
                    return
                self._drag = {"mode": hit, "start": self.mapToScene(e.position().toPoint()), "cam": self.cam}
                if hit == "move":   # inside the camera a plain click still selects what is underneath
                    self._drag["pending"] = (e.position(), QMouseEvent(e))
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
            pend = self._drag.get("pending")
            if pend is not None:
                if (e.position() - pend[0]).manhattanLength() < 4:
                    return
                self._drag.pop("pending")
            self._do_drag(self.mapToScene(e.position().toPoint()), bool(e.modifiers() & Qt.ShiftModifier))
            return
        hit = self._hit(e.position())
        self.setCursor({"move": Qt.SizeAllCursor, "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
                        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor, "rot": Qt.CrossCursor}.get(hit, Qt.ArrowCursor))
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._panning is not None:
            self._panning = None
            self.setCursor(Qt.ArrowCursor)
            return
        if self._drag:
            pend = self._drag.get("pending")
            self._drag = None
            if pend is not None:   # it was a click, not a drag
                super().mousePressEvent(pend[1])
                super().mouseReleaseEvent(e)
                return
            self.cameraEditFinished.emit()
            return
        super().mouseReleaseEvent(e)

    def _do_drag(self, p: QPointF, snap: bool):
        d = self._drag
        cx0, cy0, w0, h0, rot0 = d["cam"]
        mode = d["mode"]
        if mode == "move":
            delta = p - d["start"]
            self.cameraEdited.emit(cx0 + delta.x(), cy0 + delta.y(), w0, rot0)
        elif mode == "rot":
            rot = math.degrees(math.atan2(p.x() - cx0, -(p.y() - cy0)))
            if snap:
                rot = round(rot / 15) * 15
            self.cameraEdited.emit(cx0, cy0, w0, rot)
        else:   # resize from a corner: the opposite corner stays put, aspect locked
            sx, sy = (1 if mode in ("tr", "br") else -1), (1 if mode in ("bl", "br") else -1)
            ax, ay = -sx * w0 / 2, -sy * h0 / 2                      # fixed corner, in the camera's own frame
            lx, ly = self._to_local(p, d["cam"])
            w = max(abs(lx - ax), abs(ly - ay) * self.aspect, 50.0)
            h = w / self.aspect
            mx, my = ax + sx * w / 2, ay + sy * h / 2                # new centre in the old camera frame
            x, y = _rot(mx, my, rot0)
            self.cameraEdited.emit(cx0 + x, cy0 + y, w, rot0)

    def wheelEvent(self, e):
        f = 1.0015 ** e.angleDelta().y()
        self.scale(f, f)


class PreviewView(QGraphicsView):
    """Exactly what the camera sees (kept at the output aspect ratio by PreviewWidget)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cam: tuple | None = None
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setInteractive(False)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)

    def set_camera(self, pose: tuple | None):
        self.cam = pose
        self._apply()

    def _apply(self):
        if self.cam is None:
            return
        cx, cy, w, h, rot = self.cam
        s = self.viewport().width() / max(w, 1e-6)
        self.setTransform(QTransform().rotate(-rot).scale(s, s))
        self.centerOn(cx, cy)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply()

    def wheelEvent(self, e):
        e.ignore()


class PreviewWidget(QWidget):
    """Letterboxes a PreviewView to the output aspect ratio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = PreviewView(self)
        self.label = QLabel(self)            # shows a finished frame (with effects) instead of the live view
        self.label.setScaledContents(True)
        self.label.hide()
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
        self.label.setGeometry(self.view.geometry())
        self.view._apply()

    def show_image(self, image):
        """Show a QImage in place of the live view (None: back to the live view)."""
        if image is None:
            if self.label.isVisible():
                self.label.hide()
                self.view.show()
            return
        self.label.setPixmap(QPixmap.fromImage(image))
        if not self.label.isVisible():
            self.view.hide()
            self.label.show()
