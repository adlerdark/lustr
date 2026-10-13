"""
Cast photo cropping: the original is kept, so re-cropping starts from the whole picture.

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image                                 # noqa: E402

import database                                       # noqa: E402


def png(w, h):
    out = io.BytesIO()
    Image.new('RGB', (w, h), (200, 30, 30)).save(out, format='PNG')
    return out.getvalue()


def size(data):
    return Image.open(io.BytesIO(data)).size


class CropTests(unittest.TestCase):
    def setUp(self):
        self.db = database.Database(os.path.join(tempfile.mkdtemp(dir=_DATA), 'library.db'))
        self.assertTrue(self.db.save_cast_photo('Jane Doe', png(400, 300), 'image/png'))

    def test_crop_recrop_reset(self):
        self.assertEqual(self.db.crop_cast_photo('Jane Doe', {'x': .25, 'y': .25, 'w': .5, 'h': .5}), {'cropped': True})
        p = self.db.get_cast_photo('Jane Doe')
        self.assertEqual((size(p['data']), p['mime_type']), ((200, 150), 'image/jpeg'))
        self.assertLessEqual(size(self.db.get_cast_photo('Jane Doe', thumbnail=True)['data'])[0], 216)
        o = self.db.get_cast_photo('Jane Doe', original=True)
        self.assertEqual((size(o['data']), o['mime_type'], o['cropped']), ((400, 300), 'image/png', True))
        self.db.crop_cast_photo('Jane Doe', {'x': 0, 'y': 0, 'w': 1, 'h': .5})       # from the original, not the crop
        self.assertEqual(size(self.db.get_cast_photo('Jane Doe')['data']), (400, 150))
        self.assertEqual(self.db.crop_cast_photo('Jane Doe', reset=True), {'cropped': False})
        self.assertEqual(size(self.db.get_cast_photo('Jane Doe')['data']), (400, 300))
        self.assertFalse(self.db.get_cast_photo('Jane Doe', original=True)['cropped'])

    def test_new_photo_clears_original(self):
        self.db.crop_cast_photo('Jane Doe', {'x': 0, 'y': 0, 'w': .5, 'h': .5})
        self.db.save_cast_photo('Jane Doe', png(100, 100), 'image/png')
        self.assertFalse(self.db.get_cast_photo('Jane Doe', original=True)['cropped'])
        self.assertEqual(size(self.db.get_cast_photo('Jane Doe', original=True)['data']), (100, 100))

    def test_no_photo(self):
        with self.assertRaises(ValueError):
            self.db.crop_cast_photo('Nobody', {'x': 0, 'y': 0, 'w': 1, 'h': 1})


if __name__ == '__main__':
    unittest.main()
