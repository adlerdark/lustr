"""
Performer names: one spelling per performer ('A.J. Applegate' = 'AJ Applegate' = 'aj applegate').

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

import cast_names                                     # noqa: E402
import database                                       # noqa: E402
import ext_db                                         # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        ext_db._model = types.SimpleNamespace(db=self.db)
        ext_db._schema_ready = False
        ext_db.ensure_schema()
        self.c = self.db.get_connection()
        for vid in ('v1', 'v2', 'v3'):
            self.c.execute("INSERT INTO videos (video_id, filename_base, library) VALUES (?, ?, 'Lib')", (vid, vid))
        self.c.commit()

    def tag(self, vid, *names):
        for n in names:
            row = self.c.execute('SELECT id FROM video_cast WHERE value = ?', (n,)).fetchone()
            cid = row[0] if row else self.c.execute('INSERT INTO video_cast (value) VALUES (?)', (n,)).lastrowid
            self.c.execute('INSERT INTO video_cast_junction (video_id, cast_id) VALUES (?, ?)', (vid, cid))
        self.c.commit()

    def cast_of(self, vid):
        return sorted(r[0] for r in self.c.execute('SELECT vc.value FROM video_cast_junction j JOIN video_cast vc '
                                                   'ON vc.id = j.cast_id WHERE j.video_id = ?', (vid,)))


class NameTests(unittest.TestCase):
    def test_norm_and_suffix(self):
        self.assertEqual(cast_names.norm('A.J. Applegate'), cast_names.norm('AJ  applegate'))
        self.assertEqual(cast_names.norm('Verónica Leal'), cast_names.norm('Veronica Leal'))
        self.assertEqual(cast_names.split_suffix('Jane Doe (2)'), ('Jane Doe', 2))
        self.assertEqual(cast_names.split_suffix('Jane Doe'), ('Jane Doe', None))


class LookupTests(Base):
    def test_existing_spelling_is_reused(self):
        self.tag('v1', 'A.J. Applegate')
        for typed in ('AJ Applegate', 'aj applegate', 'A J Applegate'):
            self.assertEqual(cast_names.canonical(self.c, typed), 'A.J. Applegate')
        self.assertEqual(cast_names.canonical(self.c, 'Someone New'), 'Someone New')

    def test_bare_name_goes_to_the_main_numbered_one(self):
        self.tag('v1', 'Jane Doe (2)', 'Jane Doe (1)')
        self.assertEqual(cast_names.canonical(self.c, 'jane doe'), 'Jane Doe (1)')

    def test_saving_metadata_lands_on_existing_spelling(self):
        self.tag('v1', 'A.J. Applegate')
        self.c.execute("INSERT INTO file_versions (path, video_id, filename) VALUES ('/m/v2.mp4', 'v2', 'v2.mp4')")
        self.c.commit()
        self.db._update_junction_tables('/m/v2.mp4', {'cast': ['AJ Applegate', 'aj applegate']})
        self.c.commit()
        self.assertEqual(self.cast_of('v2'), ['A.J. Applegate'])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM video_cast').fetchone()[0], 1)

    def test_add_list_value(self):
        self.tag('v1', 'Remy LaCroix')
        ext_db._add_list_value(self.c, 'v2', 'cast', 'remy lacroix')
        self.assertEqual(self.cast_of('v2'), ['Remy LaCroix'])


class MergeTests(Base):
    def test_groups_and_merge(self):
        self.tag('v1', 'A.J. Applegate')
        self.tag('v2', 'A.J. Applegate')
        self.tag('v3', 'Aj Applegate', 'A.J. Applegate')            # both spellings on one video
        self.tag('v2', 'Someone Else')
        aj = self.c.execute("SELECT id FROM video_cast WHERE value = 'Aj Applegate'").fetchone()[0]
        self.c.execute("INSERT INTO video_cast_role (video_id, cast_id) VALUES ('v3', ?)", (aj,))
        self.c.execute("INSERT INTO cast_gender (cast_name, gender) VALUES ('Aj Applegate', 'female')")
        self.c.execute("INSERT INTO cast_aliases (primary_name, alias_name) VALUES ('Aj Applegate', 'AJ')")
        self.c.commit()
        groups = cast_names.spelling_groups(self.c)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['keep'], 'A.J. Applegate')               # most videos
        cast_names.merge_cast_values(self.c, 'A.J. Applegate', ['Aj Applegate'])
        self.c.commit()
        self.assertEqual(self.cast_of('v3'), ['A.J. Applegate'])
        keep = self.c.execute("SELECT id FROM video_cast WHERE value = 'A.J. Applegate'").fetchone()[0]
        self.assertEqual(self.c.execute('SELECT cast_id FROM video_cast_role').fetchall()[0][0], keep)   # Male Cast mark moved
        self.assertEqual(self.c.execute("SELECT gender FROM cast_gender WHERE cast_name = 'A.J. Applegate'").fetchone()[0], 'female')
        self.assertEqual(self.c.execute("SELECT primary_name FROM cast_aliases WHERE alias_name = 'AJ'").fetchone()[0], 'A.J. Applegate')
        self.assertEqual(cast_names.spelling_groups(self.c), [])

    def test_alias_member_stays_linked(self):
        self.tag('v1', 'Xxlayna Marie')
        self.tag('v2', 'XXlayna Marie')
        self.c.execute("INSERT INTO cast_aliases (primary_name, alias_name) VALUES ('Layna Marie', 'Xxlayna Marie')")
        self.c.commit()
        cast_names.merge_cast_values(self.c, 'XXlayna Marie', ['Xxlayna Marie'])
        self.assertEqual(self.c.execute("SELECT primary_name FROM cast_aliases WHERE alias_name = 'XXlayna Marie'").fetchone()[0],
                         'Layna Marie')

    def test_not_same_is_remembered(self):
        self.tag('v1', 'Ann Marie')
        self.tag('v2', 'Annmarie')
        self.assertEqual(len(cast_names.spelling_groups(self.c)), 1)
        cast_names.mark_not_same(self.c, ['Ann Marie', 'Annmarie'])
        self.assertEqual(cast_names.spelling_groups(self.c), [])


class CaseOnlyTests(Base):
    def test_case_only_merges_others_stay(self):
        self.tag('v1', 'Jessie Rogers')
        self.tag('v2', 'Jessie Rogers')
        self.tag('v3', 'jessie rogers', 'Anya Taylor Joy')
        self.tag('v2', 'Anya Taylor-Joy')
        self.c.execute("INSERT INTO cast_gender (cast_name, gender) VALUES ('jessie rogers', 'female')")
        self.c.commit()
        r = cast_names.merge_case_only(self.c)
        self.assertEqual(r['count'], 1)
        self.assertEqual(self.cast_of('v3'), ['Anya Taylor Joy', 'Jessie Rogers'])
        self.assertEqual(self.c.execute("SELECT gender FROM cast_gender WHERE cast_name = 'Jessie Rogers'").fetchone()[0], 'female')
        left = cast_names.spelling_groups(self.c)                     # a hyphen is not a capital: left for review
        self.assertEqual([g['key'] for g in left], ['Anya Taylor Joy|Anya Taylor-Joy'])

    def test_not_same_is_skipped(self):
        self.tag('v1', 'Aubrey')
        self.tag('v2', 'aubrey')
        cast_names.mark_not_same(self.c, ['Aubrey', 'aubrey'])
        self.assertEqual(cast_names.merge_case_only(self.c)['count'], 0)

    def test_maintenance_runs_it(self):
        import db_maintenance
        self.tag('v1', 'Cherie DeVille')
        self.tag('v2', 'Cherie Deville')
        self.tag('v3', 'Cherie Deville')
        r = db_maintenance._apply(self.c, ['merge_cast_case'], 30)
        self.assertEqual(r['merge_cast_case']['count'], 1)
        self.assertEqual(self.cast_of('v1'), ['Cherie Deville'])          # the spelling with more videos

    def test_stored_case(self):
        self.tag('v1', 'Dana DeArmond')
        self.assertEqual(cast_names.stored_case(self.c, 'dana dearmond'), 'Dana DeArmond')
        self.assertEqual(cast_names.stored_case(self.c, 'Dana DeArmond'), 'Dana DeArmond')
        self.assertEqual(cast_names.stored_case(self.c, 'Nobody Here'), 'Nobody Here')


if __name__ == '__main__':
    unittest.main()
