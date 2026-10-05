# This file is: ./filename_parser.py
"""
Smart filename parser: site, performers, date/year and a readable title from a video's
filename, using your known sites (and their abbreviations), performers and aliases.

Builds on ext_db's parser helpers. Used by the Parse Filenames window (preview, then
add-only apply: routes/smart_parse.py) and by scene matching (ext_db.video_clues).

No FastAPI imports.
"""
import json
import os
import re
import unicodedata
from datetime import datetime

import ext_db
from ext_db import norm_name, _JUNK_TOKENS, _RES_RE, _valid_date, _HYPHEN_APOS, _title_is_noise

VIDEO_EXT_RE = re.compile(r'\.(mp4|mkv|avi|wmv|mov|m4v|flv|mpg|mpeg|webm|ts|3gp|divx|vob|f4v|rm|rmvb|ogv)$', re.I)
DASH_RE = re.compile(r'\s+[-–—]\s+')
SITE_TLD_RE = re.compile(r'\.(com|net|org|xxx|tv|co|uk)$', re.I)
DATE_PATTERNS = [
    (re.compile(r'(?<!\d)((?:19|20)\d{2})[._-](\d{2})[._-](\d{2})(?!\d)'), 'ymd'),
    (re.compile(r'(?<!\d)(\d{2})[._-](\d{2})[._-]((?:19|20)\d{2})(?!\d)'), 'mdy'),
]
LEADING_SHORT_DATE = re.compile(r'^(\d{2})[._](\d{2})[._](\d{2})\s+(.+)$')       # "18.05.11 ExampleSite - ..."
SNN_RE = re.compile(r'^(?P<title>.+?)_s\d{2}_(?P<rest>.+)$', re.I)               # some studios' scene files
HASH_RE = re.compile(r'^(?=(?:[^0-9]*[0-9]){3})(?=(?:[^a-z]*[a-z]){3})[0-9a-z]{12,}(?:_(?:source|\d{3,4}))?$', re.I)
NOISE_TITLE_RE = re.compile(r'^(?:untitled|video|clip|movie|vid|img|dsc|mvi|result)?[\s_-]*\d*$', re.I)
EXTRA_JUNK = {'mp4', 'full', 'fr', 'en'}
CONNECTORS = {'and', '&', 'with', 'x', 'vs', '-'}
# words that make a 2-3 word phrase a title rather than somebody's name
COMMON_WORDS = set('''a an the and or of in on at to for with my your his her their our me him us them i you he she
it we they is are was be been gets got get takes take loves love wants likes fucks fucked fucking fuck sucks suck
big huge small little hot sexy teen teens milf wife hubby husband cuck cuckold slut whore bbc bwc black white
cum cock dick pussy ass anal oral blowjob gangbang threesome mmf ffm mfm orgy creampie facial interracial gay
straight bi trans shower surprise first time part scene vol episode bonus new old best full hd amateur homemade
compilation cam webcam live show sex porn xxx video clip movie eating feeding hungry girl girls guy guys boy boys
man men woman women daddy dad mom step sister brother friend friends party night day room bed hotel office'''.split())
# words that are never part of a new performer's name ('Amy Gangbang', 'The Country Hotwife', 'Bi Group SexTape')
TITLE_ONLY = set('''the a an my your his her our their with and or of in on at to for gets takes loves wants
gangbang gangbanged compilation compilations schoolgirl naughty wanna chill dp dvp group sextape tape remaster
remastered edition creampie creampies anal blowjob blowjobs orgy threesome foursome pov interracial milf teen teens
part scene episode vol bonus bbc bwc cuck cuckold wife hubby husband fuck fucks fucked fucking cock dick pussy ass cum
cumshot sex porn xxx video videos clip clips movie rough desires massage shower surprise party solo casting audition
gay straight bi trans amateur homemade hd studios studio media productions network films pictures entertainment
introducing starring featuring feat ft presents meet horny hotwives cocks dicks pussies tits boobs holes loads
two three four five six seven eight nine ten'''.split())
NAME_OK = {'black', 'white', 'love', 'little'}         # ordinary words that are also common surnames
# a part ending in one of these is a studio, not a person ('Example Studios', 'Example Media')
SITE_SUFFIXES = {'studios', 'studio', 'media', 'productions', 'production', 'pictures', 'films', 'network',
                 'entertainment', 'xxx', 'tv'}
CAMEL_SITE_RE = re.compile(r'[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+')         # 'ExampleSite', 'OnlyFans' (one token)
LEARNED_KEY = 'parse_learned'          # metadata: what you corrected in Parse Filenames


def stem_of(filename):
    return VIDEO_EXT_RE.sub('', filename or '') if VIDEO_EXT_RE.search(filename or '') else \
        re.sub(r'\.[A-Za-z0-9]{2,4}$', '', filename or '')


# ---------------------------------------------------------------------------
# context: your performers, sites and library default sites
# ---------------------------------------------------------------------------
class ParseContext:
    def __init__(self, cast_idx=None, site_keys=None, site_display=None, lib_default=None, genders=None, learned=None):
        self.cast_idx = dict(cast_idx or {})       # norm name -> display (aliases -> primary)
        self.site_keys = dict(site_keys or {})     # norm key / abbreviation -> site key
        self.site_display = dict(site_display or {})   # site key -> the spelling you use most
        self.lib_default = lib_default or {}       # library -> default site value
        self.genders = genders or {}               # performer -> gender (cast_gender)
        # your corrections (see learn()): text -> site / performer, and texts that are not names / sites
        learned = learned or {}
        self.not_names = set(learned.get('not_names') or [])
        self.not_sites = set(learned.get('not_sites') or [])
        for k in self.not_names:
            self.cast_idx.pop(k, None)
        for k, name in (learned.get('names') or {}).items():
            self.cast_idx[k] = name
        for k, value in (learned.get('sites') or {}).items():
            key = ext_db.site_key(value) or k
            self.site_keys[k] = key
            self.site_display.setdefault(key, value)

    @classmethod
    def load(cls, conn):
        groups = ext_db._site_groups(conn)
        links, _ = ext_db._site_rows(conn)
        keys = {k: k for k in groups}
        for k, ln in links.items():
            for ab in ln.get('abbreviations') or []:
                keys.setdefault(norm_name(ab), k)
        try:
            lib_default = {r[0]: r[1] for r in conn.execute('SELECT library, site_value FROM library_default_site')}
        except Exception:
            lib_default = {}
        try:
            genders = {r[0]: (r[1] or '').lower() for r in conn.execute('SELECT cast_name, gender FROM cast_gender')}
        except Exception:
            genders = {}
        return cls(ext_db._cast_index(conn), keys, {k: g['display'] for k, g in groups.items()}, lib_default, genders,
                   get_learned(conn))

    def site_of(self, text):
        """A known site key for this text (a name, 'Name.com', an abbreviation, 'Example Studios'), else None."""
        raw = SITE_TLD_RE.sub('', (text or '').strip())
        k = norm_name(raw)
        if len(k) < 3 or k in self.not_sites:
            return None
        if k in self.site_keys:
            return self.site_keys[k]
        words = raw.split()
        if len(words) > 1 and words[-1].lower() in SITE_SUFFIXES:
            k = norm_name(' '.join(words[:-1]))
            if len(k) >= 3 and k not in self.not_sites:
                return self.site_keys.get(k)
        return None

    def known_name(self, text):
        return self.cast_idx.get(norm_name(text)) if text and len(norm_name(text)) >= 3 else None


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------
def _ascii_quotes(s):
    return (s or '').replace('’', "'").replace('‘', "'").replace('`', "'")


def split_camel(token):
    """'NeverHaveIEverBeen' -> 'Never Have I Ever Been'; 'BBCBully' -> 'BBC Bully'; 'Ways02' -> 'Ways 02'."""
    t = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', token)
    t = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', t)
    t = re.sub(r'(?<=[A-Za-z])(?=\d)', ' ', t)
    return t


def _case(words_text):
    """ALL CAPS or all lowercase -> Title Case; mixed case is kept as written."""
    letters = [c for c in words_text if c.isalpha()]
    if not letters or not (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        return words_text
    out = []
    for w in words_text.split(' '):
        out.append(w[:1].upper() + w[1:].lower() if w else w)
    return ' '.join(out)


def _keep_word(core, prev_res):
    low = core.lower()
    if _RES_RE.match(core) or low in _JUNK_TOKENS or low in EXTRA_JUNK or re.fullmatch(r'[sv]\d{4,}', low):
        return False
    if prev_res and core.isdigit():                     # bitrate after a resolution: "480p_2000"
        return False
    return True


def tidy_title(text):
    """A filename part -> a readable title ('' when it's noise)."""
    t = _ascii_quotes(text).strip()
    if HASH_RE.match(t):
        return ''
    t = re.sub(r"(?<=[A-Za-z])_(s|m|t|re|ll|ve|d)(?=[_\s.\-]|$)", r"'\1", t, flags=re.I)     # Who_s -> Who's
    for pat, repl in _HYPHEN_APOS:
        t = re.sub(pat, repl, t, flags=re.I)
    t = re.sub(r'(?<=[A-Za-z])-\d{1,4}$', '', t)                     # some sites add a number: "...Title-66"
    spaced = ' ' in t.strip()
    if spaced:
        raw = re.sub(r'_+', ' ', t).split()
    else:                                              # snake_case / kebab-case / dot.case / CamelCase
        raw = [w for w in re.split(r'[_.]+|(?<=[A-Za-z0-9])-+(?=[A-Za-z0-9])', t) if w]
    words, prev_res = [], False
    for w in raw:
        core = w.strip('.,;:!?()[]{}-')
        if not _keep_word(core, prev_res):
            prev_res = bool(_RES_RE.match(core)) or prev_res
            continue
        prev_res = False
        words.append(w if spaced else split_camel(w))
    t = re.sub(r'\s+', ' ', ' '.join(words)).strip(' -_.,')
    t = _case(t)
    t = re.sub(r'\bscene\s*-?\s*(\d{1,3})\b', lambda m: 'Scene ' + m.group(1), t, flags=re.I)
    if not t or NOISE_TITLE_RE.fullmatch(t) or all(w.lower() in CONNECTORS for w in t.split()):
        return ''
    return t


def _name_display(piece):
    p = split_camel(piece) if ' ' not in piece.strip() and re.search(r'[a-z][A-Z]', piece) else piece
    p = re.sub(r'[_.]+', ' ', p).strip()
    return _case(re.sub(r'\s+', ' ', p))


def name_like(piece, ctx):
    """A new (unknown) performer name? 2-3 words, letters, not a site, not ordinary title words."""
    if re.search(r'[A-Z]{2,}', piece) and piece.upper() == piece:      # ALL CAPS: titles, rarely names
        return None
    p = _name_display(piece)
    words = p.split()
    if not 2 <= len(words) <= 3 or len(p) > 32:
        return None
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", w) for w in words):
        return None
    if all(w.lower() in COMMON_WORDS for w in words) or sum(w.lower() in COMMON_WORDS for w in words) >= 2:
        return None
    if any(w.lower() in TITLE_ONLY for w in words) or words[-1].lower() in SITE_SUFFIXES:
        return None
    if norm_name(p) in ctx.not_names or ctx.site_of(p):
        return None
    return p


def _single_name(piece, ctx):
    """One word as a new name - only inside a list of names ('Alexa Leigh And Keith', 'JustinTheJock')."""
    w = piece.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9'\-]{2,}", w):
        return None
    low = w.lower()
    if low in COMMON_WORDS or low in TITLE_ONLY or low in CONNECTORS or norm_name(w) in ctx.not_names or ctx.site_of(w):
        return None
    return w if re.search(r'[A-Z]', w) else w.capitalize()


def _token_name(words, ctx):
    """A new name from 2-3 lowercase tokens ('mako', 'kalani' -> 'Mako Kalani'), or None."""
    if not 2 <= len(words) <= 3 or not all(re.fullmatch(r"[a-z][a-z'\-]+", w) for w in words):
        return None
    if any(w in TITLE_ONLY or w in SITE_SUFFIXES or (w in COMMON_WORDS and w not in NAME_OK) for w in words):
        return None
    name = ' '.join(w[:1].upper() + w[1:] for w in words)
    if norm_name(name) in ctx.not_names or ctx.site_of(name):
        return None
    return name


def _segment(words, ctx):
    """A run of tokens that should be all names -> [names] (pairs, a 3-word name last), or None."""
    sizes = {2: [2], 3: [3], 4: [2, 2], 5: [2, 3], 6: [2, 2, 2], 7: [2, 2, 3]}.get(len(words))
    if not sizes:
        return None
    out, i = [], 0
    for s in sizes:
        n = _token_name(words[i:i + s], ctx)
        if not n:
            return None
        out.append(n)
        i += s
    return out


def _token_names(tokens, ctx, lead_new=True):
    """Names in lowercase word tokens -> ([(name, known)], rest tokens).
    Known names anywhere (2-4 tokens, longest first), plus new names joined to a name by 'and':
    'john magnum and lucas knowles', 'marc wallace adrian monroe and mako kalani' (a list at the start).
    lead_new=False: a list at the start needs a known name in it (for free text, not release names)."""
    n = len(tokens)
    spans = {}                                          # start -> (end, name, known)
    i = 0
    while i < n:
        for size in (4, 3, 2):
            k = norm_name(''.join(tokens[i:i + size])) if i + size <= n else ''
            if k and k in ctx.cast_idx:
                spans[i] = (i + size, ctx.cast_idx[k], True)
                i += size
                break
        else:
            i += 1
    used = set()

    def covered(a, b):
        return any(s < b and a < e for s, (e, _, _) in spans.items()) or any(a <= j < b for j in used)

    for a in [j for j, w in enumerate(tokens) if w == 'and']:
        if covered(a, a + 1) or a == 0 or a == n - 1:
            continue
        new = {}
        left = next((spans[s] for s in spans if spans[s][0] == a), None)
        if left:
            last_len = left[0] - next(s for s in spans if spans[s][0] == a)
        else:
            b = max([0] + [j + 1 for j in used if j < a])          # the start, or just after the list's last 'and'
            if not b and lead_new:                                 # 'rico vega dustin hazel and ...': after known names at the start
                while b in spans:
                    b = spans[b][0]
            if covered(b, a):
                continue
            names = _segment(tokens[b:a], ctx)
            if not names:
                continue
            pos = b
            for nm in names:
                size = len(nm.split())
                new[pos] = (pos + size, nm, False)
                pos += size
            last_len = len(names[-1].split())
        right = spans.get(a + 1)
        if not right:
            rem = n - a - 1                                  # 'and sugar ray steele' at the end: all of it
            for size in dict.fromkeys(((rem,) if 2 <= rem <= 3 else ()) + (last_len, 2, 3)):
                if a + 1 + size <= n and not covered(a + 1, a + 1 + size):
                    nm = _token_name(tokens[a + 1:a + 1 + size], ctx)
                    if nm:
                        new[a + 1] = (a + 1 + size, nm, False)
                        break
            if a + 1 not in new:
                continue
        if not lead_new and not left and not right:
            continue
        spans.update(new)
        used.add(a)
    if used and spans:                       # 'sean costin collin simpson and jane rogers': names in front of the list
        first = min(spans)
        lead = _segment(tokens[:first], ctx) if first and not covered(0, first) else None
        pos = 0
        for nm in lead or []:
            size = len(nm.split())
            spans[pos] = (pos + size, nm, False)
            pos += size
    names, rest, i = [], [], 0
    while i < n:
        if i in spans:
            e, nm, known = spans[i]
            names.append((nm, known))
            i = e
            continue
        if i not in used:
            rest.append(tokens[i])
        i += 1
    return names, rest


def _names_in(piece, ctx, new_ok=True):
    """'Anna Star, Bella Moon & Max' -> [(name, known)] when the whole part is a cast list, else None."""
    pieces = [x.strip() for x in re.split(r',|&|\s+(?:and|x)\s+', piece, flags=re.I) if x.strip()]
    if not pieces or len(pieces) > 8:
        return None
    out, singles = [], 0
    for x in pieces:
        k = ctx.known_name(x)
        if k:
            out.append((k, True))
            continue
        n = name_like(x, ctx) if new_ok or len(pieces) > 1 else None
        if not n and len(pieces) > 1 and new_ok:
            n = _single_name(x, ctx)
            singles += bool(n)
        if not n:
            return None
        k2 = ctx.known_name(n)
        out.append((k2, True) if k2 else (n, False))
    if not new_ok and not any(k for _, k in out):
        return None
    if singles and singles == len(out):                 # 'Christian and Cassie' alone isn't enough
        return None
    return out


def _known_inside(text, ctx):
    """Known performers (2+ word names) mentioned anywhere in a title part."""
    words = [w.lower() for w in ext_db._clean_words(split_camel(_ascii_quotes(text)))]
    names, _rest = ext_db._names_from_tokens(words, ctx.cast_idx)
    return names


def _find_date(s):
    """-> (iso date or None, year or None, s without the date)."""
    for rx, kind in DATE_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        if kind == 'ymd':
            d = _valid_date(*m.groups())
            if not d:
                continue
            y = d[:4]
        else:
            mo, da, yr = m.groups()
            d = _valid_date(yr, mo, da) or _valid_date(yr, da, mo)
            y = yr if d else None
            if not y:
                continue
            d = None if (int(mo) <= 12 and int(da) <= 12 and mo != da) else d     # ambiguous: year only
        rest = (s[:m.start()] + ' ' + s[m.end():])
        rest = re.sub(r'[\[(]\s*[\])]', ' ', rest)
        return d, y, re.sub(r'\s+', ' ', rest).strip(' -_.')
    return None, None, s


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------
def _snn(seg, ctx, out):
    """'SomeSceneTitle_s01_JaneDoe_JohnDoe_1080p_h264' -> title, performers."""
    m = SNN_RE.match(seg)
    if not m:
        return False
    for tok in m.group('rest').split('_'):
        if not tok or _RES_RE.match(tok) or tok.lower() in _JUNK_TOKENS or tok.isdigit():
            break
        k = ctx.known_name(tok) or ctx.known_name(split_camel(tok))
        if k:
            _add_name(out, k, True)
        else:
            n = name_like(split_camel(tok), ctx)
            if n:
                _add_name(out, n, False)
    title = re.sub(r'-?(?:\d{3,4}p)$', '', m.group('title'))
    out['title_raw'] = title
    return True


def _add_name(out, name, known):
    if name and norm_name(name) not in {norm_name(n['name']) for n in out['names']}:
        out['names'].append({'name': name, 'known': known})


def _set_site(out, key, ctx):
    if key and not out['site_key']:
        out['site_key'] = key
        out['site'] = ctx.site_display.get(key, key)


def smart_parse(filename, ctx, library=None):
    """-> {style, site, site_key, site_hint, names: [{name, known}], date, year, title}"""
    stem = stem_of(filename)
    out = {'style': 'other', 'site': None, 'site_key': None, 'site_hint': None, 'site_new': None, 'names': [],
           'date': None, 'year': None, 'title': '', 'title_raw': ''}
    s = _ascii_quotes(stem).strip()
    s = re.sub(r'([._ -](?:\d{3,4}p?|[248]k))[._ -]' + ext_db._SIZE_TAGS + r'$', r'\1', s, flags=re.I)

    # trailing "- Pornhub.com" / "SITE com" / leading "xhamster.com_12345_"
    m = re.search(r'(?:^|[\s_-])-?\s*([A-Za-z][A-Za-z0-9]+)(?:\.| )(com|net|xxx)\s*-?\s*$', s)
    if m:
        _set_site(out, ctx.site_of(m.group(1)), ctx)
        s = s[:m.start()].strip(' -_')
    m = re.match(r'^([A-Za-z][A-Za-z0-9-]+)\.(com|net|org|xxx)[_ .-]+(?:\d{4,}[_ .-]+)?(.*)$', s, re.I)
    if m:
        _set_site(out, ctx.site_of(m.group(1)), ctx)
        out['site_hint'] = out['site_hint'] or norm_name(m.group(1))
        s = m.group(3)

    # release / episode style: reuse ext_db's parser, then add names it can't know yet
    base = ext_db.parse_filename(s, ctx.cast_idx, ctx.site_keys)
    if base['style'] in ('release', 'episode'):
        out['style'] = base['style']
        out['site_hint'] = base['site_hint']
        _set_site(out, ctx.site_keys.get(base['site_hint']), ctx)
        out['date'] = base['date']
        names, rest = _token_names(base.get('tokens') or base['title'].split(), ctx)
        for n, k in names:
            _add_name(out, n, k)
        if not out['names'] and rest:
            parts = [p.split() for p in re.split(r'\band\b', ' '.join(rest))]
            if all(2 <= len(p) <= 3 for p in parts) and len(parts) <= 3:          # "kenna james and octavia red"
                cand = [name_like(' '.join(p), ctx) for p in parts]
                if all(cand):
                    for c in cand:
                        _add_name(out, c, False)
                    rest = []
        out['title_raw'] = ' '.join(rest)
        return _finish(out, ctx)

    # leading "YY.MM.DD Site - ..."
    m = LEADING_SHORT_DATE.match(s)
    if m and _valid_date(m.group(1), m.group(2), m.group(3)):
        out['date'] = _valid_date(m.group(1), m.group(2), m.group(3))
        s = m.group(4)
    # "[Site] ..." / "(Site) ..."
    m = re.match(r'^\s*[\[(]([^\])]+)[\])]\s*(.*)$', s)
    if m:
        out['style'] = 'bracket'
        inner = m.group(1).strip()
        key = ctx.site_of(inner)
        if key:
            _set_site(out, key, ctx)
        elif ctx.known_name(inner):
            _add_name(out, ctx.known_name(inner), True)
        out['site_hint'] = out['site_hint'] or norm_name(SITE_TLD_RE.sub('', inner))
        s = m.group(2)
    d, y, s = _find_date(s)
    out['date'] = out['date'] or d
    out['year'] = y
    s = re.sub(r'\s*[\[(](?:\d{3,4}p|[248]k)[\])]\s*', ' ', s, flags=re.I).strip()          # "[1080p]"

    parts = [p.strip() for p in DASH_RE.split(s) if p.strip()]
    if len(parts) > 1:
        out['style'] = 'dash' if out['style'] == 'other' else out['style']
        titles, named = [], False
        # 'Site - Title - Anna Star & Max Power': with a cast list at the end, earlier parts aren't new names
        tail = _names_in(parts[-1], ctx, new_ok=True)
        tail_cast = bool(tail) and (len(tail) > 1 or any(k for _, k in tail))
        for i, p in enumerate(parts):
            if re.fullmatch(r'\d+|[0-9a-f]{8,}', p, re.I):                     # "41736 - ..." / hash ids
                continue
            key = ctx.site_of(p)
            if key and not ctx.known_name(p):
                _set_site(out, key, ctx)
                continue
            if not out['site_key'] and not out['site_new'] and not ctx.known_name(p) and (
                    (i == 0 and CAMEL_SITE_RE.fullmatch(p))
                    or (i in (0, len(parts) - 1) and 1 < len(p.split()) <= 3 and p.split()[-1].lower() in SITE_SUFFIXES)):
                out['site_new'] = ext_db.user_tag_format(_name_display(p))          # 'ExampleSite - ...' -> example.site
                continue
            m2 = re.match(r'^([A-Za-z0-9]+)\s+(.+)$', p)                       # "ExampleSite Jane Doe"
            if not titles and not named and m2 and ctx.site_of(m2.group(1)) and not out['site_key']:
                _set_site(out, ctx.site_of(m2.group(1)), ctx)
                p = m2.group(2)
            if SNN_RE.match(p) and _snn(p, ctx, out):
                titles.append(out.pop('title_raw'))
                out['title_raw'] = ''
                continue
            last = i == len(parts) - 1
            # a cast list: in front of the title (new names only as the first part), or a
            # trailing list of names after the title ("Sc4 - Lance Charger, Sean Harding")
            names = None
            if not last:
                names = _names_in(p, ctx, new_ok=not titles and not named and not tail_cast)
            elif titles:
                names = _names_in(p, ctx, new_ok=True)
                if names and len(names) < 2 and not all(k for _, k in names):
                    names = None                                                # one unknown "name" after a title
            if names:
                named = True
                for n, k in names:
                    _add_name(out, n, k)
                continue
            titles.append(p)
        out['title_raw'] = ' - '.join(titles)
        return _finish(out, ctx)

    # one part: "_sNN_" style, a cast list after "[Site] (date)", leading site words, known names, else a title
    if out['style'] == 'bracket' and s:
        names = _names_in(s, ctx)                                             # "[ExampleSite.com] (2018.12.07) A and B"
        if names:
            for n, k in names:
                _add_name(out, n, k)
            return _finish(out, ctx)
    if SNN_RE.match(s) and _snn(s, ctx, out):
        out['style'] = 'episode_names' if out['style'] == 'other' else out['style']
        return _finish(out, ctx)
    toks = s.split()
    for n in (3, 2, 1):
        if len(toks) > n and ctx.site_of(''.join(toks[:n])) and len(norm_name(''.join(toks[:n]))) >= 4:
            _set_site(out, ctx.site_of(''.join(toks[:n])), ctx)
            s = ' '.join(toks[n:])
            break
    snake = ' ' not in s and bool(re.search(r'[_.]', s))
    words = [w.lower() for w in ext_db._clean_words(s)]
    pairs, rest = _token_names(words, ctx, lead_new=out['style'] == 'bracket')
    found = [n for n, k in pairs if k]
    for n, k in pairs:
        _add_name(out, n, k)
    if not found and snake and 2 <= len(words) <= 3 and all(w.isalpha() for w in words):
        n = name_like(' '.join(words), ctx)                                    # "jesse_pony_1080p_ph"
        if n:
            _add_name(out, n, False)
            rest = []
    out['title_raw'] = ' '.join(rest) if out['names'] and not found else s
    if found:
        out['title_raw'] = s
    if pairs and all(w.isdigit() for w in rest):                                   # only names ('A and B')
        out['title_raw'] = ''
    return _finish(out, ctx)


def _finish(out, ctx=None):
    raw = out.pop('title_raw', '') or ''
    if ctx is not None and not out['names']:
        for n in _known_inside(raw, ctx):                                        # "Jane Doe Does..." (only when no
            _add_name(out, n, True)                                              # names sat where names usually are)
    title = tidy_title(raw)
    names = [n['name'] for n in out['names']]
    if title and names and _title_is_noise(title, names):
        title = ''
    if title and names and norm_name(title) in {norm_name(n) for n in names}:
        title = ''
    known = ctx.known_name(title) if title and ctx is not None else None
    if known:                                                                    # only a performer's name (or alias)
        if not out['names']:
            _add_name(out, known, True)                                          # 'ecg.18.07.12.madison'
        title = ''
    out['title'] = title
    out['year'] = out['year'] or (out['date'] or '')[:4] or None
    if out['site_key'] and not out['site_hint']:
        out['site_hint'] = out['site_key']
    return out


def clues(filename, ctx):
    """For ext_db.video_clues: the old parse_filename keys."""
    p = smart_parse(filename, ctx)
    return {'style': p['style'], 'site_hint': p['site_key'] or p['site_hint'], 'date': p['date'],
            'names': [n['name'] for n in p['names']], 'title': p['title']}


# ---------------------------------------------------------------------------
# proposals and add-only apply
# ---------------------------------------------------------------------------
def _video_rows(conn, where, params):
    return conn.execute(f'''SELECT v.video_id, fv.path, fv.filename, v.title, v.year, v.library
                            FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id
                            WHERE fv.is_preferred = 1 AND fv.deleted_at IS NULL AND {where}
                            ORDER BY fv.path''', params).fetchall()


def _stems(conn, video_id):
    return {stem_of(r[0]) for r in conn.execute('SELECT filename FROM file_versions WHERE video_id = ?', (video_id,))}


def _straightish(conn, model, vid, library):
    """Could this video have Male Cast? Not when it's tagged gay / bisexual / lesbian / trans, or sits in
    a library of one of those types."""
    if {o.lower() for o in ext_db._video_field_values(conn, vid, 'orientation')} & ext_db.OTHER_ORIENTATIONS:
        return False
    if model is not None:
        try:
            import display_filter
            return display_filter.library_type(model, library) in ('straight', display_filter.MIXED)
        except Exception:
            pass
    return True


def proposals_for(conn, ctx, row, parsed=None, model=None):
    """What the parser found that this video doesn't have yet -> [{field, value, known, ticked, source}].
    field: site / cast / male_cast / year / title (male_cast = a Cast entry marked Male Cast)."""
    vid, path, filename, title, year, library = row[0], row[1], row[2], row[3], row[4], row[5]
    p = parsed or smart_parse(filename, ctx, library)
    cast = ext_db._video_field_values(conn, vid, 'cast')
    straight = None
    sites = ext_db._video_field_values(conn, vid, 'site')
    have = {norm_name(ctx.cast_idx.get(norm_name(c), c)) for c in cast}
    out = []
    if not sites:
        if p.get('site'):
            out.append({'field': 'site', 'value': p['site'], 'known': True, 'ticked': True, 'source': 'filename'})
        elif ctx.lib_default.get(library):
            out.append({'field': 'site', 'value': ctx.lib_default[library], 'known': True, 'ticked': True,
                        'source': 'library default'})
        if not p.get('site') and p.get('site_new'):
            out.append({'field': 'site', 'value': p['site_new'], 'known': False, 'ticked': False, 'source': 'new site'})
    for n in p.get('names') or []:
        primary = ctx.cast_idx.get(norm_name(n['name']), n['name'])
        if norm_name(primary) in have:
            continue
        have.add(norm_name(primary))
        field = 'cast'
        if ctx.genders.get(primary) == 'male':
            if straight is None:
                straight = _straightish(conn, model, vid, library)
            field = 'male_cast' if straight else 'cast'
        out.append({'field': field, 'value': primary, 'known': bool(n.get('known')), 'ticked': bool(n.get('known')),
                    'source': 'filename' if n.get('known') else 'new name'})
    if p.get('year') and not str(year or '').strip():
        out.append({'field': 'year', 'value': str(p['year']), 'known': True, 'ticked': True, 'source': 'filename'})
    cur = (title or '').strip()
    if p.get('title') and (not cur or cur in _stems(conn, vid)) and p['title'] != cur:
        out.append({'field': 'title', 'value': p['title'], 'known': True, 'ticked': True, 'source': 'filename'})
    return out


def _item(conn, row, props):
    return {'video_id': row[0], 'path': row[1], 'filename': row[2], 'library': row[5],
            'current': {'title': row[3] or '', 'year': row[4] or '',
                        'site': ext_db._video_field_values(conn, row[0], 'site'),
                        'cast': ext_db._video_field_values(conn, row[0], 'cast')},
            'proposals': props}


def preview(conn, model, paths=None, libraries=None, offset=0, limit=100, extra_where=None, pattern=None):
    """Videos (these paths, or these libraries in path order) with something to propose.
    pattern: optional callable (filename, path) -> values from the %-pattern parser, or None for no match.
    -> {items, scanned, total, next_offset}"""
    ctx = ParseContext.load(conn)
    if paths is not None:
        ph = ','.join('?' * len(paths)) or "''"
        rows = _video_rows(conn, f'fv.path IN ({ph})', list(paths))
        order = {p: i for i, p in enumerate(paths)}
        rows.sort(key=lambda r: order.get(r[1], 0))
    else:
        where, params = '1=1', []
        if libraries:
            where = f'v.library IN ({",".join("?" * len(libraries))})'
            params = list(libraries)
        if extra_where:
            where += f' AND {extra_where[0]}'
            params += extra_where[1]
        rows = _video_rows(conn, where, params)
    items, i = [], offset
    while i < len(rows) and len(items) < limit:
        if pattern is None:
            props = proposals_for(conn, ctx, rows[i], model=model)
        else:
            data = pattern(rows[i][2], rows[i][1])
            props = proposals_for(conn, ctx, rows[i], _parsed_from_values(ctx, data), model=model) if data else []
        if props or paths is not None:
            items.append(_item(conn, rows[i], props))
        i += 1
    return {'items': items, 'scanned': i - offset, 'total': len(rows), 'next_offset': i if i < len(rows) else None}


def _parsed_from_values(ctx, d):
    """/api/parse_filename's values -> the smart parser's shape."""
    d = d or {}
    site_key = ctx.site_of(d.get('site')) if d.get('site') else None
    cast = d.get('cast') or []
    cast = [cast] if isinstance(cast, str) else cast
    names = []
    for c in cast:
        k = ctx.known_name(c)
        names.append({'name': k or _name_display(c), 'known': bool(k)})
    year = str(d.get('year') or '')[:4] or (str(d.get('date_created') or '')[:4] or None)
    return {'site': ctx.site_display.get(site_key, d.get('site')) if d.get('site') else None,
            'names': names, 'year': year if year and year.isdigit() else None,
            'title': tidy_title(d.get('title') or '') if d.get('title') else ''}


def from_values(conn, model, items):
    """Pattern mode: values already parsed by /api/parse_filename -> the same proposals (add-only rules)."""
    ctx = ParseContext.load(conn)
    out = []
    for it in items:
        rows = _video_rows(conn, 'fv.path = ?', [it.get('path')])
        if not rows:
            continue
        parsed = _parsed_from_values(ctx, it.get('data'))
        out.append(_item(conn, rows[0], proposals_for(conn, ctx, rows[0], parsed, model=model)))
    return {'items': out}


def get_learned(conn):
    """Your Parse Filenames corrections: {sites: {text: site}, names: {text: performer}, not_names, not_sites}."""
    out = {'sites': {}, 'names': {}, 'not_names': [], 'not_sites': []}
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (LEARNED_KEY,)).fetchone()
        saved = json.loads(row[0]) if row else {}
    except Exception:
        saved = {}
    for k in out:
        if isinstance(saved.get(k), type(out[k])):
            out[k] = saved[k]
    return out


def _save_learned(conn, d):
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (LEARNED_KEY, json.dumps(d, ensure_ascii=False)))
    conn.commit()


def learn(conn, corrections):
    """Remember what you changed on a proposal, so the parser gets it right next time.
    corrections: [(original field, original value, field, value)]. -> number remembered"""
    d = get_learned(conn)
    people = ('cast', 'male_cast')
    n = 0
    for of, ov, f, v in corrections or []:
        k = norm_name(ov)
        if len(k) < 3 or (of == f and ov == v) or (of in people and f in people and norm_name(v) == k):
            continue
        if f == 'site':                                    # 'Example Site' (cast) -> site example.site; 'exs' -> example.site
            d['sites'][k] = v
            if k in d['not_sites']:
                d['not_sites'].remove(k)
        elif of == 'site':                                 # it was read as a site but isn't one
            d['sites'].pop(k, None)
            if k not in d['not_sites']:
                d['not_sites'].append(k)
        if of in people:
            if f in people:                                # a misspelt / partial name -> the right one
                d['names'][k] = v
            else:                                          # a 'name' that is really a site / title / year
                d['names'].pop(k, None)
                if k not in d['not_names']:
                    d['not_names'].append(k)
        n += 1
    if n:
        _save_learned(conn, d)
    return n


def forget(conn, kind, key):
    d = get_learned(conn)
    if kind in ('sites', 'names'):
        d[kind].pop(key, None)
    elif kind in ('not_names', 'not_sites') and key in d[kind]:
        d[kind].remove(key)
    _save_learned(conn, d)
    return d


def apply(conn, items):
    """Add-only: lists get the value if missing; year only when empty; title only while it is
    still the filename. An entry may carry orig_field / orig_value: what the parser proposed before
    you changed it (remembered by learn()). -> {videos, added, learned}"""
    import automation
    changed, added_total = 0, 0
    now = datetime.now().isoformat()
    ctx = ParseContext.load(conn)
    corrections = []
    for it in items or []:
        vid = it.get('video_id')
        row = conn.execute('SELECT title, year FROM videos WHERE video_id = ?', (vid,)).fetchone()
        if not row:
            continue
        added, new_cast, men = 0, [], []
        for a in it.get('apply') or []:
            field, value = a.get('field'), str(a.get('value') or '').strip()
            if not value:
                continue
            if a.get('orig_field') and a.get('orig_value'):
                corrections.append((a['orig_field'], str(a['orig_value']), field, value))
            if field == 'site':
                value = ctx.site_display.get(ctx.site_of(value), value)           # your spelling of a known site
            if field in ('cast', 'site', 'male_cast'):
                if ext_db._add_list_value(conn, vid, 'cast' if field == 'male_cast' else field, value):
                    added += 1
                    if field != 'site':
                        new_cast.append(value)
                if field == 'male_cast':
                    men.append(value)
            elif field == 'year' and value.isdigit():
                n = conn.execute("UPDATE videos SET year = ? WHERE video_id = ? AND (year IS NULL OR TRIM(CAST(year AS TEXT)) = '')",
                                 (value, vid)).rowcount
                added += n
            elif field == 'title':
                cur = (conn.execute('SELECT title FROM videos WHERE video_id = ?', (vid,)).fetchone()[0] or '').strip()
                if not cur or cur in _stems(conn, vid):
                    added += conn.execute('UPDATE videos SET title = ? WHERE video_id = ?', (value, vid)).rowcount
        if added:
            conn.execute('UPDATE videos SET date_last_edited = ?, updated_at = ? WHERE video_id = ?', (now, now, vid))
            changed += 1
            added_total += added
        conn.commit()
        if men:
            ext_db.male_cast_change(conn, [vid], men, [])                           # marks them (and makes new names male)
        if new_cast:
            automation.on_cast_added(conn, vid, new_cast)
    learned = learn(conn, corrections)
    return {'success': True, 'videos': changed, 'added': added_total, 'learned': learned}
