# This file is: ./display_filter.py
"""
Library orientation types and the per-user display filter.

A group can have a type (straight / gay / bisexual / trans / lesbian / mixed, or none; metadata
'group_types'), and so can a library (model.library_settings[lib]['type'], or none = follow its
group). Group < library < video tag. Each user picks which of the five types they see
(metadata key 'display_filter' = {username: [types]}; default: all, i.e. no filtering).

A video's effective types are those of its orientation tags (heteroflexible counts as
bisexual); a video with no recognised orientation tag takes its library's type. It shows
when any effective type is selected, or when it is untagged in a mixed (or untyped) library.

No FastAPI imports. The current user comes from CURRENT_USER, set by the auth middleware.
"""
import contextvars
import json

TYPES = ('straight', 'gay', 'bisexual', 'trans', 'lesbian')
MIXED = 'mixed'
TAG_TYPE = {'straight': 'straight', 'gay': 'gay', 'bisexual': 'bisexual', 'heteroflexible': 'bisexual',
            'trans': 'trans', 'lesbian': 'lesbian'}
SETTINGS_KEY = 'display_filter'

CURRENT_USER = contextvars.ContextVar('display_filter_user', default=None)


# ---------------------------------------------------------------------------
# library and group types:  group < library < video tag
# ---------------------------------------------------------------------------
GROUPS_KEY = 'group_types'            # metadata: {group: type}; a group without one is "not set"
SEEDED_KEY = 'display_filter_seeded'  # metadata: the one-off move of seeded types to the groups is done


def _valid(t):
    return t in TYPES or t == MIXED


def _conn(model):
    return model.db.get_connection()


def _meta(conn, key, default):
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    except Exception:
        return default


def _set_meta(conn, key, value):
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (key, json.dumps(value)))
    conn.commit()


def group_types(model):
    """{group: type} for groups that have one (groups that no longer exist are left out)."""
    saved = _meta(_conn(model), GROUPS_KEY, {})
    return {g: t for g, t in saved.items() if g in (model.library_groups or {}) and _valid(t)}


def library_own_types(model):
    """{library: type} for libraries with their own type; the rest follow their group."""
    return {lib: t for lib in (model.libraries or {})
            if _valid(t := ((model.library_settings or {}).get(lib) or {}).get('type'))}


def _group_of(model, name):
    for g, data in (model.library_groups or {}).items():
        if name in (data.get('libraries') or []):
            return g
    return None


def _type_from_groups(model, library, gtypes):
    g, seen = _group_of(model, library), set()
    while g and g not in seen:                    # nearest group with a type wins
        if g in gtypes:
            return gtypes[g]
        seen.add(g)
        g = (model.library_groups.get(g) or {}).get('parent') or _group_of(model, g)
    return None


def library_type(model, library, gtypes=None):
    own = ((model.library_settings or {}).get(library) or {}).get('type')
    if _valid(own):
        return own
    return _type_from_groups(model, library, group_types(model) if gtypes is None else gtypes) or MIXED


def library_types(model):
    """Effective type of every library (own type, else its group's, else mixed)."""
    gtypes = group_types(model)
    return {lib: library_type(model, lib, gtypes) for lib in (model.libraries or {})}


def seed_library_types(model):
    """Once: top-level groups named Straight / Gay / Bisexual / Trans / Lesbian get that type, and
    library types that only repeat their group's become "follow group"."""
    conn = _conn(model)
    if _meta(conn, SEEDED_KEY, False):
        return []
    gtypes = dict(_meta(conn, GROUPS_KEY, {}))
    for name, g in (model.library_groups or {}).items():
        if not g.get('parent') and name.lower() in TYPES and name not in gtypes:
            gtypes[name] = name.lower()
    _set_meta(conn, GROUPS_KEY, gtypes)
    cleared = []
    for lib, own in library_own_types(model).items():
        if own == (_type_from_groups(model, lib, gtypes) or MIXED):
            model.library_settings[lib].pop('type', None)
            cleared.append(lib)
    if cleared:
        model.save()
    _set_meta(conn, SEEDED_KEY, True)
    print(f"[Display filter] group types {gtypes}; {len(cleared)} libraries now follow their group")
    return cleared


def set_library_type(model, library, type_):
    """type_ '' = follow the group."""
    if library not in (model.libraries or {}) or (type_ and not _valid(type_)):
        raise ValueError(f'unknown library or type: {library} / {type_}')
    if type_:
        model.set_library_setting(library, 'type', type_)
    else:
        (model.library_settings.get(library) or {}).pop('type', None)
        model.save()


def set_group_type(model, group, type_):
    """type_ '' = not set (its libraries decide, else mixed)."""
    if group not in (model.library_groups or {}) or (type_ and not _valid(type_)):
        raise ValueError(f'unknown group or type: {group} / {type_}')
    conn = _conn(model)
    gtypes = _meta(conn, GROUPS_KEY, {})
    if type_:
        gtypes[group] = type_
    else:
        gtypes.pop(group, None)
    _set_meta(conn, GROUPS_KEY, gtypes)


def rename_group_type(model, old, new):
    conn = _conn(model)
    gtypes = _meta(conn, GROUPS_KEY, {})
    if old in gtypes:
        gtypes[new] = gtypes.pop(old)
        _set_meta(conn, GROUPS_KEY, gtypes)


def types_info(model):
    return {'library_types': library_types(model), 'library_own_types': library_own_types(model),
            'group_types': group_types(model), 'display_types': list(TYPES)}


# ---------------------------------------------------------------------------
# per-user selection
# ---------------------------------------------------------------------------
def _all(conn):
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (SETTINGS_KEY,)).fetchone()
        return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def get_selection(conn, user=None):
    user = user if user is not None else CURRENT_USER.get()
    sel = _all(conn).get(user or '')
    return [t for t in TYPES if t in sel] if isinstance(sel, list) else list(TYPES)


def set_selection(conn, user, types):
    data = _all(conn)
    data[user or ''] = [t for t in TYPES if t in (types or [])]
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SETTINGS_KEY, json.dumps(data)))
    conn.commit()
    return data[user or '']


def cache_key(conn):
    """Part of any cache key for filtered results."""
    return ','.join(get_selection(conn))


# ---------------------------------------------------------------------------
# filtering
# ---------------------------------------------------------------------------
def visible_libraries(model, selection):
    return [lib for lib, t in library_types(model).items() if t == MIXED or t in selection]


def _selected(conn, selection):
    return get_selection(conn) if selection is None else [t for t in TYPES if t in selection]


def shown_sql(conn, model, col='v.video_id', lib_col='v.library', selection=None):
    """The rule itself as SQL on `col` / `lib_col` (correlated subqueries), or ('', []) when nothing is filtered."""
    sel = _selected(conn, selection)
    if len(sel) == len(TYPES):
        return '', []
    shown_tags = [v for v, t in TAG_TYPE.items() if t in sel]
    hidden_libs = [lib for lib, t in library_types(model).items() if t != MIXED and t not in sel]
    tag_q = (f'SELECT 1 FROM video_orientation_junction dfj JOIN video_orientation dfo ON dfo.id = dfj.orientation_id '
             f'WHERE dfj.video_id = {col} AND LOWER(dfo.value) IN ')
    parts, params = [], []
    if shown_tags:
        parts.append(f'EXISTS ({tag_q}({",".join("?" * len(shown_tags))}))')
        params += shown_tags
    untagged = f'NOT EXISTS ({tag_q}({",".join("?" * len(TAG_TYPE))}))'
    params += list(TAG_TYPE)
    if hidden_libs:
        untagged += f' AND COALESCE({lib_col}, \'\') NOT IN ({",".join("?" * len(hidden_libs))})'
        params += hidden_libs
    parts.append(f'({untagged})')
    return '(' + ' OR '.join(parts) + ')', params


def condition(conn, model, col='v.video_id', lib_col=None, selection=None):
    """(sql, params) to AND into a query, or ('', []) when nothing is filtered.
    The hidden videos are worked out once into a temp table on this connection, so big
    queries do an indexed lookup instead of running the rule for every row."""
    sql, params = shown_sql(conn, model, selection=selection)
    if not sql:
        return '', []
    started = not conn.in_transaction
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS df_hidden (video_id TEXT PRIMARY KEY)')
    conn.execute('DELETE FROM temp.df_hidden')
    conn.execute(f'INSERT INTO temp.df_hidden SELECT v.video_id FROM videos v WHERE NOT {sql}', params)
    if started:
        conn.commit()            # don't leave a transaction (and its read lock) open on a shared connection
    return f'{col} NOT IN (SELECT video_id FROM temp.df_hidden)', []


def hidden_ids(conn, model, selection=None):
    """video_ids hidden for the current user (empty set when nothing is filtered)."""
    sql, params = shown_sql(conn, model, selection=selection)
    if not sql:
        return set()
    return {r[0] for r in conn.execute(f'SELECT v.video_id FROM videos v WHERE NOT {sql}', params)}


def hidden_paths(conn, model, selection=None):
    sql, params = shown_sql(conn, model, selection=selection)
    if not sql:
        return set()
    return {r[0] for r in conn.execute(f'SELECT fv.path FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
                                       f'WHERE NOT {sql}', params)}
