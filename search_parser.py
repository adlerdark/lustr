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

        # Boolean searches - NOT, -term, ( ), or AND / OR - are parsed into a tree (see boolean_tree)
        tree = SearchParser.boolean_tree(query)
        if tree is not None:
            return SearchParser._bool_result(tree)
        
        field_filters = {}
        library_filters = []
        
        # Enhanced pattern to match field:value or field:(value1,value2) or field:"exact value"
        # (a bare value may hold * / % wildcards: cast:jane*, site:*tube)
        field_pattern = r'(\w+):\(([^)]+)\)|(\w+):"([^"]+)"|(\w+):([\w.*%-]+)'
        
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
                field_name = SearchParser._field(match.group(1))
                values_str = match.group(2)
                if values_str.startswith('"') and values_str.endswith('"'):
                    values = [('EXACT', values_str.strip('"'))]
                else:
                    values = [v.strip() for v in values_str.split(',') if v.strip()]
            elif match.group(3):  # Quoted format: field:"exact value"
                field_name = SearchParser._field(match.group(3))
                values = [('EXACT', match.group(4))]
            else:  # Simple format: field:value
                field_name = SearchParser._field(match.group(5))
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
    
    # ------------------------------------------------------------------
    # Boolean search: (a OR b) AND NOT (c OR d), as deep as you like
    # ------------------------------------------------------------------
    # Nodes: ('and', [nodes]) ('or', [nodes]) ('not', node)
    #        ('field', name, [values])  - values as parse_query gives them (('EXACT', v) when quoted)
    #        ('text', word, exact)      - a plain word or "a phrase"
    # NOT binds tightest, then AND (also implied between neighbours), then OR.
    # 'NOT x', 'NOT(x)' and '-x' are the same.
    # Keywords that narrow the whole search (library:, res:, stars:, scene:, captions:, chapters:,
    # markers:, movie:, dates) are pulled out when they simply apply to everything (AND at the top);
    # nested inside OR / NOT they're part of the expression (routes/videos.py _bool_sql).
    SPECIAL_FIELDS = ('library', 'res', 'stars', 'scene', 'captions', 'chapters', 'markers', 'movie',
                      'date_added', 'added', 'date_created', 'date_modified', 'modified')
    _TOKEN = re.compile(r'(?P<neg>-(?=[\w"(]))?(?:(?P<fp>\w+):\((?P<fpv>[^)]*)\)|(?P<fq>\w+):"(?P<fqv>[^"]*)"'
                        r'|(?P<fs>\w+):(?P<fsv>[^\s()"]+)|"(?P<q>[^"]*)")'
                        r'|(?P<paren>[()])|(?P<word>[^\s()"]+)')

    @staticmethod
    def _field(name):
        """A field as typed (its key, or its name in Settings with _ for spaces: hair_colour) -> its key."""
        n = (name or '').lower()
        if n in SearchParser.SPECIAL_FIELDS:
            return n
        try:
            import fields
            return fields.resolve(n)
        except Exception:
            return n

    @staticmethod
    def _tokens(query):
        out = []
        for m in SearchParser._TOKEN.finditer(query):
            neg = bool(m.group('neg'))
            if m.group('paren'):
                out.append(('paren', m.group('paren')))
                continue
            if neg:
                out.append(('op', 'not'))
            if m.group('fp'):
                vs = m.group('fpv').strip()
                if vs.startswith('"') and vs.endswith('"') and len(vs) > 1:
                    values = [('EXACT', vs.strip('"'))]
                else:
                    values = [v.strip() for v in vs.split(',') if v.strip()]
                out.append(('field', SearchParser._field(m.group('fp')), values))
            elif m.group('fq'):
                out.append(('field', SearchParser._field(m.group('fq')), [('EXACT', m.group('fqv'))]))
            elif m.group('fs'):
                out.append(('field', SearchParser._field(m.group('fs')), [m.group('fsv')]))
            elif m.group('q') is not None:
                if m.group('q').strip():
                    out.append(('text', m.group('q').strip(), True))
            else:
                w = m.group('word')
                lw = w.lower()
                if lw in ('and', '&&'):
                    out.append(('op', 'and'))
                elif lw in ('or', '||'):
                    out.append(('op', 'or'))
                elif lw in ('not', '!'):
                    out.append(('op', 'not'))
                elif w.strip('-'):
                    out.append(('text', w, False))
        return out

    @staticmethod
    def boolean_tree(query):
        """The search as a tree, when it uses boolean searching (NOT / -term / parentheses / AND / OR, or one
        field given twice), else None (the simple parser handles it as before)."""
        toks = SearchParser._tokens(query or '')
        fields = [t[1] for t in toks if t[0] == 'field' and t[1] not in SearchParser.SPECIAL_FIELDS]
        if not (any(t[0] in ('op', 'paren') for t in toks) or len(fields) != len(set(fields))):
            return None
        pos = [0]

        def peek():
            return toks[pos[0]] if pos[0] < len(toks) else None

        def parse_or():
            kids = [parse_and()]
            while peek() == ('op', 'or'):
                pos[0] += 1
                kids.append(parse_and())
            kids = [k for k in kids if k is not None]
            return kids[0] if len(kids) == 1 else (('or', kids) if kids else None)

        def parse_and():
            kids = []
            while True:
                t = peek()
                if t is None or t == ('op', 'or') or t == ('paren', ')'):
                    break
                if t == ('op', 'and'):
                    pos[0] += 1
                    continue
                kids.append(parse_not())
            kids = [k for k in kids if k is not None]
            return kids[0] if len(kids) == 1 else (('and', kids) if kids else None)

        def parse_not():
            t = peek()
            if t == ('op', 'not'):
                pos[0] += 1
                inner = parse_not()
                return ('not', inner) if inner is not None else None
            return parse_atom()

        def parse_atom():
            t = peek()
            pos[0] += 1
            if t == ('paren', '('):
                inner = parse_or()
                if peek() == ('paren', ')'):
                    pos[0] += 1
                return inner
            if t[0] in ('field', 'text'):
                return t
            return None                                   # a stray ')' or operator: skipped

        tree = None
        while pos[0] < len(toks):                         # ')' with no '(' before it: go on after it
            part = parse_or()
            tree = part if tree is None else (('and', [tree, part]) if part is not None else tree)
            if peek() == ('paren', ')'):
                pos[0] += 1
        return tree

    @staticmethod
    def _bool_result(tree):
        """parse_query's (base, field_filters, libraries) for a boolean search: what applies to everything
        is pulled out as before; the rest is field_filters['__bool__']."""
        field_filters, libraries = {}, []
        top = tree[1] if tree[0] == 'and' else [tree]
        rest = []
        for node in top:
            if node[0] == 'field' and node[1] in SearchParser.SPECIAL_FIELDS:
                if node[1] == 'library':
                    libraries.extend(v[1] if isinstance(v, tuple) else v for v in node[2])
                else:
                    field_filters.setdefault(node[1], []).extend(node[2])
            else:
                rest.append(node)
        field_filters['__or_groups__'] = []
        if rest:
            field_filters['__bool__'] = rest[0] if len(rest) == 1 else ('and', rest)
        return '', field_filters, libraries

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
    def has_wildcard(value) -> bool:
        return '*' in str(value) or '%' in str(value)

    @staticmethod
    def wildcard_match(pattern: str, value: str) -> bool:
        """The whole value matches the pattern (* or % = any characters), ignoring case: jane* / *tube / *ann*."""
        return re.fullmatch(SearchParser.wildcard_to_regex(str(pattern).lower()), str(value).lower(), re.S) is not None

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
            if field_name.startswith('__'):              # __or_groups__ / __bool__: the SQL paths' business
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
                if field_name == 'cast' and SearchParser.has_wildcard(required_value):
                    import cast_names
                    for field_value in field_data:
                        # jane* also finds the same-name performers 'Jane Doe (1)', ...
                        if (SearchParser.wildcard_match(required_value, field_value)
                                or SearchParser.wildcard_match(required_value, cast_names.base_name(field_value))):
                            match_found = True
                            break
                elif field_name == 'cast':
                    import cast_names
                    plain = cast_names.split_suffix(str(required_value))[1] is None
                    for field_value in field_data:
                        # 'Jane Doe' also matches the same-name performers 'Jane Doe (1)', 'Jane Doe (2)'
                        fv = cast_names.base_name(field_value) if plain else field_value
                        if SearchParser.fuzzy_name_match(required_value, fv, exact_match=exact_match):
                            match_found = True
                            break
                else:
                    # Check for wildcard patterns
                    if SearchParser.has_wildcard(required_lower):
                        for field_value in field_data_lower:
                            if SearchParser.wildcard_match(required_lower, field_value):
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