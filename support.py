"""
Problem report (Settings -> Maintenance -> Problem report).

One text file a user can attach to a GitHub issue: the version and system, the database's health,
the library's size, background jobs, recent warnings and errors (with their error IDs), errors
from the browser, and the end of the log.

Names are redacted by default: file paths, file names, titles, performers, sites, studios,
movies, libraries, collections and user names become placeholders («file-3», «name-12», ...).
The same name always gets the same placeholder in one report, so the report still shows which
lines are about the same video. The vocabulary comes from the database itself, so a name is
caught wherever it appears in the log.
"""
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import logger
from config import DATA_DIR, VERSION
from helpers import check_auth

router = APIRouter(prefix='/api/support', tags=['support'])
log = logging.getLogger('lustr.support')
browser_log = logging.getLogger('lustr.browser')

_model = None
LOG_LINES = 400                  # how much of the log the report includes
VIDEO_EXT = r'(?:mp4|m4v|mkv|avi|wmv|mov|ts|m2ts|webm|flv|mpg|mpeg|3gp|ogv|vob|rmvb|divx|jpg|jpeg|png|gif|webp|srt|vtt|nfo|zip|rar|7z|cbz)'


def set_model(model):
    global _model
    _model = model


# ---------- browser errors ----------
_browser_errors = deque(maxlen=50)
_browser_times = deque(maxlen=30)


class ClientError(BaseModel):
    message: str = ''
    source: str = ''
    line: Optional[int] = None
    col: Optional[int] = None
    stack: str = ''
    page: str = ''
    kind: str = 'error'          # 'error' | 'promise'


@router.post('/client-error')
def client_error(body: ClientError, request: Request):
    """A JavaScript error in the browser: logged (at most 30 a minute) and kept for the report."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail='Not logged in')
    now = time.time()
    while _browser_times and now - _browser_times[0] > 60:
        _browser_times.popleft()
    if len(_browser_times) >= _browser_times.maxlen:
        return {'logged': False}
    _browser_times.append(now)
    msg = (body.message or '')[:500]
    from urllib.parse import urlparse
    where = f"{(urlparse(body.source or '').path or body.source or '?')[-80:]}:{body.line or '?'}:{body.col or '?'}"
    stack = (body.stack or '')[:2000]
    _browser_errors.append({'time': now, 'kind': body.kind[:10], 'message': msg, 'where': where, 'stack': stack,
                            'agent': request.headers.get('user-agent', '')[:200]})
    browser_log.warning(f"{body.kind[:10]}: {msg} ({where})" + (f"\n{stack}" if stack else ''))
    return {'logged': True}


# ---------- log level ----------
class LevelBody(BaseModel):
    level: str


@router.get('/log-level')
def get_log_level(request: Request):
    check_auth(request)
    return {'level': logger.current_level(), 'levels': list(logger.LEVELS), 'file': logger.APP_LOG_FILE}


@router.post('/log-level')
def set_log_level(body: LevelBody, request: Request):
    check_auth(request)
    try:
        return {'level': logger.set_level(body.level)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------- the report ----------
@router.get('/report')
def report_json(request: Request, redact: bool = True):
    """The report text, for the preview."""
    check_auth(request)
    return {'text': build_report(redact=redact), 'redacted': redact,
            'issue_url': _issue_url(), 'version': VERSION}


@router.get('/report.txt')
def report_file(request: Request, redact: bool = True):
    check_auth(request)
    name = f"lustr-report-{VERSION}-{datetime.now().strftime('%Y%m%d-%H%M')}.txt"
    return PlainTextResponse(build_report(redact=redact),
                             headers={'Content-Disposition': f'attachment; filename="{name}"'})


def _issue_url():
    try:
        import update_check
        repo = update_check.REPO
    except Exception:
        repo = 'adlerdark/lustr'
    return f'https://github.com/{repo}/issues/new'


def build_report(redact=True):
    sections = []
    add = lambda title, body: sections.append(f"## {title}\n{body.rstrip()}\n")
    add('lustr', _section(_about))
    add('System', _section(_system))
    add('Database', _section(_database))
    add('Library', _section(_library))
    add('Background jobs', _section(_jobs))
    add('Recent warnings and errors', _section(_problems))
    add('Browser errors', _section(_browser))
    add(f'Log (last {LOG_LINES} lines of {os.path.basename(logger.APP_LOG_FILE)})', _section(_log_tail))
    text = (f"# lustr problem report\n"
            f"Made {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            f"{' - names, titles and paths replaced by placeholders' if redact else ' - NOT redacted: it contains your file names'}\n\n"
            + '\n'.join(sections))
    if redact:
        text = _redactor().apply(text)
    return text


_cached = {'redactor': None, 'at': 0.0}


def _redactor():
    """The redactor (its vocabulary takes a few seconds to build on a big library): kept for 10 minutes,
    so the preview, the redact toggle and the download share it."""
    if _cached['redactor'] is None or time.time() - _cached['at'] > 600:
        _cached['redactor'], _cached['at'] = Redactor(_conn()), time.time()
    return _cached['redactor']


def _section(fn):
    try:
        return fn()
    except Exception as e:
        return f'(could not read this: {type(e).__name__}: {e})'


def _conn():
    if _model is not None and getattr(_model, 'db', None):
        return _model.db.get_connection()
    return sqlite3.connect(os.path.join(DATA_DIR, 'library.db'))


def _kv(rows):
    w = max((len(k) for k, _ in rows), default=0)
    return '\n'.join(f'{k.ljust(w)}  {v}' for k, v in rows)


def _about():
    up = int(time.time() - logger.started_at)
    return _kv([('Version', VERSION), ('Up for', f'{up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m'),
                ('Log level', logger.current_level()), ('Log file', logger.APP_LOG_FILE)])


def _ffmpeg_version():
    try:
        out = subprocess.run(['ffmpeg', '-hide_banner', '-version'], capture_output=True, text=True, timeout=5).stdout
        return out.split('\n', 1)[0].replace('ffmpeg version ', '')[:80]
    except Exception as e:
        return f'not found ({type(e).__name__})'


def _system():
    rows = [('OS', f'{platform.system()} {platform.release()} ({platform.machine()})'),
            ('In Docker', 'yes' if os.path.exists('/.dockerenv') else 'no'),
            ('Python', sys.version.split()[0]), ('SQLite', sqlite3.sqlite_version),
            ('ffmpeg', _ffmpeg_version()), ('CPUs', str(os.cpu_count()))]
    try:
        with open('/proc/meminfo') as f:
            mem = {l.split(':')[0]: int(l.split()[1]) for l in f if l.split(':')[0] in ('MemTotal', 'MemAvailable')}
        rows.append(('Memory', f"{mem.get('MemAvailable', 0) // 1024:,} MB free of {mem.get('MemTotal', 0) // 1024:,} MB"))
    except Exception:
        pass
    try:
        import transcoder
        hw = transcoder.HW
        rows.append(('GPU encoding (VAAPI)', 'available' if hw.get('vaapi') else ('not available' + (f": {hw['error']}" if hw.get('error') else ''))))
    except Exception:
        pass
    try:
        import captions
        rows.append(('Whisper (captions)', 'installed' if captions.whisper_available() else 'not installed'))
    except Exception:
        pass
    rows.append(('StashDB key', 'set' if os.environ.get('STASHDB_API_KEY') else 'not set'))
    rows.append(('ThePornDB key', 'set' if os.environ.get('TPDB_API_KEY') else 'not set'))
    try:
        from config import MEDIA_ROOTS
        for root in MEDIA_ROOTS:
            ok = os.path.isdir(root) and bool(os.listdir(root))
            rows.append((f'Media folder {root}', 'mounted' if ok else ('empty' if os.path.isdir(root) else 'missing')))
    except Exception:
        pass
    du = shutil.disk_usage(DATA_DIR)
    rows.append(('Data folder free space', f'{du.free / 1e9:,.1f} GB of {du.total / 1e9:,.1f} GB'))
    return _kv(rows)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _database():
    conn = _conn()
    db = os.path.join(DATA_DIR, 'library.db')
    rows = [('Size', f"{_size(db) / 1e6:,.0f} MB (+ {_size(db + '-wal') / 1e6:,.1f} MB write-ahead log)"),
            ('Journal mode', conn.execute('PRAGMA journal_mode').fetchone()[0]),
            ('Free pages', f"{conn.execute('PRAGMA freelist_count').fetchone()[0]:,}")]
    try:
        q = conn.execute('PRAGMA quick_check(5)').fetchall()
        rows.append(('Integrity (quick check)', 'ok' if q and q[0][0] == 'ok' else '; '.join(r[0] for r in q)[:300]))
    except sqlite3.DatabaseError as e:
        rows.append(('Integrity (quick check)', f'failed: {e}'))
    try:
        import search_index
        n = conn.execute(f'SELECT COUNT(*) FROM {search_index.TABLE}').fetchone()[0]
        d = conn.execute(f'SELECT COUNT(*) FROM {search_index.DIRTY}').fetchone()[0]
        rows.append(('Search index', f'{n:,} videos ({d:,} waiting to update)'))
    except sqlite3.DatabaseError:
        rows.append(('Search index', 'not built'))
    try:
        import photo_store
        n, size = photo_store.disk_usage()
        left = photo_store.pending(conn)
        rows.append(('Covers and photos', f'{n:,} files ({size / 1e6:,.0f} MB)' + (f', {left:,} still in the database' if left else '')))
    except Exception:
        pass
    try:
        import db_maintenance
        b = db_maintenance.list_backups()
        rows.append(('Backups', f"{len(b)} ({sum(x['size'] for x in b) / 1e9:,.1f} GB)"))
    except Exception:
        pass
    return _kv(rows)


def _count(conn, sql):
    try:
        return f'{conn.execute(sql).fetchone()[0]:,}'
    except sqlite3.DatabaseError:
        return '-'


def _library():
    conn = _conn()
    rows = [('Videos', _count(conn, 'SELECT COUNT(*) FROM videos')),
            ('Files', _count(conn, 'SELECT COUNT(*) FROM file_versions WHERE deleted_at IS NULL')),
            ('Deleted files kept', _count(conn, 'SELECT COUNT(*) FROM file_versions WHERE deleted_at IS NOT NULL')),
            ('Images', _count(conn, 'SELECT COUNT(*) FROM images WHERE deleted_at IS NULL')),
            ('Movies', _count(conn, 'SELECT COUNT(*) FROM movies')),
            ('Performers', _count(conn, 'SELECT COUNT(*) FROM video_cast')),
            ('Scene links', _count(conn, 'SELECT COUNT(*) FROM video_ext_link')),
            ('Performer links', _count(conn, 'SELECT COUNT(*) FROM cast_ext_link')),
            ('Videos with captions', _count(conn, 'SELECT COUNT(*) FROM video_captions'))]
    if _model is not None:
        rows.append(('Libraries', f'{len(getattr(_model, "libraries", {}) or {})} ({len(getattr(_model, "library_groups", {}) or {})} groups)'))
    try:
        import fields
        rows.append(('List fields', ', '.join(fields.list_fields(conn))))
    except Exception:
        pass
    return _kv(rows)


def _jobs():
    out = []
    try:
        import ext_db
        for label, st in (('Performer bulk link', ext_db._bulk_status()), ('Scene bulk link', ext_db._scene_bulk_status()),
                          ('Site linking', ext_db._site_status())):
            if st.get('running') or st.get('error'):
                out.append(f"{label}: {'running' if st.get('running') else 'stopped'} {st.get('done', 0)}/{st.get('total', 0)}"
                           + (f" - error: {st['error']}" if st.get('error') else ''))
    except Exception:
        pass
    try:
        import movies
        st = movies.bulk_status()
        if st.get('running') or st.get('error'):
            out.append(f"Movie bulk link: {'running' if st.get('running') else 'stopped'} {st.get('done', 0)}/{st.get('total', 0)}"
                       + (f" - error: {st['error']}" if st.get('error') else ''))
    except Exception:
        pass
    names = sorted({t.name for t in threading.enumerate() if t.name not in ('MainThread',) and not t.name.startswith(('AnyIO', 'ThreadPoolExecutor', 'asyncio'))})
    out.append('Threads: ' + (', '.join(names) or 'none'))
    return '\n'.join(out)


def _problems():
    items = logger.recent_problems(60)
    if not items:
        return 'none since lustr started'
    return '\n'.join(i['text'] for i in items)


def _browser():
    if not _browser_errors:
        return 'none since lustr started'
    out = []
    for e in _browser_errors:
        out.append(f"{datetime.fromtimestamp(e['time']).strftime('%Y-%m-%d %H:%M:%S')} {e['kind']}: {e['message']} ({e['where']})"
                   f"\n  {e['agent']}" + (f"\n  {e['stack'][:600]}" if e['stack'] else ''))
    return '\n'.join(out)


def _log_tail():
    lines = logger.tail().splitlines()
    return '\n'.join(lines[-LOG_LINES:]) or '(empty)'


# ---------- redaction ----------
class _Trie:
    """Many literal strings -> one fast regular expression (a trie, so matching doesn't try each one)."""

    def __init__(self):
        self.root = {}

    def add(self, word):
        node = self.root
        for ch in word:
            node = node.setdefault(ch, {})
        node[''] = True

    def _pattern(self, node):
        if '' in node and len(node) == 1:
            return None
        alts, opt = [], '' in node
        for ch in sorted(k for k in node if k):
            sub = self._pattern(node[ch])
            alts.append(re.escape(ch) + (sub or ''))
        body = alts[0] if len(alts) == 1 else '(?:' + '|'.join(alts) + ')'
        return f'(?:{body})?' if opt else body

    def regex(self):
        p = self._pattern(self.root) if self.root else None
        return re.compile(r'(?<![\w])' + p + r'(?![\w])', re.I) if p else None


_code_words_cache = None


def _code_words():
    """Every word in lustr's own code (its log messages, labels): a one-word title like 'Error' or
    'Started' isn't redacted wherever the log says it."""
    global _code_words_cache
    if _code_words_cache is None:
        here = os.path.dirname(os.path.abspath(__file__))
        words = set()
        for folder in (here, os.path.join(here, 'routes')):
            for name in os.listdir(folder):
                if name.endswith('.py'):
                    try:
                        with open(os.path.join(folder, name), encoding='utf-8') as f:
                            words |= set(re.findall(r'[a-z]{4,}', f.read().lower()))
                    except OSError:
                        pass
        _code_words_cache = words | {'debug', 'info', 'warning', 'error', 'critical', 'uvicorn', 'lustr', 'browser'}
    return _code_words_cache


class Redactor:
    MIN_LEN = 4                  # shorter names ('Ana', 'Mia') would also hit ordinary words

    def __init__(self, conn):
        self.names = {}
        self.counts = {}
        words = set()
        for sql in ('SELECT title FROM videos', 'SELECT filename_base FROM videos', 'SELECT value FROM video_cast',
                    'SELECT value FROM video_site', 'SELECT title FROM movies', 'SELECT studio FROM movies',
                    'SELECT alias FROM cast_aliases', 'SELECT name FROM collections', 'SELECT name FROM playlists',
                    'SELECT DISTINCT library FROM videos', 'SELECT DISTINCT username FROM sessions',
                    'SELECT DISTINCT username FROM play_log', 'SELECT ext_studio_name FROM video_ext_link',
                    'SELECT ext_title FROM video_ext_link'):
            try:
                for (v,) in conn.execute(sql):
                    if isinstance(v, str):
                        v = v.strip()
                        if len(v) >= self.MIN_LEN:
                            words.add(v.lower())
                            if '.' in v:                     # dotted tag style 'jane.doe' also as 'jane doe'
                                words.add(v.replace('.', ' ').lower())
            except sqlite3.DatabaseError:
                pass
        if _model is not None:
            for name in list(getattr(_model, 'libraries', {}) or {}) + list(getattr(_model, 'library_groups', {}) or {}):
                if len(name) >= self.MIN_LEN:
                    words.add(name.lower())
        try:
            with open(os.path.join(DATA_DIR, 'users.json'), encoding='utf-8') as f:
                for u in (json.load(f) or {}):
                    if isinstance(u, str) and len(u) >= 3:
                        words.add(u.lower())
        except (OSError, ValueError):
            pass
        common = _code_words()
        trie = _Trie()
        for w in words:
            if ' ' in w or w not in common:              # a one-word name that lustr's own messages use stays
                trie.add(w)
        self.word_re = trie.regex()

    def _ph(self, kind, value):
        key = (kind, value.lower())
        if key not in self.names:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.names[key] = f'«{kind}-{self.counts[kind]}»'
        return self.names[key]

    def apply(self, text):
        # request URLs first: names in them are %-encoded ('Jane%20Doe'), and some routes carry a name
        # (a library, a performer) that may not be in the vocabulary at all
        text = re.sub(r'\b(GET|POST|PUT|PATCH|DELETE|HEAD) (/[^\s?"]*)', lambda m: f'{m.group(1)} {self._url_path(m.group(2))}', text)
        # file paths: up to the file's extension, or to where the message goes on (paths have spaces)
        text = re.sub(r'/(?:media\d*|mnt|srv|home|data|volume\d*|share|storage|app/data)/[^\n"\'<>|]*?'
                      r'(?:\.' + VIDEO_EXT + r'\b|(?=["\'<>|]|: | -> |\s*$))',
                      lambda m: self._path(m.group(0)), text, flags=re.M | re.I)
        # file names with a media extension (several words joined by spaces, dots, dashes)
        text = re.sub(r'[^\s/\\:"\'<>|=«»]+(?:[ ._-][^\s/\\:"\'<>|=«»]+)*\.' + VIDEO_EXT + r'\b',
                      lambda m: self._ph('file', m.group(0)) + os.path.splitext(m.group(0))[1].lower(), text, flags=re.I)
        # query strings (paths and searches in request URLs)
        text = re.sub(r'(\?)([^\s"]+)', lambda m: '?' + self._query(m.group(2)), text)
        if self.word_re:
            text = self.word_re.sub(lambda m: self._ph('name', m.group(0)), text)
        text = re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b', '«ip»', text)
        text = re.sub(r'[\w.+-]+@[\w-]+\.[\w.-]+', '«email»', text)
        return text

    def _path(self, path):
        ext = os.path.splitext(path)[1].lower()
        root = '/' + path.strip('/').split('/', 1)[0]
        return f"{root}/{self._ph('path', path)}{ext if re.fullmatch(VIDEO_EXT, ext[1:], re.I) else ''}"

    # routes whose next path segment is a name, and the fixed words that can stand there instead
    _NAME_ROUTES = (('/api/library/', {'add_folder', 'create', 'remove_folder', 'rename', 'reorder', 'set_groups', 'setting'}),
                    ('/api/cast/category/', {'set'}), ('/api/cast/favorites/', set()), ('/api/cast/gender/', {'set', 'stats'}),
                    ('/api/cast/photo/', set()), ('/api/cast/resolve/', set()),
                    ('/api/cast/', {'all', 'apply-attributes-to-selected', 'category', 'favorites', 'gender', 'photo', 'resolve'}))
    _PATH_ROUTES = ('/files/', '/api/watch/progress/')     # the rest of the URL is a file path

    def _url_path(self, raw):
        from urllib.parse import unquote
        path = unquote(raw)
        for prefix in self._PATH_ROUTES:
            if path.startswith(prefix) and len(path) > len(prefix):
                return prefix + self._ph('path', path[len(prefix):])
        for prefix, fixed in self._NAME_ROUTES:
            if path.startswith(prefix):
                seg, sep, rest = path[len(prefix):].partition('/')
                if seg and seg not in fixed:
                    if prefix == '/api/cast/' and rest.startswith('aliases/') and rest.count('/') >= 1:
                        alias, _, tail = rest[len('aliases/'):].partition('/')
                        if alias and alias != 'set-primary':
                            rest = 'aliases/' + self._ph('name', alias) + ('/' + tail if tail else '')
                    return prefix + self._ph('name', seg) + sep + rest
                break
        return self.word_re.sub(lambda m: self._ph('name', m.group(0)), path) if self.word_re else path

    def _query(self, qs):
        parts = []
        for kv in qs.split('&'):
            k, _, v = kv.partition('=')
            if k in ('path', 'paths', 'q', 'query', 'name', 'folder', 'current_folder', 'tags', 'sites', 'cast',
                     'title', 'library', 'tags_exclude', 'sites_exclude', 'value', 'term'):
                parts.append(f'{k}=«…»' if v else k)
            else:
                parts.append(kv)
        return '&'.join(parts)
