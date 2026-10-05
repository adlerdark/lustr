# This file is: ./routes/library.py

"""
Library management routes.
Handles library CRUD operations, folder management, and organization.
"""

import os
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any
from media_handler import MediaHandler
from config import USE_SQLITE

router = APIRouter(prefix="/api/library", tags=["library"])

# Global references (will be set by web_server.py)
_model = None

def get_model():
    """Dependency function to get model."""
    return _model

def set_model(model):
    """Set the model instance."""
    global _model
    _model = model



# --- REQUEST MODELS ---

class CreateLibraryRequest(BaseModel):
    name: str


class RenameLibraryRequest(BaseModel):
    old_name: str
    new_name: str


class AddFolderRequest(BaseModel):
    library: str
    path: str


class RemoveFolderRequest(BaseModel):
    library: str
    path: str


class ReorderLibraryRequest(BaseModel):
    order: List[str]
    hidden: List[str]


class SetLibraryGroupsRequest(BaseModel):
    groups: Dict[str, Any]


class LibrarySettingRequest(BaseModel):
    library: str
    setting_key: str
    setting_value: str


# --- ROUTES ---

@router.post("/create")
def create_library(req: CreateLibraryRequest, model = Depends(get_model)):
    """Create a new library."""
    if model.create_library(req.name):
        return {
            "status": "ok",
            "libraries": model.libraries,
            "library_order": model.library_order
        }
    raise HTTPException(status_code=400, detail="Library already exists")


@router.post("/rename")
def rename_library(req: RenameLibraryRequest, model = Depends(get_model)):
    """Rename an existing library."""
    if model.rename_library(req.old_name, req.new_name):
        return {
            "status": "ok",
            "libraries": model.libraries,
            "library_order": model.library_order
        }
    raise HTTPException(status_code=400, detail="Rename failed. Name may already exist.")


@router.post("/reorder")
def reorder_libraries(req: ReorderLibraryRequest, model = Depends(get_model)):
    """Reorder libraries and set hidden status."""
    model.set_library_order(req.order, req.hidden)
    return {"status": "ok"}


@router.post("/set_groups")
def set_library_groups(req: SetLibraryGroupsRequest, model = Depends(get_model)):
    """Set the library groups configuration."""
    model.set_library_groups(req.groups)
    return {"status": "ok", "library_groups": model.library_groups}


@router.post("/setting")
def set_library_setting(req: LibrarySettingRequest, model = Depends(get_model)):
    """Update a setting for a specific library."""
    model.set_library_setting(req.library, req.setting_key, req.setting_value)
    return {"status": "ok", "library_settings": model.library_settings}


@router.delete("/{name}")
def delete_library(name: str, model = Depends(get_model)):
    """Delete a library."""
    if model.delete_library(name):
        return {"status": "ok", "libraries": model.libraries,
                "removed_files": (getattr(model, 'last_library_change', None) or {}).get('count', 0)}
    raise HTTPException(status_code=400, detail="Library not found")


@router.post("/add_folder")
def add_folder(req: AddFolderRequest, model = Depends(get_model)):
    """Add a folder path to a library."""
    if model.add_path_to_library(req.library, req.path):
        return {"status": "ok", "libraries": model.libraries}
    raise HTTPException(status_code=400, detail="Could not add path")


@router.post("/remove_folder")
def remove_folder(req: RemoveFolderRequest, model = Depends(get_model)):
    """Remove a folder path from a library."""
    if model.remove_folder_from_library(req.library, req.path):
        return {"status": "ok", "libraries": model.libraries,
                "removed_files": (getattr(model, 'last_library_change', None) or {}).get('count', 0)}
    raise HTTPException(status_code=400, detail="Folder not found in library")


@router.post("/{name}/thumbnails")
def generate_library_thumbnails(name: str, model = Depends(get_model), force: bool = False):
    """Generate thumbnails for all videos in a library."""
    allowed_prefixes = None
    if name != "All Videos":
        folders = model.libraries.get(name, [])
        allowed_prefixes = tuple(os.path.normpath(p) for p in folders)
    
    count = 0
    
    if USE_SQLITE:
        # Get files from database
        with model.db.get_connection() as conn:
            cursor = conn.cursor()
            
            if allowed_prefixes:
                # Add trailing slash to ensure exact directory match
                conditions = ' OR '.join(['path LIKE ?' for _ in allowed_prefixes])
                query = f'SELECT path FROM file_versions WHERE deleted_at IS NULL AND ({conditions})'
                params = [f"{path}/%" for path in allowed_prefixes]
                cursor.execute(query, params)
            else:
                cursor.execute('SELECT path FROM file_versions WHERE deleted_at IS NULL')
            
            for row in cursor.fetchall():
                path = row['path']
                if os.path.exists(path):
                    if MediaHandler.generate_thumbnails(path, force=force):
                        count += 1
    else:
        # Use JSON files
        for path in model.files:
            if path == "_ui_config":
                continue
            # Check if path starts with any allowed prefix
            if allowed_prefixes and not any(path.startswith(p + '/') for p in allowed_prefixes):
                continue
            if os.path.exists(path):
                if MediaHandler.generate_thumbnails(path, force=force):
                    count += 1
    
    return {"generated": count}