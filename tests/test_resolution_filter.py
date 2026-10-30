"""
Resolution: stored as the short side, Fix resolutions for older files, and the library's res: filter.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import types
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolution_fix                                 # noqa: E402
from media_handler import MediaHandler                # noqa: E402
from routes import videos                             # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


class LabelTests(unittest.TestCase):
    def test_short_side(self):
        for w, h, want in ((1920, 1080, '1080p'), (1080, 1920, '1080p'), (404, 720, '404p'), (3840, 2160, '4K'),
                           (2160, 3840, '4K'), (3840, 1728, '4K'), (1280, 674, '674p'), (640, 0, '640w'), (0, 0, '')):
            self.assertEqual(MediaHandler.resolution_label(w, h), want, (w, h))

    def test_correction(self):
        self.assertEqual(resolution_fix.correction('1920p', 1080, 1920), '1080p')
        self.assertEqual(resolution_fix.correction('720p', 404, 720), '404p')
        self.assertIsNone(resolution_fix.correction('1080p', 1920, 1080))         # already right
        self.assertIsNone(resolution_fix.correction('1080p', 1080, 1920))         # already fixed, or typed by hand
        self.assertIsNone(resolution_fix.correction('4K', 3840, 2160))
        self.assertIsNone(resolution_fix.correction('1920p', 0, 0))               # unreadable


class FilterBase(Base):
    FILES = (('v1', '/m/Lib/a.mp4', '4K', 1, None), ('v2', '/m/Lib/b.mp4', '1080p', 1, None),
             ('v3', '/m/Lib/c.mp4', '674p', 1, None), ('v4', '/m/Lib/d.mp4', '656p', 1, None),
             ('v5', '/m/Lib/e.mp4', '540p', 1, None), ('v6', '/m/Lib/f.mp4', '539p', 1, None),
             ('v7', '/m/Lib/g.mp4', '', 1, None), ('v8', '/m/Lib/h.mp4', '1920x1080', 1, None),
             # preferred is 480p: its 1080p copy doesn't count
             ('v9', '/m/Lib/i.mp4', '480p', 1, None), ('v9', '/m/Lib/i2.mp4', '1080p', 0, None),
             # its 1080p file is deleted
             ('v10', '/m/Lib/j.mp4', '1080p', 1, '2026-01-01'))

    def setUp(self):
        super().setUp()
        for vid, path, res, pref, gone in self.FILES:
            self.c.execute("INSERT OR IGNORE INTO videos (video_id, filename_base, library) VALUES (?, ?, 'Lib')", (vid, vid))
            self.c.execute('INSERT INTO file_versions (path, video_id, filename, resolution, is_preferred, deleted_at) '
                           'VALUES (?, ?, ?, ?, ?, ?)', (path, vid, os.path.basename(path), res, pref, gone))
        self.c.commit()


class FixTests(FilterBase):
    def test_run(self):
        self.c.execute("UPDATE file_versions SET resolution = '1920p' WHERE path = '/m/Lib/a.mp4'")
        self.c.execute("UPDATE file_versions SET resolution = '1920p' WHERE path = '/m/Lib/b.mp4'")   # no file to read
        sizes = {'/m/Lib/a.mp4': (1080, 1920), '/m/Lib/c.mp4': (1280, 674), '/m/Lib/h.mp4': (1080, 1920)}

        def read(row):
            return row[0], row[1], *sizes.get(row[0], (0, 0))

        self.assertEqual(resolution_fix.run(self.c, read)['changed'], 1)
        res = dict(self.c.execute('SELECT path, resolution FROM file_versions').fetchall())
        self.assertEqual(res['/m/Lib/a.mp4'], '1080p')                            # vertical
        self.assertEqual(res['/m/Lib/b.mp4'], '1920p')                            # unreadable: left alone
        self.assertEqual(res['/m/Lib/c.mp4'], '674p')
        self.assertEqual(res['/m/Lib/h.mp4'], '1920x1080')                        # typed by hand
        self.assertEqual(resolution_fix.run(self.c, read)['changed'], 0)


class ConditionTests(FilterBase):
    def ids(self, value):
        fields = {'res': [value]}
        minimum = videos._pop_resolution_filter(fields)
        self.assertNotIn('res', fields)
        conds, params = [], []
        videos._add_resolution_condition(minimum, conds, params, self.c)
        if not conds:
            return None
        return {r[0] for r in self.c.execute(f'SELECT v.video_id FROM videos v WHERE {conds[0]}', params)}

    def test_each_button(self):
        self.assertEqual(self.ids('4k'), {'v1'})
        self.assertEqual(self.ids('hd'), {'v1', 'v2', 'v3', 'v8'})
        self.assertEqual(self.ids('sdhigh'), {'v1', 'v2', 'v3', 'v4', 'v5', 'v8'})
        self.assertEqual(self.ids('sdlow'), {'v1', 'v2', 'v3', 'v4', 'v5', 'v6', 'v8', 'v9'})

    def test_unknown_value_is_ignored(self):
        self.assertIsNone(self.ids('nonsense'))

    def test_no_value_matches_nothing(self):
        conds, params = [], []
        self.c.execute("UPDATE file_versions SET resolution = '480p'")
        videos._add_resolution_condition(2160, conds, params, self.c)
        self.assertEqual(self.c.execute(f'SELECT COUNT(*) FROM videos v WHERE {conds[0]}', params).fetchone()[0], 0)

    def test_parses_from_a_search(self):
        from search_parser import SearchParser
        base, fields, _ = SearchParser.parse_query('holiday res:hd scene:linked')
        self.assertEqual(videos._pop_resolution_filter(fields), 670)
        self.assertEqual(base.strip(), 'holiday')
        self.assertNotIn('res', str(fields.get('__or_groups__')))


if __name__ == '__main__':
    unittest.main()
