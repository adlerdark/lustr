"""
Full-text index for the library search box (plain words, not field:value filters).

Searching used to load every video's record into Python and look for each word in a text made
from it (1-3 s per search on a 35k library, growing with the library). This keeps that text in an
SQLite FTS5 table with the trigram tokenizer, so a search is one indexed query and matches the
same way as before: each word anywhere inside the text ('jak' finds 'Jake'), * or % as a
wildcard, "quoted phrases", and and / or / not with parentheses.

The index has one row per video's main file (the card the library shows). Triggers on the tables
that feed the text (videos, file_versions, every list field, movies) put the changed video's ID
in search_dirty; those rows are rebuilt just before the next search (all of them when many changed,
or when the list fields changed). A missing or broken index is rebuilt from scratch, so it can't
drift for long.
"""
import logging
import os
import re
import sqlite3
import threading

log = logging.getLogger('lustr.search_index')

TABLE = 'video_search'
DIRTY = 'search_dirty'
FULL_REBUILD_AT = 3000           # more changed videos than this: rebuild everything in one pass
# record keys left out of the text: internal IDs and timestamps nobody searches for
SKIP_KEYS = {'cache_id', 'video_id', 'created_at', 'updated_at', 'movie_id', 'favorite', 'file_library'}

_lock = threading.Lock()
_known = {}                      # db path -> the list fields the triggers were made for


# ---------- the text ----------
def blob(path, rec):
    """The searchable text of one file record (as Database.get_files_bulk returns it), lowercase."""
    parts = [os.path.basename(path).lower()]
    for k, v in rec.items():
        if k in SKIP_KEYS:
            continue
        if isinstance(v, str):
            if v:
                parts.append(v.lower())
        elif isinstance(v, list):
            parts.extend(str(x).lower() for x in v if x not in (None, ''))
    return ' '.join(parts)


# ---------- schema ----------
def _fv_changed():
    cols = ('path', 'video_id', 'filename', 'is_preferred', 'deleted_at', 'resolution', 'codec', 'extension',
            'length', 'total_bitrate', 'file_size', 'date_created', 'date_modified', 'library')
    return ' OR '.join(f'NEW.{c} IS NOT OLD.{c}' for c in cols)


def _v_changed():
    cols = ('video_id', 'title', 'year', 'rating', 'date_added', 'date_last_edited', 'library', 'filename_base')
    return ' OR '.join(f'NEW.{c} IS NOT OLD.{c}' for c in cols)


def _mark(expr):
    return f'INSERT OR IGNORE INTO {DIRTY} (video_id) VALUES ({expr});'


def _list_fields(conn):
    import fields
    return tuple(fields.list_fields(conn))


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def ensure_schema(conn):
    """Create the index, the dirty list and the triggers (for the list fields there are now).
    -> True when the triggers changed (new list fields: the index needs a full rebuild)."""
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5("
                 f"path UNINDEXED, video_id UNINDEXED, text, tokenize = 'trigram', detail = 'none')")
    conn.execute(f'CREATE TABLE IF NOT EXISTS {DIRTY} (video_id TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS search_index_state (key TEXT PRIMARY KEY, value TEXT)')
    stmts = [
        f'CREATE TRIGGER IF NOT EXISTS search_v_ins AFTER INSERT ON videos BEGIN {_mark("NEW.video_id")} END',
        f'CREATE TRIGGER IF NOT EXISTS search_v_upd AFTER UPDATE ON videos WHEN {_v_changed()} '
        f'BEGIN {_mark("NEW.video_id")} {_mark("OLD.video_id")} END',
        f'CREATE TRIGGER IF NOT EXISTS search_v_del AFTER DELETE ON videos BEGIN {_mark("OLD.video_id")} END',
        f'CREATE TRIGGER IF NOT EXISTS search_fv_ins AFTER INSERT ON file_versions BEGIN {_mark("NEW.video_id")} END',
        f'CREATE TRIGGER IF NOT EXISTS search_fv_upd AFTER UPDATE ON file_versions WHEN {_fv_changed()} '
        f'BEGIN {_mark("NEW.video_id")} {_mark("OLD.video_id")} END',
        f'CREATE TRIGGER IF NOT EXISTS search_fv_del AFTER DELETE ON file_versions BEGIN {_mark("OLD.video_id")} END',
    ]
    if _table_exists(conn, 'video_movie'):
        stmts += [
            f'CREATE TRIGGER IF NOT EXISTS search_vm_ins AFTER INSERT ON video_movie BEGIN {_mark("NEW.video_id")} END',
            f'CREATE TRIGGER IF NOT EXISTS search_vm_upd AFTER UPDATE ON video_movie '
            f'BEGIN {_mark("NEW.video_id")} {_mark("OLD.video_id")} END',
            f'CREATE TRIGGER IF NOT EXISTS search_vm_del AFTER DELETE ON video_movie BEGIN {_mark("OLD.video_id")} END',
        ]
    if _table_exists(conn, 'movies'):
        stmts.append(
            f'CREATE TRIGGER IF NOT EXISTS search_movie_upd AFTER UPDATE OF title, edition ON movies '
            f'BEGIN INSERT OR IGNORE INTO {DIRTY} (video_id) SELECT video_id FROM video_movie WHERE movie_id = NEW.id; END')
    fields = _list_fields(conn)
    for f in fields:
        t, j = f'video_{f}', f'video_{f}_junction'
        if not (_table_exists(conn, t) and _table_exists(conn, j)):
            continue
        stmts += [
            f'CREATE TRIGGER IF NOT EXISTS search_{f}_ins AFTER INSERT ON {j} BEGIN {_mark("NEW.video_id")} END',
            f'CREATE TRIGGER IF NOT EXISTS search_{f}_upd AFTER UPDATE ON {j} '
            f'BEGIN {_mark("NEW.video_id")} {_mark("OLD.video_id")} END',
            f'CREATE TRIGGER IF NOT EXISTS search_{f}_del AFTER DELETE ON {j} BEGIN {_mark("OLD.video_id")} END',
            f'CREATE TRIGGER IF NOT EXISTS search_{f}_val AFTER UPDATE OF value ON {t} '
            f'BEGIN INSERT OR IGNORE INTO {DIRTY} (video_id) SELECT video_id FROM {j} WHERE {f}_id = NEW.id; END',
        ]
    for s in stmts:
        conn.execute(s)
    key = ','.join(fields)
    row = conn.execute("SELECT value FROM search_index_state WHERE key = 'fields'").fetchone()
    changed = not row or row[0] != key
    if changed:
        conn.execute("INSERT OR REPLACE INTO search_index_state (key, value) VALUES ('fields', ?)", (key,))
    conn.commit()
    return changed


# ---------- keeping it up to date ----------
def _active_paths(conn, video_ids=None):
    sql = 'SELECT path FROM file_versions WHERE is_preferred = 1 AND deleted_at IS NULL'
    if video_ids is None:
        return [r[0] for r in conn.execute(sql)]
    out = []
    ids = list(video_ids)
    for i in range(0, len(ids), 900):
        batch = ids[i:i + 900]
        out += [r[0] for r in conn.execute(sql + f' AND video_id IN ({",".join("?" * len(batch))})', batch)]
    return out


def _write(conn, db, paths):
    recs = db.get_files_bulk(paths) if paths else {}
    conn.executemany(f'INSERT INTO {TABLE} (path, video_id, text) VALUES (?, ?, ?)',
                     [(p, r.get('video_id'), blob(p, r)) for p, r in recs.items()])
    return len(recs)


def rebuild(db):
    """Make the whole index again. -> rows written."""
    conn = db.get_connection()
    with _lock:
        ensure_schema(conn)
        conn.execute(f'DELETE FROM {DIRTY}')
        conn.execute(f'DELETE FROM {TABLE}')
        n = _write(conn, db, _active_paths(conn))
        conn.execute("INSERT OR REPLACE INTO search_index_state (key, value) VALUES ('built', '1')")
        conn.commit()
    _known[db.db_path] = _list_fields(conn)
    return n


def refresh(db):
    """Bring the index up to date before a search: rebuild the changed videos' rows (or everything,
    the first time, after a list field was added or removed, or when many videos changed)."""
    conn = db.get_connection()
    fields = _list_fields(conn)
    if _known.get(db.db_path) != fields:
        if ensure_schema(conn) or not _built(conn):
            n = rebuild(db)
            log.info(f'Search index built: {n} videos')
            return
        _known[db.db_path] = fields
    with _lock:
        dirty = [r[0] for r in conn.execute(f'SELECT video_id FROM {DIRTY}')]
        if not dirty:
            return
        if len(dirty) > FULL_REBUILD_AT:
            full = True
        else:
            full = False
            for i in range(0, len(dirty), 900):              # only the ones read (a trigger may add more meanwhile)
                batch = dirty[i:i + 900]
                ph = ",".join("?" * len(batch))
                conn.execute(f'DELETE FROM {DIRTY} WHERE video_id IN ({ph})', batch)
                conn.execute(f'DELETE FROM {TABLE} WHERE video_id IN ({ph})', batch)
            _write(conn, db, _active_paths(conn, dirty))
            conn.commit()
    if full:
        n = rebuild(db)
        log.info(f'Search index rebuilt: {n} videos ({len(dirty)} had changed)')


def _built(conn):
    try:
        return conn.execute("SELECT value FROM search_index_state WHERE key = 'built'").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def warm_up(db):
    """At start-up, in the background: build the index if it isn't there, catch up otherwise."""
    def run():
        try:
            refresh(db)
        except Exception as e:
            log.warning(f'Search index not ready, searches use the slow path until it is: {e}')
    threading.Thread(target=run, name='search-index', daemon=True).start()


def is_ready(db):
    try:
        return _built(db.get_connection())
    except Exception:
        return False


# ---------- the query ----------
_OPERATORS = (' and ', ' or ', ' not ', '(', ')')


def _like(term):
    """A search word as a LIKE pattern: anywhere in the text; * and % are wildcards."""
    t = term.replace('\\', '\\\\').replace('_', '\\_')
    t = t.replace('%', '*')
    t = t.replace('*', '%')
    return f'%{t}%'


def text_condition(query, col='text'):
    """The SQL condition on the index's text column for the search box words -> (sql, params),
    or (None, []) when there is nothing to search. Same rules as SearchParser.matches_base_query."""
    q = (query or '').lower()
    if not q.strip():
        return None, []
    cond = f"{col} LIKE ? ESCAPE '\\'"
    has_op = any(op in q for op in _OPERATORS) or q.startswith('not ') or q.startswith('(')
    if not has_op:
        parts, params = [], []
        for token in re.findall(r'(?:[^\s"]+|"[^"]*")+', q):
            t = token.strip('"\'')
            if t:
                parts.append(cond)
                params.append(_like(t))
        return (' AND '.join(parts), params) if parts else (None, [])
    sql, params, check = [], [], []
    for token in re.split(r'(\(|\)| and | or | not )', q):
        if not token:
            continue
        s = token.strip()
        if s in ('(', ')', 'and', 'or', 'not'):
            sql.append(s.upper() if s.isalpha() else s)
            check.append(s)
        elif s:
            sql.append(cond)
            params.append(_like(s.strip('"\'')))
            check.append('True')
    try:                                                    # the same expressions the old search accepted
        eval(' '.join(check) or 'True', {'__builtins__': {}})
        ok = bool(sql)
    except Exception:
        ok = False
    if not ok:                                              # unbalanced: the whole query as one phrase
        return cond, [_like(q.strip())]
    return '(' + ' '.join(sql) + ')', params


def video_condition(query, path_col='fv.path'):
    """-> (sql, params) limiting the library query to files whose text matches, or (None, [])."""
    sql, params = text_condition(query)
    if not sql:
        return None, []
    return f'{path_col} IN (SELECT path FROM {TABLE} WHERE {sql})', params
