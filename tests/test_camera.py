"""The camera's position is two channels, x and y: following the music lays down x, and y only moves from
line to line and is left alone once set by hand."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication            # noqa: E402

from sheet_music_animator.build import build_score    # noqa: E402
from sheet_music_animator.layout import relayout_project   # noqa: E402
from sheet_music_animator.project import Project, auto_camera   # noqa: E402
from test_effects_alignment import make_project       # noqa: E402

_app = QApplication.instance() or QApplication([])


class TestCameraChannels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = make_project(Path(tempfile.mkdtemp()))     # 12 measures, 4 to a line
        cls.score = build_score(cls.project)

    def fresh(self) -> Project:
        p = Project(xml_path=self.project.xml_path)
        p.settings.measures_per_line = 4
        p.follow_music(self.score)
        return p

    def test_x_follows_the_music_and_y_only_changes_line(self):
        cam = auto_camera(self.score, self.project.settings)
        self.assertGreater(len(cam["x"]), len(self.score.systems) * 2)       # keys along every line
        self.assertLessEqual(len(cam["y"]), 2 * len(self.score.systems))     # arriving at and leaving each line
        heights = sorted({round(k.v[0]) for k in cam["y"]})
        self.assertEqual(len(heights), len(self.score.systems))

    def test_moving_the_camera_sideways_keys_x_only(self):
        p = self.fresh()
        cx, cy, w, _ = p.camera_at(3.0)
        n_y = len(p.channels["y"])
        keys = p.set_camera(3.0, cx + 500, cy, w)
        self.assertEqual([k for k in keys if k in p.channels["y"]], [])
        self.assertEqual(len(p.channels["y"]), n_y)
        self.assertTrue(p.keys_edited)
        self.assertFalse(p.y_edited)

    def test_following_the_music_again_keeps_a_y_set_by_hand(self):
        p = self.fresh()
        cx, cy, w, _ = p.camera_at(3.0)
        p.set_camera(3.0, cx, cy + 700, w)
        self.assertTrue(p.y_edited)
        self.assertFalse(p.keys_edited)
        y_keys = [(k.t, k.v[0]) for k in p.channels["y"]]
        p.settings.follow_lead = 0.4                     # the lead changes: x is laid down again
        x_before = [k.v[0] for k in p.channels["x"]]
        p.follow_music(self.score)
        self.assertEqual([(k.t, k.v[0]) for k in p.channels["y"]], y_keys)
        self.assertNotEqual([k.v[0] for k in p.channels["x"]], x_before)
        self.assertAlmostEqual(p.camera_at(3.0)[1], cy + 700, places=3)

    def test_projects_saved_with_one_position_channel_open_with_x_and_y(self):
        p = Project()
        p.load_dict({"version": 4, "keys_edited": True,
                     "channels": {"pos": [{"t": 1.0, "v": [100.0, 200.0], "ease": "linear"}],
                                  "size": [{"t": 1.0, "v": [5000.0]}], "rot": []}})
        self.assertEqual([(k.t, k.v, k.ease) for k in p.channels["x"]], [(1.0, [100.0], "linear")])
        self.assertEqual([(k.t, k.v) for k in p.channels["y"]], [(1.0, [200.0])])
        self.assertTrue(p.y_edited)                      # a path edited by hand keeps its height
        self.assertEqual(p.camera_at(1.0)[:3], (100.0, 200.0, 5000.0))
        q = Project()
        q.load_dict(p.to_dict())
        self.assertEqual(q.camera_at(1.0)[:3], (100.0, 200.0, 5000.0))

    def test_new_line_breaks_move_a_y_set_by_hand_with_its_measure(self):
        p = self.fresh()
        t = self.score.measure_infos[3].time + 0.1           # measure 4: the end of the first line
        cx, cy, w, _ = p.camera_at(t)
        p.set_camera(t, cx, cy + 300, w)
        p.line_starts = [0, 3, 6, 9]                         # three measures to a line: measure 4 starts line 2
        new = build_score(p)
        offset = cy + 300 - self.score.measure_infos[3].rect[1]
        relayout_project(p, self.score, new)
        key = next(k for k in p.channels["y"] if abs(k.t - t) < 1e-6)
        self.assertGreater(new.measure_infos[3].rect[1], self.score.measure_infos[3].rect[1] + 1000)
        self.assertAlmostEqual(key.v[0], new.measure_infos[3].rect[1] + offset, places=3)
        self.assertTrue(p.y_edited)


if __name__ == "__main__":
    unittest.main()
