# This file is: ./sprites.py
"""
Seek-bar previews for the new player: 81 frames taken evenly
through the video, 160 px on the long side, pasted into one 9x9 JPEG (sprite.jpg), plus a
WebVTT file that maps each time range to its tile (thumbs.vtt: 'sprite.jpg#xywh=x,y,w,h').
Both live in the video's thumbnail cache folder: data/cache/{cache_id}/.

A single background worker makes them at low priority: a video opened in the new player
goes to the front of the queue; the bulk job (Settings -> Player) queues a library.

No FastAPI imports.
"""
import io
import os
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

import transcoder

GRID = 9
FRAMES = GRID * GRID
SIZE = 160               # long side of a tile
PARALLEL = 4             # frame grabs at once
SPRITE, VTT = 'sprite.jpg', 'thumbs.vtt'


def folder(cache_id):
    return os.path.join(transcoder.CACHE_DIR, cache_id)


def ready(cache_id):
    d = folder(cache_id)
    return os.path.exists(os.path.join(d, SPRITE)) and os.path.exists(os.path.join(d, VTT))


def version(cache_id):
    try:
        return int(os.path.getmtime(os.path.join(folder(cache_id), VTT)))
    except OSError:
        return 0


def _vtt_time(s):
    ms = int(round(s * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}'


def vtt(duration, tile_w, tile_h, count=FRAMES, image=SPRITE):
    """The WebVTT text: frame i covers [i*step, (i+1)*step)."""
    step = duration / count if count else duration
    out = ['WEBVTT', '']
    for i in range(count):
        x, y = (i % GRID) * tile_w, (i // GRID) * tile_h
        end = duration if i == count - 1 else (i + 1) * step
        out += [f'{_vtt_time(i * step)} --> {_vtt_time(end)}', f'{image}#xywh={x},{y},{tile_w},{tile_h}', '']
    return '\n'.join(out)


def _grab(path, at, scale):
    """One frame as a PIL image: the nearest keyframe (decodes far less, ~3x faster on HEVC),
    else a normal fast seek, else an accurate one."""
    for seek in (['-skip_frame', 'nokey', '-ss', f'{at:.3f}', '-i', path], ['-ss', f'{at:.3f}', '-i', path],
                 ['-i', path, '-ss', f'{at:.3f}']):
        cmd = ['nice', '-n', '15', 'ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', *seek, '-frames:v', '1',
               '-vf', scale, '-f', 'image2pipe', '-c:v', 'bmp', '-']
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=60, env=transcoder.ENV).stdout
            if out:
                img = Image.open(io.BytesIO(out))
                img.load()
                return img.convert('RGB')
        except Exception:
            pass
    return None


def generate(path, cache_id, force=False):
    """Make sprite.jpg + thumbs.vtt for one file. -> True when made."""
    d = folder(cache_id)
    if not force and ready(cache_id):
        return True
    info = transcoder.probe(path)
    dur = info['duration']
    if dur <= 0:
        raise ValueError('Unknown duration')
    portrait = info['height'] > info['width']
    scale = f'scale=-2:{SIZE}' if portrait else f'scale={SIZE}:-2'
    times = [i * dur / FRAMES for i in range(FRAMES)]
    with ThreadPoolExecutor(PARALLEL) as pool:
        frames = list(pool.map(lambda at: _grab(path, at, scale), times))
    good = [f for f in frames if f is not None]
    if not good:
        raise ValueError('No frames could be read')
    tw, th = good[0].size
    sheet = Image.new('RGB', (tw * GRID, th * GRID))
    for i, f in enumerate(frames):
        if f is not None:
            sheet.paste(f if f.size == (tw, th) else f.resize((tw, th)), ((i % GRID) * tw, (i // GRID) * th))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, '.sprite.tmp.jpg')
    sheet.save(tmp, 'JPEG', quality=80)
    os.replace(tmp, os.path.join(d, SPRITE))
    with open(os.path.join(d, '.thumbs.tmp'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(vtt(dur, tw, th))
    os.replace(os.path.join(d, '.thumbs.tmp'), os.path.join(d, VTT))
    return True


class Worker:
    """One background thread; open-in-player requests jump the queue."""
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = deque()                  # (path, cache_id, force, bulk)
        self.queued = set()
        self.thread = None
        self.state = {'running': False, 'current': None, 'bulk_total': 0, 'bulk_done': 0, 'bulk_errors': 0,
                      'last_error': '', 'made': 0}

    def _start(self):
        if not self.thread:
            self.thread = threading.Thread(target=self._loop, name='player-sprites', daemon=True)
            self.thread.start()

    def enqueue(self, path, cache_id, front=False, force=False, bulk=False):
        with self.lock:
            if cache_id in self.queued:
                if front:                          # move it up
                    for item in list(self.queue):
                        if item[1] == cache_id:
                            self.queue.remove(item)
                            self.queue.appendleft(item)
                return
            item = (path, cache_id, force, bulk)
            self.queued.add(cache_id)
            if front:
                self.queue.appendleft(item)
            else:
                self.queue.append(item)
            if bulk:
                self.state['bulk_total'] += 1
        self._start()
        self.wake.set()

    def start_bulk(self, items, force=False):
        """items: [(path, cache_id)]. -> number queued."""
        with self.lock:
            if not any(b for *_, b in self.queue):
                self.state.update(bulk_total=0, bulk_done=0, bulk_errors=0)
        n = 0
        for path, cid in items:
            if force or not ready(cid):
                self.enqueue(path, cid, force=force, bulk=True)
                n += 1
        return n

    def stop_bulk(self):
        with self.lock:
            keep = deque(i for i in self.queue if not i[3])
            for i in self.queue:
                if i[3]:
                    self.queued.discard(i[1])
            dropped = len(self.queue) - len(keep)
            self.queue = keep
            self.state['bulk_total'] -= dropped
        return dropped

    def status(self):
        with self.lock:
            return dict(self.state, queued=len(self.queue), bulk_queued=sum(1 for i in self.queue if i[3]))

    def _loop(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        self.state.update(running=False, current=None)
                        break
                    path, cid, force, bulk = self.queue.popleft()
                    self.state.update(running=True, current=os.path.basename(path))
                try:
                    if os.path.exists(path):
                        generate(path, cid, force)
                        self.state['made'] += 1
                    else:
                        raise FileNotFoundError(path)
                    ok = True
                except Exception as e:
                    ok = False
                    self.state['last_error'] = f'{os.path.basename(path)}: {e}'
                with self.lock:
                    self.queued.discard(cid)
                    if bulk:
                        self.state['bulk_done'] += 1
                        if not ok:
                            self.state['bulk_errors'] += 1
                time.sleep(0.05)


worker = Worker()
