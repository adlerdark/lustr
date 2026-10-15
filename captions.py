# This file is: ./captions.py
"""
Closed captions for the players.

For each video, in this order:
  1. subtitles that already exist: a .srt / .vtt next to the file (name.srt, name.en.srt, ...),
     or a text subtitle track inside it (subrip, ass, mov_text, webvtt - picture subtitles are skipped);
  2. otherwise speech-to-text on this server with Whisper (faster-whisper, CPU, int8) - nothing leaves it.

Files live in the video's cache folder (data/cache/{cache_id}/, served under /cache):
captions.<lang>.<source>.vtt and captions.json (what's there: language, source, model, date).

One background worker at low priority (nice 15, a few cores): a video opened in the player
jumps the queue; bulk jobs queue a selection or a library; an optional night window feeds it
libraries a little at a time. No FastAPI imports.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

import transcoder

SETTINGS_KEY = 'captions'
MODELS = ('tiny', 'base', 'small', 'medium', 'large-v3-turbo')
MODEL_REPOS = {'tiny': 'Systran/faster-whisper-tiny', 'base': 'Systran/faster-whisper-base',
               'small': 'Systran/faster-whisper-small', 'medium': 'Systran/faster-whisper-medium',
               'large-v3-turbo': 'mobiuslabsgmbh/faster-whisper-large-v3-turbo'}
DEFAULTS = {'model': 'small', 'language': 'auto', 'cpu_threads': 6, 'use_existing': True, 'auto_on_play': False,
            'night': {'enabled': False, 'start': 1, 'end': 7, 'libraries': []}}
TEXT_SUB_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}
INDEX = 'captions.json'
NICE = 15
LANG_NAMES = {'en': 'English', 'de': 'Deutsch', 'fr': 'Français', 'es': 'Español', 'it': 'Italiano', 'pt': 'Português',
              'nl': 'Nederlands', 'ru': 'Русский', 'ja': '日本語', 'zh': '中文', 'ko': '한국어', 'cs': 'Čeština',
              'hu': 'Magyar', 'pl': 'Polski', 'sv': 'Svenska', 'da': 'Dansk', 'no': 'Norsk', 'fi': 'Suomi', 'und': 'Unknown'}
SOURCE_LABELS = {'whisper': 'auto', 'embedded': 'from the file', 'sidecar': 'subtitle file'}


# ---------------------------------------------------------------------------
# settings (metadata table)
# ---------------------------------------------------------------------------
def get_settings(conn):
    s = json.loads(json.dumps(DEFAULTS))
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (SETTINGS_KEY,)).fetchone()
        saved = json.loads(row[0]) if row else {}
    except Exception:
        saved = {}
    return _merge(s, saved)


def _merge(s, changes):
    if changes.get('model') in MODELS:
        s['model'] = changes['model']
    lang = str(changes.get('language', s['language']) or 'auto').strip().lower()
    s['language'] = lang if lang == 'auto' or re.fullmatch(r'[a-z]{2,3}', lang) else s['language']
    if 'cpu_threads' in changes:
        try:
            s['cpu_threads'] = max(1, min(os.cpu_count() or 4, int(changes['cpu_threads'])))
        except (TypeError, ValueError):
            pass
    for k in ('use_existing', 'auto_on_play'):
        if k in changes:
            s[k] = bool(changes[k])
    n = changes.get('night')
    if isinstance(n, dict):
        night = dict(s['night'])
        if 'enabled' in n:
            night['enabled'] = bool(n['enabled'])
        for k in ('start', 'end'):
            if k in n:
                try:
                    night[k] = int(n[k]) % 24
                except (TypeError, ValueError):
                    pass
        if isinstance(n.get('libraries'), list):
            night['libraries'] = [str(x) for x in n['libraries']]
        s['night'] = night
    return s


def save_settings(conn, changes):
    s = _merge(get_settings(conn), changes or {})
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SETTINGS_KEY, json.dumps(s)))
    conn.commit()
    return s


# ---------------------------------------------------------------------------
# Whisper availability and models
# ---------------------------------------------------------------------------
def whisper_available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def model_dir():
    return os.environ.get('HF_HOME') or os.path.join(transcoder.CACHE_DIR, '..', 'models')


def model_downloaded(name):
    repo = MODEL_REPOS.get(name, '')
    d = os.path.join(model_dir(), 'hub', 'models--' + repo.replace('/', '--'), 'snapshots')
    return os.path.isdir(d) and any(os.scandir(d))


def models_status():
    return [{'name': m, 'downloaded': model_downloaded(m)} for m in MODELS]


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
def folder(cache_id):
    return os.path.join(transcoder.CACHE_DIR, cache_id)


def _read_index(cache_id):
    try:
        with open(os.path.join(folder(cache_id), INDEX), encoding='utf-8') as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _write_index(cache_id, items):
    d = folder(cache_id)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, '.' + INDEX + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(d, INDEX))


def _add_track(cache_id, file, lang, source, model=None):
    items = [i for i in _read_index(cache_id) if i.get('file') != file]
    items.append({'file': file, 'lang': lang or 'und', 'source': source, 'model': model,
                  'created': datetime.now().isoformat(timespec='seconds')})
    _write_index(cache_id, items)


def tracks(cache_id):
    """-> [{lang, label, source, src, file}] for the player (only files that exist)."""
    out = []
    for i in _read_index(cache_id):
        p = os.path.join(folder(cache_id), i.get('file', ''))
        if not i.get('file') or not os.path.exists(p):
            continue
        lang = i.get('lang') or 'und'
        label = LANG_NAMES.get(lang, lang) + (f" ({SOURCE_LABELS.get(i.get('source'), i.get('source'))})" if i.get('source') else '')
        out.append({'lang': lang, 'label': label, 'source': i.get('source'), 'file': i['file'], 'model': i.get('model'),
                    'src': f"/cache/{cache_id}/{i['file']}?v={int(os.path.getmtime(p))}"})
    return out


def has_captions(cache_id):
    return bool(tracks(cache_id))


def remove(cache_id, file=None):
    """Delete one track (file) or all of them. -> number removed"""
    n, keep = 0, []
    for i in _read_index(cache_id):
        if file is None or i.get('file') == file:
            try:
                os.remove(os.path.join(folder(cache_id), i.get('file', '')))
            except OSError:
                pass
            n += 1
        else:
            keep.append(i)
    if keep:
        _write_index(cache_id, keep)
    else:
        try:
            os.remove(os.path.join(folder(cache_id), INDEX))
        except OSError:
            pass
    return n


# ---------------------------------------------------------------------------
# WebVTT / SRT
# ---------------------------------------------------------------------------
def vtt_time(s):
    ms = max(0, int(round(s * 1000)))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}'


def write_vtt(segments, out_path):
    """segments: [(start, end, text)]"""
    lines = ['WEBVTT', '']
    for start, end, text in segments:
        lines += [f'{vtt_time(start)} --> {vtt_time(end)}', text, '']
    tmp = out_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines))
    os.replace(tmp, out_path)


def srt_to_vtt(text):
    """SRT -> WebVTT text (timestamps use '.', cue numbers dropped)."""
    text = text.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n')
    out = ['WEBVTT', '']
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = block.split('\n')
        if lines and re.fullmatch(r'\d+', lines[0].strip()):
            lines = lines[1:]
        if not lines or '-->' not in lines[0]:
            continue
        lines[0] = re.sub(r'(\d{1,2}:\d{2}:\d{2}),(\d{1,3})', r'\1.\2', lines[0])
        out += lines + ['']
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# Whisper's invented text
# ---------------------------------------------------------------------------
_HALLUCINATIONS = [r'thank(s| you) (so much )?for watching', r'subtitles? (by|from)', r'transcri(bed|ption) by',
                   r'please (like|subscribe)', r'(like and )?subscribe', r'amara\.org', r'www\.', r'\.com\b',
                   r"don't forget to", r'see you (in the )?next']


def clean_segments(segments):
    """Drop what Whisper typically invents on music / moaning / silence:
    lines repeated 3+ times in a row, punctuation-only lines, the well-known phrases,
    and long cues with very little text."""
    out, run = [], []
    hall = re.compile('|'.join(_HALLUCINATIONS), re.I)
    for start, end, text in segments:
        t = re.sub(r'\s+', ' ', (text or '')).strip()
        if not re.search(r'\w', t):
            continue
        if hall.search(t):
            continue
        if end - start > 7 and len(t) < 8:
            continue
        key = re.sub(r'\W+', '', t.lower())
        if run and run[-1] == key:
            run.append(key)
        else:
            run = [key]
        if len(run) >= 3:                       # the 3rd+ repeat in a row: drop it and the earlier ones
            while out and re.sub(r'\W+', '', out[-1][2].lower()) == key:
                out.pop()
            continue
        out.append((start, end, t))
    return out


# ---------------------------------------------------------------------------
# existing subtitles
# ---------------------------------------------------------------------------
def sidecars(path):
    """Subtitle files next to the video: [(file, lang)]."""
    d, base = os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in sorted(names):
        stem, ext = os.path.splitext(n)
        if ext.lower() not in ('.srt', '.vtt'):
            continue
        if stem == base:
            out.append((os.path.join(d, n), 'und'))
        elif stem.startswith(base + '.') and re.fullmatch(r'[A-Za-z]{2,3}', stem[len(base) + 1:]):
            out.append((os.path.join(d, n), stem[len(base) + 1:].lower()))
    return out


def embedded(path):
    """Text subtitle tracks inside the file: [(stream number among subtitles, lang, codec)]."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries',
                            'stream=codec_name:stream_tags=language', '-of', 'json', path],
                           capture_output=True, text=True, timeout=60, env=transcoder.ENV)
        streams = json.loads(r.stdout or '{}').get('streams') or []
    except Exception:
        return []
    out = []
    for i, s in enumerate(streams):
        codec = (s.get('codec_name') or '').lower()
        if codec in TEXT_SUB_CODECS:
            out.append((i, ((s.get('tags') or {}).get('language') or 'und').lower()[:3], codec))
    return out


def _lang2(lang):
    """'eng' -> 'en' for the common ones (players use 2-letter codes)."""
    three = {'eng': 'en', 'ger': 'de', 'deu': 'de', 'fre': 'fr', 'fra': 'fr', 'spa': 'es', 'ita': 'it', 'por': 'pt',
             'dut': 'nl', 'nld': 'nl', 'rus': 'ru', 'jpn': 'ja', 'chi': 'zh', 'zho': 'zh', 'kor': 'ko', 'cze': 'cs',
             'ces': 'cs', 'hun': 'hu', 'pol': 'pl', 'swe': 'sv', 'dan': 'da', 'nor': 'no', 'fin': 'fi'}
    return three.get(lang, lang)


def use_existing(path, cache_id):
    """Copy subtitle files / text tracks into the cache as WebVTT. -> number of tracks found"""
    d = folder(cache_id)
    os.makedirs(d, exist_ok=True)
    n = 0
    for i, (sub, lang) in enumerate(sidecars(path)):
        lang = _lang2(lang)
        name = f'captions.{lang}.sidecar{i or ""}.vtt'
        try:
            with open(sub, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
            vtt = text if text.lstrip('﻿').startswith('WEBVTT') else srt_to_vtt(text)
            with open(os.path.join(d, name), 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(vtt)
            _add_track(cache_id, name, lang, 'sidecar')
            n += 1
        except OSError:
            pass
    for idx, lang, codec in embedded(path):
        lang = _lang2(lang)
        name = f'captions.{lang}.embedded{idx}.vtt'
        out = os.path.join(d, name)
        r = subprocess.run(['nice', '-n', str(NICE), 'ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-y', '-i', path,
                            '-map', f'0:s:{idx}', '-f', 'webvtt', out], capture_output=True, timeout=600, env=transcoder.ENV)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 10:
            _add_track(cache_id, name, lang, 'embedded')
            n += 1
    return n


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------
_model_cache = {'name': None, 'threads': None, 'model': None}


def _load_model(name, threads):
    from faster_whisper import WhisperModel
    if _model_cache['name'] != name or _model_cache['threads'] != threads:
        _model_cache.update(name=None, model=None)
        _model_cache['model'] = WhisperModel(name, device='cpu', compute_type='int8', cpu_threads=threads)   # downloads into HF_HOME once
        _model_cache.update(name=name, threads=threads)
    return _model_cache['model']


def _audio(path, out_wav):
    """16 kHz mono WAV for Whisper."""
    r = subprocess.run(['nice', '-n', str(NICE), 'ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-y', '-i', path,
                        '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', out_wav],
                       capture_output=True, timeout=3600, env=transcoder.ENV)
    if r.returncode != 0 or not os.path.exists(out_wav):
        raise RuntimeError('No audio could be read: ' + (r.stderr.decode('utf-8', 'replace')[-200:] if r.stderr else ''))


def transcribe(path, cache_id, settings, progress=None, stop=None):
    """Whisper -> captions.<lang>.whisper.vtt. -> (lang, number of lines)"""
    model_name = settings['model']
    big = model_name in ('medium', 'large-v3-turbo')
    with tempfile.TemporaryDirectory(prefix='lustr-cc-') as tmp:
        wav = os.path.join(tmp, 'audio.wav')
        _audio(path, wav)
        model = _load_model(model_name, settings['cpu_threads'])
        segs, info = model.transcribe(
            wav, language=None if settings['language'] == 'auto' else settings['language'],
            task='transcribe', beam_size=5 if big else 1, vad_filter=True,
            vad_parameters={'min_silence_duration_ms': 500}, condition_on_previous_text=False,
            no_speech_threshold=0.6, compression_ratio_threshold=2.4, log_prob_threshold=-1.0)
        dur = float(getattr(info, 'duration', 0) or 0)
        raw = []
        for s in segs:
            raw.append((s.start, s.end, s.text))
            if progress and dur:
                progress(min(1.0, s.end / dur))
            if stop and stop():
                raise InterruptedError('Stopped')
    lang = getattr(info, 'language', None) or 'und'
    lines = clean_segments(raw)
    name = f'captions.{lang}.whisper.vtt'
    os.makedirs(folder(cache_id), exist_ok=True)
    write_vtt(lines, os.path.join(folder(cache_id), name))
    _add_track(cache_id, name, lang, 'whisper', model_name)
    return lang, len(lines)


def process(path, cache_id, settings, force=False, progress=None, stop=None):
    """Existing subtitles first, then Whisper. -> 'existing' | 'whisper' | 'nothing'"""
    if settings.get('use_existing', True) and not force:
        if use_existing(path, cache_id):
            return 'existing'
    if not whisper_available():
        raise RuntimeError('Whisper is not installed in this image - rebuild it to make captions')
    transcribe(path, cache_id, settings, progress, stop)
    return 'whisper'


# ---------------------------------------------------------------------------
# background worker
# ---------------------------------------------------------------------------
def _lower_priority():
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)
    except Exception:
        pass


class Worker:
    """One background thread; a video opened in the player jumps the queue."""
    def __init__(self, settings_fn=None):
        self.settings_fn = settings_fn            # () -> settings dict
        self.on_change = None                     # (path, cache_id) after each video: keeps the 'has captions' list current
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = deque()                      # (path, cache_id, force, bulk)
        self.queued = set()
        self.history = deque(maxlen=100)          # finished jobs this session, newest last
        self.cancel = None                        # cache_id being stopped
        self.thread = None
        self.state = {'running': False, 'current': None, 'current_cache_id': None, 'progress': 0.0,
                      'bulk_total': 0, 'bulk_done': 0, 'bulk_errors': 0, 'made': 0, 'last_error': '',
                      'last_done': None, 'speed': None}

    def _start(self):
        if not self.thread:
            self.thread = threading.Thread(target=self._loop, name='captions', daemon=True)
            self.thread.start()

    def enqueue(self, path, cache_id, front=False, force=False, bulk=False):
        with self.lock:
            if cache_id in self.queued or self.state.get('current_cache_id') == cache_id:
                if front:
                    for item in list(self.queue):
                        if item[1] == cache_id:
                            self.queue.remove(item)
                            self.queue.appendleft(item)
                return False
            item = (path, cache_id, force, bulk)
            self.queued.add(cache_id)
            (self.queue.appendleft if front else self.queue.append)(item)
            if bulk:
                self.state['bulk_total'] += 1
        self._start()
        self.wake.set()
        return True

    def start_bulk(self, items, force=False):
        """items: [(path, cache_id)]. Videos that already have captions are skipped unless force. -> queued"""
        with self.lock:
            if not any(i[3] for i in self.queue):
                self.state.update(bulk_total=0, bulk_done=0, bulk_errors=0)
        n = 0
        for path, cid in items:
            if (force or not has_captions(cid)) and self.enqueue(path, cid, force=force, bulk=True):
                n += 1
        return n

    def stop(self, current=True):
        """Empty the queue (and stop the video being worked on). -> number dropped"""
        with self.lock:
            dropped = len(self.queue)
            self.queue.clear()
            self.queued.clear()
            if current and self.state.get('current_cache_id'):
                self.cancel = self.state['current_cache_id']
            self.state.update(bulk_total=self.state['bulk_done'])
        return dropped

    def queue_list(self, limit=200):
        with self.lock:
            return [{'file': os.path.basename(p), 'path': p, 'cache_id': c, 'bulk': bool(b)}
                    for p, c, _, b in list(self.queue)[:limit]]

    def drop(self, cache_id):
        """Take one video off the queue. -> True if it was queued"""
        with self.lock:
            for item in list(self.queue):
                if item[1] == cache_id:
                    self.queue.remove(item)
                    self.queued.discard(cache_id)
                    if item[3]:
                        self.state['bulk_total'] = max(self.state['bulk_done'], self.state['bulk_total'] - 1)
                    return True
        return False

    def queued_ids(self):
        with self.lock:
            return [i[1] for i in self.queue]

    def status(self):
        with self.lock:
            return dict(self.state, queued=len(self.queue), bulk_queued=sum(1 for i in self.queue if i[3]))

    def _loop(self):
        _lower_priority()
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        self.state.update(running=False, current=None, current_cache_id=None, progress=0.0)
                        break
                    path, cid, force, bulk = self.queue.popleft()
                    self.state.update(running=True, current=os.path.basename(path), current_cache_id=cid, progress=0.0)
                    self.cancel = None
                t0, ok, kind, err = time.time(), False, None, ''
                try:
                    if not os.path.exists(path):
                        raise FileNotFoundError(path)
                    settings = self.settings_fn() if self.settings_fn else dict(DEFAULTS)

                    def prog(f):
                        self.state['progress'] = f
                    kind = process(path, cid, settings, force, prog, lambda: self.cancel == cid)
                    self.state['made'] += 1
                    dur = transcoder.probe(path).get('duration') or 0
                    if kind == 'whisper' and dur:
                        self.state['speed'] = {'model': settings['model'], 'x_realtime': round(dur / max(1, time.time() - t0), 2)}
                    ok = True
                except InterruptedError:
                    err = 'stopped'
                    self.state['last_error'] = f'{os.path.basename(path)}: stopped'
                except Exception as e:
                    err = str(e)
                    self.state['last_error'] = f'{os.path.basename(path)}: {e}'
                if self.on_change:
                    try:
                        self.on_change(path, cid)
                    except Exception as e:
                        print(f'[captions] index update: {e}')
                with self.lock:
                    self.queued.discard(cid)
                    self.state.update(last_done={'cache_id': cid, 'ok': ok}, current_cache_id=None)
                    self.history.append({'file': os.path.basename(path), 'path': path, 'cache_id': cid, 'ok': ok,
                                         'kind': kind if ok else None, 'error': err,
                                         'seconds': round(time.time() - t0, 1), 'at': datetime.now().isoformat(timespec='seconds')})
                    if bulk:
                        self.state['bulk_done'] += 1
                        if not ok:
                            self.state['bulk_errors'] += 1
                time.sleep(0.05)


worker = Worker()


# ---------------------------------------------------------------------------
# night window: feed the worker chosen libraries a little at a time
# ---------------------------------------------------------------------------
def in_window(start, end, hour):
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def start_night_scheduler(get_settings_fn, files_fn, batch=20, every=300):
    """get_settings_fn() -> settings; files_fn(libraries) -> [(path, cache_id)] without captions yet."""
    def loop():
        time.sleep(60)
        while True:
            try:
                s = get_settings_fn()
                n = s.get('night') or {}
                if n.get('enabled') and n.get('libraries') and in_window(n.get('start', 1), n.get('end', 7), datetime.now().hour):
                    st = worker.status()
                    if not st['queued']:
                        todo = [x for x in files_fn(n['libraries']) if not has_captions(x[1])][:batch]
                        if todo:                    # small batches, so it stops soon after the window ends
                            worker.start_bulk(todo)
            except Exception as e:
                print(f'[captions] night queue: {e}')
            time.sleep(every)
    threading.Thread(target=loop, name='captions-night', daemon=True).start()
