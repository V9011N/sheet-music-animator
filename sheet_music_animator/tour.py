"""An interactive guide: dims the window, spotlights one control at a time and waits for the user to try it."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QTimer, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QRegion, QShortcut
from PySide6.QtWidgets import QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

SAMPLE = Path(__file__).with_name("sample.musicxml")
ACCENT = "#ff9f1c"
PAD = 2          # space between the spotlighted control and the dimmed area.  Keep it small: the spotlight is
                 # also the part that reacts to the mouse, and a wide margin would light up slivers of the
                 # neighbouring controls (hover highlights) that are otherwise dimmed.
CARD_W = 380


@dataclass
class Step:
    title: str
    text: str
    target: Callable | None = None      # -> QWidget, QRect (window coordinates), list of those, or None
    tab: Callable | None = None         # -> the side-panel tab widget to show first
    setup: Callable | None = None       # setup(tour) -> done() ; the step waits for the user to do the task
    extra: tuple | None = None          # (button text, callback) for an optional shortcut
    kind: str = "step"                  # "welcome" | "step" | "end"


class Tour(QWidget):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.steps = build_steps(win)
        self.i = -1
        self._done: Callable | None = None
        self._finished_task = False
        self._conns: list = []
        self._hole: QRect | None = None
        self._key = None
        self._reveal = False
        self.hide()
        self.card = QFrame(self)
        self.card.setObjectName("tourCard")
        self.card.setFixedWidth(CARD_W)
        self.card.setStyleSheet(
            f"#tourCard{{background:#2b2b31;border:2px solid {ACCENT};border-radius:10px;}}"
            "QLabel{color:#e8e8ec;background:transparent;} QCheckBox{color:#c8c8d0;}"
            "QPushButton{padding:5px 12px;border-radius:5px;background:#43434c;color:#f0f0f4;border:none;}"
            "QPushButton:hover{background:#565661;}"
            f"QPushButton#primary{{background:{ACCENT};color:#1a1a1e;font-weight:bold;}}"
            "QPushButton#primary:hover{background:#ffb44d;}"
            "QPushButton#link{background:transparent;color:#a8a8b2;padding:5px 2px;}"
            "QPushButton#link:hover{color:#ffffff;}")
        v = QVBoxLayout(self.card)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(8)
        self.lbl_step = QLabel()
        self.lbl_step.setStyleSheet("color:#9a9aa6;font-size:11px;")
        self.lbl_title = QLabel()
        self.lbl_title.setStyleSheet("font-size:16px;font-weight:bold;")
        self.lbl_title.setWordWrap(True)
        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setTextFormat(Qt.RichText)
        self.lbl_state = QLabel()
        self.lbl_state.setWordWrap(True)
        self.btn_extra = QPushButton()
        self.chk_again = QCheckBox("Show this guide again next time I open the app")
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_end = QPushButton("End guide")
        self.btn_end.setObjectName("link")
        self.btn_back = QPushButton("‹ Back")
        self.btn_next = QPushButton()
        self.btn_next.setObjectName("primary")
        row.addWidget(self.btn_end)
        row.addStretch(1)
        row.addWidget(self.btn_back)
        row.addWidget(self.btn_next)
        for w in (self.lbl_step, self.lbl_title, self.lbl_text, self.lbl_state, self.btn_extra, self.chk_again):
            v.addWidget(w)
        v.addLayout(row)
        for b in (self.btn_end, self.btn_back, self.btn_next, self.btn_extra):
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
        self.btn_end.clicked.connect(lambda: self.stop(finished=False))
        self.btn_back.clicked.connect(lambda: self.go(self.i - 1))
        self.btn_next.clicked.connect(self._next)
        self.btn_extra.clicked.connect(self._extra)
        self.poll = QTimer(self, interval=120)
        self.poll.timeout.connect(self._tick)
        self.esc = QShortcut(QKeySequence("Esc"), win)
        self.esc.setEnabled(False)
        self.esc.activated.connect(lambda: self.stop(finished=False))

    # -- lifecycle ---------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.isVisible()

    def start(self):
        self.win.pause()
        self.setGeometry(self.win.rect())
        self.show()
        self.raise_()
        self.esc.setEnabled(True)
        QApplication.instance().installEventFilter(self)   # to follow the window and silence tooltips
        self.poll.start()
        self.go(0)

    def stop(self, finished: bool):
        self._disconnect()
        self.poll.stop()
        self.esc.setEnabled(False)
        QApplication.instance().removeEventFilter(self)
        self.hide()
        self.win.cfg.setValue("tour_done", "false" if self.chk_again.isChecked() else "true")
        self.win.editor.viewport().update()

    def eventFilter(self, obj, ev):
        if not self.isVisible():
            return False
        if ev.type() == QEvent.ToolTip:     # a tooltip over the spotlight only gets in the way of the guide
            return True
        if obj is self.win and ev.type() in (QEvent.Resize, QEvent.Move):
            self.setGeometry(self.win.rect())
            self._refresh(force=True)
        return False

    # -- stepping ----------------------------------------------------------------------------------
    def _next(self):
        self.go(self.i + 1) if self.i + 1 < len(self.steps) else self.stop(finished=True)

    def _extra(self):
        step = self.steps[self.i]
        if step.extra:
            step.extra[1]()

    def _disconnect(self):
        for sig, slot in self._conns:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._conns = []

    def flag(self, signal) -> list:
        """A one-element list that turns True the next time `signal` is emitted."""
        f = [False]

        def hit(*_):
            f[0] = True
        signal.connect(hit)
        self._conns.append((signal, hit))
        return f

    def go(self, i: int):
        if not 0 <= i < len(self.steps):
            return
        self._disconnect()
        self.i = i
        step = self.steps[i]
        if step.tab is not None:
            self.win.tabs.setCurrentWidget(step.tab())
        self._done = step.setup(self) if step.setup else None
        self._finished_task = False
        n = len(self.steps)
        welcome, end = step.kind == "welcome", step.kind == "end"
        self.lbl_step.setText("" if welcome or end else f"Step {i} of {n - 2}")
        self.lbl_step.setVisible(not (welcome or end))
        self.lbl_title.setText(step.title)
        self.lbl_text.setText(step.text)
        self.btn_extra.setVisible(step.extra is not None)
        if step.extra:
            self.btn_extra.setText(step.extra[0])
        self.chk_again.setVisible(welcome or end)
        self.btn_back.setVisible(i > 1)
        self.btn_end.setText("Skip guide" if welcome else "End guide")
        self.btn_end.setVisible(not end)
        self._set_state()
        self._reveal = True
        QApplication.processEvents()   # let the tab switch lay out before measuring
        self._refresh(force=True)
        self._reveal = False

    def _set_state(self):
        step = self.steps[self.i]
        if step.kind == "welcome":
            self.btn_next.setText("Start the guide ›")
            self.lbl_state.setVisible(False)
        elif step.kind == "end":
            self.btn_next.setText("Finish")
            self.lbl_state.setVisible(False)
        elif self._done is None:
            self.btn_next.setText("Next ›")
            self.lbl_state.setVisible(False)
        elif self._finished_task:
            self.btn_next.setText("Next ›")
            self.lbl_state.setText("<span style='color:#6bd66b'>✓ Nice — that worked.</span>")
            self.lbl_state.setVisible(True)
        else:
            self.btn_next.setText("Skip this step ›")
            self.lbl_state.setText(f"<span style='color:{ACCENT}'>Try it now — the highlighted area is live.</span>")
            self.lbl_state.setVisible(True)

    # -- layout of the spotlight and the card --------------------------------------------------------
    def _resolve(self, target) -> QRect | None:
        if target is None:
            return None
        if callable(target):
            target = target()
        items = target if isinstance(target, (list, tuple)) else [target]
        rect = QRect()
        for t in items:
            if callable(t) and not isinstance(t, (QRect, QWidget)):
                t = t()
            if isinstance(t, (list, tuple)):
                t = self._resolve(t)
            if isinstance(t, QRect):
                r = t
            elif t is not None and t.isVisible():
                sa = self.win.sel_scroll
                if sa.isAncestorOf(t):   # inside the scrolling Selection tab: bring it into view, clip to it
                    if self._reveal:
                        sa.ensureWidgetVisible(t, 0, 24)
                    view = QRect(sa.viewport().mapTo(self.win, QPoint(0, 0)), sa.viewport().size())
                    r = QRect(t.mapTo(self.win, QPoint(0, 0)), t.size()).intersected(view)
                    if r.isEmpty():
                        r = view
                else:
                    r = QRect(t.mapTo(self.win, QPoint(0, 0)), t.size())
            else:
                continue
            rect = rect.united(r) if not rect.isNull() else QRect(r)
        return None if rect.isNull() else rect

    def _refresh(self, force: bool = False):
        step = self.steps[self.i]
        hole = self._resolve(step.target)
        if hole is not None:
            hole = hole.adjusted(-PAD, -PAD, PAD, PAD).intersected(self.rect())
        key = (hole.getRect() if hole else None, self.size().toTuple(), self.i)
        if not force and key == self._key:
            return
        self._key = key
        self._hole = hole
        self.card.adjustSize()
        self.card.move(self._card_pos(hole))
        region = QRegion(self.rect())
        if hole is not None:
            region = region.subtracted(QRegion(hole))
        self.setMask(region.united(QRegion(self.card.geometry())))
        self.card.raise_()
        self.update()

    def _card_pos(self, hole: QRect | None) -> QPoint:
        w, h = self.card.width(), self.card.height()
        win = self.rect().adjusted(10, 10, -10, -10)
        if hole is None:
            return QPoint((self.width() - w) // 2, (self.height() - h) // 2)
        gap = 14
        for x, y in ((hole.center().x() - w // 2, hole.bottom() + gap),    # below
                     (hole.center().x() - w // 2, hole.top() - gap - h),   # above
                     (hole.right() + gap, hole.center().y() - h // 2),     # right
                     (hole.left() - gap - w, hole.center().y() - h // 2)): # left
            x = min(max(x, win.left()), win.right() - w)
            y = min(max(y, win.top()), win.bottom() - h)
            if not QRect(x, y, w, h).intersects(hole):
                return QPoint(x, y)
        # nowhere clear: the lower corner away from the spotlight
        x = win.left() if hole.center().x() > self.width() / 2 else win.right() - w
        return QPoint(x, win.bottom() - h)

    def _tick(self):
        if not self.isVisible():
            return
        if self._done is not None and not self._finished_task:
            try:
                ok = bool(self._done())
            except Exception:
                ok = False
            if ok:
                self._finished_task = True
                self._set_state()
                self._key = None
        self._refresh()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(6, 8, 14, 170))
        if self._hole is not None:
            pen = QPen(QColor(ACCENT), 3)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(self._hole.adjusted(-1, -1, 1, 1), 5, 5)


# ---------------------------------------------------------------------------------------------------
def build_steps(win) -> list[Step]:
    """The guide, in order.  A `setup` function notes the starting state and returns a function that says
    whether the user has now done what the step asks."""
    tl, P = win.timeline, win.project

    def act(action):
        return lambda: win.toolbar.widgetForAction(action)

    def tab_button(i):
        bar = win.tabs.tabBar()
        return lambda: QRect(bar.mapTo(win, bar.tabRect(i).topLeft()), bar.tabRect(i).size())

    def tabs_and_panel():
        return win.tabs

    def n_keys():
        return sum(len(v) for v in win.project.channels.values())

    def done_after(fn):
        """`fn(tour)` returns (baseline, test); the step is done when test(baseline) holds."""
        def setup(tour):
            base, test = fn(tour)
            return lambda: test(base)
        return setup

    def changed(getter):
        def setup(tour):
            base = getter()
            return lambda: getter() != base
        return setup

    def flagged(signal_getter):
        def setup(tour):
            f = tour.flag(signal_getter())
            return lambda: f[0]
        return setup

    def lanes():
        return [tl.label_rect(), tl.lanes_rect()]

    def use_sample():
        win.open_xml_path(str(SAMPLE))

    def scene_ready():
        return win.scene is not None

    def sel_group():
        return win.grp_measures if win.grp_measures.isVisible() else win.tabs

    def line_buttons():
        vis = [b for b in (win.btn_line_up, win.btn_line_down) if b.isVisible()]
        return vis or [win.grp_measures if win.grp_measures.isVisible() else win.tabs]

    def pause(setup):
        def wrapped(tour):
            win.pause()
            return setup(tour) if setup else None
        return wrapped

    S = Step
    return [
        S("Welcome to Sheet Music Animator",
          "This short guide walks through the whole workflow — load a score, move a camera over it, keyframe "
          "the camera on the timeline, tidy the engraving, and render a video. Each step lights up one control "
          "and asks you to try it for real.<br><br>You can leave at any time, and bring the guide back with "
          "the <b>? Guide</b> button in the toolbar.", kind="welcome"),
        S("Open a score",
          "Start with <b>Open ▸ Open XML…</b> (Ctrl+O): pick a .mxl, .musicxml or .xml file. You will be asked "
          "how many <b>measures go on each line</b> — that sets the size of the canvas. <b>Open ▸ Open PDF (EXPERIMENTAL!)</b> "
          "reads the music of an engraved PDF instead (with Audiveris or homr installed).<br><br>No file at hand? "
          "Use the sample score.", target=act(win.a_open), setup=lambda t: scene_ready,
          extra=("Use the sample score", use_sample)),
        S("The sheet",
          "This is the whole score as empty staves. Scroll to move around, <b>Ctrl+wheel</b> to zoom "
          "(or middle-drag to pan), and press <b>F</b> or <b>Fit sheet</b> to see everything. The notes appear "
          "when you play.", target=lambda: win.editor),
        S("Fit the score to a recording",
          "The flagship feature. <b>Fit Score to Recording…</b> listens to a recording of the piece and moves "
          "every note of the score to where it is played, so the animation lands exactly on the sound (the "
          "recording becomes the soundtrack, and your keyframes move along). When it finishes it tells you how "
          "confident it is, and <b>Sync Heat Map</b> colours the score green where the sync is sure of itself and "
          "red where it is not. <b>Use the Score's Own Timing</b> goes back.",
          target=[act(win.a_align), act(win.a_unalign), act(win.a_heat)]),
        S("Tap to Keyframe, and playback speed",
          "Once a recording is fitted, <b>Tap to Keyframe</b> lets you time the notes yourself: it counts down "
          "3 s, plays from the seeker, and every <b>Space</b> tap makes the next note appear (Space does not "
          "pause; <b>Esc</b> ends the mode). The <b>Speed</b> box next to the time plays anything from 0.1x to "
          "5.0x, which makes tapping along much easier.",
          target=[act(win.a_tap), win.sp_speed]),
        S("The camera window",
          "The orange rectangle is what the video will show. <b>Drag it</b> to move it, drag a corner to "
          "resize it, drag the round handle above it to rotate it (hold Shift to snap).<br><br>Try moving it "
          "now.", target=lambda: win.editor, setup=lambda t: _changed_cam(win)),
        S("Play it",
          "Press <b>Play</b> (or <b>Space</b>). Notes appear as they are heard, and the small preview on the "
          "right shows exactly what the camera sees. Press again to pause.",
          target=act(win.a_play), setup=lambda t: (lambda: win.playing)),
        S("Scrub the timeline",
          "Click or drag anywhere on the time ruler to jump around (←/→ nudge by 0.1 s, Shift+←/→ by 1 s).",
          target=lambda: tl.ruler_rect(), setup=pause(flagged(lambda: tl.seeked))),
        S("Add a keyframe",
          "The camera is animated with <b>keyframes</b>. Move the playhead somewhere, then move the camera and "
          "a key appears automatically. You can also press <b>K</b> or the <b>◆ Key</b> button, or "
          "<b>double-click a lane</b>.<br><br>Add a key now.",
          target=lanes, setup=done_after(lambda t: (n_keys(), lambda b: n_keys() > b))),
        S("Channels",
          "Position X, position Y, frame size and rotation each have their own lane, so changing one never "
          "disturbs the others: <i>Follow music</i> lays down x, and a height you set on y stays. Click "
          "<b>Camera ▾</b> to choose which lanes are shown.", target=lambda: tl.label_rect()),
        S("Select several keys",
          "Click a key to select it; <b>Ctrl+click</b> toggles one, <b>Shift+click</b> selects a range and "
          "<b>Ctrl+A</b> selects them all. Dragging moves every selected key together; right-click for easing; "
          "<b>Delete</b> removes them.<br><br>Select two or more keys.",
          target=lanes, setup=lambda t: (lambda: len(tl.selected) >= 2)),
        S("Camera settings",
          "Exact numbers for the camera live here. Switch <b>Auto keyframe</b> off to move the camera without "
          "creating keys, or press <b>Follow music</b> to build a whole camera path that tracks the notes and "
          "glides from line to line. <b>Lead / lag</b> sets how far ahead of (or behind) the playing notes "
          "that camera sits.", tab=lambda: win.tab_camera, target=tabs_and_panel,
          setup=flagged(lambda: win.btn_follow.clicked)),
        S("Appear instantly or fade in",
          "Under <b>Look &amp; timing</b>, <b>Note reveal</b> chooses whether notes pop in or fade in. Switch it "
          "and press Play to see the difference. The same tab has the font for all text, the faint "
          "“ghost” of unplayed notes, a global timing shift for audio sync and the ink and paper colours.",
          tab=lambda: win.tab_look, target=lambda: win.cb_reveal, setup=flagged(lambda: win.cb_reveal.activated)),
        S("Measures per line",
          "This sets how many measures share a line (or put the whole score on one line). The canvas and the "
          "automatic camera follow. Change the number and press Enter.", tab=lambda: win.tab_look,
          target=lambda: [win.sp_mpl, win.btn_one_line],
          setup=changed(lambda: tuple(win.score.line_starts) if win.score else ())),
        S("Select a measure",
          "Click the <b>white space inside a measure</b> to select it (not a note). <b>Shift+click</b> selects "
          "a range, <b>Ctrl+click</b> adds one. The Selection tab then offers things to do with those measures."
          "<br><br>Select a measure.", tab=lambda: win.tab_selection, target=lambda: win.editor,
          setup=lambda t: (lambda: bool(win.scene and win.scene.selected_measures()))),
        S("Hide kinds of engraving",
          "With measures selected you can hide a whole category — fingerings, tuplet numbers, dynamics, slurs, "
          "ornaments… — just for those measures. Untick one of the categories.", tab=lambda: win.tab_selection, target=sel_group,
          setup=changed(lambda: repr(sorted((m, sorted(c)) for m, c in win.project.hidden.items())))),
        S("Change the line breaks",
          "Select a measure in the <i>middle</i> of a line and press <b>Move from here to the next line</b>: "
          "it and the rest of its line move down (a new line is made after the last one, with its own clefs, "
          "key signature and bracket). Select the <i>first</i> measure of a line and <b>Move this line up</b> "
          "joins it to the previous line. The canvas and camera follow.", tab=lambda: win.tab_selection, target=line_buttons,
          setup=changed(lambda: tuple(win.score.line_starts) if win.score else ())),
        S("Move, stretch, rotate and delete engravings",
          "Click almost any engraved element — clef, barline, slur, dynamic, text, accidental — then "
          "<b>drag it</b> to move it. The white handles <b>stretch</b> it (edges one way, corners both), the "
          "round handle above it <b>rotates</b> it (Shift snaps to 15°), and <b>Delete</b> removes it. "
          "(Noteheads, note tails and beams stay put.) <i>Reset position and size</i> in the Selection tab "
          "undoes it, and <i>Restore deleted engravings</i> brings deleted ones back.",
          tab=lambda: win.tab_selection, target=lambda: win.editor, setup=changed(lambda: repr(sorted(win.project.transforms.items())))),
        S("Time one element",
          "Select any element, such as a note, and use <b>Reveal earlier / later</b> to shift when it appears. "
          "Clefs, barlines and the like at the start of a line are always visible unless you time them.",
          tab=lambda: win.tab_selection, target=lambda: win.sp_note, setup=changed(lambda: repr(sorted(win.project.overrides.items())))),
        S("Undo and redo",
          "Every change can be undone — <b>↶</b> or <b>Ctrl+Z</b>; <b>↷</b> or <b>Ctrl+Y</b> redoes. Press "
          "undo now.", target=[act(win.a_undo), act(win.a_redo)], setup=flagged(lambda: win.a_undo.triggered)),
        S("Looks and layers",
          "The <b>Effects</b> tab turns the plain page into a produced look: a stack of layers — backdrops, "
          "particles, glowing notes, spotlights, grading, text — that can move with the music. Start from one "
          "of the <b>Looks</b>, or tick <b>Produced look</b> to begin your own, then add layers and link their "
          "settings to the loudness, the notes or your own automation lanes.",
          tab=lambda: win.fx_panel, target=lambda: [win.fx_panel.chk_enabled, win.fx_panel.cb_look],
          setup=flagged(lambda: win.fx_panel.chk_enabled.toggled)),
        S("Output",
          "Choose the resolution and frame rate, and the audio: a built-in piano synth, your own recording, or "
          "none. <b>Render video…</b> writes the MP4 of everything the camera sees; <b>Save current "
          "frame as PNG…</b> grabs a single still.", tab=lambda: win.tab_output, target=tabs_and_panel),
        S("Save your project",
          "<b>Save project</b> (Ctrl+S) stores the camera keys, timing, hidden items, moved elements and line "
          "breaks in a .smanim file. Closing with unsaved changes asks first.", target=act(win.a_save)),
        S("That's the tour",
          "You know the whole workflow now. Press <b>? Guide</b> in the toolbar to run this again whenever "
          "you like.", kind="end"),
    ]


def _changed_cam(win):
    base = tuple(round(v, 1) for v in win.editor.cam) if win.editor.cam else None
    return lambda: win.editor.cam is not None and tuple(round(v, 1) for v in win.editor.cam) != base
