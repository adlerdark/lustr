# This file is: ./fields.py
"""
The metadata fields and how Edit Metadata lays them out (Settings -> Metadata -> Fields & editor layout).

A field has a key that never changes (its tables are video_<key> / video_<key>_junction, and saved searches,
quick tags and tag maps refer to it) and a label you can rename. Removing a field hides it: its values stay
and come back on Restore. Only delete_permanently erases them, after a backup.

The layout is kept in the database (metadata key 'field_layout'), so a restored backup always has the
fields that match its tables. config.LIST_FIELDS is updated in place with the active fields, so code
that imported it sees the current ones.

Layout:
  {'tabs': [{'name': 'General', 'fields': ['orientation', 'site', 'cast', 'male_cast', 'tags', ...]}, ...],
   'fields': {key: {'label', 'words', 'performer', 'hidden', 'custom'}}}
  words:     single-word values (lowercase with periods), listed in the sidebar filters
  performer: offered as a cast attribute

No FastAPI imports.
"""
import json
import re
import threading

import config

LAYOUT_KEY = 'field_layout'
GENERAL = 'General'

# today's fields, in the order the app has always used for them
BUILTIN_ORDER = ['site', 'tags', 'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face', 'cock',
                 'outfit', 'theme', 'feature', 'cumshot', 'participants', 'orientation']
DEFAULT_TABS = [(GENERAL, ['orientation', 'site', 'cast', 'male_cast', 'tags']),
                ('Appearance', ['ethnicity', 'nationality', 'hair', 'face', 'body', 'tits', 'ass', 'cock', 'outfit']),
                ('Attributes', ['participants', 'theme', 'feature', 'cumshot'])]
FIXED = ('orientation', 'site', 'cast', 'male_cast', 'tags')            # stay on General, can't be hidden
PSEUDO = ('male_cast',)                                                 # shown in the editor, stored with cast
PERFORMER = ('ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face', 'cock')
FREE_TEXT = ('cast', 'male_cast')                                       # values keep their spaces
LABELS = {'male_cast': 'Male Cast'}

# keys a new field can't take: other things lustr stores or searches by
RESERVED = {'title', 'year', 'rating', 'movie', 'movie_scene', 'movie_part', 'movie_whole', 'path', 'filename',
            'favorite', 'library', 'res', 'stars', 'scene', 'captions', 'chapters', 'markers', 'added', 'modified',
            'date_added', 'date_created', 'date_modified', 'date_last_edited', 'resolution', 'codec', 'extension',
            'length', 'total_bitrate', 'file_size', 'video_id', 'id', 'value', 'all', 'type', 'cache_id', 'part',
            'parts', 'versions', 'plays', 'fav', 'cc', 'filename_base'}
KEY_RE = re.compile(r'^[a-z][a-z0-9_]{1,30}$')


class FieldError(Exception):
    pass


_lock = threading.Lock()
_cache = {}                     # db path -> normalised layout
_db = None                      # the Database whose layout the no-conn helpers use


def bind(db):
    """Database.__init__: the database the helpers without a conn read."""
    global _db
    _db = db


def _conn(conn):
    if conn is not None:
        return conn
    if _db is None:
        return None
    return _db.get_connection()


def _db_key(conn):
    try:
        return conn.execute('PRAGMA database_list').fetchone()[2] or id(conn)
    except Exception:
        return id(conn)


def default_label(key):
    return LABELS.get(key) or key.replace('_', ' ').capitalize()


def default_layout():
    return {'tabs': [{'name': n, 'fields': list(f)} for n, f in DEFAULT_TABS],
            'fields': {k: {'label': default_label(k), 'words': k not in FREE_TEXT, 'performer': k in PERFORMER,
                           'hidden': False, 'custom': False}
                       for k in BUILTIN_ORDER + list(PSEUDO)}}


def _default_tab(key):
    return next((n for n, f in DEFAULT_TABS if key in f), None)


def normalise(raw):
    """Any saved layout -> a complete, consistent one (every built-in present, fixed fields first on General,
    each field in exactly one tab, General first)."""
    base = default_layout()
    raw = raw if isinstance(raw, dict) else {}
    fields = {}
    for k, d in base['fields'].items():
        got = (raw.get('fields') or {}).get(k) or {}
        fields[k] = dict(d, **{x: got[x] for x in ('label', 'words', 'performer', 'hidden') if x in got})
    for k, got in (raw.get('fields') or {}).items():
        if k not in fields and isinstance(got, dict) and got.get('custom') and KEY_RE.match(str(k)):
            fields[k] = {'label': str(got.get('label') or default_label(k)), 'words': bool(got.get('words', True)),
                         'performer': bool(got.get('performer')), 'hidden': bool(got.get('hidden')), 'custom': True}
    for k in FIXED:
        fields[k]['hidden'] = False
    for f in fields.values():
        f['label'] = (str(f['label']).strip() or 'Field')[:40]
        f['words'] = bool(f['words'])
        f['performer'] = bool(f['performer'])
        f['hidden'] = bool(f['hidden'])

    tabs, seen = [], set()
    for t in raw.get('tabs') or base['tabs']:
        name = str((t or {}).get('name') or '').strip()[:30]
        if not name or name.lower() in {x['name'].lower() for x in tabs}:
            continue
        keys = [k for k in (t.get('fields') or []) if k in fields and k not in seen and k not in FIXED]
        seen.update(keys)
        tabs.append({'name': name, 'fields': keys})
    general = next((t for t in tabs if t['name'] == GENERAL), None)
    if general is None:
        general = {'name': GENERAL, 'fields': []}
    tabs = [general] + [t for t in tabs if t is not general]
    general['fields'] = list(FIXED) + general['fields']
    seen.update(FIXED)
    for k in list(fields):                                  # anything not placed: back to its usual tab
        if k in seen:
            continue
        want = _default_tab(k)
        tab = next((t for t in tabs if t['name'] == want), None) or (tabs[-1] if len(tabs) > 1 else general)
        tab['fields'].append(k)
        seen.add(k)
    return {'tabs': tabs, 'fields': fields}


def layout(conn=None):
    c = _conn(conn)
    if c is None:
        return normalise(None)
    key = _db_key(c)
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    raw = None
    try:
        row = c.execute('SELECT value FROM metadata WHERE key = ?', (LAYOUT_KEY,)).fetchone()
        raw = json.loads(row[0]) if row else None
    except Exception:
        raw = None
    lay = normalise(raw)
    with _lock:
        _cache[key] = lay
    _publish(lay)
    return lay


def _publish(lay):
    """config.LIST_FIELDS (imported by name across the app) = the active list fields, updated in place."""
    active = list_fields(layout_=lay)
    config.LIST_FIELDS[:] = [k.capitalize() if k in BUILTIN_ORDER else k for k in active]


def forget(conn=None):
    c = _conn(conn)
    with _lock:
        if c is None:
            _cache.clear()
        else:
            _cache.pop(_db_key(c), None)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def _ordered(lay):
    """Stored fields: built-ins in their usual order, then your fields in the order the editor shows them."""
    customs = [k for t in lay['tabs'] for k in t['fields'] if lay['fields'][k].get('custom')]
    return [k for k in BUILTIN_ORDER if k in lay['fields']] + customs


def list_fields(conn=None, layout_=None):
    """The active list fields' keys (the ones with tables; not Male Cast, which is stored with Cast)."""
    lay = layout_ or layout(conn)
    return [k for k in _ordered(lay) if not lay['fields'][k]['hidden']]


def all_fields(conn=None, layout_=None):
    """Every list field with tables, hidden ones too."""
    return _ordered(layout_ or layout(conn))


def hidden_fields(conn=None):
    lay = layout(conn)
    return [k for k in _ordered(lay) if lay['fields'][k]['hidden']]


def is_active(key, conn=None):
    return key in list_fields(conn)


def label(key, conn=None):
    f = layout(conn)['fields'].get(key)
    return f['label'] if f else default_label(key)


def words(key, conn=None):
    """Single-word values (lowercase with periods)? Unknown names (title, movie...) say no."""
    f = layout(conn)['fields'].get(key)
    return bool(f and f['words'])


def sidebar_fields(conn=None):
    """The fields the sidebar's Tags tab counts: single-word ones, except Site (its own tab) and Cast."""
    lay = layout(conn)
    return [k for k in list_fields(layout_=lay) if lay['fields'][k]['words'] and k not in ('site', 'cast')]


def performer_fields(conn=None):
    lay = layout(conn)
    order = [k for t in lay['tabs'] for k in t['fields']]
    return [k for k in order if k not in PSEUDO and lay['fields'][k]['performer'] and not lay['fields'][k]['hidden']]


def resolve(name, conn=None):
    """A field name as typed in a search or a spreadsheet: its key or its label ('Hair colour' -> 'hair')."""
    n = str(name or '').strip().lower()
    lay = layout(conn)
    if n in lay['fields']:
        return n
    flat = re.sub(r'[\s_]+', '_', n)
    for k, f in lay['fields'].items():
        if re.sub(r'[\s_]+', '_', f['label'].lower()) == flat:
            return k
    return n


def public(conn=None):
    """What the page needs: tabs (hidden fields left out), every field's settings, hidden ones listed."""
    lay = layout(conn)
    return {'tabs': [{'name': t['name'], 'fields': [k for k in t['fields'] if not lay['fields'][k]['hidden']]}
                     for t in lay['tabs']],
            'all_tabs': lay['tabs'], 'fields': lay['fields'], 'list_fields': list_fields(layout_=lay),
            'hidden': [k for k in _ordered(lay) if lay['fields'][k]['hidden']],
            'sidebar': sidebar_fields(conn), 'performer': performer_fields(conn), 'fixed': list(FIXED),
            'builtin': BUILTIN_ORDER + list(PSEUDO)}


# ---------------------------------------------------------------------------
# changing
# ---------------------------------------------------------------------------
def _write(conn, lay):
    conn.execute('INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)', (LAYOUT_KEY, json.dumps(lay)))
    conn.commit()
    forget(conn)
    _publish(layout(conn))
    _changed()


def _changed():
    try:
        import home
        home.invalidate()
    except Exception:
        pass
    try:
        import routes.videos as rv
        rv.clear_filter_caches()
    except Exception:
        pass


def ensure_tables(conn, keys=None):
    for k in keys if keys is not None else all_fields(conn):
        if not KEY_RE.match(k):
            continue
        conn.execute(f'CREATE TABLE IF NOT EXISTS video_{k} (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT UNIQUE NOT NULL)')
        conn.execute(f'CREATE TABLE IF NOT EXISTS video_{k}_junction (video_id TEXT NOT NULL, {k}_id INTEGER NOT NULL, '
                     f'PRIMARY KEY (video_id, {k}_id), FOREIGN KEY (video_id) REFERENCES videos(video_id) ON DELETE CASCADE, '
                     f'FOREIGN KEY ({k}_id) REFERENCES video_{k}(id) ON DELETE CASCADE)')
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_video_{k}_junction_video ON video_{k}_junction(video_id)')


def save_layout(conn, new):
    """The editor's Save: tab names and order, which field is on which tab, labels and options.
    Fields can't be added or removed here (add_field / hide / delete_permanently)."""
    cur = layout(conn)
    if not isinstance(new, dict):
        raise FieldError('Nothing to save')
    names = [str((t or {}).get('name') or '').strip() for t in new.get('tabs') or []]
    if not names or any(not n for n in names):
        raise FieldError('Every tab needs a name')
    if len({n.lower() for n in names}) != len(names):
        raise FieldError('Two tabs have the same name')
    if names[0] != GENERAL:
        raise FieldError('General has to stay the first tab')
    placed = [k for t in new['tabs'] for k in (t.get('fields') or [])]
    unknown = [k for k in placed if k not in cur['fields']]
    if unknown:
        raise FieldError(f'Unknown field: {unknown[0]}')
    if len(placed) != len(set(placed)):
        raise FieldError('A field is on two tabs')
    general = new['tabs'][0].get('fields') or []
    off = [k for k in FIXED if k not in general]
    if off:
        raise FieldError(f'{label(off[0], conn)} has to stay on General')
    fields = {}
    for k, f in cur['fields'].items():
        got = (new.get('fields') or {}).get(k) or {}
        lab = str(got.get('label', f['label'])).strip()
        if not lab:
            raise FieldError('Every field needs a name')
        fields[k] = dict(f, label=lab[:40], words=bool(got.get('words', f['words'])),
                         performer=bool(got.get('performer', f['performer'])) and k not in PSEUDO)
        if k in FIXED and got.get('hidden'):
            raise FieldError(f'{lab} can\'t be hidden')
        if lab != f['label']:
            _check_label(cur, k, lab)
    labels = [f['label'].lower() for k, f in fields.items() if not f['hidden']]
    if len(labels) != len(set(labels)):
        raise FieldError('Two fields have the same name')
    lay = normalise({'tabs': [{'name': n, 'fields': t.get('fields') or []} for n, t in zip(names, new['tabs'])],
                     'fields': fields})
    _write(conn, lay)
    return lay


def _flat(name):
    return re.sub(r'[\s_]+', '_', str(name or '').strip().lower())


def _check_label(lay, key, lab):
    """A name can't stand for something else in a search (field:value): another field's key, or a keyword."""
    flat = _flat(lab)
    if flat in RESERVED and flat != key:
        raise FieldError(f'"{lab}" is a word lustr uses in searches - pick another name')
    if flat in lay['fields'] and flat != key:
        raise FieldError(f'"{lab}" is another field\'s name')


def key_for(conn, label_):
    """The key a new field would get from its name: 'Studio code' -> 'studio_code'."""
    k = re.sub(r'[^a-z0-9]+', '_', str(label_ or '').strip().lower()).strip('_')
    if k and not k[0].isalpha():
        k = 'f_' + k
    return k[:30]


def _taken(conn, k):
    if k in RESERVED or k in layout(conn)['fields']:
        return True
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name IN (?, ?)", (f'video_{k}', f'video_{k}_junction')).fetchone() is not None


def add_field(conn, label_, tab=None, words_=True, performer=False):
    lab = str(label_ or '').strip()
    if not lab:
        raise FieldError('Give the field a name')
    k = key_for(conn, lab)
    if not KEY_RE.match(k):
        raise FieldError('Use letters and numbers in the name')
    if _taken(conn, k):
        raise FieldError(f'"{lab}" is already taken (by a field, a hidden field, or something lustr uses)')
    cur = layout(conn)
    if lab.lower() in {f['label'].lower() for f in cur['fields'].values() if not f['hidden']}:
        raise FieldError(f'A field is already called "{lab}"')
    _check_label(cur, k, lab)
    ensure_tables(conn, [k])
    lay = json.loads(json.dumps(cur))
    lay['fields'][k] = {'label': lab[:40], 'words': bool(words_), 'performer': bool(performer), 'hidden': False, 'custom': True}
    target = next((t for t in lay['tabs'] if t['name'] == tab), None) or (lay['tabs'][-1] if len(lay['tabs']) > 1 else lay['tabs'][0])
    target['fields'].append(k)
    _write(conn, normalise(lay))
    return k


def _set_hidden(conn, key, hidden):
    cur = layout(conn)
    if key not in cur['fields'] or key in PSEUDO:
        raise FieldError('Unknown field')
    if hidden and key in FIXED:
        raise FieldError(f'{cur["fields"][key]["label"]} can\'t be hidden')
    lay = json.loads(json.dumps(cur))
    lay['fields'][key]['hidden'] = hidden
    if not hidden:                                          # back under a name nobody else uses now
        lab, others = lay['fields'][key]['label'], {f['label'].lower() for k, f in lay['fields'].items() if k != key and not f['hidden']}
        n = 2
        while lay['fields'][key]['label'].lower() in others:
            lay['fields'][key]['label'] = f'{lab} ({n})'
            n += 1
    _write(conn, normalise(lay))


def hide(conn, key):
    _set_hidden(conn, key, True)


def restore(conn, key):
    _set_hidden(conn, key, False)


def usage(conn, key):
    """How much would go if this field were deleted: {videos, values, albums, performers, quick_tags, tag_maps}."""
    out = {'videos': 0, 'values': 0, 'albums': 0, 'performers': 0, 'quick_tags': 0, 'tag_maps': 0}
    if not KEY_RE.match(key):
        return out
    q = [('videos', f'SELECT COUNT(DISTINCT video_id) FROM video_{key}_junction', ()),
         ('values', f'SELECT COUNT(*) FROM video_{key}', ()),
         ('albums', 'SELECT COUNT(DISTINCT folder) FROM image_album_meta WHERE field = ?', (key,)),
         ('performers', 'SELECT COUNT(DISTINCT cast_name) FROM cast_attributes WHERE LOWER(attribute_type) = ?', (key,)),
         ('tag_maps', 'SELECT COUNT(*) FROM ext_tag_map WHERE LOWER(field) = ?', (key,))]
    for name, sql, args in q:
        try:
            out[name] = conn.execute(sql, args).fetchone()[0]
        except Exception:
            pass
    out['quick_tags'] = len(_quick_tags().get(key) or [])
    return out


def _quick_tags_file():
    import os
    return os.path.join(config.DATA_DIR, 'quicktags.json')


def _quick_tags():
    try:
        with open(_quick_tags_file(), encoding='utf-8') as fh:
            d = json.load(fh)
        return {str(k).lower(): v for k, v in (d or {}).items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def delete_permanently(conn, key, confirm):
    """Erase a field you added and everything in it (after a backup). Built-in fields can only be hidden.
    confirm: the field's name, typed. -> the backup's name"""
    cur = layout(conn)
    f = cur['fields'].get(key)
    if not f:
        raise FieldError('Unknown field')
    if not f.get('custom'):
        raise FieldError(f'{f["label"]} comes with lustr: it can be hidden, not deleted')
    if str(confirm or '').strip().lower() != f['label'].lower():
        raise FieldError(f'Type the field\'s name ({f["label"]}) to confirm')
    import db_maintenance
    backup = db_maintenance.safety_backup(conn, f'delete-field-{key}', reuse_seconds=0)
    conn.execute(f'DROP TABLE IF EXISTS video_{key}_junction')
    conn.execute(f'DROP TABLE IF EXISTS video_{key}')
    for sql in ('DELETE FROM image_album_meta WHERE field = ?', 'DELETE FROM cast_attributes WHERE LOWER(attribute_type) = ?',
                'DELETE FROM ext_tag_map WHERE LOWER(field) = ?'):
        try:
            conn.execute(sql, (key,))
        except Exception:
            pass
    qt = _quick_tags()
    if key in qt:
        try:
            import os
            with open(_quick_tags_file(), encoding='utf-8') as fh:
                raw = json.load(fh)
            raw = {k: v for k, v in raw.items() if str(k).lower() != key}
            tmp = _quick_tags_file() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(raw, fh, indent=2)
            os.replace(tmp, _quick_tags_file())
        except Exception:
            pass
    lay = json.loads(json.dumps(cur))
    del lay['fields'][key]
    for t in lay['tabs']:
        t['fields'] = [k for k in t['fields'] if k != key]
    _write(conn, normalise(lay))
    return backup


def reset_default(conn):
    """Your fields go back to lustr's: tabs, order, names and options. Fields you added are hidden, not deleted."""
    cur = layout(conn)
    lay = default_layout()
    for k, f in cur['fields'].items():
        if f.get('custom'):
            lay['fields'][k] = dict(f, hidden=True)
            lay['tabs'][-1]['fields'].append(k)
    _write(conn, normalise(lay))
