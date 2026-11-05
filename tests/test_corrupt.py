"""
Videos tagged corrupt.file (corrupt.py) are hidden by the display filter for everyone, and listed with
their files for Settings -> Maintenance -> Corrupt files.

    docker exec -w /app lustr python3 -m unittest tests.test_corrupt -v
"""
import os
import sys
import tempfile
import types
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import corrupt                                        # noqa: E402
import database                                       # noqa: E402
import display_filter                                 # noqa: E402

LIBS = {'A': ['/m/a']}


class Corrupt(unittest.TestCase):
    def setUp(self):
        self.db = database.Database(os.path.join(tempfile.mkdtemp(prefix='lustr-db-'), 'library.db'))
        self.db.save_libraries(LIBS)
        self.model = types.SimpleNamespace(db=self.db, libraries=dict(LIBS), library_groups={}, library_settings={},
                                           get_libraries_in_group=lambda g, recursive=True: [])
        self.c = self.db.get_connection()
        for vid in ('good', 'bad'):
            self.db.create_video(vid, vid.title(), 'A')
            self.db.add_file_version(vid, f'/m/a/{vid}.mp4', f'{vid}.mp4', {'length': '1:00'})
        self.db.add_file_version('bad', '/m/a/bad-1080.mp4', 'bad-1080.mp4', {'length': '1:00'})

    def _tag(self, vid, value='corrupt.file'):
        self.c.execute('INSERT OR IGNORE INTO video_tags (value) VALUES (?)', (value,))
        tid = self.c.execute('SELECT id FROM video_tags WHERE value = ?', (value,)).fetchone()[0]
        self.c.execute('INSERT OR IGNORE INTO video_tags_junction (video_id, tags_id) VALUES (?, ?)', (vid, tid))
        self.c.commit()

    def test_nothing_marked_filters_nothing(self):
        self._tag('bad', 'other.tag')
        self.assertEqual(display_filter.shown_sql(self.c, self.model), ('', []))
        self.assertEqual(corrupt.listing(self.c), [])

    def test_a_marked_video_is_hidden(self):
        before = display_filter.cache_key(self.c)
        self._tag('bad')
        self.assertNotEqual(display_filter.cache_key(self.c), before)
        self.assertEqual(display_filter.hidden_ids(self.c, self.model), {'bad'})
        self.assertEqual(display_filter.hidden_paths(self.c, self.model), {'/m/a/bad.mp4', '/m/a/bad-1080.mp4'})

    def test_with_an_orientation_selection_too(self):
        self._tag('bad')
        self.assertEqual(display_filter.hidden_ids(self.c, self.model, selection=['gay']), {'bad'})   # 'good': untagged, mixed library
        sql, params = display_filter.condition(self.c, self.model)
        ids = {r[0] for r in self.c.execute(f'SELECT v.video_id FROM videos v WHERE {sql}', params)}
        self.assertEqual(ids, {'good'})

    def test_listing(self):
        self._tag('bad')
        (it,) = corrupt.listing(self.c)
        self.assertEqual(it['video_id'], 'bad')
        self.assertEqual(sorted(p['path'] for p in it['paths']), ['/m/a/bad-1080.mp4', '/m/a/bad.mp4'])


if __name__ == '__main__':
    unittest.main()
