"""
kitebase.vocabulary — the keys the merged YAML may use, path by path.

Nothing in the core lists the keys it understands: `db.py` sorts out the ones
the database needs, everything else lands in `attr_other`, and pages, the
client and the validators each read what they read. A key nobody reads is
dropped without a word, so `nulable: false` leaves a column nullable and says
nothing.

The vocabulary is what the YAML of the applications we run actually uses, after
the merge: `vocabulary.yaml`, beside this module, maps a normalized path to the
keys allowed there. It is data a person reviews, like the golden of the merged
tree: generated from real applications, then read in `git diff`.

    KITEBASE_VOCABULARY_UPDATE=1 kitebase check

adds what the application uses and the file lacks, and never removes anything:
the file is the union of several applications, and one of them not using a key
says nothing about the others.

Paths are normalized: the name under a section is `*` (`tables.*`), a list item
is `[]` (`tables.*.columns[]`). A path whose value is `'*'` is an open map, whose
keys are data, not vocabulary (`source.defaults` holds column names): its keys
are not checked, and the maps beneath them are named `*` too.

A key with a dot (`myapp.owner`) belongs to whoever names it, never to the core,
and passes unchecked. A `$` key is framework metadata (`$ref`, `$plugin`).
"""
import difflib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

import yaml

VOCABULARY_FILE = Path(__file__).with_name('vocabulary.yaml')
OPEN = '*'

Vocabulary = Dict[str, Union[List[str], str]]


def load(path: Path = VOCABULARY_FILE) -> Vocabulary:
    """The vocabulary as written; empty when there is no file."""
    return load_text(path.read_text()) if path.exists() else {}


def load_text(text: str) -> Vocabulary:
    return yaml.safe_load(text) or {}


def _free(key: Any) -> bool:
    """Keys the vocabulary never judges: metadata, and an application's own."""
    return not isinstance(key, str) or key.startswith('$') or '.' in key


def walk(data: Dict[str, Any], vocabulary: Vocabulary
         ) -> Iterator[Tuple[str, str, str, Optional[str]]]:
    """Every key of the merged tree, as (normalized path, key, real path, plugin).

    The real path is for the message, the normalized one for the lookup. Under
    an open map the keys are not yielded, and the names of the maps beneath
    them become `*`, so `joins[].Committente` is read as `joins[].*`.
    """
    def visit(node: Any, norm: str, real: str, plugin: Optional[str]):
        if isinstance(node, dict):
            plugin = node.get('$plugin', plugin)
            is_open = vocabulary.get(norm) == OPEN
            for key, value in node.items():
                if _free(key) and not is_open:
                    continue
                if not is_open:
                    yield norm, key, real, plugin
                child = f'{norm}.{OPEN if is_open else key}'
                yield from visit(value, child, f'{real}.{key}', plugin)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                yield from visit(item, f'{norm}[]', f'{real}[{i}]', plugin)

    for section, content in data.items():
        if _free(section) or not isinstance(content, dict):
            continue
        for name, item in content.items():
            if _free(name):
                continue
            yield from visit(item, f'{section}.{OPEN}', f'{section}.{name}', None)


def check(data: Dict[str, Any], vocabulary: Vocabulary) -> List[Dict[str, Any]]:
    """A warning for each key the vocabulary does not have at its path."""
    from kitebase.diagnostics import make_issue

    issues = []
    for norm, key, real, plugin in walk(data, vocabulary):
        known = vocabulary.get(norm) or []
        if key in known:
            continue
        message = f"'{key}' is not a known key here"
        near = difflib.get_close_matches(key, known, n=1)
        if near:
            message += f" (did you mean '{near[0]}'?)"
        message += (". A key of the application is written with a dot (app.key); "
                    "a new key of the core goes into kitebase/vocabulary.yaml")
        issues.append(make_issue('warning', 'key-unknown', f'{real}.{key}', message, plugin))
    return issues


def update(data: Dict[str, Any], path: Path = VOCABULARY_FILE) -> int:
    """Add to the file what `data` uses and it lacks. Returns how many keys."""
    vocabulary = load(path)
    added = 0
    for norm, key, _, _ in walk(data, vocabulary):
        known = vocabulary.setdefault(norm, [])
        if known != OPEN and key not in known:
            known.append(key)
            added += 1
    if added:
        path.write_text(render(vocabulary))
    return added


def render(vocabulary: Vocabulary) -> str:
    """One line per path, sorted: a diff of it reads as the keys that came in."""
    lines = [
        '# The keys the merged YAML may use, path by path (kitebase/vocabulary.py).',
        '# Generated, then reviewed: KITEBASE_VOCABULARY_UPDATE=1 kitebase check',
        "# adds, never removes. '*' is an open map: its keys are data.",
    ]
    for norm in sorted(vocabulary):
        keys = vocabulary[norm]
        # safe_dump quotes what YAML would misread: `on` (of a join) is a
        # boolean when plain. Flow style keeps one path on one line.
        value = keys if keys == OPEN else sorted(keys)
        flow = yaml.safe_dump(value, default_flow_style=True, width=10_000).splitlines()[0]
        lines.append(f'{norm}: {flow}')
    return '\n'.join(lines) + '\n'
