"""
Which tab a file shows in (media_kind.py): a .webm is in Images, a video file can be switched either way,
and a library can hold images only or videos only. Also a photo as a cast photo (images.cast_photo_bytes).

    docker exec -w /app lustr python3 -m unittest tests.test_media_kind -v
"""
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import images                                         # noqa: E402
import media_kind                                     # noqa: E402
import model                                          # noqa: E402


def _video(path, ext_args=('-c:v', 'libvpx-vp9', '-b:v', '200k')):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=10:duration=2',
                    *ext_args, path], check=True, capture_output=True)
    t = os.path.getmtime(path) - 3600                 # older than WRITING_SECONDS: finished
    os.utime(path, (t, t))


def _photo(path, size=(3000, 2000)):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', size, (200, 100, 50)).save(path, 'JPEG')


class Kinds(unittest.TestCase):
    def test_defaults_and_choices(self):
        self.assertEqual(media_kind.kind_of('/m/a.webm'), 'image')
        self.assertEqual(media_kind.kind_of('/m/a.mp4'), 'video')
        self.assertIsNone(media_kind.kind_of('/m/a.jpg'))
        self.assertEqual(media_kind.kind_of('/m/a.webm', {'/m/a.webm': 'video'}), 'video')
        self.assertEqual(media_kind.kind_of('/m/a.mp4', {'/m/a.mp4': 'image'}), 'image')
        self.assertEqual(media_kind.kind_of('/m/a.mp4', {}, 'images'), 'image')
        self.assertEqual(media_kind.kind_of('/m/a.webm', {'/m/a.webm': 'image'}, 'videos'), 'video')


class Scans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='lustr-lib-')
        _video(os.path.join(cls.root, 'v', 'loop.webm'))
        _video(os.path.join(cls.root, 'v', 'scene.mp4'), ('-c:v', 'libx264', '-pix_fmt', 'yuv420p'))
        _photo(os.path.join(cls.root, 'v', 'still.jpg'), (400, 300))

    def setUp(self):
        self.m = model.LibraryModel()
        self.m.libraries = {'Lib': [self.root]}
        self.m.library_settings = {}
        self.m.db.save_libraries(self.m.libraries)
        self.m.fill_missing_metadata = lambda paths: None
        self.c = self.m.db.get_connection()
        for t in ('file_versions', 'videos', 'images', 'media_kind', 'file_changes'):
            try:
                self.c.execute(f'DELETE FROM {t}')
            except Exception:
                pass
        self.c.commit()

    def _videos(self):
        return sorted(os.path.basename(r[0]) for r in self.c.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL'))

    def _images(self):
        return sorted((os.path.basename(r[0]), r[1]) for r in self.c.execute('SELECT path, kind FROM images WHERE deleted_at IS NULL'))

    def test_webm_is_in_images_and_mp4_is_a_video(self):
        self.m.scan_library(fast=True, library='Lib')
        self.assertEqual(self._videos(), ['scene.mp4'])
        self.assertEqual(self._images(), [('loop.webm', 'video'), ('still.jpg', None)])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM file_changes WHERE flag_type != ?', ('new',)).fetchone()[0], 0)

    def test_switching_round_trip(self):
        self.m.scan_library(fast=True, library='Lib')
        webm = os.path.join(self.root, 'v', 'loop.webm')
        mp4 = os.path.join(self.root, 'v', 'scene.mp4')
        media_kind.set_kind(self.c, [mp4], 'image')
        media_kind.to_images(self.m, [mp4])
        self.assertEqual(self._videos(), [])
        self.assertIn(('scene.mp4', 'video'), self._images())
        media_kind.set_kind(self.c, [webm], 'video')
        media_kind.to_videos(self.m, [webm])
        self.assertEqual(self._videos(), ['loop.webm'])
        self.m.scan_library(fast=True, library='Lib')               # the next scan keeps both where you put them
        self.assertEqual(self._videos(), ['loop.webm'])
        self.assertEqual(self._images(), [('scene.mp4', 'video'), ('still.jpg', None)])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM file_changes WHERE flag_type = 'deleted'").fetchone()[0], 0)

    def test_migration_keeps_a_webm_with_metadata(self):
        webm = os.path.join(self.root, 'v', 'loop.webm')
        self.m.db.create_video('vw', 'loop', 'Lib', {'favorite': 1})
        self.m.db.add_file_version('vw', webm, 'loop.webm', {'length': '0:02'})
        self.assertEqual(media_kind.migrate(self.m), (0, 1))
        self.assertEqual(media_kind.overrides(self.c), {webm: 'video'})
        self.c.execute('UPDATE videos SET favorite = 0')
        self.c.execute('DELETE FROM media_kind')
        self.c.commit()
        self.assertEqual(media_kind.migrate(self.m), (1, 0))
        self.assertEqual(self._videos(), [])
        self.assertIn(('loop.webm', 'video'), self._images())

    def test_an_images_only_library(self):
        self.m.library_settings = {'Lib': {'content': 'images'}}
        self.m.scan_library(fast=True, library='Lib')
        self.assertEqual(self._videos(), [])
        self.assertEqual(self._images(), [('loop.webm', 'video'), ('scene.mp4', 'video'), ('still.jpg', None)])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM file_changes').fetchone()[0], 0)

    def test_a_videos_only_library(self):
        self.m.library_settings = {'Lib': {'content': 'videos'}}
        self.m.scan_library(fast=True, library='Lib')
        self.assertEqual(self._videos(), ['loop.webm', 'scene.mp4'])
        self.assertEqual(self._images(), [])

    def test_a_moving_one_has_a_thumbnail_and_size(self):
        self.m.scan_library(fast=True, library='Lib')
        i = self.c.execute("SELECT id FROM images WHERE kind = 'video'").fetchone()[0]
        self.assertTrue(images.thumb(self.c, i, _DATA))
        self.assertEqual(tuple(self.c.execute('SELECT width, height FROM images WHERE id = ?', (i,)).fetchone()), (320, 180))
        self.assertIsNone(images.cast_photo_bytes(self.c, i))         # not a still


class CastPhoto(unittest.TestCase):
    def test_a_big_photo_and_one_in_a_zip(self):
        from PIL import Image
        import io
        root = tempfile.mkdtemp(prefix='lustr-lib-')
        _photo(os.path.join(root, 'a', 'big.jpg'))
        z = os.path.join(root, 'a', 'set.zip')
        with zipfile.ZipFile(z, 'w') as f:
            f.write(os.path.join(root, 'a', 'big.jpg'), 'inside.jpg')
        m = model.LibraryModel()
        m.libraries, m.library_settings = {'P': [root]}, {}
        m.fill_missing_metadata = lambda paths: None
        m.scan_library(fast=True, library='P')
        c = m.db.get_connection()
        for (i,) in c.execute("SELECT id FROM images WHERE path LIKE ?", (root + '%',)).fetchall():
            data = images.cast_photo_bytes(c, i)
            with Image.open(io.BytesIO(data)) as im:
                self.assertEqual(im.size, (2000, 1333))


if __name__ == '__main__':
    unittest.main()
