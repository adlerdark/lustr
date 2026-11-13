# This file is: ./value_format.py
"""
The house format for new values: lowercase, spaces become periods ('Big Tits' -> 'big.tits').
A value you already have (in any case or format) keeps its spelling. Names and free text are kept
as typed: cast, male cast, title, movie, the numbers, and fields set to allow spaces (fields.py).

Used by Edit Metadata (routes/metadata.py), Edit Album (images.py) and cast attributes (routes/cast.py).
No FastAPI imports.
"""
import re

EXEMPT = {'cast', 'male_cast', 'title', 'movie', 'year', 'rating', 'movie_scene', 'movie_part', 'movie_whole'}
_TABLE = re.compile(r'^[a-z0-9_]+$')


def as_typed(field):
    """Kept as typed: names and free text (cast, title, movie...) and fields set to allow spaces (fields.py)."""
    f = (field or '').lower()
    if f in EXEMPT:
        return True
    import fields
    return f in fields.layout()['fields'] and not fields.words(f)


def fmt(value):
    v = str(value or '').strip().lower()
    v = re.sub(r'\s+', '.', v)
    return re.sub(r'\.{2,}', '.', v).strip('.')


def known(conn, field):
    """{lowercase: spelling} of the values this field already has (videos, cast attributes, albums)."""
    field = (field or '').lower()
    out = {}
    if not _TABLE.match(field):
        return out
    queries = [(f'SELECT value FROM video_{field}', ()),
               ('SELECT attribute_value FROM cast_attributes WHERE LOWER(attribute_type) = ?', (field,)),
               ('SELECT value FROM image_album_meta WHERE field = ?', (field,))]
    for sql, args in queries:
        try:
            for (v,) in conn.execute(sql, args):
                if v:
                    out.setdefault(str(v).lower(), str(v))
        except Exception:
            pass                                    # no such table (yet)
    return out


def house(field, value, have):
    """One value as it should be saved. have: known(conn, field)."""
    v = str(value or '').strip()
    if not v or as_typed(field):
        return v
    f = fmt(v)
    return have.get(v.lower()) or have.get(f) or f


def house_lists(conn, lists):
    """{field: [values]} -> the same in the house format (fields looked up once each)."""
    out = {}
    for field, values in (lists or {}).items():
        if as_typed(field):
            out[field] = values
            continue
        have = known(conn, field)
        seen, vals = set(), []
        for v in values or []:
            h = house(field, v, have)
            if h and h.lower() not in seen:
                seen.add(h.lower())
                vals.append(h)
        out[field] = vals
    return out
