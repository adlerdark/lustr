# This file is: ./db_maintenance.py
"""
Database maintenance: audit, deleted/moved file lifecycle, orphan cleanup.

The library is video-based: `videos` (one row per logical video) and
`file_versions` (one row per file on disk), with list metadata in
`video_<field>` + `video_<field>_junction`.

Deleted files
  A scan that can't find a file sets `file_versions.deleted_at` (soft delete)
  and flags it in `file_changes`. The row - and so the video's metadata - is
  kept for `retention_days`, so a file that was only moved can be re-linked
  however the scan order falls (same path reappears, same filename elsewhere,
  or a cross-library match you review). After the retention period
  `purge_expired` removes it for good, and the video too once it has no files
  left, with everything that depends on it.

Every action here works inside one transaction: `run(dry_run=True)` does the
work, reports exact counts, and rolls back.

No FastAPI imports: scripts can use this module on the host.
"""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

try:
    from config import DELETED_FILE_RETENTION_DAYS as _DEFAULT_RETENTION, DATA_FILE as _DATA_FILE
except Exception:                       # config.py is not in the repo
    _DEFAULT_RETENTION, _DATA_FILE = 7, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'x')

DATA_DIR = os.path.dirname(_DATA_FILE)
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
NEW_FLAG_DAYS = 14                      # 'new' file flags are only needed for cross-library matching
GUARD_SHARE = 0.5                       # never soft-delete more than this share of all files in one go
SETTINGS_KEY = 'maintenance'
DEFAULT_SETTINGS = {'retention_days': _DEFAULT_RETENTION, 'daily': True, 'auto_backups_keep': 5,
                    'last_run': None, 'last_result': None}

# The old file-based layer (replaced by video_<field> tables)
LEGACY_FIELDS = ['ass', 'body', 'cast', 'cock', 'cumshot', 'ethnicity', 'face', 'feature', 'hair',
                 'orientation', 'outfit', 'participants', 'site', 'tags', 'theme', 'tits']
LEGACY_TABLES = ([f'file_{f}' for f in LEGACY_FIELDS] + [f'file_{f}_junction' for f in LEGACY_FIELDS]
                 + ['cast_primary_attributes', 'video_tag_exclusions'])
LEGACY_VIEWS = ['files']
# Performer data that makes a cast name worth keeping even without videos
CAST_PROFILE_TABLES = [('cast_photos', 'cast_name'), ('cast_attributes', 'cast_name'), ('cast_gender', 'cast_name'),
                       ('cast_category', 'cast_name'), ('cast_ext_link', 'cast_name'), ('favorite_cast', 'name'),
                       ('cast_aliases', 'primary_name')]
# Rows keyed by video_id that have no foreign key back to videos
VIDEO_TABLES = ['video_ext_link', 'video_ext_bulk_log', 'video_cast_role', 'playlist_items', 'video_markers']

ACTIONS = ['sync_libraries', 'soft_delete_unowned', 'flag_hygiene', 'purge_expired', 'orphan_videos',
           'orphan_junctions', 'orphan_ext_rows', 'merge_alias_profiles', 'orphan_values']
AUTO_ACTIONS = ['sync_libraries', 'flag_hygiene', 'purge_expired', 'orphan_videos', 'orphan_junctions',
                'orphan_ext_rows', 'merge_alias_profiles']

_run_lock = threading.Lock()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _one(conn, sql, params=(), default=0):
    r = _rows(conn, sql, params)
    return r[0][0] if r and r[0][0] is not None else default


def _exists(conn, name):
    return bool(_rows(conn, "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)))


def _now():
    return datetime.now().isoformat()


def ensure_schema(conn):
    """Columns this module relies on (added to older databases)."""
    if _exists(conn, 'file_changes'):
        cols = {r[1] for r in _rows(conn, 'PRAGMA table_info(file_changes)')}
        if 'reason' not in cols:
            try:
                conn.execute('ALTER TABLE file_changes ADD COLUMN reason TEXT')
                conn.commit()
            except sqlite3.OperationalError:
                pass                            # read-only connection (audit of a copy)


def list_fields(conn):
    """Every list field that has a video_<field>_junction table."""
    out = []
    for (name,) in _rows(conn, "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'video\\_%\\_junction' ESCAPE '\\'"):
        field = name[len('video_'):-len('_junction')]
        if _exists(conn, f'video_{field}'):
            out.append(field)
    return sorted(out)


def _fk(conn, field):
    cols = [r[1] for r in _rows(conn, f'PRAGMA table_info(video_{field}_junction)')]
    return f'{field}_id' if f'{field}_id' in cols else next((c for c in cols if c.endswith('_id') and c != 'video_id'), None)


def library_roots(conn):
    """[(root, library)] from the saved library configuration, longest root first."""
    raw = _one(conn, "SELECT value FROM metadata WHERE key = 'libraries'", default=None)
    try:
        libs = json.loads(raw) if raw else {}
    except ValueError:
        libs = {}
    return roots_from(libs)


def roots_from(libs):
    """[(root, library)] from a {library: [folders]} dict, longest root first."""
    roots = []
    for name, paths in (libs or {}).items():
        for p in paths or []:
            r = str(p).replace('\\', '/').rstrip('/')
            if r:
                roots.append((r, name))
    return sorted(roots, key=lambda x: -len(x[0]))


def owner_of(path, roots):
    p = (path or '').replace('\\', '/')
    for root, name in roots:
        if p.startswith(root + '/'):
            return name
    return None


def get_settings(conn):
    s = dict(DEFAULT_SETTINGS)
    raw = _one(conn, 'SELECT value FROM metadata WHERE key = ?', (SETTINGS_KEY,), default=None)
    try:
        s.update(json.loads(raw) if raw else {})
    except ValueError:
        pass
    s['retention_days'] = max(1, int(s.get('retention_days') or _DEFAULT_RETENTION))
    return s


def save_settings(conn, changes):
    s = get_settings(conn)
    for k in ('retention_days', 'daily', 'auto_backups_keep', 'last_run', 'last_result'):
        if k in changes:
            s[k] = changes[k]
    s['retention_days'] = max(1, min(365, int(s['retention_days'])))
    s['auto_backups_keep'] = max(1, min(50, int(s.get('auto_backups_keep') or 5)))
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SETTINGS_KEY, json.dumps(s)))
    conn.commit()
    return s


def _cast_with_profile(conn):
    names = set()
    for table, col in CAST_PROFILE_TABLES:
        names |= {r[0] for r in _rows(conn, f'SELECT {col} FROM {table}')}
    return names


# ---------------------------------------------------------------------------
# deleting
# ---------------------------------------------------------------------------
def delete_video_completely(conn, video_id, fields=None):
    """Remove a video and every row that refers to it (no reliance on foreign keys)."""
    for f in fields or list_fields(conn):
        conn.execute(f'DELETE FROM video_{f}_junction WHERE video_id = ?', (video_id,))
    for t in VIDEO_TABLES:
        if _exists(conn, t):
            conn.execute(f'DELETE FROM {t} WHERE video_id = ?', (video_id,))
    if _exists(conn, 'file_changes'):
        conn.execute('DELETE FROM file_changes WHERE video_id = ?', (video_id,))
    conn.execute('DELETE FROM file_versions WHERE video_id = ?', (video_id,))
    conn.execute('DELETE FROM videos WHERE video_id = ?', (video_id,))


def purge_versions(conn, paths):
    """Permanently remove these file versions (and videos left with no files). -> (versions, videos)"""
    fields = list_fields(conn)
    vids, n = set(), 0
    for p in paths:
        r = conn.execute('SELECT video_id, is_preferred FROM file_versions WHERE path = ?', (p,)).fetchone()
        if not r:
            continue
        conn.execute('DELETE FROM file_versions WHERE path = ?', (p,))
        if _exists(conn, 'file_changes'):
            conn.execute("DELETE FROM file_changes WHERE filepath = ? AND flag_type IN ('deleted', 'pending_deletion')", (p,))
        vids.add(r[0])
        n += 1
    gone = 0
    for vid in vids:
        if not conn.execute('SELECT 1 FROM file_versions WHERE video_id = ? LIMIT 1', (vid,)).fetchone():
            delete_video_completely(conn, vid, fields)
            gone += 1
        elif not conn.execute('SELECT 1 FROM file_versions WHERE video_id = ? AND is_preferred = 1 AND deleted_at IS NULL',
                              (vid,)).fetchone():
            conn.execute('UPDATE file_versions SET is_preferred = 1 WHERE path = (SELECT path FROM file_versions '
                         'WHERE video_id = ? AND deleted_at IS NULL ORDER BY path LIMIT 1)', (vid,))
    return n, gone


def soft_delete_paths(conn, paths, reason, library=None):
    """Mark files as deleted (kept for the retention period) and flag them. -> count"""
    ensure_schema(conn)
    now, n = _now(), 0
    for p in paths:
        r = conn.execute('SELECT fv.video_id, fv.filename, fv.length_seconds, fv.codec, fv.resolution, fv.total_bitrate, v.library '
                         'FROM file_versions fv LEFT JOIN videos v ON v.video_id = fv.video_id '
                         'WHERE fv.path = ? AND fv.deleted_at IS NULL', (p,)).fetchone()
        if not r:
            continue
        conn.execute('UPDATE file_versions SET deleted_at = ? WHERE path = ?', (now, p))
        conn.execute('INSERT OR REPLACE INTO file_changes (filename, filepath, video_id, library, flag_type, flagged_at, '
                     'file_length, codec, bitrate, resolution, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (os.path.splitext(r[1] or os.path.basename(p))[0], p, r[0], library or r[6] or 'Unknown', 'deleted', now,
                      r[2], r[3], r[5], r[4], reason))
        n += 1
    return n


def clear_flags_for_path(conn, path):
    """A file is back where it was: its deletion flags no longer apply."""
    if _exists(conn, 'file_changes'):
        conn.execute("DELETE FROM file_changes WHERE filepath = ? AND flag_type IN ('deleted', 'pending_deletion')", (path,))


def forget_path(conn, path):
    """A file was re-linked to a new path: nothing about its old path needs attention."""
    if _exists(conn, 'file_changes'):
        conn.execute("DELETE FROM file_changes WHERE filepath = ? AND flag_type IN ('deleted', 'pending_deletion', 'new')", (path,))


# ---------------------------------------------------------------------------
# libraries
# ---------------------------------------------------------------------------
def _video_paths(conn):
    """video_id -> the path that decides its library (preferred active, else any active, else any)."""
    best = {}
    for vid, path, pref, dele in _rows(conn, 'SELECT video_id, path, is_preferred, deleted_at FROM file_versions'):
        rank = (0 if dele else 2) + (1 if pref else 0)
        cur = best.get(vid)
        if cur is None or rank > cur[1] or (rank == cur[1] and path < cur[0]):
            best[vid] = (path, rank)
    return {v: p for v, (p, _r) in best.items()}


def sync_video_libraries(conn, roots=None):
    """Set videos.library from where each video's file actually lives. -> {changed, samples}"""
    roots = roots if roots is not None else library_roots(conn)
    if not roots:
        return {'count': 0, 'skipped': 'no libraries configured'}
    paths = _video_paths(conn)
    changed, samples = 0, []
    for vid, lib in _rows(conn, 'SELECT video_id, library FROM videos'):
        owner = owner_of(paths.get(vid), roots)
        if owner and owner != lib:
            conn.execute('UPDATE videos SET library = ? WHERE video_id = ?', (owner, vid))
            changed += 1
            if len(samples) < 10:
                samples.append(f'{lib} -> {owner}: {os.path.basename(paths.get(vid) or "")}')
    return {'count': changed, 'samples': samples}


def unowned_active_paths(conn, roots=None):
    roots = roots if roots is not None else library_roots(conn)
    return [p for (p,) in _rows(conn, 'SELECT path FROM file_versions WHERE deleted_at IS NULL') if not owner_of(p, roots)]


def soft_delete_unowned(conn, reason='library_removed', roots=None):
    """Files no configured library covers any more (a library or folder was removed). Guarded."""
    roots = roots if roots is not None else library_roots(conn)
    if not roots:
        return {'count': 0, 'skipped': 'no libraries configured - nothing changed'}
    paths = unowned_active_paths(conn, roots)
    total = _one(conn, 'SELECT COUNT(*) FROM file_versions WHERE deleted_at IS NULL')
    if paths and total and len(paths) > GUARD_SHARE * total:
        return {'count': 0, 'skipped': f'{len(paths)} of {total} files would be removed - looks like a configuration '
                                       f'problem, nothing changed'}
    n = soft_delete_paths(conn, paths, reason)
    return {'count': n, 'samples': paths[:10]}


# ---------------------------------------------------------------------------
# lifecycle actions
# ---------------------------------------------------------------------------
def flag_hygiene(conn):
    ensure_schema(conn)
    if not _exists(conn, 'file_changes'):
        return {'count': 0}
    out = {}
    # pending_deletion (old two-step expiry) -> a plain 'deleted' flag when its file is really soft-deleted
    out['pending_converted'] = conn.execute(
        "UPDATE file_changes SET flag_type = 'deleted' WHERE flag_type = 'pending_deletion' AND filepath IN "
        "(SELECT path FROM file_versions WHERE deleted_at IS NOT NULL)").rowcount
    out['pending_removed'] = conn.execute("DELETE FROM file_changes WHERE flag_type = 'pending_deletion'").rowcount
    # deletion flags for files that are back, or no longer in the database
    out['deleted_recovered'] = conn.execute(
        "DELETE FROM file_changes WHERE flag_type = 'deleted' AND filepath IN "
        "(SELECT path FROM file_versions WHERE deleted_at IS NULL)").rowcount
    out['deleted_gone'] = conn.execute(
        "DELETE FROM file_changes WHERE flag_type = 'deleted' AND filepath NOT IN (SELECT path FROM file_versions)").rowcount
    cutoff = (datetime.now() - timedelta(days=NEW_FLAG_DAYS)).isoformat()
    out['new_expired'] = conn.execute("DELETE FROM file_changes WHERE flag_type = 'new' AND related_id IS NULL "
                                      "AND flagged_at < ?", (cutoff,)).rowcount
    out['pairs_broken'] = conn.execute("DELETE FROM file_changes WHERE related_id IS NOT NULL "
                                       "AND related_id NOT IN (SELECT id FROM file_changes)").rowcount
    out['pairs_broken'] += conn.execute("DELETE FROM file_changes WHERE flag_type = 'possible_replacement' "
                                        "AND related_id IS NULL").rowcount
    out['count'] = sum(v for k, v in out.items())
    return out


def _review_paths(conn):
    """Soft-deleted files waiting for you to review a possible replacement - never purged automatically."""
    return {r[0] for r in _rows(conn, "SELECT filepath FROM file_changes WHERE flag_type = 'possibly_replaced'")}


def expired_paths(conn, days):
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    keep = _review_paths(conn)
    return [p for (p,) in _rows(conn, 'SELECT path FROM file_versions WHERE deleted_at IS NOT NULL AND deleted_at < ?',
                                (cutoff,)) if p not in keep]


def _storage_root(path, roots):
    """The folder that must be reachable before we may believe a file under it is gone:
    its library root, or (outside every library) its top two folders, e.g. /media/s."""
    p = (path or '').replace('\\', '/')
    for root, _name in roots:
        if p.startswith(root + '/'):
            return root
    parts = [x for x in p.split('/') if x]
    return '/' + '/'.join(parts[:2]) if len(parts) > 2 else None


def check_on_disk(paths, roots):
    """-> (gone, back, unreachable). 'back': the file exists again inside a configured library.
    'gone': missing from disk, or outside every library (its library was removed).
    'unreachable': its storage can't be reached right now - a NAS that isn't mounted
    must never cause a purge."""
    gone, back, unreachable, seen = [], [], [], {}
    for p in paths:
        if not owner_of(p, roots):
            gone.append(p)                       # library or folder removed: nothing to wait for
            continue
        root = _storage_root(p, roots)
        if root not in seen:
            seen[root] = bool(root) and os.path.isdir(root)
        if not seen[root]:
            unreachable.append(p)
        elif os.path.exists(p):
            back.append(p)
        else:
            gone.append(p)
    return gone, back, unreachable


def restore_paths(conn, paths):
    n = 0
    for p in paths:
        n += conn.execute('UPDATE file_versions SET deleted_at = NULL WHERE path = ? AND deleted_at IS NOT NULL', (p,)).rowcount
        clear_flags_for_path(conn, p)
    return n


def purge_expired(conn, days):
    gone, back, unreachable = check_on_disk(expired_paths(conn, days), library_roots(conn))
    restored = restore_paths(conn, back)
    n, removed = purge_versions(conn, gone)
    out = {'count': n, 'videos_removed': removed, 'restored_found_on_disk': restored, 'samples': gone[:10]}
    if unreachable:
        out['skipped_storage_unreachable'] = len(unreachable)
    return out


def orphan_videos(conn):
    fields = list_fields(conn)
    vids = [r[0] for r in _rows(conn, 'SELECT video_id FROM videos WHERE video_id NOT IN (SELECT video_id FROM file_versions)')]
    for v in vids:
        delete_video_completely(conn, v, fields)
    return {'count': len(vids)}


def orphan_junctions(conn):
    out = {}
    for f in list_fields(conn):
        n = conn.execute(f'DELETE FROM video_{f}_junction WHERE video_id NOT IN (SELECT video_id FROM videos)').rowcount
        if n:
            out[f] = n
    return {'count': sum(out.values()), 'by_field': out}


def orphan_ext_rows(conn):
    out = {}
    for t in VIDEO_TABLES:
        if _exists(conn, t):
            n = conn.execute(f'DELETE FROM {t} WHERE video_id NOT IN (SELECT video_id FROM videos)').rowcount
            if n:
                out[t] = n
    if _exists(conn, 'video_cast_role'):
        n = conn.execute('DELETE FROM video_cast_role WHERE NOT EXISTS (SELECT 1 FROM video_cast_junction j '
                         'WHERE j.video_id = video_cast_role.video_id AND j.cast_id = video_cast_role.cast_id)').rowcount
        if n:
            out['video_cast_role (not in cast)'] = n
    if _exists(conn, 'file_changes'):
        n = conn.execute("DELETE FROM file_changes WHERE video_id IS NOT NULL AND flag_type IN ('deleted', 'pending_deletion') "
                         "AND video_id NOT IN (SELECT video_id FROM videos)").rowcount
        if n:
            out['file_changes'] = n
    return {'count': sum(out.values()), 'by_table': out}


def orphan_values(conn):
    """Values no video uses any more. Performer names with profile data are kept."""
    keep_cast = _cast_with_profile(conn)
    out, kept = {}, []
    for f in list_fields(conn):
        fk = _fk(conn, f)
        if not fk:
            continue
        rows = _rows(conn, f'SELECT id, value FROM video_{f} WHERE id NOT IN (SELECT {fk} FROM video_{f}_junction)')
        ids = []
        for vid_, value in rows:
            if f == 'cast' and value in keep_cast:
                kept.append(value)
                continue
            ids.append(vid_)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            conn.execute(f'DELETE FROM video_{f} WHERE id IN ({",".join("?" * len(chunk))})', chunk)
        if ids:
            out[f] = len(ids)
    return {'count': sum(out.values()), 'by_field': out, 'cast_profiles_kept': len(kept)}


# ---------------------------------------------------------------------------
# audit (read-only)
# ---------------------------------------------------------------------------
def audit(conn):
    ensure_schema(conn)
    settings = get_settings(conn)
    days = settings['retention_days']
    roots = library_roots(conn)
    rep = {'generated_at': _now(), 'retention_days': days, 'settings': settings}

    # legacy layer
    legacy = {}
    for t in LEGACY_TABLES:
        if _exists(conn, t):
            legacy[t] = _one(conn, f'SELECT COUNT(*) FROM "{t}"')
    views = [v for v in LEGACY_VIEWS if _exists(conn, v)]
    rep['legacy'] = {'tables': legacy, 'table_count': len(legacy), 'rows': sum(legacy.values()), 'views': views}

    # videos and files
    rep['videos'] = {
        'videos': _one(conn, 'SELECT COUNT(*) FROM videos'),
        'files': _one(conn, 'SELECT COUNT(*) FROM file_versions'),
        'active_files': _one(conn, 'SELECT COUNT(*) FROM file_versions WHERE deleted_at IS NULL'),
        'videos_without_files': _one(conn, 'SELECT COUNT(*) FROM videos WHERE video_id NOT IN (SELECT video_id FROM file_versions)'),
        'videos_all_deleted': _one(conn, 'SELECT COUNT(*) FROM videos v WHERE EXISTS (SELECT 1 FROM file_versions f WHERE f.video_id = v.video_id) '
                                         'AND NOT EXISTS (SELECT 1 FROM file_versions f WHERE f.video_id = v.video_id AND f.deleted_at IS NULL)'),
    }

    # libraries
    configured = sorted({n for _r, n in roots})
    paths = _video_paths(conn)
    stale, unknown, unowned, by_lib, samples = 0, 0, 0, {}, []
    for vid, lib in _rows(conn, 'SELECT video_id, library FROM videos'):
        by_lib[lib] = by_lib.get(lib, 0) + 1
        owner = owner_of(paths.get(vid), roots)
        if owner is None:
            unowned += 1
        elif owner != lib:
            stale += 1
            if lib == 'Unknown':
                unknown += 1
            elif len(samples) < 10:
                samples.append(f'{lib} -> {owner}')
    rep['libraries'] = {'configured': configured, 'by_db_library': dict(sorted(by_lib.items(), key=lambda x: -x[1])),
                        'not_configured': sorted(k for k in by_lib if k not in configured),
                        'stale': stale, 'unknown': unknown, 'videos_outside_libraries': unowned,
                        'active_files_outside_libraries': len(unowned_active_paths(conn, roots)) if roots else 0,
                        'samples': samples}

    # deleted files and flags
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    review = _review_paths(conn)
    soft = _rows(conn, 'SELECT path, deleted_at FROM file_versions WHERE deleted_at IS NOT NULL')
    has_reason = 'reason' in {r[1] for r in _rows(conn, 'PRAGMA table_info(file_changes)')}
    reasons = ({r[0]: r[1] for r in _rows(conn, "SELECT filepath, reason FROM file_changes WHERE flag_type IN ('deleted', 'possibly_replaced')")}
               if has_reason else {})
    rep['deleted'] = {
        'soft_deleted': len(soft),
        'within_retention': sum(1 for _p, d in soft if d >= cutoff),
        'expired': sum(1 for p, d in soft if d < cutoff and p not in review),
        'waiting_review': sum(1 for p, _d in soft if p in review),
        'library_removed': sum(1 for p, _d in soft if reasons.get(p) == 'library_removed'),
        'oldest': min((d for _p, d in soft), default=None),
    }
    flags = {r[0]: r[1] for r in _rows(conn, 'SELECT flag_type, COUNT(*) FROM file_changes GROUP BY flag_type')}
    rep['flags'] = {
        'by_type': flags,
        'pending_deletion_legacy': flags.get('pending_deletion', 0),
        'deleted_but_file_active': _one(conn, "SELECT COUNT(*) FROM file_changes WHERE flag_type IN ('deleted', 'pending_deletion') "
                                              "AND filepath IN (SELECT path FROM file_versions WHERE deleted_at IS NULL)"),
        'deleted_but_file_gone': _one(conn, "SELECT COUNT(*) FROM file_changes WHERE flag_type IN ('deleted', 'pending_deletion') "
                                            "AND filepath NOT IN (SELECT path FROM file_versions)"),
        'new_older_than_%d_days' % NEW_FLAG_DAYS: _one(conn, "SELECT COUNT(*) FROM file_changes WHERE flag_type = 'new' AND related_id IS NULL "
                                                             "AND flagged_at < ?", ((datetime.now() - timedelta(days=NEW_FLAG_DAYS)).isoformat(),)),
        'review_pairs': flags.get('possibly_replaced', 0),
    }

    # orphans
    junc, vals = {}, {}
    keep_cast = _cast_with_profile(conn)
    cast_bare = cast_profile = 0
    for f in list_fields(conn):
        n = _one(conn, f'SELECT COUNT(*) FROM video_{f}_junction WHERE video_id NOT IN (SELECT video_id FROM videos)')
        if n:
            junc[f] = n
        fk = _fk(conn, f)
        if fk:
            unused = _rows(conn, f'SELECT value FROM video_{f} WHERE id NOT IN (SELECT {fk} FROM video_{f}_junction)')
            if f == 'cast':
                cast_profile = sum(1 for (v,) in unused if v in keep_cast)
                cast_bare = len(unused) - cast_profile
            elif unused:
                vals[f] = len(unused)
    ext = {t: _one(conn, f'SELECT COUNT(*) FROM {t} WHERE video_id NOT IN (SELECT video_id FROM videos)')
           for t in VIDEO_TABLES if _exists(conn, t)}
    rep['orphans'] = {'junction_rows': junc, 'junction_total': sum(junc.values()), 'unused_values': vals,
                      'cast_bare_names': cast_bare, 'cast_profiles_without_videos': len(orphan_cast_profiles(conn)),
                      'video_rows': {k: v for k, v in ext.items() if v}}

    rep['backups'] = list_backups()
    rep['backups_bytes'] = sum(b['size'] for b in rep['backups'])
    return rep


def orphan_cast_profiles(conn):
    """Performers with profile data (photo, attributes, link...) who appear in no video."""
    in_videos = {r[0] for r in _rows(conn, 'SELECT DISTINCT vc.value FROM video_cast vc JOIN video_cast_junction j ON j.cast_id = vc.id '
                                           'JOIN videos v ON v.video_id = j.video_id')}
    aliases = {}
    for alias, primary in _rows(conn, 'SELECT alias_name, primary_name FROM cast_aliases'):
        aliases.setdefault(primary, set()).add(alias)
    alias_names = {r[0] for r in _rows(conn, 'SELECT alias_name FROM cast_aliases')}
    out = []
    for name in sorted(_cast_with_profile(conn), key=str.lower):
        if name in alias_names or name in in_videos or (aliases.get(name, set()) & in_videos):
            continue
        has = [t for t, col in CAST_PROFILE_TABLES if _rows(conn, f'SELECT 1 FROM {t} WHERE {col} = ? LIMIT 1', (name,))]
        out.append({'name': name, 'has': has})
    return out


def merge_cast_profile(conn, into, frm, winner='into'):
    """Fold the profile stored under `frm` into `into` (one performer, two names).
    Attributes are combined (starred if starred on either side). Gender, category and the
    external link come from `winner` ('into' or 'from') when both have one. The photo:
    `into` keeps its own; if it has none it takes frm's; otherwise frm's photo stays stored
    under frm's name, so making that name primary later brings it back. Nothing that
    exists on only one side is lost. -> {table: rows changed}"""
    if not into or not frm or into == frm:
        return {}
    out = {}
    if _exists(conn, 'cast_attributes'):
        conn.execute('INSERT OR IGNORE INTO cast_attributes (cast_name, attribute_type, attribute_value, is_primary, video_count) '
                     'SELECT ?, attribute_type, attribute_value, is_primary, video_count FROM cast_attributes WHERE cast_name = ?',
                     (into, frm))
        conn.execute('UPDATE cast_attributes SET is_primary = 1 WHERE cast_name = ? AND is_primary = 0 AND EXISTS ('
                     'SELECT 1 FROM cast_attributes f WHERE f.cast_name = ? AND f.attribute_type = cast_attributes.attribute_type '
                     'AND f.attribute_value = cast_attributes.attribute_value AND f.is_primary = 1)', (into, frm))
        out['cast_attributes'] = conn.execute('DELETE FROM cast_attributes WHERE cast_name = ?', (frm,)).rowcount
    for table in ('cast_gender', 'cast_category', 'cast_ext_link'):
        if not _exists(conn, table):
            continue
        has_into = conn.execute(f'SELECT 1 FROM {table} WHERE cast_name = ?', (into,)).fetchone()
        has_frm = conn.execute(f'SELECT 1 FROM {table} WHERE cast_name = ?', (frm,)).fetchone()
        if not has_frm:
            continue
        if has_into and winner == 'from':
            conn.execute(f'DELETE FROM {table} WHERE cast_name = ?', (into,))
            has_into = None
        if has_into:
            out[table] = conn.execute(f'DELETE FROM {table} WHERE cast_name = ?', (frm,)).rowcount
        else:
            out[table] = conn.execute(f'UPDATE {table} SET cast_name = ? WHERE cast_name = ?', (into, frm)).rowcount
    if _exists(conn, 'cast_ext_bulk_log'):
        conn.execute('UPDATE OR IGNORE cast_ext_bulk_log SET cast_name = ? WHERE cast_name = ?', (into, frm))
        conn.execute('DELETE FROM cast_ext_bulk_log WHERE cast_name = ?', (frm,))
    if _exists(conn, 'favorite_cast'):
        if conn.execute('SELECT 1 FROM favorite_cast WHERE name = ?', (frm,)).fetchone():
            conn.execute('INSERT OR IGNORE INTO favorite_cast (name) VALUES (?)', (into,))
            out['favorite_cast'] = conn.execute('DELETE FROM favorite_cast WHERE name = ?', (frm,)).rowcount
    if _exists(conn, 'cast_photos'):
        if not conn.execute('SELECT 1 FROM cast_photos WHERE cast_name = ?', (into,)).fetchone():
            out['cast_photos'] = conn.execute('UPDATE cast_photos SET cast_name = ? WHERE cast_name = ?', (into, frm)).rowcount
    return out


def merge_alias_profiles(conn):
    """Every alias that still has its own profile data: fold it into its primary."""
    merged, rows = [], 0
    for primary, alias in _rows(conn, 'SELECT primary_name, alias_name FROM cast_aliases'):
        r = merge_cast_profile(conn, primary, alias, winner='into')
        n = sum(v for k, v in r.items())
        if n:
            merged.append(f'{alias} -> {primary}')
            rows += n
    return {'count': rows, 'performers': len(merged), 'samples': merged[:10]}


def delete_cast_profiles(conn, names):
    n = 0
    for name in names:
        for t, col in CAST_PROFILE_TABLES + [('cast_ext_bulk_log', 'cast_name')]:
            if _exists(conn, t):
                n += conn.execute(f'DELETE FROM {t} WHERE {col} = ?', (name,)).rowcount
        if _exists(conn, 'cast_aliases'):
            conn.execute('DELETE FROM cast_aliases WHERE alias_name = ?', (name,))
        if _exists(conn, 'video_cast'):
            conn.execute('DELETE FROM video_cast WHERE value = ? AND id NOT IN (SELECT cast_id FROM video_cast_junction)', (name,))
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Tag cleanup (§2.5): malformed values, nationality words stored as ethnicity,
# and site spellings. Preview lists every change; apply does only the ticked ones.
# ---------------------------------------------------------------------------
_VALID_VALUE = re.compile(r'^[a-z0-9][a-z0-9.\-]*$')
TAG_SKIP_FIELDS = ('cast', 'site')


def _norm_key(s):
    """Same as ext_db.norm_name / site_key: accents dropped, lowercase letters and digits only."""
    import unicodedata
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def _demonyms():
    try:
        from ext_db import DEMONYMS
        return set(DEMONYMS.values())
    except Exception:
        return set()


def _is_nationality_word(value, demonyms):
    return value in demonyms and not value.startswith('indian')


def _user_format(part):
    return re.sub(r'\.{2,}', '.', re.sub(r'\s+', '.', part.strip().lower())).strip('.')


def _fix_value(field, value, vocab, demonyms):
    """-> [(field, value)] that a malformed value should become ([] = remove)."""
    raw = (value or '').strip()
    if not raw:
        return []
    if ',' in raw:
        parts = [p for p in (x.strip() for x in raw.split(',')) if p]
        joined = _user_format('.'.join(parts))
        if joined in vocab.get(field, set()):                 # 'big,tits' -> big.tits
            parts = [joined]
    else:
        parts = [raw]
    out = []
    for p in parts:
        v = _user_format(p)
        if not v:
            continue
        f = 'nationality' if field == 'ethnicity' and _is_nationality_word(v, demonyms) else field
        if (f, v) not in out:
            out.append((f, v))
    return out


def _cast_vocab(conn):
    vocab = {}
    for t, v in _rows(conn, 'SELECT DISTINCT attribute_type, attribute_value FROM cast_attributes'):
        vocab.setdefault(t, set()).add(v)
    return vocab


def _video_vocab_sets(conn, fields):
    return {f: {r[0] for r in _rows(conn, f'SELECT value FROM video_{f}')} for f in fields}


def site_keep_default(variants):
    """Your format first: a lowercase spelling (with periods when any variant has them),
    else the lowercase form of the most used one. variants: [(value, videos)]"""
    lower = [v for v, _n in variants if v == v.lower()]
    dotted = [v for v in lower if '.' in v]
    if dotted:
        return max(dotted, key=lambda v: dict(variants)[v])
    if lower:
        return max(lower, key=lambda v: dict(variants)[v])
    return max(variants, key=lambda x: x[1])[0].lower()


def tag_cleanup_preview(conn):
    demonyms = _demonyms()
    fields = [f for f in list_fields(conn) if f not in TAG_SKIP_FIELDS]
    changes = []
    # --- performers ---
    cvocab = _cast_vocab(conn)
    for t, v, n in _rows(conn, 'SELECT attribute_type, attribute_value, COUNT(*) FROM cast_attributes GROUP BY 1, 2'):
        if not _VALID_VALUE.match(v or ''):
            changes.append({'id': f'cast|malformed|{t}|{v}', 'kind': 'malformed', 'scope': 'cast', 'field': t, 'from': v,
                            'to': _fix_value(t, v, cvocab, demonyms), 'rows': n})
        elif t == 'ethnicity' and _is_nationality_word(v, demonyms):
            changes.append({'id': f'cast|nationality|{t}|{v}', 'kind': 'nationality_move', 'scope': 'cast', 'field': t,
                            'from': v, 'to': [('nationality', v)], 'rows': n})
    for name, in _rows(conn, "SELECT DISTINCT a.cast_name FROM cast_attributes a JOIN cast_ext_link l ON l.cast_name = a.cast_name "
                             "WHERE a.attribute_type = 'ethnicity' AND a.attribute_value LIKE 'indian%' "
                             "AND UPPER(COALESCE(l.ext_nationality, '')) = 'IN' AND NOT EXISTS (SELECT 1 FROM cast_attributes b "
                             "WHERE b.cast_name = a.cast_name AND b.attribute_type = 'nationality' AND b.attribute_value = 'indian')"):
        changes.append({'id': f'cast|indian|{name}', 'kind': 'indian_nationality', 'scope': 'cast', 'field': 'nationality',
                        'from': name, 'to': [('nationality', 'indian')], 'rows': 1, 'detail': 'linked record: India'})
    # --- videos ---
    vvocab = _video_vocab_sets(conn, fields)
    for f in fields:
        fk = _fk(conn, f)
        if not fk:
            continue
        for vid_, v, n in _rows(conn, f'SELECT t.id, t.value, COUNT(j.video_id) FROM video_{f} t '
                                      f'JOIN video_{f}_junction j ON j.{fk} = t.id GROUP BY t.id'):
            if not _VALID_VALUE.match(v or ''):
                changes.append({'id': f'video|malformed|{f}|{v}', 'kind': 'malformed', 'scope': 'video', 'field': f, 'from': v,
                                'to': [x for x in _fix_value(f, v, vvocab, demonyms) if x[0] in fields or x[0] == 'nationality'],
                                'rows': n})
            elif f == 'ethnicity' and _is_nationality_word(v, demonyms) and 'nationality' in fields:
                changes.append({'id': f'video|nationality|{f}|{v}', 'kind': 'nationality_move', 'scope': 'video', 'field': f,
                                'from': v, 'to': [('nationality', v)], 'rows': n})
    # --- site spellings ---
    groups = {}
    sfk = _fk(conn, 'site')
    if sfk:
        for v, n in _rows(conn, f'SELECT t.value, COUNT(j.video_id) FROM video_site t JOIN video_site_junction j '
                                f'ON j.{sfk} = t.id GROUP BY t.id'):
            k = _norm_key(v)
            if k:
                groups.setdefault(k, []).append((v, n))
    sites = [{'key': k, 'variants': [{'value': v, 'videos': n} for v, n in sorted(g, key=lambda x: -x[1])],
              'keep': site_keep_default(g), 'videos': sum(n for _v, n in g)}
             for k, g in groups.items() if len(g) > 1]
    sites.sort(key=lambda s: -s['videos'])
    counts = {}
    for c in changes:
        counts[c['kind']] = counts.get(c['kind'], 0) + 1
    counts['sites'] = len(sites)
    counts['site_videos'] = sum(sum(x['videos'] for x in s['variants'] if x['value'] != s['keep']) for s in sites)
    return {'changes': changes, 'sites': sites, 'counts': counts}


def _value_id(conn, field, value):
    row = conn.execute(f'SELECT id FROM video_{field} WHERE value = ?', (value,)).fetchone()
    return row[0] if row else conn.execute(f'INSERT INTO video_{field} (value) VALUES (?)', (value,)).lastrowid


def _move_video_value(conn, field, old_value, targets):
    """Every video with field=old_value gets the target values instead (add-only for the targets)."""
    fk = _fk(conn, field)
    old = conn.execute(f'SELECT id FROM video_{field} WHERE value = ?', (old_value,)).fetchone()
    if not fk or not old:
        return 0
    vids = [r[0] for r in conn.execute(f'SELECT video_id FROM video_{field}_junction WHERE {fk} = ?', (old[0],))]
    for tf, tv in targets:
        tfk = _fk(conn, tf)
        if not tfk:
            continue
        tid = _value_id(conn, tf, tv)
        for v in vids:
            conn.execute(f'INSERT OR IGNORE INTO video_{tf}_junction (video_id, {tfk}) SELECT ?, ? WHERE NOT EXISTS '
                         f'(SELECT 1 FROM video_{tf}_junction WHERE video_id = ? AND {tfk} = ?)', (v, tid, v, tid))
    conn.execute(f'DELETE FROM video_{field}_junction WHERE {fk} = ?', (old[0],))
    conn.execute(f'DELETE FROM video_{field} WHERE id = ?', (old[0],))
    return len(vids)


def _move_cast_value(conn, field, old_value, targets, name=None):
    """Performers with attribute field=old_value get the target attributes instead (starred flag kept)."""
    where, params = 'attribute_type = ? AND attribute_value = ?', [field, old_value]
    if name:
        where += ' AND cast_name = ?'
        params.append(name)
    rows = conn.execute(f'SELECT cast_name, is_primary, video_count FROM cast_attributes WHERE {where}', params).fetchall()
    for cast_name, star, count in rows:
        for tf, tv in targets:
            conn.execute('INSERT OR IGNORE INTO cast_attributes (cast_name, attribute_type, attribute_value, is_primary, video_count) '
                         'VALUES (?, ?, ?, ?, ?)', (cast_name, tf, tv, star, count))
            if star:
                conn.execute('UPDATE cast_attributes SET is_primary = 1 WHERE cast_name = ? AND attribute_type = ? AND attribute_value = ?',
                             (cast_name, tf, tv))
    conn.execute(f'DELETE FROM cast_attributes WHERE {where}', params)
    return len(rows)


def _merge_site_variants(conn, keep, variants):
    fk = _fk(conn, 'site')
    keep_id = _value_id(conn, 'site', keep)
    moved = 0
    for v in variants:
        if v == keep:
            continue
        row = conn.execute('SELECT id FROM video_site WHERE value = ?', (v,)).fetchone()
        if not row:
            continue
        for (vid,) in conn.execute(f'SELECT video_id FROM video_site_junction WHERE {fk} = ?', (row[0],)).fetchall():
            conn.execute(f'INSERT OR IGNORE INTO video_site_junction (video_id, {fk}) SELECT ?, ? WHERE NOT EXISTS '
                         f'(SELECT 1 FROM video_site_junction WHERE video_id = ? AND {fk} = ?)', (vid, keep_id, vid, keep_id))
            moved += 1
        conn.execute(f'DELETE FROM video_site_junction WHERE {fk} = ?', (row[0],))
        conn.execute('DELETE FROM video_site WHERE id = ?', (row[0],))
        if _exists(conn, 'library_default_site'):
            conn.execute('UPDATE library_default_site SET site_value = ? WHERE site_value = ?', (keep, v))
    return moved


def tag_cleanup_apply(conn, change_ids, sites, backup=True):
    """Apply the ticked changes (ids from the preview) and site merges ({key: kept spelling})."""
    preview = tag_cleanup_preview(conn)
    wanted = set(change_ids or [])
    by_key = {s['key']: s for s in preview['sites']}
    picked_sites = {k: keep for k, keep in (sites or {}).items() if k in by_key
                    and keep in [v['value'] for v in by_key[k]['variants']] + [by_key[k]['keep']]}
    todo = [c for c in preview['changes'] if c['id'] in wanted]
    if not todo and not picked_sites:
        return {'success': True, 'applied': {}, 'backup': None}
    conn.commit()
    name = make_backup(conn) if backup else None
    applied = {}
    try:
        for c in todo:
            if c['scope'] == 'cast':
                if c['kind'] == 'indian_nationality':
                    conn.execute("INSERT OR IGNORE INTO cast_attributes (cast_name, attribute_type, attribute_value, is_primary, video_count) "
                                 "VALUES (?, 'nationality', 'indian', 1, 0)", (c['from'],))
                    n = 1
                else:
                    n = _move_cast_value(conn, c['field'], c['from'], [tuple(t) for t in c['to']])
            else:
                n = _move_video_value(conn, c['field'], c['from'], [tuple(t) for t in c['to']])
            applied[c['kind']] = applied.get(c['kind'], 0) + n
        for k, keep in picked_sites.items():
            applied['site_videos'] = applied.get('site_videos', 0) + _merge_site_variants(
                conn, keep, [v['value'] for v in by_key[k]['variants']])
            applied['sites'] = applied.get('sites', 0) + 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'success': True, 'applied': applied, 'backup': name}


# ---------------------------------------------------------------------------
# backups
# ---------------------------------------------------------------------------
def list_backups():
    out = []
    for d in (DATA_DIR, BACKUP_DIR):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            full = os.path.join(d, n)
            if os.path.isfile(full) and n != 'library.db' and (n.endswith('.db') or '.db.' in n) and 'library' in n:
                st = os.stat(full)
                out.append({'name': os.path.relpath(full, DATA_DIR), 'size': st.st_size,
                            'modified': datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')})
    return sorted(out, key=lambda b: b['modified'], reverse=True)


def make_backup(conn, auto=False):
    """Consistent copy of the live database (SQLite backup API) into data/backups/."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"library-{'auto-' if auto else ''}{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    path = os.path.join(BACKUP_DIR, name)
    dst = sqlite3.connect(path)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return os.path.relpath(path, DATA_DIR)


def delete_backups(names):
    n = 0
    for name in names:
        full = os.path.normpath(os.path.join(DATA_DIR, name))
        if not full.startswith(os.path.normpath(DATA_DIR) + os.sep) or os.path.basename(full) == 'library.db':
            continue                                     # only backups inside data/, never the live database
        if any(b['name'] == name for b in list_backups()):
            os.remove(full)
            n += 1
    return n


def _prune_auto_backups(keep):
    autos = [b for b in list_backups() if os.path.basename(b['name']).startswith('library-auto-')]
    return delete_backups([b['name'] for b in autos[keep:]])


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def _apply(conn, actions, days):
    fns = {'sync_libraries': lambda: sync_video_libraries(conn),
           'soft_delete_unowned': lambda: soft_delete_unowned(conn),
           'flag_hygiene': lambda: flag_hygiene(conn),
           'purge_expired': lambda: purge_expired(conn, days),
           'orphan_videos': lambda: orphan_videos(conn),
           'orphan_junctions': lambda: orphan_junctions(conn),
           'orphan_ext_rows': lambda: orphan_ext_rows(conn),
           'merge_alias_profiles': lambda: merge_alias_profiles(conn),
           'orphan_values': lambda: orphan_values(conn)}
    return {a: fns[a]() for a in actions}


def _removed(results):
    return sum(r.get('count', 0) for k, r in results.items() if k not in ('sync_libraries', 'merge_alias_profiles'))


def run(conn, dry_run=True, actions=None, auto=False, vacuum=False):
    """Run maintenance actions in one transaction; dry_run reports exact counts and rolls back.
    A real manual run backs the database up first. An automatic run backs up only when
    something will be removed, at most once a day, and keeps the newest few auto backups."""
    if not _run_lock.acquire(blocking=False):
        return {'success': False, 'error': 'Maintenance is already running'}
    try:
        ensure_schema(conn)
        settings = get_settings(conn)
        days = settings['retention_days']
        actions = [a for a in (actions or (AUTO_ACTIONS if auto else ACTIONS)) if a in ACTIONS]
        started = time.time()
        conn.commit()                                    # start from a clean transaction
        backup = None
        if not dry_run:
            if auto:
                try:
                    preview = _removed(_apply(conn, actions, days))
                finally:
                    conn.rollback()
                if preview and _no_recent_auto_backup():
                    backup = make_backup(conn, auto=True)
                    _prune_auto_backups(settings['auto_backups_keep'])
            else:
                backup = make_backup(conn)
        try:
            results = _apply(conn, actions, days)
        except Exception:
            conn.rollback()
            raise
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            if vacuum:
                conn.execute('VACUUM')
        summary = {'success': True, 'dry_run': dry_run, 'auto': auto, 'actions': results, 'backup': backup,
                   'removed_total': _removed(results), 'retention_days': days,
                   'seconds': round(time.time() - started, 1), 'at': _now()}
        if not dry_run:
            save_settings(conn, {'last_run': summary['at'], 'last_result': _short(summary)})
        return summary
    finally:
        _run_lock.release()


def _short(summary):
    return {'auto': summary['auto'], 'removed_total': summary['removed_total'], 'backup': summary['backup'],
            'counts': {k: v.get('count', 0) for k, v in summary['actions'].items()}}


def _no_recent_auto_backup():
    cutoff = (datetime.now() - timedelta(days=1)).isoformat(timespec='seconds')
    return not any(os.path.basename(b['name']).startswith('library-auto-') and b['modified'] >= cutoff for b in list_backups())


# ---------------------------------------------------------------------------
# schedule: shortly after startup, then daily (when enabled)
# ---------------------------------------------------------------------------
_scheduler = {'started': False}


def start_scheduler(get_conn, first_delay=120, interval=24 * 3600):
    if _scheduler['started']:
        return
    _scheduler['started'] = True

    def loop():
        time.sleep(first_delay)
        while True:
            try:
                conn = get_conn()
                if get_settings(conn).get('daily', True):
                    r = run(conn, dry_run=False, auto=True)
                    print(f"[Maintenance] automatic run: {_short(r) if r.get('success') else r.get('error')}")
            except Exception as e:
                print(f"[Maintenance] automatic run failed: {e}")
            time.sleep(interval)

    threading.Thread(target=loop, name='db-maintenance', daemon=True).start()
