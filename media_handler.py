# This file is: ./media_handler.py
import os
import subprocess
import hashlib
import ctypes
import json
from config import CACHE_DIR, VIDEO_EXTENSIONS, THUMBNAIL_QUALITY, THUMBNAIL_FRAMES

COVER, COVER_JSON = 'cover.webp', 'cover.json'      # the chosen main thumbnail (a copy of one frame)
FRAMES_JSON = 'frames.json'                          # where the frames were taken: {"start", "times"}

class MediaHandler:
    @staticmethod
    def _hide_path_windows(path):
        """Sets the 'Hidden' and 'System' attributes on Windows."""
        if os.name == 'nt':
            try:
                ctypes.windll.kernel32.SetFileAttributesW(path, 0x06)
            except Exception:
                pass

    @staticmethod
    def is_browser_supported(extension, codec):
        """
        Determines if a file can be Direct Played by most modern browsers.
        Browsers typically support:
        - Container: MP4, WebM, MOV (sometimes)
        - Video Codec: H.264 (AVC), VP8, VP9, AV1, H.265/HEVC (in MP4 container)
        - Audio Codec: AAC, MP3, Vorbis, Opus
        
        Unsupported:
        - Container: MKV, AVI, WMV, FLV (regardless of codec)
        - Codec: MPEG-2, MPEG-4 Visual (legacy codecs)
        
        Note: HEVC/H.265 is supported in Firefox, Chrome (Windows), Safari, and Edge
        when in an MP4/M4V container. MKV+HEVC is still unsupported (container fails first).
        """
        ext = extension.lower().strip()
        cod = codec.lower().strip()
        
        # CRITICAL FIX: Ensure extension has a dot (handles both "mp4" and ".mp4")
        if ext and not ext.startswith('.'):
            ext = '.' + ext
        
        # 1. Check Container first — unsupported containers fail regardless of codec
        supported_containers = ['.mp4', '.m4v', '.webm', '.mov']
        if ext not in supported_containers:
            return False
            
        # 2. Check Codec — only truly legacy/unsupported codecs are rejected
        # HEVC/H.265 is intentionally NOT in this list — it is supported in MP4 containers
        # by Firefox, Chrome (with hardware decoding), Safari, and Edge.
        unsafe_codecs = ['mpeg2video', 'mpeg4', 'msmpeg4', 'wmv']
        if any(bad in cod for bad in unsafe_codecs):
            return False
            
        # Known safe codecs (H.264, HEVC, VP8, VP9, AV1)
        safe_codecs = ['h264', 'avc', 'hevc', 'h265', 'vp8', 'vp9', 'av1']
        if any(safe in cod for safe in safe_codecs):
            return True
            
        # Unknown codec in a supported container — default to False to trigger HLS as fallback
        return False

    @staticmethod
    def get_extended_metadata(path):
        """
        Returns a dict with: duration, bit_rate, width, height, codec, extension
        using ffprobe JSON output.
        """
        try:
            cmd = [
                'ffprobe', '-v', 'quiet', 
                '-print_format', 'json', 
                '-show_format', '-show_streams', 
                path
            ]
            
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            output = subprocess.check_output(cmd, startupinfo=startupinfo).decode().strip()
            data = json.loads(output)
            
            result = {
                "duration": 0.0,
                "bit_rate": 0.0,
                "width": 0,
                "height": 0,
                "codec": "",
                "extension": os.path.splitext(path)[1].replace('.', '').lower()
            }

            # 1. Format Info
            fmt = data.get('format', {})
            try: result['duration'] = float(fmt.get('duration', 0))
            except: pass
            
            try: result['bit_rate'] = float(fmt.get('bit_rate', 0))
            except: pass
            
            # 2. Stream Info (Find first video stream)
            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'video':
                    result['width'] = int(stream.get('width', 0))
                    result['height'] = int(stream.get('height', 0))
                    result['codec'] = stream.get('codec_name', 'unknown')
                    break
            
            return result
        except Exception as e:
            # print(f"Metadata error for {path}: {e}")
            return {
                "duration": 0, "bit_rate": 0, "width": 0, "height": 0, 
                "codec": "", "extension": os.path.splitext(path)[1].replace('.', '').lower()
            }

    @staticmethod
    def resolution_label(width, height):
        """1080p, 720p, 4K...: the short side, so a vertical 1080x1920 is 1080p."""
        if height > 0 and width > 0:
            return "4K" if max(width, height) >= 3800 else f"{min(width, height)}p"
        if height > 0:
            return f"{height}p"
        return f"{width}w" if width > 0 else ""

    @staticmethod
    def get_file_info(path):
        """Returns formatted strings and raw data dict."""
        try:
            size_bytes = os.path.getsize(path)
            meta = MediaHandler.get_extended_metadata(path)
            
            # Size String
            if size_bytes >= 1024**3: size_str = f"{size_bytes / (1024**3):.2f} GB"
            else: size_str = f"{size_bytes / (1024**2):.2f} MB"
            
            # Length String
            duration = meta['duration']
            if duration > 0:
                m, s = divmod(int(duration), 60)
                h, m = divmod(m, 60)
                length_str = f"{h:02d}:{m:02d}:{s:02d}"
            else:
                length_str = "00:00:00"

            # Bitrate String
            bps = meta['bit_rate']
            if bps > 0:
                bitrate_str = f"{int(bps / 1000):,} kbps"
            elif duration > 0 and size_bytes > 0:
                bitrate_str = f"{int((size_bytes * 8) / duration / 1000):,} kbps"
            else:
                bitrate_str = "0 kbps"
                
            # Resolution String (Shorthand Logic)
            res_str = MediaHandler.resolution_label(meta['width'], meta['height'])

            return {
                "length": length_str,
                "total_bitrate": bitrate_str,
                "file_size": size_str,
                "resolution": res_str,
                "codec": meta['codec'],
                "extension": meta['extension']
            }
            
        except Exception:
            return {}

    @staticmethod
    def _get_cache_key(path, video_id=None, cache_id=None):
        """
        Generate cache key for thumbnails.

        Priority order:
        1. cache_id - explicit, independent cache identifier stored on the
           file_versions row. This is the only way a single video can have
           a unique thumbnail set that is NOT shared with other versions/files
           that happen to share the same video_id (merge group).
        2. filename - legacy fallback. This is the scheme thumbnails were
           actually generated/keyed under for the vast majority of existing
           files (cache_id is NULL for most rows).

        Note: video_id is accepted for API compatibility but is NOT used as
        a fallback key - it was never the actual on-disk storage scheme.

        Args:
            path: File path
            video_id: Unused (kept for call-site compatibility)
            cache_id: Optional explicit cache ID (unique per file_versions row)
        """
        if cache_id:
            # Explicit cache ID - already a hash-like identifier, use directly
            return cache_id
        else:
            # Legacy fallback - filename only (this is the real on-disk scheme
            # for files with no explicit cache_id)
            filename = os.path.basename(path)
            return hashlib.md5(filename.encode('utf-8')).hexdigest()

    @staticmethod
    def thumb_times(duration, num_frames, start=0):
        """(times, start used): num_frames times spread evenly over the part after `start`.
        A start that would leave less than 10% of the video is cut to 90% of the length, and
        no time is later than half a second before the end. A negative start moves the frames
        earlier (the spread reaches back before 0:00), at most until the first frame is at 0:00."""
        n = max(1, int(num_frames))
        start = max(float(start or 0), -duration / n)
        start = min(start, duration * 0.9)
        step = (duration - start) / (n + 1)
        last = max(0.0, duration - 0.5)
        return [max(0.0, min(start + step * i, last)) for i in range(1, n + 1)], start

    @staticmethod
    def generate_thumbnails(path, num_frames=None, force=False, quality=None, video_id=None, cache_id=None, start=0):
        """
        Generate WebP thumbnails for video previews.
        start: skip this many seconds (e.g. a title card); the frames are spread over the rest.
        
        Args:
            path: Video file path (used for actual ffmpeg processing)
            num_frames: Number of frames to generate (default from config)
            force: Force regeneration even if thumbnails exist
            quality: WebP quality 0-100 (default from config)
            video_id: Optional video ID for cache key (maintains cache across moves/merges)
            cache_id: Optional explicit cache ID (takes priority - unique per file_versions row)
        """
        if num_frames is None:
            num_frames = THUMBNAIL_FRAMES
        if quality is None:
            quality = THUMBNAIL_QUALITY
            
        file_hash = MediaHandler._get_cache_key(path, video_id, cache_id)
        output_folder = os.path.join(CACHE_DIR, file_hash)
        
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)
            MediaHandler._hide_path_windows(output_folder)
        
        # Check for existing thumbnails (the frames; not the chosen cover)
        existing = sorted([os.path.join(output_folder, f) for f in os.listdir(output_folder)
                           if f.startswith('thumb_') and f.endswith('.webp')])
        
        if not force and len(existing) >= num_frames:
            return existing
            
        # If forcing or need more frames, delete old thumbnails to save space
        if force or (existing and len(existing) < num_frames):
            for f in os.listdir(output_folder):
                if f.endswith('.dat') or f.endswith('.webp') or f in (COVER_JSON, FRAMES_JSON):  # old JPEG, WebP, the chosen cover
                    try: 
                        os.remove(os.path.join(output_folder, f))
                    except: 
                        pass

        # Quick duration check for thumbs
        meta = MediaHandler.get_extended_metadata(path)
        duration = meta.get('duration', 0)
        
        if duration == 0: 
            return []

        times, _start = MediaHandler.thumb_times(duration, num_frames, start)
        return MediaHandler._write_frames(path, output_folder, times, quality, _start)

    @staticmethod
    def anchor_times(duration, num_frames, at, current=None):
        """(times, k): frame k lands exactly on `at` - k is the current frame nearest it - and the
        others are spread evenly before it (from the current start) and after it (to the end).
        current: the stored {"start", "times"} of the frames, if known."""
        last = max(0.0, duration - 0.5)
        at = max(0.0, min(float(at), last))
        cur = (current or {}).get('times') or []
        start = (current or {}).get('start') or 0.0
        if len(cur) != num_frames:
            cur, start = MediaHandler.thumb_times(duration, num_frames, 0)
        k = min(range(num_frames), key=lambda i: abs(cur[i] - at))
        lo = max(0.0, start) if start < at else 0.0      # a negative start (frames moved earlier) is 0:00 here
        before = [lo + (at - lo) * (i + 1) / (k + 1) for i in range(k)]
        after_n = num_frames - k - 1
        after = [min(at + (duration - at) * (i + 1) / (after_n + 1), last) for i in range(after_n)]
        return before + [at] + after, k

    @staticmethod
    def _write_frames(path, output_folder, times, quality=None, start=0):
        """ffmpeg one WebP frame per time as thumb_<i>.webp (each written aside, then swapped in),
        and note the times in frames.json."""
        if quality is None:
            quality = THUMBNAIL_QUALITY
        generated_files = []
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        for i, timestamp in enumerate(times):
            out_name = os.path.join(output_folder, f"thumb_{i}.webp")
            tmp_name = out_name + '.tmp'

            # Use WebP format with configurable quality
            cmd = [
                'ffmpeg', '-y', '-ss', str(timestamp), '-i', path, 
                '-frames:v', '1', '-vf', 'scale=480:-1',
                '-c:v', 'libwebp', '-quality', str(quality),
                '-f', 'webp',
                tmp_name
            ]
            
            try:
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
                if os.path.exists(tmp_name):
                    os.replace(tmp_name, out_name)
                    generated_files.append(out_name)
                    MediaHandler._hide_path_windows(out_name)
            except Exception as e:
                print(f"Error generating thumb for {path}: {e}")

        try:
            with open(os.path.join(output_folder, FRAMES_JSON), 'w', encoding='utf-8') as f:
                json.dump({'start': round(float(start), 3), 'times': [round(t, 3) for t in times]}, f)
        except OSError:
            pass
        return generated_files

    @staticmethod
    def get_thumbnails(path, video_id=None, cache_id=None):
        file_hash = MediaHandler._get_cache_key(path, video_id, cache_id)
        output_folder = os.path.join(CACHE_DIR, file_hash)
        if os.path.exists(output_folder):
            # Look for .webp files (new format) or fall back to .dat (old format)
            files = [f for f in os.listdir(output_folder)
                     if f.startswith('thumb_') and (f.endswith('.webp') or f.endswith('.dat'))]
            try:
                files.sort(key=lambda f: int(f.split('_')[1].split('.')[0]))
            except:
                files.sort()
            return [os.path.join(output_folder, f) for f in files]
        return []