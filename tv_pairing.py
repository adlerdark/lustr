# This file is: ./tv_pairing.py
"""
Linking a TV with a 4-digit code (routes/tv.py).

The TV asks for a pairing and shows its code; the owner types the code in the web app
(Settings -> Account & Security -> Link a TV); the TV, polling with its secret pair_id, then gets a
login cookie for a session of at most TV_SESSION_MAX_AGE.

A code is short, so the safety rests on: only a logged-in owner can approve, a code lasts
PAIR_TTL, the TV never sends a code (it holds a long random pair_id that is never shown or
logged), and wrong codes are rate-limited (routes/tv.py). In memory only: a restart cancels
pending pairings and the TV asks for a new code.
"""
import hashlib
import secrets
import threading
import time
from typing import Optional

PAIR_TTL = 300                 # a code is valid for 5 minutes
MAX_PENDING = 20               # the oldest pending pairing is dropped beyond this
TV_SESSION_MAX_AGE = 4 * 3600  # a TV login lasts at most 4 hours
POLL_EVERY = 2

_pending = {}                  # sha256(pair_id) -> {code, created, device_name, approved_by, ip}
_lock = threading.Lock()


def _hash(pair_id: str) -> str:
    return hashlib.sha256(pair_id.encode('utf-8')).hexdigest()


def _purge(now: float):
    for k in [k for k, p in _pending.items() if now - p['created'] > PAIR_TTL]:
        del _pending[k]


def start(device_name: Optional[str], ip: str, now: Optional[float] = None) -> dict:
    """A new pairing -> {pair_id, code, expires_in, poll_every}."""
    now = time.time() if now is None else now
    name = (device_name or '').strip()[:60] or 'TV'
    pair_id = secrets.token_urlsafe(32)
    with _lock:
        _purge(now)
        while len(_pending) >= MAX_PENDING:
            del _pending[min(_pending, key=lambda k: _pending[k]['created'])]
        taken = {p['code'] for p in _pending.values()}
        code = f'{secrets.randbelow(10000):04d}'
        while code in taken:
            code = f'{secrets.randbelow(10000):04d}'
        _pending[_hash(pair_id)] = {'code': code, 'created': now, 'device_name': name, 'approved_by': None, 'ip': ip}
    return {'pair_id': pair_id, 'code': code, 'expires_in': PAIR_TTL, 'poll_every': POLL_EVERY}


def approve(code: str, username: str, now: Optional[float] = None) -> Optional[dict]:
    """The owner typed this code: -> {device_name}, or None when no TV is showing it."""
    now = time.time() if now is None else now
    code = (code or '').strip()
    if len(code) != 4 or not code.isdigit():
        return None
    with _lock:
        _purge(now)
        for p in _pending.values():
            if p['code'] == code and p['approved_by'] is None:
                p['approved_by'] = username
                return {'device_name': p['device_name']}
    return None


def claim(pair_id: str, now: Optional[float] = None) -> tuple:
    """The TV asks: -> ('pending', None) | ('expired', None) | ('approved', (username, device_name)).
    An approved pairing is used up; an unknown pair_id is 'expired' (never says whether it existed)."""
    now = time.time() if now is None else now
    if not pair_id or not isinstance(pair_id, str):
        return 'expired', None
    h = _hash(pair_id)
    with _lock:
        _purge(now)
        p = _pending.get(h)
        if not p:
            return 'expired', None
        if p['approved_by'] is None:
            return 'pending', None
        del _pending[h]
        return 'approved', (p['approved_by'], p['device_name'])


def reset():
    """Forget every pairing (tests)."""
    with _lock:
        _pending.clear()
