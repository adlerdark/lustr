"""
The sidebar order survives a restart: top-level groups keep their place among the libraries (model.py).

    docker exec -w /app lustr python3 -m unittest tests.test_library_order -v
"""
import json
import os
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model                                          # noqa: E402


class OrderTests(unittest.TestCase):
    def setUp(self):
        m = model.LibraryModel()
        m.db.save_libraries({'Solo': ['/m/solo'], 'Vixen': ['/m/vixen'], 'Tushy': ['/m/tushy'], 'Men': ['/m/men']})
        m.db.save_library_groups({'Straight': {'libraries': ['Vixen', 'Network'], 'parent': None},
                                  'Network': {'libraries': ['Tushy'], 'parent': 'Straight'},
                                  'Gay': {'libraries': ['Men']}})
        m.db.save_library_order(['Gay', 'Solo', 'Straight', 'Network', 'Gone'])

    def test_groups_keep_their_place_after_a_restart(self):
        m = model.LibraryModel()                      # what start-up does
        self.assertEqual(m.library_order[:3], ['Gay', 'Solo', 'Straight'])
        self.assertNotIn('Network', m.library_order)  # a sub-group never sits at the top level
        self.assertNotIn('Gone', m.library_order)     # names that no longer exist go
        m.save()                                      # any later save keeps the groups
        saved = json.loads(m.db.get_connection().execute(
            "SELECT value FROM metadata WHERE key = 'library_order'").fetchone()[0])
        self.assertEqual(saved[:3], ['Gay', 'Solo', 'Straight'])


if __name__ == '__main__':
    unittest.main()
