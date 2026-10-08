"""Ready-made looks.  A preset fills in the effect settings and the automation keyframes of a project
from the measure numbers of the score, so it needs a loaded (and, for a recording, aligned) score."""
from __future__ import annotations

from .project import EFFECT_CHANNELS, Effects, Key, Project, Settings, auto_camera

PRESETS = {"plain": "Plain (white page, no effects)", "winter_wind": "Winter Wind (storm)"}


def _start(score, number: int) -> float:
    """Time of the first note of measure `number` (1-based; clamped to the score)."""
    infos = score.measure_infos
    return infos[min(max(number, 1), len(infos)) - 1].time


def apply_preset(name: str, project: Project, score) -> list[str]:
    if name == "plain":
        return plain(project, score)
    if name == "winter_wind":
        return winter_wind(project, score)
    raise KeyError(name)


def plain(project: Project, score) -> list[str]:
    """The blank slate: dark ink on a white page, no effects, the automatic camera.  The score itself (its
    layout, a fitted recording, the audio) is left as it is."""
    s, d = project.settings, Settings()
    for name in ("paper", "ink", "width", "height", "fps", "crf", "preset", "tail", "reveal", "fade", "ghost",
                 "follow_width", "follow_lead"):
        setattr(s, name, getattr(d, name))
    project.effects = Effects()
    for ch in EFFECT_CHANNELS:
        project.channels[ch] = []
    project.channels.update(auto_camera(score, s))
    project.keys_edited = False
    return []


def winter_wind(project: Project, score) -> list[str]:
    """The look of the Winter Wind (Chopin, Etude Op. 25 No. 11) video: a calm, hushed introduction (bars
    1-4), a storm that breaks in bar 5 and rages with the music, a climb through bars 89-94, a scale-out in
    bar 95 and a final spotlight on bar 96."""
    notes: list[str] = []
    n = len(score.measure_infos)
    if n < 96:
        notes.append(f"This score has {n} measures; the preset expects the 96 of Winter Wind, so its cues are "
                     "squeezed onto the last measures.")
    last = lambda m: min(m, n)                       # noqa: E731
    t = lambda m: _start(score, last(m))             # noqa: E731
    T_STORM, T_SCALE, T_FINAL = t(5), t(95), t(96)

    s = project.settings
    s.fps, s.width, s.height, s.crf, s.reveal, s.tail = 60, 1920, 1080, 16, "instant", 1.5
    s.follow_lead = 0.42
    widths = sorted(sy.rect[2] for sy in score.systems)
    system_w = widths[len(widths) // 2] if widths else score.width
    s.follow_width = 0.8 * system_w

    fx = Effects(enabled=True)
    fx.title = "Winter Wind"
    fx.subtitle = "Frédéric Chopin  ·  Étude in A minor, Op. 25 No. 11"
    fx.title_in = [0.6, 2.2]
    fx.title_out = [max(t(3) - 1.5, 1.0), 2.5]
    fx.fade_out = 1.0
    fx.impulses = [{"t": T_STORM, "s": 1.0}, {"t": T_SCALE, "s": 0.9}, {"t": T_FINAL, "s": 1.0}]
    fx.hits = [{"m0": last(93) - 1, "m1": last(94) - 1, "s": 0.85}]
    fx.spotlights = [{"t": T_FINAL, "ramp": 0.55, "end": 0.0, "m0": last(96) - 1, "m1": last(96) - 1}]
    project.effects = fx

    # mood: calm until the storm breaks; hush: the held breath just before it; lift: snow is blown upwards
    K = lambda *a: Key(a[0], [a[1]], a[2] if len(a) > 2 else "smooth")      # noqa: E731
    ch = project.channels
    ch["mood"] = [K(0.0, 0.0), K(T_STORM - 0.05, 0.0), K(T_STORM + 0.30, 1.0)]
    h0 = t(4) + 3.0
    ch["hush"] = [K(0.0, 0.0), K(h0, 0.0), K(h0 + 2.0, 1.0), K(T_STORM - 0.05, 1.0), K(T_STORM + 0.30, 0.0)]
    ch["lift"] = [K(0.0, 0.0), K(T_SCALE, 0.0), K(T_SCALE + 0.6, 1.0), K(T_FINAL + 0.39, 1.0, "linear"),
                  K(T_FINAL + 0.4, 0.0)]

    # camera: follow the music; close in during the introduction, ease out when the storm breaks, push in
    # through the climax, pull back in bar 95, then close in on the last bar
    project.channels.update(auto_camera(score, s))
    base = s.follow_width
    c0, c1 = t(89), T_SCALE
    sizes = [(0.0, base / 1.28), (h0, base / 1.28), (h0 + 2.0, base / 1.46), (T_STORM - 0.05, base / 1.46),
             (T_STORM + 0.30, base), (c0, base), (c0 + 1.5, base / 1.07), (c1, base / 1.07),
             (c1 + 0.5, base / 0.82)]
    fin = score.measure_infos[last(96) - 1]
    line = score.systems[fin.system] if fin.system < len(score.systems) else None
    if line is not None:
        h_line = line.rect[3] + 800
        w_fin = min(h_line / 0.8 * s.aspect, base / 0.82)
        sizes += [(T_FINAL - 0.12, base / 0.82), (T_FINAL + 0.43, w_fin)]
    sizes = sorted({round(a, 3): (a, b) for a, b in sizes}.values())
    ch["size"] = [Key(a, [b]) for a, b in sizes]
    if line is not None:
        pos = ch["pos"]
        cur = project.camera_at(T_FINAL - 0.12)
        pos[:] = [k for k in pos if k.t < T_FINAL - 0.12]
        if cur is not None:
            pos.append(Key(T_FINAL - 0.12, [cur[0], cur[1]]))
        pos.append(Key(T_FINAL + 0.43, [fin.rect[0] + fin.rect[2] / 2, line.rect[1] + line.rect[3] / 2 + 100]))
    project.keys_edited = True
    return notes
