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
    project.timed = {tr(u) for u in project.timed if tr(u) is not None}
    project.transforms = {tr(u): v for u, v in project.transforms.items() if tr(u) is not None}
    if not project.keys_edited or not old.measure_infos or not new.measure_infos:
        project.channels = auto_camera(new, project.settings)
        project.keys_edited = False
        return
    times = [m.time for m in old.measure_infos]
    for k in project.channels["pos"]:
        i = min(max(bisect_right(times, k.t) - 1, 0), len(old.measure_infos) - 1, len(new.measure_infos) - 1)
        o, n = old.measure_infos[i].rect, new.measure_infos[i].rect
        k.v = [n[0] + (k.v[0] - o[0]), n[1] + (k.v[1] - o[1])]
