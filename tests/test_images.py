"""
Images tab (images.py): the scan's sync, skipped screenshot folders, albums by folder, thumbnails.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import images                                         # noqa: E402


def _jpeg(path, size=(200, 100), orientation=None):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new('RGB', size, (200, 30, 30))
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        im.save(path, 'JPEG', exif=exif)
    else:
        im.save(path, 'JPEG')


class Images(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='lustr-img-')
        self.root = os.path.join(self.dir, 'Lib')
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)')
        images.ensure_schema(self.conn)

    def add(self, *rel):
        paths = []
        for r in rel:
            p = os.path.join(self.root, r)
            _jpeg(p)
            paths.append(p)
        return paths

    def sync(self, paths):
        return images.sync(self.conn, 'Lib', [self.root], paths)

    def test_sync_adds_marks_gone_and_brings_back(self):
        a, b = self.add('Set A/1.jpg', 'Set A/2.jpg')
        self.assertEqual(self.sync([a, b])['added'], 2)
        self.assertEqual(self.sync([a])['removed'], 1)
        self.assertEqual(len(images.album_images(self.conn, os.path.dirname(a))), 1)
        self.assertEqual(self.sync([a, b])['restored'], 1)
        self.assertEqual(self.sync([a, b]), {'added': 0, 'removed': 0, 'restored': 0})

    def test_an_offline_root_is_left_alone(self):
        a, = self.add('Set A/1.jpg')
        self.sync([a])
        images.sync(self.conn, 'Lib', [], [])                     # this root wasn't walked
        self.assertEqual(len(images.album_images(self.conn, os.path.dirname(a))), 1)

    def test_screenshot_folders_are_skipped_at_any_depth(self):
        self.sync(self.add('Set A/1.jpg', 'Set A/Screens/s1.jpg', 'Set B/thumbnails/x/t.jpg', 'SCREENSHOTS/z.jpg',
                           'Straight screens/q.jpg'))
        self.assertEqual([a['name'] for a in images.albums(self.conn, ['Lib'])], ['Set A'])
        self.assertEqual(images.counts(self.conn, ['Lib']), {'Lib': 1})
        images.set_skip_folders(self.conn, ['Screens'])                      # applies at once, no rescan
        self.assertEqual(sorted(a['name'] for a in images.albums(self.conn, ['Lib'])), ['SCREENSHOTS', 'Set A', 'x'])
        self.assertTrue(images.skipped('/m/Lib/Straight screens', '/m/Lib', {'screens'}))        # a word in the name
        self.assertFalse(images.skipped('/m/Lib/Screensaver Shoot', '/m/Lib', {'screens'}))     # not a whole word

    def test_albums_name_same_named_folders_by_their_parent_and_sort(self):
        self.sync(self.add('Kat/Images/1.jpg', 'Kat/Images/2.jpg', 'Aspen/Images/1.jpg', 'Solo/img10.jpg', 'Solo/img2.jpg'))
        names = [a['name'] for a in images.albums(self.conn, ['Lib'])]
        self.assertEqual(names, ['Aspen / Images', 'Kat / Images', 'Solo'])
        self.assertEqual(images.albums(self.conn, ['Lib'], sort='count')[0]['name'], 'Kat / Images')
        solo = [i['filename'] for i in images.album_images(self.conn, os.path.join(self.root, 'Solo'))]
        self.assertEqual(solo, ['img2.jpg', 'img10.jpg'])                       # natural order
        self.assertEqual(images.albums(self.conn, ['Lib'], q='aspen')[0]['count'], 1)
        self.assertEqual(images.albums(self.conn, ['Other']), [])

    def test_cover_and_favorites(self):
        a, b = self.add('Set/1.jpg', 'Set/2.jpg')
        self.sync([a, b])
        folder = os.path.dirname(a)
        ids = [i['id'] for i in images.album_images(self.conn, folder)]
        self.assertEqual(images.albums(self.conn, ['Lib'])[0]['cover_id'], ids[0])     # first by name
        images.set_cover(self.conn, folder, ids[1])
        images.set_favorite(self.conn, folder=folder, on=True)
        images.set_favorite(self.conn, ids=[ids[0]], on=True)
        al = images.albums(self.conn, ['Lib'])[0]
        self.assertEqual((al['cover_id'], al['favorite']), (ids[1], True))
        self.assertTrue(images.album_images(self.conn, folder)[0]['favorite'])

    def test_thumbnail_follows_the_exif_rotation(self):
        p = os.path.join(self.root, 'Rot/r.jpg')
        _jpeg(p, size=(200, 100), orientation=6)                            # stored sideways
        self.sync([p])
        iid = images.album_images(self.conn, os.path.dirname(p))[0]['id']
        out = images.thumb(self.conn, iid, self.dir)
        self.assertTrue(out and out.endswith('.webp') and os.path.exists(out))
        self.assertEqual(images.album_images(self.conn, os.path.dirname(p))[0]['width'], 100)   # upright: 100 x 200
        self.assertEqual(images.thumb(self.conn, iid, self.dir), out)                           # made once

    def test_gone_or_unknown_images_are_not_served(self):
        a, = self.add('Set/1.jpg')
        self.sync([a])
        iid = images.album_images(self.conn, os.path.dirname(a))[0]['id']
        self.assertIsNotNone(images.get(self.conn, iid))
        self.sync([])
        self.assertIsNone(images.get(self.conn, iid))
        self.assertIsNone(images.get(self.conn, 9999))
        self.assertIsNone(images.thumb(self.conn, iid, self.dir))


if __name__ == '__main__':
    unittest.main()
