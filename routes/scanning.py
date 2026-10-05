# This file is: ./routes/scanning.py

"""
Scanning routes for library management.
Handles library scanning and duplicate file detection.
"""

import os
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from media_handler import MediaHandler
from config import USE_SQLITE

router = APIRouter(prefix="/api", tags=["scanning"])

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

class ScanRequest(BaseModel):
    fast: bool = False
    library: str = None  # Optional: specific library to scan


# --- ROUTES ---

import threading
SCAN_LOCK = threading.Lock()      # a routine scan (automation.py) and a manual one never overlap


@router.post("/scan")
def scan_library(req: ScanRequest, model = Depends(get_model)):
    """Scan library for new and removed videos."""
    with SCAN_LOCK:
        return _scan_library(req, model)


def _scan_library(req, model):
    new, removed = model.scan_library(fast=req.fast, library=req.library)
    
    # Note: The new version-aware scan already handles metadata extraction
    # No need to call _ensure_file_entry() - that's for the old schema
    
    # Check for cross-library replacements after scanning
    if USE_SQLITE:
        model.check_cross_library_replacements()
        # Finalize deletions AFTER cross-library matching, so files flagged as
        # 'deleted' in this scan get a chance to be matched to their new location
        # before being permanently removed. Files that matched cross-library are
        # now flagged 'possibly_replaced' and are never purged automatically.
        import db_maintenance
        try:
            result = db_maintenance.run(model.db.get_connection(), dry_run=False, auto=True)
            if result.get('success') and result.get('removed_total'):
                print(f"[Maintenance] after scan: {db_maintenance._short(result)}")
        except Exception as e:
            print(f"[Maintenance] after scan failed: {e}")
    
    model.save()
    
    # Generate thumbnails for new files
    for path in new:
        MediaHandler.generate_thumbnails(path)
    
    return {"new": len(new), "removed": len(removed)}


@router.get("/duplicates/check")
def check_duplicate_filenames(model = Depends(get_model)):
    """Find files with same filename but different lengths (potential cache conflicts)."""
    # Group by filename
    by_filename = {}
    
    if USE_SQLITE:
        with model.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT fv.path, fv.length, v.title, fv.cache_id FROM file_versions fv '
                           'JOIN videos v ON v.video_id = fv.video_id WHERE fv.deleted_at IS NULL')
            
            for row in cursor.fetchall():
                path = row['path']
                filename = os.path.basename(path).lower()
                
                if filename not in by_filename:
                    by_filename[filename] = []
                
                # Parse length to seconds
                length_str = row['length'] or '00:00:00'
                try:
                    parts = length_str.split(':')
                    if len(parts) == 3:
                        h, m, s = map(int, parts)
                        length_seconds = h * 3600 + m * 60 + s
                    else:
                        length_seconds = 0
                except:
                    length_seconds = 0
                
                # Get library
                library = 'Unknown'
                for lib_name, lib_paths in model.libraries.items():
                    if any(path.startswith(os.path.normpath(p) + os.sep) for p in lib_paths):
                        library = lib_name
                        break
                
                by_filename[filename].append({
                    'path': path,
                    'length': length_str,
                    'length_seconds': length_seconds,
                    'title': row['title'] or '',
                    'cache_id': row['cache_id'] or MediaHandler._get_cache_key(path),
                    'library': library
                })
    else:
        for path, data in model.files.items():
            if path == "_ui_config":
                continue
            filename = os.path.basename(path).lower()
            if filename not in by_filename:
                by_filename[filename] = []
            
            # Parse length to seconds
            length_str = data.get('length', '00:00:00')
            try:
                parts = length_str.split(':')
                if len(parts) == 3:
                    h, m, s = map(int, parts)
                    length_seconds = h * 3600 + m * 60 + s
                else:
                    length_seconds = 0
            except:
                length_seconds = 0
            
            # Use stored cache_id if available, otherwise compute it
            cache_id = data.get('cache_id') or MediaHandler._get_cache_key(path)
            
            # Get library
            library = 'Unknown'
            for lib_name, lib_paths in model.libraries.items():
                if any(path.startswith(os.path.normpath(p) + os.sep) for p in lib_paths):
                    library = lib_name
                    break
            
            by_filename[filename].append({
                'path': path,
                'length': length_str,
                'length_seconds': length_seconds,
                'title': data.get('title', ''),
                'cache_id': cache_id,
                'library': library
            })
    
    # Find conflicts (same name, different length)
    conflicts = []
    already_fixed = []
    
    for filename, files in by_filename.items():
        if len(files) > 1:
            lengths = set(f['length_seconds'] for f in files)
            if len(lengths) > 1:  # Different lengths = potential conflict
                # Check if cache IDs are already different
                cache_ids = set(f['cache_id'] for f in files)
                
                if len(cache_ids) > 1:
                    # Already fixed - different cache IDs
                    already_fixed.append({
                        'filename': filename,
                        'files': files
                    })
                else:
                    # Still conflicted - same cache ID
                    conflicts.append({
                        'filename': filename,
                        'files': files
                    })
    
    return {
        "conflicts": conflicts,
        "count": len(conflicts),
        "already_fixed": already_fixed,
        "fixed_count": len(already_fixed)
    }