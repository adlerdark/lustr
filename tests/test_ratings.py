"""
Ratings in half stars (0.5 - 5): the one-time move from a 10-point scale, how a rating is stored,
and the Min rating filter (stars:N).

    docker exec -w /app lustr python3 -m unittest tests.test_ratings -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database                                       # noqa: E402
from routes import videos                             # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


class Stored(unittest.TestCase):
    def test_stars(self):
        for given, want in (('4', '4'), (4.24, '4'), ('4.3', '4.5'), ('0', ''), ('', ''), (None, ''), ('x', ''),
                            ('9', '5'), ('-1', ''), ('0.5', '0.5'), ('0.2', '')):
            self.assertEqual(database.stars(given), want, given)


class Migration(Base):
    def rate(self, values):
        for vid, r in zip(('v1', 'v2', 'v3'), values):
            self.c.execute('UPDATE videos SET rating = ? WHERE video_id = ?', (r, vid))
        self.c.execute("DELETE FROM metadata WHERE key = 'rating_scale'")
        self.c.commit()

    def ratings(self):
        return [r[0] for r in self.c.execute("SELECT rating FROM videos WHERE video_id IN ('v1','v2','v3') ORDER BY video_id")]

    def test_ten_point_scale_halved_once(self):
        self.rate(['10.0', '7.5', '8'])
        self.db._migrate_ratings_to_stars(self.c)
        self.assertEqual(self.ratings(), ['5', '4', '4'])               # 3.75 -> nearest half star
        self.db._migrate_ratings_to_stars(self.c)                       # done: not halved again
        self.assertEqual(self.ratings(), ['5', '4', '4'])

    def test_five_point_scale_kept(self):
        self.rate(['3', '4.5', None])
        self.db._migrate_ratings_to_stars(self.c)
        self.assertEqual(self.ratings(), ['3', '4.5', None])


class Filter(Base):
    def ids(self, query):
        from search_parser import SearchParser
        _b, fields, _l = SearchParser.parse_query(query)
        n = videos._pop_rating_filter(fields)
        self.assertNotIn('stars', fields)
        conds, params = [], []
        videos._add_rating_condition(n, conds, params)
        if not conds:
            return None
        return {r[0] for r in self.c.execute(f"SELECT v.video_id FROM videos v WHERE v.video_id IN ('v1','v2','v3') AND {conds[0]}", params)}

    def test_minimum(self):
        for vid, r in (('v1', '5'), ('v2', '3.5'), ('v3', '')):
            self.c.execute('UPDATE videos SET rating = ? WHERE video_id = ?', (r, vid))
        self.assertEqual(self.ids('stars:4'), {'v1'})
        self.assertEqual(self.ids('holiday stars:3.5'), {'v1', 'v2'})
        self.assertIsNone(self.ids('stars:0'))
        self.assertIsNone(self.ids('stars:nope'))


if __name__ == '__main__':
    unittest.main()
