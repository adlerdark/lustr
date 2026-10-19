"""
Play History: the watch log (one entry per play), plays / time per video, deleting entries,
the Plays / Time Watched columns and sorts.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ.setdefault('LUSTR_DATA_DIR', _DATA)        # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import library_stats as ls                            # noqa: E402
from tests.test_cast_names import Base                # noqa: E402

A, B, C = '/media/Lib/a.mp4', '/media/Lib/b.mp4', '/media/Lib/c.mp4'


class PlayHistoryBase(Base):
    def setUp(self):
        super().setUp()
        for vid, path in (('v1', A), ('v2', B), ('v3', C)):
            self.c.execute('INSERT INTO file_versions (path, video_id, filename, is_preferred) VALUES (?, ?, ?, 1)',
                           (path, vid, os.path.basename(path)))
        self.c.execute("UPDATE videos SET title = 'Alpha' WHERE video_id = 'v1'")
        self.c.commit()
        self.tag('v1', 'Jane Doe')
        ls._sessions.clear()
        ls._first_seen.clear()
        ls._min_cache.clear()
        ls._schema_ready.clear()
        ls.ensure_schema(self.c)

    def play(self, path, played, play_id, user='alice', t0=1000.0, duration=600, step=10):
        """Progress posts every `step` s of real playback, then one at exactly `played` s. -> counted?"""
        counted, k = False, step
        while k < played:
            counted |= ls.record_progress(self.c, user, path, k, duration, now=t0 + k, play_id=play_id, played=k)
            k += step
        return ls.record_progress(self.c, user, path, played, duration, now=t0 + played,
                                  play_id=play_id, played=played) or counted

    def log(self, user='alice'):
        return [tuple(r) for r in self.c.execute('SELECT path, played, position, plays, imported FROM play_log '
                                                 'WHERE username = ? ORDER BY started_at, id', (user,))]

    def stats(self, vid, user='alice'):
        r = self.c.execute('SELECT plays, total_played, last_played FROM watch_stats WHERE username = ? AND video_id = ?',
                           (user, vid)).fetchone()
        return tuple(r) if r else None


class LoggingTests(PlayHistoryBase):
    def test_a_b_a_gives_three_entries(self):
        ls.set_min_log_seconds(self.c, 0)
        self.assertTrue(self.play(A, 180, 'p1', t0=1000))
        self.assertFalse(self.play(B, 30, 'p2', t0=1200))
        self.assertFalse(self.play(A, 5, 'p3', t0=1300))
        self.assertEqual([(r[0], r[1], r[3]) for r in self.log()], [(A, 180, 1), (B, 30, 0), (A, 5, 0)])
        self.assertEqual(self.stats('v1')[:2], (1, 185))

    def test_default_minimum_is_15s(self):
        self.assertEqual(ls.min_log_seconds(self.c), 15)
        self.play(A, 180, 'p1')
        self.play(B, 10, 'p2', t0=1200)              # too short: not logged, not in the totals
        self.play(B, 16, 'p3', t0=1300)
        self.assertEqual([(r[0], r[1]) for r in self.log()], [(A, 180), (B, 16)])
        self.assertEqual(self.stats('v2')[:2], (0, 16))

    def test_play_length_is_not_the_playhead(self):
        ls.record_progress(self.c, 'alice', A, 20, 600, now=1020, play_id='p1', played=20)
        ls.record_progress(self.c, 'alice', A, 600, 600, now=1040, play_id='p1', played=30)   # skipped to the end
        self.assertEqual(self.log(), [(A, 30, 600, 0, 0)])

    def test_same_play_id_updates_one_entry(self):
        for k in range(1, 8):
            ls.record_progress(self.c, 'alice', A, k * 10, 600, now=1000 + k * 10, play_id='p1', played=k * 10)
        ls.record_progress(self.c, 'alice', A, 75, 600, now=1080, play_id='p1', played=50)   # never goes back
        self.assertEqual(self.log(), [(A, 70, 75, 1, 0)])

    def test_posts_without_play_id_still_log(self):
        for k in range(0, 71, 10):                   # an old page: sessions + playhead movement
            ls.record_progress(self.c, 'alice', A, k, 600, now=1000 + k)
        self.assertEqual([(r[0], r[1], r[3]) for r in self.log()], [(A, 70, 1)])

    def test_seed_once(self):
        self.c.execute("INSERT INTO watch_stats (username, video_id, plays, first_played, last_played) "
                       "VALUES ('alice', 'v1', 3, '2026-01-01T10:00:00', '2026-02-01T10:00:00')")
        history = {'alice': {A: {'last_watched': '2026-02-01T10:00:00', 'progress': 120, 'duration': 600},
                             B: {'last_watched': '2026-03-01T10:00:00', 'progress': 20, 'duration': 600}}}
        self.assertEqual(ls.seed_play_log(self.c, history), 2)
        self.assertEqual(ls.seed_play_log(self.c, history), 0)
        self.assertEqual(sorted(self.log()), [(A, None, 120, 3, 1), (B, None, 20, 0, 1)])


class DeleteTests(PlayHistoryBase):
    def ids(self, path):
        return [r[0] for r in self.c.execute('SELECT id FROM play_log WHERE path = ? ORDER BY id', (path,))]

    def test_delete_takes_plays_off(self):
        self.play(A, 70, 'p1', t0=1000)
        self.play(A, 100, 'p2', t0=5000)
        self.assertEqual(self.stats('v1')[:2], (2, 170))
        last = self.stats('v1')[2]
        ls.delete_entries(self.c, 'alice', ids=[self.ids(A)[1]])
        plays, total, last2 = self.stats('v1')
        self.assertEqual((plays, total), (1, 70))
        self.assertLess(last2, last)
        ls.delete_entries(self.c, 'alice', ids=self.ids(A))
        self.assertIsNone(self.stats('v1'))

    def test_clear_range_and_other_users(self):
        self.play(A, 70, 'p1', t0=1000)
        self.play(B, 70, 'p2', t0=1000 + 86400 * 3)
        self.play(A, 70, 'p3', user='bob')
        self.assertEqual(ls.delete_entries(self.c, 'alice', date_from=ls._ts(1000 + 86400 * 2)[:10]), 1)
        self.assertEqual([r[0] for r in self.log()], [A])
        ls.delete_entries(self.c, 'alice')
        self.assertEqual(self.log(), [])
        self.assertEqual(len(self.log('bob')), 1)


class PagesTests(PlayHistoryBase):
    def test_log_and_stats_pages(self):
        self.play(A, 70, 'p1', t0=1000)
        self.play(B, 300, 'p2', t0=2000)
        self.play(C, 70, 'p3', user='bob')
        d = ls.log_page(self.c, 'alice')
        self.assertEqual([i['path'] for i in d['items']], [B, A])          # newest first, only alice
        self.assertEqual(d['summary'], {'entries': 2, 'plays': 2, 'played': 370})
        self.assertEqual(d['items'][1]['cast'], 'Jane Doe')
        self.assertEqual([i['path'] for i in ls.log_page(self.c, 'alice', q='jane')['items']], [A])
        self.assertEqual([i['path'] for i in ls.log_page(self.c, 'alice', sort='played', desc=False)['items']], [A, B])
        self.assertEqual(ls.log_page(self.c, 'alice', sort='x; DROP TABLE play_log')['total'], 2)   # whitelist
        st = ls.video_stats_page(self.c, 'alice', sort='total_played', desc=True)
        self.assertEqual([i['path'] for i in st['items']], [B, A])
        t = ls.video_totals(self.c, 'alice', 'v1')
        self.assertEqual((t['plays'], t['total_played'], t['entries']), (1, 70, 1))

    def test_library_columns_and_sort(self):
        self.play(A, 70, 'p1', t0=1000)
        self.play(A, 70, 'p2', t0=5000)
        self.play(B, 400, 'p3', t0=9000)
        items = [{'video_id': v, 'path': p} for v, p in (('v1', A), ('v2', B), ('v3', C))]
        ls.add_play_stats(self.c, 'alice', items)
        self.assertEqual([(i['plays'], i['time_watched']) for i in items], [(2, 140), (1, 400), (0, 0)])
        self.assertEqual(ls.sort_field('Time Watched'), 'time_watched')
        self.assertEqual(ls.sort_field('Plays'), 'most_played')
        ls.special_sort(self.c, items, 'time_watched', 'alice')
        self.assertEqual([i['path'] for i in items], [B, A, C])


if __name__ == '__main__':
    unittest.main()
