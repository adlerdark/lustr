# This file is: ./routes/files.py

"""
File operations routes.
Handles filename parsing, import, and export.
"""

import os
import csv
import json
import re
import tempfile
import string
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request, Query
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from typing import List, Optional, Dict
from helpers import check_auth, get_file_data, save_json_file
from search_parser import SearchParser
from config import USE_SQLITE, LIST_FIELDS, DATA_FILE

router = APIRouter(prefix="/api", tags=["files"])

# Global references (will be set by web_server.py)
_model = None

def get_model():
    """Dependency function to get model."""
    return _model

def set_model(model):
    """Set the model instance."""
    global _model
    _model = model


# --- SMART FILENAME PARSER (filename_parser.py): preview, then add-only apply ---
class SmartPreviewRequest(BaseModel):
    paths: Optional[List[str]] = None       # these files, or
    library: Optional[str] = None           # a library or group (videos with something to propose)
    offset: int = 0
    limit: int = 100


class SmartApplyRequest(BaseModel):
    items: List[Dict] = []                  # [{video_id, apply: [{field, value}]}]


class SmartFromPatternRequest(BaseModel):
    items: List[Dict] = []                  # [{path, data: /api/parse_filename result}]


class PatternPreviewRequest(SmartPreviewRequest):
    pattern: str = ''
    options: Dict = {}


def _smart_preview(req, model, pattern=None):
    import filename_parser
    conn = model.db.get_connection()
    if req.paths is not None:
        return filename_parser.preview(conn, model, paths=req.paths[:2000], offset=0, limit=len(req.paths) or 1,
                                       pattern=pattern)
    libs = None
    if req.library and req.library not in ('All Videos', 'Home'):
        libs = model.get_libraries_in_group(req.library) if req.library in (model.library_groups or {}) else [req.library]
    import display_filter
    sql, params = display_filter.condition(conn, model)
    return filename_parser.preview(conn, model, libraries=libs, offset=max(0, req.offset), limit=max(1, min(req.limit, 200)),
                                   extra_where=(sql, params) if sql else None, pattern=pattern)


@router.post("/parse/smart/preview")
def smart_parse_preview(req: SmartPreviewRequest, model = Depends(get_model)):
    return _smart_preview(req, model)


@router.post("/parse/pattern/preview")
def pattern_parse_preview(req: PatternPreviewRequest, model = Depends(get_model)):
    """The %-pattern parser over these files or a library, as the same add-only proposals."""
    def run(filename, path):
        try:
            r = parse_filename(ParseFilenameRequest(filename=filename, path=path, pattern=req.pattern,
                                                    options=req.options or {}), model)
            return r.get('data') if r.get('match') else None
        except Exception:
            return None
    return _smart_preview(req, model, pattern=run)


@router.post("/parse/smart/from-pattern")
def smart_parse_from_pattern(req: SmartFromPatternRequest, model = Depends(get_model)):
    import filename_parser
    return filename_parser.from_values(model.db.get_connection(), model, req.items)


@router.post("/parse/smart/apply")
def smart_parse_apply(req: SmartApplyRequest, model = Depends(get_model)):
    import filename_parser
    result = filename_parser.apply(model.db.get_connection(), req.items)
    try:
        import routes.videos as rv
        rv.clear_filter_caches()
    except Exception:
        pass
    return result


class ForgetLearnedRequest(BaseModel):
    kind: str                               # sites | names | not_names | not_sites
    key: str


@router.get("/parse/learned")
def parse_learned(model = Depends(get_model)):
    """Corrections remembered from Parse Filenames."""
    import filename_parser
    return {'success': True, **filename_parser.get_learned(model.db.get_connection())}


@router.post("/parse/learned/forget")
def parse_learned_forget(req: ForgetLearnedRequest, model = Depends(get_model)):
    import filename_parser
    return {'success': True, **filename_parser.forget(model.db.get_connection(), req.kind, req.key)}


# --- REQUEST MODELS ---
class ParseFilenameRequest(BaseModel):
    filename: str
    pattern: str
    options: Dict[str, bool] = {}
    path: Optional[str] = None   # Full file path — required when pattern uses %D

@router.post("/parse_filename")
def parse_filename(req: ParseFilenameRequest, model = Depends(get_model)):

    def strip_extension(filename):
        """
        Remove the file extension if it is a known video/media extension.
        Falls back to the full filename if no known extension is present,
        so filenames like 'Studio.16.07.19.Jane.Doe' (no extension)
        are not accidentally truncated — previously 'Doe' was stripped as
        if it were an extension, breaking %L capture.
        """
        known_extensions = {
            'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'm4v', 'webm',
            'mpg', 'mpeg', 'm2ts', 'ts', 'vob', 'ogv', '3gp', 'f4v',
        }
        if '.' in filename:
            name, ext = filename.rsplit('.', 1)
            if ext.lower() in known_extensions:
                return name
        return filename

    def generate_regex(pattern):
        """
        Generate regex from pattern tokens.

        Tokens (sorted longest-first so %MM is matched before %M):
          %S   -- site (single dot-part, the shortest that fits; always lowercased)
          %F   -- first name (one word: a single dot-part, no spaces)
          %M   -- middle name (one word)
          %L   -- last name  (one word)
          %C   -- cast (greedy multi-name, smart-split in Python)
          %MC  -- male cast (the same as %C; the names go to Male Cast)
          %T   -- title (greedy to end)
          %Y   -- 4-digit year
          %R   -- resolution (e.g. 1080p)
          %YY  -- 2-4 digit year fragment
          %MM  -- 1-2 digit month  (matched before %M due to length sort)
          %DD  -- 1-2 digit day
          *    -- wildcard (non-capturing, non-greedy)
        """
        escaped = re.escape(pattern)
        escaped = escaped.replace(r'\%', '%')
        escaped = escaped.replace(r'\*', r'(?:.*?)')
        escaped = escaped.replace(r'\.', r'\.')

        has_R = '%R' in pattern

        tokens = {
            '%YY':  r'(?P<yy>\d{1,4})',
            '%MM':  r'(?P<mm>\d{1,2})',
            '%DD':  r'(?P<dd>\d{1,2})',
            '%Y':   r'(?P<year>\d{4})',
            # Enumerate common resolutions explicitly — longest alternatives first
            # so 2160/1080/1440 are tried before 720/480/360/240.
            # This prevents backtracking from matching e.g. "080p" from "1080p".
            '%R':   r'(?P<resolution>(?:2160|1440|1080|720|600|540|480|360|240)p)',
            # [^./]+ : stops at both dots AND slashes so a single dot-segment
            # token can never accidentally consume a path separator. Names are one word, so
            # '%S - %F %L %T' on 'Site - Jane Doe Does It' is Jane / Doe / Does It, not
            # 'Jane Doe Does' / 'It'; the site is the shortest that fits ('%S - %T': the first ' - ').
            '%S':   r'(?P<site>[^./]+?)',
            '%F':   r'(?P<firstname>[^./\s]+)',
            '%M':   r'(?P<middlename>[^./\s]+)',
            '%L':   r'(?P<lastname>[^./\s]+)',
            '%D':   r'(?P<directory>[^/]+)',   # immediate parent directory name
            '%T':   r'(?P<title>.+)',   # greedy; trimmed below when %R present
            '%MC':  r'(?P<male_cast>.+?)',
            '%C':   r'(?P<cast>.+?)',
        }

        for token in sorted(tokens.keys(), key=len, reverse=True):
            escaped = escaped.replace(token, tokens[token])

        # When %R is present, the greedy %T would consume the resolution digits.
        # Replace it with .*\S which stops at the last non-whitespace character
        # before whatever the pattern requires next (the resolution).
        if has_R:
            escaped = escaped.replace(r'(?P<title>.+)', r'(?P<title>.*\S)')

        return f"^{escaped}$"

    def clean_text(text, opts, force_lower=False):
        """Clean and format extracted text."""
        if not text:
            return ""
        res = text
        if opts.get('clean_underscores'):
            res = res.replace('_', ' ')
        if opts.get('clean_dashes'):
            res = res.replace('-', ' ')
        if opts.get('clean_dots'):
            res = res.replace('.', ' ')
        if force_lower:
            res = res.lower()
        elif opts.get('title_case'):
            res = string.capwords(res)
        return res.strip()

    def parse_cast_string(raw, opts):
        """
        Parse a raw dot-separated cast string into a list of formatted names.

        Handles:
          firstname                            -> [Firstname]
          first.last                           -> [Firstname Lastname]
          first.middle.last                    -> [Firstname Middle Lastname]
          first1.last1.first2.last2            -> [Firstname1 Lastname1, Firstname2 Lastname2]
          first1.last1.and.first2.last2        -> split on .and. first
          first1.last1.first2.mid2.last2       -> [First1 Last1, First2 Mid2 Last2]
        """
        if not raw:
            return []

        # Split on .and. first (case-insensitive explicit separator)
        segments = re.split(r'\.and\.', raw, flags=re.IGNORECASE)

        names = []
        for seg in segments:
            parts = [p for p in seg.split('.') if p.strip()]

            if len(parts) == 0:
                continue
            elif len(parts) == 1:
                names.append(clean_text(parts[0], opts))
            elif len(parts) == 2:
                names.append(f"{clean_text(parts[0], opts)} {clean_text(parts[1], opts)}")
            elif len(parts) == 3:
                # Treat as First Middle Last (one person, 3-part name)
                names.append(f"{clean_text(parts[0], opts)} {clean_text(parts[1], opts)} {clean_text(parts[2], opts)}")
            else:
                # 4+ parts: greedily pair as First Last, First Last, ...
                # with any remainder treated as a 3-part name if 3 parts remain
                i = 0
                while i < len(parts):
                    remaining = len(parts) - i
                    if remaining >= 4 and remaining % 2 == 0:
                        names.append(f"{clean_text(parts[i], opts)} {clean_text(parts[i+1], opts)}")
                        i += 2
                    elif remaining == 3:
                        names.append(f"{clean_text(parts[i], opts)} {clean_text(parts[i+1], opts)} {clean_text(parts[i+2], opts)}")
                        i += 3
                    elif remaining == 2:
                        names.append(f"{clean_text(parts[i], opts)} {clean_text(parts[i+1], opts)}")
                        i += 2
                    elif remaining == 1:
                        names.append(clean_text(parts[i], opts))
                        i += 1
                    else:
                        break

        return [n for n in names if n]

    try:
        filename_no_ext = strip_extension(req.filename)
        regex = generate_regex(req.pattern)

        # When the pattern uses %D (directory), prepend the immediate parent
        # directory name to the filename so the regex has something to match
        # against. The separator in the combined string is "/" which matches
        # the literal "/" that the user will write in their pattern, e.g.:
        #   Pattern:  %D/%T        -> "FolderName/Scene 1"   -> title "FolderName Scene 1"
        #   Pattern:  %D           -> "FolderName"            -> title "FolderName"
        #   Pattern:  %D/%S.%C.%T  -> "FolderName/site.Cast.Title"
        # Detect %D token — must NOT match %DD (day-of-month).
        # Use a regex check rather than plain 'in' to avoid the substring false-positive.
        has_dir_token = bool(re.search(r'%D(?!D)', req.pattern))

        if has_dir_token:
            if req.path:
                parent_dir = os.path.basename(os.path.dirname(req.path))
            else:
                parent_dir = '<directory>'
            # If the pattern is ONLY %D (no slash), match against just the
            # directory name. Otherwise match against "dir/filename".
            if req.pattern.strip() == '%D':
                match_target = parent_dir
            else:
                match_target = f"{parent_dir}/{filename_no_ext}"
        else:
            match_target = filename_no_ext

        match = re.search(regex, match_target, re.IGNORECASE)

        if match:
            raw = match.groupdict()

            # ── Fix ambiguous %C.%T boundary when filename contains ".and." ──
            # %C is non-greedy and %T is greedy, so for a filename like
            # "...candee.licious.and.sharon.white.always.the.right.time"
            # %C only captures "candee" and %T swallows the rest, including
            # "licious.and.sharon.white" which is actually a second performer.
            #
            # Reassemble cast+title into dot-parts, find "and", and split
            # the parts on each side into (first performer name, second
            # performer name, remaining title) based on the first performer's
            # length (parts before "and") — preferring a matching length for
            # the second performer, then 1, then 3, leaving the rest for the
            # title when possible.
            # With %MC too, the male cast is the list that runs into the title.
            last = 'male_cast' if raw.get('male_cast') else 'cast'
            if raw.get(last) and raw.get('title'):
                combined = raw[last] + '.' + raw['title']
                all_parts = combined.split('.')

                and_idx = None
                for i, p in enumerate(all_parts):
                    if p.lower() == 'and':
                        and_idx = i
                        break

                if and_idx is not None and and_idx > 0:
                    first_name_parts = all_parts[:and_idx]
                    after_parts = all_parts[and_idx + 1:]

                    if 1 <= len(first_name_parts) <= 3:
                        n1 = len(first_name_parts)
                        # Preferred second-name lengths given first-name length
                        pref = {1: (1, 2, 3), 2: (2, 1, 3), 3: (2, 1, 3)}[n1]

                        best = None
                        for after_take in pref:
                            if after_take > len(after_parts):
                                continue
                            second_name_parts = after_parts[:after_take]
                            remaining_title_parts = after_parts[after_take:]
                            if remaining_title_parts:
                                best = (second_name_parts, remaining_title_parts)
                                break
                            elif best is None:
                                # fallback: consumes everything, empty title
                                best = (second_name_parts, remaining_title_parts)

                        if best is not None:
                            second_name_parts, remaining_title_parts = best
                            raw[last] = '.'.join(first_name_parts + ['and'] + second_name_parts)
                            raw['title'] = '.'.join(remaining_title_parts)

            clean = {}

            for key, value in raw.items():
                if value is None:
                    continue

                if key in ('cast', 'male_cast'):
                    # Names written with spaces: 'A B, C D and E F' / 'A B & C D' -> split on the commas and the and / &
                    if re.search(r',\s|\s(?:and|&)\s', value, re.IGNORECASE):
                        clean[key] = [clean_text(n.strip(), req.options)
                                      for n in re.split(r',\s*|\s+(?:and|&)\s+', value, flags=re.IGNORECASE) if n.strip()]
                    else:
                        clean[key] = parse_cast_string(value, req.options)

                elif key == 'site':
                    # %S is always lowercased regardless of title_case option
                    clean['site'] = clean_text(value, req.options, force_lower=True)

                elif key == 'directory':
                    # Store cleaned directory name separately for now;
                    # combined with title below once all keys are processed.
                    clean['_directory'] = clean_text(value, req.options)

                else:
                    clean[key] = clean_text(value, req.options)

            # 'Put the folder name at the start of the title' (options.dir_in_title)
            if req.options.get('dir_in_title') and '_directory' not in clean:
                folder = os.path.basename(os.path.dirname(req.path)) if req.path else '<folder>'
                clean['_directory'] = clean_text(folder, req.options)

            # Combine %D (directory) with %T (title) if both were captured (older patterns; now the option above).
            # %D/%T pattern: title = "FolderName Scene 1"
            # %D alone:      title = "FolderName"
            if '_directory' in clean:
                dir_part = clean.pop('_directory')
                existing_title = clean.get('title', '').strip()
                if existing_title:
                    clean['title'] = f"{dir_part} {existing_title}"
                else:
                    clean['title'] = dir_part

            # Handle date fragments
            if 'yy' in clean and 'mm' in clean and 'dd' in clean:
                try:
                    yy = clean['yy'].zfill(2)
                    mm = clean['mm'].zfill(2)
                    dd = clean['dd'].zfill(2)
                    clean['date_created'] = f"20{yy}-{mm}-{dd}"
                    if 'year' not in clean:
                        clean['year'] = f"20{yy}"
                except Exception:
                    pass

            # Build cast from explicit %F %M %L tokens if %C wasn't used
            if 'cast' not in clean:
                parts = []
                if 'firstname' in clean:
                    parts.append(clean['firstname'])
                if 'middlename' in clean:
                    parts.append(clean['middlename'])
                if 'lastname' in clean:
                    parts.append(clean['lastname'])
                if parts:
                    clean['cast'] = [' '.join(parts)]

            # Remove the raw name fragments — they've been folded into cast.
            # Leaving them in would cause the frontend to try writing
            # 'firstname'/'lastname' as scalar video fields (they don't exist).
            for _k in ('firstname', 'middlename', 'lastname'):
                clean.pop(_k, None)

            return {"match": True, "data": clean}

    except Exception as e:
        print(f"Parse error: {e}")

    return {"match": False}



# --- SPREADSHEET EXPORT / IMPORT (metadata_import.py) ---
def _export_paths(request, model, library, query):
    """One path per video in the library / group (or All Videos), with the search applied - as the grid shows them."""
    import collections_store
    rules = {'library': 'All Videos' if library in (None, '', 'Home') else library, 'query': query or ''}
    d = collections_store._smart_query(model, request, rules, 1, 1, paths_only=True)
    return [x['path'] for x in d.get('paths', []) if x.get('path')]


def _safe_name(name):
    name = re.sub(r'[\'"/\\:*?<>|]', '', (name or 'export').lower())
    return re.sub(r'_+', '_', re.sub(r'\s+', '_', name)).strip('_') or 'export'


@router.get("/export")
def export_csv(request: Request, model = Depends(get_model), library: str = Query(None), query: str = Query(None),
               template: bool = Query(False)):
    """The videos of a library / group / search as CSV, to edit in a spreadsheet and import back.
    template=1: only the paths and filenames filled in."""
    import metadata_import
    from fastapi.responses import Response
    paths = _export_paths(request, model, library, query)
    if not paths:
        raise HTTPException(400, "No videos to export here")
    text = metadata_import.export_csv(model.db.get_connection(), model.db.get_file, paths, template=template)
    label = (library if library not in (None, '', 'Home', 'All Videos') else 'all_videos') + ('_search' if query else '')
    fname = f"lustr_{_safe_name(label)}{'_template' if template else ''}.csv"
    return Response('\ufeff' + text, media_type='text/csv; charset=utf-8',          # the BOM: Excel reads it as UTF-8
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


class ImportPlanBody(BaseModel):
    token: str
    modes: Dict[str, str] = {}


def _import_plan(model, token, modes):
    import metadata_import
    rows, name = metadata_import.stashed(token)
    summary, work = metadata_import.plan(model.db.get_connection(), model.db.get_file, rows, modes)
    return dict(summary, token=token, file=name), work


@router.post("/import/upload")
async def import_upload(file: UploadFile, model = Depends(get_model)):
    """Read a CSV and show what importing it would change (nothing is written yet)."""
    import metadata_import
    try:
        _cols, rows = metadata_import.read_csv(await file.read())
        token = metadata_import.stash(rows, file.filename)
        return {'success': True, **_import_plan(model, token, {})[0]}
    except metadata_import.ImportError_ as e:
        return {'success': False, 'error': str(e)}
    except csv.Error as e:
        return {'success': False, 'error': f'Could not read the CSV: {e}'}


@router.post("/import/preview")
def import_preview(body: ImportPlanBody, model = Depends(get_model)):
    """The same upload again, with add / replace chosen per list column."""
    import metadata_import
    try:
        return {'success': True, **_import_plan(model, body.token, body.modes)[0]}
    except metadata_import.ImportError_ as e:
        return {'success': False, 'error': str(e)}


@router.post("/import/apply")
def import_apply(body: ImportPlanBody, model = Depends(get_model)):
    """Write the previewed changes (a safety backup of the database first)."""
    import metadata_import
    from routes import metadata as rm
    try:
        summary, work = _import_plan(model, body.token, body.modes)
        conn = model.db.get_connection()

        def apply_one(path, **kw):
            rm.update_metadata(rm.UpdateMetadataRequest(paths=[path], **kw), model)

        def set_favorite(path, on):
            conn.execute('UPDATE videos SET favorite = ? WHERE video_id = (SELECT video_id FROM file_versions WHERE path = ?)',
                         (1 if on else 0, path))
            conn.commit()

        n = metadata_import.apply(conn, work, apply_one, set_favorite)
        metadata_import.stashed(body.token, drop=True)
        try:
            import home
            home.invalidate()
        except Exception:
            pass
        return {'success': True, 'updated': n}
    except metadata_import.ImportError_ as e:
        return {'success': False, 'error': str(e)}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}
