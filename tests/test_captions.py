"""
Closed captions: existing subtitles, the cleanup of Whisper's invented text, the queue, settings.
Whisper itself is replaced by a fake here.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import captions                                       # noqa: E402
import database                                       # noqa: E402
import transcoder                                     # noqa: E402

SRT = '1\n00:00:01,000 --> 00:00:02,500\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond line\n'


class TextTests(unittest.TestCase):
    def test_srt_to_vtt(self):
        v = captions.srt_to_vtt('﻿' + SRT)
        self.assertTrue(v.startswith('WEBVTT'))
        self.assertIn('00:00:01.000 --> 00:00:02.500\nHello there', v)
        self.assertNotIn('\n1\n', v)

    def test_clean_segments(self):
        segs = [(0, 2, 'Hi, how are you?'), (3, 4, '...'), (5, 6, 'Thank you for watching!'),
                (7, 8, 'Oh yes'), (9, 10, 'oh yes'), (11, 12, 'Oh yes.'), (13, 14, 'Oh yes'),
                (20, 30, 'Mm'), (31, 32, 'Come here')]
        self.assertEqual([s[2] for s in captions.clean_segments(segs)], ['Hi, how are you?', 'Come here'])

    def test_write_vtt(self):
        out = os.path.join(tempfile.mkdtemp(dir=_DATA), 'x.vtt')
        captions.write_vtt([(1.5, 3, 'Hi')], out)
        with open(out, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), 'WEBVTT\n\n00:00:01.500 --> 00:00:03.000\nHi\n')


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp(dir=_DATA)
        self.video = os.path.join(self.media, 'Scene One.mp4')
        open(self.video, 'wb').close()
        self.cid = 'cid' + str(time.time_ns())

    def test_sidecars_used(self):
        for name in ('Scene One.srt', 'Scene One.en.srt', 'Scene One extra.srt', 'Other.srt'):
            with open(os.path.join(self.media, name), 'w', encoding='utf-8') as fh:
                fh.write(SRT)
        self.assertEqual([lang for _, lang in captions.sidecars(self.video)], ['en', 'und'])
        with mock.patch.object(captions, 'embedded', return_value=[]):
            self.assertEqual(captions.use_existing(self.video, self.cid), 2)
        tr = captions.tracks(self.cid)
        self.assertEqual(sorted(t['lang'] for t in tr), ['en', 'und'])
        self.assertTrue(all(t['src'].startswith(f'/cache/{self.cid}/captions.') for t in tr))
        self.assertTrue(captions.has_captions(self.cid))
        self.assertEqual(captions.remove(self.cid), 2)
        self.assertFalse(captions.has_captions(self.cid))

    def test_process_falls_back_to_whisper(self):
        def fake(path, cid, settings, progress=None, stop=None):
            captions.write_vtt([(0, 1, 'hi')], os.path.join(captions.folder(cid), 'captions.en.whisper.vtt'))
            captions._add_track(cid, 'captions.en.whisper.vtt', 'en', 'whisper', settings['model'])
            return 'en', 1
        os.makedirs(captions.folder(self.cid), exist_ok=True)
        with mock.patch.object(captions, 'embedded', return_value=[]), \
                mock.patch.object(captions, 'whisper_available', return_value=True), \
                mock.patch.object(captions, 'transcribe', side_effect=fake):
            self.assertEqual(captions.process(self.video, self.cid, dict(captions.DEFAULTS)), 'whisper')
        self.assertEqual(captions.tracks(self.cid)[0]['model'], 'small')

    def test_no_whisper_no_subs(self):
        with mock.patch.object(captions, 'embedded', return_value=[]), \
                mock.patch.object(captions, 'whisper_available', return_value=False):
            with self.assertRaises(RuntimeError):
                captions.process(self.video, self.cid, dict(captions.DEFAULTS))


class QueueTests(unittest.TestCase):
    def test_front_and_stop(self):
        w = captions.Worker()
        w._start = lambda: None                      # no thread: just the queue
        w.enqueue('/a', 'a')
        w.enqueue('/b', 'b')
        w.enqueue('/c', 'c', front=True)
        w.enqueue('/a', 'a', front=True)             # already queued: moves up
        self.assertEqual(w.queued_ids(), ['a', 'c', 'b'])
        self.assertEqual(w.stop(), 3)
        self.assertEqual(w.queued_ids(), [])

    def test_bulk_skips_done(self):
        w = captions.Worker()
        w._start = lambda: None
        with mock.patch.object(captions, 'has_captions', side_effect=lambda cid: cid == 'done'):
            self.assertEqual(w.start_bulk([('/x', 'done'), ('/y', 'todo')]), 1)
        self.assertEqual(w.status()['bulk_total'], 1)

    def test_night_window(self):
        self.assertTrue(captions.in_window(1, 7, 3))
        self.assertFalse(captions.in_window(1, 7, 8))
        self.assertTrue(captions.in_window(22, 6, 23))
        self.assertTrue(captions.in_window(22, 6, 2))
        self.assertFalse(captions.in_window(22, 6, 12))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_save(self):
        db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        c = db.get_connection()
        self.assertEqual(captions.get_settings(c)['model'], 'small')
        s = captions.save_settings(c, {'model': 'large-v3-turbo', 'cpu_threads': 999, 'language': 'EN',
                                       'night': {'enabled': True, 'start': 23, 'libraries': ['Lib']}, 'bogus': 1})
        self.assertEqual((s['model'], s['language'], s['night']['start'], s['night']['end']), ('large-v3-turbo', 'en', 23, 7))
        self.assertLessEqual(s['cpu_threads'], os.cpu_count())
        self.assertEqual(captions.get_settings(c)['night']['libraries'], ['Lib'])
        self.assertEqual(captions.save_settings(c, {'model': 'huge'})['model'], 'large-v3-turbo')     # unknown ignored


if __name__ == '__main__':
    unittest.main()
