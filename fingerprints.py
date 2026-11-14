"""
File fingerprints for finding a video's scene on StashDB.

The OSHash (the same one Stash uses) is the file's size plus the sum of the 64-bit words in its first
and last 64 KB, so it reads 128 KB however large the file is. StashDB users submit the fingerprints of
their files; a scene whose fingerprints include yours is almost certainly the same release, with no
title or performer guessing. One request looks up 40 files.

Hashes are kept in file_fingerprints (path, size, mtime): a file that changes is hashed again.
Lookups are remembered for an hour, and the bulk scene matcher asks for the next 40 videos at once
(prefetch) so each video costs no extra request.
"""
import logging
import os
import sqlite3
import struct
import threading
import time

log = logging.getLogger('lustr.fingerprints')

CHUNK = 64 * 1024
BATCH = 40                       # StashDB refuses bigger batches ('too many scenes')
CACHE_SECONDS = 3600

_cache = {}                      # (source, hash) -> (time, [scene, ...])
_lock = threading.Lock()


def oshash(path):
    """-> 16 hex digits, or None for a file too small to have one / that can't be read."""
    try:
        size = os.path.getsize(path)
        if size < CHUNK * 2:
            return None
        h = size
        with open(path, 'rb') as f:
            for offset in (0, size - CHUNK):
                f.seek(offset)
                buf = f.read(CHUNK)
                if len(buf) != CHUNK:
                    return None
                for (v,) in struct.iter_unpack('<Q', buf):
                    h = (h + v) & 0xFFFFFFFFFFFFFFFF
        return '%016x' % h
    except OSError:
        return None


def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS file_fingerprints (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, oshash TEXT)')


def for_path(conn, path):
    """The file's OSHash, from file_fingerprints while the file is unchanged."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    ensure_schema(conn)
    row = conn.execute('SELECT size, mtime, oshash FROM file_fingerprints WHERE path = ?', (path,)).fetchone()
    if row and row[0] == st.st_size and row[1] == int(st.st_mtime):
        return row[2]
    h = oshash(path)
    if h:
        conn.execute('INSERT OR REPLACE INTO file_fingerprints (path, size, mtime, oshash) VALUES (?, ?, ?, ?)',
                     (path, st.st_size, int(st.st_mtime), h))
        conn.commit()
    return h


def _query(fragment):
    return (fragment + ' query($f: [[FingerprintQueryInput!]!]!) { findScenesBySceneFingerprints(fingerprints: $f) '
            '{ ...SC fingerprints { hash algorithm submissions reports } } }')


def lookup(source, hashes):
    """{hash: [compact scene + 'fingerprint': {submissions, reports}]} for these OSHashes (StashDB-style
    sources only); cached for an hour."""
    import ext_db
    now = time.time()
    out, ask = {}, []
    with _lock:
        for h in dict.fromkeys(x for x in hashes if x):
            hit = _cache.get((source, h))
            if hit and now - hit[0] < CACHE_SECONDS:
                out[h] = hit[1]
            else:
                ask.append(h)
    for i in range(0, len(ask), BATCH):
        batch = ask[i:i + BATCH]
        data = ext_db._scene_gql(source, _query(ext_db.SCENE_FRAGMENT),
                                 {'f': [[{'hash': h, 'algorithm': 'OSHASH'}] for h in batch]})
        for h, scenes in zip(batch, data.get('findScenesBySceneFingerprints') or []):
            found = []
            for sc in scenes or []:
                if not sc or sc.get('deleted'):
                    continue
                c = ext_db._scene_compact(sc)
                fp = next((f for f in sc.get('fingerprints') or [] if f.get('hash') == h and f.get('algorithm') == 'OSHASH'), {})
                c['fingerprint'] = {'submissions': fp.get('submissions') or 0, 'reports': fp.get('reports') or 0}
                found.append(c)
            out[h] = found
            with _lock:
                _cache[(source, h)] = (now, found)
    return out


def trusted(sc):
    """A fingerprint match more people confirmed than reported as wrong."""
    fp = sc.get('fingerprint') or {}
    return (fp.get('submissions') or 0) > (fp.get('reports') or 0)


def prefetch(conn, source, paths):
    """Hash these files and look them all up in as few requests as possible (bulk matching)."""
    hashes = [h for h in (for_path(conn, p) for p in paths) if h]
    if hashes:
        try:
            lookup(source, hashes)
        except Exception as e:                       # the per-video lookup tries again and reports it
            log.debug(f'fingerprint prefetch failed: {e}')
    return len(hashes)


def scenes_for(conn, source, path):
    """-> (hash, [scenes whose fingerprints include this file's]) - ([], when it has no hash)."""
    h = for_path(conn, path)
    if not h:
        return None, []
    return h, lookup(source, [h]).get(h, [])


def forget_missing(conn):
    """Drop fingerprints of files lustr no longer has (maintenance)."""
    try:
        return conn.execute('DELETE FROM file_fingerprints WHERE path NOT IN (SELECT path FROM file_versions)').rowcount
    except sqlite3.DatabaseError:
        return 0
