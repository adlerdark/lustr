"""
Clips: crop maths, ffmpeg commands, and real WebM / MP4 / GIF files cut from a generated test video.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clips                                          # noqa: E402
import database                                       # noqa: E402
import transcoder                                     # noqa: E402

SRC = os.path.join(_DATA, 'test source.mp4')


def setUpModule():
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=1280x720:rate=30:duration=6',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-shortest', SRC], check=True, capture_output=True)


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,codec_name,width,height:format=duration',
                          '-of', 'json', path], capture_output=True, text=True).stdout
    return json.loads(out)


class MathTests(unittest.TestCase):
    def test_crop(self):
        self.assertIsNone(clips.crop_box(1920, 1080, 'original'))
        self.assertEqual(clips.crop_box(1920, 1080, 'square', 0.5), (1080, 1080, 420, 0))
        self.assertEqual(clips.crop_box(1920, 1080, 'square', 0), (1080, 1080, 0, 0))
        w, h, x, y = clips.crop_box(1920, 1080, 'vertical', 1)
        self.assertEqual((w, h, x + w), (608, 1080, 1920))
        self.assertEqual(clips.crop_box(1080, 1920, 'square', 0.5, 0.5), (1080, 1080, 0, 420))

    def test_size(self):
        self.assertEqual(clips.output_size(1920, 1080, 'original', 720), (1280, 720))
        self.assertEqual(clips.output_size(1280, 720, 'original', 1080), (1280, 720))       # never upscaled
        self.assertEqual(clips.output_size(1920, 1080, 'vertical', 720), (608, 1080))       # 9:16 of 1080p: 608 wide, kept
        self.assertEqual(clips.output_size(1920, 1080, 'vertical', 480), (480, 852))
        self.assertEqual(clips.frame_size({'width': 1920, 'height': 1080, 'rotation': 90}), (1080, 1920))

    def test_input_stops_at_clip_end(self):
        # the GIF palette waits for the end of its input: -t must cut the input, not the output
        o = clips.clean_options({'format': 'gif', 'start': 600, 'end': 606}, {'duration': 3000})
        cmd = clips.build_cmd('/x.mp4', '/y.gif', o, {'width': 1280, 'height': 720})
        self.assertLess(cmd.index('-t'), cmd.index('-i'))
        self.assertEqual(cmd[cmd.index('-t') + 1], '6.000')

    def test_options(self):
        info = {'duration': 100, 'audio': True}
        o = clips.clean_options({'format': 'gif', 'start': 10, 'end': 16, 'audio': True}, info)
        self.assertEqual((o['size'], o['fps'], o['audio']), (480, 15, False))               # GIF: small, 15 fps, no sound
        with self.assertRaises(ValueError):
            clips.clean_options({'start': 10, 'end': 80}, info)                              # over 60 s
        with self.assertRaises(ValueError):
            clips.clean_options({'start': 10, 'end': 5}, info)


class EncodeTests(unittest.TestCase):
    def make(self, **o):
        info = transcoder.probe(SRC)
        opts = clips.clean_options({'start': 1, 'end': 3, **o}, info)
        cid = 't' + opts['format'] + opts['shape']
        name, size, w, h = clips.encode(cid, SRC, opts)
        p = probe(os.path.join(clips.CLIPS_DIR, name))
        self.assertTrue(os.path.exists(os.path.join(clips.CLIPS_DIR, cid + '.jpg')))
        return p, (w, h), size

    def test_webm_with_sound(self):
        p, wh, size = self.make(format='webm', size=360, audio=True)
        kinds = {s['codec_type']: s for s in p['streams']}
        self.assertEqual(kinds['video']['codec_name'], 'vp9')
        self.assertEqual(kinds['audio']['codec_name'], 'opus')
        self.assertEqual((kinds['video']['width'], kinds['video']['height']), wh)
        self.assertAlmostEqual(float(p['format']['duration']), 2, delta=0.3)

    def test_mp4_square_silent(self):
        p, wh, size = self.make(format='mp4', shape='square', size=360, audio=False)
        self.assertEqual([s['codec_type'] for s in p['streams']], ['video'])
        self.assertEqual((p['streams'][0]['width'], p['streams'][0]['height']), (360, 360))

    def test_gif(self):
        p, wh, size = self.make(format='gif', shape='vertical', size=240)
        self.assertEqual(p['streams'][0]['codec_name'], 'gif')
        self.assertEqual((p['streams'][0]['width'], p['streams'][0]['height']), wh)
        clips.delete_files('tgifvertical')
        self.assertFalse(any(n.startswith('tgifvertical.') for n in os.listdir(clips.CLIPS_DIR)))


class ApiTests(unittest.TestCase):
    def setUp(self):
        import routes.clips as rc
        self.rc = rc
        db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        rc._model = types.SimpleNamespace(db=db, library_groups={}, get_libraries_in_group=lambda g: [])
        c = db.get_connection()
        c.execute("INSERT INTO videos (video_id, filename_base, library, title) VALUES ('v1', 'v1', 'Lib', 'Test Scene')")
        c.execute('INSERT INTO file_versions (path, video_id, filename) VALUES (?, ?, ?)', (SRC, 'v1', 'test source.mp4'))
        c.commit()
        rc.ensure_table(c)
        clips.worker.load, clips.worker.save = rc._load, rc._save

    def test_make_list_delete(self):
        from unittest import mock
        with mock.patch.object(clips.worker, 'enqueue'):         # run the job here, not in the background thread
            r = self.rc.make(self.rc.ClipBody(path=SRC, start=0.5, end=2, format='webm', size=240, title='Bit'))
        self.assertTrue(r['success'], r)
        cid = r['clip']['id']
        src, o = self.rc._load(cid)
        name, size, w, h = clips.encode(cid, src, o)
        self.rc._save(cid, status='done', file=name, size_bytes=size, width=w, height=h)
        lst = self.rc.list_clips(video_id='v1', model=self.rc._model)
        self.assertEqual(lst['total'], 1)
        self.assertEqual((lst['clips'][0]['title'], lst['clips'][0]['video_title']), ('Bit', 'Test Scene'))
        self.assertTrue(lst['clips'][0]['src'].startswith('/clips/'))
        self.assertEqual(self.rc.delete(cid)['deleted'], 1)
        self.assertFalse(os.path.exists(os.path.join(clips.CLIPS_DIR, name)))


if __name__ == '__main__':
    unittest.main()
