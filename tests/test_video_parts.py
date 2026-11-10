"""
Multi-part videos (cd1, cd2 ...): part numbers and order, part 1 stays the preferred file, the joined
seek-preview VTT, markers moving to their place on the joined timeline, listing items (parts, total length).
    docker exec lustr python -m unittest tests.test_video_parts -v
"""
import os
import sys
import tempfile
import unittest
os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sprites                                        # noqa: E402
import video_parts                                    # noqa: E402
from tests.test_cast_names import Base                # noqa: E402

A, B, C = '/media/Lib/Movie cd1.mp4', '/media/Lib/Movie cd2.mp4', '/media/Lib/Movie cd10.mp4'


class NameTests(unittest.TestCase):
    def test_title_from(self):
        self.assertEqual(video_parts.title_from('/x/Movie (2001) cd1.avi'), 'Movie (2001)')
        self.assertEqual(video_parts.title_from('Movie.Name.Part.2.mkv'), 'Movie.Name')
        self.assertEqual(video_parts.title_from('Movie - Disc 1.mp4'), 'Movie')
        self.assertEqual(video_parts.title_from('Movie [CD2].mp4'), 'Movie')
        self.assertEqual(video_parts.title_from('Plain.mp4'), 'Plain')

    def test_order_is_natural(self):
        self.assertEqual(video_parts.order_paths([C, B, A]), [A, B, C])


class PartsTests(Base):
    def setUp(self):
        super().setUp()
        self.file('v1', A, 100, codec='h264', bitrate='1000 kbps')
        self.file('v1', B, 50, codec='h264', bitrate='9000 kbps')     # the "better" file by codec / bitrate
        self.file('v1', C, 25, codec='hevc', bitrate='500 kbps')

    def file(self, vid, path, secs, codec='h264', bitrate='', cache_id=None):
        self.c.execute('INSERT INTO file_versions (path, video_id, filename, codec, total_bitrate, length, length_seconds, cache_id) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (path, vid, os.path.basename(path), codec, bitrate, video_parts.fmt_length(secs), secs, cache_id))
        self.c.commit()

    def preferred(self, vid):
        return [r[0] for r in self.c.execute('SELECT path FROM file_versions WHERE video_id = ? AND is_preferred = 1', (vid,))]

    def test_set_order_numbers_and_leads_with_part_1(self):
        video_parts.set_order(self.c, 'v1', [A, B, C])
        self.assertEqual([(p['path'], p['part']) for p in video_parts.parts(self.c, 'v1')], [(A, 1), (B, 2), (C, 3)])
        self.assertEqual(self.preferred('v1'), [A])
        video_parts.set_order(self.c, 'v1', [B, A, C])
        self.assertEqual(self.preferred('v1'), [B])
        with self.assertRaises(ValueError):
            video_parts.set_order(self.c, 'v1', [A, '/media/Lib/other.mp4'])

    def test_auto_preferred_keeps_part_1(self):
        video_parts.set_order(self.c, 'v1', [A, B, C])
        self.db.auto_set_preferred('v1')                  # B wins on bitrate, but part 1 leads
        self.assertEqual(self.preferred('v1'), [A])

    def test_normal_video_is_not_multipart(self):
        self.assertEqual(video_parts.parts(self.c, 'v1'), [])
        self.db.auto_set_preferred('v1')
        self.assertEqual(self.preferred('v1'), [B])

    def test_renumber_after_a_part_leaves(self):
        video_parts.set_order(self.c, 'v1', [A, B, C])
        self.c.execute('DELETE FROM file_versions WHERE path = ?', (A,))
        video_parts.renumber(self.c, 'v1')
        self.assertEqual([(p['path'], p['part']) for p in video_parts.parts(self.c, 'v1')], [(B, 1), (C, 2)])
        self.assertEqual(self.preferred('v1'), [B])
        self.c.execute('DELETE FROM file_versions WHERE path = ?', (B,))
        video_parts.renumber(self.c, 'v1')                # one left: a normal file again
        self.assertEqual(video_parts.parts(self.c, 'v1'), [])

    def test_offsets_and_listing_item(self):
        video_parts.set_order(self.c, 'v1', [A, B, C])
        self.assertEqual(video_parts.offset_of(self.c, A), 0)
        self.assertEqual(video_parts.offset_of(self.c, C), 150)
        item = video_parts.annotate_from_db(self.c, {'video_id': 'v1', 'length': '00:01:40', 'versions': [1, 2, 3]})
        self.assertEqual(len(item['parts']), 3)
        self.assertEqual(item['length'], '00:02:55')
        self.assertNotIn('versions', item)
        plain = video_parts.annotate({'video_id': 'v2'}, [{'path': 'x'}, {'path': 'y'}])
        self.assertEqual(len(plain['versions']), 2)

    def test_markers_move_to_their_place_when_joined(self):
        self.c.execute('CREATE TABLE IF NOT EXISTS video_markers (id INTEGER PRIMARY KEY, video_id TEXT, seconds REAL, '
                       'end_seconds REAL, title TEXT, tag TEXT, source TEXT, source_key TEXT)')
        self.c.execute('DELETE FROM file_versions WHERE path IN (?, ?)', (B, C))
        self.file('v2', B, 50)
        self.c.execute("INSERT INTO video_markers (video_id, seconds, end_seconds, title) VALUES ('v2', 10, 20, 'x')")
        self.c.execute("INSERT INTO video_markers (video_id, seconds, end_seconds, title) VALUES ('v1', 5, NULL, 'y')")
        video_parts.shift_markers_for_join(self.c, 'v1', [A, B])
        got = dict(((r[0], r[1]), r[2:]) for r in self.c.execute('SELECT video_id, title, seconds, end_seconds FROM video_markers'))
        self.assertEqual(got[('v2', 'x')], (110, 120))    # cd2 starts 100 s in
        self.assertEqual(got[('v1', 'y')], (5, None))

    def test_joined_vtt(self):
        import routes.player as player
        self.c.execute('UPDATE file_versions SET cache_id = ? WHERE path = ?', ('cidA', A))
        self.c.execute('UPDATE file_versions SET cache_id = ? WHERE path = ?', ('cidB', B))
        self.c.execute('DELETE FROM file_versions WHERE path = ?', (C,))
        self.c.commit()
        video_parts.set_order(self.c, 'v1', [A, B])
        for cid, dur in (('cidA', 100), ('cidB', 50)):
            os.makedirs(sprites.folder(cid), exist_ok=True)
            with open(os.path.join(sprites.folder(cid), sprites.VTT), 'w', encoding='utf-8') as f:
                f.write(sprites.vtt(dur, 160, 90, count=2))
        text = player.joined_vtt(self.c, 'v1')
        cues = [l for l in text.splitlines() if '-->' in l]
        self.assertEqual(cues, ['00:00:00.000 --> 00:00:50.000', '00:00:50.000 --> 00:01:40.000',
                                '00:01:40.000 --> 00:02:05.000', '00:02:05.000 --> 00:02:30.000'])
        self.assertIn('/cache/cidB/sprite.jpg#xywh=160,0,160,90', text)


if __name__ == '__main__':
    unittest.main()
