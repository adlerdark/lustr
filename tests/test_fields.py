"""
Your own fields and the editor layout (fields.py): defaults are today's fields; a new field gets its tables
and is searchable by key or name; renaming keeps values; hiding keeps values and leaves them out of
everything; delete permanently is guarded and backs up first; revert hides your fields, never deletes.

    docker exec -w /app lustr python3 -m unittest tests.test_fields -v
"""
import json
import os
import sys
import tempfile
import types
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                         # noqa: E402
import db_maintenance as dm                           # noqa: E402
import fields                                         # noqa: E402
import value_format                                   # noqa: E402
from routes import videos                             # noqa: E402
from search_parser import SearchParser as SP          # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


class Fields(Base):
    def setUp(self):
        super().setUp()
        fields.bind(self.db)
        fields.forget()
        self.addCleanup(lambda: (fields.forget(), fields.reset_default(self.c)) and None)
        self.model = types.SimpleNamespace(library_groups={}, libraries={}, get_libraries_in_group=lambda g, recursive=True: [])

    def put(self, field, vid, value):
        row = self.c.execute(f'SELECT id FROM video_{field} WHERE value = ?', (value,)).fetchone()
        i = row[0] if row else self.c.execute(f'INSERT INTO video_{field} (value) VALUES (?)', (value,)).lastrowid
        self.c.execute(f'INSERT OR IGNORE INTO video_{field}_junction (video_id, {field}_id) VALUES (?, ?)', (vid, i))
        self.c.commit()

    def ids(self, q):
        got = videos._bool_video_ids(self.c, self.model, SP.parse_query(q + ' OR nosuchfield:x')[1])
        return got & {'v1', 'v2', 'v3'}

    def test_defaults_are_todays_fields(self):
        self.assertEqual(fields.list_fields(self.c), fields.BUILTIN_ORDER)
        self.assertEqual([f.lower() for f in config.LIST_FIELDS], fields.BUILTIN_ORDER)
        self.assertEqual([t['name'] for t in fields.layout(self.c)['tabs']], ['General', 'Appearance', 'Attributes'])
        self.assertEqual(fields.sidebar_fields(self.c), [f for f in fields.BUILTIN_ORDER if f not in ('site', 'cast')])
        self.assertEqual(sorted(fields.performer_fields(self.c)), sorted(fields.PERFORMER))

    def test_add_search_rename(self):
        k = fields.add_field(self.c, 'Studio Code', 'Attributes')
        self.assertEqual(k, 'studio_code')
        self.assertIn('studio_code', fields.list_fields(self.c))
        self.assertIn('studio_code', [f.lower() for f in config.LIST_FIELDS])        # everyone sees it
        self.put('studio_code', 'v1', 'abc')
        self.assertEqual(self.ids('studio_code:abc'), {'v1'})
        lay = fields.public(self.c)
        lay['fields']['studio_code']['label'] = 'Release code'
        fields.save_layout(self.c, {'tabs': lay['all_tabs'], 'fields': lay['fields']})
        self.assertEqual(self.ids('release_code:abc'), {'v1'})                       # by its new name
        self.assertEqual(self.ids('studio_code:abc'), {'v1'})                        # and its key, still
        self.assertEqual(SP.parse_query('release_code:abc')[1]['studio_code'], ['abc'])           # the simple parser too

    def test_hide_keeps_values(self):
        self.put('hair', 'v1', 'red')
        fields.hide(self.c, 'hair')
        self.assertNotIn('hair', fields.list_fields(self.c))
        self.assertNotIn('Hair', config.LIST_FIELDS)
        self.assertNotIn('hair', fields.sidebar_fields(self.c))
        self.assertEqual(self.ids('hair:red'), set())                                # not searched
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM video_hair_junction').fetchone()[0], 1)   # still there
        fields.restore(self.c, 'hair')
        self.assertEqual(self.ids('hair:red'), {'v1'})

    def test_fixed_fields_and_layout_rules(self):
        lay = fields.public(self.c)
        tabs = json.loads(json.dumps(lay['all_tabs']))
        with self.assertRaises(fields.FieldError):
            fields.hide(self.c, 'tags')
        moved = json.loads(json.dumps(tabs))
        moved[0]['fields'].remove('site')
        moved[1]['fields'].append('site')
        with self.assertRaises(fields.FieldError):                                    # site stays on General
            fields.save_layout(self.c, {'tabs': moved, 'fields': {}})
        for bad in ([{'name': 'General', 'fields': tabs[0]['fields']}, {'name': 'general', 'fields': []}],   # same name
                    [{'name': 'Looks', 'fields': []}] + tabs,                                               # General not first
                    tabs + [{'name': '', 'fields': []}]):
            with self.assertRaises(fields.FieldError):
                fields.save_layout(self.c, {'tabs': bad, 'fields': {}})
        renamed = json.loads(json.dumps(lay['fields']))
        renamed['hair']['label'] = 'library'
        with self.assertRaises(fields.FieldError):                                    # a search keyword
            fields.save_layout(self.c, {'tabs': tabs, 'fields': renamed})
        # a new tab with Outfit moved onto it, and General's own extra field
        k = fields.add_field(self.c, 'Mood', 'General')
        new = json.loads(json.dumps(fields.public(self.c)['all_tabs']))
        new[1]['fields'].remove('outfit')
        new.append({'name': 'Clothing', 'fields': ['outfit']})
        fields.save_layout(self.c, {'tabs': new, 'fields': {}})
        got = {t['name']: t['fields'] for t in fields.layout(self.c)['tabs']}
        self.assertEqual(got['Clothing'], ['outfit'])
        self.assertEqual(got['General'][:5], list(fields.FIXED))
        self.assertIn(k, got['General'])

    def test_bad_keys(self):
        for name in ('', '   ', '!!!', 'Title', 'Library', 'tags', 'cast role', 'ext link'):
            with self.assertRaises(fields.FieldError, msg=name):
                fields.add_field(self.c, name)
        self.assertEqual(fields.add_field(self.c, '1080 club'), 'f_1080_club')
        self.assertEqual(fields.add_field(self.c, "x'); DROP TABLE videos; --"), 'x_drop_table_videos')
        self.assertTrue(self.c.execute("SELECT 1 FROM sqlite_master WHERE name = 'videos'").fetchone())

    def test_delete_permanently(self):
        with self.assertRaises(fields.FieldError):
            fields.delete_permanently(self.c, 'hair', 'Hair')                         # built-in: hide only
        k = fields.add_field(self.c, 'Mood')
        self.put(k, 'v1', 'happy')
        self.assertEqual(fields.usage(self.c, k)['videos'], 1)
        with self.assertRaises(fields.FieldError):
            fields.delete_permanently(self.c, k, 'nope')                              # the name, typed
        calls, real = [], dm.safety_backup
        dm.safety_backup = lambda conn, reason, **kw: calls.append(reason) or 'b.db'
        self.addCleanup(setattr, dm, 'safety_backup', real)
        fields.delete_permanently(self.c, k, 'mood')
        self.assertEqual(calls, ['delete-field-mood'])
        self.assertIsNone(self.c.execute("SELECT 1 FROM sqlite_master WHERE name = 'video_mood'").fetchone())
        self.assertNotIn(k, fields.all_fields(self.c))

    def test_revert_hides_your_fields(self):
        k = fields.add_field(self.c, 'Mood')
        self.put(k, 'v2', 'calm')
        fields.hide(self.c, 'cock')
        fields.reset_default(self.c)
        self.assertEqual(fields.list_fields(self.c), fields.BUILTIN_ORDER)
        self.assertEqual(fields.hidden_fields(self.c), [k])
        self.assertEqual(self.c.execute(f'SELECT COUNT(*) FROM video_{k}_junction').fetchone()[0], 1)
        fields.restore(self.c, k)
        self.assertEqual(self.ids('mood:calm'), {'v2'})

    def test_value_format_follows_the_field(self):
        k = fields.add_field(self.c, 'Notes', words_=False)
        self.assertEqual(value_format.house(k, 'Shot on Film', {}), 'Shot on Film')
        self.assertEqual(value_format.house('tags', 'Shot on Film', {}), 'shot.on.film')
        self.assertNotIn(k, fields.sidebar_fields(self.c))                            # spaces: not a sidebar filter


if __name__ == '__main__':
    unittest.main()
