# This file is: ./routes/videos.py

"""
Video listing and search routes.
"""

import os
import re
import urllib.parse
import threading
import time
from fastapi import APIRouter, Depends, Request
from typing import Optional, List, Dict, Tuple, Any
from functools import lru_cache
from helpers import check_auth, get_file_data, get_thumbnail_path
from media_handler import MediaHandler
from search_parser import SearchParser
from config import USE_SQLITE, LIST_FIELDS, HIDE_ZERO_LENGTH_DEFAULT

router = APIRouter(prefix="/api", tags=["videos"])

# Male Cast (ext_db.py): men in straight scenes stay in the cast but don't count
# when sorting by Cast. Falls back to the plain cast if ext_db isn't available.
try:
    from ext_db import male_cast_for_video as _male_cast_for_video
except Exception:
    try:
        from routes.ext_db import male_cast_for_video as _male_cast_for_video
    except Exception:
        _male_cast_for_video = None


def _without_male_cast(item, val):
    if not _male_cast_for_video or not isinstance(val, list):
        return val
    men = _male_cast_for_video(item.get('video_id'))
    if not men:
        return val
    rest = [c for c in val if c not in men]
    return rest or val


# Scene links (ext_db.py): the scene:linked / unlinked / review / stashdb / tpdb search
# keyword. It isn't a metadata field, so it's taken out of field_filters right after
# parsing and applied on its own. Ignored if ext_db isn't available.
try:
    from ext_db import scene_filter_sql as _scene_filter_sql, scene_filter_paths as _scene_filter_paths
except Exception:
    _scene_filter_sql = _scene_filter_paths = None


def _pop_scene_filter(field_filters):
    """Remove the scene: keyword from parsed filters -> its value (lowercase) or None."""
    vals = field_filters.pop('scene', None)
    for group in field_filters.get('__or_groups__') or []:
        group.pop('scene', None)
    if not vals:
        return None
    v = vals[0] if isinstance(vals, list) else vals
    if isinstance(v, tuple):
        v = v[1]
    return str(v).strip().lower() or None


def _add_scene_condition(kind, conditions, params, col='v.video_id'):
    if not kind or not _scene_filter_sql:
        return
    try:
        cond = _scene_filter_sql(kind, col)
    except Exception as e:
        print(f"scene filter unavailable: {e}")
        return
    if cond:
        conditions.append(cond[0])
        params.extend(cond[1])

def _add_display_condition(conditions, params, model, col='v.video_id', lib_col='v.library'):
    """The current user's library-type / orientation display filter (display_filter.py)."""
    import display_filter
    sql, p = display_filter.condition(model.db.get_connection(), model, col=col, lib_col=lib_col)
    if sql:
        conditions.append(sql)
        params.extend(p)

# Global references (will be set by web_server.py)
_model = None
_watch_history = None
_filter_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 30  # seconds - increased from 2 for better performance


def get_available_filters_cached(
    library, query, tags, tags_exclude, sites, sites_exclude, favorites_only, length_filter
):
    """Cache wrapper for available_filters with 2-second TTL."""
    import display_filter
    cache_key = (library, query, tags, tags_exclude, sites, sites_exclude, favorites_only, length_filter,
                 display_filter.cache_key(_model.db.get_connection()) if _model and USE_SQLITE else '')
    
    with _cache_lock:
        if cache_key in _filter_cache:
            cached_result, timestamp = _filter_cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return cached_result
        
        # Call the actual function
        result = _get_available_filters_uncached(
            library, query, tags, tags_exclude, sites, sites_exclude, favorites_only, length_filter
        )
        
        _filter_cache[cache_key] = (result, time.time())
        
        # Clean old entries (keep only last 50)
        if len(_filter_cache) > 50:
            sorted_items = sorted(_filter_cache.items(), key=lambda x: x[1][1])
            _filter_cache.clear()
            _filter_cache.update(dict(sorted_items[-25:]))
        
        return result

def get_model():
    """Dependency function to get model."""
    return _model

def get_watch_history():
    """Dependency function to get watch_history."""
    return _watch_history

def set_model(model):
    """Set the model instance."""
    global _model
    _model = model

def set_watch_history(watch_history):
    """Set the watch_history instance."""
    global _watch_history
    _watch_history = watch_history


def _unwrap_values(values):
    """
    Unwrap field filter values, extracting the string from ('EXACT', str) tuples.
    The search parser emits tuples for quoted searches like cast:"Abella Danger".
    For SQL purposes (LOWER(x) IN (?)) we just need the lowercase string —
    the distinction between fuzzy and exact matching is handled at the Python
    filter layer, not in the SQL subqueries.
    """
    out = []
    for v in values:
        if isinstance(v, tuple):
            out.append(str(v[1]).lower())
        else:
            out.append(str(v).lower())
    return out


def _build_or_groups_sql(field_filters, conditions, params, video_id_col='v.video_id'):
    """
    Generate SQL conditions for field_filters, respecting cross-field OR groups.

    Uses the '__or_groups__' key produced by SearchParser.parse_query to group
    OR-connected terms (potentially across different fields) into a single OR
    subquery block, while AND-connected groups each produce a separate condition
    that is ANDed into the WHERE clause.

    Example: "face:braces OR face:goth OR cumshot:bukkake"
      → one OR block:  v.video_id IN (...face=braces...) OR v.video_id IN (...face=goth...)
                        OR v.video_id IN (...cumshot=bukkake...)
      → appended as a single conditions entry

    Falls back to flat AND-of-each-field behaviour for callers that don't pass
    or_groups (i.e. __or_groups__ is missing).
    """
    or_groups = field_filters.get('__or_groups__', None)
    skip_fields = {'cast', 'path', '__or_groups__'}

    # Flat fields not covered by or_groups (date, resolution, etc.) are
    # handled by their own dedicated blocks — we only deal with LIST_FIELDS here.
    list_fields_lower = [f.lower() for f in LIST_FIELDS]

    if or_groups is None:
        # Legacy path: AND all junction fields
        for field, values in field_filters.items():
            if field in skip_fields:
                continue
            if field.lower() not in list_fields_lower:
                continue
            if not isinstance(values, list):
                values = [values]
            field_lower = field.lower()
            table_name = f'video_{field_lower}'
            junction_table = f'video_{field_lower}_junction'
            placeholders = ','.join(['?' for _ in values])
            conditions.append(f"""
                {video_id_col} IN (
                    SELECT vj.video_id FROM {junction_table} vj
                    JOIN {table_name} vt ON vj.{field_lower}_id = vt.id
                    WHERE LOWER(vt.value) IN ({placeholders})
                )
            """)
            params.extend(_unwrap_values(values))
        return

    # or_groups path: each group is ANDed; within a group, each {field:[values]}
    # is OR'd together.
    for group in or_groups:
        # Build one subquery per field in the group, OR them together
        group_subqueries = []
        group_params = []
        for field, values in group.items():
            if field in skip_fields:
                continue
            if field.lower() not in list_fields_lower:
                continue
            if not isinstance(values, list):
                values = [values]
            field_lower = field.lower()
            table_name = f'video_{field_lower}'
            junction_table = f'video_{field_lower}_junction'
            placeholders = ','.join(['?' for _ in values])
            group_subqueries.append(f"""
                {video_id_col} IN (
                    SELECT vj.video_id FROM {junction_table} vj
                    JOIN {table_name} vt ON vj.{field_lower}_id = vt.id
                    WHERE LOWER(vt.value) IN ({placeholders})
                )
            """)
            group_params.extend(_unwrap_values(values))

        if group_subqueries:
            conditions.append('(' + ' OR '.join(group_subqueries) + ')')
            params.extend(group_params)


def _get_videos_sql_fast(
    query, sort_by, desc, library, tags, tags_exclude, sites, sites_exclude,
    favorites_only, page, limit, hide_zero_length, length_filter, model, username, watch_history, seed=0
):
    """
    SQL-optimized video list - uses same fast pattern as cast bio.
    
    Strategy:
    1. Build SQL query with JOINs (like cast bio)
    2. Get paths matching filters
    3. Load metadata only for results needed
    
    Expected speedup: 10-20x for large result sets
    """
    import os
    
    # Parse filter sets
    required_tags = set(t.strip().lower() for t in tags.split(',') if t.strip()) if tags else set()
    excluded_tags_set = set(t.strip().lower() for t in tags_exclude.split(',') if t.strip()) if tags_exclude else set()
    required_sites = set(s.strip().lower() for s in sites.split(',') if s.strip()) if sites else set()
    excluded_sites_set = set(s.strip().lower() for s in sites_exclude.split(',') if s.strip()) if sites_exclude else set()
    
    base_query, field_filters, library_filters = SearchParser.parse_query(query)
    scene_kind = _pop_scene_filter(field_filters)
    tag_fields = [f.lower() for f in LIST_FIELDS if f.lower() not in ['site', 'cast']]
    
    conn = model.db.get_connection()
    cursor = conn.cursor()
    
    # Build WHERE conditions
    conditions = []
    params = []
    
    # Library filter
    allowed_prefixes = _get_library_prefixes(model, library, library_filters)
    if allowed_prefixes:
        path_conditions = ' OR '.join(['fv.path LIKE ?' for _ in allowed_prefixes])
        conditions.append(f"({path_conditions})")
        params.extend([f"{path}/%" for path in allowed_prefixes])
    
    conditions.append("fv.is_preferred = 1")
    conditions.append("fv.deleted_at IS NULL")
    _add_display_condition(conditions, params, model)
    
    if favorites_only:
        conditions.append("v.favorite = 1")
    
    if hide_zero_length == "hide":
        conditions.append("(fv.length IS NULL OR fv.length = '' OR fv.length != '0:00')")
    
    # Length filter (supports multiple ranges)
    if length_filter:
        try:
            ranges = [r.strip() for r in length_filter.split(',') if r.strip()]
            if ranges:
                length_conditions = []
                for range_str in ranges:
                    parts = range_str.split('-')
                    if len(parts) == 2:
                        min_seconds = int(parts[0])
                        max_seconds = int(parts[1])
                        
                        # Simple BETWEEN check with NULL handling
                        length_conditions.append("(fv.length_seconds IS NOT NULL AND fv.length_seconds BETWEEN ? AND ?)")
                        params.extend([min_seconds, max_seconds])
                
                if length_conditions:
                    # Combine with OR (any range matches)
                    conditions.append(f"({' OR '.join(length_conditions)})")
        except (ValueError, IndexError):
            # Invalid format, ignore
            pass
    
    # Cast filter
    if 'cast' in field_filters:
        cast_values = field_filters['cast']
        if not isinstance(cast_values, list):
            cast_values = [cast_values]
        
        cast_placeholders = ','.join(['?' for _ in cast_values])
        conditions.append(f"""
            v.video_id IN (
                SELECT vcj.video_id 
                FROM video_cast_junction vcj
                JOIN video_cast vc ON vcj.cast_id = vc.id
                WHERE LOWER(vc.value) IN ({cast_placeholders})
            )
        """)
        params.extend(_unwrap_values(cast_values))
    
    # Other field filters (respects cross-field OR groups)
    _build_or_groups_sql(field_filters, conditions, params, video_id_col='v.video_id')
    _add_scene_condition(scene_kind, conditions, params)

    if 'path' in field_filters:
        conditions.append("fv.path LIKE ?")
        params.append(f"%{field_filters['path']}%")

    # ── Date field filters ─────────────────────────────────────────────────────
    # Supports:  date_added:2024-03-15   date_added:2024-03   date_added:2024
    #            date_created:...        date_modified:...
    # A bare date in base_query (e.g. "2024-03-15") also searches only date_added
    # so it no longer pollutes results with videos merely modified on that date.
    DATE_FIELD_MAP = {
        'date_added':    'v.date_added',
        'added':         'v.date_added',
        'date_created':  'fv.date_created',
        'date_modified': 'fv.date_modified',
        'modified':      'fv.date_modified',
    }
    for filter_key, col in DATE_FIELD_MAP.items():
        if filter_key in field_filters:
            for val in field_filters[filter_key]:
                if isinstance(val, tuple):
                    val = val[1]
                val = str(val).strip()
                # Use LIKE so partial dates (year, year-month) work naturally
                conditions.append(f"{col} LIKE ?")
                params.append(f"{val}%")

    
    # Tag exclusions
    if excluded_tags_set:
        exclude_union_parts = []
        for field in tag_fields:
            table_name = f'video_{field}'
            junction_table = f'video_{field}_junction'
            exclude_tag_placeholders = ','.join(['?' for _ in excluded_tags_set])
            exclude_union_parts.append(f"""
                SELECT vtj.video_id
                FROM {junction_table} vtj
                JOIN {table_name} vt ON vtj.{field}_id = vt.id
                WHERE LOWER(vt.value) IN ({exclude_tag_placeholders})
            """)
        
        exclude_subquery = " UNION ".join(exclude_union_parts)
        conditions.append(f"v.video_id NOT IN ({exclude_subquery})")
        params.extend(list(excluded_tags_set) * len(tag_fields))
    
    # Site exclusions
    if excluded_sites_set:
        exclude_site_placeholders = ','.join(['?' for _ in excluded_sites_set])
        conditions.append(f"""
            v.video_id NOT IN (
                SELECT vsj.video_id
                FROM video_site_junction vsj
                JOIN video_site vs ON vsj.site_id = vs.id
                WHERE LOWER(vs.value) IN ({exclude_site_placeholders})
            )
        """)
        params.extend(list(excluded_sites_set))
    
    # Tag requirements
    for req_tag in required_tags:
        require_union_parts = []
        for field in tag_fields:
            table_name = f'video_{field}'
            junction_table = f'video_{field}_junction'
            require_union_parts.append(f"""
                SELECT vtj.video_id
                FROM {junction_table} vtj
                JOIN {table_name} vt ON vtj.{field}_id = vt.id
                WHERE LOWER(vt.value) = ?
            """)
        
        require_subquery = " UNION ".join(require_union_parts)
        conditions.append(f"v.video_id IN ({require_subquery})")
        params.extend([req_tag] * len(tag_fields))
    
    # Site requirements
    for req_site in required_sites:
        conditions.append(f"""
            v.video_id IN (
                SELECT vsj.video_id
                FROM video_site_junction vsj
                JOIN video_site vs ON vsj.site_id = vs.id
                WHERE LOWER(vs.value) = ?
            )
        """)
        params.append(req_site)
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    
    # 1. The matching rows with the scalar fields get_file returns (enough to sort on);
    #    a list field is only loaded when it is the sort column
    db = model.db
    cursor.execute(f"""
        SELECT DISTINCT {db.FILE_SCALAR_COLUMNS}
        FROM file_versions fv
        JOIN videos v ON fv.video_id = v.video_id
        WHERE {where_clause}
    """, params)
    rows = [dict(r) for r in cursor.fetchall()]
    sort_field = sort_by.lower().replace(' ', '_')
    if sort_field in db.FILE_LIST_FIELDS and rows:
        vids = {r['video_id'] for r in rows}
        values = db.list_values_by_video(sort_field, None if len(vids) > 5000 else vids)
        for r in rows:
            r[sort_field] = list(values.get(r['video_id'], []))

    # 2. Sort and page
    def sort_key(item):
        val = item.get(sort_field)
        if sort_by.lower() == 'cast': val = _without_male_cast(item, val)
        if val is None:
            return ""
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            return val.lower()
        if isinstance(val, list):
            return ', '.join(sorted(val, key=lambda x: str(x).lower())).lower()
        return val

    import library_stats
    special = library_stats.sort_field(sort_by)
    if special:                                  # Recently Added / Most Played / Last Played / Random
        library_stats.special_sort(cursor.connection, rows, special, username, seed, desc)
    else:
        rows.sort(key=lambda r: r['path'])       # ties keep a stable order (by path) across pages
        rows.sort(key=sort_key, reverse=desc)
    total = len(rows)
    start = (page - 1) * limit
    page_rows = rows[start:start + limit]

    # 3. Full records for this page only
    paginated_items = []
    for row in page_rows:
        path = row['path']
        file_data = db.get_file(path)
        if not file_data:
            continue
        
        # Get cache_id from database (explicit, unique per file_versions row),
        # falling back to MD5(filename) which is the legacy scheme thumbnails
        # were actually generated/keyed under for files with no explicit cache_id.
        cache_id = file_data.get('cache_id')
        if not cache_id:
            cache_id = MediaHandler._get_cache_key(path)
        
        item = file_data.copy()
        item.update({
            'type': 'file',
            'path': path,
            'filename': os.path.basename(path),
            'cache_id': cache_id,
            'thumb': get_thumbnail_path(cache_id),
            'supported': MediaHandler.is_browser_supported(
                file_data.get('extension', ''),
                file_data.get('codec', '')
            )
        })
        
        # Watch progress
        if username:
            progress = watch_history.get_progress(username, path)
            if progress:
                item['watch_progress'] = progress.get('progress', 0)
                item['watch_duration'] = progress.get('duration', 0)
        
        paginated_items.append(item)
    
    # Add version info
    for item in paginated_items:
        video_id = item.get('video_id')
        if video_id:
            try:
                versions = model.db.get_file_versions(video_id)
                if versions and len(versions) > 1:
                    item['versions'] = versions
                    item['all_codecs'] = model.db.get_all_codecs_for_video(video_id)
            except:
                pass
    
    return {
        "items": paginated_items,
        "total": total,
        "page": page,
        "limit": limit,
        "stats": {"total_files": total, "filtered_files": total},
        "tags": {},
        "sites": {}
    }

@router.get("/videos")
def get_videos(
    request: Request,
    model = Depends(get_model),
    query: Optional[str] = "", 
    sort_by: str = "Date Created", 
    desc: bool = True,
    library: str = "All Videos",
    tags: Optional[str] = None,  # Required tags (AND logic)
    tags_exclude: Optional[str] = None,  # Excluded tags (AND logic)
    sites: Optional[str] = None,  # Required sites (AND logic)
    sites_exclude: Optional[str] = None,  # Excluded sites (AND logic)
    tag_filter: Optional[str] = None,  # DEPRECATED: Keep for backward compatibility
    tag_exclude: Optional[str] = None,  # DEPRECATED: Keep for backward compatibility
    favorites_only: bool = False,
    page: int = 1,
    limit: int = 50,
    folder_mode: bool = False,
    current_folder: Optional[str] = None,
    hide_zero_length: Optional[str] = None,
    length_filter: Optional[str] = None,  # Format: "min-max" in seconds, e.g., "60-300"
    seed: int = 0,                        # Random sort: same order for one seed (stable paging)
    watch_history = Depends(get_watch_history)
):
    """
    Enhanced video listing with advanced search capabilities.
    """
    username = check_auth(request)
    
    # Parse the search query for advanced features
    base_query, field_filters, library_filters = SearchParser.parse_query(query)
    
    # ===== FAST SQL PATH (like cast bio) =====
    # Use for simple searches without folder mode or text search
    # This is 10-20x faster than Python iteration

    # Detect bare date queries (e.g. "2024-03-15", "2024-03", "2024") and
    # convert to a date_added field filter so they don't match date_modified too.
    import re as _re
    if base_query and not field_filters.get('date_added') and not field_filters.get('added'):
        date_only = _re.match(r'^(\d{4}(?:-\d{2}(?:-\d{2})?)?)$', base_query.strip())
        if date_only:
            field_filters['date_added'] = [date_only.group(1)]
            base_query = ''  # consumed — don't also do text search

    if USE_SQLITE and not folder_mode and not base_query:
        return _get_videos_sql_fast(
            query, sort_by, desc, library, tags, tags_exclude, sites, sites_exclude,
            favorites_only, page, limit, hide_zero_length, length_filter, model, username, watch_history, seed=seed
        )
    
    # ===== LEGACY PYTHON PATH =====
    # For folder mode, text search, or non-SQLite backends
    
    items = []
    folders_found = set()
    
    # Parse selected tags and sites (AND logic - case insensitive)
    required_tags = set(t.strip().lower() for t in tags.split(',') if t.strip()) if tags else set()
    excluded_tags_set = set(t.strip().lower() for t in tags_exclude.split(',') if t.strip()) if tags_exclude else set()
    required_sites = set(s.strip().lower() for s in sites.split(',') if s.strip()) if sites else set()
    excluded_sites_set = set(s.strip().lower() for s in sites_exclude.split(',') if s.strip()) if sites_exclude else set()
    
    # Parse the search query for advanced features
    base_query, field_filters, library_filters = SearchParser.parse_query(query)
    scene_paths = scene_negate = None
    scene_kind = _pop_scene_filter(field_filters)
    if scene_kind and _scene_filter_paths:
        try:
            scene_paths, scene_negate = _scene_filter_paths(scene_kind)
        except Exception as e:
            print(f"scene filter unavailable: {e}")
    
    # Special Detection for Cast Search in All Videos (clicked from UI)
    is_cast_search_all = (library == "All Videos" and 'cast' in field_filters and not library_filters)
    
    # 1. Determine Scope
    allowed_prefixes = []
    if library_filters:
        if 'all' in [lf.lower() for lf in library_filters]:
            allowed_prefixes = []
        else:
            for lib_filter in library_filters:
                if lib_filter in model.library_groups:
                    group_libs = model.get_libraries_in_group(lib_filter, recursive=True)
                    for lib in group_libs:
                        if lib in model.libraries:
                            allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib]])
                elif lib_filter in model.libraries:
                    allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib_filter]])
    elif library != "All Videos":
        if library in model.library_groups:
            group_libs = model.get_libraries_in_group(library, recursive=True)
            for lib in group_libs:
                if lib in model.libraries:
                    allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib]])
        elif library in model.libraries:
            allowed_prefixes = [os.path.normpath(p) for p in model.libraries.get(library, [])]

    # FOLDER NAVIGATION ROOT LOGIC
    if folder_mode and not current_folder:
        target_libs = {}
        if library == "All Videos" and not library_filters:
            target_libs = model.libraries
        elif library in model.library_groups:
            group_libs = model.get_libraries_in_group(library, recursive=True)
            for lib in group_libs:
                if lib in model.libraries:
                    target_libs[lib] = model.libraries[lib]
        elif library in model.libraries and not library_filters:
            target_libs = {library: model.libraries[library]}
        elif library_filters:
            for lib_filter in library_filters:
                if lib_filter in model.library_groups:
                    group_libs = model.get_libraries_in_group(lib_filter, recursive=True)
                    for lib in group_libs:
                        if lib in model.libraries:
                            target_libs[lib] = model.libraries[lib]
                elif lib_filter in model.libraries:
                    target_libs[lib_filter] = model.libraries[lib_filter]
        
        for lib_name, lib_paths in target_libs.items():
            for path in lib_paths:
                items.append({
                    "type": "folder", 
                    "path": path, 
                    "title": f"[{lib_name}] {os.path.basename(path)}", 
                    "file_count": "Lib"
                })
        
        return {
            "items": items, 
            "total": len(items), 
            "page": 1, 
            "limit": 1000, 
            "stats": {"total_files": 0, "filtered_files": 0}, 
            "tags": {},
            "sites": {}
        }

    # 2. Get files based on backend
    if USE_SQLITE:
        conn = model.db.get_connection()
        cursor = conn.cursor()
        if allowed_prefixes:
            conditions = ' OR '.join(['path LIKE ?' for _ in allowed_prefixes])
            query_sql = f'SELECT path FROM file_versions WHERE deleted_at IS NULL AND ({conditions})'
            params = [f"{path}/%" for path in allowed_prefixes]
            cursor.execute(query_sql, params)
        else:
            cursor.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL')
        paths = [row['path'] for row in cursor.fetchall()]
        import display_filter
        hidden = display_filter.hidden_paths(conn, model)
        if hidden:
            paths = [p for p in paths if p not in hidden]
    else:
        paths = list(model.files.keys())
    
    # Stats Collection
    total_files_in_scope = 0
    filtered_files_count = 0
    tag_counts = {}
    site_counts = {}
    browse_root = os.path.normpath(current_folder) if (folder_mode and current_folder) else None
    search_index = model.search_index if not USE_SQLITE else None

    # 3. Iterate through files (SQLite: every record loaded in one go, not ~19 queries per file)
    bulk = model.db.get_files_bulk(paths) if USE_SQLITE else None
    for path in paths:
        if path == "_ui_config": continue 
        data = bulk.get(path) if bulk is not None else get_file_data(model, path)
        if not data: continue
        
        # Scope Check
        in_scope = not allowed_prefixes or any(path.startswith(p + '/') for p in allowed_prefixes)
        if not in_scope: continue
        
        # Zero-length video filtering
        should_hide_zero = False
        if hide_zero_length == "hide":
            should_hide_zero = True
        elif hide_zero_length == "show":
            should_hide_zero = False
        else:
            lib_setting = model.library_settings.get(library, {}).get('hide_zero_length')
            should_hide_zero = (lib_setting == "hide") if lib_setting else (HIDE_ZERO_LENGTH_DEFAULT == "hide")
        
        if should_hide_zero:
            length_str = data.get('length', '')
            if length_str:
                try:
                    parts = length_str.split(':')
                    if len(parts) == 3 and sum(map(int, parts)) == 0: continue
                except: pass
        
        # Extract metadata for tag counting
        file_tags_lower = set()
        file_sites_lower = set()
        for field in LIST_FIELDS:
            f_lower = field.lower()
            vals = data.get(f_lower, [])
            if vals:
                if f_lower == 'site':
                    file_sites_lower.update(str(v).strip().lower() for v in vals if v)
                elif f_lower != 'cast':
                    file_tags_lower.update(str(v).strip().lower() for v in vals if v)

        # SPECIAL CASE: Restrict sidebar list for cast searches in 'All Videos'
        # In get_videos (this endpoint), we count for the 'total_files_in_scope' and 
        # tag_counts displayed in the header/sidebar.
        should_count_for_sidebar = True
        if is_cast_search_all:
            cast_filter = {'cast': field_filters['cast']}
            if not SearchParser.matches_field_filters(data, cast_filter):
                should_count_for_sidebar = False

        if should_count_for_sidebar:
            total_files_in_scope += 1
            for t in file_tags_lower: tag_counts[t] = tag_counts.get(t, 0) + 1
            for s in file_sites_lower: site_counts[s] = site_counts.get(s, 0) + 1

        # --- FILTERS ---
        if favorites_only and not data.get('favorite', False): continue
        
        # Length filter (supports multiple ranges)
        if length_filter:
            try:
                ranges = [r.strip() for r in length_filter.split(',') if r.strip()]
                if ranges:
                    # Get length_seconds from data or parse from length string
                    length_seconds = data.get('length_seconds')
                    if length_seconds is None:
                        # Fallback: parse from length string
                        length_str = data.get('length', '')
                        if length_str:
                            try:
                                time_parts = length_str.split(':')
                                if len(time_parts) == 3:  # H:M:S
                                    length_seconds = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
                                elif len(time_parts) == 2:  # M:S
                                    length_seconds = int(time_parts[0]) * 60 + int(time_parts[1])
                                elif len(time_parts) == 1:  # S
                                    length_seconds = int(time_parts[0])
                            except:
                                length_seconds = None
                    
                    # Check if video matches ANY of the selected ranges
                    if length_seconds is not None:
                        matches_any_range = False
                        for range_str in ranges:
                            parts = range_str.split('-')
                            if len(parts) == 2:
                                min_seconds = int(parts[0])
                                max_seconds = int(parts[1])
                                
                                # Simple BETWEEN check
                                if min_seconds <= length_seconds <= max_seconds:
                                    matches_any_range = True
                                    break
                        
                        if not matches_any_range:
                            continue
            except (ValueError, IndexError):
                pass  # Invalid format, ignore filter
            
        if folder_mode and browse_root:
            if not path.startswith(browse_root): continue
            rel_path = os.path.relpath(path, browse_root)
            if rel_path.startswith('..'): continue 
            parts = rel_path.split(os.sep)
            if len(parts) > 1:
                sub_folder_name = parts[0]
                full_sub_path = os.path.join(browse_root, sub_folder_name)
                if full_sub_path not in folders_found:
                    folders_found.add(full_sub_path)
                    items.append({"type": "folder", "path": full_sub_path, "title": sub_folder_name, "file_count": ""})
                continue 
        
        # Sidebar logic (AND)
        if required_tags and not required_tags.issubset(file_tags_lower): continue
        if excluded_tags_set and excluded_tags_set.intersection(file_tags_lower): continue
        if required_sites and not required_sites.issubset(file_sites_lower): continue
        if excluded_sites_set and excluded_sites_set.intersection(file_sites_lower): continue

        # Query Filters
        if scene_paths is not None and (path in scene_paths) == scene_negate: continue
        if field_filters:
            if 'path' in field_filters and not SearchParser.matches_path(path, field_filters['path']): continue
            other_filters = {k: v for k, v in field_filters.items() if k not in ('path', '__or_groups__')}
            if other_filters and not SearchParser.matches_field_filters(data, other_filters): continue

        if base_query:
            if USE_SQLITE:
                search_text = model._create_search_blob(path, data)
                if not SearchParser.matches_base_query(path, data, base_query, {path: search_text}): continue
            else:
                if not SearchParser.matches_base_query(path, data, base_query, search_index): continue
        
        # Add to Results
        filtered_files_count += 1
        item = data.copy()
        # Get cache_id from database (explicit, unique per file_versions row),
        # falling back to MD5(filename) which is the legacy scheme thumbnails
        # were actually generated/keyed under for files with no explicit cache_id.
        _cache_id = data.get('cache_id')
        if not _cache_id:
            _cache_id = MediaHandler._get_cache_key(path)
        item.update({
            'type': 'file', 'path': path, 'filename': os.path.basename(path),
            'cache_id': _cache_id,
            'thumb': get_thumbnail_path(_cache_id),
            'supported': MediaHandler.is_browser_supported(item.get('extension', ''), item.get('codec', ''))
        })
        
        if username:
            progress = watch_history.get_progress(username, path)
            if progress:
                item['watch_progress'] = progress.get('progress', 0)
                item['watch_duration'] = progress.get('duration', 0)
        
        items.append(item)

    # 4. Sort & Paginate
    folders_list = [i for i in items if i.get('type') == 'folder']
    files_list = [i for i in items if i.get('type') == 'file']

    def sort_key(item):
        val = item.get(sort_by.lower().replace(' ', '_'))
        if sort_by.lower() == 'cast': val = _without_male_cast(item, val)
        if val is None: return ""
        if isinstance(val, bool): return val
        if isinstance(val, (int, float)): return float(val)
        if isinstance(val, str): return val.lower()
        if isinstance(val, list): return ', '.join(sorted(val, key=lambda x: str(x).lower())).lower()
        return val

    import library_stats
    special = library_stats.sort_field(sort_by)
    if special:
        library_stats.special_sort(model.db.get_connection(), files_list, special, username, seed, desc)
    else:
        files_list.sort(key=sort_key, reverse=desc)
    items = folders_list + files_list
    total = len(items)
    start = (page - 1) * limit
    paginated_items = items[start:start + limit]

    # Enrich file items with version data
    for item in paginated_items:
        if item.get('type') == 'file':
            video_id = item.get('video_id')
            if video_id:
                # Get all versions for this video
                try:
                    versions = model.db.get_file_versions(video_id)
                    if versions and len(versions) > 1:
                        item['versions'] = versions
                        # Get all codecs for display
                        item['all_codecs'] = model.db.get_all_codecs_for_video(video_id)
                except Exception as e:
                    # If version methods not available yet, silently skip
                    pass

    return {
        "items": paginated_items, "total": total, "page": page, "limit": limit,
        "stats": {"total_files": total_files_in_scope, "filtered_files": filtered_files_count},
        "tags": tag_counts, "sites": site_counts
    }

@router.get("/available_filters")
def get_available_filters(
    request: Request,
    model = Depends(get_model),
    library: str = "All Videos",
    tags: Optional[str] = None,
    tags_exclude: Optional[str] = None,
    sites: Optional[str] = None,
    sites_exclude: Optional[str] = None,
    query: Optional[str] = "",
    favorites_only: bool = False,
    length_filter: Optional[str] = None
):
    """
    Get available tags and sites - WITH CACHING.
    """
    username = check_auth(request)
    
    # Use cached version
    return get_available_filters_cached(
        library, query, tags, tags_exclude, sites, sites_exclude, favorites_only, length_filter
    )


def _get_library_prefixes(model, library, library_filters):
    """Helper to get allowed path prefixes for library filtering."""
    allowed_prefixes = []
    
    if library_filters:
        if 'all' in [lf.lower() for lf in library_filters]:
            allowed_prefixes = []
        else:
            for lib_filter in library_filters:
                if lib_filter in model.library_groups:
                    group_libs = model.get_libraries_in_group(lib_filter, recursive=True)
                    for lib in group_libs:
                        if lib in model.libraries:
                            allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib]])
                elif lib_filter in model.libraries:
                    allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib_filter]])
    elif library != 'All Videos':
        if library in model.library_groups:
            group_libs = model.get_libraries_in_group(library, recursive=True)
            for lib in group_libs:
                if lib in model.libraries:
                    allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib]])
        elif library in model.libraries:
            allowed_prefixes = [os.path.normpath(p) for p in model.libraries[library]]
    
    return allowed_prefixes


def _get_available_filters_uncached(
    library, query, tags, tags_exclude, sites, sites_exclude, favorites_only, length_filter
):
    """
    SQL-OPTIMIZED VERSION: Calculate tag/site counts using database aggregation.
    
    This replaces Python iteration with direct SQL queries that use GROUP BY
    and COUNT. Much faster for large result sets (100x+ speedup for cast searches).
    """
    model = _model  # Access global model
    
    # Parse filters
    base_query, field_filters, library_filters = SearchParser.parse_query(query)
    scene_kind = _pop_scene_filter(field_filters)
    is_cast_search_all = (library == "All Videos" and 'cast' in field_filters and not library_filters)
    
    required_tags = set(t.strip().lower() for t in tags.split(',') if t.strip()) if tags else set()
    excluded_tags_set = set(t.strip().lower() for t in tags_exclude.split(',') if t.strip()) if tags_exclude else set()
    required_sites = set(s.strip().lower() for s in sites.split(',') if s.strip()) if sites else set()
    excluded_sites_set = set(s.strip().lower() for s in sites_exclude.split(',') if t.strip()) if sites_exclude else set()
    
    conn = model.db.get_connection()
    cursor = conn.cursor()
    
    # Define which fields are "tags" vs "sites"
    # "tags" = all LIST_FIELDS except 'site' and 'cast'
    tag_fields = [f.lower() for f in LIST_FIELDS if f.lower() not in ['site', 'cast']]
    
    # ===== Build base video filter =====
    base_conditions = []
    base_params = []
    
    # Library filter
    allowed_prefixes = _get_library_prefixes(model, library, library_filters)
    if allowed_prefixes:
        path_conditions = ' OR '.join(['fv.path LIKE ?' for _ in allowed_prefixes])
        base_conditions.append(f"({path_conditions})")
        base_params.extend([f"{path}/%" for path in allowed_prefixes])
    
    base_conditions.append("fv.deleted_at IS NULL")
    _add_display_condition(base_conditions, base_params, model)
    
    if favorites_only:
        base_conditions.append("v.favorite = 1")
    
    # Length filter (same logic as in get_videos)
    if length_filter:
        try:
            ranges = [r.strip() for r in length_filter.split(',') if r.strip()]
            if ranges:
                length_conditions = []
                for range_str in ranges:
                    parts = range_str.split('-')
                    if len(parts) == 2:
                        min_seconds = int(parts[0])
                        max_seconds = int(parts[1])
                        
                        length_conditions.append("(fv.length_seconds IS NOT NULL AND fv.length_seconds BETWEEN ? AND ?)")
                        base_params.extend([min_seconds, max_seconds])
                
                if length_conditions:
                    base_conditions.append(f"({' OR '.join(length_conditions)})")
        except (ValueError, IndexError):
            pass
    
    # Cast filter
    if 'cast' in field_filters:
        cast_values = field_filters['cast']
        if not isinstance(cast_values, list):
            cast_values = [cast_values]
        
        cast_placeholders = ','.join(['?' for _ in cast_values])
        base_conditions.append(f"""
            v.video_id IN (
                SELECT vcj.video_id 
                FROM video_cast_junction vcj
                JOIN video_cast vc ON vcj.cast_id = vc.id
                WHERE LOWER(vc.value) IN ({cast_placeholders})
            )
        """)
        base_params.extend(_unwrap_values(cast_values))
    
    # Other field filters (respects cross-field OR groups)
    _build_or_groups_sql(field_filters, base_conditions, base_params, video_id_col='v.video_id')
    _add_scene_condition(scene_kind, base_conditions, base_params)

    if 'path' in field_filters:
        base_conditions.append("fv.path LIKE ?")
        base_params.append(f"%{field_filters['path']}%")

    # Date field filters (mirrors _get_videos_sql_fast)
    _DATE_MAP = {
        'date_added': 'v.date_added', 'added': 'v.date_added',
        'date_created': 'fv.date_created',
        'date_modified': 'fv.date_modified', 'modified': 'fv.date_modified',
    }
    for _fk, _col in _DATE_MAP.items():
        if _fk in field_filters:
            for _val in field_filters[_fk]:
                if isinstance(_val, tuple): _val = _val[1]
                base_conditions.append(f"{_col} LIKE ?")
                base_params.append(f"{str(_val).strip()}%")

    base_where = " AND ".join(base_conditions) if base_conditions else "1=1"
    
    # ===== Get tags/sites in scope =====
    if is_cast_search_all:
        # Scope = videos matching cast only
        scope_conditions = ["fv.deleted_at IS NULL"]
        scope_params = []
        
        if 'cast' in field_filters:
            cast_values = field_filters['cast']
            if not isinstance(cast_values, list):
                cast_values = [cast_values]
            
            cast_placeholders = ','.join(['?' for _ in cast_values])
            scope_conditions.append(f"""
                v.video_id IN (
                    SELECT vcj.video_id 
                    FROM video_cast_junction vcj
                    JOIN video_cast vc ON vcj.cast_id = vc.id
                    WHERE LOWER(vc.value) IN ({cast_placeholders})
                )
            """)
            scope_params.extend(_unwrap_values(cast_values))
        _add_display_condition(scope_conditions, scope_params, model)
        
        scope_where = " AND ".join(scope_conditions)
    else:
        # Scope = everything in library
        scope_where = base_where
        scope_params = base_params
    
    # Get all tags in scope
    # "tags" includes ALL fields except 'site' and 'cast'
    
    # Build UNION query for all tag fields
    union_parts = []
    for field in tag_fields:
        table_name = f'video_{field}'
        junction_table = f'video_{field}_junction'
        union_parts.append(f"""
            SELECT DISTINCT LOWER(vt.value) as tag
            FROM {table_name} vt
            JOIN {junction_table} vtj ON vt.id = vtj.{field}_id
            JOIN videos v ON vtj.video_id = v.video_id
            JOIN file_versions fv ON v.video_id = fv.video_id
            WHERE {scope_where}
        """)
    
    tags_scope_query = " UNION ".join(union_parts)
    cursor.execute(tags_scope_query, scope_params * len(tag_fields))
    all_tags_in_scope = set(row['tag'] for row in cursor.fetchall())
    
    # Get all sites in scope
    sites_scope_query = f"""
        SELECT DISTINCT LOWER(vs.value) as site
        FROM video_site vs
        JOIN video_site_junction vsj ON vs.id = vsj.site_id
        JOIN videos v ON vsj.video_id = v.video_id
        JOIN file_versions fv ON v.video_id = fv.video_id
        WHERE {scope_where}
    """
    cursor.execute(sites_scope_query, scope_params)
    all_sites_in_scope = set(row['site'] for row in cursor.fetchall())
    
    # ===== Get counts after all filters =====
    filter_conditions = list(base_conditions)
    filter_params = list(base_params)
    
    # Exclude tags (check ALL tag fields, not just video_tags)
    if excluded_tags_set:
        # Need to check if video has this tag in ANY of the tag fields
        exclude_union_parts = []
        for field in tag_fields:
            table_name = f'video_{field}'
            junction_table = f'video_{field}_junction'
            exclude_tag_placeholders = ','.join(['?' for _ in excluded_tags_set])
            exclude_union_parts.append(f"""
                SELECT vtj.video_id
                FROM {junction_table} vtj
                JOIN {table_name} vt ON vtj.{field}_id = vt.id
                WHERE LOWER(vt.value) IN ({exclude_tag_placeholders})
            """)
        
        exclude_subquery = " UNION ".join(exclude_union_parts)
        filter_conditions.append(f"v.video_id NOT IN ({exclude_subquery})")
        filter_params.extend(list(excluded_tags_set) * len(tag_fields))
    
    # Exclude sites
    if excluded_sites_set:
        exclude_site_placeholders = ','.join(['?' for _ in excluded_sites_set])
        filter_conditions.append(f"""
            v.video_id NOT IN (
                SELECT vsj.video_id
                FROM video_site_junction vsj
                JOIN video_site vs ON vsj.site_id = vs.id
                WHERE LOWER(vs.value) IN ({exclude_site_placeholders})
            )
        """)
        filter_params.extend(list(excluded_sites_set))
    
    # Require tags (check ALL tag fields, not just video_tags)
    for req_tag in required_tags:
        # Need to check if video has this tag in ANY of the tag fields
        require_union_parts = []
        for field in tag_fields:
            table_name = f'video_{field}'
            junction_table = f'video_{field}_junction'
            require_union_parts.append(f"""
                SELECT vtj.video_id
                FROM {junction_table} vtj
                JOIN {table_name} vt ON vtj.{field}_id = vt.id
                WHERE LOWER(vt.value) = ?
            """)
        
        require_subquery = " UNION ".join(require_union_parts)
        filter_conditions.append(f"v.video_id IN ({require_subquery})")
        filter_params.extend([req_tag] * len(tag_fields))
    
    # Require sites
    for req_site in required_sites:
        filter_conditions.append(f"""
            v.video_id IN (
                SELECT vsj.video_id
                FROM video_site_junction vsj
                JOIN video_site vs ON vsj.site_id = vs.id
                WHERE LOWER(vs.value) = ?
            )
        """)
        filter_params.append(req_site)
    
    filter_where = " AND ".join(filter_conditions)
    
    # Count tags after filters
    # Build UNION query for all tag fields with counts
    union_count_parts = []
    for field in tag_fields:
        table_name = f'video_{field}'
        junction_table = f'video_{field}_junction'
        union_count_parts.append(f"""
            SELECT 
                LOWER(vt.value) as tag,
                COUNT(DISTINCT v.video_id) as count
            FROM {table_name} vt
            JOIN {junction_table} vtj ON vt.id = vtj.{field}_id
            JOIN videos v ON vtj.video_id = v.video_id
            JOIN file_versions fv ON v.video_id = fv.video_id
            WHERE {filter_where}
            GROUP BY LOWER(vt.value)
        """)
    
    tags_count_query = " UNION ALL ".join(union_count_parts)
    
    # Execute and sum counts for duplicate tags across different fields
    cursor.execute(tags_count_query, filter_params * len(tag_fields))
    
    # Aggregate counts from multiple fields
    tag_count_aggregator = {}
    for row in cursor.fetchall():
        tag = row['tag']
        count = row['count']
        tag_count_aggregator[tag] = tag_count_aggregator.get(tag, 0) + count
    
    filtered_tag_counts = tag_count_aggregator
    
    # Count sites after filters
    sites_count_query = f"""
        SELECT 
            LOWER(vs.value) as site,
            COUNT(DISTINCT v.video_id) as count
        FROM video_site vs
        JOIN video_site_junction vsj ON vs.id = vsj.site_id
        JOIN videos v ON vsj.video_id = v.video_id
        JOIN file_versions fv ON v.video_id = fv.video_id
        WHERE {filter_where}
        GROUP BY LOWER(vs.value)
        HAVING count > 0
    """
    cursor.execute(sites_count_query, filter_params)
    filtered_site_counts = {row['site']: row['count'] for row in cursor.fetchall()}
    
    return {
        "tags": {tag: filtered_tag_counts.get(tag, 0) for tag in all_tags_in_scope},
        "sites": {site: filtered_site_counts.get(site, 0) for site in all_sites_in_scope}
    }


def _get_available_filters_legacy(
    library, query, tags, tags_exclude, sites, sites_exclude, favorites_only
):
    """
    LEGACY VERSION: Python iteration (kept for rollback if needed).
    """
    model = _model  # Access global model
    
    required_tags = set(t.strip().lower() for t in tags.split(',') if t.strip()) if tags else set()
    excluded_tags_set = set(t.strip().lower() for t in tags_exclude.split(',') if t.strip()) if tags_exclude else set()
    required_sites = set(s.strip().lower() for s in sites.split(',') if s.strip()) if sites else set()
    excluded_sites_set = set(s.strip().lower() for s in sites_exclude.split(',') if s.strip()) if sites_exclude else set()
    
    base_query, field_filters, library_filters = SearchParser.parse_query(query)
    is_cast_search_all = (library == "All Videos" and 'cast' in field_filters and not library_filters)
    
    # Get allowed prefixes
    allowed_prefixes = []
    if library_filters:
        if 'all' in [lf.lower() for lf in library_filters]:
            allowed_prefixes = []
        else:
            for lib_filter in library_filters:
                if lib_filter in model.library_groups:
                    group_libs = model.get_libraries_in_group(lib_filter, recursive=True)
                    for lib in group_libs:
                        if lib in model.libraries:
                            allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib]])
                elif lib_filter in model.libraries:
                    allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib_filter]])
    elif library != 'All Videos':
        if library in model.library_groups:
            group_libs = model.get_libraries_in_group(library, recursive=True)
            for lib in group_libs:
                if lib in model.libraries:
                    allowed_prefixes.extend([os.path.normpath(p) for p in model.libraries[lib]])
        elif library in model.libraries:
            allowed_prefixes = [os.path.normpath(p) for p in model.libraries[library]]
    
    if USE_SQLITE:
        conn = model.db.get_connection()
        cursor = conn.cursor()
        if allowed_prefixes:
            conditions = ' OR '.join(['path LIKE ?' for _ in allowed_prefixes])
            query_sql = f'SELECT path FROM file_versions WHERE deleted_at IS NULL AND ({conditions})'
            params = [f"{path}/%" for path in allowed_prefixes]
            cursor.execute(query_sql, params)
        else:
            cursor.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL')
        paths = [row['path'] for row in cursor.fetchall()]
        import display_filter
        hidden = display_filter.hidden_paths(conn, model)
        if hidden:
            paths = [p for p in paths if p not in hidden]
    else:
        paths = list(model.files.keys())
    
    # Load file data (no batch loading - it's broken)
    file_data_map = {}
    for path in paths:
        if path == "_ui_config":
            continue
        data = get_file_data(model, path)
        if data:
            file_data_map[path] = data
    
    # Now process with the pre-loaded data (MUCH FASTER)
    all_tags_in_scope = set()
    all_sites_in_scope = set()
    filtered_tag_counts = {}
    filtered_site_counts = {}
    search_index = model.search_index if not USE_SQLITE else None

    for path in paths:
        if path == "_ui_config":
            continue
        
        data = file_data_map.get(path)
        if not data:
            continue
        
        in_scope = not allowed_prefixes or any(path.startswith(p + '/') for p in allowed_prefixes)
        if not in_scope:
            continue
        
        file_tags_lower = set()
        file_sites_lower = set()
        for field in LIST_FIELDS:
            f_lower = field.lower()
            vals = data.get(f_lower, [])
            if vals:
                if f_lower == 'site':
                    file_sites_lower.update(str(v).strip().lower() for v in vals if v)
                elif f_lower != 'cast':
                    file_tags_lower.update(str(v).strip().lower() for v in vals if v)
        
        # Scope population logic
        if is_cast_search_all:
            cast_filter = {'cast': field_filters['cast']}
            if SearchParser.matches_field_filters(data, cast_filter):
                all_tags_in_scope.update(file_tags_lower)
                all_sites_in_scope.update(file_sites_lower)
        else:
            all_tags_in_scope.update(file_tags_lower)
            all_sites_in_scope.update(file_sites_lower)
        
        # Filtering logic
        passes_all_filters = True
        if favorites_only and not data.get('favorite', False):
            passes_all_filters = False
        if passes_all_filters and field_filters:
            if 'path' in field_filters and not SearchParser.matches_path(path, field_filters['path']):
                passes_all_filters = False
            if passes_all_filters:
                other_filters = {k: v for k, v in field_filters.items() if k not in ('path', '__or_groups__')}
                if other_filters and not SearchParser.matches_field_filters(data, other_filters):
                    passes_all_filters = False

        if passes_all_filters and base_query:
            if USE_SQLITE:
                search_text = model._create_search_blob(path, data)
                if not SearchParser.matches_base_query(path, data, base_query, {path: search_text}):
                    passes_all_filters = False
            else:
                if not SearchParser.matches_base_query(path, data, base_query, search_index):
                    passes_all_filters = False
        
        if passes_all_filters:
            if required_tags and not required_tags.issubset(file_tags_lower):
                passes_all_filters = False
            if excluded_tags_set and excluded_tags_set.intersection(file_tags_lower):
                passes_all_filters = False
            if required_sites and not required_sites.issubset(file_sites_lower):
                passes_all_filters = False
            if excluded_sites_set and excluded_sites_set.intersection(file_sites_lower):
                passes_all_filters = False
        
        if passes_all_filters:
            for tag in file_tags_lower:
                filtered_tag_counts[tag] = filtered_tag_counts.get(tag, 0) + 1
            for site in file_sites_lower:
                filtered_site_counts[site] = filtered_site_counts.get(site, 0) + 1
    
    return {
        "tags": {tag: filtered_tag_counts.get(tag, 0) for tag in all_tags_in_scope},
        "sites": {site: filtered_site_counts.get(site, 0) for site in all_sites_in_scope}
    }

@router.get("/browse")
def browse_filesystem(path: str = "/", model = Depends(get_model)):
    """
    Browse the filesystem for folder selection.

    Shows a virtual root listing only the allowed mount points (config.MEDIA_ROOTS,
    default /media and /media0 - where the host's media folders are mounted).

    Browsing is restricted to within these roots so nothing else on the
    container filesystem is accessible. Existing library paths stored as
    /media/... are completely unaffected.
    """
    from config import MEDIA_ROOTS
    ALLOWED_ROOTS = list(MEDIA_ROOTS)
    VIRTUAL_ROOT  = "/"

    path = os.path.normpath(path) if path != VIRTUAL_ROOT else VIRTUAL_ROOT

    def is_allowed(p):
        if p == VIRTUAL_ROOT:
            return True
        return any(
            p == root or p.startswith(root + os.sep)
            for root in ALLOWED_ROOTS
        )

    if not is_allowed(path):
        path = VIRTUAL_ROOT
    if path != VIRTUAL_ROOT and not os.path.exists(path):
        path = VIRTUAL_ROOT

    entries = []

    if path == VIRTUAL_ROOT:
        # Virtual root — show only the allowed mount points that exist on disk
        for root in ALLOWED_ROOTS:
            if os.path.isdir(root):
                entries.append({
                    "name": root.lstrip("/"),
                    "path": root,
                    "type": "dir"
                })
    else:
        # ".." goes up, but not above the allowed root into real filesystem
        parent = os.path.dirname(path)
        if parent == path:
            parent = VIRTUAL_ROOT
        entries.append({"name": "..", "path": parent, "type": "dir"})

        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=True):
                        entries.append({
                            "name": entry.name,
                            "path": entry.path,
                            "type": "dir"
                        })
        except PermissionError:
            pass

    entries.sort(key=lambda x: (x["name"] != "..", x["name"].lower()))
    return {"current_path": path, "entries": entries}

@router.post("/videos/set_preferred_version")
async def set_preferred_version(request: Request, model = Depends(get_model)):
    """Set preferred version for a video."""
    from fastapi.responses import JSONResponse
    
    try:
        # Parse request body correctly
        data = await request.json()
        
        video_id = data.get('video_id')
        path = data.get('path')
        
        if not video_id or not path:
            return JSONResponse({
                'error': 'Missing video_id or path'
            }, status_code=400)
        
        # Set preferred version
        success = model.db.set_preferred_version(video_id, path)
        
        if success:
            return JSONResponse({'success': True})
        else:
            return JSONResponse({
                'error': 'Failed to set preferred version'
            }, status_code=500)
            
    except Exception as e:
        print(f"Error setting preferred version: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse({
            'error': str(e)
        }, status_code=500)


@router.post("/videos/merge")
async def merge_videos(request: Request, model = Depends(get_model)):
    """Manually merge multiple files into a single video."""
    from fastapi.responses import JSONResponse
    from datetime import datetime
    import os
    from collections import defaultdict
    
    try:
        data = await request.json()
        paths = data.get('paths', [])
        primary_path = data.get('primary_path')
        video_title = data.get('title')
        
        if len(paths) < 2:
            return JSONResponse({'error': 'Need at least 2 files to merge'}, status_code=400)
        
        # Get first file to determine library
        first_path = paths[0]
        library = model.db._get_library_for_path(first_path)
        
        # Use provided title or first filename
        # Empty string should default to first filename
        if video_title and video_title.strip():
            filename_base = video_title.strip()
        else:
            filename_base = os.path.splitext(os.path.basename(first_path))[0]
        
        # Generate video_id
        video_id = f"{library}_{filename_base}".replace(' ', '_').replace('/', '_').replace('\\', '_')
        
        # CRITICAL: Collect metadata from source videos BEFORE deleting them
        merged_metadata = defaultdict(set)
        source_video_ids = set()
        
        for path in paths:
            try:
                old_video = model.db.get_video_by_path(path)
                if old_video and old_video['video_id'] != video_id:
                    source_video_ids.add(old_video['video_id'])
                    
                    # Get full video data including list metadata
                    video_data = model.db.get_file(path)
                    
                    # Collect all list field values
                    from config import LIST_FIELDS
                    for field in LIST_FIELDS:
                        field_lower = field.lower()
                        values = video_data.get(field_lower, [])
                        if values:
                            for value in values:
                                merged_metadata[field_lower].add(value)
                    
                    # Collect scalar metadata (take first non-empty value)
                    for key in ['year', 'rating']:
                        value = video_data.get(key)
                        if value and key not in merged_metadata:
                            merged_metadata[key] = value
                    
                    # If any source video is a favorite, the merged video should be too
                    if video_data.get('favorite') and 'favorite' not in merged_metadata:
                        merged_metadata['favorite'] = 1
            except:
                pass
        
        # Check if video already exists
        try:
            existing_video = model.db.get_video_by_id(video_id)
        except:
            existing_video = None
        
        if not existing_video:
            # Create new video with basic metadata
            base_metadata = {
                'title': filename_base, 
                'date_added': datetime.now().isoformat()
            }
            # Include scalar metadata if collected
            if 'year' in merged_metadata:
                base_metadata['year'] = merged_metadata['year']
            if 'rating' in merged_metadata:
                base_metadata['rating'] = merged_metadata['rating']
            if 'favorite' in merged_metadata:
                base_metadata['favorite'] = merged_metadata['favorite']
            
            model.db.create_video(video_id, filename_base, library, base_metadata)
        else:
            # If merging into an existing video, promote favorite status if any source is favorited
            if 'favorite' in merged_metadata and not existing_video.get('favorite'):
                conn = model.db.get_connection()
                conn.execute('UPDATE videos SET favorite = 1 WHERE video_id = ?', (video_id,))
                conn.commit()
        
        # Add all paths as versions (this will remove them from old videos)
        for path in paths:
            filename = os.path.basename(path)
            info = MediaHandler.get_file_info(path)
            
            try:
                stats = os.stat(path)
                c_time = datetime.fromtimestamp(stats.st_ctime).strftime('%Y-%m-%d %H:%M')
                m_time = datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M')
            except:
                c_time, m_time = "", ""
            
            metadata = {
                'resolution': info.get('resolution', ''),
                'codec': info.get('codec', ''),
                'extension': info.get('extension', ''),
                'length': info.get('length', ''),
                'total_bitrate': info.get('total_bitrate', ''),
                'file_size': info.get('file_size', ''),
                'date_created': c_time,
                'date_modified': m_time,
                'cache_id': MediaHandler._get_cache_key(path)
            }
            
            # Remove from old video if exists
            try:
                old_video = model.db.get_video_by_path(path)
                if old_video and old_video['video_id'] != video_id:
                    model.db.remove_file_version(path)
            except:
                pass
            
            # Add to new video
            model.db.add_file_version(video_id, path, filename, metadata)
        
        # Apply merged metadata to the new video
        if merged_metadata:
            conn = model.db.get_connection()
            cursor = conn.cursor()
            
            # Add list field metadata to video-level junction tables
            from config import LIST_FIELDS
            for field in LIST_FIELDS:
                field_lower = field.lower()
                if field_lower not in merged_metadata:
                    continue
                
                values = merged_metadata[field_lower]
                if not values:
                    continue
                
                table_name = f'video_{field_lower}'
                junction_table = f'video_{field_lower}_junction'
                
                # Insert values into junction tables
                for value in values:
                    if not value:
                        continue
                    
                    # Get or create value ID
                    cursor.execute(f'SELECT id FROM {table_name} WHERE value = ?', (value,))
                    row = cursor.fetchone()
                    
                    if row:
                        value_id = row['id']
                    else:
                        cursor.execute(f'INSERT INTO {table_name} (value) VALUES (?)', (value,))
                        value_id = cursor.lastrowid
                    
                    # Create junction entry (use INSERT OR IGNORE to avoid duplicates)
                    cursor.execute(
                        f'INSERT OR IGNORE INTO {junction_table} (video_id, {field_lower}_id) VALUES (?, ?)',
                        (video_id, value_id)
                    )
            
            conn.commit()
        
        # Set primary version
        if primary_path:
            model.db.set_preferred_version(video_id, primary_path)
        else:
            model.db.auto_set_preferred(video_id)
        
        return JSONResponse({'success': True, 'video_id': video_id})
        
    except Exception as e:
        print(f"Error merging videos: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post("/videos/split")
async def split_video(request: Request, model = Depends(get_model)):
    """Split a video's versions back into separate video entries."""
    from fastapi.responses import JSONResponse
    from datetime import datetime
    import os
    
    try:
        data = await request.json()
        video_id = data.get('video_id')
        
        if not video_id:
            return JSONResponse({'error': 'Missing video_id'}, status_code=400)
        
        video = model.db.get_video_by_id(video_id)
        if not video or not video.get('versions'):
            return JSONResponse({'error': 'Video not found or has no versions'}, status_code=400)
        
        if len(video['versions']) < 2:
            return JSONResponse({'error': 'Video only has one version, cannot split'}, status_code=400)
        
        versions = video['versions'].copy()
        
        # CRITICAL: Get metadata from the original video BEFORE deleting it
        original_metadata = {}
        try:
            # Get full video data including list metadata
            conn = model.db.get_connection()
            cursor = conn.cursor()
            
            # Get scalar metadata
            cursor.execute('''
                SELECT title, year, rating, favorite
                FROM videos 
                WHERE video_id = ?
            ''', (video_id,))
            
            video_row = cursor.fetchone()
            if video_row:
                original_metadata['year'] = video_row['year']
                original_metadata['rating'] = video_row['rating']
                original_metadata['favorite'] = video_row['favorite']
            
            # Get all list field metadata
            from config import LIST_FIELDS
            for field in LIST_FIELDS:
                field_lower = field.lower()
                table_name = f'video_{field_lower}'
                junction_table = f'video_{field_lower}_junction'
                
                cursor.execute(f'''
                    SELECT {table_name}.value FROM {table_name}
                    JOIN {junction_table} ON {table_name}.id = {junction_table}.{field_lower}_id
                    WHERE {junction_table}.video_id = ?
                    ORDER BY {table_name}.value
                ''', (video_id,))
                
                original_metadata[field_lower] = [row[0] for row in cursor.fetchall()]
        except Exception as e:
            print(f"Warning: Could not retrieve original metadata: {e}")
        
        # Delete the combined video
        for version in versions:
            model.db.remove_file_version(version['path'])
        
        # Recreate each as separate video with SAME metadata
        new_video_ids = []
        for version in versions:
            path = version['path']
            filename = version['filename']
            filename_base, ext = os.path.splitext(filename)
            library = model.db._get_library_for_path(path)
            
            # Use a hash of the full path to make the video_id unique per file,
            # even when multiple files share the same filename (e.g. "Scene 4.mp4"
            # in ten different folders). Without this, all split versions get the
            # same video_id and immediately re-merge.
            import hashlib
            path_hash = hashlib.md5(path.encode('utf-8')).hexdigest()[:8]
            new_video_id = f"{library}_{filename_base}_{path_hash}".replace(' ', '_').replace('/', '_').replace('\\', '_')
            new_video_ids.append(new_video_id)
            
            # Create video with original scalar metadata
            metadata = {
                'title': filename_base, 
                'date_added': datetime.now().isoformat()
            }
            if original_metadata.get('year'):
                metadata['year'] = original_metadata['year']
            if original_metadata.get('rating'):
                metadata['rating'] = original_metadata['rating']
            if original_metadata.get('favorite'):
                metadata['favorite'] = original_metadata['favorite']
            
            model.db.create_video(new_video_id, filename_base, library, metadata)
            
            # Add this file as only version
            version_metadata = {
                'resolution': version.get('resolution', ''),
                'codec': version.get('codec', ''),
                'extension': version.get('extension', ''),
                'length': version.get('length', ''),
                'total_bitrate': version.get('total_bitrate', ''),
                'file_size': version.get('file_size', ''),
                'date_created': version.get('date_created', ''),
                'date_modified': version.get('date_modified', ''),
                'cache_id': version.get('cache_id', '')
            }
            model.db.add_file_version(new_video_id, path, filename, version_metadata)
            model.db.set_preferred_version(new_video_id, path)
            
            # Apply original list metadata to this split video
            if original_metadata:
                conn = model.db.get_connection()
                cursor = conn.cursor()
                
                from config import LIST_FIELDS
                for field in LIST_FIELDS:
                    field_lower = field.lower()
                    values = original_metadata.get(field_lower, [])
                    if not values:
                        continue
                    
                    table_name = f'video_{field_lower}'
                    junction_table = f'video_{field_lower}_junction'
                    
                    # Insert values into junction tables
                    for value in values:
                        if not value:
                            continue
                        
                        # Get or create value ID
                        cursor.execute(f'SELECT id FROM {table_name} WHERE value = ?', (value,))
                        row = cursor.fetchone()
                        
                        if row:
                            value_id = row['id']
                        else:
                            cursor.execute(f'INSERT INTO {table_name} (value) VALUES (?)', (value,))
                            value_id = cursor.lastrowid
                        
                        # Create junction entry (use INSERT OR IGNORE to avoid duplicates)
                        cursor.execute(
                            f'INSERT OR IGNORE INTO {junction_table} (video_id, {field_lower}_id) VALUES (?, ?)',
                            (new_video_id, value_id)
                        )
                
                conn.commit()
        
        return JSONResponse({
            'success': True, 
            'count': len(versions),
            'video_ids': new_video_ids
        })
        
    except Exception as e:
        print(f"Error splitting video: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse({'error': str(e)}, status_code=500)

@router.get("/duplicates/find")
def find_duplicates(library: str = None, model = Depends(get_model)):
    """
    Find files with same filename in different directories within the same library.
    Returns groups of potential duplicates for user review.
    """
    if not USE_SQLITE:
        return {"duplicates": []}
    
    conn = model.db.get_connection()
    cursor = conn.cursor()
    
    # Build query
    if library and library != "All Videos":
        query = '''
            SELECT 
                fv.filename,
                fv.path,
                fv.video_id,
                fv.file_size,
                fv.length,
                fv.codec,
                fv.total_bitrate,
                fv.resolution,
                fv.date_created,
                v.library
            FROM file_versions fv
            JOIN videos v ON fv.video_id = v.video_id
            WHERE v.library = ? AND fv.deleted_at IS NULL
            ORDER BY fv.filename, fv.path
        '''
        cursor.execute(query, (library,))
    else:
        query = '''
            SELECT 
                fv.filename,
                fv.path,
                fv.video_id,
                fv.file_size,
                fv.length,
                fv.codec,
                fv.total_bitrate,
                fv.resolution,
                fv.date_created,
                v.library
            FROM file_versions fv
            JOIN videos v ON fv.video_id = v.video_id
            WHERE fv.deleted_at IS NULL
            ORDER BY fv.filename, fv.path
        '''
        cursor.execute(query)
    
    rows = cursor.fetchall()
    
    # Group by filename
    from collections import defaultdict
    filename_groups = defaultdict(list)
    
    for row in rows:
        file_dict = dict(row)
        filename = file_dict['filename']
        filename_groups[filename].append(file_dict)
    
    # Find duplicates - same filename, different directories, same library
    duplicates = []
    
    for filename, files in filename_groups.items():
        if len(files) < 2:
            continue
        
        # Group by library
        library_groups = defaultdict(list)
        for f in files:
            library_groups[f['library']].append(f)
        
        # Check each library for duplicates
        for lib, lib_files in library_groups.items():
            if len(lib_files) < 2:
                continue
            
            # Check if they're in different directories
            directories = set()
            for f in lib_files:
                import os
                directory = os.path.dirname(f['path'])
                directories.add(directory)
            
            if len(directories) > 1:
                # These are duplicates - same filename, same library, different dirs
                duplicates.append({
                    'filename': filename,
                    'library': lib,
                    'files': lib_files,
                    'count': len(lib_files)
                })
    
    return {"duplicates": duplicates, "total_groups": len(duplicates)}


@router.get("/file-changes")
def get_file_changes(library: str = None, model = Depends(get_model)):
    """Get file changes for review."""
    if not USE_SQLITE:
        return {"changes": []}
    
    # Get changes for the specified library
    changes = model.db.get_file_changes(
        library=library,
        flag_types=['possibly_replaced', 'possible_replacement', 'deleted', 'possible_duplicate']
    )
    
    # For cross-library replacements, we need to fetch the related file from the other library
    all_change_ids = set()
    for change in changes:
        all_change_ids.add(change['id'])
        if change['related_id']:
            all_change_ids.add(change['related_id'])
    
    # Get all related changes (including from other libraries)
    conn = model.db.get_connection()
    cursor = conn.cursor()
    
    if all_change_ids:
        placeholders = ','.join('?' * len(all_change_ids))
        cursor.execute(f'''
            SELECT * FROM file_changes 
            WHERE id IN ({placeholders})
            ORDER BY flagged_at DESC
        ''', list(all_change_ids))
        all_changes = [dict(row) for row in cursor.fetchall()]
    else:
        all_changes = changes
    
    # Group replacements together
    grouped = []
    processed_ids = set()
    
    for change in changes:
        if change['id'] in processed_ids:
            continue
        
        if change['flag_type'] == 'possibly_replaced' and change['related_id']:
            # Find the replacement (might be in a different library)
            replacement = next((c for c in all_changes if c['id'] == change['related_id']), None)
            
            if replacement:
                grouped.append({
                    'type': 'replacement_pair',
                    'original': change,
                    'replacement': replacement
                })
                processed_ids.add(change['id'])
                processed_ids.add(replacement['id'])
        elif change['flag_type'] == 'possible_replacement' and change['related_id']:
            # This is the replacement side - find the original
            original = next((c for c in all_changes if c['id'] == change['related_id']), None)
            
            if original:
                grouped.append({
                    'type': 'replacement_pair',
                    'original': original,
                    'replacement': change
                })
                processed_ids.add(change['id'])
                processed_ids.add(original['id'])
        elif change['flag_type'] == 'possible_duplicate' and change['related_id']:
            # Find the other duplicate
            other = next((c for c in all_changes if c['id'] == change['related_id']), None)
            
            if other and other['id'] not in processed_ids:
                grouped.append({
                    'type': 'duplicate_pair',
                    'file1': change,
                    'file2': other
                })
                processed_ids.add(change['id'])
                processed_ids.add(other['id'])
        elif change['flag_type'] == 'deleted' and not change['related_id']:
            # Deleted without replacement
            grouped.append({
                'type': 'deleted',
                'file': change
            })
            processed_ids.add(change['id'])

    # Enrich each change with cache_id so UI can find thumbnails without spamming 404s.
    # Thumbnails are keyed by cache_id (stored in file_versions), not video_id.
    import hashlib as _hashlib
    def _enrich(ch):
        if not ch:
            return
        fp = ch.get('filepath')
        if not fp:
            return
        cursor.execute('SELECT cache_id FROM file_versions WHERE path = ? LIMIT 1', (fp,))
        row = cursor.fetchone()
        if row and row['cache_id']:
            ch['cache_id'] = row['cache_id']
        else:
            ch['cache_id'] = _hashlib.md5(fp.encode()).hexdigest()

    for g in grouped:
        t = g.get('type')
        if t == 'replacement_pair':
            _enrich(g.get('original'))
            _enrich(g.get('replacement'))
        elif t == 'duplicate_pair':
            _enrich(g.get('file1'))
            _enrich(g.get('file2'))
        elif t == 'deleted':
            _enrich(g.get('file'))

    return {"changes": grouped}


@router.post("/file-changes/merge")
async def merge_file_change(request: Request, model = Depends(get_model)):
    """Merge a replaced file with its replacement - transfer metadata from deleted to active file."""
    data = await request.json()
    original_id = data.get('original_id')  # The 'possibly_replaced' (deleted) file
    replacement_id = data.get('replacement_id')  # The 'possible_replacement' (new) file
    
    if not USE_SQLITE:
        return {"success": False, "error": "SQLite required"}
    
    conn = model.db.get_connection()
    cursor = conn.cursor()
    
    # Get both file changes
    cursor.execute('SELECT * FROM file_changes WHERE id = ?', (original_id,))
    original_row = cursor.fetchone()
    
    cursor.execute('SELECT * FROM file_changes WHERE id = ?', (replacement_id,))
    replacement_row = cursor.fetchone()
    
    if not original_row or not replacement_row:
        return {"success": False, "error": "File changes not found"}
    
    original = dict(original_row)
    replacement = dict(replacement_row)
    
    # The "original" should be the 'possibly_replaced' (deleted) one with metadata
    # The "replacement" should be the 'possible_replacement' (new) one
    
    if original['flag_type'] != 'possibly_replaced' or replacement['flag_type'] != 'possible_replacement':
        return {"success": False, "error": "Invalid flag types for merge"}
    
    old_video_id = original['video_id']  # Has the metadata we want to keep
    old_filepath = original['filepath']  # Deleted file path
    new_filepath = replacement['filepath']  # Active file path
    
    if not old_video_id:
        return {"success": False, "error": "No metadata to transfer"}
    
    # ── Discover all junction tables ──────────────────────────────────────────────────
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'video_%_junction'"
    )
    junction_tables = [r[0] for r in cursor.fetchall()]

    # ── Get new file's current video_id ──────────────────────────────────────────────
    cursor.execute('SELECT video_id FROM file_versions WHERE path = ?', (new_filepath,))
    row = cursor.fetchone()
    new_video_id = row['video_id'] if row else None

    if not new_video_id:
        return {"success": False, "error": "New file not in database — rescan the target library first"}

    if new_video_id == old_video_id:
        # Already pointing at the right video_id — nothing to do
        conn.commit()
        model.db.clear_file_change(original_id)
        model.db.clear_file_change(replacement_id)
        return {"success": True}

    # ── Ensure old_video_id exists in videos table ───────────────────────────────────────
    # May have been cleaned up by finalize_deletions; reconstruct if needed.
    cursor.execute('SELECT 1 FROM videos WHERE video_id = ?', (old_video_id,))
    if not cursor.fetchone():
        cursor.execute('SELECT * FROM videos WHERE video_id = ?', (new_video_id,))
        nv = cursor.fetchone()
        if not nv:
            return {"success": False, "error": "Cannot restore metadata — videos rows missing for both IDs"}
        d = dict(nv)
        cursor.execute("""
            INSERT OR IGNORE INTO videos
                (video_id, filename_base, library, title, year, rating,
                 favorite, date_added, date_last_edited)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (old_video_id, d['filename_base'], replacement['library'],
              d['title'], d['year'], d['rating'],
              d['favorite'], d['date_added'], d['date_last_edited']))
    else:
        # Update library to reflect new location
        cursor.execute('UPDATE videos SET library = ? WHERE video_id = ?',
                       (replacement['library'], old_video_id))

    # ── Re-point file_versions to old_video_id (which has all the metadata) ──
    cursor.execute(
        'UPDATE file_versions SET video_id = ? WHERE video_id = ?',
        (old_video_id, new_video_id)
    )

    # ── Delete new_video_id cleanly (junctions first, then videos row) ───────
    for tbl in junction_tables:
        cursor.execute(f'DELETE FROM {tbl} WHERE video_id = ?', (new_video_id,))
    cursor.execute('DELETE FROM videos WHERE video_id = ?', (new_video_id,))

    # ── Remove old deleted file_versions row ───────────────────────────────────
    cursor.execute('DELETE FROM file_versions WHERE path = ?', (old_filepath,))

    # ── Refresh technical metadata for new file location ─────────────────────
    from media_handler import MediaHandler
    import os

    if os.path.exists(new_filepath):
        cursor.execute('SELECT 1 FROM file_versions WHERE path = ?', (new_filepath,))
        if cursor.fetchone():
            metadata = MediaHandler.get_extended_metadata(new_filepath)
            info = MediaHandler.get_file_info(new_filepath)
            cursor.execute("""
                UPDATE file_versions
                SET length = ?, length_seconds = ?, file_size = ?,
                    codec = ?, total_bitrate = ?, resolution = ?,
                    is_preferred = 1
                WHERE path = ?
            """, (
                info.get('length', ''),
                int(metadata.get('duration', 0)),
                info.get('file_size'),
                info.get('codec', ''),
                info.get('total_bitrate', ''),
                info.get('resolution', ''),
                new_filepath,
            ))

    conn.commit()

    # Clear file change flags
    model.db.clear_file_change(original_id)
    model.db.clear_file_change(replacement_id)

    return {"success": True}

@router.post("/file-changes/ignore")
async def ignore_file_change(request: Request, model = Depends(get_model)):
    """Mark file changes as separate (not a replacement)."""
    data = await request.json()
    change_ids = data.get('change_ids', [])
    
    if not USE_SQLITE:
        return {"success": False}
    
    conn = model.db.get_connection()
    cursor = conn.cursor()
    
    for change_id in change_ids:
        # Clear related_id to unlink them
        cursor.execute('''
            UPDATE file_changes 
            SET related_id = NULL, flag_type = 
                CASE 
                    WHEN flag_type = 'possibly_replaced' THEN 'deleted'
                    WHEN flag_type = 'possible_replacement' THEN 'new'
                    ELSE flag_type
                END
            WHERE id = ?
        ''', (change_id,))
    
    conn.commit()
    
    return {"success": True}


@router.post("/file-changes/delete")
async def delete_old_file(request: Request, model = Depends(get_model)):
    """Permanently delete metadata for a deleted file."""
    data = await request.json()
    change_id = data.get('change_id')
    
    if not USE_SQLITE:
        return {"success": False}
    
    conn = model.db.get_connection()
    cursor = conn.cursor()
    
    # Get the file change
    cursor.execute('SELECT * FROM file_changes WHERE id = ?', (change_id,))
    change = cursor.fetchone()
    
    if change:
        change_dict = dict(change)
        video_id = change_dict['video_id']
        
        if video_id:
            # Delete the video and all its data
            cursor.execute('DELETE FROM videos WHERE video_id = ?', (video_id,))
            cursor.execute('DELETE FROM file_versions WHERE video_id = ?', (video_id,))
            
            # Delete from junction tables
            from config import LIST_FIELDS
            for field in LIST_FIELDS:
                junction_table = f'video_{field.lower()}_junction'
                try:
                    cursor.execute(f'DELETE FROM {junction_table} WHERE video_id = ?', (video_id,))
                except:
                    pass
            
            conn.commit()
        
        # Clear the flag
        model.db.clear_file_change(change_id)
    
    return {"success": True}


@router.get("/library/{library}/alert-count")
def get_library_alert_count(library: str, model = Depends(get_model)):
    """Get count of file changes requiring attention."""
    count = model.get_library_alert_count(library)
    return {"count": count}

@router.post("/file-changes/cleanup")
def cleanup_orphaned_file_changes(model = Depends(get_model)):
    """Clean up orphaned file_changes entries."""
    if not USE_SQLITE:
        return {"success": False, "deleted": 0}
    
    try:
        deleted = model.db.cleanup_orphaned_file_changes()
        return {"success": True, "deleted": deleted}
    except Exception as e:
        print(f"Cleanup failed: {e}")
        return {"success": False, "error": str(e), "deleted": 0}