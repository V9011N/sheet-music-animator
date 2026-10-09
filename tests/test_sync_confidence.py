"""The confidence of an alignment: a good recording scores high, a wrong or empty one low, and the
stretches that were not found show up in the per-note numbers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np                                    # noqa: E402
from PySide6.QtWidgets import QApplication            # noqa: E402

from sheet_music_animator import analysis, audio      # noqa: E402
from sheet_music_animator.build import build_score    # noqa: E402
from test_effects_alignment import make_project       # noqa: E402

_app = QApplication.instance() or QApplication([])


class TestConfidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.score = build_score(make_project(cls.tmp))
        cls.warp = lambda self, t: 1.3 * t + 0.6 * np.sin(t / 3.0) + 0.7      # noqa: E731

    def align(self, name, notes, post=None):
        y = audio.synthesize(notes, self.warp(self.score.duration))
        if post:
            y = post(y)
        wav = self.tmp / f"{name}.wav"
        audio.write_wav(wav, y)
        return analysis.align_score(self.score.nominal_notes, str(wav))

    def test_a_good_recording_is_trusted(self):
        notes = [(p, self.warp(a), self.warp(b), v) for p, a, b, v in self.score.nominal_notes]
        al = self.align("good", notes)
        self.assertGreater(al.overall, 0.9)
        self.assertEqual(len(al.confidence), len(al.nominal))
        self.assertTrue(((al.confidence >= 0) & (al.confidence <= 1)).all())
        self.assertEqual(len(al.heat()), len(al.nominal))

    def test_noise_instead_of_music_is_not(self):
        rng = np.random.default_rng(3)
        al = self.align("noise", [(60, 0.0, 0.1, 1)], lambda y: (rng.standard_normal(len(y)) * 0.05).astype(np.float32))
        self.assertLess(al.overall, 0.5)

    def test_missing_music_shows_up_locally(self):
        notes = [(p, self.warp(a), self.warp(b), v) for p, a, b, v in self.score.nominal_notes]
        gone = [n for n in notes if not (self.warp(12) < n[1] < self.warp(20))]   # a stretch the performer skipped
        al = self.align("gap", gone)
        t = al.actual
        inside = al.confidence[(t > self.warp(13)) & (t < self.warp(19))]
        outside = al.confidence[(t < self.warp(10)) | (t > self.warp(22))]
        self.assertGreater(len(inside), 3)
        self.assertLess(inside.mean(), outside.mean() - 0.2)
        self.assertLess(al.overall, 0.9)

    def test_very_high_notes_do_not_break_the_judging(self):
        # the harmonics of these notes lie above the range the attack check looks at (this once crashed with
        # "index ... is out of bounds")
        rng = np.random.default_rng(5)
        notes = [(int(rng.integers(86, 102)), 0.5 * i, 0.5 * i + 0.4, 90) for i in range(24)]
        wav = self.tmp / "high.wav"
        audio.write_wav(wav, audio.synthesize(notes, 13.0))
        al = analysis.align_score(notes, str(wav))
        self.assertEqual(len(al.confidence), len(al.nominal))
        self.assertTrue(np.isfinite(al.confidence).all())

    def test_overall_penalises_lost_notes(self):
        good = np.full(40, 0.96)
        lost = good.copy()
        lost[:8] = 0.2
        self.assertGreater(analysis.overall_confidence(good), 0.95)
        self.assertLess(analysis.overall_confidence(lost), analysis.overall_confidence(good) - 0.2)


if __name__ == "__main__":
    unittest.main()
