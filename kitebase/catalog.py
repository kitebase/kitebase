"""
kitebase.catalog — what each key of the merged YAML means.

The vocabulary (kitebase.vocabulary) knows which keys a path uses; the catalog
says what they mean. It is written by form, not by path: `Column` is the same
under a table and under a composite type, and is described once.
`catalog.yaml`, beside this module, holds:

- `roots`: the form a normalized path holds (`tables.*.columns[]: Column`);
- `forms`: for each form its keys, with the type of the value, the closed set
  of values when there is one, a line of meaning and the section of the docs;
- `directives`: the `$` words of the merge and of the framework, which are not
  keys of any form and are told once;
- `tokens`: the `$` values resolved later, whose meaning stays with the
  registry that resolves them (`kitebase.defaults`) and is read from there.

For now it documents and judges nothing: the vocabulary still decides what a
path admits, and `check` keeps using it. A form may take the keys of another
(`includes`), leaving some out (`without`): a type is a column without a name.
"""
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

from kitebase import defaults

CATALOG_FILE = Path(__file__).with_name('catalog.yaml')

Catalog = Dict[str, Any]


def load(path: Path = CATALOG_FILE) -> Catalog:
    return yaml.safe_load(path.read_text(encoding='utf-8')) or {}


def form_at(catalog: Catalog, norm: str) -> Optional[str]:
    """The form a normalized path holds, if the catalog knows it."""
    return (catalog.get('roots') or {}).get(norm)


def keys_of(catalog: Catalog, form: str) -> Dict[str, Dict[str, Any]]:
    """The keys of a form, its own over those it includes."""
    spec = (catalog.get('forms') or {}).get(form) or {}
    keys: Dict[str, Dict[str, Any]] = {}
    base = spec.get('includes')
    if base:
        without = set(spec.get('without') or ())
        keys.update({k: v for k, v in keys_of(catalog, base).items() if k not in without})
    keys.update(spec.get('keys') or {})
    return keys


def describe(catalog: Catalog, norm: str, key: str) -> Optional[Dict[str, Any]]:
    """What `key` means at the normalized path, or None."""
    form = form_at(catalog, norm)
    return keys_of(catalog, form).get(key) if form else None


def tokens(catalog: Catalog) -> Dict[str, Dict[str, Any]]:
    """The families of `$` values, each with its names and their meaning.

    The meaning is the first line of the docstring of what the registry holds,
    so a token an application registers is told without writing it twice.
    """
    registries = {'kitebase.defaults': {n: defaults.get_default(n) for n in defaults.default_names()}}
    out: Dict[str, Dict[str, Any]] = {}
    for family, spec in (catalog.get('tokens') or {}).items():
        names = registries.get(spec.get('registry'), {})
        out[family] = {**spec, 'names': {
            f'${name}': _first_line(fn) for name, fn in sorted(names.items())}}
    return out


def _first_line(fn: Any) -> str:
    return ((getattr(fn, '__doc__', None) or '').strip().split('\n') or [''])[0]


def forms(catalog: Catalog) -> Dict[str, Dict[str, Any]]:
    """Every form with its keys resolved, for whoever shows them."""
    out = {}
    for name, spec in (catalog.get('forms') or {}).items():
        out[name] = {k: v for k, v in spec.items() if k not in ('keys', 'includes', 'without')}
        out[name]['keys'] = keys_of(catalog, name)
    return out


def uses(data: Dict[str, Any], vocabulary: Dict[str, Any], catalog: Catalog,
         ) -> Dict[str, List[Dict[str, Any]]]:
    """Where each key of a form is used in the merged tree: `Form.key` -> uses.

    A use is the real path, the plugin and the value, cut short when it is a
    structure: the point is to find it, the value is read where it lives.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for norm, key, real, plugin, value in _walk(data, vocabulary):
        form = form_at(catalog, norm)
        if form:
            out.setdefault(f'{form}.{key}', []).append(
                {'path': f'{real}.{key}', 'plugin': plugin, 'value': _short(value)})
    return out


def _walk(data, vocabulary) -> Iterator[Tuple[str, str, str, Optional[str], Any]]:
    from kitebase.vocabulary import walk_values
    return walk_values(data, vocabulary)


def _short(value: Any) -> Any:
    if isinstance(value, dict):
        return '{...}'
    if isinstance(value, list):
        return value if all(isinstance(v, (str, int, float, bool)) for v in value) and len(value) <= 8 else '[...]'
    return value


def undescribed(catalog: Catalog, vocabulary: Dict[str, Any]) -> List[Tuple[str, str]]:
    """The keys the vocabulary has at a path the catalog knows, and it does not describe."""
    missing = []
    for norm, keys in vocabulary.items():
        form = form_at(catalog, norm)
        if not form or not isinstance(keys, list):
            continue
        known = keys_of(catalog, form)
        missing += [(norm, key) for key in keys if key not in known]
    return missing
