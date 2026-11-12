"""
Rescanning one of a library's folders on its own (library settings window): only that folder is
walked - its new files are added, its images synced - and files in the library's other folders are
left alone (not added, not marked missing). The route refuses a folder that isn't one of the library's.

    docker exec -w /app lustr python3 -m unittest tests.test_scan_folder -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model                                          # noqa: E402


def _video(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=10:duration=2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', path], check=True, capture_output=True)
    t = os.path.getmtime(path) - 3600                 # older than WRITING_SECONDS: finished
    os.utime(path, (t, t))


def _photo(path):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (400, 300), (200, 100, 50)).save(path, 'JPEG')


class ScanFolder(unittest.TestCase):
    def setUp(self):
        self.a = tempfile.mkdtemp(prefix='lustr-a-')
        self.b = tempfile.mkdtemp(prefix='lustr-b-')
        self.m = model.LibraryModel()
        self.m.libraries = {'Lib': [self.a, self.b]}
        self.m.db.save_libraries(self.m.libraries)
        self.m.fill_missing_metadata = lambda paths: None
        self.c = self.m.db.get_connection()
        self.addCleanup(self.c.rollback)                 # a failed test never leaves the database locked
        for t in ('file_versions', 'videos', 'images', 'media_kind', 'file_changes'):
            try:
                self.c.execute(f'DELETE FROM {t}')
            except Exception:
                pass
        self.c.commit()

    def tearDown(self):
        shutil.rmtree(self.a, ignore_errors=True)
        shutil.rmtree(self.b, ignore_errors=True)

    def _videos(self):
        return sorted(os.path.basename(r[0]) for r in self.c.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL'))

    def _images(self):
        return sorted(os.path.basename(r[0]) for r in self.c.execute('SELECT path FROM images WHERE deleted_at IS NULL'))

    def test_only_that_folder_is_scanned(self):
        _video(os.path.join(self.a, 'one.mp4'))
        _video(os.path.join(self.b, 'two.mp4'))
        _photo(os.path.join(self.a, 'a.jpg'))
        _photo(os.path.join(self.b, 'b.jpg'))
        new, _ = self.m.scan_library(fast=True, library='Lib', folder=self.a)
        self.assertEqual([os.path.basename(p) for p in new], ['one.mp4'])
        self.assertEqual(self._videos(), ['one.mp4'])
        self.assertEqual(self._images(), ['a.jpg'])
        self.m.scan_library(fast=True, library='Lib', folder=self.b)
        self.assertEqual(self._videos(), ['one.mp4', 'two.mp4'])
        self.assertEqual(self._images(), ['a.jpg', 'b.jpg'])

    def test_other_folders_files_are_not_marked_missing(self):
        _video(os.path.join(self.a, 'one.mp4'))
        _video(os.path.join(self.b, 'two.mp4'))
        self.m.scan_library(fast=True, library='Lib')
        os.remove(os.path.join(self.b, 'two.mp4'))
        self.m.scan_library(fast=True, library='Lib', folder=self.a)
        self.assertEqual(self._videos(), ['one.mp4', 'two.mp4'])
        self.m.scan_library(fast=True, library='Lib', folder=self.b)
        self.assertEqual(self._videos(), ['one.mp4'])

    # ----- a folder moved from one scanned folder to another, each rescanned on its own -----
    def _vid(self, name):
        r = self.c.execute('SELECT video_id, path FROM file_versions WHERE filename = ? AND deleted_at IS NULL', (name,)).fetchone()
        return (r[0], r[1]) if r else None

    def _move_sub(self, src, dst):
        os.makedirs(dst, exist_ok=True)
        os.rename(os.path.join(src, 'sub'), os.path.join(dst, 'sub'))     # keeps the files' times: not "still writing"

    def test_move_in_library_destination_scanned_first(self):
        _video(os.path.join(self.b, 'sub', 'mv.mp4'))
        self.m.scan_library(fast=True, library='Lib')
        vid, _ = self._vid('mv.mp4')
        self._move_sub(self.b, self.a)
        self.m.scan_library(fast=True, library='Lib', folder=self.a)
        self.assertEqual(self._vid('mv.mp4'), (vid, os.path.join(self.a, 'sub', 'mv.mp4')))
        self.m.scan_library(fast=True, library='Lib', folder=self.b)
        self.assertEqual(self._vid('mv.mp4'), (vid, os.path.join(self.a, 'sub', 'mv.mp4')))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM file_versions').fetchone()[0], 1)

    def test_move_in_library_source_scanned_first(self):
        _video(os.path.join(self.b, 'sub', 'mv.mp4'))
        self.m.scan_library(fast=True, library='Lib')
        vid, _ = self._vid('mv.mp4')
        self._move_sub(self.b, self.a)
        self.m.scan_library(fast=True, library='Lib', folder=self.b)
        self.assertIsNone(self._vid('mv.mp4'))                         # hidden until its new folder is scanned
        self.m.scan_library(fast=True, library='Lib', folder=self.a)
        self.assertEqual(self._vid('mv.mp4'), (vid, os.path.join(self.a, 'sub', 'mv.mp4')))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM file_versions').fetchone()[0], 1)

    def _cross_library(self, source_first):
        from routes import scanning
        other = tempfile.mkdtemp(prefix='lustr-o-')
        self.addCleanup(shutil.rmtree, other, True)
        self.m.libraries = {'Lib': [self.a, self.b], 'Other': [other]}
        self.m.db.save_libraries(self.m.libraries)
        _video(os.path.join(self.b, 'sub', 'mv.mp4'))
        scanning._scan_library(scanning.ScanRequest(fast=True, library='Lib'), self.m)
        self._move_sub(self.b, other)
        order = [('Lib', self.b), ('Other', other)]
        for lib, folder in (order if source_first else order[::-1]):
            scanning._scan_library(scanning.ScanRequest(fast=True, library=lib, folder=folder), self.m)
        flags = dict(self.c.execute("SELECT flag_type, filepath FROM file_changes "
                                    "WHERE flag_type IN ('possible_replacement', 'possibly_replaced')").fetchall())
        self.assertEqual(flags, {'possibly_replaced': os.path.join(self.b, 'sub', 'mv.mp4'),
                                 'possible_replacement': os.path.join(other, 'sub', 'mv.mp4')})

    def test_move_between_libraries_source_scanned_first(self):
        self._cross_library(source_first=True)

    def test_move_between_libraries_destination_scanned_first(self):
        self._cross_library(source_first=False)

    def test_route_refuses_a_folder_outside_the_library(self):
        from fastapi import HTTPException
        from routes import scanning
        req = scanning.ScanRequest(fast=True, library='Lib', folder='/etc')
        with self.assertRaises(HTTPException) as cm:
            scanning.scan_library(req, self.m)
        self.assertEqual(cm.exception.status_code, 400)
        req = scanning.ScanRequest(fast=True, library='Nope', folder=self.a)
        with self.assertRaises(HTTPException):
            scanning.scan_library(req, self.m)


if __name__ == '__main__':
    unittest.main()
