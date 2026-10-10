"""Headless rendering:  python -m sheet_music_animator.cli render piece.mxl --audio perf.mp3 --align --look winter_wind

Does what the editor does, without a window: engrave the score, fit it to a recording, apply a look (or the
automatic camera), and render with one process per slice of the video.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _progress_printer(label: str):
    t0, last, finished = time.perf_counter(), [0.0], [False]

    def progress(done, total):
        now = time.perf_counter()
        if now - last[0] > 2.0 or (done >= total and not finished[0]):
            last[0] = now
            finished[0] = done >= total
            el = now - t0
            eta = f", about {el / done * (total - done) / 60:.1f} min left" if done > 20 else ""
            print(f"  {label}: {done}/{total} frames ({100 * done / max(total, 1):.0f}%){eta}", flush=True)
        return True
    return progress


def render(args) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication(["cli"])      # noqa: F841 - keeps Qt alive
    from . import analysis, msengraver
    from .build import build_score
    from .export import render_video, render_video_parallel, total_duration
    from . import looks
    from .project import Project
    from .scene import SheetScene

    src = Path(args.input)
    if src.suffix == ".smanim":
        project = Project.load(src)
    else:
        project = Project(xml_path=str(src))
        if not args.verovio and msengraver.find_musescore():
            project.settings.engraver = "musescore"
        project.settings.measures_per_line = args.measures_per_line
        project.settings.layout = "horizontal" if args.measures_per_line == 0 else "pages"
    s = project.settings
    if args.audio:
        s.audio = str(Path(args.audio).resolve())

    print("Engraving…", flush=True)
    score = build_score(project)
    if args.align:
        if s.audio in ("synth", "none"):
            print("--align needs a recording: add --audio <file>.")
            return 2
        print("Fitting the score to the recording…", flush=True)
        al = analysis.align_score(score.nominal_notes, s.audio,
                                  lambda f, text="": print(f"  {100 * f:3.0f}% {text}", flush=True) or True,
                                  rolls=score.rolls)
        project.time_map, s.align_audio = al.points(), s.audio
        project.sync_conf, project.sync_overall = al.heat(), al.overall
        score = build_score(project)
        print(f"  score now lasts {score.duration:.1f} s ({len(al.nominal)} note positions fitted)")
        print(f"  audio synced with {al.overall * 100:.0f}% confidence")
    if args.look:
        key = args.look
        if key not in looks.BUILTIN and not key.startswith(("user:", "file:")):
            key = "file:" + key if Path(key).exists() else "user:" + key
        for note in looks.apply_look(looks.get_look(key), project, score):
            print("  note:", note)
    if not project.has_keys():
        project.follow_music(score)
    if args.fps:
        s.fps = args.fps
    if args.size:
        s.width, s.height = (int(v) for v in args.size.lower().split("x"))
    if args.crf is not None:
        s.crf = args.crf
    if args.speed:
        s.preset = args.speed
    if args.save_project:
        project.save(args.save_project)
        print("Saved", args.save_project)

    scene = SheetScene(score, project)
    duration = total_duration(scene, project)
    audio_path = None if s.audio == "none" else s.audio
    if s.audio == "synth":
        from . import audio
        audio_path = str(Path(args.out).with_suffix(".wav"))
        from .midiroll import edited_notes, link_notes
        audio.write_wav(audio_path, audio.synthesize(edited_notes(project, score, link_notes(score)), score.duration))
    out = str(Path(args.out))
    print(f"Rendering {duration:.1f} s at {s.width}x{s.height} {s.fps} fps…", flush=True)
    t0 = time.perf_counter()
    if project.effects.enabled:
        render_video_parallel(project, out, audio_path, audio_path, duration, _progress_printer("frames"),
                              args.workers, None, args.start, args.end)
    else:
        scene.set_cache(False)
        render_video(scene, project, out, audio_path, _progress_printer("frames"))
    print(f"Done in {(time.perf_counter() - t0) / 60:.1f} min -> {out}")
    if s.audio == "synth" and audio_path and Path(audio_path).exists():
        Path(audio_path).unlink()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sheet_music_animator.cli", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="render a MusicXML file or a .smanim project to a video")
    r.add_argument("input", help="a score (.mscz, or MusicXML .mxl/.musicxml/.xml) or a .smanim project")
    r.add_argument("--verovio", action="store_true", help="engrave with Verovio even when MuseScore is installed")
    r.add_argument("--out", required=True, help="output .mp4")
    r.add_argument("--audio", help="recording to use as the soundtrack (default: the built-in synth)")
    r.add_argument("--align", action="store_true", help="fit the score's timing to the recording first")
    r.add_argument("--look", help="apply a look (after aligning): a built-in name (plain, winter_wind, ember, fireflies, "
                                  "ocean, golden_rain, neon, mono_storm, paper, photo), the name of one of yours, or a .json file")
    r.add_argument("--measures-per-line", type=int, default=4,
                   help="0 = the whole score on one line, -1 = as printed (MuseScore's own line breaks)")
    r.add_argument("--size", help="e.g. 1920x1080")
    r.add_argument("--fps", type=int)
    r.add_argument("--crf", type=int, help="quality: lower is better and bigger (default 16)")
    r.add_argument("--speed", dest="speed", help="x264 speed: ultrafast, veryfast, fast, medium (default), slow...")
    r.add_argument("--workers", type=int, help="processes for renders with effects (default: a few; more than ~4 hardly helps)")
    r.add_argument("--start", type=float, default=0.0, help="render only from this second")
    r.add_argument("--end", type=float, help="render only up to this second")
    r.add_argument("--save-project", help="also write the resulting project (.smanim)")
    r.set_defaults(fn=render)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
