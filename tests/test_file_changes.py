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


if __name__ == '__main__':
    unittest.main()
