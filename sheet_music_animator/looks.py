"""Looks: saved layer stacks.  The built-in ones are in stacks.py; yours are JSON files in
~/.sheet_music_animator/looks (share a look by sending the file).  Times inside a look are anchored to
measures, so a look made for one piece fits another."""
from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

from .layers import Layer, schema
from .project import LANE, Key, Project, Settings, auto_camera, is_lane
from .stacks import BUILTIN

USER_DIR = Path.home() / ".sheet_music_animator" / "looks"


def list_looks() -> list[dict]:
    """[{"key", "name", "description", "builtin"}] - the built-in looks, then yours."""
    out = [{"key": k, "name": v["name"], "description": v["description"], "builtin": True} for k, v in BUILTIN.items()]
    if USER_DIR.exists():
        for f in sorted(USER_DIR.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf8"))
                out.append({"key": f"user:{f.stem}", "name": d.get("name", f.stem), "description": d.get("description", ""),
                            "builtin": False})
            except (OSError, ValueError):
                continue
    return out


def get_look(key: str) -> dict:
    if key.startswith("user:"):
        return json.loads((USER_DIR / f"{key[5:]}.json").read_text(encoding="utf8"))
    if key.startswith("file:"):
        return json.loads(Path(key[5:]).read_text(encoding="utf8"))
    return BUILTIN[key]


# ---------------------------------------------------------------------------------------------- anchors
def resolve(anchor, score, notes: list | None = None) -> float:
    """Seconds for an anchor ({"m": measure number, "dt"}, {"t"}) or a plain number."""
    if isinstance(anchor, (int, float)):
        return float(anchor)
    if "t" in anchor:
        return float(anchor["t"]) + float(anchor.get("dt", 0.0))
    infos = score.measure_infos
    m = int(anchor["m"])
    if m > len(infos) and notes is not None and "squeezed" not in " ".join(notes):
        notes.append(f"The score has {len(infos)} measures; cues placed after that are squeezed onto the last one.")
    if not infos:
        return float(anchor.get("dt", 0.0))
    return infos[min(max(m, 1), len(infos)) - 1].time + float(anchor.get("dt", 0.0))


def to_anchor(t: float, score) -> dict:
    """The anchor for a time: the measure it falls in and the seconds after that measure's start."""
    infos = score.measure_infos
    i = None
    for j, m in enumerate(infos):
        if m.time <= t + 1e-6:
            i = j
        else:
            break
    if i is None:
        return {"t": round(t, 3)}
    return {"m": i + 1, "dt": round(t - infos[i].time, 3)}


# ---------------------------------------------------------------------------------------------- apply
def apply_look(look: dict, project: Project, score) -> list[str]:
    """Replace the effects of `project` (layers, lanes, events) by `look`, and set the settings it carries."""
    notes: list[str] = []
    s, fx = project.settings, project.effects
    valid = {f.name for f in fields(Settings)}
    restyle = False
    for key, v in look.get("settings", {}).items():
        if key not in valid:
            continue
        if isinstance(v, dict) and "system_fraction" in v:
            widths = sorted(sy.rect[2] for sy in score.systems)
            v = v["system_fraction"] * (widths[len(widths) // 2] if widths else score.width)
        setattr(s, key, v)
        restyle = restyle or key in ("follow_width", "follow_lead", "width", "height")
    fx.enabled = bool(look.get("enabled", True))
    layers = []
    for d in look.get("layers", []):
        lay = Layer.from_dict({**d, "id": None})
        for prm in schema(lay.type):
            v = lay.params.get(prm.name)
            if prm.kind == "time" and isinstance(v, dict):
                lay.params[prm.name] = round(resolve(v, score, notes), 3)
        layers.append(lay)
    fx.layers = layers
    for ch in [c for c in project.channels if is_lane(c)]:
        del project.channels[ch]
    fx.lanes = {}
    for name, spec in look.get("lanes", {}).items():
        fx.lanes[name] = float(spec.get("default", 0.0))
        keys = [Key(round(resolve(k["at"], score, notes), 3), [float(k["v"])], k.get("ease", "smooth")) for k in spec.get("keys", [])]
        project.channels[LANE + name] = sorted(keys, key=lambda k: k.t)
    ev = look.get("events", {})
    fx.use_dynamics, fx.use_accents = ev.get("use_dynamics", True), ev.get("use_accents", True)
    fx.impulses = [{"t": round(resolve(i["at"], score, notes), 3), "s": float(i["s"])} for i in ev.get("impulses", [])]
    fx.hits = [{"m0": int(h["m0"]) - 1, "m1": int(h["m1"]) - 1, "s": float(h["s"])} for h in ev.get("hits", [])]
    if restyle or not look.get("layers"):          # the look decides the camera framing: start from the automatic path
        project.channels.update(auto_camera(score, s))
        project.keys_edited = False
    for plan in look.get("camera", []):
        if "focus" in plan:
            _focus(project, score, plan["focus"], notes)
    return notes


def _focus(project: Project, score, plan: dict, notes: list) -> None:
    """Zoom the camera onto one measure (and its line) at a time: the closing shot."""
    infos = score.measure_infos
    if not infos:
        return
    m = min(max(int(plan["m"]), 1), len(infos)) - 1
    t0 = resolve({"m": plan["m"], "dt": plan.get("dt", 0.0)}, score, notes)
    t1 = t0 + float(plan.get("dur", 0.55))
    info = infos[m]
    line = score.systems[info.system] if info.system < len(score.systems) else None
    if line is None:
        return
    s = project.settings
    cur = project.camera_at(t0)
    for ch in ("pos", "size"):
        project.channels[ch] = [k for k in project.channels[ch] if k.t < t0 - 1e-6]
    if cur is not None:
        project.channels["pos"].append(Key(t0, [cur[0], cur[1]]))
        project.channels["size"].append(Key(t0, [cur[2]]))
    h_line = line.rect[3] + 800
    project.channels["pos"].append(Key(t1, [info.rect[0] + info.rect[2] / 2, line.rect[1] + line.rect[3] / 2 + 100]))
    project.channels["size"].append(Key(t1, [h_line / 0.8 * s.aspect]))
    project.keys_edited = True


# ---------------------------------------------------------------------------------------------- capture / save
def capture_look(project: Project, score, name: str, description: str = "") -> dict:
    """The current effects of the project as a look, with times anchored to measures."""
    fx = project.effects

    def anchor(t):
        return to_anchor(float(t), score)
    layers = []
    for lay in fx.layers:
        d = lay.to_dict()
        d.pop("id", None)
        for prm in schema(lay.type):
            v = d["params"].get(prm.name)
            if prm.kind == "time" and isinstance(v, (int, float)) and v > 0:
                d["params"][prm.name] = anchor(v)
        layers.append(d)
    lanes = {n: {"default": float(fx.lanes[n]), "keys": [{"at": anchor(k.t), "v": k.v[0], "ease": k.ease}
                                                           for k in project.channels.get(LANE + n, [])]} for n in fx.lanes}
    ev = {"use_dynamics": fx.use_dynamics, "use_accents": fx.use_accents,
          "impulses": [{"at": anchor(i["t"]), "s": i["s"]} for i in fx.impulses],
          "hits": [{"m0": h["m0"] + 1, "m1": h["m1"] + 1, "s": h["s"]} for h in fx.hits]}
    s = project.settings
    return {"name": name, "description": description, "enabled": True, "layers": layers, "lanes": lanes, "events": ev,
            "settings": {"fps": s.fps, "width": s.width, "height": s.height}, "camera": []}


def save_user_look(look: dict) -> Path:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\- ]+", "", look["name"]).strip().replace(" ", "_") or "look"
    path = USER_DIR / f"{stem}.json"
    path.write_text(json.dumps(look, indent=1), encoding="utf8")
    return path


def delete_user_look(key: str) -> None:
    if key.startswith("user:"):
        (USER_DIR / f"{key[5:]}.json").unlink(missing_ok=True)
