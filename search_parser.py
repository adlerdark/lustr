# This file is: ./search_parser.py
import re
from typing import Dict, List, Tuple, Optional

class SearchParser:
    """Parses advanced search queries with field-specific and library filters."""
    
    @staticmethod
    def parse_query(query: str) -> Tuple[str, Dict[str, List], List[str]]:
        """
        Parse a search query into:
        - base_query: remaining text after extracting special filters
        - field_filters: dict of {field_name: [values]}
        - library_filters: list of library names
        
        Supports:
        - library:name or library:(name1,name2)
        - field:value or field:(value1,value2)
        - field:"exact value" for exact matches
        - field:("exact value") for exact matches in parentheses
        - path:partial_path (searches within file paths)
        - Wildcards: * or % for pattern matching
        - Boolean operators (and, or, not) in remaining text
        - field:value1 OR field:value2 → Multiple values for same field
        - Parentheses for grouping in base query
        """
        if not query:
            return "", {}, []
        
        field_filters = {}
        library_filters = []
        
        # Enhanced pattern to match field:value or field:(value1,value2) or field:"exact value"
        field_pattern = r'(\w+):\(([^)]+)\)|(\w+):"([^"]+)"|(\w+):([\w.-]+)'
        
        remaining_parts = []
        last_end = 0
        
        # Track each extracted field term with the conjunction that PRECEDED it.
        # We use this to detect cross-field OR groups (issue 4).
        # Each entry: {'field': str, 'values': list, 'conjunction': 'and'|'or'|'start'}
        field_terms = []
        
        for match in re.finditer(field_pattern, query):
            # Text between previous match end and this match start
            between = query[last_end:match.start()].strip().lower()
            conjunction = 'or' if 'or' in between.split() else 'and'
            if not field_terms:
                conjunction = 'start'

            # Add non-operator text to remaining parts
            before_text = query[last_end:match.start()].strip()
            if before_text and before_text.lower() not in ['or', 'and', 'not']:
                remaining_parts.append(before_text)
            
            if match.group(1):  # Parenthesized format: field:(value1,value2)
                field_name = match.group(1).lower()
                values_str = match.group(2)
                if values_str.startswith('"') and values_str.endswith('"'):
                    values = [('EXACT', values_str.strip('"'))]
                else:
                    values = [v.strip() for v in values_str.split(',') if v.strip()]
            elif match.group(3):  # Quoted format: field:"exact value"
                field_name = match.group(3).lower()
                values = [('EXACT', match.group(4))]
            else:  # Simple format: field:value
                field_name = match.group(5).lower()
                value = match.group(6).strip()
                if value.startswith('"') and value.endswith('"'):
                    values = [('EXACT', value.strip('"'))]
                else:
                    values = [value]
            
            # Special handling for library
            if field_name == 'library':
                extracted_values = []
                for v in values:
                    if isinstance(v, tuple):
                        extracted_values.append(v[1])
                    else:
                        extracted_values.append(v)
                library_filters.extend(extracted_values)
            else:
                # Accumulate into field_filters (same-field OR still works as before)
                if field_name not in field_filters:
                    field_filters[field_name] = []
                field_filters[field_name].extend(values)
                
                # Track for cross-field OR grouping
                field_terms.append({
                    'field': field_name,
                    'values': values,
                    'conjunction': conjunction
                })
            
            last_end = match.end()
        
        # Add remaining text after last match
        after_text = query[last_end:].strip()
        if after_text and after_text.lower() not in ['or', 'and', 'not']:
            remaining_parts.append(after_text)
        
        base_query = ' '.join(remaining_parts).strip()
        
        # Strip leftover parentheses/operators
        if base_query:
            cleaned = re.sub(r'[\(\)\s]+', ' ', base_query).strip()
            leftover_words = [w for w in cleaned.split()
                              if w.lower() not in ('and', 'or', 'not')
                              and not re.match(r'^[*%]+$', w)]
            base_query = cleaned if leftover_words else ''

        # Build or_groups: a list of AND-separated groups, where each group is
        # a list of OR-connected {field, values} dicts.
        # Example: "face:braces OR face:goth OR cumshot:bukkake" →
        #   or_groups = [[{face:[braces,goth]}, {cumshot:[bukkake]}]]
        # Example: "face:braces AND cumshot:facial" →
        #   or_groups = [[{face:[braces]}], [{cumshot:[facial]}]]
        or_groups = []
        current_or_group = {}   # {field: [values]} for current OR-chain
        
        for term in field_terms:
            conj = term['conjunction']
            field = term['field']
            values = term['values']
            
            if conj in ('start', 'and') and current_or_group:
                # Flush current OR group, start a new one
                or_groups.append(current_or_group)
                current_or_group = {}
            
            # Accumulate into current OR group
            if field not in current_or_group:
                current_or_group[field] = []
            current_or_group[field].extend(values)
        
        if current_or_group:
            or_groups.append(current_or_group)

        # Attach or_groups to field_filters under a special key so callers
        # that understand it can use cross-field OR, while callers that don't
        # can fall back to field_filters as before.
        field_filters['__or_groups__'] = or_groups
        
        return base_query, field_filters, library_filters
    
    @staticmethod
    def wildcard_to_regex(pattern: str) -> str:
        """
        Convert a wildcard pattern (* or %) to regex.
        * or % matches any characters
        """
        # Escape special regex chars except * and %
        pattern = re.escape(pattern)
        # Replace escaped wildcards with regex .*
        pattern = pattern.replace(r'\*', '.*').replace(r'\%', '.*')
        return pattern
    
    @staticmethod
    def matches_path(path: str, search_patterns: List[str]) -> bool:
        """
        Check if path matches any of the search patterns.
        Supports wildcards (* or %) and partial matches.
        Now supports forward slashes (/) in search patterns.
        
        Examples:
        - "videos" matches "/media/videos/file.mp4"
        - "20*" matches "/media/2023/file.mp4"
        - "mov*drama" matches "/media/movies/drama/file.mp4"
        - "s/site*" matches "/media/s/site123/file.mp4"
        - "/media/s/*" matches "/media/s/anything/file.mp4"
        """
        # Normalize path to use forward slashes for consistent comparison
        path_normalized = path.replace('\\', '/').lower()
        
        for pattern in search_patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            
            # Normalize pattern to use forward slashes
            pattern = pattern.replace('\\', '/').lower()
            
            # Check if pattern contains wildcards
            if '*' in pattern or '%' in pattern:
                # Convert to regex and match
                regex_pattern = SearchParser.wildcard_to_regex(pattern)
                if re.search(regex_pattern, path_normalized):
                    return True
            else:
                # Check if pattern contains forward slashes (path-based search)
                if '/' in pattern:
                    # Direct path matching - pattern must be in the path
                    if pattern in path_normalized:
                        return True
                else:
                    # Simple substring match for path components (backwards compatible)
                    # Split path into components and check each
                    path_parts = path_normalized.split('/')
                    
                    # Check if pattern matches any path component (folder or file name)
                    for part in path_parts:
                        if pattern in part:
                            return True
        
        return False
    
    @staticmethod
    def normalize_name(name: str) -> str:
        """
        Normalize a name for fuzzy matching.
        - Removes periods
        - Converts to lowercase
        - Collapses multiple spaces
        - Strips whitespace
        """
        return re.sub(r'\s+', ' ', name.replace('.', ' ').lower()).strip()
    
    @staticmethod
    def fuzzy_name_match(search_term: str, target: str, exact_match: bool = False) -> bool:
        """
        Check if search term matches target with fuzzy logic for cast names.
        
        Args:
            search_term: The search term to match
            target: The target name to match against
            exact_match: If True, require exact match after normalization
        
        Strategy for partial name searches:
        - Single word searches (e.g., "John" or "Smith") match any name containing that word
        - Requires minimum 3 characters for fuzzy matching (prevents Alice matching Allie)
        - First 2 characters must match exactly (prevents false positives)
        - Allows for 1-2 character typos in remaining characters
        
        Strategy for full name searches:
        - For full names (2+ words), BOTH parts must match closely
        - Allows for small typos (1-2 characters difference per word)
        
        Examples that SHOULD match:
        - "Alice" → "Alice", "Alice Cooper", "Alice Springs"
        - "Alice Cooper" → "Alice Cooper" (exact match after normalization)
        - "Alica Cooper" → "Alice Cooper" (1 character typo allowed)
        - "Alice Coper" → "Alice Cooper" (1 character typo allowed)
        - "Smith" → "John Smith", "Agent Smith"
        
        Examples that should NOT match:
        - "Alice" → "Alicia" (too many character differences)
        - "Penny" → "Benny" or "Jenny" (first character differs)
        - "Alice Cooper" → "Alice" (missing last name)
        - "Alice" → "Alice Cooper" when exact_match=True
        """
        if not search_term or not target:
            return False
        
        # Normalize both strings
        search_normalized = SearchParser.normalize_name(search_term)
        target_normalized = SearchParser.normalize_name(target)
        
        # Exact match mode
        if exact_match:
            return search_normalized == target_normalized
        
        # Split into words
        search_words = search_normalized.split()
        target_words = target_normalized.split()
        
        # --- SPECIAL CASE: Single word search (partial name) ---
        if len(search_words) == 1:
            search_word = search_words[0]
            
            # Minimum 3 characters for fuzzy matching (prevents "Ali" matching "Alison", "Alicia", etc.)
            if len(search_word) < 3:
                # For very short searches, require exact word match
                return search_word in target_words
            
            # Check for exact word match first (most common case)
            if search_word in target_words:
                return True
            
            # Fuzzy match against each word in target
            for target_word in target_words:
                # Skip if target word is too short
                if len(target_word) < 3:
                    continue
                
                # First 2 characters must match exactly
                # This prevents "Penny" from matching "Benny", "Jenny", etc.
                # which are likely different people, not typos
                if search_word[:2] != target_word[:2]:
                    continue
                
                # Check character differences
                # Allow up to 2 character difference for fuzzy matching
                diff = abs(len(search_word) - len(target_word))
                if diff > 2:
                    continue
                
                # Count character differences position by position
                max_len = max(len(search_word), len(target_word))
                min_len = min(len(search_word), len(target_word))
                char_diff = sum(1 for i in range(min_len) if search_word[i] != target_word[i])
                char_diff += (max_len - min_len)  # Add length difference
                
                # Allow up to 2 character differences total
                if char_diff <= 2:
                    return True
            
            return False
        
        # --- FULL NAME SEARCH (2+ words) ---
        else:
            # For full names, require same number of words
            if len(search_words) != len(target_words):
                return False
            
            # Each search word must match the corresponding target word (allowing for typos)
            # Track which target indices have been matched
            matched_target_indices = set()
            words_with_diffs = 0
            
            for search_word in search_words:
                found_match = False
                best_char_diff = float('inf')
                best_idx = None
                
                # Try to match this search word to an unmatched target word
                for idx, target_word in enumerate(target_words):
                    if idx in matched_target_indices:
                        continue
                    
                    # Exact match (most common case)
                    if search_word == target_word:
                        matched_target_indices.add(idx)
                        found_match = True
                        break
                    
                    # Fuzzy match: First 2 characters must match
                    # This prevents "Penny" from matching "Benny", "Jenny", etc.
                    # which are likely different people, not typos
                    if search_word[0] != target_word[0]:
                        continue
                    
                    # Allow up to 2 character difference per word for matching consideration
                    diff = abs(len(search_word) - len(target_word))
                    if diff > 2:
                        continue
                    
                    max_len = max(len(search_word), len(target_word))
                    min_len = min(len(search_word), len(target_word))
                    char_diff = sum(1 for i in range(min_len) if search_word[i] != target_word[i]) + (max_len - min_len)
                    
                    # Track the best match for this search word (up to 2 diffs)
                    if char_diff <= 2 and char_diff < best_char_diff:
                        best_char_diff = char_diff
                        best_idx = idx
                
                # If we found an exact match, continue to next search word
                if found_match:
                    continue
                
                # If we found a fuzzy match, use it
                if best_idx is not None:
                    matched_target_indices.add(best_idx)
                    words_with_diffs += 1
                    
                    # Only ONE word can have differences!
                    # This prevents "Penny Pax" from matching "Banny Paxx" (both words have diffs)
                    if words_with_diffs > 1:
                        return False
                else:
                    # One of the search words didn't match any target word
                    return False
            
            # All search words matched with acceptable total differences!
            return True
        
        # Mismatch in word count or other cases
        return False
    
    @staticmethod
    def matches_field_filters(data: Dict, field_filters: Dict[str, List]) -> bool:
        """
        Check if a video's data matches all field filters.
        
        Now supports exact matching via ('EXACT', value) tuples in field_filters.
        
        Logic:
        - Within a field: OR (any value matches)
        - Across fields: AND (all fields must match)
        """
        if not field_filters:
            return True
        
        # Check each field
        for field_name, required_values in field_filters.items():
            if field_name == '__or_groups__':
                continue
            field_data = data.get(field_name, [])
            
            # Convert to list if it's a string
            if isinstance(field_data, str):
                field_data = [field_data]
            elif not isinstance(field_data, list):
                field_data = [str(field_data)]
            
            # Normalize for comparison
            field_data_lower = [str(v).lower() for v in field_data]
            
            # Check if ANY of the required values match (OR within field)
            match_found = False
            for required_value in required_values:
                # Check if this is an exact match request
                exact_match = False
                if isinstance(required_value, tuple) and required_value[0] == 'EXACT':
                    exact_match = True
                    required_value = required_value[1]
                
                required_lower = str(required_value).lower()
                
                # Special fuzzy matching for cast field
                if field_name == 'cast':
                    for field_value in field_data:
                        if SearchParser.fuzzy_name_match(required_value, field_value, exact_match=exact_match):
                            match_found = True
                            break
                else:
                    # Check for wildcard patterns
                    if '*' in required_lower or '%' in required_lower:
                        regex_pattern = SearchParser.wildcard_to_regex(required_lower)
                        for field_value in field_data_lower:
                            if re.search(regex_pattern, field_value):
                                match_found = True
                                break
                    else:
                        # Standard matching for other fields
                        if exact_match:
                            # Exact match required
                            for field_value in field_data_lower:
                                if required_lower == field_value:
                                    match_found = True
                                    break
                        else:
                            # Partial match (substring)
                            for field_value in field_data_lower:
                                if required_lower in field_value or field_value in required_lower:
                                    match_found = True
                                    break
                
                if match_found:
                    break
            
            # If this field had no matches, the overall filter fails (AND across fields)
            if not match_found:
                return False
        
        return True
    
    @staticmethod
    def fuzzy_match(token: str, search_text: str, is_exact: bool = False) -> bool:
        """Match a token against search text with wildcard support."""
        if is_exact:
            return token.lower() in search_text
        
        # Check for wildcards
        if '*' in token or '%' in token:
            regex_pattern = SearchParser.wildcard_to_regex(token.lower())
            return bool(re.search(regex_pattern, search_text))
        
        return token.lower() in search_text
    
    @staticmethod
    def matches_base_query(path: str, data: Dict, query: str, search_index: Optional[Dict] = None) -> bool:
        """Check if a video matches the base text query (after field filters extracted)."""
        if not query.strip():
            return True
        
        # Build search text
        if search_index and path in search_index:
            search_text = search_index[path]
        else:
            import os
            parts = [os.path.basename(path).lower()]
            for val in data.values():
                if isinstance(val, str):
                    parts.append(val.lower())
                elif isinstance(val, list):
                    parts.extend([str(x).lower() for x in val])
            search_text = " ".join(parts)
        
        q_lower = query.lower()
        
        # Check for boolean operators
        operators = [' and ', ' or ', ' not ', '(', ')']
        has_operator = any(op in q_lower for op in operators) or \
                      q_lower.startswith('not ') or q_lower.startswith('(')
        
        if not has_operator:
            # Simple token matching
            tokens = re.findall(r'(?:[^\s"]+|"[^"]*")+', q_lower)
            for token in tokens:
                t = token.strip('"\'')
                if not t:
                    continue
                is_exact = token.startswith('"') and token.endswith('"')
                if not SearchParser.fuzzy_match(t, search_text, is_exact):
                    return False
            return True
        
        # Boolean expression evaluation
        tokens = re.split(r'(\(|\)| and | or | not )', q_lower)
        eval_string = ""
        
        for token in tokens:
            if not token:
                continue
            t_stripped = token.strip()
            
            if t_stripped in ['(', ')', 'and', 'or', 'not']:
                eval_string += f" {t_stripped} "
            elif t_stripped == "":
                continue
            else:
                raw_t = t_stripped.strip('"\'')
                is_exact = t_stripped.startswith('"') and t_stripped.endswith('"')
                is_match = SearchParser.fuzzy_match(raw_t, search_text, is_exact)
                eval_string += f" {is_match} "
        
        try:
            result = eval(eval_string, {"__builtins__": {}})
            # eval can return non-bool (e.g. empty tuple for "( ) and ( )") — be explicit
            return bool(result)
        except Exception:
            return q_lower in search_text