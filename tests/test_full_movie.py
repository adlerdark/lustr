"""
Whole movie <-> the full.movie tag: each sets the other when it first happens.

    docker exec -w /app lustr python3 -m unittest tests.test_full_movie -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import movies                                         # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


class FullMovie(Base):
    def setUp(self):
        super().setUp()
        movies._schema_ready.clear()
        movies.ensure_schema(self.c)
        movies.set_video_movie(self.c, 'v1', 'Road Trip')

    def whole(self):
        return self.c.execute("SELECT whole FROM video_movie WHERE video_id = 'v1'").fetchone()[0]

    def test_whole_adds_the_tag(self):
        self.assertFalse(movies.has_full_movie_tag(self.c, 'v1'))
        movies.apply_updates(self.c, 'v1', {'movie_whole': '1'})          # Suggest metadata: "I have the full movie"
        self.assertEqual(self.whole(), 1)
        self.assertTrue(movies.has_full_movie_tag(self.c, 'v1'))

    def test_tag_added_later_makes_it_whole(self):
        self.assertFalse(movies.on_full_movie_tagged(self.c, 'v1'))       # no tag: nothing changes
        movies.add_full_movie_tag(self.c, 'v1')
        self.assertTrue(movies.on_full_movie_tagged(self.c, 'v1'))
        self.assertEqual(self.whole(), 1)

    def test_tag_without_a_movie_changes_nothing(self):
        movies.add_full_movie_tag(self.c, 'v2')
        self.assertFalse(movies.on_full_movie_tagged(self.c, 'v2'))


if __name__ == '__main__':
    unittest.main()
