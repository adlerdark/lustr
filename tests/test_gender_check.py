"""
Gender check tests: which way a performer leans, what gets flagged, where a video's orientation comes from.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest tests.test_gender_check -v
"""
import os
import sys
import tempfile
import types
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ext_db                                         # noqa: E402
import gender_check as gc                             # noqa: E402
import movies                                         # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


def bucket(**counts):
    return {k: [{}] * n for k, n in counts.items()}


def kinds(gender, **counts):
    return [(k, e) for k, e, _ in gc.classify(gender, bucket(**counts))]


class ClassifyTests(unittest.TestCase):
    def test_a_fit_is_not_flagged(self):
        self.assertEqual(kinds('female', straight_cast=9), [])
        self.assertEqual(kinds('male', gay=9), [])
        self.assertEqual(kinds('male', straight_male=9), [])
        self.assertEqual(kinds('female', lesbian=3, straight_cast=3), [])

    def test_female_in_gay_videos(self):
        self.assertEqual(kinds('female', gay=1), [('gender', 'male')])
        self.assertEqual(kinds('female', gay=5, straight_cast=1), [('gender', 'male')])

    def test_bi_videos_alone_prove_nothing(self):
        self.assertEqual(kinds('female', bisexual=9), [])
        self.assertEqual(kinds('male', bisexual=9), [])

    def test_bi_video_is_judged_by_their_other_videos(self):
        self.assertEqual(kinds('female', bisexual=1, straight_cast=20), [])      # a woman in a bi video
        self.assertEqual(kinds('female', bisexual=1, gay=20), [('gender', 'male')])   # mistagged man

    def test_mostly_straight_woman_in_one_gay_video_is_a_one_off(self):
        self.assertEqual(kinds('female', gay=1, straight_cast=9), [('odd', 'female')])

    def test_male_cast_marks(self):
        self.assertEqual(kinds('male', straight_cast=2, straight_male=8), [('unmarked', 'male')])
        self.assertEqual(kinds('female', straight_cast=9, straight_male=1), [('marked', 'female')])
        self.assertEqual(kinds('female', straight_male=8), [('gender', 'male')])

    def test_male_in_lesbian_videos(self):
        self.assertEqual(kinds('male', lesbian=4), [('gender', 'female')])
        self.assertEqual(kinds('male', lesbian=1, gay=9), [('odd', 'male')])

    def test_no_gender(self):
        self.assertEqual(kinds('', gay=3), [('unset', 'male')])
        self.assertEqual(kinds('', straight_cast=30), [])                        # no gender counts as female

    def test_trans_is_left_alone(self):
        self.assertEqual(kinds('trans_female', gay=5, straight_male=5), [])
        self.assertEqual(kinds('trans_male', lesbian=5), [])


class ReportTests(Base):
    def setUp(self):
        super().setUp()
        movies._schema_ready.clear()
        movies.ensure_schema(self.c)
        for vid in ('v1', 'v2', 'v3'):
            self.c.execute("INSERT INTO file_versions (path, video_id, filename, is_preferred, extension, codec) VALUES (?, ?, ?, 1, 'mp4', 'h264')",
                           (f'/media/Lib/{vid}.mp4', vid, vid))
        self.c.commit()
        self.model = types.SimpleNamespace(db=self.db, library_groups={}, libraries={'Lib': ['/media/Lib']},
                                           library_settings={'Lib': {}}, get_libraries_in_group=lambda g, recursive=False: [])

    def orient(self, vid, value):
        row = self.c.execute('SELECT id FROM video_orientation WHERE value = ?', (value,)).fetchone()
        oid = row[0] if row else self.c.execute('INSERT INTO video_orientation (value) VALUES (?)', (value,)).lastrowid
        self.c.execute('INSERT INTO video_orientation_junction (video_id, orientation_id) VALUES (?, ?)', (vid, oid))
        self.c.commit()

    def gender(self, name, g):
        self.db.set_cast_gender(name, g)

    def found(self):
        return {f['name']: f for f in gc.report(self.c, self.model)['findings']}

    def test_tag_beats_movie_beats_library(self):
        self.model.library_settings = {'Lib': {'type': 'straight'}}
        self.assertEqual(gc.video_orientations(self.c, self.model)['v1'], ({'straight'}, 'library'))
        movies.set_video_movie(self.c, 'v1', 'Some Movie')
        self.c.execute("INSERT INTO movie_tags (movie_id, value) VALUES ((SELECT movie_id FROM video_movie WHERE video_id = 'v1'), 'gay')")
        self.assertEqual(gc.video_orientations(self.c, self.model)['v1'], ({'gay'}, 'movie'))
        self.orient('v1', 'Bisexual')
        self.assertEqual(gc.video_orientations(self.c, self.model)['v1'], ({'bisexual'}, 'tag'))

    def test_mixed_library_says_nothing(self):
        self.assertEqual(gc.video_orientations(self.c, self.model)['v1'], (set(), None))
        self.tag('v1', 'Pat')
        self.gender('Pat', 'female')
        self.assertEqual(self.found(), {})

    def test_gay_library_flags_a_woman_and_the_fix_clears_it(self):
        self.model.library_settings = {'Lib': {'type': 'gay'}}
        self.tag('v1', 'Pat')
        self.gender('Pat', 'female')
        f = self.found()['Pat']
        self.assertEqual((f['kind'], f['expected'], f['count'], f['strong']), ('gender', 'male', 1, False))
        self.gender('Pat', 'male')
        self.assertEqual(self.found(), {})

    def test_alias_counts_as_the_main_name_and_can_be_marked(self):
        self.orient('v1', 'Straight')
        self.orient('v2', 'Straight')
        self.tag('v1', 'Bob Alias')
        self.tag('v2', 'Bob')
        self.c.execute("INSERT INTO cast_aliases (alias_name, primary_name) VALUES ('Bob Alias', 'Bob')")
        self.c.commit()
        self.gender('Bob', 'male')
        f = self.found()['Bob']
        self.assertEqual((f['kind'], f['count']), ('unmarked', 2))
        changed = gc.set_marks(self.c, gc.performers(self.c, self.model)['Bob'], True)
        self.assertEqual(changed, 2)
        self.assertEqual(self.found(), {})
        self.assertEqual(ext_db.male_cast_for_video('v1'), {'Bob Alias'})

    def test_remove_a_mark(self):
        self.orient('v1', 'Straight')
        self.tag('v1', 'Pat')
        self.gender('Pat', 'female')
        ext_db.male_cast_change(self.c, ['v1'], ['Pat'], [])
        self.assertEqual(self.found()['Pat']['kind'], 'gender')
        self.orient('v2', 'Straight')
        self.orient('v3', 'Straight')
        self.tag('v2', 'Pat')
        self.tag('v3', 'Pat')
        self.assertEqual(self.found()['Pat']['kind'], 'marked')
        self.assertEqual(gc.set_marks(self.c, gc.performers(self.c, self.model)['Pat'], False), 1)
        self.assertEqual(self.found(), {})

    def test_deleted_files_dont_count(self):
        self.orient('v1', 'Gay')
        self.tag('v1', 'Pat')
        self.gender('Pat', 'female')
        self.c.execute("UPDATE file_versions SET deleted_at = CURRENT_TIMESTAMP WHERE video_id = 'v1'")
        self.assertEqual(self.found(), {})

    def test_ignore_and_bring_back(self):
        self.orient('v1', 'Gay')
        self.tag('v1', 'Zoë')
        self.gender('Zoë', 'female')
        gc.set_ignored(self.c, 'Zoë', 'gender')
        r = gc.report(self.c, self.model)
        self.assertEqual((r['findings'], [f['name'] for f in r['ignored']]), ([], ['Zoë']))
        gc.set_ignored(self.c, 'Zoë', 'gender', False)
        self.assertEqual(list(self.found()), ['Zoë'])


if __name__ == '__main__':
    unittest.main()
