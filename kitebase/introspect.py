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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
        if isinstance(step, int):
            # A list without identity: its positions change in the merge, and
            # what a file holds at one is not what the application holds there
            return None
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


# ── The merged tree ────────────────────────────────────────────────────────────
#
# Every root of the merge (`tables`, `pages`, `menu_items`, `roles`...) shown in
# one way: a node of the tree as it runs, the plugins that write it, and their
# lines. A node is named by a path, `pages.book_detail.panels[authors]`: keys
# joined by dots, the item of a list by the value of its identity key (the one
# the merge goes by, `utils.identity_key`), or by its position when the list has
# none. Keys may hold dots themselves (`roles.library.desk`): the path is read
# along the tree, the longest key that fits first.

def parse_path(data: Any, text: str) -> List[Step]:
    """The steps of `text` in `data`; KeyError where the tree has no such node."""
    from kitebase.utils import identity_key

    steps: List[Step] = []
    node, rest = data, text.strip()
    while rest:
        if rest.startswith('['):
            close = rest.find(']')
            if close < 0 or not isinstance(node, list):
                raise KeyError(text)
            token, rest = rest[1:close], rest[close + 1:].removeprefix('.')
            key = identity_key(node)
            match = next((i for i in node if isinstance(i, dict) and key
                          and str(i.get(key)) == token), None)
            if match is not None:
                steps.append({key: match[key]})
                node = match
            elif token.isdigit() and int(token) < len(node):
                # A position, as the paths of `check` say it: the item by its
                # name when it has one
                node = node[int(token)]
                named = key and isinstance(node, dict) and node.get(key) is not None
                steps.append({key: node[key]} if named else int(token))
            else:
                raise KeyError(text)
            continue
        if not isinstance(node, dict):
            raise KeyError(text)
        fits = [k for k in node if isinstance(k, str) and (
            rest == k or rest.startswith(k + '.') or rest.startswith(k + '['))]
        if not fits:
            raise KeyError(text)
        key = max(fits, key=len)
        steps.append(key)
        node, rest = node[key], rest[len(key):].removeprefix('.')
    return steps


def path_text(steps: Sequence[Step]) -> str:
    out = ''
    for step in steps:
        if isinstance(step, dict):
            out += f'[{next(iter(step.values()))}]'
        elif isinstance(step, int):
            out += f'[{step}]'
        else:
            out += f'.{step}' if out else str(step)
    return out


def _at(data: Any, steps: Sequence[Step]) -> Any:
    for step in steps:
        if isinstance(step, int):
            data = data[step]
        elif isinstance(step, dict):
            (key, value), = step.items()
            data = next(i for i in data if isinstance(i, dict) and i.get(key) == value)
        else:
            data = data[step]
    return data


def writers(app: Any, steps: Sequence[Step]) -> List[str]:
    """The plugins whose files write the node, from what they parsed: cheap
    enough for every child of a node, where `find_layers` reads the files."""
    return list(dict.fromkeys(plugin for plugin, _, parsed in plugin_files(app)
                              if _get(parsed, steps) is not None))


def _kind(value: Any) -> str:
    return 'map' if isinstance(value, dict) else 'list' if isinstance(value, list) else 'value'


def _summary(value: Any) -> Any:
    if isinstance(value, dict):
        return len([k for k in value if not str(k).startswith('$')])
    if isinstance(value, list):
        return len(value)
    return value


def _children(app: Any, steps: List[Step], value: Any) -> List[Dict[str, Any]]:
    # `value` comes masked: the summaries of its values are shown as they are
    from kitebase.utils import identity_key

    if isinstance(value, dict):
        entries = [(k, [*steps, k], v) for k, v in value.items()]
    elif isinstance(value, list):
        key = identity_key(value)
        entries = []
        for n, item in enumerate(value):
            named = key and isinstance(item, dict) and item.get(key) is not None
            step = {key: item[key]} if named else n
            entries.append((item[key] if named else n, [*steps, step], item))
    else:
        return []
    return [{
        'key': str(k),
        'path': path_text(s),
        'kind': _kind(v),
        'summary': _summary(v),
        'plugins': writers(app, s),
    } for k, s, v in entries]


def roots(app: Any) -> List[Dict[str, Any]]:
    """The roots of the merge, with how many entries and who writes them."""
    return [{
        'name': name,
        'kind': _kind(value),
        'count': _summary(value),
        'plugins': writers(app, [name]),
    } for name, value in app.pm.data.items() if not str(name).startswith('$')]


def node(app: Any, path: str, subject: Optional[str] = None) -> Dict[str, Any]:
    """A node of the merged tree as it runs, its children, and where it is written.

    The layers are those of the node itself, or, under a list without identity,
    of the nearest node above it that a file can be searched for (`layers_of`).
    A page or a view also comes resolved (`resolved`), and, for a `subject` of
    the guard, as that subject would receive it.
    """
    data = app.pm.data
    steps = parse_path(data, path)
    # Masked from its root down: whether a value is secret is said by the key
    # that holds it, which the value alone no longer has
    value = _at(_masked({steps[0]: _drop_plugin(data[steps[0]])}), steps)
    located = steps[:next((i for i, s in enumerate(steps) if isinstance(s, int)), len(steps))]
    return {
        'path': path_text(steps),
        'trail': [{'key': str(s if not isinstance(s, dict) else next(iter(s.values()))),
                   'path': path_text(steps[:i + 1])} for i, s in enumerate(steps)],
        'kind': _kind(value),
        'value': value,
        'plugin': _at(data, steps).get('$plugin') if isinstance(value, dict) else None,
        'children': _children(app, steps, value),
        'layers': find_layers(app, located) if located else [],
        'layers_of': path_text(located),
        'resolved': _resolved(app, steps, subject),
        'detail': _detail(app, steps),
    }


# What a plugin adds to the nodes of its own root, which the core cannot know:
# `roles.<code>` of acl, its closure and what it gives. `fn(app, key)` returns
# blocks the page draws without knowing what they mean:
#
#   {'title': str, 'doc': str?, 'links': [cell, ...]}
#   {'title': str, 'doc': str?, 'columns': [str, ...], 'rows': [[cell, ...], ...]}
#
# A cell is a text, or {'text', 'path'?, 'subject'?, 'tone'?, 'title'?}: `path`
# opens that node (seen by `subject`), `tone` is ok, cond, no or faint, and
# `title` is what the cell says when pointed at.
_DETAILS: Dict[str, Callable[[Any, str], List[Dict[str, Any]]]] = {}


def add_detail(root: str, fn: Callable[[Any, str], List[Dict[str, Any]]]) -> None:
    """Register what the nodes `<root>.<key>` show beyond the tree: one per root."""
    if _DETAILS.get(root, fn) is not fn:
        raise ValueError(f"The nodes of '{root}' already have a detail")
    _DETAILS[root] = fn


def _detail(app: Any, steps: Sequence[Step]) -> Optional[List[Dict[str, Any]]]:
    fn = _DETAILS.get(steps[0]) if len(steps) == 2 and isinstance(steps[1], str) else None
    return _masked(fn(app, steps[1])) if fn else None


# The descriptors the client receives resolved: a page by `get_page`, a view
# inside the pages that take it
_RESOLVED = ('pages', 'views')


def _resolved(app: Any, steps: Sequence[Step], subject: Optional[str]) -> Optional[Dict[str, Any]]:
    """A page or a view with its `$ref`s resolved, as `load_page` makes it.

    With a subject of the guard (`access.subjects`: a role, for `acl`), what
    `get_page` would take out for it is taken out (`pages.restrict_page`), and a
    page says the permissions it would be refused at the door. Without one
    nothing is taken out: there is no user here to judge.
    """
    import copy
    from kitebase import access
    from kitebase.pages import load_page, page_permissions, restrict_page

    if len(steps) != 2 or steps[0] not in _RESOLVED or not isinstance(steps[1], str):
        return None
    root, ident = steps
    subjects = access.subjects(app)
    holds = None
    if subject:
        holds = subjects.get(subject)
        if holds is None:
            raise KeyError(subject)
    out: Dict[str, Any] = {'subjects': list(subjects), 'subject': subject or None}
    try:
        if root == 'pages':
            obj = load_page(app, ident)
        else:
            obj = app.pm.resolve_refs(copy.deepcopy(app.pm.data[root][ident]))
    except ValueError as e:
        return {**out, 'error': str(e)}
    if holds is not None:
        if root == 'pages':
            out['refused'] = [p for p in page_permissions(app, ident) or [] if not holds(p)]
        with access.holding(holds):
            restrict_page(app, obj)
    out['value'] = _masked(_drop_plugin(obj))
    return out


def _drop_plugin(value: Any) -> Any:
    # `$plugin` is the merge's bookkeeping, told by the layers; the other `$`
    # keys are the author's (`$ref`) and stay
    if isinstance(value, dict):
        return {k: _drop_plugin(v) for k, v in value.items() if k != '$plugin'}
    if isinstance(value, list):
        return [_drop_plugin(v) for v in value]
    return value


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
