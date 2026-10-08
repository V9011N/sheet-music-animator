"""Regression tests for audio alignment, effects signals and the effects compositor.

Run:  python -m unittest discover tests
They build a small MusicXML score on the fly, so no files are needed.
"""
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np                                    # noqa: E402
from PySide6.QtWidgets import QApplication            # noqa: E402

from sheet_music_animator import analysis, audio     # noqa: E402
from sheet_music_animator.build import build_score    # noqa: E402
from sheet_music_animator.project import Key, Project, auto_camera, retimer   # noqa: E402

_app = QApplication.instance() or QApplication([])

PITCHES = ["C", "D", "E", "F", "G", "A", "B", "C"]


def make_score_xml(measures=12) -> str:
    """Piano, 4/4, two staves: running quarter notes on top, a bass note below; a dynamic, an accent, a tie."""
    rng = random.Random(7)          # no repeating pattern: every note has to be found on its own
    out = ['<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1"><part-list><score-part id="P1">'
           '<part-name>Piano</part-name></score-part></part-list><part id="P1">']
    for m in range(1, measures + 1):
        out.append(f'<measure number="{m}">')
        if m == 1:
            out.append('<attributes><divisions>2</divisions><key><fifths>0</fifths></key><time><beats>4</beats>'
                       '<beat-type>4</beat-type></time><staves>2</staves><clef number="1"><sign>G</sign><line>2</line></clef>'
                       '<clef number="2"><sign>F</sign><line>4</line></clef></attributes>'
                       '<direction placement="below"><direction-type><metronome><beat-unit>quarter</beat-unit>'
                       '<per-minute>100</per-minute></metronome></direction-type><sound tempo="100"/></direction>')
        if m == 3:
            out.append('<direction placement="below"><direction-type><dynamics><ff/></dynamics></direction-type>'
                       '<staff>1</staff></direction>')
        for i in range(4):
            step = PITCHES[rng.randrange(7)]
            octave = rng.choice((4, 5))
            art = "<notations><articulations><accent/></articulations></notations>" if (m == 5 and i == 0) else ""
            out.append(f'<note><pitch><step>{step}</step><octave>{octave}</octave></pitch><duration>2</duration>'
                       f'<type>quarter</type><staff>1</staff>{art}</note>')
        out.append('<backup><duration>8</duration></backup>')
        tie_start = '<tie type="start"/>' if m == 6 else ""
        tie_stop = '<tie type="stop"/>' if m == 7 else ""
        notations = ('<notations><tied type="start"/></notations>' if m == 6 else
                     '<notations><tied type="stop"/></notations>' if m == 7 else "")
        out.append(f'<note><pitch><step>C</step><octave>3</octave></pitch><duration>8</duration>{tie_stop}{tie_start}'
                   f'<type>whole</type><staff>2</staff>{notations}</note>')
        out.append("</measure>")
    out.append("</part></score-partwise>")
    return "".join(out)


def make_project(tmp: Path) -> Project:
    xml = tmp / "test.musicxml"
    xml.write_text(make_score_xml(), encoding="utf8")
    p = Project(xml_path=str(xml))
    p.settings.measures_per_line = 4
    return p


class TestScore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.project = make_project(cls.tmp)
        cls.score = build_score(cls.project)

    def test_noteheads_carry_staff_and_tie_information(self):
        heads = [h for u in self.score.units for h in u.heads]
        self.assertEqual(len(heads), 12 * 5)                       # four quarters and a bass note per measure
        self.assertEqual({h[4] for h in heads}, {0, 1})
        self.assertEqual(sum(1 for h in heads if h[5]), 1)         # one note only continues a tie

    def test_dynamics_and_accents_are_labelled(self):
        labels = [u.label for u in self.score.units if u.kind in ("dynam", "artic")]
        self.assertIn("ff", labels)
        self.assertIn("acc", labels)

    def test_warp_moves_everything_consistently(self):
        score = build_score(self.project)
        t0 = score.units[10].time
        score.warp([[0.0, 1.0], [10.0, 25.0], [score.duration + 1, score.duration + 40]])
        self.assertGreater(score.units[10].time, t0)
        self.assertTrue(all(b[1] >= a[1] for a, b in zip(score.measures, score.measures[1:])))
        self.assertGreater(score.duration, score.nominal_notes[-1][2])

    def test_retimer_maps_between_two_time_maps(self):
        f = retimer([[0, 0], [10, 10]], [[0, 0], [10, 20]], [0, 5, 10])
        self.assertAlmostEqual(f(5.0), 10.0, places=6)
        self.assertAlmostEqual(f(12.0), 22.0, places=6)            # beyond the map: shifted by the last offset


class TestAlignment(unittest.TestCase):
    def test_recovers_a_wobbly_tempo(self):
        tmp = Path(tempfile.mkdtemp())
        score = build_score(make_project(tmp))
        warp = lambda t: 1.3 * t + 0.6 * np.sin(t / 3.0) + 0.7 + (1.5 if t > 12 else 0.0)   # noqa: E731  (tempo wobble + a 1.5 s pause)
        notes = [(p, warp(a), warp(b), v) for p, a, b, v in score.nominal_notes]
        wav = tmp / "perf.wav"
        audio.write_wav(wav, audio.synthesize(notes, warp(score.duration)))
        al = analysis.align_score(score.nominal_notes, str(wav))
        err = np.abs(al.actual - np.array([warp(x) for x in al.nominal]))
        self.assertLess(np.median(err), 0.04)
        self.assertLess(err.max(), 0.08)                           # every note, including the one after the pause

    def test_loudness_follows_the_music(self):
        tmp = Path(tempfile.mkdtemp())
        notes = [(60, 0.0, 1.0, 120), (60, 2.0, 3.0, 30)]
        wav = tmp / "two.wav"
        audio.write_wav(wav, audio.synthesize(notes, 3.5))
        rms, fps = analysis.loudness(str(wav))
        self.assertGreater(rms[int(0.5 * fps)], 2 * rms[int(2.5 * fps)])


class TestEffects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.project = make_project(cls.tmp)
        cls.score = build_score(cls.project)
        cls.project.channels.update(auto_camera(cls.score, cls.project.settings))
        cls.project.effects.enabled = True

    def test_channel_sampling_matches_pointwise_evaluation(self):
        p = self.project
        p.channels["mood"] = [Key(1.0, [0.0]), Key(3.0, [1.0]), Key(5.0, [0.2], "hold"), Key(7.0, [0.9])]
        ts = np.linspace(0, 9, 91)
        vec = p.sample_effect("mood", ts)
        pt = np.array([p.effect_at("mood", t) for t in ts])
        self.assertLess(np.abs(vec - pt).max(), 1e-5)

    def test_dynamics_and_accents_make_events(self):
        from sheet_music_animator.effects import EffectTracks
        tr = EffectTracks(self.project, self.score, 30, 30.0, None)
        self.assertGreaterEqual(len(tr.impulses), 2)
        k = int(tr.impulses[0][0] * 30)
        self.assertGreater(tr.shake_x[k:k + 6].std() + tr.shake_y[k:k + 6].std(), 0)

    def test_frame_has_a_backdrop_and_lights_up_a_struck_note(self):
        from sheet_music_animator.export import EffectsRenderer
        from sheet_music_animator.scene import SheetScene
        scene = SheetScene(self.score, self.project)
        scene.set_cache(False)
        W, H = 480, 270
        r = EffectsRenderer(scene, self.project, W, H, 30, self.score.duration + 2, None)
        t_hit = next(u.time for u in self.score.units if u.heads and u.time > 4.0)
        lit = r.frame(int(t_hit * 30) + 2)
        dark = r.frame(int(t_hit * 30) + 40)
        self.assertEqual(lit.shape, (H, W, 3))
        self.assertEqual(lit.dtype, np.uint8)
        # the glow: a cyan-ish (blue > red) bright patch that has faded 1.3 s later
        blue_excess = lambda f: int(((f[..., 2].astype(int) - f[..., 0].astype(int)) > 90).sum())   # noqa: E731
        self.assertGreater(blue_excess(lit), blue_excess(dark))
        self.assertGreater(lit.mean(), 5)                          # not black: backdrop and snow are drawn


class TestPresets(unittest.TestCase):
    def test_plain_preset_restores_the_blank_slate(self):
        from sheet_music_animator.presets import apply_preset
        project = make_project(Path(tempfile.mkdtemp()))
        score = build_score(project)
        project.channels.update(auto_camera(score, project.settings))
        apply_preset("winter_wind", project, score)
        self.assertTrue(project.effects.enabled)
        project.settings.ink, project.settings.paper = "#ffffff", "#000000"
        apply_preset("plain", project, score)
        self.assertFalse(project.effects.enabled)
        self.assertEqual((project.settings.paper, project.settings.ink, project.settings.fps), ("#ffffff", "#1a1a1a", 30))
        self.assertTrue(all(not project.channels[c] for c in ("mood", "hush", "lift")))
        self.assertTrue(project.has_keys())


class TestProject(unittest.TestCase):
    def test_effects_and_time_map_survive_a_round_trip(self):
        p = Project()
        p.effects.enabled, p.effects.title = True, "Winter Wind"
        p.effects.impulses.append({"t": 1.5, "s": 0.8})
        p.time_map = [[0.0, 0.5], [10.0, 14.0]]
        p.channels["lift"].append(Key(2.0, [1.0]))
        q = Project()
        q.load_dict(json.loads(json.dumps(p.to_dict())))
        self.assertTrue(q.effects.enabled)
        self.assertEqual(q.effects.title, "Winter Wind")
        self.assertEqual(q.effects.impulses, [{"t": 1.5, "s": 0.8}])
        self.assertEqual(q.time_map, [[0.0, 0.5], [10.0, 14.0]])
        self.assertEqual(q.channels["lift"][0].v, [1.0])

    def test_retime_moves_keys_and_events(self):
        p = Project()
        p.channels["mood"].append(Key(4.0, [1.0]))
        p.effects.impulses.append({"t": 4.0, "s": 1.0})
        p.retime(lambda t: t * 2)
        self.assertEqual(p.channels["mood"][0].t, 8.0)
        self.assertEqual(p.effects.impulses[0]["t"], 8.0)


if __name__ == "__main__":
    unittest.main()
