"""
kitebase.cli — introspection and management utilities.

Two layers:

  dump_*()          Pure functions: receive an app, return (yaml_text, label).
                    No file I/O, no argparse — fully testable in isolation.

  make_parser()     Build and return the ArgumentParser for the kitebase CLI.
                    An application adds its own commands through
                    `parser.commands` and handles them before run_cli().

  run_cli()         Dispatch parsed args to the right dump_* function and
                    write output to file or stdout.  Accepts an output_dir
                    so callers can point it at their project's data folder.

Commands needing a live database (db-check, db-sync, db-backup, db-copy) are listed in DB_COMMANDS:
the caller builds the app with an engine for those, and with the schema alone
for the rest.

Planned sections:
  dump_*     — read-only introspection (pages, tables, types, plugins, endpoints)
  db_*       — schema alignment (kitebase.schema_sync)
  [future]   — dump-endpoints: catalog of registered endpoints with metadata
  [future]   — backup / restore
  [future]   — db engine migration

Future standalone entry-point (pyproject.toml scripts):
  kitebase --config config.yaml dump-page author_list
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import yaml

from kitebase.dump import dump_app, table_dict


# ── Helpers ────────────────────────────────────────────────────────────────────

def _to_yaml(data: Any) -> str:
    """Serialize a dict to a human-readable YAML string."""
    return yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        indent=2,
    )


# ── dump_page ──────────────────────────────────────────────────────────────────

def dump_page(app: Any, page_id: str, auto: bool = False, raw: bool = False) -> Tuple[str, str]:
    """
    Generate a YAML snippet for a page descriptor.

    Resolution order (unless --auto):
      1. Explicit pages dict (YAML plugin pages)
      2. Auto-generated fallback from table schema

    Args:
        app:     Initialized kitebase app (setup_schema() is sufficient — no DB engine needed)
        page_id: Page identifier (e.g. 'author_list', 'book_with_reviews')
        auto:    Force auto-generation from table schema, ignoring any explicit YAML
        raw:     Skip $ref resolution — output the raw descriptor as declared in YAML

    Returns:
        (yaml_text, source_label)
        yaml_text    — YAML string wrapped in {page_id: descriptor}, ready to paste
        source_label — human-readable description of the resolution path taken

    Raises:
        ValueError: if the page cannot be found or auto-generated
    """
    from kitebase.pages import load_page, resolve_auto_page, strip_meta

    descriptor = None
    source_label = ''

    if auto:
        descriptor = resolve_auto_page(app, page_id)
        if descriptor is None:
            raise ValueError(f"Cannot auto-generate '{page_id}' — no matching table found")
        source_label = 'auto-generated'
    else:
        raw_panel = app.plugins.get(f'pages.{page_id}')
        if raw_panel is not None:
            if raw:
                descriptor = strip_meta(raw_panel)
                source_label = 'explicit, $ref not expanded'
            else:
                # The full resolution the endpoint performs — refs expanded and
                # collection nodes completed — so the dump shows what runs.
                descriptor = strip_meta(load_page(app, page_id))
                source_label = 'explicit, $ref expanded'
        else:
            descriptor = resolve_auto_page(app, page_id)
            if descriptor is None:
                raise ValueError(f"Page '{page_id}' not found and cannot be auto-generated")
            source_label = 'auto-generated — no explicit YAML found'

    # Remove internal marker before serializing
    descriptor.pop('_auto', None)

    yaml_text = _to_yaml({page_id: descriptor})
    return yaml_text, source_label


# ── dump_table ─────────────────────────────────────────────────────────────────

def dump_table(app: Any, table_names: Optional[List[str]] = None) -> Tuple[str, str]:
    """
    Generate a YAML snapshot of one or more tables after all plugin merges.

    Shows the full effective schema: real columns + mixin columns + virtual
    columns, with all resolved attributes (nullable, unique, FK target, …).

    Args:
        app:         Initialized kitebase app (setup_schema() is sufficient)
        table_names: List of table names to dump; None = all tables

    Returns:
        (yaml_text, label)

    Raises:
        ValueError: if a requested table name is not found
    """
    if table_names:
        unknown = [n for n in table_names if n not in app.tables]
        if unknown:
            raise ValueError(f"Unknown table(s): {', '.join(unknown)}")
        tables = {n: app.tables[n] for n in table_names}
        label = ', '.join(table_names)
    else:
        tables = dict(app.tables)
        label = 'all tables'

    output = {name: table_dict(table) for name, table in tables.items()}
    return _to_yaml(output), label


# ── dump_types ─────────────────────────────────────────────────────────────────

# Attributes skipped in the tree nodes (structural, not useful as reference)
_TYPE_SKIP = {'base', 'columns', 'autoincrement'}


def _type_delta(type_attrs: Dict, parent_attrs: Optional[Dict]) -> Dict:
    """
    Return only attributes that this type adds or overrides vs its parent.
    Skips structural keys and anything identical to the parent.
    """
    result = {}
    for key, val in type_attrs.items():
        if key in _TYPE_SKIP:
            continue
        if parent_attrs is None or key not in parent_attrs or parent_attrs[key] != val:
            result[key] = val
    return result


def _build_scalar_tree(
    types: Dict[str, Any],
    parent_name: Optional[str],
    include_builtin: bool,
) -> Dict:
    """
    Recursively build the scalar type tree rooted at parent_name.
    Each node contains its delta attributes + child type names as nested keys.
    """
    node: Dict = {}
    parent_obj = types.get(parent_name) if parent_name else None
    parent_attrs = parent_obj.attributes if parent_obj else None

    # Find direct non-builtin children of parent_name
    children = [
        (name, t) for name, t in types.items()
        if not t.columns                              # scalar only
        and not (t.plugin == "")                     # non-builtin only
        and (t.attributes.get('base') == parent_name  # direct child
             or (parent_name is None and 'base' not in t.attributes))
    ]

    for name, t in sorted(children, key=lambda x: x[0]):
        delta = _type_delta(t.attributes, parent_attrs)
        subtree = _build_scalar_tree(types, name, include_builtin)
        entry: Dict = {**delta, **subtree}
        node[name] = entry

    return node


def dump_types(app: Any, include_builtin: bool = False) -> Tuple[str, str]:
    """
    Generate a YAML snapshot of the type registry as two sections:

      types:           — scalar types, shown as an inheritance tree.
                         Each node contains only the attributes it ADDS or
                         OVERRIDES vs its parent; child type names appear as
                         nested keys (PascalCase vs snake_case, no collision).

      compound_types:  — types with sub-columns (Address, TimeStamp, Archivable, …).
                         Shown flat with their full column list.

    Args:
        app:             Initialized kitebase app
        include_builtin: If True, also include builtin SQLAlchemy types as
                         explicit root nodes even when they have no custom children.

    Returns:
        (yaml_text, label)
    """
    all_types = app.types  # Dict[str, DbType]

    # ── Compound types (have sub-columns) ─────────────────────────────────────
    compound: Dict = {}
    for name, t in sorted(all_types.items()):
        if not t.columns:
            continue
        if t.plugin == "" and not include_builtin:
            continue
        entry: Dict = {}
        label = t.attributes.get('label')
        if label:
            entry['label'] = label
        help_text = t.attributes.get('help')
        if help_text:
            entry['help'] = help_text.strip()
        _col_skip = {'$plugin', 'plugin', 'base'}
        entry['columns'] = [
            {k: v for k, v in col.attributes.items()
             if k not in _col_skip and v is not None}
            for col in t.columns
        ]
        compound[name] = entry

    # ── Scalar type tree ───────────────────────────────────────────────────────
    # Find builtin types that have at least one non-builtin child (tree roots).
    # Non-builtin types with no base at all also become roots (top-level).
    scalar_types = {n: t for n, t in all_types.items() if not t.columns}

    # Collect all parent names that non-builtin scalars point to
    builtin_parents_used: set = set()
    for t in scalar_types.values():
        if t.plugin == "":
            continue
        parent = t.attributes.get('base')
        if parent and scalar_types.get(parent) and scalar_types[parent].plugin == "":
            builtin_parents_used.add(parent)

    # Build tree: top-level keys are builtin parents (if they have children)
    # plus non-builtin orphan roots (no base).
    tree: Dict = {}

    for builtin_parent in sorted(builtin_parents_used):
        subtree = _build_scalar_tree(scalar_types, builtin_parent, include_builtin)
        if subtree:
            tree[builtin_parent] = subtree

    # Non-builtin types with no parent
    orphan_subtree = _build_scalar_tree(scalar_types, None, include_builtin)
    tree.update(orphan_subtree)

    output: Dict = {}
    if tree:
        output['types'] = tree
    if compound:
        output['compound_types'] = compound

    return _to_yaml(output), 'type registry'


# ── db-check / db-sync ─────────────────────────────────────────────────────────

# Commands that need app.initialize_db() to have run — the others work on the
# merged schema alone.
DB_COMMANDS = {'db-check', 'db-sync', 'db-backup', 'db-copy'}


def db_check(app: Any) -> Tuple[str, bool]:
    """
    Compare the database with the schema the plugins describe.

    Returns:
        (report, aligned) — read-only, nothing is written.
    """
    from kitebase.model_base import Base
    from kitebase.schema_sync import diff_schema, format_diff

    diff = diff_schema(app.engine, Base.metadata)
    return format_diff(diff), diff.is_aligned


def _confirm_forced(changes: List[Any]) -> bool:
    """List what --force is about to lose and ask. Anything but yes is no."""
    print('--force will also apply, losing data:')
    for change in changes:
        print(f'  ! {change.target:<40} {change.description}')
        print(f'      {change.reason}')
    try:
        answer = input('Proceed? [y/N] ')
    except EOFError:
        return False
    return answer.strip().lower() in ('y', 'yes')


def db_sync(app: Any, dry_run: bool = False, force: bool = False,
            confirm: Optional[Any] = None) -> Tuple[str, bool]:
    """
    Apply the changes that lose no data: new tables and columns, new indexes,
    widened types, relaxed NOT NULLs, strings shortened where every value fits.
    With `force`, also the forceable refusals (drops, truncating strings),
    after `confirm(changes)` returns True when given. The rest is reported and
    left alone — see kitebase.schema_sync for why.

    Returns:
        (report, aligned_after) — with dry_run, the DDL that would run.
    """
    from kitebase.model_base import Base
    from kitebase.schema_sync import apply_diff, diff_schema, format_diff, plan_sql

    diff = diff_schema(app.engine, Base.metadata)

    if diff.is_aligned:
        return 'Database aligned with the schema.', True

    lines = [format_diff(diff, sync_command=None)]

    if dry_run:
        sql = plan_sql(app.engine, diff, force=force)
        if sql:
            lines += ['', '--- SQL that would run ---', sql.rstrip()]
        return '\n'.join(lines), False

    forced = diff.forceable if force else []
    if forced and confirm is not None and not confirm(forced):
        lines += ['', 'Forced changes not confirmed: nothing applied.']
        return '\n'.join(lines), False

    count = len(diff.safe) + len(forced)
    if count:
        executed = apply_diff(app.engine, diff, logger=app.plugins.logger, force=bool(forced))
        lines += ['', f'Applied {count} change(s):']
        lines += [f'  {statement}' for statement in executed]

    return '\n'.join(lines), len(diff.refused) == len(forced)


def db_backup(app: Any, dest: Optional[str] = None, media: bool = False) -> Tuple[str, str]:
    """
    A consistent copy of a SQLite database, taken while the server runs.

    `cp` on a live file is not that: in WAL mode the last writes sit in the
    `-wal` file until a checkpoint, and a copy taken mid-write can be torn.
    SQLite's online backup API reads a snapshot through the engine itself, so
    the result opens cleanly and holds everything committed at that moment —
    no need to stop the service. Other engines are refused by name: the day an
    application runs on one, this is where `pg_dump`/`mysqldump` plug in,
    under the same command.

    With `media` the backup is the unit a database makes with its files: a
    folder `<name>-<stamp>/` holding the snapshot and the relative media roots
    (kitebase.db_copy.media_roots), each file hard-linked to the previous such
    folder when unchanged, so a series costs what changed.

    Returns:
        (report, path) — the file, or with `media` the folder, written.
    """
    import sqlite3
    from datetime import datetime

    url = app.engine.url
    if url.get_backend_name() != 'sqlite' or not url.database:
        raise SystemExit(f'db-backup copies SQLite files; this application uses '
                         f'{url.get_backend_name()} — use that engine\'s own tool.')

    source = Path(url.database)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    if media:
        return _backup_unit(app, source, Path(dest) if dest else source.parent, stamp)

    if dest is None:
        target = source.with_name(f'{source.stem}-{stamp}{source.suffix}')
    else:
        target = Path(dest)
        if target.is_dir():
            target = target / source.name
    if target.resolve() == source.resolve():
        raise SystemExit('db-backup: the destination is the database itself.')

    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as out:
        src.backup(out)
    size = target.stat().st_size
    return f'Backup written: {target} ({size:,} bytes)', str(target)


def _backup_unit(app: Any, source: Path, parent: Path, stamp: str) -> Tuple[str, str]:
    """The snapshot and the media in one folder, linked to the previous one."""
    import sqlite3
    from kitebase.db_copy import media_roots, snapshot_files

    if not parent.is_dir():
        raise SystemExit(f'db-backup --media: {parent} is not a directory.')
    folder = parent / f'{source.stem}-{stamp}'
    earlier = sorted(p for p in parent.glob(f'{source.stem}-*')
                     if p.is_dir() and p != folder)
    previous = earlier[-1] if earlier else None
    folder.mkdir()

    db_file = folder / source.name
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(db_file)) as out:
        src.backup(out)

    roots, shared = media_roots(app.plugins.config.get('media') or {})
    base = app.plugins.media_base()
    copied = linked = 0
    for _, path in roots:
        c, l = snapshot_files(base / path, folder / path,
                              previous / path if previous else None)
        copied, linked = copied + c, linked + l

    lines = [f'Backup written: {folder}',
             f'  database  {db_file.name} ({db_file.stat().st_size:,} bytes)',
             f'  media     {copied} file(s) copied, {linked} linked to '
             f'{previous.name if previous else "nothing (first backup)"}']
    if shared:
        lines.append(f"  not here  {', '.join(shared)}: absolute roots, back them up "
                     'where they live')
    return '\n'.join(lines), str(folder)


# ── CLI parser ─────────────────────────────────────────────────────────────────

def db_copy(app: Any, to: str, force: bool = False, dry_run: bool = False,
            media: bool = False, confirm: Optional[Any] = None,
            metadata: Optional[Any] = None) -> Tuple[str, bool]:
    """
    Copy every row of the application's database (the source: its engine,
    chosen with --from) into the engine `to`, a name of secrets/engines.yaml or
    a URL. With `media`, the files of the relative media roots go too, from the
    source's media base to the target's (PluginsManager.media_base). See
    kitebase.db_copy for the preconditions and the checks.

    Returns:
        (report, ok)
    """
    from sqlalchemy import create_engine
    from kitebase.model_base import Base
    from kitebase.db_copy import CopyError, MediaCopy, copy_database, media_roots

    metadata = Base.metadata if metadata is None else metadata
    target = create_engine(app._resolve_db_url(to))
    shown = target.url.render_as_string(hide_password=True)
    source = app.engine.url.render_as_string(hide_password=True)
    head = f'{source}  ->  {shown}'
    roots, shared = media_roots(app.plugins.config.get('media') or {})

    try:
        files = None
        if media and roots:
            if '://' in to:
                raise CopyError('--media needs the target engine by name: the name '
                                'places its media folder')
            files = MediaCopy(app.plugins.media_base(), app.plugins.media_base(to), roots)
            occupied = files.occupied()
            if occupied and not force:
                raise CopyError(
                    f"The target's media are not empty ({', '.join(occupied)} in "
                    f"{files.target}): pass --force to replace them")

        if force and not dry_run and confirm is not None and not confirm(
                shown + (f' and the media in {files.target}' if files else '')):
            return 'Not confirmed: nothing copied.', False

        if dry_run or files is None:
            report = copy_database(app.engine, target, metadata,
                                   truncate=force, dry_run=dry_run)
            count = files.count() if files else 0
        else:
            count = files.copy_to_stage()
            try:
                report = copy_database(app.engine, target, metadata, truncate=force)
            except BaseException:
                files.abort()
                raise
            files.commit()
    except CopyError as e:
        return f'{head}\n\n{e}', False
    finally:
        target.dispose()

    lines = [report.format(source, shown)]
    if files is not None:
        verb = 'would be copied' if dry_run else 'copied'
        lines.append(f'Media: {count} file(s) {verb}, {app.plugins.media_base()} -> {files.target}.')
    elif roots:
        lines.append('Media not copied (--media copies them): the files these rows '
                     'refer to are where the target engine finds them, or missing.')
    if shared:
        lines.append(f"Shared by every engine, not copied: {', '.join(shared)} "
                     '(absolute paths).')
    return '\n'.join(lines), True


def _confirm_truncate(target: str) -> bool:
    """--force empties the target first. Anything but yes is no."""
    try:
        answer = input(f'--force deletes every row of {target} first. Proceed? [y/N] ')
    except EOFError:
        return False
    return answer.strip().lower() in ('y', 'yes')


class _ChooseEngine(argparse.Action):
    """`--engine NAME` is KITEBASE_ENGINE=NAME for this process.

    Set while parsing, so it is in place when the application loads its
    config.yaml right after, and any process it starts inherits it.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        import os
        os.environ['KITEBASE_ENGINE'] = values
        setattr(namespace, self.dest, values)


class _ChooseConfig(argparse.Action):
    """`--config FILE` is KITEBASE_CONFIG=FILE for this process: a variant of
    config.yaml beside it, which the application loads instead (and any
    process it starts, the server and the client of `dev`)."""

    def __call__(self, parser, namespace, values, option_string=None):
        import os
        os.environ['KITEBASE_CONFIG'] = values
        setattr(namespace, self.dest, values)


def _config_option(p: argparse.ArgumentParser) -> None:
    # SUPPRESS: given before the command or after it, neither erases the other
    p.add_argument('--config', metavar='FILE', action=_ChooseConfig, default=argparse.SUPPRESS,
                   help='Load FILE, a variant beside config.yaml (`extends: config.yaml`), '
                        'instead of config.yaml')


def _take_config(argv: List[str]) -> List[str]:
    """A leading `--config FILE`, applied and taken out: what follows may be a
    command the application adds, which this parser does not know."""
    import os
    if argv[:1] == ['--config'] and len(argv) > 1:
        os.environ['KITEBASE_CONFIG'] = argv[1]
        return argv[2:]
    if argv and argv[0].startswith('--config='):
        os.environ['KITEBASE_CONFIG'] = argv[0].split('=', 1)[1]
        return argv[1:]
    return argv


def _engine_option(p: argparse.ArgumentParser) -> None:
    p.add_argument('--engine', metavar='NAME', action=_ChooseEngine,
                   help='Use the engine NAME of secrets/engines.yaml instead of '
                        "config.yaml's db_engine")


def make_parser() -> argparse.ArgumentParser:
    """
    Build and return the kitebase CLI argument parser.

    Add new subparsers here as new commands are implemented.
    The parser is intentionally separate from run_cli() so callers
    can inspect or extend it before parsing.

    `parser.commands` is the subparsers action: an application adds its own
    commands with `parser.commands.add_parser(...)` and dispatches them itself,
    before handing the rest to run_cli().
    """
    parser = argparse.ArgumentParser(
        prog='kitebase',
        description='Kitebase CLI — introspection and management',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  dump-page author_list                 auto-gen list (no explicit YAML)
  dump-page book_with_reviews           explicit page, $ref expanded
  dump-page book_with_reviews --raw     explicit page, $ref kept
  dump-page book_list --auto            force auto-gen (ignore explicit YAML)
  dump-table Author                     Author schema after plugin merge
  dump-table                            all tables
  dump-types                            type inheritance tree + compound types
  dump-types --include-builtin          also show SQLAlchemy built-in roots
  check                                 validate merged descriptors (refs, models, fields)
  check --dump                          also write full JSON dump to <output_dir>/appdump.json
  db-check                              compare the database with the schema (exit 1 if it differs)
  db-sync --dry-run                     show the DDL an alignment would run
  db-sync                               apply what loses no data
  db-sync --force                       also drop and truncate, after a confirmation
  db-sync --engine pg                   the same, on the engine 'pg' of secrets/engines.yaml
  db-backup                             consistent SQLite snapshot next to the database, stamped
  db-backup /mnt/backup/                a directory, or a file path
  db-backup --media /mnt/backup/        the database and its media, unchanged files linked
  db-copy --to pg                       every row into the engine 'pg', empty and aligned
  db-copy --from pg --to test --dry-run the order and the counts, nothing written
  dev --engine pg                       server and client, the server on 'pg'
  dev --config config.pg.yaml           the same app, as the variant config.pg.yaml says
  dev                                   run this app and its client, together
  dev /path/to/app --no-client          just the server, on another app
  build-client                          compile this app's client into clientui/
  inspector                             the developer's view of this app: http://localhost:8400
  inspector build                       compile the inspector page (done by itself when needed)
        """,
    )

    _config_option(parser)
    sub = parser.add_subparsers(dest='command', metavar='command')
    parser.commands = sub

    # ── dump-page ──────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'dump-page',
        help='Generate YAML for a page descriptor (auto-gen or explicit)',
    )
    p.add_argument('page_id', help='Page id (e.g. author_list, book_with_reviews)')
    p.add_argument('--auto', action='store_true',
                   help='Force auto-generation from table schema (ignore explicit YAML)')
    p.add_argument('--raw', action='store_true',
                   help='No $ref expansion — output raw descriptor as declared in YAML')
    p.add_argument('-o', '--output', metavar='PATH',
                   help='Output file path (- = stdout; default: <output_dir>/pages/<id>.yaml)')

    # ── help ───────────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'help',
        help='Write the guide of a page as a standalone HTML file',
    )
    p.add_argument('page_id', help='The page of a menu entry (e.g. book_list), or @manual for all of them')
    p.add_argument('--lang', default='en', metavar='LANG',
                   help='Language of the guide (default: en); what is not translated stays English')
    p.add_argument('-o', '--output', metavar='PATH',
                   help='Output file path (- = stdout; default: <output_dir>/help/<id>.html)')

    # ── sync-help ──────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'sync-help',
        help='Bring the translations of the guide (help/<lang>.yaml) in step with the English',
    )
    p.add_argument('--lang', metavar='LANG',
                   help='Start this language too, in every plugin that has texts')
    p.add_argument('--plugin', action='append', metavar='NAME',
                   help='Only this plugin (repeatable)')

    # ── dump-table ─────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'dump-table',
        help='Dump table schema after plugin merge (effective columns, PK, mixins, …)',
    )
    p.add_argument('table', nargs='*', metavar='TABLE',
                   help='Table name(s) to dump (omit for all tables)')
    p.add_argument('-o', '--output', metavar='PATH',
                   help='Output file path (- = stdout; default: <output_dir>/tables/<name>.yaml)')

    # ── dump-types ─────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'dump-types',
        help='Dump type registry as inheritance tree + compound types',
    )
    p.add_argument('--include-builtin', action='store_true',
                   help='Include SQLAlchemy built-in types as explicit root nodes')
    p.add_argument('-o', '--output', metavar='PATH',
                   help='Output file path (- = stdout; default: <output_dir>/types.yaml)')

    # ── check ──────────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'check',
        help='Validate merged descriptors; report unresolved refs, unknown models/fields, orphans',
    )
    p.add_argument('--dump', nargs='?', const='', metavar='PATH',
                   help='Also write the full effective-state JSON dump '
                        '(default path: <output_dir>/appdump.json)')

    # ── db-check ───────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'db-check',
        help='Compare the database with the schema described by the plugins (read-only)',
    )
    _engine_option(p)

    # ── db-sync ────────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'db-sync',
        help='Align the database with what loses no data; --force also drops and truncates',
    )
    p.add_argument('--dry-run', action='store_true',
                   help='Print the DDL that would run, without touching the database')
    p.add_argument('--force', action='store_true',
                   help='Also apply the changes marked --force: drops, truncated strings')
    p.add_argument('--yes', action='store_true',
                   help='With --force, do not ask for confirmation')
    _engine_option(p)

    # ── db-backup ──────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'db-backup',
        help='Consistent snapshot of a SQLite database, safe while the server runs',
    )
    p.add_argument('dest', nargs='?',
                   help='Target file or directory (default: next to the database, timestamped)')
    p.add_argument('--media', action='store_true',
                   help='A folder with the database and its media, files unchanged since '
                        'the previous one hard-linked')
    _engine_option(p)

    # ── db-copy ────────────────────────────────────────────────────────────────
    p = sub.add_parser(
        'db-copy',
        help='Copy every row into another engine, aligned with the schema and empty',
    )
    p.add_argument('--from', dest='engine', metavar='NAME', action=_ChooseEngine,
                   help="Source engine (default: config.yaml's db_engine); same as --engine")
    p.add_argument('--to', required=True, metavar='NAME',
                   help='Target engine: a name of secrets/engines.yaml, or a URL')
    p.add_argument('--force', action='store_true',
                   help='Copy into a target that has rows, emptying it first, after a confirmation')
    p.add_argument('--yes', action='store_true',
                   help='With --force, do not ask for confirmation')
    p.add_argument('--dry-run', action='store_true',
                   help='Show the order, the deferred keys and the counts; write nothing')
    p.add_argument('--media', action='store_true',
                   help="Copy the files of the media roots too, into the target engine's folder")

    # ── new ────────────────────────────────────────────────────────────────────
    # The one command that runs without an application, hence from the `kitebase`
    # console script rather than from an application's entry point.
    p = sub.add_parser(
        'new',
        help='Write a new application that runs (use the `kitebase` command)',
    )
    p.add_argument('name', help='Application name — also the name of its plugin')
    p.add_argument('--directory', metavar='PATH',
                   help='Where to write it (default: ./<name>)')
    p.add_argument('--force', action='store_true',
                   help='Write into a directory that already holds files')
    p.add_argument('--server', choices=('flask', 'fastapi', 'both'), default='both',
                   help='Which server to write (default: both — they answer the same)')

    # ── dev ────────────────────────────────────────────────────────────────────
    # Also without an application loaded: it starts processes, it does not
    # compose the app — the server it spawns does that for itself.
    p = sub.add_parser(
        'dev',
        help='Start the development processes: the app server and the client',
    )
    p.add_argument('app', nargs='?', metavar='APP',
                   help='Application directory (default: the current one)')
    p.add_argument('--flask', dest='framework', action='store_const', const='flask',
                   help='Run the Flask entry point')
    p.add_argument('--fastapi', dest='framework', action='store_const', const='fastapi',
                   help='Run the FastAPI entry point')
    p.add_argument('--src', metavar='PATH',
                   help='Library checkout to run against (default: $KITEBASE_SRC, '
                        'or the one this command was imported from)')
    p.add_argument('--ui', metavar='PATH',
                   help='Client checkout (default: $KITEBASE_UI, or the workspace layout)')
    p.add_argument('--no-server', action='store_true', help='Client only')
    p.add_argument('--no-client', action='store_true', help='Server only')
    _engine_option(p)

    # ── inspector ──────────────────────────────────────────────────────────────
    # Serving needs the application, and is handed to its script like `check`;
    # `build` and `dev` only start processes.
    p = sub.add_parser(
        'inspector',
        help="Serve the developer's view of the application on localhost",
    )
    p.add_argument('action', nargs='?', choices=['build', 'dev'],
                   help='build: compile the page; dev: develop the inspector itself')
    p.add_argument('--port', type=int, default=8400, help='Port on 127.0.0.1 (default: 8400)')
    p.add_argument('--no-build', action='store_true',
                   help='Do not compile the page, even when missing or old')

    # ── build-client ───────────────────────────────────────────────────────────
    p = sub.add_parser(
        'build-client',
        help="Compile the admin client into the application's clientui/",
    )
    p.add_argument('app', nargs='?', metavar='APP',
                   help='Application directory (default: the current one)')
    p.add_argument('--ui', metavar='PATH',
                   help='Client checkout (default: $KITEBASE_UI, or the workspace layout)')

    # Every command that loads the application takes a variant of its config
    for name, command in sub.choices.items():
        if name != 'new':
            _config_option(command)

    return parser


# ── CLI dispatcher ─────────────────────────────────────────────────────────────

def print_issues(issues: List[Dict[str, Any]]) -> int:
    """
    Print issues grouped by severity (errors first). Returns the error count.
    """
    by_sev: Dict[str, List[Dict[str, Any]]] = {'error': [], 'warning': [], 'info': []}
    for issue in issues:
        by_sev.setdefault(issue['severity'], []).append(issue)

    for sev in ('error', 'warning', 'info'):
        group = by_sev[sev]
        if not group:
            continue
        print(f'\n{sev.upper()} ({len(group)}):')
        for i in sorted(group, key=lambda x: (x['code'], x['path'])):
            plugin = f"  [{i['plugin']}]" if i.get('plugin') else ''
            print(f"  {i['code']:<20} {i['path']} — {i['message']}{plugin}")

    total = sum(len(g) for g in by_sev.values())
    if total == 0:
        print('No issues found.')
    else:
        print(f"\n{len(by_sev['error'])} errors, {len(by_sev['warning'])} warnings, "
              f"{len(by_sev['info'])} info.")
    return len(by_sev['error'])


def _write(yaml_text: str, label: str, out_arg: str, default_path: Path) -> None:
    """Write yaml_text to stdout or a file, printing a status line."""
    if out_arg == '-':
        print(f'# [{label}]\n')
        print(yaml_text)
    else:
        out_path = Path(out_arg) if out_arg else default_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f'# [{label}]\n{yaml_text}', encoding='utf-8')
        print(f'Written [{label}]: {out_path}')


def run_cli(app: Any, args: argparse.Namespace, output_dir: Path = Path('.')) -> None:
    """
    Dispatch parsed CLI args to the appropriate dump_* function.

    Args:
        app:        Initialized kitebase app (setup_schema() is sufficient)
        args:       Parsed argparse.Namespace
        output_dir: Base directory for default output paths.
                    devtest uses Path('data'), a standalone CLI would use Path('.')
                    or a user-supplied --output-dir.
    """
    out_arg = getattr(args, 'output', None) or ''

    if args.command in DB_COMMANDS and getattr(app, 'engine', None) is not None:
        # Which database the report is about, before the report: with --engine
        # the answer is not the one config.yaml gives. The password is masked.
        engine = app.engine.url.render_as_string(hide_password=True)
        print(f'Engine: {engine}', file=sys.stderr)

    if args.command == 'dump-page':
        try:
            yaml_text, label = dump_page(app, args.page_id, auto=args.auto, raw=args.raw)
        except ValueError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        _write(yaml_text, label, out_arg,
               output_dir / 'pages' / f'{args.page_id}.yaml')

    elif args.command == 'help':
        from kitebase.help import help_manual, help_merge, render_html, render_manual
        if args.lang != 'en':
            app.plugins.load_all_locales()
        try:
            if args.page_id == '@manual':
                rendered = render_manual(app, help_manual(app, args.lang))
            else:
                rendered = render_html(app, help_merge(app, args.page_id, args.lang))
        except ValueError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        for warning in rendered.warnings:
            print(f'Warning: {warning}', file=sys.stderr)
        if rendered.external:
            print(f"Linked, not generated: {', '.join(rendered.external)}", file=sys.stderr)
        if out_arg == '-':
            print(rendered.html)
        else:
            name = 'manual' if args.page_id == '@manual' else args.page_id
            out_path = Path(out_arg) if out_arg else output_dir / 'help' / f'{name}.html'
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered.html, encoding='utf-8')
            print(f'Written: {out_path}')

    elif args.command == 'sync-help':
        from kitebase.help_texts import sync_help
        try:
            results = sync_help(app, args.lang, args.plugin)
        except ValueError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        for r in results:
            news = []
            if r.added:
                news.append(f'{len(r.added)} new')
            if r.changed:
                news.append(f'{len(r.changed)} changed in English')
            state = 'written' if r.written else 'unchanged'
            print(f"{r.path}: {state}{' (' + ', '.join(news) + ')' if news else ''}")
        if not results:
            print('No translation files: start one with --lang')

    elif args.command == 'dump-table':
        names = args.table or None
        try:
            yaml_text, label = dump_table(app, names)
        except ValueError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        default = (output_dir / 'tables' / f'{names[0]}.yaml'
                   if names and len(names) == 1
                   else output_dir / 'tables' / 'all.yaml')
        _write(yaml_text, label, out_arg, default)

    elif args.command == 'dump-types':
        yaml_text, label = dump_types(app, include_builtin=args.include_builtin)
        _write(yaml_text, label, out_arg, output_dir / 'types.yaml')

    elif args.command == 'check':
        import json
        from kitebase.diagnostics import run_checks

        issues = run_checks(app)
        n_errors = print_issues(issues)

        if args.dump is not None:
            dump = dump_app(app)
            out_path = Path(args.dump) if args.dump else output_dir / 'appdump.json'
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(dump, indent=2, ensure_ascii=False, default=str),
                encoding='utf-8')
            print(f'Full dump written: {out_path}')

        if n_errors:
            sys.exit(1)

    elif args.command == 'inspector':
        from kitebase import inspector
        try:
            inspector.serve(app, args.port, build_page=not args.no_build)
        except inspector.InspectorError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    elif args.command == 'db-check':
        report, aligned = db_check(app)
        print(report)
        if not aligned:
            sys.exit(1)

    elif args.command == 'db-sync':
        confirm = None if args.yes else _confirm_forced
        report, aligned = db_sync(app, dry_run=args.dry_run, force=args.force,
                                  confirm=confirm)
        print(report)
        if not aligned:
            sys.exit(1)

    elif args.command == 'db-backup':
        report, _ = db_backup(app, args.dest, media=args.media)
        print(report)

    elif args.command == 'db-copy':
        confirm = None if args.yes else _confirm_truncate
        report, ok = db_copy(app, args.to, force=args.force, dry_run=args.dry_run,
                             media=args.media, confirm=confirm)
        print(report)
        if not ok:
            sys.exit(1)

    elif args.command == 'new':
        print('`new` writes a fresh application, so it does not run from one: '
              'use the `kitebase` command.', file=sys.stderr)
        sys.exit(1)

    else:
        make_parser().print_help()
        sys.exit(1)


# ── Console script ─────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> None:
    """
    Entry point of the `kitebase` command.

    It carries what can be done **without** an application — `new`, `dev`,
    `build-client`. Every
    other command needs the application loaded, and loading it is the
    application's own business: its `kite.py` composes the sequence, registers
    its query behaviours and then calls `run_cli`. Two sequences for the same
    job would answer differently the day one of them forgot a step, so here
    those commands are handed to that script, run in the application's own
    environment (`cli:` in config.yaml names it, when it is not `kite.py`).
    """
    parser = make_parser()
    argv = _take_config(sys.argv[1:] if argv is None else list(argv))

    # A command this parser does not know may be one the application adds to
    # its own (`parser.commands.add_parser` in kite.py): the script decides.
    if argv and not argv[0].startswith('-') and argv[0] not in parser.commands.choices:
        from kitebase import dev
        try:
            sys.exit(dev.delegate(argv))
        except dev.DevError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    args = parser.parse_args(argv)

    if args.command == 'new':
        from kitebase.scaffold import create_app, print_next_steps
        try:
            target = create_app(args.name,
                                Path(args.directory) if args.directory else None,
                                force=args.force, server=args.server)
        except (ValueError, FileExistsError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        print_next_steps(target, args.name, args.server)
        return

    if args.command == 'inspector' and args.action:
        from kitebase import inspector
        try:
            if args.action == 'build':
                inspector.build()
                sys.exit(0)
            sys.exit(inspector.dev(args.port))
        except inspector.InspectorError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    if args.command in ('dev', 'build-client'):
        from kitebase import dev
        try:
            if args.command == 'dev':
                sys.exit(dev.run(app=args.app, framework=args.framework,
                                 src=args.src, ui=args.ui,
                                 no_server=args.no_server, no_client=args.no_client))
            sys.exit(dev.build_client(app=args.app, ui=args.ui))
        except dev.DevError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    if args.command:
        from kitebase import dev
        try:
            sys.exit(dev.delegate(argv))
        except dev.DevError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    parser.print_help()
