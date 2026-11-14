"""
File fingerprints (fingerprints.py) and how the scene matcher uses them.

    docker exec -w /app lustr python3 -m unittest tests.test_fingerprints -v
"""
import os
import struct
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ext_db                                         # noqa: E402
import fingerprints                                   # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


def scene(i, title='A Scene', fp=(3, 0), duration=1200, studio='s1'):
    return {'id': i, 'title': title, 'date': '2024-01-02', 'duration': duration, 'code': '', 'studio_id': studio,
            'studio_name': 'Studio', 'parent_id': None, 'parent_name': None, 'performers': [], 'image_url': None,
            'tags': [], 'details': '', 'fingerprint': {'submissions': fp[0], 'reports': fp[1]}}


class Hash(unittest.TestCase):
    def test_oshash(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'f.mp4')
        words = [1, 2, 3]
        with open(p, 'wb') as f:
            f.write(struct.pack('<3Q', *words) + b'\0' * (200 * 1024 - 24))
        self.assertEqual(fingerprints.oshash(p), '%016x' % (200 * 1024 + 6))
        small = os.path.join(d, 's.mp4')
        with open(small, 'wb') as f:
            f.write(b'x' * 1000)
        self.assertIsNone(fingerprints.oshash(small))
        self.assertIsNone(fingerprints.oshash(os.path.join(d, 'missing.mp4')))


class Matching(Base):
    def setUp(self):
        super().setUp()
        d = tempfile.mkdtemp()
        self.path = os.path.join(d, 'video.mp4')
        with open(self.path, 'wb') as f:
            f.write(b'\1' * 200 * 1024)
        self.c.execute("INSERT INTO file_versions (path, video_id, filename, is_preferred) VALUES (?, 'v1', 'video.mp4', 1)",
                       (self.path,))
        self.c.commit()
        self.saved = (fingerprints.lookup, ext_db.scenes_search, ext_db.scenes_query)
        self.found = []
        fingerprints.lookup = lambda source, hashes: {h: list(self.found) for h in hashes}
        self.searched = []
        ext_db.scenes_search = lambda source, term, per_page=25: self.searched.append(term) or []
        ext_db.scenes_query = lambda source, inp, pages=1, per_page=100: []

    def tearDown(self):
        fingerprints.lookup, ext_db.scenes_search, ext_db.scenes_query = self.saved

    def clues(self):
        return {'video_id': 'v1', 'path': self.path, 'stem': 'video', 'title': 'Something Else Entirely', 'movie_title': None,
                'scene_number': None, 'date': None, 'year': None, 'duration': 1200, 'cast': [], 'sites': []}

    def test_hash_is_kept_until_the_file_changes(self):
        h = fingerprints.for_path(self.c, self.path)
        self.assertEqual(self.c.execute('SELECT oshash FROM file_fingerprints WHERE path = ?', (self.path,)).fetchone()[0], h)
        with open(self.path, 'ab') as f:
            f.write(b'\2' * 10)
        os.utime(self.path, (1, 1))
        self.assertNotEqual(fingerprints.for_path(self.c, self.path), h)

    def test_one_trusted_match_wins_without_searching(self):
        self.found = [scene('s-1')]
        out, notes = ext_db.find_scene_candidates(self.c, 'stashdb', self.clues(), None, 'stashdb')
        self.assertEqual(out[0]['id'], 's-1')
        self.assertGreaterEqual(out[0]['score'], 90)
        self.assertIn('file fingerprint', out[0]['found_by'])
        self.assertEqual(self.searched, [])
        self.assertIn('fingerprint', ext_db._strong_signals(out[0], self.clues()))

    def test_reported_match_still_searches_and_is_not_trusted(self):
        self.found = [scene('s-1', fp=(1, 2))]
        out, _ = ext_db.find_scene_candidates(self.c, 'stashdb', self.clues(), None, 'stashdb')
        self.assertTrue(self.searched)
        self.assertNotIn('fingerprint', ext_db._strong_signals(out[0], self.clues()))

    def test_two_scenes_for_one_file_are_told_apart_by_the_usual_clues(self):
        self.found = [scene('s-1', title='Wrong One', duration=600), scene('s-2', title='Something Else Entirely')]
        out, _ = ext_db.find_scene_candidates(self.c, 'stashdb', self.clues(), None, 'stashdb')
        self.assertEqual(out[0]['id'], 's-2')
        self.assertGreater(out[0]['score'], out[1]['score'])


if __name__ == '__main__':
    unittest.main()
