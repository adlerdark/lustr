"""
The library scan and files that change (model.py): a file whose size changed is re-read after the scan,
not during it; a 0-byte file counts as still downloading.

    docker exec -w /app lustr python3 -m unittest tests.test_scan_changes -v
"""
import os
import sys
import tempfile
import time
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model                                          # noqa: E402


def _file(path, size, age=3600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\0' * size)
    t = time.time() - age                             # older than WRITING_SECONDS: finished
    os.utime(path, (t, t))


class ScanChanges(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='lustr-lib-')
        self.m = model.LibraryModel()
        self.m.libraries = {'Lib': [self.root]}
        self.reread = []
        self.m.fill_missing_metadata = lambda paths: self.reread.append(sorted(paths))

    def test_a_file_that_changed_size_is_read_again_after_the_scan(self):
        p = os.path.join(self.root, 'a.mp4')
        _file(p, 2_000_000)
        self.m.scan_library(fast=True, library='Lib')
        self.m.scan_library(library='Lib')
        self.assertEqual(self.reread[-1], [])         # size not read yet: left to the missing-details filler
        self.m.db.get_connection().execute("UPDATE file_versions SET file_size = '1.91 MB' WHERE path = ?", (p,))
        self.m.db.get_connection().commit()           # what reading its details stores
        _file(p, 2_000_100)                           # a rounding-sized difference: not a change
        self.m.scan_library(library='Lib')
        self.assertEqual(self.reread[-1], [])
        _file(p, 3_000_000)                           # replaced
        self.m.scan_library(library='Lib')
        self.assertEqual(self.reread[-1], [p])

    def test_a_file_added_while_empty_is_read_again_once_it_has_arrived(self):
        p = os.path.join(self.root, 'c.mp4')
        _file(p, 2_000_000)
        self.m.scan_library(fast=True, library='Lib')
        self.m.db.get_connection().execute("UPDATE file_versions SET file_size = '0 B' WHERE path = ?", (p,))
        self.m.db.get_connection().commit()
        self.m.scan_library(library='Lib')
        self.assertEqual(self.reread[-1], [p])

    def test_an_empty_or_recent_file_is_still_being_written(self):
        p = os.path.join(self.root, 'b.mp4')
        _file(p, 0)
        self.assertTrue(self.m._still_writing(p))
        _file(p, 1000, age=10)
        self.assertTrue(self.m._still_writing(p))
        _file(p, 1000)
        self.assertFalse(self.m._still_writing(p))
        self.assertFalse(self.m._still_writing(os.path.join(self.root, 'missing.mp4')))


if __name__ == '__main__':
    unittest.main()
