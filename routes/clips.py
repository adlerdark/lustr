# This file is: ./routes/clips.py
"""
Clips (clips.py): make a short WebM / MP4 / GIF from any video, list, rename, delete.
The clips table lives here; files are in data/clips (served under /clips, login required).
"""
import json
import os
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

import clips
import transcoder

router = APIRouter(prefix='/api/clips', tags=['clips'])
_model = None
COLUMNS = ('id', 'video_id', 'source_path', 'start', 'end', 'format', 'shape', 'width', 'height', 'fps', 'audio', 'quality',
           'title', 'file', 'size_bytes', 'encode_seconds', 'options', 'status', 'error', 'created')


def set_model(model):
    global _model
    _model = model


def get_model():
    return _model


def _conn():
    return _model.db.get_connection()


def _err(e):
    return {'success': False, 'error': str(e)}


def ensure_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS clips (
        id TEXT PRIMARY KEY, video_id TEXT, source_path TEXT, start REAL, "end" REAL, format TEXT, shape TEXT,
        width INTEGER, height INTEGER, fps INTEGER, audio INTEGER, quality TEXT, title TEXT, file TEXT,
        size_bytes INTEGER, encode_seconds REAL, options TEXT, status TEXT, error TEXT,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_clips_video ON clips(video_id)')


def _row(r):
    d = dict(zip(COLUMNS, r))
    d['audio'] = bool(d['audio'])
    d['duration'] = round((d['end'] or 0) - (d['start'] or 0), 2)
    v = int(os.path.getmtime(os.path.join(clips.CLIPS_DIR, d['file']))) if d['file'] and os.path.exists(
        os.path.join(clips.CLIPS_DIR, d['file'])) else 0
    d['src'] = f"/clips/{d['file']}?v={v}" if d['file'] else None
    d['poster'] = f"/clips/{d['id']}.jpg?v={v}" if v else None
    return d


def _select(where='', params=()):
    cols = ', '.join(f'c."{c}"' for c in COLUMNS)
    return f'SELECT {cols} FROM clips c {where}', params


def get_clip(conn, clip_id):
    sql, p = _select('WHERE c.id = ?', (clip_id,))
    r = conn.execute(sql, p).fetchone()
    return _row(r) if r else None


def _load(clip_id):
    c = get_clip(_conn(), clip_id)
    if not c:
        raise ValueError('Clip not found')
    return c['source_path'], json.loads(c['options'] or '{}')


def _save(clip_id, **fields):
    conn = _conn()
    sets = ', '.join(f'"{k}" = ?' for k in fields)
    conn.execute(f'UPDATE clips SET {sets} WHERE id = ?', (*fields.values(), clip_id))
    conn.commit()


def start_background():
    conn = _conn()
    ensure_table(conn)
    conn.commit()
    clips.worker.load, clips.worker.save = _load, _save
    for (cid,) in conn.execute("SELECT id FROM clips WHERE status IN ('queued', 'working') ORDER BY created").fetchall():
        clips.worker.enqueue(cid)                  # unfinished after a restart


class ClipBody(BaseModel):
    path: str
    start: float
    end: float
    format: str = 'webm'
    shape: str = 'original'
    size: int = 0
    fps: int = 0
    cx: float = 0.5
    cy: float = 0.5
    audio: bool = True
    quality: str = 'standard'
    title: Optional[str] = None


@router.post('')
def make(body: ClipBody, model=Depends(get_model)):
    try:
        conn = _conn()
        ensure_table(conn)
        row = conn.execute('SELECT video_id FROM file_versions WHERE path = ? AND deleted_at IS NULL', (body.path,)).fetchone()
        if not row:
            raise FileNotFoundError('Not in your library')
        info = transcoder.probe(body.path)
        o = clips.clean_options(body.dict(), info)
        cid = uuid.uuid4().hex[:12]
        conn.execute('INSERT INTO clips (id, video_id, source_path, start, "end", format, shape, fps, audio, quality, title, '
                     'options, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (cid, row[0], body.path, o['start'], o['end'], o['format'], o['shape'], o['fps'], int(o['audio']),
                      o['quality'], o['title'], json.dumps(o), 'queued'))
        conn.commit()
        clips.worker.enqueue(cid)
        return {'success': True, 'clip': get_clip(conn, cid)}
    except Exception as e:
        return _err(e)


@router.get('')
def list_clips(video_id: Optional[str] = None, path: Optional[str] = None, cast: Optional[str] = None, library: Optional[str] = None,
               format: Optional[str] = None, q: Optional[str] = None, sort: str = 'newest',
               page: int = 1, per_page: int = 60, model=Depends(get_model)):
    try:
        conn = _conn()
        ensure_table(conn)
        where, params = [], []
        if video_id:
            where.append('c.video_id = ?')
            params.append(video_id)
        if path:                                     # a file's video (File Info)
            where.append('c.video_id = (SELECT video_id FROM file_versions WHERE path = ?)')
            params.append(path)
        if cast:
            where.append('c.video_id IN (SELECT j.video_id FROM video_cast_junction j JOIN video_cast vc ON vc.id = j.cast_id '
                         'WHERE vc.value = ?)')
            params.append(cast)
        if library:
            libs = (model.get_libraries_in_group(library) if library in (getattr(model, 'library_groups', None) or {})
                    else [library])
            where.append(f'c.video_id IN (SELECT video_id FROM videos WHERE library IN ({",".join("?" * len(libs))}))')
            params += list(libs)
        if format in clips.FORMATS:
            where.append('c.format = ?')
            params.append(format)
        if q:
            where.append('(c.title LIKE ? OR c.source_path LIKE ?)')
            params += [f'%{q}%', f'%{q}%']
        w = ('WHERE ' + ' AND '.join(where)) if where else ''
        total = conn.execute(f'SELECT COUNT(*) FROM clips c {w}', params).fetchone()[0]
        order = 'c.created ASC' if sort == 'oldest' else 'c.created DESC'
        sql, p = _select(f'{w} ORDER BY {order} LIMIT ? OFFSET ?', (*params, per_page, (max(1, page) - 1) * per_page))
        rows = [_row(r) for r in conn.execute(sql, p)]
        cast_of = {}
        ids = list({r['video_id'] for r in rows})
        if ids:
            for vid, name in conn.execute('SELECT j.video_id, vc.value FROM video_cast_junction j JOIN video_cast vc '
                                          f'ON vc.id = j.cast_id WHERE j.video_id IN ({",".join("?" * len(ids))})', ids):
                cast_of.setdefault(vid, []).append(name)
            titles = dict(conn.execute(f'SELECT video_id, COALESCE(title, filename_base) FROM videos '
                                       f'WHERE video_id IN ({",".join("?" * len(ids))})', ids).fetchall())
        else:
            titles = {}
        for r in rows:
            r['cast'] = sorted(cast_of.get(r['video_id'], []), key=str.lower)
            r['video_title'] = titles.get(r['video_id'])
        return {'success': True, 'clips': rows, 'total': total, 'status': clips.worker.status()}
    except Exception as e:
        return _err(e)


@router.get('/status')
def status(model=Depends(get_model)):
    return {'success': True, 'status': clips.worker.status()}


@router.get('/{clip_id}')
def one(clip_id: str, model=Depends(get_model)):
    c = get_clip(_conn(), clip_id)
    return {'success': True, 'clip': c} if c else _err('Clip not found')


class RenameBody(BaseModel):
    title: str


@router.post('/{clip_id}')
def rename(clip_id: str, body: RenameBody, model=Depends(get_model)):
    try:
        _save(clip_id, title=(body.title or '').strip()[:200] or 'Clip')
        return {'success': True, 'clip': get_clip(_conn(), clip_id)}
    except Exception as e:
        return _err(e)


@router.delete('/{clip_id}')
def delete(clip_id: str, model=Depends(get_model)):
    try:
        conn = _conn()
        clips.delete_files(clip_id)
        n = conn.execute('DELETE FROM clips WHERE id = ?', (clip_id,)).rowcount
        conn.commit()
        return {'success': True, 'deleted': n}
    except Exception as e:
        return _err(e)


@router.get('/{clip_id}/download')
def download(clip_id: str, model=Depends(get_model)):
    c = get_clip(_conn(), clip_id)
    if not c or not c['file']:
        return _err('Clip not ready')
    cast = [r[0] for r in _conn().execute('SELECT vc.value FROM video_cast_junction j JOIN video_cast vc ON vc.id = j.cast_id '
                                           'WHERE j.video_id = ? ORDER BY vc.value', (c['video_id'],))][:3]
    t = int(c['start'] or 0)
    name = (', '.join(cast) + ' - ' if cast else '') + f"{c['title']} @ {t // 60:02d}-{t % 60:02d}"
    name = re.sub(r'[\\/:*?"<>|]+', '', name).strip() or 'clip'
    return FileResponse(os.path.join(clips.CLIPS_DIR, c['file']), filename=f"{name}.{c['format']}")
