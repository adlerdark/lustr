# This file is: ./transcoder.py
"""
HLS transcoding for the new player: segments made on demand, at the resolution asked for.

The playlist is synthetic: fixed 2-second segments worked out from the duration. Segments
are made on demand by one ffmpeg per (file, resolution), started at the requested segment
with `-ss N*2 ... -copyts -force_key_frames expr:gte(t,n_forced*2)`, so a restart anywhere
lines up with the playlist. A monitor thread:
  - restarts ffmpeg when a requested segment is behind it or more than MAX_GAP ahead,
  - stops it once BUFFER segments after the last requested one exist,
  - kills it and deletes its folder after IDLE seconds without requests.
ffmpeg writes '.N.ts'; a segment is complete (renamed to 'N.ts') once '.N+1.ts' exists or
ffmpeg ended cleanly.

Encoding, best first: full VAAPI (GPU decode + scale + encode; probed once per file),
software decode + VAAPI encode, then libx264. Separate from stream_handler.py (the classic
player) on purpose: no shared state, its own cache folder.

No FastAPI imports.
"""
import json
import math
import os
import shutil
import signal
import subprocess
import threading
import time

try:
    from config import CACHE_DIR
except Exception:                                   # tests without a config.py
    CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'cache')

SEGMENT = 2              # seconds per segment
MAX_GAP = 5              # a request this far past the encoder restarts it there
BUFFER = 15              # segments made ahead of the last request before ffmpeg is stopped
WAIT = 15                # seconds a segment request waits
IDLE = 30                # seconds without requests before a stream is dropped
MONITOR = 0.2
MAX_PROCS = 3            # ffmpeg processes at once; the longest-idle one gives way

# name -> short side in pixels (0 = as is). 'original' is always offered.
RESOLUTIONS = (('original', 0), ('1080', 1080), ('720', 720), ('480', 480), ('240', 240))
BITRATE_K = {'original': 8000, '1080': 8000, '720': 4000, '480': 2000, '240': 600}
MODES = ('full', 'hwupload', 'software')

DRI = os.environ.get('LUSTR_DRI_DEVICE', '/dev/dri/renderD128')
ROOT = os.path.join(CACHE_DIR, 'player_hls')
# jellyfin-ffmpeg ships its own VA drivers; libva picks the one for the GPU (Intel iHD, AMD radeonsi)
# unless LIBVA_DRIVER_NAME is set in the environment.
ENV = dict(os.environ, LIBVA_DRIVERS_PATH=os.environ.get('LIBVA_DRIVERS_PATH', '/usr/lib/jellyfin-ffmpeg/lib/dri'))

HW = {'checked': False, 'vaapi': False, 'error': ''}
_full_hw = {}            # path -> True/False (can the GPU decode this file?)
_probes = {}             # (path, size, mtime) -> info


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------
def probe(path):
    """-> {duration, width, height, codec, audio, rotation} (cached per file version)."""
    try:
        st = os.stat(path)
        key = (path, st.st_size, int(st.st_mtime))
    except OSError:
        key = (path, 0, 0)
    if key in _probes:
        return _probes[key]
    info = {'duration': 0.0, 'width': 0, 'height': 0, 'codec': '', 'audio': False, 'rotation': 0}
    try:
        out = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', path],
                             capture_output=True, timeout=30, env=ENV).stdout
        data = json.loads(out or b'{}')
        try:
            info['duration'] = float((data.get('format') or {}).get('duration') or 0)
        except ValueError:
            pass
        for s in data.get('streams') or []:
            if s.get('codec_type') == 'video' and not info['codec'] and not (s.get('disposition') or {}).get('attached_pic'):
                info.update(width=int(s.get('width') or 0), height=int(s.get('height') or 0), codec=s.get('codec_name') or '')
                rot = 0
                for sd in s.get('side_data_list') or []:
                    if 'rotation' in sd:
                        rot = int(sd['rotation'])
                rot = rot or int((s.get('tags') or {}).get('rotate') or 0)
                info['rotation'] = rot
                if abs(rot) in (90, 270):                     # shown sideways: swap
                    info['width'], info['height'] = info['height'], info['width']
                if not info['duration']:
                    try:
                        info['duration'] = float(s.get('duration') or 0)
                    except ValueError:
                        pass
            elif s.get('codec_type') == 'audio':
                info['audio'] = True
    except Exception:
        pass
    _probes[key] = info
    if len(_probes) > 2000:
        _probes.pop(next(iter(_probes)))
    return info


def detect_hw():
    """Start-up test: can we encode h264 with VAAPI?"""
    cmd = ['ffmpeg', '-hide_banner', '-v', 'warning', '-vaapi_device', DRI, '-f', 'lavfi', '-i', 'color=c=red:s=1280x720',
           '-t', '0.1', '-c:v', 'h264_vaapi', '-vf', 'format=nv12,hwupload,scale_vaapi=-2:480', '-f', 'null', '-']
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20, env=ENV)
        HW.update(vaapi=r.returncode == 0, error='' if r.returncode == 0 else r.stderr.decode(errors='replace')[-400:])
    except Exception as e:
        HW.update(vaapi=False, error=str(e))
    HW['checked'] = True
    print(f"[Player] VAAPI h264 encode: {'available' if HW['vaapi'] else 'not available'}")
    return HW


def full_hw_ok(path):
    """Can the GPU decode this file too? A 1-second test, remembered per file."""
    if path in _full_hw:
        return _full_hw[path]
    cmd = ['ffmpeg', '-hide_banner', '-v', 'warning', '-xerror', '-vaapi_device', DRI, '-hwaccel', 'vaapi',
           '-hwaccel_output_format', 'vaapi', '-i', path, '-t', '1', '-c:v', 'h264_vaapi', '-vf', 'scale_vaapi=format=nv12',
           '-an', '-f', 'null', '-']
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=20, env=ENV).returncode == 0
    except Exception:
        ok = False
    _full_hw[path] = ok
    if len(_full_hw) > 5000:
        _full_hw.pop(next(iter(_full_hw)))
    return ok


# ---------------------------------------------------------------------------
# resolutions, playlist, ffmpeg arguments
# ---------------------------------------------------------------------------
def short_side(width, height):
    return min(width, height) if width and height else 0


def resolutions(width, height, max_res='original'):
    """The HLS resolutions to offer: original, then each smaller standard size (short side)."""
    short = short_side(width, height)
    cap = dict(RESOLUTIONS).get(str(max_res), 0)
    out = []
    if not cap or not short or short <= cap:
        out.append('original')
    for name, px in RESOLUTIONS[1:]:
        if short and px < short and (not cap or px <= cap):
            out.append(name)
    return out or [RESOLUTIONS[-1][0] if short else 'original']


def resolution_label(name, width, height):
    if name == 'original':
        short = short_side(width, height)
        return f'Original ({short}p)' if short else 'Original'
    return f'{name}p'


def scale_dims(width, height, res):
    """-> (w, h) for the scaler, or None when no scaling is needed. Even sizes, aspect kept (-2)."""
    target = dict(RESOLUTIONS).get(res, 0)
    short = short_side(width, height)
    if not target or not short or target >= short:
        return None
    return (-2, target) if width >= height else (target, -2)


def segment_count(duration):
    return max(1, math.ceil(max(duration, 0.001) / SEGMENT))


def playlist(duration, segment_url):
    """A VOD playlist of fixed segments. segment_url(n) -> the URL for segment n."""
    n = segment_count(duration)
    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-MEDIA-SEQUENCE:0', f'#EXT-X-TARGETDURATION:{SEGMENT}',
             '#EXT-X-PLAYLIST-TYPE:VOD']
    for i in range(n):
        length = SEGMENT if i < n - 1 else (duration - SEGMENT * (n - 1)) or SEGMENT
        lines += [f'#EXTINF:{length:.6f},', segment_url(i)]
    lines.append('#EXT-X-ENDLIST')
    return '\n'.join(lines) + '\n'


def build_args(path, info, res, start_seg, mode, out_dir):
    """ffmpeg arguments for one HLS run. mode: full | hwupload | software."""
    dims = scale_dims(info.get('width', 0), info.get('height', 0), res)
    rate = BITRATE_K.get(res, 8000)
    args = ['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin']
    if mode == 'full':
        args += ['-vaapi_device', DRI, '-hwaccel', 'vaapi', '-hwaccel_output_format', 'vaapi']
    elif mode == 'hwupload':
        args += ['-vaapi_device', DRI]
    if start_seg:
        args += ['-ss', str(start_seg * SEGMENT)]
    args += ['-i', path, '-map', '0:v:0']
    if mode == 'full':
        vf = f'scale_vaapi={dims[0]}:{dims[1]}:format=nv12' if dims else 'scale_vaapi=format=nv12'
    elif mode == 'hwupload':
        vf = 'format=nv12,hwupload' + (f',scale_vaapi={dims[0]}:{dims[1]}' if dims else '')
    else:
        vf = f'scale={dims[0]}:{dims[1]}' if dims else ''
    if mode == 'software':
        args += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
                 '-maxrate', f'{int(rate * 1.5)}k', '-bufsize', f'{rate * 3}k']
    else:
        args += ['-c:v', 'h264_vaapi', '-rc_mode', 'VBR', '-b:v', f'{rate}k', '-maxrate', f'{int(rate * 1.5)}k']
    args += ['-flags', '+cgop', '-force_key_frames', f'expr:gte(t,n_forced*{SEGMENT})']
    if vf:
        args += ['-vf', vf]
    if info.get('audio'):
        args += ['-map', '0:a:0', '-c:a', 'aac', '-ac', '2', '-b:a', '160k']
    else:
        args += ['-an']
    args += ['-sn', '-copyts', '-avoid_negative_ts', 'disabled', '-f', 'hls', '-start_number', str(start_seg),
             '-hls_time', str(SEGMENT), '-hls_flags', 'split_by_time', '-hls_segment_type', 'mpegts',
             '-hls_playlist_type', 'vod', '-hls_segment_filename', os.path.join(out_dir, '.%d.ts'),
             os.path.join(out_dir, 'ff.m3u8')]
    return args


# ---------------------------------------------------------------------------
# the segment manager
# ---------------------------------------------------------------------------
class SegmentError(Exception):
    pass


class _Stream:
    def __init__(self, key, path, info, res, folder, modes):
        self.key, self.path, self.info, self.res, self.dir = key, path, info, res, folder
        self.modes = list(modes)               # still to try, best first
        self.mode = None
        self.proc = None
        self.start_seg = 0
        self.made = -1                         # highest finished segment of this run
        self.last_access = time.monotonic()
        self.last_seg = 0
        self.waiting = 0
        self.error = ''


class SegmentManager:
    def __init__(self, root=ROOT):
        self.root = root
        self.cond = threading.Condition()
        self.streams = {}
        self._monitor = None
        self.log = []                          # recent starts (for tests / Settings): (key, mode, start_seg)

    # ----- lifecycle -----
    def start(self):
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root, exist_ok=True)
        if not self._monitor:
            self._monitor = threading.Thread(target=self._loop, name='player-hls', daemon=True)
            self._monitor.start()

    def stop_all(self):
        with self.cond:
            for st in list(self.streams.values()):
                self._kill(st)
                shutil.rmtree(st.dir, ignore_errors=True)
            self.streams.clear()

    # ----- requests -----
    def segment(self, path, res, n, hw=True, wait=WAIT):
        """-> the finished segment file, waiting for ffmpeg to make it."""
        info = probe(path)
        count = segment_count(info['duration'])
        if n < 0 or n >= count:
            raise SegmentError(f'No segment {n}')
        modes = self._modes(path, hw)
        key = (path, res)
        deadline = time.monotonic() + wait
        with self.cond:
            st = self.streams.get(key)
            if st is None:
                folder = os.path.join(self.root, f'{abs(hash(key)) & 0xffffffffffff:x}_{res}')
                os.makedirs(folder, exist_ok=True)
                st = self.streams[key] = _Stream(key, path, info, res, folder, modes)
            st.waiting += 1
            try:
                while True:
                    st.last_access = time.monotonic()
                    st.last_seg = n
                    self._promote(st)
                    f = os.path.join(st.dir, f'{n}.ts')
                    if os.path.exists(f):
                        return f
                    self._ensure(st, n)
                    if st.error and not st.proc:
                        raise SegmentError(st.error)
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise SegmentError(f'Segment {n} took too long')
                    self.cond.wait(min(MONITOR, left))
            finally:
                st.waiting -= 1

    def _modes(self, path, hw):
        if not hw or not HW['vaapi']:
            return ['software']
        return (['full'] if full_hw_ok(path) else []) + ['hwupload', 'software']

    # ----- ffmpeg control (called with the lock held) -----
    def _running(self, st):
        return st.proc is not None and st.proc.poll() is None

    def _ensure(self, st, n):
        if self._running(st):
            if n < st.start_seg or n > max(st.made, st.start_seg) + MAX_GAP:
                self._kill(st)
            else:
                return
        if st.proc is not None:                      # ended on its own
            self._finish(st)
        if st.error:
            return
        self._launch(st, n)

    def _launch(self, st, n):
        if not st.modes:
            st.error = st.error or 'Transcoding failed'
            return
        busy = [s for s in self.streams.values() if s is not st and self._running(s)]
        if len(busy) >= MAX_PROCS:
            self._kill(min(busy, key=lambda s: s.last_access))
        for f in os.listdir(st.dir):                 # leftovers of an earlier run
            if f.startswith('.'):
                os.remove(os.path.join(st.dir, f))
        st.mode = st.modes[0]
        st.start_seg, st.made = n, n - 1
        args = build_args(st.path, st.info, st.res, n, st.mode, st.dir)
        try:
            st.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                       env=ENV, start_new_session=True)
        except OSError as e:
            st.proc = None
            st.modes.pop(0)
            st.error = '' if st.modes else str(e)
            return
        st.proc._lustr_err = b''
        threading.Thread(target=self._drain, args=(st.proc,), daemon=True).start()
        self.log.append((st.key, st.mode, n))
        del self.log[:-50]

    @staticmethod
    def _drain(proc):
        try:
            for line in proc.stderr:
                proc._lustr_err = (proc._lustr_err + line)[-2000:]
        except Exception:
            pass

    def _kill(self, st):
        if st.proc is None:
            return
        if st.proc.poll() is None:
            try:
                os.killpg(st.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    st.proc.kill()
                except Exception:
                    pass
            try:
                st.proc.wait(timeout=3)
            except Exception:
                pass
        self._close(st.proc)
        st.proc = None
        for f in os.listdir(st.dir) if os.path.isdir(st.dir) else []:
            if f.startswith('.'):                    # the unfinished segment
                os.remove(os.path.join(st.dir, f))

    def _finish(self, st):
        """ffmpeg ended by itself: keep its last segment if it succeeded, else try the next mode."""
        rc = st.proc.returncode
        self._promote(st, final=rc == 0)
        if rc != 0:
            produced = st.made >= st.start_seg
            err = (getattr(st.proc, '_lustr_err', b'') or b'').decode(errors='replace').strip()
            print(f"[Player] ffmpeg ({st.mode}) ended with {rc} for {os.path.basename(st.path)}: {err[-300:]}")
            if not produced:
                st.modes.pop(0)
                if not st.modes:
                    st.error = f'Transcoding failed: {err[-200:] or rc}'
        self._close(st.proc)
        st.proc = None

    @staticmethod
    def _close(proc):
        try:
            proc.stderr.close()
        except Exception:
            pass

    def _promote(self, st, final=False):
        """'.N.ts' -> 'N.ts' once '.N+1.ts' exists (or ffmpeg is done)."""
        try:
            temps = sorted(int(f[1:-3]) for f in os.listdir(st.dir) if f.startswith('.') and f.endswith('.ts') and f[1:-3].isdigit())
        except OSError:
            return
        done = set(temps[:-1]) | ({temps[-1]} if temps and final else set())
        for k in done:
            src, dst = os.path.join(st.dir, f'.{k}.ts'), os.path.join(st.dir, f'{k}.ts')
            if os.path.exists(dst):
                os.remove(src)
            else:
                os.replace(src, dst)
            st.made = max(st.made, k)

    # ----- the monitor -----
    def _loop(self):
        while True:
            time.sleep(MONITOR)
            try:
                self.tick()
            except Exception as e:
                print(f"[Player] monitor: {e}")

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        with self.cond:
            for key, st in list(self.streams.items()):
                if st.proc is not None and st.proc.poll() is not None:
                    self._finish(st)
                else:
                    self._promote(st)
                last = min(st.last_seg + BUFFER, segment_count(st.info['duration']) - 1)
                if self._running(st) and all(os.path.exists(os.path.join(st.dir, f'{k}.ts'))
                                             for k in range(st.last_seg, last + 1)):
                    self._kill(st)                     # far enough ahead; restarts when needed
                if not st.waiting and now - st.last_access > IDLE:
                    self._kill(st)
                    shutil.rmtree(st.dir, ignore_errors=True)
                    del self.streams[key]
            self.cond.notify_all()

    def status(self):
        with self.cond:
            return [{'file': os.path.basename(p), 'res': r, 'mode': st.mode, 'running': self._running(st),
                     'start': st.start_seg, 'made': st.made, 'last_request': st.last_seg}
                    for (p, r), st in self.streams.items()]


manager = SegmentManager()
