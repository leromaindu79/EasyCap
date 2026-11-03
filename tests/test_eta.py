"""Tests déterministes du compte à rebours global EasyCap v1.5."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_eta import RemainingTimeEstimator


class TestRemainingTimeEstimator(unittest.TestCase):
    def test_waits_for_real_progress_before_forecast(self):
        eta = RemainingTimeEstimator(100., with_vo=True)
        self.assertIsNone(eta.seconds_left(105.))
        eta.update(.20, 'Cappella_Video.mp4 — passe 1/2', 105.)
        self.assertIsNone(eta.seconds_left(105.))
        eta.update(.25, 'Cappella_Video.mp4 — passe 2/2', 112.)
        self.assertGreater(eta.seconds_left(112.), 0)

    def test_counts_down_without_more_events(self):
        eta = RemainingTimeEstimator(0., with_vo=True)
        eta.update(.22, 'Cappella_Video.mp4 — passe 1/2', 25.)
        before = eta.seconds_left(25.)
        self.assertIsNotNone(before)
        self.assertEqual(eta.seconds_left(28.), before - 3)

    def test_audio_progress_does_not_mean_entire_job_almost_finished(self):
        eta = RemainingTimeEstimator(0., with_vo=True)
        eta.update(.77, 'Cappella_Sans_Voix.mp3', 60.)
        # La barre de progression brute est à 77%, mais les deux passes
        # vidéo x264 n'ont pas commencé : le travail total reste important.
        self.assertLess(eta._weighted_fraction(.77), .5)
        self.assertGreater(eta.seconds_left(60.), 50)

    def test_without_vo_near_end_after_audio(self):
        eta = RemainingTimeEstimator(0., with_vo=False)
        eta.update(.77, 'Cappella_Sans_Voix.mp3', 60.)
        self.assertGreater(eta._weighted_fraction(.77), .95)
        self.assertGreaterEqual(eta.seconds_left(60.), 1)

    def test_detects_real_ffv1_cache(self):
        eta = RemainingTimeEstimator(0., with_vo=True)
        eta.update(.10, 'Préparation vidéo partagée (cache FFV1)', 12.)
        self.assertIs(eta.cache_active, True)
        self.assertGreater(eta._weighted_fraction(.10), .02)
        no_cache = RemainingTimeEstimator(0., with_vo=True)
        no_cache.update(.1, 'Cappella_Video.mp4 — passe 1/2', 12.)
        self.assertIs(no_cache.cache_active, False)

    def test_updates_are_smooth(self):
        eta = RemainingTimeEstimator(0., with_vo=True)
        eta.update(.12, 'Cappella_Video.mp4 — passe 1/2', 12.)
        prior = eta.finish_at
        eta.update(.7, 'Cappella_Sans_Voix.mp3', 13.)
        self.assertLess(abs(eta.finish_at-prior), 10.0)
        eta.update(.83, 'Scène_VOSTFR.mp4 — passe 1/2', 21.)
        self.assertIsNotNone(eta.seconds_left(21.))

    def test_no_zero_before_confirmation(self):
        eta = RemainingTimeEstimator(0., with_vo=True)
        eta.update(.5, 'Cappella_Voix.mp3', 20.)
        self.assertGreaterEqual(eta.seconds_left(400.), 1)
        self.assertTrue(eta.needs_recalculation(400.))
        eta.update(1., 'Terminé', 402.)
        self.assertEqual(eta.seconds_left(402.), 0)
        self.assertFalse(eta.needs_recalculation(402.))

    def test_sanitizes_progress_and_ignores_rewinding(self):
        eta = RemainingTimeEstimator(0., with_vo=False)
        eta.update(float('nan'), 'x', 30.)
        self.assertEqual(eta.fraction, 0.)
        eta.update(.4, 'Cappella_Video.mp4 — passe 1/2', 30.)
        first = eta.fraction
        eta.update(.1, 'Cappella_Video.mp4 — passe 1/2', 32.)
        self.assertEqual(eta.fraction, first)
        eta.update(2., 'Terminé', 33.)
        self.assertEqual(eta.fraction, 1.)
        self.assertEqual(eta.seconds_left(33.), 0)


if __name__ == '__main__':
    unittest.main()
