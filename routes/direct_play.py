# This file is: ./routes/direct_play.py

"""
Optimized direct video file serving with proper range request support for seeking.
Replaces the generic StaticFiles mount for better seeking performance.

URL scheme:
  /files/media/subdir/file.mp4  -> serves /media/subdir/file.mp4
  /files/media0/subdir/file.mp4 -> serves /media0/subdir/file.mp4

This keeps the mount-point name as the first path segment under /files/ so
both volumes are unambiguously addressable and the security check is simple.
"""

import os
import subprocess
import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pathlib import Path

router = APIRouter(prefix="/files", tags=["direct_play"])

# Will be set by web_server.py
_model = None

def set_model(model):
    global _model
    _model = model

# All allowed media roots. Each entry is the on-disk path of a mounted volume.
# The URL path under /files/ mirrors the on-disk path (e.g. /files/media/... -> /media/...).
from config import MEDIA_ROOTS        # LUSTR_MEDIA_ROOTS (default /media, /media0)

CHUNK_SIZE = 1024 * 1024  # 1MB chunks for streaming


def get_range_header(range_header: str, file_size: int):
    """Parse Range header and return (start, end) bytes."""
    try:
        byte_range = range_header.replace("bytes=", "").strip()
        if "-" not in byte_range:
            return (0, file_size - 1)
        start, end = byte_range.split("-")
        start = int(start) if start else 0
        end = int(end) if end else file_size - 1
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        return (start, end)
    except:
        return (0, file_size - 1)


def ranged_file_iterator(file_path: str, start: int, end: int, chunk_size: int = CHUNK_SIZE):
    """
    Generator that yields chunks of the file from start to end byte positions.
    Optimized for seeking - only reads the requested range.
    """
    with open(file_path, 'rb') as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk_bytes = min(chunk_size, remaining)
            data = f.read(chunk_bytes)
            if not data:
                break
            remaining -= len(data)
            yield data


def get_video_codec(file_path: str) -> str:
    """
    Get the video codec for a file. Checks the model's DB cache first
    (since all scanned files already have codec stored), falls back to ffprobe.
    Returns lowercase codec name e.g. 'hevc', 'h264', or '' on failure.
    """
    if _model:
        try:
            file_data = _model.db.get_file_by_path(file_path)
            if file_data and file_data.get('codec'):
                return file_data['codec'].lower()
        except Exception:
            pass

    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 'v:0',
            file_path
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5)
        data = json.loads(output)
        streams = data.get('streams', [])
        if streams:
            return streams[0].get('codec_name', '').lower()
    except Exception:
        pass

    return ''


def get_content_type(file_path: str) -> str:
    """
    Return the correct Content-Type for the file.

    For HEVC/H.265 in MP4 containers, Firefox requires the codec parameter
    in the Content-Type header to identify and play the stream.
    """
    ext = os.path.splitext(file_path)[1].lower()

    plain_types = {
        '.mkv':  'video/x-matroska',
        '.avi':  'video/x-msvideo',
        '.mov':  'video/quicktime',
        '.wmv':  'video/x-ms-wmv',
        '.flv':  'video/x-flv',
        '.webm': 'video/webm',
        '.m4v':  'video/x-m4v',
    }
    if ext in plain_types:
        return plain_types[ext]

    if ext == '.mp4':
        codec = get_video_codec(file_path)
        if 'hevc' in codec or 'h265' in codec:
            return 'video/mp4; codecs="hev1.1.6.L93.B0"'
        elif 'h264' in codec or 'avc' in codec:
            return 'video/mp4; codecs="avc1.42E01E"'
        else:
            return 'video/mp4'

    return 'application/octet-stream'


@router.get("/{file_path:path}")
async def serve_video(file_path: str, request: Request):
    """
    Serve video files with optimized range request support.

    URL path format: /files/<mount>/<subpath>
      e.g. /files/media/Studio/video.mp4   -> /media/Studio/video.mp4
           /files/media0/Studio/video.mp4   -> /media0/Studio/video.mp4

    Security: only files under a declared MEDIA_ROOTS entry are served.
    """
    # Reconstruct the on-disk path: file_path is e.g. "media/Studio/video.mp4"
    # so the on-disk path is "/media/Studio/video.mp4"
    full_path = "/" + file_path
    full_path = os.path.normpath(full_path)

    # Security: must start with one of the allowed roots
    allowed = any(
        full_path == root or full_path.startswith(root + os.sep)
        for root in MEDIA_ROOTS
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    file_size = os.path.getsize(full_path)
    content_type = get_content_type(full_path)
    range_header = request.headers.get('range')

    if range_header:
        start, end = get_range_header(range_header, file_size)
        content_length = end - start + 1
        headers = {
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Content-Length': str(content_length),
            'Accept-Ranges': 'bytes',
            'Content-Type': content_type,
            'Cache-Control': 'public, max-age=3600',
        }
        return StreamingResponse(
            ranged_file_iterator(full_path, start, end),
            status_code=206,
            headers=headers,
            media_type=content_type
        )
    else:
        headers = {
            'Content-Length': str(file_size),
            'Accept-Ranges': 'bytes',
            'Content-Type': content_type,
            'Cache-Control': 'public, max-age=3600',
        }
        return StreamingResponse(
            ranged_file_iterator(full_path, 0, file_size - 1),
            headers=headers,
            media_type=content_type
        )