"""
Tag mappings with their own value(s) per orientation (Creampie -> creampie, but breeding on gay videos).

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

import database                                       # noqa: E402
import ext_db                                         # noqa: E402

CREAMPIE = {'field': 'tags', 'value': 'creampie', 'status': 'confirmed',
            'type_values': {'gay': 'breeding', 'bisexual': 'creampie, breeding', 'lesbian': '-'}}


class MappingValuesTests(unittest.TestCase):
    def values(self, vtypes, m=CREAMPIE):
        return ext_db.mapping_values(m, vtypes)[0]

    def test_default_where_no_override(self):
        self.assertEqual(self.values(['straight']), ['creampie'])
        self.assertEqual(self.values(['trans']), ['creampie'])

    def test_override(self):
        self.assertEqual(self.values(['gay']), ['breeding'])
        self.assertEqual(ext_db.mapping_values(CREAMPIE, ['gay'])[1], ['gay'])

    def test_several_values(self):
        self.assertEqual(self.values(['bisexual']), ['creampie', 'breeding'])

    def test_nothing(self):
        self.assertEqual(self.values(['lesbian']), [])

    def test_unknown_orientation_uses_default(self):
        self.assertEqual(self.values([]), ['creampie'])

    def test_several_types_union(self):
        self.assertEqual(self.values(['straight', 'gay']), ['creampie', 'breeding'])

    def test_stored_json(self):
        self.assertEqual(ext_db._type_values('{"gay": "breeding", "nope": "x", "straight": " "}'), {'gay': 'breeding'})
        self.assertEqual(ext_db._type_values('not json'), {})


class TypeValueEndpointTests(unittest.TestCase):
    def setUp(self):
        db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        ext_db._model = types.SimpleNamespace(db=db)
        ext_db._schema_ready = False
        ext_db.ensure_schema()
        self.conn = db.get_connection()
        self.conn.execute("INSERT INTO ext_tag_map (ext_source, ext_tag, field, value, status) "
                          "VALUES ('stashdb', 'creampie', 'tags', 'creampie', 'auto')")
        self.conn.commit()

    def row(self):
        return self.conn.execute("SELECT status, type_values FROM ext_tag_map WHERE ext_tag = 'creampie'").fetchone()

    def test_set_and_clear(self):
        r = ext_db.tagmap_type_value(ext_db.TagTypeValueBody(source='stashdb', tag='Creampie', type='gay', value='Breeding'))
        self.assertTrue(r['success'], r)
        self.assertEqual(r['type_values'], {'gay': 'breeding'})          # your format
        self.assertEqual(self.row()[0], 'confirmed')                       # auto -> confirmed
        r = ext_db.tagmap_type_value(ext_db.TagTypeValueBody(source='stashdb', tag='creampie', type='gay', value=''))
        self.assertEqual(r['type_values'], {})
        self.assertIsNone(self.row()[1])

    def test_nothing_and_bad_type(self):
        r = ext_db.tagmap_type_value(ext_db.TagTypeValueBody(source='stashdb', tag='creampie', type='lesbian', value='-'))
        self.assertEqual(r['type_values'], {'lesbian': '-'})
        r = ext_db.tagmap_type_value(ext_db.TagTypeValueBody(source='stashdb', tag='creampie', type='martian', value='x'))
        self.assertFalse(r['success'])

    def test_lookup_returns_them(self):
        ext_db.tagmap_type_value(ext_db.TagTypeValueBody(source='stashdb', tag='creampie', type='gay', value='breeding'))
        m = ext_db._tag_mapping(self.conn, 'stashdb', 'Creampie', {})
        self.assertEqual(ext_db.mapping_values(m, ['gay'])[0], ['breeding'])
        self.assertEqual(ext_db.mapping_values(m, ['straight'])[0], ['creampie'])


if __name__ == '__main__':
    unittest.main()
