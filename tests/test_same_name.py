"""
Same-name performers: 'Jane Doe (1)' (the main one), 'Jane Doe (2)', ...

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cast_names                                     # noqa: E402
import ext_db                                         # noqa: E402
from search_parser import SearchParser                # noqa: E402
from routes.videos import _cast_match_sql             # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


class SplitTests(Base):
    def setUp(self):
        super().setUp()
        self.tag('v1', 'Jane Doe')
        self.tag('v2', 'Jane Doe')
        self.tag('v3', 'Jane Doe')
        jd = self.c.execute("SELECT id FROM video_cast WHERE value = 'Jane Doe'").fetchone()[0]
        self.c.execute("INSERT INTO video_cast_role (video_id, cast_id) VALUES ('v3', ?)", (jd,))
        self.c.execute("INSERT INTO cast_gender (cast_name, gender) VALUES ('Jane Doe', 'female')")
        self.c.execute("INSERT INTO cast_ext_link (cast_name, ext_source, ext_id) VALUES ('Jane Doe', 'stashdb', 'x')")
        self.c.execute("INSERT INTO cast_aliases (primary_name, alias_name) VALUES ('Jane Doe', 'JD')")
        self.c.commit()

    def test_split(self):
        r = cast_names.split_performer(self.c, 'Jane Doe', ['v3'], ['ext_link'])
        self.c.commit()
        self.assertEqual(r, {'kept': 'Jane Doe (1)', 'new': 'Jane Doe (2)', 'videos': 1})
        self.assertEqual(self.cast_of('v1'), ['Jane Doe (1)'])
        self.assertEqual(self.cast_of('v3'), ['Jane Doe (2)'])
        two = self.c.execute("SELECT id FROM video_cast WHERE value = 'Jane Doe (2)'").fetchone()[0]
        self.assertEqual(self.c.execute('SELECT cast_id FROM video_cast_role').fetchone()[0], two)      # Male Cast moved
        self.assertEqual(self.c.execute('SELECT cast_name FROM cast_ext_link').fetchone()[0], 'Jane Doe (2)')   # chosen part
        self.assertEqual(self.c.execute('SELECT cast_name FROM cast_gender').fetchone()[0], 'Jane Doe (1)')     # the rest stays
        self.assertEqual(self.c.execute("SELECT primary_name FROM cast_aliases WHERE alias_name = 'JD'").fetchone()[0],
                         'Jane Doe (1)')
        r = cast_names.split_performer(self.c, 'Jane Doe (1)', ['v2'])
        self.assertEqual(r['new'], 'Jane Doe (3)')
        self.assertEqual(cast_names.siblings(self.c, 'Jane Doe'), ['Jane Doe (1)', 'Jane Doe (2)', 'Jane Doe (3)'])

    def test_make_main(self):
        cast_names.split_performer(self.c, 'Jane Doe', ['v3'])
        self.assertEqual(cast_names.make_main(self.c, 'Jane Doe (2)'), 'Jane Doe (1)')
        self.c.commit()
        self.assertEqual(self.cast_of('v3'), ['Jane Doe (1)'])
        self.assertEqual(self.cast_of('v1'), ['Jane Doe (2)'])
        self.assertEqual(self.c.execute('SELECT cast_name FROM cast_gender').fetchone()[0], 'Jane Doe (2)')   # profile travels

    def test_search(self):
        cast_names.split_performer(self.c, 'Jane Doe', ['v3'])
        self.c.commit()

        def sql(name):
            cond, params = _cast_match_sql([name.lower()])
            return sorted(r[0] for r in self.c.execute(
                f'SELECT j.video_id FROM video_cast_junction j JOIN video_cast vc ON vc.id = j.cast_id WHERE {cond}', params))
        self.assertEqual(sql('Jane Doe'), ['v1', 'v2', 'v3'])
        self.assertEqual(sql('Jane Doe (2)'), ['v3'])
        self.assertEqual(sql('Jane'), [])                         # no partial matches in SQL
        p = SearchParser()
        self.assertTrue(p.matches_field_filters({'cast': ['Jane Doe (2)']}, {'cast': [('EXACT', 'Jane Doe')]}))
        self.assertFalse(p.matches_field_filters({'cast': ['Jane Doe (1)']}, {'cast': [('EXACT', 'Jane Doe (2)')]}))

    def test_lookup_and_parser_index(self):
        cast_names.split_performer(self.c, 'Jane Doe', ['v3'])
        self.assertEqual(ext_db._cast_index(self.c)[cast_names.norm('Jane Doe')], 'Jane Doe (1)')
        self.assertEqual(cast_names.canonical(self.c, 'jane doe'), 'Jane Doe (1)')


if __name__ == '__main__':
    unittest.main()
