"""Tap to Keyframe: listen to the (fitted) recording and tap Space on every note; each tap sets the time at
which the next engraving appears."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFormLayout, QLabel, QPushButton, QVBoxLayout)

from .engraver import NOTE_KINDS, REST_KINDS

MODES = (("Notes only", "notes"), ("Notes and Rests", "notes_rests"))
REMARK = ("Tap along with the music to time the engravings yourself. The recording plays from where the seeker "
          "is now, after a 3 second count-down. Press <b>Space</b> once for every note (a chord counts as one) "
          "and the next engraving appears exactly when you tap — Space does not pause the music. Press "
          "<b>Esc</b> to stop and leave this mode; everything you tapped is kept (and can be undone in one step).")


class TapDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Tap to Keyframe")
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        lbl = QLabel(REMARK)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        v.addWidget(lbl)
        f = QFormLayout()
        self.combo = QComboBox()
        for text, key in MODES:
            self.combo.addItem(text, key)
        f.addRow("Each tap advances by", self.combo)
        v.addLayout(f)
        self.btn_start = QPushButton("Start")
        self.btn_start.setDefault(True)
        self.btn_start.clicked.connect(self.accept)
        v.addWidget(self.btn_start)

    @property
    def mode(self) -> str:
        return self.combo.currentData()


def tap_events(score, mode: str) -> list:
    """[(score time, [units that appear then])]: one entry per tap.  A note that only continues a tie is not
    struck, so it is not an event; with `mode == "notes_rests"` rests are."""
    kinds = NOTE_KINDS | (REST_KINDS if mode == "notes_rests" else set())
    groups: dict = {}
    for u in score.units:
        if u.kind not in kinds or u.static:
            continue
        if u.kind in NOTE_KINDS and u.heads and all(h[5] for h in u.heads):
            continue
        groups.setdefault(round(u.time, 3), []).append(u)
    return sorted(groups.items())


class TapSession(QObject):
    """Count-down, playback and taps.  While it runs, Space taps (it does not pause) and Esc ends it."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.state = "idle"            # idle | countdown | tapping
        self.events: list = []
        self.next = 0
        self.taps = 0
        self.start_time = 0.0
        self.count = 3
        self._gate_from = 0.0
        self.timer = QTimer(self, interval=1000)
        self.timer.timeout.connect(self._count)
        self.big = QLabel(win)         # the count-down number
        self.big.setAlignment(Qt.AlignCenter)
        self.big.setStyleSheet("background:rgba(15,15,20,200);color:#ff9f1c;font-size:96px;font-weight:bold;"
                               "border-radius:24px;")
        self.big.hide()
        self.banner = QLabel(win)
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setStyleSheet("background:rgba(15,15,20,215);color:#f0f0f4;font-size:14px;padding:8px 16px;"
                                  "border-radius:10px;border:2px solid #ff9f1c;")
        self.banner.hide()
        self._play_shortcut = None

    @property
    def active(self) -> bool:
        return self.state != "idle"

    # -- start / end ------------------------------------------------------------------------------------------
    def begin(self, mode: str) -> bool:
        win = self.win
        if win.score is None or self.active:
            return False
        self.events = tap_events(win.score, mode)
        self.start_time = win.t
        self.next = next((i for i, (t, _) in enumerate(self.events) if t + win.project.settings.offset >= win.t - 1e-3),
                         len(self.events))
        if self.next >= len(self.events):
            win.status.showMessage("There is nothing left to tap after the seeker: move it earlier.", 6000)
            return False
        self.taps = 0
        win.pause()
        w = QApplication.focusWidget()
        if w is not None:
            w.clearFocus()
        win.editor.setFocus()
        self._play_shortcut = win.a_play.shortcut()
        win.a_play.setShortcut(QKeySequence())        # Space must not pause while tapping
        QApplication.instance().installEventFilter(self)
        self._gate_from = self.events[self.next][0]
        win.scene.set_gate((self._gate_from, self._gate_from - 0.01))   # nothing from the first tap's note on yet
        self.state, self.count = "countdown", 3
        self.big.setText(str(self.count))
        self._place()
        self.big.show()
        self.big.raise_()
        self.timer.start()
        win.status.showMessage("Get ready…  Space = tap, Esc = stop", 0)
        return True

    def _count(self):
        self.count -= 1
        if self.count > 0:
            self.big.setText(str(self.count))
            return
        self.timer.stop()
        self.big.hide()
        self.state = "tapping"
        self._update_banner()
        self.banner.show()
        self.banner.raise_()
        self._place()
        self.win.play()

    def finish(self, message: str | None = None):
        if not self.active:
            return
        win = self.win
        self.timer.stop()
        QApplication.instance().removeEventFilter(self)
        self.state = "idle"
        self.big.hide()
        self.banner.hide()
        if self._play_shortcut is not None:
            win.a_play.setShortcut(self._play_shortcut)
        win.pause()
        win.scene.set_gate(None)
        win.scene.reindex()
        win.scene.apply_time(force=True)
        win._refresh_time(follow=False)
        win.timeline.update()
        if self.taps:
            win.commit()
        win.status.showMessage(message or f"Tapped {self.taps} note{'s' if self.taps != 1 else ''}.", 8000)

    # -- keys -----------------------------------------------------------------------------------------------------
    def eventFilter(self, obj, ev):
        if not self.active:
            return False
        t = ev.type()
        if t in (QEvent.KeyPress, QEvent.KeyRelease, QEvent.ShortcutOverride) and ev.key() in (Qt.Key_Space, Qt.Key_Escape):
            if t == QEvent.KeyPress:
                if ev.key() == Qt.Key_Escape:
                    self.finish("Tap mode ended." if not self.taps else None)
                elif not ev.isAutoRepeat() and self.state == "tapping":
                    self.tap()
            ev.accept()
            return True
        return False

    def tap(self):
        win = self.win
        if self.next >= len(self.events):
            return
        t_tap = win.current_time()
        t_nom, units = self.events[self.next]
        s = win.project.settings
        for u in units:
            off = t_tap - u.time - s.offset
            if abs(off) < 1e-4:
                win.project.overrides.pop(u.uid, None)
            else:
                win.project.overrides[u.uid] = off
        self.next += 1
        self.taps += 1
        win.scene.reindex()
        win.scene.set_gate((self._gate_from, t_nom))      # releases everything up to this note
        win._refresh_time(follow=False)
        self._update_banner()
        if self.next >= len(self.events):
            self.finish("Tapped the last note — done.")

    # -- display --------------------------------------------------------------------------------------------------
    def _update_banner(self):
        left = len(self.events) - self.next
        self.banner.setText(f"Tap mode — press Space on every note   ·   {self.taps} tapped, {left} to go   ·   "
                            f"Esc to stop")
        self.banner.adjustSize()

    def _place(self):
        w = self.win
        top = w.editor.mapTo(w, QPoint(0, 0))
        self.big.resize(190, 190)
        self.big.move(top.x() + (w.editor.width() - 190) // 2, top.y() + (w.editor.height() - 190) // 2)
        self.banner.adjustSize()
        self.banner.move(top.x() + (w.editor.width() - self.banner.width()) // 2, top.y() + 14)
