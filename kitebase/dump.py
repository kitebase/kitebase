"""
kitebase.dump: the complete effective state of a loaded application.

  dump_app(app)    -> Dict
      JSON-serializable snapshot: plugins, issues, tables, types, every owned
      root of the merged tree (pages, views, menus, ... with $ref resolved),
      endpoints, merge history, and source_files (raw pre-merge YAML per
      plugin/file). Secret values and URL credentials are masked
      (diagnostics.mask_secrets). $plugin attribution keys are kept so an
      external viewer (web app, tkinter, jq, ...) can show which plugin
      contributed each piece. DB/type roots (tables/types/schemas) have
      dumpers of their own; the other owned roots are treated uniformly
      (owners: kitebase.sections).

  table_dict(table) -> Dict
      A table as the developer reads it, also for `kitebase dump-table`.
"""
from datetime import datetime
from typing import Any, Dict

from kitebase import introspect
from kitebase.diagnostics import mask_secrets, mask_yaml_text, owned_sections, run_checks

# Roots with dumpers of their own (tables via table_dict, types via the schema
# registry): every other owned root is dumped as it was merged.
_DB_SECTIONS = frozenset({'tables', 'types', 'schemas'})


# Column attributes exposed in the dump (superset of what get_table_schema sends
# to the client — here we want the full developer view).
_COL_ATTRS = (
    'type', 'label', 'help', 'doc',
    'primary_key', 'autoincrement',
    'nullable', 'unique', 'index', 'default',
    'virtual', 'secret', 'editable',
    'widget',
)


def column_dict(col: Any) -> Dict[str, Any]:
    """Serialize a DbColumn to a dict for developer inspection."""
    d: Dict[str, Any] = {'name': col.name}

    for attr in _COL_ATTRS:
        if attr in col.attributes:
            d[attr] = col.attributes[attr]
        elif attr == 'type' and col.db_type:
            d['type'] = col.db_type.name

    # Foreign key — show as "Table.field" string instead of resolved objects
    fk = col.attributes.get('foreign_key')
    if fk and isinstance(fk, dict) and 'table' in fk:
        d['foreign_key'] = f"{fk['table'].name}.{fk['id']}"

    return d


def table_dict(table: Any) -> Dict[str, Any]:
    """Serialize a DbTable to a dict for developer inspection."""
    # PK fields (same logic as get_table_schema)
    m2m = table.attributes.get('many_to_many')
    pk_fields = [
        col.name for col in table.effective_columns
        if col.attributes.get('primary_key')
    ]

    d: Dict[str, Any] = {'pk_fields': pk_fields}

    # Table-level metadata
    for attr in ('label', 'help', 'doc', 'tags', 'mixins'):
        val = table.attributes.get(attr)
        if val:
            d[attr] = val

    # M2M targets summary
    if m2m:
        d['many_to_many'] = {
            'target1': f"{m2m['target1']['table'].name}.{m2m['target1']['id']} → {m2m['target1']['column']}",
            'target2': f"{m2m['target2']['table'].name}.{m2m['target2']['id']} → {m2m['target2']['column']}",
        }

    d['columns'] = [column_dict(col) for col in table.effective_columns]

    # Plugins that contributed to this table
    d['defined_in'] = [p.name for p in table.plugins]

    return d


def dump_app(app: Any) -> Dict[str, Any]:
    """
    Build the complete effective-state snapshot of a loaded app.

    Everything is JSON-serializable (write with json.dumps(..., default=str)
    to be safe with dates or other stray objects).
    """
    pm = app.plugins

    # Every owned root, dumped uniformly with $ref resolved and $plugin
    # attribution kept (pages, views, menus, menu_items, the plugins' roots).
    # DB/type roots have dedicated dumpers below; unowned roots are left out,
    # the section-unowned warning among the issues says they are there.
    sections: Dict[str, Dict[str, Any]] = {}
    for name in owned_sections(pm):
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
        'tables': {name: table_dict(t) for name, t in app.tables.items()},
        'types': app.get_type_schema(include_builtin=False),
        **sections,
        'endpoints': introspect.endpoints(),
        'catalog': introspect.catalog(app),
        'contested_keys': contested,
        'merge_history': dict(sorted(pm.history.items())),
        'source_files': source_files,
    })
