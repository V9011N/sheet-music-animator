"""Timeline widget: ruler + note density + camera keyframe channels + playhead."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QMenu, QWidget

from .engraver import NOTE_KINDS
from .project import CHANNEL_LABELS, CHANNELS, EASES, Project

GUTTER, RULER_H, NOTES_H, CAM_H = 84, 24, 30, 26
CHANNEL_COLORS = {"pos": "#ff9f1a", "size": "#34c759", "rot": "#bf5af2"}
STEPS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600)


def fmt(t: float) -> str:
    m, s = divmod(max(t, 0.0), 60)
    return f"{int(m)}:{s:04.1f}"


class Timeline(QWidget):
    seeked = Signal(float)
    keysChanged = Signal()
    keysEditFinished = Signal()          # a drag or an edit of keyframes is complete (for undo)
    addKeyRequested = Signal(str, float)
    channelsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.score = None
        self.duration = 10.0
        self.t = 0.0
        self.view_start, self.view_span = 0.0, 10.0
        self.selected: set = set()    # the selected Keys (of any channel)
        self._anchor = None           # (channel, key) of the last plain/ctrl click, for shift-click ranges
        self.visible_channels = [c for c in CHANNELS]
        self._drag = None
        self._panning = None
        self._density: list[int] = []
        self._update_height()
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
        self.selected, self._anchor = set(), None
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

    def set_visible_channels(self, channels):
        self.visible_channels = [c for c in CHANNELS if c in channels] or ["pos"]
        self._update_height()
        self.update()
        self.channelsChanged.emit()

    def _update_height(self):
        self.setMinimumHeight(RULER_H + NOTES_H + CAM_H * len(self.visible_channels) + 6)
        self.updateGeometry()

    # ---- selection -----------------------------------------------------------------------------------
    def select_only(self, key):
        self.selected = {key} if key is not None else set()
        self.update()

    def select_all(self):
        if self.project:
            self.selected = {k for ch in self.visible_channels for k in self.project.channels[ch]}
            self.update()

    # ---- coordinate helpers -------------------------------------------------------------------------
    def _x(self, t):
        return GUTTER + (t - self.view_start) / self.view_span * (self.width() - GUTTER - 8)

    def _t(self, x):
        return self.view_start + (x - GUTTER) / max(self.width() - GUTTER - 8, 1) * self.view_span

    def _lane_y(self, i):
        return RULER_H + NOTES_H + 2 + i * CAM_H

    # -- rectangles of parts of the timeline (the in-app guide highlights them) ---------------------------
    def ruler_rect(self) -> QRect:
        return QRect(GUTTER, 0, self.width() - GUTTER, RULER_H + NOTES_H)

    def label_rect(self) -> QRect:
        return QRect(0, int(self._lane_y(0)), GUTTER, CAM_H)

    def lanes_rect(self) -> QRect:
        return QRect(GUTTER, int(self._lane_y(0)), self.width() - GUTTER, CAM_H * len(self.visible_channels))

    def _lane_at(self, y):
        i = int((y - (RULER_H + NOTES_H + 2)) // CAM_H)
        return self.visible_channels[i] if 0 <= i < len(self.visible_channels) else None

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
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 0.85)
        p.setFont(font)
        right = self.width() - 8

        # lane backgrounds + labels
        p.fillRect(QRectF(GUTTER, RULER_H, right - GUTTER, NOTES_H), faint)
        p.setPen(dim)
        p.drawText(QRectF(6, RULER_H, GUTTER - 8, NOTES_H), Qt.AlignVCenter, "Notes")
        for i, ch in enumerate(self.visible_channels):
            y = self._lane_y(i)
            p.fillRect(QRectF(GUTTER, y, right - GUTTER, CAM_H - 2), faint)
            p.setPen(dim)
            if i == 0:
                p.drawText(QRectF(6, y, GUTTER - 8, CAM_H), Qt.AlignVCenter, "Camera ▾")
            p.drawText(QRectF(GUTTER + 4, y, 90, CAM_H), Qt.AlignVCenter, CHANNEL_LABELS[ch])

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

        # camera keyframes, one lane per visible channel
        if self.project:
            for i, ch in enumerate(self.visible_channels):
                color = QColor(CHANNEL_COLORS[ch])
                cy = self._lane_y(i) + (CAM_H - 2) / 2
                ks = self.project.channels[ch]
                p.setPen(QPen(color, 1.5))
                for a, b in zip(ks, ks[1:]):
                    if a.ease != "hold":
                        p.drawLine(QPointF(self._x(a.t), cy), QPointF(self._x(b.t), cy))
                    else:
                        p.setPen(QPen(color, 1, Qt.DotLine))
                        p.drawLine(QPointF(self._x(a.t), cy), QPointF(self._x(b.t), cy))
                        p.setPen(QPen(color, 1.5))
                for k in ks:
                    x = self._x(k.t)
                    if x < GUTTER - 6 or x > right + 6:
                        continue
                    r = 6
                    poly = QPolygonF([QPointF(x, cy - r), QPointF(x + r, cy), QPointF(x, cy + r), QPointF(x - r, cy)])
                    p.setBrush(QColor("#ffffff") if k in self.selected else color)
                    p.setPen(QPen(color.darker(220), 1))
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
        """(channel, key) under the mouse."""
        ch = self._lane_at(pos.y()) if self.project else None
        if ch is None:
            return None
        best = min(self.project.channels[ch], key=lambda k: abs(self._x(k.t) - pos.x()), default=None)
        return (ch, best) if best is not None and abs(self._x(best.t) - pos.x()) <= 8 else None

    def _in_label(self, pos):
        return pos.x() < GUTTER and self._lane_y(0) <= pos.y() <= self._lane_y(0) + CAM_H

    def mousePressEvent(self, e):
        self.setFocus()
        pos = e.position()
        if e.button() == Qt.MiddleButton:
            self._panning = (pos.x(), self.view_start)
            return
        if e.button() == Qt.LeftButton and self._in_label(pos):
            self._channel_menu(e.globalPosition().toPoint())
            return
        hit = self._key_at(pos)
        mods = e.modifiers()
        if e.button() == Qt.RightButton:
            if hit:
                if hit[1] not in self.selected:
                    self.selected = {hit[1]}
                    self._anchor = hit
                self._key_menu(e.globalPosition().toPoint())
                self.update()
            return
        if e.button() != Qt.LeftButton:
            return
        if hit:
            ch, k = hit
            if mods & Qt.ControlModifier:
                self.selected ^= {k}
                self._anchor = hit
            elif mods & Qt.ShiftModifier and self._anchor and self._anchor[0] == ch:
                lo, hi = sorted((self._anchor[1].t, k.t))
                self.selected = {q for q in self.project.channels[ch] if lo - 1e-9 <= q.t <= hi + 1e-9}
            else:
                if k not in self.selected:
                    self.selected = {k}
                self._anchor = hit
            if k in self.selected:
                self._drag = ("keys", self._t(pos.x()), {q: q.t for q in self.selected})
            self.seeked.emit(k.t)
        else:
            if not mods & (Qt.ControlModifier | Qt.ShiftModifier):
                self.selected = set()
            self._drag = ("scrub", None, None)
            self._scrub(pos.x())
        self.update()

    def mouseDoubleClickEvent(self, e):
        ch = self._lane_at(e.position().y())
        if ch and e.position().x() >= GUTTER and not self._key_at(e.position()):
            self.addKeyRequested.emit(ch, max(self._t(e.position().x()), 0.0))

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._panning:
            x0, s0 = self._panning
            self.view_start = max(0.0, s0 - (pos.x() - x0) / (self.width() - GUTTER) * self.view_span)
            self.update()
        elif self._drag:
            kind, t0, starts = self._drag
            if kind == "scrub":
                self._scrub(pos.x())
            else:
                dt = self._t(pos.x()) - t0
                if abs(dt) > 1e-9:
                    for k, k0 in starts.items():
                        k.t = max(k0 + dt, 0.0)
                    for ch in CHANNELS:
                        self.project.channels[ch].sort(key=lambda q: q.t)
                    self.project.keys_edited = True
                    self._drag = (kind, t0, starts)
                    self._dragged = True
                    self.seeked.emit(max(self._t(pos.x()), 0.0))
                    self.keysChanged.emit()
        else:
            self.setCursor(Qt.PointingHandCursor if self._key_at(pos) or self._in_label(pos) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        dragged = self._drag and self._drag[0] == "keys" and getattr(self, "_dragged", False)
        self._drag = self._panning = None
        self._dragged = False
        if dragged:
            self.keysEditFinished.emit()

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

    def _channel_menu(self, global_pos):
        menu = QMenu(self)
        for ch in CHANNELS:
            a = menu.addAction(CHANNEL_LABELS[ch])
            a.setCheckable(True)
            a.setChecked(ch in self.visible_channels)
            a.toggled.connect(lambda on, ch=ch: self._toggle_channel(ch, on))
        menu.exec(global_pos)

    def _toggle_channel(self, ch, on):
        shown = set(self.visible_channels)
        (shown.add if on else shown.discard)(ch)
        self.set_visible_channels(shown)

    def _key_menu(self, global_pos):
        menu = QMenu(self)
        common = {k.ease for k in self.selected}
        for ease in EASES:
            a = menu.addAction(("● " if common == {ease} else "   ") + f"Ease: {ease}")
            a.triggered.connect(lambda _=False, ease=ease: self._set_ease(ease))
        menu.addSeparator()
        menu.addAction(f"Delete {len(self.selected)} keyframe" + ("s" if len(self.selected) != 1 else "")
                       ).triggered.connect(self.delete_selected)
        menu.exec(global_pos)

    def _set_ease(self, ease):
        for k in self.selected:
            k.ease = ease
        self.project.keys_edited = True
        self.keysChanged.emit()
        self.keysEditFinished.emit()
        self.update()

    def delete_selected(self):
        if not self.project or not self.selected:
            return
        for ch in CHANNELS:
            self.project.channels[ch] = [k for k in self.project.channels[ch] if k not in self.selected]
        self.project.keys_edited = True
        self.selected = set()
        self._anchor = None
        self.keysChanged.emit()
        self.keysEditFinished.emit()
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
        elif e.key() == Qt.Key_A and e.modifiers() & Qt.ControlModifier:
            self.select_all()
        else:
            super().keyPressEvent(e)
