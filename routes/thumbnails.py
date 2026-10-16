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

import json
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
from media_handler import MediaHandler, COVER, COVER_JSON, FRAMES_JSON
from config import USE_SQLITE, CACHE_DIR, THUMBNAIL_FRAMES

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


def _cache_key_in_use(model, key, except_path):
    """True if another file's thumbnails / captions live in this cache folder."""
    rows = model.db.get_connection().execute(
        'SELECT path, video_id, cache_id FROM file_versions WHERE path != ? AND deleted_at IS NULL AND '
        '(cache_id = ? OR cache_id IS NULL)', (except_path, key)).fetchall()
    return any(MediaHandler._get_cache_key(r[0], r[1], r[2]) == key for r in rows)


def _move_captions(old_dir, new_dir):
    """Captions belong to the file, not to its thumbnails: keep them when the folder changes."""
    try:
        import captions
        idx = os.path.join(old_dir, captions.INDEX)
        if not os.path.exists(idx):
            return
        os.makedirs(new_dir, exist_ok=True)
        with open(idx, encoding='utf-8') as f:
            items = json.load(f)
        for i in items:
            src = os.path.join(old_dir, i.get('file', ''))
            if i.get('file') and os.path.exists(src):
                shutil.move(src, os.path.join(new_dir, i['file']))
        shutil.move(idx, os.path.join(new_dir, captions.INDEX))
    except Exception as e:
        print(f"[thumb] Could not move captions from {old_dir}: {e}")


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


def _shown_cache_key(model, path):
    """The cache key whose thumbnails the library shows for this file: the preferred version's,
    for a video with several versions (as /thumbnails/check does)."""
    return _shown_version(model, path)[0]


def _shown_version(model, path):
    """(cache key, path of the file those thumbnails come from) - see _shown_cache_key."""
    video_id, cache_id, check_path = None, None, path
    if USE_SQLITE:
        row = _row_for_path(model, path)
        if row:
            video_id, cache_id = row['video_id'], row['cache_id']
            versions = model.db.get_file_versions(video_id)
            if len(versions) > 1:
                primary = next((v for v in versions if v.get('is_preferred') == 1), None) or versions[0]
                cache_id, check_path = primary['cache_id'], primary['path']
    return MediaHandler._get_cache_key(check_path, video_id, cache_id), check_path


def _cover_index(folder):
    try:
        with open(os.path.join(folder, COVER_JSON), encoding='utf-8') as f:
            return int(json.load(f).get('index', 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


@router.get("/thumbnails/frames")
def thumbnail_frames(path: str, model = Depends(get_model)):
    """Every thumbnail frame of a video, and which one is the main thumbnail (cover)."""
    key = _shown_cache_key(model, path)
    folder = os.path.join(CACHE_DIR, key)
    frames = []
    for f in MediaHandler.get_thumbnails(path, None, key):
        frames.append(f"/cache/{key}/{os.path.basename(f)}?v={int(os.path.getmtime(f))}")
    cover = _cover_index(folder) if os.path.exists(os.path.join(folder, COVER)) else 0
    return {'cache_id': key, 'frames': frames, 'cover': cover}


class CoverRequest(BaseModel):
    path: str
    index: int


@router.post("/thumbnails/cover")
def set_thumbnail_cover(req: CoverRequest, model = Depends(get_model)):
    """Make frame `index` the main thumbnail: a copy of it as cover.webp (frame 0, the default,
    removes the cover). The frames keep their order, so the hover preview is unchanged."""
    key = _shown_cache_key(model, req.path)
    folder = os.path.join(CACHE_DIR, key)
    frame = os.path.join(folder, f"thumb_{req.index}.webp")
    if req.index < 0 or not os.path.exists(frame):
        raise HTTPException(404, "No such thumbnail frame")
    _set_cover(folder, req.index)
    from helpers import get_thumbnail_path
    return {'success': True, 'cover': req.index, 'thumb': get_thumbnail_path(key)}


def _set_cover(folder, index):
    """cover.webp = a copy of frame `index`; frame 0 (the default) removes the cover."""
    cover, meta = os.path.join(folder, COVER), os.path.join(folder, COVER_JSON)
    if index == 0:
        for f in (cover, meta):
            if os.path.exists(f):
                os.remove(f)
    else:
        tmp = cover + '.tmp'
        shutil.copyfile(os.path.join(folder, f"thumb_{index}.webp"), tmp)
        os.replace(tmp, cover)
        with open(meta, 'w', encoding='utf-8') as f:
            json.dump({'index': index}, f)


class ThumbAtRequest(BaseModel):
    path: str
    seconds: float


@router.post("/thumbnails/at")
def set_thumbnail_at(req: ThumbAtRequest, model = Depends(get_model)):
    """Make the frame at `seconds` the main thumbnail (from the player). The existing frame nearest
    that moment moves onto it and the others are re-spread around it, so the hover preview
    still runs through the whole video."""
    key, shown_path = _shown_version(model, req.path)
    if os.path.normpath(shown_path) != os.path.normpath(req.path):
        raise HTTPException(409, "This is not the version the library shows - make it the preferred "
                                 "version first, or set the thumbnail while playing that one")
    if not os.path.exists(req.path):
        raise HTTPException(404, "The file is not on disk")
    duration = MediaHandler.get_extended_metadata(req.path).get('duration', 0) or 0
    if not duration:
        raise HTTPException(422, "Could not read the length of this video")
    folder = os.path.join(CACHE_DIR, key)
    os.makedirs(folder, exist_ok=True)
    try:
        with open(os.path.join(folder, FRAMES_JSON), encoding='utf-8') as f:
            current = json.load(f)
    except (OSError, ValueError):
        current = None
    n = THUMBNAIL_FRAMES
    times, k = MediaHandler.anchor_times(duration, n, req.seconds, current)
    start = (current or {}).get('start') or 0
    written = MediaHandler._write_frames(req.path, folder, times, None, start if start < times[k] else 0)
    if not os.path.exists(os.path.join(folder, f"thumb_{k}.webp")) or len(written) < n:
        raise HTTPException(500, "ffmpeg could not make the frames")
    for f in os.listdir(folder):                 # frames left over from a larger frame count
        if f.startswith('thumb_') and f.endswith('.webp'):
            try:
                if int(f[6:-5]) >= n:
                    os.remove(os.path.join(folder, f))
            except ValueError:
                pass
    _set_cover(folder, k)
    from helpers import get_thumbnail_path
    return {'success': True, 'index': k, 'frames': n, 'seconds': times[k], 'thumb': get_thumbnail_path(key)}


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

    # optional: skip this many seconds at the start (title cards); the frames spread over the rest
    try:
        offset = max(0.0, float(data.get('offset') or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "offset must be a number of seconds")

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
        MediaHandler.generate_thumbnails(primary_path, force=True, video_id=video_id, cache_id=new_cache_id,
                                         start=offset)
        offset_used = offset
        if offset:
            duration = MediaHandler.get_extended_metadata(primary_path).get('duration', 0) or 0
            offset_used = MediaHandler.thumb_times(duration, 1, offset)[1] if duration else 0

        # Clean up the old cache folder - unless another file still uses it (same filename, no
        # cache_id of its own). This file's captions move to the new folder first.
        if old_cache_dir != new_cache_dir and os.path.exists(old_cache_dir):
            if _cache_key_in_use(model, old_cache_key, primary_path):
                print(f"[thumb] Kept {old_cache_key}: another file still uses it")
            else:
                _move_captions(old_cache_dir, new_cache_dir)
                shutil.rmtree(old_cache_dir, ignore_errors=True)

        thumb_count = len(MediaHandler.get_thumbnails(primary_path, video_id, new_cache_id))

        success = thumb_count > 0
        results.append({
            'path': path,
            'success': success,
            'old_cache_id': old_cache_id,
            'cache_id': new_cache_id,
            'thumbnails': thumb_count,
            'offset_used': round(offset_used, 1),
            'capped': bool(offset) and offset_used < offset,
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