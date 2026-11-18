"""
The seed: the standard configuration a plugin brings to an installation.

A plugin ships sets of rows in `<plugin>/seed/<set>/<Model>.yaml` (or `.json`,
or `.csv` for a flat list). A set fills a table **while it is empty**; from then
on the rows belong to the installation, which changes or replaces them. The set
`base` is applied by itself when its table is born — created by the server on a
new database, or by `db-sync` when a plugin adds it — and never to a table that
already exists, empty or not. Every other set, `demo` included, only through
`kitebase db-seed <plugin>/<set>`.

The file, a list of rows:

    - name: Sales               # the display field: translated on writing
      Stage:                    # an owned collection, by the child's model name
        - { name: New, sequence: 1 }
      payment_method_id.code: WT   # a key outside the set, by the target's code

- the ids of a file are **local labels**, never ids of the database: a row
  carries one only where another row of the run cites it, and the loader maps
  it onto the id it writes. A foreign key holding a plain value is such a
  label, written earlier in the same run;
- a key outside the set is written as `<column>.<code>`, the target's declared
  `code_field` (DATA_MODEL.md §4.4), looked up exactly;
- a child of an owned foreign key nests under its parent, its key the child's
  model name; `Model.column` where two owned keys of a child point at the
  same parent.

Rows are written as the system (no permission of a user, no rule of a page),
through the rules of the columns: `on_write`, `validate`, a code's case. A row
that cannot be written stops its file, and nothing of the file stays.

`export` writes the same shape from the database, which is how a seed is
prepared: configured in the application, exported, put in the plugin.
"""
import csv
import datetime
import decimal
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from sqlalchemy import Boolean, Integer, Numeric, func

from kitebase.endpoint_db import write_values, coerce_value, require_columns, pk_field
from kitebase.i18n import _, use_locale
from kitebase.querybuilder import DynamicQueryBuilder
from kitebase.transforms import ValidationError

SEED_DIR = 'seed'
BASE_SET = 'base'
SUFFIXES = ('.yaml', '.yml', '.json', '.csv')


class SeedError(Exception):
    """A seed file that cannot be read or written, said with where."""


@dataclass
class SeedFile:
    plugin: str
    set: str
    model: str
    path: Path

    @property
    def name(self) -> str:
        return f'{self.plugin}/{self.set}/{self.model}'


# ── Finding the files ─────────────────────────────────────────────────────────

def seed_files(app: Any, set_ref: Optional[str] = None) -> List[SeedFile]:
    """
    The seed files of the application, in the order they can be written: the
    plugins by `depends_on`, the tables of a plugin by their foreign keys.

    `set_ref` narrows to one set, `<plugin>/<set>`; None lists them all.
    """
    plugin_name = set_name = None
    if set_ref:
        plugin_name, _sep, set_name = set_ref.partition('/')
        if not set_name or plugin_name not in app.plugins.plugins:
            raise SeedError(f"Unknown set '{set_ref}': write it as <plugin>/<set>")

    order = _table_order(app)
    files: List[SeedFile] = []
    for name in app.plugins.sorted:
        if plugin_name and name != plugin_name:
            continue
        root = app.plugins.plugins[name].plugin_dir / SEED_DIR
        if not root.is_dir():
            continue
        for set_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if set_name and set_dir.name != set_name:
                continue
            found = []
            for path in sorted(set_dir.iterdir()):
                if path.suffix not in SUFFIXES:
                    continue
                if path.stem not in app.models:
                    raise SeedError(f"{path}: '{path.stem}' is no model of this application")
                found.append(SeedFile(name, set_dir.name, path.stem, path))
            files += sorted(found, key=lambda f: order.get(f.model, 0))
    if set_ref and not files:
        raise SeedError(f"No set '{set_ref}': nothing in "
                        f"{app.plugins.plugins[plugin_name].plugin_dir / SEED_DIR / set_name}")
    return files


def _table_order(app: Any) -> Dict[str, int]:
    """Model name -> place in the order of the foreign keys (targets first)."""
    from kitebase.model_base import Base
    by_table = {getattr(cls, '__tablename__', None): name for name, cls in app.models.items()}
    return {by_table[t.name]: i for i, t in enumerate(Base.metadata.sorted_tables)
            if t.name in by_table}


def read_file(path: Path) -> List[Dict[str, Any]]:
    """The rows of a seed file. YAML reads JSON too; CSV by its extension."""
    try:
        text = path.read_text(encoding='utf-8')
        if path.suffix == '.csv':
            return [{k: (v if v != '' else None) for k, v in row.items()}
                    for row in csv.DictReader(io.StringIO(text))]
        rows = yaml.safe_load(text)
    except (OSError, yaml.YAMLError, csv.Error) as e:
        raise SeedError(f'{path}: {e}') from e
    if rows is None:
        return []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SeedError(f'{path}: a seed file is a list of rows')
    return rows


# ── The shape of a model ──────────────────────────────────────────────────────

def _fk(col: Any) -> Optional[Dict[str, Any]]:
    fk = col.attributes.get('foreign_key')
    return fk if fk and 'table' in fk else None


def _children(app: Any, model: str) -> Dict[str, Tuple[str, str]]:
    """The owned collections of a model: key in the file -> (child, its key)."""
    found: List[Tuple[str, str]] = []
    for name, table in app.tables.items():
        for col in table.effective_columns:
            fk = _fk(col)
            if fk and fk.get('owned') and fk['table'].name == model:
                found.append((name, col.name))
    counts: Dict[str, int] = {}
    for child, _col in found:
        counts[child] = counts.get(child, 0) + 1
    return {(child if counts[child] == 1 else f'{child}.{col}'): (child, col)
            for child, col in found}


def _stored_columns(table: Any) -> Dict[str, Any]:
    return {c.name: c for c in table.effective_columns
            if not c.attributes.get('virtual', False)}


# ── Writing ───────────────────────────────────────────────────────────────────

class _Run:
    """One application of seed files: the labels written so far, the counts."""

    def __init__(self, app: Any, session: Any):
        self.app = app
        self.session = session
        self.labels: Dict[Tuple[str, Any], Any] = {}
        self.locale = app.plugins.config.get('locale', 'en')

    def write_file(self, seed: SeedFile) -> int:
        rows = read_file(seed.path)
        written = 0
        for index, row in enumerate(rows, 1):
            try:
                written += self.write(seed.model, row)
            except SeedError as e:
                raise SeedError(f'{seed.path}, row {index}: {e}') from e
        return written

    def write(self, model: str, row: Dict[str, Any],
              owner: Optional[Tuple[str, Any]] = None) -> int:
        """Write a row and what it owns; returns how many rows were written."""
        app = self.app
        table, cls = app.tables[model], app.models[model]
        pk = pk_field(table)
        columns = _stored_columns(table)
        children = _children(app, model)

        row = dict(row)
        label = row.pop(pk, None)
        nested = {key: row.pop(key) for key in list(row) if key in children}

        values: Dict[str, Any] = {}
        for key, value in row.items():
            if '.' in key:
                column, _dot, target_column = key.partition('.')
                values[column] = self._by_code(model, columns, column, target_column, value)
            elif key not in columns:
                raise SeedError(f"'{model}' has no column '{key}'")
            elif _fk(columns[key]) and value is not None:
                target = _fk(columns[key])['table'].name
                if (target, value) not in self.labels:
                    raise SeedError(
                        f"'{key}: {value}' names no row of {target} written before it: "
                        f"a key outside the set is written as {key}.<code>")
                values[key] = self.labels[(target, value)]
            else:
                values[key] = _coerce(cls, key, value)
        if owner:
            values[owner[0]] = owner[1]

        display = table.search_info.get('display_field')
        if display and isinstance(values.get(display), str):
            with use_locale(self.locale):
                values[display] = _(values[display])

        try:
            values = write_values(table, values, system=True)
            require_columns(cls, values)
        except ValidationError as e:
            raise SeedError('; '.join(f'{k}: {v}' for k, v in e.errors.items())) from e

        obj = cls(**values)
        self.session.add(obj)
        self.session.flush()
        record_id = getattr(obj, pk)
        if label is not None:
            self.labels[(model, label)] = record_id

        written = 1
        for key, child_rows in nested.items():
            child, column = children[key]
            if not isinstance(child_rows, list):
                raise SeedError(f"'{key}' of {model} is a list of rows")
            for child_row in child_rows:
                written += self.write(child, child_row, (column, record_id))
        return written

    def _by_code(self, model: str, columns: Dict[str, Any], column: str,
                 target_column: str, value: Any) -> Any:
        fk = _fk(columns[column]) if column in columns else None
        if fk is None:
            raise SeedError(f"'{column}.{target_column}': '{column}' is no foreign key of {model}")
        if value is None:
            return None
        target = fk['table']
        if target.search_info.get('code_field') != target_column:
            raise SeedError(f"'{column}.{target_column}': {target.name} declares no "
                            f"code_field '{target_column}'")
        # `resolve`: an archived row is still the row the code names
        query = DynamicQueryBuilder(self.session, self.app.models).build_query({
            'table': target.name, 'select': [fk['id']], 'code': str(value),
            'resolve': True, 'limit': 2})
        found = [row[0] for row in self.session.execute(query)]
        if len(found) != 1:
            raise SeedError(f"'{column}.{target_column}: {value}': no {target.name} has it")
        return found[0]


def _coerce(cls: Any, key: str, value: Any) -> Any:
    """A value of a file as the column takes it: a CSV gives only text."""
    if not isinstance(value, str):
        return value
    col = cls.__table__.columns.get(key)
    if col is not None:
        if isinstance(col.type, Boolean):
            return value.strip().lower() in ('1', 'true', 'yes', 'y')
        if isinstance(col.type, Numeric) and not isinstance(col.type, Integer):
            return decimal.Decimal(value)
    return coerce_value(cls, key, value)


def row_count(app: Any, session: Any, model: str) -> int:
    cls = app.models[model]
    return session.query(func.count()).select_from(cls).scalar() or 0


def apply(app: Any, files: Iterable[SeedFile], dry_run: bool = False) -> Tuple[List[str], bool]:
    """
    Apply seed files to the tables that are empty; a table with rows is skipped,
    and said. Each file is a transaction of its own: one that fails leaves
    nothing written and the others go on.

    Returns:
        (report lines, all written or skipped without error)
    """
    lines: List[str] = []
    ok = True
    for seed in files:
        with app.get_session() as session:
            present = row_count(app, session, seed.model)
            if present:
                lines.append(f'{seed.name}: skipped, {seed.model} has {present} rows')
                continue
            try:
                written = _Run(app, session).write_file(seed)
            except SeedError as e:
                session.rollback()
                lines.append(f'{seed.name}: not written: {e}')
                ok = False
                continue
            if dry_run:
                session.rollback()
                lines.append(f'{seed.name}: would write {written} rows')
            else:
                session.commit()
                lines.append(f'{seed.name}: {written} rows written')
    return lines, ok


def on_birth(app: Any, born: Iterable[str]) -> List[str]:
    """
    The `base` sets of the tables just created, said in the log. A table that
    existed before is never seeded from here.
    """
    born = set(born)
    if not born:
        return []
    try:
        files = [f for f in seed_files(app) if f.set == BASE_SET and f.model in born]
    except SeedError as e:
        app.plugins.logger.error(f'seed: {e}')
        return [str(e)]
    if not files:
        return []
    lines, ok = apply(app, files)
    for line in lines:
        (app.plugins.logger.info if ok else app.plugins.logger.error)(f'seed: {line}')
    return lines


def describe(app: Any) -> List[str]:
    """The sets of the application, and whether each table is still empty."""
    lines = []
    with app.get_session() as session:
        for seed in seed_files(app):
            present = row_count(app, session, seed.model)
            state = f'{present} rows in the table' if present else 'table empty'
            auto = ' (applied when the table is born)' if seed.set == BASE_SET else ''
            lines.append(f'{seed.name}: {len(read_file(seed.path))} rows in the file, '
                         f'{state}{auto}')
    return lines or ['No seed sets in the plugins of this application.']


# ── Exporting ─────────────────────────────────────────────────────────────────

def _exported_columns(app: Any, model: str, owner_column: Optional[str]) -> List[Any]:
    """What a seed carries: the configuration, not the bookkeeping."""
    table = app.tables[model]
    pk = pk_field(table)
    kept = []
    for col in _stored_columns(table).values():
        attrs = col.attributes
        default = attrs.get('default')
        if (col.name in (pk, owner_column) or attrs.get('secret') or attrs.get('media')
                or (isinstance(default, str) and default.startswith('$'))):
            continue
        kept.append(col)
    return kept


def _plain(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _static_default(cls: Any, name: str) -> Any:
    col = cls.__table__.columns.get(name)
    default = getattr(col, 'default', None) if col is not None else None
    arg = getattr(default, 'arg', None)
    return None if callable(arg) else arg


def export_rows(app: Any, session: Any, model: str) -> List[Dict[str, Any]]:
    """The rows of a table and what they own, in the shape of a seed file."""
    cited: set = set()
    tree_models = {model} | _descendants(app, model)

    def rows_of(model_name: str, owner: Optional[Tuple[str, Any]]) -> List[Dict[str, Any]]:
        table, cls = app.tables[model_name], app.models[model_name]
        pk = pk_field(table)
        query = session.query(cls)
        if owner:
            query = query.filter(getattr(cls, owner[0]) == owner[1])
        out = []
        for obj in query.order_by(getattr(cls, pk)).all():
            row: Dict[str, Any] = {'$id': (model_name, getattr(obj, pk))}
            for col in _exported_columns(app, model_name, owner[0] if owner else None):
                value = getattr(obj, col.name)
                if value is None or value == _static_default(cls, col.name):
                    continue
                fk = _fk(col)
                if fk is None:
                    row[col.name] = _plain(value)
                elif fk['table'].name in tree_models:
                    cited.add((fk['table'].name, value))
                    row[col.name] = _Ref(fk['table'].name, value)
                else:
                    row.update(_as_code(app, session, model_name, col, fk, value))
            for key, (child, column) in _children(app, model_name).items():
                nested = rows_of(child, (column, getattr(obj, pk)))
                if nested:
                    row[key] = nested
            out.append(row)
        return out

    # The labels are numbered 1, 2, … in the order the rows come out, not the
    # ids of this database: exported again after a reload, the file is the same.
    numbers: Dict[Tuple[str, Any], int] = {}

    def number(rows: List[Dict[str, Any]]) -> None:
        for row in rows:
            if row['$id'] in cited:
                numbers[row['$id']] = len(numbers) + 1
            for value in row.values():
                if isinstance(value, list):
                    number(value)

    def clean(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for row in rows:
            ref = row.pop('$id')
            out: Dict[str, Any] = {'id': numbers[ref]} if ref in numbers else {}
            for key, value in row.items():
                if isinstance(value, list):
                    out[key] = clean(value)
                elif isinstance(value, _Ref):
                    if (value.model, value.id) not in numbers:
                        raise SeedError(f'{key}: {value.model} {value.id} is outside what '
                                        f'is exported, and has no code to name it by')
                    out[key] = numbers[(value.model, value.id)]
                else:
                    out[key] = value
            result.append(out)
        return result

    rows = rows_of(model, None)
    number(rows)
    return clean(rows)


@dataclass(frozen=True)
class _Ref:
    """A key to a row of the exported tree, until the labels are numbered."""
    model: str
    id: Any


def _descendants(app: Any, model: str) -> set:
    found: set = set()
    for child, _column in _children(app, model).values():
        if child not in found:
            found |= {child} | _descendants(app, child)
    return found


def _as_code(app: Any, session: Any, model: str, col: Any, fk: Dict[str, Any],
             value: Any) -> Dict[str, Any]:
    """A key outside the exported tree, as the target's code."""
    target = fk['table']
    code_field = target.search_info.get('code_field')
    if not code_field:
        raise SeedError(
            f"{model}.{col.name} points at {target.name}, which declares no code_field: "
            f"its id would name a different row in every database")
    target_cls = app.models[target.name]
    obj = session.query(target_cls).filter(getattr(target_cls, fk['id']) == value).first()
    if obj is None:
        raise SeedError(f'{model}.{col.name}: no {target.name} {value}')
    return {f'{col.name}.{code_field}': _plain(getattr(obj, code_field))}


def export(app: Any, model: str, set_ref: str) -> Tuple[Path, int]:
    """
    Write the rows of `model` as `<plugin>/seed/<set>/<model>.yaml`.

    Returns:
        (the file written, how many rows at the top level)
    """
    if model not in app.models:
        raise SeedError(f"'{model}' is no model of this application")
    plugin_name, _sep, set_name = set_ref.partition('/')
    if not set_name or plugin_name not in app.plugins.plugins:
        raise SeedError(f"Unknown destination '{set_ref}': write it as <plugin>/<set>")
    with app.get_session() as session:
        rows = export_rows(app, session, model)
    path = app.plugins.plugins[plugin_name].plugin_dir / SEED_DIR / set_name / f'{model}.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(rows), encoding='utf-8')
    return path, len(rows)


def dump(rows: List[Dict[str, Any]]) -> str:
    return yaml.safe_dump(rows, sort_keys=False, allow_unicode=True, default_flow_style=False)
