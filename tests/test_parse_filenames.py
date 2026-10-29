"""
Parse Filenames: a title made from the filename (by an earlier parse) may be replaced by a parsed
one; a title you typed may not. The pattern parser's 'folder name at the start of the title'.
New sites: studios from the cached StashDB scene lists, and the site-like text the parser sees.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filename_parser as fp                          # noqa: E402
from filename_parser import title_is_filename         # noqa: E402

STEM = 'Harmony Rose - downthehatch21_downthehatch21_scene11_1_1'


class TitleIsFilename(unittest.TestCase):
    def test_empty_or_the_filename(self):
        self.assertTrue(title_is_filename('', {STEM}))
        self.assertTrue(title_is_filename(STEM, {STEM}))

    def test_a_tidied_run_of_the_filename(self):
        self.assertTrue(title_is_filename('Downthehatch 21 Downthehatch 21 Scene 11 1 1', {STEM}))

    def test_a_typed_title_is_kept(self):
        self.assertFalse(title_is_filename('Scene 11', {STEM}))
        self.assertFalse(title_is_filename('Down The Hatch 21: Scene 11', {STEM}))   # reworded
        self.assertFalse(title_is_filename('Something Else Entirely Here', {STEM}))


class DirInTitle(unittest.TestCase):
    def parse(self, pattern, options, path='/media/Down The Hatch 21/Harmony Rose - downthehatch21_downthehatch21_scene11_1_1.wmv'):
        from routes.files import parse_filename, ParseFilenameRequest
        return parse_filename(ParseFilenameRequest(filename=os.path.basename(path), path=path, pattern=pattern, options=options), None)

    def test_pattern_from_the_report(self):
        r = self.parse('%C - downthehatch21_%T_1_1', {})
        self.assertEqual(r['data'], {'cast': ['Harmony Rose'], 'title': 'downthehatch21_scene11'})

    def test_folder_first(self):
        r = self.parse('%C - downthehatch21_%T_1_1', {'dir_in_title': True})
        self.assertEqual(r['data']['title'], 'Down The Hatch 21 downthehatch21_scene11')

    def test_old_dir_token_still_works(self):
        r = self.parse('%D/%C - downthehatch21_%T_1_1', {})
        self.assertEqual(r['data']['title'], 'Down The Hatch 21 downthehatch21_scene11')

    def test_names_are_one_word_with_spaces(self):
        path = '/media/x/BABES - ALEXA GRACE OFFERS RICKY A THERAPEUTIC HANDJOB.mp4'
        r = self.parse('%S - %F %L %T', {'title_case': True}, path=path)
        self.assertEqual(r['data'], {'site': 'babes', 'cast': ['Alexa Grace'], 'title': 'Offers Ricky A Therapeutic Handjob'})
        r = self.parse('%S - %F %M %L %T', {}, path='/media/x/Site - Jane Ann Doe Does It.mp4')
        self.assertEqual(r['data']['cast'], ['Jane Ann Doe'])
        r = self.parse('%S - %T', {}, path='/media/x/Team Skeet - Part One - Part Two.mp4')
        self.assertEqual((r['data']['site'], r['data']['title']), ('team skeet', 'Part One - Part Two'))
        r = self.parse('%S.%F.%L.%T', {'clean_dots': True}, path='/media/x/site.jane.doe.the.title.mp4')
        self.assertEqual(r['data'], {'site': 'site', 'cast': ['jane doe'], 'title': 'the title'})



class NewSites(unittest.TestCase):
    def setUp(self):
        self.ctx = fp.ParseContext(site_keys={'brazzers': 'brazzers'}, site_display={'brazzers': 'brazzers'})
        self.ctx.studios = {fp.norm_name(n): n for n in ('The Country Hotwife', 'Colby Knox', 'BelAmi', 'Lust HD')}

    def parse(self, fn):
        return fp.smart_parse(fn + '.mp4', self.ctx)

    def test_studio_as_a_dash_part(self):
        p = self.parse('The Country Hotwife - Another Amazing Kissing Compilation (2025)')
        self.assertEqual((p['site_new'], p['site_new_src']), ('the.country.hotwife', 'stashdb'))
        self.assertNotIn('Hotwife', p['title'])

    def test_studio_in_brackets_and_at_either_end(self):
        self.assertEqual(self.parse('[ColbyKnox] 4-way with Seth Knight')['site_new'], 'colby.knox')
        p = self.parse('lusthd_ariana3_full_hi')
        self.assertEqual(p['site_new'], 'lust.hd')
        self.assertNotIn('Lusthd', p['title'])
        self.assertEqual(self.parse('BelAmi Star Jack Harrer Fucks Darius')['site_new'], 'belami')

    def test_catalog_studios(self):
        import json, sqlite3
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE ext_scene_catalog (scenes_json TEXT, fetched_at TEXT)')
        scenes = [{'studio_name': 'The Gym', 'parent_name': 'Bait Buddies'}, {'studio_name': 'The Office'},
                  {'studio_name': 'BelAmi', 'parent_name': None}, {'studio_name': 'ABC'}]
        conn.execute('INSERT INTO ext_scene_catalog VALUES (?, ?)', (json.dumps(scenes), '2026-10-01'))
        self.assertEqual(fp._catalog_studios(conn), {'baitbuddies': 'Bait Buddies', 'belami': 'BelAmi'})   # ordinary words / short: out
        self.assertIsNone(self.parse('Gabi Gold - Teen Fucked at the Gym')['site_new'])
        self.ctx.studios['thegym'] = 'The Gym'                                    # even if it were in the list
        self.assertIsNone(self.parse('Teen Fucked at the Gym')['site_new'])
        self.ctx.studios['cocksuremen'] = 'CocksureMen'
        p = self.parse('Cocksure Men Pool Party 3-Way - Tristan Baldwin & AJ Irons')
        self.assertEqual(p['site_new'], 'cocksuremen')
        self.assertTrue(p['title'].startswith('Pool Party'), p['title'])

    def test_guesses(self):
        p = self.parse('xhamster.com_810536_hot_bisexual_action')
        self.assertEqual((p['site_new'], p['site_new_src']), ('xhamster', 'guess'))
        self.assertEqual(self.parse('wifelovesbbc-2021-06-27-0grx8vrlxitfsvs5hchjf_source')['site_new'], 'wifelovesbbc')
        self.assertIsNone(self.parse('1a601e33-e042-4773-b764-b2edb497f964')['site_new'])
        self.assertEqual(self.parse('Men.com - Joey In The Middle Part 2 (1080p)')['site_new'], 'men.com')
        self.assertEqual(self.parse('[LegalPorno.com - AnalVids.com - PornBox.com] Emma Without Rules')['site_new'], 'legalporno')

    def test_a_one_word_site_beats_a_performer_of_the_same_name(self):
        self.ctx.site_keys['babes'] = 'babes'
        self.ctx.site_display['babes'] = 'babes'
        self.ctx.cast_idx.update({'babes': 'BABES', 'alexagrace': 'Alexa Grace'})        # 'BABES' from an old parse
        p = self.parse('BABES - ALEXA GRACE OFFERS RICKY A THERAPEUTIC HANDJOB THAT TURNS INTO FUCKING')
        self.assertEqual((p['site_key'], [n['name'] for n in p['names']]), ('babes', ['Alexa Grace']))
        self.assertTrue(p['title'].startswith('Alexa Grace Offers'), p['title'])

    def test_a_known_site_is_never_new(self):
        p = self.parse('[Brazzers] Some Title')
        self.assertEqual((p['site_key'], p['site_new']), ('brazzers', None))


if __name__ == '__main__':
    unittest.main()
