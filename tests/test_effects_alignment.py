"""Regression tests for audio alignment, the effect layers, signals, looks and the project's effect data.

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

from sheet_music_animator import analysis, audio, looks   # noqa: E402
from sheet_music_animator.build import build_score    # noqa: E402
from sheet_music_animator.layers import LAYER_TYPES, Layer, bind, new_layer   # noqa: E402
from sheet_music_animator.project import LANE, Key, Project, auto_camera, retimer   # noqa: E402

_app = QApplication.instance() or QApplication([])

PITCHES = ["C", "D", "E", "F", "G", "A", "B", "C"]


def make_score_xml(measures=12) -> str:
    """Piano, 4/4, two staves: random quarter notes on top, a bass note below; a dynamic, an accent, a tie."""
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

    def test_noteheads_carry_staff_tie_and_pitch(self):
        heads = [h for u in self.score.units for h in u.heads]
        self.assertEqual(len(heads), 12 * 5)                       # four quarters and a bass note per measure
        self.assertEqual({h[4] for h in heads}, {0, 1})
        self.assertEqual(sum(1 for h in heads if h[5]), 1)         # one note only continues a tie
        self.assertEqual({h[6] for h in heads if h[4] == 1}, {48})  # the bass note is C3

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

    def test_follows_a_long_ritardando_and_a_faster_ending(self):
        tmp = Path(tempfile.mkdtemp())
        score = build_score(make_project(tmp))

        def warp(t):                      # a ritardando to half speed, then faster than the score
            return t if t < 9 else 9 + 2.2 * (t - 9) if t < 19 else 31 + 0.85 * (t - 19)
        notes = [(p, warp(a), warp(b), v) for p, a, b, v in score.nominal_notes]
        wav = tmp / "rit.wav"
        audio.write_wav(wav, audio.synthesize(notes, warp(score.duration)))
        al = analysis.align_score(score.nominal_notes, str(wav))
        err = np.abs(al.actual - np.array([warp(x) for x in al.nominal]))
        self.assertLess(err.max(), 0.1)

    def test_a_held_final_chord_does_not_drag_the_ending_late(self):
        """The recording rings on for seconds after the last attack (a fermata): the last notes must still be
        found at their attacks, not stretched over the ring."""
        tmp = Path(tempfile.mkdtemp())
        score = build_score(make_project(tmp))
        warp = lambda t: 1.1 * t + 0.4 * np.sin(t / 3.0) + 0.5                              # noqa: E731
        last = max(n[2] for n in score.nominal_notes)
        notes = [(p, warp(a), warp(b) + (6.0 if b >= last - 0.01 else 0.0), v) for p, a, b, v in score.nominal_notes]
        wav = tmp / "fermata.wav"
        audio.write_wav(wav, audio.synthesize(notes, warp(score.duration) + 6.0))
        al = analysis.align_score(score.nominal_notes, str(wav))
        err = np.abs(al.actual - np.array([warp(x) for x in al.nominal]))
        self.assertLess(err.max(), 0.1)

    def test_loudness_follows_the_music(self):
        tmp = Path(tempfile.mkdtemp())
        notes = [(60, 0.0, 1.0, 120), (60, 2.0, 3.0, 30)]
        wav = tmp / "two.wav"
        audio.write_wav(wav, audio.synthesize(notes, 3.5))
        rms, fps = analysis.loudness(str(wav))
        self.assertGreater(rms[int(0.5 * fps)], 2 * rms[int(2.5 * fps)])


class TestLayers(unittest.TestCase):
    def test_every_layer_type_has_a_complete_catalogue_entry(self):
        for key, t in LAYER_TYPES.items():
            lay = new_layer(key)
            for p in t.all_params():
                v = lay.get(p.name)
                if p.kind in ("float", "int", "time"):
                    self.assertTrue(p.lo - 1e-9 <= float(v) <= p.hi + 1e-9 or p.kind == "time", (key, p.name, v))
                if p.kind == "choice":
                    self.assertIn(v, p.choices, (key, p.name))
            self.assertEqual(Layer.from_dict(lay.to_dict()).to_dict()["params"], lay.to_dict()["params"])

    def test_layers_survive_a_round_trip_with_bindings(self):
        p = Project()
        lay = bind(new_layer("particles", "Snow", count=40), "speed", "activity", 300.0)
        p.effects.layers.append(lay)
        p.effects.enabled = True
        q = Project()
        q.load_dict(json.loads(json.dumps(p.to_dict())))
        self.assertTrue(q.effects.enabled)
        self.assertEqual(q.effects.layers[0].get("count"), 40)
        self.assertEqual(q.effects.layers[0].bindings["speed"][0]["amount"], 300.0)


class TestProject(unittest.TestCase):
    def test_lanes_are_named_and_sampled(self):
        p = Project()
        name = p.add_lane("Storm")
        p.channels[LANE + name] = [Key(1.0, [0.0]), Key(3.0, [1.0]), Key(5.0, [0.2], "hold"), Key(7.0, [0.9])]
        ts = np.linspace(0, 9, 91)
        vec = p.sample_lane(name, ts)
        pt = np.array([p.lane_at(name, t) for t in ts])
        self.assertLess(np.abs(vec - pt).max(), 1e-5)
        self.assertEqual(p.add_lane("Storm"), "Storm 2")

    def test_renaming_a_lane_keeps_the_bindings(self):
        p = Project()
        n = p.add_lane("Storm")
        lay = bind(new_layer("gradient"), "mix", LANE + n)
        p.effects.layers.append(lay)
        p.rename_lane("Storm", "Gale")
        self.assertEqual(lay.bindings["mix"][0]["src"], "lane:Gale")
        self.assertIn("lane:Gale", p.channels)
        p.remove_lane("Gale")
        self.assertEqual(lay.bindings["mix"], [])

    def test_retime_moves_keys_events_and_layer_times(self):
        p = Project()
        n = p.add_lane("L")
        p.channels[LANE + n].append(Key(4.0, [1.0]))
        p.effects.impulses.append({"t": 4.0, "s": 1.0})
        p.effects.layers.append(new_layer("text", text="x", start=2.0, end=-3.0))
        p.retime(lambda t: t * 2)
        self.assertEqual(p.channels[LANE + n][0].t, 8.0)
        self.assertEqual(p.effects.impulses[0]["t"], 8.0)
        self.assertEqual(p.effects.layers[0].get("start"), 4.0)
        self.assertEqual(p.effects.layers[0].get("end"), -3.0)      # relative to the end: unchanged

    def test_projects_of_the_first_version_still_open(self):
        old = {"version": 3, "channels": {"pos": [], "size": [], "rot": [], "mood": [{"t": 5.0, "v": [1.0], "ease": "smooth"}]},
               "effects": {"enabled": True, "bg_calm": ["#101010", "#202020"], "flash_colors": ["#ff0000", "#00ff00"],
                           "title": "Old", "mist": 1.0, "snow": 1.0}}
        p = Project()
        p.load_dict(old)
        self.assertTrue(p.effects.enabled)
        self.assertIn("Mood", p.effects.lanes)
        self.assertEqual(p.channels[LANE + "Mood"][0].t, 5.0)
        self.assertEqual(p.effects.layers[0].get("top"), "#101010")
        self.assertTrue(any(x.type == "text" and x.get("text") == "Old" for x in p.effects.layers))


class TestSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.project = make_project(cls.tmp)
        cls.score = build_score(cls.project)

    def test_dynamics_and_accents_make_events_and_bindings_follow_signals(self):
        from sheet_music_animator.signals import Signals
        sg = Signals(self.project, self.score, 30, 30.0, None)
        self.assertGreaterEqual(len(sg.events_list), 2)
        k = int(sg.events_list[0][0] * 30)
        self.assertGreater(sg.s["events"][k], 0.3)
        val = sg.evaluate(0.5, [{"src": "events", "amount": 2.0}], 0.0, 1.0)
        self.assertEqual(val.max(), 1.0)                            # clipped to the setting's range
        self.assertAlmostEqual(float(sg.evaluate(0.5, [], 0, 1)), 0.5)


class TestLooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.project = make_project(cls.tmp)
        cls.score = build_score(cls.project)
        cls.project.channels.update(auto_camera(cls.score, cls.project.settings))

    def _renderer(self, W=320, H=180):
        from sheet_music_animator.export import EffectsRenderer
        from sheet_music_animator.scene import SheetScene
        scene = SheetScene(self.score, self.project)
        scene.set_cache(False)
        return EffectsRenderer(scene, self.project, W, H, 30, self.score.duration + 2, None)

    def test_every_builtin_look_applies_and_renders(self):
        for key in looks.BUILTIN:
            p = Project(xml_path=self.project.xml_path)
            p.settings.measures_per_line = 4
            p.channels.update(auto_camera(self.score, p.settings))
            looks.apply_look(looks.get_look(key), p, self.score)
            self.assertEqual(p.effects.enabled, looks.BUILTIN[key]["enabled"], key)
            if not p.effects.enabled:
                continue
            from sheet_music_animator.export import EffectsRenderer
            from sheet_music_animator.scene import SheetScene
            scene = SheetScene(self.score, p)
            scene.set_cache(False)
            r = EffectsRenderer(scene, p, 320, 180, 30, self.score.duration + 2, None)
            for t in (1.0, 9.5):
                f = r.frame(int(t * 30))
                self.assertEqual((f.shape, f.dtype), ((180, 320, 3), np.uint8), key)
            self.assertGreater(f.mean(), 1.0, key)                  # something was drawn
            r.close()

    def test_a_look_can_be_captured_and_applied_to_another_project(self):
        p = Project(xml_path=self.project.xml_path)
        looks.apply_look(looks.get_look("winter_wind"), p, self.score)
        cap = looks.capture_look(p, self.score, "mine")
        q = Project(xml_path=self.project.xml_path)
        looks.apply_look(json.loads(json.dumps(cap)), q, self.score)
        self.assertEqual([x.type for x in q.effects.layers], [x.type for x in p.effects.layers])
        self.assertEqual(set(q.effects.lanes), set(p.effects.lanes))
        for n in p.effects.lanes:
            self.assertEqual(len(q.channels[LANE + n]), len(p.channels[LANE + n]))
            for a, b in zip(p.channels[LANE + n], q.channels[LANE + n]):
                self.assertAlmostEqual(a.t, b.t, places=2)

    def test_anchors_round_trip(self):
        t = self.score.measure_infos[5].time + 0.37
        a = looks.to_anchor(t, self.score)
        self.assertEqual(a["m"], 6)
        self.assertAlmostEqual(looks.resolve(a, self.score), t, places=3)

    def test_highlight_lights_up_a_struck_note_and_fades(self):
        p = Project(xml_path=self.project.xml_path)
        p.settings.measures_per_line = 4
        p.channels.update(auto_camera(self.score, p.settings))
        p.effects.enabled = True
        p.effects.layers = [new_layer("solid", color="#000000"), new_layer("score", color="#ffffff"),
                            new_layer("highlight", color_mode="single", color_a="#00a0ff", duration=0.5)]
        saved, self.project = self.project, p
        try:
            r = self._renderer(480, 270)
        finally:
            self.project = saved
        t_hit = next(u.time for u in self.score.units if u.heads and u.time > 4.0)
        lit, dark = r.frame(int(t_hit * 30) + 2), r.frame(int(t_hit * 30) + 40)
        blue = lambda f: int(((f[..., 2].astype(int) - f[..., 0].astype(int)) > 90).sum())   # noqa: E731
        self.assertGreater(blue(lit), blue(dark))


if __name__ == "__main__":
    unittest.main()
