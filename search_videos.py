#!/usr/bin/env python3
"""
Search videos by metadata field and value.

Usage:
    python3 search_videos.py <field> <value>
    
Examples:
    python3 search_videos.py feature anal
    python3 search_videos.py hair blonde
    python3 search_videos.py cast "Jane Doe"
"""

import sqlite3
import sys
from pathlib import Path

# Database path
DB_PATH = Path(__file__).parent / 'data' / 'library.db'

# All list fields from config
LIST_FIELDS = [
    "Site", "Tags", "Cast", "Ethnicity", "Nationality", "Hair", "Body", 
    "Tits", "Ass", "Face", "Cock", "Outfit", "Theme", 
    "Feature", "Cumshot", "Participants", "Orientation"
]

def search_videos(field, value):
    """Search for videos with a specific field value (case-insensitive)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    field_lower = field.lower()
    
    # Validate field
    if field_lower not in [f.lower() for f in LIST_FIELDS]:
        print(f"Error: Invalid field '{field}'")
        print(f"Valid fields: {', '.join(LIST_FIELDS)}")
        conn.close()
        return []
    
    table_name = f'video_{field_lower}'
    junction_table = f'video_{field_lower}_junction'
    field_id_col = f'{field_lower}_id'
    
    try:
        # Get videos with this field value (case-insensitive)
        cursor.execute(f'''
            SELECT DISTINCT v.video_id, v.filename_base, v.library, v.title, 
                   fv.path, fv.filename, fv.length, fv.file_size
            FROM videos v
            JOIN {junction_table} vfj ON v.video_id = vfj.video_id
            JOIN {table_name} vf ON vfj.{field_id_col} = vf.id
            JOIN file_versions fv ON v.video_id = fv.video_id AND fv.is_preferred = 1
            WHERE LOWER(vf.value) = LOWER(?)
            AND fv.deleted_at IS NULL
            ORDER BY v.filename_base
        ''', (value,))
        
        results = cursor.fetchall()
        conn.close()
        return results
        
    except sqlite3.OperationalError as e:
        print(f"Error: {e}")
        print(f"Field '{field}' table might not exist")
        conn.close()
        return []

def format_size(size_bytes):
    """Format file size in human-readable format."""
    if not size_bytes:
        return "N/A"
    
    try:
        size = float(size_bytes)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    except:
        return size_bytes

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 search_videos.py <field> <value>")
        print()
        print("Examples:")
        print("  python3 search_videos.py feature anal")
        print("  python3 search_videos.py hair blonde")
        print('  python3 search_videos.py cast "Jane Doe"')
        print()
        print(f"Valid fields: {', '.join(LIST_FIELDS)}")
        sys.exit(1)
    
    if not DB_PATH.exists():
        print(f"Error: Database not found at {DB_PATH}")
        sys.exit(1)
    
    field = sys.argv[1]
    value = ' '.join(sys.argv[2:])  # Join remaining args in case value has spaces
    
    print(f"Searching for videos where {field.upper()} = '{value}'")
    print("=" * 80)
    
    results = search_videos(field, value)
    
    if not results:
        print(f"\nNo videos found with {field.upper()} = '{value}'")
        sys.exit(0)
    
    print(f"\nFound {len(results)} video(s):\n")
    
    for idx, (video_id, filename_base, library, title, path, filename, length, file_size) in enumerate(results, 1):
        print(f"{idx}. {filename_base}")
        print(f"   Library: {library}")
        if title:
            print(f"   Title: {title}")
        print(f"   File: {filename}")
        if length:
            print(f"   Length: {length}")
        if file_size:
            print(f"   Size: {format_size(file_size)}")
        print(f"   Path: {path}")
        print()

if __name__ == "__main__":
    main()
