"""
The update check (update_check.py): the highest vX.Y.Z tag on GitHub against the running version.

    docker exec -w /app lustr python3 -m unittest tests.test_update_check -v
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import update_check as uc                             # noqa: E402


def db():
    c = sqlite3.connect(':memory:', check_same_thread=False)
    c.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)')
    return c


class Versions(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(uc.parse_version('v0.26.10'), (0, 26, 10))
        self.assertEqual(uc.parse_version('1.2.3'), (1, 2, 3))
        for bad in ('v1.2', 'latest', 'v1.2.3-beta', '', None):
            self.assertIsNone(uc.parse_version(bad), bad)

    def test_latest_and_newer(self):
        self.assertEqual(uc.latest_of(['v0.26.9', 'v0.27.0', 'v0.26.10', 'nightly']), '0.27.0')
        self.assertEqual(uc.latest_of(['v0.26.9', 'v0.26.10']), '0.26.10')     # numbers, not text
        self.assertIsNone(uc.latest_of(['nightly']))
        self.assertTrue(uc.is_newer('0.27.0', '0.26.0'))
        self.assertFalse(uc.is_newer('0.26.0', '0.26.0'))
        self.assertFalse(uc.is_newer('0.25.9', '0.26.0'))
        self.assertFalse(uc.is_newer(None, '0.26.0'))


class Checking(unittest.TestCase):
    def test_check_and_status(self):
        c = db()
        st = uc.check_now(c, fetch=lambda: ['v0.1.0', 'v99.0.0'])
        self.assertEqual(st['latest'], '99.0.0')
        s = uc.status(c)
        self.assertTrue(s['newer'])
        self.assertEqual(s['current'], uc.VERSION)
        self.assertTrue(s['url'].endswith('/releases/tag/v99.0.0'))

    def test_not_newer(self):
        c = db()
        uc.check_now(c, fetch=lambda: ['v' + uc.VERSION])
        self.assertFalse(uc.status(c)['newer'])

    def test_error_kept_not_raised(self):
        c = db()
        uc.check_now(c, fetch=lambda: ['v99.0.0'])

        def boom():
            raise OSError('no network')
        st = uc.check_now(c, fetch=boom)
        self.assertIn('no network', st['error'])
        self.assertEqual(st['latest'], '99.0.0')                # the last good answer stays

    def test_stale(self):
        now = time.time()
        self.assertTrue(uc.is_stale({}, now))
        self.assertFalse(uc.is_stale({'checked_at': now - 3600}, now))
        self.assertTrue(uc.is_stale({'checked_at': now - uc.CHECK_EVERY - 1}, now))
        self.assertTrue(uc.is_stale({'checked_at': now - uc.RETRY_AFTER_ERROR - 1, 'error': 'x'}, now))

    def test_disabled_never_fetches_or_shows(self):
        c = db()
        uc.check_now(c, fetch=lambda: ['v99.0.0'])
        uc.set_enabled(c, False)
        calls = []
        self.assertFalse(uc.refresh_in_background(lambda: c, fetch=lambda: calls.append(1) or []))
        self.assertEqual(calls, [])
        self.assertFalse(uc.status(c)['newer'])

    def test_fresh_cache_not_refetched(self):
        c = db()
        uc.check_now(c, fetch=lambda: ['v0.1.0'])
        calls = []
        self.assertFalse(uc.refresh_in_background(lambda: c, fetch=lambda: calls.append(1) or []))
        self.assertEqual(calls, [])

    def test_stale_cache_refetched_in_background(self):
        c = db()
        self.assertTrue(uc.refresh_in_background(lambda: c, fetch=lambda: ['v99.0.0']))
        for _ in range(50):
            if uc.load(c).get('latest'):
                break
            time.sleep(0.05)
        self.assertEqual(uc.load(c)['latest'], '99.0.0')


if __name__ == '__main__':
    unittest.main()
