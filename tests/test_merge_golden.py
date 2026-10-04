"""The merged tree of the real application, pinned.

`test_plugin_merge.py` pins the merge *primitives* on synthetic dicts. This one
pins their **result** on the application we ship: every table, page, view, menu
item and schema that the plugins of `devtest` produce once composed.

It exists because the primitives are load-bearing far beyond the file that
defines them — a change to `_merge_lists` moves descriptors nobody was thinking
about — and because a failure here is *readable*: the assertion names the paths
that moved, which no test over hand-written dicts can do.

Regenerate deliberately, never to make the suite green:

    KITEBASE_GOLDEN_UPDATE=1 python -m pytest tests/test_merge_golden.py

then read `git diff` on the snapshot. That diff is the answer to "does this
change anything we already wrote".
"""
import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

import kitebase.utils
from kitebase.plugins import PluginsManager

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).parent / 'golden'

# Applications that live inside this repository. A checkout has devtest and
# nothing else, so nothing here may depend on a sibling repository being present.
APPS = {'devtest': REPO_ROOT / 'devtest'}

# How many differing paths to print before giving up: enough to see the shape of
# a regression, few enough to read.
MAX_REPORTED = 25


def merged_tree(app_dir: Path, monkeypatch) -> dict:
    """Compose the application's plugins, stopping before the schema is computed.

    Plugin roots resolve against the working directory, so the directory is the
    argument in disguise — hence the chdir, undone by monkeypatch.
    """
    monkeypatch.chdir(app_dir)
    pm = PluginsManager()
    pm.load_config('config.yaml')
    kitebase.utils.register_standard_handlers(pm)
    pm.load_plugins()
    return pm.data


def render(data: Any) -> str:
    """Insertion order is part of the answer — column order, layout order, tabs."""
    return json.dumps(data, indent=1, default=str, ensure_ascii=False, sort_keys=False) + '\n'


def differences(old: Any, new: Any, path: str = '') -> Iterator[str]:
    """Every place the two trees disagree, named by path.

    Order counts: two lists with the same items in another order are a
    difference, because the order of a layout is what the user sees.
    """
    if type(old) is not type(new):
        yield f"{path or '.'}: {type(old).__name__} → {type(new).__name__}"
        return

    if isinstance(old, dict):
        for key in old:
            if key not in new:
                yield f"{path}.{key}: removed"
        for key in new:
            if key not in old:
                yield f"{path}.{key}: added"
            else:
                yield from differences(old[key], new[key], f'{path}.{key}')
        return

    if isinstance(old, list):
        if len(old) != len(new):
            yield f"{path}: {len(old)} items → {len(new)} items"
        for i, (a, b) in enumerate(zip(old, new)):
            yield from differences(a, b, f'{path}[{i}]')
        return

    if old != new:
        yield f"{path}: {old!r} → {new!r}"


@pytest.mark.parametrize('app', sorted(APPS))
def test_the_merged_tree_is_what_it_was(app, monkeypatch):
    tree = merged_tree(APPS[app], monkeypatch)
    golden = GOLDEN_DIR / f'{app}.json'

    if os.environ.get('KITEBASE_GOLDEN_UPDATE'):
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden.write_text(render(tree))
        pytest.skip(f'golden rewritten: {golden.relative_to(REPO_ROOT)} — read the diff')

    assert golden.exists(), (
        f'No snapshot for {app}. Create it with KITEBASE_GOLDEN_UPDATE=1 and commit it.')

    found = list(differences(json.loads(golden.read_text()), json.loads(render(tree))))
    assert not found, (
        f'The merged tree of {app} moved in {len(found)} places:\n  '
        + '\n  '.join(found[:MAX_REPORTED])
        + ('\n  …' if len(found) > MAX_REPORTED else '')
        + '\nIf the change is intended, regenerate with KITEBASE_GOLDEN_UPDATE=1.')


def test_the_snapshot_covers_what_the_merge_composes():
    """A snapshot of half the tree would go green through a change it never saw."""
    golden = json.loads((GOLDEN_DIR / 'devtest.json').read_text())

    assert set(golden) >= {'tables', 'types', 'pages', 'views', 'menu_items'}
    # The two lists the merge actually crosses plugins on today, and the one the
    # form protocol lives in.
    assert 'columns' in golden['tables']['Book']
    assert 'layout' in golden['pages']['book_form']['content']
