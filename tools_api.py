#!/usr/bin/env python3
# This file is: ./tools_api.py

"""
API endpoints for database tools (cleanup, search, metadata browsing).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from config import DATA_FILE, LIST_FIELDS
import db_maintenance as dm

router = APIRouter(prefix="/api/tools", tags=["tools"])

# Database path
DB_PATH = Path(DATA_FILE).parent / 'library.db'

class SearchRequest(BaseModel):
    field: str
    value: str

class CleanupRequest(BaseModel):
    dry_run: bool = True


_model = None


def set_model(model):
    global _model
    _model = model


def _conn():
    if _model is None or not getattr(_model, 'db', None):
        raise HTTPException(status_code=500, detail='Database not available')
    return _model.db.get_connection()            # foreign keys on, same as the rest of the app

# ============================================
# METADATA BROWSER / SEARCH
# ============================================

@router.get("/fields")
def get_fields():
    """Get list of all searchable fields."""
    # Exclude Cast and Site as requested
    fields = [f for f in LIST_FIELDS if f.lower() not in ['cast', 'site']]
    return {
        "fields": fields,
        "cast_site_available": True  # These are available as separate options
    }

@router.get("/values/{field}")
def get_field_values(field: str):
    """Get all unique values for a specific field."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail="Database not found")
    
    field_lower = field.lower()
    
    # Validate field
    valid_fields = [f.lower() for f in LIST_FIELDS]
    if field_lower not in valid_fields:
        raise HTTPException(status_code=400, detail=f"Invalid field: {field}")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    table_name = f'video_{field_lower}'
    junction_table = f'video_{field_lower}_junction'
    field_id_col = f'{field_lower}_id'
    
    try:
        # Get values with video counts (only non-deleted files)
        cursor.execute(f'''
            SELECT vf.value, COUNT(DISTINCT v.video_id) as video_count
            FROM {table_name} vf
            LEFT JOIN {junction_table} vfj ON vf.id = vfj.{field_id_col}
            LEFT JOIN videos v ON vfj.video_id = v.video_id
            LEFT JOIN file_versions fv ON v.video_id = fv.video_id
            WHERE fv.deleted_at IS NULL
            GROUP BY vf.value
            HAVING video_count > 0
            ORDER BY vf.value COLLATE NOCASE
        ''')
        
        values = [{"value": row[0], "count": row[1]} for row in cursor.fetchall()]
        conn.close()
        
        return {
            "field": field,
            "values": values,
            "total": len(values)
        }
        
    except sqlite3.OperationalError as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.post("/search")
def search_videos(req: SearchRequest):
    """Search for videos with a specific field value."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail="Database not found")
    
    field_lower = req.field.lower()
    
    # Validate field
    valid_fields = [f.lower() for f in LIST_FIELDS]
    if field_lower not in valid_fields:
        raise HTTPException(status_code=400, detail=f"Invalid field: {req.field}")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    table_name = f'video_{field_lower}'
    junction_table = f'video_{field_lower}_junction'
    field_id_col = f'{field_lower}_id'
    
    try:
        # Search for videos (case-insensitive)
        cursor.execute(f'''
            SELECT DISTINCT 
                v.video_id, 
                v.filename_base, 
                v.library, 
                v.title,
                v.year,
                v.favorite,
                fv.path, 
                fv.filename, 
                fv.length, 
                fv.length_seconds,
                fv.file_size,
                fv.resolution
            FROM videos v
            JOIN {junction_table} vfj ON v.video_id = vfj.video_id
            JOIN {table_name} vf ON vfj.{field_id_col} = vf.id
            JOIN file_versions fv ON v.video_id = fv.video_id AND fv.is_preferred = 1
            WHERE LOWER(vf.value) = LOWER(?)
            AND fv.deleted_at IS NULL
            ORDER BY v.filename_base
        ''', (req.value,))
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "video_id": row[0],
                "filename_base": row[1],
                "library": row[2],
                "title": row[3],
                "year": row[4],
                "favorite": row[5],
                "path": row[6],
                "filename": row[7],
                "length": row[8],
                "length_seconds": row[9],
                "file_size": row[10],
                "resolution": row[11]
            })
        
        conn.close()
        
        return {
            "field": req.field,
            "value": req.value,
            "count": len(results),
            "videos": results
        }
        
    except sqlite3.OperationalError as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

# ============================================
# DATABASE CLEANUP
# ============================================

@router.post("/cleanup")
def cleanup_database(req: CleanupRequest):
    """Run every maintenance action (Preview = dry run). Kept for older callers -
    the DB Maintenance page uses /maintenance/run."""
    return dm.run(_conn(), dry_run=req.dry_run)


# ============================================
# DATABASE MAINTENANCE
# ============================================
class MaintenanceRunBody(BaseModel):
    dry_run: bool = True
    actions: Optional[List[str]] = None
    vacuum: bool = False


class MaintenanceSettingsBody(BaseModel):
    retention_days: Optional[int] = None
    daily: Optional[bool] = None
    auto_backups_keep: Optional[int] = None


class PathsBody(BaseModel):
    paths: Optional[List[str]] = None
    library: Optional[str] = None
    expired_only: bool = False


class NamesBody(BaseModel):
    names: List[str]


@router.get("/maintenance/audit")
def maintenance_audit():
    rep = dm.audit(_conn())
    rep['actions'] = dm.ACTIONS
    rep['auto_actions'] = dm.AUTO_ACTIONS
    return rep


@router.post("/maintenance/run")
def maintenance_run(body: MaintenanceRunBody):
    try:
        return dm.run(_conn(), dry_run=body.dry_run, actions=body.actions, vacuum=body.vacuum)
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.get("/maintenance/settings")
def maintenance_settings():
    return dm.get_settings(_conn())


@router.post("/maintenance/settings")
def maintenance_settings_save(body: MaintenanceSettingsBody):
    changes = {k: v for k, v in body.dict().items() if v is not None}
    return dm.save_settings(_conn(), changes)


@router.get("/maintenance/deleted")
def maintenance_deleted(library: str = '', reason: str = '', q: str = '', page: int = 1, per_page: int = 200):
    """Soft-deleted files kept for move detection, newest first."""
    conn = _conn()
    dm.ensure_schema(conn)
    days = dm.get_settings(conn)['retention_days']
    roots = dm.library_roots(conn)
    rows = conn.execute(
        "SELECT fv.path, fv.filename, fv.deleted_at, fv.length_seconds, v.library, v.title, "
        "(SELECT fc.reason FROM file_changes fc WHERE fc.filepath = fv.path ORDER BY fc.id DESC LIMIT 1), "
        "(SELECT fc.flag_type FROM file_changes fc WHERE fc.filepath = fv.path ORDER BY fc.id DESC LIMIT 1), "
        "(SELECT COUNT(*) FROM file_versions o WHERE o.video_id = fv.video_id AND o.deleted_at IS NULL) "
        "FROM file_versions fv LEFT JOIN videos v ON v.video_id = fv.video_id "
        "WHERE fv.deleted_at IS NOT NULL ORDER BY fv.deleted_at DESC").fetchall()
    out, libs = [], {}
    for r in rows:
        reason_ = r[6] or 'scan'
        lib = r[4] or 'Unknown'
        libs[lib] = libs.get(lib, 0) + 1
        if library and lib != library:
            continue
        if reason and reason_ != reason:
            continue
        if q and q.lower() not in (r[0] or '').lower():
            continue
        try:
            left = (datetime.fromisoformat(r[2]) + timedelta(days=days) - datetime.now()).days
        except (TypeError, ValueError):
            left = None
        out.append({'path': r[0], 'filename': r[1], 'deleted_at': r[2], 'length_seconds': r[3], 'library': lib,
                    'title': r[5], 'reason': reason_, 'flag': r[7], 'other_active_files': r[8],
                    'days_left': left, 'review': r[7] == 'possibly_replaced'})
    total = len(out)
    page_rows = out[(max(1, page) - 1) * per_page: max(1, page) * per_page]
    for row in page_rows:                        # only for the rows shown: is the file back?
        row['on_disk'] = os.path.exists(row['path'])
        row['in_library'] = bool(dm.owner_of(row['path'], roots))
    return {'rows': page_rows, 'total': total, 'page': page, 'per_page': per_page, 'retention_days': days,
            'libraries': dict(sorted(libs.items(), key=lambda x: -x[1]))}


def _selected_deleted(conn, body):
    if body.paths:
        return list(body.paths)
    if body.library:
        rows = conn.execute("SELECT fv.path, fv.deleted_at FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id "
                            "WHERE fv.deleted_at IS NOT NULL AND v.library = ?", (body.library,)).fetchall()
        if body.expired_only:
            cutoff = (datetime.now() - timedelta(days=dm.get_settings(conn)['retention_days'])).isoformat()
            rows = [r for r in rows if r[1] < cutoff]
        return [r[0] for r in rows]
    return []


@router.post("/maintenance/deleted/purge")
def maintenance_deleted_purge(body: PathsBody):
    """Remove these soft-deleted files now. A file that is back on disk inside a library is
    restored instead (purging it would throw its metadata away)."""
    conn = _conn()
    paths = [p for p in _selected_deleted(conn, body)
             if conn.execute('SELECT 1 FROM file_versions WHERE path = ? AND deleted_at IS NOT NULL', (p,)).fetchone()]
    if not paths:
        return {'success': True, 'purged': 0, 'videos_removed': 0, 'restored': 0}
    roots = dm.library_roots(conn)
    back = [p for p in paths if dm.owner_of(p, roots) and os.path.exists(p)]
    try:
        backup = dm.make_backup(conn)
        restored = dm.restore_paths(conn, back)
        n, gone = dm.purge_versions(conn, [p for p in paths if p not in back])
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {'success': False, 'error': str(e)}
    return {'success': True, 'purged': n, 'videos_removed': gone, 'restored': restored, 'backup': backup}


@router.post("/maintenance/deleted/restore")
def maintenance_deleted_restore(body: PathsBody):
    """Un-delete files that exist on disk again inside a configured library."""
    conn = _conn()
    roots = dm.library_roots(conn)
    ok = [p for p in _selected_deleted(conn, body) if dm.owner_of(p, roots) and os.path.exists(p)]
    n = dm.restore_paths(conn, ok)
    conn.commit()
    return {'success': True, 'restored': n, 'not_on_disk': len(body.paths or []) - len(ok) if body.paths else None}


@router.get("/maintenance/orphan-cast")
def maintenance_orphan_cast():
    return {'performers': dm.orphan_cast_profiles(_conn())}


@router.post("/maintenance/orphan-cast/delete")
def maintenance_orphan_cast_delete(body: NamesBody):
    conn = _conn()
    allowed = {p['name'] for p in dm.orphan_cast_profiles(conn)}       # never someone who has videos
    names = [n for n in body.names if n in allowed]
    return {'success': True, 'deleted': len(names), 'rows': dm.delete_cast_profiles(conn, names)}


class AutomationBody(BaseModel):
    cast_attributes: Optional[bool] = None
    scan: Optional[Dict] = None


@router.get("/automation")
def automation_settings():
    import automation
    s = automation.get_settings(_conn())
    s['scan']['running'] = automation.scan_running()
    s['libraries'] = sorted((_model.libraries or {}).keys()) if _model else []
    return s


@router.post("/automation")
def automation_settings_save(body: AutomationBody):
    import automation
    changes = {k: v for k, v in body.dict().items() if v is not None}
    s = automation.save_settings(_conn(), changes)
    s['scan']['running'] = automation.scan_running()
    return {'success': True, **s}


class TagCleanupBody(BaseModel):
    ids: List[str] = []
    sites: Dict[str, str] = {}


@router.get("/maintenance/tag-cleanup")
def maintenance_tag_cleanup():
    """Malformed values, nationality words stored as ethnicity, site spellings (read-only)."""
    return dm.tag_cleanup_preview(_conn())


@router.post("/maintenance/tag-cleanup/apply")
def maintenance_tag_cleanup_apply(body: TagCleanupBody):
    try:
        return dm.tag_cleanup_apply(_conn(), body.ids, body.sites)
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.get("/maintenance/backups")
def maintenance_backups():
    b = dm.list_backups()
    return {'backups': b, 'bytes': sum(x['size'] for x in b)}


@router.post("/maintenance/backups/create")
def maintenance_backups_create():
    return {'success': True, 'backup': dm.make_backup(_conn())}


@router.post("/maintenance/backups/delete")
def maintenance_backups_delete(body: NamesBody):
    return {'success': True, 'deleted': dm.delete_backups(body.names)}
