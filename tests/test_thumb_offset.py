"""
Force New thumbnails: the offset moves where the frames are taken. Positive skips the start,
negative moves every frame earlier (at most until the first one is at 0:00).

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_handler import MediaHandler as M           # noqa: E402


class ThumbOffsetTests(unittest.TestCase):
    def test_default_and_positive(self):
        times, start = M.thumb_times(2400, 8, 0)
        self.assertEqual(start, 0)
        self.assertAlmostEqual(times[0], 2400 / 9)
        times, start = M.thumb_times(2400, 8, 120)
        self.assertGreater(times[0], 2400 / 9)

    def test_negative_moves_frames_earlier(self):
        times, start = M.thumb_times(2400, 8, -100)
        self.assertEqual(start, -100)
        self.assertLess(times[0], 2400 / 9)
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))

    def test_negative_stops_at_zero(self):
        times, start = M.thumb_times(2400, 8, -10000)
        self.assertAlmostEqual(start, -300)                 # -length / frames: first frame at 0:00
        self.assertAlmostEqual(times[0], 0)
        self.assertTrue(all(t >= 0 for t in times))

    def test_too_far_forward_is_capped(self):
        times, start = M.thumb_times(2400, 8, 5000)
        self.assertAlmostEqual(start, 2160)
        self.assertTrue(all(t <= 2399.5 for t in times))

    def test_anchor_never_before_zero(self):
        cur = {'start': -300, 'times': M.thumb_times(2400, 8, -300)[0]}
        times, k = M.anchor_times(2400, 8, 1000, cur)
        self.assertEqual(times[k], 1000)
        self.assertTrue(all(t >= 0 for t in times))


if __name__ == '__main__':
    unittest.main()
