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

`compare` sets a set, or a file a person uploaded, against a table that has
rows, and `apply_new` writes only the new rows chosen, each whole: a row the
installation has is never changed from here.
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
    except (OSError, UnicodeDecodeError) as e:
        raise SeedError(f'{path}: {e}') from e
    return parse_rows(text, path.suffix, str(path))


def parse_rows(text: str, suffix: str, where: str) -> List[Dict[str, Any]]:
    """The rows of a text in the shape of a seed file, `suffix` saying which
    format; `where` names it in an error. A file uploaded reads the same."""
    if suffix.lower() not in SUFFIXES:
        raise SeedError(f"{where}: a seed file is {', '.join(SUFFIXES)}")
    try:
        if suffix.lower() == '.csv':
            return [{k: (v if v != '' else None) for k, v in row.items()
                     if isinstance(k, str) and not k.startswith(NOTE)}
                    for row in csv.DictReader(io.StringIO(text))]
        rows = yaml.safe_load(text)
    except (yaml.YAMLError, csv.Error) as e:
        raise SeedError(f'{where}: {e}') from e
    if rows is None:
        return []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SeedError(f'{where}: a seed file is a list of rows')
    return _without_notes(rows)


NOTE = '#'      # a key or a header starting with it is the writer's own: never read


def _without_notes(value: Any) -> Any:
    """The rows without the keys of the writer's own notes, at every level."""
    if isinstance(value, list):
        return [_without_notes(v) for v in value]
    if isinstance(value, dict):
        return {k: _without_notes(v) for k, v in value.items()
                if not (isinstance(k, str) and k.startswith(NOTE))}
    return value


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

    def __init__(self, app: Any, session: Any, system: bool = True):
        self.app = app
        self.session = session
        self.system = system
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
            values = write_values(table, values, system=self.system)
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


def export_rows(app: Any, session: Any, model: str,
                with_ids: bool = False) -> List[Dict[str, Any]]:
    """
    The rows of a table and what they own, in the shape of a seed file.

    `with_ids` keeps the id of each row at the top level under `$pk`: not part
    of a file, the way back to the row for whoever compares.
    """
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

    def clean(rows: List[Dict[str, Any]], top: bool = False) -> List[Dict[str, Any]]:
        result = []
        for row in rows:
            ref = row.pop('$id')
            out: Dict[str, Any] = {'id': numbers[ref]} if ref in numbers else {}
            if top and with_ids:
                out['$pk'] = ref[1]
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
    return clean(rows, top=True)


@dataclass(frozen=True)
class _Ref:
    """A key to a row of the exported tree, until the labels are numbered."""
    model: str
    id: Any


def owned_models(app: Any, model: str) -> set:
    """The models a row of `model` carries with it: its owned collections, down."""
    return _descendants(app, model)


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


# ── Comparing ─────────────────────────────────────────────────────────────────
#
# A set, or a file shaped like one, against what a table holds. The rows meet
# at the top level by their identity: the declared code, otherwise the display
# field as the installation reads it (translated). Below that, the children of
# a row meet the same way among their siblings. What differs is said, never
# changed: a row the installation has is the installation's.

EQUAL, DIFFERENT, NEW, ONLY_DB, AMBIGUOUS = 'equal', 'different', 'new', 'only_db', 'ambiguous'
NAME = '$name'              # what a normalized row is met by
ALIAS = '$as_written'       # a translated name of the file: the text it was written in


@dataclass
class Compared:
    """One row of the comparison."""
    state: str
    key: Any                                    # the identity, as written
    row: Optional[Dict[str, Any]] = None        # the row of the file
    index: Optional[int] = None                 # its place in the file, from 1
    db_id: Any = None                           # the row of the table it meets
    changes: Optional[List[Dict[str, Any]]] = None
    note: str = ''
    reason: str = ''                            # why ambiguous: no_key, in_file, in_table
    count: int = 0                              # how many rows have the key


def identity_field(app: Any, model: str) -> Optional[str]:
    """What the rows of `model` are met by: its code, else its display field."""
    info = app.tables[model].search_info
    return info.get('code_field') or info.get('display_field')


def _identity(value: Any) -> Any:
    return value.strip().casefold() if isinstance(value, str) else value


def _names(row: Dict[str, Any]) -> List[Any]:
    """What a row of the file is met by: its name as the installation reads it,
    then as the file writes it."""
    return [_identity(v) for v in (row.get(NAME), row.get(ALIAS)) if v is not None]


class _Normalizer:
    """
    Both sides in one form: the file's values as they would be written (types,
    `on_write`, the display translated), a default left out filled in, a label
    turned into the identity of the row it names. With `only_given`, a row of
    the file keeps only the columns it has.
    """

    def __init__(self, app: Any, locale: str, only_given: bool = False):
        self.app = app
        self.locale = locale
        self.only_given = only_given

    def tree(self, model: str, rows: List[Dict[str, Any]], from_file: bool) -> List[Dict[str, Any]]:
        names: Dict[Tuple[str, Any], Any] = {}
        self._labels(model, rows, from_file, names)
        return [self.row(model, r, None, from_file, names) for r in rows]

    def _labels(self, model: str, rows: List[Dict[str, Any]], from_file: bool,
                names: Dict[Tuple[str, Any], Any]) -> None:
        field = identity_field(self.app, model)
        children = _children(self.app, model)
        for row in rows:
            if row.get('id') is not None and field:
                names[(model, row['id'])] = self._display(model, field, row.get(field), from_file)
            for key, (child, _column) in children.items():
                if isinstance(row.get(key), list):
                    self._labels(child, row[key], from_file, names)

    def _display(self, model: str, field: str, value: Any, from_file: bool) -> Any:
        info = self.app.tables[model].search_info
        if from_file and field == info.get('display_field') and isinstance(value, str):
            with use_locale(self.locale):
                return _(value)
        return value

    def row(self, model: str, row: Dict[str, Any], owner_column: Optional[str],
            from_file: bool, names: Dict[Tuple[str, Any], Any]) -> Dict[str, Any]:
        table, cls = self.app.tables[model], self.app.models[model]
        out: Dict[str, Any] = {}
        for col in _exported_columns(self.app, model, owner_column):
            fk = _fk(col)
            if fk is not None:
                target = fk['table']
                code = target.search_info.get('code_field')
                dotted = f'{col.name}.{code}' if code else None
                if dotted and (dotted in row or col.name not in row):
                    value = row.get(dotted)
                    out[dotted] = _identity(str(value)) if value is not None else None
                elif row.get(col.name) is not None:
                    label = row[col.name]
                    out[col.name] = names.get((target.name, label), ('?', label))
                else:
                    out[col.name] = None
                continue
            value = row.get(col.name)
            if value is None:
                value = _static_default(cls, col.name)
            elif from_file:
                value = self._as_written(model, cls, col, value)
            out[col.name] = _comparable(value)
        field = identity_field(self.app, model)
        if field in out:
            shown = self._display(model, field, out[field], from_file)
            if shown != out[field]:
                # born in another language, a row keeps the text it was born with
                out[ALIAS] = out[field]
            out[field] = out[NAME] = shown
        if from_file and self.only_given:
            # what the row does not say is not compared: the name it is met by stays
            out = {k: v for k, v in out.items() if k in row or k in (field, NAME, ALIAS)}
        for key, (child, column) in _children(self.app, model).items():
            if from_file and self.only_given and key not in row:
                continue
            out[key] = [self.row(child, r, column, from_file, names)
                        for r in (row.get(key) or [])]
        return out

    @staticmethod
    def _as_written(model: str, cls: Any, col: Any, value: Any) -> Any:
        try:
            value = _coerce(cls, col.name, value)
        except (ValueError, ArithmeticError):
            return value
        transform_name = col.attributes.get('on_write')
        if transform_name and value != '':
            from kitebase.transforms import get_write_transform
            transform = get_write_transform(transform_name)
            if transform is not None:
                try:
                    value = transform(value)
                except Exception:   # a value the rule refuses: compared as it is
                    pass
        return value


def _comparable(value: Any) -> Any:
    if isinstance(value, float):
        value = decimal.Decimal(str(value))
    if isinstance(value, decimal.Decimal):
        return value.normalize()
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            return value
    return value


def _shown(value: Any) -> Any:
    """A normalized value as a person reads it."""
    if isinstance(value, decimal.Decimal):
        return format(value, 'f')
    if isinstance(value, tuple):            # a label nobody resolved
        return value[1]
    return value


def _diff(app: Any, model: str, db: Dict[str, Any], file: Dict[str, Any],
          prefix: str = '') -> List[Dict[str, Any]]:
    """What the file would change in the row, as {path, kind, db, seed}."""
    children = _children(app, model)
    field = identity_field(app, model)
    changes: List[Dict[str, Any]] = []
    for key in file:
        if key in children or key in (NAME, ALIAS):
            continue
        if key == field and _identity(db.get(key)) in _names(file):
            continue    # met by it: a code is the same code in any case
        if db.get(key) != file[key]:
            changes.append({'path': prefix + key, 'kind': 'changed',
                            'db': _shown(db.get(key)), 'seed': _shown(file[key])})
    for key, (child, _column) in children.items():
        if key not in file:
            continue    # a collection the file does not say
        changes += _diff_children(app, child, db.get(key) or [], file.get(key) or [],
                                  prefix + key)
    return changes


def _diff_children(app: Any, model: str, db_rows: List[Dict[str, Any]],
                   file_rows: List[Dict[str, Any]], path: str) -> List[Dict[str, Any]]:
    field = identity_field(app, model)
    if not field:
        # nothing to pair the siblings by: the collection is equal or it is not
        if db_rows == file_rows:
            return []
        return [{'path': path, 'kind': 'changed', 'db': f'{len(db_rows)} rows',
                 'seed': f'{len(file_rows)} rows'}]
    changes: List[Dict[str, Any]] = []
    by_key: Dict[Any, List[Dict[str, Any]]] = {}
    for row in db_rows:
        by_key.setdefault(_identity(row.get(field)), []).append(row)
    for row in file_rows:
        name = row.get(field)
        found = next((by_key[n] for n in _names(row) if by_key.get(n)), [])
        if not found:
            changes.append({'path': f'{path}[{name}]', 'kind': 'added', 'db': None, 'seed': name})
            continue
        changes += _diff(app, model, found.pop(0), row, f'{path}[{name}].')
    for rows in by_key.values():
        for row in rows:
            changes.append({'path': f'{path}[{row.get(field)}]', 'kind': 'missing',
                            'db': row.get(field), 'seed': None})
    return changes


def by_titles(app: Any, model: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The rows of a file a person made, its headers turned into column names: a
    header is a column's name or its title as a list shows it (the label, in
    English or in the language of the user or of the application, any case).
    What an export of a list writes comes back. A header that is neither
    stays as it is, for the comparison to say.

    For a file uploaded: a plugin's seed is written for a program, by name.
    """
    titles: Dict[str, str] = {}
    locale = app.plugins.config.get('locale', 'en')
    for col in _stored_columns(app.tables[model]).values():
        label = col.attributes.get('label')
        spelled = [col.name, col.name.replace('_', ' ')]
        if label:
            with use_locale(locale):
                spelled += [label, _(label)]
            spelled.append(_(label))
        for text in spelled:
            titles.setdefault(_identity(text), col.name)
    return [{titles.get(_identity(k), k) if isinstance(k, str) else k: v for k, v in row.items()}
            for row in rows]


def _unknown_keys(app: Any, model: str, rows: List[Dict[str, Any]]) -> set:
    """
    The keys of the rows that are nothing of `model`: no column, no key by
    code, no collection. A file meant for another table shows here, before
    the comparison would quietly pass over what it cannot place.
    """
    table = app.tables[model]
    columns = _stored_columns(table)
    children = _children(app, model)
    unknown: set = set()
    for row in rows:
        for key, value in row.items():
            column, dot, code = key.partition('.')
            if key in children:
                child, _column = children[key]
                if isinstance(value, list):
                    unknown |= {f'{key}.{k}' for k in _unknown_keys(
                        app, child, [r for r in value if isinstance(r, dict)])}
            elif not dot and key in columns:
                continue
            elif dot and column in columns and _fk(columns[column]) \
                    and _fk(columns[column])['table'].search_info.get('code_field') == code:
                continue
            else:
                unknown.add(key)
    return unknown


def compare(app: Any, model: str, rows: List[Dict[str, Any]],
            session: Any = None, only_given: bool = False) -> List[Compared]:
    """
    Compare rows shaped like a seed file with the table of `model`: in the
    order of the file, then the rows only the table has.

    A seed leaves out a column at its default, so a column missing from a row
    is the default. `only_given` compares only the columns the rows have: a
    file a person made, an export of some columns of a list.

    Raises:
        SeedError: a table with nothing to meet its rows by (neither a
            code_field nor a display field), or one the export cannot write
    """
    if model not in app.models:
        raise SeedError(f"'{model}' is no model of this application")
    field = identity_field(app, model)
    if not field:
        raise SeedError(f'{model} declares neither a code_field nor a display field: '
                        f'nothing to meet its rows by')
    unknown = _unknown_keys(app, model, rows)
    if unknown:
        raise SeedError(f"The rows do not fit {model}: it has no {', '.join(sorted(unknown))}. "
                        f"Tip: start the columns of your own with {NOTE}")
    if session is None:
        with app.get_session() as own:
            return compare(app, model, rows, own, only_given)

    norm = _Normalizer(app, app.plugins.config.get('locale', 'en'), only_given)
    db_rows = export_rows(app, session, model, with_ids=True)
    db_ids = [r.pop('$pk') for r in db_rows]
    db_norm = norm.tree(model, db_rows, from_file=False)
    file_norm = norm.tree(model, rows, from_file=True)

    in_db: Dict[Any, List[int]] = {}
    for i, row in enumerate(db_norm):
        in_db.setdefault(_identity(row.get(field)), []).append(i)
    in_file: Dict[Any, int] = {}
    for row in file_norm:
        key = _identity(row.get(field))
        in_file[key] = in_file.get(key, 0) + 1

    result: List[Compared] = []
    met: set = set()
    for index, (row, normal) in enumerate(zip(rows, file_norm), 1):
        name = normal.get(field)
        key = _identity(name)
        found = next((in_db[n] for n in _names(normal) if in_db.get(n)), [])
        item = Compared(NEW, name, row, index)
        if key is None or key == '':
            item.state, item.reason, item.note = AMBIGUOUS, 'no_key', f'no {field} to meet it by'
        elif in_file[key] > 1:
            item.state, item.reason, item.count = AMBIGUOUS, 'in_file', in_file[key]
            item.note = f'{in_file[key]} rows of the file have it'
        elif len(found) > 1:
            item.state, item.reason, item.count = AMBIGUOUS, 'in_table', len(found)
            item.note = f'{len(found)} rows of the table have it'
        elif found:
            item.db_id = db_ids[found[0]]
            item.changes = _diff(app, model, db_norm[found[0]], normal)
            item.state = DIFFERENT if item.changes else EQUAL
        met.update(found)
        result.append(item)
    for i, row in enumerate(db_norm):
        if i not in met:
            result.append(Compared(ONLY_DB, row.get(field), db_id=db_ids[i]))
    return result


def apply_new(app: Any, model: str, rows: List[Dict[str, Any]], keys: Iterable[Any],
              system: bool = False, dry_run: bool = False) -> Tuple[List[str], int]:
    """
    Write the rows of a comparison that were chosen, by their key: each whole,
    with what it owns, and only while it is still new. The comparison is made
    again here, so a row created by hand since it was shown, or grown ambiguous,
    is skipped and said rather than written twice.

    A label of the file naming a row the table already has is that row: a new
    account under an existing parent finds it. One row that cannot be written
    stops all of them, and nothing stays.

    `system` writes past the permissions of the user, as the seed at birth;
    from a request it stays False, and a column the user may not write takes
    its default.

    Returns:
        (report lines, rows written at the top level)

    Raises:
        SeedError: a row that cannot be written, said with its key
    """
    wanted = {_identity(k): k for k in keys}
    with app.get_session() as session:
        result = compare(app, model, rows, session)
        pk = pk_field(app.tables[model])
        run = _Run(app, session, system=system)
        for item in result:
            if item.db_id is not None and item.row and item.row.get(pk) is not None:
                run.labels[(model, item.row[pk])] = item.db_id

        lines: List[str] = []
        written = 0
        seen = set()
        for item in result:
            key = _identity(item.key)
            if item.row is None or key not in wanted:
                continue
            seen.add(key)
            if item.state != NEW:
                lines.append(f'{item.key}: skipped, no longer new ({item.state})')
                continue
            try:
                count = run.write(model, item.row)
            except SeedError as e:
                session.rollback()
                raise SeedError(f'{item.key}: {e}') from e
            written += 1
            verb = 'would be written' if dry_run else 'written'
            lines.append(f'{item.key}: {verb}'
                         + (f', with {count - 1} rows it owns' if count > 1 else ''))
        for key in wanted.keys() - seen:
            lines.append(f'{wanted[key]}: skipped, not in the file')
        if dry_run:
            session.rollback()
        else:
            session.commit()
    return lines, written


def compare_lines(app: Any, files: Iterable[SeedFile]) -> List[str]:
    """The comparison of seed files, as the CLI prints it: equal rows counted only."""
    labels = {DIFFERENT: 'different', NEW: 'new', ONLY_DB: 'only db', AMBIGUOUS: 'ambiguous'}
    lines: List[str] = []
    for seed in files:
        result = compare(app, seed.model, read_file(seed.path))
        counts = {state: sum(1 for c in result if c.state == state)
                  for state in (EQUAL, DIFFERENT, NEW, ONLY_DB, AMBIGUOUS)}
        lines.append(f'{seed.name}: ' + ', '.join(
            f'{n} {labels.get(state, state)}' for state, n in counts.items() if n))
        for item in result:
            if item.state == EQUAL:
                continue
            detail = item.note or '; '.join(_change_text(c) for c in item.changes or [])
            lines.append(f'  {labels[item.state]:<10} {item.key}' + (f': {detail}' if detail else ''))
    return lines


def _change_text(change: Dict[str, Any]) -> str:
    if change['kind'] == 'added':
        return f"{change['path']} added"
    if change['kind'] == 'missing':
        return f"{change['path']} missing"
    return f"{change['path']} {change['db']!r} -> {change['seed']!r}"
