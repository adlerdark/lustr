# This file is: ./routes/playlists.py

"""
Playlist and Watchlist management routes.
"""

from fastapi import APIRouter, Depends, Request, HTTPException
from typing import Optional, List
from pydantic import BaseModel
from helpers import check_auth

router = APIRouter(prefix="/api/playlists", tags=["playlists"])

# Global references (set by web_server.py)
_model = None
_db = None

def get_model():
    return _model

def get_db():
    return _db

def set_model(model):
    global _model
    _model = model

def set_db(db):
    global _db
    _db = db


def _display(conn, col='v.video_id', lib_col='v.library'):
    """(' AND <cond>', params) for the current user's display filter, or ('', [])."""
    if not _model:
        return '', []
    import display_filter
    sql, params = display_filter.condition(conn, _model, col=col, lib_col=lib_col)
    return (f' AND {sql}', params) if sql else ('', [])


# ============================================
# REQUEST MODELS
# ============================================

class CreatePlaylistRequest(BaseModel):
    name: str

class RenamePlaylistRequest(BaseModel):
    playlist_id: int
    name: str

class AddToPlaylistRequest(BaseModel):
    playlist_id: int
    video_ids: List[str]

class RemoveFromPlaylistRequest(BaseModel):
    playlist_id: int
    video_ids: List[str]

class ReorderPlaylistRequest(BaseModel):
    playlist_id: int
    video_id: str
    new_position: int


# ============================================
# PLAYLIST CRUD
# ============================================

@router.get("")
def list_playlists(request: Request):
    """Get all playlists including the watchlist."""
    username = check_auth(request)
    db = get_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    shown, shown_params = _display(conn)          # items hidden by the display filter aren't counted
    
    cursor.execute(f'''
        SELECT 
            p.id,
            p.name,
            p.is_watchlist,
            p.created_at,
            p.updated_at,
            COUNT(v.video_id) as item_count
        FROM playlists p
        LEFT JOIN playlist_items pi ON p.id = pi.playlist_id
        LEFT JOIN videos v ON v.video_id = pi.video_id{shown}
        WHERE COALESCE(p.kind, 'playlist') = 'playlist'
        GROUP BY p.id
        ORDER BY p.is_watchlist DESC, p.name ASC
    ''', shown_params)
    
    playlists = []
    for row in cursor.fetchall():
        playlists.append({
            'id': row[0],
            'name': row[1],
            'is_watchlist': bool(row[2]),
            'created_at': row[3],
            'updated_at': row[4],
            'item_count': row[5]
        })
    
    return {'playlists': playlists}


@router.post("/create")
def create_playlist(request: Request, data: CreatePlaylistRequest):
    """Create a new playlist (max 5000 playlists)."""
    username = check_auth(request)
    db = get_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Check playlist limit
    cursor.execute('SELECT COUNT(*) FROM playlists WHERE is_watchlist = 0')
    count = cursor.fetchone()[0]
    if count >= 5000:
        raise HTTPException(status_code=400, detail="Maximum 5000 playlists allowed")
    
    # Check for duplicate name
    cursor.execute('SELECT id FROM playlists WHERE name = ?', (data.name,))
    if cursor.fetchone():
        raise HTTPException(status_code=400, detail="Playlist name already exists")
    
    # Create playlist
    cursor.execute('''
        INSERT INTO playlists (name, is_watchlist) 
        VALUES (?, 0)
    ''', (data.name,))
    
    playlist_id = cursor.lastrowid
    conn.commit()
    
    return {'id': playlist_id, 'name': data.name}


@router.post("/rename")
def rename_playlist(request: Request, data: RenamePlaylistRequest):
    """Rename a playlist (cannot rename watchlist)."""
    username = check_auth(request)
    db = get_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Check if playlist exists and is not watchlist
    cursor.execute('SELECT is_watchlist FROM playlists WHERE id = ?', (data.playlist_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if row[0]:
        raise HTTPException(status_code=400, detail="Cannot rename Watchlist")
    
    # Check for duplicate name
    cursor.execute('SELECT id FROM playlists WHERE name = ? AND id != ?', (data.name, data.playlist_id))
    if cursor.fetchone():
        raise HTTPException(status_code=400, detail="Playlist name already exists")
    
    # Rename
    cursor.execute('''
        UPDATE playlists 
        SET name = ?, updated_at = CURRENT_TIMESTAMP 
        WHERE id = ?
    ''', (data.name, data.playlist_id))
    
    conn.commit()
    return {'success': True}


@router.delete("/{playlist_id}")
def delete_playlist(request: Request, playlist_id: int):
    """Delete a playlist (cannot delete watchlist)."""
    username = check_auth(request)
    db = get_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Check if playlist exists and is not watchlist
    cursor.execute('SELECT is_watchlist FROM playlists WHERE id = ?', (playlist_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if row[0]:
        raise HTTPException(status_code=400, detail="Cannot delete Watchlist")
    
    # Delete playlist (cascade will delete items)
    cursor.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
    conn.commit()
    
    return {'success': True}


# ============================================
# PLAYLIST ITEMS
# ============================================

@router.get("/{playlist_id}/items")
def get_playlist_items(
    request: Request,
    playlist_id: int,
    sort_by: str = "position",
    desc: bool = False
):
    """Get all videos in a playlist with full metadata."""
    username = check_auth(request)
    db = get_db()
    model = get_model()
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Verify playlist exists
    cursor.execute('SELECT name FROM playlists WHERE id = ?', (playlist_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    playlist_name = row[0]
    
    # Build sort clause
    sort_field_map = {
        'position': 'pi.position',
        'added_at': 'pi.added_at',
        'Filename': 'v.filename_base',
        'Title': 'v.title',
        'Year': 'v.year',
        'Rating': 'v.rating',
        'Date Added': 'v.date_added',
        'Length': 'fv.length_seconds'
    }
    
    sort_field = sort_field_map.get(sort_by, 'pi.position')
    sort_order = 'DESC' if desc else 'ASC'
    
    # Get playlist items with full video metadata (minus those hidden by the display filter)
    shown, shown_params = _display(conn)
    cursor.execute(f'''
        SELECT 
            v.video_id,
            v.filename_base,
            v.title,
            v.year,
            v.rating,
            v.favorite,
            fv.path,
            fv.filename,
            fv.resolution,
            fv.codec,
            fv.extension,
            fv.length,
            fv.length_seconds,
            fv.file_size,
            fv.cache_id,
            v.date_added,
            pi.position,
            pi.added_at
        FROM playlist_items pi
        JOIN videos v ON pi.video_id = v.video_id
        JOIN file_versions fv ON v.video_id = fv.video_id AND fv.is_preferred = 1
        WHERE pi.playlist_id = ? AND fv.deleted_at IS NULL{shown}
        ORDER BY {sort_field} {sort_order}
    ''', (playlist_id, *shown_params))
    
    items = []
    for row in cursor.fetchall():
        video_id = row[0]
        cache_id = row[14]
        filename = row[7]
        
        # If cache_id is NULL, compute it from filename (matching library behavior)
        if not cache_id:
            import hashlib
            cache_id = hashlib.md5(filename.encode('utf-8')).hexdigest()
        
        video_data = {
            'video_id': video_id,
            'filename_base': row[1],
            'title': row[2],
            'year': row[3],
            'rating': row[4],
            'favorite': bool(row[5]),
            'path': row[6],
            'filename': filename,
            'resolution': row[8],
            'codec': row[9],
            'extension': row[10],
            'length': row[11],
            'length_seconds': row[12],
            'file_size': row[13],
            'cache_id': cache_id,
            'date_added': row[15],
            'playlist_position': row[16],
            'added_to_playlist': row[17],
            # Now use the computed cache_id
            'thumb': f'/cache/{cache_id}/thumb_0.webp'
        }
        
        # Get cast members using the correct table structure
        cursor.execute('''
            SELECT vc.value 
            FROM video_cast_junction vcj
            JOIN video_cast vc ON vcj.cast_id = vc.id
            WHERE vcj.video_id = ?
            ORDER BY vc.value
        ''', (video_id,))
        video_data['cast'] = [r[0] for r in cursor.fetchall()]
        
        # Get ALL list field values (tags, hair, face, feature, etc.) - excluding cast and site
        # This matches the library behavior where "tags" in the sidebar = all list fields
        from config import LIST_FIELDS
        
        all_tag_values = []  # Combined for sidebar tag filtering
        
        for field in LIST_FIELDS:
            field_lower = field.lower()
            if field_lower in ['cast', 'site']:  # Already fetched separately
                continue
            
            table_name = f'video_{field_lower}'
            junction_table = f'video_{field_lower}_junction'
            field_id_col = f'{field_lower}_id'
            
            try:
                cursor.execute(f'''
                    SELECT vf.value 
                    FROM {junction_table} vfj
                    JOIN {table_name} vf ON vfj.{field_id_col} = vf.id
                    WHERE vfj.video_id = ?
                    ORDER BY vf.value
                ''', (video_id,))
                field_values = [r[0] for r in cursor.fetchall()]
                video_data[field_lower] = field_values   # individual field for metadata editor
                all_tag_values.extend(field_values)      # combined for sidebar
            except Exception as e:
                video_data[field_lower] = []
        
        video_data['_all_tags'] = all_tag_values  # combined blob for sidebar - does NOT overwrite real tags field
        
        # Get sites
        cursor.execute('''
            SELECT vs.value 
            FROM video_site_junction vsj
            JOIN video_site vs ON vsj.site_id = vs.id
            WHERE vsj.video_id = ?
            ORDER BY vs.value
        ''', (video_id,))
        site_values = [r[0] for r in cursor.fetchall()]
        video_data['site'] = site_values    # singular - matches metadata editor field name
        video_data['sites'] = site_values   # plural - kept for sidebar filtering
        
        # Get file versions
        cursor.execute('''
            SELECT path, filename, resolution, codec, extension, file_size, is_preferred
            FROM file_versions
            WHERE video_id = ? AND deleted_at IS NULL
            ORDER BY is_preferred DESC, file_size DESC
        ''', (video_id,))
        versions = []
        for v in cursor.fetchall():
            versions.append({
                'path': v[0],
                'filename': v[1],
                'resolution': v[2],
                'codec': v[3],
                'extension': v[4],
                'file_size': v[5],
                'is_preferred': bool(v[6])
            })
        video_data['versions'] = versions
        
        items.append(video_data)
    
    return {
        'playlist_id': playlist_id,
        'playlist_name': playlist_name,
        'items': items,
        'total': len(items)
    }


@router.post("/add")
def add_to_playlist(request: Request, data: AddToPlaylistRequest):
    """Add videos to a playlist (max 1000 items per playlist)."""
    username = check_auth(request)
    db = get_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Check current item count
    cursor.execute('SELECT COUNT(*) FROM playlist_items WHERE playlist_id = ?', (data.playlist_id,))
    current_count = cursor.fetchone()[0]
    
    if current_count + len(data.video_ids) > 1000:
        raise HTTPException(
            status_code=400, 
            detail=f"Maximum 1000 items per playlist. Currently {current_count} items."
        )
    
    # Get next position
    cursor.execute('SELECT MAX(position) FROM playlist_items WHERE playlist_id = ?', (data.playlist_id,))
    max_pos = cursor.fetchone()[0]
    next_position = (max_pos or 0) + 1
    
    # Add videos
    added = 0
    for video_id in data.video_ids:
        try:
            cursor.execute('''
                INSERT INTO playlist_items (playlist_id, video_id, position)
                VALUES (?, ?, ?)
            ''', (data.playlist_id, video_id, next_position))
            next_position += 1
            added += 1
        except Exception:
            # Skip duplicates
            continue
    
    # Update playlist timestamp
    cursor.execute('UPDATE playlists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (data.playlist_id,))
    
    conn.commit()
    return {'added': added, 'skipped': len(data.video_ids) - added}


@router.post("/remove")
def remove_from_playlist(request: Request, data: RemoveFromPlaylistRequest):
    """Remove videos from a playlist."""
    username = check_auth(request)
    db = get_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    
    print(f"[DEBUG] Removing from playlist {data.playlist_id}: video_ids={data.video_ids}")
    
    # Remove videos
    placeholders = ','.join(['?' for _ in data.video_ids])
    query = f'DELETE FROM playlist_items WHERE playlist_id = ? AND video_id IN ({placeholders})'
    params = [data.playlist_id] + data.video_ids
    print(f"[DEBUG] Query: {query}")
    print(f"[DEBUG] Params: {params}")
    
    cursor.execute(query, params)
    
    removed = cursor.rowcount
    print(f"[DEBUG] Removed {removed} items")
    
    # Reorder remaining items
    cursor.execute('''
        SELECT id FROM playlist_items 
        WHERE playlist_id = ? 
        ORDER BY position ASC
    ''', (data.playlist_id,))
    
    items = cursor.fetchall()
    for i, (item_id,) in enumerate(items, start=1):
        cursor.execute('UPDATE playlist_items SET position = ? WHERE id = ?', (i, item_id))
    
    # Update playlist timestamp
    cursor.execute('UPDATE playlists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (data.playlist_id,))
    
    conn.commit()
    return {'removed': removed}


@router.post("/reorder")
def reorder_playlist_item(request: Request, data: ReorderPlaylistRequest):
    """Move a video to a new position in the playlist."""
    username = check_auth(request)
    db = get_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Get current position
    cursor.execute('''
        SELECT position FROM playlist_items 
        WHERE playlist_id = ? AND video_id = ?
    ''', (data.playlist_id, data.video_id))
    
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video not in playlist")
    
    old_position = row[0]
    new_position = data.new_position
    
    if old_position == new_position:
        return {'success': True}
    
    # Shift other items
    if old_position < new_position:
        # Moving down: shift items up
        cursor.execute('''
            UPDATE playlist_items 
            SET position = position - 1
            WHERE playlist_id = ? AND position > ? AND position <= ?
        ''', (data.playlist_id, old_position, new_position))
    else:
        # Moving up: shift items down
        cursor.execute('''
            UPDATE playlist_items 
            SET position = position + 1
            WHERE playlist_id = ? AND position >= ? AND position < ?
        ''', (data.playlist_id, new_position, old_position))
    
    # Update the moved item
    cursor.execute('''
        UPDATE playlist_items 
        SET position = ? 
        WHERE playlist_id = ? AND video_id = ?
    ''', (new_position, data.playlist_id, data.video_id))
    
    # Update playlist timestamp
    cursor.execute('UPDATE playlists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (data.playlist_id,))
    
    conn.commit()
    return {'success': True}


# ============================================
# PLAYLIST CONTEXT (for video playback)
# ============================================

@router.get("/{playlist_id}/context/{video_id}")
def get_playlist_context(request: Request, playlist_id: int, video_id: str):
    """Get previous/next videos in playlist for sequential playback."""
    username = check_auth(request)
    db = get_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Get current video's position
    cursor.execute('''
        SELECT position FROM playlist_items 
        WHERE playlist_id = ? AND video_id = ?
    ''', (playlist_id, video_id))
    
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video not in playlist")
    
    current_position = row[0]
    
    # Previous / next video, skipping items hidden by the display filter
    shown, shown_params = _display(conn, col='pi.video_id', lib_col='(SELECT library FROM videos WHERE video_id = pi.video_id)')
    cursor.execute(f'''
        SELECT pi.video_id, fv.path 
        FROM playlist_items pi
        JOIN file_versions fv ON pi.video_id = fv.video_id AND fv.is_preferred = 1
        WHERE pi.playlist_id = ? AND pi.position < ? AND fv.deleted_at IS NULL{shown}
        ORDER BY pi.position DESC LIMIT 1
    ''', (playlist_id, current_position, *shown_params))
    
    prev_row = cursor.fetchone()
    previous = {'video_id': prev_row[0], 'path': prev_row[1]} if prev_row else None
    
    cursor.execute(f'''
        SELECT pi.video_id, fv.path 
        FROM playlist_items pi
        JOIN file_versions fv ON pi.video_id = fv.video_id AND fv.is_preferred = 1
        WHERE pi.playlist_id = ? AND pi.position > ? AND fv.deleted_at IS NULL{shown}
        ORDER BY pi.position ASC LIMIT 1
    ''', (playlist_id, current_position, *shown_params))
    
    next_row = cursor.fetchone()
    next_video = {'video_id': next_row[0], 'path': next_row[1]} if next_row else None
    
    return {
        'playlist_id': playlist_id,
        'current_video_id': video_id,
        'current_position': current_position,
        'previous': previous,
        'next': next_video
    }