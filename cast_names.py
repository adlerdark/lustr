"""
Performer names (cast values).

* One performer, one spelling: names are compared by `norm` - capitals, dots, spaces,
  apostrophes and accents don't count, so "A.J. Applegate", "AJ Applegate" and
  "aj applegate" are the same performer. New values land on the spelling you already have.
* Same-name performers: different people called "Jane Doe" are stored as "Jane Doe (1)"
  (the main one), "Jane Doe (2)", ...

No FastAPI here: used by database.py, ext_db.py, the parser, automation and routes.
"""
import json
import re
import sqlite3
import unicodedata

NOT_SAME_KEY = 'cast_not_same'       # metadata: spelling groups you said are different people
_SUFFIX = re.compile(r'^(.*\S)\s*\((\d+)\)\s*$')


def norm(name):
    """'A.J. Applegate' -> 'ajapplegate' (same as ext_db.norm_name)."""
    s = unicodedata.normalize('NFKD', name or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def split_suffix(name):
    """'Jane Doe (2)' -> ('Jane Doe', 2); 'Jane Doe' -> ('Jane Doe', None)."""
    m = _SUFFIX.match(name or '')
    return (m.group(1), int(m.group(2))) if m else ((name or '').strip(), None)


def base_name(name):
    return split_suffix(name)[0]


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _exists(conn, table):
    return bool(_rows(conn, "SELECT 1 FROM sqlite_master WHERE name = ?", (table,)))


def numbered(conn):
    """norm(base name) -> ['Jane Doe (1)', 'Jane Doe (2)', ...], numbers in order."""
    out = {}
    for (v,) in _rows(conn, 'SELECT value FROM video_cast'):
        b, n = split_suffix(v)
        if n is not None:
            out.setdefault(norm(b), []).append((n, v))
    return {k: [v for _, v in sorted(vs)] for k, vs in out.items()}


def find_existing(conn, value):
    """The stored spelling for a cast name, or None. Exact first; then the same name written
    differently (capitals, dots, accents); a bare name that only exists as numbered same-name
    performers goes to the main one (1)."""
    value = (value or '').strip()
    if not value:
        return None
    row = conn.execute('SELECT value FROM video_cast WHERE value = ?', (value,)).fetchone()
    if row:
        return row[0]
    key = norm(value)
    if not key:
        return None
    hits = []
    for vid, v in _rows(conn, 'SELECT id, value FROM video_cast'):
        if norm(v) == key:
            n = conn.execute('SELECT COUNT(*) FROM video_cast_junction WHERE cast_id = ?', (vid,)).fetchone()[0]
            hits.append((n, v))
    if hits:
        return max(hits)[1]                               # several spellings left: the most used
    nums = numbered(conn).get(key)
    return nums[0] if nums else None


def stored_case(conn, name):
    """A name as it is stored, ignoring capitals only: 'jessie rogers' -> 'Jessie Rogers'.
    For reading a profile (bio, photo, aliases); unknown names come back unchanged."""
    name = (name or '').strip()
    if not name:
        return name
    places = [(t, c) for t, c in [('video_cast', 'value'), ('cast_aliases', 'alias_name'), ('cast_aliases', 'primary_name'),
                                  ('cast_photos', 'cast_name'), ('cast_attributes', 'cast_name')] if _exists(conn, t)]
    for collate in ('', ' COLLATE NOCASE'):           # exact anywhere first, then ignoring capitals
        for table, col in places:
            row = conn.execute(f'SELECT {col} FROM {table} WHERE {col} = ?{collate} LIMIT 1', (name,)).fetchone()
            if row:
                return row[0]
    return name


def cast_value_id(conn, value):
    """Get or create the video_cast row for a name, reusing the spelling you already have."""
    value = (value or '').strip()
    existing = find_existing(conn, value) or value
    row = conn.execute('SELECT id FROM video_cast WHERE value = ?', (existing,)).fetchone()
    return row[0] if row else conn.execute('INSERT INTO video_cast (value) VALUES (?)', (existing,)).lastrowid


def canonical(conn, value):
    """The spelling a new cast value should be stored as."""
    return find_existing(conn, value) or (value or '').strip()


# ---------------------------------------------------------------------------
# Same performer, different spelling
# ---------------------------------------------------------------------------
def _not_same(conn):
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (NOT_SAME_KEY,)).fetchone()
        return set(json.loads(row[0])) if row else set()
    except Exception:
        return set()


def _group_key(values):
    return '|'.join(sorted(values))


def spelling_groups(conn):
    """Cast values that are one name written differently. -> [{key, values: [{value, videos, alias_of}], keep}]"""
    counts = {r[0]: r[1] for r in _rows(conn, 'SELECT vc.value, COUNT(j.video_id) FROM video_cast vc '
                                              'LEFT JOIN video_cast_junction j ON j.cast_id = vc.id GROUP BY vc.id')}
    alias_of = {r[0]: r[1] for r in _rows(conn, 'SELECT alias_name, primary_name FROM cast_aliases')}
    groups = {}
    for v in counts:
        groups.setdefault(norm(v), []).append(v)
    skip = _not_same(conn)
    out = []
    for key, vs in groups.items():
        if len(vs) < 2 or not key or _group_key(vs) in skip:
            continue
        rows = sorted(({'value': v, 'videos': counts[v], 'alias_of': alias_of.get(v)} for v in vs),
                      key=lambda r: (r['alias_of'] is not None, -r['videos'], r['value']))
        out.append({'key': _group_key(vs), 'values': rows, 'keep': rows[0]['value']})
    return sorted(out, key=lambda g: g['values'][0]['value'].lower())


def case_only_groups(conn):
    """The spelling groups that differ only in capitals ('Jessie Rogers' / 'jessie rogers')."""
    return [g for g in spelling_groups(conn) if len({v['value'].lower() for v in g['values']}) == 1]


def merge_case_only(conn):
    """Merge every case-only group into its usual spelling (spelling_groups' keep). Other spelling
    differences (dots, hyphens, spaces) are left for Maintenance, as they may be different people."""
    merged, links = [], 0
    for g in case_only_groups(conn):
        others = [v['value'] for v in g['values'] if v['value'] != g['keep']]
        links += merge_cast_values(conn, g['keep'], others)
        merged.append(f"{', '.join(others)} -> {g['keep']}")
    return {'count': len(merged), 'links': links, 'samples': merged[:10]}


def mark_not_same(conn, values):
    s = _not_same(conn)
    s.add(_group_key(values))
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (NOT_SAME_KEY, json.dumps(sorted(s), ensure_ascii=False)))


def _rename_in_learned(conn, old, new):
    """Parse Filenames' remembered corrections that point at a performer follow the rename."""
    row = _rows(conn, "SELECT value FROM metadata WHERE key = 'parse_learned'")
    if not row:
        return
    try:
        d = json.loads(row[0][0])
    except ValueError:
        return
    names = d.get('names') or {}
    if old in names.values():
        d['names'] = {k: (new if v == old else v) for k, v in names.items()}
        conn.execute("UPDATE metadata SET value = ? WHERE key = 'parse_learned'", (json.dumps(d, ensure_ascii=False),))


def merge_cast_values(conn, keep, others):
    """`others` become `keep` everywhere: video links (no duplicates), Male Cast marks, profile,
    aliases and remembered corrections. -> number of video links moved"""
    import db_maintenance
    keep_id = conn.execute('SELECT id FROM video_cast WHERE value = ?', (keep,)).fetchone()
    keep_id = keep_id[0] if keep_id else conn.execute('INSERT INTO video_cast (value) VALUES (?)', (keep,)).lastrowid
    has_role = _exists(conn, 'video_cast_role')
    moved = 0
    for other in others:
        if other == keep:
            continue
        row = conn.execute('SELECT id FROM video_cast WHERE value = ?', (other,)).fetchone()
        if row:
            oid = row[0]
            moved += conn.execute('INSERT OR IGNORE INTO video_cast_junction (video_id, cast_id) '
                                  'SELECT j.video_id, ? FROM video_cast_junction j WHERE j.cast_id = ? AND NOT EXISTS '
                                  '(SELECT 1 FROM video_cast_junction k WHERE k.video_id = j.video_id AND k.cast_id = ?)',
                                  (keep_id, oid, keep_id)).rowcount
            conn.execute('DELETE FROM video_cast_junction WHERE cast_id = ?', (oid,))
            if has_role:
                conn.execute('INSERT OR IGNORE INTO video_cast_role (video_id, cast_id, role) '
                             'SELECT video_id, ?, role FROM video_cast_role WHERE cast_id = ?', (keep_id, oid))
                conn.execute('DELETE FROM video_cast_role WHERE cast_id = ?', (oid,))
            conn.execute('DELETE FROM video_cast WHERE id = ?', (oid,))
        # aliases: other's aliases now point at keep; if other was someone's alias, keep is too
        if _exists(conn, 'cast_aliases'):
            for (p,) in _rows(conn, 'SELECT primary_name FROM cast_aliases WHERE alias_name = ?', (other,)):
                if p != keep:
                    conn.execute('INSERT OR IGNORE INTO cast_aliases (primary_name, alias_name) VALUES (?, ?)', (p, keep))
            conn.execute('DELETE FROM cast_aliases WHERE alias_name = ?', (other,))
            conn.execute('UPDATE OR IGNORE cast_aliases SET primary_name = ? WHERE primary_name = ?', (keep, other))
            conn.execute('DELETE FROM cast_aliases WHERE primary_name = ?', (other,))
            conn.execute('DELETE FROM cast_aliases WHERE primary_name = alias_name')
        db_maintenance.merge_cast_profile(conn, keep, other)
        if _exists(conn, 'cast_photos'):                  # keep has its own photo: other's isn't needed any more
            conn.execute('DELETE FROM cast_photos WHERE cast_name = ?', (other,))
        _rename_in_learned(conn, other, keep)
    return moved


# ---------------------------------------------------------------------------
# Same-name performers: 'Jane Doe (1)' (the main one), 'Jane Doe (2)', ...
# ---------------------------------------------------------------------------
PROFILE_PARTS = {                     # what can move to the other performer when splitting
    'photo': [('cast_photos', 'cast_name')],
    'ext_link': [('cast_ext_link', 'cast_name'), ('cast_ext_bulk_log', 'cast_name')],
    'attributes': [('cast_attributes', 'cast_name')],
    'aliases': [('cast_aliases', 'primary_name')],
    'gender': [('cast_gender', 'cast_name')],
    'category': [('cast_category', 'cast_name')],
}
_NAME_TABLES = [t for parts in PROFILE_PARTS.values() for t in parts] + [('favorite_cast', 'name')]


def siblings(conn, name):
    """Every performer sharing this name: ['Jane Doe (1)', 'Jane Doe (2)'] (or [] when there's only one)."""
    return numbered(conn).get(norm(base_name(name)), [])


def rename_cast_value(conn, old, new):
    """`old` is called `new` everywhere (videos, profile, aliases, favourites, remembered corrections).
    If `new` already exists the two are merged."""
    if old == new:
        return
    if conn.execute('SELECT 1 FROM video_cast WHERE value = ?', (new,)).fetchone():
        merge_cast_values(conn, new, [old])
        return
    conn.execute('UPDATE video_cast SET value = ? WHERE value = ?', (new, old))
    for table, col in _NAME_TABLES:
        if _exists(conn, table):
            conn.execute(f'UPDATE OR IGNORE {table} SET {col} = ? WHERE {col} = ?', (new, old))
            conn.execute(f'DELETE FROM {table} WHERE {col} = ?', (old,))
    if _exists(conn, 'cast_aliases'):
        conn.execute('UPDATE OR IGNORE cast_aliases SET alias_name = ? WHERE alias_name = ?', (new, old))
    _rename_in_learned(conn, old, new)


def split_performer(conn, name, video_ids, move=None):
    """Some of `name`'s videos belong to a different person with the same name. They move to a new
    'Name (n)'; a plain 'Name' becomes 'Name (1)' first. `move`: profile parts (PROFILE_PARTS keys)
    that go with them. -> {'kept': 'Jane Doe (1)', 'new': 'Jane Doe (2)', 'videos': n}"""
    base, n = split_suffix(name)
    row = conn.execute('SELECT id FROM video_cast WHERE value = ?', (name,)).fetchone()
    if not row:
        raise ValueError(f'No performer called "{name}"')
    if n is None:
        taken = siblings(conn, name)
        if taken:                               # 'Jane Doe' next to 'Jane Doe (1)': can't happen through the UI
            raise ValueError(f'"{name}" already has numbered namesakes - merge or rename it first')
        rename_cast_value(conn, name, f'{base} (1)')
        name = f'{base} (1)'
    old_id = conn.execute('SELECT id FROM video_cast WHERE value = ?', (name,)).fetchone()[0]
    nums = [split_suffix(v)[1] for v in siblings(conn, name)]
    new = f'{base} ({max(nums + [1]) + 1})'
    new_id = conn.execute('INSERT INTO video_cast (value) VALUES (?)', (new,)).lastrowid
    moved = 0
    for vid in video_ids or []:
        moved += conn.execute('UPDATE video_cast_junction SET cast_id = ? WHERE cast_id = ? AND video_id = ?',
                              (new_id, old_id, vid)).rowcount
        if _exists(conn, 'video_cast_role'):
            conn.execute('UPDATE video_cast_role SET cast_id = ? WHERE cast_id = ? AND video_id = ?', (new_id, old_id, vid))
    for part in move or []:
        for table, col in PROFILE_PARTS.get(part, []):
            if _exists(conn, table):
                conn.execute(f'UPDATE {table} SET {col} = ? WHERE {col} = ?', (new, name))
    return {'kept': name, 'new': new, 'videos': moved}


def make_main(conn, name):
    """'Jane Doe (3)' becomes the main 'Jane Doe (1)'; the old (1) takes number 3."""
    base, n = split_suffix(name)
    if n is None or n == 1:
        return name
    main = f'{base} (1)'
    if not conn.execute('SELECT 1 FROM video_cast WHERE value = ?', (main,)).fetchone():
        rename_cast_value(conn, name, main)
        return main
    temp = f'{base} (#swap#)'
    rename_cast_value(conn, main, temp)
    rename_cast_value(conn, name, main)
    rename_cast_value(conn, temp, name)
    return main


def expand_search_names(conn, names):
    """cast:"Jane Doe" means every Jane Doe: bare names get their numbered namesakes added."""
    nums = None
    out = []
    for n in names:
        out.append(n)
        if split_suffix(n)[1] is None:
            nums = numbered(conn) if nums is None else nums
            out += [v for v in nums.get(norm(n), []) if v not in out]
    return out
