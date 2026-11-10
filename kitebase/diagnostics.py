"""
kitebase.diagnostics — post-load validation and effective-state dump.

Two entry points (run_checks is pure; dump_app reads the plugin source files
to embed them; neither uses argparse):

  run_checks(app)  -> List[issue]
      Validate the merged plugin data after everything is loaded
      (calc_db done): unresolved $ref, views pointing at unknown
      tables/columns, stack_push targets that don't exist, orphan
      views, roots nobody owns, and the checks the owners of the
      others registered. Merge-time issues collected by PluginsManager (value
      overlaps, positional anchors not found) are included first.

  dump_app(app)    -> Dict
      Complete JSON-serializable snapshot of the effective application
      state: plugins, issues, tables, types, every owned root of the
      merged tree (pages, views, menus, … with $ref resolved), endpoints,
      merge history, and source_files (raw pre-merge YAML per plugin/file).
      Secret values and URL credentials are masked (mask_secrets).
      $plugin attribution keys are kept so an external viewer (web app,
      tkinter, jq, ...) can show which plugin contributed each piece.
      DB/type roots (tables/types/schemas) are special-cased; the other
      owned roots are treated uniformly (owners: kitebase.sections).

Issue format (shared with PluginsManager.add_issue):
  {severity: 'error'|'warning'|'info', code, path, message, plugin}
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from kitebase import access, help_texts, media, settings
from kitebase.sections import get_section

# Only plain identifiers ('title') and dotted fields ('Publisher.name') are
# checkable against the schema; anything else (SQL expressions, aliases,
# functions) is passed through to the querybuilder untouched.
_CHECKABLE_FIELD = re.compile(r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$')

# Roots with dumpers of their own (tables via _table_dict, types via the schema
# registry): every other owned root is dumped as it was merged.
_DB_SECTIONS = frozenset({'tables', 'types', 'schemas'})


def _owned_sections(pm: Any) -> List[str]:
    """The roots of the merged tree that a registered module owns."""
    return [k for k, v in pm.data.items()
            if get_section(k) is not None and isinstance(v, dict)]


def _descriptor_sections(pm: Any) -> List[str]:
    """The roots that are trees of descriptors (pages, views, menus, ...)."""
    return [k for k in _owned_sections(pm) if get_section(k).descriptor]


def _check_sections(app: Any, issues: List[Dict[str, Any]]) -> None:
    """A root nobody owns is read by nobody: a typo, or the plugin that reads it
    is not loaded. The owned ones bring their own checks."""
    pm = app.plugins
    for root in pm.data:
        if root.startswith('$') or get_section(root) is not None:
            continue
        declared_by = sorted(set(pm.history.get(root, [])))
        issues.append(make_issue(
            'warning', 'section-unowned', root,
            f"no loaded plugin owns '{root}': load the plugin that reads it, "
            f"or remove the declaration",
            ', '.join(declared_by) or None))
    for root in _owned_sections(pm):
        check = get_section(root).check
        if check is not None:
            issues.extend(check(app))


def make_issue(severity: str, code: str, path: str, message: str,
               plugin: Optional[str] = None) -> Dict[str, Any]:
    """Build an issue dict in the shared diagnostics format."""
    return {'severity': severity, 'code': code, 'path': path,
            'message': message, 'plugin': plugin}


# ── Checks ─────────────────────────────────────────────────────────────────────

def run_checks(app: Any) -> List[Dict[str, Any]]:
    """
    Validate the merged descriptor data of a fully loaded app.

    Args:
        app: Initialized kitebase app (setup_schema() is sufficient —
             needs app.plugins and app.tables, no DB engine)

    Returns:
        List of issues, merge-time ones first, then descriptor checks.
    """
    pm = app.plugins
    issues: List[Dict[str, Any]] = list(getattr(pm, 'issues', []))
    referenced: Set[str] = set()

    # Generic descriptor validation: walk every descriptor root uniformly
    # (pages, views, menus, menu_items, and any root registered as one). The
    # per-node checks ($ref, view source, stack_push target) fire wherever they
    # appear.
    for section in _descriptor_sections(pm):
        data = pm.data.get(section) or {}
        for item_id, item in data.items():
            if item_id.startswith('$'):
                continue
            _walk(app, item, f'{section}.{item_id}', None, issues, referenced)

    _check_auto_pages(app, issues)
    _check_query_ranks(app, issues)
    _check_client(app, issues)
    _check_validators(app, issues)
    _check_vocabulary(app, issues)
    issues.extend(access.diagnose(app))
    issues.extend(media.diagnose(app))
    issues.extend(help_texts.diagnose(app))
    issues.extend(settings.diagnose(app, _descriptor_sections(pm)))
    _check_sections(app, issues)

    # Orphan views: defined but never targeted by a $ref
    views = pm.data.get('views') or {}
    for view_id, view in views.items():
        if view_id.startswith('$'):
            continue
        if f'views.{view_id}' not in referenced:
            plugin = view.get('$plugin') if isinstance(view, dict) else None
            issues.append(make_issue(
                'info', 'view-orphan', f'views.{view_id}',
                'view is not referenced by any page', plugin))

    return issues


def _walk(app: Any, obj: Any, path: str, plugin: Optional[str],
          issues: List[Dict[str, Any]], referenced: Set[str]) -> None:
    """Recursively check a descriptor subtree, tracking $plugin attribution."""
    if isinstance(obj, dict):
        plugin = obj.get('$plugin', plugin)

        ref = obj.get('$ref')
        if isinstance(ref, str):
            referenced.add(ref)
            if app.plugins.get(ref) is None:
                issues.append(make_issue(
                    'error', 'ref-unresolved', path,
                    f"$ref '{ref}' does not resolve", plugin))

        if isinstance(obj.get('source'), dict):
            _check_view(app, obj, path, plugin, issues)

        if obj.get('action') == 'stack_push':
            _check_push_target(app, obj, path, plugin, issues)

        if obj.get('type') == 'collection':
            _check_collection(app, obj, path, plugin, issues)

        if isinstance(obj.get('commands'), list):
            _check_commands(app, obj['commands'], f'{path}.commands', plugin, issues)

        if isinstance(obj.get('phases'), list):
            _check_phases(app, obj, path, plugin, issues)

        for key, value in obj.items():
            if not isinstance(key, str):
                # YAML 1.1 reads an unquoted on/off/yes/no as a boolean: `on:`
                # in a join arrives as `True:` and the condition is lost.
                issues.append(make_issue(
                    'warning', 'key-not-string', f'{path}.{key}',
                    f"key {key!r} is not a string — YAML reads unquoted "
                    f"on/off/yes/no as booleans: quote it (\"on\":)", plugin))
                continue
            if key.startswith('$'):
                continue
            _walk(app, value, f'{path}.{key}', plugin, issues, referenced)

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            label: Any = i
            if isinstance(item, dict):
                label = item.get('id') or item.get('name') or item.get('field') or i
            _walk(app, item, f'{path}[{label}]', plugin, issues, referenced)


def _check_view(app: Any, view: Dict[str, Any], path: str,
                plugin: Optional[str], issues: List[Dict[str, Any]]) -> None:
    """Check a view-like dict (has source.model): model + columns/fields."""
    model = view['source'].get('model')
    if not isinstance(model, str):
        return

    table = app.tables.get(model)
    if table is None:
        issues.append(make_issue(
            'error', 'model-missing', f'{path}.source.model',
            f"table '{model}' is not defined", plugin))
        return

    # A RefSet is read with the record: a list column or a form field may name it
    colnames = {c.name for c in table.effective_columns} | set(table.ref_sets)

    keys: Dict[str, str] = {}
    for col in view.get('columns') or []:
        if isinstance(col, dict) and isinstance(col.get('field'), str):
            field = col['field']
            _check_field(app, field, colnames, model,
                         f'{path}.columns[{field}]', plugin, issues)
            key = _field_key(field)
            if key in keys:
                issues.append(make_issue(
                    'error', 'field-key-collision', f'{path}.columns[{field}]',
                    f"'{field}' and '{keys[key]}' both arrive as '{key}': the "
                    f"client cannot tell them apart and one column shows the "
                    f"other's values — give one an alias ('{field} as ...')", plugin))
            else:
                keys[key] = field
            _check_variant(app, table, col, f'{path}.columns[{field}]', plugin, issues)
            _check_tone(col, f'{path}.columns[{field}]', plugin, issues)

    _check_card(view, keys, path, plugin, issues)
    _check_fields(view.get('fields') or [], colnames, model,
                  f'{path}.fields', plugin, issues)


# The tones a view may give a row (client: components/tone.ts): names, drawn
# by each renderer with the theme's tokens.
TONES = ('accent', 'success', 'warning', 'danger', 'muted')


def _check_tone(col: Dict[str, Any], path: str, plugin: Optional[str],
                issues: List[Dict[str, Any]]) -> None:
    """A column's tone names tones of the list, by exact value or by range."""
    tone = col.get('tone')
    if not isinstance(tone, dict):
        return

    def named(name: Any, where: str) -> None:
        if name not in TONES:
            issues.append(make_issue(
                'warning', 'tone-value', f'{path}.tone.{where}',
                f"'{name}' is not a tone, so it gives none; "
                f"the tones are: {', '.join(TONES)}", plugin))

    for value, name in (tone.get('values') or {}).items():
        if not str(value).startswith('$'):          # merge metadata ($plugin)
            named(name, f'values.{value}')
    for i, entry in enumerate(tone.get('ranges') or []):
        if (not isinstance(entry, list) or len(entry) != 2
                or isinstance(entry[0], bool) or not isinstance(entry[0], (int, float))):
            issues.append(make_issue(
                'warning', 'tone-range', f'{path}.tone.ranges[{i}]',
                f"a range is [minimum, tone], as [4, success]; {entry!r} gives no tone", plugin))
        else:
            named(entry[1], f'ranges[{i}]')


def _check_card(view: Dict[str, Any], keys: Dict[str, str], path: str,
                plugin: Optional[str], issues: List[Dict[str, Any]]) -> None:
    """A card names columns of its view by key: the card shows what the query brought."""
    card = view.get('card')
    if not isinstance(card, dict):
        return
    named = [(k, card[k]) for k in ('image', 'title', 'subtitle', 'ribbon') if isinstance(card.get(k), str)]
    named += [('details', f) for f in card.get('details') or [] if isinstance(f, str)]
    for where, key in named:
        if key not in keys:
            known = ', '.join(sorted(keys)) or 'none'
            issues.append(make_issue(
                'warning', 'card-field', f'{path}.card.{where}',
                f"'{key}' is not the key of a column of the view, so the card "
                f"shows nothing there; the keys are: {known}", plugin))


def _check_collection(app: Any, node: Dict[str, Any], path: str,
                      plugin: Optional[str], issues: List[Dict[str, Any]]) -> None:
    """A collection node should say whether the rows it edits are parts of the parent.

    Ownership and presentation are different axes. `owned` (PLUGIN_MODEL § 4.5) is
    about lifetime: whether the row is a part and dies with its parent. The node is
    about editing: whether it is worked on inside the parent's form, in one buffer
    and one transaction. Most collections are compositions — document lines, the
    contacts of a partner — but not all: the reviews of a book can be edited there
    and still outlive it.

    So this asks for a decision instead of imposing one, and goes quiet as soon as
    either answer is written. It exists because the mistake it catches is silent:
    rows that look contained, and are left behind with a null key the first time the
    parent is deleted.
    """
    model = node.get('model')
    fk = node.get('fk')
    if not isinstance(model, str) or not isinstance(fk, str):
        return          # a malformed node: pages.resolve_collections says so, loudly

    table = app.tables.get(model)
    if table is None:
        issues.append(make_issue(
            'error', 'model-missing', f'{path}.model',
            f"table '{model}' is not defined", plugin))
        return

    column = next((c for c in table.effective_columns if c.name == fk), None)
    if column is None:
        issues.append(make_issue(
            'error', 'field-unknown', f'{path}.fk',
            f"'{model}' has no column '{fk}'", plugin))
        return

    foreign_key = column.attributes.get('foreign_key')
    if not foreign_key:
        issues.append(make_issue(
            'error', 'collection-fk-not-a-key', f'{path}.fk',
            f"'{model}.{fk}' is not a foreign key", plugin))
        return

    # A junction is owned by both its ends unless a target derogates, so there the
    # question is already answered and nothing needs writing.
    if 'many_to_many' in table.attributes:
        return

    if 'owned' not in foreign_key:
        issues.append(make_issue(
            'warning', 'collection-ownership-unstated', f'{path}.fk',
            f"'{model}.{fk}' does not say whether it is owned: rows edited here will "
            f"not follow the parent when it is deleted — write 'owned: false' to "
            f"confirm that, or 'owned: true' to make them parts of it", plugin))


def _check_variant(app: Any, table: Any, col: Dict[str, Any], path: str,
                   plugin: Optional[str], issues: List[Dict[str, Any]]) -> None:
    """An image column of a grid names the variant it shows, and one that exists.

    Without one the grid gets the originals, each photo at full size; an
    unknown one makes the query fail, as a Files column does: its files are in
    a folder, not in the row.
    """
    media = getattr(app, 'media', None)
    variants = getattr(media, 'variants', None) or {}
    column = next((c for c in table.effective_columns if c.name == col['field']), None)
    if column is not None and column.attributes.get('media') == 'files':
        issues.append(make_issue(
            'error', 'media-files', path,
            f"'{col['field']}' is a Files column: a grid has no value to show "
            f"for it, and its query fails; the form lists its files", plugin))
        return
    if column is None or column.attributes.get('media') != 'image':
        return
    variant = col.get('variant')
    if variant is None:
        if variants:
            issues.append(make_issue(
                'warning', 'media-variant', path,
                f"image column without variant: the grid downloads every original; "
                f"name one of {', '.join(sorted(variants))}", plugin))
    elif variant not in variants:
        issues.append(make_issue(
            'error', 'media-variant', path,
            f"variant '{variant}' is not in media.variants: the query fails", plugin))


def _check_field(app: Any, field: str, colnames: Set[str], model: str,
                 path: str, plugin: Optional[str],
                 issues: List[Dict[str, Any]]) -> None:
    """Check a table column reference; dotted fields resolve on the joined table."""
    if field.startswith('$') or not _CHECKABLE_FIELD.match(field):
        return

    if '.' in field:
        t_name, col_name = field.split('.', 1)
        target = app.tables.get(t_name)
        if target is None:
            issues.append(make_issue(
                'warning', 'field-unknown', path,
                f"joined field '{field}': table '{t_name}' is not defined", plugin))
        elif col_name not in {c.name for c in target.effective_columns}:
            issues.append(make_issue(
                'warning', 'field-unknown', path,
                f"joined field '{field}': column '{col_name}' not in '{t_name}'", plugin))
        return

    if field not in colnames:
        issues.append(make_issue(
            'warning', 'field-unknown', path,
            f"field '{field}' not found in table '{model}'", plugin))


def _field_key(field: str) -> str:
    """The key a column's values arrive under: the alias, else the last segment.

    The same rule as the client's `extractFieldKey` (dataview.query.ts): two
    columns of a view with the same key are one column to the grid.
    """
    as_idx = field.lower().rfind(' as ')
    if as_idx != -1:
        return field[as_idx + 4:].strip()
    return field.rsplit('.', 1)[-1]


def _check_fields(fields: List[Any], colnames: Set[str], model: str,
                  path: str, plugin: Optional[str],
                  issues: List[Dict[str, Any]]) -> None:
    """Check form fields, recursing into groups ({group, fields})."""
    for f in fields:
        if not isinstance(f, dict):
            continue
        if isinstance(f.get('fields'), list):
            _check_fields(f['fields'], colnames, model,
                          f"{path}[{f.get('group', '?')}]", plugin, issues)
            continue
        name = f.get('name')
        if (isinstance(name, str) and not name.startswith('$')
                and '.' not in name and name not in colnames):
            issues.append(make_issue(
                'warning', 'field-unknown', f'{path}[{name}]',
                f"field '{name}' not found in table '{model}'", plugin))


# Where a column stands when a query is built — the rule editor's field list,
# the order combo. `normal` is the default and the declaration order within.
QUERY_RANKS = ('top', 'normal', 'low', 'more', 'none')


def _check_query_ranks(app: Any, issues: List[Dict[str, Any]]) -> None:
    """A `query_rank` outside the scale is an error, not a quiet `normal`.

    The client would place an unknown value like the default, so a typo
    (`hight`) would leave the field where it was with nothing to say why.
    """
    for t_name, table in (getattr(app, 'tables', None) or {}).items():
        for col in table.effective_columns:
            rank = col.attributes.get('query_rank')
            if rank is not None and rank not in QUERY_RANKS:
                issues.append(make_issue(
                    'error', 'query-rank-unknown', f'tables.{t_name}.{col.name}',
                    f"query_rank '{rank}' is not one of {', '.join(QUERY_RANKS)}"))


def _check_validators(app: Any, issues: List[Dict[str, Any]]) -> None:
    """A `validate:` nobody registered is an error: every write to that column
    would fail, and the column says it is checked when it is not.

    Checked after the plugins are loaded, which is when their validators register.
    """
    from kitebase.transforms import validator_names

    known = validator_names()
    for t_name, table in (getattr(app, 'tables', None) or {}).items():
        for col in table.effective_columns:
            name = col.attributes.get('validate')
            if name and name not in known:
                issues.append(make_issue(
                    'error', 'validator-unknown', f'tables.{t_name}.{col.name}',
                    f"validate '{name}' is not a registered validator "
                    f"(known: {', '.join(sorted(known))})"))


def _check_vocabulary(app: Any, issues: List[Dict[str, Any]]) -> None:
    """Keys the merged YAML uses where the owner of the root has never used
    them: a typo, or a new key that goes into the owner's vocabulary.

    Each root is read with the vocabulary its owner registered (kitebase.sections):
    the core's file for the core's roots, a plugin's own for a plugin's. A root
    registered without one has its keys unchecked; a root nobody owns already
    has its warning.

    With KITEBASE_VOCABULARY_UPDATE=1 the vocabularies take them in instead:
    run it on the applications that show the new key, then read the diff.
    """
    import os
    from kitebase import vocabulary

    by_file: Dict[Any, Dict[str, Any]] = {}
    for root, content in app.plugins.data.items():
        section = get_section(root)
        if section is not None and section.vocabulary is not None:
            by_file.setdefault(section.vocabulary, {})[root] = content

    update = os.environ.get('KITEBASE_VOCABULARY_UPDATE') == '1'
    for path, data in by_file.items():
        where = '/'.join(path.parts[-3:])
        if update:
            added = vocabulary.update(data, path)
            issues.append(make_issue('info', 'vocabulary-updated', str(path),
                                     f'{added} keys added to {where}'))
        else:
            issues.extend(vocabulary.check(data, vocabulary.load(path), where))


def _check_client(app: Any, issues: List[Dict[str, Any]]) -> None:
    """The `client:` section: where the client is mounted and who logs in.

    A role or a path the servers cannot use is an error: the client would be
    mounted where nobody looks. A login page with `role: app` is only a warning
    - the application has no host to send people to, and the key is simply
    unused, which is worth knowing but breaks nothing. So is a host login
    without a logout: leaving would not end the host's session.
    """
    from kitebase.clientui import client_settings

    try:
        settings = client_settings(app.plugins.config)
    except ValueError as e:
        issues.append(make_issue('error', 'client-config', 'config.client', str(e)))
        return
    if settings.role == 'app' and settings.login:
        issues.append(make_issue(
            'warning', 'client-login-without-host', 'config.client.login',
            "client.login names a host login page, but with role 'app' kitebase "
            "is the application and logs people in itself"))
    elif settings.login and not settings.logout:
        issues.append(make_issue(
            'warning', 'client-login-without-logout', 'config.client.logout',
            "client.login sends people to the host's login, but there is no "
            "client.logout: leaving the client keeps the host's session, and "
            "the next visit gets a token again without asking"))


def _check_auto_pages(app: Any, issues: List[Dict[str, Any]]) -> None:
    """A page declaring `$auto` must have a generated page underneath.

    Checked here and not only when the page is opened: the page carries no
    `source` of its own, so nothing else in the walk would notice.
    """
    from kitebase.pages import AUTO, resolve_auto_page

    for page_id, page in (app.plugins.data.get('pages') or {}).items():
        if not isinstance(page, dict) or not page.get(AUTO):
            continue
        if resolve_auto_page(app, page_id) is None:
            issues.append(make_issue(
                'error', 'auto-missing', f'pages.{page_id}',
                f"declares {AUTO}, but no table answers to '{page_id}' "
                f"(expected <table>_list or <table>_form)", page.get('$plugin')))


def _check_commands(app: Any, commands: List[Any], path: str,
                    plugin: Optional[str], issues: List[Dict[str, Any]]) -> None:
    """A command whose endpoint is not registered is not on the page: `get_page`
    drops it. That is the purpose when the endpoint belongs to a plugin the app
    does not load, and the declaring plugin says so under `optional`, with the
    endpoint named after it (`reports.print` and `optional: {reports: [...]}`).
    Otherwise it is most likely a misspelt name, and the button vanishes silently.
    """
    cp = getattr(app, 'dispatcher', None)
    if cp is None:
        return      # nothing loaded to compare with
    for cmd in commands:
        if not isinstance(cmd, dict) or not isinstance(cmd.get('endpoint'), str):
            continue
        name = cmd['endpoint']
        if name in cp.endpoints:
            continue
        owner = cmd.get('$plugin', plugin)
        declarer = app.plugins.plugins.get(owner) if owner else None
        optional = declarer.config.get('optional') if declarer is not None else None
        prefix = name.split('.', 1)[0]
        if '.' in name and isinstance(optional, dict) and prefix in optional \
                and prefix not in app.plugins.plugins:
            continue
        issues.append(make_issue(
            'warning', 'command-endpoint-unknown', f"{path}[{cmd.get('id', '?')}].endpoint",
            f"endpoint '{name}' is not registered, so the command is not shown; "
            f"if it belongs to a plugin this app may not load, declare that plugin "
            f"under `optional`", owner))


def _check_phases(app: Any, content: Dict[str, Any], path: str,
                  plugin: Optional[str], issues: List[Dict[str, Any]]) -> None:
    """The phases of a form group the top nodes of its layout by id: a node
    named and missing, or in two phases, breaks the form; a node in none is
    never drawn, which may be the intent of a derived plugin but rarely is."""
    layout = content.get('layout') if isinstance(content.get('layout'), list) else []
    tops = {n.get('id'): n for n in layout if isinstance(n, dict) and n.get('id')}
    placed: Dict[str, str] = {}
    cp = getattr(app, 'dispatcher', None)
    for phase in content['phases']:
        if not isinstance(phase, dict):
            continue
        pid = phase.get('id', '?')
        where = f'{path}.phases[{pid}]'
        fields: Set[str] = set()
        for node_id in phase.get('nodes') or []:
            if node_id not in tops:
                issues.append(make_issue(
                    'error', 'phase-node-unknown', f'{where}.nodes',
                    f"'{node_id}' is not the id of a node at the top of the layout", plugin))
            elif node_id in placed:
                issues.append(make_issue(
                    'error', 'phase-node-twice', f'{where}.nodes',
                    f"'{node_id}' is already in phase '{placed[node_id]}'", plugin))
            else:
                placed[node_id] = pid
                _layout_fields(tops[node_id], fields)
        for name in phase.get('summary') or []:
            if name not in fields:
                issues.append(make_issue(
                    'warning', 'phase-summary-field', f'{where}.summary',
                    f"'{name}' is not a field of this phase: the summary shows "
                    f"what the phase filled in", plugin))
        nxt = phase.get('next')
        endpoint = nxt.get('endpoint') if isinstance(nxt, dict) else None
        if isinstance(endpoint, str) and cp is not None and endpoint not in cp.endpoints:
            issues.append(make_issue(
                'error', 'phase-endpoint-unknown', f'{where}.next.endpoint',
                f"endpoint '{endpoint}' is not registered: the form could not "
                f"leave this phase", plugin))
    for node_id in tops:
        if node_id not in placed:
            issues.append(make_issue(
                'warning', 'phase-node-unplaced', f'{path}.layout[{node_id}]',
                f"'{node_id}' is in no phase, so it is never drawn: add it to "
                f"the `nodes` of one", plugin))


def _layout_fields(obj: Any, out: Set[str]) -> None:
    """The names of the fields a layout node draws, not those of its collections."""
    if isinstance(obj, list):
        for item in obj:
            _layout_fields(item, out)
    elif isinstance(obj, dict):
        if obj.get('type') == 'collection':
            return
        if isinstance(obj.get('name'), str):
            out.add(obj['name'])
        for key, value in obj.items():
            if isinstance(key, str) and not key.startswith('$'):
                _layout_fields(value, out)


def _check_push_target(app: Any, action: Dict[str, Any], path: str,
                       plugin: Optional[str],
                       issues: List[Dict[str, Any]]) -> None:
    """A stack_push target must be an explicit page or auto-generatable."""
    target = action.get('panel')
    if not isinstance(target, str):
        return
    pages = app.plugins.data.get('pages') or {}
    if target in pages:
        return

    from kitebase.pages import resolve_auto_page
    if resolve_auto_page(app, target) is None:
        issues.append(make_issue(
            'error', 'panel-missing', f'{path}.panel',
            f"stack_push target '{target}' is not a page and cannot be auto-generated",
            plugin))


# ── Secrets ────────────────────────────────────────────────────────────────────
# The dump goes to tools and to whoever develops, never to production readers:
# it still holds no secrets, whatever a plugin writes in its YAML. A value is
# hidden by the name of its key, and the credentials of a URL wherever it is.

MASK = '***'
_SECRET_NAMES = {'token', 'api_key', 'apikey', 'access_token', 'private_key'}
_URL_CREDENTIALS = re.compile(r'([a-z][a-z0-9+.-]*://[^/\s:@]+):[^@\s/]+@', re.IGNORECASE)
# A YAML mapping line: indent (and list dash), key (maybe quoted), value.
_YAML_LINE = re.compile(r'^(\s*(?:-\s+)?)(["\']?)([\w.-]+)\2(\s*:\s+)(.*)$')
_YAML_PLAIN = {'true', 'false', 'yes', 'no', 'null', '~', ''}


def is_secret_key(key: Any) -> bool:
    name = str(key).lower()
    return 'password' in name or 'secret' in name or name in _SECRET_NAMES


def _secret_value(value: Any) -> bool:
    """What a secret key hides: a written value, not a flag or an absence."""
    if isinstance(value, bool) or value is None:
        return False
    return isinstance(value, (int, float)) or (isinstance(value, str) and value != '')


def mask_secrets(obj: Any) -> Any:
    """A copy of `obj` with secret values and URL credentials replaced by MASK."""
    if isinstance(obj, dict):
        return {k: MASK if is_secret_key(k) and _secret_value(v) else mask_secrets(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_secrets(v) for v in obj]
    if isinstance(obj, str):
        return _URL_CREDENTIALS.sub(rf'\1:{MASK}@', obj)
    return obj


def mask_yaml_text(text: str) -> str:
    """The raw YAML with the same masking, comments and layout untouched.
    A block scalar under a secret key (`key: |`) loses its whole body; an
    inline mapping or list is a structure, as in the parsed content."""
    out: List[str] = []
    block_indent: Optional[int] = None
    for line in text.splitlines(keepends=True):
        if block_indent is not None:
            if not line.strip() or len(line) - len(line.lstrip()) > block_indent:
                if line.strip():
                    out.append(line[:len(line) - len(line.lstrip())] + MASK + '\n')
                else:
                    out.append(line)
                continue
            block_indent = None
        m = _YAML_LINE.match(line.rstrip('\r\n'))
        if m and is_secret_key(m.group(3)):
            value = m.group(5)
            bare = re.sub(r'\s+#.*$', '', value).strip()
            if bare[:1] in ('|', '>'):
                block_indent = len(m.group(1))
            elif bare[:1] not in ('{', '[') and bare.lower() not in _YAML_PLAIN:
                eol = line[len(line.rstrip('\r\n')):]
                line = m.group(1) + m.group(2) + m.group(3) + m.group(2) + m.group(4) + MASK + eol
        out.append(_URL_CREDENTIALS.sub(rf'\1:{MASK}@', line))
    return ''.join(out)


# ── Full dump ──────────────────────────────────────────────────────────────────

def dump_app(app: Any) -> Dict[str, Any]:
    """
    Build the complete effective-state snapshot of a loaded app.

    Everything is JSON-serializable (write with json.dumps(..., default=str)
    to be safe with dates or other stray objects).
    """
    from kitebase import introspect
    from kitebase.cli import _table_dict

    pm = app.plugins

    # Every owned root, dumped uniformly with $ref resolved and $plugin
    # attribution kept (pages, views, menus, menu_items, the plugins' roots).
    # DB/type roots have dedicated dumpers below; unowned roots are left out,
    # the section-unowned warning among the issues says they are there.
    sections: Dict[str, Dict[str, Any]] = {}
    for name in _owned_sections(pm):
        if name in _DB_SECTIONS:
            continue
        out: Dict[str, Any] = {}
        for item_id, item in pm.data[name].items():
            if item_id.startswith('$'):
                continue
            try:
                out[item_id] = pm.resolve_refs(item)
            except ValueError as e:  # circular $ref
                out[item_id] = {'$error': str(e)}
        sections[name] = out

    # Keys contributed by more than one plugin — the interesting slice of history
    contested = {k: sorted(set(v)) for k, v in pm.history.items()
                 if len(set(v)) > 1}

    # Raw pre-merge YAML per plugin/file. Lossless where the merged snapshot is
    # not: comments, ordering, $after/$remove directives before they are consumed,
    # types before mixin flattening. Provenance is free — each file is attributed
    # by its plugin + path. The inspector searches/diffs these instead of walking
    # the filesystem, and never has to re-run the merge (it stays a viewer).
    # `content` = parsed (in-memory, structured search); `text` = raw (comments,
    # textual search), best-effort.
    source_files: Dict[str, Any] = {}
    for name in pm.sorted:
        p = pm.plugins[name]
        files: Dict[str, Any] = {}
        entries = [(p.plugin_dir / 'config.yaml', p.config)]
        entries += list(zip(p.data_files, p.data))
        for path, parsed in entries:
            try:
                text = path.read_text(encoding='utf-8')
            except OSError:
                text = None
            files[path.name] = {'content': parsed,
                                'text': mask_yaml_text(text) if text is not None else None}
        source_files[name] = {'dir': str(p.plugin_dir), 'files': files}

    # Masked as a whole: a secret written in a data root reaches the merged
    # sections too, not only source_files.
    return mask_secrets({
        # Contract version for external consumers (inspector, tooling).
        # Bump on any structural change to the sections below.
        # v2: descriptor sections are generic (pages/views + menus/menu_items/…)
        #     and + source_files (raw pre-merge YAML per plugin/file).
        'dump_version': 2,
        'generated': datetime.now().isoformat(timespec='seconds'),
        'app': {k: pm.config.get(k) for k in ('name', 'version', 'description')},
        'plugins': [
            {
                'name': name,
                'version': pm.plugins[name].config.get('version'),
                'depends_on': pm.plugins[name].config.get('depends_on'),
                'dir': str(pm.plugins[name].plugin_dir),
                'data_files': [f.name for f in pm.plugins[name].data_files],
                'sources': [f.name for f in pm.plugins[name].sources],
            }
            for name in pm.sorted
        ],
        'issues': run_checks(app),
        'tables': {name: _table_dict(t) for name, t in app.tables.items()},
        'types': app.get_type_schema(include_builtin=False),
        **sections,
        'endpoints': introspect.endpoints(),
        'catalog': introspect.catalog(app),
        'contested_keys': contested,
        'merge_history': dict(sorted(pm.history.items())),
        'source_files': source_files,
    })
