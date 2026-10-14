# This file is: ./clips.py
"""
Clips: short GIFs and RedGIFs-style videos cut from any video.

Formats: WebM (VP9, optional Opus sound), MP4 (H.264, optional AAC sound) and GIF
(one ffmpeg pass with its own palette: palettegen + paletteuse with dithering).
Shapes: the original frame, square (1:1) or vertical (9:16), with a chosen crop position.

Files: data/clips/<id>.<ext> plus <id>.jpg (a poster for the Clips page).
One background worker at low priority (nice 15). The clips table lives in routes/clips.py;
the worker reads / writes it through the callbacks it is given. No FastAPI imports.
"""
import os
import re
import subprocess
import threading
import time
from collections import deque

import transcoder
from config import DATA_DIR

CLIPS_DIR = os.path.join(DATA_DIR, 'clips')
FORMATS = ('webm', 'mp4', 'gif')
SHAPES = {'original': None, 'square': 1.0, 'vertical': 9 / 16}
MAX_SECONDS = 60
NICE = 15


def _even(n):
    n = int(round(n))
    return max(2, n - n % 2)


def crop_box(width, height, shape, cx=0.5, cy=0.5):
    """The part of the frame to keep -> (w, h, x, y), or None for the whole frame.
    cx / cy (0-1): where the crop sits horizontally / vertically."""
    aspect = SHAPES.get(shape)
    if not aspect or not width or not height:
        return None
    cx, cy = min(1.0, max(0.0, float(cx))), min(1.0, max(0.0, float(cy)))
    if width / height > aspect:                     # too wide: cut the sides
        w, h = _even(height * aspect), _even(height)
    else:                                           # too tall: cut top / bottom
        w, h = _even(width), _even(width / aspect)
    w, h = min(w, width - width % 2), min(h, height - height % 2)
    return w, h, int((width - w) * cx), int((height - h) * cy)


def output_size(width, height, shape, size):
    """-> (w, h) after crop + scale. size: the short side (0 = keep); never upscaled."""
    box = crop_box(width, height, shape)
    w, h = (box[0], box[1]) if box else (width, height)
    if not size or size >= min(w, h):
        return _even(w), _even(h)
    if w >= h:
        return _even(w * size / h), _even(size)
    return _even(size), _even(h * size / w)


def clean_options(o, info):
    """Check and fill a clip request. -> dict"""
    fmt = o.get('format') if o.get('format') in FORMATS else 'webm'
    start = max(0.0, float(o.get('start') or 0))
    end = float(o.get('end') or 0)
    dur = info.get('duration') or 0
    if dur:
        end = min(end, dur)
    if end <= start:
        raise ValueError('The clip ends before it starts')
    if end - start > MAX_SECONDS:
        raise ValueError(f'Clips can be at most {MAX_SECONDS} seconds')
    shape = o.get('shape') if o.get('shape') in SHAPES else 'original'
    size = int(o.get('size') or (480 if fmt == 'gif' else 720))
    fps = int(o.get('fps') or (15 if fmt == 'gif' else 0))
    return {'format': fmt, 'start': round(start, 3), 'end': round(end, 3), 'shape': shape, 'size': max(0, size),
            'fps': max(0, min(60, fps)), 'cx': float(o.get('cx', 0.5)), 'cy': float(o.get('cy', 0.5)),
            'audio': bool(o.get('audio')) and fmt != 'gif' and bool(info.get('audio')),
            'quality': 'high' if o.get('quality') == 'high' else 'standard',
            'title': (str(o.get('title') or '').strip() or 'Clip')[:200]}


def frame_size(info):
    """(width, height) as shown: phone videos stored sideways (rotation 90 / 270) are turned by ffmpeg."""
    w, h = info.get('width') or 0, info.get('height') or 0
    return (h, w) if abs(int(info.get('rotation') or 0)) % 180 == 90 else (w, h)


def video_filters(o, info):
    W, H = frame_size(info)
    f = []
    if o['fps']:
        f.append(f"fps={o['fps']}")
    box = crop_box(W, H, o['shape'], o['cx'], o['cy'])
    if box:
        f.append('crop={}:{}:{}:{}'.format(*box))
    w, h = output_size(W, H, o['shape'], o['size'])
    f.append(f'scale={w}:{h}:flags=lanczos')
    return ','.join(f)


def build_cmd(src, out, o, info):
    """The ffmpeg command for one clip."""
    dur = o['end'] - o['start']
    cmd = ['nice', '-n', str(NICE), 'ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-y',
           # -t before -i: the input itself stops there (the GIF palette needs the end of its input)
           '-ss', f"{o['start']:.3f}", '-t', f'{dur:.3f}', '-i', src, '-map_metadata', '-1',
           '-progress', 'pipe:1', '-nostats']
    vf = video_filters(o, info)
    high = o['quality'] == 'high'
    if o['format'] == 'gif':
        cmd += ['-filter_complex', f'[0:v]{vf},split[a][b];[a]palettegen=stats_mode=diff[p];'
                                   f'[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle',
                '-an', '-loop', '0', out]
        return cmd
    cmd += ['-map', '0:v:0', '-vf', vf]
    if o['format'] == 'webm':
        cmd += ['-c:v', 'libvpx-vp9', '-crf', '24' if high else '30', '-b:v', '0', '-row-mt', '1',
                '-deadline', 'good', '-cpu-used', '2', '-pix_fmt', 'yuv420p']
        audio = ['-c:a', 'libopus', '-b:a', '128k']
    else:
        cmd += ['-c:v', 'libx264', '-crf', '17' if high else '20', '-preset', 'slow', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart']
        audio = ['-c:a', 'aac', '-b:a', '160k']
    if o['audio']:
        cmd += ['-map', '0:a:0?'] + audio
    else:
        cmd += ['-an']
    return cmd + [out]


def file_name(clip_id, fmt):
    return f'{clip_id}.{fmt}'


def encode(clip_id, src, o, progress=None):
    """Make the clip and its poster. -> (file name, size in bytes, width, height)"""
    os.makedirs(CLIPS_DIR, exist_ok=True)
    info = transcoder.probe(src)
    name = file_name(clip_id, o['format'])
    out = os.path.join(CLIPS_DIR, name)
    tmp = os.path.join(CLIPS_DIR, f'.{clip_id}.tmp.{o["format"]}')
    total = max(0.1, o['end'] - o['start'])
    p = subprocess.Popen(build_cmd(src, tmp, o, info), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=transcoder.ENV)
    for line in p.stdout:
        m = re.match(r'out_time_(?:us|ms)=(\d+)', line.strip())
        if m and progress:
            progress(min(1.0, int(m.group(1)) / 1e6 / total))
    err = p.stderr.read()
    if p.wait() != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError('ffmpeg: ' + (err.strip().splitlines() or ['failed'])[-1][:300])
    os.replace(tmp, out)
    subprocess.run(['nice', '-n', str(NICE), 'ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-y',
                    '-ss', f'{min(total / 2, 1.0):.2f}', '-i', out, '-frames:v', '1', '-vf', 'scale=-2:360',
                    os.path.join(CLIPS_DIR, f'{clip_id}.jpg')], capture_output=True, timeout=120, env=transcoder.ENV)
    w, h = output_size(*frame_size(info), o['shape'], o['size'])
    return name, os.path.getsize(out), w, h


def delete_files(clip_id):
    for n in os.listdir(CLIPS_DIR) if os.path.isdir(CLIPS_DIR) else []:
        if n.startswith(clip_id + '.') or n.startswith('.' + clip_id + '.'):
            try:
                os.remove(os.path.join(CLIPS_DIR, n))
            except OSError:
                pass


class Worker:
    """One background thread making clips in order. load(id) -> (src, options); save(id, **fields)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = deque()
        self.thread = None
        self.load = self.save = None
        self.state = {'current': None, 'progress': 0.0, 'done': 0, 'last_error': ''}

    def enqueue(self, clip_id):
        with self.lock:
            if clip_id not in self.queue:
                self.queue.append(clip_id)
        if not self.thread:
            self.thread = threading.Thread(target=self._loop, name='clips', daemon=True)
            self.thread.start()
        self.wake.set()

    def status(self):
        with self.lock:
            return dict(self.state, queued=list(self.queue))

    def _loop(self):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)
        except Exception:
            pass
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        self.state.update(current=None, progress=0.0)
                        break
                    cid = self.queue.popleft()
                    self.state.update(current=cid, progress=0.0)
                t0 = time.time()
                try:
                    src, o = self.load(cid)
                    self.save(cid, status='working')

                    def prog(f):
                        self.state['progress'] = f
                    name, size, w, h = encode(cid, src, o, prog)
                    self.save(cid, status='done', file=name, size_bytes=size, width=w, height=h,
                              encode_seconds=round(time.time() - t0, 1), error=None)
                    self.state['done'] += 1
                except Exception as e:
                    self.state['last_error'] = f'{cid}: {e}'
                    try:
                        self.save(cid, status='error', error=str(e)[:500])
                    except Exception:
                        pass


worker = Worker()
