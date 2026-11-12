"""
Boolean search: parentheses, AND / OR / NOT (also NOT(...) and -term), nested as deep as you like,
over fields, cast, paths and plain words (search_parser.boolean_tree -> routes/videos.py _bool_sql).

    docker exec -w /app lustr python3 -m unittest tests.test_search_boolean -v
"""
import os
import sys
import tempfile
import types
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import videos                             # noqa: E402
from search_parser import SearchParser as SP          # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


class Parse(unittest.TestCase):
    def tree(self, q):
        return SP.parse_query(q)[1].get('__bool__')

    def test_not_forms_are_the_same(self):
        want = ('and', [('field', 'tags', ['a']), ('not', ('field', 'tags', ['b']))])
        for q in ('tags:a NOT tags:b', 'tags:a AND NOT(tags:b)', 'tags:a -tags:b', 'tags:a and not (tags:b)'):
            self.assertEqual(self.tree(q), want, q)

    def test_precedence(self):
        # NOT, then AND (also between neighbours), then OR
        self.assertEqual(self.tree('a OR b c'), ('or', [('text', 'a', False), ('and', [('text', 'b', False), ('text', 'c', False)])]))
        self.assertEqual(self.tree('(a OR b) c'), ('and', [('or', [('text', 'a', False), ('text', 'b', False)]), ('text', 'c', False)]))

    def test_nested(self):
        t = self.tree('(tags:x OR tags:y) AND (cast:"Jane Doe" AND NOT(hair:red OR (site:s AND tags:z)))')
        self.assertEqual(t[0], 'and')
        self.assertEqual(t[1][1][1][1], ('not', ('or', [('field', 'hair', ['red']),
                                                         ('and', [('field', 'site', ['s']), ('field', 'tags', ['z'])])])))

    def test_simple_searches_unchanged(self):
        self.assertIsNone(SP.boolean_tree('tags:(a,b) beach'))
        self.assertEqual(SP.parse_query('tags:(a,b) beach')[0], 'beach')
        self.assertIsNone(SP.boolean_tree('semi-vintage tags:x'))          # a hyphen inside a word isn't NOT

    def test_whole_search_keywords_pulled_out(self):
        base, ff, libs = SP.parse_query('library:(A,B) res:hd (tags:a OR tags:b)')
        self.assertEqual((base, libs, ff['res']), ('', ['A', 'B'], ['hd']))
        self.assertEqual(ff['__bool__'][0], 'or')
        self.assertEqual(SP.parse_query('NOT library:A tags:x')[2], [])    # under NOT: part of the expression

    def test_unbalanced_is_forgiven(self):
        self.assertEqual(self.tree('(a OR b'), ('or', [('text', 'a', False), ('text', 'b', False)]))
        self.assertEqual(self.tree('a) OR b'), ('and', [('text', 'a', False), ('text', 'b', False)]))


class Results(Base):
    """v1: tags a,b  hair red  cast Jane Doe   v2: tags a  hair blonde   v3: tags c  title 'Beach Day'"""
    def setUp(self):
        super().setUp()
        self.add('tags', 'v1', 'a', 'b')
        self.add('tags', 'v2', 'a')
        self.add('tags', 'v3', 'c')
        self.add('hair', 'v1', 'red')
        self.add('hair', 'v2', 'blonde')
        self.tag('v1', 'Jane Doe')
        self.c.execute("UPDATE videos SET title = 'Beach Day' WHERE video_id = 'v3'")
        for vid in ('v1', 'v2', 'v3'):
            self.c.execute("INSERT INTO file_versions (path, video_id, filename, is_preferred) VALUES (?, ?, ?, 1)",
                           (f'/m/Lib/{vid}.mp4', vid, f'{vid}.mp4'))
        self.c.commit()
        self.model = types.SimpleNamespace(library_groups={}, libraries={'Lib': ['/m/Lib']}, get_libraries_in_group=lambda g, recursive=True: [])

    def add(self, field, vid, *values):
        for v in values:
            row = self.c.execute(f'SELECT id FROM video_{field} WHERE value = ?', (v,)).fetchone()
            vid_ = row[0] if row else self.c.execute(f'INSERT INTO video_{field} (value) VALUES (?)', (v,)).lastrowid
            self.c.execute(f'INSERT INTO video_{field}_junction (video_id, {field}_id) VALUES (?, ?)', (vid, vid_))

    def ids(self, q):
        _b, ff, _l = SP.parse_query(q)
        got = videos._bool_video_ids(self.c, self.model, ff)
        return got & {'v1', 'v2', 'v3'}

    def test_examples(self):
        self.assertEqual(self.ids('(tags:a OR tags:c) AND (tags:a) AND (NOT(tags:b))'), {'v2'})
        self.assertEqual(self.ids('(tags:a OR tags:c) AND (tags:a AND NOT(hair:red))'), {'v2'})
        self.assertEqual(self.ids('NOT tags:a'), {'v3'})
        self.assertEqual(self.ids('-tags:a OR cast:"Jane Doe"'), {'v1', 'v3'})

    def test_same_field_twice_means_both(self):
        self.assertEqual(self.ids('tags:a tags:b'), {'v1'})
        self.assertEqual(self.ids('tags:a OR tags:c'), {'v1', 'v2', 'v3'})

    def test_words_paths_and_titles(self):
        self.assertEqual(self.ids('beach OR hair:blonde'), {'v2', 'v3'})
        self.assertEqual(self.ids('NOT beach AND NOT jane'), {'v2'})               # words also search cast
        self.assertEqual(self.ids('path:v1 OR title:beach'), {'v1', 'v3'})
        self.assertEqual(self.ids('(tags:a* OR tags:c) -tags:b'), {'v2', 'v3'})   # wildcards

    def test_unknown_field_matches_nothing(self):
        self.assertEqual(self.ids('nosuchfield:x OR tags:c'), {'v3'})


if __name__ == '__main__':
    unittest.main()
