"""
Pictures as files (photo_store.py): covers and performer photos leave the database, rows not moved
yet still work, and unused files are cleaned up only when old.

    docker exec -w /app lustr python3 -m unittest tests.test_photo_store -v
"""
import io
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import movies                                         # noqa: E402
import photo_store                                    # noqa: E402
from tests.test_cast_names import Base                # noqa: E402


def jpeg(color=(200, 10, 10), size=(40, 60)):
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, format='JPEG')
    return out.getvalue()


class Store(Base):
    def setUp(self):
        super().setUp()
        movies._schema_ready.clear()                    # (keyed by connection id, which a new test's connection can reuse)
        movies.ensure_schema(self.c)
        self.mid = movies.set_video_movie(self.c, 'v1', 'Summer Road Trip 13')

    def test_cover_is_a_file(self):
        pic = jpeg()
        movies.set_cover(self.c, self.mid, pic, 'image/jpeg')
        stored = self.c.execute('SELECT cover FROM movies WHERE id = ?', (self.mid,)).fetchone()[0]
        self.assertTrue(photo_store.is_ref(stored))
        self.assertEqual(movies.cover(self.c, self.mid), (pic, 'image/jpeg'))
        movies.set_cover(self.c, self.mid, None, None)
        self.assertIsNone(movies.cover(self.c, self.mid))

    def test_old_rows_still_read_and_get_moved(self):
        pic = jpeg((0, 0, 255))
        self.c.execute('UPDATE movies SET back_cover = ?, back_cover_mime = ? WHERE id = ?', (pic, 'image/jpeg', self.mid))
        self.c.commit()
        self.assertEqual(movies.cover(self.c, self.mid, back=True)[0], pic)        # bytes in the row: still fine
        self.assertEqual(photo_store.pending(self.c), 1)
        self.assertEqual(photo_store.migrate(self.c, pause=0), 1)
        self.assertEqual(photo_store.pending(self.c), 0)
        self.assertEqual(movies.cover(self.c, self.mid, back=True)[0], pic)

    def test_cast_photo_crop_and_reset(self):
        pic = jpeg(size=(100, 150))
        self.assertTrue(self.db.save_cast_photo('Jane Doe', pic, 'image/jpeg'))
        row = self.c.execute("SELECT photo_data, thumbnail_data FROM cast_photos WHERE cast_name = 'Jane Doe'").fetchone()
        self.assertTrue(photo_store.is_ref(row[0]))
        self.assertIsInstance(row[1], bytes)                                        # thumbnails stay in the database
        self.assertEqual(self.db.get_cast_photo('Jane Doe')['data'], pic)
        self.assertTrue(self.db.crop_cast_photo('Jane Doe', {'x': 0, 'y': 0, 'w': 0.5, 'h': 0.5})['cropped'])
        self.assertEqual(self.db.get_cast_photo('Jane Doe', original=True)['data'], pic)
        self.assertNotEqual(self.db.get_cast_photo('Jane Doe')['data'], pic)
        self.db.crop_cast_photo('Jane Doe', reset=True)
        self.assertEqual(self.db.get_cast_photo('Jane Doe')['data'], pic)

    def test_gc_keeps_used_and_recent_files(self):
        used, unused = jpeg((1, 2, 3)), jpeg((4, 5, 6))
        movies.set_cover(self.c, self.mid, used, 'image/jpeg')
        ref = photo_store.put(unused, 'image/jpeg')
        self.assertEqual(photo_store.gc(self.c)[0], 0)                              # unused but new: kept
        old = time.time() - (photo_store.GC_DAYS + 1) * 86400
        for r in (ref, self.c.execute('SELECT cover FROM movies WHERE id = ?', (self.mid,)).fetchone()[0]):
            os.utime(photo_store._path(r), (old, old))
        self.assertEqual(photo_store.gc(self.c)[0], 1)
        self.assertFalse(os.path.exists(photo_store._path(ref)))
        self.assertEqual(movies.cover(self.c, self.mid)[0], used)

    def test_same_picture_stored_once(self):
        pic = jpeg((9, 9, 9))
        self.assertEqual(photo_store.put(pic, 'image/jpeg'), photo_store.put(pic, 'image/jpeg'))

    def test_bad_reference_is_refused(self):
        self.assertIsNone(photo_store.read('store:../../etc/passwd'))


if __name__ == '__main__':
    unittest.main()
