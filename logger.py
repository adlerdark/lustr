"""
Logging for lustr.

One setup for everything: the app's own messages, print() calls, uvicorn and the libraries lustr
uses. Each line goes once to the console (`docker logs lustr`) and once to <data>/logs/lustr.log
(rotated: 5 x 5 MB), so the log survives upgrading or re-creating the container.

- print() is routed through logging: each call becomes one log line under the module that printed
  it ('lustr.model', 'lustr.ext_db', ...), at INFO, or WARNING / ERROR when it reads like one.
- Level: LUSTR_LOG_LEVEL (DEBUG / INFO / WARNING), or the setting saved from Settings -> Maintenance
  (<data>/log_settings.json), which wins. DEBUG adds the chatty details (per-file scan steps,
  playback progress, ffmpeg output).
- Requests: errors and slow ones are always logged; routine polling (thumbnails, heartbeats,
  progress saves, job status) is left out of the access log.
- Recent warnings and errors are kept in memory (recent_problems()) for the problem report.
- Unhandled errors get a short ID (new_error_id()) that the page shows, so a report can point at
  the exact log entry.
"""

import builtins
import collections
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import threading
import time
import traceback

try:
    from config import DATA_DIR
except Exception:                                           # pragma: no cover - config always imports
    DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

LOG_DIR = os.path.join(DATA_DIR, 'logs')
APP_LOG_FILE = os.path.join(LOG_DIR, 'lustr.log')
FFMPEG_LOG_FILE = os.path.join(LOG_DIR, 'ffmpeg.log')
SETTINGS_FILE = os.path.join(DATA_DIR, 'log_settings.json')
LEVELS = ('DEBUG', 'INFO', 'WARNING')

LOG_FORMAT = '%(asctime)s %(levelname)-7s %(name)s: %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# Libraries that log every routine step at INFO; their warnings still come through.
CHATTY = ('faster_whisper', 'httpx', 'httpx2', 'httpcore', 'huggingface_hub', 'urllib3', 'PIL',
          'multipart', 'asyncio', 'watchfiles', 'ctranslate2')


# ---------- level ----------
def _saved_level():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            lvl = str(json.load(f).get('level', '')).upper()
        return lvl if lvl in LEVELS else None
    except (OSError, ValueError, AttributeError):
        return None


def current_level():
    """The level in use: the saved setting, else LUSTR_LOG_LEVEL, else INFO."""
    lvl = _saved_level() or str(os.environ.get('LUSTR_LOG_LEVEL', '')).upper()
    return lvl if lvl in LEVELS else 'INFO'


def set_level(level):
    """Change the level now and remember it (Settings -> Maintenance). -> the level in use."""
    level = str(level or '').upper()
    if level not in LEVELS:
        raise ValueError(f'Log level must be one of {", ".join(LEVELS)}')
    tmp = SETTINGS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'level': level}, f)
    os.replace(tmp, SETTINGS_FILE)
    _apply_level(level)
    logging.getLogger('lustr').info(f'Log level set to {level}')
    return level


def _apply_level(level):
    num = getattr(logging, level)
    logging.getLogger().setLevel(num)
    for name in CHATTY:
        logging.getLogger(name).setLevel(logging.DEBUG if num <= logging.DEBUG else logging.WARNING)
    logging.getLogger('ffmpeg').setLevel(num)


# ---------- recent problems (for the problem report) ----------
_problems = collections.deque(maxlen=300)
_problems_lock = threading.Lock()


class _ProblemBuffer(logging.Handler):
    """Keeps the last few hundred warnings and errors, formatted, with their tracebacks."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

    def emit(self, record):
        try:
            text = self.format(record)
            with _problems_lock:
                _problems.append({'time': record.created, 'level': record.levelname, 'logger': record.name,
                                  'error_id': getattr(record, 'error_id', None), 'text': text[:8000]})
        except Exception:
            pass


def recent_problems(limit=100):
    """The latest warnings and errors, newest last: [{time, level, logger, error_id, text}]."""
    with _problems_lock:
        return list(_problems)[-limit:]


def new_error_id():
    return secrets.token_hex(3)


# ---------- access log ----------
# Requests made constantly while the app is open (thumbnails, heartbeats, progress saves, job
# status polls, video bytes). Errors on them are still logged.
_QUIET_REQUESTS = re.compile(
    r'^(/api/activity|/api/health|/api/check_auth|/api/watch/progress|/favicon\.ico|/manifest\.webmanifest'
    r'|/static/|/cache/|/clips/|/files/|/api/stream/|/api/player/(hls|direct|thumbs|sprites|vtt)'
    r'|/api/images/\d+/(thumb|file|view)|/api/movies/\d+/(cover|back)|/api/cast/photo|/api/cast/thumb'
    r'|/api/extdb/cast/thumb|/api/thumbnails/frames|/api/.*/status$|/api/.*/bulk/status)')


class _AccessFilter(logging.Filter):
    def filter(self, record):
        try:
            _client, _method, path, _ver, status = record.args
            return int(status) >= 400 or not _QUIET_REQUESTS.match(str(path).split('?', 1)[0])
        except Exception:
            return True


class _DropDuplicateTracebacks(logging.Filter):
    """uvicorn logs an unhandled error's traceback again after lustr's error handler has logged it
    (with its error ID); keep only lustr's."""

    def filter(self, record):
        record.name = 'uvicorn'                            # its normal start-up lines aren't errors
        return not str(record.msg).startswith('Exception in ASGI application')


# ---------- print() -> logging ----------
_ERROR_WORDS = re.compile(r'\b(error|errors?:|failed|failure|traceback|exception)\b', re.I)
_WARN_WORDS = re.compile(r"^(⚠|warning\b)|\b(could not|couldn't|can't|cannot|not applied|unavailable|skipped)\b", re.I)
_original_print = builtins.print


def _level_of(message):
    head = message.lstrip()
    if head.startswith('✗') or _ERROR_WORDS.search(message):
        return logging.ERROR if not re.search(r'\b0 (errors|failed)\b', message) else logging.INFO
    if _WARN_WORDS.search(head):
        return logging.WARNING
    return logging.INFO


def _logging_print(*args, sep=' ', end='\n', file=None, flush=False):
    if file not in (None, sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        return _original_print(*args, sep=sep, end=end, file=file, flush=flush)
    try:
        message = (sep if sep is not None else ' ').join(str(a) for a in args).strip('\n')
    except Exception:
        message = ' '.join(repr(a) for a in args)
    if not message.strip():
        return                                              # blank spacer lines
    try:
        module = sys._getframe(1).f_globals.get('__name__', 'app')
    except Exception:
        module = 'app'
    name = 'lustr' if module in ('__main__', 'web_server') else 'lustr.' + module.rsplit('.', 1)[-1]
    lg = logging.getLogger(name)
    level = _level_of(message)
    if lg.isEnabledFor(level):
        lg.log(level, message)


def _logging_print_exc(limit=None, file=None, chain=True):
    if file is not None:
        return _original_print_exc(limit=limit, file=file, chain=chain)
    logging.getLogger('lustr').error(traceback.format_exc(limit=limit, chain=chain).rstrip())


_original_print_exc = traceback.print_exc


# ---------- setup ----------
_configured = False


def setup():
    global _configured
    if _configured:
        return
    _configured = True
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        app_file = logging.handlers.RotatingFileHandler(APP_LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5,
                                                        encoding='utf-8')
        app_file.setFormatter(fmt)
        root.addHandler(app_file)
    except OSError as e:
        _original_print(f'lustr: cannot write {APP_LOG_FILE} ({e}); logging to the console only')
    root.addHandler(_ProblemBuffer())

    # uvicorn: no handlers of its own (uvicorn.run gets log_config=None), so its lines come
    # through the root logger once, in the same format
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger('uvicorn.access').addFilter(_AccessFilter())
    logging.getLogger('uvicorn.error').addFilter(_DropDuplicateTracebacks())

    # ffmpeg command lines and output: their own file, not the main log
    ff = logging.getLogger('ffmpeg')
    ff.propagate = False
    ff.handlers = []
    try:
        ff_file = logging.handlers.RotatingFileHandler(FFMPEG_LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2,
                                                       encoding='utf-8')
        ff_file.setFormatter(fmt)
        ff.addHandler(ff_file)
    except OSError:
        ff.addHandler(console)

    _apply_level(current_level())

    builtins.print = _logging_print
    traceback.print_exc = _logging_print_exc

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        logging.getLogger('lustr').critical('Unhandled error', exc_info=(exc_type, exc, tb))
    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, 'name', '?')
        logging.getLogger('lustr').error(f'Unhandled error in background task {name}',
                                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _thread_excepthook


setup()

app_logger = logging.getLogger('lustr')
ffmpeg_logger = logging.getLogger('ffmpeg')
started_at = time.time()


def get_app_logger():
    return app_logger


def get_ffmpeg_logger():
    return ffmpeg_logger


def log_info(message):
    app_logger.info(message)


def log_warning(message):
    app_logger.warning(message)


def log_error(message):
    app_logger.error(message)


def log_debug(message):
    app_logger.debug(message)


def log_ffmpeg(message):
    ffmpeg_logger.info(message)


def log_ffmpeg_debug(message):
    ffmpeg_logger.debug(message)


def tail(path=APP_LOG_FILE, max_bytes=256 * 1024):
    """The end of a log file (for the problem report)."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        text = data.decode('utf-8', errors='replace')
        return text if size <= max_bytes else text.split('\n', 1)[-1]
    except OSError:
        return ''
