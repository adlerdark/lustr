# This file is: ./database.py

"""
SQLite-based storage for video metadata.
Provides better performance and indexing compared to JSON.
"""
import os
import sqlite3
import json
import threading
from datetime import datetime, timedelta
from config import DATA_FILE, LIST_FIELDS

class Database:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(DATA_FILE), 'library.db')
        
        self.db_path = db_path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self.initialized = False
        self.init_db()
    
    def get_connection(self):
        """Get thread-local database connection."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(self.db_path, timeout=30.0)
            self._local.connection.row_factory = sqlite3.Row
            # Enable foreign keys
            self._local.connection.execute('PRAGMA foreign_keys = ON')
        return self._local.connection
    
    def safe_commit(self, conn, max_retries=5):
        """Commit with retry logic for database locks."""
        import time
        retry_delay = 0.1
        
        for attempt in range(max_retries):
            try:
                conn.commit()
                return True
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    raise
        return False
    
    def init_db(self):
        """Initialize database schema with video-based structure."""
        if self.initialized:
            return
        
        with self._init_lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # ============================================
            # VIDEO-BASED SCHEMA
            # ============================================
            
            # Videos table - one record per logical video
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    filename_base TEXT NOT NULL,
                    library TEXT NOT NULL,
                    title TEXT,
                    year TEXT,
                    rating TEXT,
                    favorite INTEGER DEFAULT 0,
                    date_added TEXT,
                    date_last_edited TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # File versions table - multiple versions per video
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS file_versions (
                    path TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    is_preferred INTEGER DEFAULT 0,
                    resolution TEXT,
                    codec TEXT,
                    extension TEXT,
                    length TEXT,
                    length_seconds INTEGER,
                    total_bitrate TEXT,
                    file_size TEXT,
                    date_created TEXT,
                    date_modified TEXT,
                    cache_id TEXT,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    added_at TIMESTAMP,
                    deleted_at TIMESTAMP,
                    FOREIGN KEY (video_id) REFERENCES videos(video_id) ON DELETE CASCADE
                )
            ''')
            
            # Create indexes for videos table
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_videos_library ON videos(library)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_videos_favorite ON videos(favorite)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_videos_updated_at ON videos(updated_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_videos_filename_base ON videos(filename_base)')
            
            # Create indexes for file_versions table
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_versions_video_id ON file_versions(video_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_versions_preferred ON file_versions(is_preferred)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_versions_last_seen ON file_versions(last_seen)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_versions_deleted ON file_versions(deleted_at)')
            # Note: idx_file_versions_length_seconds is created in migration

            # ============================================
            # CONFIGURATION TABLES
            # ============================================

            # Metadata table for storing configuration (libraries, settings, etc.)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Favorite cast members table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS favorite_cast (
                    name TEXT PRIMARY KEY,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Cast photos table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cast_photos (
                    cast_name TEXT PRIMARY KEY,
                    photo_data BLOB,
                    photo_mime_type TEXT,
                    thumbnail_data BLOB,
                    source_type TEXT,
                    source_url TEXT,
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # ============================================
            # JUNCTION TABLES FOR LIST FIELDS
            # ============================================
            
            # ALL attributes are video-level since different file versions
            # represent the same video content with different encodings
            # (codec, bitrate, resolution may differ but content is identical)
            video_level_fields = [
                'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 
                'ass', 'face', 'cock', 'outfit', 'tags',
                'orientation', 'site', 'theme', 'feature', 'cumshot', 'participants'
            ]
            
            # Create video-level junction tables
            for field in video_level_fields:
                field_lower = field.lower()
                table_name = f'video_{field_lower}'
                junction_table = f'video_{field_lower}_junction'
                
                # Value table (stores unique values)
                cursor.execute(f'''
                    CREATE TABLE IF NOT EXISTS {table_name} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        value TEXT UNIQUE NOT NULL
                    )
                ''')
                
                # Junction table (links video_id to values)
                cursor.execute(f'''
                    CREATE TABLE IF NOT EXISTS {junction_table} (
                        video_id TEXT NOT NULL,
                        {field_lower}_id INTEGER NOT NULL,
                        PRIMARY KEY (video_id, {field_lower}_id),
                        FOREIGN KEY (video_id) REFERENCES videos(video_id) ON DELETE CASCADE,
                        FOREIGN KEY ({field_lower}_id) REFERENCES {table_name}(id) ON DELETE CASCADE
                    )
                ''')
                
                # Create index for faster lookups
                cursor.execute(f'CREATE INDEX IF NOT EXISTS idx_{junction_table}_video ON {junction_table}(video_id)')
            
            # ============================================
            # PLAYLISTS AND WATCHLIST
            # ============================================
            
            # Playlists table - stores playlist metadata
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    is_watchlist INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create the watchlist if it doesn't exist
            cursor.execute('''
                INSERT OR IGNORE INTO playlists (name, is_watchlist) 
                VALUES ('Watchlist', 1)
            ''')
            
            # Playlist items table - stores videos in playlists with ordering
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS playlist_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    video_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
                    FOREIGN KEY (video_id) REFERENCES videos(video_id) ON DELETE CASCADE,
                    UNIQUE(playlist_id, video_id)
                )
            ''')
            
            # Create indexes for playlists
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_playlist_items_playlist ON playlist_items(playlist_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_playlist_items_video ON playlist_items(video_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_playlist_items_position ON playlist_items(playlist_id, position)')
            
            # Cast aliases - alternative names that resolve to a primary cast name
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cast_aliases (
                    primary_name TEXT NOT NULL,
                    alias_name   TEXT NOT NULL,
                    PRIMARY KEY (primary_name, alias_name)
                )
            ''')
            
            # Cast attributes - appearance tags of a performer (is_primary = applied to their videos)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cast_attributes (
                    cast_name TEXT NOT NULL,
                    attribute_type TEXT NOT NULL,
                    attribute_value TEXT NOT NULL,
                    is_primary INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (cast_name, attribute_type, attribute_value)
                )
            ''')
            
            # Cast gender - permanent gender attribute for cast members
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cast_gender (
                    cast_name TEXT PRIMARY KEY,
                    gender TEXT NOT NULL CHECK(gender IN ('female', 'male', 'trans')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cast_category (
                    cast_name TEXT PRIMARY KEY,
                    category TEXT NOT NULL CHECK(category IN ('pro', 'amateur', 'celeb'))
                        DEFAULT 'pro',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_cast_gender_gender ON cast_gender(gender)')
            
            # File changes tracking - for move detection and replacement suggestions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS file_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    filepath TEXT,
                    video_id TEXT,
                    library TEXT NOT NULL,
                    flag_type TEXT NOT NULL,
                    flagged_at TEXT NOT NULL,
                    file_length INTEGER,
                    file_size INTEGER,
                    codec TEXT,
                    bitrate TEXT,
                    resolution TEXT,
                    related_id INTEGER,
                    UNIQUE(filepath, library),
                    FOREIGN KEY (related_id) REFERENCES file_changes(id) ON DELETE SET NULL
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_changes_filename ON file_changes(filename)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_changes_library ON file_changes(library)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_changes_flag_type ON file_changes(flag_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_changes_video_id ON file_changes(video_id)')
            
            conn.commit()
            self.initialized = True
            
            # Run migrations for existing databases
            self._migrate_add_length_seconds_column()
            cols = {r[1] for r in cursor.execute('PRAGMA table_info(file_versions)').fetchall()}
            if 'added_at' not in cols:
                cursor.execute('ALTER TABLE file_versions ADD COLUMN added_at TIMESTAMP')
                conn.commit()
            # Collections (roadmap #9) live in playlists too: kind 'collection' (manual) or 'smart'
            pcols = {r[1] for r in cursor.execute('PRAGMA table_info(playlists)').fetchall()}
            for col, typ in (('kind', "TEXT DEFAULT 'playlist'"), ('rules', 'TEXT'), ('sort', 'TEXT'), ('description', 'TEXT'),
                             ('cover_video_id', 'TEXT'), ('show_on_home', 'INTEGER DEFAULT 0')):
                if col not in pcols:
                    cursor.execute(f'ALTER TABLE playlists ADD COLUMN {col} {typ}')
            conn.commit()
    
    def _migrate_add_length_seconds_column(self):
        """
        Migration: Add length_seconds column and populate from existing length strings.
        This allows efficient filtering by video duration.
        Safe to run multiple times - checks if column exists first.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Check if column already exists
        cursor.execute("PRAGMA table_info(file_versions)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'length_seconds' in columns:
            # Column exists, check if we need to populate null values
            cursor.execute('''
                SELECT COUNT(*) as count FROM file_versions 
                WHERE length IS NOT NULL AND length != '' AND length_seconds IS NULL
            ''')
            unpopulated_count = cursor.fetchone()[0]
            
            # Ensure index exists even if column was already added
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_versions_length_seconds ON file_versions(length_seconds)')
            
            if unpopulated_count == 0:
                return  # Already migrated
            
            print(f"Populating length_seconds for {unpopulated_count} videos...")
        else:
            # Column doesn't exist, add it
            print("Adding length_seconds column to file_versions table...")
            cursor.execute('ALTER TABLE file_versions ADD COLUMN length_seconds INTEGER')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_versions_length_seconds ON file_versions(length_seconds)')
        
        # Populate length_seconds from length strings
        cursor.execute('''
            SELECT path, length FROM file_versions 
            WHERE length IS NOT NULL AND length != '' AND length_seconds IS NULL
        ''')
        
        rows = cursor.fetchall()
        updated = 0
        
        for row in rows:
            path = row[0]
            length_str = row[1]
            
            try:
                # Parse length string formats:
                # "1:23:45" (H:M:S) -> 5025 seconds
                # "23:45" (M:S) -> 1425 seconds  
                # "45" (S) -> 45 seconds
                # "0:00" -> 0 seconds
                
                parts = length_str.strip().split(':')
                seconds = 0
                
                if len(parts) == 3:  # H:M:S
                    hours, minutes, secs = parts
                    seconds = int(hours) * 3600 + int(minutes) * 60 + int(secs)
                elif len(parts) == 2:  # M:S
                    minutes, secs = parts
                    seconds = int(minutes) * 60 + int(secs)
                elif len(parts) == 1:  # S
                    seconds = int(parts[0])
                
                cursor.execute('''
                    UPDATE file_versions SET length_seconds = ? WHERE path = ?
                ''', (seconds, path))
                updated += 1
                
            except (ValueError, IndexError) as e:
                # Invalid format, skip this entry
                print(f"Warning: Could not parse length '{length_str}' for {path}: {e}")
                continue
        
        if updated > 0:
            conn.commit()
            print(f"✓ Successfully populated length_seconds for {updated} videos")
    
    def add_file(self, path, filename, metadata):
        """
        Add or update a file in the version-aware database.
        
        This method:
        1. Determines the video_id (creates video if doesn't exist)
        2. Creates/updates file_version entry
        3. Updates video-level metadata
        4. Updates junction table entries (cast, hair, outfit, etc.)
        
        Args:
            path: Full path to video file
            filename: Filename (basename of path)
            metadata: Dict containing all metadata fields

        IMPORTANT: video_id is looked up from the existing file_versions row
        first. Only if this path is brand-new (not in file_versions) is the
        video_id computed from the filename. This prevents "Scene 4" in ten
        different folders from all being re-merged into one video_id every
        time add_file is called for a refresh/update operation.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Determine library for this file
        library = self._get_library_for_path(path)
        filename_base = os.path.splitext(filename)[0]

        # --- Resolve video_id ---
        # Priority 1: use whatever video_id the row already has in file_versions.
        # This preserves splits and custom merges; the filename-based fallback
        # is only for genuinely new files being added for the first time.
        cursor.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,))
        existing_version_row = cursor.fetchone()

        if existing_version_row:
            # File already exists in DB — use its real video_id, don't recompute.
            video_id = existing_version_row['video_id']
            version_exists = True
        else:
            # New file — compute video_id from library + filename. If that id is already taken,
            # the new file gets its own id: a same-name file is only joined to an existing video
            # by the scan, after a length check (model.scan_library), and never by overwriting
            # that video's metadata with the new file's blank defaults.
            base_id = f"{library}_{filename_base}".replace(' ', '_').replace('/', '_').replace('\\', '_')
            video_id, n = base_id, 1
            while cursor.execute('SELECT 1 FROM videos WHERE video_id = ?', (video_id,)).fetchone():
                n += 1
                video_id = f"{base_id}_{n}"
            version_exists = False

        # --- Ensure the videos row exists ---
        cursor.execute('SELECT 1 FROM videos WHERE video_id = ?', (video_id,))
        video_exists = cursor.fetchone() is not None
        
        if not video_exists:
            # Create new video entry
            self.create_video(video_id, filename_base, library, metadata)
        else:
            # Update existing video metadata
            self._update_video_metadata(video_id, metadata)
        
        # --- Create or update the file_versions row ---
        if version_exists:
            # Update existing file version (codec, bitrate, resolution, etc.)
            self._update_file_version(path, metadata)
        else:
            # Add new file version
            self.add_file_version(video_id, path, filename, metadata)
            
            # Ensure at least one version is preferred
            cursor.execute('''
                SELECT COUNT(*) as preferred_count 
                FROM file_versions \
                WHERE video_id = ? AND is_preferred = 1 AND deleted_at IS NULL
            ''', (video_id,))
            preferred_count = cursor.fetchone()['preferred_count']
            
            if preferred_count == 0:
                # No preferred version - make this one preferred
                cursor.execute('UPDATE file_versions SET is_preferred = 1 WHERE path = ?', (path,))
        
        # Update junction tables for fields like cast, hair, outfit, tags, etc.
        self._update_junction_tables(path, metadata)
        
        conn.commit()
    
    def _update_video_metadata(self, video_id, metadata):
        """Update video-level metadata fields (non-list fields only)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Fields that live in videos table (non-list fields only)
        # List fields (orientation, site, theme, etc.) are now in junction tables
        video_fields = ['title', 'year', 'rating', 'favorite']
        
        updates = []
        values = []
        
        for field in video_fields:
            if field in metadata:
                val = metadata[field]
                
                # Handle boolean
                if isinstance(val, bool):
                    val = 1 if val else 0
                
                updates.append(f"{field} = ?")
                values.append(val)
        
        # Add date_last_edited
        if 'date_last_edited' in metadata:
            updates.append("date_last_edited = ?")
            values.append(metadata['date_last_edited'])
        
        # Add updated_at
        updates.append("updated_at = ?")
        values.append(datetime.now().isoformat())
        
        if updates:
            query = f"UPDATE videos SET {', '.join(updates)} WHERE video_id = ?"
            values.append(video_id)
            cursor.execute(query, values)
    
    def _update_file_version(self, path, metadata):
        """Update file version entry with file-specific metadata."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Fields that live in file_versions table
        version_fields = [
            'resolution', 'codec', 'extension', 'length', 'total_bitrate',
            'file_size', 'date_created', 'date_modified', 'cache_id'
        ]
        
        updates = []
        values = []
        
        for field in version_fields:
            if field in metadata:
                val = metadata[field]
                
                # Convert to string if not None
                if val is not None:
                    val = str(val)
                
                updates.append(f"{field} = ?")
                values.append(val)
        
        # If length was updated, also update length_seconds
        if 'length' in metadata:
            length_seconds = self._parse_length_to_seconds(metadata['length'])
            updates.append("length_seconds = ?")
            values.append(length_seconds)
        
        if updates:
            query = f"UPDATE file_versions SET {', '.join(updates)} WHERE path = ?"
            values.append(path)
            cursor.execute(query, values)
    
    def _update_junction_tables(self, path, metadata):
        """Update junction table entries for all list fields.
        
        ALL list fields are now video-level since different file versions
        represent the same video content with different encodings.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get video_id for this path
        cursor.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,))
        row = cursor.fetchone()
        if not row:
            return
        
        video_id = row['video_id']
        
        # ALL junction fields are now video-level (use video_id as key)
        # Files with same content but different encodings share all metadata
        video_junction_fields = [
            'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 
            'ass', 'face', 'cock', 'outfit', 'tags',
            'orientation', 'site', 'theme', 'feature', 'cumshot', 'participants'
        ]
        
        # Update video-level junction tables
        for field in video_junction_fields:
            field_lower = field.lower()
            
            if field_lower not in metadata:
                continue
            
            values = metadata[field_lower]
            
            # Convert to list if needed
            if isinstance(values, str):
                values = [v.strip() for v in values.split(',') if v.strip()]
            elif not isinstance(values, list):
                values = []
            
            table_name = f'video_{field_lower}'
            junction_table = f'video_{field_lower}_junction'
            
            # Delete existing entries for this video
            cursor.execute(f'DELETE FROM {junction_table} WHERE video_id = ?', (video_id,))
            
            # Insert new entries
            for value in values:
                if not value:
                    continue
                
                # Get or create value ID
                cursor.execute(f'SELECT id FROM {table_name} WHERE value = ?', (value,))
                row = cursor.fetchone()
                
                if row:
                    value_id = row['id']
                else:
                    cursor.execute(f'INSERT INTO {table_name} (value) VALUES (?)', (value,))
                    value_id = cursor.lastrowid
                
                # Create junction entry
                cursor.execute(
                    f'INSERT INTO {junction_table} (video_id, {field_lower}_id) VALUES (?, ?)',
                    (video_id, value_id)
                )
    
    def mark_missing_files(self, library_paths=None, found=None):
        """
        Mark missing files as deleted (soft delete) during scan.
        These will be permanently deleted at end of scan if not recovered.
        
        Args:
            library_paths: Optional list of library root paths to restrict scan to.
                          If None, checks all files (for backward compatibility).
            found: Optional set of paths the scan's folder walk just saw; those are known to
                   exist and skip the per-file existence check (anything else is still checked).
        
        Returns:
            List of paths that were marked as deleted
        """
        import os
        from datetime import datetime
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get all non-deleted file versions
        cursor.execute('''
            SELECT path, video_id, is_preferred 
            FROM file_versions 
            WHERE deleted_at IS NULL
        ''')
        all_versions = cursor.fetchall()
        
        marked_paths = []
        videos_to_check = set()
        now = datetime.now().isoformat()
        
        # Mark missing files as deleted
        for version in all_versions:
            # If library_paths specified, only check files in those paths
            if library_paths:
                path_in_library = False
                normalized_path = os.path.normpath(version['path'])
                for lib_path in library_paths:
                    normalized_lib = os.path.normpath(lib_path)
                    if normalized_path.startswith(normalized_lib + os.sep):
                        path_in_library = True
                        break
                
                if not path_in_library:
                    continue  # Skip files not in the libraries being scanned
            
            if found is not None and os.path.normpath(version['path']) in found:
                continue
            if not os.path.exists(version['path']):
                marked_paths.append(version['path'])
                videos_to_check.add(version['video_id'])
                
                # Soft delete the file version
                cursor.execute('''
                    UPDATE file_versions 
                    SET deleted_at = ? 
                    WHERE path = ?
                ''', (now, version['path']))
        
        # For each affected video, check if it needs cleanup
        for video_id in videos_to_check:
            cursor.execute('''
                SELECT COUNT(*) as count 
                FROM file_versions 
                WHERE video_id = ? AND deleted_at IS NULL
            ''', (video_id,))
            
            active_count = cursor.fetchone()['count']
            
            if active_count == 0:
                # No active versions left - video will be cleaned up at end of scan
                pass
                
            elif active_count == 1:
                # Only one active version left - make sure it's preferred
                cursor.execute('''
                    UPDATE file_versions 
                    SET is_preferred = 1 
                    WHERE video_id = ? AND deleted_at IS NULL
                ''', (video_id,))
            else:
                # Multiple active versions - ensure one is preferred
                cursor.execute('''
                    SELECT COUNT(*) as count 
                    FROM file_versions 
                    WHERE video_id = ? AND is_preferred = 1 AND deleted_at IS NULL
                ''', (video_id,))
                
                pref_count = cursor.fetchone()['count']
                if pref_count == 0:
                    # No preferred - set first active as preferred
                    cursor.execute('''
                        UPDATE file_versions 
                        SET is_preferred = 1 
                        WHERE video_id = ? AND deleted_at IS NULL
                        ORDER BY path LIMIT 1
                    ''', (video_id,))
        
        conn.commit()
        return marked_paths
    
    def get_file(self, path):
        """Get a single file record with all metadata, including video-level metadata."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get file version data (active files only)
        cursor.execute('''
            SELECT fv.path, fv.filename, v.title, v.year, v.rating, v.favorite, fv.resolution, fv.codec,
                   fv.extension, fv.length, fv.total_bitrate, fv.file_size, fv.date_created, fv.date_modified,
                   v.date_added, v.date_last_edited, fv.cache_id, v.created_at, v.updated_at, v.video_id
            FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id
            WHERE fv.path = ? AND fv.deleted_at IS NULL
        ''', (path,))
        row = cursor.fetchone()
        
        if not row:
            return None
        
        result = dict(row)
        
        # Get video_id from the file
        video_id = result.get('video_id')
        
        # Merge in video-level metadata if available
        if video_id:
            cursor.execute('''
                SELECT title, year, rating, favorite, date_last_edited
                FROM videos 
                WHERE video_id = ?
            ''', (video_id,))
            
            video_row = cursor.fetchone()
            if video_row:
                video_data = dict(video_row)
                # Merge video metadata into result
                for key, value in video_data.items():
                    if value is not None:
                        result[key] = value
            
            # Fetch ALL list fields from video-level junction tables
            # (all metadata is at video level since files are just different encodings)
            all_list_fields = [
                'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 
                'ass', 'face', 'cock', 'outfit', 'tags',
                'orientation', 'site', 'theme', 'feature', 'cumshot', 'participants'
            ]
            
            for field in all_list_fields:
                field_lower = field.lower()
                table_name = f'video_{field_lower}'
                junction_table = f'video_{field_lower}_junction'
                
                cursor.execute(f'''
                    SELECT {table_name}.value FROM {table_name}
                    JOIN {junction_table} ON {table_name}.id = {junction_table}.{field_lower}_id
                    WHERE {junction_table}.video_id = ?
                    ORDER BY {table_name}.value
                ''', (video_id,))
                
                rows = cursor.fetchall()
                result[field_lower] = [row[0] for row in rows]
        
        return result
    
    # Bulk version of get_file: the same record shape, in ~20 queries for any number of files
    FILE_SCALAR_COLUMNS = '''fv.path, fv.filename, v.title, v.year, v.rating, v.favorite, fv.resolution, fv.codec,
                   fv.extension, fv.length, fv.total_bitrate, fv.file_size, fv.date_created, fv.date_modified,
                   v.date_added, v.date_last_edited, fv.cache_id, v.created_at, v.updated_at, v.video_id'''
    FILE_LIST_FIELDS = ('cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face', 'cock', 'outfit',
                        'tags', 'orientation', 'site', 'theme', 'feature', 'cumshot', 'participants')

    def list_values_by_video(self, field, video_ids=None):
        """{video_id: [values sorted as in get_file]} for one list field (all videos, or these)."""
        cursor = self.get_connection().cursor()
        sql = (f'SELECT j.video_id, t.value FROM video_{field} t JOIN video_{field}_junction j ON t.id = j.{field}_id')
        out = {}
        if video_ids is None:
            batches = [None]
        else:
            ids = list(video_ids)
            batches = [ids[i:i + 900] for i in range(0, len(ids), 900)]
        for batch in batches:
            if batch is None:
                cursor.execute(sql + ' ORDER BY t.value')
            elif batch:
                cursor.execute(sql + f' WHERE j.video_id IN ({",".join("?" * len(batch))}) ORDER BY t.value', batch)
            else:
                continue
            for vid, value in cursor.fetchall():
                out.setdefault(vid, []).append(value)
        if video_ids is not None and len(batches) > 1:      # batches are each sorted; keep the overall order
            for vid in out:
                out[vid].sort()
        return out

    def get_files_bulk(self, paths=None, list_fields=None):
        """{path: record} exactly like get_file(path) for each active file (or for these paths).
        list_fields: which list fields to fill (default: all, as get_file)."""
        cursor = self.get_connection().cursor()
        base = f'SELECT {self.FILE_SCALAR_COLUMNS} FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id WHERE fv.deleted_at IS NULL'
        rows = []
        if paths is not None and len(paths) > 2000:       # most of the library: one pass is faster than batches
            wanted = set(paths)
            cursor.execute(base)
            rows = [r for r in cursor.fetchall() if r['path'] in wanted]
        elif paths is None:
            cursor.execute(base)
            rows = cursor.fetchall()
        else:
            ps = list(paths)
            for i in range(0, len(ps), 900):
                batch = ps[i:i + 900]
                cursor.execute(base + f' AND fv.path IN ({",".join("?" * len(batch))})', batch)
                rows += cursor.fetchall()
        records = {r['path']: dict(r) for r in rows}
        vids = None if paths is None or len(records) > 2000 else {r['video_id'] for r in records.values()}
        for field in (self.FILE_LIST_FIELDS if list_fields is None else list_fields):
            values = self.list_values_by_video(field, vids)
            for rec in records.values():
                rec[field] = list(values.get(rec['video_id'], []))
        return records

    def get_all_tags(self, field_name=None):
        """Values of the list fields with the number of videos (active files only) using each.
        With field_name: that field's values. Without: every field except cast and site,
        lowercased and merged, leaving out values that are also sites."""
        conn = self.get_connection()
        cursor = conn.cursor()
        active = 'SELECT video_id FROM file_versions WHERE deleted_at IS NULL'

        def field_counts(field_lower):
            try:
                cursor.execute(f'''
                    SELECT t.value, COUNT(DISTINCT j.video_id)
                    FROM video_{field_lower} t
                    JOIN video_{field_lower}_junction j ON j.{field_lower}_id = t.id
                    WHERE j.video_id IN ({active})
                    GROUP BY t.id, t.value
                ''')
                return cursor.fetchall()
            except sqlite3.OperationalError:
                return []                               # field table not created yet

        if field_name:
            rows = field_counts(field_name.lower())
            return {v: n for v, n in sorted(rows, key=lambda r: (-r[1], str(r[0])))}

        site_values = {str(v).strip().lower() for v, _n in field_counts('site') if v}
        tag_videos = {}                                  # lowercase value -> set of video ids
        for field in LIST_FIELDS:
            field_lower = field.lower()
            if field_lower in ('cast', 'site'):
                continue
            try:
                cursor.execute(f'''
                    SELECT t.value, j.video_id
                    FROM video_{field_lower} t
                    JOIN video_{field_lower}_junction j ON j.{field_lower}_id = t.id
                    WHERE j.video_id IN ({active})
                ''')
            except sqlite3.OperationalError:
                continue
            for value, video_id in cursor.fetchall():
                key = str(value).strip().lower() if value else ''
                if key and key not in site_values:
                    tag_videos.setdefault(key, set()).add(video_id)
        return {tag: len(v) for tag, v in tag_videos.items()}

    def get_all_sites(self):
        """Every site value (lowercased, variants merged) with the number of videos using it."""
        conn = self.get_connection()
        cursor = conn.cursor()
        site_videos = {}
        try:
            cursor.execute('''
                SELECT t.value, j.video_id
                FROM video_site t
                JOIN video_site_junction j ON j.site_id = t.id
                WHERE j.video_id IN (SELECT video_id FROM file_versions WHERE deleted_at IS NULL)
            ''')
            for value, video_id in cursor.fetchall():
                key = str(value).strip().lower() if value else ''
                if key:
                    site_videos.setdefault(key, set()).add(video_id)
        except sqlite3.OperationalError:
            pass
        return {site: len(v) for site, v in site_videos.items()}

    def get_ui_config(self):
        """Get UI configuration (stored as special _ui_config entry)."""
        # UI config is stored in the JSON file, not in SQLite
        # Return empty dict as SQLite doesn't store this
        return {}
    
    def set_ui_config(self, config):
        """Set UI configuration (no-op for SQLite, stored in JSON)."""
        # UI config is stored in the JSON file, not in SQLite
        pass
    
    def migrate_from_json(self, json_path):
        """Migrate data from existing JSON file to SQLite."""
        if not os.path.exists(json_path):
            return 0
        
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            files_data = data.get('files', {})
            count = 0
            
            for path, metadata in files_data.items():
                if path == '_ui_config':
                    continue
                
                filename = os.path.basename(path)
                self.add_file(path, filename, metadata)
                count += 1
            
            print(f"Migrated {count} files from JSON to SQLite")
            return count
        except Exception as e:
            print(f"Migration error: {e}")
            return 0
    
    def export_to_csv(self, csv_path):
        """
        Export all videos to CSV format (one row per video, using preferred version data).
        This is the default export format.
        List fields use pipe (|) separator to match existing format.
        All list fields are video-level since files are just different encodings.
        """
        import csv
        
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Get all videos with their preferred versions
            cursor.execute('''
                SELECT 
                    v.video_id,
                    v.filename_base,
                    v.library,
                    v.title,
                    v.year,
                    v.rating,
                    v.favorite,
                    v.date_added,
                    v.date_last_edited,
                    fv.path,
                    fv.filename,
                    fv.resolution,
                    fv.codec,
                    fv.extension,
                    fv.length,
                    fv.total_bitrate,
                    fv.file_size,
                    fv.cache_id
                FROM videos v
                LEFT JOIN file_versions fv ON v.video_id = fv.video_id AND fv.is_preferred = 1
                ORDER BY v.library, v.filename_base
            ''')
            
            videos = cursor.fetchall()
            
            if not videos:
                return False
            
            # Get all junction table data for each video
            video_data_list = []
            
            # All list fields are video-level now
            all_list_fields = [
                'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 
                'ass', 'face', 'cock', 'outfit', 'tags',
                'orientation', 'site', 'theme', 'feature', 'cumshot', 'participants'
            ]
            
            for video in videos:
                video_dict = dict(video)
                video_id = video_dict['video_id']
                
                # Get all list fields from video-level junction tables
                for field in all_list_fields:
                    cursor.execute(f'''
                        SELECT vt.value
                        FROM video_{field}_junction vj
                        JOIN video_{field} vt ON vj.{field}_id = vt.id
                        WHERE vj.video_id = ?
                        ORDER BY vt.value
                    ''', (video_id,))
                    values = [row[0] for row in cursor.fetchall()]
                    video_dict[field] = '|'.join(values) if values else ''
                
                # Convert favorite to string
                video_dict['favorite'] = 'true' if video_dict.get('favorite') else 'false'
                
                video_data_list.append(video_dict)
            
            # Write to CSV
            fieldnames = [
                'path', 'filename', 'title', 'year', 'rating', 'resolution', 
                'codec', 'extension', 'length', 'total_bitrate', 'file_size',
                'date_created', 'date_modified', 'favorite', 'date_added', 'date_last_edited',
                'cache_id', 'video_id', 'filename_base', 'library',
                'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face', 'cock', 'outfit', 'tags',
                'orientation', 'site', 'theme', 'feature', 'cumshot', 'participants'
            ]
            
            with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(video_data_list)
            
            print(f"Exported {len(video_data_list)} videos to CSV")
            return True
            
        except Exception as e:
            print(f"CSV Export Error: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def export_to_json(self, json_path):
        """
        Export all videos to JSON format (one entry per video with versions array).
        All list fields are video-level since files are just different encodings.
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Get all videos
            cursor.execute('SELECT * FROM videos ORDER BY library, filename_base')
            videos = cursor.fetchall()
            
            export_data = []
            
            # All list fields are video-level now
            all_list_fields = [
                'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 
                'ass', 'face', 'cock', 'outfit', 'tags',
                'orientation', 'site', 'theme', 'feature', 'cumshot', 'participants'
            ]
            
            for video_row in videos:
                video = dict(video_row)
                video_id = video['video_id']
                
                # Get all file versions for this video
                cursor.execute('''
                    SELECT * FROM file_versions 
                    WHERE video_id = ?
                    ORDER BY is_preferred DESC, length DESC
                ''', (video_id,))
                versions = [dict(row) for row in cursor.fetchall()]
                
                # Get all list fields from video-level junction tables
                for field in all_list_fields:
                    cursor.execute(f'''
                        SELECT vt.value
                        FROM video_{field}_junction vj
                        JOIN video_{field} vt ON vj.{field}_id = vt.id
                        WHERE vj.video_id = ?
                        ORDER BY vt.value
                    ''', (video_id,))
                    video[field] = [row[0] for row in cursor.fetchall()]
                
                video['versions'] = versions
                
                # Remove internal fields
                video.pop('created_at', None)
                video.pop('updated_at', None)
                
                export_data.append(video)
            
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
            
            print(f"Exported {len(export_data)} videos to JSON")
            return True
            
        except Exception as e:
            print(f"JSON Export Error: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    
    def import_from_csv(self, csv_path):
        """
        Import metadata from CSV file.
        Matches videos by filename and updates metadata.
        List fields use pipe (|) separator to match existing format.
        """
        import csv
        
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                count = 0
                
                for row in reader:
                    # Get the filename to match
                    filename = row.get('filename') or row.get('filename_base')
                    if not filename:
                        continue
                    
                    # Try to find matching video by filename
                    filename_base = os.path.splitext(filename)[0]
                    cursor.execute('''
                        SELECT v.video_id, fv.path
                        FROM videos v
                        JOIN file_versions fv ON v.video_id = fv.video_id
                        WHERE v.filename_base = ? OR fv.filename = ?
                        LIMIT 1
                    ''', (filename_base, filename))
                    
                    match = cursor.fetchone()
                    if not match:
                        print(f"⚠ Skipping {filename}: not found in database")
                        continue
                    
                    video_id, path = match
                    
                    # Build metadata dict from CSV row
                    metadata = {}
                    
                    # Simple string fields
                    for field in ['title', 'year', 'rating']:
                        if row.get(field):
                            metadata[field] = row[field].strip()
                    
                    # Boolean field
                    if row.get('favorite'):
                        val = row['favorite'].strip().lower()
                        metadata['favorite'] = val in ('1', 'true', 'yes', 'y')
                    
                    # List fields (pipe-separated in CSV)
                    list_fields = [
                        'orientation', 'site', 'theme', 'feature', 'cumshot', 'participants',
                        'cast', 'ethnicity', 'nationality', 'hair', 'body', 'tits', 'ass', 'face', 'cock', 'outfit', 'tags'
                    ]
                    
                    for field in list_fields:
                        if row.get(field) and row[field].strip():
                            # Split by pipe and clean up
                            values = [v.strip() for v in row[field].split('|') if v.strip()]
                            if values:
                                metadata[field] = values
                    
                    # Update the video using add_file (which handles all the junction tables)
                    self.add_file(path, os.path.basename(path), metadata)
                    count += 1
                    
                    if count % 100 == 0:
                        print(f"Imported {count} videos...", flush=True)
                        conn.commit()
                
                conn.commit()
                print(f"\n✓ Import Summary: {count} videos updated")
                return count
                
        except Exception as e:
            print(f"CSV Import Error: {e}")
            import traceback
            traceback.print_exc()
            return 0
    
    def import_from_json_data(self, json_data):
        """
        Import from JSON data (dictionary).
        Used by web_server.py to handle uploaded JSON files.
        
        Supports two formats:
        1. New format: {"/path/to/file.mp4": {metadata with "filename" field}}
        2. Old format: {"files": {"/path/to/file.mp4": {metadata}}}
        
        Matching strategy:
        - First tries exact path match
        - If path doesn't exist, tries to find by filename in database
        
        Args:
            json_data: Dictionary with file metadata
        
        Returns:
            Count of files imported/updated
        """
        try:
            # Handle both old and new export formats
            if 'files' in json_data:
                # Old format: {"files": {...}}
                files_dict = json_data.get('files', {})
            else:
                # New format: just the files directly
                files_dict = json_data
            
            if not isinstance(files_dict, dict):
                return 0
            
            count = 0
            conn = self.get_connection()
            cursor = conn.cursor()
            
            for export_path, metadata in files_dict.items():
                if export_path == '_ui_config':
                    continue
                
                if not isinstance(metadata, dict):
                    continue
                
                # Get filename from metadata (new format) or path (old format)
                if 'filename' in metadata:
                    filename = metadata['filename']
                else:
                    filename = os.path.basename(export_path)
                
                # Try to find the file in database
                # Strategy 1: Try exact path match
                cursor.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL AND path = ?', (export_path,))
                existing = cursor.fetchone()
                
                if existing:
                    # Found exact path match - update it
                    target_path = export_path
                    print(f"✓ Exact match: {filename}", flush=True)
                else:
                    # Strategy 2: Try to find by filename
                    cursor.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL AND path LIKE ?', (f'%/{filename}',))
                    matches = cursor.fetchall()
                    
                    if len(matches) == 1:
                        # Found exactly one file with this filename - use it
                        target_path = matches[0][0]
                        print(f"✓ Filename match: {filename} → {target_path}", flush=True)
                    elif len(matches) > 1:
                        # Multiple files with same filename - skip (ambiguous)
                        print(f"✗ Skipping {filename}: {len(matches)} files with same name found", flush=True)
                        continue
                    else:
                        # File doesn't exist in database - skip
                        print(f"✗ Skipping {filename}: file not found in database", flush=True)
                        continue
                
                # Update the file metadata
                self.add_file(target_path, filename, metadata)
                count += 1
            
            print(f"\n📊 Import Summary: {count} files updated out of {len(files_dict)} in export", flush=True)
            return count
        except Exception as e:
            print(f"JSON Import Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return 0
    
    def close(self):
        """Close database connection."""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None

# Add these methods to your Database class in database.py

    def save_libraries(self, libraries):
        """Save libraries configuration to metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value) 
            VALUES ('libraries', ?)
        ''', (json.dumps(libraries),))
        conn.commit()
    
    def save_library_order(self, library_order):
        """Save library order to metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value) 
            VALUES ('library_order', ?)
        ''', (json.dumps(library_order),))
        conn.commit()
    
    def save_hidden_libraries(self, hidden_libraries):
        """Save hidden libraries list to metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value) 
            VALUES ('hidden_libraries', ?)
        ''', (json.dumps(hidden_libraries),))
        conn.commit()
    
    def save_library_groups(self, library_groups):
        """Save library groups to metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value) 
            VALUES ('library_groups', ?)
        ''', (json.dumps(library_groups),))
        conn.commit()
    
    def save_library_settings(self, library_settings):
        """Save library-specific settings to metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value) 
            VALUES ('library_settings', ?)
        ''', (json.dumps(library_settings),))
        conn.commit()
    
    def load_libraries(self):
        """Load libraries configuration from metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', ('libraries',))
        row = cursor.fetchone()
        return json.loads(row['value']) if row else {}
    
    def load_library_order(self):
        """Load library order from metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', ('library_order',))
        row = cursor.fetchone()
        return json.loads(row['value']) if row else []
    
    def load_hidden_libraries(self):
        """Load hidden libraries from metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', ('hidden_libraries',))
        row = cursor.fetchone()
        return json.loads(row['value']) if row else []
    
    def load_library_groups(self):
        """Load library groups from metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', ('library_groups',))
        row = cursor.fetchone()
        return json.loads(row['value']) if row else {}
    
    def load_library_settings(self):
        """Load library settings from metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', ('library_settings',))
        row = cursor.fetchone()
        return json.loads(row['value']) if row else {}            
    
    def save_ui_config(self, ui_config):
        """Save UI configuration to metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value) 
            VALUES ('ui_config', ?)
        ''', (json.dumps(ui_config),))
        conn.commit()
    
    def load_ui_config(self):
        """Load UI configuration from metadata table."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', ('ui_config',))
        row = cursor.fetchone()
        return json.loads(row['value']) if row else {}
    
    def get_favorite_cast(self):
        """Get all favorite cast members."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT name FROM favorite_cast ORDER BY name')
        return [row['name'] for row in cursor.fetchall()]

    def add_favorite_cast(self, cast_name):
        """Add a cast member to favorites."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO favorite_cast (name) VALUES (?)', (cast_name,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Already exists
            return False

    def remove_favorite_cast(self, cast_name):
        """Remove a cast member from favorites."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM favorite_cast WHERE name = ?', (cast_name,))
        conn.commit()
        return cursor.rowcount > 0

    def is_favorite_cast(self, cast_name):
        """Check if a cast member is favorited."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM favorite_cast WHERE name = ?', (cast_name,))
        return cursor.fetchone() is not None
    
    def save_cast_photo(self, cast_name, photo_data, mime_type, source_type='upload', source_url=None):
        """Save cast photo with automatic thumbnail generation."""
        try:
            from PIL import Image
            import io
            
            # Generate thumbnail (216x324 - 3x2 portrait ratio)
            img = Image.open(io.BytesIO(photo_data))
            
            # Convert to RGB if needed (for transparency)
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background
            
            # Resize to 216x324 maintaining aspect ratio
            img.thumbnail((216, 324), Image.Resampling.LANCZOS)
            
            thumb_io = io.BytesIO()
            img.save(thumb_io, format='JPEG', quality=85)
            thumbnail_data = thumb_io.getvalue()
            
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO cast_photos 
                (cast_name, photo_data, photo_mime_type, thumbnail_data, source_type, source_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (cast_name, photo_data, mime_type, thumbnail_data, source_type, source_url))
            conn.commit()
            return True
        except Exception as e:
            print(f"Error saving cast photo: {e}")
            return False

    def get_cast_photo(self, cast_name, thumbnail=False):
        """Get cast photo or thumbnail."""
        conn = self.get_connection()
        cursor = conn.cursor()
        field = 'thumbnail_data' if thumbnail else 'photo_data'
        cursor.execute(f'''
            SELECT {field}, photo_mime_type FROM cast_photos WHERE cast_name = ?
        ''', (cast_name,))
        row = cursor.fetchone()
        if row and row[0]:
            return {'data': row[0], 'mime_type': row[1]}
        return None

    def delete_cast_photo(self, cast_name):
        """Delete cast photo."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM cast_photos WHERE cast_name = ?', (cast_name,))
        conn.commit()
        return cursor.rowcount > 0
    # =============================================================================
    # VERSION-AWARE METHODS
    # =============================================================================

    def get_video_by_id(self, video_id):
        """Get video with all versions and metadata."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get video record
        cursor.execute('SELECT * FROM videos WHERE video_id = ?', (video_id,))
        video = cursor.fetchone()
        if not video:
            return None
        
        video_dict = dict(video)
        
        # Get all file versions
        cursor.execute('''
            SELECT * FROM file_versions 
            WHERE video_id = ? 
            ORDER BY is_preferred DESC, 
                     CAST(REPLACE(REPLACE(total_bitrate, ' kbps', ''), ' Mbps', '000') AS INTEGER) DESC
        ''', (video_id,))
        versions = [dict(row) for row in cursor.fetchall()]
        video_dict['versions'] = versions
        
        # Get preferred version (highest bitrate h264, then highest bitrate other)
        preferred = self._select_preferred_version(versions)
        if preferred:
            # Add preferred version's technical data to main dict
            video_dict['path'] = preferred['path']
            video_dict['filename'] = preferred['filename']
            video_dict['resolution'] = preferred['resolution']
            video_dict['codec'] = preferred['codec']
            video_dict['extension'] = preferred['extension']
            video_dict['length'] = preferred['length']
            video_dict['total_bitrate'] = preferred['total_bitrate']
            video_dict['file_size'] = preferred['file_size']
            video_dict['cache_id'] = preferred['cache_id']
        
        # Get list field values
        for field in LIST_FIELDS:
            field_lower = field.lower()
            cursor.execute(f'''
                SELECT fv.value
                FROM video_{field_lower}_junction vj
                JOIN video_{field_lower} fv ON vj.{field_lower}_id = fv.id
                WHERE vj.video_id = ?
                ORDER BY fv.value
            ''', (video_id,))
            values = [row[0] for row in cursor.fetchall()]
            video_dict[field_lower] = values
        
        return video_dict

    def get_video_by_path(self, path):
        """Get video by any of its file paths."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Find video_id from file path
        cursor.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,))
        row = cursor.fetchone()
        if not row:
            return None
        
        return self.get_video_by_id(row[0])

    def _select_preferred_version(self, versions):
        """
        Select preferred version based on:
        1. h264 codec preferred
        2. Highest bitrate within same codec
        """
        if not versions:
            return None
        
        # Parse bitrate helper
        def get_bitrate_value(bitrate_str):
            if not bitrate_str:
                return 0
            try:
                # Handle "5000 kbps" or "5 Mbps" or "5000" (raw number)
                bitrate_str = str(bitrate_str).lower().replace(',', '').strip()
                if 'mbps' in bitrate_str:
                    val = float(bitrate_str.replace('mbps', '').strip()) * 1000
                elif 'kbps' in bitrate_str:
                    val = float(bitrate_str.replace('kbps', '').strip())
                else:
                    # Try as raw number
                    val = float(bitrate_str)
                return val
            except Exception as e:
                print(f"Warning: Could not parse bitrate '{bitrate_str}': {e}")
                return 0
        
        # Group by codec
        h264_versions = []
        other_versions = []
        
        for v in versions:
            codec = (v.get('codec') or '').lower()
            if 'h264' in codec or 'avc' in codec or 'h.264' in codec:
                h264_versions.append(v)
            else:
                other_versions.append(v)
        
        # Prefer h264 with highest bitrate
        if h264_versions:
            # Debug: show bitrates
            for v in h264_versions:
                br = get_bitrate_value(v.get('total_bitrate'))
                print(f"  h264 version: {v.get('filename')} - bitrate: {v.get('total_bitrate')} -> {br}")
            
            h264_versions.sort(key=lambda x: get_bitrate_value(x.get('total_bitrate')), reverse=True)
            return h264_versions[0]
        
        # Otherwise highest bitrate of any codec
        if other_versions:
            other_versions.sort(key=lambda x: get_bitrate_value(x.get('total_bitrate')), reverse=True)
            return other_versions[0]
        
        # Fallback
        return versions[0]

    def get_all_codecs_for_video(self, video_id):
        """Get comma-separated list of all codecs for a video."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT DISTINCT codec 
            FROM file_versions 
            WHERE video_id = ? AND codec IS NOT NULL
            ORDER BY 
                CASE 
                    WHEN LOWER(codec) LIKE '%h264%' OR LOWER(codec) LIKE '%avc%' THEN 0
                    ELSE 1
                END,
                codec
        ''', (video_id,))
        
        codecs = [row[0] for row in cursor.fetchall()]
        return ', '.join(codecs) if codecs else ''

    def get_file_versions(self, video_id):
        """
        Get all ACTIVE file versions for a video, sorted by preference.

        Rows with deleted_at set are excluded - these are files that were
        removed from disk (e.g. a deleted duplicate) and are being retained
        for DELETED_FILE_RETENTION_DAYS in case they're a moved-file match,
        but should NOT count as a "version" for badge/playback purposes.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM file_versions 
            WHERE video_id = ? AND deleted_at IS NULL
            ORDER BY 
                CASE 
                    WHEN LOWER(codec) LIKE '%h264%' OR LOWER(codec) LIKE '%avc%' THEN 0
                    ELSE 1
                END,
                CAST(REPLACE(REPLACE(total_bitrate, ' kbps', ''), ' Mbps', '000') AS INTEGER) DESC
        ''', (video_id,))
        
        return [dict(row) for row in cursor.fetchall()]

    def set_preferred_version(self, video_id, path):
        """Set which file version is preferred for playback."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        print(f"=== SET_PREFERRED_VERSION ===")
        print(f"video_id: {video_id}")
        print(f"path: {path}")
        
        # Clear all preferred flags for this video
        cursor.execute('''
            UPDATE file_versions 
            SET is_preferred = 0 
            WHERE video_id = ?
        ''', (video_id,))
        
        # Set new preferred
        cursor.execute('''
            UPDATE file_versions 
            SET is_preferred = 1 
            WHERE video_id = ? AND path = ?
        ''', (video_id, path))
        
        rows_affected = cursor.rowcount
        print(f"Rows updated: {rows_affected}")
        
        conn.commit()
        
        # Verify it was set
        cursor.execute('SELECT path, is_preferred FROM file_versions WHERE video_id = ?', (video_id,))
        for row in cursor.fetchall():
            print(f"  {row['path']}: is_preferred={row['is_preferred']}")
        
        print(f"=== END SET_PREFERRED_VERSION ===")
        return True

    def auto_set_preferred(self, video_id):
        """Automatically set preferred version based on codec and bitrate."""
        print(f"=== AUTO_SET_PREFERRED called for {video_id} ===")
        versions = self.get_file_versions(video_id)
        if not versions:
            print("  No versions found!")
            return False
        
        print(f"  Found {len(versions)} versions")
        preferred = self._select_preferred_version(versions)
        if preferred:
            print(f"  Selected: {preferred['path']}")
            print(f"  Bitrate: {preferred.get('total_bitrate')}")
            self.set_preferred_version(video_id, preferred['path'])
            return True
        
        print("  Could not select preferred version")
        return False
    
    def _parse_length_to_seconds(self, length_str):
        """
        Convert length string to seconds.
        Formats: "1:23:45" (H:M:S), "23:45" (M:S), "45" (S), "0:00"
        Returns: Integer seconds or None if invalid
        """
        if not length_str or length_str.strip() == '':
            return None
        
        try:
            parts = length_str.strip().split(':')
            
            if len(parts) == 3:  # H:M:S
                hours, minutes, secs = parts
                return int(hours) * 3600 + int(minutes) * 60 + int(secs)
            elif len(parts) == 2:  # M:S
                minutes, secs = parts
                return int(minutes) * 60 + int(secs)
            elif len(parts) == 1:  # S
                return int(parts[0])
            
            return None
        except (ValueError, IndexError):
            return None

    def add_file_version(self, video_id, path, filename, metadata):
        """Add a new file version to existing video."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Check if video exists
        cursor.execute('SELECT 1 FROM videos WHERE video_id = ?', (video_id,))
        if not cursor.fetchone():
            raise ValueError(f"Video {video_id} does not exist")
        
        # Check if this path already exists
        cursor.execute('SELECT 1 FROM file_versions WHERE path = ?', (path,))
        if cursor.fetchone():
            # Update existing version
            return self.update_file_version_metadata(path, metadata)
        
        # Calculate length_seconds from length string
        length_seconds = self._parse_length_to_seconds(metadata.get('length'))
        
        # Insert file version
        cursor.execute('''
            INSERT INTO file_versions (
                path, video_id, filename,
                resolution, codec, extension, length, length_seconds, total_bitrate, file_size,
                date_created, date_modified, cache_id,
                is_preferred, last_seen, added_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        ''', (
            path, video_id, filename,
            metadata.get('resolution'),
            metadata.get('codec'),
            metadata.get('extension'),
            metadata.get('length'),
            length_seconds,
            metadata.get('total_bitrate'),
            metadata.get('file_size'),
            metadata.get('date_created'),
            metadata.get('date_modified'),
            metadata.get('cache_id'),
            datetime.now().isoformat(),
            datetime.now().isoformat()
        ))
        
        conn.commit()
        
        # Auto-update preferred if this is better
        self.auto_set_preferred(video_id)
        
        return True

    def update_file_version_metadata(self, path, metadata):
        """Update technical metadata for a specific file version."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        updates = []
        params = []
        
        # Technical fields that can be updated
        updatable_fields = ['resolution', 'codec', 'length', 'total_bitrate', 
                           'file_size', 'date_modified']
        
        for field in updatable_fields:
            if field in metadata:
                updates.append(f"{field} = ?")
                params.append(metadata[field])
        
        if updates:
            params.append(datetime.now().isoformat())
            params.append(path)
            
            query = f'''
                UPDATE file_versions 
                SET {', '.join(updates)}, last_seen = ?
                WHERE path = ?
            '''
            cursor.execute(query, params)
            self.safe_commit(conn)
            return True
        
        return False

    def remove_file_version(self, path):
        """Remove a file version. If it's the last version, delete the video."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get video_id
        cursor.execute('SELECT video_id, is_preferred FROM file_versions WHERE path = ?', (path,))
        row = cursor.fetchone()
        if not row:
            return False
        
        video_id = row[0]
        was_preferred = row[1]
        
        # Count remaining versions
        cursor.execute('SELECT COUNT(*) FROM file_versions WHERE video_id = ?', (video_id,))
        count = cursor.fetchone()[0]
        
        if count <= 1:
            # Last version - delete entire video
            cursor.execute('DELETE FROM videos WHERE video_id = ?', (video_id,))
            # Cascade will delete file_versions and junction records
        else:
            # Just delete this version
            cursor.execute('DELETE FROM file_versions WHERE path = ?', (path,))
            
            # If we deleted the preferred version, set a new one
            if was_preferred:
                self.auto_set_preferred(video_id)
        
        conn.commit()
        return True

    def create_video(self, video_id, filename_base, library, metadata=None):
        """Create a new video entry (without file versions)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        metadata = metadata or {}
        
        # Helper to convert values properly
        def to_value(val):
            if val is None:
                return None
            elif isinstance(val, bool):
                return 1 if val else 0
            else:
                return val
        
        cursor.execute('''
            INSERT OR IGNORE INTO videos (
                video_id, filename_base, library,
                title, year, rating, favorite,
                date_added, date_last_edited, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            video_id, filename_base, library,
            to_value(metadata.get('title')),
            to_value(metadata.get('year')),
            to_value(metadata.get('rating')),
            to_value(metadata.get('favorite', 0)),
            metadata.get('date_added', datetime.now().isoformat()),
            metadata.get('date_last_edited'),
            datetime.now().isoformat(),
            datetime.now().isoformat()
        ))
        
        # Add list field values if provided
        for field in LIST_FIELDS:
            field_lower = field.lower()
            if field_lower in metadata and metadata[field_lower]:
                values = metadata[field_lower]
                if isinstance(values, str):
                    values = [v.strip() for v in values.split(',') if v.strip()]
                
                for value in values:
                    # Get or create value ID
                    cursor.execute(f'INSERT OR IGNORE INTO video_{field_lower} (value) VALUES (?)', (value,))
                    cursor.execute(f'SELECT id FROM video_{field_lower} WHERE value = ?', (value,))
                    value_id = cursor.fetchone()[0]
                    
                    # Insert junction record
                    cursor.execute(f'''
                        INSERT OR IGNORE INTO video_{field_lower}_junction (video_id, {field_lower}_id)
                        VALUES (?, ?)
                    ''', (video_id, value_id))
        
        conn.commit()
        return True

    def _get_library_for_path(self, path):
        """Determine which library a path belongs to."""
        # Get libraries from metadata
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT value FROM metadata WHERE key = 'libraries'")
        row = cursor.fetchone()
        if not row:
            return "Unknown"
        
        libraries = json.loads(row[0])
        
        # Normalize path
        path_normalized = path.replace('\\', '/')
        
        # Build a flat list of (lib_name, lib_path) and sort by path length
        # descending so more specific paths match first (e.g. StudioRaw
        # before Studio when both share a common prefix).
        all_candidates = []
        for lib_name, lib_paths in libraries.items():
            for lib_path in lib_paths:
                all_candidates.append((lib_name, lib_path.replace('\\', '/')))
        all_candidates.sort(key=lambda x: len(x[1]), reverse=True)

        for lib_name, lib_path_normalized in all_candidates:
            if (path_normalized.startswith(lib_path_normalized + '/') or
                    path_normalized.startswith(lib_path_normalized + '\\')):
                return lib_name

        return "Unknown"
    
    # ============================================
    # CAST PRIMARY ATTRIBUTES METHODS
    # ============================================
    
    # ============================================
    # CAST GENDER METHODS
    # ============================================
    
    def get_cast_gender(self, cast_name):
        """Get gender for a specific cast member."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT gender FROM cast_gender WHERE cast_name = ?', (cast_name,))
        row = cursor.fetchone()
        
        return row[0] if row else None
    
    def set_cast_gender(self, cast_name, gender):
        """Set gender for a cast member."""
        if gender not in ['female', 'male', 'trans']:
            raise ValueError(f"Invalid gender: {gender}. Must be 'female', 'male', or 'trans'")
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO cast_gender (cast_name, gender, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(cast_name) DO UPDATE SET
                gender = excluded.gender,
                updated_at = CURRENT_TIMESTAMP
        ''', (cast_name, gender))
        
        conn.commit()
        return True
    
    def get_all_cast_genders(self):
        """Get all cast genders as a dictionary."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT cast_name, gender FROM cast_gender')
        
        return {row[0]: row[1] for row in cursor.fetchall()}
    
    def get_cast_gender_stats(self):
        """Get statistics about gender distribution."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT gender, COUNT(*) as count
            FROM cast_gender
            GROUP BY gender
            ORDER BY count DESC
        ''')
        
        stats = {}
        for row in cursor.fetchall():
            stats[row[0]] = row[1]
        
        # Get total cast members
        cursor.execute('SELECT COUNT(DISTINCT value) FROM video_cast')
        total = cursor.fetchone()[0]
        
        stats['total'] = total
        stats['assigned'] = sum(stats.get(g, 0) for g in ['female', 'male', 'trans'])
        stats['unassigned'] = total - stats['assigned']
        
        return stats

    # ============================================
    # CAST CATEGORY METHODS
    # ============================================

    def get_cast_category(self, cast_name):
        """Get category for a specific cast member. Defaults to 'pro' if not set."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT category FROM cast_category WHERE cast_name = ?', (cast_name,)
        )
        row = cursor.fetchone()
        return row[0] if row else 'pro'

    def set_cast_category(self, cast_name, category):
        """Set category for a cast member ('pro', 'amateur', or 'celeb')."""
        if category not in ('pro', 'amateur', 'celeb'):
            raise ValueError(
                f"Invalid category: {category!r}. Must be 'pro', 'amateur', or 'celeb'."
            )
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO cast_category (cast_name, category, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(cast_name) DO UPDATE SET
                category   = excluded.category,
                updated_at = CURRENT_TIMESTAMP
        ''', (cast_name, category))
        conn.commit()

    def get_all_cast_categories(self):
        """Return {cast_name: category} for all cast members that have a row."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT cast_name, category FROM cast_category')
        return {row[0]: row[1] for row in cursor.fetchall()}

    # ============================================
    # FILE CHANGES TRACKING METHODS
    # ============================================

    def flag_file_change(self, filename, filepath, video_id, library, flag_type, file_metadata, related_id=None):
        """Add or update a file change flag."""
        from datetime import datetime
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO file_changes 
            (filename, filepath, video_id, library, flag_type, flagged_at, 
             file_length, file_size, codec, bitrate, resolution, related_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            filename,
            filepath,
            video_id,
            library,
            flag_type,
            datetime.now().isoformat(),
            file_metadata.get('length_seconds'),
            file_metadata.get('file_size'),
            file_metadata.get('codec'),
            file_metadata.get('total_bitrate'),
            file_metadata.get('resolution'),
            related_id
        ))
        
        conn.commit()
        return cursor.lastrowid
    
    def get_file_changes(self, library=None, flag_types=None):
        """Get file changes, optionally filtered by library and flag types."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        query = 'SELECT * FROM file_changes WHERE 1=1'
        params = []
        
        if library:
            query += ' AND library = ?'
            params.append(library)
        
        if flag_types:
            placeholders = ','.join('?' * len(flag_types))
            query += f' AND flag_type IN ({placeholders})'
            params.extend(flag_types)
        
        query += ' ORDER BY flagged_at DESC'
        
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    
    def clear_file_change(self, change_id):
        """Remove a file change flag."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM file_changes WHERE id = ?', (change_id,))
        conn.commit()
    
    def cleanup_orphaned_file_changes(self):
        """Clean up file_changes entries with invalid related_id references."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Delete entries where related_id points to non-existent row
        cursor.execute('''
            DELETE FROM file_changes 
            WHERE related_id IS NOT NULL 
            AND related_id NOT IN (SELECT id FROM file_changes)
        ''')
        deleted = cursor.rowcount
        
        # Also delete orphaned possible_replacement entries (no related_id)
        cursor.execute('''
            DELETE FROM file_changes
            WHERE flag_type = 'possible_replacement'
            AND related_id IS NULL
        ''')
        deleted += cursor.rowcount
        
        conn.commit()
        return deleted
    
    def find_possible_replacements(self, filename, library, duration, tolerance=5):
        """Find potential replacement files for a deleted file (called when processing new files)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Look for recently deleted files with same filename that might have been moved
        # This is called when processing a NEW file to see if it matches a DELETED one
        cursor.execute('''
            SELECT * FROM file_changes
            WHERE filename = ?
            AND flag_type = 'deleted'
            AND library = ?
            AND ABS(file_length - ?) <= ?
            ORDER BY flagged_at DESC
            LIMIT 5
        ''', (filename, library, duration, tolerance))
        
        return [dict(row) for row in cursor.fetchall()]