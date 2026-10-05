# This file is: ./model.py

import os
import json
import hashlib
import threading
import hmac
from datetime import datetime
from config import DATA_FILE, USER_FILE, VIDEO_EXTENSIONS, LIST_FIELDS, MAX_USERS, USE_SQLITE, DATABASE_FILE
from media_handler import MediaHandler

# Import database manager if using SQLite
if USE_SQLITE:
    from database import Database


class UserModel:
    """Handles user data, hashing, and the max user limit."""
    def __init__(self):
        self.users = {} 
        self._save_lock = threading.Lock()
        self.load()

    def load(self):
        if os.path.exists(USER_FILE):
            try:
                with open(USER_FILE, 'r', encoding='utf-8') as f:
                    self.users = json.load(f)
            except Exception as e:
                print(f"Error loading user data: {e}")

    def save(self):
        def _write():
            with self._save_lock:
                try:
                    temp_file = USER_FILE + ".tmp"
                    data_to_save = {k: v for k, v in self.users.items()}
                    with open(temp_file, 'w', encoding='utf-8') as f: 
                        json.dump(data_to_save, f, indent=4)
                    if os.path.exists(USER_FILE): 
                        os.remove(USER_FILE)
                    os.rename(temp_file, USER_FILE)
                except Exception as e:
                    print(f"Error saving user data: {e}")
        threading.Thread(target=_write, daemon=True).start()

    def _hash_password(self, password, salt=None):
        if salt is None: salt = os.urandom(16)
        elif isinstance(salt, str): salt = bytes.fromhex(salt)
        pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return f"{salt.hex()}${pwd_hash.hex()}"

    def create_user(self, username, password):
        username = username.lower().strip()
        if not username: return False, "Username cannot be empty."
        if len(self.users) >= MAX_USERS: return False, "Cannot exceed maximum number of users."
        if username in self.users: return False, "User already exists."
        self.users[username] = self._hash_password(password)
        self.save()
        return True, "User created successfully."

    def verify_user(self, username, password):
        username = username.lower().strip()
        if username not in self.users: return False
        stored_value = self.users[username]
        if "$" in stored_value:
            try:
                salt_hex, hash_hex = stored_value.split('$')
                check_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), 100000).hex()
                return hmac.compare_digest(hash_hex, check_hash)
            except Exception: return False
        else:
            legacy_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
            if legacy_hash == stored_value:
                self.users[username] = self._hash_password(password)
                self.save()
                return True
            return False
    
    def get_user_count(self): return len(self.users)
    def get_max_users(self): return MAX_USERS


class LibraryModel:
    def __init__(self):
        self.use_sqlite = USE_SQLITE
        self.libraries = {}  
        self.files = {}
        self.column_config = None
        self.library_order = []
        self.hidden_libraries = []
        self.library_groups = {}
        self.library_settings = {}  # Per-library settings
        self.search_index = {} 
        self._save_lock = threading.Lock()
        
        if self.use_sqlite:
            self.db = Database(DATABASE_FILE)
            print(f"✓ Using SQLite database: {DATABASE_FILE}")
        
        self.load()

    def load(self):
        if self.use_sqlite:
            self._load_from_sqlite()
        else:
            self._load_from_json()

    def _load_from_sqlite(self):
        """Load data from SQLite database."""
        try:
            # Load library configuration from database
            self.libraries = self.db.load_libraries()
            self.library_order = self.db.load_library_order()
            self.hidden_libraries = self.db.load_hidden_libraries()
            self.library_groups = self.db.load_library_groups()
            self.library_settings = self.db.load_library_settings()
            
            # Load UI config from database
            ui_config = self.db.load_ui_config()
            if ui_config:
                self.files["_ui_config"] = ui_config
            
            # Normalize library order if missing
            current_libs = list(self.libraries.keys())
            for lib in current_libs:
                if lib not in self.library_order:
                    self.library_order.append(lib)
            self.library_order = [l for l in self.library_order if l in self.libraries]
            
            # Note: We don't load all files into memory immediately for performance
            # Files will be loaded on-demand from SQLite
            
            threading.Thread(target=self.rebuild_search_index, daemon=True).start()
            
        except Exception as e:
            print(f"Error loading from SQLite: {e}")
            import traceback
            traceback.print_exc()

    def _load_from_json(self):
        """Load data from JSON file (legacy method)."""
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    raw_libs = data.get('libraries', {})
                    if isinstance(raw_libs, list): self.libraries = {"Default": raw_libs}
                    else: self.libraries = raw_libs
                    self.files = data.get('files', {})
                    self.column_config = data.get('column_config')
                    self.library_order = data.get('library_order', [])
                    self.hidden_libraries = data.get('hidden_libraries', [])
                    self.library_groups = data.get('library_groups', {})
                    self.library_settings = data.get('library_settings', {})
                
                # Normalize library order if missing
                current_libs = list(self.libraries.keys())
                for lib in current_libs:
                    if lib not in self.library_order:
                        self.library_order.append(lib)
                self.library_order = [l for l in self.library_order if l in self.libraries]

                self._migrate_metadata_format()
                threading.Thread(target=self.rebuild_search_index, daemon=True).start()
            except Exception as e:
                print(f"Error loading data: {e}")

    def _migrate_metadata_format(self):
        """Legacy migration for old JSON format."""
        changed = False
        if changed: self.save()

    def rebuild_search_index(self):
        """Rebuild the search index."""
        if self.use_sqlite:
            # For SQLite, we'll search directly in the database
            # No need to keep full index in memory
            return
        else:
            new_index = {}
            for path, data in self.files.items():
                new_index[path] = self._create_search_blob(path, data)
            self.search_index = new_index

    def _create_search_blob(self, path, data):
        """Create searchable text blob for a file."""
        parts = [os.path.basename(path).lower()]
        for val in data.values():
            if isinstance(val, str): parts.append(val.lower())
            elif isinstance(val, list): parts.extend([str(x).lower() for x in val])
        return " ".join(parts)

    def _update_search_index_entry(self, path):
        """Update search index for a single file."""
        if self.use_sqlite:
            return  # Not needed for SQLite
        
        if path in self.files:
            self.search_index[path] = self._create_search_blob(path, self.files[path])

    def save(self):
        """Save data to storage."""
        if self.use_sqlite:
            self._save_to_sqlite()
        else:
            self._save_to_json()

    def _save_to_sqlite(self):
        """Save data to SQLite database."""
        def _write():
            with self._save_lock:
                try:
                    self.db.save_libraries(self.libraries)
                    self.db.save_library_order(self.library_order)
                    self.db.save_hidden_libraries(self.hidden_libraries)
                    self.db.save_library_groups(self.library_groups)
                    self.db.save_library_settings(self.library_settings)
                    
                    # Save UI config
                    ui_config = self.files.get("_ui_config", {})
                    if ui_config:
                        self.db.save_ui_config(ui_config)
                        
                except Exception as e:
                    print(f"Error saving to SQLite: {e}")
                    import traceback
                    traceback.print_exc()
        
        threading.Thread(target=_write, daemon=True).start()

    def _save_to_json(self):
        """Save data to JSON file (legacy method)."""
        def _write():
            with self._save_lock:
                data = {
                    'libraries': self.libraries, 
                    'files': self.files, 
                    'column_config': self.column_config,
                    'library_order': self.library_order,
                    'hidden_libraries': self.hidden_libraries,
                    'library_groups': self.library_groups,
                    'library_settings': self.library_settings
                }
                try:
                    temp_file = DATA_FILE + ".tmp"
                    with open(temp_file, 'w', encoding='utf-8') as f: 
                        json.dump(data, f, indent=4)
                    if os.path.exists(DATA_FILE): 
                        os.remove(DATA_FILE)
                    os.rename(temp_file, DATA_FILE)
                except Exception as e:
                    print(f"Error saving data: {e}")
        threading.Thread(target=_write, daemon=True).start()

    def set_column_config(self, columns):
        self.column_config = columns
        self.save()
        
    def set_library_order(self, order, hidden):
        self.library_order = order
        self.hidden_libraries = hidden
        self.save()
    
    # def set_library_groups(self, groups):
    #     """Set the library groups configuration"""
    #     self.library_groups = groups
    #     self.save()

    def set_library_groups(self, groups):
        """Set the library groups configuration"""
        self.library_groups = groups
        
        # Update library_order to include groups instead of grouped libraries
        # Get all libraries that are in groups
        libraries_in_groups = set()
        for group_name, group_data in groups.items():
            for lib in group_data.get('libraries', []):
                if lib in self.libraries:  # Only add actual libraries, not nested groups
                    libraries_in_groups.add(lib)
        
        # Rebuild library_order:
        # 1. Add all group names (top-level groups only)
        # 2. Add any ungrouped libraries
        new_order = []
        
        # Add top-level groups (those without a parent)
        for group_name in groups:
            if not groups[group_name].get('parent'):
                if group_name not in new_order:
                    new_order.append(group_name)
        
        # Add any libraries that aren't in a group
        for lib in self.library_order:
            if lib in self.libraries and lib not in libraries_in_groups:
                if lib not in new_order:
                    new_order.append(lib)
        
        # Add any new ungrouped libraries not in the old order
        for lib in self.libraries:
            if lib not in libraries_in_groups and lib not in new_order:
                new_order.append(lib)
        
        self.library_order = new_order
        print(f"Updated library_order to include groups: {new_order}")
        
        self.save()
    
    def set_library_setting(self, library_name: str, setting_key: str, setting_value: str):
        """Set a specific setting for a library"""
        if library_name not in self.library_settings:
            self.library_settings[library_name] = {}
        self.library_settings[library_name][setting_key] = setting_value
        self.save()
    
    def get_libraries_in_group(self, group_name, recursive=True):
        """Get all libraries in a group, optionally recursing into subgroups"""
        if group_name not in self.library_groups:
            return []
        
        libs = []
        group = self.library_groups[group_name]
        
        for item in group.get('libraries', []):
            if item in self.library_groups and recursive:
                # This is a subgroup
                libs.extend(self.get_libraries_in_group(item, recursive=True))
            elif item in self.libraries:
                # This is an actual library
                libs.append(item)
        
        return libs

    # --- Library Management ---
    def create_library(self, name):
        if name not in self.libraries:
            self.libraries[name] = []
            self.library_order.append(name)
            self.save()
            return True
        return False

    def rename_library(self, old_name, new_name):
        if old_name in self.libraries and new_name not in self.libraries:
            self.libraries[new_name] = self.libraries.pop(old_name)
            if old_name in self.library_order:
                idx = self.library_order.index(old_name)
                self.library_order[idx] = new_name
            else:
                self.library_order.append(new_name)
            # everything else that refers to the library by name
            self.hidden_libraries = [new_name if n == old_name else n for n in self.hidden_libraries]
            if old_name in self.library_settings:
                self.library_settings[new_name] = self.library_settings.pop(old_name)
            for group in self.library_groups.values():
                libs = group.get('libraries') if isinstance(group, dict) else None
                if libs and old_name in libs:
                    group['libraries'] = [new_name if n == old_name else n for n in libs]
            self.save()
            if self.use_sqlite:
                try:
                    conn = self.db.get_connection()
                    for table, col in (('videos', 'library'), ('file_changes', 'library'),
                                       ('library_default_site', 'library'), ('library_ext_source', 'library')):
                        try:
                            conn.execute(f'UPDATE {table} SET {col} = ? WHERE {col} = ?', (new_name, old_name))
                        except Exception:
                            pass                      # table not created yet
                    conn.commit()
                except Exception as e:
                    print(f"[Libraries] rename: database not updated: {e}")
            return True
        return False

    def _after_library_change(self):
        """Keep the database in step with the library configuration: videos.library follows
        where each file lives, and files that no library covers any more are soft-deleted -
        kept for the retention period and restored if their folder is added back and scanned."""
        if not self.use_sqlite:
            return None
        try:
            import db_maintenance as dm
            conn = self.db.get_connection()
            roots = dm.roots_from(self.libraries)     # the saved copy is written in the background
            dm.sync_video_libraries(conn, roots)
            result = dm.soft_delete_unowned(conn, roots=roots)
            conn.commit()
            if result.get('count') or result.get('skipped'):
                print(f"[Libraries] files outside every library: {result}")
            return result
        except Exception as e:
            print(f"[Libraries] database not updated after a library change: {e}")
            return None

    def delete_library(self, name):
        if name in self.libraries:
            paths_to_remove = self.libraries[name]
            del self.libraries[name]
            if name in self.library_order:
                self.library_order.remove(name)
            self.hidden_libraries = [n for n in self.hidden_libraries if n != name]
            self.library_settings.pop(name, None)
            for group in self.library_groups.values():
                libs = group.get('libraries') if isinstance(group, dict) else None
                if libs and name in libs:
                    group['libraries'] = [n for n in libs if n != name]

            if not self.use_sqlite:
                remaining_paths = set()
                for paths in self.libraries.values():
                    remaining_paths.update(paths)
                for folder in paths_to_remove:
                    if folder not in remaining_paths:
                        search_prefix = os.path.join(folder, "")
                        for fp in [fp for fp in self.files if fp.startswith(search_prefix)]:
                            del self.files[fp]
                            self.search_index.pop(fp, None)

            self.save()
            self.last_library_change = self._after_library_change()
            return True
        return False

    def add_path_to_library(self, lib_name, path):
        path = os.path.normpath(path)
        if lib_name in self.libraries:
            if path not in self.libraries[lib_name]:
                self.libraries[lib_name].append(path)
                self.scan_library(fast=True)
                self.save()
                self.last_library_change = self._after_library_change()
                return True
        return False

    def remove_library(self, path):
        path = os.path.normpath(path)
        changed = False
        for lib in self.libraries:
            if path in self.libraries[lib]:
                self.libraries[lib].remove(path)
                changed = True
        if changed:
            self.save()
            self.last_library_change = self._after_library_change()
            return True
        return False

    def remove_folder_from_library(self, library, path):
        """Take one folder out of one library (its files stay if another library covers them)."""
        if library in self.libraries and path in self.libraries[library]:
            self.libraries[library].remove(path)
            self.save()
            self.last_library_change = self._after_library_change()
            return True
        return False

    # --- File Operations ---
    def scan_library(self, fast=False, library=None):
        """
        Scan library folders for video files.
        
        Args:
            fast: If True, skip metadata extraction for new files
            library: Optional library name to scan (if None, scans all libraries)
        """
        found_files = set()
        new_files = []
        joined_files = set()          # new copies added to an existing video (not flagged as new)
        recovered_files = []
        
        # Determine which libraries to scan
        all_paths = set()
        if library and library != "All Videos":
            # Scan specific library only
            if library in self.libraries:
                all_paths.update(self.libraries[library])
            else:
                # Might be a library group
                if library in self.library_groups:
                    group_libs = self.get_libraries_in_group(library, recursive=True)
                    for lib in group_libs:
                        if lib in self.libraries:
                            all_paths.update(self.libraries[lib])
        else:
            # Scan all libraries
            for paths in self.libraries.values(): 
                all_paths.update(paths)
            
        import db_maintenance as dm
        roots = dm.roots_from(self.libraries)

        # Walk the library folders first, several at once (network shares answer faster in
        # parallel); the results are processed below in the usual order
        walked = self._walk_roots(all_paths)
        seen = {os.path.normpath(os.path.join(root, f))
                for tree in walked.values() for root, _dirs, files in tree for f in files}

        # PHASE 1: Mark missing files for deletion (only in libraries being scanned)
        if self.use_sqlite:
            marked_paths = self.db.mark_missing_files(library_paths=list(all_paths), found=seen)
            print(f"Marked {len(marked_paths)} missing files for deletion in current library")
        
        # PHASE 2: Scan for files and recover moved ones
        for lib_path in all_paths:
            if not os.path.exists(lib_path): continue
            for root, dirs, filenames in walked.get(lib_path, []):
                for filename in filenames:
                    if os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS:
                        full_path = os.path.normpath(os.path.join(root, filename))
                        found_files.add(full_path)
                        
                        # Check if file exists in database (including deleted entries)
                        file_exists = False
                        if self.use_sqlite:
                            # Check file_versions directly, including deleted entries
                            conn = self.db.get_connection()
                            cursor = conn.cursor()
                            cursor.execute('''
                                SELECT path, deleted_at FROM file_versions WHERE path = ?
                            ''', (full_path,))
                            existing = cursor.fetchone()
                            
                            if existing:
                                existing_dict = dict(existing)
                                if existing_dict['deleted_at'] is None:
                                    # File exists and is active - check if modified
                                    file_exists = True
                                    
                                    # Check if file has been replaced (size or mtime changed)
                                    try:
                                        current_stats = os.stat(full_path)
                                        current_size = current_stats.st_size
                                        
                                        # Parse file_size string to bytes if needed
                                        stored_size_str = existing_dict.get('file_size', '')
                                        if stored_size_str:
                                            stored_size = self._convert_file_size_to_bytes(stored_size_str)
                                            
                                            # If file size changed, it's been replaced
                                            if stored_size and stored_size != current_size:
                                                print(f"File replaced (size changed): {filename}")
                                                # Refresh technical metadata
                                                from media_handler import MediaHandler
                                                metadata = MediaHandler.get_extended_metadata(full_path)
                                                info = MediaHandler.get_file_info(full_path)
                                                
                                                cursor.execute('''
                                                    UPDATE file_versions
                                                    SET 
                                                        length = ?,
                                                        length_seconds = ?,
                                                        file_size = ?,
                                                        codec = ?,
                                                        total_bitrate = ?,
                                                        resolution = ?
                                                    WHERE path = ?
                                                ''', (
                                                    info.get('length', ''),
                                                    int(metadata.get('duration', 0)),
                                                    info.get('file_size'),
                                                    info.get('codec', ''),
                                                    info.get('total_bitrate', ''),
                                                    info.get('resolution', ''),
                                                    full_path
                                                ))
                                                conn.commit()
                                    except Exception as e:
                                        print(f"Error checking file modification: {e}")
                                else:
                                    # File exists but was marked deleted - recover it!
                                    print(f"Recovered deleted file at same path: {full_path}")
                                    cursor.execute('''
                                        UPDATE file_versions 
                                        SET deleted_at = NULL 
                                        WHERE path = ?
                                    ''', (full_path,))
                                    dm.clear_flags_for_path(conn, full_path)
                                    conn.commit()
                                    file_exists = True
                                    recovered_files.append(full_path)
                        else:
                            file_exists = full_path in self.files
                        
                        if not file_exists:
                            # Before creating new entry, check if this is a move within library
                            if self.use_sqlite:
                                conn = self.db.get_connection()
                                cursor = conn.cursor()
                                
                                lib_here = dm.owner_of(full_path, roots)
                                print(f"[Move Check] Checking if {filename} exists elsewhere in {lib_here}")
                                cursor.execute('''
                                    SELECT fv.path, fv.video_id, fv.length_seconds, fv.deleted_at
                                    FROM file_versions fv
                                    WHERE fv.filename = ?
                                    AND fv.path != ?
                                    ORDER BY
                                        CASE WHEN fv.deleted_at IS NOT NULL THEN 0 ELSE 1 END,
                                        fv.deleted_at DESC
                                ''', (filename, full_path))
                                same_lib = [dict(r) for r in cursor.fetchall()
                                            if lib_here and dm.owner_of(r['path'], roots) == lib_here]
                                gone = [r for r in same_lib if r['deleted_at'] or not os.path.exists(r['path'])]
                                if len(gone) > 1:
                                    # several missing files with this name: the length decides (within 5 s)
                                    from media_handler import MediaHandler
                                    dur = int(MediaHandler.get_extended_metadata(full_path).get('duration', 0) or 0)
                                    same_lib = [r for r in gone if r['length_seconds'] and abs(r['length_seconds'] - dur) <= 5][:1]
                                existing = same_lib[0] if same_lib else None
                                
                                if existing:
                                    # File exists elsewhere in same library
                                    existing_dict = dict(existing)
                                    old_path = existing_dict['path']
                                    old_video_id = existing_dict['video_id']
                                    
                                    print(f"[Move Check] Found existing entry: {old_path}")
                                    print(f"[Move Check] Old path exists on disk: {os.path.exists(old_path)}")
                                    
                                    if not os.path.exists(old_path):
                                        # Old path gone - this is a move!
                                        print(f"[Move Detection] CONFIRMED MOVE: {old_path} -> {full_path}")
                                        
                                        # Update path and clear deleted_at (Phase 1 may have
                                        # set it on the old path since it was no longer there)
                                        cursor.execute(
                                            'UPDATE file_versions SET path = ?, filename = ?, deleted_at = NULL WHERE path = ?',
                                            (full_path, filename, old_path)
                                        )
                                        rows_updated = cursor.rowcount
                                        dm.forget_path(conn, old_path)
                                        print(f"[Move Detection] Updated {rows_updated} rows in file_versions")
                                        
                                        # Refresh technical metadata
                                        from media_handler import MediaHandler
                                        metadata = MediaHandler.get_extended_metadata(full_path)
                                        info = MediaHandler.get_file_info(full_path)
                                        
                                        cursor.execute('''
                                            UPDATE file_versions
                                            SET length = ?, length_seconds = ?, file_size = ?,
                                                codec = ?, total_bitrate = ?, resolution = ?, is_preferred = 1
                                            WHERE path = ?
                                        ''', (info.get('length', ''), int(metadata.get('duration', 0)),
                                              info.get('file_size'), info.get('codec', ''),
                                              info.get('total_bitrate', ''), info.get('resolution', ''),
                                              full_path))
                                        
                                        conn.commit()
                                        print(f"[Move Detection] Successfully updated path and metadata for {filename}")
                                        
                                        # Don't add to new_files - it's not actually new
                                        self._update_search_index_entry(full_path)
                                        continue  # Skip _ensure_file_entry
                                    else:
                                        print(f"[Move Check] Old path still exists - treating as copy/duplicate")
                                else:
                                    print(f"[Move Check] No existing entry found for {filename} in {library}")

                                # Another copy of a video already in this library (same name, any
                                # extension, same length): add it as a version of that video and
                                # leave the video's metadata alone
                                joined_to = self._same_video_in_library(conn, full_path, filename, lib_here, roots)
                                if joined_to:
                                    from media_handler import MediaHandler
                                    self.db.add_file_version(joined_to, full_path, filename, MediaHandler.get_file_info(full_path))
                                    conn.commit()
                                    print(f"[Copy] joined {full_path} -> {joined_to}")
                                    new_files.append(full_path)
                                    joined_files.add(full_path)
                                    self._update_search_index_entry(full_path)
                                    continue
                            
                            # Truly new file - create entry
                            print(f"[New File] Creating new entry for {full_path}")
                            new_files.append(full_path)
                            self._ensure_file_entry(full_path, filename, skip_metadata=fast)
                            self._update_search_index_entry(full_path)
        
        # PHASE 3: Track file changes for replacement detection (BEFORE finalizing deletions)
        if self.use_sqlite:
            # Flag deleted / new files per library (also on whole-collection scans), so moves
            # between folders and libraries can be matched up, whichever is seen first.
            by_lib = {}
            for path in marked_paths:
                lib = dm.owner_of(path, roots)
                if lib:
                    by_lib.setdefault(lib, ([], []))[1].append(path)
            for path in new_files:
                if path in joined_files:
                    continue
                lib = dm.owner_of(path, roots)
                if lib:
                    by_lib.setdefault(lib, ([], []))[0].append(path)
            for lib, (lib_new, lib_marked) in by_lib.items():
                self._track_file_changes(lib, lib_new, lib_marked)
        
        # PHASE 4: Finalize deletions moved to scanning.py AFTER check_cross_library_replacements
        # so that files flagged 'deleted' in phase 3 get a chance to be matched cross-library
        # before being permanently removed.
        removed_files = []
        if not self.use_sqlite:
            # JSON mode only - SQLite finalization happens in scanning.py
            current_db_files = list(self.files.keys())
            for db_path in current_db_files:
                belongs_to_lib = False
                for lib_root in all_paths:
                    if db_path.startswith(os.path.join(lib_root, "")):
                        belongs_to_lib = True
                        break
                
                if belongs_to_lib and db_path not in found_files:
                    del self.files[db_path]
                    if db_path in self.search_index: 
                        del self.search_index[db_path]
                    removed_files.append(db_path)
        
        # PHASE 5: purging files deleted longer ago than the retention period, and flag
        # clean-up, run in db_maintenance after the scan (routes/scanning.py).

        if fast and new_files:
            # a fast scan skips the technical details (codec, length, resolution) of new files:
            # read them in the background so the files play and sort right without a manual refresh
            threading.Thread(target=self._fill_metadata_background, args=(list(new_files),),
                             name='fill-metadata', daemon=True).start()

        return new_files, removed_files

    _fill_lock = threading.Lock()

    def _fill_metadata_background(self, paths):
        with self._fill_lock:
            try:
                n = self.refresh_technical_metadata(paths=paths)
                print(f"[Scan] read technical details for {n} new file(s)")
            except Exception as e:
                print(f"[Scan] reading technical details failed: {e}")

    @staticmethod
    def _walk_roots(roots, workers=6):
        """{root: list(os.walk(root))} for every existing root, walked in parallel."""
        from concurrent.futures import ThreadPoolExecutor
        roots = [r for r in roots if os.path.exists(r)]
        if not roots:
            return {}
        with ThreadPoolExecutor(max_workers=min(workers, len(roots))) as pool:
            trees = pool.map(lambda r: list(os.walk(r, followlinks=True)), roots)
            return dict(zip(roots, trees))

    def _same_video_in_library(self, conn, path, filename, library, roots, tolerance=5):
        """The video_id of the one video in this library that has an active file with the same
        name (ignoring extension and case) and the same length (within `tolerance` s), else None.
        Several such videos: ambiguous, None."""
        if not library:
            return None
        import db_maintenance as dm
        from media_handler import MediaHandler
        base = os.path.splitext(filename)[0]
        rows = conn.execute('''SELECT path, video_id, length_seconds FROM file_versions
                               WHERE deleted_at IS NULL AND path != ? AND filename LIKE ? ESCAPE '\\' ''',
                            (path, base.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '.%')).fetchall()
        rows = [r for r in rows if os.path.splitext(os.path.basename(r['path']))[0].lower() == base.lower()
                and dm.owner_of(r['path'], roots) == library]
        if not rows:
            return None
        dur = int(MediaHandler.get_extended_metadata(path).get('duration', 0) or 0)
        if not dur:
            return None
        def length(r):       # files added by a fast scan have no stored length yet
            if r['length_seconds']:
                return r['length_seconds']
            return int(MediaHandler.get_extended_metadata(r['path']).get('duration', 0) or 0) if os.path.exists(r['path']) else 0
        vids = {r['video_id'] for r in rows if (n := length(r)) and abs(n - dur) <= tolerance}
        return vids.pop() if len(vids) == 1 else None

    def refresh_technical_metadata(self, paths=None, force_all=False):
        """Refreshes technical metadata."""
        count = 0
        
        if paths:
            targets = paths
        elif self.use_sqlite:
            # Get all file paths from database
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL')
                targets = [row['path'] for row in cursor.fetchall()]
        else:
            targets = self.files.keys()
        
        for path in targets:
            if not os.path.exists(path): 
                continue
            
            # Check if we need to update
            if not paths and not force_all:
                if self.use_sqlite:
                    file_data = self.db.get_file(path)
                    if file_data and file_data.get('resolution') and file_data.get('codec'):
                        continue
                else:
                    if self.files.get(path, {}).get('resolution') and self.files.get(path, {}).get('codec'):
                        continue

            info = MediaHandler.get_file_info(path)
            
            try:
                stats = os.stat(path)
                c_time = datetime.fromtimestamp(stats.st_ctime).strftime('%Y-%m-%d %H:%M')
                m_time = datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M')
            except: 
                c_time, m_time = "", ""

            # Update metadata
            if self.use_sqlite:
                file_data = self.db.get_file(path) or {}
                file_data.update({
                    'length': info.get('length', ''),
                    'total_bitrate': info.get('total_bitrate', ''),
                    'file_size': info.get('file_size', ''),
                    'codec': info.get('codec', ''),
                    'extension': info.get('extension', ''),
                    'date_created': c_time,
                    'date_modified': m_time
                })
                if info.get('resolution'):
                    file_data['resolution'] = info.get('resolution')
                
                filename = os.path.basename(path)
                self.db.add_file(path, filename, file_data)
            else:
                self.files[path]['length'] = info.get('length', '')
                self.files[path]['total_bitrate'] = info.get('total_bitrate', '')
                self.files[path]['file_size'] = info.get('file_size', '')
                self.files[path]['codec'] = info.get('codec', '')
                self.files[path]['extension'] = info.get('extension', '')
                if info.get('resolution'):
                    self.files[path]['resolution'] = info.get('resolution')
                
                self.files[path]['date_created'] = c_time
                self.files[path]['date_modified'] = m_time
            
            count += 1
            
        if count > 0 and not self.use_sqlite: 
            self.save()
        
        return count
        
    def _ensure_file_entry(self, path, filename, update_only=False, skip_metadata=False):
        """Ensure a file has an entry in the database."""
        try:
            stats = os.stat(path)
            c_time = datetime.fromtimestamp(stats.st_ctime).strftime('%Y-%m-%d %H:%M')
            m_time = datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M')
        except: 
            c_time, m_time = "", ""
        
        info = {}
        if not update_only and not skip_metadata:
            info = MediaHandler.get_file_info(path)
        else:
            if self.use_sqlite:
                existing = self.db.get_file(path)
                if existing:
                    info = existing
            elif path in self.files:
                entry = self.files[path]
                info = {
                    'length': entry.get('length', ''),
                    'total_bitrate': entry.get('total_bitrate', ''),
                    'file_size': entry.get('file_size', ''),
                    'resolution': entry.get('resolution', ''),
                    'codec': entry.get('codec', ''),
                    'extension': entry.get('extension', '')
                }

        if not update_only:
            now = str(datetime.now())
            default_entry = {
                'title': os.path.splitext(filename)[0],
                'year': '', 
                'resolution': info.get('resolution', ''), 
                'codec': info.get('codec', ''),
                'extension': info.get('extension', ''),
                'rating': '', 
                'favorite': False,
                'length': info.get('length', ''), 
                'total_bitrate': info.get('total_bitrate', ''), 
                'file_size': info.get('file_size', ''),
                'added': now,
                'date_added': now,
                'date_last_edited': now,
                'date_created': c_time, 
                'date_modified': m_time
            }
            
            # Add list fields
            for field in LIST_FIELDS:
                default_entry[field.lower()] = []
            
            if self.use_sqlite:
                filename = os.path.basename(path); self.db.add_file(path, filename, default_entry)
            else:
                self.files[path] = default_entry
        else:
            if self.use_sqlite:
                file_data = self.db.get_file(path)
                if file_data:
                    for k in LIST_FIELDS:
                        if k.lower() not in file_data: 
                            file_data[k.lower()] = []
                    file_data['date_created'] = c_time
                    file_data['date_modified'] = m_time
                    file_data['date_last_edited'] = str(datetime.now())
                    filename = os.path.basename(path); self.db.add_file(path, filename, file_data)
            else:
                entry = self.files[path]
                for k in LIST_FIELDS:
                    if k.lower() not in entry: 
                        entry[k.lower()] = []
                entry['date_created'] = c_time
                entry['date_modified'] = m_time
                entry['date_last_edited'] = str(datetime.now())

    def update_field(self, path, field, value):
        """Update a field for a file."""
        if self.use_sqlite:
            file_data = self.db.get_file(path)
            if not file_data:
                return
        else:
            if path not in self.files:
                return
            file_data = self.files[path]
        
        list_fields_lower = [x.lower() for x in LIST_FIELDS]
        field_key = field.lower()
        
        if field_key in list_fields_lower:
            clean_list = [x.strip() for x in value.split(',') if x.strip()]
            file_data[field_key] = clean_list
        elif field_key == "rating":
            try:
                if value.strip() == "": 
                    file_data[field_key] = ""
                else:
                    val_float = float(value)
                    if 0 <= val_float <= 10:
                        file_data[field_key] = str(val_float)
            except ValueError: 
                pass
        else:
            file_data[field_key] = value.strip()
        
        # Update date_last_edited
        file_data['date_last_edited'] = str(datetime.now())
        
        if self.use_sqlite:
            filename = os.path.basename(path); self.db.add_file(path, filename, file_data)
        else:
            self._update_search_index_entry(path)
            self.save()

    def toggle_favorite(self, path):
        """Toggle favorite status for a file."""
        if self.use_sqlite:
            # Get current favorite status
            conn = self.db.get_connection()
            cursor = conn.cursor()
            
            # Get video_id for this path
            cursor.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,))
            row = cursor.fetchone()
            
            if row:
                video_id = row['video_id']
                
                # Toggle favorite in videos table
                cursor.execute('SELECT favorite FROM videos WHERE video_id = ?', (video_id,))
                video_row = cursor.fetchone()
                
                if video_row:
                    new_fav = not video_row['favorite']
                    cursor.execute('UPDATE videos SET favorite = ? WHERE video_id = ?', 
                                 (1 if new_fav else 0, video_id))
                    conn.commit()
                    return new_fav
        else:
            if path in self.files:
                self.files[path]['favorite'] = not self.files[path].get('favorite', False)
                self.save()
                return self.files[path]['favorite']
        return False

    def get_all_tags(self):
        """Get all unique tags across all fields (excluding cast and site)."""
        if self.use_sqlite:
            return self.db.get_all_tags()
        else:
            # Build case-insensitive tag counts (count unique FILES per tag)
            tag_file_map = {}  # Map lowercase tag -> set of file paths
            target_fields = [x.lower() for x in LIST_FIELDS if x.lower() not in ['cast', 'site']]
            
            for path, data in self.files.items():
                if path == '_ui_config':
                    continue
                    
                for field in target_fields:
                    values = data.get(field, [])
                    if isinstance(values, list):
                        for value in values:
                            if value:
                                # Normalize to lowercase for grouping
                                value_lower = str(value).strip().lower()
                                if value_lower not in tag_file_map:
                                    tag_file_map[value_lower] = set()
                                tag_file_map[value_lower].add(path)
            
            # Convert to counts
            tag_counts = {tag: len(files) for tag, files in tag_file_map.items()}
            return tag_counts
    
    def get_all_sites(self):
        """Get all unique sites with counts."""
        if self.use_sqlite:
            return self.db.get_all_sites()
        else:
            # Build case-insensitive site counts (count unique FILES per site)
            site_file_map = {}  # Map lowercase site -> set of file paths
            
            for path, data in self.files.items():
                if path == '_ui_config':
                    continue
                    
                sites = data.get('site', [])
                if isinstance(sites, list):
                    for site in sites:
                        if site:
                            # Normalize to lowercase for grouping
                            site_lower = str(site).strip().lower()
                            if site_lower not in site_file_map:
                                site_file_map[site_lower] = set()
                            site_file_map[site_lower].add(path)
            
            # Convert to counts
            site_counts = {site: len(files) for site, files in site_file_map.items()}
            return site_counts

    def export_to_json(self, json_path):
        """Export data to a JSON file."""
        try:
            export_data = []
            
            if self.use_sqlite:
                # Delegate to database export method
                return self.db.export_to_json(json_path)
            else:
                # Export from JSON
                for path, data in self.files.items():
                    _, ext = os.path.splitext(path)
                    if ext.lower() not in VIDEO_EXTENSIONS:
                        continue

                    item = data.copy()
                    item['path'] = path
                    
                    fname = path
                    if '\\' in fname: fname = fname.split('\\')[-1]
                    if '/' in fname: fname = fname.split('/')[-1]
                    
                    item['filename'] = fname
                    item['cache_id'] = hashlib.md5(fname.encode('utf-8')).hexdigest()
                    export_data.append(item)
                
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(export_data, f, indent=4)
                return True
        except Exception as e:
            print(f"JSON Export Error: {e}")
            return False

    def import_from_json(self, json_path):
        """Import data from a JSON file."""
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if self.use_sqlite:
                # Delegate to database import method
                count = self.db.import_from_json_data(data)
                return count
            else:
                # Import to JSON-based model
                list_fields_lower = [x.lower() for x in LIST_FIELDS]
                updates_count = 0
                
                # Handle both old (list) and new (dict) formats
                items_to_import = []
                
                if isinstance(data, dict):
                    # New format: {"/path": {metadata}} or {"files": {"/path": {metadata}}}
                    if 'files' in data:
                        # Old format with "files" wrapper
                        files_dict = data.get('files', {})
                    else:
                        # New format without wrapper
                        files_dict = data
                    
                    # Convert dict to list of items with path included
                    for path, metadata in files_dict.items():
                        if path == '_ui_config':
                            continue
                        if isinstance(metadata, dict):
                            item = metadata.copy()
                            if 'path' not in item:
                                item['path'] = path
                            items_to_import.append(item)
                
                elif isinstance(data, list):
                    # Very old format: [{path: ..., ...}, ...]
                    items_to_import = data
                else:
                    print("Import Error: Data is neither list nor dict")
                    return 0
                
                # Process each item
                for item in items_to_import:
                    if not isinstance(item, dict): 
                        continue
                    norm_item = {k.lower(): v for k, v in item.items()}
                    matched_path = self._find_matched_path(norm_item)
                    if matched_path:
                        self._apply_import_data(matched_path, norm_item, list_fields_lower)
                        updates_count += 1
                        print(f"✓ Updated: {os.path.basename(matched_path)}", flush=True)
                    else:
                        filename = norm_item.get('filename', norm_item.get('path', 'unknown'))
                        print(f"✗ Skipping: {filename} (not found in files)", flush=True)
                
                if updates_count > 0:
                    self.save()
                    print(f"💾 Saved {updates_count} updates to {DATA_FILE}", flush=True)
                
                print(f"\n📊 Import Summary: {updates_count} files updated out of {len(items_to_import)} in export", flush=True)
                return updates_count
        except Exception as e:
            print(f"JSON Import Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return -1

    def _find_matched_path(self, row_data):
        """Find a file path based on row data."""
        # Strategy 1: Try exact path match
        if "path" in row_data and row_data["path"]:
            p = os.path.normpath(row_data["path"].strip())
            if self.use_sqlite:
                if self.db.get_file(p):
                    return p
            else:
                if p in self.files:
                    print(f"  → Exact path match: {p}", flush=True)
                    return p
        
        # Strategy 2: Try filename match
        target_name = row_data.get("filename", "").strip().lower()
        if target_name:
            if self.use_sqlite:
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                    SELECT path FROM file_versions
                    WHERE deleted_at IS NULL AND LOWER(path) LIKE ?
                    ''', (f'%{target_name}',))
                    row = cursor.fetchone()
                    if row:
                        return row['path']
            else:
                # Search through all loaded files
                for fp in self.files:
                    if os.path.basename(fp).lower() == target_name:
                        print(f"  → Filename match: {target_name} → {fp}", flush=True)
                        return fp
                
                # If we get here, file not found
                print(f"  → No match for: {target_name} (checked {len(self.files)} files)", flush=True)
        return None

    def _apply_import_data(self, path, row_data, list_fields_lower):
        """Apply imported data to a file."""
        if self.use_sqlite:
            file_data = self.db.get_file(path) or {}
        else:
            entry = self.files[path]
            file_data = entry
        
        for col, val in row_data.items():
            if val is None or (isinstance(val, str) and not val.strip()): 
                continue 
            key = col.lower()
            if key in ["filename", "path"]: 
                continue
            if key == "number": 
                key = "participants"
            if key == "res": 
                key = "resolution"
            
            if key == "favorite":
                if isinstance(val, bool):
                    file_data['favorite'] = val
                elif isinstance(val, str):
                    v = val.strip().lower()
                    file_data['favorite'] = v in ("true", "1", "yes", "y")
                else:
                    file_data['favorite'] = bool(val)
                continue
            
            if key in list_fields_lower or key == 'participants':
                new_items = []
                if isinstance(val, list):
                    for item in val:
                        item_str = str(item)
                        if "," in item_str:
                            new_items.extend([x.strip() for x in item_str.split(',') if x.strip()])
                        elif item_str.strip():
                            new_items.append(item_str.strip())
                else:
                    new_items = [x.strip() for x in str(val).split(',') if x.strip()]
                
                current_items = file_data.get(key, [])
                if not isinstance(current_items, list): 
                    current_items = []
                merged_set = set(current_items)
                merged_set.update(new_items)
                file_data[key] = sorted(list(merged_set))
            elif key in file_data or key in ['title', 'year', 'rating', 'resolution', 'codec']:
                file_data[key] = str(val).strip()
        
        if self.use_sqlite:
            filename = os.path.basename(path); self.db.add_file(path, filename, file_data)
        else:
            self._update_search_index_entry(path)
    def _convert_file_size_to_bytes(self, size_str):
        """Convert formatted file size string (e.g., '1.23 GB') to bytes."""
        if not size_str or isinstance(size_str, int):
            return size_str or 0
        
        try:
            # Parse "1.23 GB" or "456.78 MB"
            parts = size_str.strip().split()
            if len(parts) >= 2:
                value = float(parts[0])
                unit = parts[1].upper()
                
                if 'GB' in unit:
                    return int(value * 1024 * 1024 * 1024)
                elif 'MB' in unit:
                    return int(value * 1024 * 1024)
                elif 'KB' in unit:
                    return int(value * 1024)
            
            # Try parsing as a number
            return int(float(size_str))
        except:
            return 0
    
    def _track_file_changes(self, library, new_files, removed_files):
        """Track file changes for replacement detection."""
        from datetime import datetime
        
        if not library or library == "All Videos":
            # Can't track changes for all libraries at once
            return
        
        print(f"[File Changes] Tracking for library '{library}': {len(new_files)} new, {len(removed_files)} removed")
        
        # Track deleted files
        for path in removed_files:
            # Get file info before it's gone
            conn = self.db.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT fv.*, v.video_id 
                FROM file_versions fv
                JOIN videos v ON fv.video_id = v.video_id
                WHERE fv.path = ? AND fv.deleted_at IS NOT NULL
            ''', (path,))
            
            row = cursor.fetchone()
            if row:
                file_info = dict(row)
                filename = os.path.basename(path)
                filename_base = os.path.splitext(filename)[0]
                
                # Flag as deleted
                self.db.flag_file_change(
                    filename=filename_base,
                    filepath=path,
                    video_id=file_info['video_id'],
                    library=library,
                    flag_type='deleted',
                    file_metadata={
                        'length_seconds': file_info.get('length_seconds'),
                        'file_size': self._convert_file_size_to_bytes(file_info.get('file_size')),
                        'codec': file_info.get('codec'),
                        'total_bitrate': file_info.get('total_bitrate'),
                        'resolution': file_info.get('resolution')
                    }
                )
                print(f"Flagged deleted file: {filename}")
        
        # Check new files for potential replacements
        for path in new_files:
            filename = os.path.basename(path)
            filename_base = os.path.splitext(filename)[0]
            
            # Get metadata from both functions
            metadata = MediaHandler.get_extended_metadata(path)
            info = MediaHandler.get_file_info(path)
            duration = metadata.get('duration', 0)
            
            # Get raw file size in bytes
            try:
                file_size_bytes = os.path.getsize(path)
            except:
                file_size_bytes = 0
            
            # Look for deleted files that might match (Scenario 1a)
            # This handles when Dir1 deletion is detected before Dir2 scan
            matches = self.db.find_possible_replacements(
                filename=filename_base,
                library=library,
                duration=duration,
                tolerance=5
            )
            
            if matches:
                # Get the deleted file's details
                deleted_file = matches[0]
                
                # AUTO-MERGE if same library + same filename (obvious move within library)
                if deleted_file['library'] == library and deleted_file['filename'] == filename_base:
                    print(f"Auto-merging moved file: {deleted_file['filepath']} -> {path}")
                    
                    # Get the old video_id
                    old_video_id = deleted_file['video_id']
                    
                    if old_video_id:
                        # Get the new file's current video_id (it was just created)
                        conn = self.db.get_connection()
                        cursor = conn.cursor()
                        
                        cursor.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,))
                        row = cursor.fetchone()
                        new_video_id = row['video_id'] if row else None
                        
                        # Update the new file to use the old video_id
                        cursor.execute('UPDATE file_versions SET video_id = ? WHERE path = ?', 
                                      (old_video_id, path))
                        
                        # Delete the temporary video entry that was created
                        if new_video_id and new_video_id != old_video_id:
                            cursor.execute('DELETE FROM videos WHERE video_id = ?', (new_video_id,))
                        
                        # Update technical metadata for the new location
                        cursor.execute('''
                            UPDATE file_versions
                            SET length = ?, length_seconds = ?, file_size = ?,
                                codec = ?, total_bitrate = ?, resolution = ?
                            WHERE path = ?
                        ''', (info.get('length', ''), int(duration), info.get('file_size'),
                              info.get('codec', ''), info.get('total_bitrate', ''), 
                              info.get('resolution', ''), path))
                        
                        # Clear the deleted flag
                        cursor.execute('DELETE FROM file_changes WHERE id = ?', (deleted_file['id'],))
                        
                        conn.commit()
                        print(f"Auto-merged: retained metadata for {filename}")
                    
                else:
                    # Different library or different filename - flag for manual review
                    related_id = matches[0]['id']
                    
                    new_flag_id = self.db.flag_file_change(
                        filename=filename_base,
                        filepath=path,
                        video_id=None,  # New file doesn't have video_id in flags yet
                        library=library,
                        flag_type='possible_replacement',
                        file_metadata={
                            'length_seconds': int(duration),
                            'file_size': file_size_bytes,
                            'codec': metadata.get('codec'),
                            'total_bitrate': info.get('total_bitrate'),
                            'resolution': info.get('resolution')
                        },
                        related_id=related_id
                    )
                    
                    # Update original to 'possibly_replaced'
                    conn = self.db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE file_changes 
                        SET flag_type = 'possibly_replaced', related_id = ?
                        WHERE id = ?
                    ''', (new_flag_id, related_id))
                    conn.commit()
                    
                    print(f"Flagged possible replacement: {filename} -> {matches[0]['filename']}")
            else:
                # No match in same library - flag as 'new' for cross-library detection
                self.db.flag_file_change(
                    filename=filename_base,
                    filepath=path,
                    video_id=None,
                    library=library,
                    flag_type='new',
                    file_metadata={
                        'length_seconds': int(duration),
                        'file_size': file_size_bytes,
                        'codec': metadata.get('codec'),
                        'total_bitrate': info.get('total_bitrate'),
                        'resolution': info.get('resolution')
                    }
                )
                print(f"Flagged new file: {filename}")
    
    def check_cross_library_replacements(self):
        """Check for files moved/copied between libraries after all scans complete."""
        if not self.use_sqlite:
            return
        
        # Check 1: Deleted files with new files (moves)
        deleted_flags = self.db.get_file_changes(flag_types=['deleted'])
        
        for deleted in deleted_flags:
            # Look for new files with the same name and length - in another library, or in the
            # same one when the new copy was found before the original went missing (a scan in
            # the same run would already have re-linked it in _track_file_changes)
            conn = self.db.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT fc.* FROM file_changes fc
                WHERE fc.filename = ?
                AND fc.filepath != ?
                AND fc.flag_type = 'new'
                AND (COALESCE(fc.file_length, 0) = 0 OR COALESCE(?, 0) = 0
                     OR ABS(fc.file_length - ?) <= 5)
            ''', (deleted['filename'], deleted['filepath'], deleted['file_length'], deleted['file_length']))
            
            matches = cursor.fetchall()
            
            if matches:
                for match in matches:
                    match_dict = dict(match)
                    
                    # Link them as possible replacements
                    cursor.execute('''
                        UPDATE file_changes 
                        SET flag_type = 'possible_replacement', related_id = ?
                        WHERE id = ?
                    ''', (deleted['id'], match_dict['id']))
                    
                    cursor.execute('''
                        UPDATE file_changes 
                        SET flag_type = 'possibly_replaced', related_id = ?
                        WHERE id = ?
                    ''', (match_dict['id'], deleted['id']))
                    
                    conn.commit()
                    
                    print(f"Cross-library match: {deleted['library']}/{deleted['filename']} -> {match_dict['library']}/{match_dict['filename']}")
        
        # Copies of one video in two different libraries are left as separate videos (only a
        # move - deleted in one place, new in another - is matched across libraries, above).
    
    def get_library_alert_count(self, library):
        """Get count of file changes requiring user attention for a library."""
        if not self.use_sqlite:
            return 0
        
        # Clean up orphaned entries first
        try:
            deleted = self.db.cleanup_orphaned_file_changes()
            if deleted > 0:
                print(f"Cleaned up {deleted} orphaned file_changes entries")
        except Exception as e:
            print(f"Error cleaning orphaned file_changes: {e}")
        
        # Count only actionable flags for this library
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        # Debug: show what's actually in the table for this library
        cursor.execute('SELECT id, flag_type, filename, related_id FROM file_changes WHERE library = ?', (library,))
        all_entries = cursor.fetchall()
        if all_entries:
            print(f"\n=== File changes for library '{library}' ===")
            for row in all_entries:
                print(f"  ID {row[0]}: {row[1]} - {row[2]} (related_id={row[3]})")
        
        cursor.execute('''
            SELECT COUNT(*) FROM file_changes
            WHERE library = ?
            AND flag_type IN ('possible_replacement', 'possible_duplicate')
        ''', (library,))
        
        count = cursor.fetchone()[0]
        print(f"Alert count for '{library}': {count}")
        return count