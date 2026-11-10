"""
kitebase.db_copy — the rows of one database into another, through SQLAlchemy.

A dump does not travel between engines: SQLite writes booleans as 0/1, dates
and JSON as text, and its own quoting, and translating one dialect into another
is the fragile part. The schema is declared instead, so `db-sync` builds it on
any engine, and what is left is copying rows between two databases already
aligned with it. Each value is read through its column type and written through
the same type on the other side.

    copy_database(source, target, metadata)            # -> CopyReport

- **Preconditions**: both sides aligned with the schema (`diff_schema`), the
  target empty unless `truncate` (the CLI's --force) empties it first. A failed precondition writes nothing.
- **Order**: the tables in foreign-key order. A key that closes a cycle, or
  points to its own table, cannot be satisfied row by row: its columns go in as
  NULL and are filled by a second pass once every row is there. Such columns
  must be nullable, and the report names them.
- **Core, not ORM nor endpoints**: `insert()` on the Table. The endpoints apply
  write transforms (a password hashed twice) and the ORM session carries the
  access guard (a record rule would read fewer rows, silently). A copy moves
  every value as it is.
- **Sequences**: on PostgreSQL each one is set to the highest copied id, or the
  first insert after the copy fails on a duplicate key.
- **Verification**: rows counted on both sides before the commit.

Everything runs in one transaction on the target: all of it arrives, or none.
"""

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.schema import sort_tables_and_constraints

from kitebase.schema_sync import diff_schema, format_diff

CHUNK = 1000


class CopyError(RuntimeError):
    """A precondition or a check failed; the message says which, and where."""


@dataclass
class CopyReport:
    order: List[str] = field(default_factory=list)
    deferred: Dict[str, List[str]] = field(default_factory=dict)  # table -> columns
    counts: Dict[str, int] = field(default_factory=dict)          # table -> source rows
    sequences: List[str] = field(default_factory=list)
    dry_run: bool = False

    def format(self, source: str, target: str) -> str:
        width = max((len(t) for t in self.order), default=0)
        lines = [f'{source}  ->  {target}', '']
        for name in self.order:
            later = self.deferred.get(name)
            note = f'   (then: {", ".join(later)})' if later else ''
            lines.append(f'  {name:<{width}}  {self.counts.get(name, 0):>8}{note}')
        lines.append('')
        total = sum(self.counts.values())
        if self.dry_run:
            lines.append(f'Dry run: {total} row(s) in {len(self.order)} table(s) would be copied.')
        else:
            lines.append(f'Copied {total} row(s) in {len(self.order)} table(s), '
                         'counts verified on both sides.')
            if self.sequences:
                lines.append(f'Sequences set: {len(self.sequences)}.')
        return '\n'.join(lines)


def plan(metadata: sa.MetaData) -> Tuple[List[sa.Table], Dict[str, List[sa.ForeignKeyConstraint]]]:
    """
    The tables in foreign-key order, and the constraints left for a second pass:
    those closing a cycle, and those pointing to their own table.
    """
    ordered: List[sa.Table] = []
    deferred: Dict[str, List[sa.ForeignKeyConstraint]] = {}
    for table, cyclic in sort_tables_and_constraints(list(metadata.tables.values())):
        if table is not None:
            ordered.append(table)
        else:
            for fkc in cyclic:
                deferred.setdefault(fkc.table.name, []).append(fkc)
    for table in ordered:
        for fkc in table.foreign_key_constraints:
            if fkc.referred_table is table and fkc not in deferred.get(table.name, []):
                deferred.setdefault(table.name, []).append(fkc)
    return ordered, deferred


def _deferred_columns(fkcs: List[sa.ForeignKeyConstraint]) -> List[sa.Column]:
    columns = [col for fkc in fkcs for col in fkc.columns]
    rigid = [f'{c.table.name}.{c.name}' for c in columns if not c.nullable]
    if rigid:
        raise CopyError(
            f"Cannot copy: {', '.join(rigid)} closes a cycle of foreign keys and is "
            "NOT NULL, so no row order satisfies it")
    return columns


def _check_aligned(engine: Engine, metadata: sa.MetaData, side: str) -> None:
    diff = diff_schema(engine, metadata)
    if not diff.is_aligned:
        raise CopyError(
            f"The {side} differs from the schema; align it first "
            f"(db-sync on that engine):\n{format_diff(diff, sync_command=None)}")


def _count(conn: Any, table: sa.Table) -> int:
    return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def _masked(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=True)


def copy_database(source: Engine, target: Engine, metadata: sa.MetaData,
                  truncate: bool = False, dry_run: bool = False,
                  chunk: int = CHUNK) -> CopyReport:
    """
    Copy every table `metadata` declares from `source` to `target`.

    Raises CopyError, writing nothing, when a precondition fails or the counts
    differ at the end.
    """
    if _masked(source) == _masked(target):
        raise CopyError(f"Source and target are the same database: {_masked(source)}")

    _check_aligned(source, metadata, 'source')
    _check_aligned(target, metadata, 'target')

    ordered, deferred_fkcs = plan(metadata)
    later = {name: _deferred_columns(fkcs) for name, fkcs in deferred_fkcs.items()}

    report = CopyReport(order=[t.name for t in ordered],
                        deferred={n: [c.name for c in cols] for n, cols in later.items()},
                        dry_run=dry_run)

    with source.connect() as src:
        for table in ordered:
            report.counts[table.name] = _count(src, table)

        with target.connect() as probe:
            filled = [t.name for t in ordered if _count(probe, t)]
        if filled and not truncate:
            raise CopyError(
                f"The target is not empty ({', '.join(filled)}): "
                "copy into an empty database, or pass --force to empty it first")

        if dry_run:
            return report

        with target.begin() as dst:
            if truncate:
                for table in reversed(ordered):
                    dst.execute(table.delete())

            for table in ordered:
                _copy_table(src, dst, table, later.get(table.name, []), chunk)

            for table in ordered:
                if later.get(table.name):
                    _fill_deferred(src, dst, table, later[table.name], chunk)

            if target.dialect.name == 'postgresql':
                report.sequences = _reset_sequences(dst, ordered)

            wrong = [f'{t.name}: {report.counts[t.name]} -> {_count(dst, t)}'
                     for t in ordered if _count(dst, t) != report.counts[t.name]]
            if wrong:
                raise CopyError('Row counts differ, nothing written:\n  ' + '\n  '.join(wrong))

    return report


def _copy_table(src: Any, dst: Any, table: sa.Table, nulled: List[sa.Column],
                chunk: int) -> None:
    columns = list(table.columns)
    blank = {c.key for c in nulled}
    result = src.execution_options(stream_results=True).execute(sa.select(table))
    try:
        while True:
            rows = result.fetchmany(chunk)
            if not rows:
                break
            batch = [{c.key: (None if c.key in blank else row._mapping[c]) for c in columns}
                     for row in rows]
            dst.execute(table.insert(), batch)
    except sa.exc.DBAPIError as exc:
        raise CopyError(f"Copying {table.name}: {exc.orig}") from exc


def _fill_deferred(src: Any, dst: Any, table: sa.Table, columns: List[sa.Column],
                   chunk: int) -> None:
    pk = list(table.primary_key.columns)
    if not pk:
        raise CopyError(f"{table.name} has deferred foreign keys but no primary key "
                        "to find its rows again")
    stmt = (table.update()
            .where(sa.and_(*[c == sa.bindparam(f'_pk_{c.key}') for c in pk]))
            .values({c.key: sa.bindparam(f'_v_{c.key}') for c in columns}))
    some = sa.or_(*[c.isnot(None) for c in columns])
    result = src.execute(sa.select(*pk, *columns).where(some))
    while True:
        rows = result.fetchmany(chunk)
        if not rows:
            break
        dst.execute(stmt, [{**{f'_pk_{c.key}': row._mapping[c] for c in pk},
                            **{f'_v_{c.key}': row._mapping[c] for c in columns}}
                           for row in rows])


def _reset_sequences(dst: Any, tables: List[sa.Table]) -> List[str]:
    """Each serial or identity column's sequence set past the highest id copied."""
    done = []
    for table in tables:
        for column in table.primary_key.columns:
            if not isinstance(column.type, sa.Integer):
                continue
            seq = dst.execute(sa.text('SELECT pg_get_serial_sequence(:t, :c)'),
                              {'t': table.name, 'c': column.name}).scalar()
            if not seq:
                continue
            top = dst.execute(sa.select(sa.func.max(column))).scalar()
            dst.execute(sa.text('SELECT setval(:s, :v, :called)'),
                        {'s': seq, 'v': top or 1, 'called': top is not None})
            done.append(seq)
    return done


# ── Media: the files that go with the rows ────────────────────────────────────

def media_roots(media_config: Dict[str, Any]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    The roots that belong to a database, and those every engine shares.

    A relative filesystem root hangs from the engine's media base, so it is
    copied and backed up with the rows. An absolute one is the same folder for
    every engine: left alone, and named. The cache root is left out too: its
    renditions are made again on the first request.

    Returns:
        ([(root, relative path)], [shared root names])
    """
    roots = (media_config or {}).get('roots') or {}
    cache = (media_config or {}).get('cache')
    own: List[Tuple[str, str]] = []
    shared: List[str] = []
    for name, decl in roots.items():
        decl = decl or {}
        if decl.get('type', 'fs') != 'fs' or name == cache:
            continue
        if Path(decl['path']).is_absolute():
            shared.append(name)
        else:
            own.append((name, decl['path']))
    return own, shared


def _files(folder: Path) -> List[Path]:
    return [p for p in folder.rglob('*') if p.is_file()] if folder.is_dir() else []


class MediaCopy:
    """
    The media of one engine into the base of another, staged first.

    The files are copied into a hidden folder beside the target and moved into
    place only after the rows are committed: a copy that fails leaves the
    target's files as they were, like its rows.
    """

    STAGE = '.kitebase-copy'

    def __init__(self, source: Path, target: Path, roots: List[Tuple[str, str]]):
        self.source, self.target, self.roots = Path(source), Path(target), roots
        self.stage = self.target / self.STAGE

    def count(self) -> int:
        return sum(len(_files(self.source / path)) for _, path in self.roots)

    def occupied(self) -> List[str]:
        """The roots that already hold files on the target side."""
        return [name for name, path in self.roots if _files(self.target / path)]

    def copy_to_stage(self) -> int:
        if self.stage.exists():
            shutil.rmtree(self.stage)
        copied = 0
        for _, path in self.roots:
            src = self.source / path
            if src.is_dir():
                shutil.copytree(src, self.stage / path)
                copied += len(_files(self.stage / path))
        return copied

    def commit(self) -> None:
        """The staged roots replace the target's; what was there is removed."""
        for _, path in self.roots:
            staged, final = self.stage / path, self.target / path
            if final.exists():
                shutil.rmtree(final)
            if staged.exists():
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, final)
        shutil.rmtree(self.stage, ignore_errors=True)

    def abort(self) -> None:
        shutil.rmtree(self.stage, ignore_errors=True)


def snapshot_files(source: Path, target: Path, previous: Optional[Path] = None) -> Tuple[int, int]:
    """
    Copy the tree `source` into `target`, hard-linking each file that is the
    same in `previous` (same relative path, size and modification time).

    Media files are written once and never rewritten, so a series of backups
    costs the space of what changed: the result of `rsync --link-dest`, with
    nothing but Python, on Windows (NTFS) as on Linux. Where a link cannot be
    made (another device, a filesystem without them) the file is copied.

    Returns:
        (files copied, files linked)
    """
    copied = linked = 0
    for src in _files(source):
        rel = src.relative_to(source)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        old = previous / rel if previous is not None else None
        if old is not None and old.is_file():
            a, b = src.stat(), old.stat()
            if a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime):
                try:
                    os.link(old, dst)
                    linked += 1
                    continue
                except OSError:
                    pass
        shutil.copy2(src, dst)
        copied += 1
    return copied, linked
