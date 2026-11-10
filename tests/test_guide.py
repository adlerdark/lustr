"""
The user guide (Help): guide/index.json lists the pages, guide/<id>.md holds the written ones, routes/ui.py serves them.

    docker exec -w /app lustr python3 -m unittest tests.test_guide -v
"""
import json
import os
import re
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from routes import ui                                  # noqa: E402

GUIDE = os.path.join(ROOT, 'guide')


def slug(heading):
    """The anchor GitHub (and the Help page, helpSlug) makes from a heading."""
    s = re.sub(r'[^\w\s-]', '', heading.lower(), flags=re.UNICODE)
    return s.strip().replace(' ', '-')


def read(name):
    with open(os.path.join(GUIDE, name), encoding='utf-8') as f:
        return f.read()


class GuideFiles(unittest.TestCase):
    def setUp(self):
        self.index = json.loads(read('index.json'))
        self.ids = [p['id'] for p in self.index]

    def test_index_entries(self):
        self.assertEqual(len(self.ids), len(set(self.ids)), 'duplicate ids')
        for p in self.index:
            self.assertRegex(p['id'], r'^[a-z0-9-]+$')
            self.assertIn(p['status'], ('done', 'planned'))
            self.assertTrue(p['title'] and p['group'])

    def test_written_pages_have_files_and_files_are_listed(self):
        done = {p['id'] for p in self.index if p['status'] == 'done'}
        files = {f[:-3] for f in os.listdir(GUIDE) if f.endswith('.md')}
        self.assertEqual(done, files)

    def test_settings_ids_exist(self):
        with open(os.path.join(ROOT, 'templates', 'index.html'), encoding='utf-8') as f:
            html = f.read()
        block = html[html.index('settingsPages: ['):]
        block = block[:block.index('\n                ],')]       # the list's own closing bracket
        pages = set(re.findall(r"\{ id: '([a-z]+)'", block))
        self.assertIn('player', pages)
        for p in self.index:
            for s in p.get('settings', []):
                self.assertIn(s, pages, f"{p['id']}: no Settings page '{s}'")

    def test_internal_links_point_somewhere(self):
        anchors = {}
        for p in self.index:
            if p['status'] == 'done':
                anchors[p['id']] = {slug(h) for h in re.findall(r'^#{1,4}\s+(.*)$', read(p['id'] + '.md'), re.M)}
        for page, have in anchors.items():
            text = read(page + '.md')
            for target, anchor in re.findall(r'\]\((?:([a-z0-9-]+)\.md)?(?:#([a-z0-9-]+))?\)', text):
                target = target or page
                self.assertIn(target, anchors, f'{page}: link to {target}.md, which is not a written page')
                if anchor:
                    self.assertIn(anchor, anchors[target], f'{page}: no heading #{anchor} in {target}.md')

    def test_no_raw_html(self):
        for p in self.index:
            if p['status'] == 'done':
                self.assertIsNone(re.search(r'<[a-zA-Z/][^>]*>', read(p['id'] + '.md')), p['id'])


class GuideRoutes(unittest.TestCase):
    def test_index(self):
        d = ui.get_guide_index()
        self.assertTrue(d['version'])
        self.assertTrue(any(p['id'] == 'getting-started' for p in d['pages']))

    def test_page(self):
        d = ui.get_guide_page('player')
        self.assertIn('# Watching videos', d['markdown'])

    def test_refused(self):
        from fastapi import HTTPException
        for bad in ('../config', '..', 'config', 'Player', 'player.md', '', 'nope'):
            with self.assertRaises(HTTPException, msg=bad) as cm:
                ui.get_guide_page(bad)
            self.assertEqual(cm.exception.status_code, 404)

    def test_planned_page_refused(self):
        from fastapi import HTTPException
        real = ui.guide_index
        ui.guide_index = lambda: [dict(p, status='planned') if p['id'] == 'player' else p for p in real()]
        try:
            with self.assertRaises(HTTPException):
                ui.get_guide_page('player')                 # its file exists, but it isn't listed as written
        finally:
            ui.guide_index = real


if __name__ == '__main__':
    unittest.main()
