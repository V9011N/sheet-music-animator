"""Carrying a project over to a new line layout of the same score."""
from __future__ import annotations

from bisect import bisect_right

from .engraver import Score
from .project import Project, auto_camera


def unit_keys(score: Score) -> dict[int, tuple]:
    """A key for every element that survives a change of the line breaks: (measure, kind, n-th of that kind
    in the measure).  Element numbers themselves shift because every line has its own labels and braces."""
    seen: dict[tuple, int] = {}
    out = {}
    for u in score.units:
        k = (u.measure, u.kind)
        out[u.uid] = k + (seen.get(k, 0),)
        seen[k] = seen.get(k, 0) + 1
    return out


def relayout_project(project: Project, old: Score, new: Score) -> None:
    """Re-map the element numbers and the camera of `project` from the layout of `old` to that of `new`."""
    old_keys = unit_keys(old)
    new_by_key = {k: uid for uid, k in unit_keys(new).items()}

    def tr(uid):
        return new_by_key.get(old_keys.get(uid))

    project.overrides = {tr(u): v for u, v in project.overrides.items() if tr(u) is not None}
    project.ends = {tr(u): v for u, v in project.ends.items() if tr(u) is not None}
    project.timed = {tr(u) for u in project.timed if tr(u) is not None}
    project.transforms = {tr(u): v for u, v in project.transforms.items() if tr(u) is not None}
    project.deleted = {tr(u) for u in project.deleted if tr(u) is not None}
    # keys set by hand move with the measure they are at; the automatic path is laid down again
    can_move = bool(old.measure_infos and new.measure_infos)
    keep_x, keep_y = project.keys_edited and can_move, project.y_edited and can_move
    auto = auto_camera(new, project.settings, y=not keep_y)
    if not keep_x:
        project.channels.update({c: v for c, v in auto.items() if c != "y"})
        project.keys_edited = False
    if not keep_y:
        project.channels["y"] = auto["y"]
        project.y_edited = False
    times = [m.time for m in old.measure_infos]
    for ch, axis, keep in (("x", 0, keep_x), ("y", 1, keep_y)):
        for k in project.channels[ch] if keep else []:
            i = min(max(bisect_right(times, k.t) - 1, 0), len(old.measure_infos) - 1, len(new.measure_infos) - 1)
            o, n = old.measure_infos[i].rect, new.measure_infos[i].rect
            k.v = [n[axis] + (k.v[0] - o[axis])]
