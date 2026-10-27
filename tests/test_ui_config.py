"""
Saving the UI config: sort / columns are what you used last, but the opening defaults (Settings -> Browsing)
and per-library overrides are merged in, so a page left open elsewhere can't undo them.

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

import config                                         # noqa: E402
from routes.ui import save_ui_config, UIConfigRequest  # noqa: E402


class _DB:
    def __init__(self):
        self.cfg = {}

    def load_ui_config(self):
        return dict(self.cfg)

    def save_ui_config(self, c):
        self.cfg = dict(c)


BASE = dict(sort_field='Filename', sort_desc=False, visible_columns=['Title'], column_widths={}, view_mode='grid')


class SaveUIConfig(unittest.TestCase):
    def setUp(self):
        if not config.USE_SQLITE:
            self.skipTest('SQLite config only')
        self.model = types.SimpleNamespace(db=_DB(), files={})

    def save(self, **kw):
        save_ui_config(UIConfigRequest(**{**BASE, **kw}), self.model)
        return self.model.db.cfg

    def test_an_override_survives_a_page_that_never_saw_it(self):
        self.save(client=2, library_defaults_patch={'Straight Torrents': {'sort': 'Cast', 'desc': False}})
        self.save(client=2, sort_field='Length')                                   # another page: just a sort change
        self.save(library_defaults={}, default_sort='Filename')                    # a page from before 0.17.2
        c = self.model.db.cfg
        self.assertEqual(c['library_defaults'], {'Straight Torrents': {'sort': 'Cast', 'desc': False}})
        self.assertEqual(c['sort_field'], 'Filename')                              # last used: fine to follow

    def test_patches_add_change_and_remove(self):
        self.save(client=2, library_defaults_patch={'A': {'sort': 'Cast'}, 'B': {'view': 'list'}})
        self.save(client=2, library_defaults_patch={'A': None, 'B': {'view': 'grid', 'tab': 'library'}})
        self.assertEqual(self.model.db.cfg['library_defaults'], {'B': {'view': 'grid', 'tab': 'library'}})

    def test_defaults_only_change_when_sent(self):
        self.save(client=2, default_sort='Cast', default_sort_desc=True, default_tab='library')
        c = self.save(client=2, sort_field='Length')
        self.assertEqual((c['default_sort'], c['default_sort_desc'], c['default_tab']), ('Cast', True, 'library'))
        c = self.save(default_sort='Filename', default_tab='recommended')                      # an old page
        self.assertEqual((c['default_sort'], c['default_tab']), ('Cast', 'library'))


if __name__ == '__main__':
    unittest.main()
