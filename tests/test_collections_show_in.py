"""
Where a smart collection is listed (collections_store._shows_in): the libraries / groups picked when
saving, "wherever its videos are", All Videos - and as before for ones saved without a choice;
and the label on its card (source_label).

    docker exec -w /app lustr python3 -m unittest tests.test_collections_show_in -v
"""
import os
import sys
import tempfile
import types
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collections_store as cs                        # noqa: E402

GROUPS = {'G': {'libraries': ['A', 'Sub']}, 'Sub': {'libraries': ['C']}}
LIBS = {'A': ['/a'], 'B': ['/b'], 'C': ['/c']}


def _in_group(name, recursive=True):
    out = []
    for item in GROUPS.get(name, {}).get('libraries', []):
        out += _in_group(item) if item in GROUPS else [item]
    return out


MODEL = types.SimpleNamespace(library_groups=GROUPS, libraries=LIBS, get_libraries_in_group=_in_group)


FOUND = {}                                           # the libraries the collection's videos are in now


def tabs(rules):
    c = {'id': 1, 'kind': 'smart', 'rules': rules}
    return {t for t in ('All Videos', 'Home', 'A', 'B', 'C', 'G', 'Sub') if cs._shows_in(MODEL, None, c, t)}


class ShowIn(unittest.TestCase):
    def setUp(self):
        real = cs.found_libraries
        cs.found_libraries = lambda model, conn, c: dict(FOUND)
        self.addCleanup(setattr, cs, 'found_libraries', real)
        FOUND.clear()

    def test_picked_library(self):
        self.assertEqual(tabs({'library': 'All Videos', 'show_in': ['A'], 'show_in_found': False}), {'A', 'G'})

    def test_picked_group_and_all_videos(self):
        self.assertEqual(tabs({'show_in': ['All Videos', 'Sub'], 'show_in_found': False}), {'All Videos', 'Home', 'Sub', 'G'})

    def test_wherever_its_videos_are(self):
        r = {'library': 'G', 'query': 'tags:x', 'show_in': [], 'show_in_found': True}
        FOUND.update(C=3)
        self.assertEqual(tabs(r), {'C', 'Sub', 'G'})                     # not A: none of its videos are there
        FOUND.update(A=1)                                                # a video moved / got the tag
        self.assertEqual(tabs(r), {'A', 'C', 'Sub', 'G'})
        FOUND.clear()
        self.assertEqual(tabs(r), set())

    def test_old_name_still_read(self):
        FOUND.update(B=2)
        self.assertEqual(tabs({'show_in': [], 'show_in_searched': True}), {'B'})

    def test_saved_before_the_choice(self):
        self.assertEqual(tabs({'library': 'A'}), {'All Videos', 'Home', 'A', 'G'})
        self.assertEqual(tabs({'library': 'All Videos'}), {'All Videos', 'Home'})

    def test_rules_keep_the_choice(self):
        r = cs.clean_rules({'library': 'A', 'show_in': ['B'], 'show_in_found': True, 'junk': 1})
        self.assertEqual((r['show_in'], r['show_in_found']), (['B'], True))
        self.assertNotIn('junk', r)

    def test_cache_cleared_with_home(self):
        import home
        cs._found[('x', '')] = (0, {})
        home.invalidate()
        self.assertEqual(cs._found, {})


class Label(unittest.TestCase):
    # Outer > Inner > (LibA, LibB); Outer > Other
    M = types.SimpleNamespace(library_groups={'Outer': {'libraries': ['Inner', 'Other']},
                                              'Inner': {'libraries': ['LibA', 'LibB']}})

    def test_one_library(self):
        for tab in ('Inner', 'Outer', 'All Videos'):
            self.assertEqual(cs.source_label(self.M, ['LibA'], tab), 'LibA')

    def test_lowest_common_group(self):
        both = ['LibA', 'LibB']
        self.assertEqual(cs.source_label(self.M, both, 'Inner'), 'Multiple libraries')
        self.assertEqual(cs.source_label(self.M, both, 'Outer'), 'Inner')
        self.assertEqual(cs.source_label(self.M, both, 'All Videos'), 'Inner')
        self.assertEqual(cs.source_label(self.M, ['LibA', 'Other'], 'All Videos'), 'Outer')
        self.assertEqual(cs.source_label(self.M, ['LibA', 'Loose'], 'All Videos'), 'Multiple libraries')
        self.assertEqual(cs.source_label(self.M, [], 'All Videos'), '')



class OpenWithFilters(unittest.TestCase):
    """An open smart collection filtered in the sidebar: items() uses those filters, the saved ones stay as they are."""
    def test_override_rules(self):
        saved = {'library': 'A', 'query': 'res:hd', 'tags': 'x', 'show_in': ['A'], 'show_in_found': True}
        seen = []
        patches = {'get': lambda conn, cid: {'id': cid, 'kind': 'smart', 'rules': dict(saved)},
                   '_smart_query': lambda model, req, rules, *a, **k: seen.append(rules) or {'items': [], 'total': 0},
                   'found_libraries': lambda model, conn, c: {}}
        for name, fn in patches.items():
            self.addCleanup(setattr, cs, name, getattr(cs, name))
            setattr(cs, name, fn)
        d = cs.items(MODEL, None, 7, rules={'library': 'A', 'query': 'res:hd stars:4', 'tags': 'x,y', 'show_in': ['B']})
        self.assertEqual((seen[-1]['query'], seen[-1]['tags']), ('res:hd stars:4', 'x,y'))
        self.assertEqual(seen[-1]['show_in'], ['A'])                     # where it shows isn't the sidebar's to change
        self.assertTrue(d['collection']['filtered'])
        cs.items(MODEL, None, 7)
        self.assertEqual(seen[-1]['query'], 'res:hd')                   # nothing was saved


if __name__ == '__main__':
    unittest.main()
