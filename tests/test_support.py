"""
Problem report (support.py): names, titles and paths are hidden; lustr's own words are not.

    docker exec -w /app lustr python3 -m unittest tests.test_support -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import support                                        # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


class Redaction(Base):
    def setUp(self):
        super().setUp()
        self.c.execute("UPDATE videos SET title = 'Summer Heat Wave' WHERE video_id = 'v1'")
        self.c.execute("UPDATE videos SET title = 'Error' WHERE video_id = 'v2'")      # a one-word title lustr's messages use
        self.c.commit()
        self.tag('v1', 'Jane Doe', 'Kimberly')
        self.r = support.Redactor(self.c)

    def test_paths_keep_the_message_after_them(self):
        out = self.r.apply('[thumb-bg] Error for /media/Lib/Studio X/Jane Doe - Beach (2021) 1080p.mp4: moov atom not found')
        self.assertIn('/media/«path-1».mp4: moov atom not found', out)
        self.assertNotIn('Beach', out)

    def test_moves_and_file_names(self):
        out = self.r.apply('CONFIRMED MOVE: /media/a/old name.mkv -> /media0/b/new name.mkv\n'
                           'Progress: admin - some.studio.24.01.02.someone.clip.mp4 - 10.0s/20.0s')
        self.assertNotIn('old name', out)
        self.assertNotIn('new name', out)
        self.assertNotIn('someone', out)
        self.assertIn('-> /media0/«path-', out)

    def test_names_from_the_database(self):
        out = self.r.apply('apply-attrs: Jane Doe in Summer Heat Wave, kimberly too; Error: disk full')
        self.assertNotIn('Jane Doe', out)
        self.assertNotIn('Summer Heat Wave', out)
        self.assertNotIn('kimberly', out.lower())
        self.assertIn('Error: disk full', out)                    # 'Error' is also a title, but lustr's word

    def test_same_name_same_placeholder(self):
        out = self.r.apply('Jane Doe ... jane doe ... Kimberly')
        self.assertEqual(out.count('«name-1»'), 2)

    def test_request_urls_and_ips(self):
        out = self.r.apply('172.25.0.1:5000 - "GET /api/videos?query=jane&sort_by=Title&path=%2Fmedia%2Fx HTTP/1.1" 200')
        self.assertIn('«ip»', out)
        self.assertIn('query=«…»', out)
        self.assertIn('path=«…»', out)
        self.assertIn('sort_by=Title', out)

    def test_names_in_request_paths(self):
        out = self.r.apply('"GET /api/library/Straight%20Torrents/alert-count HTTP/1.1" 200\n'
                           '"GET /api/cast/resolve/Rico%20Strong HTTP/1.1" 200\n'
                           '"POST /api/cast/Jane%20Doe/aliases/JD%20Smith HTTP/1.1" 200\n'
                           'took 0.5s: GET /api/cast/photo/Some%20One?thumbnail=true\n'
                           '"POST /api/library/create HTTP/1.1" 200\n'
                           '"POST /api/watch/progress/media/Lib/My%20Clip.mp4 HTTP/1.1" 200')
        for name in ('Straight', 'Torrents', 'Rico', 'Strong', 'Jane', 'Smith', 'Some', 'My%20Clip', 'My Clip'):
            self.assertNotIn(name, out)
        self.assertIn('/api/library/«name-', out)
        self.assertIn('/alert-count', out)
        self.assertIn('/api/library/create', out)
        self.assertIn('/aliases/«name-', out)

    def test_report_builds(self):
        support.set_model(None)
        text = support.build_report(redact=False)
        for title in ('## System', '## Database', '## Recent warnings and errors', '## Log'):
            self.assertIn(title, text)


if __name__ == '__main__':
    unittest.main()
