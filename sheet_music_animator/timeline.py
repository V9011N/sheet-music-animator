"""Timeline widget: ruler + note density + camera keyframe track + playhead."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QMenu, QWidget

from .engraver import NOTE_KINDS
from .project import EASES, Project

GUTTER, RULER_H, NOTES_H, CAM_H = 74, 24, 30, 30
STEPS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600)


def fmt(t: float) -> str:
    m, s = divmod(max(t, 0.0), 60)
    return f"{int(m)}:{s:04.1f}"


class Timeline(QWidget):
    seeked = Signal(float)
    keysChanged = Signal()
    addKeyRequested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.score = None
        self.duration = 10.0
        self.t = 0.0
        self.view_start, self.view_span = 0.0, 10.0
        self.selected = None          # CameraKey
        self._drag = None
        self._panning = None
        self._density: list[int] = []
        self.setMinimumHeight(RULER_H + NOTES_H + CAM_H + 6)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    # ---- data ----------------------------------------------------------------------------------
    def set_data(self, project: Project, score, duration: float):
        self.project, self.score, self.duration = project, score, max(duration, 1.0)
        bins = [0] * (int(self.duration / 0.25) + 2)
        for u in score.units:
            if u.kind in NOTE_KINDS:
                bins[min(int(u.time / 0.25), len(bins) - 1)] += 1
        self._density = bins
        self.selected = None
        self.fit()

    def fit(self):
        self.view_start, self.view_span = 0.0, self.duration
        self.update()

    def set_time(self, t: float):
        self.t = t
        # keep the playhead on screen while playing
        if t > self.view_start + self.view_span or t < self.view_start:
            self.view_start = max(0.0, t - self.view_span * 0.1)
        self.update()

    # ---- coordinate helpers -------------------------------------------------------------------------
    def _x(self, t):
        return GUTTER + (t - self.view_start) / self.view_span * (self.width() - GUTTER - 8)

    def _t(self, x):
        return self.view_start + (x - GUTTER) / max(self.width() - GUTTER - 8, 1) * self.view_span

    def _cam_y(self):
        return RULER_H + NOTES_H + 2

    # ---- painting ----------------------------------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        bg, fg = pal.window().color(), pal.windowText().color()
        dim = QColor(fg)
        dim.setAlpha(110)
        faint = QColor(fg)
        faint.setAlpha(40)
        p.fillRect(self.rect(), bg)
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 0.85)
        p.setFont(font)
        right = self.width() - 8

        # lane backgrounds + labels
        p.fillRect(QRectF(GUTTER, RULER_H, right - GUTTER, NOTES_H), faint)
        p.fillRect(QRectF(GUTTER, self._cam_y(), right - GUTTER, CAM_H), faint)
        p.setPen(dim)
        p.drawText(QRectF(6, RULER_H, GUTTER - 8, NOTES_H), Qt.AlignVCenter, "Notes")
        p.drawText(QRectF(6, self._cam_y(), GUTTER - 8, CAM_H), Qt.AlignVCenter, "Camera")

        # ruler ticks
        step = next((s for s in STEPS if s / self.view_span * (right - GUTTER) >= 70), STEPS[-1])
        t = (int(self.view_start / step)) * step
        while t <= self.view_start + self.view_span + step:
            x = self._x(t)
            if GUTTER - 1 <= x <= right:
                p.setPen(faint)
                p.drawLine(QPointF(x, RULER_H), QPointF(x, self.height()))
                p.setPen(dim)
                p.drawLine(QPointF(x, RULER_H - 6), QPointF(x, RULER_H))
                p.drawText(QPointF(x + 3, RULER_H - 9), fmt(t))
            t += step

        # measure ticks
        if self.score:
            mp = (right - GUTTER) / self.view_span
            show_nums = mp * 2 > 30
            p.setPen(dim)
            for mt, n in self.score.measures:
                x = self._x(mt)
                if GUTTER <= x <= right:
                    p.drawLine(QPointF(x, RULER_H - 3), QPointF(x, RULER_H))
                    if show_nums and n % (1 if mp * 2 > 60 else 5) == 0:
                        p.drawText(QPointF(x + 2, RULER_H + 11), str(n))

        # note density
        if self._density:
            acc = QColor("#4a9eff")
            acc.setAlpha(170)
            peak = max(self._density) or 1
            p.setPen(Qt.NoPen)
            p.setBrush(acc)
            i0 = max(int(self.view_start / 0.25) - 1, 0)
            i1 = min(int((self.view_start + self.view_span) / 0.25) + 2, len(self._density))
            for i in range(i0, i1):
                c = self._density[i]
                if c:
                    x0, x1 = self._x(i * 0.25), self._x((i + 1) * 0.25)
                    h = (NOTES_H - 8) * c / peak
                    p.drawRect(QRectF(x0, RULER_H + NOTES_H - 3 - h, max(x1 - x0 - 0.5, 1), h))

        # camera keyframes
        if self.project:
            cy = self._cam_y() + CAM_H / 2
            ks = self.project.keys
            p.setPen(QPen(QColor("#ff9f1a"), 1.5))
            for a, b in zip(ks, ks[1:]):
                if a.ease != "hold":
                    p.drawLine(QPointF(self._x(a.t), cy), QPointF(self._x(b.t), cy))
                else:
                    p.setPen(QPen(QColor("#ff9f1a"), 1, Qt.DotLine))
                    p.drawLine(QPointF(self._x(a.t), cy), QPointF(self._x(b.t), cy))
                    p.setPen(QPen(QColor("#ff9f1a"), 1.5))
            for k in ks:
                x = self._x(k.t)
                if x < GUTTER - 6 or x > right + 6:
                    continue
                r = 6
                poly = QPolygonF([QPointF(x, cy - r), QPointF(x + r, cy), QPointF(x, cy + r), QPointF(x - r, cy)])
                p.setBrush(QColor("#ffffff") if k is self.selected else QColor("#ff9f1a"))
                p.setPen(QPen(QColor("#7a4a00"), 1))
                p.drawPolygon(poly)

        # playhead
        x = self._x(self.t)
        if GUTTER <= x <= right:
            p.setPen(QPen(QColor("#ff3b30"), 1.5))
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            p.setBrush(QColor("#ff3b30"))
            p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 9)]))

    # ---- interaction ------------------------------------------------------------------------------------
    def _key_at(self, pos):
        if not self.project or not (self._cam_y() <= pos.y() <= self._cam_y() + CAM_H):
            return None
        best = min(self.project.keys, key=lambda k: abs(self._x(k.t) - pos.x()), default=None)
        return best if best and abs(self._x(best.t) - pos.x()) <= 8 else None

    def mousePressEvent(self, e):
        pos = e.position()
        if e.button() == Qt.MiddleButton:
            self._panning = (pos.x(), self.view_start)
            return
        k = self._key_at(pos)
        if e.button() == Qt.RightButton:
            if k:
                self.selected = k
                self._key_menu(k, e.globalPosition().toPoint())
            return
        if e.button() != Qt.LeftButton:
            return
        if k:
            self.selected = k
            self._drag = ("key", k)
            self.seeked.emit(k.t)
        else:
            self.selected = None
            self._drag = ("scrub", None)
            self._scrub(pos.x())
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self._cam_y() <= e.position().y() <= self._cam_y() + CAM_H and not self._key_at(e.position()):
            self.addKeyRequested.emit(max(self._t(e.position().x()), 0.0))

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._panning:
            x0, s0 = self._panning
            self.view_start = max(0.0, s0 - (pos.x() - x0) / (self.width() - GUTTER) * self.view_span)
            self.update()
        elif self._drag:
            kind, k = self._drag
            if kind == "scrub":
                self._scrub(pos.x())
            else:
                k.t = max(self._t(pos.x()), 0.0)
                self.project.keys.sort(key=lambda q: q.t)
                self.project.keys_edited = True
                self.seeked.emit(k.t)
                self.keysChanged.emit()
        else:
            self.setCursor(Qt.PointingHandCursor if self._key_at(pos) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        self._drag = self._panning = None

    def _scrub(self, x):
        self.seeked.emit(min(max(self._t(x), 0.0), self.duration))

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            anchor = self._t(e.position().x())
            f = 0.85 if e.angleDelta().y() > 0 else 1 / 0.85
            self.view_span = min(max(self.view_span * f, 1.0), self.duration * 1.2)
            self.view_start = max(0.0, anchor - (e.position().x() - GUTTER) / (self.width() - GUTTER) * self.view_span)
        else:
            self.view_start = max(0.0, self.view_start - e.angleDelta().y() / 120 * self.view_span * 0.1)
        self.update()

    def _key_menu(self, k, global_pos):
        menu = QMenu(self)
        for ease in EASES:
            a = menu.addAction(("● " if k.ease == ease else "   ") + f"Ease: {ease}")
            a.triggered.connect(lambda _=False, ease=ease: self._set_ease(k, ease))
        menu.addSeparator()
        menu.addAction("Delete keyframe").triggered.connect(self.delete_selected)
        menu.exec(global_pos)

    def _set_ease(self, k, ease):
        k.ease = ease
        self.project.keys_edited = True
        self.keysChanged.emit()
        self.update()

    def delete_selected(self):
        if self.project and self.selected in self.project.keys and len(self.project.keys) > 0:
            self.project.keys.remove(self.selected)
            self.project.keys_edited = True
            self.selected = None
            self.keysChanged.emit()
            self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
        else:
            super().keyPressEvent(e)
