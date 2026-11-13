"""
Spreadsheet import / export (metadata_import.py, routes/files.py): the preview shows only real changes,
blank cells change nothing, '*' clears, add vs replace for list columns, matching by unique filename,
technical columns ignored, a backup before writing; and the full backup zip.

    docker exec -w /app lustr python3 -m unittest tests.test_metadata_import -v
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_maintenance as dm                           # noqa: E402
import metadata_import as mi                          # noqa: E402
import model                                          # noqa: E402
from routes import metadata as rm                     # noqa: E402


def _video(path, seconds=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=size=320x180:rate=10:duration={seconds}',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', path], check=True, capture_output=True)
    t = os.path.getmtime(path) - 3600
    os.utime(path, (t, t))


class Import(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='lustr-lib-')
        for rel, sec in (('a/one.mp4', 1), ('a/two.mp4', 1), ('b/two.mp4', 12), ('b/three.mp4', 1), ('b/one.mp4', 1)):
            _video(os.path.join(cls.root, rel), sec)        # two.mp4: two videos; one.mp4: one video in two files

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.m = model.LibraryModel()
        self.m.libraries = {'Lib': [self.root]}
        self.m.db.save_libraries(self.m.libraries)
        self.m.fill_missing_metadata = lambda paths: None
        self.c = self.m.db.get_connection()
        self.addCleanup(self.c.rollback)                 # a failed test never leaves the database locked
        for t in ('file_versions', 'videos', 'file_changes'):
            self.c.execute(f'DELETE FROM {t}')
        self.c.commit()
        self.m.scan_library(fast=True, library='Lib')
        self.one = os.path.join(self.root, 'a', 'one.mp4')
        self.three = os.path.join(self.root, 'b', 'three.mp4')
        rm.update_metadata(rm.UpdateMetadataRequest(paths=[self.one], updates={'title': 'One'},
                                                    add_to_lists={'tags': ['blonde', 'outdoor'], 'hair': ['long']}), self.m)

    def get(self, path, field):
        return self.m.db.get_file(path).get(field)

    def plan(self, text, modes=None):
        _cols, rows = mi.read_csv(text)
        return mi.plan(self.c, self.m.db.get_file, rows, modes or {})

    def run_import(self, text, modes=None):
        summary, work = self.plan(text, modes)
        mi.apply(self.c, work, lambda p, **kw: rm.update_metadata(rm.UpdateMetadataRequest(paths=[p], **kw), self.m),
                 lambda p, on: None)
        return summary

    def test_round_trip_changes_nothing(self):
        text = mi.export_csv(self.c, self.m.db.get_file, [self.one, self.three])
        summary, work = self.plan(text, {'tags': 'replace', 'hair': 'replace'})
        self.assertEqual((summary['matched'], summary['changed']), (2, 0))
        self.assertIn('codec', summary['columns']['ignored'])                                 # technical: not imported

    def test_blank_add_replace_clear(self):
        self.run_import(f'path,title,tags,hair,codec\n{self.one},,curly,*,xyz\n')
        self.assertEqual(self.get(self.one, 'title'), 'One')                                  # blank: left alone
        self.assertEqual(sorted(self.get(self.one, 'tags')), ['blonde', 'curly', 'outdoor'])  # add (default)
        self.assertEqual(self.get(self.one, 'hair') or [], [])                                # '*': cleared
        self.run_import(f'path,tags\n{self.one},curly|indoor\n', {'tags': 'replace'})
        self.assertEqual(sorted(self.get(self.one, 'tags')), ['curly', 'indoor'])

    def test_matching_by_filename(self):
        s = self.run_import('filename,title\nthree.mp4,Three\ntwo.mp4,Two\nnope.mp4,X\none.mp4,Uno\n')
        self.assertEqual(self.get(self.three, 'title'), 'Three')
        self.assertEqual(self.get(self.one, 'title'), 'Uno')                                  # copies of one video
        self.assertEqual((s['ambiguous'], s['unmatched']), (['two.mp4'], ['nope.mp4']))

    def test_columns(self):
        _c, rows = mi.read_csv('Path;Tags;Movie Scene;Codec;Whatever\n/x;a;1;b;c\n')           # semicolons, loose names
        k = mi.column_kinds(list(rows[0]))
        self.assertEqual((k['list'], k['single'], k['ignored']), (['tags'], ['movie_scene'], ['codec', 'whatever']))
        with self.assertRaises(mi.ImportError_):
            mi.read_csv('title,tags\nx,y\n')

    def test_apply_backs_up_first(self):
        calls, real = [], dm.safety_backup
        dm.safety_backup = lambda conn, reason, **kw: calls.append(reason) or real(conn, reason, **kw)
        self.addCleanup(setattr, dm, 'safety_backup', real)
        self.run_import(f'path,title\n{self.one},New\n')
        self.assertEqual(self.get(self.one, 'title'), 'New')
        self.assertEqual(calls, ['import'])
        self.assertTrue(any(os.path.basename(b['name']).startswith(dm.SAFETY_PREFIX) for b in dm.list_backups()))

    def test_export_scope(self):
        from routes import files, videos
        videos._model = self.m
        paths = files._export_paths(None, self.m, 'Lib', '')
        self.assertEqual(len(paths), 4)                                                       # one.mp4 x2 is one video
        self.assertEqual(files._export_paths(None, self.m, 'Lib', 'three'), [self.three])
        text = mi.export_csv(self.c, self.m.db.get_file, [self.one], template=True)
        self.assertEqual(text.splitlines()[1].split(',')[:3], [self.one, 'one.mp4', ''])

    def test_your_own_field(self):
        import fields
        fields.bind(self.m.db)
        fields.forget()
        if 'mood' not in fields.all_fields(self.c):
            fields.add_field(self.c, 'Mood', words_=False)
        fields.restore(self.c, 'mood') if 'mood' in fields.hidden_fields(self.c) else None
        self.addCleanup(fields.hide, self.c, 'mood')
        self.assertIn('mood', mi.export_columns())
        self.run_import(f'path,Mood\n{self.one},Very Calm|happy\n')                 # its name as a column header
        self.assertEqual(sorted(self.get(self.one, 'mood')), ['Very Calm', 'happy'])    # spaces allowed: kept as typed
        text = mi.export_csv(self.c, self.m.db.get_file, [self.one])
        self.assertIn('Very Calm', text)
        fields.hide(self.c, 'mood')
        self.assertNotIn('mood', mi.export_columns())                                   # hidden: not exported
        self.assertEqual(mi.column_kinds(['path', 'mood'])['ignored'], ['mood'])       # nor imported

    def test_full_backup_zip(self):
        import tools_api
        tools_api._model = self.m
        for f in ('quicktags.json', 'users.json'):
            with open(os.path.join(dm.DATA_DIR, f), 'w') as fh:
                fh.write('{}')
        zpath, _name = tools_api.full_backup_zip()
        try:
            with zipfile.ZipFile(zpath) as z:
                names = set(z.namelist())
                self.assertIn('library.db', names)
                self.assertIn('quicktags.json', names)
                self.assertNotIn('users.json', names)
                self.assertTrue(z.read('library.db').startswith(b'SQLite format 3'))
        finally:
            os.remove(zpath)


if __name__ == '__main__':
    unittest.main()
