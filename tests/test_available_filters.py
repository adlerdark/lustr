"""
Sidebar Tags / Sites counts (/api/available_filters).

    docker exec -w /app lustr python3 -m unittest tests.test_available_filters -v
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


class AvailableFiltersTests(unittest.TestCase):
    def setUp(self):
        import routes.videos as rv
        self.rv = rv
        self.db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        rv._model = types.SimpleNamespace(db=self.db, libraries={}, library_groups={}, library_settings={})
        rv.clear_filter_caches()
        # v1: blonde in Tags *and* Hair (counts once), v2: brunette, v3: blonde, other site
        for name, meta in (('v1', {'site': ['Alpha'], 'tags': ['Blonde'], 'hair': ['Blonde']}),
                           ('v2', {'site': ['Alpha'], 'hair': ['Brunette']}),
                           ('v3', {'site': ['Beta'], 'tags': ['Blonde']})):
            self.db.add_file(f'/m/{name}.mkv', f'{name}.mkv', meta)

    def _get(self, **kw):
        args = dict(library='All Videos', query='', tags=None, tags_exclude=None, sites=None,
                    sites_exclude=None, favorites_only=False, length_filter=None)
        args.update(kw)
        return self.rv.get_available_filters_cached(**args)

    def test_no_filters(self):
        r = self._get()
        self.assertEqual(r['tags'], {'blonde': 2, 'brunette': 1})       # v1 once, though it's in two fields
        self.assertEqual(r['sites'], {'alpha': 2, 'beta': 1})

    def test_include_site(self):
        r = self._get(sites='Alpha')
        self.assertEqual(r['sites'], {'alpha': 2, 'beta': 0})
        self.assertEqual(r['tags'], {'blonde': 1, 'brunette': 1})

    def test_exclude_site(self):                                         # was a NameError (500)
        r = self._get(sites_exclude='Alpha')
        self.assertEqual(r['sites'], {'alpha': 0, 'beta': 1})
        self.assertEqual(r['tags'], {'blonde': 1, 'brunette': 0})

    def test_include_and_exclude_tags(self):
        self.assertEqual(self._get(tags='blonde')['sites'], {'alpha': 1, 'beta': 1})
        r = self._get(tags_exclude='blonde')
        self.assertEqual(r['sites'], {'alpha': 1, 'beta': 0})
        self.assertEqual(r['tags'], {'blonde': 0, 'brunette': 1})

    def test_scope_cache_cleared(self):
        self._get()
        self.db.add_file('/m/v4.mkv', 'v4.mkv', {'site': ['Gamma']})
        self.rv.clear_filter_caches()
        self.assertEqual(self._get()['sites'].get('gamma'), 1)


if __name__ == '__main__':
    unittest.main()
