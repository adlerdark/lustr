"""
Thumbnail management routes.
Handles thumbnail generation and cache regeneration.

CACHE KEY MODEL
---------------
Each row in file_versions can have its own `cache_id`. This is the
authoritative thumbnail cache key for that row, via
MediaHandler._get_cache_key(path, video_id, cache_id), which prefers
(in order): explicit cache_id -> MD5(video_id) -> MD5(filename).

- Merged videos: all versions share `video_id`. Normally only the
  preferred (primary) version has a thumbnail set, keyed by
  MD5(video_id) (when cache_id is NULL) or by its own cache_id.
- "Force New" (regenerate with a brand new cache_id) ALWAYS targets the
  preferred/primary row of a video_id group. Sibling (non-preferred)
  rows are never modified, so unmerging later does not affect them.
- For a video with only one version, the preferred row IS that single
  row, so the same logic applies uniformly.
"""

import os
import shutil
import hashlib
import time
import uuid
import threading
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List
from helpers import get_file_data
from media_handler import MediaHandler
from config import USE_SQLITE, CACHE_DIR

router = APIRouter(prefix="/api", tags=["thumbnails"])

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

class GenThumbsRequest(BaseModel):
    paths: List[str]
    force: bool = False
    video_ids: List[str] = []  # Optional: for merged videos, use primary version


class RegenCacheRequest(BaseModel):
    paths: List[str] = []
    path: str = None  # Backwards-compat single-path field


# --- HELPERS ---

def _get_preferred_row(model, video_id):
    """
    Return the preferred (primary) file_versions row dict for a video_id,
    or None if not found. Falls back to the first version if no row is
    marked is_preferred.
    """
    versions = model.db.get_file_versions(video_id)
    if not versions:
        return None
    primary = next((v for v in versions if v.get('is_preferred') == 1), None)
    if not primary:
        primary = versions[0]
    return primary


def _row_for_path(model, path):
    """Look up the file_versions row for a given path. Returns dict or None."""
    conn = model.db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT path, video_id, cache_id, is_preferred FROM file_versions WHERE path = ?',
        (path,)
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _set_cache_id(model, path, cache_id):
    """Persist a new cache_id for a specific file_versions row (by path)."""
    conn = model.db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE file_versions SET cache_id = ? WHERE path = ?',
        (cache_id, path)
    )
    conn.commit()


def _new_unique_cache_id(path):
    """Generate a brand-new, collision-resistant cache_id for a file."""
    unique_seed = f"{os.path.basename(path)}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    return hashlib.md5(unique_seed.encode('utf-8')).hexdigest()


# --- BACKGROUND WORKER ---

def _generate_thumbnail_background(jobs):
    """
    Run thumbnail generation in a background thread.
    jobs: list of (thumbnail_source_path, force, video_id, cache_id)
    Database updates (if any) are done by the caller before spawning this
    thread - this worker only runs FFmpeg.
    """
    for (source_path, force, video_id, cache_id) in jobs:
        try:
            print(f"[thumb-bg] Generating: {os.path.basename(source_path)} (force={force}, cache_id={cache_id})")
            result = MediaHandler.generate_thumbnails(
                source_path, force=force, video_id=video_id, cache_id=cache_id
            )

            if result and len(result) > 0:
                print(f"[thumb-bg] Done: {os.path.basename(source_path)} - {len(result)} thumbnails")
            else:
                print(f"[thumb-bg] No thumbnails generated for: {os.path.basename(source_path)}")
        except Exception as e:
            print(f"[thumb-bg] Error for {source_path}: {e}")


# --- ROUTES ---

@router.post("/thumbnails/generate")
def generate_thumbnails(req: GenThumbsRequest, model = Depends(get_model)):
    """
    Queue thumbnail generation for specified video files.
    Returns immediately - generation runs in a background thread.

    Generates into each file's CURRENT cache key (cache_id if set on the
    preferred row, else MD5(video_id), else MD5(filename)). This is used
    for "Generate Thumbnails" on videos that don't have thumbnails yet —
    it does NOT assign a new cache_id.
    """
    jobs = []

    for path in req.paths:
        video_id = None
        cache_id = None
        thumbnail_source_path = path

        if USE_SQLITE:
            row = _row_for_path(model, path)

            if row:
                video_id = row['video_id']
                cache_id = row['cache_id']

                # Get all versions for this video
                versions = model.db.get_file_versions(video_id)

                if len(versions) > 1:
                    # Multi-file video - find preferred/primary version
                    primary = next((v for v in versions if v.get('is_preferred') == 1), None)
                    if not primary:
                        primary = versions[0]
                    thumbnail_source_path = primary['path']
                    cache_id = primary['cache_id']
                    print(f"[thumb] Multi-file video {video_id}, using: {thumbnail_source_path}")

        if os.path.exists(thumbnail_source_path):
            jobs.append((thumbnail_source_path, req.force, video_id, cache_id))
        else:
            print(f"[thumb] File not found: {thumbnail_source_path}")

    if jobs:
        # Spawn a single background thread for all jobs in this request
        t = threading.Thread(
            target=_generate_thumbnail_background,
            args=(jobs,),
            daemon=True,
            name=f"thumb-gen-{int(time.time())}"
        )
        t.start()
        print(f"[thumb] Queued {len(jobs)} job(s) in background thread")

    # Return immediately - don't wait for FFmpeg
    return {"generated": len(jobs), "queued": True}


@router.post("/thumbnails/check")
async def check_thumbnails(request: Request, model = Depends(get_model)):
    """
    Check which videos currently have thumbnails.

    Uses the CURRENT cache key for the preferred version of each video
    (cache_id if set, else MD5(video_id), else MD5(filename)) — i.e.
    whatever the UI is actually displaying right now.
    """
    data = await request.json()
    paths = data.get('paths', [])

    results = {}

    for path in paths:
        video_id = None
        cache_id = None
        check_path = path

        if USE_SQLITE:
            row = _row_for_path(model, path)
            if row:
                video_id = row['video_id']
                cache_id = row['cache_id']

                # For merged videos, thumbnail status reflects the PRIMARY
                # version's cache, regardless of which sibling was clicked.
                versions = model.db.get_file_versions(video_id)
                if len(versions) > 1:
                    primary = next((v for v in versions if v.get('is_preferred') == 1), None)
                    if not primary:
                        primary = versions[0]
                    cache_id = primary['cache_id']
                    check_path = primary['path']

        thumbs = MediaHandler.get_thumbnails(check_path, video_id, cache_id)
        results[path] = len(thumbs) > 0

    return results


@router.post("/cache/regenerate")
async def regenerate_cache(request: Request, model = Depends(get_model)):
    """
    "Force New" - assign a brand-new, independent cache_id and regenerate
    thumbnails into it.

    For each requested path:
    - Resolve the video's preferred/primary file_versions row (via
      video_id). This is the ONLY row that gets a new cache_id and new
      thumbnails - sibling versions in a merge group are left untouched,
      so unmerging later does not affect their cache.
    - For a video with a single version, that row IS the primary, so the
      same logic applies.
    - The old cache folder for the previous key (cache_id / MD5(video_id) /
      MD5(filename)) is removed so stale thumbnails don't linger.
    """
    data = await request.json()
    paths = data.get('paths', [])

    # Support single path for backwards compatibility
    if 'path' in data and data.get('path'):
        paths = [data['path']]

    if not paths:
        raise HTTPException(400, "No paths provided")

    results = []
    # Avoid redundant work if multiple selected paths belong to the same
    # merge group (same primary row would otherwise be processed repeatedly).
    processed_video_ids = {}

    for path in paths:
        if not USE_SQLITE:
            results.append({'path': path, 'success': False, 'error': 'Cache regeneration requires SQLite backend'})
            continue

        row = _row_for_path(model, path)
        if not row:
            results.append({'path': path, 'success': False, 'error': 'File not found in DB'})
            continue

        video_id = row['video_id']

        # Resolve to the preferred/primary row for this video_id.
        if video_id and video_id in processed_video_ids:
            # Already handled this video_id in this request
            prior = processed_video_ids[video_id]
            results.append({
                'path': path,
                'success': prior['success'],
                'cache_id': prior['cache_id'],
                'thumbnails': prior['thumbnails'],
                'message': f"Shares video with already-processed {os.path.basename(prior['primary_path'])}"
            })
            continue

        primary = _get_preferred_row(model, video_id) if video_id else row
        if not primary:
            primary = row

        primary_path = primary['path']
        old_cache_id = primary.get('cache_id')

        if not os.path.exists(primary_path):
            results.append({'path': path, 'success': False, 'error': f'Primary file not on disk: {primary_path}'})
            if video_id:
                processed_video_ids[video_id] = {
                    'success': False, 'cache_id': old_cache_id,
                    'thumbnails': 0, 'primary_path': primary_path
                }
            continue

        # Determine the OLD cache folder so we can clean it up afterward.
        old_cache_key = MediaHandler._get_cache_key(primary_path, video_id, old_cache_id)
        old_cache_dir = os.path.join(CACHE_DIR, old_cache_key)

        # Generate a brand-new, independent cache_id for the primary row.
        new_cache_id = _new_unique_cache_id(primary_path)
        new_cache_dir = os.path.join(CACHE_DIR, new_cache_id)

        # Persist the new cache_id on the primary row ONLY.
        _set_cache_id(model, primary_path, new_cache_id)

        # Create the new cache folder and generate thumbnails into it.
        os.makedirs(new_cache_dir, exist_ok=True)
        MediaHandler.generate_thumbnails(primary_path, force=True, video_id=video_id, cache_id=new_cache_id)

        # Clean up the old cache folder, but only if it's different from the
        # new one and isn't still in use (it shouldn't be, since we just
        # repointed the primary row away from it).
        if old_cache_dir != new_cache_dir and os.path.exists(old_cache_dir):
            shutil.rmtree(old_cache_dir, ignore_errors=True)

        thumb_count = len([f for f in os.listdir(new_cache_dir) if f.endswith('.webp')]) if os.path.exists(new_cache_dir) else 0

        success = thumb_count > 0
        results.append({
            'path': path,
            'success': success,
            'old_cache_id': old_cache_id,
            'cache_id': new_cache_id,
            'thumbnails': thumb_count,
            'message': f'{thumb_count} thumbnails generated' if success else 'No thumbnails generated'
        })

        if video_id:
            processed_video_ids[video_id] = {
                'success': success, 'cache_id': new_cache_id,
                'thumbnails': thumb_count, 'primary_path': primary_path
            }

    return {
        'results': results,
        'message': f'Regenerated thumbnails for {sum(1 for r in results if r["success"])} of {len(results)} file(s)'
    }