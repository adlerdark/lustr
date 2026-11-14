"""
The full-text search index (search_index.py): same matches as the old word search, kept up to date
by triggers.

    docker exec -w /app lustr python3 -m unittest tests.test_search_index -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import search_index as si                             # noqa: E402
from search_parser import SearchParser                # noqa: E402
from tests.test_cast_names import Base                # noqa: E402

P = {'v1': '/media/Lib/Studio X/jane doe - beach day.mp4',
     'v2': '/media/Lib/Studio Y/Émilie et Zoé 100%_real.mkv',
     'v3': '/media/Lib/other/clip_003.mp4'}


class Index(Base):
    def setUp(self):
        super().setUp()
        for vid, path in P.items():
            self.c.execute("INSERT INTO file_versions (path, video_id, filename, is_preferred, extension, codec) "
                           "VALUES (?, ?, ?, 1, '.mp4', 'h264')", (path, vid, os.path.basename(path)))
        self.c.execute("UPDATE videos SET title = 'Beach Day' WHERE video_id = 'v1'")
        self.c.commit()
        self.tag('v1', 'Jane Doe')
        self.tag('v3', 'Ann Lee')
        si._known.clear()
        si.refresh(self.db)

    def found(self, query):
        sql, params = si.text_condition(query)
        return sorted(r[0] for r in self.c.execute(f'SELECT video_id FROM {si.TABLE} WHERE {sql}', params))

    def old(self, query):
        recs = self.db.get_files_bulk()
        return sorted(r['video_id'] for p, r in recs.items() if SearchParser.matches_base_query(p, r, query))

    def test_same_matches_as_the_old_search(self):
        for q in ('jane', 'JAN', 'doe beach', 'studio*day', '"jane doe"', 'émilie', '100%', '_real', 'clip_0',
                  'jane or ann', 'jane and not ann', '(ann or zoé) and studio', 'nothing here', 'mp4'):
            self.assertEqual(self.found(q), self.old(q), q)

    def test_edits_are_picked_up(self):
        self.assertEqual(self.found('sunset'), [])
        self.c.execute("UPDATE videos SET title = 'Sunset' WHERE video_id = 'v3'")
        self.c.commit()
        si.refresh(self.db)
        self.assertEqual(self.found('sunset'), ['v3'])

    def test_list_values_and_renames(self):
        self.tag('v2', 'Kim Park')
        si.refresh(self.db)
        self.assertEqual(self.found('kim park'), ['v2'])
        self.c.execute("UPDATE video_cast SET value = 'Kim Parker' WHERE value = 'Kim Park'")
        self.c.commit()
        si.refresh(self.db)
        self.assertEqual(self.found('parker'), ['v2'])
        self.c.execute("DELETE FROM video_cast_junction WHERE video_id = 'v2'")
        self.c.commit()
        si.refresh(self.db)
        self.assertEqual(self.found('parker'), [])

    def test_deleted_and_moved_files(self):
        self.c.execute("UPDATE file_versions SET deleted_at = CURRENT_TIMESTAMP WHERE video_id = 'v3'")
        self.c.commit()
        si.refresh(self.db)
        self.assertEqual(self.found('clip'), [])
        self.c.execute("UPDATE file_versions SET deleted_at = NULL, path = '/media/Lib/moved/clip_003.mp4' "
                       "WHERE video_id = 'v3'")
        self.c.commit()
        si.refresh(self.db)
        self.assertEqual(self.found('moved'), ['v3'])

    def test_unrelated_updates_do_not_dirty(self):
        self.c.execute("UPDATE file_versions SET last_seen = CURRENT_TIMESTAMP")
        self.c.execute("UPDATE videos SET favorite = 1")
        self.c.commit()
        self.assertEqual(self.c.execute(f'SELECT COUNT(*) FROM {si.DIRTY}').fetchone()[0], 0)

    def test_unbalanced_query_is_one_phrase(self):
        sql, params = si.text_condition('(jane')
        self.assertEqual(params, ['%(jane%'])


if __name__ == '__main__':
    unittest.main()
