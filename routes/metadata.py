# This file is: ./routes/metadata.py

"""
Metadata management routes.
Handles metadata refresh, bulk updates, and schema management.
"""

import os
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional, Dict
from helpers import get_file_data
from config import USE_SQLITE, DATA_FILE
import movies
import logging as _logging
_log = _logging.getLogger('lustr.metadata')

router = APIRouter(prefix="/api", tags=["metadata"])

# Global references (will be set by web_server.py)
_model = None

def get_model():
    """Dependency function to get model."""
    return _model

def set_model(model):
    """Set the model instance."""
    global _model
    _model = model


# --- REQUEST MODELS ---

class RefreshMetadataRequest(BaseModel):
    force_all: bool = False
    paths: Optional[List[str]] = None


class ToggleFavRequest(BaseModel):
    path: str


class UpdateMetadataRequest(BaseModel):
    paths: List[str]
    updates: Dict[str, str]
    add_to_lists: Optional[Dict[str, List[str]]] = None
    remove_from_lists: Optional[Dict[str, List[str]]] = None
    replace_with: Optional[Dict[str, Dict[str, List[str]]]] = None
    removals: Optional[Dict[str, List[str]]] = None
    clear_lists: Optional[List[str]] = None     # list fields emptied on every video ('*' in the editor);
                                                # 'cast' keeps the Male Cast, 'male_cast' clears only them


def _male_names(video_id):
    try:
        from ext_db import male_cast_for_video
        return {str(n) for n in male_cast_for_video(video_id)}
    except Exception:
        return set()


# --- ROUTES ---

@router.post("/metadata/refresh")
def refresh_metadata(req: RefreshMetadataRequest, model = Depends(get_model)):
    """Refresh technical metadata for videos."""
    count = model.refresh_technical_metadata(paths=req.paths, force_all=req.force_all)
    return {"updated": count}


@router.post("/toggle_fav")
def toggle_fav(req: ToggleFavRequest, model = Depends(get_model)):
    """Toggle favorite status for a video."""
    return {"favorite": model.toggle_favorite(req.path)}


@router.post("/metadata/update")
def update_metadata(req: UpdateMetadataRequest, model = Depends(get_model)):
    """Bulk update metadata for multiple files."""
    try:
        _log.debug(f"=== UPDATE METADATA DEBUG ===")
        _log.debug(f"Received {len(req.paths)} paths: {req.paths}")
        _log.debug(f"Updates: {req.updates}")
        _log.debug(f"Add to lists: {req.add_to_lists}")
        _log.debug(f"Remove from lists: {req.remove_from_lists}")
        
        count = 0
        target_files = set()
        if req.add_to_lists and USE_SQLITE:          # new values in the house format ('Big Tits' -> 'big.tits'; value_format.py)
            import value_format
            req.add_to_lists = value_format.house_lists(model.db.get_connection(), req.add_to_lists)
        
        # Expand directories to files
        for path in req.paths:
            if os.path.isdir(path):
                search_prefix = os.path.normpath(path)
                
                if USE_SQLITE:
                    with model.db.get_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL AND path LIKE ?', (f"{search_prefix}%",))
                        for row in cursor.fetchall():
                            target_files.add(row['path'])
                else:
                    for db_path in model.files:
                        if db_path.startswith(search_prefix):
                            target_files.add(db_path)
            else:
                # Check if file exists
                file_data = get_file_data(model, path)
                if file_data:
                    target_files.add(path)
        
        # Update each target file
        for path in target_files:
            # Get fresh file data for each file
            file_data = get_file_data(model, path)
            if not file_data:
                continue
            cast_before = {str(c).lower() for c in (file_data.get('cast') or [])}
            
            # Make a working copy if using JSON, or work directly if using SQLite
            if not USE_SQLITE:
                file_data = model.files[path]
            
            changed = False

            # Fields cleared on every video. Cast and Male Cast share one list: 'cast' empties
            # the rest of the cast, 'male_cast' only the Male Cast, both empty it all.
            clear = {f.lower() for f in (req.clear_lists or [])}
            if clear & {'cast', 'male_cast'}:
                current_vals = file_data.get('cast') or []
                if {'cast', 'male_cast'} <= clear:
                    new_vals = []
                else:
                    vrow = model.db.get_video_by_path(path) if USE_SQLITE else None
                    male = _male_names(vrow['video_id']) if vrow else set()
                    new_vals = [c for c in current_vals if (c in male) == ('cast' in clear)]
                if new_vals != current_vals:
                    file_data['cast'] = new_vals
                    changed = True
            for field_lower in clear - {'cast', 'male_cast'}:
                if file_data.get(field_lower):
                    file_data[field_lower] = []
                    changed = True

            # Handle smart case replacements (NEW)
            if req.replace_with:
                for field, replacements in req.replace_with.items():
                    field_lower = field.lower()
                    current_vals = file_data.get(field_lower, [])
                    if isinstance(current_vals, list) and current_vals:
                        new_vals = []
                        for val in current_vals:
                            if val in replacements:
                                # Replace with other variants
                                new_vals.extend(replacements[val])
                                changed = True
                            else:
                                # Keep as-is
                                new_vals.append(val)
                        
                        if changed:
                            # Remove duplicates while preserving order
                            seen = set()
                            final_vals = []
                            for v in new_vals:
                                v_lower = str(v).lower()
                                if v_lower not in seen:
                                    seen.add(v_lower)
                                    final_vals.append(v)
                            file_data[field_lower] = sorted(final_vals)
            
            # Handle list field removals (case-sensitive exact match)
            if req.remove_from_lists:
                for field, tags_to_remove in req.remove_from_lists.items():
                    field_lower = field.lower()
                    current_vals = file_data.get(field_lower, [])
                    if isinstance(current_vals, list) and current_vals:
                        new_vals = [t for t in current_vals if t not in tags_to_remove]
                        if new_vals != current_vals:
                            file_data[field_lower] = new_vals
                            changed = True
            
            # Handle legacy removals format (case-sensitive exact match)
            if req.removals:
                for field, tags_to_remove in req.removals.items():
                    field_lower = field.lower()
                    current_vals = file_data.get(field_lower, [])
                    if isinstance(current_vals, list) and current_vals:
                        new_vals = [t for t in current_vals if t not in tags_to_remove]
                        if new_vals != current_vals:
                            file_data[field_lower] = new_vals
                            changed = True
            
            # Handle list field additions (case-insensitive duplicate check)
            if req.add_to_lists:
                for field, tags_to_add in req.add_to_lists.items():
                    field_lower = field.lower()
                    current_vals = file_data.get(field_lower, [])
                    if not isinstance(current_vals, list):
                        current_vals = []
                    
                    # Build case-insensitive set of existing values
                    current_vals_lower = set(str(v).lower() for v in current_vals)
                    
                    field_changed = False
                    for t in tags_to_add:
                        if t and str(t).lower() not in current_vals_lower:
                            current_vals.append(t)
                            current_vals_lower.add(str(t).lower())
                            field_changed = True
                    
                    if field_changed:
                        file_data[field_lower] = sorted(current_vals)
                        changed = True
            
            # Handle scalar field updates
            if req.updates:
                for field, val in req.updates.items():
                    field_lower = field.lower()
                    if field_lower == 'rating':                   # half stars, 0.5 - 5 ('' / 0: no rating)
                        from database import stars
                        val = stars(val)
                    if file_data.get(field_lower) != val.strip():
                        file_data[field_lower] = val.strip()
                        changed = True
            
            # Update date_last_edited if anything changed
            if changed:
                file_data['date_last_edited'] = str(datetime.now())
                
                # Save the changes
                if USE_SQLITE:
                    # Get video_id for this path
                    video = model.db.get_video_by_path(path)
                    if video:
                        video_id = video['video_id']
                        
                        # Verify video exists in videos table
                        conn = model.db.get_connection()
                        cursor = conn.cursor()
                        cursor.execute('SELECT 1 FROM videos WHERE video_id = ?', (video_id,))
                        if not cursor.fetchone():
                            print(f"ERROR: video_id '{video_id}' not found in videos table for path: {path}")
                            print(f"This file may need to be rescanned. Skipping metadata update.")
                            continue
                        
                        # Verify path exists in file_versions table
                        cursor.execute('SELECT 1 FROM file_versions WHERE path = ?', (path,))
                        if not cursor.fetchone():
                            print(f"ERROR: path '{path}' not found in file_versions table")
                            print(f"This file may need to be rescanned. Skipping metadata update.")
                            continue
                        
                        # Update the videos table with changed metadata
                        conn = model.db.get_connection()
                        cursor = conn.cursor()
                        
                        # Build update fields (only non-list metadata fields in videos table)
                        videos_table_fields = ['title', 'year', 'rating']
                        
                        # ALL list fields now use video-level junction tables
                        # (files are just different encodings of the same content)
                        import fields
                        video_junction_fields = fields.list_fields(conn)
                        
                        # Update videos table fields (non-list only)
                        updates = []
                        values = []
                        for field in videos_table_fields:
                            if field in file_data:
                                val = file_data[field]
                                updates.append(f"{field} = ?")
                                values.append(val)
                        
                        # Add date_last_edited
                        updates.append("date_last_edited = ?")
                        values.append(file_data['date_last_edited'])
                        updates.append("updated_at = ?")
                        values.append(datetime.now().isoformat())
                        
                        if updates:
                            query = f"UPDATE videos SET {', '.join(updates)} WHERE video_id = ?"
                            values.append(video_id)
                            cursor.execute(query, values)
                        
                        # Update ALL junction table fields (all are video-level now)
                        for field in video_junction_fields:
                            if field in file_data:
                                field_lower = field.lower()
                                new_values = file_data[field]
                                if not isinstance(new_values, list):
                                    continue
                                
                                table_name = f'video_{field_lower}'
                                junction_table = f'video_{field_lower}_junction'
                                
                                try:
                                    # Delete existing entries for this video
                                    cursor.execute(f'''
                                        DELETE FROM {junction_table} WHERE video_id = ?
                                    ''', (video_id,))
                                    
                                    # Insert new entries
                                    for value in new_values:
                                        if not value:
                                            continue
                                        
                                        # Get or create the value ID
                                        cursor.execute(f'''
                                            SELECT id FROM {table_name} WHERE value = ?
                                        ''', (value,))
                                        
                                        row = cursor.fetchone()
                                        if row:
                                            value_id = row[0]
                                        else:
                                            cursor.execute(f'''
                                                INSERT INTO {table_name} (value) VALUES (?)
                                            ''', (value,))
                                            value_id = cursor.lastrowid
                                        
                                        # Create junction entry
                                        cursor.execute(f'''
                                            INSERT INTO {junction_table} (video_id, {field_lower}_id)
                                            VALUES (?, ?)
                                        ''', (video_id, value_id))
                                
                                except Exception as e:
                                    print(f"ERROR updating {field} junction table:")
                                    print(f"  Field: {field_lower}")
                                    print(f"  Video ID: {video_id}")
                                    print(f"  Values: {new_values}")
                                    print(f"  Error: {e}")
                                    raise
                        
                        if req.clear_lists and {'cast', 'male_cast'} & {f.lower() for f in req.clear_lists}:
                            try:                   # Male Cast marks of people no longer in the cast
                                cursor.execute('DELETE FROM video_cast_role WHERE video_id = ? AND cast_id NOT IN '
                                               '(SELECT cast_id FROM video_cast_junction WHERE video_id = ?)', (video_id, video_id))
                            except Exception:
                                pass
                        if any(k in movies.UPDATE_KEYS for k in (req.updates or {})):
                            movies.apply_updates(conn, video_id, req.updates)   # Movie / Scene # / Part #
                        conn.commit()
                        # performers added to this video bring their starred attributes (Settings)
                        added_cast = [c for c in (file_data.get('cast') or []) if str(c).lower() not in cast_before]
                        if added_cast:
                            import automation
                            automation.on_cast_added(conn, video_id, added_cast)
                # For JSON, changes are already in model.files[path]
                
                count += 1
        
        if req.clear_lists and {'cast', 'male_cast'} & {f.lower() for f in req.clear_lists}:
            try:
                from ext_db import _male_invalidate
                _male_invalidate()
            except Exception:
                pass

        # Save JSON once at the end if using JSON
        if not USE_SQLITE and count > 0:
            model.save()
        
        _log.debug(f"=== UPDATE COMPLETE: {count} files updated ===")
        _log.debug(f"Target files found: {len(target_files)}")
        
        return {"updated": count}
    
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"=== ERROR IN UPDATE_METADATA ===")
        print(error_details)
        raise HTTPException(status_code=500, detail=str(e))


# --- FIELDS & EDITOR LAYOUT (fields.py; Settings -> Metadata) ---

def _fields_conn():
    return _model.db.get_connection()


def _fields_reply(fn):
    import fields
    try:
        out = fn()
        return {'success': True, 'layout': fields.public(_fields_conn()), **(out or {})}
    except fields.FieldError as e:
        return {'success': False, 'error': str(e)}


@router.get("/fields")
def get_fields():
    import fields
    return {'success': True, 'layout': fields.public(_fields_conn())}


class FieldLayoutBody(BaseModel):
    tabs: List[dict]
    fields: Dict[str, dict] = {}


@router.post("/fields/layout")
def save_field_layout(body: FieldLayoutBody):
    import fields
    return _fields_reply(lambda: fields.save_layout(_fields_conn(), body.dict()) and None)


class FieldAddBody(BaseModel):
    label: str
    tab: Optional[str] = None
    words: bool = True
    performer: bool = False


@router.post("/fields/add")
def add_field(body: FieldAddBody):
    import fields
    return _fields_reply(lambda: {'key': fields.add_field(_fields_conn(), body.label, body.tab, body.words, body.performer)})


class FieldKeyBody(BaseModel):
    key: str
    confirm: Optional[str] = None


@router.post("/fields/hide")
def hide_field(body: FieldKeyBody):
    import fields
    return _fields_reply(lambda: fields.hide(_fields_conn(), body.key))


@router.post("/fields/restore")
def restore_field(body: FieldKeyBody):
    import fields
    return _fields_reply(lambda: fields.restore(_fields_conn(), body.key))


@router.get("/fields/usage")
def field_usage(key: str):
    """What Delete permanently would erase."""
    import fields
    return {'success': True, 'usage': fields.usage(_fields_conn(), key)}


@router.get("/fields/check")
def field_check(key: str):
    """Before turning on single-word values: how many of the field's values have spaces or capitals."""
    import fields
    import value_format
    if key not in fields.all_fields(_fields_conn()):
        return {'success': False, 'error': 'Unknown field'}
    vals = [r[0] for r in _fields_conn().execute(f'SELECT value FROM video_{key}')]
    odd = [v for v in vals if v and value_format.fmt(v) != v]
    return {'success': True, 'values': len(vals), 'not_single_word': len(odd), 'examples': odd[:5]}


@router.post("/fields/delete")
def delete_field(body: FieldKeyBody):
    import fields
    return _fields_reply(lambda: {'backup': fields.delete_permanently(_fields_conn(), body.key, body.confirm)})


@router.post("/fields/reset")
def reset_fields():
    import fields
    return _fields_reply(lambda: fields.reset_default(_fields_conn()))