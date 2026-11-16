"""
Site linking on StashDB and ThePornDB (ext_db.py): links and 'not on DB' marks are kept per database,
and scene matching compares a scene's studio with your links on that scene's database.

    docker exec -w /app lustr python3 -m unittest tests.test_site_links -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ext_db                                         # noqa: E402
from tests.test_cast_names import Base                # noqa: E402

TPDB_SITE = {'id': 94, 'uuid': 'u-94', 'name': 'Brazzers Exxtra', 'short_name': 'brazzersexxtra', 'url': 'https://brazzers.com',
             'parent_id': 92, 'network_id': 1298, 'parent': {'id': 92, 'name': 'Brazzers'}, 'network': {'id': 1298, 'name': 'MindGeek'}}


def tpdb_scene(site_id, parent_id=None, network_id=None, name='Some Site'):
    return ext_db._tpdb_compact({'id': 's1', 'title': 'A Scene', 'date': '2024-01-06',
                                 'site': {'id': site_id, 'name': name, 'parent_id': parent_id, 'network_id': network_id}}, 'scene')


def clues(studios):
    return {'video_id': 'v1', 'title': 'A Scene', 'date': None, 'year': None, 'duration': None, 'cast': [],
            'movie_title': None, 'scene_number': None,
            'sites': [{'value': 'my.site', 'site_key': 'mysite', 'status': 'linked', 'studios': studios}]}


class TpdbSites(unittest.TestCase):
    def test_site_looks_like_a_studio(self):
        st = ext_db._tpdb_site(TPDB_SITE)
        self.assertEqual((st['id'], st['parent']), ('94', {'id': '92', 'name': 'Brazzers'}))
        outcome, pick, _ = ext_db.classify_site('brazzersexxtra', [st])          # its short name is an alias
        self.assertEqual((outcome, pick['ext_id']), ('exact', '94'))

    def test_network_used_when_there_is_no_parent(self):
        st = ext_db._tpdb_site(dict(TPDB_SITE, parent=None, parent_id=None))
        self.assertEqual(st['parent']['id'], '1298')


class Links(Base):
    def link(self, source, ext_id, kind='studio', key='mysite'):
        ext_db._save_site_link(self.c, key, source, {'ext_id': ext_id, 'name': 'X' + ext_id}, kind)

    def test_links_and_marks_are_per_database(self):
        self.link('stashdb', 'uuid-1')
        self.c.execute("INSERT INTO site_ext_check (site_key, ext_source, outcome, detail) VALUES ('mysite', 'tpdb', 'not_on_db', '')")
        self.c.commit()
        stash, _ = ext_db._site_rows(self.c, 'stashdb')
        tpdb, _ = ext_db._site_rows(self.c, 'tpdb')
        self.assertEqual(stash['mysite']['status'], 'linked')
        self.assertEqual(tpdb['mysite']['status'], 'not_on_db')
        self.assertEqual(tpdb['mysite']['linked_on'], ['stashdb'])
        self.link('tpdb', '94')                                                    # linking clears that database's mark
        tpdb, _ = ext_db._site_rows(self.c, 'tpdb')
        self.assertEqual([s['ext_id'] for s in tpdb['mysite']['studios']], ['94'])
        both, _ = ext_db._site_rows(self.c)
        self.assertEqual({s['ext_source'] for s in both['mysite']['studios']}, {'stashdb', 'tpdb'})

    def test_old_not_on_db_mark_moves_to_the_default_database(self):
        self.c.execute("INSERT OR REPLACE INTO site_ext_link (site_key, status) VALUES ('oldsite', 'not_on_db')")
        self.c.commit()
        ext_db._schema_ready = False
        ext_db.ensure_schema()
        self.assertEqual(ext_db._site_rows(self.c, 'stashdb')[0]['oldsite']['status'], 'not_on_db')
        self.assertEqual(ext_db._site_rows(self.c, 'tpdb')[0]['oldsite']['status'], 'unlinked')


class CrossLink(Base):
    """Linking a site on one database tries the others, by the linked studio's name first."""
    def setUp(self):
        super().setUp()
        self.saved = (ext_db.check_site, ext_db.api_key)
        self.asked = []
        ext_db.api_key = lambda source: 'key'

        def check(source, value, variants=None):
            self.asked.append((source, value))
            if value == 'Brazzers Exxtra':                      # the StashDB studio's name finds the ThePornDB site
                return 'exact', {'ext_id': '94', 'name': 'Brazzers Exxtra', 'parent': None}, []
            return 'close', None, [{'name': 'Something Else'}]
        ext_db.check_site = check

    def tearDown(self):
        ext_db.check_site, ext_db.api_key = self.saved

    def test_link_on_one_links_the_other(self):
        ext_db._save_site_link(self.c, 'mysite', 'stashdb', {'ext_id': 'uuid-1', 'name': 'Brazzers Exxtra'}, 'studio')
        out = ext_db._cross_link(self.c, 'mysite', 'stashdb', 'studio', ['Brazzers Exxtra'], groups={})
        self.assertEqual(out, [{'source': 'tpdb', 'outcome': 'linked', 'name': 'Brazzers Exxtra'}])
        self.assertEqual(self.asked[0], ('tpdb', 'Brazzers Exxtra'))
        tpdb, _ = ext_db._site_rows(self.c, 'tpdb')
        self.assertEqual(tpdb['mysite']['studios'][0]['ext_id'], '94')

    def test_no_exact_match_is_kept_for_review(self):
        out = ext_db._cross_link(self.c, 'mysite', 'stashdb', 'studio', ['Other Studio'], groups={})
        self.assertEqual(out[0]['outcome'], 'close')
        tpdb, checks = ext_db._site_rows(self.c, 'tpdb')
        self.assertEqual(checks['mysite']['outcome'], 'close')
        self.assertNotIn('mysite', {k for k, v in tpdb.items() if v['status'] == 'linked'})

    def test_marked_not_on_the_other_is_left_alone(self):
        self.c.execute("INSERT INTO site_ext_check (site_key, ext_source, outcome, detail) VALUES ('mysite', 'tpdb', 'not_on_db', '')")
        self.c.commit()
        self.assertEqual(ext_db._cross_link(self.c, 'mysite', 'stashdb', 'studio', ['Brazzers Exxtra'], groups={}), [])
        self.assertEqual(self.asked, [])

    def test_network_link_stays_a_network(self):
        ext_db._cross_link(self.c, 'mysite', 'stashdb', 'network', ['Brazzers Exxtra'], groups={})
        self.assertEqual(ext_db._site_rows(self.c, 'tpdb')[0]['mysite']['studios'][0]['link_kind'], 'network')


class Scoring(unittest.TestCase):
    def score(self, sc, studios):
        return ext_db.score_scene(sc, clues(studios))[1]

    def test_tpdb_site_link_matches_by_id(self):
        linked = [{'ext_source': 'tpdb', 'ext_id': '94', 'ext_name': 'Brazzers Exxtra', 'link_kind': 'studio'}]
        self.assertTrue(any(r.startswith('studio:') for r in self.score(tpdb_scene(94), linked)))
        self.assertTrue(any(r.startswith('different studio') for r in self.score(tpdb_scene(55), linked)))

    def test_tpdb_network_link_covers_its_sites(self):
        linked = [{'ext_source': 'tpdb', 'ext_id': '92', 'ext_name': 'Brazzers', 'link_kind': 'network'}]
        self.assertTrue(any(r.startswith('studio:') for r in self.score(tpdb_scene(94, parent_id=92), linked)))

    def test_stashdb_links_dont_judge_tpdb_ids(self):
        linked = [{'ext_source': 'stashdb', 'ext_id': '94', 'ext_name': 'Other Name', 'link_kind': 'studio'}]
        reasons = self.score(tpdb_scene(94), linked)
        self.assertFalse(any(r.startswith(('studio:', 'different studio')) for r in reasons))
        named = [{'ext_source': 'stashdb', 'ext_id': 'uuid', 'ext_name': 'Some Site', 'link_kind': 'studio'}]
        self.assertTrue(any(r.startswith('studio:') for r in self.score(tpdb_scene(94), named)))     # by name, as before


if __name__ == '__main__':
    unittest.main()
