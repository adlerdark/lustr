# This file is: ./gender_check.py
"""
Gender check (Settings -> Maintenance): performers whose gender, or Male Cast mark, doesn't fit the
orientation of their videos.

A video's orientation is its orientation tags, else its movie's, else its library's type. Only videos
of one clear orientation say anything about a performer:
    male evidence:    gay videos, and straight videos where they're marked Male Cast
    female evidence:  lesbian videos, and straight videos where they're in Cast
Bisexual, trans, mixed and unknown videos fit anyone, so they're counted but prove nothing. That is what
lets a bi video be judged: someone 'female' in one is flagged when their other videos are mostly gay.

Trans performers are never flagged: both their gender and their videos are outside these rules.

No FastAPI imports. Reports read only; ignored findings are kept in metadata ('gender_check_ignored').
"""
import json
import os

IGNORED_KEY = 'gender_check_ignored'
LEAN_SHARE = 0.75               # this share of someone's evidence decides which way they lean
STRONG = 3                      # this much evidence is a strong finding, less is worth a look
MAX_VIDEOS = 50                 # videos listed per finding (the fixes cover all of them)

JUDGED = ('straight', 'gay', 'lesbian')
BUCKETS = ('straight_cast', 'straight_male', 'gay', 'lesbian', 'bisexual', 'trans', 'other')


def lean(b):
    """Which way someone's videos lean -> 'male', 'female' or None (no evidence, or too mixed)."""
    men = len(b.get('gay', ())) + len(b.get('straight_male', ()))
    women = len(b.get('lesbian', ())) + len(b.get('straight_cast', ()))
    total = men + women
    if not total:
        return None
    if men / total >= LEAN_SHARE:
        return 'male'
    return 'female' if women / total >= LEAN_SHARE else None


def classify(gender, b):
    """One performer -> [(kind, expected gender or None, buckets holding the videos that don't fit)].
    gender   their gender looks wrong
    unset    no gender yet (which lustr treats as female), but their videos say male
    unmarked male, but in Cast on straight videos
    marked   not male, but marked Male Cast on straight videos
    odd      fits overall, but these videos don't: the video may be what's wrong"""
    side = lean(b)
    if gender == 'male':
        if b.get('lesbian') and side == 'female':
            return [('gender', 'female', [k for k in ('lesbian', 'straight_cast') if b.get(k)])]
        out = [('unmarked', side, ['straight_cast'])] if b.get('straight_cast') else []
        return out + ([('odd', side, ['lesbian'])] if b.get('lesbian') else [])
    if gender == 'female':
        if side == 'male':
            return [('gender', 'male', [k for k in ('gay', 'straight_male') if b.get(k)])]
        out = [('marked', side, ['straight_male'])] if b.get('straight_male') else []
        return out + ([('odd', side, ['gay'])] if b.get('gay') else [])
    if gender or side != 'male':                  # trans; or no gender counts as female, which fits
        return []
    return [('unset', 'male', [k for k in ('gay', 'straight_male') if b.get(k)])]


def video_orientations(conn, model):
    """video_id -> (orientation types, where they came from: 'tag' / 'movie' / 'library' / None)."""
    import display_filter as df
    def types(values):
        return {df.TAG_TYPE[v.lower()] for v in values if v.lower() in df.TAG_TYPE}
    tags, movie_tags, out = {}, {}, {}
    for vid, value in conn.execute('SELECT j.video_id, o.value FROM video_orientation_junction j '
                                   'JOIN video_orientation o ON o.id = j.orientation_id'):
        tags.setdefault(vid, []).append(value)
    if df._has_movie_tags(conn):
        for vid, value in conn.execute('SELECT vm.video_id, t.value FROM video_movie vm JOIN movie_tags t ON t.movie_id = vm.movie_id'):
            movie_tags.setdefault(vid, []).append(value)
    lib_types = df.library_types(model) if model is not None else {}
    for vid, library in conn.execute('SELECT video_id, library FROM videos'):
        own, movie = types(tags.get(vid, ())), types(movie_tags.get(vid, ()))
        lib = lib_types.get(library)
        if own:
            out[vid] = (own, 'tag')
        elif movie:
            out[vid] = (movie, 'movie')
        elif lib in df.TYPES:
            out[vid] = ({lib}, 'library')
        else:
            out[vid] = (set(), None)
    return out


def _bucket(types, male):
    if len(types) == 1:
        t = next(iter(types))
        if t == 'straight':
            return 'straight_male' if male else 'straight_cast'
        if t in BUCKETS:
            return t
    return 'trans' if 'trans' in types else ('bisexual' if 'bisexual' in types else 'other')


def performers(conn, model):
    """Primary name -> {bucket: [video entry]}, active files only."""
    orient = video_orientations(conn, model)
    paths = {}
    for vid, path in conn.execute('SELECT video_id, path FROM file_versions WHERE deleted_at IS NULL ORDER BY path'):
        paths.setdefault(vid, path)
    primary = dict(conn.execute('SELECT alias_name, primary_name FROM cast_aliases').fetchall())
    people = {}
    for vid, value, role in conn.execute(
            'SELECT j.video_id, vc.value, r.cast_id FROM video_cast_junction j JOIN video_cast vc ON vc.id = j.cast_id '
            'LEFT JOIN video_cast_role r ON r.video_id = j.video_id AND r.cast_id = j.cast_id'):
        if vid not in paths:
            continue
        types, source = orient.get(vid, (set(), None))
        bucket = _bucket(types, role is not None)
        people.setdefault(primary.get(value, value), {}).setdefault(bucket, []).append(
            {'video_id': vid, 'path': paths[vid], 'value': value, 'male_cast': role is not None,
             'orientation': ', '.join(sorted(types)) or 'unknown', 'source': source})
    return people


def _genders(conn):
    return {name: (g or '').lower() for name, g in conn.execute('SELECT cast_name, gender FROM cast_gender')}


def ignored(conn):
    row = conn.execute('SELECT value FROM metadata WHERE key = ?', (IGNORED_KEY,)).fetchone()
    try:
        return set(json.loads(row[0])) if row else set()
    except ValueError:
        return set()


def set_ignored(conn, name, kind, ignore=True):
    keys = ignored(conn)
    (keys.add if ignore else keys.discard)(f'{name}|{kind}')
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (IGNORED_KEY, json.dumps(sorted(keys), ensure_ascii=False)))
    conn.commit()


def _finding(name, gender, kind, expected, keys, b):
    videos = [v for k in keys for v in b.get(k, ())]
    return {'key': f'{name}|{kind}', 'name': name, 'gender': gender, 'kind': kind, 'expected': expected,
            'strong': len(videos) >= STRONG, 'count': len(videos),
            'counts': {k: len(b[k]) for k in BUCKETS if b.get(k)},
            'videos': [{'path': v['path'], 'filename': os.path.basename(v['path']), 'orientation': v['orientation'],
                        'source': v['source'], 'male_cast': v['male_cast']} for v in videos[:MAX_VIDEOS]]}


def report(conn, model):
    """-> {findings (biggest first within each kind), ignored, counts per kind}."""
    genders, skip = _genders(conn), ignored(conn)
    found, hidden = [], []
    for name, b in performers(conn, model).items():
        gender = genders.get(name, '')
        for kind, expected, keys in classify(gender, b):
            (hidden if f'{name}|{kind}' in skip else found).append(_finding(name, gender, kind, expected, keys, b))
    order = {k: i for i, k in enumerate(('gender', 'unset', 'unmarked', 'marked', 'odd'))}
    sort = lambda f: (order[f['kind']], -f['count'], f['name'].lower())
    counts = {}
    for f in found:
        counts[f['kind']] = counts.get(f['kind'], 0) + 1
    return {'findings': sorted(found, key=sort), 'ignored': sorted(hidden, key=sort), 'counts': counts}


def set_marks(conn, b, mark):
    """Mark or un-mark someone (b: their entry in performers()) as Male Cast on the straight videos that
    don't fit -> videos changed. Goes by the name each video credits them under, so aliases are covered."""
    import ext_db
    by_value = {}
    for v in b.get('straight_cast' if mark else 'straight_male', ()):
        by_value.setdefault(v['value'], []).append(v['video_id'])
    changed = 0
    for value, vids in by_value.items():
        r = ext_db.male_cast_change(conn, vids, [value] if mark else [], [] if mark else [value])
        changed += r['added'] + r['removed']
    return changed
