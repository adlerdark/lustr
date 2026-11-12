"""
Clearing a field on several videos at once (a lone '*' in Edit Metadata -> clear_lists / an empty value):
the field is emptied on every video whatever each had, other fields stay; Cast keeps the Male Cast and
Male Cast clears only them.

    docker exec -w /app lustr python3 -m unittest tests.test_metadata_clear -v
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
from routes import metadata as rm                     # noqa: E402


def _video(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=10:duration=1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', path], check=True, capture_output=True)
    t = os.path.getmtime(path) - 3600
    os.utime(path, (t, t))


class ClearFields(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='lustr-lib-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.m = model.LibraryModel()
        self.m.libraries = {'Lib': [self.root]}
        self.m.db.save_libraries(self.m.libraries)
        self.m.fill_missing_metadata = lambda paths: None
        self.c = self.m.db.get_connection()
        self.addCleanup(self.c.rollback)                 # a failed test never leaves the database locked
        for t in ('file_versions', 'videos', 'file_changes'):
            self.c.execute(f'DELETE FROM {t}')
        self.c.commit()
        self.paths = [os.path.join(self.root, n) for n in ('one.mp4', 'two.mp4')]
        for p in self.paths:
            _video(p)
        self.m.scan_library(fast=True, library='Lib')
        self.update(self.paths[:1], updates={'title': 'First'}, add_to_lists={'tags': ['a', 'b'], 'hair': ['blonde'], 'cast': ['Jane', 'John']})
        self.update(self.paths[1:], updates={'title': 'Second'}, add_to_lists={'tags': ['c'], 'hair': ['red'], 'cast': ['Jane']})

    def update(self, paths, **kw):
        return rm.update_metadata(rm.UpdateMetadataRequest(paths=paths, updates=kw.pop('updates', {}), **kw), self.m)

    def get(self, path, field):
        return self.m.db.get_file(path).get(field)

    def test_list_field_cleared_on_every_video(self):
        self.update(self.paths, clear_lists=['tags'])
        for p in self.paths:
            self.assertEqual(self.get(p, 'tags') or [], [])
        self.assertEqual(self.get(self.paths[0], 'hair'), ['blonde'])        # other fields stay
        self.assertEqual(self.get(self.paths[1], 'hair'), ['red'])

    def test_single_field_cleared(self):
        self.update(self.paths, updates={'title': ''})
        for p in self.paths:
            self.assertEqual(self.get(p, 'title') or '', '')

    def test_cast_and_male_cast(self):
        import ext_db
        ext_db._model = self.m
        self.addCleanup(setattr, ext_db, '_model', None)
        ext_db._schema_ready = False                     # another test module may have made its tables elsewhere
        ext_db.ensure_schema()
        vid = self.m.db.get_video_by_path(self.paths[0])['video_id']
        ext_db.male_cast_change(self.c, {vid}, ['John'], [])
        ext_db._male_invalidate()
        self.update(self.paths, clear_lists=['cast'])                        # keeps the Male Cast
        self.assertEqual(self.get(self.paths[0], 'cast'), ['John'])
        self.assertEqual(self.get(self.paths[1], 'cast') or [], [])
        self.update(self.paths, clear_lists=['male_cast'])
        self.assertEqual(self.get(self.paths[0], 'cast') or [], [])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM video_cast_role WHERE video_id = ?', (vid,)).fetchone()[0], 0)



class HouseFormat(ClearFields):
    """New values: lowercase with periods (value_format.py); names kept as typed; your spellings kept."""
    def test_new_values(self):
        self.update(self.paths[:1], add_to_lists={'tags': ['Big Tits', 'A'], 'site': ['Team Skeet'], 'cast': ['Mary Ann']})
        self.assertEqual(sorted(self.get(self.paths[0], 'tags')), ['a', 'b', 'big.tits'])
        self.assertEqual(self.get(self.paths[0], 'site'), ['team.skeet'])
        self.assertIn('Mary Ann', self.get(self.paths[0], 'cast'))

    def test_existing_spelling_kept(self):
        self.c.execute("INSERT INTO video_site (value) VALUES ('TeamSkeetVIP')")
        self.c.commit()
        self.update(self.paths[1:], add_to_lists={'site': ['teamskeetvip']})
        self.assertEqual(self.get(self.paths[1], 'site'), ['TeamSkeetVIP'])

    def test_format(self):
        import value_format as vf
        self.assertEqual(vf.fmt('  Big   Natural. Tits '), 'big.natural.tits')
        self.assertEqual(vf.house('cast', 'Jane Doe', {}), 'Jane Doe')



if __name__ == '__main__':
    unittest.main()
