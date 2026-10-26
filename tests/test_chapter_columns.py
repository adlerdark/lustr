"""
Chapters / Chapter Markers: list columns, sorting and the chapters: / chaptermarkers: search keywords.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import types
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chapters                                       # noqa: E402
import database                                       # noqa: E402


class ChapterColumnTests(unittest.TestCase):
    def setUp(self):
        import routes.player as rp
        self.db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        rp._model = types.SimpleNamespace(db=self.db)
        rp._markers_ready = False
        self.c = self.db.get_connection()
        rp.ensure_markers(self.c)
        for vid in ('v1', 'v2', 'v3'):
            self.c.execute("INSERT INTO videos (video_id, filename_base, library) VALUES (?, ?, 'Lib')", (vid, vid))
            self.c.execute("INSERT INTO file_versions (path, video_id, filename) VALUES (?, ?, ?)", (f'/m/{vid}.mkv', vid, f'{vid}.mkv'))
        # v1: 3 chapters, 2 markers made from them; v2: checked, none; v3: never checked
        self.c.execute("INSERT INTO chapter_checks (path, size, mtime, chapters) VALUES ('/m/v1.mkv', 1, 1, 3)")
        self.c.execute("INSERT INTO chapter_checks (path, size, mtime, chapters) VALUES ('/m/v2.mkv', 1, 1, 0)")
        for s in (0, 60):
            self.c.execute("INSERT INTO video_markers (video_id, seconds, title, tag, source, source_key) "
                           "VALUES ('v1', ?, 'c', '', 'chapter', ?)", (s, f'chapter:{s}'))
        self.c.execute("INSERT INTO video_markers (video_id, seconds, title, tag) VALUES ('v2', 5, 'mine', '')")
        self.c.commit()

    def test_add_info(self):
        items = chapters.add_info(self.c, [{'video_id': 'v1'}, {'video_id': 'v2'}, {'video_id': 'v3'}])
        self.assertEqual([(i['chapters'], i['chapter_markers']) for i in items], [(3, 2), (0, 0), (None, 0)])

    def _ids(self, kind, has):
        cond = chapters.filter_sql(self.c, kind, has)
        return sorted(r[0] for r in self.c.execute(f'SELECT v.video_id FROM videos v WHERE {cond}'))

    def test_filter_sql(self):
        self.assertEqual(self._ids('file', True), ['v1'])
        self.assertEqual(self._ids('file', False), ['v2', 'v3'])
        self.assertEqual(self._ids('markers', True), ['v1'])
        self.assertEqual(self._ids('markers', False), ['v2', 'v3'])

    def test_keyword_parsing(self):
        from routes import videos
        f = {'chapters': ['yes'], 'markers': ['no'], 'title': ['x']}
        self.assertEqual(videos._pop_chapters_filter(f), [('file', True), ('markers', False)])
        self.assertEqual(f, {'title': ['x']})

    def test_sort(self):
        from routes import videos
        rows = [{'video_id': 'v3'}, {'video_id': 'v1'}, {'video_id': 'v2'}]
        for field, want in (('chapters', ['v3', 'v2', 'v1']), ('chapter_markers', ['v3', 'v2', 'v1'])):
            videos._prepare_sort(self.c, rows, field)
            got = [r['video_id'] for r in sorted(rows, key=videos._sort_key(field))]
            self.assertEqual(got[-1], 'v1', field)
            self.assertEqual(videos._sort_field_of('Chapter Markers'), 'chapter_markers')


if __name__ == '__main__':
    unittest.main()
