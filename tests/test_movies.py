"""
Full movies: reading the movie out of titles, a video's movie (set / change / clear), merges and
splits, Find Movies, the Movie column / movie: search, and ThePornDB movie links.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import types
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import movies                                         # noqa: E402
from tests.test_cast_names import Base                # noqa: E402

A, B, C = '/media/Lib/a.mp4', '/media/Lib/b.mp4', '/media/Lib/c.mp4'


class ParseTests(unittest.TestCase):
    def p(self, title, fname=''):
        r = movies.parse_video(title, fname)
        return r and (r['movie'], r['scene'], r['part'], r['sure'])

    def test_scene_forms(self):
        self.assertEqual(self.p('Summer Road Trip 13 Scene 1'), ('Summer Road Trip 13', 1, None, True))
        self.assertEqual(self.p('Sun & Sand, Scene 4'), ('Sun & Sand', 4, None, True))
        self.assertEqual(self.p('Beach Days 24 Scene 03'), ('Beach Days 24', 3, None, True))
        self.assertEqual(self.p('Lost In The City 7 Scene2'), ('Lost In The City 7', 2, None, True))
        self.assertEqual(self.p('Road Show 4: Jane Doe - Scene 2'), ('Road Show 4: Jane Doe', 2, None, True))
        self.assertEqual(self.p('Velocity - Part 1, Scene 2'), ('Velocity - Part 1', 2, None, True))

    def test_not_sure_forms(self):
        self.assertEqual(self.p('River S03'), ('River', 3, None, False))
        self.assertEqual(self.p('Heading North Part 1 720p'), ('Heading North', None, 1, False))
        self.assertEqual(self.p('Studio X - Harbor (Disc 1)'), ('Studio X - Harbor', None, 1, False))
        self.assertEqual(self.p('Skatepark Scene1 720p 3800'), ('Skatepark', 1, None, False))
        self.assertEqual(self.p('Big Movie Scene 3 Part 2'), ('Big Movie', 3, 2, True))

    def test_names_and_filenames(self):
        self.assertEqual(self.p('BIG CITY 8 - SCENE 2'), ('Big City 8', 2, None, True))
        self.assertEqual(self.p('JaneDoeGetsDP_s01')[0], 'Jane Doe Gets DP')
        self.assertEqual(self.p('', 'Jane Doe - ANightAtTheDPOverdose_s02_JaneDoe')[0], 'A Night At The DP Overdose')
        self.assertEqual(self.p('', 'Jane Doe - Some_Movie_2_Scene_3'), ('Some Movie 2', 3, None, False))
        self.assertEqual(self.p('Harbor', 'Jane Doe - Harbor_s02_JaneDoe_720p'), ('Harbor', 2, None, False))

    def test_not_movies(self):
        for t in ('SCENE 1 - JANE DOE, JOHN ROE', 'Jane Doe solo', 'Obscene 2', ''):
            self.assertIsNone(movies.parse_movie(t), t)

    def test_title_key(self):
        self.assertEqual(movies.title_key("Jane's Diary 5"), movies.title_key('jane-s diary 5'))
        self.assertEqual(movies.title_key('Night Shift 02'), movies.title_key('Night Shift 2'))
        self.assertNotEqual(movies.title_key('Season 18 11'), movies.title_key('Season 18 1'))


class MovieBase(Base):
    def setUp(self):
        super().setUp()
        for vid, path in (('v1', A), ('v2', B), ('v3', C)):
            self.c.execute("INSERT INTO file_versions (path, video_id, filename, is_preferred, extension, codec) VALUES (?, ?, ?, 1, '.mp4', 'h264')",
                           (path, vid, os.path.basename(path)))
        self.c.commit()
        movies._schema_ready.clear()
        movies.ensure_schema(self.c)
        self.model = types.SimpleNamespace(db=self.db, library_groups={}, libraries={'Lib': ['/media/Lib']},
                                           get_libraries_in_group=lambda g, recursive=False: [])

    def movie_of(self, vid):
        r = self.c.execute('SELECT m.title, vm.scene, vm.part, vm.whole FROM video_movie vm JOIN movies m ON m.id = vm.movie_id '
                           'WHERE vm.video_id = ?', (vid,)).fetchone()
        return tuple(r) if r else None

    def n_movies(self):
        return self.c.execute('SELECT COUNT(*) FROM movies').fetchone()[0]


class VideoMovieTests(MovieBase):
    def test_set_change_clear(self):
        movies.set_video_movie(self.c, 'v1', 'Summer Road Trip 13', scene=2)
        movies.set_video_movie(self.c, 'v2', 'summer road trip 13', scene='3')      # same movie, by name
        self.assertEqual(self.movie_of('v2'), ('Summer Road Trip 13', 3, None, 0))
        self.assertEqual(self.n_movies(), 1)
        movies.set_video_movie(self.c, 'v2', scene='')                      # numbers only: same movie
        self.assertEqual(self.movie_of('v2'), ('Summer Road Trip 13', None, None, 0))
        movies.set_video_movie(self.c, 'v1', 'Other Movie')                 # moved: keeps its scene number
        self.assertEqual(self.movie_of('v1'), ('Other Movie', 2, None, 0))
        movies.set_video_movie(self.c, 'v2', '')
        self.assertIsNone(self.movie_of('v2'))
        self.assertEqual(self.n_movies(), 1)                                 # the empty one went
        movies.set_video_movie(self.c, 'v3', scene=4)                       # numbers without a movie: nothing
        self.assertIsNone(self.movie_of('v3'))

    def test_empty_linked_movie_is_kept(self):
        mid = movies.set_video_movie(self.c, 'v1', 'Kept')
        self.c.execute("UPDATE movies SET ext_source = 'tpdb', ext_id = 'x1' WHERE id = ?", (mid,))
        movies.set_video_movie(self.c, 'v1', '')
        self.assertEqual(self.n_movies(), 1)

    def test_edit_metadata_fields(self):
        movies.apply_updates(self.c, 'v1', {'movie': 'Full DVD', 'movie_whole': '1', 'title': 'ignored'})
        self.c.commit()
        rec = self.db.get_file(A)
        self.assertEqual((rec['movie'], rec['movie_scene'], rec['movie_part'], rec['movie_whole']), ('Full DVD', '', '', '1'))
        self.assertEqual(self.db.get_file(B)['movie'], '')
        bulk = self.db.get_files_bulk([A])
        self.assertEqual(bulk[A]['movie'], 'Full DVD')

    def test_merge_and_split(self):
        from routes import videos as rv
        movies.set_video_movie(self.c, 'v1', 'Movie', scene=1)
        self.c.execute("INSERT INTO videos (video_id, filename_base, library) VALUES ('m', 'm', 'Lib')")
        rv._merge_move_rows(self.c, 'm', ['v1', 'v2'])
        self.assertEqual(self.movie_of('m'), ('Movie', 1, None, 0))
        self.assertIsNone(self.movie_of('v1'))
        movies.copy_movie(self.c, 'm', 'v3')                                # split: each new video gets it
        self.assertEqual(self.movie_of('v3'), ('Movie', 1, None, 0))

    def test_prune_after_delete(self):
        movies.set_video_movie(self.c, 'v1', 'Gone')
        self.c.execute("DELETE FROM videos WHERE video_id = 'v1'")
        self.c.commit()
        self.assertEqual(movies.prune(self.c), (1, 1))

    def test_sort_and_filter(self):
        from routes import videos as rv
        movies.set_video_movie(self.c, 'v1', 'Beta', scene=10)
        movies.set_video_movie(self.c, 'v2', 'Beta', scene=2)
        movies.set_video_movie(self.c, 'v3', 'Alpha', whole=1)
        rows = [{'video_id': v} for v in ('v1', 'v2', 'v3')]
        rv._prepare_sort(self.c, rows, 'movie')
        rows.sort(key=rv._sort_key('movie'))
        self.assertEqual([r['video_id'] for r in rows], ['v3', 'v2', 'v1'])
        for q, want in (([('EXACT', 'beta')], {'v1', 'v2'}), (['alp*'], {'v3'}), (['no'], set()), (['yes'], {'v1', 'v2', 'v3'})):
            sql, params = movies.filter_sql(q)
            got = {r[0] for r in self.c.execute(f'SELECT v.video_id FROM videos v WHERE {sql}', params)}
            self.assertEqual(got, want, q)


class MovieTests(MovieBase):
    def test_rename_onto_another_merges_on_request(self):
        a = movies.set_video_movie(self.c, 'v1', 'Movie One')
        b = movies.set_video_movie(self.c, 'v2', 'Movie 1')
        self.c.execute("UPDATE movies SET year = '2001' WHERE id = ?", (a,))
        out = movies.update(self.c, b, {'title': 'movie one'})
        self.assertEqual(out['conflict']['id'], a)
        out = movies.update(self.c, b, {'title': 'movie one'}, merge=True)
        self.assertEqual(out['merged_into'], a)
        self.assertEqual(self.movie_of('v2')[0], 'Movie One')
        self.assertEqual(movies.update(self.c, a, {'studio': 'Some Studio'})['movie']['studio'], 'Some Studio')

    def test_delete_keeps_videos(self):
        mid = movies.set_video_movie(self.c, 'v1', 'Movie')
        movies.delete(self.c, mid)
        self.assertIsNone(self.movie_of('v1'))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM videos WHERE video_id = 'v1'").fetchone()[0], 1)

    def test_listing_and_detail(self):
        movies.set_video_movie(self.c, 'v2', 'Movie', scene=2)
        movies.set_video_movie(self.c, 'v1', 'Movie', scene=1)
        self.model.db.get_file = self.db.get_file
        d = movies.listing(self.model, self.c, 'Lib')
        self.assertEqual([(m['title'], m['count']) for m in d['movies']], [('Movie', 2)])
        self.assertEqual(movies.listing(self.model, self.c, 'Other')['total'], 0)
        det = movies.detail(self.model, self.c, d['movies'][0]['id'])
        self.assertEqual([i['path'] for i in det['items']], [A, B])


class FindTests(MovieBase):
    def setUp(self):
        super().setUp()
        for vid, title in (('v1', 'Summer Road Trip 13 Scene 1'), ('v2', 'summer road trip 13 scene 2'), ('v3', 'River S03')):
            self.c.execute('UPDATE videos SET title = ? WHERE video_id = ?', (title, vid))
        self.c.commit()

    def test_groups(self):
        g = {x['key']: x for x in movies.find(self.model, self.c, 'Lib')}
        self.assertEqual(g['summerroadtrip13']['name'], 'Summer Road Trip 13')
        self.assertTrue(g['summerroadtrip13']['sure'])
        self.assertEqual([v['scene'] for v in g['summerroadtrip13']['videos']], [1, 2])
        self.assertFalse(g['river']['sure'])

    def test_apply_skips_videos_with_a_movie(self):
        movies.set_video_movie(self.c, 'v2', 'Something Else')
        groups = movies.find(self.model, self.c, 'Lib')
        self.assertEqual([len(x['videos']) for x in groups if x['key'] == 'summerroadtrip13'], [1])
        stale = [{'name': 'Summer Road Trip 13', 'videos': [{'video_id': 'v1', 'scene': 1}, {'video_id': 'v2', 'scene': 2}]}]
        self.assertEqual(movies.apply_found(self.c, stale), (1, 1))
        self.assertEqual(self.movie_of('v1'), ('Summer Road Trip 13', 1, None, 0))
        self.assertEqual(self.movie_of('v2')[0], 'Something Else')
        self.assertFalse([x for x in movies.find(self.model, self.c, 'Lib') if x['key'] == 'summerroadtrip13'])

    def test_whole_library(self):
        self.c.execute("UPDATE videos SET title = 'A Full Movie' WHERE video_id = 'v3'")
        g = {x['key']: x for x in movies.find(self.model, self.c, 'Lib', whole=True)}
        self.assertTrue(g['afullmovie']['videos'][0]['whole'])
        self.assertTrue(g['summerroadtrip13']['sure'])

    def test_lone_release_names_are_left_out(self):
        self.c.execute("UPDATE videos SET title = '', filename_base = 'Jane Doe - JaneDoe_s01_JohnRoe_1080p' WHERE video_id = 'v3'")
        self.assertFalse([x for x in movies.find(self.model, self.c, 'Lib') if x['key'] == 'janedoe'])


class ExtTests(MovieBase):
    def test_movie_scene_link_sets_the_movie(self):
        raw = {'id': 'tp-1', 'title': 'Big Movie 2', 'date': '2014-07-14', 'site': {'name': 'Studio'}, 'description': 'About it'}
        mid = movies.on_video_linked(self.c, 'v1', 'movie', raw, 3, (b'img', 'image/jpeg'))
        self.assertEqual(self.movie_of('v1'), ('Big Movie 2', 3, None, 0))
        m = movies.get(self.c, mid)
        self.assertEqual((m['ext_id'], m['year'], m['studio'], m['has_cover']), ('tp-1', '2014', 'Studio', True))
        movies.set_video_movie(self.c, 'v2', 'My Own Name')                 # a choice you made is kept
        movies.on_video_linked(self.c, 'v2', 'movie', raw, 4)
        self.assertEqual(self.movie_of('v2')[0], 'My Own Name')

    def test_scene_that_names_its_movie(self):
        self.c.execute("UPDATE videos SET title = 'Summer House Scene 2' WHERE video_id = 'v1'")
        raw = {'id': 's-9', 'movies': [{'id': 'tp-7', 'title': 'Summer House', 'date': '2010-01-01'}]}
        movies.on_video_linked(self.c, 'v1', 'scene', raw)
        self.assertEqual(self.movie_of('v1'), ('Summer House', 2, None, 0))

    def test_find_uses_links_and_links_the_movie(self):
        self.c.execute("INSERT INTO video_ext_link (video_id, ext_source, ext_id, ext_kind, ext_movie_title, ext_scene_number, ext_raw_json) "
                       "VALUES ('v1', 'tpdb', 'tp-3', 'movie', 'Linked Movie', 5, ?)",
                       (json.dumps({'id': 'tp-3', 'title': 'Linked Movie', 'date': '2003-02-01'}),))
        self.c.commit()
        g = [x for x in movies.find(self.model, self.c, 'Lib') if x['key'] == 'linkedmovie'][0]
        self.assertEqual((g['videos'][0]['scene'], g['videos'][0]['source']), (5, 'ext'))
        movies.apply_found(self.c, [g])
        m = movies.get(self.c, movies.find_movie(self.c, 'Linked Movie'))
        self.assertEqual((m['ext_id'], m['year']), ('tp-3', '2003'))


if __name__ == '__main__':
    unittest.main()
