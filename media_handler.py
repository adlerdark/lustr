# This file is: ./media_handler.py
import os
import subprocess
import hashlib
import ctypes
import json
from config import CACHE_DIR, VIDEO_EXTENSIONS, THUMBNAIL_QUALITY, THUMBNAIL_FRAMES

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
            res_str = ""
            if meta['height'] > 0:
                # Basic shorthand: 1080p, 720p, 480p, etc.
                res_str = f"{meta['height']}p"
                # Optional: Add 4K label if needed, but height is usually preferred for sorting
                if meta['width'] >= 3800: res_str = "4K"
            elif meta['width'] > 0:
                res_str = f"{meta['width']}w"

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
    def generate_thumbnails(path, num_frames=None, force=False, quality=None, video_id=None, cache_id=None):
        """
        Generate WebP thumbnails for video previews.
        
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
        
        # Check for existing thumbnails (look for .webp files now)
        existing = sorted([os.path.join(output_folder, f) for f in os.listdir(output_folder) if f.endswith('.webp')])
        
        if not force and len(existing) >= num_frames:
            return existing
            
        # If forcing or need more frames, delete old thumbnails to save space
        if force or (existing and len(existing) < num_frames):
            for f in os.listdir(output_folder):
                if f.endswith('.dat') or f.endswith('.webp'):  # Delete both old JPEG and WebP
                    try: 
                        os.remove(os.path.join(output_folder, f))
                    except: 
                        pass

        # Quick duration check for thumbs
        meta = MediaHandler.get_extended_metadata(path)
        duration = meta.get('duration', 0)
        
        if duration == 0: 
            return []

        generated_files = []
        step = 1.0 / (num_frames + 1)
        intervals = [step * i for i in range(1, num_frames + 1)]
        
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        for i, pct in enumerate(intervals):
            timestamp = duration * pct
            out_name = os.path.join(output_folder, f"thumb_{i}.webp")
            
            # Use WebP format with configurable quality
            cmd = [
                'ffmpeg', '-y', '-ss', str(timestamp), '-i', path, 
                '-frames:v', '1', '-vf', 'scale=480:-1',
                '-c:v', 'libwebp', '-quality', str(quality),
                '-f', 'webp',
                out_name
            ]
            
            try:
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
                if os.path.exists(out_name):
                    generated_files.append(out_name)
                    MediaHandler._hide_path_windows(out_name)
            except Exception as e:
                print(f"Error generating thumb for {path}: {e}")

        return generated_files

    @staticmethod
    def get_thumbnails(path, video_id=None, cache_id=None):
        file_hash = MediaHandler._get_cache_key(path, video_id, cache_id)
        output_folder = os.path.join(CACHE_DIR, file_hash)
        if os.path.exists(output_folder):
            # Look for .webp files (new format) or fall back to .dat (old format)
            files = [f for f in os.listdir(output_folder) if f.endswith('.webp') or f.endswith('.dat')]
            try:
                files.sort(key=lambda f: int(f.split('_')[1].split('.')[0]))
            except:
                files.sort()
            return [os.path.join(output_folder, f) for f in files]
        return []