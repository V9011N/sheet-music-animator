"""Main window."""
from __future__ import annotations

from bisect import bisect_right
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox,
                               QFileDialog, QFontComboBox, QFormLayout, QFrame, QGraphicsView, QGroupBox, QHBoxLayout,
                               QLabel, QMainWindow, QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
                               QSplitter, QTabWidget, QToolBar, QVBoxLayout, QWidget)

from . import audio
from .engraver import engrave
from .export import render_frame, render_video, total_duration
from .project import CATEGORIES, CHANNEL_LABELS, CHANNELS, FIXED_KINDS, Key, Project, auto_camera
from .scene import EditorView, PreviewWidget, SheetScene
from .layout import relayout_project
from .timeline import Timeline, fmt
from .tour import Tour

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except ImportError:  # pragma: no cover - multimedia is optional, playback just goes silent
    QMediaPlayer = None

RESOLUTIONS = {"1920 × 1080 (16:9)": (1920, 1080), "1280 × 720 (16:9)": (1280, 720),
               "3840 × 2160 (4K)": (3840, 2160), "1080 × 1920 (vertical)": (1080, 1920),
               "1080 × 1080 (square)": (1080, 1080), "Custom": None}


def dark_palette() -> QPalette:
    p = QPalette()
    for role, c in ((QPalette.Window, "#262629"), (QPalette.WindowText, "#e6e6e6"), (QPalette.Base, "#1c1c1f"),
                    (QPalette.AlternateBase, "#262629"), (QPalette.Text, "#e6e6e6"), (QPalette.Button, "#333337"),
                    (QPalette.ButtonText, "#e6e6e6"), (QPalette.ToolTipBase, "#333337"),
                    (QPalette.ToolTipText, "#e6e6e6"), (QPalette.Highlight, "#2f7bff"),
                    (QPalette.HighlightedText, "#ffffff"), (QPalette.PlaceholderText, "#808085")):
        p.setColor(role, QColor(c))
    return p


SELECT_HINT = ("Click any element of the score (note, rest, beam, slur, clef, barline, arpeggio…) or the white "
               "space of a measure, or drag a box around several, to select them. Ctrl+click adds to the "
               "selection; Shift+click selects the measures in between.")


class History:
    """Undo/redo as a list of project snapshots (JSON text); `index` is the current one."""

    LIMIT = 300

    def __init__(self):
        self.states: list[str] = []
        self.index = -1

    def reset(self, state: str):
        self.states, self.index = [state], 0

    def push(self, state: str) -> bool:
        if self.states and state == self.states[self.index]:
            return False
        del self.states[self.index + 1:]
        self.states.append(state)
        if len(self.states) > self.LIMIT:
            del self.states[0]
        self.index = len(self.states) - 1
        return True

    def can_undo(self) -> bool:
        return self.index > 0

    def can_redo(self) -> bool:
        return self.index < len(self.states) - 1

    def undo(self) -> str:
        self.index -= 1
        return self.states[self.index]

    def redo(self) -> str:
        self.index += 1
        return self.states[self.index]


class TriBox(QCheckBox):
    """A tri-state checkbox that only toggles between 'all' and 'none' when clicked."""

    def nextCheckState(self):
        self.setCheckState(Qt.Unchecked if self.checkState() == Qt.Checked else Qt.Checked)


class LayoutDialog(QDialog):
    """Asks how many measures go on each line of the score."""

    def __init__(self, parent, default: int, measures: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Measures per line")
        self.result_value: int | None = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("How many measures should each line of the score hold?\n"
                             "This sets the size of the canvas and the automatic camera path."))
        row = QHBoxLayout()
        self.spin = QSpinBox()
        self.spin.setRange(1, 500)
        self.spin.setValue(max(default, 1))
        row.addWidget(self.spin)
        row.addWidget(QLabel("measures per line" + (f"  (the score has {measures})" if measures else "")))
        row.addStretch(1)
        lay.addLayout(row)
        buttons = QHBoxLayout()
        ok, one, cancel = QPushButton("OK"), QPushButton("Whole score on one line"), QPushButton("Cancel")
        ok.setDefault(True)
        ok.clicked.connect(lambda: self._done(self.spin.value()))
        one.clicked.connect(lambda: self._done(0))
        cancel.clicked.connect(self.reject)
        for b in (ok, one, cancel):
            buttons.addWidget(b)
        lay.addLayout(buttons)

    def _done(self, value: int):
        self.result_value = value
        self.accept()


def spin(lo, hi, step, decimals=2, suffix="") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sheet Music Animator")
        self.resize(1500, 900)
        self.cfg = QSettings("SheetMusicAnimator", "SheetMusicAnimator")
        self.project = Project()
        self.project_path: str | None = None
        self.score = None
        self.scene: SheetScene | None = None
        self.t = 0.0
        self.playing = False
        self.wav_path: str | None = None
        self._updating = False
        self._clock = QElapsedTimer()
        self._sel_units, self._sel_measures, self._reselecting = [], [], False
        self.history = History()
        self._saved_state = self.project.snapshot()
        self._editor_clock = QElapsedTimer()
        self._clock_t0 = 0.0
        self._timer = QTimer(self, interval=16)
        self._timer.timeout.connect(self._tick)
        self.player = self.audio_out = None
        if QMediaPlayer is not None:
            try:
                self.player = QMediaPlayer(self)
                self.audio_out = QAudioOutput(self)
                self.player.setAudioOutput(self.audio_out)
            except Exception:
                self.player = None
        self.tour = None
        self._build_ui()
        self._build_actions()
        self._sync_settings_to_ui()
        self._update_enabled()

    # ================================================================== UI construction
    def _build_ui(self):
        self.timeline = Timeline()
        self.timeline.seeked.connect(self.seek)
        self.timeline.keysChanged.connect(self._keys_changed)
        self.timeline.keysEditFinished.connect(self.commit)
        self.timeline.addKeyRequested.connect(self.add_key_at)
        shown = self.cfg.value("timeline_channels", "pos,size,rot")
        self.timeline.set_visible_channels(str(shown).split(","))
        self.timeline.channelsChanged.connect(
            lambda: self.cfg.setValue("timeline_channels", ",".join(self.timeline.visible_channels)))

        self.editor = EditorView()
        self.editor.cameraEdited.connect(self._camera_dragged)
        self.editor.cameraEditFinished.connect(self._camera_edit_finished)
        self.editor.setDragMode(EditorView.RubberBandDrag)
        self.preview = PreviewWidget()

        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 6, 6, 6)
        pv = QGroupBox("Camera view")
        pvl = QVBoxLayout(pv)
        pvl.setContentsMargins(4, 4, 4, 4)
        pvl.addWidget(self.preview)
        sl.addWidget(pv, 3)
        self.tabs = QTabWidget()
        self.tab_selection = self._note_tab()
        self.tabs.addTab(self._camera_tab(), "Camera")
        self.tabs.addTab(self._look_tab(), "Look && timing")   # a single & would be eaten as a shortcut marker
        self.tabs.addTab(self._output_tab(), "Output")
        self.tabs.addTab(self.tab_selection, "Selection")
        sl.addWidget(self.tabs, 2)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.editor)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([1000, 420])

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self.timeline)
        cl.addWidget(split, 1)
        self.setCentralWidget(central)
        self.status = self.statusBar()

    def _camera_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.cam_x, self.cam_y, self.cam_w = spin(-1e7, 1e7, 100, 0), spin(-1e7, 1e7, 100, 0), spin(100, 1e7, 100, 0)
        self.cam_rot = spin(-360, 360, 1, 1, "°")
        for s_ in (self.cam_x, self.cam_y, self.cam_w, self.cam_rot):
            s_.valueChanged.connect(self._camera_spin_changed)
        f.addRow("Center X", self.cam_x)
        f.addRow("Center Y", self.cam_y)
        f.addRow("Width", self.cam_w)
        f.addRow("Rotation", self.cam_rot)
        self.chk_autokey = QCheckBox("Auto keyframe when the camera is moved, resized or rotated")
        self.chk_autokey.setChecked(str(self.cfg.value("autokey", "true")).lower() == "true")
        self.chk_autokey.setToolTip("Off: dragging the camera only changes keyframes that already sit at the playhead.")
        self.chk_autokey.toggled.connect(lambda on: self.cfg.setValue("autokey", "true" if on else "false"))
        f.addRow(self.chk_autokey)
        self.sp_follow = spin(2000, 200000, 500, 0)
        self.sp_follow.setToolTip("Width of the camera used by 'Follow music' (about 200 units per staff space)")
        self.sp_follow.valueChanged.connect(self._settings_changed)
        f.addRow("Follow-music width", self.sp_follow)
        row = QHBoxLayout()
        for text, fn in (("Add keys here", lambda: self.add_key_at(None, self.t)),
                         ("Delete keys", self.timeline.delete_selected),
                         ("Follow music", self.generate_camera)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.btn_follow = b
        f.addRow(row)
        hint = QLabel("Drag the orange window on the sheet to move it, drag a corner to resize it and the round "
                      "handle above it to rotate (Shift snaps to 15°). Keyframes live on three channels "
                      "(position, frame size, rotation) in the timeline; the Camera label there chooses which are "
                      "shown. Double-click a lane to add a key; click/Ctrl+click/Shift+click/Ctrl+A to select; "
                      "right-click for easing.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9a9aa0")
        f.addRow(hint)
        return w

    def _look_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.cb_reveal = QComboBox()
        self.cb_reveal.addItem("Fade in", "fade")
        self.cb_reveal.addItem("Appear instantly", "instant")
        self.sp_fade = spin(0, 3, 0.05, 2, " s")
        self.sp_ghost = spin(0, 0.5, 0.02, 2)
        self.sp_look = QSpinBox()
        self.sp_look.setRange(0, 64)
        self.sp_look.setSpecialValueText("All")
        self.sp_look.setSuffix(" measures")
        self.sp_look.setKeyboardTracking(False)
        self.sp_look.setToolTip("Unplayed notes are only drawn this many measures ahead of the playhead, "
                                "which keeps long pieces fast.")
        self.sp_offset = spin(-5, 5, 0.05, 2, " s")
        self.sp_tail = spin(0, 30, 0.5, 1, " s")
        self.sp_mpl = QSpinBox()
        self.sp_mpl.setRange(1, 500)
        self.sp_mpl.setSuffix(" measures")
        self.sp_mpl.setKeyboardTracking(False)
        self.btn_one_line = QPushButton("Whole score on one line")
        self.font_box = QFontComboBox()
        self.font_box.setEditable(True)
        self.font_box.currentFontChanged.connect(lambda f: self._font_changed(f.family()))
        self.btn_ink, self.btn_paper = QPushButton(), QPushButton()
        for b, which in ((self.btn_ink, "ink"), (self.btn_paper, "paper")):
            b.clicked.connect(lambda _=False, which=which: self._pick_color(which))
        for s in (self.sp_fade, self.sp_ghost, self.sp_look, self.sp_offset, self.sp_tail):
            s.valueChanged.connect(self._settings_changed)
        self.sp_mpl.editingFinished.connect(lambda: self._layout_changed(self.sp_mpl.value()))
        self.btn_one_line.clicked.connect(lambda: self._layout_changed(0))
        self.cb_reveal.activated.connect(self._settings_changed)
        f.addRow("Note reveal", self.cb_reveal)
        f.addRow("Fade-in time", self.sp_fade)
        f.addRow("Unplayed notes opacity", self.sp_ghost)
        f.addRow("Unplayed notes shown ahead", self.sp_look)
        f.addRow("Shift all notes", self.sp_offset)
        f.addRow("End padding", self.sp_tail)
        f.addRow("Font (all text)", self.font_box)
        f.addRow("Measures per line", self.sp_mpl)
        f.addRow(self.btn_one_line)
        f.addRow("Ink colour", self.btn_ink)
        f.addRow("Paper colour", self.btn_paper)
        return w

    def _output_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.cb_res = QComboBox()
        self.cb_res.addItems(list(RESOLUTIONS))
        self.sp_w, self.sp_h = QSpinBox(), QSpinBox()
        for s in (self.sp_w, self.sp_h):
            s.setRange(64, 8192)
            s.setSingleStep(2)
            s.setKeyboardTracking(False)
            s.valueChanged.connect(self._settings_changed)
        self.cb_res.activated.connect(self._res_preset)
        self.cb_fps = QComboBox()
        self.cb_fps.addItems(["24", "30", "60"])
        self.cb_fps.activated.connect(self._settings_changed)
        self.cb_audio = QComboBox()
        self.cb_audio.addItems(["Built-in piano synth", "Audio file…", "No audio"])
        self.cb_audio.activated.connect(self._audio_changed)
        self.lbl_audio = QLabel("")
        self.lbl_audio.setStyleSheet("color:#9a9aa0")
        f.addRow("Resolution", self.cb_res)
        sizes = QHBoxLayout()
        sizes.addWidget(self.sp_w)
        sizes.addWidget(QLabel("×"))
        sizes.addWidget(self.sp_h)
        f.addRow("", sizes)
        f.addRow("Frame rate", self.cb_fps)
        f.addRow("Audio", self.cb_audio)
        f.addRow("", self.lbl_audio)
        b = QPushButton("Render video…")
        b.clicked.connect(self.render)
        b2 = QPushButton("Save current frame as PNG…")
        b2.clicked.connect(self.save_frame)
        f.addRow(b)
        f.addRow(b2)
        return w

    def _note_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.lbl_sel = QLabel(SELECT_HINT)
        self.lbl_sel.setWordWrap(True)
        self.chk_timed = QCheckBox("Reveal with the music (otherwise always visible)")
        self.chk_timed.setVisible(False)
        self.chk_timed.toggled.connect(self._timed_toggled)
        self.sp_note = spin(-10, 10, 0.05, 2, " s")
        self.sp_note.valueChanged.connect(self._note_offset_changed)
        reset = QPushButton("Reset to XML timing")
        reset.clicked.connect(lambda: self.sp_note.setValue(0.0))
        self.btn_reset_geom = QPushButton("Reset position and size")
        self.btn_reset_geom.clicked.connect(self._reset_geometry)
        self.lbl_geom = QLabel("Drag the selected engraving to move it; drag a corner handle to resize it. "
                               "Noteheads, note tails and beams cannot be moved.")
        self.lbl_geom.setWordWrap(True)
        self.lbl_geom.setStyleSheet("color:#9a9aa0")
        # measures
        self.grp_measures = QGroupBox("Show in the selected measures")
        gl = QVBoxLayout(self.grp_measures)
        self.cat_boxes: dict[str, TriBox] = {}
        for cat in CATEGORIES:
            cb = TriBox(cat)
            cb.setTristate(True)
            cb.clicked.connect(lambda _=False, cat=cat: self._category_clicked(cat))
            self.cat_boxes[cat] = cb
            gl.addWidget(cb)
        self.btn_line_up = QPushButton("Move this line up to the previous line")
        self.btn_line_up.clicked.connect(self.move_lines_up)
        gl.addWidget(self.btn_line_up)
        self.btn_line_down = QPushButton("Move from here to the next line")
        self.btn_line_down.setToolTip("This measure and the ones after it on its line move to the start of the next "
                                      "line (a new line is made after the last one)")
        self.btn_line_down.clicked.connect(self.move_measures_down)
        gl.addWidget(self.btn_line_down)
        self.grp_measures.setVisible(False)
        f.addRow(self.lbl_sel)
        f.addRow(self.chk_timed)
        f.addRow("Reveal earlier / later", self.sp_note)
        f.addRow(reset)
        f.addRow(self.lbl_geom)
        f.addRow(self.btn_reset_geom)
        f.addRow(self.grp_measures)
        # the category list is long: scroll it instead of making the window taller than the screen
        self.sel_scroll = QScrollArea()
        self.sel_scroll.setWidgetResizable(True)
        self.sel_scroll.setFrameShape(QFrame.NoFrame)
        self.sel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sel_scroll.setWidget(w)
        return self.sel_scroll

    def _build_actions(self):
        tb = self.toolbar = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, fn, shortcut=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(fn)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
            return a

        self.a_open = act("Open MusicXML…", self.open_xml, "Ctrl+O")
        self.a_openp = act("Open project…", self.open_project, "Ctrl+Shift+O")
        self.a_save = act("Save project", self.save_project, "Ctrl+S")
        self.a_play = act("▶  Play", self.toggle_play, "Space")
        self.a_home = act("⏮", lambda: self.seek(0.0), "Home", "Back to start")
        self.a_undo = act("↶", self.undo, "Ctrl+Z", "Undo (Ctrl+Z)")
        self.a_redo = act("↷", self.redo, None, "Redo (Ctrl+Y or Ctrl+Shift+Z)")
        self.a_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.a_key = act("◆ Key", lambda: self.add_key_at(None, self.t), "K", "Add camera keyframes at the playhead (K)")
        self.a_fit = act("Fit sheet", self.editor_fit, "F", "Fit the sheet to the editor (F)")
        self.a_cam = act("Show camera", self.editor_to_camera, "C", "Centre the editor on the camera (C)")
        self.a_render = act("Render…", self.render, "Ctrl+R")
        self.a_guide = act("? Guide", self.start_tour, None, "Walk through the features with an interactive guide")
        for a in (self.a_open, self.a_openp, self.a_save, self.a_undo, self.a_redo):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.a_home, self.a_play):
            tb.addAction(a)
        self.lbl_time = QLabel("0:00.0 / 0:00.0")
        self.lbl_time.setStyleSheet("font-family:Consolas,monospace; padding:0 10px;")
        tb.addWidget(self.lbl_time)
        tb.addSeparator()
        for a in (self.a_key, self.a_fit, self.a_cam):
            tb.addAction(a)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.a_guide)
        tb.addAction(self.a_render)
        for key, fn in (("Left", lambda: self.seek(self.t - 0.1)), ("Right", lambda: self.seek(self.t + 0.1)),
                        ("Shift+Left", lambda: self.seek(self.t - 1)), ("Shift+Right", lambda: self.seek(self.t + 1))):
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(fn)
            self.addAction(a)

    # ================================================================== guide
    def start_tour(self):
        if self.tour is None:
            self.tour = Tour(self)
        if self.tour.active:
            return
        self.tour.chk_again.setChecked(False)
        self.tour.start()

    def maybe_start_tour(self):
        """Offer the guide on the first run (until it has been finished or skipped)."""
        if str(self.cfg.value("tour_done", "false")).lower() != "true":
            self.start_tour()

    def _update_enabled(self):
        has = self.score is not None
        for a in (self.a_play, self.a_home, self.a_key, self.a_fit, self.a_cam, self.a_render, self.a_save):
            a.setEnabled(has)
        self.a_undo.setEnabled(self.history.can_undo())
        self.a_redo.setEnabled(self.history.can_redo())
        self.tabs.setEnabled(True)

    # ================================================================== loading
    def _confirm_discard(self) -> bool:
        """True when it is fine to throw away the current project (saved, or the user says so)."""
        if self.scene is None or not self.is_dirty():
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save the changes to this project first?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            self.save_project()
            return not self.is_dirty()
        return True

    def open_xml(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open MusicXML", self.cfg.value("last_dir", ""),
            "MusicXML (*.mxl *.musicxml *.xml);;All files (*)")
        if path:
            self.open_xml_path(path)

    def open_xml_path(self, path: str) -> bool:
        """Start a new project from a MusicXML file, asking how many measures go on a line."""
        dlg = LayoutDialog(self, int(self.cfg.value("measures_per_line", 4)))
        if dlg.exec() != QDialog.Accepted:
            return False
        self.cfg.setValue("last_dir", str(Path(path).parent))
        if dlg.result_value:
            self.cfg.setValue("measures_per_line", dlg.result_value)
        old = self.project.settings
        self.project = Project(xml_path=path, settings=old)
        self.project.settings.measures_per_line = dlg.result_value
        self.project.settings.layout = "horizontal" if dlg.result_value == 0 else "pages"
        self.project_path = None
        self._sync_settings_to_ui()
        return self.load_score()

    def open_project(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", self.cfg.value("last_dir", ""),
                                              "Sheet Music Animator project (*.smanim);;All files (*)")
        if path:
            try:
                project = Project.load(path)
            except Exception as e:
                QMessageBox.critical(self, "Could not open project", str(e))
                return
            self.project = project
            self.project_path = path
            self.cfg.setValue("last_dir", str(Path(path).parent))
            self._sync_settings_to_ui()
            self.load_score()

    def save_project(self):
        if not self.project_path:
            default = str(Path(self.project.xml_path).with_suffix(".smanim")) if self.project.xml_path else ""
            path, _ = QFileDialog.getSaveFileName(self, "Save project", default, "Sheet Music Animator project (*.smanim)")
            if not path:
                return
            self.project_path = path
        self.project.save(self.project_path)
        self._saved_state = self.project.snapshot()
        self._update_title()
        self.status.showMessage(f"Saved {self.project_path}", 4000)

    def is_dirty(self) -> bool:
        return self.project.snapshot() != self._saved_state

    def _update_title(self):
        name = Path(self.project.xml_path).name if self.project.xml_path else ""
        self.setWindowTitle(f"Sheet Music Animator — {name}{' *' if self.scene and self.is_dirty() else ''}")

    def closeEvent(self, e):
        self.pause()
        if self._confirm_discard():
            super().closeEvent(e)
        else:
            e.ignore()

    def _engrave(self):
        s = self.project.settings
        return engrave(self.project.xml_path, s.layout, s.ink, None,
                       measures_per_line=None if s.measures_per_line < 0 else s.measures_per_line,
                       line_starts=self.project.line_starts)

    def load_score(self, old_score=None, fresh: bool = True) -> bool:
        """(Re)engrave the project's MusicXML and rebuild the scene.  With `old_score` the project is carried
        over from that layout (undo history is kept); otherwise a fresh history starts."""
        self.pause()
        s = self.project.settings
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.status.showMessage("Engraving…")
            QApplication.processEvents()
            score = self._engrave()
            self.status.showMessage("Building scene…")
            QApplication.processEvents()
            if old_score is not None:
                relayout_project(self.project, old_score, score)
            self.score = score
            self.scene = SheetScene(score, self.project)
            self._sel_units, self._sel_measures = [], []
            self.editor.setScene(self.scene)
            self.preview.view.setScene(self.scene)
            if not self.project.has_keys():
                self.project.channels = auto_camera(score, s)
            self._build_audio()
            self.timeline.set_data(self.project, score, total_duration(self.scene, self.project))
            self.scene.selectionChanged.connect(self._selection_changed)
            self.scene.geometryChanged.connect(self._geometry_changed)
            self.scene.editFinished.connect(self.commit)
            self._layout_key = self._layout_signature()
            if fresh:
                self.history.reset(self.project.snapshot())
                self._saved_state = self.history.states[0]
            self._update_title()
            self.seek(0.0)
            QTimer.singleShot(60, self.editor_fit)  # after the window has been laid out
            self.status.showMessage(f"{len(score.units)} elements, {len(score.notes)} notes, "
                                    f"{fmt(score.duration)} of music", 6000)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.status.clearMessage()
            QMessageBox.critical(self, "Could not load score", f"{type(e).__name__}: {e}")
            return False
        QApplication.restoreOverrideCursor()
        self._update_enabled()
        return True

    def _layout_signature(self):
        s = self.project.settings
        return (s.measures_per_line, tuple(self.project.line_starts or ()), s.ink, self.project.xml_path)

    def _build_audio(self):
        self.wav_path = None
        mode = self.project.settings.audio
        if self.player is None:
            return
        if mode == "synth" and self.score and self.score.notes:
            self.status.showMessage("Synthesising audio…")
            QApplication.processEvents()
            out = Path(tempfile.gettempdir()) / "sheet_music_animator"
            out.mkdir(exist_ok=True)
            self.wav_path = str(out / f"{Path(self.project.xml_path).stem}.wav")
            audio.write_wav(self.wav_path, audio.synthesize(self.score.notes, self.score.duration))
        elif mode not in ("synth", "none") and Path(mode).exists():
            self.wav_path = mode
        self.player.setSource(QUrl.fromLocalFile(self.wav_path) if self.wav_path else QUrl())

    # ================================================================== time / playback
    def end_time(self) -> float:
        return total_duration(self.scene, self.project) if self.scene else 0.0

    def seek(self, t: float):
        if self.scene is None:
            return
        self.t = min(max(t, 0.0), self.end_time())
        if self.playing:
            self._clock_t0 = self.t
            self._clock.restart()
            if self.player and self.wav_path:
                self.player.setPosition(int(self.t * 1000))
        self._refresh_time()

    def toggle_play(self):
        self.pause() if self.playing else self.play()

    def play(self):
        if self.scene is None:
            return
        if self.t >= self.end_time() - 0.01:
            self.t = 0.0
        self.playing = True
        # The whole-page editor repaints far more than the preview; while playing redraw it ~15x/s by hand.
        self.editor.setViewportUpdateMode(QGraphicsView.NoViewportUpdate)
        self._editor_clock.start()
        self.a_play.setText("⏸  Pause")
        self._clock_t0 = self.t
        self._clock.start()
        if self.player and self.wav_path:
            self.player.setPosition(int(self.t * 1000))
            self.player.play()
        self._timer.start()

    def pause(self):
        if not self.playing:
            return
        self.playing = False
        self._timer.stop()
        self.editor.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.editor.viewport().update()
        self.a_play.setText("▶  Play")
        if self.player:
            self.player.pause()

    def _tick(self):
        t = self._clock_t0 + self._clock.elapsed() / 1000.0
        if t >= self.end_time():
            self.t = self.end_time()
            self.pause()
        else:
            self.t = t
        self._refresh_time()

    def _refresh_time(self, follow: bool = True):
        if self.scene is None:
            return
        self.scene.apply_time(self.t)
        pose = self.project.camera_pose(self.t)
        self.editor.cam, self.editor.aspect = pose, self.project.settings.aspect
        if not self.playing or self._editor_clock.elapsed() > 66:
            self._editor_clock.restart()
            self.editor.viewport().update()
        self.preview.set_aspect(self.project.settings.aspect)
        self.preview.view.set_camera(pose)
        self.timeline.set_time(self.t)
        self.lbl_time.setText(f"{fmt(self.t)} / {fmt(self.end_time())}")
        if pose:
            self._updating = True
            self.cam_x.setValue(pose[0])
            self.cam_y.setValue(pose[1])
            self.cam_w.setValue(pose[2])
            self.cam_rot.setValue(pose[4])
            self._updating = False
            if follow:
                view_rect = self.editor.mapToScene(self.editor.viewport().rect()).boundingRect()
                centre = QPointF(pose[0], pose[1])
                if not view_rect.adjusted(view_rect.width() * .1, view_rect.height() * .1,
                                          -view_rect.width() * .1, -view_rect.height() * .1).contains(centre):
                    self.editor.centerOn(centre)

    # ================================================================== camera editing
    def _current_camera(self, t):
        c = self.project.camera_at(t)
        if c:
            return c
        r = self.editor.mapToScene(self.editor.viewport().rect()).boundingRect()
        return r.center().x(), r.center().y(), r.width() * 0.6, 0.0

    def add_key_at(self, channel, t: float):
        """Add a keyframe at t on `channel` (None: on every channel shown in the timeline)."""
        if self.scene is None:
            return
        if self.project.camera_at(t) is None:   # no camera yet: start from what the editor shows
            cx, cy, w, rot = self._current_camera(t)
            self.project.set_camera(t, cx, cy, w, rot)
        keys = []
        for ch in ([channel] if channel else self.timeline.visible_channels):
            k = self.project.add_key(ch, t)
            if k is not None:
                keys.append(k)
        self.timeline.selected = set(keys)
        self._keys_changed()
        self.commit()

    def _apply_camera(self, cx, cy, w, rot, create=True):
        """Store a camera pose edited by hand.  Without auto keyframing only keys that already sit at the
        playhead change."""
        auto = self.chk_autokey.isChecked()
        keys = self.project.set_camera(self.t, cx, cy, w, rot, create=auto and create)
        if keys:
            self.timeline.selected = set(keys)
        elif not auto:
            self.status.showMessage("Auto keyframe is off: add a keyframe at the playhead (K) to edit the camera there.", 4000)
        self._refresh_time(follow=False)  # don't auto-scroll the editor while the user is dragging
        self.timeline.update()

    def _camera_dragged(self, cx, cy, w, rot):
        self._apply_camera(cx, cy, w, rot)

    def _camera_edit_finished(self):
        self._keys_changed()
        self.commit()

    def _camera_spin_changed(self):
        if self._updating or self.scene is None:
            return
        self._apply_camera(self.cam_x.value(), self.cam_y.value(), self.cam_w.value(), self.cam_rot.value())
        self.commit()

    def _keys_changed(self):
        self._refresh_time()
        self.timeline.update()

    def generate_camera(self):
        if self.score is None:
            return
        if self.project.keys_edited and QMessageBox.question(
                self, "Replace camera path?",
                "This replaces your camera keyframes with an automatic path that follows the music.") != QMessageBox.Yes:
            return
        self.project.channels = auto_camera(self.score, self.project.settings)
        self.project.keys_edited = False
        self.timeline.selected = set()
        self._keys_changed()
        self.commit()

    def editor_fit(self):
        if self.scene is None:
            return
        s = self.scene
        scale = self.editor.viewport().width() / max(s.score.width, 1.0)
        if self.project.settings.measures_per_line == 0:
            scale = min(scale, self.editor.viewport().height() / max(s.score.height, 1.0))
        self.editor.resetTransform()
        self.editor.scale(scale, scale)
        self.editor.horizontalScrollBar().setValue(0)
        self.editor.verticalScrollBar().setValue(0)
        self.editor_to_camera()

    def editor_to_camera(self):
        pose = self.project.camera_pose(self.t)
        if pose:
            self.editor.centerOn(QPointF(pose[0], pose[1]))

    # ================================================================== settings
    def _sync_settings_to_ui(self):
        s = self.project.settings
        self._updating = True
        self.sp_follow.setValue(s.follow_width)
        self.cb_reveal.setCurrentIndex(self.cb_reveal.findData(s.reveal))
        self.sp_fade.setValue(s.fade)
        self.sp_fade.setEnabled(s.reveal == "fade")
        self.sp_ghost.setValue(s.ghost)
        self.sp_look.setValue(s.lookahead)
        self.sp_look.setEnabled(s.ghost > 0)
        self.sp_offset.setValue(s.offset)
        self.sp_tail.setValue(s.tail)
        self.sp_mpl.setValue(s.measures_per_line if s.measures_per_line > 0 else int(self.cfg.value("measures_per_line", 4)))
        self.sp_mpl.setEnabled(True)
        self.font_box.setCurrentFont(QFont(s.font))
        self.font_box.lineEdit().setText(s.font)
        self.sp_w.setValue(s.width)
        self.sp_h.setValue(s.height)
        self.cb_fps.setCurrentText(str(s.fps))
        self.cb_res.setCurrentText(next((k for k, v in RESOLUTIONS.items() if v == (s.width, s.height)), "Custom"))
        self.cb_audio.setCurrentIndex({"synth": 0, "none": 2}.get(s.audio, 1))
        self.lbl_audio.setText(s.audio if s.audio not in ("synth", "none") else "")
        for b, c in ((self.btn_ink, s.ink), (self.btn_paper, s.paper)):
            b.setText(c)
            fg = "#000" if QColor(c).lightness() > 128 else "#fff"
            b.setStyleSheet(f"background:{c}; color:{fg};")
        self._updating = False

    def _settings_changed(self, *_):
        if self._updating:
            return
        s = self.project.settings
        s.follow_width = self.sp_follow.value()
        s.reveal = self.cb_reveal.currentData()
        s.lookahead = self.sp_look.value()
        self.sp_look.setEnabled(self.sp_ghost.value() > 0)
        self.sp_fade.setEnabled(s.reveal == "fade")
        s.fade, s.ghost, s.offset, s.tail = (self.sp_fade.value(), self.sp_ghost.value(),
                                             self.sp_offset.value(), self.sp_tail.value())
        s.width, s.height, s.fps = self.sp_w.value(), self.sp_h.value(), int(self.cb_fps.currentText())
        self._updating = True
        self.cb_res.setCurrentText(next((k for k, v in RESOLUTIONS.items() if v == (s.width, s.height)), "Custom"))
        self._updating = False
        if self.scene:
            self.scene.refresh()
            self.timeline.duration = max(self.end_time(), 1.0)
            self._refresh_time()
            self.commit()

    def _res_preset(self):
        size = RESOLUTIONS.get(self.cb_res.currentText())
        if size:
            self._updating = True
            self.sp_w.setValue(size[0])
            self.sp_h.setValue(size[1])
            self._updating = False
            self._settings_changed()

    def _layout_changed(self, n: int):
        """Change the number of measures per line (0: the whole score on one line): re-engrave, and carry
        the camera and the edits over to the new layout."""
        s = self.project.settings
        if self._updating or self.scene is None or (n == s.measures_per_line and self.project.line_starts is None):
            return
        old = self.score
        s.measures_per_line = n
        s.layout = "horizontal" if n == 0 else "pages"
        if n:
            self.cfg.setValue("measures_per_line", n)
        self.project.line_starts = None
        self._sync_settings_to_ui()
        if self.load_score(old_score=old, fresh=False):
            self.commit()

    def _font_changed(self, family: str):
        if self._updating or not family:
            return
        s = self.project.settings
        if family == s.font:
            return
        s.font = family
        if self.scene:
            self.scene.refresh()
            self.editor.viewport().update()
            self.preview.view.viewport().update()
        self.commit()

    def _pick_color(self, which):
        s = self.project.settings
        c = QColorDialog.getColor(QColor(getattr(s, which)), self, f"Choose {which} colour")
        if not c.isValid():
            return
        setattr(s, which, c.name())
        self._sync_settings_to_ui()
        if self.scene:
            if which == "ink":
                old = self.score
                if self.load_score(old_score=old, fresh=False):  # ink is baked into the vector layers
                    self.commit()
            else:
                self.scene.refresh()
                self.editor.viewport().update()
                self.commit()

    def _audio_changed(self):
        i = self.cb_audio.currentIndex()
        s = self.project.settings
        if i == 1:
            path, _ = QFileDialog.getOpenFileName(self, "Choose audio", self.cfg.value("last_dir", ""),
                                                  "Audio (*.wav *.mp3 *.flac *.ogg *.m4a);;All files (*)")
            if not path:
                self._sync_settings_to_ui()
                return
            s.audio = path
        else:
            s.audio = "synth" if i == 0 else "none"
        self._sync_settings_to_ui()
        if self.scene:
            self._build_audio()
            self.commit()

    # ================================================================== undo / redo
    def commit(self):
        """Record the current project state as an undo step (nothing happens if it did not change)."""
        if self.scene is None:
            return
        if self.history.push(self.project.snapshot()):
            self._update_enabled()
        self._update_title()

    def undo(self):
        if self.history.can_undo():
            self._apply_state(self.history.undo())

    def redo(self):
        if self.history.can_redo():
            self._apply_state(self.history.redo())

    def _apply_state(self, state: str):
        before = self._layout_signature()
        self.pause()
        self.project.restore(state)
        if self._layout_signature() != before:   # the lines were broken differently: engrave again
            self.load_score(fresh=False)
        else:
            self.scene.refresh()
            self.timeline.set_data(self.project, self.score, total_duration(self.scene, self.project))
            self._refresh_time(follow=False)
        self._sync_settings_to_ui()
        self.scene.clearSelection()
        self._update_selection_panel()
        self._update_enabled()
        self._update_title()

    # ================================================================== selection
    def _selection_changed(self):
        if self._reselecting:   # Qt deselects items that become hidden; the panel keeps its elements
            return
        self._sel_units = self.scene.selected_units() if self.scene else []
        self._sel_measures = self.scene.selected_measures() if self.scene else []
        self._update_selection_panel()

    def _update_selection_panel(self):
        units, measures = self._sel_units, self._sel_measures
        self._updating = True
        n = len(units) + len(measures)
        self.tabs.setTabText(self.tabs.indexOf(self.tab_selection), f"Selection ({n})" if n else "Selection")
        static = [u for u in units if u.static]
        self.chk_timed.setVisible(bool(static))
        movable = [u for u in units if u.kind not in FIXED_KINDS]
        self.lbl_geom.setVisible(bool(units))
        self.btn_reset_geom.setVisible(any(u.uid in self.project.transforms for u in units))
        if not units:
            self.sp_note.setValue(0.0)
            self.lbl_sel.setText(SELECT_HINT if not measures else
                                 f"{len(measures)} measure{'s' if len(measures) != 1 else ''} selected "
                                 f"(from {measures[0] + 1} to {measures[-1] + 1}).")
        else:
            u = units[0]
            self.chk_timed.setChecked(bool(static) and all(s.uid in self.project.timed for s in static))
            if u.static and u.uid not in self.project.timed:
                self.lbl_sel.setText(f"{len(units)} selected — {u.kind} is always visible. Tick the box or set a "
                                     f"time to make it appear with the music (XML timing {fmt(u.time)}).")
            else:
                self.lbl_sel.setText(f"{len(units)} selected — {u.kind} first heard at {fmt(u.time)} (XML timing)")
            self.sp_note.setValue(self.project.overrides.get(u.uid, 0.0))
        # measures: which categories are shown
        self.grp_measures.setVisible(bool(measures))
        if measures:
            for cat, cb in self.cat_boxes.items():
                hidden = [cat in self.project.hidden.get(m, ()) for m in measures]
                cb.setCheckState(Qt.Unchecked if all(hidden) else Qt.Checked if not any(hidden) else Qt.PartiallyChecked)
            starts = set(self.score.line_starts[1:])
            self.btn_line_up.setVisible(any(m in starts for m in measures))
            self.btn_line_down.setVisible(any(m not in self.score.line_starts for m in measures))
        self._updating = False

    def _retime_selection(self, change):
        """Apply `change(unit)` to the selected elements.  An element that is moved later than the playhead
        disappears, and Qt would drop it from the selection, so the selection is restored afterwards."""
        if self._updating or not self.scene:
            return
        units = self._sel_units
        for u in units:
            change(u)
        self._reselecting = True
        self.scene.apply_time(force=True)
        for u in units:
            self.scene.items_by_uid[u.uid].setSelected(True)   # no-op for hidden ones
        self._reselecting = False
        self._update_selection_panel()
        self._refresh_time(follow=False)
        self.commit()

    def _note_offset_changed(self, v):
        def change(u):
            if abs(v) < 1e-9:
                self.project.overrides.pop(u.uid, None)
            else:
                self.project.overrides[u.uid] = v
                if u.static:   # moving a clef or barline in time only makes sense once it is timed
                    self.project.timed.add(u.uid)
        self._retime_selection(change)

    def _timed_toggled(self, on):
        self._retime_selection(lambda u: u.static and (self.project.timed.add if on else self.project.timed.discard)(u.uid))

    def _geometry_changed(self):
        self._update_selection_panel()
        self.editor.viewport().update()

    def _reset_geometry(self):
        if not self.scene:
            return
        for u in self._sel_units:
            self.project.transforms.pop(u.uid, None)
        self.scene.refresh()
        self._update_selection_panel()
        self.editor.viewport().update()
        self.commit()

    def _category_clicked(self, cat: str):
        """Show or hide a category of engravings in the selected measures."""
        if self._updating or not self.scene:
            return
        hide = self.cat_boxes[cat].checkState() == Qt.Unchecked
        for m in self._sel_measures:
            cats = self.project.hidden.setdefault(m, set())
            (cats.add if hide else cats.discard)(cat)
            if not cats:
                self.project.hidden.pop(m, None)
        self._reselecting = True
        self.scene.refresh()
        self._reselecting = False
        self._refresh_time(follow=False)
        self.editor.viewport().update()
        self._update_selection_panel()
        self.commit()

    def move_lines_up(self):
        """Merge each selected line start with the line before it: the whole line joins the previous one."""
        if not self.scene:
            return
        starts = list(self.score.line_starts)
        drop = {m for m in self._sel_measures if m in starts[1:]}
        if not drop:
            return
        old = self.score
        self.project.line_starts = [m for m in starts if m not in drop]
        self.project.settings.layout = "pages" if len(self.project.line_starts) > 1 else "horizontal"
        if self.load_score(old_score=old, fresh=False):
            self.commit()

    def move_measures_down(self):
        """The selected measure, and every measure after it on its line, move to the start of the next line;
        after the last line a new one is made.  The system's clefs, key signature and bracket are engraved
        by Verovio at the start of every line."""
        if not self.scene:
            return
        starts = list(self.score.line_starts)
        first: dict[int, int] = {}   # line -> its first selected measure that is not the line's first
        for m in self._sel_measures:
            li = bisect_right(starts, m) - 1
            if m != starts[li]:
                first[li] = min(m, first.get(li, m))
        if not first:
            return
        for li in sorted(first, reverse=True):   # from the last line up, so the line numbers stay valid
            if li + 1 < len(starts):
                starts[li + 1] = first[li]
            else:
                starts.append(first[li])
        old = self.score
        self.project.line_starts = starts
        self.project.settings.layout = "pages"
        if self.load_score(old_score=old, fresh=False):
            self._sync_settings_to_ui()
            self.commit()

    # ================================================================== output
    def _audio_for_render(self):
        mode = self.project.settings.audio
        if mode == "none":
            return None
        return self.wav_path

    def render(self):
        if self.scene is None:
            return
        self.pause()
        default = str(Path(self.project.xml_path).with_suffix(".mp4"))
        path, _ = QFileDialog.getSaveFileName(self, "Render video", default, "MP4 video (*.mp4)")
        if not path:
            return
        total = int(self.end_time() * self.project.settings.fps)
        dlg = QProgressDialog("Rendering frames…", "Cancel", 0, max(total, 1), self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setWindowTitle("Rendering")

        fps, t_before, next_ui = self.project.settings.fps, self.t, [0.0]

        def progress(done, total):
            dlg.setMaximum(max(total, 1))
            dlg.setValue(done)
            dlg.setLabelText(f"Rendering frame {done} of {total}")
            now = time.perf_counter()
            if now >= next_ui[0]:   # let the camera window, preview and timeline follow the render...
                self.t = min(done / fps, self.end_time())
                self._refresh_time()
                next_ui[0] = time.perf_counter() + max(0.25, 8 * (time.perf_counter() - now))   # ...at ~10% cost
            QApplication.processEvents()
            return not dlg.wasCanceled()

        self.scene.set_cache(False)
        try:
            render_video(self.scene, self.project, path, self._audio_for_render(), progress)
        except Exception as e:
            dlg.close()
            QMessageBox.critical(self, "Render failed", str(e))
        else:
            dlg.close()
            if Path(path).exists():
                self.status.showMessage(f"Rendered {path}", 8000)
        finally:
            self.scene.set_cache(True)
            self.t = t_before
            self._refresh_time()

    def save_frame(self):
        if self.scene is None or not self.project.has_keys():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save frame", str(Path(self.project.xml_path).with_suffix(".png")),
                                              "PNG image (*.png)")
        if not path:
            return
        s = self.project.settings
        img = QImage(s.width, s.height, QImage.Format_RGBA8888)
        self.scene.set_cache(False)
        sel = self.scene.selectedItems()
        self.scene.clearSelection()
        render_frame(self.scene, self.project, self.t, img)
        for it in sel:
            it.setSelected(True)
        self.scene.set_cache(True)
        img.save(path)
        self.status.showMessage(f"Saved {path}", 5000)

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    win = MainWindow()
    win.show()
    QTimer.singleShot(400, win.maybe_start_tour)
    if len(sys.argv) > 1:
        arg = Path(sys.argv[1])
        if arg.suffix == ".smanim":
            win.project = Project.load(arg)
            win.project_path = str(arg)
            win._sync_settings_to_ui()
            win.load_score()
        else:
            win.open_xml_path(str(arg))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
