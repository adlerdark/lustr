#!/usr/bin/env python3
"""
Build lustr's themes:
  static/themes/<id>.css   one :root block of colour variables per theme (from THEMES below)
  static/theme-map.css     every Tailwind colour class index.html uses -> those variables
                           (also hover: / focus: / group-hover: / placeholder- / divide- / ring- and /NN
                           opacity variants), text on coloured fills, and the parts that stay dark
                           (players, image viewer, overlays) in light themes

Each theme is a few base colours: the page background, the text, and one colour per family
(primary, success, danger, warning, purple, cast, orange, info). The other shades are derived and then
nudged until they pass the contrast checks in tests/test_themes.py (text >= 4.5:1 and so on).

    python3 scripts/build_themes.py          # from the repo root; rerun after changing markup or THEMES
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEME_DIR = os.path.join(ROOT, 'static', 'themes')
MAP_FILE = os.path.join(ROOT, 'static', 'theme-map.css')
INDEX = os.path.join(ROOT, 'templates', 'index.html')

FAMILIES = ('primary', 'success', 'danger', 'warning', 'purple', 'cast', 'orange', 'info')
ROLES = ('subtle', 'dark', 'base', 'light', 'soft', 'softer', 'faint', 'faintest')
SURFACES = ('deepest', 'base', 'mid', 'raised', 'elevated', 'high', 'higher')
BORDERS = ('subtle', 'default', 'strong', 'stronger')
TEXTS = ('default', 'brightest', 'brighter', 'primary', 'secondary', 'muted', 'faint')

# Tailwind hue -> theme family
HUES = {'blue': 'primary', 'sky': 'info', 'cyan': 'info', 'teal': 'info',
        'green': 'success', 'emerald': 'success', 'lime': 'success',
        'red': 'danger', 'rose': 'danger', 'pink': 'purple',
        'yellow': 'warning', 'amber': 'warning',
        'purple': 'purple', 'fuchsia': 'purple', 'violet': 'cast', 'indigo': 'cast', 'orange': 'orange'}
GRAYS = ('gray', 'slate', 'zinc', 'neutral', 'stone')
# Tailwind shade -> family role, per property
BG_ROLE = {'50': 'faintest', '100': 'faintest', '200': 'faint', '300': 'softer', '400': 'light', '500': 'light',
           '600': 'base', '700': 'dark', '800': 'dark', '900': 'subtle', '950': 'subtle'}
TEXT_ROLE = {'50': 'faintest', '100': 'faintest', '200': 'faint', '300': 'softer', '400': 'soft', '500': 'light',
             '600': 'base', '700': 'dark', '800': 'dark', '900': 'dark', '950': 'dark'}
BORDER_ROLE = {'50': 'faint', '100': 'faint', '200': 'faint', '300': 'softer', '400': 'soft', '500': 'light',
               '600': 'base', '700': 'dark', '800': 'dark', '900': 'subtle', '950': 'subtle'}
GRAY_BG = {'50': 'surface-raised', '100': 'surface-raised', '200': 'surface-elevated', '300': 'surface-high',
           '400': 'surface-higher', '500': 'surface-higher', '600': 'surface-high', '700': 'surface-elevated',
           '750': 'surface-mid', '800': 'surface-raised', '850': 'surface-mid', '900': 'surface-base',
           '950': 'surface-deepest'}
GRAY_TEXT = {'50': 'text-brightest', '100': 'text-brightest', '200': 'text-brighter', '300': 'text-primary',
             '400': 'text-secondary', '500': 'text-muted', '600': 'text-faint', '700': 'text-faint',
             '800': 'surface-raised', '900': 'surface-base', '950': 'surface-deepest'}
GRAY_BORDER = {'200': 'border-stronger', '300': 'border-stronger', '400': 'border-stronger', '500': 'border-stronger',
               '600': 'border-strong', '700': 'border-default', '750': 'border-default', '800': 'border-subtle',
               '900': 'border-subtle', '950': 'border-subtle'}
FILL_SHADES = ('500', '600', '700', '800')            # bg shades that are a coloured fill: text on them is on-<family>

# ---------------------------------------------------------------------------------------------------------
# themes: mode, page background, text, one colour per family, and optional exact values
# ---------------------------------------------------------------------------------------------------------
TAILWIND = {   # used as-is by the default theme (900, 700, 600, 500, 400, 300, 200, 100)
    'primary': ('#1e3a8a', '#1d4ed8', '#2563eb', '#3b82f6', '#60a5fa', '#93c5fd', '#bfdbfe', '#dbeafe'),
    'success': ('#14532d', '#15803d', '#16a34a', '#22c55e', '#4ade80', '#86efac', '#bbf7d0', '#dcfce7'),
    'danger':  ('#7f1d1d', '#b91c1c', '#dc2626', '#ef4444', '#f87171', '#fca5a5', '#fecaca', '#fee2e2'),
    'warning': ('#713f12', '#a16207', '#ca8a04', '#eab308', '#facc15', '#fde047', '#fef08a', '#fef9c3'),
    'purple':  ('#581c87', '#7e22ce', '#9333ea', '#a855f7', '#c084fc', '#d8b4fe', '#e9d5ff', '#f3e8ff'),
    'cast':    ('#312e81', '#4338ca', '#4f46e5', '#6366f1', '#818cf8', '#a5b4fc', '#c7d2fe', '#e0e7ff'),
    'orange':  ('#7c2d12', '#c2410c', '#ea580c', '#f97316', '#fb923c', '#fdba74', '#fed7aa', '#ffedd5'),
    'info':    ('#164e63', '#0e7490', '#0891b2', '#06b6d4', '#22d3ee', '#67e8f9', '#a5f3fc', '#cffafe'),
}

THEMES = {
    'default': dict(name='Dark (Default)', mode='dark', bg='#111827', text='#ffffff', tailwind=True,
                    surfaces=dict(deepest='#030712', base='#111827', mid='#18212f', raised='#1f2937', elevated='#374151',
                                  high='#4b5563', higher='#6b7280'),
                    borders=dict(subtle='#1f2937', default='#374151', strong='#4b5563', stronger='#6b7280'),
                    texts=dict(default='#ffffff', brightest='#f3f4f6', brighter='#e5e7eb', primary='#d1d5db',
                               secondary='#9ca3af', muted='#8b93a1', faint='#6b7280')),
    'light': dict(name='Light', mode='light', bg='#eef2f6', text='#0f172a',
                  colors=dict(primary='#2563eb', success='#15803d', danger='#c62828', warning='#b45309', purple='#7e22ce',
                              cast='#4338ca', orange='#c2410c', info='#0e7490')),
    'paper': dict(name='Paper', mode='light', bg='#f3ede2', text='#2b2420', raised='#fbf8f2',
                  colors=dict(primary='#8a5a2b', success='#3f7d4e', danger='#b23a2e', warning='#a8661a', purple='#7d4e8f',
                              cast='#4f5f9a', orange='#b8582a', info='#2f7f86')),
    'mist': dict(name='Mist', mode='light', bg='#e6ecf2', text='#1b2733', raised='#f6f9fc',
                 colors=dict(primary='#3b6ea5', success='#2f855a', danger='#c53030', warning='#a8661a', purple='#6b46c1',
                             cast='#4c51bf', orange='#c05621', info='#2c7a7b')),
    'dusk': dict(name='Dusk', mode='dark', bg='#272e3b', text='#e7eaf0', step=1.25,
                 colors=dict(primary='#5b8def', success='#3f9d63', danger='#e05d5d', warning='#d9a441', purple='#a37be0',
                             cast='#7b86e8', orange='#e0864a', info='#3fa9bf')),
    'ember': dict(name='Dark Ember', mode='dark', bg='#1a1614', text='#f0ebe5',
                  colors=dict(primary='#b4532a', success='#2f8f4e', danger='#c0392b', warning='#c8901a', purple='#8e5aa8',
                              cast='#6a5acd', orange='#d9661f', info='#3a8f9a')),
    'midnight-forest': dict(name='Midnight Forest', mode='dark', bg='#0d1512', text='#e8f0ec',
                            colors=dict(primary='#2e8b57', success='#3a9d5d', danger='#b5443a', warning='#c99a2e',
                                        purple='#7a6bb0', cast='#4f7cac', orange='#c8742b', info='#2f8f8f')),
    'volcanic': dict(name='Volcanic', mode='dark', bg='#141008', text='#f5ede0',
                     colors=dict(primary='#c86a04', success='#4d8a2f', danger='#c2361f', warning='#d9a000', purple='#8b4f8f',
                                 cast='#7a5cc2', orange='#e8590c', info='#3b8ea5')),
    'arctic': dict(name='Arctic', mode='dark', bg='#0a0e18', text='#e8f4f8',
                   colors=dict(primary='#1597b8', success='#1f9d6b', danger='#d04060', warning='#d6a419', purple='#7c6fd6',
                               cast='#5a7bd8', orange='#e07a3a', info='#22b8cf')),
    'noir': dict(name='Noir', mode='dark', bg='#0b0b0b', text='#f5f5f5',
                 colors=dict(primary='#d4d4d4', success='#3f9e5f', danger='#c43c3c', warning='#d4a72c', purple='#9d7fcf',
                             cast='#8a8fd6', orange='#d97a3a', info='#5fa8b8')),
    'sakura': dict(name='Sakura', mode='dark', bg='#150f18', text='#f8f0f4',
                   colors=dict(primary='#d0508a', success='#3c9a6a', danger='#d23c4b', warning='#d9a440', purple='#9b5cc8',
                               cast='#7a6ad8', orange='#e0784a', info='#4aa3b8')),
    'synthwave': dict(name='Synthwave', mode='dark', bg='#0e0a1a', text='#f8e8ff',
                      colors=dict(primary='#b026ff', success='#19c39a', danger='#ff2e88', warning='#ffb000', purple='#7b2cbf',
                                  cast='#3a86ff', orange='#ff7a18', info='#00d1ff')),
    'deep-sea': dict(name='Deep Sea', mode='dark', bg='#06161c', text='#e2f4f6',
                     colors=dict(primary='#13b5c8', success='#2bd49a', danger='#ff5f6d', warning='#ffc145', purple='#8f7cff',
                                 cast='#4f8dff', orange='#ff8a4c', info='#36d7e6')),
    'aurora': dict(name='Aurora', mode='dark', bg='#0b1020', text='#eef2ff',
                   colors=dict(primary='#34d399', success='#4ade80', danger='#f87171', warning='#fbbf24', purple='#a78bfa',
                               cast='#818cf8', orange='#fb923c', info='#22d3ee')),
}
ORDER = ['default', 'light', 'paper', 'mist', 'dusk', 'ember', 'midnight-forest', 'volcanic', 'arctic', 'noir', 'sakura',
         'synthwave', 'deep-sea', 'aurora']


# ---------------------------------------------------------------------------------------------------------
# colour maths
# ---------------------------------------------------------------------------------------------------------
def rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hexc(c):
    return '#%02x%02x%02x' % tuple(max(0, min(255, round(v))) for v in c)


def mix(a, b, t):
    """t of b into a."""
    a, b = rgb(a), rgb(b)
    return hexc(tuple(x + (y - x) * t for x, y in zip(a, b)))


def lum(h):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def push(c, against, need, toward):
    """Move c toward `toward` (black or white) in small steps until it has `need` contrast with every colour in against."""
    for _ in range(60):
        if all(contrast(c, x) >= need for x in against):
            return c
        c = mix(c, toward, 0.05)
    return c


WHITE, BLACK = '#ffffff', '#000000'


# ---------------------------------------------------------------------------------------------------------
# a theme's variables
# ---------------------------------------------------------------------------------------------------------
def build(tid):
    t = THEMES[tid]
    light = t['mode'] == 'light'
    B, T = t['bg'], t['text']
    v = {}
    k = t.get('step', 1.0)
    if 'surfaces' in t:
        s = dict(t['surfaces'])
    elif light:
        s = dict(deepest=mix(B, T, 0.07), base=B, mid=mix(B, WHITE, 0.45), raised=t.get('raised', mix(B, WHITE, 0.8)),
                 elevated=mix(B, T, 0.07), high=mix(B, T, 0.13), higher=mix(B, T, 0.26))
    else:
        s = dict(deepest=mix(B, BLACK, 0.35), base=B, mid=mix(B, T, 0.035 * k), raised=mix(B, T, 0.06 * k),
                 elevated=mix(B, T, 0.13 * k), high=mix(B, T, 0.2 * k), higher=mix(B, T, 0.32 * k))
    for n in SURFACES:
        v['surface-' + n] = s[n]
    if 'borders' in t:
        bo = t['borders']
    elif light:
        bo = dict(subtle=mix(B, T, 0.09), default=mix(B, T, 0.16), strong=mix(B, T, 0.3), stronger=mix(B, T, 0.48))
    else:
        bo = dict(subtle=mix(B, T, 0.08 * k), default=mix(B, T, 0.15 * k), strong=mix(B, T, 0.23 * k),
                  stronger=mix(B, T, 0.36 * k))
    for n in BORDERS:
        v['border-' + n] = bo[n]
    if 'texts' in t:
        tx = dict(t['texts'])
    else:
        tx = dict(default=T, brightest=T, brighter=mix(T, B, 0.06), primary=mix(T, B, 0.15), secondary=mix(T, B, 0.3),
                  muted=mix(T, B, 0.45), faint=mix(T, B, 0.6))
    toward_text = BLACK if light else WHITE
    hover = [s['raised'], s['base'], s['elevated'], s['high']]
    tx['primary'] = push(tx['primary'], hover, 4.5, toward_text)
    tx['secondary'] = push(tx['secondary'], [s['raised'], s['base'], s['mid']], 4.5, toward_text)
    tx['muted'] = push(tx['muted'], [s['raised'], s['base']], 3.0, toward_text)
    for n in TEXTS:
        v['text-' + n] = tx[n]

    for fam in FAMILIES:
        if t.get('tailwind'):
            sub, dk, base, lt, soft, softer, faint, faintest = TAILWIND[fam]
        else:
            A = t['colors'][fam]
            base, dk, lt = A, mix(A, BLACK, 0.2), mix(A, WHITE, 0.12)
            if light:
                sub = mix(A, WHITE, 0.86)
                soft, softer, faint, faintest = mix(A, BLACK, 0.12), mix(A, BLACK, 0.25), mix(A, BLACK, 0.38), mix(A, BLACK, 0.5)
            else:
                sub = mix(B, A, 0.28)
                soft, softer, faint, faintest = mix(A, WHITE, 0.3), mix(A, WHITE, 0.5), mix(A, WHITE, 0.68), mix(A, WHITE, 0.82)
        # text on the fills: white, or near-black for a pale colour; the fills move away from it until readable
        on = WHITE if contrast(base, WHITE) >= contrast(base, '#111111') or contrast(base, WHITE) >= 3.0 else '#111111'
        away = BLACK if on == WHITE else WHITE
        base = push(base, [on], 4.5, away)
        dk = push(dk, [on], 4.5, away)
        lt = push(lt, [on], 4.5, away)
        # coloured text must read on the page, panels and buttons; the tinted background must carry the light shades
        tsurf = [s['raised'], s['base'], s['mid']]
        if light:
            sub = push(sub, [softer], 4.5, WHITE)
        soft = push(soft, tsurf, 4.5, toward_text)
        softer = push(softer, tsurf + [sub], 4.5, toward_text)
        faint = push(faint, tsurf + [sub], 4.5, toward_text)
        faintest = push(faintest, tsurf + [sub], 4.5, toward_text)
        if not light:
            sub = push(sub, [softer, faint], 4.5, BLACK)
        for role, c in zip(ROLES, (sub, dk, base, lt, soft, softer, faint, faintest)):
            v[f'{fam}-{role}'] = c
        v['on-' + fam] = on
    return v


def theme_css(tid, v):
    t = THEMES[tid]
    lines = [f'/*', f' * lustr theme: {t["name"]}  ({t["mode"]})', ' *',
             ' * Made by scripts/build_themes.py - change the theme there and rebuild, or copy this file to make your own',
             ' * (Settings -> Appearance -> Upload Custom Theme). A theme is only these colour variables;',
             ' * static/theme-map.css applies them to the whole page.',
             ' *',
             ' *   surface-*   backgrounds: deepest (behind everything), base (page), mid, raised (panels, cards),',
             ' *               elevated (buttons at rest), high (hover), higher (pressed)',
             ' *   border-*    subtle, default, strong (inputs), stronger',
             ' *   text-*      default / brightest (headings), brighter, primary (body), secondary, muted, faint',
             ' *   <family>-*  primary (main accent), success, danger, warning, purple, cast, orange, info:',
             ' *               subtle (tinted background), dark / base / light (button fills: pressed, normal, hover),',
             ' *               soft, softer, faint, faintest (coloured text, strongest first)',
             ' *   on-<family> text on that family\'s button fills',
             ' */', ':root {']
    lines.append(f'    --theme-mode: {t["mode"]};')
    for key, val in v.items():
        lines.append(f'    --{key}: {val};')
    lines.append('}')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------------------------------------
# the class map
# ---------------------------------------------------------------------------------------------------------
CLASS_RE = re.compile(r'(?<![\w:-])((?:hover:|focus:|group-hover:|active:)?'
                      r'(bg|text|border|ring|divide|placeholder)-([a-z]+)(?:-(\d{2,3}))?(?:/(\d{1,3}))?)(?![\w/-])')
SKIP_COLOURS = {'black', 'white', 'transparent', 'current', 'inherit'}


def used_classes(html):
    out = set()
    for m in CLASS_RE.finditer(html):
        full, prop, colour, shade, op = m.groups()
        if colour in SKIP_COLOURS or (colour not in HUES and colour not in GRAYS):
            continue
        if shade is None:
            continue
        out.add(full)
    return out


def esc(cls):
    return re.sub(r'([:/.])', r'\\\1', cls)


def variable_for(prop, colour, shade):
    if colour in GRAYS:
        table = {'bg': GRAY_BG, 'text': GRAY_TEXT, 'placeholder': GRAY_TEXT}.get(prop, GRAY_BORDER)
        name = table.get(shade)
        if not name:
            return None
        return name
    fam = HUES[colour]
    table = {'bg': BG_ROLE, 'text': TEXT_ROLE, 'placeholder': TEXT_ROLE}.get(prop, BORDER_ROLE)
    role = table.get(shade)
    return f'{fam}-{role}' if role else None


def rule_for(cls):
    m = CLASS_RE.fullmatch(cls)
    full, prop, colour, shade, op = m.groups()
    variant = ''
    base = full
    for pre in ('hover:', 'focus:', 'group-hover:', 'active:'):
        if full.startswith(pre):
            variant, base = pre[:-1], full[len(pre):]
    var = variable_for(prop, colour, shade)
    if not var:
        return None
    value = f'var(--{var})'
    if op:
        value = f'color-mix(in srgb, {value} {int(op)}%, transparent)'
    sel = '.' + esc(full)
    if variant == 'hover':
        sel += ':hover'
    elif variant == 'focus':
        sel += ':focus'
    elif variant == 'active':
        sel += ':active'
    elif variant == 'group-hover':
        sel = '.group:hover ' + sel
    if prop == 'bg':
        return f'{sel} {{ background-color: {value} !important; }}'
    if prop == 'text':
        return f'{sel} {{ color: {value} !important; }}'
    if prop == 'placeholder':
        return f'{sel}::placeholder {{ color: {value} !important; }}'
    if prop == 'ring':
        return f'{sel} {{ --tw-ring-color: {value} !important; }}'
    if prop == 'divide':
        return f'{sel} > :not([hidden]) ~ :not([hidden]) {{ border-color: {value} !important; }}'
    return f'{sel} {{ border-color: {value} !important; }}'


def theme_map(classes, island_vars, default_vars):
    out = ['/*', ' * lustr theme map - made by scripts/build_themes.py from the classes in templates/index.html; do not edit.',
           ' * Every colour class the page uses -> the active theme\'s variables (static/themes/*.css).',
           ' */']
    out.append(':root {')
    out += [f'    --{k}: {val};' for k, val in default_vars.items()]
    out.append('}')
    out.append('html { color-scheme: dark; }')
    out.append('html[data-theme-mode="light"] { color-scheme: light; }')
    out.append('body, .text-white { color: var(--text-default) !important; }')
    out.append('.hover\\:text-white:hover, .group:hover .group-hover\\:text-white { color: var(--text-brightest) !important; }')
    out.append('.text-white\\/70 { color: color-mix(in srgb, var(--text-default) 70%, transparent) !important; }')
    out.append('.text-white\\/60 { color: color-mix(in srgb, var(--text-default) 60%, transparent) !important; }')
    out.append('hr { border-color: var(--border-default) !important; }')
    order = {'bg': 0, 'border': 1, 'divide': 2, 'ring': 3, 'text': 4, 'placeholder': 5}
    rules = []
    for cls in sorted(classes, key=lambda c: (order[CLASS_RE.fullmatch(c).group(2)], c.startswith(('hover', 'focus', 'group', 'active')), c)):
        r = rule_for(cls)
        if r:
            rules.append(r)
    out += rules

    # text on coloured fills (buttons, badges): on-<family>, on the element and its text inside it
    out.append('/* text on coloured fills */')
    fam_fills = {}
    for cls in classes:
        m = CLASS_RE.fullmatch(cls)
        full, prop, colour, shade, op = m.groups()
        if prop != 'bg' or op or colour not in HUES or shade not in FILL_SHADES:
            continue
        variant = full.split(':')[0] if ':' in full else ''
        if variant not in ('', 'hover'):
            continue
        sel = '.' + esc(full) + (':hover' if variant == 'hover' else '')
        fam_fills.setdefault(HUES[colour], []).append(sel)
    inner = ':not(input):not(select):not(textarea):not(option):not([class*="bg-gray"])'
    for fam, sels in sorted(fam_fills.items()):
        sels = sorted(sels)
        both = ', '.join(sels + [f'{s} *{inner}' for s in sels])
        out.append(f'{both} {{ color: var(--on-{fam}) !important; }}')

    # parts that stay dark in light themes: the players, the image viewer, and overlays on pictures
    out.append('/* always dark: players, viewers, overlays on pictures (light themes) */')
    # a class that starts with bg-black (not hover:bg-black), on anything but a full-screen backdrop
    island = 'html[data-theme-mode="light"] :is(.lustr-dark, :is([class^="bg-black"], [class*=" bg-black"]):not(.fixed))'
    out.append(island + ' {')
    out += [f'    --{k}: {val};' for k, val in island_vars.items()]
    out.append('    color: var(--text-default);')
    out.append('}')
    out.append('::-webkit-scrollbar { width: 6px; height: 6px; }')
    out.append('::-webkit-scrollbar-track { background: var(--surface-base); }')
    out.append('::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 3px; }')
    return '\n'.join(out) + '\n'


def main():
    built = {tid: build(tid) for tid in ORDER}
    for tid, v in built.items():
        with open(os.path.join(THEME_DIR, tid + '.css'), 'w', encoding='utf-8') as f:
            f.write(theme_css(tid, v))
    with open(INDEX, encoding='utf-8') as f:
        classes = used_classes(f.read())
    with open(MAP_FILE, 'w', encoding='utf-8') as f:
        f.write(theme_map(classes, built['default'], built['default']))
    print(f'{len(built)} themes, {len(classes)} classes mapped')
    for tid in ORDER:
        t = THEMES[tid]
        print(f"  {{ id: '{tid}', name: '{t['name']}', preview: ['{built[tid]['surface-base']}','{built[tid]['primary-base']}',"
              f"'{built[tid]['surface-elevated']}'] }},")


if __name__ == '__main__':
    sys.exit(main())
