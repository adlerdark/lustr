"""
A merged video with files in two libraries is in both (library_scope.py): the library grid, Home / movies
(home.scope_sql) and the display filter see it in each, once, labelled with its primary file's library.

    docker exec -w /app lustr python3 -m unittest tests.test_library_scope -v
"""
import os
import sys
import tempfile
import types
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database                                       # noqa: E402
import display_filter                                 # noqa: E402
import home                                           # noqa: E402
import library_scope                                  # noqa: E402
from routes import videos as videos_routes            # noqa: E402

LIBS = {'A': ['/m/a'], 'B': ['/m/b'], 'Studio': ['/m/studio'], 'StudioRaw': ['/m/studio/raw']}


class LibraryScope(unittest.TestCase):
    def setUp(self):
        self.db = database.Database(os.path.join(tempfile.mkdtemp(prefix='lustr-db-'), 'library.db'))
        self.db.save_libraries(LIBS)
        self.model = types.SimpleNamespace(db=self.db, libraries=dict(LIBS), library_groups={}, library_settings={},
                                           get_libraries_in_group=lambda g, recursive=True: [])
        self.c = self.db.get_connection()
        for vid, path in (('one', '/m/a/one.mp4'), ('two', '/m/b/two.mp4'), ('raw', '/m/studio/raw/r.mp4')):
            self.db.create_video(vid, vid, self.db._get_library_for_path(path))
            self.db.add_file_version(vid, path, os.path.basename(path), {'length': '1:00'})

    def _merge(self):
        """What /api/videos/merge does to the rows: the B file joins the A video."""
        self.db.remove_file_version('/m/b/two.mp4')
        self.db.add_file_version('one', '/m/b/two.mp4', 'two.mp4', {'length': '1:00'})
        self.db.set_preferred_version('one', '/m/a/one.mp4')

    def _ids(self, library):
        where, params = home.scope_sql(self.model, self.c, library)
        return [r[0] for r in self.c.execute(f'SELECT v.video_id FROM videos v JOIN file_versions fv ON fv.video_id = v.video_id '
                                             f'WHERE {where} ORDER BY v.video_id', params)]

    def test_each_file_knows_its_library_the_most_specific_one(self):
        libs = dict(self.c.execute('SELECT path, library FROM file_versions').fetchall())
        self.assertEqual(libs, {'/m/a/one.mp4': 'A', '/m/b/two.mp4': 'B', '/m/studio/raw/r.mp4': 'StudioRaw'})

    def test_a_merged_video_is_in_both_libraries_once(self):
        self._merge()
        self.assertEqual(self._ids('A'), ['one'])
        self.assertEqual(self._ids('B'), ['one'])
        self.assertEqual(self._ids('All Videos'), ['one', 'raw'])
        self.assertEqual(self._ids('Studio'), [])

    def test_the_grid_condition_lists_it_in_both(self):
        self._merge()
        for lib in ('A', 'B'):
            conds, params = ['fv.is_preferred = 1'], []
            videos_routes._add_library_condition(conds, params, self.model, lib, [])
            rows = self.c.execute(f'SELECT fv.path FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
                                  f'WHERE {" AND ".join(conds)}', params).fetchall()
            self.assertEqual([r[0] for r in rows], ['/m/a/one.mp4'], lib)

    def test_the_label_follows_the_primary_file(self):
        self._merge()
        lib = lambda: self.c.execute("SELECT library FROM videos WHERE video_id = 'one'").fetchone()[0]
        self.assertEqual(lib(), 'A')
        self.db.set_preferred_version('one', '/m/b/two.mp4')
        self.assertEqual(lib(), 'B')

    def test_refresh_repairs_labels_and_is_idempotent(self):
        self._merge()
        self.c.execute("UPDATE videos SET library = 'B' WHERE video_id = 'one'")
        self.c.execute("UPDATE file_versions SET library = NULL")
        self.c.commit()
        self.assertEqual(self.db.refresh_file_libraries(), 1)
        self.assertEqual(self.c.execute("SELECT library FROM videos WHERE video_id = 'one'").fetchone()[0], 'A')
        self.assertEqual(self.db.refresh_file_libraries(), 0)

    def test_refresh_leaves_one_primary_file(self):
        self._merge()
        self.c.execute("UPDATE file_versions SET is_preferred = 1 WHERE video_id = 'one'")
        self.c.commit()
        self.db.refresh_file_libraries()
        n = self.c.execute("SELECT COUNT(*) FROM file_versions WHERE video_id = 'one' AND is_preferred = 1").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(self._ids('A'), ['one'])

    def test_editing_library_folders_moves_files(self):
        self.db.save_libraries({**LIBS, 'B': ['/m/elsewhere'], 'A': ['/m/a', '/m/b']})
        self.assertEqual(self.c.execute("SELECT library FROM file_versions WHERE path = '/m/b/two.mp4'").fetchone()[0], 'A')

    def test_display_filter_shows_it_when_any_of_its_libraries_is_shown(self):
        self._merge()
        self.model.library_settings = {'A': {'type': 'gay'}, 'B': {'type': 'straight'}}
        for sel, shown in ((['straight'], True), (['gay'], True), (['trans'], False)):
            sql, params = display_filter.shown_sql(self.c, self.model, selection=sel)
            hit = self.c.execute(f"SELECT 1 FROM videos v WHERE v.video_id = 'one' AND {sql}", params).fetchone()
            self.assertEqual(bool(hit), shown, sel)

    def test_expand(self):
        self.assertIsNone(library_scope.expand(self.model, 'All Videos'))
        self.assertEqual(library_scope.expand(self.model, 'A'), ['A'])


if __name__ == '__main__':
    unittest.main()
