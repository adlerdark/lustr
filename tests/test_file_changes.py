"""
File Changes Review (model.check_cross_library_replacements, /api/file-changes/ignore): a moved file is
paired with its old entry; "Separate" unpairs them for good, so the next move check leaves them apart.

    docker exec -w /app lustr python3 -m unittest tests.test_file_changes -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model                                          # noqa: E402
from routes import videos as videos_routes            # noqa: E402


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class FileChanges(unittest.TestCase):
    def setUp(self):
        self.m = model.LibraryModel()
        self.c = self.m.db.get_connection()
        self.c.execute('DELETE FROM file_changes')
        self.c.commit()
        meta = {'length_seconds': 600, 'file_size': 1000}
        self.old = self.m.db.flag_file_change('scene', '/m/a/scene.mp4', 'A_scene', 'A', 'deleted', meta)
        self.new = self.m.db.flag_file_change('scene', '/m/b/scene.mp4', None, 'B', 'new', meta)

    def _flags(self):
        return {r[0]: (r[1], r[2]) for r in self.c.execute('SELECT id, flag_type, related_id FROM file_changes')}

    def test_a_move_is_paired(self):
        self.m.check_cross_library_replacements()
        self.assertEqual(self._flags(), {self.old: ('possibly_replaced', self.new), self.new: ('possible_replacement', self.old)})

    def test_separate_is_not_paired_again(self):
        self.m.check_cross_library_replacements()
        asyncio.run(videos_routes.ignore_file_change(_Req({'change_ids': [self.old, self.new]}), self.m))
        self.assertEqual(self._flags(), {self.old: ('deleted', None), self.new: ('new', None)})
        self.m.check_cross_library_replacements()
        self.assertEqual(self._flags(), {self.old: ('deleted', None), self.new: ('new', None)})

    def test_keep_for_now_on_a_deleted_file_changes_nothing(self):
        asyncio.run(videos_routes.ignore_file_change(_Req({'change_ids': [self.old]}), self.m))
        self.m.check_cross_library_replacements()            # still free to be matched with its moved copy
        self.assertEqual(self._flags()[self.old], ('possibly_replaced', self.new))



class DeleteMissingFile(unittest.TestCase):
    """Delete on a missing file removes that file only; the video goes with its last file (2026-11-08)."""
    def setUp(self):
        self.m = model.LibraryModel()
        self.c = self.m.db.get_connection()
        for t in ('file_changes', 'file_versions', 'videos', 'video_cast_junction'):
            self.c.execute(f'DELETE FROM {t}')
        self.c.execute("INSERT INTO videos (video_id, filename_base, library) VALUES ('V', 'x', 'A')")
        self.c.execute("INSERT OR IGNORE INTO video_cast (value) VALUES ('Jane Doe')")
        self.c.execute("INSERT INTO video_cast_junction (video_id, cast_id) SELECT 'V', id FROM video_cast WHERE value = 'Jane Doe'")
        for path, codec, pref in (('/m/a/x.wmv', 'h264', 1), ('/m/a/x.mp4', 'hevc', 0)):
            self.c.execute('INSERT INTO file_versions (path, video_id, filename, codec, is_preferred) VALUES (?, ?, ?, ?, ?)',
                           (path, 'V', os.path.basename(path), codec, pref))
        self.c.commit()

    def delete(self, path):
        cid = self.m.db.flag_file_change(os.path.basename(path), path, 'V', 'A', 'deleted', {})
        asyncio.run(videos_routes.delete_old_file(_Req({'change_id': cid}), self.m))

    def test_the_other_file_and_the_video_stay(self):
        self.delete('/m/a/x.wmv')
        self.assertEqual([r[0] for r in self.c.execute("SELECT path FROM file_versions WHERE video_id = 'V' AND is_preferred = 1")],
                         ['/m/a/x.mp4'])
        self.assertTrue(self.c.execute("SELECT 1 FROM videos WHERE video_id = 'V'").fetchone())
        self.assertTrue(self.c.execute("SELECT 1 FROM video_cast_junction WHERE video_id = 'V'").fetchone())

    def test_the_last_file_takes_the_video(self):
        self.c.execute("DELETE FROM file_versions WHERE path = '/m/a/x.mp4'")
        self.c.commit()
        self.delete('/m/a/x.wmv')
        self.assertFalse(self.c.execute("SELECT 1 FROM videos WHERE video_id = 'V'").fetchone())



class SafetyBackup(unittest.TestCase):
    """Before many files' metadata is removed at once, the database is copied first."""
    def setUp(self):
        import db_maintenance as dm
        self.dm, self.m = dm, model.LibraryModel()
        self.c = self.m.db.get_connection()
        self.dm.delete_backups([b['name'] for b in dm.list_backups() if os.path.basename(b['name']).startswith(dm.SAFETY_PREFIX)])

    def safety(self):
        return [b['name'] for b in self.dm.list_backups() if os.path.basename(b['name']).startswith(self.dm.SAFETY_PREFIX)]

    def test_endpoint_backs_up_and_reuses_a_fresh_copy(self):
        r = asyncio.run(videos_routes.file_changes_backup(_Req({'action': 'delete', 'count': 376}), self.m))
        self.assertTrue(r['success'])
        self.assertIn('file-changes-delete-376', r['backup'])
        self.assertTrue(os.path.getsize(os.path.join(self.dm.DATA_DIR, r['backup'])) > 0)
        again = asyncio.run(videos_routes.file_changes_backup(_Req({'action': 'merge', 'count': 12}), self.m))
        self.assertEqual(again['backup'], r['backup'])            # minutes apart: the same copy
        self.assertEqual(len(self.safety()), 1)

    def test_old_ones_are_pruned(self):
        import time as _t
        for i in range(self.dm.SAFETY_KEEP + 2):
            self.dm.safety_backup(self.c, f'run {i}', reuse_seconds=0)
            _t.sleep(1.05)                                        # names are per second
        self.assertEqual(len(self.safety()), self.dm.SAFETY_KEEP)


if __name__ == '__main__':
    unittest.main()
