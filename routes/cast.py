# This file is: ./routes/cast.py

"""
Cast member management routes.
Handles cast member listings, video searches, and biographical data.
"""

import os
import urllib.parse
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from helpers import get_thumbnail_path, check_auth
from media_handler import MediaHandler
from cast_manager import CastManager
from config import USE_SQLITE

router = APIRouter(prefix="/api/cast", tags=["cast"])

# Global references (will be set by web_server.py)
_model = None

def get_model():
    """Dependency function to get model."""
    return _model

def set_model(model):
    """Set the model instance."""
    global _model
    _model = model


@router.get("/all")
def get_all_cast(model = Depends(get_model)):
    """Get all cast members with video counts and favorite status."""
    import time
    start_time = time.time()
    
    if USE_SQLITE:
        # Get favorites
        t1 = time.time()
        favorites = set(model.db.get_favorite_cast())
        print(f"[PERF] get_favorite_cast: {(time.time() - t1)*1000:.0f}ms")
        
        # For SQLite, get cast members directly from database
        with model.db.get_connection() as conn:
            cursor = conn.cursor()
            
            # Get all cast members with their counts
            t2 = time.time()
            cursor.execute('''
            SELECT vc.value as name, COUNT(*) as count
            FROM video_cast vc
            JOIN video_cast_junction vcj ON vc.id = vcj.cast_id
            GROUP BY vc.value
            ORDER BY count DESC
            ''')
            cast_rows = cursor.fetchall()
            print(f"[PERF] get cast counts: {(time.time() - t2)*1000:.0f}ms - {len(cast_rows)} cast members")
            
            # OPTIMIZED: Get all cast with photos in ONE query instead of 500+ individual queries
            t3 = time.time()
            cursor.execute('''
            SELECT cast_name, source_type FROM cast_photos WHERE thumbnail_data IS NOT NULL
            ''')
            # source_type: 'upload'/'url' = manual; 'stashdb'/'tpdb' = from an external DB
            photo_sources = {row['cast_name']: (row['source_type'] or '') for row in cursor.fetchall()}
            cast_with_photos = set(photo_sources)
            print(f"[PERF] get cast with photos: {(time.time() - t3)*1000:.0f}ms - {len(cast_with_photos)} have photos")
            
            # Get all cast genders and categories
            t4 = time.time()
            cursor.execute('SELECT cast_name, gender FROM cast_gender')
            cast_genders = {row['cast_name']: row['gender'] for row in cursor.fetchall()}
            cursor.execute('SELECT cast_name, category FROM cast_category')
            cast_categories = {row['cast_name']: row['category'] for row in cursor.fetchall()}
            print(f"[PERF] get cast genders: {(time.time() - t4)*1000:.0f}ms - {len(cast_genders)} have gender")
            
            # Get cast attributes from cast_attributes table (if it exists)
            cast_attributes = {}
            try:
                # Check if cast_attributes table exists
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cast_attributes'")
                if cursor.fetchone():
                    # Get all cast attributes
                    cursor.execute('''
                    SELECT cast_name, attribute_type, attribute_value, is_primary
                    FROM cast_attributes
                    ORDER BY cast_name, attribute_type, is_primary DESC
                    ''')
                    
                    for row in cursor.fetchall():
                        cast_name = row['cast_name']
                        attr_type = row['attribute_type']
                        attr_value = row['attribute_value']
                        
                        if cast_name not in cast_attributes:
                            cast_attributes[cast_name] = {}
                        if attr_type not in cast_attributes[cast_name]:
                            cast_attributes[cast_name][attr_type] = []
                        
                        cast_attributes[cast_name][attr_type].append(attr_value)
                    
                    print(f"Loaded attributes for {len(cast_attributes)} cast members from cast_attributes table")
                else:
                    print("cast_attributes table does not exist, falling back to video attributes")
            except Exception as e:
                print(f"Error loading cast_attributes: {e}")
            
            # Load aliases: primary_name → [alias, alias, ...]
            # Also build a set of all alias names so we can exclude them
            # from the main list (aliases should not appear as separate entries).
            cast_aliases_map = {}   # primary → list of alias names
            alias_names_set  = set()
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cast_aliases'")
                if cursor.fetchone():
                    cursor.execute('SELECT primary_name, alias_name FROM cast_aliases')
                    for r in cursor.fetchall():
                        cast_aliases_map.setdefault(r['primary_name'], []).append(r['alias_name'])
                        alias_names_set.add(r['alias_name'])
            except Exception as e:
                print(f"[cast/all] alias load error: {e}")

            # External DB links (ext_db.py) - table exists once ext_db has started
            ext_linked = set()
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cast_ext_link'")
                if cursor.fetchone():
                    cursor.execute('SELECT cast_name FROM cast_ext_link')
                    ext_linked = {r['cast_name'] for r in cursor.fetchall()}
            except Exception as e:
                print(f"[cast/all] ext link load error: {e}")
            external_photo_sources = ('stashdb', 'tpdb')

            # One entry per performer: videos filed under an alias count for its primary,
            # and a performer whose videos all use an alias still appears (under the primary)
            alias_to_primary = {a: p for p, aliases in cast_aliases_map.items() for a in aliases}
            videos_of = {}
            import display_filter     # videos hidden by the current user's display filter don't count
            shown, shown_params = display_filter.condition(conn, model)
            cursor.execute('SELECT vc.value AS name, vcj.video_id FROM video_cast vc '
                           'JOIN video_cast_junction vcj ON vc.id = vcj.cast_id'
                           + (' JOIN videos v ON v.video_id = vcj.video_id WHERE ' + shown if shown else ''), shown_params)
            for r in cursor.fetchall():
                videos_of.setdefault(alias_to_primary.get(r['name'], r['name']), set()).add(r['video_id'])

            # Build cast list using the set for O(1) lookup
            cast_list = []
            for name, vids in videos_of.items():
                if name in alias_names_set:
                    continue
                cast_list.append({
                    'name': name,
                    'count': len(vids),
                    'is_favorite': name in favorites,
                    'has_photo': name in cast_with_photos,
                    'photo_source': (None if name not in cast_with_photos else
                                     'external' if photo_sources.get(name) in external_photo_sources
                                     else 'manual'),
                    'ext_linked': name in ext_linked,
                    'gender': cast_genders.get(name, 'female'),
                    'category': cast_categories.get(name, 'pro'),
                    'attributes': cast_attributes.get(name, {}),
                    'aliases': cast_aliases_map.get(name, []),
                })
    else:
        cast_list = CastManager.get_all_cast_members(model.files)
        favorites = set(model.files.get('_favorite_cast', []))
        
        # Add favorite status, photo status, and gender to each cast member
        for cast_member in cast_list:
            cast_member['is_favorite'] = cast_member['name'] in favorites
            cast_member['has_photo'] = '_cast_photos' in model.files and cast_member['name'] in model.files['_cast_photos']
            cast_member['gender'] = 'female'  # Default for JSON mode (no gender table)
    
    # Sort: favorites first, then by count
    cast_list.sort(key=lambda x: (not x.get('is_favorite', False), -x['count']))
    
    print(f"[PERF] TOTAL /api/cast/all: {(time.time() - start_time)*1000:.0f}ms - returned {len(cast_list)} cast members")
    
    return {"cast": cast_list}


@router.get("/{cast_name}/attributes")
def get_cast_attributes(cast_name: str, model = Depends(get_model)):
    """Get all attributes for a specific cast member."""
    if not USE_SQLITE:
        return {"error": "SQLite only"}
    
    with model.db.get_connection() as conn:
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cast_attributes'")
        if not cursor.fetchone():
            return {"cast_name": cast_name, "attributes": {}}
        
        # Get all attributes for this cast member
        cursor.execute('''
        SELECT attribute_type, attribute_value, is_primary, video_count
        FROM cast_attributes
        WHERE cast_name = ?
        ORDER BY attribute_type, is_primary DESC, video_count DESC
        ''', (cast_name,))
        
        attributes = {}
        for row in cursor.fetchall():
            attr_type = row['attribute_type']
            if attr_type not in attributes:
                attributes[attr_type] = []
            
            attributes[attr_type].append({
                'value': row['attribute_value'],
                'is_primary': bool(row['is_primary']),
                'video_count': row['video_count']
            })
        
        return {
            'cast_name': cast_name,
            'attributes': attributes
        }


@router.post("/{cast_name}/attributes")
def update_cast_attributes(cast_name: str, attributes: dict, model = Depends(get_model)):
    """
    Update cast attributes for a cast member.
    Expected format: {"hair": [{"value": "blonde", "is_primary": true}, ...], ...}
    """
    if not USE_SQLITE:
        return {"error": "SQLite only"}
    
    with model.db.get_connection() as conn:
        cursor = conn.cursor()
        
        # Delete existing attributes for this cast member
        cursor.execute('DELETE FROM cast_attributes WHERE cast_name = ?', (cast_name,))
        
        # Insert new attributes
        inserted = 0
        for attr_type, attr_list in attributes.items():
            for attr_data in attr_list:
                cursor.execute('''
                INSERT INTO cast_attributes 
                (cast_name, attribute_type, attribute_value, is_primary, video_count)
                VALUES (?, ?, ?, ?, ?)
                ''', (
                    cast_name,
                    attr_type,
                    attr_data['value'],
                    1 if attr_data.get('is_primary', False) else 0,
                    attr_data.get('video_count', 0)
                ))
                inserted += 1
        
        conn.commit()
        
        return {
            'success': True,
            'cast_name': cast_name,
            'inserted': inserted
        }



@router.post("/{cast_name}/attributes/apply-to-videos")
def apply_cast_attributes_to_videos(cast_name: str, model = Depends(get_model)):
    """
    Apply primary cast attributes to all videos featuring this cast member.
    Only applies primary attributes, doesn't remove existing values.
    """
    if not USE_SQLITE:
        return {"error": "SQLite only"}
    
    try:
        with model.db.get_connection() as conn:
            cursor = conn.cursor()
            
            # Get all primary attributes for this cast member
            cursor.execute('''
            SELECT attribute_type, attribute_value
            FROM cast_attributes
            WHERE cast_name = ? AND is_primary = 1
            ''', (cast_name,))
            
            primary_attrs = {}
            for row in cursor.fetchall():
                attr_type = row['attribute_type']
                attr_value = row['attribute_value']
                
                if attr_type not in primary_attrs:
                    primary_attrs[attr_type] = []
                primary_attrs[attr_type].append(attr_value)
            
            if not primary_attrs:
                return {
                    'success': False,
                    'error': 'No primary attributes found',
                    'updated': 0
                }
            
            # Resolve aliases so we include videos filed under any alias name
            names_to_search = [cast_name]
            try:
                cursor.execute(
                    'SELECT alias_name FROM cast_aliases WHERE primary_name = ? ORDER BY alias_name',
                    (cast_name,)
                )
                names_to_search.extend(r['alias_name'] for r in cursor.fetchall())
                # Also handle: cast_name might itself be an alias
                cursor.execute(
                    'SELECT primary_name FROM cast_aliases WHERE alias_name = ? LIMIT 1',
                    (cast_name,)
                )
                row = cursor.fetchone()
                if row:
                    primary = row['primary_name']
                    if primary not in names_to_search:
                        names_to_search.insert(0, primary)
                    cursor.execute(
                        'SELECT alias_name FROM cast_aliases WHERE primary_name = ? ORDER BY alias_name',
                        (primary,)
                    )
                    for r in cursor.fetchall():
                        if r['alias_name'] not in names_to_search:
                            names_to_search.append(r['alias_name'])
            except Exception:
                pass  # cast_aliases table may not exist yet

            # Get all videos featuring this cast member (or any alias)
            placeholders = ','.join('?' * len(names_to_search))
            cursor.execute(f'''
            SELECT DISTINCT vcj.video_id
            FROM video_cast vc
            JOIN video_cast_junction vcj ON vc.id = vcj.cast_id
            WHERE vc.value IN ({placeholders})
            ''', names_to_search)

            video_ids = [row['video_id'] for row in cursor.fetchall()]
            
            if not video_ids:
                return {
                    'success': False,
                    'error': 'No videos found for this cast member',
                    'updated': 0
                }
            
            # Apply primary attributes to each video
            updates_made = 0
            
            for video_id in video_ids:
                for attr_type, attr_values in primary_attrs.items():
                    # All attributes are video-level now
                    table_name = f'video_{attr_type}'
                    junction_table = f'video_{attr_type}_junction'
                    
                    for attr_value in attr_values:
                        try:
                            # Get or create the attribute value ID
                            cursor.execute(f'SELECT id FROM {table_name} WHERE value = ?', (attr_value,))
                            row = cursor.fetchone()
                            
                            if row:
                                value_id = row['id']
                            else:
                                # Create new value
                                cursor.execute(f'INSERT INTO {table_name} (value) VALUES (?)', (attr_value,))
                                value_id = cursor.lastrowid
                            
                            # Add to video (if not already present)
                            cursor.execute(f'''
                            INSERT OR IGNORE INTO {junction_table} (video_id, {attr_type}_id)
                            VALUES (?, ?)
                            ''', (video_id, value_id))
                            
                            if cursor.rowcount > 0:
                                updates_made += 1
                        except Exception as e:
                            # Get a sample path for error reporting
                            cursor.execute('''
                                SELECT path FROM file_versions 
                                WHERE video_id = ? AND is_preferred = 1
                                LIMIT 1
                            ''', (video_id,))
                            path_row = cursor.fetchone()
                            path = path_row['path'] if path_row else video_id
                            print(f"Error applying {attr_type}={attr_value} to {path}: {e}")
            
            conn.commit()
            
            return {
                'success': True,
                'cast_name': cast_name,
                'videos_processed': len(video_ids),
                'attributes_added': updates_made,
                'primary_attributes': primary_attrs
            }
            
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'updated': 0
        }


@router.get("/{cast_name}/attributes/suggest-from-videos")
def suggest_cast_attributes_from_videos(cast_name: str, model = Depends(get_model)):
    """
    Scan all videos featuring this cast member and return a frequency table
    of attribute values found across those videos.

    Returns for each attribute field: every value found, how many videos it
    appeared in, and whether it's already saved in cast_attributes for this
    person. The UI uses this to let the user vet and selectively import
    values into the working cast attribute editor — nothing is saved here.

    Fields excluded: Cast (not a personal attribute), Site (not personal).
    """
    if not USE_SQLITE:
        return {"error": "SQLite only"}

    # Fields to scan on videos (excludes Cast and Site)
    SCAN_FIELDS = [
        'ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face',
        'cock', 'outfit', 'theme', 'feature', 'cumshot',
        'participants', 'orientation', 'tags',
    ]

    try:
        with model.db.get_connection() as conn:
            cursor = conn.cursor()

            # Resolve aliases — include all names that map to this person
            names_to_search = [cast_name]
            try:
                cursor.execute(
                    'SELECT alias_name FROM cast_aliases WHERE primary_name = ? ORDER BY alias_name',
                    (cast_name,)
                )
                names_to_search.extend(r['alias_name'] for r in cursor.fetchall())
                cursor.execute(
                    'SELECT primary_name FROM cast_aliases WHERE alias_name = ? LIMIT 1',
                    (cast_name,)
                )
                row = cursor.fetchone()
                if row:
                    primary = row['primary_name']
                    if primary not in names_to_search:
                        names_to_search.insert(0, primary)
                    cursor.execute(
                        'SELECT alias_name FROM cast_aliases WHERE primary_name = ? ORDER BY alias_name',
                        (primary,)
                    )
                    for r in cursor.fetchall():
                        if r['alias_name'] not in names_to_search:
                            names_to_search.append(r['alias_name'])
            except Exception:
                pass

            # Find all video_ids featuring this cast member (or any alias)
            placeholders = ','.join('?' * len(names_to_search))
            cursor.execute(f'''
                SELECT DISTINCT vcj.video_id
                FROM video_cast vc
                JOIN video_cast_junction vcj ON vc.id = vcj.cast_id
                WHERE vc.value IN ({placeholders})
            ''', names_to_search)
            video_ids = [r['video_id'] for r in cursor.fetchall()]

            if not video_ids:
                return {
                    'cast_name': cast_name,
                    'video_count': 0,
                    'suggestions': {}
                }

            # Load existing cast_attributes so we can flag already-saved values
            cursor.execute('''
                SELECT attribute_type, attribute_value, is_primary
                FROM cast_attributes
                WHERE cast_name = ?
            ''', (cast_name,))
            existing = {}
            for r in cursor.fetchall():
                existing.setdefault(r['attribute_type'], {})[r['attribute_value'].lower()] = {
                    'is_primary': bool(r['is_primary'])
                }

            # Tally attribute values across all videos
            # {field: {value: video_count}}
            tallies = {}
            vid_placeholders = ','.join('?' * len(video_ids))

            for field in SCAN_FIELDS:
                table_name    = f'video_{field}'
                junction_table = f'video_{field}_junction'
                try:
                    cursor.execute(f'''
                        SELECT vt.value, COUNT(DISTINCT vcj.video_id) as cnt
                        FROM {junction_table} vcj
                        JOIN {table_name} vt ON vt.id = vcj.{field}_id
                        WHERE vcj.video_id IN ({vid_placeholders})
                        GROUP BY vt.value
                        ORDER BY cnt DESC, vt.value
                    ''', video_ids)
                    rows = cursor.fetchall()
                    if rows:
                        tallies[field] = [(r['value'], r['cnt']) for r in rows]
                except Exception:
                    pass   # table may not exist for all fields

            # Build response
            suggestions = {}
            for field, values in tallies.items():
                field_existing = existing.get(field, {})
                entries = []
                for value, count in values:
                    already = value.lower() in field_existing
                    entries.append({
                        'value':       value,
                        'video_count': count,
                        'already_in_attrs': already,
                        'is_primary':  field_existing.get(value.lower(), {}).get('is_primary', False)
                            if already else False,
                    })
                suggestions[field] = entries

            return {
                'cast_name':   cast_name,
                'video_count': len(video_ids),
                'suggestions': suggestions,
            }

    except Exception as e:
        return {'error': str(e), 'suggestions': {}}



def apply_cast_attributes_to_videos(cast_name: str, model = Depends(get_model)):
    """
    Apply primary cast attributes to all videos featuring this cast member.
    Only applies primary attributes, doesn't remove existing values.
    """
    if not USE_SQLITE:
        return {"error": "SQLite only"}
    
    try:
        with model.db.get_connection() as conn:
            cursor = conn.cursor()
            
            # Get all primary attributes for this cast member
            cursor.execute('''
            SELECT attribute_type, attribute_value
            FROM cast_attributes
            WHERE cast_name = ? AND is_primary = 1
            ''', (cast_name,))
            
            primary_attrs = {}
            for row in cursor.fetchall():
                attr_type = row['attribute_type']
                attr_value = row['attribute_value']
                
                if attr_type not in primary_attrs:
                    primary_attrs[attr_type] = []
                primary_attrs[attr_type].append(attr_value)
            
            if not primary_attrs:
                return {
                    'success': False,
                    'error': 'No primary attributes found',
                    'updated': 0
                }
            
            # Resolve aliases so we include videos filed under any alias name
            names_to_search = [cast_name]
            try:
                cursor.execute(
                    'SELECT alias_name FROM cast_aliases WHERE primary_name = ? ORDER BY alias_name',
                    (cast_name,)
                )
                names_to_search.extend(r['alias_name'] for r in cursor.fetchall())
                # Also handle: cast_name might itself be an alias
                cursor.execute(
                    'SELECT primary_name FROM cast_aliases WHERE alias_name = ? LIMIT 1',
                    (cast_name,)
                )
                row = cursor.fetchone()
                if row:
                    primary = row['primary_name']
                    if primary not in names_to_search:
                        names_to_search.insert(0, primary)
                    cursor.execute(
                        'SELECT alias_name FROM cast_aliases WHERE primary_name = ? ORDER BY alias_name',
                        (primary,)
                    )
                    for r in cursor.fetchall():
                        if r['alias_name'] not in names_to_search:
                            names_to_search.append(r['alias_name'])
            except Exception:
                pass  # cast_aliases table may not exist yet

            # Get all videos featuring this cast member (or any alias)
            placeholders = ','.join('?' * len(names_to_search))
            cursor.execute(f'''
            SELECT DISTINCT vcj.video_id
            FROM video_cast vc
            JOIN video_cast_junction vcj ON vc.id = vcj.cast_id
            WHERE vc.value IN ({placeholders})
            ''', names_to_search)

            video_ids = [row['video_id'] for row in cursor.fetchall()]
            
            if not video_ids:
                return {
                    'success': False,
                    'error': 'No videos found for this cast member',
                    'updated': 0
                }
            
            # Apply primary attributes to each video
            updates_made = 0
            
            for video_id in video_ids:
                for attr_type, attr_values in primary_attrs.items():
                    # All attributes are video-level now
                    table_name = f'video_{attr_type}'
                    junction_table = f'video_{attr_type}_junction'
                    
                    for attr_value in attr_values:
                        try:
                            # Get or create the attribute value ID
                            cursor.execute(f'SELECT id FROM {table_name} WHERE value = ?', (attr_value,))
                            row = cursor.fetchone()
                            
                            if row:
                                value_id = row['id']
                            else:
                                # Create new value
                                cursor.execute(f'INSERT INTO {table_name} (value) VALUES (?)', (attr_value,))
                                value_id = cursor.lastrowid
                            
                            # Add to video (if not already present)
                            cursor.execute(f'''
                            INSERT OR IGNORE INTO {junction_table} (video_id, {attr_type}_id)
                            VALUES (?, ?)
                            ''', (video_id, value_id))
                            
                            if cursor.rowcount > 0:
                                updates_made += 1
                        except Exception as e:
                            # Get a sample path for error reporting
                            cursor.execute('''
                                SELECT path FROM file_versions 
                                WHERE video_id = ? AND is_preferred = 1
                                LIMIT 1
                            ''', (video_id,))
                            path_row = cursor.fetchone()
                            path = path_row['path'] if path_row else video_id
                            print(f"Error applying {attr_type}={attr_value} to {path}: {e}")
            
            conn.commit()
            
            return {
                'success': True,
                'cast_name': cast_name,
                'videos_processed': len(video_ids),
                'attributes_added': updates_made,
                'primary_attributes': primary_attrs
            }
            
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'updated': 0
        }


class ApplyAttrsToVideosRequest(BaseModel):
    paths: list

@router.post("/apply-attributes-to-selected")
def apply_cast_attributes_to_selected_videos(
    body: ApplyAttrsToVideosRequest,
    request: Request,
    model = Depends(get_model)
):
    """
    For each of the selected video paths:
      1. Find all cast members tagged on that video.
      2. For each cast member, load their primary attributes.
      3. Apply those primary attributes to that video (additive — never removes).

    This is the reverse of apply-to-videos (which starts from one cast member
    and fans out to all their videos). Here we start from a set of videos and
    pull in the attributes from every cast member on each video.
    """
    check_auth(request)

    if not USE_SQLITE:
        return {"error": "SQLite only"}

    if not body.paths:
        return {"success": False, "error": "No paths provided", "videos_processed": 0}

    try:
        with model.db.get_connection() as conn:
            cursor = conn.cursor()

            # Pre-load all primary cast attributes into a cache so we only
            # query each cast member once even if they appear in multiple videos.
            # {cast_name: {attr_type: [attr_value, ...]}}
            cast_attrs_cache = {}

            def get_primary_attrs(name):
                if name in cast_attrs_cache:
                    return cast_attrs_cache[name]

                # Resolve aliases so we always use the primary name's attributes
                primary_name = name
                try:
                    cursor.execute(
                        'SELECT primary_name FROM cast_aliases WHERE alias_name = ? LIMIT 1',
                        (name,)
                    )
                    row = cursor.fetchone()
                    if row:
                        primary_name = row['primary_name']
                except Exception:
                    pass

                cursor.execute('''
                    SELECT attribute_type, attribute_value
                    FROM cast_attributes
                    WHERE cast_name = ? AND is_primary = 1
                ''', (primary_name,))
                attrs = {}
                for r in cursor.fetchall():
                    attrs.setdefault(r['attribute_type'], []).append(r['attribute_value'])

                cast_attrs_cache[name] = attrs
                return attrs

            videos_processed = 0
            attributes_added = 0
            videos_skipped   = 0   # no cast or no cast has primary attrs

            for path in body.paths:
                # Resolve path → video_id
                cursor.execute(
                    'SELECT video_id FROM file_versions WHERE path = ? LIMIT 1',
                    (path,)
                )
                row = cursor.fetchone()
                if not row:
                    videos_skipped += 1
                    continue
                video_id = row['video_id']

                # Find all cast members on this video
                cursor.execute('''
                    SELECT vc.value as cast_name
                    FROM video_cast_junction vcj
                    JOIN video_cast vc ON vc.id = vcj.cast_id
                    WHERE vcj.video_id = ?
                ''', (video_id,))
                cast_members = [r['cast_name'] for r in cursor.fetchall()]

                if not cast_members:
                    videos_skipped += 1
                    continue

                video_had_attrs = False
                for cast_name in cast_members:
                    primary_attrs = get_primary_attrs(cast_name)
                    if not primary_attrs:
                        continue

                    video_had_attrs = True
                    for attr_type, attr_values in primary_attrs.items():
                        table_name    = f'video_{attr_type}'
                        junction_table = f'video_{attr_type}_junction'

                        for attr_value in attr_values:
                            try:
                                cursor.execute(
                                    f'SELECT id FROM {table_name} WHERE value = ?',
                                    (attr_value,)
                                )
                                r = cursor.fetchone()
                                if r:
                                    value_id = r['id']
                                else:
                                    cursor.execute(
                                        f'INSERT INTO {table_name} (value) VALUES (?)',
                                        (attr_value,)
                                    )
                                    value_id = cursor.lastrowid

                                cursor.execute(f'''
                                    INSERT OR IGNORE INTO {junction_table}
                                        (video_id, {attr_type}_id)
                                    VALUES (?, ?)
                                ''', (video_id, value_id))

                                if cursor.rowcount > 0:
                                    attributes_added += 1
                            except Exception as e:
                                print(f"[apply-attrs] Error {attr_type}={attr_value} "
                                      f"on {video_id}: {e}")

                if video_had_attrs:
                    videos_processed += 1
                else:
                    videos_skipped += 1

            conn.commit()

            return {
                'success':          True,
                'videos_processed': videos_processed,
                'videos_skipped':   videos_skipped,
                'attributes_added': attributes_added,
            }

    except Exception as e:
        return {'success': False, 'error': str(e), 'videos_processed': 0}


@router.get("/{cast_name}/videos")
def get_cast_videos_route(cast_name: str, model = Depends(get_model)):
    """Get all videos featuring a specific cast member."""
    # Decode URL encoding
    cast_name = urllib.parse.unquote(cast_name)
    
    if USE_SQLITE:
        # Get videos from database
        with model.db.get_connection() as conn:
            cursor = conn.cursor()
            # Get all video_ids for this cast member, then get their preferred file paths
            cursor.execute('''
            SELECT DISTINCT fv.path
            FROM video_cast vc
            JOIN video_cast_junction vcj ON vc.id = vcj.cast_id
            JOIN file_versions fv ON vcj.video_id = fv.video_id
            WHERE vc.value LIKE ? AND fv.is_preferred = 1
            ''', (f'%{cast_name}%',))
            
            paths = [row['path'] for row in cursor.fetchall()]
            results = []
            
            for path in paths:
                file_data = model.db.get_file(path)
                if file_data:
                    results.append({
                        'path': path,
                        'data': file_data
                    })
    else:
        results = CastManager.get_cast_videos(model.files, cast_name, model.libraries)
    
    # Enrich results with full video data
    enriched = []
    for match in results:
        video_data = match['data'].copy()
        video_data['path'] = match['path']
        video_data['filename'] = os.path.basename(match['path'])
        video_data['type'] = 'file'
        video_data['cache_id'] = video_data.get('cache_id') or MediaHandler._get_cache_key(match['path'])
        
        video_data['thumb'] = get_thumbnail_path(video_data['cache_id'])
        video_data['supported'] = MediaHandler.is_browser_supported(
            video_data.get('extension', ''), 
            video_data.get('codec', '')
        )
        
        # Find which library this belongs to
        for lib_name, lib_paths in model.libraries.items():
            for lib_path in lib_paths:
                if match['path'].startswith(os.path.normpath(lib_path) + os.sep):
                    video_data['library'] = lib_name
                    break
        
        enriched.append(video_data)
    
    return {"videos": enriched, "count": len(enriched)}


@router.get("/{cast_name}/bio")
def get_cast_bio(cast_name: str, model = Depends(get_model)):
    """Get comprehensive bio data for a cast member, including alias videos."""
    cast_name = urllib.parse.unquote(cast_name)

    if USE_SQLITE:
        with model.db.get_connection() as conn:
            cursor = conn.cursor()

            # Resolve alias → primary
            cursor.execute(
                'SELECT primary_name FROM cast_aliases WHERE alias_name = ? LIMIT 1',
                (cast_name,)
            )
            row = cursor.fetchone()
            if row:
                cast_name = row['primary_name']

            # Collect primary + all aliases for video search
            names_to_search = [cast_name]
            cursor.execute(
                'SELECT alias_name FROM cast_aliases WHERE primary_name = ? ORDER BY alias_name',
                (cast_name,)
            )
            aliases = [r['alias_name'] for r in cursor.fetchall()]
            names_to_search.extend(aliases)

            # Exact match across all names
            ph = ','.join('?' * len(names_to_search))
            import display_filter     # minus videos hidden by the current user's display filter
            shown, shown_params = display_filter.condition(conn, model)
            cursor.execute(f'''
                SELECT DISTINCT fv.path
                FROM video_cast vc
                JOIN video_cast_junction vcj ON vc.id = vcj.cast_id
                JOIN file_versions fv ON vcj.video_id = fv.video_id
                JOIN videos v ON v.video_id = fv.video_id
                WHERE vc.value IN ({ph}) AND fv.is_preferred = 1{' AND ' + shown if shown else ''}
            ''', [*names_to_search, *shown_params])

            paths = [row['path'] for row in cursor.fetchall()]
            exact_matches = []
            for path in paths:
                file_data = model.db.get_file(path)
                if file_data:
                    exact_matches.append({'path': path, 'data': file_data})

            # Library distribution
            library_distribution = {}
            for match in exact_matches:
                for lib_name, lib_paths in model.libraries.items():
                    for lib_path in lib_paths:
                        if match['path'].startswith(os.path.normpath(lib_path) + os.sep):
                            library_distribution[lib_name] = library_distribution.get(lib_name, 0) + 1
                            break

            # Appearance attributes
            appearance_attrs = ['ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face', 'cock']
            appearance_summary = {}
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cast_attributes'")
                if cursor.fetchone():
                    cursor.execute('''
                        SELECT attribute_type, attribute_value, is_primary, video_count
                        FROM cast_attributes WHERE cast_name = ?
                        ORDER BY attribute_type, is_primary DESC, video_count DESC
                    ''', (cast_name,))
                    for row in cursor.fetchall():
                        attr_type = row['attribute_type']
                        if attr_type in appearance_attrs:
                            appearance_summary.setdefault(attr_type, []).append(row['attribute_value'])
                else:
                    appearance_data = {}
                    for match in exact_matches:
                        file_data = match['data']
                        for attr in appearance_attrs:
                            if attr in file_data and file_data[attr]:
                                values = file_data[attr] if isinstance(file_data[attr], list) else [file_data[attr]]
                                for value in values:
                                    if value and value.strip():
                                        appearance_data.setdefault(attr, {})
                                        appearance_data[attr][value] = appearance_data[attr].get(value, 0) + 1
                    for attr, values_dict in appearance_data.items():
                        if values_dict:
                            most_common = sorted(values_dict.items(), key=lambda x: x[1], reverse=True)
                            appearance_summary[attr] = [val for val, count in most_common]
            except Exception as e:
                print(f"Error loading cast attributes: {e}")

            bio_data = {
                'name':                 cast_name,
                'aliases':              aliases,
                'exact_matches':        exact_matches,
                'potential_matches':    [],
                'library_distribution': library_distribution,
                'appearance':           appearance_summary,
                'stats': {
                    'total_videos':   len(exact_matches),
                    'potential_videos': 0,
                    'libraries':      len(library_distribution)
                }
            }
    else:
        bio_data = CastManager.get_cast_bio_data(model.files, model.libraries, cast_name)
        bio_data['aliases'] = []

    # Enrich exact matches
    enriched_exact = []
    for match in bio_data['exact_matches']:
        video_data = match['data'].copy()
        video_data['path'] = match['path']
        video_data['filename'] = os.path.basename(match['path'])
        video_data['cache_id'] = video_data.get('cache_id') or MediaHandler._get_cache_key(match['path'])
        video_data['thumb'] = get_thumbnail_path(video_data['cache_id'])
        video_data['supported'] = MediaHandler.is_browser_supported(
            video_data.get('extension', ''), video_data.get('codec', ''))
        for lib_name, lib_paths in model.libraries.items():
            for lib_path in lib_paths:
                if match['path'].startswith(os.path.normpath(lib_path) + os.sep):
                    video_data['library'] = lib_name
                    break
        enriched_exact.append(video_data)

    enriched_potential = []
    for match in bio_data['potential_matches']:
        video_data = match['data'].copy()
        video_data['path'] = match['path']
        video_data['filename'] = os.path.basename(match['path'])
        video_data['cache_id'] = video_data.get('cache_id') or MediaHandler._get_cache_key(match['path'])
        video_data['match_score'] = match.get('match_score', 0)
        video_data['thumb'] = get_thumbnail_path(video_data['cache_id'])
        video_data['supported'] = MediaHandler.is_browser_supported(
            video_data.get('extension', ''), video_data.get('codec', ''))
        enriched_potential.append(video_data)

    if USE_SQLITE:
        is_favorite = model.db.is_favorite_cast(cast_name)
        has_photo   = model.db.get_cast_photo(cast_name, thumbnail=True) is not None
        gender      = model.db.get_cast_gender(cast_name) or 'female'
        category    = model.db.get_cast_category(cast_name)
    else:
        favorites   = model.files.get('_favorite_cast', [])
        is_favorite = cast_name in favorites
        has_photo   = '_cast_photos' in model.files and cast_name in model.files['_cast_photos']
        gender      = 'female'
        category    = 'pro'

    return {
        "name":                 bio_data['name'],
        "aliases":              bio_data.get('aliases', []),
        "is_favorite":          is_favorite,
        "has_photo":            has_photo,
        "gender":               gender,
        "category":             category,
        "exact_matches":        enriched_exact,
        "potential_matches":    enriched_potential,
        "library_distribution": bio_data['library_distribution'],
        "appearance":           bio_data.get('appearance', {}),
        "stats":                bio_data['stats']
    }

@router.get("/favorites")
def get_favorite_cast(model = Depends(get_model)):
    """Get all favorite cast members."""
    if USE_SQLITE:
        favorites = model.db.get_favorite_cast()
    else:
        # For JSON mode, we'll store in a simple list in files
        favorites = model.files.get('_favorite_cast', [])
    
    return {"favorites": favorites}


@router.post("/favorites/{cast_name}")
def add_favorite_cast_route(cast_name: str, model = Depends(get_model)):
    """Add a cast member to favorites."""
    cast_name = urllib.parse.unquote(cast_name)
    
    if USE_SQLITE:
        success = model.db.add_favorite_cast(cast_name)
    else:
        # For JSON mode
        if '_favorite_cast' not in model.files:
            model.files['_favorite_cast'] = []
        if cast_name not in model.files['_favorite_cast']:
            model.files['_favorite_cast'].append(cast_name)
            model.save()
            success = True
        else:
            success = False
    
    return {"success": success, "cast_name": cast_name}


@router.delete("/favorites/{cast_name}")
def remove_favorite_cast_route(cast_name: str, model = Depends(get_model)):
    """Remove a cast member from favorites."""
    cast_name = urllib.parse.unquote(cast_name)
    
    if USE_SQLITE:
        success = model.db.remove_favorite_cast(cast_name)
    else:
        # For JSON mode
        if '_favorite_cast' in model.files and cast_name in model.files['_favorite_cast']:
            model.files['_favorite_cast'].remove(cast_name)
            model.save()
            success = True
        else:
            success = False
    
    return {"success": success, "cast_name": cast_name}


# Cast Photo Endpoints
from fastapi import UploadFile, File, Form
from fastapi.responses import Response
import requests


@router.post("/photo/{cast_name}/upload")
async def upload_cast_photo(cast_name: str, file: UploadFile = File(...), model = Depends(get_model)):
    """Upload a photo file for a cast member."""
    cast_name = urllib.parse.unquote(cast_name)
    
    # Validate file type
    if not file.content_type or not file.content_type.startswith('image/'):
        return {"success": False, "error": "File must be an image"}
    
    # Read file data
    photo_data = await file.read()
    
    # Check size (max 5MB)
    if len(photo_data) > 5 * 1024 * 1024:
        return {"success": False, "error": "Image must be less than 5MB"}
    
    if USE_SQLITE:
        success = model.db.save_cast_photo(cast_name, photo_data, file.content_type, 'upload')
    else:
        # For JSON mode - save to files
        import base64
        if '_cast_photos' not in model.files:
            model.files['_cast_photos'] = {}
        model.files['_cast_photos'][cast_name] = {
            'data': base64.b64encode(photo_data).decode(),
            'mime_type': file.content_type,
            'source_type': 'upload'
        }
        model.save()
        success = True
    
    return {"success": success}


@router.post("/photo/{cast_name}/url")
async def set_cast_photo_url(cast_name: str, url: str = Form(...), model = Depends(get_model)):
    """Set cast photo from a URL."""
    cast_name = urllib.parse.unquote(cast_name)
    
    try:
        # Download image from URL
        response = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        
        # Check content type
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            return {"success": False, "error": "URL must point to an image"}
        
        photo_data = response.content
        
        # Check size
        if len(photo_data) > 5 * 1024 * 1024:
            return {"success": False, "error": "Image must be less than 5MB"}
        
        if USE_SQLITE:
            success = model.db.save_cast_photo(cast_name, photo_data, content_type, 'url', url)
        else:
            import base64
            if '_cast_photos' not in model.files:
                model.files['_cast_photos'] = {}
            model.files['_cast_photos'][cast_name] = {
                'data': base64.b64encode(photo_data).decode(),
                'mime_type': content_type,
                'source_type': 'url',
                'source_url': url
            }
            model.save()
            success = True
        
        return {"success": success}
    except requests.RequestException as e:
        return {"success": False, "error": f"Failed to download image: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/photo/{cast_name}")
def get_cast_photo_route(cast_name: str, thumbnail: bool = False, model = Depends(get_model)):
    """Get cast photo with browser caching."""
    cast_name = urllib.parse.unquote(cast_name)
    
    if USE_SQLITE:
        photo = model.db.get_cast_photo(cast_name, thumbnail)
        if photo:
            # Add cache headers for browser caching (1 year)
            headers = {
                'Cache-Control': 'public, max-age=31536000, immutable',
                'ETag': f'"{cast_name}-{thumbnail}"'
            }
            return Response(
                content=photo['data'], 
                media_type=photo['mime_type'],
                headers=headers
            )
    else:
        if '_cast_photos' in model.files and cast_name in model.files['_cast_photos']:
            import base64
            photo_info = model.files['_cast_photos'][cast_name]
            photo_data = base64.b64decode(photo_info['data'])
            headers = {
                'Cache-Control': 'public, max-age=31536000, immutable',
                'ETag': f'"{cast_name}-{thumbnail}"'
            }
            return Response(
                content=photo_data, 
                media_type=photo_info['mime_type'],
                headers=headers
            )
    
    # Return 404 if no photo
    return Response(status_code=404)


@router.delete("/photo/{cast_name}")
def delete_cast_photo_route(cast_name: str, model = Depends(get_model)):
    """Delete cast photo."""
    cast_name = urllib.parse.unquote(cast_name)
    
    if USE_SQLITE:
        success = model.db.delete_cast_photo(cast_name)
    else:
        if '_cast_photos' in model.files and cast_name in model.files['_cast_photos']:
            del model.files['_cast_photos'][cast_name]
            model.save()
            success = True
        else:
            success = False
    
    return {"success": success}

# ============================================
# CAST GENDER ENDPOINTS
# ============================================

class SetGenderRequest(BaseModel):
    cast_name: str
    gender: str

@router.post("/gender/set")
def set_cast_gender(body: SetGenderRequest, request: Request, model = Depends(get_model)):
    """Set gender for a cast member."""
    check_auth(request)  # Verify authentication
    
    if not USE_SQLITE:
        raise HTTPException(status_code=501, detail="Gender requires SQLite mode")
    
    if body.gender not in ['female', 'male', 'trans']:
        raise HTTPException(status_code=400, detail="Gender must be 'female', 'male', or 'trans'")
    
    try:
        model.db.set_cast_gender(body.cast_name, body.gender)
        return {"success": True, "cast_name": body.cast_name, "gender": body.gender}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/gender/{cast_name}")
def get_cast_gender(cast_name: str, request: Request, model = Depends(get_model)):
    """Get gender for a specific cast member."""
    check_auth(request)
    
    cast_name = urllib.parse.unquote(cast_name)
    
    if not USE_SQLITE:
        return {"cast_name": cast_name, "gender": "female"}  # Default for JSON mode
    
    gender = model.db.get_cast_gender(cast_name)
    
    return {"cast_name": cast_name, "gender": gender or "female"}

@router.get("/gender/stats")
def get_gender_stats(request: Request, model = Depends(get_model)):
    """Get gender statistics."""
    check_auth(request)
    
    if not USE_SQLITE:
        raise HTTPException(status_code=501, detail="Gender stats require SQLite mode")
    
    return model.db.get_cast_gender_stats()

# ============================================
# CAST CATEGORY ENDPOINTS
# ============================================

class SetCategoryRequest(BaseModel):
    cast_name: str
    category: str

@router.post("/category/set")
def set_cast_category(body: SetCategoryRequest, request: Request, model = Depends(get_model)):
    """Set category for a cast member ('pro', 'amateur', or 'celeb')."""
    check_auth(request)
    if not USE_SQLITE:
        raise HTTPException(status_code=501, detail="Category requires SQLite mode")
    if body.category not in ('pro', 'amateur', 'celeb'):
        raise HTTPException(status_code=400, detail="Category must be 'pro', 'amateur', or 'celeb'")
    model.db.set_cast_category(body.cast_name, body.category)
    return {"success": True, "cast_name": body.cast_name, "category": body.category}

@router.get("/category/{cast_name}")
def get_cast_category(cast_name: str, request: Request, model = Depends(get_model)):
    """Get category for a specific cast member."""
    check_auth(request)
    cast_name = urllib.parse.unquote(cast_name)
    if not USE_SQLITE:
        return {"cast_name": cast_name, "category": "pro"}
    category = model.db.get_cast_category(cast_name)
    return {"cast_name": cast_name, "category": category}

# ============================================
# CAST ALIAS ENDPOINTS
# ============================================

from pydantic import BaseModel as _BaseModel

class AliasRequest(_BaseModel):
    alias_name: str

@router.get("/{cast_name}/aliases")
def get_aliases(cast_name: str, request: Request, model = Depends(get_model)):
    """Return all aliases for a cast member."""
    check_auth(request)
    cast_name = urllib.parse.unquote(cast_name)
    if not USE_SQLITE:
        return {"primary": cast_name, "aliases": []}
    with model.db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT alias_name FROM cast_aliases WHERE primary_name = ? ORDER BY alias_name',
            (cast_name,)
        )
        aliases = [r['alias_name'] for r in cursor.fetchall()]
    return {"primary": cast_name, "aliases": aliases}


@router.post("/{cast_name}/aliases")
def add_alias(cast_name: str, body: AliasRequest, request: Request, model = Depends(get_model)):
    """
    Add alias_name as an alias of cast_name (primary).
    If alias_name itself has aliases, they are re-pointed to cast_name.
    """
    check_auth(request)
    cast_name  = urllib.parse.unquote(cast_name)
    alias_name = body.alias_name.strip()

    if not alias_name or alias_name == cast_name:
        return {"success": False, "error": "Invalid alias name"}
    if not USE_SQLITE:
        return {"success": False, "error": "SQLite only"}

    with model.db.get_connection() as conn:
        cursor = conn.cursor()

        # Prevent circular
        cursor.execute(
            'SELECT 1 FROM cast_aliases WHERE primary_name = ? AND alias_name = ?',
            (alias_name, cast_name)
        )
        if cursor.fetchone():
            return {"success": False, "error": f"{cast_name} is already an alias of {alias_name}."}

        # How many videos tagged with the alias?
        cursor.execute('''
            SELECT COUNT(DISTINCT vcj.video_id) as n
            FROM video_cast vc
            JOIN video_cast_junction vcj ON vc.id = vcj.cast_id
            WHERE vc.value = ?
        ''', (alias_name,))
        alias_video_count = cursor.fetchone()['n']

        # Absorb sub-aliases that alias_name owned
        cursor.execute('SELECT alias_name FROM cast_aliases WHERE primary_name = ?', (alias_name,))
        sub_aliases = [r['alias_name'] for r in cursor.fetchall()]
        if sub_aliases:
            cursor.execute('DELETE FROM cast_aliases WHERE primary_name = ?', (alias_name,))
            for sub in sub_aliases:
                cursor.execute(
                    'INSERT OR IGNORE INTO cast_aliases (primary_name, alias_name) VALUES (?,?)',
                    (cast_name, sub)
                )

        cursor.execute(
            'INSERT OR IGNORE INTO cast_aliases (primary_name, alias_name) VALUES (?,?)',
            (cast_name, alias_name)
        )
        # One performer from now on: fold the alias's own profile (and its old aliases') into this one
        import db_maintenance
        profile = {}
        for other in [alias_name] + sub_aliases:
            for k, v in db_maintenance.merge_cast_profile(conn, cast_name, other, winner='into').items():
                profile[k] = profile.get(k, 0) + v
        conn.commit()

    return {
        "success": True,
        "primary": cast_name,
        "alias": alias_name,
        "profile_merged": profile,
        "merged": alias_video_count > 0 or len(sub_aliases) > 0,
        "alias_video_count": alias_video_count,
        "sub_aliases_absorbed": sub_aliases,
    }


@router.delete("/{cast_name}/aliases/{alias_name}")
def remove_alias(cast_name: str, alias_name: str, request: Request, model = Depends(get_model)):
    """Remove an alias without deleting any videos or cast entries."""
    check_auth(request)
    cast_name  = urllib.parse.unquote(cast_name)
    alias_name = urllib.parse.unquote(alias_name)
    if not USE_SQLITE:
        return {"success": False, "error": "SQLite only"}
    with model.db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM cast_aliases WHERE primary_name = ? AND alias_name = ?',
            (cast_name, alias_name)
        )
        conn.commit()
        deleted = cursor.rowcount
    return {"success": deleted > 0, "primary": cast_name, "alias": alias_name}


@router.post("/{cast_name}/aliases/set-primary")
def set_alias_primary(cast_name: str, body: AliasRequest, request: Request, model = Depends(get_model)):
    """
    Promote body.alias_name to primary, demoting cast_name to alias.
    All other aliases are re-pointed to the new primary.
    """
    check_auth(request)
    cast_name   = urllib.parse.unquote(cast_name)
    new_primary = body.alias_name.strip()
    if not USE_SQLITE:
        return {"success": False, "error": "SQLite only"}

    with model.db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT alias_name FROM cast_aliases WHERE primary_name = ?', (cast_name,))
        all_aliases = [r['alias_name'] for r in cursor.fetchall()]

        if new_primary not in all_aliases:
            return {"success": False, "error": f"{new_primary} is not an alias of {cast_name}"}

        cursor.execute('DELETE FROM cast_aliases WHERE primary_name = ?', (cast_name,))
        new_aliases = [a for a in all_aliases if a != new_primary] + [cast_name]
        for alias in new_aliases:
            cursor.execute(
                'INSERT OR IGNORE INTO cast_aliases (primary_name, alias_name) VALUES (?,?)',
                (new_primary, alias)
            )

        # Move the profile (attributes, gender, category, favourite, external link) to the new
        # primary - the current profile wins over anything left under the alias. A photo the
        # alias had of its own becomes the profile photo; otherwise the current one moves over.
        import db_maintenance
        db_maintenance.merge_cast_profile(conn, new_primary, cast_name, winner='from')
        conn.commit()

    return {"success": True, "new_primary": new_primary, "aliases": new_aliases}


@router.get("/resolve/{name}")
def resolve_cast_name(name: str, model = Depends(get_model)):
    """
    Given any name (primary or alias), return the canonical primary name.
    Used by the cast browser to redirect alias searches to the right profile.
    """
    name = urllib.parse.unquote(name)
    if not USE_SQLITE:
        return {"name": name, "primary": name, "is_alias": False}
    with model.db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT primary_name FROM cast_aliases WHERE alias_name = ? LIMIT 1', (name,))
        row = cursor.fetchone()
        if row:
            return {"name": name, "primary": row['primary_name'], "is_alias": True}
    return {"name": name, "primary": name, "is_alias": False}