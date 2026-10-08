"""The Effects tab: the look (a stack of layers), the settings of the selected layer, signals and lanes."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFontComboBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget, QAbstractItemView)

from . import looks
from .layers import BLENDS, CATEGORIES, CURVES, LAYER_TYPES, SIGNALS, new_layer, new_id, schema
from .project import LANE

LAYER_ICONS = {"Backdrop": "▦", "Atmosphere": "✦", "Notation": "♪", "Finish": "◐", "Text": "T", "Camera": "◉"}


def _tm(t: float) -> str:
    m, s = divmod(max(t, 0.0), 60)
    return f"{int(m)}:{s:04.1f}"


def _spin(lo, hi, step, decimals=2, suffix=""):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


def _decimals(step):
    return 4 if step < 0.01 else 3 if step < 0.1 else 2 if step < 1 else 1


class EffectsPanel(QWidget):
    changed = Signal()               # something was edited (the preview needs rebuilding, an undo step is made)
    lookRequested = Signal(str)      # apply a look (key from looks.list_looks)
    saveLookRequested = Signal()
    deleteLookRequested = Signal(str)
    structureChanged = Signal()      # layers or lanes were added/removed/renamed (the timeline's lanes may change)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._updating = False
        self._key = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)

        self.chk_enabled = QCheckBox("Produced look (layers below are drawn)")
        self.chk_enabled.toggled.connect(self._enabled_toggled)
        self.chk_preview = QCheckBox("Show the effects in the camera view")
        self.chk_preview.setToolTip("Draws the camera view the way it will be rendered (slower than the plain view).")
        lay.addWidget(self.chk_enabled)
        lay.addWidget(self.chk_preview)

        # ---- looks
        g = QGroupBox("Looks")
        gl = QVBoxLayout(g)
        row = QHBoxLayout()
        self.cb_look = QComboBox()
        self.cb_look.currentIndexChanged.connect(self._look_tip)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setToolTip("Replace the layers, lanes and events by this look (undoable).")
        self.btn_apply.clicked.connect(lambda: self.lookRequested.emit(self.cb_look.currentData()))
        row.addWidget(self.cb_look, 1)
        row.addWidget(self.btn_apply)
        gl.addLayout(row)
        self.lbl_look = QLabel("")
        self.lbl_look.setWordWrap(True)
        self.lbl_look.setStyleSheet("color:#9a9aa0")
        gl.addWidget(self.lbl_look)
        row = QHBoxLayout()
        b = QPushButton("Save this look…")
        b.setToolTip("Keep the current layers, lanes and events as your own look (a JSON file you can share).")
        b.clicked.connect(self.saveLookRequested.emit)
        self.btn_del_look = QPushButton("Delete")
        self.btn_del_look.clicked.connect(lambda: self.deleteLookRequested.emit(self.cb_look.currentData()))
        b2 = QPushButton("Open folder")
        b2.clicked.connect(self._open_folder)
        for w in (b, self.btn_del_look, b2):
            row.addWidget(w)
        gl.addLayout(row)
        lay.addWidget(g)
        self.refresh_looks()

        # ---- layers
        g = QGroupBox("Layers (the top of the list is drawn last, in front)")
        gl = QVBoxLayout(g)
        self.list = QListWidget()
        self.list.setMinimumHeight(170)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.currentRowChanged.connect(self._select)
        self.list.itemChanged.connect(self._item_changed)
        self.list.model().rowsMoved.connect(self._reordered)
        gl.addWidget(self.list)
        row = QHBoxLayout()
        self.btn_add = QToolButton()
        self.btn_add.setText("Add layer ▾")
        self.btn_add.setPopupMode(QToolButton.InstantPopup)
        self.btn_add.setMenu(self._add_menu())
        for text, fn in (("Duplicate", self._duplicate), ("Remove", self._remove), ("▲", lambda: self._move(-1)),
                         ("▼", lambda: self._move(1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b if text not in ("▲", "▼") else b)
            if text in ("▲", "▼"):
                b.setMaximumWidth(34)
        row.insertWidget(0, self.btn_add)
        gl.addLayout(row)
        lay.addWidget(g)

        # ---- the selected layer
        self.grp_layer = QGroupBox("Selected layer")
        self.form_host = QVBoxLayout(self.grp_layer)
        lay.addWidget(self.grp_layer)
        self.lbl_blurb = QLabel("")
        self.lbl_blurb.setWordWrap(True)
        self.lbl_blurb.setStyleSheet("color:#9a9aa0")

        # ---- signals and events
        g = QGroupBox("Music signals and events")
        f = QFormLayout(g)
        self.chk_dyn = QCheckBox("Loud dynamics (f, ff, fff, sfz…) make events")
        self.chk_acc = QCheckBox("Accents and marcatos make events")
        for c in (self.chk_dyn, self.chk_acc):
            c.toggled.connect(self._signals_edited)
            f.addRow(c)
        self.sp_floor, self.sp_range, self.sp_dens = _spin(-90, 0, 1, 0, " dB"), _spin(3, 80, 1, 0, " dB"), _spin(1, 60, 1, 0, " /s")
        for s in (self.sp_floor, self.sp_range, self.sp_dens):
            s.valueChanged.connect(self._signals_edited)
        f.addRow("Loudness 0 at", self.sp_floor)
        f.addRow("…and 1 this much louder", self.sp_range)
        f.addRow("Note density 1 at", self.sp_dens)
        self.events = QListWidget()
        self.events.setMaximumHeight(90)
        f.addRow(QLabel("Extra events (layers follow them through 'events', 'big events' and 'every note'):"))
        f.addRow(self.events)
        self.sp_strength = _spin(0.05, 1.0, 0.05)
        self.sp_strength.setValue(0.6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Strength"))
        row.addWidget(self.sp_strength)
        for text, fn in (("At playhead", self._add_event), ("Every note in selected measures", self._add_hits), ("Remove", self._remove_event)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addRow(row)
        lay.addWidget(g)

        # ---- lanes
        g = QGroupBox("Automation lanes")
        gl = QVBoxLayout(g)
        hint = QLabel("A lane is a curve you keyframe in the timeline (any name, any number). Make any setting follow "
                      "one with its 'Link' button, e.g. a lane 'Storm' that fades the sky between two palettes.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9a9aa0")
        gl.addWidget(hint)
        self.lanes = QListWidget()
        self.lanes.setMaximumHeight(90)
        self.lanes.currentRowChanged.connect(self._lane_selected)
        gl.addWidget(self.lanes)
        row = QHBoxLayout()
        for text, fn in (("Add lane…", self._add_lane), ("Rename…", self._rename_lane), ("Remove", self._remove_lane)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        gl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Value without keyframes"))
        self.sp_lane_default = _spin(-100, 100, 0.1)
        self.sp_lane_default.valueChanged.connect(self._lane_default)
        row.addWidget(self.sp_lane_default)
        gl.addLayout(row)
        row = QHBoxLayout()
        self.lbl_key = QLabel("Selected keyframe value")
        self.sp_key = _spin(-100, 100, 0.05)
        self.sp_key.setEnabled(False)
        self.sp_key.valueChanged.connect(self._key_value)
        row.addWidget(self.lbl_key)
        row.addWidget(self.sp_key)
        gl.addLayout(row)
        lay.addWidget(g)
        lay.addStretch(1)

    # ------------------------------------------------------------------ project <-> widgets
    @property
    def fx(self):
        return self.win.project.effects

    def refresh_looks(self, select: str | None = None):
        self.cb_look.blockSignals(True)
        self.cb_look.clear()
        for item in looks.list_looks():
            self.cb_look.addItem(item["name"] + ("" if item["builtin"] else "  (yours)"), item["key"])
            self.cb_look.setItemData(self.cb_look.count() - 1, item["description"], Qt.ToolTipRole)
        if select:
            self.cb_look.setCurrentIndex(max(self.cb_look.findData(select), 0))
        self.cb_look.blockSignals(False)
        self._look_tip()

    def _look_tip(self, *_):
        i = self.cb_look.currentIndex()
        self.lbl_look.setText(self.cb_look.itemData(i, Qt.ToolTipRole) or "")
        self.btn_del_look.setEnabled(str(self.cb_look.currentData()).startswith("user:"))

    @staticmethod
    def _open_folder():
        import os
        looks.USER_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(looks.USER_DIR) if os.name == "nt" else None

    def sync(self):
        """Fill every widget from the project."""
        fx = self.fx
        self._updating = True
        self.chk_enabled.setChecked(fx.enabled)
        self.chk_dyn.setChecked(fx.use_dynamics)
        self.chk_acc.setChecked(fx.use_accents)
        self.sp_floor.setValue(fx.loud_floor_db)
        self.sp_range.setValue(fx.loud_range_db)
        self.sp_dens.setValue(fx.density_max)
        cur = self.current_layer()
        self._fill_layers(cur.id if cur else None)
        self._fill_events()
        self._fill_lanes()
        self._updating = False
        self._build_form()

    def current_layer(self):
        r = self.list.currentRow()
        if r < 0:
            return None
        lid = self.list.item(r).data(Qt.UserRole)
        return self.fx.layer(lid)

    def _fill_layers(self, select_id=None):
        self.list.blockSignals(True)
        self.list.clear()
        for lay in reversed(self.fx.layers):
            t = LAYER_TYPES[lay.type]
            it = QListWidgetItem(f"{LAYER_ICONS.get(t.category, '•')}  {lay.title()}   ·  {t.label}" if lay.name else
                                 f"{LAYER_ICONS.get(t.category, '•')}  {t.label}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(Qt.Checked if lay.enabled else Qt.Unchecked)
            it.setData(Qt.UserRole, lay.id)
            self.list.addItem(it)
            if lay.id == select_id:
                self.list.setCurrentItem(it)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)

    # ------------------------------------------------------------------ layer list actions
    def _enabled_toggled(self, on):
        if not self._updating:
            self.fx.enabled = on
            if on and not self.fx.layers:       # an empty stack would be a black screen: start from something
                self.fx.layers = [new_layer("gradient", "Backdrop"), new_layer("score", "Notation", color="#f2f2f2"),
                                  new_layer("highlight", "Note highlight")]
                self._after_structure(self.fx.layers[1].id)
                return
            self.structureChanged.emit()
            self.changed.emit()

    def _add_menu(self):
        menu = QMenu(self)
        for cat in CATEGORIES:
            sub = menu.addMenu(f"{LAYER_ICONS.get(cat, '')}  {cat}")
            for t in LAYER_TYPES.values():
                if t.category == cat:
                    a = sub.addAction(t.label)
                    a.setToolTip(t.blurb)
                    a.triggered.connect(lambda _=False, key=t.key: self._add(key))
        return menu

    def _add(self, type_key):
        lay = new_layer(type_key)
        cur = self.current_layer()
        i = self.fx.layers.index(cur) + 1 if cur else len(self.fx.layers)
        self.fx.layers.insert(i, lay)
        if not self.fx.enabled:
            self.fx.enabled = True
        self._after_structure(lay.id)

    def _duplicate(self):
        cur = self.current_layer()
        if cur:
            d = cur.to_dict()
            d["id"], d["name"] = new_id(), (cur.name or LAYER_TYPES[cur.type].label) + " copy"
            from .layers import Layer
            lay = Layer.from_dict(copy.deepcopy(d))
            self.fx.layers.insert(self.fx.layers.index(cur) + 1, lay)
            self._after_structure(lay.id)

    def _remove(self):
        cur = self.current_layer()
        if cur:
            i = self.fx.layers.index(cur)
            self.fx.layers.remove(cur)
            nxt = self.fx.layers[min(i, len(self.fx.layers) - 1)].id if self.fx.layers else None
            self._after_structure(nxt)

    def _move(self, d):
        cur = self.current_layer()
        if cur:
            L = self.fx.layers
            i = L.index(cur)
            j = i - d                      # the list is shown top (last drawn) first
            if 0 <= j < len(L):
                L[i], L[j] = L[j], L[i]
                self._after_structure(cur.id)

    def _reordered(self, *_):
        if self._updating:
            return
        order = [self.list.item(r).data(Qt.UserRole) for r in range(self.list.count())]
        by_id = {x.id: x for x in self.fx.layers}
        self.fx.layers = [by_id[i] for i in reversed(order) if i in by_id]
        self.changed.emit()

    def _item_changed(self, item):
        if self._updating:
            return
        lay = self.fx.layer(item.data(Qt.UserRole))
        if lay:
            lay.enabled = item.checkState() == Qt.Checked
            self.changed.emit()

    def _after_structure(self, select_id=None):
        self._updating = True
        self._fill_layers(select_id)
        self._updating = False
        self._build_form()
        self.structureChanged.emit()
        self.changed.emit()

    def _select(self, row):
        if not self._updating:
            self._build_form()

    # ------------------------------------------------------------------ the form of the selected layer
    def _clear_form(self):
        while self.form_host.count():
            it = self.form_host.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif it.layout() is not None:
                self._drop(it.layout())

    def _drop(self, layout):
        while layout.count():
            it = layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif it.layout() is not None:
                self._drop(it.layout())

    def _build_form(self):
        self._clear_form()
        lay = self.current_layer()
        self.grp_layer.setTitle("Selected layer" if lay is None else f"Settings: {lay.title()}")
        if lay is None:
            self.form_host.addWidget(QLabel("Add a layer to begin, or apply a look."))
            return
        t = LAYER_TYPES[lay.type]
        blurb = QLabel(t.blurb)
        blurb.setWordWrap(True)
        blurb.setStyleSheet("color:#9a9aa0")
        self.form_host.addWidget(blurb)
        top = QFormLayout()
        name = QLineEdit(lay.name)
        name.setPlaceholderText(t.label)
        name.editingFinished.connect(lambda: self._set_name(lay, name.text()))
        top.addRow("Name", name)
        if t.blendable:
            bl = QComboBox()
            bl.addItems(BLENDS)
            bl.setCurrentText(lay.blend)
            bl.setToolTip("normal = paint over; add / screen = light; multiply = darken")
            bl.activated.connect(lambda: self._set_blend(lay, bl.currentText()))
            top.addRow("Blend", bl)
        self.form_host.addLayout(top)
        group = None
        form = None
        for p in schema(lay.type):
            if p.group != group or form is None:
                group = p.group
                if p.group:
                    lab = QLabel(f"<b>{p.group}</b>")
                    self.form_host.addWidget(lab)
                form = QFormLayout()
                self.form_host.addLayout(form)
            w = self._make_widget(lay, p)
            if w is not None:
                form.addRow(p.label, w)

    def _set_name(self, lay, text):
        if text != lay.name:
            lay.name = text
            self._updating = True
            self._fill_layers(lay.id)
            self._updating = False
            self.grp_layer.setTitle(f"Settings: {lay.title()}")
            self.changed.emit()

    def _set_blend(self, lay, blend):
        lay.blend = blend
        self.changed.emit()

    def _set(self, lay, name, value):
        if self._updating:
            return
        lay.params[name] = value
        self.changed.emit()

    def _make_widget(self, lay, p):
        v = lay.get(p.name)
        if p.kind == "float":
            s = _spin(p.lo, p.hi, p.step, _decimals(p.step))
            s.setValue(float(v))
            s.valueChanged.connect(lambda x, n=p.name: self._set(lay, n, float(x)))
            if p.hint:
                s.setToolTip(p.hint)
            if not p.bind:
                return s
            box = QWidget()
            vl = QVBoxLayout(box)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(2)
            row = QHBoxLayout()
            row.addWidget(s, 1)
            btn = QToolButton()
            btn.setText("Link ▾")
            btn.setToolTip("Make this setting follow the music or an automation lane")
            btn.setPopupMode(QToolButton.InstantPopup)
            btn.setMenu(self._link_menu(lay, p))
            row.addWidget(btn)
            vl.addLayout(row)
            for i, b in enumerate(lay.bindings.get(p.name, [])):
                vl.addWidget(self._binding_row(lay, p, b))
            return box
        if p.kind == "int":
            s = QSpinBox()
            s.setRange(int(p.lo), int(p.hi))
            s.setValue(int(v))
            s.setKeyboardTracking(False)
            s.valueChanged.connect(lambda x, n=p.name: self._set(lay, n, int(x)))
            return s
        if p.kind == "bool":
            c = QCheckBox()
            c.setChecked(bool(v))
            c.toggled.connect(lambda x, n=p.name: self._set(lay, n, bool(x)))
            return c
        if p.kind == "color":
            b = QPushButton()
            self._paint_button(b, str(v))
            b.clicked.connect(lambda _=False, n=p.name, btn=b: self._pick_color(lay, n, btn))
            return b
        if p.kind == "choice":
            c = QComboBox()
            c.addItems(p.choices)
            c.setCurrentText(str(v))
            if p.hint:
                c.setToolTip(p.hint)
            c.activated.connect(lambda _=0, n=p.name, cb=c: self._set(lay, n, cb.currentText()))
            return c
        if p.kind == "text":
            if p.name == "text":
                e = QPlainTextEdit(str(v))
                e.setMaximumHeight(60)
                e.textChanged.connect(lambda n=p.name, ed=e: self._set(lay, n, ed.toPlainText()))
                return e
            e = QLineEdit(str(v))
            e.editingFinished.connect(lambda n=p.name, ed=e: self._set(lay, n, ed.text()))
            return e
        if p.kind == "file":
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            e = QLineEdit(str(v))
            e.setPlaceholderText("choose a picture or a video…")
            e.editingFinished.connect(lambda n=p.name, ed=e: self._set(lay, n, ed.text()))
            b = QPushButton("…")
            b.setMaximumWidth(30)
            b.clicked.connect(lambda _=False, n=p.name, ed=e: self._pick_file(lay, n, ed))
            hl.addWidget(e, 1)
            hl.addWidget(b)
            return row
        if p.kind == "font":
            c = QFontComboBox()
            c.setCurrentFont(QFont(str(v)))
            c.currentFontChanged.connect(lambda f, n=p.name: self._set(lay, n, f.family()))
            return c
        if p.kind == "time":
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            s = _spin(p.lo, p.hi, p.step, 2, " s")
            s.setValue(float(v))
            s.valueChanged.connect(lambda x, n=p.name: self._set(lay, n, float(x)))
            if p.hint:
                s.setToolTip(p.hint)
            b = QPushButton("⌖")
            b.setMaximumWidth(30)
            b.setToolTip("Set to the playhead")
            b.clicked.connect(lambda _=False, sp=s: sp.setValue(round(self.win.t, 2)))
            hl.addWidget(s, 1)
            hl.addWidget(b)
            return row
        return None

    @staticmethod
    def _paint_button(b, c):
        b.setText(c)
        b.setStyleSheet(f"background:{c}; color:{'#000' if QColor(c).lightness() > 128 else '#fff'};")

    def _pick_color(self, lay, name, btn):
        c = QColorDialog.getColor(QColor(str(lay.get(name))), self, lay.spec(name).label)
        if c.isValid():
            self._paint_button(btn, c.name())
            self._set(lay, name, c.name())

    def _pick_file(self, lay, name, edit):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a picture or video", "",
                                              "Pictures and videos (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.mp4 *.mov *.mkv *.webm *.avi *.gif);;All files (*)")
        if path:
            edit.setText(path)
            self._set(lay, name, path)

    # ------------------------------------------------------------------ bindings
    def _sources(self):
        out = list(SIGNALS.items())
        out += [(LANE + n, f"Lane: {n}") for n in self.fx.lanes]
        return out

    def _link_menu(self, lay, p):
        menu = QMenu(self)
        for key, label in self._sources():
            a = menu.addAction(label)
            a.triggered.connect(lambda _=False, k=key: self._add_binding(lay, p, k))
        return menu

    def _add_binding(self, lay, p, src):
        span = (p.hi - p.lo)
        lay.bindings.setdefault(p.name, []).append({"src": src, "amount": round(span * 0.25, 4), "smooth": 0.0, "curve": "linear"})
        self._build_form()
        self.changed.emit()

    def _binding_row(self, lay, p, b):
        w = QWidget()
        w.setStyleSheet("QWidget{font-size:11px}")
        hl = QHBoxLayout(w)
        hl.setContentsMargins(8, 0, 0, 0)
        src = QComboBox()
        for key, label in self._sources():
            src.addItem(label, key)
        if src.findData(b["src"]) < 0:
            src.addItem(b["src"], b["src"])
        src.setCurrentIndex(src.findData(b["src"]))
        src.setToolTip("The signal this setting follows")
        amount = _spin(-1e6, 1e6, max((p.hi - p.lo) / 50, 0.001), _decimals(max((p.hi - p.lo) / 50, 0.001)))
        amount.setValue(b.get("amount", 1.0))
        amount.setToolTip("How much the signal adds (the signal runs 0 to 1)")
        smooth = _spin(0, 10, 0.05, 2, " s")
        smooth.setValue(b.get("smooth", 0.0))
        smooth.setToolTip("Smooth the signal over this time")
        curve = QComboBox()
        curve.addItems(CURVES)
        curve.setCurrentText(b.get("curve", "linear"))
        rm = QPushButton("✕")
        rm.setMaximumWidth(24)

        def edit(*_):
            b["src"], b["amount"], b["smooth"], b["curve"] = src.currentData(), amount.value(), smooth.value(), curve.currentText()
            self.changed.emit()
        src.activated.connect(edit)
        curve.activated.connect(edit)
        amount.valueChanged.connect(edit)
        smooth.valueChanged.connect(edit)

        def remove():
            lay.bindings[p.name].remove(b)
            self._build_form()
            self.changed.emit()
        rm.clicked.connect(remove)
        hl.addWidget(QLabel("←"))
        hl.addWidget(src, 3)
        hl.addWidget(QLabel("×"))
        hl.addWidget(amount, 2)
        hl.addWidget(smooth, 2)
        hl.addWidget(curve, 2)
        hl.addWidget(rm)
        return w

    # ------------------------------------------------------------------ signals / events
    def _signals_edited(self, *_):
        if self._updating:
            return
        fx = self.fx
        fx.use_dynamics, fx.use_accents = self.chk_dyn.isChecked(), self.chk_acc.isChecked()
        fx.loud_floor_db, fx.loud_range_db, fx.density_max = self.sp_floor.value(), self.sp_range.value(), self.sp_dens.value()
        self.changed.emit()

    def _fill_events(self):
        fx = self.fx
        self.events.clear()
        for im in fx.impulses:
            self.events.addItem(f"{_tm(im['t'])}   strength {im['s']:.2f}")
        for h in fx.hits:
            lo, hi = sorted((h["m0"], h["m1"]))
            self.events.addItem(f"every note in measures {lo + 1}–{hi + 1}   strength {h['s']:.2f}")

    def _add_event(self):
        self.fx.impulses.append({"t": round(self.win.t, 3), "s": self.sp_strength.value()})
        self.fx.impulses.sort(key=lambda i: i["t"])
        self._fill_events()
        self.changed.emit()

    def _add_hits(self):
        ms = self.win._sel_measures
        if not ms:
            self.win.status.showMessage("Select some measures first.", 4000)
            return
        self.fx.hits.append({"m0": min(ms), "m1": max(ms), "s": self.sp_strength.value()})
        self._fill_events()
        self.changed.emit()

    def _remove_event(self):
        i, n = self.events.currentRow(), len(self.fx.impulses)
        if i >= 0:
            (self.fx.impulses if i < n else self.fx.hits).pop(i if i < n else i - n)
            self._fill_events()
            self.changed.emit()

    # ------------------------------------------------------------------ lanes
    def _fill_lanes(self, select=None):
        self.lanes.blockSignals(True)
        self.lanes.clear()
        for name in self.fx.lanes:
            self.lanes.addItem(name)
            if name == select:
                self.lanes.setCurrentRow(self.lanes.count() - 1)
        self.lanes.blockSignals(False)
        self._lane_selected(self.lanes.currentRow())

    def _lane_selected(self, row):
        name = self.lanes.item(row).text() if row >= 0 else None
        self._updating, was = True, self._updating
        self.sp_lane_default.setEnabled(name is not None)
        if name is not None:
            self.sp_lane_default.setValue(self.fx.lanes.get(name, 0.0))
        self._updating = was

    def _lane_default(self, v):
        if self._updating:
            return
        r = self.lanes.currentRow()
        if r >= 0:
            self.fx.lanes[self.lanes.item(r).text()] = v
            self.changed.emit()

    def _add_lane(self):
        name, ok = QInputDialog.getText(self, "New automation lane", "Name of the lane (for example Storm, Calm, Glow):")
        if ok and name.strip():
            n = self.win.project.add_lane(name)
            self._fill_lanes(n)
            self._build_form()
            self.structureChanged.emit()
            self.changed.emit()

    def _rename_lane(self):
        r = self.lanes.currentRow()
        if r < 0:
            return
        old = self.lanes.item(r).text()
        name, ok = QInputDialog.getText(self, "Rename lane", "New name:", text=old)
        if ok and name.strip():
            n = self.win.project.rename_lane(old, name)
            self._fill_lanes(n)
            self._build_form()
            self.structureChanged.emit()
            self.changed.emit()

    def _remove_lane(self):
        r = self.lanes.currentRow()
        if r >= 0:
            self.win.project.remove_lane(self.lanes.item(r).text())
            self._fill_lanes()
            self._build_form()
            self.structureChanged.emit()
            self.changed.emit()

    def show_key(self, key):
        """`key`: the one selected keyframe of an automation lane, or None."""
        self._key = key
        was, self._updating = self._updating, True
        self.sp_key.setEnabled(key is not None)
        if key is not None:
            self.sp_key.setValue(key.v[0])
        self._updating = was

    def _key_value(self, v):
        if self._updating or self._key is None:
            return
        self._key.v = [v]
        self.win._keys_changed()
        self.win.commit()
