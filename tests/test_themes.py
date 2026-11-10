"""
Themes (static/themes/*.css, static/theme-map.css, made by scripts/build_themes.py):
  - every colour class index.html uses is mapped to theme variables (so no theme misses one)
  - the theme files and the map are what the build script makes now (run it after changing markup)
  - every built-in theme has every variable, and passes the contrast checks (WCAG ratios)

    docker exec -w /app lustr python3 -m unittest tests.test_themes -v
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import build_themes as bt                              # noqa: E402


def read(*p):
    with open(os.path.join(ROOT, *p), encoding='utf-8') as f:
        return f.read()


def theme_vars(tid):
    return dict(re.findall(r'--([a-z0-9-]+):\s*(#[0-9a-f]{6})\s*;', read('static', 'themes', tid + '.css')))


class Coverage(unittest.TestCase):
    def test_every_colour_class_is_mapped(self):
        css = read('static', 'theme-map.css')
        missing = [c for c in sorted(bt.used_classes(read('templates', 'index.html')))
                   if '.' + bt.esc(c) not in css]
        self.assertEqual(missing, [], 'run scripts/build_themes.py')

    def test_files_are_up_to_date(self):
        built = {tid: bt.build(tid) for tid in bt.ORDER}
        for tid in bt.ORDER:
            self.assertEqual(read('static', 'themes', tid + '.css'), bt.theme_css(tid, built[tid]), tid)
        classes = bt.used_classes(read('templates', 'index.html'))
        self.assertEqual(read('static', 'theme-map.css'), bt.theme_map(classes, built['default'], built['default']),
                         'run scripts/build_themes.py')

    def test_registered_in_the_page(self):
        html = read('templates', 'index.html')
        for tid in bt.ORDER:
            self.assertTrue(f"id: '{tid}'" in html, f'{tid} is not in builtinThemes')


class Contrast(unittest.TestCase):
    def check(self, tid, fg, bgs, need):
        v = theme_vars(tid)
        for bg in bgs:
            r = bt.contrast(v[fg], v[bg])
            self.assertGreaterEqual(round(r, 2), need, f'{tid}: {fg} {v[fg]} on {bg} {v[bg]} is {r:.2f}:1')

    def test_variables_complete(self):
        names = set(bt.build('default'))
        for tid in bt.ORDER:
            self.assertEqual(set(theme_vars(tid)), names, tid)

    def test_text(self):
        panels = ['surface-raised', 'surface-base', 'surface-mid']
        for tid in bt.ORDER:
            with self.subTest(tid):
                self.check(tid, 'text-default', panels, 7.0)
                self.check(tid, 'text-default', ['surface-elevated', 'surface-high'], 4.5)       # buttons, hovered rows
                self.check(tid, 'text-primary', panels + ['surface-elevated', 'surface-high'], 4.5)
                self.check(tid, 'text-secondary', panels, 4.5)
                self.check(tid, 'text-muted', ['surface-raised', 'surface-base'], 3.0)

    def test_text_on_fills(self):
        for tid in bt.ORDER:
            for fam in bt.FAMILIES:
                with self.subTest(f'{tid} {fam}'):
                    self.check(tid, f'on-{fam}', [f'{fam}-base', f'{fam}-dark', f'{fam}-light'], 4.5)

    def test_coloured_text(self):
        panels = ['surface-raised', 'surface-base', 'surface-mid']
        for tid in bt.ORDER:
            for fam in bt.FAMILIES:
                with self.subTest(f'{tid} {fam}'):
                    for role in ('soft', 'softer', 'faint', 'faintest'):
                        self.check(tid, f'{fam}-{role}', panels, 4.5)
                    self.check(tid, f'{fam}-softer', [f'{fam}-subtle'], 4.5)    # tinted badge: bg-x-900 text-x-300

    def test_borders_visible(self):
        for tid in bt.ORDER:
            with self.subTest(tid):
                self.check(tid, 'border-strong', ['surface-raised', 'surface-base'], 1.4)
                self.check(tid, 'surface-high', ['surface-raised'], 1.15)                 # a hover shows


if __name__ == '__main__':
    unittest.main()
