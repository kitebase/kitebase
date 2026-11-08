"""
kitebase.introspect — what the inspector asks of a loaded application.

A thin layer: each function receives the application, already composed by its
own script, and returns plain data, ready for JSON. Nothing here decides or
judges (that is `diagnostics`); it shows what the application is, and where
each piece of it was written.

The point the merged tree cannot answer by itself is provenance: which plugin
wrote which key, in which file and on which line. The plugins' files are read
again for that, as they are on disk now: `yaml.compose` keeps the position of
every node, so the source of a column is found by walking to it, without
carrying positions through the merge.

What leaves here never holds a secret: the files are masked as the dump masks
them (`diagnostics.mask_yaml_text`, which keeps the lines where they are), and
the data as a whole goes through `mask_secrets`.
"""
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml


# ── Plain data ─────────────────────────────────────────────────────────────────

def plain(value: Any) -> Any:
    """The value with what JSON cannot hold turned into names or text.

    A resolved foreign key holds the table object it points at, a type its
    plugin: they become their names.
    """
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    name = getattr(value, 'name', None)
    if isinstance(name, str):
        return name
    return str(value)


def _masked(data: Any) -> Any:
    from kitebase.diagnostics import mask_secrets
    return mask_secrets(plain(data))


# ── The source files, and the place of a node in them ──────────────────────────

def plugin_files(app: Any) -> List[Tuple[str, Path, Any]]:
    """Every data file of every plugin, in load order: (plugin, path, parsed)."""
    pm = app.pm
    out = []
    for name in pm.sorted:
        p = pm.plugins[name]
        out += [(name, path, parsed) for path, parsed in zip(p.data_files, p.data)]
    return out


def read_source(path: Path) -> Optional[str]:
    """The file as it may be shown: masked, each line where it was."""
    from kitebase.diagnostics import mask_yaml_text
    raw = _read(path)
    return None if raw is None else mask_yaml_text(raw)


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return None


# A step of a path in a file: a key of a mapping, or the item of a list whose
# identity key has that value (`{'name': 'title'}`).
Step = Any


def locate(text: str, steps: Sequence[Step]) -> Optional[Tuple[int, int]]:
    """The lines (first, last, 1-based) of the node at `steps`, or None."""
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return None
    for step in steps:
        node = _child(_unwrap(node), step)
        if node is None:
            return None
    lines = text.splitlines()
    first = node.start_mark.line
    # A block ends where what follows begins: on the next item's line, after
    # its indentation, or at the start of a line. Only a flow node ends after
    # text of its own on the same line.
    end = node.end_mark
    last = end.line if lines[end.line:end.line + 1] and lines[end.line][:end.column].strip() else end.line - 1
    # The comments after a node introduce what follows it
    while last > first and (not lines[last].strip() or lines[last].lstrip().startswith('#')):
        last -= 1
    return first + 1, max(last, first) + 1


def _child(node: Any, step: Step) -> Any:
    if isinstance(step, dict):
        if not isinstance(node, yaml.SequenceNode):
            return None
        (key, value), = step.items()
        for item in node.value:
            if isinstance(item, yaml.MappingNode) and any(
                    k.value == key and getattr(v, 'value', None) == value
                    for k, v in item.value):
                return item
        return None
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if k.value == step:
            # The entry starts at its key: `- name: title` is the item, `title:` the key
            return _Span(k, v)
    return None


class _Span:
    """A mapping entry, from its key to the end of its value."""

    def __init__(self, key: Any, value: Any):
        self.start_mark = key.start_mark
        self.end_mark = value.end_mark
        self.value = value.value
        self._node = value

    def __getattr__(self, name):
        return getattr(self._node, name)


def _unwrap(node: Any) -> Any:
    return node._node if isinstance(node, _Span) else node


def layer(plugin: str, path: Path, steps: Sequence[Step], declared: Any) -> Dict[str, Any]:
    """What one plugin wrote at `steps` of one file, with its lines.

    The place is found in the file as written, the lines are cut from the
    masked one: masking keeps every line where it is, but what it writes
    (`***`) is not always valid YAML.
    """
    from kitebase.diagnostics import mask_yaml_text
    out: Dict[str, Any] = {'plugin': plugin, 'file': str(path), 'declared': _masked(_no_meta(declared))}
    raw = _read(path)
    lines = locate(raw, steps) if raw is not None else None
    if lines:
        first, last = lines
        shown = mask_yaml_text(raw).splitlines(keepends=True)
        out.update(line=first, end=last, text=''.join(shown[first - 1:last]))
    return out


def _no_meta(value: Any) -> Any:
    # The merge writes `$plugin` into what it reads: the layer already says it
    if isinstance(value, dict):
        return {k: _no_meta(v) for k, v in value.items() if not str(k).startswith('$')}
    if isinstance(value, list):
        return [_no_meta(v) for v in value]
    return value


def find_layers(app: Any, steps: Sequence[Step]) -> List[Dict[str, Any]]:
    """Every plugin file that writes the node at `steps`, in load order."""
    out = []
    for plugin, path, parsed in plugin_files(app):
        declared = _get(parsed, steps)
        if declared is not None:
            out.append(layer(plugin, path, steps, declared))
    return out


def _get(data: Any, steps: Sequence[Step]) -> Any:
    for step in steps:
        if isinstance(step, dict):
            (key, value), = step.items()
            if not isinstance(data, list):
                return None
            data = next((i for i in data if isinstance(i, dict) and i.get(key) == value), None)
        elif isinstance(data, dict):
            data = data.get(step)
        else:
            return None
        if data is None:
            return None
    return data


# ── The application ────────────────────────────────────────────────────────────

def app_summary(app: Any) -> Dict[str, Any]:
    pm = app.pm
    return {
        'app': {k: pm.config.get(k) for k in ('name', 'version', 'description')},
        'root': str(pm.app_root),
        'plugins': [
            {
                'name': name,
                'version': pm.plugins[name].config.get('version'),
                'description': pm.plugins[name].config.get('description'),
                'depends_on': pm.plugins[name].config.get('depends_on') or [],
                'dir': str(pm.plugins[name].plugin_dir),
                'data_files': [f.name for f in pm.plugins[name].data_files],
                'sources': [f.name for f in pm.plugins[name].sources],
            }
            for name in pm.sorted
        ],
        'counts': {
            'tables': len(app.tables),
            'types': sum(1 for t in app.types.values() if t.plugin != ''),
            'pages': _count(pm.data.get('pages')),
            'views': _count(pm.data.get('views')),
            'endpoints': len(_endpoints()),
        },
    }


def _count(section: Any) -> int:
    return sum(1 for k in (section or {}) if not str(k).startswith('$'))


def issues(app: Any) -> List[Dict[str, Any]]:
    from kitebase.diagnostics import run_checks
    return run_checks(app)


def catalog(app: Any) -> Dict[str, Any]:
    """The catalog of the forms, and where the application uses each key."""
    from kitebase import catalog as cat_mod, vocabulary

    cat = cat_mod.load()
    sections = {norm.split('.')[0] for norm in cat.get('roots') or {}}
    data = {name: app.pm.data[name] for name in sections if name in app.pm.data}
    return _masked({
        'roots': cat.get('roots') or {},
        'forms': cat_mod.forms(cat),
        'directives': cat.get('directives') or {},
        'tokens': cat_mod.tokens(cat),
        'uses': cat_mod.uses(data, vocabulary.load(), cat),
    })


def _endpoints() -> Dict[str, Any]:
    from kitebase.endpoints import _ENDPOINTS
    return _ENDPOINTS


def endpoints() -> Dict[str, Dict[str, Any]]:
    """Every registered operation, with its docstring and where it is written."""
    out = {}
    for name, fn in sorted(_endpoints().items()):
        doc = inspect.cleandoc(getattr(fn, '__doc__', None) or '')
        entry: Dict[str, Any] = {
            'module': getattr(fn, '__module__', ''),
            'doc': doc.split('\n')[0] if doc else '',
            'docstring': doc,
        }
        try:
            entry['file'] = inspect.getsourcefile(fn)
            entry['line'] = inspect.getsourcelines(fn)[1]
        except (OSError, TypeError):
            pass
        out[name] = entry
    return out


# ── Tables ─────────────────────────────────────────────────────────────────────

def tables(app: Any) -> List[Dict[str, Any]]:
    """The tables, with who declares them: the index of the inspector."""
    return [
        {
            'name': name,
            'label': t.attributes.get('label'),
            'plugins': list(dict.fromkeys(l['plugin'] for l in find_layers(app, ['tables', name]))),
            'columns': len(t.effective_columns),
            'mixins': t.attributes.get('mixins') or [],
        }
        for name, t in sorted(app.tables.items())
    ]


def table(app: Any, name: str) -> Dict[str, Any]:
    """A table as it runs, and, column by column, where each piece was written."""
    t = app.tables.get(name)
    if t is None:
        raise KeyError(name)
    own = {id(c) for c in t.columns + t.virtual_columns}
    columns = []
    for col in t.effective_columns:
        if id(col) in own:
            origin = {'kind': 'table'}
            layers = find_layers(app, ['tables', name, 'columns', {'name': col.name}])
        else:
            mixin = _mixin_of(app, t, col)
            origin = {'kind': 'mixin', 'type': mixin}
            layers = find_layers(app, ['types', mixin, 'columns', {'name': col.name}]) if mixin else []
        columns.append({
            'name': col.name,
            'attributes': _masked(_own(col.attributes, 'plugin')),
            'type': col.db_type.name if col.db_type else None,
            'types': type_chain(app, col.db_type.name) if col.db_type else [],
            'origin': origin,
            'layers': layers,
        })
    layers = find_layers(app, ['tables', name])
    return {
        'name': name,
        'attributes': _masked(_own(t.attributes, 'columns')),
        'plugins': list(dict.fromkeys(l['plugin'] for l in layers)),
        'layers': layers,
        'columns': columns,
        'used_by': used_by(app, name),
    }


def _own(attributes: Dict[str, Any], *left_out: str) -> Dict[str, Any]:
    # The attribution is told by the layers, and `$` keys are the framework's
    return {k: v for k, v in attributes.items()
            if k not in left_out and not str(k).startswith('$')}


def _mixin_of(app: Any, table: Any, col: Any) -> Optional[str]:
    for mixin in table.attributes.get('mixins', []):
        mtype = app.types.get(mixin)
        if mtype and any(c is col for c in mtype.columns + mtype.virtual_columns):
            return mixin
    return None


def type_chain(app: Any, name: str) -> List[Dict[str, Any]]:
    """A type and those it derives from, each with the plugin and file that declare it."""
    out = []
    seen = set()
    while name and name not in seen:
        seen.add(name)
        t = app.types.get(name)
        if t is None:
            break
        builtin = t.plugin == ''
        out.append({
            'name': name,
            'builtin': builtin,
            'plugin': None if builtin else getattr(t.plugin, 'name', str(t.plugin)),
            'layers': [] if builtin else find_layers(app, ['types', name]),
        })
        if builtin:
            break
        name = _declared_base(app, name)
    return out


def _declared_base(app: Any, name: str) -> Optional[str]:
    # The resolved attributes carry the whole chain merged: the base a type
    # names is the one its own declaration writes.
    for _, _, parsed in plugin_files(app):
        declared = _get(parsed, ['types', name])
        if isinstance(declared, dict) and declared.get('base'):
            return declared['base']
    t = app.types.get(name)
    return t.inheritance[0] if t is not None and t.inheritance else None


def used_by(app: Any, name: str) -> Dict[str, List[str]]:
    """What points at the table: foreign keys of other tables, views and pages on it."""
    fks = []
    for other, t in sorted(app.tables.items()):
        for col in t.effective_columns:
            fk = col.attributes.get('foreign_key')
            if isinstance(fk, dict) and getattr(fk.get('table'), 'name', None) == name:
                fks.append(f'{other}.{col.name}')
    views = [vid for vid, v in (app.pm.data.get('views') or {}).items()
             if not str(vid).startswith('$') and _model(v) == name]
    pages = [pid for pid, p in (app.pm.data.get('pages') or {}).items()
             if not str(pid).startswith('$') and _model((p or {}).get('content')) == name]
    return {'foreign_keys': fks, 'views': sorted(views), 'pages': sorted(pages)}


def _model(node: Any) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    source = node.get('source')
    return source.get('model') if isinstance(source, dict) else None


# ── A whole file ───────────────────────────────────────────────────────────────

def source(app: Any, plugin: str, file: str) -> Dict[str, Any]:
    """A data file of a plugin, masked, to read it whole around a layer."""
    p = app.pm.plugins.get(plugin)
    if p is None:
        raise KeyError(plugin)
    allowed = {f.name: f for f in p.data_files}
    allowed['config.yaml'] = p.plugin_dir / 'config.yaml'
    path = allowed.get(file)
    if path is None:
        raise KeyError(file)
    return {'plugin': plugin, 'file': str(path), 'text': read_source(path) or ''}
