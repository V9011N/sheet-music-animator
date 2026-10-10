"""Building the engraved score of a project (shared by the editor and the render workers)."""
from __future__ import annotations

from pathlib import Path

from . import msengraver
from .engraver import Score, engrave
from .project import Project

NEEDS_MUSESCORE = (".mscz", ".mscx")


def build_score(project: Project, progress=None) -> Score:
    """Engrave the project's score with its layout and fit it to the recording it was aligned to.  MuseScore lays
    it out when the project says so and it is installed (a .mscz always needs it); otherwise Verovio does.  A
    MusicXML file MuseScore cannot lay out is engraved by Verovio, and the project remembers that."""
    s = project.settings
    mpl = None if s.measures_per_line < 0 else s.measures_per_line
    suffix = Path(project.xml_path).suffix.lower()
    if suffix in NEEDS_MUSESCORE or (s.engraver == "musescore" and msengraver.find_musescore()):
        try:
            score = msengraver.engrave_musescore(project.xml_path, s.ink, progress, measures_per_line=mpl,
                                                 line_starts=project.line_starts)
        except Exception:
            if suffix in NEEDS_MUSESCORE:
                raise
            s.engraver = "verovio"
            score = None
    else:
        score = None
    if score is None:
        if s.engraver == "musescore":
            s.engraver = "verovio"       # MuseScore is not installed: the elements are Verovio's from now on
        score = engrave(project.xml_path, s.layout, s.ink, progress, measures_per_line=mpl,
                        line_starts=project.line_starts)
    score.nominal_notes = list(score.notes)
    if project.time_map:
        score.warp(project.time_map)
    return score
