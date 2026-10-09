"""The built-in looks, as data.  A look is a layer stack plus the lanes, events and settings that go with it;
times in it are *anchors* ({"m": measure number, "dt": seconds} or {"t": seconds}) so a look fits any piece."""
from __future__ import annotations

from .layers import bind, new_layer


def A(m, dt=0.0):
    return {"m": m, "dt": dt}


def L(type_key, name="", blend=None, bindings=None, enabled=True, **params):
    """A layer as a dict.  bindings: {setting: [(source, amount), ...]}"""
    lay = new_layer(type_key, name, **params)
    if blend:
        lay.blend = blend
    lay.enabled = enabled
    for setting, items in (bindings or {}).items():
        for it in items:
            bind(lay, setting, *it)
    return lay.to_dict()


def K(at, v, ease="smooth"):
    return {"at": at, "v": v, "ease": ease}


def lane(default, *keys):
    return {"default": default, "keys": list(keys)}


def _look(name, description, layers, lanes=None, events=None, settings=None, camera=None, enabled=True):
    return {"name": name, "description": description, "enabled": enabled, "layers": layers, "lanes": lanes or {},
            "events": events or {}, "settings": settings or {}, "camera": camera or []}


# ------------------------------------------------------------------------------------------------------
PLAIN = _look(
    "Plain (white page, no effects)", "The blank slate: dark ink on white, nothing else. Fits any piece.",
    [], enabled=False,
    settings={"paper": "#ffffff", "ink": "#1a1a1a", "width": 1920, "height": 1080, "fps": 30, "crf": 16, "tail": 2.0,
              "reveal": "instant", "fade": 0.25, "ghost": 0.0, "follow_width": 14000.0, "follow_lead": 0.25})

WINTER_WIND = _look(
    "Winter Wind (storm)",
    "A calm, hushed beginning; a storm that breaks at measure 5 with wind-blown snow, light-up notes, shake on "
    "loud chords, a title, and a spotlight on the last measure of a 96-measure piece. Made for Chopin's Etude Op. 25 "
    "No. 11 but every part can be changed.",
    [L("gradient", "Sky", top="#0e1424", bottom="#1a2238", top_b="#060a14", bottom_b="#10192c",
       bindings={"mix": [("lane:Storm", 1.0)]}),
     L("noise", "Mist", blend="add", style="soft", color_dark="#000000", color_mid="#0d1626", color_bright="#2c4068",
       scale=0.5, speed=0.01, evolve=0.1, opacity=0.4,
       bindings={"speed": [("activity", 0.12)], "opacity": [("lane:Storm", 0.35)]}),
     L("particles", "Snow", shape="streak", count=460, size=1.6, size_var=0.3, alpha=0.4, speed=30.0, angle=178.0,
       spread=0.0, gravity=60.0, streak=0.022, depth=3, softness=0.5, color_a="#c8d7eb", color_b="#ebf0fa",
       bindings={"speed": [("activity", 1700.0, 0.0, "squared"), ("lane:Storm", 200.0)], "opacity": [("lane:Storm", 0.55)]}, opacity=0.4),
     L("score", "Notation", color="#bec8de", color_b="#ecf3fc", edge_fade=0.125, bindings={"mix": [("lane:Storm", 1.0)]}),
     L("highlight", "Note highlight", color_a="#78deff", color_b="#ffc070", duration=0.45, bloom=1.6),
     L("solid", "Flash of light", blend="add", color="#bed2ff", opacity=0.0, bindings={"opacity": [("impacts", 0.4)]}),
     L("vignette", "Dark corners", strength=0.55, bindings={"strength": [("lane:Storm", -0.2), ("loudness", 0.15)]}),
     L("grade", "Hush", bindings={"brightness": [("lane:Hush", -0.12)]}),
     L("text", "Title", text="Winter Wind", size=0.062, y=0.145, spacing=0.26, uppercase=True, color="#dee8f6",
       start=0.6, fade_in=2.2, end=A(3, -1.5), fade_out=2.5),
     L("text", "Subtitle", text="Frédéric Chopin  ·  Étude in A minor, Op. 25 No. 11", size=0.026, y=0.2,
       italic=True, color="#dee8f6", start=0.6, fade_in=2.2, end=A(3, -1.5), fade_out=2.5),
     L("spotlight", "Last measure", from_measure=96, to_measure=96, at=A(96), ramp=0.55),
     L("shake", "Shake", bindings={"amount": [("events", 24.0)]}),
     L("zoom", "Breathing zoom", bindings={"zoom": [("lane:Zoom", 1.0), ("loudness", 0.04), ("events_slow", 0.05)]}),
     L("solid", "Fade to black", color="#000000", start=-1.0, fade_in=0.95)],
    lanes={
        "Storm": lane(0.0, K(A(5, -0.05), 0.0), K(A(5, 0.3), 1.0)),
        "Hush": lane(0.0, K(A(4, 3.0), 0.0), K(A(4, 5.0), 1.0), K(A(5, -0.05), 1.0), K(A(5, 0.3), 0.0)),
        "Snow lift": lane(0.0, K(A(95), 0.0), K(A(95, 0.6), 1.0), K(A(96, 0.39), 1.0, "linear"), K(A(96, 0.4), 0.0)),
        "Zoom": lane(0.0, K({"t": 0.0}, 0.28), K(A(4, 3.0), 0.28), K(A(4, 5.0), 0.46), K(A(5, -0.05), 0.46),
                     K(A(5, 0.3), 0.0), K(A(89), 0.0), K(A(89, 1.5), 0.07), K(A(95), 0.07), K(A(95, 0.5), -0.18),
                     K(A(96, -0.12), -0.18), K(A(96, 0.43), 0.0))},
    events={"use_dynamics": True, "use_accents": True,
            "impulses": [{"at": A(5), "s": 1.0}, {"at": A(95), "s": 0.9}, {"at": A(96), "s": 1.0}],
            "hits": [{"m0": 93, "m1": 94, "s": 0.85}]},
    settings={"fps": 60, "width": 1920, "height": 1080, "crf": 16, "reveal": "instant", "tail": 1.5,
              "follow_lead": 0.42, "follow_width": {"system_fraction": 0.8}},
    camera=[{"focus": {"m": 96, "dt": -0.12, "dur": 0.55}}])

EMBER = _look(
    "Ember (fire)", "A furnace: a dark red sky, rising flames, embers floating up, white-hot ink, orange flares on "
    "every note. For stormy, virtuosic pieces.",
    [L("gradient", "Sky", top="#120202", bottom="#4a0d04"),
     L("noise", "Flames", blend="add", style="flame", scale=1.5, stretch=3.2, detail=4, speed=0.22, direction=-90.0,
       evolve=0.9, warp=0.12, contrast=0.9, color_dark="#000000", color_mid="#8a1c04", color_bright="#ffa63a", opacity=0.75,
       bindings={"opacity": [("activity", 0.4)]}),
     L("particles", "Embers", shape="dot", count=240, size=4.5, size_var=0.7, alpha=1.0, speed=-120.0, angle=-90.0, spread=50.0,
       wobble=22.0, softness=1.6, depth=3, color_a="#ff7a1a", color_b="#ffd070", bindings={"speed": [("activity", -200.0)]}),
     L("score", "Notation", color="#fff1de", color_b="#ffffff", glow=0.5, glow_radius=10.0),
     L("highlight", "Flares", shape="flare", color_mode="single", color_a="#ff9a3c", duration=0.5, size=1.2, bloom=2.0),
     L("particles", "Sparks", emit="on notes", shape="streak", count=8, size=1.6, speed=420.0, angle=-90.0, spread=140.0,
       gravity=200.0, life=0.7, streak=0.04, color_a="#ffb040", color_b="#fff0b0", alpha=0.9),
     L("bloom", "Heat bloom", threshold=0.5, radius=18.0, strength=0.6, bindings={"strength": [("loudness", 0.5)]}),
     L("grade", "Warm grade", tint="#ff7a30", tint_amount=0.12, contrast=1.1),
     L("vignette", "Dark corners", strength=0.6, color="#0a0100"),
     L("shake", "Shake", bindings={"amount": [("events", 14.0)]}),
     L("zoom", "Punch", bindings={"zoom": [("events_slow", 0.04)]})],
    events={"use_dynamics": True, "use_accents": True})

FIREFLIES = _look(
    "Fireflies (night garden)", "A deep green night: slow mist, drifting glowing fireflies, mint-white ink, ripples "
    "and sparks on every note.",
    [L("gradient", "Sky", top="#020b06", bottom="#0a2a14", style="radial", center_y=0.6, radius=1.1),
     L("noise", "Mist", blend="add", style="soft", scale=0.4, speed=0.015, color_mid="#06200f", color_bright="#2b8a4a", opacity=0.5),
     L("particles", "Fireflies", shape="dot", count=55, size=7.0, size_var=0.5, alpha=1.0, speed=18.0, angle=-80.0, spread=180.0,
       wobble=40.0, softness=3.5, depth=3, color_a="#a8ff8a", color_b="#f4ff9a"),
     L("score", "Notation", color="#e6fff0", color_b="#ffffff", glow=0.7, glow_radius=14.0),
     L("highlight", "Ripples", shape="ring", color_mode="single", color_a="#9dffb0", duration=0.8, size=1.2, bloom=1.4),
     L("particles", "Sparks", emit="on notes", shape="spark", count=6, size=1.4, speed=180.0, angle=0.0, spread=360.0,
       gravity=60.0, life=1.0, color_a="#b6ffb0", color_b="#fffbb0", alpha=0.9),
     L("bloom", "Glow", threshold=0.55, radius=16.0, strength=0.5),
     L("vignette", "Dark corners", strength=0.7, color="#000a04")],
    events={"use_dynamics": True, "use_accents": True})

OCEAN = _look(
    "Ocean (underwater)", "Deep blue water with shifting light caustics, rising bubbles and a gentle sway of the camera.",
    [L("gradient", "Water", top="#0a3a6a", bottom="#021226"),
     L("noise", "Caustics", blend="add", style="ridged", scale=1.1, detail=3, speed=0.03, direction=60.0, evolve=0.5, warp=0.5,
       contrast=0.9, color_dark="#000000", color_mid="#03233d", color_bright="#58b4e0", opacity=0.4,
       bindings={"opacity": [("loudness", 0.2)]}),
     L("particles", "Bubbles", shape="ring", count=110, size=3.0, size_var=0.7, alpha=0.5, speed=-70.0, angle=-90.0, spread=30.0,
       wobble=18.0, depth=3, color_a="#bfe8ff", color_b="#ffffff"),
     L("score", "Notation", color="#e8f6ff", color_b="#ffffff", glow=0.5, glow_radius=12.0),
     L("highlight", "Ripples", shape="ring", color_mode="single", color_a="#9ee6ff", duration=1.0, size=1.4, bloom=1.2),
     L("grade", "Teal grade", tint="#00b5c8", tint_amount=0.1),
     L("vignette", "Depth", strength=0.65, color="#00060e"),
     L("sway", "Sway", degrees=0.8, period=9.0),
     L("zoom", "Breathing", bindings={"zoom": [("loudness", 0.03)]})],
    events={"use_dynamics": True, "use_accents": True})

GOLDEN_RAIN = _look(
    "Golden rain", "A warm, dark amber hall with streaks of golden light falling like rain, gold-white ink and "
    "columns of light through the notes.",
    [L("gradient", "Hall", top="#0c0703", bottom="#2c1a07"),
     L("particles", "Rain", shape="streak", count=380, size=1.4, size_var=0.5, alpha=0.5, speed=500.0, angle=90.0, spread=0.0,
       streak=0.06, depth=3, color_a="#ffcf70", color_b="#fff0c0", bindings={"speed": [("activity", 900.0)]}),
     L("score", "Notation", color="#ffeccc", color_b="#ffffff", glow=0.6, glow_radius=10.0),
     L("highlight", "Light columns", shape="column", color_mode="single", color_a="#ffc860", duration=0.3, size=0.35, bloom=1.2),
     L("bloom", "Glow", threshold=0.5, radius=20.0, strength=0.5),
     L("grain", "Film grain", amount=5.0),
     L("vignette", "Dark corners", strength=0.6, color="#050200")],
    events={"use_dynamics": True, "use_accents": True})

NEON = _look(
    "Neon (night city)", "Black glass with neon magenta and cyan: every note gets its own colour, ripples and sparks, "
    "colour fringing on loud hits.",
    [L("gradient", "Night", top="#06020f", bottom="#150524"),
     L("noise", "Haze", blend="add", style="soft", scale=0.35, speed=0.01, color_mid="#1a0830", color_bright="#6a1fa0", opacity=0.45),
     L("score", "Notation", color="#ffffff", color_b="#ffffff", glow=1.0, glow_radius=10.0),
     L("highlight", "Pitch colours", shape="glow", color_mode="pitch", duration=0.6, bloom=2.4),
     L("highlight", "Ripples", shape="ring", color_mode="pitch", duration=0.9, size=1.4, bloom=1.0),
     L("particles", "Sparks", emit="on notes", shape="dot", count=6, size=2.2, speed=260.0, spread=360.0, gravity=120.0, life=0.8,
       color_a="#ff4fd8", color_b="#4fe8ff", color_by="staff", alpha=0.9, softness=1.0),
     L("bloom", "Neon bloom", threshold=0.45, radius=22.0, strength=0.9),
     L("aberration", "Fringing", amount=0.0, bindings={"amount": [("events", 5.0)]}),
     L("grain", "Grain", amount=4.0),
     L("vignette", "Dark corners", strength=0.55, color="#02000a"),
     L("shake", "Shake", bindings={"amount": [("events", 10.0)]})],
    events={"use_dynamics": True, "use_accents": True})

MONO_STORM = _look(
    "Monochrome storm", "Black and white thunderstorm: grey boiling clouds, slanting rain, flashes of lightning on loud "
    "events, film grain.",
    [L("gradient", "Sky", top="#101214", bottom="#2a2d30"),
     L("noise", "Clouds", blend="add", style="billow", scale=0.45, detail=4, speed=0.04, direction=0.0, evolve=0.3,
       color_dark="#000000", color_mid="#16181a", color_bright="#52575c", opacity=0.45, bindings={"opacity": [("activity", 0.3)]}),
     L("particles", "Rain", shape="streak", count=420, size=1.2, alpha=0.4, speed=900.0, angle=100.0, spread=6.0, streak=0.04,
       depth=3, color_a="#c0c6cc", color_b="#ffffff", bindings={"speed": [("activity", 900.0)]}),
     L("score", "Notation", color="#f4f4f4", color_b="#ffffff", glow=0.3),
     L("highlight", "Highlight", shape="glow", color_mode="single", color_a="#ffffff", duration=0.35, bloom=1.0),
     L("solid", "Lightning", blend="add", color="#dfe6ff", opacity=0.0, bindings={"opacity": [("impacts", 0.7)]}),
     L("grade", "Black and white", saturation=0.0, contrast=1.15),
     L("grain", "Film grain", amount=9.0, size=2),
     L("vignette", "Dark corners", strength=0.7, color="#000000"),
     L("shake", "Shake", bindings={"amount": [("events", 18.0)]})],
    events={"use_dynamics": True, "use_accents": True})

PAPER = _look(
    "Paper (daylight)", "Warm cream paper with a touch of grain, brown ink, soft orange highlights. Calm and legible.",
    [L("gradient", "Paper", top="#f4ead6", bottom="#e8d8b8", style="radial", center_y=0.4, radius=1.2),
     L("grain", "Paper grain", amount=5.0, size=2),
     L("score", "Notation", color="#2b1d12", color_b="#2b1d12"),
     L("highlight", "Highlight", shape="glow", color_mode="single", color_a="#e0701a", duration=0.7, bloom=0.0, intensity=1.0),
     L("vignette", "Soft edges", strength=0.25, color="#8a6a3a")],
    settings={"paper": "#f4ead6"})

PHOTO = _look(
    "Photo backdrop (your own picture)", "Your photo or video behind the music, slowly zooming, with light-up notes "
    "and a soft vignette. Open the 'Picture or video' layer and choose a file.",
    [L("gradient", "Fallback", top="#2a1a3a", bottom="#e08a3a", enabled=True),
     L("media", "Picture", end_zoom=1.12, brightness=-0.15, fit="cover"),
     L("particles", "Dust", shape="dot", count=90, size=2.0, alpha=0.5, speed=14.0, angle=-80.0, spread=60.0, wobble=20.0,
       softness=1.5, color_a="#ffe2a0", color_b="#ffffff"),
     L("score", "Notation", color="#fffaf0", color_b="#ffffff", glow=0.9, glow_radius=16.0),
     L("highlight", "Highlight", shape="glow", color_mode="single", color_a="#ffd890", bloom=1.6),
     L("bloom", "Glow", threshold=0.6, radius=22.0, strength=0.5),
     L("vignette", "Dark corners", strength=0.5)],
    events={"use_dynamics": True, "use_accents": True})

BUILTIN = {"plain": PLAIN, "winter_wind": WINTER_WIND, "ember": EMBER, "fireflies": FIREFLIES, "ocean": OCEAN,
           "golden_rain": GOLDEN_RAIN, "neon": NEON, "mono_storm": MONO_STORM, "paper": PAPER, "photo": PHOTO}
