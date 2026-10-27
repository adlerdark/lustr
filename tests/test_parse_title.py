"""
Parse Filenames: a title made from the filename (by an earlier parse) may be replaced by a parsed
one; a title you typed may not. Also the pattern parser's 'folder name at the start of the title'.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


if __name__ == '__main__':
    unittest.main()
