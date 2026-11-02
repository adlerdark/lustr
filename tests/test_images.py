"""
Images tab (images.py): the scan's sync, skipped screenshot folders, albums by folder, thumbnails.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile

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

    def zip(self, rel, names, mtime=None):
        src = os.path.join(self.dir, 'src.jpg')
        _jpeg(src)
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with zipfile.ZipFile(p, 'w') as z:
            for n in names:
                z.write(src, n)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_a_zip_is_an_album_read_without_unpacking(self):
        p = self.zip('Set One.zip', ['1.jpg', '2.jpg', 'notes.txt', '__MACOSX/._1.jpg', '../out.jpg'])
        self.assertEqual(self.sync([p])['added'], 2)
        al = images.albums(self.conn, ['Lib'])
        self.assertEqual([(a['name'], a['count']) for a in al], [('Set One', 2)])
        items = images.album_images(self.conn, p)
        self.assertEqual([i['filename'] for i in items], ['1.jpg', '2.jpg'])
        self.assertTrue(images.archived(self.conn, items[0]['id']).startswith(b'\xff\xd8'))        # a JPEG
        out = images.thumb(self.conn, items[0]['id'], self.dir)
        self.assertTrue(out and os.path.exists(out))
        self.assertEqual(images.album_images(self.conn, p)[0]['width'], 200)

    def test_a_zip_folder_inside_is_its_own_album_and_can_be_skipped(self):
        p = self.zip('Set.zip', ['pics/1.jpg', 'thumbs/1.jpg'])
        self.sync([p])
        self.assertEqual([a['name'] for a in images.albums(self.conn, ['Lib'])], ['pics'])
        self.assertEqual(images.counts(self.conn, ['Lib']), {'Lib': 1})

    def test_a_plain_image_has_no_archived_bytes(self):
        a, = self.add('Set/1.jpg')
        self.sync([a])
        self.assertIsNone(images.archived(self.conn, images.album_images(self.conn, os.path.dirname(a))[0]['id']))

    def test_a_zip_is_only_read_again_when_it_changes(self):
        p = self.zip('Set.zip', ['1.jpg'], mtime=1000)
        self.sync([p])
        reads = []
        real, images._read_zip = images._read_zip, lambda z: reads.append(z) or real(z)
        self.addCleanup(setattr, images, '_read_zip', real)
        self.assertEqual(self.sync([p]), {'added': 0, 'removed': 0, 'restored': 0})
        self.assertEqual(reads, [])
        first = images.album_images(self.conn, p)[0]
        self.zip('Set.zip', ['2.jpg'] + ['1.jpg'], mtime=2000)
        self.assertEqual(self.sync([p])['added'], 1)
        self.assertEqual(reads, [p])
        self.assertEqual([i['mtime'] for i in images.album_images(self.conn, p)], [2000, 2000])    # a new thumbnail URL
        self.assertEqual(images.album_images(self.conn, p)[0]['id'], first['id'])                  # favorites stay

    def test_a_removed_zip_goes_and_comes_back_and_an_unreadable_one_is_left_alone(self):
        p = self.zip('Set.zip', ['1.jpg'], mtime=1000)
        self.sync([p])
        self.assertEqual(self.sync([])['removed'], 1)
        self.assertEqual(self.sync([p])['restored'], 1)
        os.remove(p)                                                        # stat fails, e.g. the share dropped
        self.assertEqual(self.sync([p]), {'added': 0, 'removed': 0, 'restored': 0})

    def test_a_bad_or_half_downloaded_zip_is_skipped_until_it_changes(self):
        p = os.path.join(self.root, 'Broken.zip')
        os.makedirs(self.root, exist_ok=True)
        with open(p, 'wb') as f:
            f.write(b'not a zip')
        self.assertEqual(self.sync([p])['added'], 0)
        os.remove(p)
        self.zip('Broken.zip', ['1.jpg'], mtime=int(time.time()) - 60)
        self.assertEqual(self.sync([p])['added'], 1)

    def test_file_info_gives_the_paths(self):
        a, = self.add('Set/1.jpg')
        z = self.zip('Pack.zip', ['pics/1.jpg', 'pics/2.jpg'])
        self.sync([a, z])
        one = images.image_info(self.conn, images.album_images(self.conn, os.path.dirname(a))[0]['id'])
        self.assertEqual((one['path'], one['archive'], one['library']), (a, None, 'Lib'))
        inner = os.path.join(z, 'pics')
        two = images.image_info(self.conn, images.album_images(self.conn, inner)[1]['id'])
        self.assertEqual((two['path'], two['archive'], two['member']), (os.path.join(inner, '2.jpg'), z, 'pics/2.jpg'))
        al = images.album_info(self.conn, inner)
        self.assertEqual((al['path'], al['count'], al['archive']), (inner, 2, z))
        self.assertIsNone(images.album_info(self.conn, '/nowhere'))
        self.assertIsNone(images.image_info(self.conn, 9999))

    # ----- album details -----
    def albums3(self):
        a, b, c, d = self.add('Kat Set/1.jpg', 'Beach/1.jpg', 'Beach/2.jpg', 'Zed/1.jpg')
        self.sync([a, b, c, d])
        return [os.path.dirname(x) for x in (a, b, d)]

    def test_rename_and_metadata_for_one_or_several_albums(self):
        kat, beach, zed = self.albums3()
        images.set_album_meta(self.conn, [kat], updates={'title': 'Kat at Home', 'year': '2021'},
                              add={'cast': ['Kat Doe'], 'tags': ['outdoor'], 'site': ['ExampleSite']})
        images.set_album_meta(self.conn, [kat, beach], add={'tags': ['Outdoor', 'bikini'], 'cast': ['Ann Roe']})
        m = images.album_meta(self.conn)
        self.assertEqual(m[kat]['tags'], ['bikini', 'outdoor'])                         # 'Outdoor' not added twice
        self.assertEqual(m[beach]['cast'], ['Ann Roe'])
        self.assertEqual(m[kat]['year'], ['2021'])
        self.assertEqual(images.album_title(self.conn, kat), 'Kat at Home')
        images.set_album_meta(self.conn, [kat, beach], remove={'cast': ['Ann Roe']}, updates={'year': ''})
        m = images.album_meta(self.conn)
        self.assertEqual(m[kat]['cast'], ['Kat Doe'])
        self.assertNotIn('cast', m.get(beach, {}))
        self.assertNotIn('year', m[kat])
        images.set_album_meta(self.conn, [kat], updates={'title': ''})                 # back to the folder's name
        self.assertEqual(images.album_title(self.conn, kat), 'Kat Set')

    def test_search_filter_sort_and_pages(self):
        kat, beach, zed = self.albums3()
        images.set_album_meta(self.conn, [kat], add={'cast': ['Zoe Moe'], 'tags': ['indoor'], 'site': ['SiteA']})
        images.set_album_meta(self.conn, [zed], add={'cast': ['Amy Lee'], 'tags': ['outdoor']})
        images.set_album_meta(self.conn, [beach], updates={'title': '#1 Beach'})
        names = lambda **k: [a['name'] for a in images.albums(self.conn, ['Lib'], **k)]
        self.assertEqual(names(), ['#1 Beach', 'Kat Set', 'Zed'])
        self.assertEqual(names(sort='cast'), ['Zed', 'Kat Set', '#1 Beach'])           # no cast: last
        self.assertEqual(names(sort='count')[0], '#1 Beach')
        self.assertEqual(names(q='zoe'), ['Kat Set'])                                  # any value is searched
        self.assertEqual(names(q='beach'), ['#1 Beach'])
        self.assertEqual(names(tags='outdoor'), ['Zed'])
        self.assertEqual(names(tags_exclude='outdoor'), ['#1 Beach', 'Kat Set'])
        self.assertEqual(names(sites='sitea'), ['Kat Set'])
        self.assertEqual(names(cast='amy lee'), ['Zed'])
        page = images.listing(self.conn, ['Lib'], 'name', '', page=2, limit=2)
        self.assertEqual(([a['name'] for a in page['albums']], page['total']), (['Zed'], 3))
        self.assertEqual(page['letters'], [{'letter': '#', 'index': 0}, {'letter': 'K', 'index': 1}, {'letter': 'Z', 'index': 2}])
        self.assertTrue(page['albums'][0]['cover_id'])
        self.assertEqual(images.listing(self.conn, ['Lib'], 'cast')['letters'], [{'letter': 'A', 'index': 0}, {'letter': 'Z', 'index': 1}])
        self.assertEqual(images.filter_counts(self.conn, ['Lib']), {'tags': {'indoor': 1, 'outdoor': 1}, 'sites': {'SiteA': 1}})

    def _videos(self, *paths):
        self.conn.execute('CREATE TABLE IF NOT EXISTS file_versions (path TEXT PRIMARY KEY, video_id TEXT, is_preferred INTEGER, deleted_at TEXT)')
        for i, p in enumerate(paths):
            self.conn.execute('INSERT INTO file_versions VALUES (?, ?, 1, NULL)', (os.path.join(self.root, p), f'v{i}'))
        self.conn.commit()

    def test_linking_and_suggesting_videos(self):
        a, = self.add('Set Kat/pics/1.jpg')
        self.sync([a])
        album = os.path.dirname(a)
        self._videos('Set Kat/kat.mp4', 'Set Kat/pics/x.mp4', 'Other/far.mp4', 'loose.mp4', 'Set Kat/vids/v.mp4')
        sug = [os.path.basename(p) for _v, p in images.suggest_videos(self.conn, album)]
        self.assertEqual(sug[0], 'x.mp4')                                              # in the album's own folder first
        self.assertEqual(set(sug), {'x.mp4', 'kat.mp4', 'v.mp4'})                    # a folder next to it too                               # not further up or elsewhere
        images.link_videos(self.conn, album, ['v0'])
        self.assertEqual(images.linked_videos(self.conn, album), [('v0', os.path.join(self.root, 'Set Kat/kat.mp4'))])
        self.assertNotIn('kat.mp4', [os.path.basename(p) for _v, p in images.suggest_videos(self.conn, album)])
        self.assertEqual(images.albums_of_video(self.conn, 'v0'), [{'folder': album, 'name': 'pics'}])
        images.link_videos(self.conn, album, ['v0'], on=False)
        self.assertEqual(images.linked_videos(self.conn, album), [])

    def test_an_album_at_the_library_folder_suggests_only_its_own(self):
        a, = self.add('1.jpg')
        self.sync([a])
        self._videos('top.mp4', 'Sub/deep.mp4')
        self.assertEqual(sorted(os.path.basename(p) for _v, p in images.suggest_videos(self.conn, self.root)), ['deep.mp4', 'top.mp4'])

    def test_sizes_are_read_from_the_header(self):
        a, = self.add('S/wide.jpg')
        r = os.path.join(self.root, 'S/rot.jpg')
        _jpeg(r, size=(200, 100), orientation=6)
        z = self.zip('Z.zip', ['p.jpg'])
        self.sync([a, r, z])
        self.assertEqual(images.fill_sizes(self.conn, os.path.dirname(a)), 2)
        dims = {i['filename']: (i['width'], i['height']) for i in images.album_images(self.conn, os.path.dirname(a))}
        self.assertEqual(dims, {'wide.jpg': (200, 100), 'rot.jpg': (100, 200)})
        self.assertEqual(images.fill_sizes(self.conn, z), 1)
        self.assertEqual(images.fill_sizes(self.conn, z), 0)                            # once

    def test_album_covers_come_with_their_shape(self):
        a, = self.add('Set/1.jpg')
        self.sync([a])
        al = images.albums(self.conn, ['Lib'])[0]
        self.assertEqual((al['cover_w'], al['cover_h']), (200, 100))                 # read on the way

    def test_a_big_album_reads_the_first_sizes_now_in_name_order(self):
        paths = self.add(*[f'Big/{n}.jpg' for n in (10, 2, 1, 3)])
        self.sync(paths)
        folder = os.path.dirname(paths[0])
        self.assertEqual(images.fill_sizes(self.conn, folder, limit=2), 2)
        self.assertEqual({i['filename'] for i in images.album_images(self.conn, folder) if i['width']}, {'1.jpg', '2.jpg'})
        self.assertEqual(len(images.sizes(self.conn, folder)), 2)
        self.assertEqual(images.fill_sizes(self.conn, folder), 2)                       # the rest

    def test_an_older_database_gains_the_zip_columns(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE images (id INTEGER PRIMARY KEY, path TEXT UNIQUE, library TEXT, folder TEXT, '
                     'deleted_at TEXT)')
        images.ensure_schema(conn)
        images.ensure_schema(conn)
        self.assertTrue({'archive', 'member'} <= {r[1] for r in conn.execute('PRAGMA table_info(images)')})
        self.assertIn('title', {r[1] for r in conn.execute('PRAGMA table_info(image_albums)')})


if __name__ == '__main__':
    unittest.main()
