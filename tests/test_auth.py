"""
Login and session tests. Run inside the image, with the source mounted over /app:

    docker run --rm -v "$PWD":/app -w /app lustr:0.1.0 python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import time
import unittest

_DATA = tempfile.mkdtemp(prefix='lustr-test-')
os.environ['LUSTR_DATA_DIR'] = _DATA                 # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException                     # noqa: E402
from starlette.requests import Request                # noqa: E402
from starlette.responses import Response              # noqa: E402

import auth_sessions                                  # noqa: E402
import helpers                                        # noqa: E402
import config                                         # noqa: E402
from model import UserModel                           # noqa: E402
import routes.auth as auth                            # noqa: E402
import reset_password                                 # noqa: E402


def make_request(cookie=None, headers=None, method='GET', scheme='http', client='10.0.0.5'):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookie:
        hdrs.append((b'cookie', f'{config.AUTH_COOKIE_NAME}={cookie}'.encode()))
    if not any(k == b'host' for k, _ in hdrs):
        hdrs.append((b'host', b'lustr.local:8008'))
    return Request({'type': 'http', 'method': method, 'path': '/', 'headers': hdrs, 'scheme': scheme,
                    'query_string': b'', 'client': (client, 5000), 'server': ('lustr.local', 8008)})


def cookie_from(response):
    raw = response.headers.get('set-cookie', '')
    return raw.split(';')[0].split('=', 1)[1], raw


class SessionTests(unittest.TestCase):
    def setUp(self):
        auth_sessions.set_db_path(os.path.join(_DATA, 'library.db'))
        auth_sessions.end_all()
        helpers.SESSION['timeout'] = 900

    def age(self, token, idle=0, created=0):
        """Pretend the session was last used `idle` s ago and made `created` s ago."""
        now = time.time()
        c = auth_sessions._conn()
        c.execute('UPDATE sessions SET last_active = ?, created = ? WHERE token_hash = ?',
                  (now - idle, now - max(idle, created), auth_sessions._hash(token)))
        c.commit()

    def test_token_is_random_and_only_its_hash_is_stored(self):
        a, b = auth_sessions.create('jane'), auth_sessions.create('jane')
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 40)
        stored = [r[0] for r in auth_sessions._conn().execute('SELECT token_hash FROM sessions')]
        self.assertNotIn(a, stored)
        self.assertIn(auth_sessions._hash(a), stored)

    def test_valid_session(self):
        t = auth_sessions.create('jane')
        self.assertEqual(helpers.check_auth(make_request(t)), 'jane')
        self.assertIsNone(helpers.check_auth(make_request('made-up-token')))
        self.assertIsNone(helpers.check_auth(make_request()))

    def test_idle_session_ends_and_check_auth_cannot_revive_it(self):
        t = auth_sessions.create('jane')
        self.age(t, idle=901)
        self.assertIsNone(helpers.check_auth(make_request(t), check_inactivity=False))   # /api/check_auth
        self.assertEqual(auth_sessions._conn().execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        self.assertIsNone(helpers.check_auth(make_request(t)))

    def test_check_without_activity_does_not_extend(self):
        t = auth_sessions.create('jane')
        self.age(t, idle=600)
        self.assertEqual(helpers.check_auth(make_request(t), check_inactivity=False), 'jane')
        self.age(t, idle=901)                               # still counted from the real activity
        self.assertIsNone(helpers.check_auth(make_request(t)))

    def test_activity_extends(self):
        t = auth_sessions.create('jane')
        self.age(t, idle=600)
        self.assertEqual(helpers.check_auth(make_request(t)), 'jane')
        last = auth_sessions._conn().execute('SELECT last_active FROM sessions').fetchone()[0]
        self.assertAlmostEqual(last, time.time(), delta=5)

    def test_never_timeout_still_ends_after_max_age(self):
        helpers.SESSION['timeout'] = 0
        t = auth_sessions.create('jane')
        self.age(t, idle=10 * 24 * 3600)
        self.assertEqual(helpers.check_auth(make_request(t)), 'jane')
        self.age(t, idle=0, created=auth_sessions.MAX_AGE + 1)
        self.assertIsNone(helpers.check_auth(make_request(t)))

    def test_sessions_survive_a_restart(self):
        t = auth_sessions.create('jane')
        auth_sessions.set_db_path(os.path.join(_DATA, 'library.db'))     # what start-up does
        self.assertEqual(helpers.check_auth(make_request(t)), 'jane')

    def test_purge(self):
        old, fresh = auth_sessions.create('jane'), auth_sessions.create('jane')
        self.age(old, idle=1000)
        self.assertEqual(auth_sessions.purge(900), 1)
        self.assertEqual(helpers.check_auth(make_request(fresh)), 'jane')

    def test_end_all_keeps_one(self):
        a, b, c = (auth_sessions.create('jane') for _ in range(3))
        self.assertEqual(auth_sessions.end_all('jane', keep=b), 2)
        self.assertIsNone(helpers.check_auth(make_request(a)))
        self.assertEqual(helpers.check_auth(make_request(b)), 'jane')


class LimiterTests(unittest.TestCase):
    def test_sixth_try_refused_until_window_passes(self):
        lim = helpers.LoginLimiter(limit=5, window=300)
        for i in range(5):
            self.assertEqual(lim.retry_after('1.2.3.4', now=1000 + i), 0)
            lim.failed('1.2.3.4', now=1000 + i)
        self.assertGreater(lim.retry_after('1.2.3.4', now=1010), 0)
        self.assertEqual(lim.retry_after('5.6.7.8', now=1010), 0)            # other addresses unaffected
        self.assertEqual(lim.retry_after('1.2.3.4', now=1000 + 301), 0)       # the oldest failure expired

    def test_success_clears(self):
        lim = helpers.LoginLimiter(limit=5, window=300)
        for i in range(4):
            lim.failed('1.2.3.4', now=1000 + i)
        lim.succeeded('1.2.3.4')
        lim.failed('1.2.3.4', now=1005)
        self.assertEqual(lim.retry_after('1.2.3.4', now=1006), 0)


class OriginTests(unittest.TestCase):
    def test_same_origin(self):
        self.assertTrue(helpers.same_origin(make_request(headers={'Origin': 'http://lustr.local:8008'})))
        self.assertTrue(helpers.same_origin(make_request(headers={'Referer': 'http://lustr.local:8008/x'})))
        self.assertTrue(helpers.same_origin(make_request()))                  # no browser headers: curl etc.

    def test_other_site_refused(self):
        self.assertFalse(helpers.same_origin(make_request(headers={'Origin': 'https://evil.example'})))
        self.assertFalse(helpers.same_origin(make_request(headers={'Referer': 'http://lustr.local.evil.example/'})))
        self.assertFalse(helpers.same_origin(make_request(headers={'Origin': 'null'})))

    def test_reverse_proxy(self):
        r = make_request(headers={'Origin': 'https://videos.example.org', 'Host': 'lustr:8008',
                                  'X-Forwarded-Host': 'videos.example.org', 'X-Forwarded-Proto': 'https'})
        self.assertTrue(helpers.same_origin(r))
        self.assertTrue(helpers.request_is_https(r))
        self.assertFalse(helpers.request_is_https(make_request()))


class RouteTests(unittest.TestCase):
    def setUp(self):
        for f in (config.USER_FILE,):
            if os.path.exists(f):
                os.remove(f)
        auth_sessions.set_db_path(os.path.join(_DATA, 'library.db'))
        auth_sessions.end_all()
        helpers.SESSION['timeout'] = 900
        helpers.login_limiter = helpers.LoginLimiter()
        auth.login_limiter = helpers.login_limiter
        self.users = UserModel()
        auth.set_user_model(self.users)

    def signup(self, name='Jane', pw='correct-horse'):
        resp = Response()
        auth.signup(make_request(), resp, auth.AuthRequest(username=name, password=pw), self.users)
        return cookie_from(resp)

    def login(self, name='jane', pw='correct-horse', client='10.0.0.5', scheme='http', headers=None):
        resp = Response()
        auth.login(make_request(client=client, scheme=scheme, headers=headers), resp,
                   auth.AuthRequest(username=name, password=pw), self.users)
        return cookie_from(resp)

    def test_signup_once_short_password_refused(self):
        with self.assertRaises(HTTPException) as e:
            self.signup(pw='short')
        self.assertEqual(e.exception.status_code, 400)
        token, raw = self.signup()
        self.assertEqual(helpers.check_auth(make_request(token)), 'jane')       # stored lower-case
        self.assertIn('HttpOnly', raw)
        self.assertNotIn('Max-Age', raw)                                        # ends with the browser
        with self.assertRaises(HTTPException) as e:
            self.signup('john')
        self.assertEqual(e.exception.status_code, 403)

    def test_cookie_secure_over_https(self):
        self.signup()
        self.assertNotIn('Secure', self.login()[1])
        self.assertIn('Secure', self.login(headers={'X-Forwarded-Proto': 'https'})[1])

    def test_logout_ends_session(self):
        token, _ = self.signup()
        auth.logout(Response(), make_request(token))
        self.assertIsNone(helpers.check_auth(make_request(token)))

    def test_rate_limit(self):
        self.signup()
        for _ in range(5):
            with self.assertRaises(HTTPException) as e:
                self.login(pw='wrong-password')
            self.assertEqual(e.exception.status_code, 401)
        with self.assertRaises(HTTPException) as e:
            self.login()                                    # even the right password waits
        self.assertEqual(e.exception.status_code, 429)
        self.assertIn('Retry-After', e.exception.headers)
        self.login(client='10.0.0.6')                       # another device is fine

    def test_change_password(self):
        mine, _ = self.signup()
        other, _ = self.login()
        with self.assertRaises(HTTPException) as e:
            auth.change_password(make_request(mine), auth.PasswordChange(current_password='nope-nope', new_password='new-password-1'), self.users)
        self.assertEqual(e.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            auth.change_password(make_request(mine), auth.PasswordChange(current_password='correct-horse', new_password='short'), self.users)
        r = auth.change_password(make_request(mine), auth.PasswordChange(current_password='correct-horse', new_password='new-password-1'), self.users)
        self.assertEqual(r['other_sessions_ended'], 1)
        self.assertEqual(helpers.check_auth(make_request(mine)), 'jane')
        self.assertIsNone(helpers.check_auth(make_request(other)))
        time.sleep(0.2)                                     # the save runs on a thread
        self.assertFalse(UserModel().verify_user('jane', 'correct-horse'))
        self.assertTrue(UserModel().verify_user('jane', 'new-password-1'))

    def test_reset_password_script(self):
        token, _ = self.signup()
        reset_password.reset('jane', 'from-the-server')
        self.assertIsNone(helpers.check_auth(make_request(token)))
        self.assertTrue(self.users.verify_user('jane', 'from-the-server'))      # the running server sees it
        self.assertFalse(self.users.verify_user('jane', 'correct-horse'))


if __name__ == '__main__':
    unittest.main()
