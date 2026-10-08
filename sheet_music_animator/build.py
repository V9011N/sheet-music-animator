"""Building the engraved score of a project (shared by the editor and the render workers)."""
from __future__ import annotations

from .engraver import Score, engrave
from .project import Project


def build_score(project: Project, progress=None) -> Score:
    """Engrave the project's MusicXML with its layout and fit it to the recording it was aligned to."""
    s = project.settings
    score = engrave(project.xml_path, s.layout, s.ink, progress,
                    measures_per_line=None if s.measures_per_line < 0 else s.measures_per_line,
                    line_starts=project.line_starts)
    score.nominal_notes = list(score.notes)
    if project.time_map:
        score.warp(project.time_map)
    return score
