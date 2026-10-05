# This file is: ./cast_manager.py

import os
import re
from typing import Dict, List, Set
from collections import defaultdict

class CastManager:
    """Handles cast-related operations and suggestions."""
    
    @staticmethod
    def get_cast_videos(files: Dict, cast_name: str, libraries: Dict = None) -> List[Dict]:
        """Get all videos featuring a specific cast member."""
        results = []
        cast_lower = cast_name.lower()
        
        for path, data in files.items():
            if path == "_ui_config":
                continue
            
            cast_list = data.get('cast', [])
            if not isinstance(cast_list, list):
                cast_list = []
            
            # Check if cast member is in the cast list
            for cast_member in cast_list:
                if cast_lower in cast_member.lower():
                    results.append({
                        'path': path,
                        'data': data,
                        'match_type': 'exact'
                    })
                    break
        
        return results
    
    @staticmethod
    def find_potential_matches(files: Dict, cast_name: str) -> List[Dict]:
        """
        Find videos that might feature a cast member based on filename matching.
        This helps identify videos where metadata hasn't been added yet.
        """
        results = []
        
        # Split cast name into parts
        name_parts = cast_name.lower().split()
        if not name_parts:
            return results
        
        # Create search patterns
        # Pattern 1: "firstname.lastname" or "firstname_lastname"
        pattern1 = r'\.'.join(name_parts) if len(name_parts) > 1 else name_parts[0]
        pattern2 = r'_'.join(name_parts) if len(name_parts) > 1 else name_parts[0]
        
        for path, data in files.items():
            if path == "_ui_config":
                continue
            
            # Skip if already in exact matches (has cast metadata)
            cast_list = data.get('cast', [])
            if isinstance(cast_list, list):
                has_exact = any(cast_name.lower() in c.lower() for c in cast_list)
                if has_exact:
                    continue
            
            filename = os.path.basename(path).lower()
            
            # Check for name parts in filename
            match_score = 0
            
            # Check each name part
            for part in name_parts:
                if part in filename:
                    match_score += 1
            
            # If all parts found, it's a potential match
            if match_score == len(name_parts):
                results.append({
                    'path': path,
                    'data': data,
                    'match_type': 'potential',
                    'match_score': match_score
                })
        
        # Sort by match score
        results.sort(key=lambda x: x.get('match_score', 0), reverse=True)
        
        return results
    
    @staticmethod
    def get_all_cast_members(files: Dict) -> List[Dict]:
        """Get all unique cast members with video counts."""
        cast_counts = defaultdict(int)
        
        for path, data in files.items():
            if path == "_ui_config":
                continue
            
            cast_list = data.get('cast', [])
            if isinstance(cast_list, list):
                for cast_member in cast_list:
                    if cast_member:
                        cast_counts[cast_member] += 1
        
        # Convert to sorted list
        result = [
            {'name': name, 'count': count}
            for name, count in cast_counts.items()
        ]
        
        result.sort(key=lambda x: x['count'], reverse=True)
        
        return result
    
    @staticmethod
    def get_cast_bio_data(files: Dict, libraries: Dict, cast_name: str) -> Dict:
        """
        Get comprehensive bio data for a cast member including:
        - Exact matches (videos with cast metadata)
        - Potential matches (filename matches)
        - Library distribution
        - Stats
        """
        exact_matches = CastManager.get_cast_videos(files, cast_name, libraries)
        potential_matches = CastManager.find_potential_matches(files, cast_name)
        
        # Calculate library distribution
        library_distribution = defaultdict(int)
        
        for match in exact_matches:
            path = match['path']
            # Find which library this video belongs to
            for lib_name, lib_paths in libraries.items():
                for lib_path in lib_paths:
                    if path.startswith(os.path.normpath(lib_path) + os.sep):
                        library_distribution[lib_name] += 1
                        break
        
        return {
            'name': cast_name,
            'exact_matches': exact_matches,
            'potential_matches': potential_matches,
            'library_distribution': dict(library_distribution),
            'stats': {
                'total_videos': len(exact_matches),
                'potential_videos': len(potential_matches),
                'libraries': len(library_distribution)
            }
        }