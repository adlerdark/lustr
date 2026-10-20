"""
TV apps: linking a TV with a 4-digit code, TV sessions of at most 4 hours (tv_pairing.py, routes/tv.py).

    docker exec -w /app lustr python3 -m unittest tests.test_tv_pairing -v
"""
import asyncio
import json
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
import config                                         # noqa: E402
import helpers                                        # noqa: E402
import tv_pairing                                     # noqa: E402
import routes.tv as tv                                # noqa: E402
import routes.auth as auth                            # noqa: E402
from model import UserModel                           # noqa: E402


def make_request(cookie=None, body=None, client='10.0.0.7', path='/', method='POST'):
    hdrs = [(b'host', b'lustr.local:8008')]
    if cookie:
        hdrs.append((b'cookie', f'{config.AUTH_COOKIE_NAME}={cookie}'.encode()))
    raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()

    async def receive():
        return {'type': 'http.request', 'body': raw, 'more_body': False}
    return Request({'type': 'http', 'method': method, 'path': path, 'headers': hdrs, 'scheme': 'http',
                    'query_string': b'', 'client': (client, 5000), 'server': ('lustr.local', 8008)}, receive)


def cookie_from(response):
    raw = response.headers.get('set-cookie', '')
    return (raw.split(';')[0].split('=', 1)[1] if raw else None), raw


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        auth_sessions.set_db_path(os.path.join(_DATA, 'library.db'))
        auth_sessions.end_all()
        helpers.SESSION['timeout'] = 900
        tv_pairing.reset()
        tv.start_limiter = helpers.LoginLimiter(limit=10, window=300)
        tv.code_limiter = helpers.LoginLimiter(limit=5, window=300)
        self.owner = auth_sessions.create('jane')

    def start(self, name='SHIELD Android TV', client='10.0.0.7'):
        return run(tv.pair_start(make_request(body={'device_name': name}, client=client)))

    def poll(self, pair_id):
        resp = Response()
        d = run(tv.pair_poll(make_request(body={'pair_id': pair_id}), resp))
        return d['status'], cookie_from(resp)

    def approve(self, code):
        return tv.pair_approve(make_request(self.owner), tv.ApproveBody(code=code))


class PairingTests(Base):
    def test_full_flow(self):
        s = self.start()
        self.assertEqual((len(s['code']), s['expires_in'], s['poll_every']), (4, 300, 2))
        self.assertEqual(self.poll(s['pair_id'])[0], 'pending')
        self.assertEqual(self.approve(s['code']), {'success': True, 'device_name': 'SHIELD Android TV'})
        status, (token, raw) = self.poll(s['pair_id'])
        self.assertEqual(status, 'approved')
        self.assertIn('Max-Age=14400', raw)
        self.assertIn('HttpOnly', raw)
        row = auth_sessions._conn().execute('SELECT kind, expires, device FROM sessions WHERE token_hash = ?',
                                            (auth_sessions._hash(token),)).fetchone()
        self.assertEqual(row[0], 'tv')
        self.assertAlmostEqual(row[1], time.time() + 4 * 3600, delta=5)
        self.assertEqual(row[2], 'SHIELD Android TV')
        self.assertEqual(helpers.check_auth(make_request(token)), 'jane')
        self.assertEqual(self.poll(s['pair_id'])[0], 'expired')                 # a pair_id is used once
        self.assertEqual(self.poll('made-up')[0], 'expired')

    def test_wrong_codes(self):
        s = self.start()
        wrong = '0000' if s['code'] != '0000' else '1111'
        for _ in range(5):
            with self.assertRaises(HTTPException) as e:
                self.approve(wrong)
            self.assertEqual(e.exception.status_code, 404)
        with self.assertRaises(HTTPException) as e:
            self.approve(s['code'])                                             # even the right code waits
        self.assertEqual(e.exception.status_code, 429)
        self.assertIn('Too many wrong codes', e.exception.detail)

    def test_code_expires(self):
        t = time.time()
        s = tv_pairing.start('TV', '1.2.3.4', now=t)
        self.assertIsNone(tv_pairing.approve(s['code'], 'jane', now=t + 301))
        self.assertEqual(tv_pairing.claim(s['pair_id'], now=t + 301)[0], 'expired')

    def test_codes_never_shared(self):
        codes = [tv_pairing.start('TV', '1.2.3.4')['code'] for _ in range(tv_pairing.MAX_PENDING)]
        self.assertEqual(len(set(codes)), len(codes))
        tv_pairing.start('TV', '1.2.3.4')                                        # beyond the cap: the oldest goes
        self.assertEqual(len(tv_pairing._pending), tv_pairing.MAX_PENDING)

    def test_start_rate_limit(self):
        for _ in range(10):
            self.start(client='10.0.0.9')
        with self.assertRaises(HTTPException) as e:
            self.start(client='10.0.0.9')
        self.assertEqual(e.exception.status_code, 429)
        self.assertIn('Retry-After', e.exception.headers)
        self.start(client='10.0.0.10')

    def test_big_or_bad_body_ignored(self):
        resp = Response()
        self.assertEqual(run(tv.pair_poll(make_request(body=b'x' * 2000), resp))['status'], 'expired')
        self.assertEqual(run(tv.pair_poll(make_request(body=b'not json'), resp))['status'], 'expired')


class SessionTests(Base):
    def test_tv_session_ends_at_expires_even_when_active(self):
        token = auth_sessions.create('jane', kind='tv', max_age=4 * 3600, device='TV')
        self.assertEqual(auth_sessions.check_session(token, 900), ('jane', 'tv'))
        c = auth_sessions._conn()
        c.execute('UPDATE sessions SET expires = ? WHERE token_hash = ?', (time.time() - 1, auth_sessions._hash(token)))
        c.commit()
        self.assertIsNone(auth_sessions.check(token, 900))

    def test_end_all_ends_tv_sessions(self):
        token = auth_sessions.create('jane', kind='tv', max_age=4 * 3600)
        auth_sessions.end_all('jane', keep=self.owner)
        self.assertIsNone(auth_sessions.check(token, 900))
        self.assertEqual(auth_sessions.check(self.owner, 900), 'jane')

    def test_list_and_logout(self):
        a = auth_sessions.create('jane', kind='tv', max_age=4 * 3600, device='Living room')
        auth_sessions.create('jane', kind='tv', max_age=4 * 3600, device='Bedroom')
        rows = tv.tv_sessions(make_request(self.owner, method='GET'))['sessions']
        self.assertEqual([r['device'] for r in rows], ['Bedroom', 'Living room'])
        self.assertTrue(all(len(r['id']) == 12 for r in rows))
        living = next(r['id'] for r in rows if r['device'] == 'Living room')
        self.assertEqual(tv.tv_sessions_logout(make_request(self.owner), tv.LogoutBody(id=living))['ended'], 1)
        self.assertIsNone(auth_sessions.check(a, 900))
        self.assertEqual(tv.tv_sessions_logout(make_request(self.owner), tv.LogoutBody(all=True))['ended'], 1)
        self.assertEqual(auth_sessions.check(self.owner, 900), 'jane')               # the browser stays logged in

    def test_purge_drops_expired(self):
        token = auth_sessions.create('jane', kind='tv', max_age=60)
        c = auth_sessions._conn()
        c.execute('UPDATE sessions SET expires = ? WHERE token_hash = ?', (time.time() - 1, auth_sessions._hash(token)))
        c.commit()
        auth_sessions.purge(900)
        self.assertIsNone(c.execute('SELECT 1 FROM sessions WHERE token_hash = ?', (auth_sessions._hash(token),)).fetchone())


class PasswordLoginTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.USER_FILE):
            os.remove(config.USER_FILE)
        auth_sessions.set_db_path(os.path.join(_DATA, 'library.db'))
        auth_sessions.end_all()
        auth.login_limiter = helpers.login_limiter = helpers.LoginLimiter()
        self.users = UserModel()
        auth.set_user_model(self.users)
        auth.signup(make_request(), Response(), auth.AuthRequest(username='jane', password='correct-horse'), self.users)

    def login(self, **extra):
        resp = Response()
        auth.login(make_request(), resp, auth.AuthRequest(username='jane', password='correct-horse', **extra), self.users)
        return cookie_from(resp)

    def test_tv_client_gets_a_four_hour_session(self):
        token, raw = self.login(client='tv', device_name='SHIELD')
        self.assertIn('Max-Age=14400', raw)
        self.assertEqual(auth_sessions.check_session(token, 900), ('jane', 'tv'))

    def test_browser_login_unchanged(self):
        token, raw = self.login()
        self.assertNotIn('Max-Age', raw)
        self.assertEqual(auth_sessions.check_session(token, 900), ('jane', 'browser'))


class DisplayFilterTests(unittest.TestCase):
    """A TV keeps its own 'Show libraries and videos' selection; until it has one, the web app's."""
    def test_tv_selection(self):
        import sqlite3
        import display_filter as df
        c = sqlite3.connect(':memory:')
        c.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)')
        everything = df.get_selection(c, 'jane')
        df.set_selection(c, 'jane', ['straight'])
        self.assertEqual(df.get_selection(c, 'jane#tv'), ['straight'])          # follows the web app
        df.set_selection(c, 'jane#tv', ['gay', 'straight'])
        df.set_selection(c, 'jane', everything)
        self.assertEqual(sorted(df.get_selection(c, 'jane#tv')), ['gay', 'straight'])
        self.assertEqual(df.get_selection(c, 'jane'), everything)

    def test_middleware_picks_the_tv_key(self):
        try:
            import web_server
        except Exception as e:
            self.skipTest(f'web_server not importable: {e}')
        import display_filter as df
        auth_sessions.set_db_path(os.path.join(_DATA, 'library.db'))
        seen = []

        async def nxt(req):
            seen.append(df.CURRENT_USER.get())
            return Response('ok')
        for kind in ('browser', 'tv'):
            token = auth_sessions.create('jane', kind=kind, max_age=3600 if kind == 'tv' else None)
            run(web_server.auth_middleware(make_request(token, path='/api/videos', method='GET'), nxt))
        self.assertEqual(seen, ['jane', 'jane#tv'])


class MiddlewareTests(Base):
    """Only the two pairing paths are public under /api/tv."""
    @classmethod
    def setUpClass(cls):
        try:
            import web_server
            cls.mw = staticmethod(web_server.auth_middleware)
        except Exception as e:                       # the app can't start here: skip, don't fail
            raise unittest.SkipTest(f'web_server not importable: {e}')

    def call(self, path, cookie=None):
        async def nxt(req):
            return Response('ok')
        return run(self.mw(make_request(cookie, path=path), nxt)).status_code

    def test_public_and_private(self):
        self.assertEqual(self.call('/api/tv/pair/start'), 200)
        self.assertEqual(self.call('/api/tv/pair/poll'), 200)
        self.assertEqual(self.call('/api/tv/pair/approve'), 401)
        self.assertEqual(self.call('/api/tv/sessions'), 401)
        self.assertEqual(self.call('/api/tv/pair/approve', self.owner), 200)


if __name__ == '__main__':
    unittest.main()
