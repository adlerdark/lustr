# This file is: ./metadata_import.py
"""
Bulk editing through a spreadsheet: Settings -> Import / Export.

Export writes a CSV of the videos in a library (or a search): one row per video, its path and
filename, then the metadata columns. You edit it in a spreadsheet and import it back. The import
never writes blindly: it first shows what would change, and then writes only those differences.

Column rules on import:
  - a blank cell changes nothing
  - a lone '*' clears the field
  - list fields hold several values separated by '|'; per column you choose
      add      (default) the values are added, the video's other values stay
      replace  the cell becomes the field's whole list
  - title / year / rating / movie columns replace the value (rating: 0.5 - 5 stars); favorite is yes / no
  - columns it doesn't know, and the technical ones (codec, length, ...), are ignored

Rows are matched to videos by path, else by a filename only one video has (its copies count as one).
The writes go through the same path as Edit Metadata (routes/metadata.py), passed in as apply_one.

No FastAPI imports.
"""
import csv
import io
import os
import secrets
import threading
import time

import fields

SEP = '|'
CLEAR = '*'
SCALAR = ('title', 'year', 'rating')
MOVIE = ('movie', 'movie_scene', 'movie_part', 'movie_whole')
FAVORITE = 'favorite'
KEY_COLUMNS = ('path', 'filename')
TECHNICAL = ('resolution', 'codec', 'extension', 'length', 'total_bitrate', 'file_size', 'date_created',
             'date_modified', 'date_added', 'date_last_edited')


def _list():
    """The list fields, as Settings has them now (hidden ones aren't exported or imported)."""
    return fields.list_fields()


def export_columns():
    return list(KEY_COLUMNS) + list(SCALAR) + [FAVORITE] + list(MOVIE) + _list() + list(TECHNICAL)

MODES = ('add', 'replace')
YES = ('1', 'yes', 'y', 'true', 'x', '✓')
NO = ('0', 'no', 'n', 'false')


class ImportError_(Exception):
    pass


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def export_csv(conn, get_file, paths, template=False):
    """CSV text for these paths (one per video). template=True: only the path and filename filled in."""
    ids = {r[0]: r[1] for k in range(0, len(paths), 500) for r in conn.execute(
        f'SELECT path, video_id FROM file_versions WHERE path IN ({",".join("?" * len(paths[k:k + 500]))})', paths[k:k + 500])}
    minfo = _movie_info(conn, list(ids.values())) if not template else {}
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=export_columns(), extrasaction='ignore')
    w.writeheader()
    for p in paths:
        row = {'path': p, 'filename': os.path.basename(p)}
        if not template:
            v = get_file(p) or {}
            for f in SCALAR + TECHNICAL:
                val = v.get(f)
                row[f] = '' if val in (None, False) else str(val)
            row[FAVORITE] = 'yes' if v.get('favorite') else ''
            for f in _list():
                row[f] = SEP.join(str(x) for x in (v.get(f) or []) if x)
            m = minfo.get(ids.get(p))
            if m:
                row.update(movie=m[1], movie_scene=m[2] or '', movie_part=m[3] or '', movie_whole='yes' if m[4] else '')
        w.writerow(row)
    return out.getvalue()


# ---------------------------------------------------------------------------
# import: read, match, preview, apply
# ---------------------------------------------------------------------------
def _movie_info(conn, video_ids):
    import sqlite3
    import movies
    try:
        return movies.info_map(conn, video_ids)
    except sqlite3.OperationalError:                         # no movie tables yet: no movies
        return {}


def read_csv(data):
    """(columns, rows) from the uploaded bytes. Column names are matched loosely ('Male Cast' -> male_cast)."""
    if isinstance(data, bytes):
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('cp1252')                      # a spreadsheet saved as "CSV" on Windows
    else:
        text = data
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ImportError_('The file is empty')
    cols = [h.strip().lower().replace(' ', '_') for h in header]
    cols = [fields.resolve(c) for c in cols]                    # a field's name as Settings shows it -> its key
    if 'path' not in cols and 'filename' not in cols:
        raise ImportError_('The file needs a "path" or a "filename" column (the first row holds the column names)')
    rows = [dict(zip(cols, r)) for r in reader if any((c or '').strip() for c in r)]
    return cols, rows


def column_kinds(cols):
    lst = _list()
    known = set(lst) | set(SCALAR) | set(MOVIE) | {FAVORITE}
    return {'list': [c for c in cols if c in lst],
            'single': [c for c in cols if c in known and c not in lst],
            'ignored': [c for c in cols if c not in known and c not in KEY_COLUMNS]}


def match(conn, rows):
    """[(row, path)] for the rows that match one video; plus the unmatched and ambiguous rows."""
    active, by_name = set(), {}
    for p, vid, pref in conn.execute('SELECT path, video_id, is_preferred FROM file_versions WHERE deleted_at IS NULL '
                                     'ORDER BY is_preferred DESC'):
        active.add(p)
        hits = by_name.setdefault(os.path.basename(p).lower(), {})
        hits.setdefault(vid, p)                              # one path per video: its preferred file
    matched, unmatched, ambiguous = [], [], []
    for row in rows:
        path = (row.get('path') or '').strip()
        name = (row.get('filename') or '').strip() or os.path.basename(path)
        if path in active:
            matched.append((row, path))
            continue
        hits = list(by_name.get(name.lower(), {}).values())
        label = path or name
        if len(hits) == 1:                                   # one video (maybe several copies of it)
            matched.append((row, hits[0]))
        elif hits:
            ambiguous.append(label)
        else:
            unmatched.append(label)
    return matched, unmatched, ambiguous


def _split(cell):
    return [v.strip() for v in str(cell).split(SEP) if v.strip()]


def changes_for(row, current, kinds, modes, movie_now=None):
    """What importing this row would change on its video -> dict for apply_one, and readable lines."""
    upd, add, remove, clear, lines = {}, {}, {}, [], []
    fav = None
    for f in kinds['list']:
        cell = (row.get(f) or '').strip()
        if not cell:
            continue
        have = [str(x) for x in (current.get(f) or [])]
        have_l = {x.lower() for x in have}
        if cell == CLEAR:
            if have:
                clear.append(f)
                lines.append(f'{f}: cleared ({", ".join(have)})')
            continue
        new = _split(cell)
        new_l = {x.lower() for x in new}
        plus = [x for x in dict.fromkeys(new) if x.lower() not in have_l]
        minus = [x for x in have if x.lower() not in new_l] if modes.get(f) == 'replace' else []
        if plus:
            add[f] = plus
        if minus:
            remove[f] = minus
        if plus or minus:
            lines.append(f'{f}: ' + ', '.join(['+' + x for x in plus] + ['-' + x for x in minus]))
    for f in kinds['single']:
        cell = (row.get(f) or '').strip()
        if not cell:
            continue
        if f == FAVORITE:
            low = cell.lower()
            want = False if cell == CLEAR or low in NO else True if low in YES else None
            if want is not None and want != bool(current.get('favorite')):
                fav = want
                lines.append(f'favorite: {"yes" if want else "no"}')
            continue
        now = (movie_now or {}).get(f, '') if f in MOVIE else current.get(f)
        now = '' if now in (None, False) else str(now)
        val = '' if cell == CLEAR else cell
        if f == 'rating':                                     # half stars: '4.0' is 4, '9' becomes 5
            from database import stars
            val, now = stars(val), stars(now)
        if f == 'movie_whole' and val:
            val = '1' if val.lower() in YES else '0'
            now = '1' if now.lower() in YES else '0'
        if val != now:
            upd[f] = val
            lines.append(f'{f}: {now or "(empty)"} → {val or "(empty)"}')
    return {'updates': upd, 'add_to_lists': add, 'remove_from_lists': remove, 'clear_lists': clear, 'favorite': fav}, lines


def _clear_fields(clear):
    """'*' in Cast clears the whole cast (the spreadsheet's Cast column holds the Male Cast too)."""
    return clear + ['male_cast'] if 'cast' in clear else clear


def plan(conn, get_file, rows, modes):
    """Preview: per video what changes, and counts. -> (summary, work)"""
    cols = list(dict.fromkeys(c for r in rows for c in r))
    kinds = column_kinds(cols)
    modes = {f: (modes or {}).get(f) if (modes or {}).get(f) in MODES else 'add' for f in kinds['list']}
    matched, unmatched, ambiguous = match(conn, rows)
    ids = {}
    if any(c in MOVIE for c in kinds['single']):
        paths = [p for _, p in matched]
        for k in range(0, len(paths), 500):
            chunk = paths[k:k + 500]
            ids.update(conn.execute(f'SELECT path, video_id FROM file_versions WHERE path IN ({",".join("?" * len(chunk))})', chunk).fetchall())
    minfo = _movie_info(conn, list(ids.values())) if ids else {}
    work, sample, per_column = [], [], {}
    seen = set()
    for row, path in matched:
        if path in seen:
            continue
        seen.add(path)
        m = minfo.get(ids.get(path))
        movie_now = {'movie': m[1], 'movie_scene': m[2] or '', 'movie_part': m[3] or '', 'movie_whole': '1' if m[4] else '0'} if m else {}
        ch, lines = changes_for(row, get_file(path) or {}, kinds, modes, movie_now)
        if not lines:
            continue
        work.append((path, ch))
        for ln in lines:
            col = ln.split(':', 1)[0]
            per_column[col] = per_column.get(col, 0) + 1
        if len(sample) < 40:
            sample.append({'path': path, 'changes': lines})
    summary = {'rows': len(rows), 'matched': len(seen), 'changed': len(work), 'unchanged': len(seen) - len(work),
               'unmatched': unmatched[:50], 'unmatched_count': len(unmatched),
               'ambiguous': ambiguous[:50], 'ambiguous_count': len(ambiguous),
               'columns': kinds, 'modes': modes, 'per_column': per_column, 'sample': sample}
    return summary, work


def apply(conn, work, apply_one, set_favorite):
    """Write the planned changes (after a safety backup). -> how many videos changed"""
    import db_maintenance
    if work:
        db_maintenance.safety_backup(conn, 'import')
    n = 0
    for path, ch in work:
        clear = _clear_fields(ch['clear_lists'])
        if ch['updates'] or ch['add_to_lists'] or ch['remove_from_lists'] or clear:
            apply_one(path, updates=ch['updates'], add_to_lists=ch['add_to_lists'],
                      remove_from_lists=ch['remove_from_lists'], clear_lists=clear)
        if ch['favorite'] is not None:
            set_favorite(path, ch['favorite'])
        n += 1
    return n


# ---------------------------------------------------------------------------
# an upload waits here between preview and apply
# ---------------------------------------------------------------------------
_KEEP_SECONDS = 600
_pending = {}
_lock = threading.Lock()


def stash(rows, name):
    with _lock:
        now = time.time()
        for t in [t for t, v in _pending.items() if now - v[0] > _KEEP_SECONDS]:
            del _pending[t]
        token = secrets.token_urlsafe(12)
        _pending[token] = (now, rows, name)
        return token


def stashed(token, drop=False):
    with _lock:
        v = _pending.pop(token, None) if drop else _pending.get(token)
    if not v or time.time() - v[0] > _KEEP_SECONDS:
        raise ImportError_('That upload has expired - choose the file again')
    return v[1], v[2]
