"""
HLS options for TV apps: surround / copied audio and the video bitrate (transcoder.py, routes/player.py).

    docker exec -w /app lustr python3 -m unittest tests.test_hls_options -v
"""
import os
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transcoder                                     # noqa: E402
import routes.player as player                        # noqa: E402


def info(**kw):
    d = {'duration': 600.0, 'width': 1920, 'height': 1080, 'codec': 'h264', 'audio': True,
         'audio_channels': 2, 'audio_codec': 'aac', 'bit_rate': 0, 'rotation': 0}
    d.update(kw)
    return d


def audio_of(args):
    i = args.index('-c:a') if '-c:a' in args else None
    return args[i:i + 6] if i is not None else ['-an'] if '-an' in args else []


class AudioTests(unittest.TestCase):
    def args(self, inf, audio):
        return transcoder.build_args('/m/a.mkv', inf, '720', 0, 'software', '/tmp/x', audio=audio)

    def test_stereo_is_the_default(self):
        a = transcoder.build_args('/m/a.mkv', info(audio_channels=6), '720', 0, 'software', '/tmp/x')
        self.assertEqual(audio_of(a)[:4], ['-c:a', 'aac', '-ac', '2'])

    def test_surround_needs_six_channels(self):
        self.assertEqual(audio_of(self.args(info(audio_channels=6), 'surround'))[:6], ['-c:a', 'aac', '-ac', '6', '-b:a', '384k'])
        self.assertIn('2', audio_of(self.args(info(audio_channels=2), 'surround')))

    def test_copy(self):
        self.assertEqual(audio_of(self.args(info(audio_codec='eac3', audio_channels=6), 'copy'))[:2], ['-c:a', 'copy'])
        a = audio_of(self.args(info(audio_codec='dts', audio_channels=6), 'copy'))    # TS can't carry it: 5.1 AAC
        self.assertEqual(a[:4], ['-c:a', 'aac', '-ac', '6'])

    def test_no_audio(self):
        self.assertIn('-an', self.args(info(audio=False), 'surround'))


class BitrateTests(unittest.TestCase):
    def test_original_by_height(self):
        self.assertEqual(transcoder.video_rate('original', info(width=3840, height=2160)), 25000)
        self.assertEqual(transcoder.video_rate('original', info(width=2560, height=1440)), 16000)
        self.assertEqual(transcoder.video_rate('original', info()), 8000)
        self.assertEqual(transcoder.video_rate('original', info(width=2160, height=3840)), 25000)    # portrait 4K

    def test_original_capped_at_the_source(self):
        self.assertEqual(transcoder.video_rate('original', info(width=3840, height=2160, bit_rate=12000)), 12000)
        self.assertEqual(transcoder.video_rate('original', info(width=3840, height=2160, bit_rate=3000)), 8000)

    def test_max_kbps(self):
        self.assertEqual(transcoder.video_rate('1080', info(), 4000), 4000)
        self.assertEqual(transcoder.video_rate('480', info(), 4000), 2000)                # a cap, never a raise
        self.assertEqual(transcoder.video_rate('720', info(), 50), transcoder.MIN_K)
        a = transcoder.build_args('/m/a.mkv', info(), '1080', 0, 'hwupload', '/tmp/x', max_kbps=4000)
        self.assertEqual(a[a.index('-b:v') + 1], '4000k')
        self.assertEqual(a[a.index('-maxrate') + 1], '6000k')

    def test_other_resolutions_unchanged(self):
        for res, k in transcoder.BITRATE_K.items():
            if res != 'original':
                self.assertEqual(transcoder.video_rate(res, info(width=3840, height=2160)), k)


class UrlTests(unittest.TestCase):
    def test_defaults_stay_out_of_the_url(self):
        self.assertNotIn('audio=', player.hls_url('/m/a b.mkv', '720'))
        self.assertNotIn('audio=', player.hls_url('/m/a.mkv', '720', '3.ts', 'stereo'))
        u = player.hls_url('/m/a.mkv', '720', '3.ts', 'surround', 4000)
        self.assertTrue(u.startswith('/api/player/hls/720/3.ts?path=%2Fm%2Fa.mkv'))
        self.assertIn('&audio=surround', u)
        self.assertIn('&max_kbps=4000', u)

    def test_options_checked(self):
        self.assertEqual(player._hls_opts('copy', None), ('copy', None))
        self.assertEqual(player._hls_opts('stereo', 10), ('stereo', transcoder.MIN_K))
        with self.assertRaises(ValueError):
            player._hls_opts('7.1', None)
        with self.assertRaises(ValueError):
            player._hls_opts('stereo', -5)

    def test_options_make_separate_streams(self):
        m = transcoder.SegmentManager(root=tempfile.mkdtemp(dir=_DATA))
        m._launch = lambda st, n: setattr(st, 'error', 'not running ffmpeg in tests')
        orig = transcoder.probe
        transcoder.probe = lambda p: info()
        try:
            for audio, cap in (('stereo', None), ('surround', None), ('stereo', 4000)):
                with self.assertRaises(transcoder.SegmentError):
                    m.segment('/m/a.mkv', '720', 0, hw=False, wait=0.5, audio=audio, max_kbps=cap)
        finally:
            transcoder.probe = orig
        self.assertEqual(len(m.streams), 3)
        self.assertEqual(len({st.dir for st in m.streams.values()}), 3)


if __name__ == '__main__':
    unittest.main()
