# This file is: ./stream_handler.py

import os
import subprocess
import shutil
import uuid
import time
import threading
import signal
from config import CACHE_DIR

# Import logging
try:
    from logger import log_ffmpeg, log_ffmpeg_debug, log_error, log_info
except ImportError:
    # Fallback to print if logger not available
    def log_ffmpeg(msg): print(f"[FFMPEG] {msg}")
    def log_ffmpeg_debug(msg): print(f"[FFMPEG DEBUG] {msg}")
    def log_error(msg): print(f"[ERROR] {msg}")
    def log_info(msg): print(f"[INFO] {msg}")

class StreamSession:
    def __init__(self, session_id, process, output_dir, source_path, start_offset):
        self.session_id = session_id
        self.process = process
        self.output_dir = output_dir
        self.source_path = source_path
        self.start_offset = start_offset
        self.last_access = time.time()
        self.created_at = time.time()
        self.grace_period = 30  # NEW: 30-second grace period for startup

class StreamHandler:
    def __init__(self):
        self.sessions = {}  # {session_id: StreamSession}
        self.lock = threading.Lock()
        
        # Ensure base temp dir exists. Sessions live in memory only, so any folder left
        # from an earlier run (crash, restart) belongs to no one: clear them out.
        self.stream_dir = os.path.join(CACHE_DIR, 'hls_temp')
        shutil.rmtree(self.stream_dir, ignore_errors=True)
        os.makedirs(self.stream_dir, exist_ok=True)
            
        # Start background cleaner
        self.cleaning = True
        self.cleaner_thread = threading.Thread(target=self._monitor_sessions, daemon=True)
        self.cleaner_thread.start()

    # Codecs that Intel VAAPI can hardware-decode reliably
    VAAPI_DECODE_CODECS = {'h264', 'avc', 'hevc', 'h265', 'vp9', 'av1', 'mpeg2video'}
    
    # Codecs that are already browser-compatible and can be stream-copied
    COPY_CODECS = {'h264', 'avc'}

    def _codec_supports_vaapi(self, codec_info: str) -> bool:
        """Check if the source codec can be hardware-decoded via VAAPI."""
        c = codec_info.lower()
        return any(k in c for k in self.VAAPI_DECODE_CODECS)

    def _is_copy_codec(self, codec_info: str) -> bool:
        """Check if the video stream can be stream-copied without re-encoding."""
        c = codec_info.lower()
        return any(k in c for k in self.COPY_CODECS)

    def start_session(self, path, codec_info, start_offset=0):
        """
        Starts an ffmpeg process to stream the file via HLS.

        Strategy:
          1. H.264 source          → stream-copy video (zero CPU/GPU, perfect quality)
          2. HEVC/VP9/AV1/MPEG-2   → VAAPI hardware decode + h264_vaapi hardware encode
          3. Everything else (AVI/
             MPEG-4/WMV/unknown)   → software decode (libavcodec) + h264_vaapi encode
                                     if VAAPI encoder available, else libx264 software

        The key fix for AVI: never use -hwaccel_output_format vaapi for codecs that
        VAAPI cannot decode — the pipeline breaks because scale_vaapi expects GPU frames
        but gets CPU frames.  Software-decoded frames are uploaded to VAAPI explicitly
        via the 'format=vaapi,hwupload' filter chain instead.
        """
        session_id = str(uuid.uuid4())
        
        # Kill any existing sessions for this path to prevent GPU resource exhaustion
        # BUT don't kill the session we're about to create
        killed_any = False
        with self.lock:
            for sid, sess in list(self.sessions.items()):
                # Skip if this is somehow our new session (shouldn't happen but be safe)
                if sid == session_id:
                    continue
                    
                if sess.source_path == path:
                    # Found old session for same file - kill it immediately
                    try:
                        if sess.process.poll() is None:
                            try:
                                os.killpg(os.getpgid(sess.process.pid), signal.SIGKILL)
                                log_ffmpeg(f"Killed old session {sid} for {os.path.basename(path)}")
                                killed_any = True
                            except ProcessLookupError:
                                # Process already dead
                                log_ffmpeg(f"Process for session {sid} already terminated")
                            except Exception as kill_err:
                                log_ffmpeg(f"Error sending kill signal: {kill_err}")
                                # Try regular kill as fallback
                                sess.process.kill()
                                killed_any = True
                    except Exception as e:
                        log_ffmpeg(f"Error killing old session: {e}")
                    # Remove from tracking
                    del self.sessions[sid]
        
        # If we killed a session, wait for GPU to free up
        if killed_any:
            log_ffmpeg("Waiting 500ms for GPU cleanup after kill...")
            time.sleep(0.5)
        
        output_dir = os.path.join(self.stream_dir, session_id)
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # ── Build FFmpeg command ──────────────────────────────────────────────
        #
        # Three paths:
        #  1. H.264 source     → stream-copy (no encode, zero cost)
        #  2. HEVC/VP9/AV1/
        #     MPEG-2 source    → VAAPI hw-decode + h264_vaapi hw-encode
        #  3. Everything else  → software decode + h264_vaapi hw-encode
        #                        (CPU decode → hwupload → GPU encode)
        #  4. No usable GPU    → software decode + libx264 encode
        #
        # jellyfin-ffmpeg bundles its own VA drivers in /usr/lib/jellyfin-ffmpeg/lib/dri;
        # LIBVA_DRIVERS_PATH points there (transcoder.ENV).

        # Environment for the ffmpeg subprocess: the same as the new player's (jellyfin-ffmpeg's
        # VA drivers; libva picks the driver for the GPU unless LIBVA_DRIVER_NAME is set)
        import transcoder
        ffmpeg_env = dict(transcoder.ENV)
        dri = transcoder.DRI
        hw = transcoder.HW.get('vaapi', False)     # checked at start-up; False = no usable GPU

        can_copy         = self._is_copy_codec(codec_info)
        can_vaapi_decode = hw and self._codec_supports_vaapi(codec_info)

        if start_offset <= 0:
            pre_seek_args  = []
            post_seek_args = []
        else:
            fast_seek      = max(0.0, start_offset - 5)
            precise_seek   = start_offset - fast_seek
            pre_seek_args  = ['-ss', str(fast_seek)]
            post_seek_args = ['-ss', str(precise_seek)]

        if can_copy:
            # Stream-copy: no encode at all, just remux into HLS segments (no GPU needed)
            cmd = ['ffmpeg'] + pre_seek_args + ['-i', path] + post_seek_args
            cmd += ['-c:v', 'copy', '-avoid_negative_ts', 'make_zero']

        elif can_vaapi_decode:
            # Full GPU pipeline: VAAPI decode → scale_vaapi → h264_vaapi encode
            # Frames stay on GPU the entire time — minimum CPU involvement
            cmd = (
                ['ffmpeg',
                 '-init_hw_device',        f'vaapi=va:{dri}',
                 '-filter_hw_device',      'va',
                 '-hwaccel',               'vaapi',
                 '-hwaccel_device',        'va',
                 '-hwaccel_output_format', 'vaapi']
                + pre_seek_args + ['-i', path] + post_seek_args
            )
            cmd += [
                '-vf',      'scale_vaapi=format=nv12',
                '-c:v',     'h264_vaapi',
                '-b:v',     '8M',
                '-maxrate', '10M',
                '-g',       '48',
                '-quality', '4',
            ]

        elif not hw:
            # No usable GPU: software decode + libx264 encode
            cmd = ['ffmpeg'] + pre_seek_args + ['-i', path] + post_seek_args
            cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p', '-g', '48']

        else:
            # Software decode (MPEG-4, WMV, etc) → GPU encode
            # CPU decodes, hwupload transfers frames to GPU, h264_vaapi encodes
            cmd = (
                ['ffmpeg',
                 '-init_hw_device', f'vaapi=va:{dri}',
                 '-filter_hw_device', 'va']
                + pre_seek_args + ['-i', path] + post_seek_args
            )
            cmd += [
                '-vf',      'format=nv12,hwupload,scale_vaapi=format=nv12',
                '-c:v',     'h264_vaapi',
                '-b:v',     '8M',
                '-maxrate', '10M',
                '-g',       '48',
                '-quality', '4',
            ]

        # Audio: always AAC stereo (AC3/DTS/MP3 not reliable in HLS)
        cmd += ['-c:a', 'aac', '-ac', '2', '-b:a', '128k']

        # HLS muxer
        playlist_path = os.path.join(output_dir, 'playlist.m3u8')
        segment_path  = os.path.join(output_dir, 'seg_%03d.ts')

        cmd += [
            '-f',                    'hls',
            '-hls_time',             '2',
            '-hls_list_size',        '0',
            '-hls_segment_filename', segment_path,
            '-start_number',         '0',
            playlist_path,
        ]

        mode = 'copy' if can_copy else ('vaapi-full' if can_vaapi_decode else ('sw+vaapi-enc' if hw else 'software'))
        log_ffmpeg(f"Codec: {codec_info!r}  mode={mode}")

        # Start Process with logging
        # We hide the window on Windows
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            # Log FFmpeg command
            log_ffmpeg(f"Starting session {session_id}")
            log_ffmpeg(f"Source: {path}")
            log_ffmpeg(f"Start offset: {start_offset}s")
            log_ffmpeg_debug(f"Command: {' '.join(cmd)}")
            
            # Own process group: killpg() below must hit ffmpeg only, never the server's group.
            # stdout isn't read, so it goes nowhere (a full pipe would stall ffmpeg).
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=ffmpeg_env,
                startupinfo=startupinfo,
                start_new_session=(os.name != 'nt')
            )
            
            log_ffmpeg(f"FFmpeg process started: PID {process.pid}")

            # Background thread: drain stderr and log it so crashes are visible.
            # Filter out the high-frequency segment/playlist writing lines —
            # at 11× speed these flood the log with hundreds of lines per second.
            def _log_stderr(proc, sid):
                try:
                    for raw in proc.stderr:
                        line = raw.decode('utf-8', errors='replace').rstrip()
                        if not line:
                            continue
                        # Skip per-segment file-open noise
                        if 'Opening' in line and ('.ts' in line or '.m3u8' in line):
                            continue
                        # Log progress lines (fps/speed) and errors
                        log_ffmpeg_debug(f"[{sid[:8]}] {line}")
                except Exception:
                    pass
                rc = proc.wait()
                if rc not in (0, -9, -15):   # ignore clean exit / SIGKILL / SIGTERM
                    log_error(f"FFmpeg [{sid[:8]}] exited with code {rc}")

            t = threading.Thread(target=_log_stderr, args=(process, session_id), daemon=True)
            t.start()
            
            with self.lock:
                self.sessions[session_id] = StreamSession(
                    session_id, process, output_dir, path, start_offset
                )
            
            return session_id
        except Exception as e:
            print(f"Failed to start ffmpeg: {e}")
            # Cleanup dir if failed
            if os.path.exists(output_dir):
                shutil.rmtree(output_dir, ignore_errors=True)
            return None

    def stop_session(self, session_id):
        """Stops the FFmpeg process and removes temp files."""
        log_ffmpeg(f"Stopping session {session_id}")
        
        with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                log_ffmpeg(f"Session {session_id} not found")
                return

            # Kill Process
            if session.process.poll() is None:
                try:
                    log_ffmpeg(f"Terminating FFmpeg process PID {session.process.pid}")
                    # Try nice terminate first
                    session.process.terminate()
                    # Give it a moment, then force kill if needed
                    try:
                        session.process.wait(timeout=2)
                        log_ffmpeg(f"FFmpeg process {session.process.pid} terminated gracefully")
                    except subprocess.TimeoutExpired:
                        log_ffmpeg(f"FFmpeg process {session.process.pid} did not terminate, force killing")
                        session.process.kill()
                        log_ffmpeg(f"FFmpeg process {session.process.pid} killed")
                except Exception as e:
                    log_error(f"Error killing process {session_id}: {e}")
            else:
                log_ffmpeg(f"FFmpeg process already stopped (exit code: {session.process.poll()})")

            # Remove Files
            if os.path.exists(session.output_dir):
                try:
                    shutil.rmtree(session.output_dir, ignore_errors=True)
                    log_ffmpeg_debug(f"Removed temp directory: {session.output_dir}")
                except Exception as e:
                    log_error(f"Error removing temp dir {session.output_dir}: {e}")

            del self.sessions[session_id]
            log_ffmpeg(f"Session {session_id} cleaned up")

    def cleanup_all_sessions(self):
        """Stop every session (used by /api/stream/cleanup-all)."""
        with self.lock:
            ids = list(self.sessions)
        for sid in ids:
            self.stop_session(sid)
        log_ffmpeg(f"Stopped all sessions ({len(ids)})")

    def get_playlist_path(self, session_id):
        self._touch_session(session_id)
        with self.lock:
            session = self.sessions.get(session_id)
            if not session: return None
            return os.path.join(session.output_dir, 'playlist.m3u8')

    def get_segment_path(self, session_id, segment_name):
        self._touch_session(session_id)
        with self.lock:
            session = self.sessions.get(session_id)
            if not session: return None
            return os.path.join(session.output_dir, segment_name)

    def _touch_session(self, session_id):
        """Updates last access time for timeout logic."""
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id].last_access = time.time()

    def _monitor_sessions(self):
        """Background thread to kill inactive sessions."""
        TIMEOUT = 20              # Kill after 20 s of inactivity (tab closed / navigated away)
        STARTUP_GRACE_PERIOD = 15 # Don't timeout brand-new sessions still buffering
        
        while self.cleaning:
            time.sleep(5)
            now = time.time()
            ids_to_remove = []
            
            with self.lock:
                for sid, session in self.sessions.items():
                    session_age = now - session.created_at

                    # Give new sessions time to produce their first segments
                    if session_age < STARTUP_GRACE_PERIOD:
                        continue

                    # Inactivity timeout — browser closed or navigated away
                    if now - session.last_access > TIMEOUT:
                        ids_to_remove.append(sid)
                        continue

                    # ffmpeg finished naturally (EOF) — clean up promptly
                    if session.process.poll() is not None and session_age > 10:
                        ids_to_remove.append(sid)

            for sid in ids_to_remove:
                log_ffmpeg(f"Session {sid[:8]} timed out or finished. Cleaning up.")
                self.stop_session(sid)

# Singleton instance
stream_manager = StreamHandler()