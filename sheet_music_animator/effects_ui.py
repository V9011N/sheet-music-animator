"""The Effects tab: settings of the produced look (backdrop, light-up, reactions, title)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFontComboBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .project import Effects
from .presets import PRESETS

# (attribute, label, minimum, maximum, step, suffix) of the numeric settings
SPINS = {
    "backdrop": [("mist", "Mist", 0, 1, 0.05, ""), ("snow", "Snow", 0, 1, 0.05, ""),
                 ("vignette", "Dark corners", 0, 1, 0.05, ""),
                 ("edge_fade", "Score fades out at the sides", 0, 0.45, 0.01, " of width")],
    "flash": [("flash_time", "Glow lasts", 0.05, 3, 0.05, " s"), ("glow", "Bloom", 0, 4, 0.1, "")],
    "react": [("react", "Wind and snow follow the music", 0, 2, 0.05, ""), ("shake", "Camera shake", 0, 3, 0.05, ""),
              ("punch", "Zoom punch", 0, 3, 0.05, ""), ("breathe", "Zoom with loudness", 0, 3, 0.05, "")],
}
COLORS = {  # label -> (attribute, index or None)
    "Calm sky: top": ("bg_calm", 0), "Calm sky: bottom": ("bg_calm", 1),
    "Storm sky: top": ("bg_storm", 0), "Storm sky: bottom": ("bg_storm", 1),
    "Ink (calm)": ("ink_calm", None), "Ink (storm)": ("ink_storm", None),
    "Right hand glow": ("flash_colors", 0), "Left hand glow": ("flash_colors", 1),
    "Title": ("title_color", None),
}


def _spin(lo, hi, step, suffix="", decimals=2):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


def _tm(t: float) -> str:
    m, s = divmod(max(t, 0.0), 60)
    return f"{int(m)}:{s:04.1f}"


class EffectsPanel(QScrollArea):
    changed = Signal()            # a setting was edited
    presetRequested = Signal(str)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._updating = False
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)

        self.chk_enabled = QCheckBox("Produced look (backdrop, snow, light-up, reactions, title)")
        self.chk_enabled.toggled.connect(self._edited)
        self.chk_preview = QCheckBox("Show the effects in the camera view")
        self.chk_preview.setToolTip("Draws the camera view the way it will be rendered (slower than the plain view).")
        lay.addWidget(self.chk_enabled)
        lay.addWidget(self.chk_preview)
        row = QHBoxLayout()
        self.cb_preset = QComboBox()
        for key, label in PRESETS.items():
            self.cb_preset.addItem(label, key)
        self.btn_preset = QPushButton("Apply preset")
        self.btn_preset.setToolTip("Sets every effect and the mood/hush/lift keyframes from the measure numbers of "
                                   "the score. Align the score to its recording first.")
        self.btn_preset.clicked.connect(lambda: self.presetRequested.emit(self.cb_preset.currentData()))
        row.addWidget(self.cb_preset, 1)
        row.addWidget(self.btn_preset)
        lay.addLayout(row)

        self.spins: dict[str, QDoubleSpinBox] = {}
        self.color_buttons: dict[str, QPushButton] = {}

        def group(title, spins=(), colors=()):
            g = QGroupBox(title)
            f = QFormLayout(g)
            for attr, label, lo, hi, step, suf in spins:
                s = _spin(lo, hi, step, suf)
                s.valueChanged.connect(self._edited)
                self.spins[attr] = s
                f.addRow(label, s)
            for label in colors:
                b = QPushButton()
                b.clicked.connect(lambda _=False, label=label: self._pick(label))
                self.color_buttons[label] = b
                f.addRow(label, b)
            lay.addWidget(g)
            return f

        group("Backdrop and ink", SPINS["backdrop"],
              ("Calm sky: top", "Calm sky: bottom", "Storm sky: top", "Storm sky: bottom", "Ink (calm)", "Ink (storm)"))
        f = group("Notes light up as they sound", SPINS["flash"], ("Right hand glow", "Left hand glow"))
        self.chk_flash = QCheckBox("Light up notes")
        self.chk_flash.toggled.connect(self._edited)
        f.insertRow(0, self.chk_flash)

        f = group("Reactions to the music", SPINS["react"])
        self.chk_dyn = QCheckBox("Loud dynamics (f, ff, fff, sfz…) make events")
        self.chk_acc = QCheckBox("Accents and marcatos make events")
        for c in (self.chk_dyn, self.chk_acc):
            c.toggled.connect(self._edited)
            f.addRow(c)
        self.events = QListWidget()
        self.events.setMaximumHeight(90)
        self.events.setToolTip("Events: shake, zoom punch and a flash of light (strength 0.55+ flashes).")
        f.addRow(QLabel("Extra events:"))
        f.addRow(self.events)
        self.sp_strength = _spin(0.05, 1.0, 0.05)
        self.sp_strength.setValue(0.6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Strength"))
        row.addWidget(self.sp_strength)
        for text, fn in (("At playhead", self._add_event), ("Every note in selected measures", self._add_hits),
                         ("Remove", self._remove_event)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addRow(row)
        self.spots = QListWidget()
        self.spots.setMaximumHeight(60)
        f.addRow(QLabel("Spotlights (only these measures stay visible):"))
        f.addRow(self.spots)
        row = QHBoxLayout()
        for text, fn in (("Spotlight selected measures from the playhead", self._add_spot), ("Remove", self._remove_spot)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addRow(row)

        g = QGroupBox("Title and fades")
        f = QFormLayout(g)
        self.ed_title, self.ed_sub = QLineEdit(), QLineEdit()
        self.ed_title.setPlaceholderText("Title")
        self.ed_sub.setPlaceholderText("Subtitle")
        for e in (self.ed_title, self.ed_sub):
            e.editingFinished.connect(self._edited)
        self.font_box = QFontComboBox()
        self.font_box.currentFontChanged.connect(self._edited)
        self.color_buttons["Title"] = QPushButton()
        self.color_buttons["Title"].clicked.connect(lambda: self._pick("Title"))
        self.sp_tin, self.sp_tind = _spin(0, 600, 0.1, " s"), _spin(0.1, 30, 0.1, " s")
        self.sp_tout, self.sp_toutd = _spin(0, 600, 0.5, " s"), _spin(0.1, 30, 0.1, " s")
        self.sp_fade = _spin(0, 20, 0.1, " s")
        for s in (self.sp_tin, self.sp_tind, self.sp_tout, self.sp_toutd, self.sp_fade):
            s.valueChanged.connect(self._edited)
        f.addRow("Title", self.ed_title)
        f.addRow("Subtitle", self.ed_sub)
        f.addRow("Font", self.font_box)
        f.addRow("Colour", self.color_buttons["Title"])
        f.addRow("Fades in at", self.sp_tin)
        f.addRow("…over", self.sp_tind)
        f.addRow("Fades out at (0 = stays)", self.sp_tout)
        f.addRow("…over", self.sp_toutd)
        f.addRow("Picture fades to black for the last", self.sp_fade)
        lay.addWidget(g)

        hint = QLabel("The mood (calm → storm), hush and snow-lift automation lanes are in the timeline "
                      "(click “Camera ▾” to show them). Their keys use the same editing as camera keys; "
                      "select one to set its value here.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9a9aa0")
        lay.addWidget(hint)
        row = QHBoxLayout()
        self.lbl_key = QLabel("Selected effect key:")
        self.sp_key = _spin(0, 1, 0.05)
        self.sp_key.setEnabled(False)
        self.sp_key.valueChanged.connect(self._key_value)
        row.addWidget(self.lbl_key)
        row.addWidget(self.sp_key)
        lay.addLayout(row)
        lay.addStretch(1)

    # ------------------------------------------------------------------ project <-> widgets
    @property
    def fx(self) -> Effects:
        return self.win.project.effects

    def sync(self):
        fx = self.fx
        self._updating = True
        self.chk_enabled.setChecked(fx.enabled)
        self.chk_flash.setChecked(fx.flash)
        self.chk_dyn.setChecked(fx.use_dynamics)
        self.chk_acc.setChecked(fx.use_accents)
        for attr, s in self.spins.items():
            s.setValue(getattr(fx, attr))
        for label, (attr, i) in COLORS.items():
            v = getattr(fx, attr)
            c = v if i is None else v[i]
            b = self.color_buttons[label]
            b.setText(c)
            b.setStyleSheet(f"background:{c}; color:{'#000' if QColor(c).lightness() > 128 else '#fff'};")
        self.ed_title.setText(fx.title)
        self.ed_sub.setText(fx.subtitle)
        self.font_box.setCurrentFont(QFont(fx.title_font))
        self.sp_tin.setValue(fx.title_in[0])
        self.sp_tind.setValue(fx.title_in[1])
        self.sp_tout.setValue(fx.title_out[0])
        self.sp_toutd.setValue(fx.title_out[1])
        self.sp_fade.setValue(fx.fade_out)
        self._fill_lists()
        self._updating = False

    def _fill_lists(self):
        fx = self.fx
        self.events.clear()
        for im in fx.impulses:
            self.events.addItem(f"{_tm(im['t'])}   strength {im['s']:.2f}")
        for h in fx.hits:
            lo, hi = sorted((h["m0"], h["m1"]))
            self.events.addItem(f"every note in measures {lo + 1}–{hi + 1}   strength {h['s']:.2f}")
        self.spots.clear()
        for sp in fx.spotlights:
            lo, hi = sorted((sp["m0"], sp["m1"]))
            self.spots.addItem(f"from {_tm(sp['t'])}: measure{'s' if hi > lo else ''} {lo + 1}"
                               + (f"–{hi + 1}" if hi > lo else ""))

    def _edited(self, *_):
        if self._updating:
            return
        fx = self.fx
        fx.enabled = self.chk_enabled.isChecked()
        fx.flash = self.chk_flash.isChecked()
        fx.use_dynamics, fx.use_accents = self.chk_dyn.isChecked(), self.chk_acc.isChecked()
        for attr, s in self.spins.items():
            setattr(fx, attr, s.value())
        fx.title, fx.subtitle = self.ed_title.text(), self.ed_sub.text()
        fx.title_font = self.font_box.currentFont().family()
        fx.title_in = [self.sp_tin.value(), self.sp_tind.value()]
        fx.title_out = [self.sp_tout.value(), self.sp_toutd.value()]
        fx.fade_out = self.sp_fade.value()
        self.changed.emit()

    def _pick(self, label):
        attr, i = COLORS[label]
        v = getattr(self.fx, attr)
        c = QColorDialog.getColor(QColor(v if i is None else v[i]), self, label)
        if not c.isValid():
            return
        if i is None:
            setattr(self.fx, attr, c.name())
        else:
            v[i] = c.name()
        self.sync()
        self.changed.emit()

    # ------------------------------------------------------------------ events and spotlights
    def _add_event(self):
        self.fx.impulses.append({"t": round(self.win.t, 3), "s": self.sp_strength.value()})
        self.fx.impulses.sort(key=lambda i: i["t"])
        self._fill_lists()
        self.changed.emit()

    def _add_hits(self):
        ms = self.win._sel_measures
        if not ms:
            self.win.status.showMessage("Select some measures first.", 4000)
            return
        self.fx.hits.append({"m0": min(ms), "m1": max(ms), "s": self.sp_strength.value()})
        self._fill_lists()
        self.changed.emit()

    def _remove_event(self):
        i = self.events.currentRow()
        n = len(self.fx.impulses)
        if i < 0:
            return
        (self.fx.impulses if i < n else self.fx.hits).pop(i if i < n else i - n)
        self._fill_lists()
        self.changed.emit()

    def _add_spot(self):
        ms = self.win._sel_measures
        if not ms:
            self.win.status.showMessage("Select the measures to spotlight first.", 4000)
            return
        self.fx.spotlights.append({"t": round(self.win.t, 3), "ramp": 0.55, "end": 0.0, "m0": min(ms), "m1": max(ms)})
        self._fill_lists()
        self.changed.emit()

    def _remove_spot(self):
        i = self.spots.currentRow()
        if i >= 0:
            self.fx.spotlights.pop(i)
            self._fill_lists()
            self.changed.emit()

    # ------------------------------------------------------------------ the selected automation key
    def show_key(self, key):
        """`key`: the one selected effect-channel keyframe, or None."""
        self._key = key
        self._updating = True
        self.sp_key.setEnabled(key is not None)
        if key is not None:
            self.sp_key.setValue(key.v[0])
        self._updating = False

    def _key_value(self, v):
        key = getattr(self, "_key", None)
        if self._updating or key is None:
            return
        key.v = [v]
        self.win._keys_changed()
        self.win.commit()
