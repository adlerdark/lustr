# This file is: ./automation.py
"""
Automation: routine library scans, and applying a performer's starred
attributes to a video when the performer is added to it.

Settings live in the metadata table under 'automation':
    cast_attributes  - apply starred attributes when cast is added (default on)
    scan             - {enabled, interval_hours, at_hour, libraries, last_run, last_result}
                       libraries: [] = every library; at_hour is used for daily (24 h) scans

No FastAPI imports (endpoints are in tools_api.py).
"""
import json
import threading
import time
from datetime import datetime, timedelta

SETTINGS_KEY = 'automation'
DEFAULTS = {
    'cast_attributes': True,
    'scan': {'enabled': False, 'interval_hours': 24, 'at_hour': 4, 'libraries': [],
             'since': None, 'last_run': None, 'last_result': None},
}
INTERVALS = (1, 3, 6, 12, 24)
CAST_ATTRIBUTE_FIELDS = ('ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face', 'cock')


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def get_settings(conn):
    s = json.loads(json.dumps(DEFAULTS))
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (SETTINGS_KEY,)).fetchone()
        saved = json.loads(row[0]) if row else {}
    except Exception:
        saved = {}
    if 'cast_attributes' in saved:
        s['cast_attributes'] = bool(saved['cast_attributes'])
    s['scan'].update(saved.get('scan') or {})
    s['scan']['next_run'] = next_scan_time(s['scan'])
    return s


def save_settings(conn, changes):
    s = get_settings(conn)
    s['scan'].pop('next_run', None)
    if 'cast_attributes' in changes:
        s['cast_attributes'] = bool(changes['cast_attributes'])
    scan = changes.get('scan') or {}
    if 'enabled' in scan:
        if scan['enabled'] and not s['scan']['enabled']:
            s['scan']['since'] = datetime.now().isoformat(timespec='seconds')    # schedule starts now
        s['scan']['enabled'] = bool(scan['enabled'])
    if 'interval_hours' in scan and int(scan['interval_hours']) in INTERVALS:
        s['scan']['interval_hours'] = int(scan['interval_hours'])
    if 'at_hour' in scan:
        s['scan']['at_hour'] = max(0, min(23, int(scan['at_hour'])))
    if 'libraries' in scan:
        s['scan']['libraries'] = [str(x) for x in (scan['libraries'] or [])]
    for k in ('last_run', 'last_result'):
        if k in scan:
            s['scan'][k] = scan[k]
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SETTINGS_KEY, json.dumps(s)))
    conn.commit()
    return get_settings(conn)


def _when(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def next_scan_time(scan, now=None):
    """When the next routine scan is due (ISO string), or None when off.
    Daily: at at_hour; a run missed while the server was down happens as soon as it's back.
    Every N hours: N hours after the last scan (or after scheduling was turned on)."""
    if not scan.get('enabled'):
        return None
    now = now or datetime.now()
    hours = int(scan.get('interval_hours') or 24)
    last = _when(scan.get('last_run'))
    since = _when(scan.get('since')) or now
    if hours >= 24:
        due = now.replace(hour=int(scan.get('at_hour') or 0), minute=0, second=0, microsecond=0)
        if due > now:
            prev = due - timedelta(days=1)
            if (last or since) < prev:            # yesterday's slot was missed
                due = prev
        elif last and last >= due or (not last and since >= due):
            due += timedelta(days=1)
        return due.isoformat(timespec='minutes')
    return ((last or since) + timedelta(hours=hours)).isoformat(timespec='minutes')


# ---------------------------------------------------------------------------
# starred cast attributes -> one video
# ---------------------------------------------------------------------------
def _primary(conn, name):
    try:
        row = conn.execute('SELECT primary_name FROM cast_aliases WHERE alias_name = ? LIMIT 1', (name,)).fetchone()
        return row[0] if row else name
    except Exception:
        return name


def apply_cast_attributes_to_video(conn, video_id, names):
    """Add the starred (primary) attributes of these performers to one video.
    Add-only. Returns the number of values added. Does not commit."""
    added = 0
    for name in names or []:
        prim = _primary(conn, name)
        rows = conn.execute('SELECT attribute_type, attribute_value FROM cast_attributes '
                            'WHERE cast_name = ? AND is_primary = 1', (prim,)).fetchall()
        for field, value in rows:
            if field not in CAST_ATTRIBUTE_FIELDS or not value:
                continue
            table, junction = f'video_{field}', f'video_{field}_junction'
            try:
                row = conn.execute(f'SELECT id FROM {table} WHERE value = ?', (value,)).fetchone()
                vid_ = row[0] if row else conn.execute(f'INSERT INTO {table} (value) VALUES (?)', (value,)).lastrowid
                if not conn.execute(f'SELECT 1 FROM {junction} WHERE video_id = ? AND {field}_id = ?', (video_id, vid_)).fetchone():
                    conn.execute(f'INSERT INTO {junction} (video_id, {field}_id) VALUES (?, ?)', (video_id, vid_))
                    added += 1
            except Exception as e:
                print(f"[Automation] {field}={value} not applied to {video_id}: {e}")
    return added


def on_cast_added(conn, video_id, names):
    """Called wherever performers are added to a video. Respects the setting; commits."""
    if not names:
        return 0
    try:
        if not get_settings(conn)['cast_attributes']:
            return 0
        n = apply_cast_attributes_to_video(conn, video_id, names)
        conn.commit()
        return n
    except Exception as e:
        print(f"[Automation] cast attributes not applied: {e}")
        return 0


# ---------------------------------------------------------------------------
# routine scans
# ---------------------------------------------------------------------------
_scheduler = {'started': False, 'running': False}


def run_scan(model, libraries=None):
    """Scan like the Rescan button (incl. cross-library matching and maintenance).
    libraries: [] / None = everything."""
    from routes.scanning import scan_library, ScanRequest
    totals = {'new': 0, 'removed': 0}
    targets = [l for l in (libraries or []) if l in model.libraries] or [None]
    for lib in targets:
        r = scan_library(ScanRequest(fast=False, **({'library': lib} if lib else {})), model)
        totals['new'] += r.get('new', 0)
        totals['removed'] += r.get('removed', 0)
    return totals


def start_scheduler(model, check_every=60):
    if _scheduler['started']:
        return
    _scheduler['started'] = True

    def loop():
        time.sleep(check_every)
        while True:
            try:
                conn = model.db.get_connection()
                s = get_settings(conn)['scan']
                due = s.get('next_run')
                if s.get('enabled') and due and datetime.now() >= datetime.fromisoformat(due) and not _scheduler['running']:
                    _scheduler['running'] = True
                    started = datetime.now()
                    try:
                        result = run_scan(model, s.get('libraries'))
                        result['seconds'] = round((datetime.now() - started).total_seconds())
                        print(f"[Automation] routine scan: {result}")
                    except Exception as e:
                        result = {'error': str(e)}
                        print(f"[Automation] routine scan failed: {e}")
                    finally:
                        _scheduler['running'] = False
                    save_settings(conn, {'scan': {'last_run': started.isoformat(timespec='seconds'), 'last_result': result}})
            except Exception as e:
                print(f"[Automation] scheduler error: {e}")
            time.sleep(check_every)

    threading.Thread(target=loop, name='routine-scans', daemon=True).start()


def scan_running():
    return _scheduler['running']
