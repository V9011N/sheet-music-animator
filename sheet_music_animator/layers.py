"""The effect layers: what kinds there are, which settings each has, their ranges and defaults.

A *look* is a stack of layers drawn bottom to top (the notation itself is one of them).  Every number
setting can be *bound* to a signal of the music (loudness, note density, events, ...) or to an automation lane.
This module is pure data: the editor builds its panels from it, `effects.py` draws from it, and looks
(saved stacks) are made of it.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

BLENDS = ("normal", "add", "screen", "multiply")

# signals a setting can follow (lanes are added as "lane:<name>")
SIGNALS = {
    "loudness": "Loudness of the recording",
    "density": "How many notes are starting",
    "activity": "Loudness + density (a good wind)",
    "events": "Events: short kick (shake)",
    "events_slow": "Events: long kick (zoom punch)",
    "impacts": "Big events only (flash)",
    "onsets": "Every note that starts",
    "pitch": "Pitch height of the notes now",
    "progress": "Position in the piece (0 to 1)",
}
CURVES = ("linear", "squared", "square root", "inverted")


@dataclass
class P:
    """One setting of a layer type."""
    name: str
    label: str
    kind: str                       # float int bool color choice text file font time
    default: object = 0.0
    lo: float = 0.0
    hi: float = 1.0
    step: float = 0.05
    choices: tuple = ()
    bind: bool = False              # can follow a signal
    group: str = ""
    hint: str = ""


def F(name, label, default, lo, hi, step=0.05, bind=False, group="", hint=""):
    return P(name, label, "float", default, lo, hi, step, (), bind, group, hint)


def I(name, label, default, lo, hi, group="", hint=""):          # noqa: E743
    return P(name, label, "int", default, lo, hi, 1, (), False, group, hint)


def B(name, label, default, group="", hint=""):
    return P(name, label, "bool", default, group=group, hint=hint)


def C(name, label, default, group="", hint=""):
    return P(name, label, "color", default, group=group, hint=hint)


def CH(name, label, default, choices, group="", hint=""):
    return P(name, label, "choice", default, choices=tuple(choices), group=group, hint=hint)


def T(name, label, default, lo=-3600.0, hi=3600.0, group="", hint=""):
    return P(name, label, "time", default, lo, hi, 0.5, group=group, hint=hint)


COMMON = [
    F("opacity", "Opacity", 1.0, 0.0, 1.0, bind=True),
    T("start", "Appears at", 0.0, hint="Seconds from the start; negative = seconds before the end", group="Timing"),
    T("end", "Disappears at", 0.0, hint="0 = stays; negative = seconds before the end", group="Timing"),
    F("fade_in", "Fades in over", 0.0, 0.0, 60.0, 0.1, group="Timing"),
    F("fade_out", "Fades out over", 0.0, 0.0, 60.0, 0.1, group="Timing"),
]


@dataclass
class LayerType:
    key: str
    label: str
    category: str
    blurb: str
    params: list = field(default_factory=list)
    blendable: bool = True            # has a blend mode
    default_blend: str = "normal"
    common: bool = True               # has the common settings (opacity, timing)
    default_name: str = ""

    def all_params(self):
        return (COMMON if self.common else []) + self.params


def _t(key, label, category, blurb, params, **kw):
    return LayerType(key, label, category, blurb, params, **kw)


LAYER_TYPES: dict[str, LayerType] = {t.key: t for t in [
    # ------------------------------------------------------------------------------------ backdrops
    _t("solid", "Solid colour", "Backdrop", "A flat colour. With 'add' or 'screen' it is a flash of light; bind its "
       "opacity to events. Black with a late start is a fade to black.", [
        C("color", "Colour", "#000000"), C("color_b", "Second colour", "#ffffff"),
        F("mix", "Mix towards the second colour", 0.0, 0.0, 1.0, bind=True)]),
    _t("gradient", "Gradient", "Backdrop", "A sky: two colours blending top to bottom (or any angle, or radial). "
       "A second pair of colours can be mixed in, e.g. calm to storm.", [
        C("top", "Top", "#0e1424"), C("bottom", "Bottom", "#1a2238"),
        C("top_b", "Top (second palette)", "#060a14"), C("bottom_b", "Bottom (second palette)", "#10192c"),
        F("mix", "Mix towards the second palette", 0.0, 0.0, 1.0, bind=True),
        CH("style", "Style", "linear", ("linear", "radial"), group="Shape"),
        F("angle", "Angle", 0.0, -180.0, 180.0, 5.0, group="Shape", hint="0 = top to bottom, 90 = left to right"),
        F("center_x", "Centre x", 0.5, -0.5, 1.5, group="Shape"), F("center_y", "Centre y", 0.5, -0.5, 1.5, group="Shape"),
        F("radius", "Radius (radial)", 0.8, 0.05, 3.0, group="Shape")]),
    _t("noise", "Moving texture", "Backdrop", "Flowing procedural texture: mist, clouds, fire, water caustics, aurora. "
       "Three colours are blended by the texture (use black as the dark colour with 'add' or 'screen').", [
        CH("style", "Style", "soft", ("soft", "billow", "ridged", "flame"),
           hint="soft = mist, billow = clouds, ridged = water caustics / lightning, flame = rising fire"),
        C("color_dark", "Dark colour", "#000000", group="Colours"), C("color_mid", "Middle colour", "#1d2a46", group="Colours"),
        C("color_bright", "Bright colour", "#8aa4d6", group="Colours"),
        F("scale", "Pattern scale", 0.6, 0.05, 8.0, 0.05, group="Shape", hint="Bigger = finer detail"),
        I("detail", "Detail levels", 3, 1, 6, group="Shape"),
        F("stretch", "Vertical stretch", 1.0, 0.2, 6.0, group="Shape"),
        F("warp", "Swirl", 0.0, 0.0, 1.0, group="Shape"),
        F("contrast", "Contrast", 1.0, 0.2, 5.0, group="Shape"), F("brightness", "Brightness", 0.0, -1.0, 1.0, group="Shape"),
        F("speed", "Drift speed", 0.03, -2.0, 2.0, 0.01, bind=True, group="Motion", hint="Screens per second"),
        F("direction", "Drift direction", 0.0, -180.0, 180.0, 5.0, group="Motion", hint="0 = right, 90 = down, -90 = up"),
        F("evolve", "Changes shape over time", 0.15, 0.0, 3.0, group="Motion"),
        I("seed", "Random seed", 1, 0, 999, group="Motion")], default_blend="add"),
    _t("media", "Picture or video", "Backdrop", "Your own photo or video as the backdrop, with slow zoom and drift "
       "(a sunset field, an ocean clip, a portrait...).", [
        P("file", "File", "file", ""),
        CH("fit", "Fit", "cover", ("cover", "contain", "stretch")),
        F("zoom", "Zoom", 1.0, 0.2, 5.0), F("end_zoom", "Zoom at the end", 1.0, 0.2, 5.0, hint="Slowly zooms between the two"),
        F("drift_x", "Drift sideways over the piece", 0.0, -1.0, 1.0), F("drift_y", "Drift up/down over the piece", 0.0, -1.0, 1.0),
        F("speed", "Video speed", 1.0, 0.1, 4.0, 0.1), B("loop", "Loop the video", True),
        F("brightness", "Brightness", 0.0, -1.0, 1.0), F("saturation", "Saturation", 1.0, 0.0, 2.0),
        F("blur", "Blur", 0.0, 0.0, 60.0, 1.0)]),
    # ------------------------------------------------------------------------------------ atmosphere
    _t("particles", "Particles", "Atmosphere", "Drifting particles (snow, embers, fireflies, rain, bubbles, dust) or "
       "bursts that fly off every note as it sounds (sparks, fireworks).", [
        CH("emit", "Emitted", "ambient", ("ambient", "on notes"), hint="'on notes' bursts from every note that sounds"),
        I("count", "Count", 300, 1, 4000, hint="Per note for 'on notes'"),
        F("size", "Size", 2.0, 0.3, 60.0, 0.1), F("size_var", "Size variation", 0.4, 0.0, 1.0),
        CH("shape", "Shape", "dot", ("dot", "streak", "spark", "ring"), group="Look"),
        F("streak", "Streak length", 0.03, 0.0, 0.6, 0.01, group="Look", hint="Seconds of travel drawn as a streak"),
        C("color_a", "Colour", "#c8d7eb", group="Look"), C("color_b", "Second colour", "#ffffff", group="Look"),
        CH("color_by", "Colour by", "random mix", ("random mix", "staff"), group="Look", hint="'staff' (on notes) uses the two colours for the two hands"),
        F("alpha", "Brightness", 0.4, 0.0, 1.0, group="Look"), F("softness", "Softness", 0.0, 0.0, 8.0, 0.25, group="Look"),
        F("speed", "Speed", 60.0, -3000.0, 3000.0, 5.0, bind=True, group="Motion", hint="Pixels per second (of a 1080p frame)"),
        F("angle", "Direction", 100.0, -180.0, 360.0, 5.0, group="Motion", hint="0 = right, 90 = down, -90 = up"),
        F("spread", "Spread", 20.0, 0.0, 360.0, 5.0, group="Motion"),
        F("gravity", "Falls (extra, px/s)", 0.0, -600.0, 600.0, 5.0, group="Motion"),
        F("wobble", "Wobble (px)", 0.0, 0.0, 80.0, 1.0, group="Motion"),
        F("life", "Lifetime (on notes)", 1.2, 0.1, 10.0, 0.1, group="Motion"),
        I("depth", "Depth layers", 3, 1, 4, group="Motion", hint="Parallax: far ones are slower and finer"),
        I("seed", "Random seed", 11, 0, 999, group="Motion")], default_blend="add"),
    # ------------------------------------------------------------------------------------ notation
    _t("score", "Notation (the sheet music)", "Notation", "The music itself. Put layers below it for the backdrop and "
       "above it for glow and overlays.", [
        C("color", "Ink colour", "#eeeeee"), C("color_b", "Second ink colour", "#ffffff"),
        F("mix", "Mix towards the second colour", 0.0, 0.0, 1.0, bind=True),
        F("glow", "Glow", 0.0, 0.0, 4.0, 0.1, group="Glow"), F("glow_radius", "Glow radius", 12.0, 1.0, 80.0, 1.0, group="Glow"),
        F("edge_fade", "Fade out at the sides", 0.0, 0.0, 0.45, 0.01, hint="Fraction of the frame width")]),
    _t("highlight", "Note highlight", "Notation", "Lights up every note as it sounds: a glow in the ink, a ripple "
       "ring, a star flare or a column of light. Colour by hand, by pitch, or one colour.", [
        CH("shape", "Shape", "glow", ("glow", "ring", "flare", "column")),
        CH("color_mode", "Colour by", "staff", ("staff", "single", "pitch", "register"),
           hint="staff = the two hands, pitch = a colour per note name, register = low to high"),
        C("color_a", "Colour (right hand / first)", "#78deff"), C("color_b", "Colour (left hand / second)", "#ffc070"),
        F("duration", "Lasts", 0.45, 0.05, 6.0, 0.05), F("size", "Size", 1.0, 0.2, 8.0, 0.1),
        F("intensity", "Brightness", 1.0, 0.0, 4.0, bind=True),
        F("bloom", "Bloom", 1.6, 0.0, 6.0, 0.1), B("tint_ink", "Tint the ink under the note", True),
        CH("staves", "Which staff", "all", ("all", "upper", "lower"))], default_blend="add"),
    _t("spotlight", "Spotlight on measures", "Notation", "From a moment on, only the chosen measures keep their ink "
       "(a finale, a cadenza). Applies to the notation and highlight layers.", [
        I("from_measure", "From measure", 1, 1, 9999), I("to_measure", "To measure", 1, 1, 9999),
        T("at", "Starts at", 0.0, hint="Seconds"), F("ramp", "Takes", 0.55, 0.05, 20.0, 0.05),
        T("until", "Ends at (0 = stays)", 0.0), F("outside", "Ink outside stays at", 0.0, 0.0, 1.0)],
       blendable=False, common=False),
    # ------------------------------------------------------------------------------------ post
    _t("bloom", "Bloom (glow of bright parts)", "Finish", "Bright areas bleed light into their surroundings.", [
        F("threshold", "Threshold", 0.55, 0.0, 1.0), F("radius", "Radius", 14.0, 1.0, 100.0, 1.0),
        F("strength", "Strength", 0.6, 0.0, 4.0, 0.05, bind=True)], blendable=False),
    _t("vignette", "Dark corners", "Finish", "Darkens (or tints) the edges of the frame.", [
        F("strength", "Strength", 0.6, 0.0, 1.0, bind=True), F("size", "Clear centre", 0.3, 0.0, 1.0),
        F("softness", "Softness", 0.95, 0.05, 2.0), C("color", "Colour", "#02040a")], blendable=False),
    _t("grade", "Colour grade", "Finish", "Brightness, contrast, saturation, hue and tint of everything below.", [
        F("brightness", "Brightness", 0.0, -1.0, 1.0, bind=True), F("contrast", "Contrast", 1.0, 0.2, 3.0, bind=True),
        F("saturation", "Saturation", 1.0, 0.0, 3.0, bind=True), F("hue", "Hue shift", 0.0, -180.0, 180.0, 5.0, bind=True),
        C("tint", "Tint colour", "#ff9a3c"), F("tint_amount", "Tint amount", 0.0, 0.0, 1.0, bind=True)], blendable=False),
    _t("blur", "Blur", "Finish", "Softens everything below.", [
        F("radius", "Radius", 4.0, 0.0, 60.0, 0.5, bind=True)], blendable=False),
    _t("aberration", "Colour fringing", "Finish", "Splits red and blue sideways, like a cheap lens.", [
        F("amount", "Amount (px)", 3.0, 0.0, 60.0, 0.5, bind=True)], blendable=False),
    _t("grain", "Film grain", "Finish", "Fine moving noise over the picture.", [
        F("amount", "Amount", 8.0, 0.0, 80.0, 0.5, bind=True), I("size", "Grain size", 1, 1, 8)], blendable=False),
    # ------------------------------------------------------------------------------------ text
    _t("text", "Text", "Text", "A title, a subtitle, a credit, a caption: any text, any time.", [
        P("text", "Text", "text", "Title"), P("font", "Font", "font", "Times New Roman"),
        F("size", "Size", 0.06, 0.01, 0.5, 0.005, hint="Fraction of the frame height"), C("color", "Colour", "#dee8f6"),
        F("x", "Position x", 0.5, -0.5, 1.5), F("y", "Position y", 0.14, -0.5, 1.5),
        CH("align", "Align", "center", ("center", "left", "right")), F("spacing", "Letter spacing", 0.0, 0.0, 1.0, 0.01),
        B("italic", "Italic", False), B("bold", "Bold", False), B("uppercase", "Capitals", False),
        F("glow", "Glow", 0.0, 0.0, 4.0, 0.1)], default_blend="normal"),
    # ------------------------------------------------------------------------------------ camera
    _t("shake", "Camera shake", "Camera", "Shakes the camera. Its amount is 0 by default and follows 'events'.", [
        F("amount", "Amount (px)", 0.0, 0.0, 120.0, 1.0, bind=True), F("speed", "Speed", 1.0, 0.2, 4.0, 0.1)],
       blendable=False, common=False),
    _t("zoom", "Camera zoom", "Camera", "Zooms the camera in (positive) or out (negative) on top of its keyframes. "
       "Follow loudness for a breathing zoom, events for a punch, a lane for a planned zoom.", [
        F("zoom", "Zoom", 0.0, -0.8, 3.0, 0.01, bind=True)], blendable=False, common=False),
    _t("sway", "Camera sway", "Camera", "Slowly rocks the camera.", [
        F("degrees", "Angle", 1.5, 0.0, 45.0, 0.1, bind=True), F("period", "Period (s)", 8.0, 1.0, 120.0, 0.5)],
       blendable=False, common=False),
]}

CATEGORIES = ["Backdrop", "Atmosphere", "Notation", "Finish", "Text", "Camera"]
_ids = itertools.count(1)


def schema(type_key: str) -> list:
    return LAYER_TYPES[type_key].all_params()


@dataclass(eq=False)
class Layer:
    id: str
    type: str
    name: str = ""
    enabled: bool = True
    blend: str = "normal"
    params: dict = field(default_factory=dict)
    bindings: dict = field(default_factory=dict)     # setting -> [{"src", "amount", "smooth", "curve"}]

    def get(self, name):
        if name in self.params:
            return self.params[name]
        for p in schema(self.type):
            if p.name == name:
                return p.default
        raise KeyError(name)

    def spec(self, name):
        return next(p for p in schema(self.type) if p.name == name)

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "name": self.name, "enabled": self.enabled, "blend": self.blend,
                "params": dict(self.params), "bindings": {k: [dict(b) for b in v] for k, v in self.bindings.items() if v}}

    @classmethod
    def from_dict(cls, d: dict) -> "Layer":
        known = {p.name for p in schema(d["type"])}
        return cls(id=d.get("id") or new_id(), type=d["type"], name=d.get("name", ""), enabled=d.get("enabled", True),
                   blend=d.get("blend", "normal"), params={k: v for k, v in d.get("params", {}).items() if k in known},
                   bindings={k: [dict(b) for b in v] for k, v in d.get("bindings", {}).items() if k in known})

    def title(self) -> str:
        return self.name or LAYER_TYPES[self.type].label


def new_id() -> str:
    return "L%s" % (next(_ids) + int.from_bytes(__import__("os").urandom(2), "big"))


def new_layer(type_key: str, name: str = "", **params) -> Layer:
    t = LAYER_TYPES[type_key]
    return Layer(new_id(), type_key, name or "", True, t.default_blend, dict(params), {})


def bind(layer: Layer, setting: str, src: str, amount: float = 1.0, smooth: float = 0.0, curve: str = "linear") -> Layer:
    """Make `setting` follow a signal: value = setting + amount * curve(signal)."""
    layer.bindings.setdefault(setting, []).append({"src": src, "amount": amount, "smooth": smooth, "curve": curve})
    return layer


def legacy_layers(d: dict) -> list:
    """The layer stack equivalent to the settings of the first, fixed-feature version of the effects."""
    g = d.get
    L = []

    def add(type_key, name, blend=None, **params):
        lay = new_layer(type_key, name, **params)
        if blend:
            lay.blend = blend
        L.append(lay)
        return lay
    bg_c, bg_s = g("bg_calm", ["#0e1424", "#1a2238"]), g("bg_storm", ["#060a14", "#10192c"])
    bind(add("gradient", "Sky", top=bg_c[0], bottom=bg_c[1], top_b=bg_s[0], bottom_b=bg_s[1]), "mix", "lane:Mood")
    if g("mist", 1.0) > 0:
        bind(add("noise", "Mist", style="soft", color_mid="#3a4d70", color_bright="#a8c0e8", scale=0.5,
                 opacity=0.5 * g("mist", 1.0), speed=0.02), "speed", "activity", 0.08)
    if g("snow", 1.0) > 0:
        bind(add("particles", "Snow", shape="streak", count=int(480 * g("snow", 1.0)), size=1.5, alpha=0.4, speed=36.0,
                 angle=180.0, spread=0.0, gravity=80.0, streak=0.025, depth=3, softness=0.5), "speed", "activity", 1750.0)
    bind(add("score", "Notation", blend=None, color=g("ink_calm", "#bec8de"), color_b=g("ink_storm", "#ecf3fc"),
             edge_fade=g("edge_fade", 0.125)), "mix", "lane:Mood")
    if g("flash", True):
        fc = g("flash_colors", ["#78deff", "#ffc070"])
        add("highlight", "Note highlight", color_a=fc[0], color_b=fc[1], duration=g("flash_time", 0.45), bloom=g("glow", 1.6))
    bind(add("solid", "Flash of light", blend="add", color="#becfff", opacity=0.0), "opacity", "impacts", 0.45)
    if g("vignette", 1.0) > 0:
        bind(bind(add("vignette", "Dark corners", strength=0.55 * g("vignette", 1.0)), "strength", "lane:Mood", -0.25),
             "strength", "loudness", 0.2)
    bind(add("grade", "Hush"), "brightness", "lane:Hush", -0.12)
    if g("title"):
        add("text", "Title", text=g("title"), font=g("title_font", "Times New Roman"), color=g("title_color", "#dee8f6"),
            size=0.062, y=0.145, spacing=0.26, uppercase=True, start=g("title_in", [0.6, 2.2])[0],
            fade_in=g("title_in", [0.6, 2.2])[1], end=g("title_out", [0, 2.5])[0], fade_out=g("title_out", [0, 2.5])[1])
    if g("subtitle"):
        add("text", "Subtitle", text=g("subtitle"), font=g("title_font", "Times New Roman"), color=g("title_color", "#dee8f6"),
            size=0.026, y=0.2, italic=True, start=g("title_in", [0.6, 2.2])[0], fade_in=g("title_in", [0.6, 2.2])[1],
            end=g("title_out", [0, 2.5])[0], fade_out=g("title_out", [0, 2.5])[1])
    for sp in g("spotlights", []):
        add("spotlight", "Spotlight", from_measure=int(sp["m0"]) + 1, to_measure=int(sp["m1"]) + 1, at=sp["t"],
            ramp=sp.get("ramp", 0.55), until=sp.get("end", 0.0))
    bind(add("shake", "Shake"), "amount", "events", 24.0 * g("shake", 1.0))
    z = add("zoom", "Zoom")
    bind(z, "zoom", "loudness", 0.05 * g("breathe", 1.0))
    bind(z, "zoom", "events_slow", 0.06 * g("punch", 1.0))
    if g("fade_out", 1.0) > 0:
        add("solid", "Fade to black", color="#000000", start=-g("fade_out", 1.0), fade_in=g("fade_out", 1.0) * 0.95)
    return L
