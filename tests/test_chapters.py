"""
Chapters in video files -> markers: once per file, never undoing your edits, never duplicating.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chapters                                       # noqa: E402
import database                                       # noqa: E402

META = """;FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=4000
title=Intro
[CHAPTER]
TIMEBASE=1/1000
START=4000
END=8000
title=Kissing
[CHAPTER]
TIMEBASE=1/1000
START=8000
END=12000
"""


def make_video(path, meta=META):
    mfile = path + '.txt'
    with open(mfile, 'w') as fh:
        fh.write(meta)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=10:duration=12',
                    '-i', mfile, '-map_metadata', '1', '-map_chapters', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', path],
                   check=True, capture_output=True)


class ChapterTests(unittest.TestCase):
    def setUp(self):
        import routes.player as rp
        self.rp = rp
        self.db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        rp._model = types.SimpleNamespace(db=self.db)
        rp._markers_ready = False
        self.c = self.db.get_connection()
        rp.ensure_markers(self.c)
        self.path = os.path.join(tempfile.mkdtemp(dir=_DATA), 'scene.mp4')
        make_video(self.path)
        self.c.execute("INSERT INTO videos (video_id, filename_base, library) VALUES ('v1', 'scene', 'Lib')")
        self.c.execute("INSERT INTO file_versions (path, video_id, filename) VALUES (?, 'v1', 'scene.mp4')", (self.path,))
        self.c.commit()

    def markers(self):
        return [(m['seconds'], m['title'], m['source']) for m in self.rp.list_markers(self.c, 'v1')]

    def test_read(self):
        self.assertEqual(chapters.read(self.path), [(0.0, 'Intro'), (4.0, 'Kissing'), (8.0, 'Chapter 3')])

    def test_import_once(self):
        self.assertEqual(chapters.import_file(self.c, self.path), 3)
        self.assertEqual(self.markers(), [(0.0, 'Intro', 'chapter'), (4.0, 'Kissing', 'chapter'), (8.0, 'Chapter 3', 'chapter')])
        self.assertTrue(chapters.checked(self.c, self.path))
        self.assertEqual(chapters.import_file(self.c, self.path), 0)               # checked: nothing again

    def test_renamed_moved_and_deleted_stay(self):
        chapters.import_file(self.c, self.path)
        ms = self.rp.list_markers(self.c, 'v1')
        k = next(m for m in ms if m['title'] == 'Kissing')
        self.rp.save_marker(self.c, self.rp.MarkerBody(id=k['id'], video_id='v1', seconds=5.5, title='First kiss'))   # rename + move
        intro = next(m for m in ms if m['title'] == 'Intro')
        self.rp.markers_delete(self.rp.MarkerDeleteBody(id=intro['id']))
        self.assertEqual(chapters.import_file(self.c, self.path, force=True), 0)    # forced re-check
        self.assertEqual(self.markers(), [(5.5, 'First kiss', 'chapter'), (8.0, 'Chapter 3', 'chapter')])

    def test_own_marker_nearby_blocks(self):
        self.rp.save_marker(self.c, self.rp.MarkerBody(video_id='v1', seconds=4.8, title='Mine'))
        self.assertEqual(chapters.import_file(self.c, self.path), 2)
        self.assertEqual([t for _, t, _ in self.markers()], ['Intro', 'Mine', 'Chapter 3'])
        self.assertEqual(self.markers()[1][2], 'user')

    def test_changed_file_rechecked_without_duplicates(self):
        chapters.import_file(self.c, self.path)
        os.utime(self.path, (time.time() + 100, time.time() + 100))
        self.assertFalse(chapters.checked(self.c, self.path))
        self.assertEqual(chapters.import_file(self.c, self.path), 0)
        self.assertEqual(len(self.markers()), 3)

    def test_single_chapter_is_nothing(self):
        p = os.path.join(os.path.dirname(self.path), 'one.mp4')
        make_video(p, ';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=12000\ntitle=All\n')
        self.assertEqual(chapters.read(p), [])


if __name__ == '__main__':
    unittest.main()
