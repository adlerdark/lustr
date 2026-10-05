# This file is: ./ext_db.py
"""
External performer database integration (Phase 1: storage + backend).

Talks to "stash-box" GraphQL servers - StashDB, and ThePornDB's stash-box
endpoint - to look up performer data and store it in separate ext_* fields.

Design rules:
  * Nothing runs automatically. Every network call is triggered by the user
    through one of the endpoints below.
  * Nothing here writes to existing cast tables (attributes, aliases, gender,
    category, photos). External data lives only in the cast_ext_link table.
    The one exception is /set-photo, which only runs when the user asks for it.
  * External IDs are stored as (ext_source, ext_id), e.g. ('stashdb', '<uuid>'),
    so they can never be confused with any local ID or cache_id.

API keys come from environment variables (see compose.yml / .env):
    STASHDB_API_KEY   (required for StashDB)
    TPDB_API_KEY      (optional, ThePornDB stash-box endpoint)
    EXTDB_DEFAULT_SOURCE  (optional, 'stashdb' or 'tpdb'; default 'stashdb')
"""

import io
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from collections import Counter
from datetime import date, datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/extdb", tags=["extdb"])

# ---------------------------------------------------------------------------
# Model wiring (same pattern as cast_attributes.py)
# ---------------------------------------------------------------------------
_model = None
_schema_ready = False


def get_model():
    return _model


def set_model(model):
    global _model
    _model = model
    try:
        ensure_schema()
    except Exception as e:  # never block app startup
        print(f"⚠ ext_db: could not prepare schema at startup: {e}")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
SOURCES = {
    'stashdb': {
        'label': 'StashDB',
        'endpoint': os.environ.get('STASHDB_ENDPOINT', 'https://stashdb.org/graphql'),
        'key_env': 'STASHDB_API_KEY',
        'page_url': 'https://stashdb.org/performers/{id}',
    },
    'tpdb': {
        'label': 'ThePornDB',
        'endpoint': os.environ.get('TPDB_ENDPOINT', 'https://theporndb.net/graphql'),
        'key_env': 'TPDB_API_KEY',
        'page_url': 'https://theporndb.net/performers/{id}',
    },
}
USER_AGENT = 'lustr/1.0 (self-hosted video library)'
TIMEOUT = (10, 30)                 # (connect, read) seconds
THUMB_BOX = (150, 225)             # stored thumbnail max size (2:3 portrait)
THUMB_QUALITY = 70                 # WebP quality -> typically 4-10 KB
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PHOTO_BYTES = 5 * 1024 * 1024  # same limit as the existing photo-from-URL route


def default_source():
    s = os.environ.get('EXTDB_DEFAULT_SOURCE', 'stashdb').strip().lower()
    return s if s in SOURCES else 'stashdb'


def api_key(source):
    return os.environ.get(SOURCES[source]['key_env'], '').strip()


class ExtDBError(Exception):
    pass


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
EXT_COLUMNS = [
    # (column, SQL type)  - all external values use the ext_ prefix
    ('ext_name', 'TEXT'),
    ('ext_disambiguation', 'TEXT'),
    ('ext_aliases', 'TEXT'),          # JSON list
    ('ext_gender', 'TEXT'),
    ('ext_birthdate', 'TEXT'),        # 'YYYY-MM-DD', 'YYYY-MM' or 'YYYY'
    ('ext_deathdate', 'TEXT'),
    ('ext_career_start', 'INTEGER'),
    ('ext_career_end', 'INTEGER'),
    ('ext_ethnicity', 'TEXT'),
    ('ext_nationality', 'TEXT'),      # stash-box "country"
    ('ext_height_cm', 'INTEGER'),
    ('ext_weight_kg', 'INTEGER'),     # not provided by stash-box; reserved
    ('ext_cup_size', 'TEXT'),
    ('ext_band_size', 'INTEGER'),
    ('ext_waist_size', 'INTEGER'),
    ('ext_hip_size', 'INTEGER'),
    ('ext_breast_type', 'TEXT'),
    ('ext_eye_color', 'TEXT'),
    ('ext_hair_color', 'TEXT'),
    ('ext_tattoos', 'TEXT'),          # JSON list of {location, description}
    ('ext_piercings', 'TEXT'),        # JSON list of {location, description}
    ('ext_urls', 'TEXT'),             # JSON list of URLs
    ('ext_scene_count', 'INTEGER'),
    ('ext_image_url', 'TEXT'),
    ('ext_page_url', 'TEXT'),
    ('ext_thumb', 'BLOB'),
    ('ext_thumb_mime', 'TEXT'),
    ('ext_raw_json', 'TEXT'),         # full record as received, for "view all"
]
JSON_COLUMNS = {'ext_aliases', 'ext_tattoos', 'ext_piercings', 'ext_urls'}
# Fields compared when reporting what a refresh changed
COMPARE_COLUMNS = [c for c, _ in EXT_COLUMNS
                   if c not in ('ext_thumb', 'ext_thumb_mime', 'ext_raw_json')]


def _conn():
    if _model is None or not getattr(_model, 'db', None):
        raise ExtDBError('Database not available (SQLite mode required)')
    return _model.db.get_connection()


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    conn = _conn()
    cols = ',\n        '.join(f'{c} {t}' for c, t in EXT_COLUMNS)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS cast_ext_link (
        cast_name TEXT PRIMARY KEY,
        ext_source TEXT NOT NULL,
        ext_id TEXT NOT NULL,
        {cols},
        linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        refreshed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    # Add any columns introduced by later versions of this module
    have = {r[1] for r in conn.execute('PRAGMA table_info(cast_ext_link)').fetchall()}
    for c, t in EXT_COLUMNS:
        if c not in have:
            conn.execute(f'ALTER TABLE cast_ext_link ADD COLUMN {c} {t}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cast_ext_link_ext '
                 'ON cast_ext_link(ext_source, ext_id)')
    # Bulk linker: latest outcome per performer + source (lets re-runs skip them)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cast_ext_bulk_log (
        cast_name TEXT NOT NULL,
        ext_source TEXT NOT NULL,
        run_id TEXT,
        outcome TEXT NOT NULL,
        detail TEXT,
        ext_id TEXT,
        checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (cast_name, ext_source)
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cast_ext_bulk_run ON cast_ext_bulk_log(run_id)')
    # Sites -> external studios. One row per site *group*: variants like
    # 'example.studio' / 'examplestudio' / 'Examplestudio' share the same site_key.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS site_ext_link (
        site_key TEXT PRIMARY KEY,
        status TEXT NOT NULL,              -- 'linked' | 'not_on_db'
        ext_source TEXT,
        ext_id TEXT,
        ext_name TEXT,
        ext_parent_id TEXT,
        ext_parent_name TEXT,
        link_kind TEXT,                    -- 'studio' | 'network'
        abbreviations TEXT,                -- JSON list of your own filename abbreviations
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS site_ext_studio (
        site_key TEXT NOT NULL,
        ext_source TEXT NOT NULL,
        ext_id TEXT NOT NULL,
        ext_name TEXT,
        ext_parent_id TEXT,
        ext_parent_name TEXT,
        link_kind TEXT,                    -- 'studio' | 'network'
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (site_key, ext_source, ext_id)
        )''')
    # one-time move of single links (first version) into site_ext_studio
    conn.execute("INSERT OR IGNORE INTO site_ext_studio (site_key, ext_source, ext_id, ext_name, ext_parent_id, "
                 "ext_parent_name, link_kind) SELECT site_key, ext_source, ext_id, ext_name, ext_parent_id, "
                 "ext_parent_name, link_kind FROM site_ext_link WHERE ext_id IS NOT NULL")
    conn.execute("UPDATE site_ext_link SET ext_source = NULL, ext_id = NULL, ext_name = NULL, ext_parent_id = NULL, "
                 "ext_parent_name = NULL, link_kind = NULL WHERE ext_id IS NOT NULL")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS site_ext_check (
        site_key TEXT NOT NULL,
        ext_source TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT,
        checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (site_key, ext_source)
        )''')
    # Videos -> external scenes
    conn.execute('''
        CREATE TABLE IF NOT EXISTS video_ext_link (
        video_id TEXT PRIMARY KEY,
        ext_source TEXT NOT NULL,
        ext_id TEXT NOT NULL,
        ext_title TEXT,
        ext_date TEXT,
        ext_code TEXT,
        ext_duration INTEGER,
        ext_director TEXT,
        ext_details TEXT,
        ext_studio_id TEXT,
        ext_studio_name TEXT,
        ext_studio_parent TEXT,
        ext_performers TEXT,             -- JSON [{id, name, as, gender}]
        ext_tags TEXT,                   -- JSON [{id, name}]
        ext_urls TEXT,                   -- JSON [url]
        ext_image_url TEXT,
        ext_thumb BLOB,
        ext_raw_json TEXT,
        match_score INTEGER,
        linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        refreshed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    have = {r[1] for r in conn.execute('PRAGMA table_info(video_ext_link)').fetchall()}
    for col, typ in (('ext_kind', 'TEXT'), ('ext_movie_title', 'TEXT'), ('ext_scene_number', 'INTEGER'),
                     ('ext_performer_scope', 'TEXT')):
        if col not in have:
            conn.execute(f'ALTER TABLE video_ext_link ADD COLUMN {col} {typ}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_video_ext_link_ext ON video_ext_link(ext_source, ext_id)')
    # Compact cache of linked performers' scene lists (see SCENE_CACHE_DAYS)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ext_scene_catalog (
        ext_source TEXT NOT NULL,
        performer_id TEXT NOT NULL,
        scenes_json TEXT NOT NULL,       -- compact scene records
        scene_count INTEGER,
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (ext_source, performer_id)
        )''')
    # Male Cast: which cast members of a video are "male cast in a straight scene".
    # They stay in the video's cast (so bios, counts, renames, aliases keep working);
    # this only changes how they're shown and sorted.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS video_cast_role (
        video_id TEXT NOT NULL,
        cast_id INTEGER NOT NULL,
        role TEXT NOT NULL DEFAULT 'male',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (video_id, cast_id)
        )''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS library_default_site (
        library TEXT PRIMARY KEY,
        site_value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    # External tag -> your own (field, value), learned from scene suggestions
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ext_tag_map (
        ext_source TEXT NOT NULL,
        ext_tag TEXT NOT NULL,             -- external tag name, lowercased
        field TEXT,                        -- NULL when ignored
        value TEXT,
        status TEXT NOT NULL,              -- 'auto' | 'confirmed' | 'ignored'
        uses INTEGER DEFAULT 0,            -- times applied through scene suggestions
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (ext_source, ext_tag)
        )''')
    # Pre-tick rule per mapping: NULL = the Settings default, 'always', 'never',
    # or 'types' = only on videos of these orientation types (tick_types: 'gay,bisexual,trans')
    have = {r[1] for r in conn.execute('PRAGMA table_info(ext_tag_map)').fetchall()}
    for col in ('tick', 'tick_types'):
        if col not in have:
            conn.execute(f'ALTER TABLE ext_tag_map ADD COLUMN {col} TEXT')
    # Bulk scene matching: latest outcome per video (lets re-runs skip them)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS video_ext_bulk_log (
        video_id TEXT PRIMARY KEY,
        outcome TEXT NOT NULL,             -- linked | review | not_found | already_linked | scene_taken | error
        detail TEXT,
        best_json TEXT,                    -- best candidate {id, source, kind, title, studio, date, score, reasons, signals}
        run_id TEXT,
        checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_video_ext_bulk_run ON video_ext_bulk_log(run_id)')
    # Which databases Find scene / bulk matching search, per library
    conn.execute('''
        CREATE TABLE IF NOT EXISTS library_ext_source (
        library TEXT PRIMARY KEY,
        sources TEXT NOT NULL,             -- both | stashdb | tpdb
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    conn.commit()
    _schema_ready = True


def resolve_primary(conn, name):
    """Alias -> primary name (same rule as the cast bio route)."""
    row = conn.execute('SELECT primary_name FROM cast_aliases WHERE alias_name = ? LIMIT 1',
                       (name,)).fetchone()
    return row[0] if row else name


def local_names(conn, primary):
    rows = conn.execute('SELECT alias_name FROM cast_aliases WHERE primary_name = ? '
                        'ORDER BY alias_name', (primary,)).fetchall()
    return [primary] + [r[0] for r in rows]


def local_gender(conn, primary):
    try:
        row = conn.execute('SELECT gender FROM cast_gender WHERE cast_name = ?',
                           (primary,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def get_link_row(conn, primary):
    cur = conn.execute('SELECT * FROM cast_ext_link WHERE cast_name = ?', (primary,))
    row = cur.fetchone()
    if not row:
        return None
    names = [d[0] for d in cur.description]
    return dict(zip(names, row))


def linked_elsewhere(conn, source, ext_id, exclude=None):
    rows = conn.execute('SELECT cast_name FROM cast_ext_link WHERE ext_source = ? AND ext_id = ?',
                        (source, ext_id)).fetchall()
    return [r[0] for r in rows if r[0] != exclude]


# ---------------------------------------------------------------------------
# GraphQL client
# ---------------------------------------------------------------------------
# Each entry: (field name, GraphQL selection). Fields a server rejects are
# dropped automatically and remembered, so older servers still work.
PERFORMER_FIELDS = [
    ('id', 'id'), ('name', 'name'), ('disambiguation', 'disambiguation'),
    ('aliases', 'aliases'), ('gender', 'gender'),
    ('birth_date', 'birth_date'), ('birthdate', 'birthdate { date accuracy }'),
    ('death_date', 'death_date'),
    ('ethnicity', 'ethnicity'), ('country', 'country'),
    ('eye_color', 'eye_color'), ('hair_color', 'hair_color'), ('height', 'height'),
    ('cup_size', 'cup_size'), ('band_size', 'band_size'),
    ('waist_size', 'waist_size'), ('hip_size', 'hip_size'),
    ('measurements', 'measurements { cup_size band_size waist hip }'),
    ('breast_type', 'breast_type'),
    ('career_start_year', 'career_start_year'), ('career_end_year', 'career_end_year'),
    ('tattoos', 'tattoos { location description }'),
    ('piercings', 'piercings { location description }'),
    ('urls', 'urls { url }'),
    ('images', 'images { id url width height }'),
    ('scene_count', 'scene_count'),
    ('deleted', 'deleted'), ('merged_into_id', 'merged_into_id'),
]
_REQUIRED = {'id', 'name'}
_dropped = {s: set() for s in SOURCES}          # fields a server rejected
_use_old_search = {s: False for s in SOURCES}   # server lacks searchPerformers
_BAD_FIELD = re.compile(r'Cannot query field "(\w+)" on type "(\w+)"')


def _fragment(source):
    sel = ' '.join(s for f, s in PERFORMER_FIELDS if f not in _dropped[source])
    return f'fragment P on Performer {{ {sel} }}'


# Request pacing. A bulk run sets an interval so StashDB is never hammered;
# while a bulk run is active, interactive requests share the same pace.
_pace_lock = threading.Lock()
_pace_interval = 0.0
_pace_last = 0.0


def _pace():
    global _pace_last
    with _pace_lock:
        if _pace_interval > 0:
            wait = _pace_last + _pace_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        _pace_last = time.monotonic()


def _post(source, query, variables):
    key = api_key(source)
    if not key:
        raise ExtDBError(f"No API key configured for {SOURCES[source]['label']} "
                         f"(set {SOURCES[source]['key_env']})")
    _pace()
    try:
        r = requests.post(SOURCES[source]['endpoint'],
                          json={'query': query, 'variables': variables},
                          headers={'ApiKey': key, 'User-Agent': USER_AGENT,
                                   'Content-Type': 'application/json'},
                          timeout=TIMEOUT)
    except requests.RequestException as e:
        raise ExtDBError(f"Could not reach {SOURCES[source]['label']}: {e}")
    if r.status_code in (401, 403):
        raise ExtDBError(f"{SOURCES[source]['label']} rejected the API key (HTTP {r.status_code})")
    if r.status_code == 429:
        raise ExtDBError(f"{SOURCES[source]['label']} rate limit reached - wait a minute and retry")
    try:
        data = r.json()
    except ValueError:
        raise ExtDBError(f"{SOURCES[source]['label']} returned HTTP {r.status_code} (not JSON)")
    return data


def gql(source, query_builder, variables):
    """Run a query. query_builder(fragment, use_old_search) -> query text.
    Retries after dropping fields/queries the server doesn't support."""
    for _ in range(4):
        data = _post(source, query_builder(_fragment(source), _use_old_search[source]), variables)
        errors = data.get('errors') or []
        if not errors:
            return data.get('data') or {}
        retry = False
        for err in errors:
            msg = str(err.get('message', ''))
            m = _BAD_FIELD.search(msg)
            if m and m.group(2) == 'Performer' and m.group(1) not in _REQUIRED:
                _dropped[source].add(m.group(1)); retry = True
            elif m and m.group(2) == 'Query' and m.group(1) == 'searchPerformers':
                _use_old_search[source] = True; retry = True
        if not retry:
            msgs = '; '.join(str(e.get('message', e)) for e in errors)[:400]
            if 'not authorized' in msgs.lower() or 'unauthorized' in msgs.lower():
                raise ExtDBError(f"{SOURCES[source]['label']} rejected the API key: {msgs}")
            raise ExtDBError(f"{SOURCES[source]['label']} error: {msgs}")
    raise ExtDBError('Server kept rejecting the query (schema mismatch)')


def search_performers(source, term, limit=10):
    def build(frag, old):
        if old:
            return (frag + ' query($t: String!, $l: Int) '
                    '{ searchPerformer(term: $t, limit: $l) { ...P } }')
        return (frag + ' query($t: String!, $l: Int) '
                '{ searchPerformers(term: $t, limit: $l) { count performers { ...P } } }')
    data = gql(source, build, {'t': term, 'l': limit})
    if 'searchPerformers' in data:
        return (data['searchPerformers'] or {}).get('performers') or []
    return data.get('searchPerformer') or []


def find_performer(source, ext_id, follow_merges=3):
    """Fetch one performer; follows merge redirects. Returns (record, merged_from)."""
    merged_from = None
    current = ext_id
    for _ in range(follow_merges + 1):
        data = gql(source, lambda frag, old: frag + ' query($id: ID!) { findPerformer(id: $id) { ...P } }',
                   {'id': current})
        p = data.get('findPerformer')
        if not p:
            raise ExtDBError(f'Performer {current} not found on {SOURCES[source]["label"]}')
        nxt = p.get('merged_into_id')
        if nxt and nxt != current:
            merged_from = merged_from or current
            current = nxt
            continue
        return p, merged_from
    raise ExtDBError('Too many merge redirects')


# ---------------------------------------------------------------------------
# Normalising external records
# ---------------------------------------------------------------------------
def pick_image(images):
    imgs = [i for i in (images or []) if i and i.get('url')]
    if not imgs:
        return None
    portrait = [i for i in imgs if (i.get('height') or 0) >= (i.get('width') or 0)]
    return (portrait or imgs)[0]['url']


def _int(v):
    try:
        return int(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def normalize(source, p):
    meas = p.get('measurements') or {}
    birth = p.get('birth_date') or ((p.get('birthdate') or {}).get('date'))
    bt = p.get('breast_type')
    page = SOURCES[source]['page_url']
    return {
        'ext_name': p.get('name'),
        'ext_disambiguation': p.get('disambiguation') or None,
        'ext_aliases': sorted(set(a for a in (p.get('aliases') or []) if a)),
        'ext_gender': p.get('gender'),
        'ext_birthdate': birth or None,
        'ext_deathdate': p.get('death_date') or None,
        'ext_career_start': _int(p.get('career_start_year')),
        'ext_career_end': _int(p.get('career_end_year')),
        'ext_ethnicity': p.get('ethnicity'),
        'ext_nationality': p.get('country') or None,
        'ext_height_cm': _int(p.get('height')),
        'ext_weight_kg': None,
        'ext_cup_size': p.get('cup_size') or meas.get('cup_size') or None,
        'ext_band_size': _int(p.get('band_size') or meas.get('band_size')),
        'ext_waist_size': _int(p.get('waist_size') or meas.get('waist')),
        'ext_hip_size': _int(p.get('hip_size') or meas.get('hip')),
        'ext_breast_type': None if bt in (None, '', 'NA') else bt,
        'ext_eye_color': p.get('eye_color'),
        'ext_hair_color': p.get('hair_color'),
        'ext_tattoos': p.get('tattoos') or [],
        'ext_piercings': p.get('piercings') or [],
        'ext_urls': [u.get('url') for u in (p.get('urls') or []) if u and u.get('url')],
        'ext_scene_count': _int(p.get('scene_count')),
        'ext_image_url': pick_image(p.get('images')),
        'ext_page_url': page.format(id=p['id']) if page else None,
    }


def download_image(url, max_bytes=MAX_IMAGE_BYTES):
    """Download an image. Returns (bytes, content_type) or None."""
    if not url:
        return None
    try:
        _pace()
        r = requests.get(url, timeout=TIMEOUT, headers={'User-Agent': USER_AGENT}, stream=True)
        r.raise_for_status()
        ctype = (r.headers.get('content-type') or '').split(';')[0].strip().lower()
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                print(f"⚠ ext_db: image larger than {max_bytes // (1024 * 1024)} MB, skipped: {url}")
                return None
        return bytes(buf), ctype
    except Exception as e:
        print(f"⚠ ext_db: image download failed for {url}: {e}")
        return None


def image_mime(raw, ctype=''):
    """Content type for image bytes (trusts the server header, else sniffs)."""
    if ctype and ctype.startswith('image/'):
        return ctype
    try:
        from PIL import Image
        fmt = Image.open(io.BytesIO(raw)).format
        return f'image/{fmt.lower()}' if fmt else None
    except Exception:
        return None


def thumb_from_bytes(raw):
    """Small WebP thumbnail (bytes) from image bytes, or None."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGBA')
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        img.thumbnail(THUMB_BOX, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format='WEBP', quality=THUMB_QUALITY, method=6)
        return out.getvalue()
    except Exception as e:
        print(f"⚠ ext_db: thumbnail failed: {e}")
        return None


def make_thumbnail(url):
    """Download an image and return a small WebP (bytes) or None."""
    img = download_image(url)
    return thumb_from_bytes(img[0]) if img else None


def save_ext_photo(primary, source, url, image=None):
    """Store the external image as the local cast photo (source_type = source,
    e.g. 'stashdb', which marks it as an external photo)."""
    image = image or download_image(url, MAX_PHOTO_BYTES)
    if not image:
        raise ExtDBError('Could not download the external image (or it is larger than 5 MB)')
    raw, ctype = image
    if len(raw) > MAX_PHOTO_BYTES:
        raise ExtDBError('External image is larger than 5 MB')
    mime = image_mime(raw, ctype)
    if not mime:
        raise ExtDBError('External URL did not return an image')
    if not _model.db.save_cast_photo(primary, raw, mime, source, url):
        raise ExtDBError('Saving the photo failed')


def has_local_photo(conn, primary):
    """Same rule the cast browser uses: a cast_photos row with a thumbnail."""
    try:
        return conn.execute('SELECT 1 FROM cast_photos WHERE cast_name = ? AND thumbnail_data IS NOT NULL',
                            (primary,)).fetchone() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Display helpers (computed on read, never stored)
# ---------------------------------------------------------------------------
def _pretty(enum_val):
    if not enum_val:
        return None
    return ' '.join(w.capitalize() for w in str(enum_val).split('_'))


def _parse_date(s):
    """-> (date or None, year or None, is_full_date)"""
    if not s:
        return None, None, False
    m = re.match(r'^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?', str(s))
    if not m:
        return None, None, False
    y = int(m.group(1))
    if m.group(2) and m.group(3):
        try:
            return date(y, int(m.group(2)), int(m.group(3))), y, True
        except ValueError:
            pass
    return None, y, False


def _age_on(birth_full, birth_year, on):
    if birth_full:
        return on.year - birth_full.year - ((on.month, on.day) < (birth_full.month, birth_full.day))
    if birth_year:
        return on.year - birth_year   # approximate (year only)
    return None


# ISO 3166-1 alpha-2 -> country name (stash-box 'country' is usually a code)
COUNTRY_NAMES = {
    'AD': 'Andorra', 'AE': 'United Arab Emirates', 'AF': 'Afghanistan',
    'AG': 'Antigua and Barbuda', 'AI': 'Anguilla', 'AL': 'Albania', 'AM': 'Armenia',
    'AO': 'Angola', 'AQ': 'Antarctica', 'AR': 'Argentina', 'AS': 'American Samoa',
    'AT': 'Austria', 'AU': 'Australia', 'AW': 'Aruba', 'AX': 'Åland Islands',
    'AZ': 'Azerbaijan', 'BA': 'Bosnia and Herzegovina', 'BB': 'Barbados', 'BD': 'Bangladesh',
    'BE': 'Belgium', 'BF': 'Burkina Faso', 'BG': 'Bulgaria', 'BH': 'Bahrain', 'BI': 'Burundi',
    'BJ': 'Benin', 'BL': 'Saint Barthélemy', 'BM': 'Bermuda', 'BN': 'Brunei Darussalam',
    'BO': 'Bolivia', 'BQ': 'Bonaire, Sint Eustatius and Saba', 'BR': 'Brazil', 'BS': 'Bahamas',
    'BT': 'Bhutan', 'BV': 'Bouvet Island', 'BW': 'Botswana', 'BY': 'Belarus', 'BZ': 'Belize',
    'CA': 'Canada', 'CC': 'Cocos (Keeling) Islands',
    'CD': 'Congo, The Democratic Republic of the', 'CF': 'Central African Republic',
    'CG': 'Congo', 'CH': 'Switzerland', 'CI': "Côte d'Ivoire", 'CK': 'Cook Islands',
    'CL': 'Chile', 'CM': 'Cameroon', 'CN': 'China', 'CO': 'Colombia', 'CR': 'Costa Rica',
    'CU': 'Cuba', 'CV': 'Cabo Verde', 'CW': 'Curaçao', 'CX': 'Christmas Island',
    'CY': 'Cyprus', 'CZ': 'Czech Republic', 'DE': 'Germany', 'DJ': 'Djibouti', 'DK': 'Denmark',
    'DM': 'Dominica', 'DO': 'Dominican Republic', 'DZ': 'Algeria', 'EC': 'Ecuador',
    'EE': 'Estonia', 'EG': 'Egypt', 'EH': 'Western Sahara', 'ER': 'Eritrea', 'ES': 'Spain',
    'ET': 'Ethiopia', 'FI': 'Finland', 'FJ': 'Fiji', 'FK': 'Falkland Islands (Malvinas)',
    'FM': 'Micronesia, Federated States of', 'FO': 'Faroe Islands', 'FR': 'France',
    'GA': 'Gabon', 'GB': 'United Kingdom', 'GD': 'Grenada', 'GE': 'Georgia',
    'GF': 'French Guiana', 'GG': 'Guernsey', 'GH': 'Ghana', 'GI': 'Gibraltar',
    'GL': 'Greenland', 'GM': 'Gambia', 'GN': 'Guinea', 'GP': 'Guadeloupe',
    'GQ': 'Equatorial Guinea', 'GR': 'Greece',
    'GS': 'South Georgia and the South Sandwich Islands', 'GT': 'Guatemala', 'GU': 'Guam',
    'GW': 'Guinea-Bissau', 'GY': 'Guyana', 'HK': 'Hong Kong',
    'HM': 'Heard Island and McDonald Islands', 'HN': 'Honduras', 'HR': 'Croatia',
    'HT': 'Haiti', 'HU': 'Hungary', 'ID': 'Indonesia', 'IE': 'Ireland', 'IL': 'Israel',
    'IM': 'Isle of Man', 'IN': 'India', 'IO': 'British Indian Ocean Territory', 'IQ': 'Iraq',
    'IR': 'Iran', 'IS': 'Iceland', 'IT': 'Italy', 'JE': 'Jersey', 'JM': 'Jamaica',
    'JO': 'Jordan', 'JP': 'Japan', 'KE': 'Kenya', 'KG': 'Kyrgyzstan', 'KH': 'Cambodia',
    'KI': 'Kiribati', 'KM': 'Comoros', 'KN': 'Saint Kitts and Nevis', 'KP': 'North Korea',
    'KR': 'South Korea', 'KW': 'Kuwait', 'KY': 'Cayman Islands', 'KZ': 'Kazakhstan',
    'LA': 'Laos', 'LB': 'Lebanon', 'LC': 'Saint Lucia', 'LI': 'Liechtenstein',
    'LK': 'Sri Lanka', 'LR': 'Liberia', 'LS': 'Lesotho', 'LT': 'Lithuania', 'LU': 'Luxembourg',
    'LV': 'Latvia', 'LY': 'Libya', 'MA': 'Morocco', 'MC': 'Monaco', 'MD': 'Moldova',
    'ME': 'Montenegro', 'MF': 'Saint Martin (French part)', 'MG': 'Madagascar',
    'MH': 'Marshall Islands', 'MK': 'North Macedonia', 'ML': 'Mali', 'MM': 'Myanmar',
    'MN': 'Mongolia', 'MO': 'Macao', 'MP': 'Northern Mariana Islands', 'MQ': 'Martinique',
    'MR': 'Mauritania', 'MS': 'Montserrat', 'MT': 'Malta', 'MU': 'Mauritius', 'MV': 'Maldives',
    'MW': 'Malawi', 'MX': 'Mexico', 'MY': 'Malaysia', 'MZ': 'Mozambique', 'NA': 'Namibia',
    'NC': 'New Caledonia', 'NE': 'Niger', 'NF': 'Norfolk Island', 'NG': 'Nigeria',
    'NI': 'Nicaragua', 'NL': 'Netherlands', 'NO': 'Norway', 'NP': 'Nepal', 'NR': 'Nauru',
    'NU': 'Niue', 'NZ': 'New Zealand', 'OM': 'Oman', 'PA': 'Panama', 'PE': 'Peru',
    'PF': 'French Polynesia', 'PG': 'Papua New Guinea', 'PH': 'Philippines', 'PK': 'Pakistan',
    'PL': 'Poland', 'PM': 'Saint Pierre and Miquelon', 'PN': 'Pitcairn', 'PR': 'Puerto Rico',
    'PS': 'Palestine, State of', 'PT': 'Portugal', 'PW': 'Palau', 'PY': 'Paraguay',
    'QA': 'Qatar', 'RE': 'Réunion', 'RO': 'Romania', 'RS': 'Serbia', 'RU': 'Russia',
    'RW': 'Rwanda', 'SA': 'Saudi Arabia', 'SB': 'Solomon Islands', 'SC': 'Seychelles',
    'SD': 'Sudan', 'SE': 'Sweden', 'SG': 'Singapore',
    'SH': 'Saint Helena, Ascension and Tristan da Cunha', 'SI': 'Slovenia',
    'SJ': 'Svalbard and Jan Mayen', 'SK': 'Slovakia', 'SL': 'Sierra Leone', 'SM': 'San Marino',
    'SN': 'Senegal', 'SO': 'Somalia', 'SR': 'Suriname', 'SS': 'South Sudan',
    'ST': 'Sao Tome and Principe', 'SV': 'El Salvador', 'SX': 'Sint Maarten (Dutch part)',
    'SY': 'Syria', 'SZ': 'Eswatini', 'TC': 'Turks and Caicos Islands', 'TD': 'Chad',
    'TF': 'French Southern Territories', 'TG': 'Togo', 'TH': 'Thailand', 'TJ': 'Tajikistan',
    'TK': 'Tokelau', 'TL': 'Timor-Leste', 'TM': 'Turkmenistan', 'TN': 'Tunisia', 'TO': 'Tonga',
    'TR': 'Türkiye', 'TT': 'Trinidad and Tobago', 'TV': 'Tuvalu', 'TW': 'Taiwan',
    'TZ': 'Tanzania', 'UA': 'Ukraine', 'UG': 'Uganda',
    'UM': 'United States Minor Outlying Islands', 'US': 'United States', 'UY': 'Uruguay',
    'UZ': 'Uzbekistan', 'VA': 'Holy See (Vatican City State)',
    'VC': 'Saint Vincent and the Grenadines', 'VE': 'Venezuela',
    'VG': 'Virgin Islands, British', 'VI': 'Virgin Islands, U.S.', 'VN': 'Vietnam',
    'VU': 'Vanuatu', 'WF': 'Wallis and Futuna', 'WS': 'Samoa', 'XK': 'Kosovo', 'YE': 'Yemen',
    'YT': 'Mayotte', 'ZA': 'South Africa', 'ZM': 'Zambia', 'ZW': 'Zimbabwe',
}


def country_name(code):
    if not code:
        return None
    c = str(code).strip()
    return COUNTRY_NAMES.get(c.upper(), c)


def build_display(row):
    today = date.today()
    b_full, b_year, b_exact = _parse_date(row.get('ext_birthdate'))
    d_full, d_year, _ = _parse_date(row.get('ext_deathdate'))
    ref = d_full or (date(d_year, 12, 31) if d_year else today)
    age = _age_on(b_full, b_year, ref)

    start, end = row.get('ext_career_start'), row.get('ext_career_end')
    years = None
    if start or end:
        age_start = (start - b_year) if (start and b_year) else None
        age_end = ((end - b_year) if (end and b_year) else None)
        years = {
            'start': start, 'end': end, 'ongoing': bool(start and not end),
            'age_at_start': age_start, 'age_at_end': age_end,
            'text': f"{start or '?'} – {end or 'present'}",
        }

    band, cup = row.get('ext_band_size'), row.get('ext_cup_size')
    waist, hip = row.get('ext_waist_size'), row.get('ext_hip_size')
    meas = None
    if band or cup or waist or hip:
        meas = f"{band or '?'}{cup or ''}-{waist or '?'}-{hip or '?'}"

    h = row.get('ext_height_cm')
    height = None
    if h:
        inches = round(h / 2.54)
        height = f"{h} cm ({inches // 12}'{inches % 12}\")"

    return {
        'aliases': row.get('ext_aliases') or [],
        'birthdate': row.get('ext_birthdate'),
        'age': age,
        'age_is_approx': bool(age is not None and not b_exact),
        'deceased': bool(row.get('ext_deathdate')),
        'years_active': years,
        'ethnicity': _pretty(row.get('ext_ethnicity')),
        'nationality': country_name(row.get('ext_nationality')),
        'nationality_code': row.get('ext_nationality'),
        'measurements': meas,
        'cup_size': cup,
        'breast_type': _pretty(row.get('ext_breast_type')),
        'height': height,
    }


def row_to_public(row):
    if not row:
        return None
    out = {}
    for k, v in row.items():
        if k in ('ext_thumb', 'ext_raw_json'):
            continue
        if k in JSON_COLUMNS:
            try:
                v = json.loads(v) if v else []
            except ValueError:
                v = []
        out[k] = v
    out['has_thumb'] = bool(row.get('ext_thumb'))
    out['source_label'] = SOURCES.get(row.get('ext_source'), {}).get('label', row.get('ext_source'))
    out['display'] = build_display(out)
    return out


# ---------------------------------------------------------------------------
# Matching (search results scoring)
# ---------------------------------------------------------------------------
def norm_name(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())


_GENDER_MAP = {'FEMALE': 'female', 'MALE': 'male',
               'TRANSGENDER_FEMALE': 'trans', 'TRANSGENDER_MALE': 'trans'}


def score_candidate(p, local, gender):
    """Score an external performer against the local cast member.

    local: list of (name, is_primary) - the primary name plus local aliases.
    Each local name is checked separately. A match through a multi-word name
    ("Riley Reid") is strong; a match through a single-word name ("Riley")
    is weak, because many performers share first names. Matching more than
    one local name adds a bonus, so "Riley" + alias "Riley Reid" both
    pointing at the same performer ranks that performer first.
    """
    ext_name = norm_name(p.get('name'))
    ext_aliases = {norm_name(a) for a in (p.get('aliases') or [])} - {''}
    hits = []                                   # (weight, reason)
    for name, is_primary in local:
        n = norm_name(name)
        if not n:
            continue
        single = len(name.split()) == 1
        tag = ' (single name)' if single else ''
        if n == ext_name:
            w = (100 if is_primary else 92) if not single else (65 if is_primary else 60)
            hits.append((w, ('exact name' if is_primary else f'name = local alias "{name}"') + tag))
        elif n in ext_aliases:
            w = (88 if is_primary else 80) if not single else (55 if is_primary else 50)
            hits.append((w, f'"{name}" is one of their aliases' + tag))

    if hits:
        hits.sort(key=lambda h: -h[0])
        score = hits[0][0] + 6 * (len(hits) - 1)
        reasons = [h[1] for h in hits]
        if all(r.endswith('(single name)') for r in reasons):
            reasons.append('verify')
    else:
        primary = local[0][0] if local else ''
        pn = norm_name(primary)
        if pn and ext_name and (pn in ext_name or ext_name in pn):
            score, reasons = (45 if len(primary.split()) == 1 else 60), ['partial name match']
        else:
            score, reasons = 40, []

    eg = _GENDER_MAP.get(p.get('gender') or '')
    if gender and eg:
        if eg == gender:
            score += 3
        else:
            score -= 25
            reasons.append(f'gender differs ({_pretty(p.get("gender"))})')
    return max(0, min(score, 100)), reasons


# ---------------------------------------------------------------------------
# Search by ID / link (StashDB ID, IAFD ID, AFDB ID, or any profile URL)
# ---------------------------------------------------------------------------
_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'


def url_variants(url):
    """The forms a profile URL may be stored in on StashDB. Its URL filter is an
    EXACT match (see stash-box performer/query.go), so each form is tried."""
    u = re.sub(r'^https?://', '', (url or '').strip(), flags=re.I)
    u = re.sub(r'^www\.', '', u, flags=re.I).split('#')[0]
    base = u.rstrip('/')
    out = []
    for scheme in ('https://', 'http://'):
        for www in ('www.', ''):
            for tail in ('', '/'):
                v = f'{scheme}{www}{base}{tail}'
                if base and v not in out:
                    out.append(v)
    return out


def parse_id_query(q):
    """Recognise ID/link searches. Returns a dict or None (= normal name search).

    Accepted forms:
        stashdb:<uuid>   https://stashdb.org/performers/<uuid>   <bare uuid>
        iafd:<uuid>      https://www.iafd.com/person.rme/id=<uuid>  (or older perfid=... links)
        an AFDB profile link  https://www.adultfilmdatabase.com/actor/<name>-<number>/
        any other profile link stored on the performer (FreeOnes, Babepedia...)
    An AFDB number on its own can't be looked up: StashDB stores the full link,
    which includes the performer's name.
    """
    q = (q or '').strip()
    if not q:
        return None
    sep = r'(?:\s*[:#]\s*|\s+)'
    m = re.match(r'^(stashdb|stash|sdb)' + sep + '(' + _UUID + ')$', q, re.I)
    if m:
        return {'kind': 'id', 'id': m.group(2), 'label': 'StashDB ID'}
    m = re.search(r'stashdb\.org/performers/(' + _UUID + ')', q, re.I)
    if m:
        return {'kind': 'id', 'id': m.group(1), 'label': 'StashDB link'}
    m = re.match(r'^iafd' + sep + r'(\S+)$', q, re.I)
    if m:
        if re.fullmatch(_UUID, m.group(1)):
            return {'kind': 'urls', 'urls': url_variants('iafd.com/person.rme/id=' + m.group(1).lower()),
                    'label': 'IAFD ID ' + m.group(1)}
        return {'kind': 'error', 'label': 'IAFD ID',
                'message': 'IAFD IDs look like 742ab03f-9531-40a7-85d4-77b3adb1658b. For older profiles, paste the full IAFD link.'}
    if re.match(r'^(afdb|adultfilmdatabase)' + sep + r'\d+$', q, re.I):
        return {'kind': 'error', 'label': 'AFDB ID',
                'message': "An AFDB number on its own can't be looked up - StashDB stores the full AFDB profile link, "
                           "which includes the performer's name. Paste the AFDB profile link instead."}
    if re.fullmatch(_UUID, q):
        return {'kind': 'uuid', 'id': q, 'label': 'ID'}
    if re.match(r'^(https?://)?(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+/\S*', q, re.I):
        site = 'IAFD link' if 'iafd.com' in q.lower() else ('AFDB link' if 'adultfilmdatabase.com' in q.lower() else 'link')
        return {'kind': 'urls', 'urls': url_variants(q), 'label': site}
    return None


def query_by_url(source, url, per_page=10):
    """Performers that have exactly this URL stored."""
    def build(frag, old):
        return (frag + ' query($u: String!, $n: Int!) '
                '{ queryPerformers(input: {url: $u, per_page: $n}) { count performers { ...P } } }')
    data = gql(source, build, {'u': url, 'n': per_page})
    return (data.get('queryPerformers') or {}).get('performers') or []


def id_search(source, parsed):
    """Returns (performers, label)."""
    if parsed['kind'] == 'error':
        raise ExtDBError(parsed['message'])
    if parsed['kind'] in ('id', 'uuid'):
        try:
            p, _ = find_performer(source, parsed['id'])
            return [p], (f"{parsed['label']} {parsed['id']}" if parsed['kind'] == 'id'
                         else f"{SOURCES[source]['label']} ID {parsed['id']}")
        except ExtDBError:
            if parsed['kind'] == 'id':
                return [], f"{parsed['label']} {parsed['id']}"
            parsed = {'kind': 'urls', 'urls': url_variants('iafd.com/person.rme/id=' + parsed['id'].lower()),
                      'label': 'IAFD ID ' + parsed['id']}
    for u in parsed['urls']:
        found = query_by_url(source, u)
        if found:
            return found, parsed['label']
    return [], parsed['label']


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class SearchBody(BaseModel):
    cast_name: str
    query: Optional[str] = None    # optional custom search text
    source: Optional[str] = None


class LinkBody(BaseModel):
    cast_name: str
    ext_id: str
    source: Optional[str] = None
    add_photo_if_missing: bool = False    # used by the bulk review (same rule as bulk links)


class NameBody(BaseModel):
    cast_name: str


def _source(s):
    s = (s or default_source()).lower()
    if s not in SOURCES:
        raise ExtDBError(f'Unknown source {s}')
    return s


def _err(e):
    return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get('/status')
def status():
    return {
        'default_source': default_source(),
        'sources': [{'key': k, 'label': v['label'], 'configured': bool(api_key(k))}
                    for k, v in SOURCES.items()],
    }


@router.get('/cast')
def get_cast_link(name: str = Query(...), model=Depends(get_model)):
    """Stored external data for a cast member (no network call)."""
    try:
        ensure_schema()
        conn = _conn()
        primary = resolve_primary(conn, name)
        row = get_link_row(conn, primary)
        return {'success': True, 'cast_name': primary, 'linked': bool(row),
                'link': row_to_public(row)}
    except Exception as e:
        return _err(e)


@router.get('/cast/thumb')
def get_cast_thumb(name: str = Query(...), model=Depends(get_model)):
    try:
        ensure_schema()
        conn = _conn()
        row = conn.execute('SELECT ext_thumb, ext_thumb_mime, refreshed_at FROM cast_ext_link '
                           'WHERE cast_name = ?', (resolve_primary(conn, name),)).fetchone()
        if not row or not row[0]:
            return Response(status_code=404)
        return Response(content=bytes(row[0]), media_type=row[1] or 'image/webp',
                        headers={'Cache-Control': 'private, max-age=300'})
    except Exception:
        return Response(status_code=500)


@router.get('/cast/raw')
def get_cast_raw(name: str = Query(...), model=Depends(get_model)):
    """Full external record as received (for 'view all data')."""
    try:
        ensure_schema()
        conn = _conn()
        row = conn.execute('SELECT ext_raw_json FROM cast_ext_link WHERE cast_name = ?',
                           (resolve_primary(conn, name),)).fetchone()
        return {'success': True, 'raw': json.loads(row[0]) if row and row[0] else None}
    except Exception as e:
        return _err(e)


@router.post('/search')
def search(body: SearchBody, model=Depends(get_model)):
    """User-initiated search. Searches the local name and each local alias
    (or a custom query), merges results, and ranks them."""
    try:
        ensure_schema()
        source = _source(body.source)
        conn = _conn()
        primary = resolve_primary(conn, body.cast_name)
        names = local_names(conn, primary)
        custom = (body.query or '').strip()
        parsed = parse_id_query(custom)

        merged, order = {}, []

        def add(p, term):
            if not p or not p.get('id') or p.get('deleted'):
                return
            if p['id'] not in merged:
                merged[p['id']] = {'p': p, 'terms': [term]}
                order.append(p['id'])
            elif term not in merged[p['id']]['terms']:
                merged[p['id']]['terms'].append(term)

        if parsed:
            found, label = id_search(source, parsed)
            terms = [label]
            for p in found:
                add(p, label)
        else:
            terms = [custom] if custom else names[:4]
            for term in terms:
                for p in search_performers(source, term, limit=10):
                    add(p, term)

        local = [(n, i == 0) for i, n in enumerate(names)]
        gender = local_gender(conn, primary)
        current = get_link_row(conn, primary)

        scored = []
        for pid in order:
            p = merged[pid]['p']
            if parsed:
                scored.append([100, [f'matched by {terms[0]}'], p])
            else:
                scored.append(list(score_candidate(p, local, gender)) + [p])
        if not parsed:
            apply_videography(conn, source, primary, scored)
        results = []
        for rank, (score, reasons, p) in enumerate(scored):
            results.append(_result_from_n(conn, source, primary, p['id'], normalize(source, p),
                                          score, reasons, merged[p['id']]['terms'], rank, current))
        results.sort(key=lambda r: (-r['score'], r['search_rank']))
        return {'success': True, 'cast_name': primary, 'source': source,
                'source_label': SOURCES[source]['label'], 'terms': terms,
                'id_search': bool(parsed), 'results': results}
    except Exception as e:
        return _err(e)


def _result_from_n(conn, source, primary, ext_id, n, score, reasons, terms, rank, current):
    """One search result card (shared by /search and the bulk review)."""
    return {
        'ext_id': ext_id,
        'score': score,
        'search_rank': rank,
        'reasons': reasons,
        'matched_terms': terms,
        'already_linked_to': linked_elsewhere(conn, source, ext_id, exclude=primary),
        'is_current_link': bool(current and current['ext_source'] == source and current['ext_id'] == ext_id),
        'name': n['ext_name'],
        'disambiguation': n['ext_disambiguation'],
        'aliases': (n['ext_aliases'] or [])[:12],
        'gender': _pretty(n['ext_gender']),
        'image_url': n['ext_image_url'],
        'page_url': n['ext_page_url'],
        'scene_count': n['ext_scene_count'],
        'display': build_display(n),
    }


def _store(conn, primary, source, p, keep_linked_at=True, image=None):
    n = normalize(source, p)
    thumb = thumb_from_bytes(image[0]) if image else make_thumbnail(n['ext_image_url'])
    old = get_link_row(conn, primary)
    values = dict(n)
    for c in JSON_COLUMNS:
        values[c] = json.dumps(values[c], ensure_ascii=False)
    same_performer = bool(old and old.get('ext_id') == p['id'])
    if thumb is None and same_performer:
        thumb = old.get('ext_thumb')          # download failed: keep previous thumbnail
    values['ext_thumb'] = thumb
    values['ext_thumb_mime'] = 'image/webp' if values['ext_thumb'] else None
    values['ext_raw_json'] = json.dumps(p, ensure_ascii=False)

    cols = ['cast_name', 'ext_source', 'ext_id'] + list(values.keys())
    params = [primary, source, p['id']] + list(values.values())
    linked_at = old['linked_at'] if (keep_linked_at and same_performer) else None
    if linked_at:
        cols.append('linked_at'); params.append(linked_at)
    ph = ','.join('?' * len(cols))
    conn.execute(f"INSERT OR REPLACE INTO cast_ext_link ({','.join(cols)}, refreshed_at) "
                 f"VALUES ({ph}, CURRENT_TIMESTAMP)", params)
    conn.commit()

    changed = []
    if same_performer:
        for c in COMPARE_COLUMNS:
            a, b = old.get(c), values.get(c)
            if str(a if a is not None else '') != str(b if b is not None else ''):
                changed.append(c)
    return changed


@router.post('/link')
def link(body: LinkBody, model=Depends(get_model)):
    """Link a cast member to an external performer (user confirmed)."""
    try:
        ensure_schema()
        source = _source(body.source)
        conn = _conn()
        primary = resolve_primary(conn, body.cast_name)
        p, merged_from = find_performer(source, body.ext_id)
        n = normalize(source, p)
        image = download_image(n['ext_image_url']) if n['ext_image_url'] else None
        _store(conn, primary, source, p, keep_linked_at=False, image=image)
        photo_added = False
        if body.add_photo_if_missing and image and not has_local_photo(conn, primary):
            try:
                save_ext_photo(primary, source, n['ext_image_url'], image)
                photo_added = True
            except ExtDBError as e:
                print(f"⚠ ext_db: photo not added for {primary}: {e}")
        _bulk_log_update(conn, primary, source, 'linked', 'Linked manually: ' + _label(p), p['id'])
        return {'success': True, 'cast_name': primary, 'merged_from': merged_from,
                'photo_added': photo_added,
                'also_linked_to': linked_elsewhere(conn, source, p['id'], exclude=primary),
                'link': row_to_public(get_link_row(conn, primary))}
    except Exception as e:
        return _err(e)


@router.post('/refresh')
def refresh(body: NameBody, model=Depends(get_model)):
    """Re-pull the same external performer to bring data up to date."""
    try:
        ensure_schema()
        conn = _conn()
        primary = resolve_primary(conn, body.cast_name)
        row = get_link_row(conn, primary)
        if not row:
            return _err('Not linked to an external database')
        p, merged_from = find_performer(row['ext_source'], row['ext_id'])
        changed = _store(conn, primary, row['ext_source'], p)
        return {'success': True, 'cast_name': primary, 'merged_from': merged_from,
                'changed_fields': changed, 'link': row_to_public(get_link_row(conn, primary))}
    except Exception as e:
        return _err(e)


@router.post('/clear')
def clear(body: NameBody, model=Depends(get_model)):
    """Remove the link and all ext_* data for this cast member. Nothing else changes."""
    try:
        ensure_schema()
        conn = _conn()
        primary = resolve_primary(conn, body.cast_name)
        old = get_link_row(conn, primary)
        cur = conn.execute('DELETE FROM cast_ext_link WHERE cast_name = ?', (primary,))
        conn.commit()
        if old:
            _bulk_log_update(conn, primary, old['ext_source'], 'cleared', 'Link cleared by user', None)
        return {'success': True, 'cast_name': primary, 'removed': cur.rowcount > 0}
    except Exception as e:
        return _err(e)


@router.post('/set-photo')
def set_photo(body: NameBody, model=Depends(get_model)):
    """User-requested: replace the local cast photo with the external image."""
    try:
        ensure_schema()
        conn = _conn()
        primary = resolve_primary(conn, body.cast_name)
        row = get_link_row(conn, primary)
        if not row or not row.get('ext_image_url'):
            return _err('No external image available')
        save_ext_photo(primary, row['ext_source'], row['ext_image_url'])
        return {'success': True, 'cast_name': primary}
    except Exception as e:
        return _err(e)


# ===========================================================================
# Bulk linker
# ===========================================================================
# Links performers automatically ONLY on a "100% match":
#   * a StashDB performer scores 100 (exact name match on a multi-word name -
#     single names like "Riley" can never reach 100 - and no gender conflict)
#   * no other candidate scores 90 or more (otherwise: ambiguous, skipped)
#   * that StashDB performer is not already linked to another local cast member
# It only creates the link. Attributes are never touched. If the performer has
# no photo yet, the external image is added as the photo (source = 'stashdb').
# Every performer checked is logged in cast_ext_bulk_log with the outcome.

BULK_OUTCOMES = {
    'linked': 'Linked',
    'ambiguous': 'More than one strong match',
    'no_exact': 'No 100% match',
    'not_found': 'Not found',
    'linked_elsewhere': 'Match already linked to another performer',
    'skipped': 'Skipped',
    'cleared': 'Link cleared',
    'error': 'Error',
}
BULK_GENDERS = ('female', 'male', 'trans')
BULK_CATEGORIES = ('pro', 'amateur', 'celeb')

_bulk_lock = threading.Lock()
_bulk = {'running': False, 'run_id': None}

# Candidates found for performers that were NOT linked, so the results review
# can show them without asking StashDB again. Kept in server memory only
# (never in the database): cleared when a new run starts or the app restarts,
# trimmed to the top candidates, and capped in size.
_bulk_cands = {}
BULK_CANDS_PER_PERFORMER = 8
BULK_CANDS_MAX_PERFORMERS = 5000


def _bulk_log_update(conn, primary, source, outcome, detail, ext_id):
    """If the performer has a bulk-log row, record a manual change on it."""
    try:
        conn.execute('UPDATE cast_ext_bulk_log SET outcome = ?, detail = ?, ext_id = ?, '
                     'checked_at = CURRENT_TIMESTAMP WHERE cast_name = ? AND ext_source = ?',
                     (outcome, detail, ext_id, primary, source))
        conn.commit()
    except Exception as e:
        print(f"⚠ ext_db: bulk log update failed for {primary}: {e}")
    _bulk_cands.pop(primary, None)


# ---------------------------------------------------------------------------
# Videography check: studios a candidate has worked for vs. the sites of your
# performer's videos (through your site links). Makes single-word names and
# weak candidates safe to judge.
# ---------------------------------------------------------------------------
VIDEOGRAPHY_TOP_N = 5            # candidates checked per search (one request each)
VIDEOGRAPHY_POINTS = 10          # per shared studio...
VIDEOGRAPHY_MAX = 30             # ...up to this many points
VIDEOGRAPHY_LINK_MIN = 2         # bulk: an exact name + this many shared studios links
_studios_cache = {}


def performer_studios(source, ext_id):
    """[{id, name, parent_id, scenes}] - the studios of an external performer (StashDB only)."""
    if source != 'stashdb' or not ext_id:
        return None
    key = (source, ext_id)
    if key not in _studios_cache:
        q = ('query($id: ID!) { findPerformer(id: $id) { id studios { scene_count '
             'studio { id name parent { id name } } } } }')
        data = _post(source, q, {'id': ext_id})
        if data.get('errors'):
            return None
        st = ((data.get('data') or {}).get('findPerformer') or {}).get('studios') or []
        if len(_studios_cache) > 5000:
            _studios_cache.clear()
        _studios_cache[key] = [{'id': (s.get('studio') or {}).get('id'), 'name': (s.get('studio') or {}).get('name'),
                                'parent_id': ((s.get('studio') or {}).get('parent') or {}).get('id'),
                                'scenes': s.get('scene_count') or 0} for s in st if s and s.get('studio')]
    return _studios_cache[key]


def local_studio_ids(conn, primary):
    """({studio_id: name}, {network_id: name}) linked to the sites of this performer's videos
    (under their name or any alias)."""
    names = local_names(conn, primary)
    ph = ','.join('?' * len(names))
    keys = {site_key(r[0]) for r in _rows(conn, f'SELECT DISTINCT s.value FROM video_cast vc '
                                                 f'JOIN video_cast_junction cj ON cj.cast_id = vc.id '
                                                 f'JOIN video_site_junction sj ON sj.video_id = cj.video_id '
                                                 f'JOIN video_site s ON s.id = sj.site_id WHERE vc.value IN ({ph})', names)}
    studios, networks = {}, {}
    for key, sid, name, kind in _rows(conn, "SELECT site_key, ext_id, ext_name, link_kind FROM site_ext_studio "
                                            "WHERE ext_source = 'stashdb'"):
        if key in keys:
            (networks if kind == 'network' else studios)[sid] = name
    return studios, networks


def videography(cand_studios, studios, networks):
    """Your linked studios/networks this candidate has scenes for -> {id: name}."""
    shared = {}
    for s in cand_studios or []:
        if s['id'] in studios:
            shared[s['id']] = studios[s['id']]
        elif s['id'] in networks:
            shared[s['id']] = networks[s['id']]
        elif s['parent_id'] and s['parent_id'] in networks:
            shared[s['parent_id']] = networks[s['parent_id']]
    return shared


def apply_videography(conn, source, primary, scored):
    """scored: [[score, reasons, p], ...] (modified in place). Checks the best candidates that
    aren't already certain. -> {ext_id: {id: name}} of shared studios."""
    if source != 'stashdb':
        return {}
    studios, networks = local_studio_ids(conn, primary)
    if not studios and not networks:
        return {}
    out = {}
    for row in sorted(scored, key=lambda r: -r[0])[:VIDEOGRAPHY_TOP_N]:
        if row[0] >= 100:
            continue
        try:
            shared = videography(performer_studios(source, row[2]['id']), studios, networks)
        except ExtDBError:
            continue
        out[row[2]['id']] = shared
        if shared:
            row[0] = min(100, row[0] + min(VIDEOGRAPHY_MAX, VIDEOGRAPHY_POINTS * len(shared)))
            row[1] = row[1] + ['studios in common: ' + ', '.join(sorted(shared.values())[:6])
                               + (f' (+{len(shared) - 6})' if len(shared) > 6 else '')]
    return out


def _exact_name(p, local):
    """The candidate's own name is your performer's name or one of their aliases."""
    ext = norm_name(p.get('name'))
    return bool(ext) and any(norm_name(n) == ext for n, _primary in local)


def _cache_candidates(primary, source, scored, terms_by_id):
    if len(_bulk_cands) >= BULK_CANDS_MAX_PERFORMERS and primary not in _bulk_cands:
        return
    items = []
    for sc, reasons, p in scored[:BULK_CANDS_PER_PERFORMER]:
        n = normalize(source, p)
        for k in ('ext_tattoos', 'ext_piercings', 'ext_urls'):
            n.pop(k, None)                  # not needed for a result card
        n['ext_aliases'] = n['ext_aliases'][:12]
        items.append({'ext_id': p['id'], 'n': n, 'score': sc, 'reasons': reasons,
                      'terms': terms_by_id.get(p['id'], [])})
    _bulk_cands[primary] = {'source': source, 'items': items}


def _label(p):
    return (p.get('name') or '?') + (f" ({p['disambiguation']})" if p.get('disambiguation') else '')


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []            # table missing -> treat as empty


def bulk_candidates(conn, source, genders, categories, skip_checked, names=None):
    """Unlinked performers matching the filters (or the explicit selection in
    `names`, in which case gender/category are ignored), most videos first.
    Mirrors how /api/cast/all builds the Browse Cast Members list."""
    rows = _rows(conn, 'SELECT vc.value AS name, COUNT(*) AS n FROM video_cast vc '
                       'JOIN video_cast_junction vcj ON vc.id = vcj.cast_id '
                       'GROUP BY vc.value ORDER BY n DESC, vc.value')
    aliases = {r[0] for r in _rows(conn, 'SELECT alias_name FROM cast_aliases')}
    gmap = {r[0]: r[1] for r in _rows(conn, 'SELECT cast_name, gender FROM cast_gender')}
    cmap = {r[0]: r[1] for r in _rows(conn, 'SELECT cast_name, category FROM cast_category')}
    linked = {r[0] for r in _rows(conn, 'SELECT cast_name FROM cast_ext_link')}
    checked = set()
    if skip_checked:
        checked = {r[0] for r in _rows(conn, "SELECT cast_name FROM cast_ext_bulk_log "
                                             "WHERE ext_source = ? AND outcome NOT IN ('error', 'skipped')",
                                       (source,))}
    selected = set(names) if names is not None else None
    stats = Counter()
    out = []
    for r in rows:
        name, n = r[0], r[1]
        if name in aliases:
            continue
        if selected is not None:
            if name not in selected:
                continue
            stats['selected'] += 1
        stats['total'] += 1
        if selected is None and (gmap.get(name, 'female') not in genders
                                 or cmap.get(name, 'pro') not in categories):
            stats['filtered_out'] += 1
        elif name in linked:
            stats['already_linked'] += 1
        elif name in checked:
            stats['checked_before'] += 1
        else:
            out.append((name, n))
    stats['eligible'] = len(out)
    return out, dict(stats)


def _bulk_one(conn, primary, source):
    """Check one performer. Returns (outcome, detail, ext_id, photo_added)."""
    if get_link_row(conn, primary):
        return 'skipped', 'Already linked', None, False
    names = local_names(conn, primary)
    local = [(n, i == 0) for i, n in enumerate(names)]
    gender = local_gender(conn, primary)
    found = {}
    terms_by_id = {}

    def run(term):
        for p in search_performers(source, term, limit=10):
            if p and p.get('id') and not p.get('deleted') and not p.get('merged_into_id'):
                found.setdefault(p['id'], p)
                terms_by_id.setdefault(p['id'], [])
                if term not in terms_by_id[p['id']]:
                    terms_by_id[p['id']].append(term)

    def ranked():
        sc = [(score_candidate(p, local, gender)[0], p) for p in found.values()]
        return sorted(sc, key=lambda x: -x[0])

    def remember():
        full = sorted(((*score_candidate(p, local, gender), p) for p in found.values()),
                      key=lambda x: -x[0])
        _cache_candidates(primary, source, full, terms_by_id)

    run(primary)
    scored = ranked()
    if (not scored or scored[0][0] < 100) and len(names) > 1:
        for term in names[1:4]:           # try local aliases too
            run(term)
        scored = ranked()

    if not scored:
        remember()                        # caches "nothing found" - review won't re-query
        return 'not_found', 'No results', None, False
    top_score, top = scored[0]
    if top_score < 100:
        # videography: an exact name (single words too) that shares 2+ of your studios, when no
        # other candidate shares any, is the same person
        full = [list(score_candidate(p, local, gender)) + [p] for p in found.values()]
        vg = apply_videography(conn, source, primary, full)
        sharing = [(len(sh), pid) for pid, sh in vg.items() if sh]
        if sharing:
            best_n, best_id = max(sharing)
            best = found[best_id]
            best_reasons = next(r[1] for r in full if r[2]['id'] == best_id)
            if (best_n >= VIDEOGRAPHY_LINK_MIN and len(sharing) == 1 and _exact_name(best, local)
                    and not any(r.startswith('gender differs') for r in best_reasons)
                    and not linked_elsewhere(conn, source, best_id, exclude=primary)):
                top, top_score = best, 100
                vg_detail = f' · linked by videography ({best_n} studios: ' + ', '.join(sorted(vg[best_id].values())[:4]) + ')'
                return _bulk_link(conn, primary, source, top, vg_detail)
        _cache_candidates(primary, source, sorted(((r[0], r[1], r[2]) for r in full), key=lambda x: -x[0]), terms_by_id)
        return 'no_exact', f'Best: {_label(top)} ({top_score})', top['id'], False
    strong = [p for sc, p in scored if sc >= 90]
    if len(strong) > 1:
        remember()
        return 'ambiguous', f'{len(strong)} strong matches: ' + ', '.join(_label(p) for p in strong[:4]), None, False
    elsewhere = linked_elsewhere(conn, source, top['id'], exclude=primary)
    if elsewhere:
        remember()
        return 'linked_elsewhere', f'{_label(top)} is linked to {", ".join(elsewhere)}', top['id'], False

    return _bulk_link(conn, primary, source, top)


def _bulk_link(conn, primary, source, top, detail=''):
    n = normalize(source, top)
    image = download_image(n['ext_image_url']) if n['ext_image_url'] else None
    _store(conn, primary, source, top, keep_linked_at=False, image=image)
    photo = False
    if image and not has_local_photo(conn, primary):
        try:
            save_ext_photo(primary, source, n['ext_image_url'], image)
            photo = True
        except ExtDBError as e:
            print(f"⚠ ext_db bulk: photo not added for {primary}: {e}")
    return 'linked', _label(top) + (' + photo' if photo else '') + detail, top['id'], photo


def _sleep_unless_stopped(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not _bulk.get('stop'):
        time.sleep(0.5)


def _bulk_worker(names, source, delay):
    global _pace_interval
    st = _bulk
    run_id = st['run_id']
    _pace_interval = delay
    errors_in_a_row = 0
    try:
        conn = _conn()                      # this thread's own connection
        for idx, (name, _count) in enumerate(names):
            if st['stop']:
                st['stopped'] = True
                break
            st['current'] = name
            outcome, detail, ext_id, photo = 'error', 'Rate limited', None, False
            for attempt in range(3):
                try:
                    outcome, detail, ext_id, photo = _bulk_one(conn, name, source)
                    break
                except ExtDBError as e:
                    msg = str(e)
                    if 'rate limit' in msg.lower() and attempt < 2:
                        st['note'] = 'Rate limited by the server - pausing for 60 seconds'
                        _sleep_unless_stopped(60)
                        st['note'] = ''
                        if st['stop']:
                            break
                        continue
                    if 'api key' in msg.lower():
                        raise                   # every request would fail
                    outcome, detail = 'error', msg
                    break
                except Exception as e:
                    outcome, detail = 'error', f'{type(e).__name__}: {e}'
                    break
            try:
                conn.execute('INSERT OR REPLACE INTO cast_ext_bulk_log '
                             '(cast_name, ext_source, run_id, outcome, detail, ext_id, checked_at) '
                             'VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                             (name, source, run_id, outcome, detail, ext_id))
                conn.commit()
            except Exception as e:
                print(f"⚠ ext_db bulk: could not log {name}: {e}")
            if st.get('tpdb_fallback') and outcome in ('not_found', 'no_exact') and not st['stop']:
                try:
                    o2, d2, id2, ph2 = _bulk_one(conn, name, 'tpdb')
                except Exception as e:
                    o2, d2, id2, ph2 = 'error', f'ThePornDB: {e}', None, False
                try:
                    conn.execute('INSERT OR REPLACE INTO cast_ext_bulk_log '
                                 '(cast_name, ext_source, run_id, outcome, detail, ext_id, checked_at) '
                                 'VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)', (name, 'tpdb', run_id, o2, d2, id2))
                    conn.commit()
                except Exception:
                    pass
                if o2 == 'linked':
                    outcome, detail, ext_id, photo = o2, 'ThePornDB: ' + d2, id2, ph2
            st['counts'][outcome] = st['counts'].get(outcome, 0) + 1
            if photo:
                st['photos'] += 1
            st['recent'].insert(0, {'cast_name': name, 'outcome': outcome,
                                    'outcome_label': BULK_OUTCOMES.get(outcome, outcome), 'detail': detail})
            del st['recent'][40:]
            st['done'] = idx + 1
            errors_in_a_row = errors_in_a_row + 1 if outcome == 'error' else 0
            if errors_in_a_row >= 10:
                st['error'] = f'Stopped after 10 errors in a row. Last error: {detail}'
                break
    except ExtDBError as e:
        st['error'] = str(e)
    except Exception as e:
        st['error'] = f'Unexpected error: {e}'
    finally:
        _pace_interval = 0.0
        st['running'] = False
        st['current'] = None
        st['note'] = ''
        st['finished_at'] = time.time()
        print(f"ext_db bulk run {run_id} finished: {st['done']}/{st['total']} {st['counts']}")


def _bulk_status():
    st = dict(_bulk)
    st.pop('stop', None)
    if st.get('started_at'):
        end = st.get('finished_at') or time.time()
        st['elapsed_seconds'] = round(end - st['started_at'])
        done, total = st.get('done', 0), st.get('total', 0)
        st['eta_seconds'] = (round((end - st['started_at']) / done * (total - done))
                             if st.get('running') and done else None)
    st['outcome_labels'] = BULK_OUTCOMES
    return st


class BulkBody(BaseModel):
    source: Optional[str] = None
    genders: list = ['female']
    categories: list = ['pro']
    skip_checked: bool = True
    delay: float = 1.0
    names: Optional[list] = None     # explicit selection (overrides gender/category)
    tpdb_fallback: bool = False      # StashDB found nothing (or nothing exact): try ThePornDB too


def _bulk_filters(genders, categories):
    g = [x for x in (genders or []) if x in BULK_GENDERS]
    c = [x for x in (categories or []) if x in BULK_CATEGORIES]
    if not g or not c:
        raise ExtDBError('Pick at least one gender and one category')
    return g, c


@router.get('/bulk/preview')
def bulk_preview(source: Optional[str] = None, genders: str = 'female', categories: str = 'pro',
                 skip_checked: bool = True, delay: float = 1.0, model=Depends(get_model)):
    """How many performers a bulk run would check (local DB only)."""
    try:
        ensure_schema()
        src = _source(source)
        g, c = _bulk_filters(genders.split(','), categories.split(','))
        names, stats = bulk_candidates(_conn(), src, g, c, skip_checked)
        delay = min(max(float(delay), 0.5), 10.0)
        return {'success': True, 'source': src, 'stats': stats,
                'estimate_seconds': round(len(names) * delay * 2.5),
                'first': [n for n, _ in names[:5]]}
    except Exception as e:
        return _err(e)


@router.post('/bulk/preview')
def bulk_preview_post(body: BulkBody, model=Depends(get_model)):
    """Same as GET /bulk/preview, but accepts an explicit selection of names."""
    try:
        ensure_schema()
        src = _source(body.source)
        if body.names is not None:
            g, c = list(BULK_GENDERS), list(BULK_CATEGORIES)
        else:
            g, c = _bulk_filters(body.genders, body.categories)
        names, stats = bulk_candidates(_conn(), src, g, c, body.skip_checked, names=body.names)
        delay = min(max(float(body.delay), 0.5), 10.0)
        return {'success': True, 'source': src, 'stats': stats,
                'estimate_seconds': round(len(names) * delay * 2.5),
                'first': [n for n, _ in names[:5]]}
    except Exception as e:
        return _err(e)


@router.post('/bulk/start')
def bulk_start(body: BulkBody, model=Depends(get_model)):
    try:
        ensure_schema()
        src = _source(body.source)
        if not api_key(src):
            raise ExtDBError(f"No API key configured for {SOURCES[src]['label']}")
        if body.names is not None:
            g, c = list(BULK_GENDERS), list(BULK_CATEGORIES)
        else:
            g, c = _bulk_filters(body.genders, body.categories)
        delay = min(max(float(body.delay), 0.5), 10.0)
        with _bulk_lock:
            if _bulk.get('running'):
                raise ExtDBError('A bulk run is already in progress')
            if _sbulk.get('running'):
                raise ExtDBError('Bulk scene matching is running - wait for it to finish or stop it first')
            names, stats = bulk_candidates(_conn(), src, g, c, body.skip_checked, names=body.names)
            if not names:
                raise ExtDBError('None of these performers need checking' if body.names is not None
                                 else 'No performers match these filters')
            _bulk_cands.clear()
            _bulk.clear()
            _bulk.update({
                'running': True, 'stop': False, 'stopped': False, 'run_id': uuid.uuid4().hex[:12],
                'source': src, 'source_label': SOURCES[src]['label'], 'genders': g, 'categories': c,
                'selection': len(body.names) if body.names is not None else None,
                'delay': delay, 'total': len(names), 'done': 0, 'counts': {}, 'photos': 0,
                'current': None, 'note': '', 'error': None, 'recent': [],
                'started_at': time.time(), 'finished_at': None,
            })
            _bulk['tpdb_fallback'] = bool(body.tpdb_fallback and src == 'stashdb' and api_key('tpdb'))
            threading.Thread(target=_bulk_worker, args=(names, src, delay),
                             name='extdb-bulk', daemon=True).start()
        return {'success': True, 'status': _bulk_status()}
    except Exception as e:
        return _err(e)


@router.get('/bulk/status')
def bulk_status():
    return {'success': True, 'status': _bulk_status()}


@router.post('/bulk/stop')
def bulk_stop():
    if _bulk.get('running'):
        _bulk['stop'] = True
    return {'success': True, 'status': _bulk_status()}


@router.get('/bulk/results')
def bulk_results(run_id: Optional[str] = None, model=Depends(get_model)):
    """Per-performer outcomes of a run (default: the latest run)."""
    try:
        ensure_schema()
        conn = _conn()
        if not run_id:
            row = conn.execute('SELECT run_id FROM cast_ext_bulk_log ORDER BY checked_at DESC LIMIT 1').fetchone()
            run_id = row[0] if row else None
        if not run_id:
            return {'success': True, 'run_id': None, 'results': []}
        rows = conn.execute('SELECT cast_name, outcome, detail, ext_id, checked_at FROM cast_ext_bulk_log '
                            'WHERE run_id = ? ORDER BY checked_at, cast_name', (run_id,)).fetchall()
        return {'success': True, 'run_id': run_id, 'outcome_labels': BULK_OUTCOMES,
                'results': [{'cast_name': r[0], 'outcome': r[1], 'outcome_label': BULK_OUTCOMES.get(r[1], r[1]),
                             'detail': r[2], 'ext_id': r[3], 'checked_at': r[4]} for r in rows]}
    except Exception as e:
        return _err(e)


@router.post('/bulk/forget-checked')
def bulk_forget_checked(source: Optional[str] = None, model=Depends(get_model)):
    """Clear the 'already checked' memory so the next run re-checks everyone unlinked."""
    try:
        ensure_schema()
        conn = _conn()
        cur = conn.execute('DELETE FROM cast_ext_bulk_log WHERE ext_source = ?', (_source(source),))
        conn.commit()
        return {'success': True, 'removed': cur.rowcount}
    except Exception as e:
        return _err(e)


@router.get('/bulk/candidates')
def bulk_review_candidates(name: str = Query(...), source: Optional[str] = None, model=Depends(get_model)):
    """Candidates for one performer from the bulk results. Uses what the run
    already found (no request to StashDB); falls back to a live search only if
    that is gone (new run started, or the app restarted)."""
    try:
        ensure_schema()
        src = _source(source)
        conn = _conn()
        primary = resolve_primary(conn, name)
        cached = _bulk_cands.get(primary)
        if cached and cached['source'] == src:
            current = get_link_row(conn, primary)
            results = [_result_from_n(conn, src, primary, it['ext_id'], it['n'], it['score'],
                                      it['reasons'], it['terms'], i, current)
                       for i, it in enumerate(cached['items'])]
            return {'success': True, 'cast_name': primary, 'source': src, 'cached': True,
                    'terms': sorted({t for it in cached['items'] for t in it['terms']}), 'results': results}
        live = search(SearchBody(cast_name=primary, source=src), model)
        if isinstance(live, dict):
            live['cached'] = False
        return live
    except Exception as e:
        return _err(e)


# ===========================================================================
# Attribute suggestions
# ===========================================================================
# Suggests local attribute values from the linked external data. Every rule is
# re-checked against how YOU tagged your other linked performers, each time
# suggestions are requested (nothing is stored), so the percentages shown are
# "how often you agreed with this rule so far". Suggestions that you agreed
# with at least PRETICK of the time (over MIN_SAMPLES+ performers) start ticked.
# Nothing is added until the user clicks Add; additions never remove anything.

PRETICK = 0.75            # recalibrated Oct 2026 (1,220 linked / 665 tagged): 0.70 gave tits 80%, body 78%
MIN_SAMPLES = 5          # fewer examples than this -> fall back to a default rule
PRETICK_MIN_N = 10       # a live percentage needs at least this many examples to pre-tick
SUGGEST_FIELDS = ('tits', 'body', 'ass', 'ethnicity', 'nationality')
CUP_ORDER = ['AAA', 'AA', 'A', 'B', 'C', 'D', 'DD', 'DDD', 'E', 'F', 'FF', 'G', 'GG',
             'H', 'HH', 'I', 'J', 'K', 'L', 'M', 'N']
SIZE_TAGS = ['very.small.tits', 'small.tits', 'medium.tits', 'big.tits', 'very.big.tits']
MAIN_SIZE = ['small.tits', 'medium.tits', 'big.tits']
# Body rule: frame from height, softness from waist-to-height, curves from cup
# or hip-to-height. Re-tuned on 417 tagged performers (Sep 2026, 2nd calibration:
# 52% cross-validated). The similarity vote below does better (60%) and its
# confidence is reliable (>=70% agreement -> 81% correct), so it leads.
BODY_RULE = {'small_h': 157, 'soft': 0.44, 'lean': 0.36, 'curve_cup': 'DD',
             'curve_hip': 0.60, 'vpet_h': 150, 'thin': 0.34}
# Similarity vote: the K most similar tagged performers (height, waist, hip,
# cup, waist-to-height), each field divided by its weight.
BODY_VOTE = {'k': 35, 'min_pool': 40, 'weights': (10.0, 1.5, 2.0, 1.0, 0.02),
             'min_share': 0.25, 'max_items': 3}
ASS_HIP_MIN = 36          # big.ass: 88% of ass-tagged performers with 36"+ hips
LATAM = {'MX', 'GT', 'HN', 'SV', 'NI', 'CR', 'PA', 'CU', 'DO', 'PR', 'CO', 'VE', 'EC',
         'PE', 'BO', 'CL', 'AR', 'UY', 'PY', 'BR'}
BODY_TAGS = {'very.petite', 'petite', 'curvy.petite', 'perky.chubby', 'thin', 'fit',
             'curvy', 'thick', 'chubby'}
ETHNICITY_DEFAULT = {'CAUCASIAN': 'white', 'ASIAN': 'asian', 'LATIN': 'latina',
                     'MIXED': 'mixed.race', 'INDIAN': 'indian.middle.eastern',
                     'MIDDLE_EASTERN': 'indian.middle.eastern'}
DEMONYMS = {  # ISO 3166-1 alpha-2 -> nationality word used as the tag (lowercase, spaces -> periods)
    'AD': 'andorran', 'AE': 'emirati', 'AF': 'afghan', 'AG': 'antiguan', 'AL': 'albanian', 'AM': 'armenian',
    'AO': 'angolan', 'AR': 'argentinian', 'AT': 'austrian', 'AU': 'australian', 'AW': 'aruban', 'AZ': 'azerbaijani',
    'BA': 'bosnian', 'BB': 'barbadian', 'BD': 'bangladeshi', 'BE': 'belgian', 'BF': 'burkinabe', 'BG': 'bulgarian',
    'BH': 'bahraini', 'BI': 'burundian', 'BJ': 'beninese', 'BM': 'bermudian', 'BN': 'bruneian', 'BO': 'bolivian',
    'BR': 'brazilian', 'BS': 'bahamian', 'BT': 'bhutanese', 'BW': 'botswanan', 'BY': 'belarusian', 'BZ': 'belizean',
    'CA': 'canadian', 'CD': 'congolese', 'CF': 'central.african', 'CG': 'congolese', 'CH': 'swiss', 'CI': 'ivorian',
    'CL': 'chilean', 'CM': 'cameroonian', 'CN': 'chinese', 'CO': 'colombian', 'CR': 'costa.rican', 'CU': 'cuban',
    'CV': 'cape.verdean', 'CW': 'curacaoan', 'CY': 'cypriot', 'CZ': 'czech', 'DE': 'german', 'DJ': 'djiboutian',
    'DK': 'danish', 'DM': 'dominican', 'DO': 'dominican', 'DZ': 'algerian', 'EC': 'ecuadorian', 'EE': 'estonian',
    'EG': 'egyptian', 'ER': 'eritrean', 'ES': 'spanish', 'ET': 'ethiopian', 'FI': 'finnish', 'FJ': 'fijian',
    'FR': 'french', 'GA': 'gabonese', 'GB': 'british', 'GD': 'grenadian', 'GE': 'georgian', 'GH': 'ghanaian',
    'GM': 'gambian', 'GN': 'guinean', 'GQ': 'equatoguinean', 'GR': 'greek', 'GT': 'guatemalan', 'GU': 'guamanian',
    'GY': 'guyanese', 'HK': 'hong.konger', 'HN': 'honduran', 'HR': 'croatian', 'HT': 'haitian', 'HU': 'hungarian',
    'ID': 'indonesian', 'IE': 'irish', 'IL': 'israeli', 'IN': 'indian', 'IQ': 'iraqi', 'IR': 'iranian',
    'IS': 'icelandic', 'IT': 'italian', 'JM': 'jamaican', 'JO': 'jordanian', 'JP': 'japanese', 'KE': 'kenyan',
    'KG': 'kyrgyz', 'KH': 'cambodian', 'KR': 'korean', 'KP': 'north.korean', 'KW': 'kuwaiti', 'KZ': 'kazakh',
    'LA': 'laotian', 'LB': 'lebanese', 'LI': 'liechtensteiner', 'LK': 'sri.lankan', 'LR': 'liberian',
    'LT': 'lithuanian', 'LU': 'luxembourgish', 'LV': 'latvian', 'LY': 'libyan', 'MA': 'moroccan', 'MC': 'monegasque',
    'MD': 'moldovan', 'ME': 'montenegrin', 'MG': 'malagasy', 'MK': 'macedonian', 'ML': 'malian', 'MM': 'burmese',
    'MN': 'mongolian', 'MO': 'macanese', 'MT': 'maltese', 'MU': 'mauritian', 'MV': 'maldivian', 'MW': 'malawian',
    'MX': 'mexican', 'MY': 'malaysian', 'MZ': 'mozambican', 'NA': 'namibian', 'NE': 'nigerien', 'NG': 'nigerian',
    'NI': 'nicaraguan', 'NL': 'dutch', 'NO': 'norwegian', 'NP': 'nepali', 'NZ': 'new.zealander', 'OM': 'omani',
    'PA': 'panamanian', 'PE': 'peruvian', 'PG': 'papua.new.guinean', 'PH': 'filipina', 'PK': 'pakistani',
    'PL': 'polish', 'PR': 'puerto.rican', 'PS': 'palestinian', 'PT': 'portuguese', 'PY': 'paraguayan',
    'QA': 'qatari', 'RO': 'romanian', 'RS': 'serbian', 'RU': 'russian', 'RW': 'rwandan', 'SA': 'saudi',
    'SC': 'seychellois', 'SD': 'sudanese', 'SE': 'swedish', 'SG': 'singaporean', 'SI': 'slovenian', 'SK': 'slovak',
    'SL': 'sierra.leonean', 'SN': 'senegalese', 'SO': 'somali', 'SR': 'surinamese', 'SV': 'salvadoran',
    'SY': 'syrian', 'TG': 'togolese', 'TH': 'thai', 'TJ': 'tajik', 'TM': 'turkmen', 'TN': 'tunisian', 'TR': 'turkish',
    'TT': 'trinidadian', 'TW': 'taiwanese', 'TZ': 'tanzanian', 'UA': 'ukrainian', 'UG': 'ugandan', 'US': 'american',
    'UY': 'uruguayan', 'UZ': 'uzbek', 'VE': 'venezuelan', 'VN': 'vietnamese', 'XK': 'kosovar', 'YE': 'yemeni',
    'ZA': 'south.african', 'ZM': 'zambian', 'ZW': 'zimbabwean'}


def nationality_tag(code):
    """ISO code -> tag value, e.g. 'US' -> 'american'. Falls back to the country
    name in tag form ('Isle of Man' -> 'isle.of.man') for anything not listed."""
    if not code:
        return None
    code = str(code).strip().upper()
    if code in DEMONYMS:
        return DEMONYMS[code]
    name = unicodedata.normalize('NFKD', country_name(code) or code)
    name = ''.join(ch for ch in name if not unicodedata.combining(ch)).lower()
    name = re.sub(r'[^a-z0-9]+', '.', name).strip('.')
    return name or None
EASTERN_EUROPE = {'CZ', 'SK', 'PL', 'HU', 'RO', 'BG', 'RU', 'UA', 'BY', 'LV', 'LT', 'EE', 'RS',
                  'HR', 'SI', 'MD', 'BA', 'MK', 'AL', 'ME', 'XK'}


def cup_rank(cup):
    """'DD' -> index in CUP_ORDER; 'D/DD' takes the larger; None if unknown."""
    if not cup:
        return None
    c = re.sub(r'[^A-Z/]', '', str(cup).upper()).split('/')[-1]
    return CUP_ORDER.index(c) if c in CUP_ORDER else None


def _measures(row):
    m = {'cup': cup_rank(row.get('ext_cup_size')), 'cup_label': row.get('ext_cup_size'),
         'band': row.get('ext_band_size'), 'waist': row.get('ext_waist_size'),
         'hip': row.get('ext_hip_size'), 'height': row.get('ext_height_cm')}
    m['whtr'] = (m['waist'] * 2.54 / m['height']) if m['waist'] and m['height'] else None
    m['hipr'] = (m['hip'] * 2.54 / m['height']) if m['hip'] and m['height'] else None
    return m


def classify_body(m):
    """Body tags from measurements, following the library's own definitions:
    petite frames (height) split by softness (waist/height) and curves (cup)."""
    if m['cup'] is None or not m['height'] or m['whtr'] is None:
        return []
    R = BODY_RULE
    soft, lean = m['whtr'] >= R['soft'], m['whtr'] < R['lean']
    curves = m['cup'] >= CUP_ORDER.index(R['curve_cup']) or (m['hipr'] or 0) >= R['curve_hip']
    if m['height'] <= R['small_h']:
        if soft:
            return ['perky.chubby']
        if curves:
            return ['curvy.petite']
        if m['height'] <= R['vpet_h'] and lean:
            return ['very.petite', 'petite']
        return ['petite']
    if m['whtr'] < R['thin']:
        return ['thin']
    if soft and curves:
        return ['thick']
    if soft:
        return ['chubby']
    return ['curvy'] if curves else ['fit']


def _vocabulary(conn):
    """Values the user already uses, per field (quicktags.json + existing attributes)."""
    vocab = {f: set() for f in SUGGEST_FIELDS}
    for r in _rows(conn, 'SELECT DISTINCT attribute_type, attribute_value FROM cast_attributes'):
        if r[0] in vocab:
            vocab[r[0]].add(r[1])
    try:
        from config import DATA_FILE
        path = os.path.join(os.path.dirname(DATA_FILE), 'quicktags.json')
        with open(path, 'r', encoding='utf-8') as f:
            for field, values in (json.load(f) or {}).items():
                if field in vocab:
                    vocab[field].update(values)
    except Exception:
        pass
    return vocab


def _usage(conn, field, value):
    row = conn.execute('SELECT COUNT(*) FROM cast_attributes WHERE attribute_type = ? AND attribute_value = ?',
                       (field, value)).fetchone()
    return row[0] if row else 0


def _calibration(conn, exclude):
    """Your other linked performers: their external data + the tags you gave them."""
    attrs = {}
    for r in _rows(conn, 'SELECT a.cast_name, a.attribute_type, a.attribute_value FROM cast_attributes a '
                         'JOIN cast_ext_link l ON l.cast_name = a.cast_name'):
        if r[2] and _VALUE_RE.match(r[2]):          # skip malformed values like "white, russian"
            attrs.setdefault(r[0], {}).setdefault(r[1], set()).add(r[2])
    out = []
    for r in _rows(conn, 'SELECT cast_name, ext_cup_size, ext_band_size, ext_waist_size, ext_hip_size, '
                         'ext_height_cm, ext_breast_type, ext_ethnicity, ext_nationality FROM cast_ext_link'):
        if r[0] == exclude or r[0] not in attrs:
            continue
        row = dict(zip(['cast_name', 'ext_cup_size', 'ext_band_size', 'ext_waist_size', 'ext_hip_size',
                        'ext_height_cm', 'ext_breast_type', 'ext_ethnicity', 'ext_nationality'], r))
        out.append({'row': row, 'm': _measures(row), 'tags': attrs[r[0]]})
    return out


def _share(group, test):
    n = len(group)
    k = sum(1 for g in group if test(g))
    return k, n, (k / n if n else None)


def suggestion_context(conn):
    """What every suggestion needs and is the same for all performers: your vocabulary and the
    calibration data of all linked performers. Build it once when suggesting for many."""
    return {'vocab': _vocabulary(conn), 'cal_all': _calibration(conn, None)}


def build_suggestions(conn, primary, ctx=None):
    link = get_link_row(conn, primary)
    if not link:
        raise ExtDBError('Not linked to an external database')
    have = {}
    for r in _rows(conn, 'SELECT attribute_type, attribute_value FROM cast_attributes WHERE cast_name = ?', (primary,)):
        have.setdefault(r[0], set()).add(r[1])
    ctx = ctx or suggestion_context(conn)
    vocab = ctx['vocab']
    cal = [c for c in ctx['cal_all'] if c['row']['cast_name'] != primary]     # everyone except this performer
    m = _measures(link)
    gender = local_gender(conn, primary) or 'female'
    label = SOURCES.get(link['ext_source'], {}).get('label', 'the external DB')
    items, hints = [], []

    def add(field, value, reason, k=None, n=None, factual=False, may_replace=True, may_pretick=True):
        share = (k / n) if (k is not None and n) else None
        dup = next((i for i in items if i['field'] == field and i['value'] == value), None)
        if dup:
            # keep the stronger evidence - unless this rule is only allowed to add new values
            if not may_replace or dup['factual'] or (dup['confidence'] or 0) >= (share or 0):
                return
            items.remove(dup)
        strong = (factual and may_pretick) or (may_pretick and share is not None and n >= PRETICK_MIN_N and share >= PRETICK)
        already = value in have.get(field, set())
        items.append({'field': field, 'value': value, 'reason': reason,
                      'confidence': round(share, 2) if share is not None else None,
                      'samples': n or 0, 'factual': factual, 'strong': strong,
                      'pre_tick': strong and not already, 'already_set': already,
                      'new_value': value not in vocab.get(field, set())})

    tagged = lambda field: [c for c in cal if c['tags'].get(field)]
    meas = link.get('ext_cup_size') and f"{link.get('ext_band_size') or '?'}{link['ext_cup_size']}"

    # ---- tits: size by cup letter (your tags follow the cup, not the band) ----
    if gender != 'male' and m['cup'] is not None:
        cup = CUP_ORDER[m['cup']]
        sized = [c for c in tagged('tits') if c['m']['cup'] is not None and c['tags']['tits'] & set(SIZE_TAGS)]
        same = [c for c in sized if c['m']['cup'] == m['cup']]
        if len(same) >= MIN_SAMPLES:
            best = max(MAIN_SIZE, key=lambda t: sum(t in c['tags']['tits'] for c in same))
            k, n, _ = _share(same, lambda c: best in c['tags']['tits'])
            add('tits', best, f'{meas} → you tagged {k} of {n} {cup}-cup performers {best}', k, n)
        else:
            best = 'small.tits' if m['cup'] <= CUP_ORDER.index('B') else (
                'medium.tits' if m['cup'] == CUP_ORDER.index('C') else 'big.tits')
            add('tits', best, f'{meas} → default rule (fewer than {MIN_SAMPLES} tagged {cup}-cup performers yet)')
        if m['cup'] <= CUP_ORDER.index('A'):
            grp = [c for c in sized if c['m']['cup'] <= CUP_ORDER.index('A')]
            k, n, _ = _share(grp, lambda c: 'very.small.tits' in c['tags']['tits'])
            add('tits', 'very.small.tits', f'A cup or smaller → you added very.small.tits to {k} of {n}'
                if n else 'A cup or smaller', k if n else None, n if n else None)
        if m['cup'] >= CUP_ORDER.index('H'):
            grp = [c for c in sized if c['m']['cup'] >= CUP_ORDER.index('H')]
            k, n, _ = _share(grp, lambda c: 'very.big.tits' in c['tags']['tits'])
            add('tits', 'very.big.tits', f'H cup or larger → you added very.big.tits to {k} of {n}'
                if n else 'H cup or larger', k if n else None, n if n else None)
    bt = (link.get('ext_breast_type') or '').upper()
    if gender != 'male' and bt in ('FAKE', 'NATURAL'):
        # a fact from the record, but you tag it on only ~1 in 4 performers: offered, not pre-ticked
        add('tits', 'fake.tits' if bt == 'FAKE' else 'natural.tits',
            f'{label} lists breast type as {bt.lower()}', factual=True, may_pretick=False)

    # ---- body ----
    if gender != 'male':
        bodyc = [c for c in tagged('body') if c['tags']['body'] & BODY_TAGS]
        # 1) similarity vote among your tagged performers
        pool = [c for c in bodyc if c['m']['cup'] is not None and c['m']['whtr'] and c['m']['hip']]
        if m['cup'] is not None and m['whtr'] and m['hip'] and len(pool) >= BODY_VOTE['min_pool']:
            W = BODY_VOTE['weights']
            key = lambda x: (x['height'] / W[0], x['waist'] / W[1], x['hip'] / W[2], x['cup'] / W[3], x['whtr'] / W[4])
            me = key(m)
            near = sorted(pool, key=lambda c: sum((a - b) ** 2 for a, b in zip(key(c['m']), me)))[:BODY_VOTE['k']]
            cnt = Counter(t for c in near for t in c['tags']['body'] if t in BODY_TAGS)
            for tag, v in cnt.most_common(BODY_VOTE['max_items']):
                if v / len(near) >= BODY_VOTE['min_share']:
                    add('body', tag, f'{v} of the {len(near)} performers with the most similar measurements are tagged {tag}',
                        v, len(near))
        # 2) narrow petite rule
        if m['height'] and m['cup'] is not None and m['height'] <= 155 and m['cup'] <= CUP_ORDER.index('B'):
            grp = [c for c in bodyc if c['m']['height'] and c['m']['cup'] is not None
                   and c['m']['height'] <= 155 and c['m']['cup'] <= CUP_ORDER.index('B')]
            k, n, _ = _share(grp, lambda c: 'petite' in c['tags']['body'])
            add('body', 'petite', f"{m['height']} cm with {m['cup_label']} cup → you tagged {k} of {n} like this petite",
                k, n)
        # 3) the frame/softness/curves rule: only ADDS types the vote didn't mention
        #    (e.g. thick). It must never override the vote: when similar performers
        #    disagree, this rule's overall agreement rate doesn't hold (calibration
        #    run 2: 0 of 23 right in exactly those cases), so it never pre-ticks either.
        guess = classify_body(m)
        if guess:
            desc = f"{m['height']} cm, waist {m['waist']}\", waist/height {m['whtr']:.2f}, {m['cup_label']} cup"
            for tag in guess:
                grp = [c for c in bodyc if tag in classify_body(c['m'])]
                k, n, _ = _share(grp, lambda c: tag in c['tags']['body'])
                add('body', tag, f'{desc} → when this rule says {tag}, you agreed {k} of {n}', k, n,
                    may_replace=False, may_pretick=False)

    # ---- ass ----
    if gender != 'male' and m['hip'] and m['hip'] >= ASS_HIP_MIN:
        grp = [c for c in tagged('ass') if c['m']['hip'] and c['m']['hip'] >= ASS_HIP_MIN]
        k, n, _ = _share(grp, lambda c: 'big.ass' in c['tags']['ass'])
        add('ass', 'big.ass', f"hip {m['hip']}\" → {k} of {n} performers you gave an ass tag with {ASS_HIP_MIN}\"+ hips are big.ass",
            k, n)

    # ---- ethnicity (from the external ethnicity, calibrated on your tags) ----
    eth = (link.get('ext_ethnicity') or '').upper()
    if eth:
        grp = [c for c in tagged('ethnicity') if (c['row'].get('ext_ethnicity') or '').upper() == eth]
        pretty = _pretty(eth)
        region = lambda code: 'latam' if code in LATAM else ('us' if code == 'US' else 'other')
        my_region = region((link.get('ext_nationality') or '').upper())
        sub = [c for c in grp if region((c['row'].get('ext_nationality') or '').upper()) == my_region]
        where = ''
        if len(sub) >= PRETICK_MIN_N and my_region != 'other':
            grp = sub
            where = ' from Latin America' if my_region == 'latam' else ' from the US'
        if len(grp) >= MIN_SAMPLES:
            best = Counter(v for c in grp for v in c['tags']['ethnicity']).most_common(1)[0][0]
            k, n, _ = _share(grp, lambda c: best in c['tags']['ethnicity'])
            add('ethnicity', best, f'{label}: {pretty}{where} → you tagged {k} of {n} {pretty} performers{where} {best}', k, n)
        else:
            if eth == 'BLACK':      # your own split (black.girl / black.male) when you use it, else 'black'
                split = {'black.girl', 'black.male'} & vocab.get('ethnicity', set())
                best = ('black.male' if gender == 'male' else 'black.girl') if split else 'black'
            else:
                best = ETHNICITY_DEFAULT.get(eth)
            if best:
                add('ethnicity', best, f'{label}: {pretty} → default rule (fewer than {MIN_SAMPLES} tagged yet)')

    # ---- nationality: its own field; always suggested when the record has a country ----
    # (a fact from the linked record, so pre-ticked like breast type)
    nat = (link.get('ext_nationality') or '').upper()
    if nat:
        country = country_name(nat)
        value = nationality_tag(nat)
        same_nat = [c for c in tagged('ethnicity') if (c['row'].get('ext_nationality') or '').upper() == nat]
        if value:
            used = _usage(conn, 'nationality', value)
            add('nationality', value, f'{label} lists nationality as {country}'
                + (f' (used for {used} performer{"s" if used != 1 else ""} so far)' if used else ''), factual=True)
        # region tag, only if you use it - offered but not pre-ticked
        if nat in EASTERN_EUROPE and 'eastern.european' in vocab['ethnicity']:
            if len(same_nat) >= MIN_SAMPLES:
                k, n, _ = _share(same_nat, lambda c: 'eastern.european' in c['tags']['ethnicity'])
                add('ethnicity', 'eastern.european', f'{country} is in Eastern Europe → you tagged {k} of {n} performers from {country} eastern.european', k, n)
            else:
                add('ethnicity', 'eastern.european', f'{country} is in Eastern Europe (you use eastern.european for {_usage(conn, "ethnicity", "eastern.european")} performers)')

    order = {f: i for i, f in enumerate(SUGGEST_FIELDS)}
    items.sort(key=lambda i: (order[i['field']], not i['pre_tick'], -(i['confidence'] or 0)))
    return {'items': items, 'hints': hints, 'calibration_size': len(cal),
            'measurements': build_display(row_to_public(link) or {}).get('measurements'),
            'height_cm': m['height']}


class ApplyBody(BaseModel):
    cast_name: str
    items: list          # [{"field": "tits", "value": "small.tits"}, ...]


_VALUE_RE = re.compile(r'^[a-z0-9][a-z0-9.\-]*$')


def _apply_attributes(conn, primary, items):
    """Add attributes to a performer (starred, so they apply to their videos). Only ever adds."""
    clean = []
    for it in items:
        field = str((it or {}).get('field', '')).strip().lower()
        value = str((it or {}).get('value', '')).strip().lower()
        if field not in SUGGEST_FIELDS or not _VALUE_RE.match(value):
            raise ExtDBError(f'Invalid attribute {field}={value}')
        clean.append((field, value))
    added = []
    for field, value in clean:
        cur = conn.execute('INSERT OR IGNORE INTO cast_attributes '
                           '(cast_name, attribute_type, attribute_value, is_primary, video_count) '
                           'VALUES (?, ?, ?, 1, 0)', (primary, field, value))
        if cur.rowcount:
            added.append({'field': field, 'value': value})
    return added


@router.get('/suggest')
def suggest(name: str = Query(...), model=Depends(get_model)):
    """Attribute suggestions for a linked performer (local data only, no network)."""
    try:
        ensure_schema()
        conn = _conn()
        primary = resolve_primary(conn, name)
        return {'success': True, 'cast_name': primary, **build_suggestions(conn, primary)}
    except Exception as e:
        return _err(e)


@router.post('/suggest/apply')
def suggest_apply(body: ApplyBody, model=Depends(get_model)):
    """Add the chosen values to the performer's attributes. Only ever adds -
    existing attributes are never removed or changed."""
    try:
        ensure_schema()
        conn = _conn()
        primary = resolve_primary(conn, body.cast_name)
        added = _apply_attributes(conn, primary, body.items or [])
        conn.commit()
        attrs = {}
        for r in _rows(conn, 'SELECT attribute_type, attribute_value FROM cast_attributes WHERE cast_name = ? '
                             'ORDER BY attribute_type, is_primary DESC, video_count DESC', (primary,)):
            attrs.setdefault(r[0], []).append(r[1])
        return {'success': True, 'cast_name': primary, 'added': added, 'attributes': attrs}
    except Exception as e:
        return _err(e)


# ---------- many performers at once (Cast -> Bulk suggest) ----------
ATTR_REQUIRED = ('ethnicity', 'face', 'body', 'tits')     # same rule as the Cast browser's "complete"


class BulkSuggestApplyBody(BaseModel):
    performers: list                  # [{"cast_name": ..., "items": [{"field", "value"}]}]
    apply_to_videos: bool = True


@router.get('/suggest/bulk')
def suggest_bulk(genders: str = 'female', categories: str = 'pro', status: str = 'any', q: str = '',
                 only_pretick: bool = True, page: int = 1, per_page: int = 100, model=Depends(get_model)):
    """Attribute suggestions for many linked performers (local data only, no requests)."""
    try:
        ensure_schema()
        conn = _conn()
        g, c = _bulk_filters([x for x in genders.split(',') if x], [x for x in categories.split(',') if x])
        gmap = {r[0]: r[1] for r in _rows(conn, 'SELECT cast_name, gender FROM cast_gender')}
        cmap = {r[0]: r[1] for r in _rows(conn, 'SELECT cast_name, category FROM cast_category')}
        attrs, existing = {}, {}
        for r in _rows(conn, 'SELECT cast_name, attribute_type, attribute_value FROM cast_attributes '
                             'ORDER BY attribute_type, is_primary DESC, attribute_value'):
            attrs.setdefault(r[0], set()).add(r[1])
            existing.setdefault(r[0], {}).setdefault(r[1], []).append(r[2])
        photos = {r[0] for r in _rows(conn, 'SELECT cast_name FROM cast_photos WHERE thumbnail_data IS NOT NULL')}
        aliases = {r[0] for r in _rows(conn, 'SELECT alias_name FROM cast_aliases')}
        needle = q.strip().lower()
        ctx = suggestion_context(conn)
        stats = Counter()
        out = []
        for (name,) in _rows(conn, 'SELECT cast_name FROM cast_ext_link ORDER BY cast_name COLLATE NOCASE'):
            if name in aliases or gmap.get(name, 'female') not in g or cmap.get(name, 'pro') not in c:
                continue
            if needle and needle not in name.lower():
                continue
            have = attrs.get(name, set())
            n_req = sum(1 for f in ATTR_REQUIRED if f in have)
            st = 'complete' if n_req == len(ATTR_REQUIRED) else ('none' if n_req == 0 else 'partial')
            if status in ('none', 'partial', 'complete') and st != status:
                continue
            stats['matched'] += 1
            try:
                s = build_suggestions(conn, name, ctx)
            except ExtDBError:
                continue
            items = [i for i in s['items'] if not i['already_set']]
            pre = sum(1 for i in items if i['pre_tick'])
            if not items:
                stats['nothing_new'] += 1
                continue
            if only_pretick and not pre:
                stats['nothing_pretick'] += 1
                continue
            stats['with_suggestions'] += 1
            stats['pretick_items'] += pre
            out.append({'cast_name': name, 'gender': gmap.get(name, 'female'), 'category': cmap.get(name, 'pro'),
                        'status': st, 'measurements': s.get('measurements'), 'height_cm': s.get('height_cm'),
                        'has_photo': name in photos, 'existing': existing.get(name, {}),
                        'items': [{k: i[k] for k in ('field', 'value', 'reason', 'confidence', 'samples', 'factual',
                                                     'strong', 'pre_tick', 'new_value')} for i in items]})
        page = max(1, int(page))
        per_page = min(max(int(per_page), 10), 500)
        return {'success': True, 'rows': out[(page - 1) * per_page: page * per_page], 'total': len(out),
                'page': page, 'per_page': per_page, 'stats': dict(stats), 'calibration_size': len(ctx['cal_all'])}
    except Exception as e:
        return _err(e)


@router.post('/suggest/bulk-apply')
def suggest_bulk_apply(body: BulkSuggestApplyBody, model=Depends(get_model)):
    """Add the chosen attributes to each performer (add only), then apply their starred
    attributes to their videos, like the single-performer flow."""
    try:
        ensure_schema()
        conn = _conn()
        results = []
        for p in (body.performers or [])[:100]:
            name = resolve_primary(conn, str((p or {}).get('cast_name', '')))
            res = {'cast_name': name}
            try:
                if not get_link_row(conn, name):
                    raise ExtDBError('not linked')
                res['added'] = _apply_attributes(conn, name, (p or {}).get('items') or [])
                conn.commit()
                if body.apply_to_videos and res['added']:
                    from routes.cast import apply_cast_attributes_to_videos
                    v = apply_cast_attributes_to_videos(name, model=_model) or {}
                    res['videos_processed'] = v.get('videos_processed', 0)
                    res['video_values_added'] = v.get('attributes_added', 0)
                    if v.get('success') is False:
                        res['video_error'] = v.get('error')
            except Exception as e:
                conn.rollback()
                res['error'] = str(e)
            res['attributes'] = {}
            for r in _rows(conn, 'SELECT attribute_type, attribute_value FROM cast_attributes WHERE cast_name = ? '
                                 'ORDER BY attribute_type, is_primary DESC, video_count DESC', (name,)):
                res['attributes'].setdefault(r[0], []).append(r[1])
            results.append(res)
        return {'success': True, 'results': results}
    except Exception as e:
        return _err(e)


# ===========================================================================
# Fill in nationality for linked performers (local data only - no requests)
# ===========================================================================
def _nationality_plan(conn):
    """Linked performers with a country on record but no nationality attribute yet.
    Anyone who already has a nationality value (any value) is left alone."""
    has = {r[0] for r in _rows(conn, "SELECT DISTINCT cast_name FROM cast_attributes "
                                     "WHERE attribute_type = 'nationality'")}
    plan, stats = [], Counter()
    for r in _rows(conn, 'SELECT cast_name, ext_nationality FROM cast_ext_link ORDER BY cast_name'):
        name, code = r[0], (r[1] or '').strip()
        stats['linked'] += 1
        value = nationality_tag(code) if code else None
        if not value:
            stats['no_country'] += 1
        elif name in has:
            stats['already_set'] += 1
        else:
            plan.append((name, value))
    stats['to_add'] = len(plan)
    return plan, dict(stats)


@router.get('/nationality/preview')
def nationality_preview(model=Depends(get_model)):
    try:
        ensure_schema()
        plan, stats = _nationality_plan(_conn())
        return {'success': True, 'stats': stats,
                'by_value': Counter(v for _, v in plan).most_common(),
                'examples': [{'cast_name': n, 'value': v} for n, v in plan[:10]]}
    except Exception as e:
        return _err(e)


@router.post('/nationality/fill')
def nationality_fill(model=Depends(get_model)):
    """Add the nationality from the linked record to every linked performer who
    has none yet. Only adds; existing values are never changed or removed."""
    try:
        ensure_schema()
        conn = _conn()
        plan, stats = _nationality_plan(conn)
        added, names = 0, []
        for name, value in plan:
            cur = conn.execute('INSERT OR IGNORE INTO cast_attributes '
                               '(cast_name, attribute_type, attribute_value, is_primary, video_count) '
                               "VALUES (?, 'nationality', ?, 1, 0)", (name, value))
            if cur.rowcount:
                added += 1
                names.append(name)
        conn.commit()
        print(f"ext_db: filled nationality for {added} linked performers")
        return {'success': True, 'added': added, 'names': names, 'stats': stats,
                'by_value': Counter(v for _, v in plan).most_common()}
    except Exception as e:
        return _err(e)


# ===========================================================================
# Sites -> external studios
# ===========================================================================
# Your site values are grouped by a normalised key (case, dots, spaces removed),
# so 'example.studio', 'examplestudio' and 'Examplestudio' are linked together once.
# Matching rules (from the Sep 2026 studio preview of this library):
#   * exact NAME beats alias beats website; one best match = exact
#   * '.network' sites also match "<name> network" and prefer a parent studio;
#     if the exact match is a sub-studio, the network links to its parent
#   * names written without dots (julesjordan, evilangel) fall back to a
#     website search, since StashDB's name search matches whole words
NETWORK_SUFFIXES = ('.network', '.networks')
STUDIO_FRAGMENT = 'fragment S on Studio { id name aliases urls { url } parent { id name } deleted }'
_site_cands = {}                 # site_key -> candidates from the last check (memory only)
_site_job = {'running': False}
_site_lock = threading.Lock()


def site_key(value):
    return norm_name(value)


def _site_term(value):
    s = (value or '').strip().lower()
    is_net = s.endswith(NETWORK_SUFFIXES)
    for suf in NETWORK_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s.replace('.', ' ').replace('_', ' ').strip(), is_net


def _url_roots(urls):
    roots = set()
    for u in urls or []:
        m = re.match(r'^(?:https?://)?(?:www\.)?([^/:?#]+)', (u or '').strip(), re.I)
        if m:
            roots.add(norm_name(m.group(1).lower().rsplit('.', 1)[0]))
    return roots


def _studio_public(st, how=None):
    return {'ext_id': st['id'], 'name': st['name'], 'aliases': (st.get('aliases') or [])[:8],
            'urls': [u.get('url') for u in (st.get('urls') or []) if u and u.get('url')][:3],
            'parent': ({'id': st['parent']['id'], 'name': st['parent']['name']} if st.get('parent') else None),
            'match': how}


def search_studios(source, term):
    data = gql(source, lambda f, old: STUDIO_FRAGMENT + ' query($t: String!) { searchStudio(term: $t, limit: 10) { ...S } }',
               {'t': term})
    return [x for x in (data.get('searchStudio') or []) if x and not x.get('deleted')]


def studios_by_letters(source, key, per_page=50):
    """Names/aliases containing the key's letters in order, whatever the spacing:
    'julesjordan' -> 'j%u%l%e%s%j%o%r%d%a%n' matches "Jules Jordan Video". StashDB's
    studio 'names' filter is a LIKE search (stash-box studio/query.go). Results are
    still judged by the same exact rules afterwards."""
    if len(key) < 5:
        return []
    pattern = '%'.join(key)
    data = gql(source, lambda f, old: STUDIO_FRAGMENT + ' query($p: String!, $n: Int!) '
               '{ queryStudios(input: {names: $p, per_page: $n}) { studios { ...S } } }', {'p': pattern, 'n': per_page})
    return [x for x in ((data.get('queryStudios') or {}).get('studios') or []) if x and not x.get('deleted')]


def find_studio(source, ext_id):
    data = gql(source, lambda f, old: STUDIO_FRAGMENT + ' query($id: ID!) { findStudio(id: $id) { ...S } }', {'id': ext_id})
    st = data.get('findStudio')
    if not st or st.get('deleted'):
        raise ExtDBError(f'Studio {ext_id} not found on {SOURCES[source]["label"]}')
    return st


def classify_site(value, results):
    """-> (outcome, pick_or_None, candidates). outcome: exact | network | ambiguous | close | not_found"""
    term, is_net = _site_term(value)
    key = norm_name(term)
    keys = {key, key + 'network'} if is_net else {key}
    ranked = []
    for st in results:
        how = None
        if norm_name(st['name']) in keys:
            how = 'name'
        elif keys & {norm_name(a) for a in st.get('aliases') or []}:
            how = 'alias'
        elif key in _url_roots(u.get('url') for u in st.get('urls') or []):
            how = 'website'
        ranked.append((st, how))
    cands = [_studio_public(st, how) for st, how in ranked]
    exact = [(st, how) for st, how in ranked if how]
    if not results:
        return 'not_found', None, cands
    if not exact:
        return 'close', None, cands
    if is_net:
        tops = [(st, h) for st, h in exact if not st.get('parent')]
        if len(tops) == 1:
            return 'network', _studio_public(tops[0][0], tops[0][1]), cands
        parents = {st['parent']['id']: st['parent'] for st, _ in exact if st.get('parent')}
        if not tops and len(parents) == 1:
            par = next(iter(parents.values()))
            return 'network', {'ext_id': par['id'], 'name': par['name'], 'aliases': [], 'urls': [],
                               'parent': None, 'match': 'parent of ' + exact[0][0]['name']}, cands
    for level in ('name', 'alias', 'website'):
        best = [(st, h) for st, h in exact if h == level]
        if len(best) == 1:
            return 'exact', _studio_public(best[0][0], level), cands
        if len(best) > 1:
            return 'ambiguous', None, cands
    return 'ambiguous', None, cands


def check_site(source, value, variants=None):
    """Search StashDB for a site: every spelling variant as its own search term
    ('cheatingsis' and 'cheating sis'), then a letters-in-order fallback."""
    terms = []
    for v in [value] + list(variants or []):
        t, _ = _site_term(v)
        if t and t not in terms:
            terms.append(t)
    results, seen = [], set()
    for t in terms[:4]:
        for st in search_studios(source, t):
            if st['id'] not in seen:
                seen.add(st['id'])
                results.append(st)
    outcome, pick, cands = classify_site(value, results)
    if outcome in ('close', 'not_found'):
        more = [st for st in studios_by_letters(source, norm_name(_site_term(value)[0])) if st['id'] not in seen]
        if more:
            outcome2, pick2, cands2 = classify_site(value, results + more)
            if outcome2 in ('exact', 'network', 'ambiguous') or outcome == 'not_found':
                outcome, pick, cands = outcome2, pick2, cands2
    return outcome, pick, cands


def _site_groups(conn):
    """{site_key: {'display', 'values': {value: videos}, 'videos'}} from your video_site values."""
    groups = {}
    jcols = [r[1] for r in _rows(conn, 'PRAGMA table_info(video_site_junction)')]
    fk = next((c for c in jcols if c.endswith('_id') and c != 'video_id'), None)
    if not fk:
        return groups
    for r in _rows(conn, f'SELECT s.value, COUNT(DISTINCT j.video_id) FROM video_site s '
                         f'JOIN video_site_junction j ON j.{fk} = s.id GROUP BY s.value'):
        value, n = r[0], r[1]
        k = site_key(value)
        if not k:
            continue
        g = groups.setdefault(k, {'values': {}, 'videos': 0})
        g['values'][value] = n
        g['videos'] += n
    for k, g in groups.items():
        g['display'] = max(g['values'].items(), key=lambda kv: (kv[1], kv[0].islower()))[0]
        g['is_network'] = any(v.lower().endswith(NETWORK_SUFFIXES) for v in g['values'])
    return groups


def _site_rows(conn):
    links = {}
    for r in _rows(conn, 'SELECT site_key, status, abbreviations FROM site_ext_link'):
        links[r[0]] = {'status': r[1], 'abbreviations': json.loads(r[2]) if r[2] else [], 'studios': []}
    for r in _rows(conn, 'SELECT site_key, ext_source, ext_id, ext_name, ext_parent_id, ext_parent_name, link_kind '
                         'FROM site_ext_studio ORDER BY added_at, ext_name'):
        ln = links.setdefault(r[0], {'status': 'linked', 'abbreviations': [], 'studios': []})
        ln['studios'].append({'ext_source': r[1], 'ext_id': r[2], 'ext_name': r[3], 'ext_parent_id': r[4],
                              'ext_parent_name': r[5], 'link_kind': r[6]})
    for ln in links.values():
        ln['status'] = 'linked' if ln['studios'] else (ln['status'] if ln['status'] == 'not_on_db' else 'unlinked')
        first = ln['studios'][0] if ln['studios'] else {}
        ln['ext_name'] = ', '.join(x['ext_name'] for x in ln['studios']) or None    # for display / filtering
        ln['link_kind'] = first.get('link_kind')
    checks = {r[0]: {'outcome': r[1], 'detail': r[2]} for r in
              _rows(conn, 'SELECT site_key, outcome, detail FROM site_ext_check')}
    return links, checks


def _save_site_link(conn, key, source, pick, kind):
    """Add a studio to a site (a site can have several)."""
    old = conn.execute('SELECT abbreviations FROM site_ext_link WHERE site_key = ?', (key,)).fetchone()
    conn.execute("INSERT OR REPLACE INTO site_ext_link (site_key, status, abbreviations, updated_at) "
                 "VALUES (?, 'linked', ?, CURRENT_TIMESTAMP)", (key, old[0] if old else None))
    conn.execute('INSERT OR REPLACE INTO site_ext_studio (site_key, ext_source, ext_id, ext_name, ext_parent_id, '
                 'ext_parent_name, link_kind) VALUES (?, ?, ?, ?, ?, ?, ?)',
                 (key, source, pick['ext_id'], pick['name'], (pick.get('parent') or {}).get('id'),
                  (pick.get('parent') or {}).get('name'), kind))
    conn.commit()


class SiteBody(BaseModel):
    site: str                    # any variant of the site value
    query: Optional[str] = None  # custom search text, a studio link, or a StashDB studio ID
    source: Optional[str] = None


class SiteLinkBody(BaseModel):
    site: str
    ext_id: str
    as_network: bool = False
    source: Optional[str] = None


class SiteAbbrevBody(BaseModel):
    site: str
    abbreviations: list = []


@router.get('/sites')
def sites_list(model=Depends(get_model)):
    """All your sites (grouped variants) with link status. Local only."""
    try:
        ensure_schema()
        conn = _conn()
        groups = _site_groups(conn)
        links, checks = _site_rows(conn)
        out = []
        for k, g in groups.items():
            ln = links.get(k)
            out.append({'site_key': k, 'display': g['display'], 'variants': sorted(g['values']),
                        'videos': g['videos'], 'is_network': g['is_network'],
                        'status': ln['status'] if ln else 'unlinked', 'link': ln,
                        'check': checks.get(k), 'has_candidates': k in _site_cands})
        out.sort(key=lambda x: -x['videos'])
        total = sum(x['videos'] for x in out) or 1
        covered = sum(x['videos'] for x in out if x['status'] == 'linked')
        return {'success': True, 'sites': out,
                'stats': {'sites': len(out), 'linked': sum(x['status'] == 'linked' for x in out),
                          'not_on_db': sum(x['status'] == 'not_on_db' for x in out),
                          'videos_covered': covered, 'videos_total': total}}
    except Exception as e:
        return _err(e)


@router.post('/sites/search')
def sites_search(body: SiteBody, model=Depends(get_model)):
    """User-initiated studio search for one site. Accepts a custom term, a
    stashdb.org/studios/<id> link, or a studio ID."""
    try:
        ensure_schema()
        source = _source(body.source)
        k = site_key(body.site)
        q = (body.query or '').strip()
        m = re.search(r'(' + _UUID + ')', q) if q else None
        if m:
            st = find_studio(source, m.group(1))
            outcome, pick, cands = 'exact', _studio_public(st, 'ID'), [_studio_public(st, 'ID')]
        elif q:
            res = search_studios(source, q)
            outcome, pick, cands = classify_site(q, res)
        else:
            g = _site_groups(_conn()).get(k)
            outcome, pick, cands = check_site(source, body.site, sorted(g['values']) if g else None)
        _site_cands[k] = {'source': source, 'outcome': outcome, 'pick': pick, 'cands': cands}
        return {'success': True, 'site_key': k, 'outcome': outcome, 'pick': pick, 'candidates': cands,
                'cached': False}
    except Exception as e:
        return _err(e)


@router.get('/sites/candidates')
def sites_candidates(site: str = Query(...), model=Depends(get_model)):
    """Candidates remembered from the last check (no request). Falls back to a search."""
    k = site_key(site)
    c = _site_cands.get(k)
    if c:
        return {'success': True, 'site_key': k, 'outcome': c['outcome'], 'pick': c['pick'],
                'candidates': c['cands'], 'cached': True}
    return sites_search(SiteBody(site=site), model)


@router.post('/sites/link')
def sites_link(body: SiteLinkBody, model=Depends(get_model)):
    try:
        ensure_schema()
        source = _source(body.source)
        conn = _conn()
        k = site_key(body.site)
        st = find_studio(source, body.ext_id)
        pick = _studio_public(st, 'chosen')
        _save_site_link(conn, k, source, pick, 'network' if body.as_network else 'studio')
        conn.execute("INSERT OR REPLACE INTO site_ext_check (site_key, ext_source, outcome, detail, checked_at) "
                     "VALUES (?, ?, 'linked', ?, CURRENT_TIMESTAMP)", (k, source, 'Linked manually: ' + st['name']))
        conn.commit()
        links, _ = _site_rows(conn)
        return {'success': True, 'site_key': k, 'link': links.get(k)}
    except Exception as e:
        return _err(e)


class SiteStudioBody(BaseModel):
    site: str
    ext_id: str


@router.post('/sites/unlink-studio')
def sites_unlink_studio(body: SiteStudioBody, model=Depends(get_model)):
    """Remove one studio from a site (the others stay linked)."""
    try:
        ensure_schema()
        conn = _conn()
        k = site_key(body.site)
        conn.execute('DELETE FROM site_ext_studio WHERE site_key = ? AND ext_id = ?', (k, body.ext_id))
        left = conn.execute('SELECT COUNT(*) FROM site_ext_studio WHERE site_key = ?', (k,)).fetchone()[0]
        if not left:
            conn.execute("UPDATE site_ext_link SET status = 'unlinked', updated_at = CURRENT_TIMESTAMP WHERE site_key = ?", (k,))
        conn.commit()
        links, _ = _site_rows(conn)
        return {'success': True, 'site_key': k, 'link': links.get(k)}
    except Exception as e:
        return _err(e)


@router.post('/sites/not-on-db')
def sites_not_on_db(body: SiteBody, model=Depends(get_model)):
    try:
        ensure_schema()
        conn = _conn()
        k = site_key(body.site)
        old = conn.execute('SELECT abbreviations FROM site_ext_link WHERE site_key = ?', (k,)).fetchone()
        conn.execute("INSERT OR REPLACE INTO site_ext_link (site_key, status, abbreviations, updated_at) "
                     "VALUES (?, 'not_on_db', ?, CURRENT_TIMESTAMP)", (k, old[0] if old else None))
        conn.execute('DELETE FROM site_ext_studio WHERE site_key = ?', (k,))
        conn.commit()
        _site_cands.pop(k, None)
        return {'success': True, 'site_key': k}
    except Exception as e:
        return _err(e)


@router.post('/sites/clear')
def sites_clear(body: SiteBody, model=Depends(get_model)):
    """Remove the link (or 'not on StashDB' mark). Abbreviations are kept."""
    try:
        ensure_schema()
        conn = _conn()
        k = site_key(body.site)
        old = conn.execute('SELECT abbreviations FROM site_ext_link WHERE site_key = ?', (k,)).fetchone()
        conn.execute('DELETE FROM site_ext_link WHERE site_key = ?', (k,))
        conn.execute('DELETE FROM site_ext_studio WHERE site_key = ?', (k,))
        if old and old[0] and json.loads(old[0]):
            conn.execute("INSERT INTO site_ext_link (site_key, status, abbreviations) VALUES (?, 'unlinked', ?)", (k, old[0]))
        conn.execute('DELETE FROM site_ext_check WHERE site_key = ?', (k,))
        conn.commit()
        return {'success': True, 'site_key': k}
    except Exception as e:
        return _err(e)


@router.post('/sites/abbreviations')
def sites_abbreviations(body: SiteAbbrevBody, model=Depends(get_model)):
    """Your own filename abbreviations for a site, e.g. 'bgf' or 'tog'."""
    try:
        ensure_schema()
        conn = _conn()
        k = site_key(body.site)
        abbr = sorted({norm_name(a) for a in (body.abbreviations or []) if norm_name(a)})
        row = conn.execute('SELECT status FROM site_ext_link WHERE site_key = ?', (k,)).fetchone()
        if row:
            conn.execute('UPDATE site_ext_link SET abbreviations = ?, updated_at = CURRENT_TIMESTAMP WHERE site_key = ?',
                         (json.dumps(abbr), k))
        else:
            conn.execute("INSERT INTO site_ext_link (site_key, status, abbreviations) VALUES (?, 'unlinked', ?)",
                         (k, json.dumps(abbr)))
        conn.commit()
        return {'success': True, 'site_key': k, 'abbreviations': abbr}
    except Exception as e:
        return _err(e)


# ---------- bulk auto-link (exact + network only) ----------
def _site_worker(keys, groups, source, delay):
    global _pace_interval
    st = _site_job
    _pace_interval = delay
    try:
        conn = _conn()
        for i, k in enumerate(keys):
            if st.get('stop'):
                st['stopped'] = True
                break
            g = groups[k]
            st['current'] = g['display']
            try:
                outcome, pick, cands = check_site(source, g['display'], sorted(g['values']))
            except ExtDBError as e:
                if 'api key' in str(e).lower():
                    st['error'] = str(e)
                    break
                if 'rate limit' in str(e).lower():
                    st['note'] = 'Rate limited - pausing 60 s'
                    end = time.monotonic() + 60
                    while time.monotonic() < end and not st.get('stop'):
                        time.sleep(0.5)
                    st['note'] = ''
                outcome, pick, cands = 'error', None, []
            detail = ''
            if outcome in ('exact', 'network') and pick:
                _save_site_link(conn, k, source, pick, 'network' if outcome == 'network' else 'studio')
                detail = pick['name'] + (f" (part of {pick['parent']['name']})" if pick.get('parent') else '')
            else:
                _site_cands[k] = {'source': source, 'outcome': outcome, 'pick': pick, 'cands': cands}
                detail = ' | '.join(c['name'] for c in cands[:3])
            conn.execute('INSERT OR REPLACE INTO site_ext_check (site_key, ext_source, outcome, detail, checked_at) '
                         'VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)', (k, source, outcome, detail))
            conn.commit()
            st['counts'][outcome] = st['counts'].get(outcome, 0) + 1
            st['done'] = i + 1
    except Exception as e:
        st['error'] = f'Unexpected error: {e}'
    finally:
        _pace_interval = 0.0
        st['running'] = False
        st['current'] = None
        st['finished_at'] = time.time()


class SiteBulkBody(BaseModel):
    limit: int = 0            # 0 = every unlinked, unchecked site; otherwise the top N by videos
    recheck: bool = False     # also re-check sites that were already checked
    delay: float = 1.0
    source: Optional[str] = None


@router.post('/sites/bulk/start')
def sites_bulk_start(body: SiteBulkBody, model=Depends(get_model)):
    try:
        ensure_schema()
        source = _source(body.source)
        if not api_key(source):
            raise ExtDBError(f"No API key configured for {SOURCES[source]['label']}")
        with _site_lock:
            if _site_job.get('running') or _bulk.get('running'):
                raise ExtDBError('Another bulk job is running - wait for it to finish')
            conn = _conn()
            groups = _site_groups(conn)
            links, checks = _site_rows(conn)
            keys = [k for k, g in sorted(groups.items(), key=lambda kv: -kv[1]['videos'])
                    if (links.get(k) or {}).get('status') not in ('linked', 'not_on_db')
                    and (body.recheck or k not in checks)]
            if body.limit and body.limit > 0:
                keys = keys[:body.limit]
            if not keys:
                raise ExtDBError('No unlinked sites left to check')
            delay = min(max(float(body.delay), 0.5), 10.0)
            _site_job.clear()
            _site_job.update({'running': True, 'stop': False, 'stopped': False, 'total': len(keys), 'done': 0,
                              'counts': {}, 'current': None, 'note': '', 'error': None,
                              'started_at': time.time(), 'finished_at': None})
            threading.Thread(target=_site_worker, args=(keys, groups, source, delay), daemon=True,
                             name='extdb-sites').start()
        return {'success': True, 'status': _site_status()}
    except Exception as e:
        return _err(e)


def _site_status():
    st = dict(_site_job)
    st.pop('stop', None)
    if st.get('started_at'):
        end = st.get('finished_at') or time.time()
        st['elapsed_seconds'] = round(end - st['started_at'])
        d, t = st.get('done', 0), st.get('total', 0)
        st['eta_seconds'] = round((end - st['started_at']) / d * (t - d)) if st.get('running') and d else None
    return st


@router.get('/sites/bulk/status')
def sites_bulk_status():
    return {'success': True, 'status': _site_status()}


@router.post('/sites/bulk/stop')
def sites_bulk_stop():
    if _site_job.get('running'):
        _site_job['stop'] = True
    return {'success': True, 'status': _site_status()}


# ---------- libraries: default site ----------
LIB_DOMINANT = 0.80


@router.get('/libraries')
def libraries_list(model=Depends(get_model)):
    """Each library's site make-up, a suggested default site for single-site
    libraries, and the default you've set (if any). Local only."""
    try:
        ensure_schema()
        conn = _conn()
        jcols = [r[1] for r in _rows(conn, 'PRAGMA table_info(video_site_junction)')]
        fk = next((c for c in jcols if c.endswith('_id') and c != 'video_id'), None)
        lib_of = {r[0]: r[1] or '' for r in _rows(conn, 'SELECT video_id, library FROM videos')}
        sites_of = {}
        if fk:
            for r in _rows(conn, f'SELECT j.video_id, s.value FROM video_site_junction j JOIN video_site s ON s.id = j.{fk}'):
                sites_of.setdefault(r[0], set()).add(r[1])
        defaults = {r[0]: r[1] for r in _rows(conn, 'SELECT library, site_value FROM library_default_site')}
        libs = {}
        for vid, lib in lib_of.items():
            L = libs.setdefault(lib, {'videos': 0, 'sited': 0, 'sites': Counter(), 'variants': {}})
            L['videos'] += 1
            if sites_of.get(vid):
                L['sited'] += 1
                for k in {site_key(sv) for sv in sites_of[vid]}:      # spelling variants count as one site
                    L['sites'][k] += 1
                for sv in sites_of[vid]:
                    L['variants'].setdefault(site_key(sv), Counter())[sv] += 1
        out = []
        for lib, L in libs.items():
            name_of = lambda k: L['variants'][k].most_common(1)[0][0]
            sugg = None
            if L['sited'] >= 5:
                strong = [(k, n) for k, n in L['sites'].most_common() if n / L['sited'] >= LIB_DOMINANT]
                if strong:
                    # you tag network + site together: prefer the specific site
                    specific = [x for x in strong if not name_of(x[0]).lower().endswith(NETWORK_SUFFIXES)]
                    k, n = (specific or strong)[0]
                    sugg = {'site': name_of(k), 'share': round(n / L['sited'], 2)}
            out.append({'library': lib, 'videos': L['videos'], 'with_site': L['sited'],
                        'without_site': L['videos'] - L['sited'],
                        'top_sites': [{'site': name_of(k), 'share': round(n / L['sited'], 2)} for k, n in L['sites'].most_common(3)],
                        'suggestion': sugg, 'default_site': defaults.get(lib)})
        out.sort(key=lambda x: -x['videos'])
        return {'success': True, 'libraries': out}
    except Exception as e:
        return _err(e)


class LibraryDefaultBody(BaseModel):
    library: str
    site: Optional[str] = None      # None / '' clears it


@router.post('/libraries/default')
def libraries_default(body: LibraryDefaultBody, model=Depends(get_model)):
    """Set or clear a library's default site (used as a matching clue for its
    videos that have no site; nothing is written to the videos)."""
    try:
        ensure_schema()
        conn = _conn()
        if body.site and body.site.strip():
            conn.execute('INSERT OR REPLACE INTO library_default_site (library, site_value, updated_at) '
                         'VALUES (?, ?, CURRENT_TIMESTAMP)', (body.library, body.site.strip()))
        else:
            conn.execute('DELETE FROM library_default_site WHERE library = ?', (body.library,))
        conn.commit()
        return {'success': True}
    except Exception as e:
        return _err(e)


# ===========================================================================
# Videos -> external scenes
# ===========================================================================
# StashDB scene search behaviour this relies on (stash-box scene/query.go and
# queries/sql/scene.sql):
#   * searchScenes(term): every word is matched against title, code, date,
#     performer names, studio and network names/aliases; scenes matching more
#     words rank higher. A scene ID or a scene URL returns that scene.
#   * queryScenes: studios = exact studio IDs, parentStudio = a studio or any of
#     its sub-studios, date = exact release date, performers = IDs.
SCENE_CACHE_DAYS = 30
SCENE_CATALOG_MAX_PAGES = 20            # 100 scenes per page
SCENE_FRAGMENT = ('fragment SC on Scene { id title release_date date duration code deleted details tags { name } '
                  'studio { id name parent { id name } } '
                  'performers { as performer { id name disambiguation gender } } '
                  'images { url width height } }')
SCENE_FULL_FRAGMENT = ('fragment SF on Scene { id title release_date date duration code deleted details director '
                       'urls { url } tags { id name } '
                       'studio { id name parent { id name } } '
                       'performers { as performer { id name disambiguation gender } } '
                       'images { url width height } }')
_JUNK_TOKENS = {'xxx', 'mp4', 'mkv', 'wmv', 'avi', 'mov', 'm4v', 'hevc', 'h264', 'h265', 'x264', 'x265', 'avc',
                'prt', 'int', 'web', 'webdl', 'dl', 'full', 'en', 'sd', 'hd', 'fhd', 'uhd', 'hq', 'mobile', 'source',
                'split', 'scenes', 'rq', 'ktr', 'xleech', 'gush', 'ipt', 'nbq', 'vsex', 'wrb', 'p2p', 'mp4v', 'hdr', 'fullhd'}
_RES_RE = re.compile(r'^(\d{3,4}p|\d{3,4}x\d{3,4}|[248]k|\d{3,4}k|uhd|\d{3,4}i)$', re.I)


def _scene_gql(source, query, variables):
    data = _post(source, query, variables)
    if data.get('errors'):
        msgs = '; '.join(str(e.get('message', e)) for e in data['errors'])[:400]
        raise ExtDBError(f"{SOURCES[source]['label']} error: {msgs}")
    return data.get('data') or {}


def _scene_compact(sc):
    if not sc:
        return None
    st = sc.get('studio') or {}
    imgs = [i for i in (sc.get('images') or []) if i and i.get('url')]
    land = [i for i in imgs if (i.get('width') or 0) >= (i.get('height') or 0)]
    return {
        'id': sc['id'], 'title': sc.get('title') or '', 'date': sc.get('release_date') or sc.get('date') or '',
        'duration': _int(sc.get('duration')), 'code': sc.get('code') or '',
        'studio_id': st.get('id'), 'studio_name': st.get('name'),
        'parent_id': (st.get('parent') or {}).get('id'), 'parent_name': (st.get('parent') or {}).get('name'),
        'performers': [{'id': (a.get('performer') or {}).get('id'), 'name': (a.get('performer') or {}).get('name'),
                        'as': a.get('as') or None, 'gender': (a.get('performer') or {}).get('gender')}
                       for a in (sc.get('performers') or []) if a and a.get('performer')],
        'image_url': ((land or imgs)[0]['url'] if imgs else None),
        'tags': [t.get('name') for t in (sc.get('tags') or []) if t and t.get('name')],
        'details': (sc.get('details') or '')[:600],
    }


def scenes_search(source, term, per_page=25):
    q = SCENE_FRAGMENT + ' query($t: String!, $n: Int) { searchScenes(term: $t, per_page: $n) { count scenes { ...SC } } }'
    try:
        data = _scene_gql(source, q, {'t': term, 'n': per_page})
        res = (data.get('searchScenes') or {}).get('scenes') or []
    except ExtDBError as e:
        if 'searchScenes' not in str(e):
            raise
        data = _scene_gql(source, SCENE_FRAGMENT + ' query($t: String!, $n: Int) { searchScene(term: $t, limit: $n) { ...SC } }',
                          {'t': term, 'n': per_page})
        res = data.get('searchScene') or []
    return [x for x in (_scene_compact(r) for r in res if r and not r.get('deleted')) if x]


def scenes_query(source, inp, pages=1, per_page=100):
    out = []
    q = SCENE_FRAGMENT + ' query($i: SceneQueryInput!) { queryScenes(input: $i) { count scenes { ...SC } } }'
    for page in range(1, pages + 1):
        data = _scene_gql(source, q, {'i': dict(inp, page=page, per_page=per_page)})
        r = data.get('queryScenes') or {}
        batch = r.get('scenes') or []
        out += [x for x in (_scene_compact(b) for b in batch if b and not b.get('deleted')) if x]
        if len(batch) < per_page or len(out) >= (r.get('count') or 0):
            break
    return out


def scene_find(source, ext_id):
    data = _scene_gql(source, SCENE_FULL_FRAGMENT + ' query($id: ID!) { findScene(id: $id) { ...SF } }', {'id': ext_id})
    sc = data.get('findScene')
    if not sc or sc.get('deleted'):
        raise ExtDBError(f'Scene {ext_id} not found on {SOURCES[source]["label"]}')
    return sc


def performer_catalog(conn, source, performer_id, refresh=False):
    """A linked performer's scene list, from the compact cache when fresh."""
    if not refresh:
        row = conn.execute("SELECT scenes_json FROM ext_scene_catalog WHERE ext_source = ? AND performer_id = ? "
                           "AND fetched_at >= datetime('now', ?)", (source, performer_id, f'-{SCENE_CACHE_DAYS} days')).fetchone()
        if row:
            return json.loads(row[0]), True
    scenes = scenes_query(source, {'performers': {'value': [performer_id], 'modifier': 'INCLUDES'},
                                   'sort': 'DATE', 'direction': 'DESC'}, pages=SCENE_CATALOG_MAX_PAGES)
    conn.execute('INSERT OR REPLACE INTO ext_scene_catalog (ext_source, performer_id, scenes_json, scene_count, fetched_at) '
                 'VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)', (source, performer_id, json.dumps(scenes, ensure_ascii=False), len(scenes)))
    conn.commit()
    return scenes, False


# ---------- clues from your metadata, library default and filename ----------
_HYPHEN_APOS = [(r"(?<=[A-Za-z])-(s)(?=-|\.|$)", r"'\1"),            # it-s -> it's, mom-s -> mom's
                (r"(?<=n)-(t)(?=-|\.|$)", r"'\1"),                    # don-t -> don't, can-t -> can't
                (r"(?<=\b[Ii])-(m)(?=-|\.|$)", r"'\1"),              # i-m -> i'm
                (r"(?<=[A-Za-z])-(ll|re|ve)(?=-|\.|$)", r"'\1")]      # i-ll, you-re, we-ve


def _clean_words(text):
    t = re.sub(r"(?<=[A-Za-z])_(s|m|t|re|ll|ve|d)(?=[_\s.\-]|$)", r"'\1", text or '', flags=re.I)   # Who_s -> Who's
    for pat, repl in _HYPHEN_APOS:
        t = re.sub(pat, repl, t, flags=re.I)
    t = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', t)                    # DarkNight -> Dark Night
    t = re.sub(r'[_\.]+', ' ', t)
    words = [w for w in re.split(r'[^A-Za-z0-9\']+', t) if w]
    return [w for w in words if w.lower() not in _JUNK_TOKENS and not _RES_RE.match(w)]


def _norm_title(t):
    out = []
    t = re.sub(r"['\u2019`]", '', t or '')        # It's / Its / It_s all compare the same
    for w in re.split(r'[^a-z0-9]+', unicodedata.normalize('NFKD', t.lower())):
        if not w:
            continue
        m = re.fullmatch(r's(\d{1,2})', w)           # "S02" -> "scene 2"
        if m:
            out += ['scene', str(int(m.group(1)))]
        elif w.isdigit():
            out.append(str(int(w)))                  # "#03" / "03" -> "3"
        elif w not in ('the', 'a', 'an', 'and', 'of'):
            out.append(w)
    return out


def _site_hint(raw, site_keys):
    k = norm_name(re.sub(r'\.(com|net|org|xxx|tv|co|uk)$', '', (raw or '').strip(), flags=re.I))
    return site_keys.get(k) or k


def _names_or_title(text, cast_idx):
    """'Wade County and Nicole Kidd' / 'sandra_romain_640_big' -> (names, leftover title)."""
    parts = [x.strip() for x in re.split(r',|&|\band\b', text or '') if x.strip()]
    if parts and all(norm_name(x) in cast_idx for x in parts):
        return [cast_idx[norm_name(x)] for x in parts], ''
    toks = [t.lower() for t in _clean_words(text)]
    names, rest = _names_from_tokens(toks, cast_idx)
    return names, ' '.join(rest) if names else ' '.join(_clean_words(text))


def _yy(y):
    y = int(y)
    if y >= 100:
        return y
    return 2000 + y if y <= (date.today().year % 100) + 1 else 1900 + y


def _valid_date(y, m, d):
    try:
        return date(_yy(y), int(m), int(d)).isoformat()
    except (ValueError, TypeError):
        return None


def _cast_index(conn):
    """normalised name -> display name, for every cast value and alias you have."""
    idx = {}
    for r in _rows(conn, 'SELECT value FROM video_cast'):
        idx.setdefault(norm_name(r[0]), r[0])
    for r in _rows(conn, 'SELECT alias_name, primary_name FROM cast_aliases'):
        idx.setdefault(norm_name(r[0]), r[1])
    return idx


def _names_from_tokens(tokens, cast_idx):
    """Greedy: longest run of 2-4 tokens that is a known cast name. -> (names, remaining tokens)"""
    names, rest, i = [], [], 0
    while i < len(tokens):
        hit = None
        for n in (4, 3, 2):
            if i + n <= len(tokens):
                k = norm_name(''.join(tokens[i:i + n]))
                if k in cast_idx:
                    hit = (n, cast_idx[k])
                    break
        if hit:
            names.append(hit[1])
            i += hit[0]
        else:
            rest.append(tokens[i])
            i += 1
    return names, rest


_SIZE_TAGS = r'(?:big|med|medium|sm|small|ph|lg|large|mobile|hi|lo|hq|lq)'


def _title_is_noise(title, names):
    """True when a 'title' is only the performer's name and/or numbers ('tina kay 2', '640')."""
    name_toks = {w for n in names for w in re.split(r'[^a-z0-9]+', n.lower()) if w}
    rest = [t for t in re.split(r'[^a-z0-9]+', (title or '').lower()) if t and t not in name_toks]
    return all(t.isdigit() for t in rest)


def _release_tokens(text):
    """'jane.doe.xxx.1080p.mp4-group' -> ['jane', 'doe']: words up to the release tags."""
    raw = [t for t in re.split(r'[._ -]+', text or '') if t]
    cut = next((i for i, t in enumerate(raw) if i and (t.lower() == 'xxx' or _RES_RE.match(t))), len(raw))
    return [t for t in raw[:cut] if t.lower() not in _JUNK_TOKENS and not _RES_RE.match(t)]


def parse_filename(stem, cast_idx, site_keys):
    """-> dict(style, site_hint, date, names, title). site_keys: normalised site key/abbreviation -> site key."""
    out = {'style': 'other', 'site_hint': None, 'date': None, 'names': [], 'title': ''}
    # download-size tags after a resolution: "..._720p_big", "..._480p_med", "..._640_sm", "..._720p_ph"
    s = re.sub(r'([._ -](?:\d{3,4}p?|[248]k))[._ -]' + _SIZE_TAGS + r'$', r'\1', stem.strip(), flags=re.I)
    m = re.match(r'^([A-Za-z0-9]+)[._ -]((?:19|20)\d{2}|\d{2})[._ -](\d{2})[._ -](\d{2})(?:[._ -](.*))?$', s)
    if m and _valid_date(m.group(2), m.group(3), m.group(4)):
        out.update(style='release', date=_valid_date(m.group(2), m.group(3), m.group(4)))
        out['site_hint'] = _site_hint(m.group(1), site_keys)
        toks = _release_tokens(m.group(5))
        out['tokens'] = [t.lower() for t in toks]
        out['names'], rest = _names_from_tokens(out['tokens'], cast_idx)
        out['title'] = ' '.join(rest)
        return out
    m = re.match(r'^([A-Za-z0-9]+)[._ -]e(\d{1,5})[._ -](.*)$', s, re.I)
    if m:
        out.update(style='episode')
        out['site_hint'] = _site_hint(m.group(1), site_keys)
        toks = _release_tokens(m.group(3))
        out['tokens'] = [t.lower() for t in toks]
        out['names'], rest = _names_from_tokens(out['tokens'], cast_idx)
        out['title'] = ' '.join(rest)
        return out
    m = re.match(r'^\s*[\[(]([^\])]+)[\])]\s*(.*)$', s)
    if m:
        out.update(style='bracket', site_hint=_site_hint(m.group(1), site_keys))
        s = m.group(2)
        d = re.search(r'[\[(]?((?:19|20)\d{2})[._ -](\d{2})[._ -](\d{2})[\])]?', s)
        if d and _valid_date(*d.groups()):
            out['date'] = _valid_date(*d.groups())
            s = (s[:d.start()] + ' ' + s[d.end():]).strip()
    d = re.search(r'((?:19|20)\d{2})[._ -](\d{2})[._ -](\d{2})', s)
    if d and not out['date'] and _valid_date(*d.groups()):
        out['date'] = _valid_date(*d.groups())
    if ' - ' in s:
        left, right = s.split(' - ', 1)
        if out['style'] == 'other':
            out['style'] = 'dash'
        cand = [x.strip() for x in re.split(r',|&| and ', left) if x.strip()]
        known = [cast_idx.get(norm_name(c), c) for c in cand]
        if right and (any(norm_name(c) in cast_idx for c in cand) or len(cand) > 1 or len(left.split()) <= 3):
            out['names'] = known
            s = right
        m2 = re.match(r'^([A-Za-z0-9]+)_s\d{2}_', s)                         # "SceneTitle_s01_..." style
        if m2:
            s = m2.group(1)
        out['title'] = ' '.join(_clean_words(s))
        if _title_is_noise(out['title'], out['names']):                    # "Jane Doe - jane_doe_2_720p_big"
            out['title'] = ''
        return out
    # no "Name - Title" split: the rest may be just performer names
    names, title = _names_or_title(s, cast_idx)
    out['names'] = out['names'] or names
    out['title'] = '' if (out['names'] and _title_is_noise(title, out['names'])) else title   # "tina_kay_2" -> no title
    return out


def movie_scene(title):
    """'Gag On This 13 Scene 1' -> ('Gag On This 13', 1); 'Deep Strokes S02' -> ('Deep Strokes', 2)."""
    t = (title or '').strip()
    m = re.match(r'^(.*\S)\s+(?:scene|sc)\s*#?\s*(\d{1,2})$', t, re.I) or re.match(r'^(.*\S)\s+s(\d{1,2})$', t, re.I)
    if m and len(m.group(1)) >= 3:
        return m.group(1).strip(' -,'), int(m.group(2))
    return None, None


def _junction_fk(conn, field):
    jc = [r[1] for r in _rows(conn, f'PRAGMA table_info(video_{field}_junction)')]
    return next((c for c in jc if c.endswith('_id') and c != 'video_id'), None)


def _video_field_values(conn, video_id, field):
    """One video's values for a list field (video_<field> + junction)."""
    fk = _junction_fk(conn, field)
    if not fk:
        return []
    return [r[0] for r in _rows(conn, f'SELECT t.value FROM video_{field}_junction j JOIN video_{field} t ON t.id = j.{fk} '
                                      f'WHERE j.video_id = ? ORDER BY t.value', (video_id,))]


def video_clues(conn, path):
    """Everything local we know about a video that helps find its scene."""
    row = conn.execute('SELECT fv.video_id, fv.filename, fv.length_seconds, v.title, v.year, v.library, fv.cache_id '
                       'FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id WHERE fv.path = ?', (path,)).fetchone()
    if not row:
        raise ExtDBError('Video not found in the library database - it may need a rescan')
    vid, filename, secs, title, year, library = row[0], row[1], row[2], row[3], row[4], row[5]
    cache_id = row[6]
    if not cache_id:
        try:
            from media_handler import MediaHandler
            cache_id = MediaHandler._get_cache_key(path)
        except Exception:
            cache_id = None
    jvals = lambda field: _video_field_values(conn, vid, field)
    cast_idx = _cast_index(conn)
    groups = _site_groups(conn)
    links, _ = _site_rows(conn)
    site_keys = {}
    for k in groups:
        site_keys[k] = k
    for k, ln in links.items():
        for ab in ln.get('abbreviations') or []:
            site_keys.setdefault(ab, k)
    stem = re.sub(r'\.[A-Za-z0-9]{2,4}$', '', filename or '')
    import filename_parser                       # the smart parser (sites, performers, dates, titles)
    fn = filename_parser.clues(filename or '', filename_parser.ParseContext(
        cast_idx, {norm_name(k): v for k, v in site_keys.items()}, {k: g['display'] for k, g in groups.items()}))

    # cast: metadata first, then names found in the filename
    cast_vals = jvals('cast')
    src_cast = 'metadata' if cast_vals else 'filename'
    names = cast_vals or fn['names']
    ext_links = {r[0]: r[1] for r in _rows(conn, 'SELECT cast_name, ext_id FROM cast_ext_link')}
    cast = []
    for n in names:
        prim = resolve_primary(conn, n)
        if any(c['name'] == prim for c in cast):
            continue
        cast.append({'name': prim, 'ext_id': ext_links.get(prim)})

    # site: metadata, else library default, else filename prefix
    site_vals = jvals('site')
    site_src = 'metadata'
    if not site_vals:
        d = conn.execute('SELECT site_value FROM library_default_site WHERE library = ?', (library,)).fetchone()
        if d:
            site_vals, site_src = [d[0]], 'library default'
    if not site_vals and fn['site_hint']:
        k = fn['site_hint']
        site_vals = [groups[k]['display']] if k in groups else [k]
        site_src = 'filename'
    sites = []
    for sv in site_vals:
        k = site_key(sv)
        ln = links.get(k) or {}
        sites.append({'value': sv, 'site_key': k, 'status': ln.get('status', 'unlinked'),
                      'studios': ln.get('studios') or []})

    # title: filename title part; a metadata title you edited yourself wins
    t_meta = (title or '').strip()
    t_title = fn['title']
    if t_meta and norm_name(t_meta) not in (norm_name(stem), norm_name(fn['title'])) and ' - ' not in t_meta:
        t_title = t_meta
    movie_title, scene_no = movie_scene(t_title)
    return {'video_id': vid, 'path': path, 'filename': filename, 'stem': stem, 'library': library, 'style': fn['style'],
            'cache_id': cache_id,
            'title': t_title, 'movie_title': movie_title, 'scene_number': scene_no,
            'date': fn['date'], 'year': str(year) if year else (fn['date'] or '')[:4] or None,
            'duration': int(secs) if secs else None, 'cast': cast, 'cast_source': src_cast,
            'sites': sites, 'site_source': site_src if site_vals else None}


# ---------- scoring ----------
def _title_similarity(a, b):
    ta, tb = _norm_title(a), _norm_title(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb)
    contain = len(sa & sb) / min(len(sa), len(sb))
    import difflib
    ratio = difflib.SequenceMatcher(None, ''.join(ta), ''.join(tb)).ratio()
    sim = max(jac, ratio, contain * 0.9 if min(len(sa), len(sb)) >= 2 else 0)
    na_, nb_ = {w for w in ta if w.isdigit()}, {w for w in tb if w.isdigit()}
    if na_ and nb_ and na_ != nb_:          # "Scene 1" vs "Scene 2", "Vol 3" vs "Vol 4": different scenes
        sim = min(sim, 0.5)
    elif bool(na_) != bool(nb_):            # "Gag On This 13" vs "Gag on This": probably another volume
        sim = min(sim, 0.7)
    return sim


def score_scene(sc, clues):
    pts, reasons = 0.0, []
    if clues['title'] and sc['title']:
        mine = (clues.get('movie_title') or clues['title']) if sc.get('kind') == 'movie' else clues['title']
        sim = _title_similarity(mine, sc['title'])
        pts += (45 if sc.get('kind') == 'movie' else 40) * sim
        if sim >= 0.85:
            reasons.append('title matches')
        elif sim >= 0.5:
            reasons.append(f'title similar ({round(sim * 100)}%)')
    no_title = not clues['title']
    w_perf, w_studio, w_len, w_len_near = (40, 25, 25, 13) if no_title else (30, 15, 15, 8)
    scene_pids = {p['id'] for p in sc['performers']}
    scene_names = {norm_name(p['name']) for p in sc['performers']} | {norm_name(p['as']) for p in sc['performers'] if p['as']}
    if clues['cast']:
        hit = [c['name'] for c in clues['cast'] if (c['ext_id'] and c['ext_id'] in scene_pids) or norm_name(c['name']) in scene_names]
        if hit:
            pts += w_perf * len(hit) / len(clues['cast'])
            reasons.append('performer' + ('s' if len(hit) > 1 else '') + ': ' + ', '.join(hit))
        else:
            pts -= 15
            reasons.append('none of your cast in this scene')
    studio_ids = {st['ext_id'] for s_ in clues['sites'] for st in s_['studios'] if st.get('link_kind') != 'network'}
    network_ids = {st['ext_id'] for s_ in clues['sites'] for st in s_['studios'] if st.get('link_kind') == 'network'}
    if sc.get('source') == 'tpdb':
        # ThePornDB has its own studio IDs: compare names, reward a match, never penalise
        names = {norm_name(s_['value']) for s_ in clues['sites']} | {norm_name(st['ext_name']) for s_ in clues['sites'] for st in s_['studios']}
        if names & {norm_name(sc.get('studio_name')), norm_name(sc.get('parent_name'))} - {''}:
            pts += 10
            reasons.append('studio: ' + (sc['studio_name'] or ''))
    elif studio_ids or network_ids:
        if sc['studio_id'] in studio_ids or sc['studio_id'] in network_ids or sc['parent_id'] in network_ids:
            pts += w_studio
            reasons.append('studio: ' + (sc['studio_name'] or ''))
        elif sc['studio_id']:
            pts -= 10
            reasons.append('different studio: ' + (sc['studio_name'] or ''))
    if clues['date'] and sc['date']:
        if sc['date'] == clues['date']:
            pts += 20
            reasons.append('same date')
        elif sc['date'][:4] == clues['date'][:4]:
            pts += 3
        else:
            pts -= 10
            reasons.append('different date')
    elif clues['year'] and sc['date'] and sc['date'][:4] == str(clues['year'])[:4]:
        pts += 5
    if sc.get('kind') == 'movie':
        n = clues.get('scene_number')
        if n and sc.get('scenes') and 1 <= n <= len(sc['scenes']):
            pts += 5
            reasons.append(f'movie lists scene {n}')
    elif clues['duration'] and sc['duration']:
        diff = abs(sc['duration'] - clues['duration'])
        if diff <= 5:
            pts += w_len
            reasons.append('same length')
        elif diff <= 30:
            pts += w_len_near
            reasons.append(f'length within {diff}s')
        elif diff > 120 and diff > 0.1 * sc['duration']:
            pts -= 10
            reasons.append(f'length differs by {diff // 60} min')
    return max(0, min(100, round(pts))), reasons


TPDB_API = os.environ.get('TPDB_API_URL', 'https://api.theporndb.net')


def tpdb_get(path, params=None):
    """ThePornDB REST API (same token as TPDB_API_KEY, sent as a Bearer token)."""
    key = api_key('tpdb')
    if not key:
        raise ExtDBError('No ThePornDB API key configured (set TPDB_API_KEY)')
    _pace()
    try:
        r = requests.get(TPDB_API + path, params=params or {}, timeout=TIMEOUT,
                         headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json', 'User-Agent': USER_AGENT})
    except requests.RequestException as e:
        raise ExtDBError(f'Could not reach ThePornDB: {e}')
    if r.status_code in (401, 403):
        raise ExtDBError(f'ThePornDB rejected the API key (HTTP {r.status_code})')
    if r.status_code == 429:
        raise ExtDBError('ThePornDB rate limit reached - wait a minute and retry')
    if r.status_code == 404:
        return {}
    try:
        return r.json()
    except ValueError:
        raise ExtDBError(f'ThePornDB returned HTTP {r.status_code} (not JSON)')


def _tpdb_performer(p):
    par = p.get('parent') or {}
    credited, canonical = p.get('name'), par.get('name') or p.get('name')
    gender = (par.get('extra') or {}).get('gender') or (p.get('extra') or {}).get('gender')
    return {'id': par.get('id') or p.get('id'), 'name': canonical,
            'as': credited if credited and norm_name(credited) != norm_name(canonical) else None,
            'gender': (gender or '').upper() or None}


def _tpdb_image(x):
    for k in ('image', 'poster', 'poster_image'):
        if x.get(k):
            return x[k]
    for k in ('posters', 'background'):
        d = x.get(k) or {}
        if d.get('medium') or d.get('large'):
            return d.get('medium') or d.get('large')
    return None


def _tpdb_compact(x, kind):
    site = x.get('site') or {}
    par = site.get('parent') or site.get('network') or {}
    scenes = [{'id': sc_.get('id'), 'title': sc_.get('title') or '', 'duration': _int(sc_.get('duration')),
               'performers': [_tpdb_performer(p) for p in (sc_.get('performers') or []) if p]}
              for sc_ in (x.get('scenes') or []) if sc_]
    return {'id': x.get('id'), 'source': 'tpdb', 'kind': kind, 'title': x.get('title') or '', 'date': x.get('date') or '',
            'duration': _int(x.get('duration')), 'code': x.get('sku') or '',
            'studio_id': str(site.get('id') or '') or None, 'studio_name': site.get('name'),
            'parent_id': None, 'parent_name': par.get('name') if par.get('name') != site.get('name') else None,
            'performers': [_tpdb_performer(p) for p in (x.get('performers') or []) if p],
            'image_url': _tpdb_image(x), 'scenes': scenes,
            'tags': [t.get('name') for t in (x.get('tags') or []) if isinstance(t, dict) and t.get('name')],
            'details': (x.get('description') or '')[:600],
            'page_url': f"https://theporndb.net/{'movies' if kind == 'movie' else 'scenes'}/{x.get('slug') or x.get('id')}"}


def tpdb_search(kind, text, per_page=10):
    js = tpdb_get('/movies' if kind == 'movie' else '/scenes', {'parse': text, 'per_page': per_page})
    return [_tpdb_compact(x, kind) for x in (js.get('data') or []) if x and x.get('id')]


def tpdb_find(kind, ext_id):
    js = tpdb_get(('/movies/' if kind == 'movie' else '/scenes/') + str(ext_id))
    d = js.get('data')
    if not d:
        raise ExtDBError(f'{kind.capitalize()} {ext_id} not found on ThePornDB')
    return d


def _movie_scene_pick(movie, clues):
    """For a movie that lists its scenes: the scene for this file (by number,
    confirmed by performer), or the one your performer is in."""
    scenes = movie.get('scenes') or []
    if not scenes:
        return None, None
    mine = {norm_name(c['name']) for c in clues['cast']}
    has_me = lambda sc_: bool(mine & ({norm_name(p['name']) for p in sc_['performers']} | {norm_name(p['as']) for p in sc_['performers'] if p['as']}))
    n = clues.get('scene_number')
    if n and 1 <= n <= len(scenes) and (not mine or has_me(scenes[n - 1])):
        return n, scenes[n - 1]
    hits = [(i + 1, sc_) for i, sc_ in enumerate(scenes) if has_me(sc_)]
    if len(hits) == 1:
        return hits[0]
    return n, (scenes[n - 1] if n and 1 <= n <= len(scenes) else None)


def find_scene_candidates(conn, source, clues, query=None, which='both'):
    """-> (scored candidate list, strategy notes). which: 'both' | 'stashdb' | 'tpdb'"""
    found, notes = {}, []

    def add(scenes, how):
        for sc in scenes:
            if sc['id'] not in found:
                found[sc['id']] = (sc, set())
            found[sc['id']][1].add(how)

    which = which if which in ('stashdb', 'tpdb') else 'both'
    if which == 'tpdb' and not api_key('tpdb'):
        raise ExtDBError('ThePornDB is not configured (set TPDB_API_KEY)')
    tpdb_on = bool(api_key('tpdb')) and which != 'stashdb'
    use_stash = which != 'tpdb'
    q = (query or '').strip()
    if q:
        m = re.search(r'(' + _UUID + ')', q)
        term = m.group(1) if m else q
        if use_stash and 'theporndb' not in q.lower():
            add(scenes_search(source, term), 'your search')
        if tpdb_on:
            try:
                add(tpdb_search('scene', q), 'ThePornDB scene search')
                add(tpdb_search('movie', q), 'ThePornDB movie search')
            except ExtDBError as e:
                notes.append(f'ThePornDB: {e}')
        notes.append(f'search: {q}')
    elif use_stash or tpdb_on:
        studios = [st['ext_id'] for s_ in clues['sites'] for st in s_['studios'] if st.get('link_kind') != 'network']
        networks = [st['ext_id'] for s_ in clues['sites'] for st in s_['studios'] if st.get('link_kind') == 'network']
        linked = [c['ext_id'] for c in clues['cast'] if c['ext_id']]
        if not use_stash:
            studios, networks, linked = [], [], []
        if use_stash and clues['date']:
            if studios:
                add(scenes_query(source, {'studios': {'value': studios, 'modifier': 'INCLUDES'},
                                          'date': {'value': clues['date'], 'modifier': 'EQUALS'}}), 'studio + date')
                notes.append('studio + date')
            for nid in networks[:3]:
                add(scenes_query(source, {'parentStudio': nid, 'date': {'value': clues['date'], 'modifier': 'EQUALS'}}), 'network + date')
                notes.append('network + date')
            if linked and not studios and not networks:
                add(scenes_query(source, {'performers': {'value': linked[:3], 'modifier': 'INCLUDES'},
                                          'date': {'value': clues['date'], 'modifier': 'EQUALS'}}), 'performer + date')
                notes.append('performer + date')
        names = [c['name'] for c in clues['cast'][:2]]
        studio_word = next((st['ext_name'] for s_ in clues['sites'] for st in s_['studios'] if st.get('link_kind') != 'network'), None) \
            or next((st['ext_name'] for s_ in clues['sites'] for st in s_['studios']), None) \
            or next((s_['value'].replace('.', ' ').replace('_', ' ') for s_ in clues['sites'] if s_.get('status') != 'not_on_db'), None)
        searches = []
        if clues['title']:
            searches.append(' '.join(names + [clues['title']]))
            if names and studio_word:
                searches.append(' '.join(names + [studio_word]))       # so a noisy title can't bury the scene
        else:
            searches.append(' '.join(names + ([studio_word] if studio_word else []) + ([clues['date']] if clues['date'] else [])))
        for words in searches:
            if use_stash and words.strip():
                add(scenes_search(source, words), 'word search')
                notes.append(f'word search: {words}')
        for pid in linked[:2]:
            try:
                cat, cached = performer_catalog(conn, source, pid)
                name = next((c['name'] for c in clues['cast'] if c['ext_id'] == pid), pid)
                notes.append(f"{name}'s {len(cat)} scenes" + (' (cached)' if cached else ''))
                scored = sorted(cat, key=lambda sc: -score_scene(sc, clues)[0])[:15]
                add(scored, "performer's scenes")
            except ExtDBError as e:
                notes.append(f'performer scenes unavailable: {e}')
        if tpdb_on:
            try:
                # movie search: for "Movie Name Scene N" files, always when ThePornDB is chosen,
                # and as a fallback when StashDB found nothing convincing
                best_so_far = max([score_scene(sc_, clues)[0] for sc_, _ in found.values()] or [0])
                movie_text = clues.get('movie_title') or (clues['title'] if (which == 'tpdb' or best_so_far < 50) else None)
                if movie_text:
                    movies = tpdb_search('movie', movie_text)
                    notes.append(f"ThePornDB movies: {movie_text}" + (f" (scene {clues['scene_number']})" if clues.get('scene_number') else ''))
                    for mv in sorted(movies, key=lambda x: -score_scene(x, clues)[0])[:2]:   # open the best two
                        try:
                            full = _tpdb_compact(tpdb_find('movie', mv['id']), 'movie')
                            mv.update(scenes=full['scenes'], performers=full['performers'] or mv['performers'])
                        except ExtDBError:
                            pass
                    add(movies, 'ThePornDB movie search')
                add(tpdb_search('scene', clues.get('stem') or clues['title']), 'ThePornDB scene search')
                notes.append('ThePornDB scene search')
            except ExtDBError as e:
                notes.append(f'ThePornDB: {e}')
    cur_row = conn.execute('SELECT ext_source, ext_id FROM video_ext_link WHERE video_id = ?', (clues['video_id'],)).fetchone()
    out = []
    for sc, hows in found.values():
        src = sc.get('source') or source
        sc.setdefault('source', src)
        sc.setdefault('kind', 'scene')
        score, reasons = score_scene(sc, clues)
        extra = {}
        if sc['kind'] == 'movie':
            n, picked = _movie_scene_pick(sc, clues)
            extra = {'scene_number': n or clues.get('scene_number'), 'scene_title': (picked or {}).get('title'),
                     'scene_performers': (picked or {}).get('performers')}
            if picked:
                reasons = [x for x in reasons if not x.startswith('movie lists scene')] + [f"scene {n} is " + (picked.get('title') or 'listed')[:40]]
        others = [] if sc['kind'] == 'movie' else [r[0] for r in _rows(conn,
                  'SELECT fv.path FROM video_ext_link l JOIN file_versions fv ON fv.video_id = l.video_id '
                  'AND fv.is_preferred = 1 WHERE l.ext_source = ? AND l.ext_id = ? AND l.video_id != ?',
                  (src, sc['id'], clues['video_id']))]
        out.append(dict(sc, **extra, score=score, reasons=reasons, found_by=sorted(hows),
                        source_label=SOURCES.get(src, {}).get('label', src),
                        is_current_link=bool(cur_row and cur_row[0] == src and cur_row[1] == sc['id']),
                        linked_to_other=others[:3]))
    out.sort(key=lambda x: (-x['score'], x['date'] or ''))
    return out[:24], notes


# ---------- storing a scene link ----------
def _store_scene(conn, video_id, source, sc, score=None, keep_linked_at=True):
    c = _scene_compact(sc)
    image = download_image(c['image_url']) if c['image_url'] else None
    thumb = None
    if image:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(image[0])); img.load()
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            img.thumbnail((240, 135), Image.Resampling.LANCZOS)          # 16:9 scene cover
            buf = io.BytesIO(); img.save(buf, format='WEBP', quality=THUMB_QUALITY, method=6); thumb = buf.getvalue()
        except Exception as e:
            print(f"⚠ ext_db: scene thumbnail failed: {e}")
    old = conn.execute('SELECT ext_id, linked_at, ext_thumb, match_score FROM video_ext_link WHERE video_id = ?', (video_id,)).fetchone()
    same = bool(old and old[0] == sc['id'])
    if thumb is None and same:
        thumb = old[2]
    vals = (video_id, source, sc['id'], c['title'], c['date'], c['code'], c['duration'], sc.get('director'), sc.get('details'),
            c['studio_id'], c['studio_name'], c['parent_name'], json.dumps(c['performers'], ensure_ascii=False),
            json.dumps([{'id': t.get('id'), 'name': t.get('name')} for t in (sc.get('tags') or [])], ensure_ascii=False),
            json.dumps([u.get('url') for u in (sc.get('urls') or []) if u and u.get('url')], ensure_ascii=False),
            c['image_url'], thumb, json.dumps(sc, ensure_ascii=False),
            score if score is not None else (old[3] if same else None))
    conn.execute('INSERT OR REPLACE INTO video_ext_link (video_id, ext_source, ext_id, ext_title, ext_date, ext_code, ext_duration, '
                 'ext_director, ext_details, ext_studio_id, ext_studio_name, ext_studio_parent, ext_performers, ext_tags, ext_urls, '
                 'ext_image_url, ext_thumb, ext_raw_json, match_score, linked_at, refreshed_at) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                 vals + ((old[1] if (same and keep_linked_at) else datetime_now_sql()),))
    conn.commit()


def _store_tpdb(conn, video_id, kind, raw, clues, score=None, scene_number=None, keep_linked_at=True):
    c = _tpdb_compact(raw, kind)
    n, picked = _movie_scene_pick(c, clues) if kind == 'movie' else (None, None)
    n = scene_number or n or (clues.get('scene_number') if kind == 'movie' else None)
    if kind == 'movie' and n and c['scenes'] and 1 <= n <= len(c['scenes']) and not picked:
        picked = c['scenes'][n - 1]
    title = (picked or {}).get('title') or c['title']
    perfs = (picked or {}).get('performers') or c['performers']
    scope = 'scene' if (kind == 'scene' or picked) else 'movie'
    image = download_image(c['image_url']) if c['image_url'] else None
    thumb = None
    if image:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(image[0])); img.load()
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            img.thumbnail((240, 240), Image.Resampling.LANCZOS)
            buf = io.BytesIO(); img.save(buf, format='WEBP', quality=THUMB_QUALITY, method=6); thumb = buf.getvalue()
        except Exception as e:
            print(f"ext_db: cover thumbnail failed: {e}")
    old = conn.execute('SELECT ext_id, linked_at, ext_thumb FROM video_ext_link WHERE video_id = ?', (video_id,)).fetchone()
    same = bool(old and old[0] == c['id'])
    if thumb is None and same:
        thumb = old[2]
    tags = [{'id': t.get('id'), 'name': t.get('name')} for t in (raw.get('tags') or []) if t and t.get('name')]
    urls = [u for u in [raw.get('url')] + [(l or {}).get('url') if isinstance(l, dict) else l for l in (raw.get('links') or [])] if u]
    conn.execute('INSERT OR REPLACE INTO video_ext_link (video_id, ext_source, ext_id, ext_title, ext_date, ext_code, ext_duration, '
                 'ext_director, ext_details, ext_studio_id, ext_studio_name, ext_studio_parent, ext_performers, ext_tags, ext_urls, '
                 'ext_image_url, ext_thumb, ext_raw_json, match_score, ext_kind, ext_movie_title, ext_scene_number, ext_performer_scope, '
                 'linked_at, refreshed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                 (video_id, 'tpdb', c['id'], title, c['date'], c['code'], (picked or {}).get('duration') if kind == 'movie' else c['duration'],
                  ', '.join(d.get('name', '') for d in (raw.get('directors') or []) if isinstance(d, dict)) or None,
                  raw.get('description'), c['studio_id'], c['studio_name'], c['parent_name'],
                  json.dumps(perfs, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), json.dumps(urls[:10], ensure_ascii=False),
                  c['image_url'], thumb, json.dumps(raw, ensure_ascii=False)[:500000], score, kind,
                  c['title'] if kind == 'movie' else None, n, scope,
                  old[1] if (same and keep_linked_at) else datetime_now_sql()))
    conn.commit()


def datetime_now_sql():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())


def _scene_link_public(conn, video_id):
    cur = conn.execute('SELECT * FROM video_ext_link WHERE video_id = ?', (video_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(zip([x[0] for x in cur.description], row))
    for k in ('ext_performers', 'ext_tags', 'ext_urls'):
        try:
            d[k] = json.loads(d[k]) if d[k] else []
        except ValueError:
            d[k] = []
    d['has_thumb'] = bool(d.pop('ext_thumb', None))
    d.pop('ext_raw_json', None)
    d['source_label'] = SOURCES.get(d['ext_source'], {}).get('label', d['ext_source'])
    d['page_url'] = (f"https://stashdb.org/scenes/{d['ext_id']}" if d['ext_source'] == 'stashdb' else
                     f"https://theporndb.net/{'movies' if d.get('ext_kind') == 'movie' else 'scenes'}/{d['ext_id']}")
    linked = {r[1]: r[0] for r in _rows(conn, 'SELECT cast_name, ext_id FROM cast_ext_link WHERE ext_source = ?', (d['ext_source'],))}
    idx = _cast_index(conn) if d['ext_source'] == 'tpdb' else {}
    for p in d['ext_performers']:
        p['local_name'] = linked.get(p.get('id')) or ((idx.get(norm_name(p.get('name'))) or idx.get(norm_name(p.get('as')))) if idx else None)
    return d


# ---------- endpoints ----------
class ScenePathBody(BaseModel):
    path: str
    query: Optional[str] = None
    source: Optional[str] = None
    sources: Optional[str] = 'both'        # which databases to search: both | stashdb | tpdb


class SceneLinkBody(BaseModel):
    path: str
    ext_id: str
    score: Optional[int] = None
    source: Optional[str] = None
    kind: Optional[str] = 'scene'          # 'scene' | 'movie' (ThePornDB movies)
    scene_number: Optional[int] = None


def _video_id(conn, path):
    r = conn.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,)).fetchone()
    if not r:
        raise ExtDBError('Video not found in the library database - it may need a rescan')
    return r[0]


@router.get('/scene')
def scene_get(path: str = Query(...), model=Depends(get_model)):
    """Clues for a video and its current scene link (local only, no requests)."""
    try:
        ensure_schema()
        conn = _conn()
        clues = video_clues(conn, path)
        return {'success': True, 'clues': clues, 'link': _scene_link_public(conn, clues['video_id']),
                'library_sources': library_sources(conn, clues['library'])}
    except Exception as e:
        return _err(e)


@router.post('/scene/search')
def scene_search(body: ScenePathBody, model=Depends(get_model)):
    try:
        ensure_schema()
        source = _source(body.source)
        conn = _conn()
        clues = video_clues(conn, body.path)
        results, notes = find_scene_candidates(conn, source, clues, body.query, body.sources or 'both')
        return {'success': True, 'clues': clues, 'results': results, 'notes': notes, 'source_label': SOURCES[source]['label']}
    except Exception as e:
        return _err(e)


@router.post('/scene/link')
def scene_link(body: SceneLinkBody, model=Depends(get_model)):
    try:
        ensure_schema()
        source = _source(body.source)
        conn = _conn()
        vid = _video_id(conn, body.path)
        if source == 'tpdb':
            kind = 'movie' if body.kind == 'movie' else 'scene'
            _store_tpdb(conn, vid, kind, tpdb_find(kind, body.ext_id), video_clues(conn, body.path), body.score,
                        body.scene_number, keep_linked_at=False)
        else:
            _store_scene(conn, vid, source, scene_find(source, body.ext_id), body.score, keep_linked_at=False)
        conn.execute("UPDATE video_ext_bulk_log SET outcome = 'linked', detail = 'Linked by you', checked_at = CURRENT_TIMESTAMP "
                     "WHERE video_id = ? AND outcome != 'linked'", (vid,))
        conn.commit()
        return {'success': True, 'link': _scene_link_public(conn, vid)}
    except Exception as e:
        return _err(e)


@router.post('/scene/refresh')
def scene_refresh(body: ScenePathBody, model=Depends(get_model)):
    try:
        ensure_schema()
        conn = _conn()
        vid = _video_id(conn, body.path)
        row = conn.execute('SELECT ext_source, ext_id, ext_kind, ext_scene_number, match_score FROM video_ext_link WHERE video_id = ?',
                           (vid,)).fetchone()
        if not row:
            raise ExtDBError('This video is not linked to a scene')
        before = _scene_link_public(conn, vid)
        if row[0] == 'tpdb':
            kind = row[2] or 'scene'
            _store_tpdb(conn, vid, kind, tpdb_find(kind, row[1]), video_clues(conn, body.path), row[4], row[3])
        else:
            _store_scene(conn, vid, row[0], scene_find(row[0], row[1]))
        after = _scene_link_public(conn, vid)
        changed = [k for k in ('ext_title', 'ext_date', 'ext_code', 'ext_duration', 'ext_studio_name', 'ext_performers', 'ext_tags', 'ext_details')
                   if json.dumps(before.get(k), sort_keys=True) != json.dumps(after.get(k), sort_keys=True)]
        return {'success': True, 'link': after, 'changed_fields': changed}
    except Exception as e:
        return _err(e)


@router.post('/scene/clear')
def scene_clear(body: ScenePathBody, model=Depends(get_model)):
    try:
        ensure_schema()
        conn = _conn()
        vid = _video_id(conn, body.path)
        cur = conn.execute('DELETE FROM video_ext_link WHERE video_id = ?', (vid,))
        conn.commit()
        return {'success': True, 'removed': cur.rowcount > 0}
    except Exception as e:
        return _err(e)


@router.get('/scene/thumb')
def scene_thumb(path: str = Query(...), model=Depends(get_model)):
    try:
        ensure_schema()
        conn = _conn()
        row = conn.execute('SELECT l.ext_thumb FROM video_ext_link l JOIN file_versions fv ON fv.video_id = l.video_id '
                           'WHERE fv.path = ?', (path,)).fetchone()
        if not row or not row[0]:
            return Response(status_code=404)
        return Response(content=bytes(row[0]), media_type='image/webp', headers={'Cache-Control': 'private, max-age=300'})
    except Exception:
        return Response(status_code=500)


@router.get('/scene-cache')
def scene_cache_stats(model=Depends(get_model)):
    try:
        ensure_schema()
        r = _conn().execute('SELECT COUNT(*), COALESCE(SUM(scene_count), 0), COALESCE(SUM(LENGTH(scenes_json)), 0), MIN(fetched_at) '
                            'FROM ext_scene_catalog').fetchone()
        return {'success': True, 'performers': r[0], 'scenes': r[1], 'bytes': r[2], 'oldest': r[3], 'expires_days': SCENE_CACHE_DAYS}
    except Exception as e:
        return _err(e)


@router.post('/scene-cache/clear')
def scene_cache_clear(model=Depends(get_model)):
    try:
        ensure_schema()
        conn = _conn()
        cur = conn.execute('DELETE FROM ext_scene_catalog')
        conn.commit()
        return {'success': True, 'removed': cur.rowcount}
    except Exception as e:
        return _err(e)


# ===========================================================================
# Scene suggestions: metadata for a video from its linked scene (local only)
# ===========================================================================
# Same pattern as the attribute suggestions: evidence shown, strong items
# pre-ticked, nothing written here. The frontend applies the chosen items
# through /api/metadata/update (add only) and /male-cast/set.
TAG_AUTO_MIN_VIDEOS = 3          # an 'auto' tag mapping pre-ticks once its value is on this many videos

# What Suggest metadata pre-ticks (metadata key 'scene_tick'). Bulk matching's "safe metadata"
# is what's pre-ticked here, so these apply to it too. 'tags' is the rule for a mapping
# without its own: never | confirmed | often (confirmed, or an auto mapping on 3+ videos).
SCENE_TICK_KEY = 'scene_tick'
SCENE_TICK_DEFAULTS = {'year': True, 'site': True, 'cast_linked': True, 'cast_name': False, 'tags': 'never'}
TAG_TICK_DEFAULTS = ('never', 'confirmed', 'often')
TAG_TICK_RULES = ('always', 'never', 'types')
ORIENTATION_TYPES = ('straight', 'gay', 'bisexual', 'trans', 'lesbian')


def get_scene_tick(conn):
    s = dict(SCENE_TICK_DEFAULTS)
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (SCENE_TICK_KEY,)).fetchone()
        saved = json.loads(row[0]) if row else {}
    except Exception:
        saved = {}
    for k in ('year', 'site', 'cast_linked', 'cast_name'):
        if k in saved:
            s[k] = bool(saved[k])
    if saved.get('tags') in TAG_TICK_DEFAULTS:
        s['tags'] = saved['tags']
    return s


def save_scene_tick(conn, changes):
    s = get_scene_tick(conn)
    for k in ('year', 'site', 'cast_linked', 'cast_name'):
        if k in changes:
            s[k] = bool(changes[k])
    if changes.get('tags') in TAG_TICK_DEFAULTS:
        s['tags'] = changes['tags']
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SCENE_TICK_KEY, json.dumps(s)))
    conn.commit()
    return s


def _tick_types(value):
    return [t for t in ORIENTATION_TYPES if t in {x.strip().lower() for x in str(value or '').split(',')}]


def video_types(conn, video_id, orientations=None, straight=None):
    """A video's orientation types for tick rules: its orientation tags, else its library's type,
    else 'straight' when the scene looks straight. Empty when it can't be told."""
    import display_filter
    orient = _video_field_values(conn, video_id, 'orientation') if orientations is None else orientations
    types = {display_filter.TAG_TYPE[o.lower()] for o in orient if o.lower() in display_filter.TAG_TYPE}
    if types:
        return sorted(types)
    model = get_model()
    if model is not None:
        row = conn.execute('SELECT library FROM videos WHERE video_id = ?', (video_id,)).fetchone()
        try:
            t = display_filter.library_type(model, row[0]) if row and row[0] else None
        except Exception:
            t = None
        if t in ORIENTATION_TYPES:
            return [t]
    return ['straight'] if straight else []


def tag_pre_tick(m, n, defaults, vtypes):
    """-> (pre_tick, rule label) for a tag mapping {status, tick, tick_types} whose value is on n videos."""
    rule = m.get('tick')
    if rule == 'always':
        return True, 'always ticked'
    if rule == 'never':
        return False, 'never pre-ticked'
    if rule == 'types':
        want = _tick_types(m.get('tick_types'))
        return bool(set(vtypes) & set(want)), 'pre-ticked only on ' + (', '.join(want) or 'no') + ' videos'
    d = defaults.get('tags', 'never')
    if d == 'confirmed':
        return m['status'] == 'confirmed', 'default: confirmed mappings'
    if d == 'often':
        return m['status'] == 'confirmed' or n >= TAG_AUTO_MIN_VIDEOS, f'default: confirmed, or a value on {TAG_AUTO_MIN_VIDEOS}+ videos'
    return False, 'default: not pre-ticked'


def _tag_fields():
    try:
        from config import LIST_FIELDS
        fields = [f.lower() for f in LIST_FIELDS]
    except Exception:
        fields = ['site', 'tags', 'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face', 'cock',
                  'outfit', 'theme', 'feature', 'cumshot', 'participants', 'orientation']
    return [f for f in fields if f not in ('site', 'cast')]


def user_tag_format(name):
    """'Big Tits' -> 'big.tits' (your tag format: lowercase, spaces -> periods)."""
    s = re.sub(r'\s+', '.', (name or '').strip().lower())
    return re.sub(r'\.{2,}', '.', s).strip('.')


def _video_vocab(conn, fields):
    """{field: {value: number of videos}} - the values you already use."""
    vocab = {}
    for f in fields:
        fk = _junction_fk(conn, f)
        vocab[f] = ({r[0]: r[1] for r in _rows(conn, f'SELECT t.value, COUNT(DISTINCT j.video_id) FROM video_{f} t '
                                                     f'JOIN video_{f}_junction j ON j.{fk} = t.id GROUP BY t.value')}
                    if fk else {})
    return vocab


def split_values(value):
    """A mapping's value can name several of your values: 'thigh.highs, boots'."""
    return [v.strip() for v in str(value or '').split(',') if v.strip()]


def _vocab_lookup(vocab, field, value):
    """Your spelling of a value in a field (case-insensitive), or None."""
    low = (value or '').lower()
    return next((v for v in vocab.get(field, {}) if v.lower() == low), None)


def _tag_mapping(conn, source, name, vocab):
    """-> {'status', 'field', 'value', 'choices'} for an external tag.
    No stored mapping yet: an exact match (in your format) in exactly one field
    becomes an 'auto' mapping; several fields -> 'ambiguous'; none -> 'unmapped'."""
    key = (name or '').strip().lower()
    row = conn.execute('SELECT field, value, status, tick, tick_types FROM ext_tag_map WHERE ext_source = ? AND ext_tag = ?',
                       (source, key)).fetchone()
    if row:
        return {'status': row[2], 'field': row[0], 'value': row[1], 'choices': [], 'tick': row[3], 'tick_types': row[4]}
    want = user_tag_format(name)
    hits = [(f, v) for f in vocab for v in [_vocab_lookup(vocab, f, want)] if v]
    if len(hits) == 1:
        conn.execute("INSERT OR IGNORE INTO ext_tag_map (ext_source, ext_tag, field, value, status) VALUES (?, ?, ?, ?, 'auto')",
                     (source, key, hits[0][0], hits[0][1]))
        conn.commit()
        return {'status': 'auto', 'field': hits[0][0], 'value': hits[0][1], 'choices': [], 'tick': None, 'tick_types': None}
    return {'status': 'ambiguous' if hits else 'unmapped', 'field': None, 'value': want,
            'choices': [{'field': f, 'value': v} for f, v in hits]}


def _scene_gender(p, local):
    g = (p.get('gender') or '').upper().replace(' ', '_')
    if g.startswith('TRANS'):
        return 'trans'
    return _GENDER_MAP.get(g) or local or None


FULL_MOVIE_TAG = 'full.movie'       # a video with this tag is the whole movie: suggest the movie's full cast


def build_scene_suggestions(conn, path, full_movie=None):
    """full_movie: for a movie linked without its scene list, suggest the whole movie cast (True),
    or not (False); None = only when the video has the full.movie tag."""
    vid = _video_id(conn, path)
    link = _scene_link_public(conn, vid)
    if not link:
        raise ExtDBError('This video is not linked to a scene')
    source = link['ext_source']
    label = link['source_label']
    vrow = conn.execute('SELECT title, year FROM videos WHERE video_id = ?', (vid,)).fetchone() or (None, None)
    cur_title, cur_year = (vrow[0] or '').strip(), str(vrow[1] or '').strip()
    fields = _tag_fields()
    vocab = _video_vocab(conn, fields + ['site'])
    have = {f: {v.lower() for v in _video_field_values(conn, vid, f)} for f in fields + ['site', 'cast']}
    tick = get_scene_tick(conn)
    items, info = [], []

    def add(field, value, reason, pre_tick=False, **extra):
        if any(i['field'] == field and i['value'].lower() == value.lower() for i in items):
            return
        already = extra.pop('already_set', None)
        if already is None:
            already = value.lower() in have.get(field, set())
        items.append({'field': field, 'value': value, 'reason': reason, 'pre_tick': bool(pre_tick and not already),
                      'already_set': bool(already), 'new_value': extra.pop('new_value', False), **extra})

    # --- title: as-is, never pre-ticked ---
    title = (link.get('ext_title') or '').strip()
    if link.get('ext_kind') == 'movie' and link.get('ext_scene_number') and title == (link.get('ext_movie_title') or '').strip():
        title = f"{title} Scene {link['ext_scene_number']}"
    if title:
        add('title', title, f'from {label}', already_set=(title == cur_title),
            change=bool(cur_title and title != cur_title))

    # --- year ---
    year = (link.get('ext_date') or '')[:4]
    if re.fullmatch(r'\d{4}', year):
        if cur_year and cur_year != year:
            add('year', year, f'from {label} · replaces {cur_year}', False, already_set=False, change=True)
        else:
            add('year', year, f'from {label}', not cur_year and tick['year'], already_set=(cur_year == year))

    # --- site: the scene's studio, mapped back through your site links ---
    parent_id = None
    if source == 'stashdb':
        try:
            raw = json.loads(conn.execute('SELECT ext_raw_json FROM video_ext_link WHERE video_id = ?', (vid,)).fetchone()[0] or '{}')
            parent_id = ((raw.get('studio') or {}).get('parent') or {}).get('id')
        except (TypeError, ValueError):
            pass
    st_id, st_name, par_name = link.get('ext_studio_id'), link.get('ext_studio_name'), link.get('ext_studio_parent')
    groups = _site_groups(conn)
    no_site = not have['site'] and tick['site']
    for r in _rows(conn, 'SELECT site_key, ext_source, ext_id, ext_name, link_kind FROM site_ext_studio'):
        key, s_src, s_id, s_name, kind = r
        if source == 'stashdb':
            is_studio = s_src == 'stashdb' and st_id and s_id == st_id
            is_net = s_src == 'stashdb' and kind == 'network' and parent_id and s_id == parent_id
        else:          # site links hold StashDB ids: compare ThePornDB studios by name
            is_studio = bool(st_name) and norm_name(s_name) == norm_name(st_name)
            is_net = kind == 'network' and bool(par_name) and norm_name(s_name) == norm_name(par_name)
        if not (is_studio or is_net):
            continue
        g = groups.get(key)
        value = g['display'] if g else key
        why = f'via site link {s_name}' if is_studio else f'network of {st_name} (via site link {s_name})'
        add('site', value, why, no_site, new_value=not g)
    if st_name and not any(i['field'] == 'site' for i in items):
        info.append(f'Studio "{st_name}"' + (f' ({par_name})' if par_name else '') +
                    ' is not linked to any of your sites - link it in Metadata Browser → Link sites.')

    # --- cast / male cast ---
    cast_prim = {resolve_primary(conn, c).lower() for c in _video_field_values(conn, vid, 'cast')}
    marked = {n.lower() for n in _male_by_video(conn).get(vid, set())}
    marked |= {resolve_primary(conn, n).lower() for n in marked}
    genders = {r[0]: (r[1] or '').lower() for r in _rows(conn, 'SELECT cast_name, gender FROM cast_gender')}
    orient = _video_field_values(conn, vid, 'orientation')
    perfs = link.get('ext_performers') or []
    movie_cast = None
    if link.get('ext_performer_scope') == 'movie' and perfs:
        tagged = FULL_MOVIE_TAG in {v.lower() for v in _video_field_values(conn, vid, 'tags')}
        use = tagged if full_movie is None else bool(full_movie)
        movie_cast = {'names': [p.get('name') or '?' for p in perfs], 'full_movie': use, 'tagged': tagged}
        if not use:
            perfs = []
    linked = {r[1]: r[0] for r in _rows(conn, 'SELECT cast_name, ext_id FROM cast_ext_link WHERE ext_source = ?', (source,))}
    idx = _cast_index(conn)
    resolved = []
    for p in perfs:
        local, how = None, None
        if source != 'tpdb' and linked.get(p.get('id')):
            local, how = linked[p['id']], 'linked'
        else:
            hit = idx.get(norm_name(p.get('name'))) or (idx.get(norm_name(p.get('as'))) if p.get('as') else None)
            if hit:
                local = resolve_primary(conn, hit)
                how = 'tpdb-name' if source == 'tpdb' else 'name'      # ThePornDB performers match by canonical/credited name
        gender = _scene_gender(p, genders.get(local) if local else None)
        resolved.append((p, local, how, gender))
    straight = is_straight_video(orient, [g for *_, g in resolved if g])
    for p, local, how, gender in resolved:
        ext_label = p.get('name') or '?'
        if p.get('as') and norm_name(p['as']) != norm_name(ext_label):
            ext_label += f' (as {p["as"]})'
        is_man = straight and gender == 'male'
        field = 'male_cast' if is_man else 'cast'
        if local:
            in_cast = local.lower() in cast_prim
            if how in ('linked', 'tpdb-name'):
                reason = (f'{ext_label} · ' + ('linked performer' if how == 'linked' else 'matched by name')
                          + (' · man in a straight scene' if is_man else ''))
                pre = tick['cast_linked']
            else:
                reason = f'{ext_label} · name match (not linked)'
                pre = tick['cast_name']
            if is_man:
                if in_cast and local.lower() not in marked:
                    reason = f'{ext_label} · already in Cast, mark as Male Cast'
                add('male_cast', local, reason, pre, already_set=local.lower() in marked, linked=how == 'linked')
            else:
                add('cast', local, reason, pre, already_set=in_cast, linked=how == 'linked')
        else:
            add(field, p.get('name') or '', f'{ext_label} · not in your cast yet' +
                (' · man in a straight scene' if is_man else ''), False, already_set=False, new_cast=True)

    # --- tags, through the tag mapping ---
    vtypes = video_types(conn, vid, orient, straight)
    unmapped, ignored = [], []
    for t in link.get('ext_tags') or []:
        name = (t.get('name') or '').strip()
        if not name:
            continue
        m = _tag_mapping(conn, source, name, vocab)
        if m['status'] == 'ignored':
            ignored.append(name)
        elif m['status'] in ('auto', 'confirmed') and m['field'] in fields and m['value']:
            for value in split_values(m['value']):          # one tag can map to several values
                n = vocab.get(m['field'], {}).get(value, 0)
                pre, rule_label = tag_pre_tick(m, n, tick, vtypes)
                add(m['field'], value, f'tag mapping: {name} → {m["field"]}: {m["value"]}'
                    + (' (confirmed)' if m['status'] == 'confirmed' else f' (exact name, on {n} video{"s" if n != 1 else ""})')
                    + f' · {rule_label}',
                    pre, new_value=not n, ext_tag=name, tick=m.get('tick') or 'default',
                    tick_types=_tick_types(m.get('tick_types')))
        else:
            unmapped.append({'tag': name, 'suggested': m['value'], 'choices': m['choices']})

    for it in items:
        if it['field'] in ('site',) or it['field'] in fields:
            it['new_value'] = it.get('new_value') or not _vocab_lookup(vocab, it['field'], it['value'])
    current = {'title': [cur_title] if cur_title else [], 'year': [cur_year] if cur_year else [],
               'site': _video_field_values(conn, vid, 'site')}
    all_cast = _video_field_values(conn, vid, 'cast')
    men = _male_by_video(conn).get(vid, set())
    current['cast'] = [c for c in all_cast if c not in men]
    current['male_cast'] = [c for c in all_cast if c in men]
    for f in fields:
        current[f] = _video_field_values(conn, vid, f)
    pv = conn.execute('SELECT cache_id, filename FROM file_versions WHERE video_id = ? AND deleted_at IS NULL '
                      'ORDER BY is_preferred DESC, path LIMIT 1', (vid,)).fetchone()
    return {'items': items, 'info': info, 'unmapped': unmapped, 'ignored': ignored, 'straight': straight,
            'video_types': vtypes, 'tick_settings': tick, 'movie_cast': movie_cast,
            'current': current, 'cache_id': pv[0] if pv else None, 'filename': pv[1] if pv else None,
            'source': source, 'source_label': label, 'fields': fields,
            'vocab': {f: sorted(vocab.get(f, {}), key=str.lower) for f in fields}}


class TagMapBody(BaseModel):
    source: str
    tag: str
    field: Optional[str] = None
    value: Optional[str] = None
    ignore: bool = False


class TagUsedBody(BaseModel):
    source: str
    tags: list = []


@router.get('/scene/suggest')
def scene_suggest(path: str = Query(...), full_movie: Optional[bool] = None, model=Depends(get_model)):
    """Metadata suggestions for a video from its linked scene (local only, no requests)."""
    try:
        ensure_schema()
        return {'success': True, **build_scene_suggestions(_conn(), path, full_movie)}
    except Exception as e:
        return _err(e)


@router.post('/scene/tagmap')
def scene_tagmap(body: TagMapBody, model=Depends(get_model)):
    """Remember what an external tag means in your library (or that you ignore it)."""
    try:
        ensure_schema()
        conn = _conn()
        source = _source(body.source)
        key = (body.tag or '').strip().lower()
        if not key:
            raise ExtDBError('No tag given')
        if body.ignore:
            field = value = None
            status = 'ignored'
        else:
            field = (body.field or '').strip().lower()
            if field not in _tag_fields():
                raise ExtDBError(f'Unknown field: {body.field}')
            raw = (body.value or '').strip()
            if not raw:
                raise ExtDBError('No value given')
            fvocab = _video_vocab(conn, [field])
            values = []
            for part in split_values(raw):                  # 'thigh.highs, boots' -> two values
                v = _vocab_lookup(fvocab, field, part) or user_tag_format(part)
                if v and v not in values:
                    values.append(v)
            if not values:
                raise ExtDBError('No value given')
            value = ', '.join(values)
            status = 'confirmed'
        # ignoring keeps an existing field/value, so the tag can be switched back without retyping
        conn.execute('INSERT INTO ext_tag_map (ext_source, ext_tag, field, value, status, updated_at) '
                     'VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(ext_source, ext_tag) DO UPDATE SET '
                     'field = COALESCE(excluded.field, ext_tag_map.field), value = COALESCE(excluded.value, ext_tag_map.value), '
                     'status = excluded.status, updated_at = CURRENT_TIMESTAMP',
                     (source, key, field, value, status))
        conn.commit()
        return {'success': True, 'tag': body.tag, 'field': field, 'value': value, 'status': status}
    except Exception as e:
        return _err(e)


class TagStatusBody(BaseModel):
    source: str
    tag: str
    status: str


@router.post('/tagmap/status')
def tagmap_status(body: TagStatusBody, model=Depends(get_model)):
    """Change only a mapping's status (auto / confirmed / ignored); its field and value stay."""
    try:
        ensure_schema()
        if body.status not in ('auto', 'confirmed', 'ignored'):
            raise ExtDBError(f'Unknown status: {body.status}')
        conn = _conn()
        key = (body.tag or '').strip().lower()
        row = conn.execute('SELECT field, value FROM ext_tag_map WHERE ext_source = ? AND ext_tag = ?', (body.source, key)).fetchone()
        if not row:
            raise ExtDBError(f'No mapping for "{body.tag}"')
        if body.status != 'ignored' and not (row[0] and row[1]):
            raise ExtDBError(f'"{body.tag}" has no field and value yet - map it first')
        conn.execute('UPDATE ext_tag_map SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE ext_source = ? AND ext_tag = ?',
                     (body.status, body.source, key))
        conn.commit()
        return {'success': True, 'tag': key, 'status': body.status, 'field': row[0], 'value': row[1]}
    except Exception as e:
        return _err(e)


class TagTickBody(BaseModel):
    source: str
    tag: str
    tick: Optional[str] = None          # None/'default' | 'always' | 'never' | 'types'
    types: list = []


@router.post('/tagmap/tick')
def tagmap_tick(body: TagTickBody, model=Depends(get_model)):
    """Set a mapping's pre-tick rule; its field, value and status stay."""
    try:
        ensure_schema()
        rule = None if body.tick in (None, '', 'default') else body.tick
        if rule is not None and rule not in TAG_TICK_RULES:
            raise ExtDBError(f'Unknown pre-tick rule: {body.tick}')
        types = _tick_types(','.join(str(x) for x in body.types or []))
        if rule == 'types' and not types:
            raise ExtDBError('Pick at least one orientation')
        conn = _conn()
        key = (body.tag or '').strip().lower()
        n = conn.execute('UPDATE ext_tag_map SET tick = ?, tick_types = ?, updated_at = CURRENT_TIMESTAMP '
                         'WHERE ext_source = ? AND ext_tag = ?',
                         (rule, ','.join(types) if rule == 'types' else None, body.source, key)).rowcount
        conn.commit()
        if not n:
            raise ExtDBError(f'No mapping for "{body.tag}"')
        return {'success': True, 'tag': key, 'tick': rule or 'default', 'tick_types': types if rule == 'types' else []}
    except Exception as e:
        return _err(e)


class SceneTickBody(BaseModel):
    changes: dict = {}


@router.get('/scene/tick-settings')
def scene_tick_get(model=Depends(get_model)):
    try:
        ensure_schema()
        return {'success': True, 'settings': get_scene_tick(_conn()), 'types': list(ORIENTATION_TYPES)}
    except Exception as e:
        return _err(e)


@router.post('/scene/tick-settings')
def scene_tick_set(body: SceneTickBody, model=Depends(get_model)):
    try:
        ensure_schema()
        return {'success': True, 'settings': save_scene_tick(_conn(), body.changes or {})}
    except Exception as e:
        return _err(e)


@router.post('/scene/tagmap/used')
def scene_tagmap_used(body: TagUsedBody, model=Depends(get_model)):
    """Count applied tag mappings (bookkeeping only)."""
    try:
        ensure_schema()
        conn = _conn()
        n = 0
        for t in body.tags or []:
            n += conn.execute('UPDATE ext_tag_map SET uses = COALESCE(uses, 0) + 1 WHERE ext_source = ? AND ext_tag = ?',
                              (body.source, str(t).strip().lower())).rowcount
        conn.commit()
        return {'success': True, 'updated': n}
    except Exception as e:
        return _err(e)


# ---------- tag mapping management (Settings -> External DB) ----------
class TagDeleteBody(BaseModel):
    source: str
    tag: str


@router.get('/tagmap')
def tagmap_list(model=Depends(get_model)):
    """Every stored tag mapping, plus the external tags seen in your linked scenes (read-only)."""
    try:
        ensure_schema()
        conn = _conn()
        fields = _tag_fields()
        vocab = _video_vocab(conn, fields)
        rows = []
        for r in _rows(conn, 'SELECT ext_source, ext_tag, field, value, status, uses, updated_at, tick, tick_types FROM ext_tag_map '
                             'ORDER BY ext_tag, ext_source'):
            parts = split_values(r[3]) if r[2] else []
            counts = [vocab.get(r[2], {}).get(v, 0) for v in parts]
            rows.append({'source': r[0], 'tag': r[1], 'field': r[2], 'value': r[3], 'values': parts, 'status': r[4],
                         'uses': r[5] or 0, 'updated_at': r[6], 'tick': r[7] or 'default', 'tick_types': _tick_types(r[8]),
                         'videos': (counts[0] if len(counts) == 1 else ' / '.join(map(str, counts))) if counts else 0,
                         'missing': bool(r[4] != 'ignored' and parts and any(not _vocab_lookup(vocab, r[2], v) for v in parts))})
        seen = {}
        for src, tags_json in _rows(conn, 'SELECT ext_source, ext_tags FROM video_ext_link'):
            try:
                tags = json.loads(tags_json) if tags_json else []
            except ValueError:
                continue
            for t in {(t.get('name') or '').strip() for t in tags if isinstance(t, dict)}:
                if t:
                    s = seen.setdefault((src, t.lower()), {'source': src, 'tag': t, 'videos': 0})
                    s['videos'] += 1
        mapped = {(r['source'], r['tag']) for r in rows}
        for (src, key), s in seen.items():
            s['mapped'] = (src, key) in mapped
        return {'success': True, 'rows': rows, 'fields': fields,
                'seen': sorted(seen.values(), key=lambda s: (-s['videos'], s['tag'].lower())),
                'vocab': {f: sorted(vocab.get(f, {}), key=str.lower) for f in fields},
                'sources': [{'key': k, 'label': v['label']} for k, v in SOURCES.items()],
                'tick_settings': get_scene_tick(conn), 'types': list(ORIENTATION_TYPES)}
    except Exception as e:
        return _err(e)


@router.post('/tagmap/delete')
def tagmap_delete(body: TagDeleteBody, model=Depends(get_model)):
    """Forget a tag mapping. An exact-name match is re-created as 'auto' on the next
    suggestion - ignore the tag instead to stop it for good."""
    try:
        ensure_schema()
        conn = _conn()
        cur = conn.execute('DELETE FROM ext_tag_map WHERE ext_source = ? AND ext_tag = ?',
                           (body.source, (body.tag or '').strip().lower()))
        conn.commit()
        return {'success': True, 'removed': cur.rowcount > 0}
    except Exception as e:
        return _err(e)


@router.get('/overview')
def extdb_overview(model=Depends(get_model)):
    """Counts for the Settings -> External DB overview (local only)."""
    try:
        ensure_schema()
        conn = _conn()
        one = lambda sql: (conn.execute(sql).fetchone() or [0])[0]
        maps = {r[0]: r[1] for r in _rows(conn, 'SELECT status, COUNT(*) FROM ext_tag_map GROUP BY status')}
        return {'success': True,
                'performers': {r[0]: r[1] for r in _rows(conn, 'SELECT ext_source, COUNT(*) FROM cast_ext_link GROUP BY ext_source')},
                'videos': {r[0]: r[1] for r in _rows(conn, 'SELECT ext_source, COUNT(*) FROM video_ext_link GROUP BY ext_source')},
                'sites': one('SELECT COUNT(DISTINCT site_key) FROM site_ext_studio'),
                'tag_maps': maps}
    except Exception as e:
        return _err(e)


# ===========================================================================
# Bulk scene matching (videos -> scenes, many at once)
# ===========================================================================
# Same searches and scores as Find scene. A video is linked automatically only
# on very strong evidence; everything else goes to a review list whose
# candidates are kept in memory so reviewing needs no new searches.
SCENE_BULK_OUTCOMES = {
    'linked': 'Auto-linked', 'review': 'Needs review', 'not_found': 'Not found',
    'already_linked': 'Already linked', 'scene_taken': 'Scene taken', 'error': 'Error',
}
SCENE_AUTO_MIN_SCORE = 85        # top candidate's score
SCENE_AUTO_MIN_LEAD = 20         # ...ahead of the next different candidate
SCENE_AUTO_MIN_SIGNALS = 2       # ...with at least this many strong signals agreeing
SCENE_REVIEW_MIN_SCORE = 40      # below this a video counts as "not found"
SCENE_BULK_CANDS = 12            # candidates kept per video for review
SCENE_BULK_CANDS_MAX = 5000      # videos whose candidates are kept in memory
LIBRARY_SOURCES = ('both', 'stashdb', 'tpdb')
_sbulk_lock = threading.Lock()
_sbulk = {'running': False, 'run_id': None}
_sbulk_cands = {}                # video_id -> candidate list from the last check (memory only)


def library_sources(conn, library):
    r = conn.execute('SELECT sources FROM library_ext_source WHERE library = ?', (library,)).fetchone() if library else None
    return r[0] if r and r[0] in LIBRARY_SOURCES else 'both'


def _deleted_filter(conn, alias='fv'):
    cols = {c[1] for c in _rows(conn, 'PRAGMA table_info(file_versions)')}
    return f' AND {alias}.deleted_at IS NULL' if 'deleted_at' in cols else ''


def _scene_targets(conn, paths=None, libraries=None):
    """-> [(video_id, path)], one per video (its preferred file). A path that isn't
    a file (a folder from folder mode) covers everything below it."""
    dele = _deleted_filter(conn)
    rows = []
    if libraries:
        libs = list(libraries)
        for i in range(0, len(libs), 400):
            chunk = libs[i:i + 400]
            rows += _rows(conn, f'SELECT fv.video_id, fv.path, fv.is_preferred FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
                                f'WHERE v.library IN ({",".join("?" * len(chunk))}){dele}', chunk)
    for p in paths or []:
        p = str(p)
        exact = _rows(conn, f'SELECT fv.video_id, fv.path, fv.is_preferred FROM file_versions fv WHERE fv.path = ?{dele}', (p,))
        if exact:
            rows += [(r[0], r[1], 2) for r in exact]        # the file you picked beats the preferred version
        else:
            prefix = p.rstrip('/') + '/'
            rows += _rows(conn, f"SELECT fv.video_id, fv.path, fv.is_preferred FROM file_versions fv "
                                f"WHERE fv.path LIKE ? ESCAPE '\\'{dele}",
                          (prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%',))
    best = {}
    for vid, path, pref in rows:
        cur = best.get(vid)
        if cur is None or (pref or 0) > cur[1] or ((pref or 0) == cur[1] and path < cur[0]):
            best[vid] = (path, pref or 0)
    return [(vid, v[0]) for vid, v in best.items()]


def _scene_queue_order(conn, targets):
    """Videos with a linked performer first, grouped by that performer (one cached
    scene catalog then serves all of their videos), then the rest by site."""
    linked = {r[0]: r[1] for r in _rows(conn, "SELECT cast_name, ext_id FROM cast_ext_link WHERE ext_source = 'stashdb'")}
    cast = _junction_values(conn, 'cast')
    sites = _junction_values(conn, 'site')
    prim = {}

    def key(t):
        vid, path = t
        perf = None
        for _cid, name in cast.get(vid, []):
            p = prim.get(name)
            if p is None:
                p = prim[name] = resolve_primary(conn, name)
            if linked.get(p):
                perf = linked[p]
                break
        site = min((v for _i, v in sites.get(vid, [])), default='')
        return (0 if perf else 1, perf or '', site_key(site), path)
    return sorted(targets, key=key)


def _scene_scope(conn, paths, libraries, skip_linked, skip_checked):
    targets = _scene_targets(conn, paths, libraries)
    linked = {r[0] for r in _rows(conn, 'SELECT video_id FROM video_ext_link')}
    checked = {r[0] for r in _rows(conn, "SELECT video_id FROM video_ext_bulk_log WHERE outcome != 'error'")}
    todo = [t for t in targets if not (skip_linked and t[0] in linked) and not (skip_checked and t[0] in checked and t[0] not in linked)]
    stats = {'total': len(targets), 'already_linked': sum(1 for t in targets if t[0] in linked),
             'already_checked': sum(1 for t in targets if t[0] in checked and t[0] not in linked), 'to_check': len(todo)}
    return todo, stats


def _scene_cache(video_id, results):
    _sbulk_cands.pop(video_id, None)
    _sbulk_cands[video_id] = results[:SCENE_BULK_CANDS]
    while len(_sbulk_cands) > SCENE_BULK_CANDS_MAX:
        _sbulk_cands.pop(next(iter(_sbulk_cands)))


def _strong_signals(sc, clues):
    """The independent pieces of evidence that agree between a candidate and your video."""
    sig = set()
    if sc.get('kind') != 'movie' and clues['duration'] and sc.get('duration') and abs(sc['duration'] - clues['duration']) <= 5:
        sig.add('same length')
    if sc.get('source') == 'tpdb':
        names = {norm_name(p['name']) for p in sc['performers']} | {norm_name(p['as']) for p in sc['performers'] if p.get('as')}
        if any(norm_name(c['name']) in names for c in clues['cast']):
            sig.add('performer')
        mine = {norm_name(s_['value']) for s_ in clues['sites']} | {norm_name(st['ext_name']) for s_ in clues['sites'] for st in s_['studios']}
        if mine & ({norm_name(sc.get('studio_name')), norm_name(sc.get('parent_name'))} - {''}):
            sig.add('studio')
    else:
        linked = {c['ext_id'] for c in clues['cast'] if c['ext_id']}
        if linked & {p['id'] for p in sc['performers']}:
            sig.add('performer')
        studios = {st['ext_id'] for s_ in clues['sites'] for st in s_['studios'] if st.get('link_kind') != 'network'}
        networks = {st['ext_id'] for s_ in clues['sites'] for st in s_['studios'] if st.get('link_kind') == 'network'}
        if sc.get('studio_id') and (sc['studio_id'] in studios | networks or sc.get('parent_id') in networks):
            sig.add('studio')
    if clues['date'] and sc.get('date') == clues['date']:
        sig.add('same date')
    if clues['title'] and sc.get('title') and _title_similarity(clues['title'], sc['title']) >= 0.85:
        sig.add('title')
    return sig


def _same_scene(a, b):
    """The same release listed in both databases (so it doesn't count as a rival)."""
    return (a.get('source') != b.get('source') and a.get('date') and a.get('date') == b.get('date')
            and _title_similarity(a.get('title') or '', b.get('title') or '') >= 0.85)


def _best_public(sc, signals=()):
    return {'id': sc['id'], 'source': sc.get('source'), 'kind': sc.get('kind', 'scene'), 'title': sc.get('title'),
            'studio': sc.get('studio_name'), 'date': sc.get('date'), 'score': sc.get('score'),
            'reasons': sc.get('reasons') or [], 'signals': sorted(signals), 'scene_number': sc.get('scene_number'),
            'image_url': sc.get('image_url')}


def _add_list_value(conn, video_id, field, value):
    """Add one value to a video's list field (add only). -> True if it was new."""
    fk = _junction_fk(conn, field)
    if not fk or not value:
        return False
    row = conn.execute(f'SELECT id FROM video_{field} WHERE value = ?', (value,)).fetchone()
    vid_ = row[0] if row else conn.execute(f'INSERT INTO video_{field} (value) VALUES (?)', (value,)).lastrowid
    if conn.execute(f'SELECT 1 FROM video_{field}_junction WHERE video_id = ? AND {fk} = ?', (video_id, vid_)).fetchone():
        return False
    conn.execute(f'INSERT INTO video_{field}_junction (video_id, {fk}) VALUES (?, ?)', (video_id, vid_))
    return True


def _apply_safe_suggestions(conn, video_id, path, safe=True, title=False, all_men=False):
    """After an automatic link, add metadata from the scene (never removes anything):
    safe    - the year and site (only when the video has none) and performers linked to your cast
    title   - the scene's title, replacing the video's title
    all_men - in a straight scene, every male performer as Male Cast, also men who aren't in
              your cast yet (they get a new cast entry with gender male)"""
    d = build_scene_suggestions(conn, path)
    picks = []
    for i in d['items']:
        if i['already_set']:
            continue
        if i['field'] == 'title':
            if title:
                picks.append(i)
        elif i['field'] == 'male_cast' and all_men:
            picks.append(i)
        elif safe and i['pre_tick'] and not i.get('change') and not i.get('new_cast') and (
                i['field'] in ('year', 'site') or (i['field'] in ('cast', 'male_cast') and i.get('linked'))):
            picks.append(i)
    applied, men = [], []
    for it in picks:
        if it['field'] == 'title':
            n = conn.execute("UPDATE videos SET title = ? WHERE video_id = ? AND COALESCE(title, '') != ?",
                             (it['value'], video_id, it['value'])).rowcount
        elif it['field'] == 'year':
            n = conn.execute("UPDATE videos SET year = ? WHERE video_id = ? AND (year IS NULL OR TRIM(CAST(year AS TEXT)) = '')",
                             (it['value'], video_id)).rowcount
        else:
            n = _add_list_value(conn, video_id, 'cast' if it['field'] == 'male_cast' else it['field'], it['value'])
            if it['field'] == 'male_cast':
                men.append(it['value'])
                n = True
        if n:
            applied.append(f"{it['field']}: {it['value']}")
    if applied:
        conn.execute('UPDATE videos SET date_last_edited = ?, updated_at = ? WHERE video_id = ?',
                     (str(datetime.now()), datetime.now().isoformat(), video_id))
    conn.commit()
    if men:
        male_cast_change(conn, [video_id], men, [])
    new_cast = [a.split(': ', 1)[1] for a in applied if a.startswith(('cast: ', 'male_cast: '))]
    if new_cast:
        import automation
        automation.on_cast_added(conn, video_id, new_cast)
    return applied


def _bulk_scene_one(conn, video_id, path, which, apply_safe):
    """Check one video. apply_safe: True (safe metadata) or {'safe', 'title', 'all_men'}.
    -> (outcome, detail, best)"""
    opts = apply_safe if isinstance(apply_safe, dict) else {'safe': bool(apply_safe)}
    apply_safe = any(opts.values())
    clues = video_clues(conn, path)
    results, _notes = find_scene_candidates(conn, default_source(), clues, None, which)
    _scene_cache(video_id, results)
    if not results or results[0]['score'] < SCENE_REVIEW_MIN_SCORE:
        best = _best_public(results[0]) if results else None
        return 'not_found', (f"Best: {results[0]['title'] or '(untitled)'} ({results[0]['score']})" if results else 'No results'), best
    top = results[0]
    rival = next((r for r in results[1:] if not _same_scene(top, r)), None)
    lead = top['score'] - (rival['score'] if rival else 0)
    sig = _strong_signals(top, clues)
    best = _best_public(top, sig)
    label = f"{top['title'] or '(untitled)'} ({top['score']})"
    if top.get('is_current_link'):
        return 'already_linked', f'{label} - already linked to this scene', best
    if top.get('linked_to_other'):
        return 'scene_taken', f"{label} is linked to {top['linked_to_other'][0]}", best
    if top.get('kind') == 'movie':
        return 'review', f'{label} - ThePornDB movies are always reviewed', best
    why = []
    if top['score'] < SCENE_AUTO_MIN_SCORE:
        why.append(f'score below {SCENE_AUTO_MIN_SCORE}')
    if lead < SCENE_AUTO_MIN_LEAD:
        why.append(f'only {lead} ahead of the next')
    if len(sig) < SCENE_AUTO_MIN_SIGNALS:
        why.append('only ' + (', '.join(sorted(sig)) if sig else 'no') + ' strong signal' + ('' if len(sig) == 1 else 's'))
    if why:
        return 'review', f"{label} - {'; '.join(why)}", best
    if top.get('source') == 'tpdb':
        _store_tpdb(conn, video_id, 'scene', tpdb_find('scene', top['id']), clues, top['score'], None, keep_linked_at=False)
    else:
        src = top.get('source') or default_source()
        _store_scene(conn, video_id, src, scene_find(src, top['id']), top['score'], keep_linked_at=False)
    detail = f"{label} · {', '.join(sorted(sig))}"
    if apply_safe:
        try:
            applied = _apply_safe_suggestions(conn, video_id, path, safe=opts.get('safe', False),
                                              title=opts.get('title', False), all_men=opts.get('all_men', False))
            if applied:
                detail += ' · added ' + ', '.join(applied)
                best['applied'] = applied
        except Exception as e:
            detail += f' · metadata not applied: {e}'
    return 'linked', detail, best


def _scene_bulk_worker(targets, which, delay, apply_safe):
    global _pace_interval
    st = _sbulk
    run_id = st['run_id']
    _pace_interval = delay
    errors_in_a_row = 0
    try:
        conn = _conn()
        for idx, (vid, path) in enumerate(targets):
            if st['stop']:
                st['stopped'] = True
                break
            st['current'] = os.path.basename(path)
            outcome, detail, best = 'error', 'Rate limited', None
            for attempt in range(3):
                try:
                    outcome, detail, best = _bulk_scene_one(conn, vid, path, which, apply_safe)
                    break
                except ExtDBError as e:
                    msg = str(e)
                    if 'rate limit' in msg.lower() or '429' in msg:
                        if attempt < 2:
                            st['note'] = 'Rate limited by the server - pausing for 60 seconds'
                            _sleep_unless_scene_stopped(60)
                            st['note'] = ''
                            if st['stop']:
                                break
                            continue
                    if 'api key' in msg.lower():
                        raise
                    outcome, detail = 'error', msg
                    break
                except Exception as e:
                    outcome, detail = 'error', f'{type(e).__name__}: {e}'
                    break
            try:
                conn.execute('INSERT OR REPLACE INTO video_ext_bulk_log (video_id, outcome, detail, best_json, run_id, checked_at) '
                             'VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                             (vid, outcome, detail, json.dumps(best, ensure_ascii=False) if best else None, run_id))
                conn.commit()
            except Exception as e:
                print(f"⚠ ext_db scene bulk: could not log {path}: {e}")
            st['counts'][outcome] = st['counts'].get(outcome, 0) + 1
            st['recent'].insert(0, {'path': path, 'filename': os.path.basename(path), 'outcome': outcome,
                                    'outcome_label': SCENE_BULK_OUTCOMES.get(outcome, outcome), 'detail': detail})
            del st['recent'][40:]
            st['done'] = idx + 1
            errors_in_a_row = errors_in_a_row + 1 if outcome == 'error' else 0
            if errors_in_a_row >= 10:
                st['error'] = f'Stopped after 10 errors in a row. Last error: {detail}'
                break
    except ExtDBError as e:
        st['error'] = str(e)
    except Exception as e:
        st['error'] = f'Unexpected error: {e}'
    finally:
        _pace_interval = 0.0
        st['running'] = False
        st['current'] = None
        st['note'] = ''
        st['finished_at'] = time.time()
        print(f"ext_db scene bulk run {run_id} finished: {st['done']}/{st['total']} {st['counts']}")


def _sleep_unless_scene_stopped(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not _sbulk.get('stop'):
        time.sleep(0.5)


def _scene_bulk_status():
    st = dict(_sbulk)
    st.pop('stop', None)
    if st.get('started_at'):
        end = st.get('finished_at') or time.time()
        st['elapsed_seconds'] = round(end - st['started_at'])
        done, total = st.get('done', 0), st.get('total', 0)
        st['eta_seconds'] = (round((end - st['started_at']) / done * (total - done))
                             if st.get('running') and done else None)
    st['outcome_labels'] = SCENE_BULK_OUTCOMES
    return st


class SceneBulkBody(BaseModel):
    paths: Optional[list] = None
    libraries: Optional[list] = None
    skip_linked: bool = True
    skip_checked: bool = True
    sources: Optional[str] = None          # both | stashdb | tpdb (default: the library's choice)
    delay: float = 1.0
    apply_safe: bool = False
    set_title: bool = False                # replace titles with the scene's title
    all_male_cast: bool = False            # every man in a straight scene becomes Male Cast


class SceneSourceBody(BaseModel):
    library: str
    sources: str


def _scene_bulk_which(conn, body):
    if body.sources in LIBRARY_SOURCES:
        return body.sources
    libs = body.libraries or []
    return library_sources(conn, libs[0]) if len(libs) == 1 else 'both'


@router.post('/scenes-bulk/preview')
def scenes_bulk_preview(body: SceneBulkBody, model=Depends(get_model)):
    try:
        ensure_schema()
        conn = _conn()
        if not body.paths and not body.libraries:
            raise ExtDBError('Nothing selected')
        todo, stats = _scene_scope(conn, body.paths, body.libraries, body.skip_linked, body.skip_checked)
        perf = {r[0] for r in _rows(conn, 'SELECT cast_name FROM cast_ext_link')}
        cast = _junction_values(conn, 'cast')
        sites = _junction_values(conn, 'site')
        links, _ = _site_rows(conn)
        ids = [t[0] for t in todo]
        stats['with_linked_performer'] = sum(1 for v in ids if any(resolve_primary(conn, n) in perf for _c, n in cast.get(v, [])))
        stats['with_linked_site'] = sum(1 for v in ids if any((links.get(site_key(n)) or {}).get('studios') for _c, n in sites.get(v, [])))
        which = _scene_bulk_which(conn, body)
        per_video = 5 if which == 'both' and api_key('tpdb') else 4        # rough number of requests per video
        stats['estimate_seconds'] = round(len(todo) * per_video * max(float(body.delay), 0.5))
        return {'success': True, 'stats': stats, 'sources': which, 'tpdb_configured': bool(api_key('tpdb')),
                'status': _scene_bulk_status()}
    except Exception as e:
        return _err(e)


@router.post('/scenes-bulk/start')
def scenes_bulk_start(body: SceneBulkBody, model=Depends(get_model)):
    try:
        ensure_schema()
        if not api_key(default_source()):
            raise ExtDBError(f"No API key configured for {SOURCES[default_source()]['label']}")
        delay = min(max(float(body.delay), 0.5), 10.0)
        with _sbulk_lock:
            if _sbulk.get('running'):
                raise ExtDBError('Bulk scene matching is already running')
            if _bulk.get('running') or _site_job.get('running'):
                raise ExtDBError('Another bulk job (performers or sites) is running - wait for it or stop it first')
            conn = _conn()
            which = _scene_bulk_which(conn, body)
            todo, stats = _scene_scope(conn, body.paths, body.libraries, body.skip_linked, body.skip_checked)
            if not todo:
                raise ExtDBError('None of these videos need checking')
            todo = _scene_queue_order(conn, todo)
            scope = (f"{len(body.paths)} selected" if body.paths else ', '.join(body.libraries or []))
            _sbulk.clear()
            _sbulk.update({
                'running': True, 'stop': False, 'stopped': False, 'run_id': uuid.uuid4().hex[:12],
                'scope': scope, 'sources': which, 'apply_safe': body.apply_safe, 'set_title': body.set_title,
                'all_male_cast': body.all_male_cast, 'delay': delay,
                'total': len(todo), 'done': 0, 'counts': {}, 'current': None, 'note': '', 'error': None,
                'recent': [], 'started_at': time.time(), 'finished_at': None,
            })
            apply = {'safe': body.apply_safe, 'title': body.set_title, 'all_men': body.all_male_cast}
            threading.Thread(target=_scene_bulk_worker, args=(todo, which, delay, apply),
                             name='extdb-scene-bulk', daemon=True).start()
        return {'success': True, 'status': _scene_bulk_status()}
    except Exception as e:
        return _err(e)


@router.get('/scenes-bulk/status')
def scenes_bulk_status():
    return {'success': True, 'status': _scene_bulk_status()}


@router.post('/scenes-bulk/stop')
def scenes_bulk_stop():
    _sbulk['stop'] = True
    return {'success': True, 'status': _scene_bulk_status()}


@router.get('/scenes-bulk/results')
def scenes_bulk_results(run_id: Optional[str] = None, model=Depends(get_model)):
    """Outcomes of a run (default: the latest), with the current link for each video."""
    try:
        ensure_schema()
        conn = _conn()
        rid = run_id or _sbulk.get('run_id')
        if not rid:
            r = conn.execute('SELECT run_id FROM video_ext_bulk_log ORDER BY checked_at DESC LIMIT 1').fetchone()
            rid = r[0] if r else None
        out = []
        if rid:
            dele = _deleted_filter(conn)
            for r in _rows(conn, f'SELECT b.video_id, b.outcome, b.detail, b.best_json, b.checked_at, '
                                 f'(SELECT fv.path FROM file_versions fv WHERE fv.video_id = b.video_id{dele} '
                                 f' ORDER BY fv.is_preferred DESC, fv.path LIMIT 1), '
                                 f'l.ext_source, l.ext_id, l.ext_title, l.ext_studio_name, l.ext_date '
                                 f'FROM video_ext_bulk_log b LEFT JOIN video_ext_link l ON l.video_id = b.video_id '
                                 f'WHERE b.run_id = ? ORDER BY b.checked_at', (rid,)):
                try:
                    best = json.loads(r[3]) if r[3] else None
                except ValueError:
                    best = None
                out.append({'video_id': r[0], 'outcome': r[1], 'outcome_label': SCENE_BULK_OUTCOMES.get(r[1], r[1]),
                            'detail': r[2], 'best': best, 'checked_at': r[4], 'path': r[5],
                            'filename': os.path.basename(r[5] or ''), 'has_candidates': r[0] in _sbulk_cands,
                            'link': ({'source': r[6], 'ext_id': r[7], 'title': r[8], 'studio': r[9], 'date': r[10]} if r[7] else None)})
        return {'success': True, 'run_id': rid, 'results': out, 'outcome_labels': SCENE_BULK_OUTCOMES}
    except Exception as e:
        return _err(e)


@router.get('/scenes-bulk/candidates')
def scenes_bulk_candidates(path: str = Query(...), model=Depends(get_model)):
    """The candidates found for a video in the last bulk check (memory only, no requests)."""
    try:
        ensure_schema()
        conn = _conn()
        vid = _video_id(conn, path)
        return {'success': True, 'results': _sbulk_cands.get(vid) or [], 'cached': vid in _sbulk_cands}
    except Exception as e:
        return _err(e)


@router.post('/scenes-bulk/forget-checked')
def scenes_bulk_forget(body: SceneBulkBody, model=Depends(get_model)):
    """Forget earlier outcomes for these videos so the next run checks them again."""
    try:
        ensure_schema()
        conn = _conn()
        ids = [t[0] for t in _scene_targets(conn, body.paths, body.libraries)]
        n = 0
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            n += conn.execute(f'DELETE FROM video_ext_bulk_log WHERE video_id IN ({",".join("?" * len(chunk))})', chunk).rowcount
        conn.commit()
        return {'success': True, 'removed': n}
    except Exception as e:
        return _err(e)


@router.post('/scene/source')
def scene_source_set(body: SceneSourceBody, model=Depends(get_model)):
    """Remember which databases to search for a library."""
    try:
        ensure_schema()
        if body.sources not in LIBRARY_SOURCES:
            raise ExtDBError(f'Unknown choice: {body.sources}')
        conn = _conn()
        conn.execute('INSERT INTO library_ext_source (library, sources, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                     'ON CONFLICT(library) DO UPDATE SET sources = excluded.sources, updated_at = CURRENT_TIMESTAMP',
                     (body.library, body.sources))
        conn.commit()
        return {'success': True}
    except Exception as e:
        return _err(e)


@router.get('/scene/sources')
def scene_sources_list(model=Depends(get_model)):
    try:
        ensure_schema()
        return {'success': True, 'sources': {r[0]: r[1] for r in _rows(_conn(), 'SELECT library, sources FROM library_ext_source')}}
    except Exception as e:
        return _err(e)


# ---------- what's linked to what ----------
@router.get('/scene-links')
def scene_links(q: str = '', source: str = '', library: str = '', page: int = 1, per_page: int = 100, model=Depends(get_model)):
    """Every video linked to a scene or movie (for Settings -> External DB -> Linked videos)."""
    try:
        ensure_schema()
        conn = _conn()
        where, params = [], []
        if source:
            where.append('l.ext_source = ?')
            params.append(source)
        if library:
            where.append('v.library = ?')
            params.append(library)
        if q.strip():
            like = f'%{q.strip()}%'
            where.append('(l.ext_title LIKE ? OR l.ext_studio_name LIKE ? OR l.ext_movie_title LIKE ? OR '
                         'EXISTS (SELECT 1 FROM file_versions f2 WHERE f2.video_id = l.video_id AND f2.path LIKE ?))')
            params += [like, like, like, like]
        w = (' WHERE ' + ' AND '.join(where)) if where else ''
        base = f'FROM video_ext_link l LEFT JOIN videos v ON v.video_id = l.video_id{w}'
        total = conn.execute(f'SELECT COUNT(*) {base}', params).fetchone()[0]
        page = max(1, int(page))
        per_page = min(max(int(per_page), 10), 500)
        dele = _deleted_filter(conn)
        rows = []
        for r in _rows(conn, f'SELECT l.video_id, v.library, l.ext_source, l.ext_id, l.ext_kind, l.ext_title, l.ext_movie_title, '
                             f'l.ext_scene_number, l.ext_studio_name, l.ext_date, l.match_score, l.linked_at, '
                             f'(SELECT fv.path FROM file_versions fv WHERE fv.video_id = l.video_id{dele} ORDER BY fv.is_preferred DESC, fv.path LIMIT 1), '
                             f'(SELECT b.outcome FROM video_ext_bulk_log b WHERE b.video_id = l.video_id), '
                             f'(SELECT b.detail FROM video_ext_bulk_log b WHERE b.video_id = l.video_id) '
                             f'{base} ORDER BY l.linked_at DESC LIMIT ? OFFSET ?', params + [per_page, (page - 1) * per_page]):
            rows.append({'video_id': r[0], 'library': r[1], 'source': r[2], 'source_label': SOURCES.get(r[2], {}).get('label', r[2]),
                         'ext_id': r[3], 'kind': r[4] or 'scene', 'title': r[5], 'movie_title': r[6], 'scene_number': r[7],
                         'studio': r[8], 'date': r[9], 'score': r[10], 'linked_at': r[11], 'path': r[12],
                         'filename': os.path.basename(r[12] or ''),
                         'linked_by': 'bulk' if (r[13] == 'linked' and r[14] != 'Linked by you') else 'you'})
        libs = [r[0] for r in _rows(conn, 'SELECT DISTINCT v.library FROM video_ext_link l JOIN videos v ON v.video_id = l.video_id '
                                          'WHERE v.library IS NOT NULL ORDER BY v.library')]
        return {'success': True, 'rows': rows, 'total': total, 'page': page, 'per_page': per_page, 'libraries': libs}
    except Exception as e:
        return _err(e)


# ---------- library filter (scene:linked / unlinked / review / stashdb / tpdb) ----------
SCENE_FILTERS = ('linked', 'unlinked', 'review', 'stashdb', 'tpdb')


def scene_filter_sql(kind, col='v.video_id'):
    """SQL condition for the scene: search keyword, or None for an unknown keyword."""
    ensure_schema()
    kind = (kind or '').strip().lower()
    if kind == 'linked':
        return f'{col} IN (SELECT video_id FROM video_ext_link)', []
    if kind == 'unlinked':
        return f'{col} NOT IN (SELECT video_id FROM video_ext_link)', []
    if kind == 'review':
        return (f"{col} IN (SELECT video_id FROM video_ext_bulk_log WHERE outcome IN ('review', 'scene_taken')) "
                f"AND {col} NOT IN (SELECT video_id FROM video_ext_link)"), []
    if kind in ('stashdb', 'tpdb'):
        return f'{col} IN (SELECT video_id FROM video_ext_link WHERE ext_source = ?)', [kind]
    return None


def scene_filter_paths(kind):
    """-> (paths, negate) for the in-memory search path: keep a file when
    (path in paths) != negate."""
    ensure_schema()
    conn = _conn()
    cond = scene_filter_sql(kind, 'fv.video_id')
    if not cond:
        return set(), True
    negate = (kind or '').strip().lower() == 'unlinked'
    sql, params = (scene_filter_sql('linked', 'fv.video_id') if negate else cond)
    return {r[0] for r in _rows(conn, f'SELECT fv.path FROM file_versions fv WHERE {sql}', params)}, negate


# ===========================================================================
# Male Cast (men in straight scenes)
# ===========================================================================
# A video is "straight" when its orientation is straight or heteroflexible, or
# when it has no orientation and its cast has women and men and no trans
# performer. Gay, bisexual, lesbian and trans videos are never marked.
STRAIGHT_ORIENTATIONS = {'straight', 'heteroflexible'}
OTHER_ORIENTATIONS = {'gay', 'bisexual', 'lesbian', 'trans'}
_male_cache = {'by_video': None}
_male_lock = threading.Lock()


def _male_invalidate():
    _male_cache['by_video'] = None


def _male_by_video(conn=None):
    with _male_lock:
        if _male_cache['by_video'] is None:
            conn = conn or _conn()
            m = {}
            for r in _rows(conn, 'SELECT r.video_id, vc.value FROM video_cast_role r JOIN video_cast vc ON vc.id = r.cast_id '
                                 'JOIN video_cast_junction j ON j.video_id = r.video_id AND j.cast_id = r.cast_id'):
                m.setdefault(r[0], set()).add(r[1])
            _male_cache['by_video'] = m
        return _male_cache['by_video']


def male_cast_for_video(video_id):
    """Names in this video's cast that are Male Cast (used by videos.py for sorting)."""
    try:
        ensure_schema()
        return _male_by_video().get(video_id, set())
    except Exception:
        return set()


def _gender_of(conn):
    g = {r[0]: (r[1] or '').lower() for r in _rows(conn, 'SELECT cast_name, gender FROM cast_gender')}
    return lambda name: g.get(resolve_primary(conn, name)) or g.get(name) or 'female'


def _junction_values(conn, field):
    """video_id -> [(value_id, value)] for a video-level list field."""
    jc = [r[1] for r in _rows(conn, f'PRAGMA table_info(video_{field}_junction)')]
    fk = next((c for c in jc if c.endswith('_id') and c != 'video_id'), None)
    out = {}
    if not fk:
        return out
    for r in _rows(conn, f'SELECT j.video_id, t.id, t.value FROM video_{field}_junction j JOIN video_{field} t ON t.id = j.{fk}'):
        out.setdefault(r[0], []).append((r[1], r[2]))
    return out


def is_straight_video(orientations, genders):
    """The Male Cast rule for one video. orientations: its orientation values;
    genders: the genders of its cast. Straight = straight/heteroflexible, or no
    orientation with women and men and no trans performer."""
    o = {x.lower() for x in orientations}
    if o & OTHER_ORIENTATIONS:
        return False
    if o & STRAIGHT_ORIENTATIONS:
        return True
    g = [x.lower() for x in genders]
    return not o and 'male' in g and 'female' in g and 'trans' not in g


@router.get('/male-cast/map')
def male_cast_map(model=Depends(get_model)):
    """Every file path -> its Male Cast names, plus per-performer counts (local only)."""
    try:
        ensure_schema()
        conn = _conn()
        by_video = _male_by_video(conn)
        paths = {}
        if by_video:
            for r in _rows(conn, 'SELECT path, video_id FROM file_versions WHERE deleted_at IS NULL' if 'deleted_at' in
                           {c[1] for c in _rows(conn, 'PRAGMA table_info(file_versions)')} else 'SELECT path, video_id FROM file_versions'):
                if r[1] in by_video:
                    paths[r[0]] = sorted(by_video[r[1]])
        counts = Counter()
        for names in by_video.values():
            for n in names:
                counts[resolve_primary(conn, n)] += 1
        return {'success': True, 'map': paths, 'counts': dict(counts), 'videos': len(by_video)}
    except Exception as e:
        return _err(e)


class MaleCastSetBody(BaseModel):
    paths: list
    add: list = []          # names to mark as Male Cast (must already be in the video's cast)
    remove: list = []       # names to un-mark (they stay in the cast)


@router.post('/male-cast/set')
def male_cast_set(body: MaleCastSetBody, model=Depends(get_model)):
    try:
        ensure_schema()
        conn = _conn()
        vids = {r[0] for p in body.paths for r in [conn.execute('SELECT video_id FROM file_versions WHERE path = ?', (p,)).fetchone()] if r}
        return {'success': True, 'videos': len(vids), **male_cast_change(conn, vids, body.add or [], body.remove or [])}
    except Exception as e:
        return _err(e)


def male_cast_change(conn, video_ids, add, remove):
    """Mark / un-mark Male Cast on these videos (names must already be in their cast).
    Anyone added without a gender yet (e.g. a brand-new cast entry) becomes male;
    a gender that's already set is left alone and reported."""
    added = removed = missing = 0
    made_male, not_male = [], []
    for n in add:
        prim = resolve_primary(conn, str(n))
        row = conn.execute('SELECT gender FROM cast_gender WHERE cast_name = ?', (prim,)).fetchone()
        if not row:
            conn.execute("INSERT OR IGNORE INTO cast_gender (cast_name, gender) VALUES (?, 'male')", (prim,))
            made_male.append(prim)
        elif (row[0] or '').lower() != 'male':
            not_male.append({'name': prim, 'gender': row[0]})
    for vid in video_ids:
        members = {v.lower(): cid for cid, v in _junction_values_one(conn, vid)}
        for n in add:
            cid = members.get(str(n).lower())
            if cid is None:
                missing += 1
                continue
            added += conn.execute('INSERT OR IGNORE INTO video_cast_role (video_id, cast_id) VALUES (?, ?)', (vid, cid)).rowcount
        for n in remove:
            cid = members.get(str(n).lower())
            if cid is not None:
                removed += conn.execute('DELETE FROM video_cast_role WHERE video_id = ? AND cast_id = ?', (vid, cid)).rowcount
    conn.commit()
    _male_invalidate()
    return {'added': added, 'removed': removed, 'not_in_cast': missing, 'made_male': made_male, 'not_male': not_male}


def _junction_values_one(conn, video_id):
    return [(r[0], r[1]) for r in _rows(conn, 'SELECT vc.id, vc.value FROM video_cast_junction j JOIN video_cast vc ON vc.id = j.cast_id '
                                              'WHERE j.video_id = ?', (video_id,))]