# This file is: ./routes/ui.py

"""
UI routes for templates and initial data.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
from helpers import check_auth

# Two routers: one for root paths, one for /api paths
router = APIRouter(tags=["ui"])  # No prefix for root routes
api_router = APIRouter(prefix="/api", tags=["ui"])  # /api prefix for API routes

# Global references (will be set by web_server.py)
_model = None
_templates = None

def get_model():
    """Dependency function to get model."""
    return _model

def set_model(model):
    """Set the model instance."""
    global _model
    _model = model

def set_templates(templates):
    """Set the templates instance."""
    global _templates
    _templates = templates


@router.get("/")
def serve_home(request: Request):
    """Serve the main application page."""
    from helpers import SESSION
    return _templates.TemplateResponse(
        request=request,
        name="index.html",
        context={'inactivity_timeout': SESSION['timeout'], 'show_inactivity_timer': SESSION['show_countdown']}
    )


class SessionSettingsRequest(BaseModel):
    timeout_minutes: Optional[int] = None      # 0 = never log out for inactivity
    show_countdown: Optional[bool] = None


@api_router.get("/settings/session")
def get_session_settings():
    from helpers import SESSION
    return {'timeout_minutes': SESSION['timeout'] // 60, 'show_countdown': SESSION['show_countdown']}


@api_router.post("/settings/session")
def set_session_settings(req: SessionSettingsRequest, request: Request, model = Depends(get_model)):
    """Auto-logout after inactivity (minutes, 0 = off) and whether the countdown is shown."""
    from helpers import save_session_settings
    s = save_session_settings(model.db, None if req.timeout_minutes is None else max(0, req.timeout_minutes) * 60,
                              req.show_countdown)
    return {'success': True, 'timeout_minutes': s['timeout'] // 60, 'show_countdown': s['show_countdown']}


class DisplayFilterRequest(BaseModel):
    types: List[str] = []


class LibraryTypeRequest(BaseModel):
    library: str
    type: str = ''          # '' = follow the group


class GroupTypeRequest(BaseModel):
    group: str
    type: str = ''          # '' = not set
    rename_from: Optional[str] = None


def _clear_filter_cache():
    try:
        import routes.videos as rv
        with rv._cache_lock:
            rv._filter_cache.clear()
    except Exception:
        pass


@api_router.get("/settings/display-filter")
def get_display_filter(model = Depends(get_model)):
    """The current user's shown library types, and every library's type (display_filter.py)."""
    import display_filter as df
    return {'types': df.get_selection(model.db.get_connection()), **df.types_info(model)}


@api_router.post("/settings/display-filter")
def set_display_filter(req: DisplayFilterRequest, model = Depends(get_model)):
    import display_filter as df
    types = df.set_selection(model.db.get_connection(), df.CURRENT_USER.get(), req.types)
    _clear_filter_cache()
    return {'success': True, 'types': types}


@api_router.post("/library/type")
def set_library_type(req: LibraryTypeRequest, model = Depends(get_model)):
    """A library's orientation type: straight / gay / bisexual / trans / lesbian / mixed, or '' to follow its group."""
    import display_filter as df
    try:
        df.set_library_type(model, req.library, req.type)
    except ValueError as e:
        return {'success': False, 'error': str(e)}
    _clear_filter_cache()
    return {'success': True, **df.types_info(model)}


@api_router.post("/group/type")
def set_group_type(req: GroupTypeRequest, model = Depends(get_model)):
    """A group's orientation type, or '' for not set. rename_from: move the type of a renamed group."""
    import display_filter as df
    try:
        if req.rename_from:
            df.rename_group_type(model, req.rename_from, req.group)
        else:
            df.set_group_type(model, req.group, req.type)
    except ValueError as e:
        return {'success': False, 'error': str(e)}
    _clear_filter_cache()
    return {'success': True, **df.types_info(model)}


@router.get("/install_vlc_link.bat")
def get_vlc_installer():
    """Serve the VLC installer batch file."""
    import os
    
    # Create the batch file content
    bat_content = """@echo off
echo Installing VLC Handler...
reg add "HKEY_CLASSES_ROOT\\vlc" /ve /d "URL:VLC Protocol" /f
reg add "HKEY_CLASSES_ROOT\\vlc" /v "URL Protocol" /d "" /f
reg add "HKEY_CLASSES_ROOT\\vlc\\shell\\open\\command" /ve /d "\\"C:\\Program Files\\VideoLAN\\VLC\\vlc.exe\\" \\"%%1\\"" /f
echo VLC Handler Installed!
pause
"""
    
    # Write to temp file
    temp_path = "/tmp/install_vlc_link.bat"
    with open(temp_path, 'w') as f:
        f.write(bat_content)
    
    return FileResponse(temp_path, media_type='application/octet-stream', filename='install_vlc_link.bat')


# Request models
class UIConfigRequest(BaseModel):
    sort_field: str
    sort_desc: bool
    visible_columns: List[str]
    column_widths: Dict[str, int]
    view_mode: str


class LibrarySetting(BaseModel):
    """Model for library-specific settings."""
    sort_field: Optional[str] = None
    sort_desc: Optional[bool] = None
    hide_zero_length: Optional[str] = None


class LibrarySettingRequest(BaseModel):
    """Request to update library settings."""
    library: str
    settings: LibrarySetting


@api_router.get("/init")
def get_initial_data(model = Depends(get_model)):
    """Get initial application data for UI initialization."""
    from config import LIST_FIELDS, HIDE_ZERO_LENGTH_DEFAULT, USE_SQLITE
    
    # Load UI config from database if using SQLite, otherwise from files
    if USE_SQLITE:
        ui_config = model.db.load_ui_config()
    else:
        ui_config = model.files.get("_ui_config", {})
    
    return {
        "libraries": model.libraries,
        "library_order": model.library_order,
        "hidden_libraries": model.hidden_libraries,
        "library_groups": model.library_groups,
        "tags": model.get_all_tags(),
        "sites": model.get_all_sites(),
        "list_fields": [f.lower() for f in LIST_FIELDS],
        "config": ui_config,
        "all_columns": ["Fav", "Title", "Cast", "Date Created", "File Size", "Length", "Rating", "Site", "Tags", "Path", "Resolution", "Codec", "Extension", "Date Added", "Date Edited"],
        "hide_zero_length_default": HIDE_ZERO_LENGTH_DEFAULT,
        "library_settings": model.library_settings,
        **_display_filter_init(model),
    }


def _display_filter_init(model):
    try:
        import display_filter as df
        return {**df.types_info(model), 'display_filter': df.get_selection(model.db.get_connection())}
    except Exception as e:
        print(f"display filter init: {e}")
        return {}


@api_router.post("/config/save")
def save_ui_config(req: UIConfigRequest, model = Depends(get_model)):
    """Save UI configuration preferences."""
    from config import USE_SQLITE
    
    config_data = {
        "sort_field": req.sort_field,
        "sort_desc": req.sort_desc,
        "visible_columns": req.visible_columns,
        "column_widths": req.column_widths,
        "view_mode": req.view_mode
    }
    
    if USE_SQLITE:
        model.db.save_ui_config(config_data)
    else:
        if "_ui_config" not in model.files:
            model.files["_ui_config"] = {}
        model.files["_ui_config"] = config_data
        model.save()
    
    return {"status": "ok"}


@api_router.post("/library/setting")
def set_library_setting(req: LibrarySettingRequest, model = Depends(get_model)):
    """Save library-specific settings."""
    if req.library not in model.library_settings:
        model.library_settings[req.library] = {}
    
    if req.settings.sort_field is not None:
        model.library_settings[req.library]["sort_field"] = req.settings.sort_field
    if req.settings.sort_desc is not None:
        model.library_settings[req.library]["sort_desc"] = req.settings.sort_desc
    if req.settings.hide_zero_length is not None:
        model.library_settings[req.library]["hide_zero_length"] = req.settings.hide_zero_length
    
    model.save()
    return {"status": "ok"}


@api_router.get("/config/get")
def get_config(request: Request, key: str, model = Depends(get_model)):
    """Get a config value by key from database metadata."""
    check_auth(request)
    from config import USE_SQLITE
    
    if not USE_SQLITE:
        # Fallback for non-SQLite
        return {"value": model.files.get("_ui_config", {}).get(key, "")}
    
    try:
        conn = model.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', (key,))
        row = cursor.fetchone()
        
        if row:
            return {"value": row['value']}
        else:
            return {"value": ""}
    except Exception as e:
        print(f"Error loading config key {key}: {e}")
        return {"value": ""}


@api_router.post("/config/save_key")
def save_config_key(request: Request, data: dict, model = Depends(get_model)):
    """Save a config key-value pair to database metadata."""
    check_auth(request)
    from config import USE_SQLITE
    
    key = data.get('key')
    value = data.get('value', '')
    
    if not key:
        return {"error": "Key is required"}
    
    if not USE_SQLITE:
        # Fallback for non-SQLite
        if "_ui_config" not in model.files:
            model.files["_ui_config"] = {}
        model.files["_ui_config"][key] = value
        model.save()
        return {"success": True}
    
    try:
        conn = model.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, value))
        conn.commit()
        print(f"✓ Saved config key '{key}' to database ({len(value)} chars)")
        return {"success": True}
    except Exception as e:
        print(f"✗ Error saving config key {key}: {e}")
        return {"error": str(e)}

@api_router.get("/quicktags")
def get_quicktags():
    """Get quick tags from JSON file."""
    import os
    import json
    
    # Use the data directory from DATA_FILE path
    from config import DATA_FILE
    data_dir = os.path.dirname(DATA_FILE)
    quicktags_path = os.path.join(data_dir, 'quicktags.json')
    
    # Create default quicktags.json if it doesn't exist
    if not os.path.exists(quicktags_path):
        # A small starting set of common tags; edit them in Settings -> Quick Tags.
        # (The names match what the external-database suggestions use, e.g. small.tits, petite.)
        default_quicktags = {
            "ethnicity": ["white", "black", "latina", "asian", "mixed.race", "indian.middle.eastern"],
            "hair": ["blonde", "brunette", "redhead", "black.hair", "long.hair", "short.hair", "curly.hair"],
            "body": ["very.petite", "petite", "thin", "fit", "curvy.petite", "curvy", "thick", "chubby"],
            "tits": ["very.small.tits", "small.tits", "medium.tits", "big.tits", "very.big.tits", "fake.tits"],
            "ass": ["small.ass", "big.ass", "round.ass"],
            "face": ["cute", "pretty", "sexy", "milf", "cougar", "ugly", "plain", "glasses"],
            "cock": ["big.cock", "cut", "uncut"],
            "outfit": ["lingerie", "stockings", "fishnet", "uniform", "schoolgirl"],
            "participants": ["solo", "one.on.one", "threesome", "foursome", "gangbang", "group"],
            "theme": ["amateur", "interracial", "romantic", "roleplay", "cheating", "teacher", "cuckold", "step.siblings"],
            "feature": ["oral", "anal", "double.penetration", "toys", "outdoor", "blowbang", "squirting"],
            "cumshot": ["facial", "cum.in.mouth", "cum.on.chest", "creampie", "swallowing", "bukkake"],
            "tags": ["full.movie"],
            "orientation": ["straight", "gay", "bisexual", "heteroflexible", "lesbian", "trans"]
        }
        
        with open(quicktags_path, 'w', encoding='utf-8') as f:
            json.dump(default_quicktags, f, indent=2, ensure_ascii=False)
    
    try:
        with open(quicktags_path, 'r', encoding='utf-8') as f:
            quicktags = json.load(f)
        return quicktags
    except Exception as e:
        print(f"Error loading quicktags: {e}")
        return {}


def _format_quicktags(data):
    """Same layout as the hand-written file: one line per field."""
    import json
    lines = [f'  {json.dumps(field, ensure_ascii=False)}: {json.dumps(values, ensure_ascii=False)}'
             for field, values in data.items()]
    return '{\n' + ',\n'.join(lines) + '\n}\n'


@api_router.post("/quicktags")
def save_quicktags(request: Request, data: dict):
    """Replace quicktags.json with {field: [values]} from the Quick Tags editor.
    The old file is copied to data/backups/ first."""
    check_auth(request)
    import os
    import re
    import shutil
    import tempfile
    from datetime import datetime
    from config import DATA_FILE

    clean = {}
    for field, values in (data or {}).items():
        f = str(field).strip().lower()
        if not re.fullmatch(r'[a-z0-9_]+', f):
            return {"success": False, "error": f"Invalid field name: {field}"}
        if not isinstance(values, list):
            return {"success": False, "error": f"Values for {f} must be a list"}
        out, seen = [], set()
        for v in values:
            v = str(v).strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
        clean[f] = out

    data_dir = os.path.dirname(DATA_FILE)
    path = os.path.join(data_dir, 'quicktags.json')
    try:
        if os.path.exists(path):
            backups = os.path.join(data_dir, 'backups')
            os.makedirs(backups, exist_ok=True)
            shutil.copy2(path, os.path.join(backups, f"quicktags-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"))
        fd, tmp = tempfile.mkstemp(dir=data_dir, prefix='.quicktags-', suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(_format_quicktags(clean))
        if os.path.exists(path):
            shutil.copymode(path, tmp)          # mkstemp creates 0600
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        return {"success": True, "quicktags": clean}
    except Exception as e:
        print(f"Error saving quicktags: {e}")
        return {"success": False, "error": str(e)}




# ── Theme upload endpoint ──────────────────────────────────────────────────────

import os as _os
import re as _re

@api_router.post("/themes/upload")
async def upload_theme(request: Request, file: "UploadFile" = None):
    """
    Accept a .css theme file and save it to <data>/themes/ (kept across image updates).
    """
    from fastapi import UploadFile, File, HTTPException
    from fastapi.responses import JSONResponse
    from starlette.datastructures import UploadFile as StarletteUploadFile

    check_auth(request)

    # Parse the multipart form manually since we can't use File(...) at module level
    form = await request.form()
    file = form.get("file")

    if file is None or not hasattr(file, 'filename'):
        raise HTTPException(status_code=400, detail="No file provided")

    if not file.filename.endswith('.css'):
        raise HTTPException(status_code=400, detail="Only .css files are accepted")

    safe_name = _re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
    if not safe_name.endswith('.css'):
        safe_name += '.css'

    BUILTIN = {'default.css', 'light.css', 'ember.css'}
    if safe_name in BUILTIN:
        raise HTTPException(status_code=400, detail=f"Cannot overwrite built-in theme '{safe_name}'")

    from config import DATA_DIR
    themes_dir = _os.path.join(DATA_DIR, 'themes')
    _os.makedirs(themes_dir, exist_ok=True)

    content = await file.read()
    dest = _os.path.join(themes_dir, safe_name)
    with open(dest, 'wb') as f:
        f.write(content)

    return JSONResponse({"ok": True, "id": safe_name.replace('.css', '')})