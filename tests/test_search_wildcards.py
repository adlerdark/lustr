"""
Search wildcards: cast:jane*, site:*tube, tags:(out*,pool), path:clips/*2023 - in the parser, the SQL paths
(routes/videos.py) and the Python path (SearchParser.matches_field_filters). Also path: in the SQL paths, which
used to put the whole value list into the LIKE pattern and so never matched.

    docker exec -w /app lustr python3 -m unittest tests.test_search_wildcards -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search_parser import SearchParser as SP          # noqa: E402
from routes import videos                             # noqa: E402


class Parse(unittest.TestCase):
    def test_bare_wildcards_are_kept(self):
        self.assertEqual(SP.parse_query('cast:Jane*')[1]['cast'], ['Jane*'])
        base, ff, _ = SP.parse_query('site:*tube beach')
        self.assertEqual(ff['site'], ['*tube'])
        self.assertEqual(base, 'beach')
        self.assertEqual(SP.parse_query('site:%tube%')[1]['site'], ['%tube%'])

    def test_unchanged_forms(self):
        self.assertEqual(SP.parse_query('cast:"Jane Doe"')[1]['cast'], [('EXACT', 'Jane Doe')])
        self.assertEqual(SP.parse_query('tags:(out*,pool)')[1]['tags'], ['out*', 'pool'])
        self.assertEqual(SP.parse_query('res:hd')[1]['res'], ['hd'])


class PythonMatch(unittest.TestCase):
    def test_wildcard_is_whole_value(self):
        self.assertTrue(SP.wildcard_match('jane*', 'Jane Doe'))
        self.assertFalse(SP.wildcard_match('jane*', 'Mary Jane'))
        self.assertTrue(SP.wildcard_match('*jane*', 'Mary Jane'))
        self.assertTrue(SP.wildcard_match('*tube', 'ExampleTube'))
        self.assertFalse(SP.wildcard_match('*tube', 'Tube Site'))
        self.assertTrue(SP.wildcard_match('a.b*', 'a.bc'))
        self.assertFalse(SP.wildcard_match('a.b*', 'axbc'))      # . is literal

    def test_field_filters(self):
        data = {'cast': ['Jane Doe (2)', 'Ann Roe'], 'site': ['ExampleTube'], 'tags': ['Outdoor']}
        self.assertTrue(SP.matches_field_filters(data, {'cast': ['jane*']}))
        self.assertTrue(SP.matches_field_filters(data, {'cast': ['*doe']}))   # the base name of 'Jane Doe (2)'
        self.assertFalse(SP.matches_field_filters(data, {'cast': ['kim*']}))
        self.assertTrue(SP.matches_field_filters(data, {'site': ['*tube']}))
        self.assertFalse(SP.matches_field_filters(data, {'site': ['tube*']}))
        self.assertTrue(SP.matches_field_filters(data, {'tags': ['out*']}))


class SQL(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.execute('CREATE TABLE t (value TEXT)')
        self.c.executemany('INSERT INTO t VALUES (?)', [(v,) for v in
                           ['Jane Doe', 'Jane Doe (1)', 'Mary Jane', 'ExampleTube', 'Tube Site', 'snake_case', 'snakeXcase']])

    def q(self, sql, params):
        return sorted(r[0] for r in self.c.execute(f'SELECT value FROM t WHERE {sql}', params))

    def test_like_pattern(self):
        self.assertIsNone(videos._like_pattern('jane'))
        self.assertEqual(videos._like_pattern('jane*'), 'jane%')
        self.assertEqual(videos._like_pattern('snake_*'), 'snake\\_%')

    def test_value_match(self):
        sql, p = videos._value_match_sql('value', ['*tube'])
        self.assertEqual(self.q(sql, p), ['ExampleTube'])
        sql, p = videos._value_match_sql('value', ['mary jane', 'example*'])
        self.assertEqual(self.q(sql, p), ['ExampleTube', 'Mary Jane'])
        sql, p = videos._value_match_sql('value', ['snake_*'])            # _ is not a wildcard
        self.assertEqual(self.q(sql, p), ['snake_case'])
        sql, p = videos._value_match_sql('value', [('EXACT', 'Jane Doe')])
        self.assertEqual(self.q(sql, p), ['Jane Doe'])

    def test_cast(self):
        sql, p = videos._cast_match_sql(['jane*'])
        self.assertEqual(self.q(sql.replace('vc.value', 'value'), p), ['Jane Doe', 'Jane Doe (1)'])
        sql, p = videos._cast_match_sql(['*doe'])
        self.assertEqual(self.q(sql.replace('vc.value', 'value'), p), ['Jane Doe', 'Jane Doe (1)'])
        sql, p = videos._cast_match_sql(['jane doe'])                      # unchanged: name + numbered
        self.assertEqual(self.q(sql.replace('vc.value', 'value'), p), ['Jane Doe', 'Jane Doe (1)'])

    def test_path(self):
        c = sqlite3.connect(':memory:')
        c.execute('CREATE TABLE fv (path TEXT)')
        c.executemany('INSERT INTO fv VALUES (?)', [('/media/clips/2023/a.mp4',), ('/media/clips/x2023y/b.mp4',),
                                                    ('/media/movies/2021/c.mp4',)])
        get = lambda vals: sorted(r[0] for r in c.execute('SELECT path FROM fv fv WHERE ' + videos._path_match_sql(vals)[0],
                                                           videos._path_match_sql(vals)[1]))
        self.assertEqual(len(get(['2023'])), 2)                             # was: never matched (list in the pattern)
        self.assertEqual(get(['movies', 'nothing']), ['/media/movies/2021/c.mp4'])
        self.assertEqual(get(['clips/2023/*']), ['/media/clips/2023/a.mp4'])
        self.assertEqual(get(['MEDIA/CLIPS/X*']), ['/media/clips/x2023y/b.mp4'])


if __name__ == '__main__':
    unittest.main()
