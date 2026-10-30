"""
Gender tests: trans_female / trans_male, the one-time 'trans' migration and the rules that use gender.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database                                       # noqa: E402
import ext_db                                         # noqa: E402


def old_library(path):
    """A library.db from before the split: gender 'trans', plus external data to read the direction from."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE cast_gender (cast_name TEXT PRIMARY KEY,
                 gender TEXT NOT NULL CHECK(gender IN ('female', 'male', 'trans')),
                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.executemany('INSERT INTO cast_gender (cast_name, gender) VALUES (?, ?)', [
        ('Jane Doe', 'female'), ('John Roe', 'male'),
        ('Alex Linked', 'trans'),       # StashDB link says trans male
        ('Bea Credited', 'trans'),      # only credited in a ThePornDB scene
        ('Cam Unknown', 'trans'),       # nothing known
    ])
    c.execute('CREATE TABLE cast_ext_link (cast_name TEXT PRIMARY KEY, ext_source TEXT, ext_id TEXT, ext_gender TEXT)')
    c.execute("INSERT INTO cast_ext_link VALUES ('Alex Linked', 'stashdb', 'sd-1', 'TRANSGENDER_MALE')")
    c.execute('CREATE TABLE video_ext_link (video_id TEXT PRIMARY KEY, ext_source TEXT, ext_performers TEXT)')
    c.execute('INSERT INTO video_ext_link VALUES (?, ?, ?)', ('v1', 'tpdb', json.dumps([
        {'id': 'tp-9', 'name': 'Bea Credited', 'gender': 'TRANS FEMALE'},
        {'id': 'sd-1', 'name': 'Alex Linked', 'gender': 'TRANS FEMALE'},   # tpdb ids aren't StashDB ids
    ])))
    c.commit()
    c.close()


class GenderTests(unittest.TestCase):
    def test_external_values(self):
        f = database.gender_from_external
        self.assertEqual(f('TRANSGENDER_FEMALE'), 'trans_female')
        self.assertEqual(f('Trans Female'), 'trans_female')
        self.assertEqual(f('TRANSGENDER_MALE'), 'trans_male')
        self.assertEqual(f('FEMALE'), 'female')
        self.assertEqual(f('MALE'), 'male')
        for raw in ('Trans', 'NON_BINARY', 'UNKNOWN', None, ''):
            self.assertIsNone(f(raw))

    def test_suggested_gender_default(self):
        f = ext_db.default_gender
        self.assertEqual(f('female', 'male', True, True), 'female')          # yours wins
        self.assertEqual(f(None, 'female', False, True), 'female')           # then the scene's
        self.assertEqual(f(None, None, False, True), 'male')                 # a gay video, nothing known
        self.assertEqual(f(None, None, True, False), 'male')                 # a man in a straight scene
        self.assertEqual(f(None, None, False, False), 'female')

    def test_gay_library_is_not_a_straight_scene(self):
        f = ext_db.scene_is_straight
        both = ['male', 'female']
        self.assertFalse(f(['gay'], [], both))
        self.assertFalse(f(['bisexual'], [], both))
        self.assertTrue(f([], [], both))                                     # unknown: the old guess
        self.assertTrue(f(['straight'], [], both))
        self.assertTrue(f(['straight'], ['Straight'], ['female']))

    def test_migration(self):
        path = os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db')
        old_library(path)
        db = database.Database(path)
        rows = dict(db.get_connection().execute('SELECT cast_name, gender FROM cast_gender').fetchall())
        self.assertEqual(rows, {'Jane Doe': 'female', 'John Roe': 'male', 'Alex Linked': 'trans_male',
                                'Bea Credited': 'trans_female', 'Cam Unknown': 'trans_female'})
        backups = os.listdir(os.path.join(os.path.dirname(path), 'backups'))
        self.assertTrue(any(b.startswith('library_before_trans_genders_') for b in backups))
        # a second start changes nothing and makes no second copy
        database.Database(path)
        self.assertEqual(len(os.listdir(os.path.join(os.path.dirname(path), 'backups'))), len(backups))

    def test_set_gender_values(self):
        db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        for g in database.GENDERS:
            db.set_cast_gender('Jane Doe', g)
            self.assertEqual(db.get_cast_gender('Jane Doe'), g)
        with self.assertRaises(ValueError):
            db.set_cast_gender('Jane Doe', 'trans')

    def test_straight_rule(self):
        s = ext_db.is_straight_video
        self.assertTrue(s([], ['male', 'female']))
        self.assertFalse(s([], ['male', 'trans_female']))
        self.assertFalse(s([], ['trans_male', 'female']))
        self.assertTrue(s(['straight'], ['trans_female']))          # an orientation tag decides

    def test_filter_bucket(self):
        self.assertEqual(ext_db._gender_bucket('trans_female'), 'trans')
        self.assertEqual(ext_db._gender_bucket('trans_male'), 'trans')
        self.assertEqual(ext_db._gender_bucket(None), 'female')

    def test_scene_gender(self):
        self.assertEqual(ext_db._scene_gender({'gender': 'TRANSGENDER_MALE'}, 'female'), 'trans_male')
        self.assertEqual(ext_db._scene_gender({'gender': 'TRANS'}, None), 'trans_female')
        self.assertEqual(ext_db._scene_gender({'gender': 'TRANS'}, 'trans_male'), 'trans_male')
        self.assertEqual(ext_db._scene_gender({}, 'male'), 'male')


if __name__ == '__main__':
    unittest.main()
