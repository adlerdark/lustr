"""
Tag cleanup → Site format (db_maintenance.py): sites with capitals or spaces are renamed to your format
(merging into an existing site of that name), and sites no video uses are removed.

    docker exec -w /app lustr python3 -m unittest tests.test_site_format -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_maintenance as dm                           # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


class SiteFormat(Base):
    def site(self, vid, value):
        row = self.c.execute('SELECT id FROM video_site WHERE value = ?', (value,)).fetchone()
        sid = row[0] if row else self.c.execute('INSERT INTO video_site (value) VALUES (?)', (value,)).lastrowid
        if vid:
            self.c.execute('INSERT INTO video_site_junction (video_id, site_id) VALUES (?, ?)', (vid, sid))
        self.c.commit()

    def sites_of(self, vid):
        return sorted(r[0] for r in self.c.execute('SELECT s.value FROM video_site_junction j JOIN video_site s '
                                                   'ON s.id = j.site_id WHERE j.video_id = ?', (vid,)))

    def all_sites(self):
        return sorted(r[0] for r in self.c.execute('SELECT value FROM video_site'))

    def rows(self):
        return {r['value']: r for r in dm.tag_cleanup_preview(self.c)['site_format']}

    def test_preview(self):
        self.site('v1', 'Sunny Beach Days')
        self.site('v2', 'fine.site')
        self.site(None, 'Old Site')
        self.site(None, 'kept.default')
        self.c.execute("INSERT INTO library_default_site (library, site_value) VALUES ('Lib', 'kept.default')")
        self.c.commit()
        rows = self.rows()
        self.assertEqual(rows['Sunny Beach Days']['to'], 'sunny.beach.days')
        self.assertEqual(rows['Old Site']['to'], '')
        self.assertNotIn('fine.site', rows)
        self.assertNotIn('kept.default', rows)              # a library's default site, even unused

    def test_spelling_groups_are_left_to_their_own_section(self):
        self.site('v1', 'Examplestudio')
        self.site('v2', 'examplestudio')
        self.assertNotIn('Examplestudio', self.rows())

    def test_rename_merge_and_remove(self):
        self.site('v1', 'Lpi')
        self.site('v2', 'Big Site')
        self.site(None, 'big.site')                         # unused, so not a spelling group: the rename lands on it
        self.site('v3', 'other.site')
        self.site(None, 'Old Site')
        rows = self.rows()
        self.assertTrue(rows['Big Site']['merge'])
        self.assertNotIn('big.site', rows)                  # a rename's target isn't removed as unused
        out = dm.tag_cleanup_apply(self.c, [], {}, backup=False,
                                   site_format={'Lpi': 'L P I', 'Big Site': 'big.site', 'Old Site': '', 'not.listed': 'x'})
        self.assertEqual(out['applied']['site_renamed'], 2)
        self.assertEqual(out['applied']['site_removed'], 1)
        self.assertEqual(self.sites_of('v1'), ['l.p.i'])    # an edited name is put in your format too
        self.assertEqual(self.sites_of('v2'), ['big.site'])
        self.assertEqual(self.all_sites(), ['big.site', 'l.p.i', 'other.site'])

    def test_default_site_follows_a_rename(self):
        self.site('v1', 'Some Site')
        self.c.execute("INSERT INTO library_default_site (library, site_value) VALUES ('Lib', 'Some Site')")
        self.c.commit()
        dm.tag_cleanup_apply(self.c, [], {}, backup=False, site_format={'Some Site': 'some.site'})
        self.assertEqual(self.c.execute("SELECT site_value FROM library_default_site").fetchone()[0], 'some.site')


if __name__ == '__main__':
    unittest.main()
