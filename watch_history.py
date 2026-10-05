# This file is: ./watch_history.py

import os
import json
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from config import USE_SQLITE, DATABASE_FILE

if USE_SQLITE:
    from database import Database

# Import logging
try:
    from logger import log_info, log_debug
except ImportError:
    def log_info(msg): print(f"[INFO] {msg}")
    def log_debug(msg): print(f"[DEBUG] {msg}")


class WatchHistory:
    """Tracks video playback history and progress per user."""
    
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.history_file = os.path.join(data_dir, 'watch_history.json')
        # self.use_sqlite = USE_SQLITE
        self.use_sqlite = False
        self.history: Dict[str, Dict] = {}  # {username: {path: {last_watched, progress, duration}}}
        self._save_lock = threading.Lock()
        
        if self.use_sqlite:
            self.db = Database(DATABASE_FILE)
            print(f"✓ Watch history using SQLite: {DATABASE_FILE}")
        else:
            self.load()
    
    def load(self):
        """Load watch history from JSON file."""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.history = json.load(f)
            except Exception as e:
                print(f"Error loading watch history: {e}")
                self.history = {}
    
    def save(self):
        """Save watch history to JSON file (async)."""
        if self.use_sqlite:
            return  # No need to save to JSON when using SQLite
        
        def _write():
            with self._save_lock:
                try:
                    temp_file = self.history_file + ".tmp"
                    with open(temp_file, 'w', encoding='utf-8') as f:
                        json.dump(self.history, f, indent=4)
                    if os.path.exists(self.history_file):
                        os.remove(self.history_file)
                    os.rename(temp_file, self.history_file)
                except Exception as e:
                    print(f"Error saving watch history: {e}")
        threading.Thread(target=_write, daemon=True).start()
    
    def record_play(self, username: str, path: str, duration: float = 0):
        """Record that a video was played."""
        if self.use_sqlite:
            self.db.save_watch_progress(username, path, 0, duration, False)
        else:
            if username not in self.history:
                self.history[username] = {}
            
            self.history[username][path] = {
                'last_watched': datetime.now().isoformat(),
                'progress': 0,
                'duration': duration,
                'completed': False
            }
            self.save()
    
    def update_progress(self, username: str, path: str, current_time: float, duration: float):
        """Update playback progress for a video."""
        # Mark as completed if watched >90%
        completed = duration > 0 and (current_time / duration) > 0.9
        
        # Log progress saves (use info level so it shows up)
        filename = os.path.basename(path)
        progress_pct = (current_time / duration * 100) if duration > 0 else 0
        log_info(f"Progress: {username} - {filename} - {current_time:.1f}s/{duration:.1f}s ({progress_pct:.0f}%) {'✓ COMPLETED' if completed else ''}")
        
        if self.use_sqlite:
            self.db.save_watch_progress(username, path, current_time, duration, completed)
        else:
            if username not in self.history:
                self.history[username] = {}
            
            if path not in self.history[username]:
                self.history[username][path] = {
                    'last_watched': datetime.now().isoformat(),
                    'progress': 0,
                    'duration': duration,
                    'completed': False
                }
            
            # Update progress
            self.history[username][path]['progress'] = current_time
            self.history[username][path]['duration'] = duration
            self.history[username][path]['last_watched'] = datetime.now().isoformat()
            self.history[username][path]['completed'] = completed
            
            self.save()
    
    def get_recently_watched(self, username: str, limit: int = 20) -> List[Dict]:
        """Get recently watched videos for a user."""
        if self.use_sqlite:
            results = self.db.get_watch_history(username, limit)
            return results
        else:
            if username not in self.history:
                return []
            
            user_history = self.history[username]
            sorted_items = sorted(
                user_history.items(),
                key=lambda x: x[1].get('last_watched', ''),
                reverse=True
            )
            
            result = []
            for path, data in sorted_items[:limit]:
                result.append({
                    'path': path,
                    'last_watched': data.get('last_watched'),
                    'progress': data.get('progress', 0),
                    'duration': data.get('duration', 0),
                    'completed': data.get('completed', False)
                })
            
            return result
    
    def get_continue_watching(self, username: str, limit: int = 10) -> List[Dict]:
        """Get videos that are in-progress (not completed)."""
        if self.use_sqlite:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                SELECT file_path, last_watched, progress, duration
                FROM watch_history
                WHERE username = ? AND completed = 0 AND progress > 0
                ORDER BY last_watched DESC
                LIMIT ?
                ''', (username, limit))
                
                result = []
                for row in cursor.fetchall():
                    result.append({
                        'path': row['file_path'],
                        'last_watched': row['last_watched'],
                        'progress': row['progress'],
                        'duration': row['duration']
                    })
                return result
        else:
            if username not in self.history:
                return []
            
            user_history = self.history[username]
            in_progress = []
            
            for path, data in user_history.items():
                # Only include if not completed and has some progress
                if not data.get('completed', False) and data.get('progress', 0) > 0:
                    in_progress.append({
                        'path': path,
                        'last_watched': data.get('last_watched'),
                        'progress': data.get('progress', 0),
                        'duration': data.get('duration', 0)
                    })
            
            # Sort by last watched
            in_progress.sort(key=lambda x: x.get('last_watched', ''), reverse=True)
            
            return in_progress[:limit]
    
    def get_progress(self, username: str, path: str) -> Optional[Dict]:
        """Get progress for a specific video."""
        if self.use_sqlite:
            return self.db.get_watch_progress(username, path)
        else:
            if username not in self.history or path not in self.history[username]:
                return None
            return self.history[username][path]
    
    def clear_user_history(self, username: str):
        """Clear all watch history for a specific user."""
        if self.use_sqlite:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM watch_history WHERE username = ?', (username,))
                conn.commit()
        else:
            if username in self.history:
                del self.history[username]
                self.save()
    
    def cleanup_old_history(self, username: str, retention_days: int):
        """Remove watch history entries older than retention_days."""
        cutoff_date = datetime.now() - timedelta(days=retention_days)
        
        if self.use_sqlite:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                DELETE FROM watch_history 
                WHERE username = ? AND last_watched < ?
                ''', (username, cutoff_date.isoformat()))
                conn.commit()
                return cursor.rowcount
        else:
            if username not in self.history:
                return 0
            
            items_to_remove = []
            for path, data in self.history[username].items():
                last_watched_str = data.get('last_watched', '')
                try:
                    last_watched = datetime.fromisoformat(last_watched_str)
                    if last_watched < cutoff_date:
                        items_to_remove.append(path)
                except:
                    pass
            
            for path in items_to_remove:
                del self.history[username][path]
            
            if items_to_remove:
                self.save()
            
            return len(items_to_remove)