"""Cheap guards for hand-written static assets — catches things the page tests can't,
like an unclosed brace that silently swallows every CSS rule after it."""
import glob
import os
import re

import pytest

STATIC = os.path.join(os.path.dirname(__file__), '..', 'app', 'static')
STYLESHEETS = sorted(p for p in glob.glob(os.path.join(STATIC, '*.css')))


@pytest.mark.parametrize('path', STYLESHEETS, ids=os.path.basename)
def test_stylesheet_braces_balance(path):
    css = re.sub(r'/\*.*?\*/', '', open(path, encoding='utf-8').read(), flags=re.S)
    depth = 0
    for lineno, line in enumerate(css.splitlines(), 1):
        for ch in line:
            depth += ch == '{'
            depth -= ch == '}'
            assert depth >= 0, f'{os.path.basename(path)}: unexpected "}}" on line {lineno}'
    assert depth == 0, f'{os.path.basename(path)}: {depth} unclosed "{{" block(s)'


def test_no_leftover_merge_markers():
    root = os.path.join(os.path.dirname(__file__), '..', 'app')
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.endswith(('.py', '.html', '.css', '.js')):
                text = open(os.path.join(dirpath, name), encoding='utf-8', errors='ignore').read()
                assert not re.search(r'^(<<<<<<<|>>>>>>>) ', text, re.M), f'merge marker in {name}'
