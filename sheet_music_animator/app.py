"""Main window."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QRectF, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPalette
from PySide6.QtWidgets import (QApplication, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QProgressDialog,
                               QPushButton, QSizePolicy, QSpinBox, QSplitter, QTabWidget, QToolBar, QVBoxLayout,
                               QWidget)

from . import audio
from .engraver import engrave
from .export import render_frame, render_video, total_duration
from .project import Project, auto_camera
from .scene import EditorView, PreviewWidget, SheetScene
from .timeline import Timeline, fmt

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
        self._build_ui()
        self._build_actions()
        self._sync_settings_to_ui()
        self._update_enabled()

    # ================================================================== UI construction
    def _build_ui(self):
        self.timeline = Timeline()
        self.timeline.seeked.connect(self.seek)
        self.timeline.keysChanged.connect(self._keys_changed)
        self.timeline.addKeyRequested.connect(self.add_key_at)

        self.editor = EditorView()
        self.editor.cameraEdited.connect(self._camera_dragged)
        self.editor.cameraEditFinished.connect(self._keys_changed)
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
        self.tabs.addTab(self._camera_tab(), "Camera")
        self.tabs.addTab(self._look_tab(), "Look & timing")
        self.tabs.addTab(self._output_tab(), "Output")
        self.tabs.addTab(self._note_tab(), "Selection")
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
        for s in (self.cam_x, self.cam_y, self.cam_w):
            s.valueChanged.connect(self._camera_spin_changed)
        f.addRow("Center X", self.cam_x)
        f.addRow("Center Y", self.cam_y)
        f.addRow("Width", self.cam_w)
        self.sp_follow = spin(2000, 200000, 500, 0)
        self.sp_follow.setToolTip("Width of the camera used by 'Follow music' (about 200 units per staff space)")
        self.sp_follow.valueChanged.connect(self._settings_changed)
        f.addRow("Follow-music width", self.sp_follow)
        row = QHBoxLayout()
        for text, fn in (("Add key here", lambda: self.add_key_at(self.t)),
                         ("Delete key", self.timeline.delete_selected),
                         ("Follow music", self.generate_camera)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addRow(row)
        hint = QLabel("Drag the orange window on the sheet to move it, drag its corners to resize. "
                      "Any change at the playhead creates/updates a keyframe. "
                      "Double-click the Camera lane to add a key; right-click a key for easing.")
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
        self.sp_offset = spin(-5, 5, 0.05, 2, " s")
        self.sp_tail = spin(0, 30, 0.5, 1, " s")
        self.cb_layout = QComboBox()
        self.cb_layout.addItem("Pages (systems stacked)", "pages")
        self.cb_layout.addItem("Horizontal (one long line)", "horizontal")
        self.btn_ink, self.btn_paper = QPushButton(), QPushButton()
        for b, which in ((self.btn_ink, "ink"), (self.btn_paper, "paper")):
            b.clicked.connect(lambda _=False, which=which: self._pick_color(which))
        for s in (self.sp_fade, self.sp_ghost, self.sp_offset, self.sp_tail):
            s.valueChanged.connect(self._settings_changed)
        self.cb_layout.activated.connect(self._layout_changed)
        self.cb_reveal.activated.connect(self._settings_changed)
        f.addRow("Note reveal", self.cb_reveal)
        f.addRow("Fade-in time", self.sp_fade)
        f.addRow("Unplayed notes opacity", self.sp_ghost)
        f.addRow("Shift all notes", self.sp_offset)
        f.addRow("End padding", self.sp_tail)
        f.addRow("Score layout", self.cb_layout)
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
        self.lbl_sel = QLabel("Click a note (or drag a box) in the sheet to select it.")
        self.lbl_sel.setWordWrap(True)
        self.sp_note = spin(-10, 10, 0.05, 2, " s")
        self.sp_note.valueChanged.connect(self._note_offset_changed)
        reset = QPushButton("Reset to XML timing")
        reset.clicked.connect(lambda: self.sp_note.setValue(0.0))
        f.addRow(self.lbl_sel)
        f.addRow("Reveal earlier / later", self.sp_note)
        f.addRow(reset)
        return w

    def _build_actions(self):
        tb = QToolBar("Main")
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
        self.a_key = act("◆ Key", lambda: self.add_key_at(self.t), "K", "Add camera keyframe at playhead (K)")
        self.a_fit = act("Fit sheet", self.editor_fit, "F", "Fit the sheet to the editor (F)")
        self.a_cam = act("Show camera", self.editor_to_camera, "C", "Centre the editor on the camera (C)")
        self.a_render = act("Render…", self.render, "Ctrl+R")
        for a in (self.a_open, self.a_openp, self.a_save):
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
        tb.addAction(self.a_render)
        for key, fn in (("Left", lambda: self.seek(self.t - 0.1)), ("Right", lambda: self.seek(self.t + 0.1)),
                        ("Shift+Left", lambda: self.seek(self.t - 1)), ("Shift+Right", lambda: self.seek(self.t + 1))):
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(fn)
            self.addAction(a)

    def _update_enabled(self):
        has = self.score is not None
        for a in (self.a_play, self.a_home, self.a_key, self.a_fit, self.a_cam, self.a_render, self.a_save):
            a.setEnabled(has)
        self.tabs.setEnabled(True)

    # ================================================================== loading
    def open_xml(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open MusicXML", self.cfg.value("last_dir", ""),
            "MusicXML (*.mxl *.musicxml *.xml);;All files (*)")
        if path:
            self.cfg.setValue("last_dir", str(Path(path).parent))
            self.project = Project(xml_path=path, settings=self.project.settings)
            self.project_path = None
            self.load_score()

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open project", self.cfg.value("last_dir", ""),
                                              "Sheet Music Animator project (*.smanim);;All files (*)")
        if path:
            try:
                self.project = Project.load(path)
            except Exception as e:
                QMessageBox.critical(self, "Could not open project", str(e))
                return
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
        self.status.showMessage(f"Saved {self.project_path}", 4000)

    def load_score(self):
        """(Re)engrave the project's MusicXML and rebuild the scene."""
        self.pause()
        s = self.project.settings
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.status.showMessage("Engraving…")
            QApplication.processEvents()
            score = engrave(self.project.xml_path, s.layout, s.ink)
            self.status.showMessage("Building scene…")
            QApplication.processEvents()
            self.score = score
            self.scene = SheetScene(score, self.project)
            self.editor.setScene(self.scene)
            self.preview.view.setScene(self.scene)
            if not self.project.keys:
                self.project.keys = auto_camera(score, s)
            self._build_audio()
            self.timeline.set_data(self.project, score, total_duration(self.scene, self.project))
            self.setWindowTitle(f"Sheet Music Animator — {Path(self.project.xml_path).name}")
            self.scene.selectionChanged.connect(self._selection_changed)
            self.seek(0.0)
            QTimer.singleShot(60, self.editor_fit)  # after the window has been laid out
            self.status.showMessage(f"{len(score.units)} elements, {len(score.notes)} notes, "
                                    f"{fmt(score.duration)} of music", 6000)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.status.clearMessage()
            QMessageBox.critical(self, "Could not load score", f"{type(e).__name__}: {e}")
            return
        QApplication.restoreOverrideCursor()
        self._update_enabled()

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
        rect = self.project.camera_rect(self.t)
        qrect = QRectF(*rect) if rect else None
        self.editor.set_camera(qrect, self.project.settings.aspect)
        self.preview.set_aspect(self.project.settings.aspect)
        self.preview.view.set_camera(qrect)
        self.timeline.set_time(self.t)
        self.lbl_time.setText(f"{fmt(self.t)} / {fmt(self.end_time())}")
        if rect:
            self._updating = True
            self.cam_x.setValue(rect[0] + rect[2] / 2)
            self.cam_y.setValue(rect[1] + rect[3] / 2)
            self.cam_w.setValue(rect[2])
            self._updating = False
            if follow:
                view_rect = self.editor.mapToScene(self.editor.viewport().rect()).boundingRect()
                if not view_rect.adjusted(view_rect.width() * .1, view_rect.height() * .1,
                                          -view_rect.width() * .1, -view_rect.height() * .1).contains(qrect.center()):
                    self.editor.centerOn(qrect.center())

    # ================================================================== camera editing
    def _current_camera(self, t):
        c = self.project.camera_at(t)
        if c:
            return c
        r = self.editor.mapToScene(self.editor.viewport().rect()).boundingRect()
        return r.center().x(), r.center().y(), r.width() * 0.6

    def add_key_at(self, t: float):
        if self.scene is None:
            return
        cx, cy, w = self._current_camera(t)
        self.timeline.selected = self.project.set_key(t, cx, cy, w)
        self._keys_changed()

    def _camera_dragged(self, cx, cy, w):
        self.project.set_key(self.t, cx, cy, w)
        self.timeline.selected = next((k for k in self.project.keys if abs(k.t - self.t) <= 0.02), None)
        self._refresh_time(follow=False)  # don't auto-scroll the editor while the user is dragging

    def _camera_spin_changed(self):
        if self._updating or self.scene is None:
            return
        self.project.set_key(self.t, self.cam_x.value(), self.cam_y.value(), self.cam_w.value())
        self._keys_changed()

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
        self.project.keys = auto_camera(self.score, self.project.settings)
        self.project.keys_edited = False
        self._keys_changed()

    def editor_fit(self):
        if self.scene is None:
            return
        s = self.scene
        scale = self.editor.viewport().width() / max(s.score.width, 1.0)
        if self.project.settings.layout == "horizontal":
            scale = min(scale, self.editor.viewport().height() / max(s.score.height, 1.0))
        self.editor.resetTransform()
        self.editor.scale(scale, scale)
        self.editor.horizontalScrollBar().setValue(0)
        self.editor.verticalScrollBar().setValue(0)
        self.editor_to_camera()

    def editor_to_camera(self):
        rect = self.project.camera_rect(self.t)
        if rect:
            self.editor.centerOn(QRectF(*rect).center())

    # ================================================================== settings
    def _sync_settings_to_ui(self):
        s = self.project.settings
        self._updating = True
        self.sp_follow.setValue(s.follow_width)
        self.cb_reveal.setCurrentIndex(self.cb_reveal.findData(s.reveal))
        self.sp_fade.setValue(s.fade)
        self.sp_fade.setEnabled(s.reveal == "fade")
        self.sp_ghost.setValue(s.ghost)
        self.sp_offset.setValue(s.offset)
        self.sp_tail.setValue(s.tail)
        self.cb_layout.setCurrentIndex(self.cb_layout.findData(s.layout))
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

    def _res_preset(self):
        size = RESOLUTIONS.get(self.cb_res.currentText())
        if size:
            self._updating = True
            self.sp_w.setValue(size[0])
            self.sp_h.setValue(size[1])
            self._updating = False
            self._settings_changed()

    def _layout_changed(self):
        layout = self.cb_layout.currentData()
        if layout == self.project.settings.layout or self.scene is None:
            return
        if self.project.keys_edited and QMessageBox.question(
                self, "Change layout?", "Switching layout re-engraves the score and resets the camera path. Continue?"
        ) != QMessageBox.Yes:
            self._sync_settings_to_ui()
            return
        self.project.settings.layout = layout
        self.project.keys, self.project.keys_edited, self.project.overrides = [], False, {}
        self.load_score()

    def _pick_color(self, which):
        s = self.project.settings
        c = QColorDialog.getColor(QColor(getattr(s, which)), self, f"Choose {which} colour")
        if not c.isValid():
            return
        setattr(s, which, c.name())
        self._sync_settings_to_ui()
        if self.scene:
            if which == "ink":
                self.load_score()  # ink is baked into the vector layers
            else:
                self.scene.refresh()
                self.editor.viewport().update()

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

    # ================================================================== note selection
    def _selection_changed(self):
        units = self.scene.selected_units() if self.scene else []
        self._updating = True
        self.tabs.setTabText(3, f"Selection ({len(units)})" if units else "Selection")
        if not units:
            self.lbl_sel.setText("Click a note (or drag a box) in the sheet to select it.")
            self.sp_note.setValue(0.0)
        else:
            u = units[0]
            self.lbl_sel.setText(f"{len(units)} selected — {u.kind} first heard at {fmt(u.time)} (XML timing)")
            self.sp_note.setValue(self.project.overrides.get(u.uid, 0.0))
        self._updating = False

    def _note_offset_changed(self, v):
        if self._updating or not self.scene:
            return
        for u in self.scene.selected_units():
            if abs(v) < 1e-9:
                self.project.overrides.pop(u.uid, None)
            else:
                self.project.overrides[u.uid] = v
        self.scene.apply_time(force=True)

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

        fps, t_before, shown = self.project.settings.fps, self.t, QElapsedTimer()
        shown.start()

        def progress(done, total):
            dlg.setMaximum(max(total, 1))
            dlg.setValue(done)
            dlg.setLabelText(f"Rendering frame {done} of {total}")
            if shown.elapsed() > 100:   # let the camera window, preview and timeline follow the render
                shown.restart()
                self.t = min(done / fps, self.end_time())
                self._refresh_time()
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
        if self.scene is None or not self.project.keys:
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

    def closeEvent(self, e):
        self.pause()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        arg = Path(sys.argv[1])
        if arg.suffix == ".smanim":
            win.project = Project.load(arg)
            win.project_path = str(arg)
            win._sync_settings_to_ui()
        else:
            win.project.xml_path = str(arg)
        win.load_score()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
