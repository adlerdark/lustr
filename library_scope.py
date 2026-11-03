# This file is: ./library_scope.py
"""Which videos are in a library. A video is in every library that holds one of its files (a merged
video can have files in several), and is shown once, by its primary file. file_versions.library is
the library a file sits in (database.refresh_file_libraries); rows without one go by videos.library."""

EVERYTHING = ('Home', 'All Videos', 'all', '', None)


def expand(model, library):
    """A library or group -> its library names; None for everything."""
    if library in EVERYTHING:
        return None
    if library in (getattr(model, 'library_groups', None) or {}):
        return model.get_libraries_in_group(library)
    return [library]


def videos_in(libs, col='v.video_id'):
    """(sql, params): the video in `col` has a file in one of these libraries."""
    libs = list(libs)
    return (f'{col} IN (SELECT lsf.video_id FROM file_versions lsf JOIN videos lsv ON lsv.video_id = lsf.video_id '
            f'WHERE lsf.deleted_at IS NULL AND COALESCE(lsf.library, lsv.library) IN ({",".join("?" * len(libs)) or "NULL"}))',
            libs)


def files_in(libs, fv='fv', v='v'):
    """(sql, params): the file `fv` (of video `v`) sits in one of these libraries."""
    libs = list(libs)
    return f'COALESCE({fv}.library, {v}.library) IN ({",".join("?" * len(libs)) or "NULL"})', libs
