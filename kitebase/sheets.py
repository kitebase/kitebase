"""
Documents read from a spreadsheet, as their form would write them.

A layout is declared in Python, by subclassing `SheetImport`: the form whose
documents the sheet holds, and what each column is.

    class OrderSheet(SheetImport):
        page = 'purchaseorder_form'
        collection = 'lines'
        columns = {
            'order':     Key(),
            'publisher': Lookup('Publisher', 'name', field='publisher_id', head=True),
            'isbn':      Lookup('Book', 'isbn', field='book_id', ignore='- '),
            'title':     Ignored(),
            'quantity':  Number(field='quantity', positive=True, required=True),
        }

The first row names the columns. Reading goes in two passes and writes
nothing until both are clean: the header first (every column of the layout,
no other), then the rows (types, ranges, keys found), with every mistake
collected by row and column. The rows are grouped into documents by the
`Key` column, or the sheet is one document without one, and the documents go
through `flow.run_forms`: the proposals of the form's phases, then one
transaction for the whole sheet.

A different layout for the same documents (a supplier's own sheet) is a
subclass that changes the columns, or a check.
"""
import io
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func

import kitebase
from kitebase import access
from kitebase.i18n import _f


class CellError(ValueError):
    """What is wrong with one cell, in words for the person who wrote it."""


# ── Columns ──────────────────────────────────────────────────────────────────

class Column:
    """
    One column of the sheet. `field` is where its value goes (None: nowhere);
    `head` puts it on the document rather than on the row, and then every row
    of a document must agree on it.
    """

    def __init__(self, field: Optional[str] = None, head: bool = False,
                 required: bool = False):
        self.field = field
        self.head = head
        self.required = required

    def parse(self, value: Any) -> Any:
        """The value as the field takes it, or CellError."""
        return value


class Ignored(Column):
    """A column for whoever reads the sheet: expected, never read."""


class Key(Column):
    """The column that groups the rows into documents: one document per value."""

    def __init__(self):
        super().__init__(required=True)

    def parse(self, value: Any) -> Any:
        return str(value).strip()


class Text(Column):
    def __init__(self, field: str, length: Optional[int] = None, **kw):
        super().__init__(field, **kw)
        self.length = length

    def parse(self, value: Any) -> Any:
        text = str(value).strip()
        if self.length is not None and len(text) > self.length:
            raise CellError(_f('longer than {n} characters', n=self.length))
        return text


class Number(Column):
    """A number. In a text cell, a point or a comma is the decimal separator."""

    def __init__(self, field: str, min: Optional[float] = None, max: Optional[float] = None,
                 positive: bool = False, **kw):
        super().__init__(field, **kw)
        self.min, self.max, self.positive = min, max, positive

    def parse(self, value: Any) -> Any:
        if isinstance(value, bool):
            raise CellError(_f('not a number: {value}', value=value))
        if not isinstance(value, (int, float)):
            text = self.clean(str(value).strip())
            try:
                value = float(text.replace(',', '.'))
            except ValueError:
                raise CellError(_f('not a number: {value}', value=value)) from None
        if self.positive and value <= 0:
            raise CellError(_f('must be more than 0: {value}', value=value))
        if self.min is not None and value < self.min:
            raise CellError(_f('below {min}: {value}', min=self.min, value=value))
        if self.max is not None and value > self.max:
            raise CellError(_f('over {max}: {value}', max=self.max, value=value))
        return value

    def clean(self, text: str) -> str:
        """The text before it is read as a number: a subclass strips a '%'."""
        return text


AMBIGUOUS = object()


class Lookup(Column):
    """
    A key found by a readable value: the publisher by its name, the book by
    its ISBN. `ignore` lists characters left out on both sides (the dashes of
    an ISBN); text is compared without regard to case. One query per column,
    under the reader's permissions: a value nobody can read is not found.
    """

    def __init__(self, model: str, by: str, field: str, ignore: str = '', **kw):
        super().__init__(field, **kw)
        self.model, self.by, self.ignore = model, by, ignore

    def normal(self, value: Any) -> str:
        text = str(value).strip().lower()
        for ch in self.ignore:
            text = text.replace(ch, '')
        return text

    def resolve(self, session, values: List[str]) -> Dict[str, Any]:
        """Each normal value: its key, or AMBIGUOUS when two rows share it."""
        model = kitebase.utils.get_app().model
        cls = getattr(model, self.model)
        expr = func.lower(getattr(cls, self.by))
        for ch in self.ignore:
            expr = func.replace(expr, ch, '')
        found: Dict[str, Any] = {}
        for key, normal in session.query(cls.id, expr).filter(expr.in_(values)):
            found[normal] = AMBIGUOUS if normal in found else key
        return found


# ── The sheet ────────────────────────────────────────────────────────────────

class SheetImport:
    page: str = ''                       # the form whose documents the sheet holds
    collection: str = 'lines'            # the collection the rows go into
    sequence: Optional[str] = None       # a field numbered 10, 20... in sheet order
    columns: Dict[str, Column] = {}
    max_bytes = 2 * 1024 * 1024
    max_errors = 50
    noun, nouns = 'document', 'documents'   # what a document is called in the answer

    # ── Reading ──────────────────────────────────────────────────────────────

    def run(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """The endpoint's answer for an uploaded sheet: written, or why not."""
        from kitebase.flow import run_forms
        from kitebase.media import MediaError, UploadedFile

        upload = data.get('file')
        if not isinstance(upload, UploadedFile):
            return {'status': 'error', 'code': 400, 'message': 'No file: call through upload/<op>'}
        if not upload.filename.lower().endswith('.xlsx'):
            return {'status': 'error', 'code': 415,
                    'message': _f('{name} is not an Excel sheet (.xlsx)', name=upload.filename)}
        try:
            content = upload.read(self.max_bytes)
        except MediaError as e:
            return {'status': 'error', 'code': e.status, 'message': str(e)}

        try:
            rows = self.read(content)
        except CellError as e:
            return {'status': 'error', 'code': 415, 'message': str(e)}
        documents, errors = self.documents(rows)
        if errors:
            return self.refusal(errors)

        result = run_forms(self.page, [tree for _, tree in documents], data.get('answers'),
                           labels=[label for label, _ in documents])
        if result.get('status') != 'success':
            return result
        ids = result['data']['ids']
        out = {'status': 'success',
               'data': {'message': _f('{n} {noun} imported', n=len(ids),
                                      noun=self.noun if len(ids) == 1 else self.nouns),
                        'detail': '\n'.join(f'{label}: #{key}'
                                            for (label, _), key in zip(documents, ids))}}
        if result.get('$flow'):
            out['$flow'] = result['$flow']
        return out

    def read(self, content: bytes) -> List[Tuple[int, List[Any]]]:
        """The rows of the first sheet, numbered as the spreadsheet numbers them."""
        from openpyxl import load_workbook
        try:
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception:
            raise CellError(_f('This file is not an Excel sheet')) from None
        sheet = book.worksheets[0]
        rows = [(n, list(row)) for n, row in enumerate(sheet.iter_rows(values_only=True), start=1)]
        book.close()
        return [(n, row) for n, row in rows if any(v not in (None, '') for v in row)]

    # ── Checking ─────────────────────────────────────────────────────────────

    def header(self, row: List[Any]) -> Tuple[Dict[str, int], List[str]]:
        """Where each column of the layout is, and what is wrong with the header."""
        names = [str(v).strip() if v is not None else '' for v in row]
        where = {name.lower(): i for i, name in enumerate(names) if name}
        expected = {name.lower(): name for name in self.columns}
        missing = [name for low, name in expected.items() if low not in where]
        extra = [name for name in names if name and name.lower() not in expected]
        errors = []
        if missing:
            errors.append(_f('Row 1: missing columns: {names}', names=', '.join(missing)))
        if extra:
            errors.append(_f('Row 1: columns this import does not know: {names}',
                             names=', '.join(extra)))
        return {name: where[name.lower()] for name in self.columns if name.lower() in where}, errors

    def documents(self, rows: List[Tuple[int, List[Any]]]
                  ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str]]:
        """The documents of the sheet, `(label, tree)`, and every mistake found."""
        if not rows:
            return [], [_f('The sheet is empty')]
        (_, first), body = rows[0], rows[1:]
        where, errors = self.header(first)
        if errors:
            return [], errors
        if not body:
            return [], [_f('The sheet has no rows below the header')]

        parsed: List[Tuple[int, Dict[str, Any]]] = []
        for n, row in body:
            cells: Dict[str, Any] = {}
            for name, column in self.columns.items():
                if isinstance(column, Ignored):
                    continue
                raw = row[where[name]] if where[name] < len(row) else None
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    if column.required:
                        errors.append(_f('Row {n}, {column}: missing', n=n, column=name))
                    continue
                try:
                    cells[name] = column.parse(raw)
                except CellError as e:
                    errors.append(_f('Row {n}, {column}: {error}', n=n, column=name, error=e))
            parsed.append((n, cells))

        self.resolve(parsed, errors)
        errors += self.check(parsed)
        # Grouped even with mistakes: all of them are told at once
        documents = self.group(parsed, errors)
        return ([], errors) if errors else (documents, [])

    def resolve(self, parsed: List[Tuple[int, Dict[str, Any]]], errors: List[str]) -> None:
        """Replace each looked-up value by its key; what is not found is an error."""
        app = kitebase.utils.get_app()
        with app.get_session() as session:
            access.guard_session(session)
            for name, column in self.columns.items():
                if not isinstance(column, Lookup):
                    continue
                values = {column.normal(cells[name]) for _, cells in parsed if name in cells}
                found = column.resolve(session, sorted(values)) if values else {}
                for n, cells in parsed:
                    if name not in cells:
                        continue
                    key = found.get(column.normal(cells[name]))
                    if key is None:
                        errors.append(_f('Row {n}, {column}: no {model} "{value}"', n=n,
                                         column=name, model=column.model, value=cells[name]))
                    elif key is AMBIGUOUS:
                        errors.append(_f('Row {n}, {column}: more than one {model} "{value}"',
                                         n=n, column=name, model=column.model, value=cells[name]))
                    if key is None or key is AMBIGUOUS:
                        del cells[name]     # already told: out of the comparisons below
                    else:
                        cells[name] = key

    def check(self, parsed: List[Tuple[int, Dict[str, Any]]]) -> List[str]:
        """A subclass's own checks on the parsed rows: the mistakes it finds."""
        return []

    def group(self, parsed: List[Tuple[int, Dict[str, Any]]], errors: List[str]
              ) -> List[Tuple[str, Dict[str, Any]]]:
        """Rows into documents, by the Key column, in the order they first appear."""
        key = next((name for name, c in self.columns.items() if isinstance(c, Key)), None)
        documents: Dict[str, Dict[str, Any]] = {}
        first_row: Dict[str, int] = {}
        for n, cells in parsed:
            label = cells.get(key, '') if key else ''
            doc = documents.setdefault(label, {'values': {}, 'children': {self.collection: []}})
            line: Dict[str, Any] = {}
            for name, column in self.columns.items():
                if column.field is None or name not in cells:
                    continue
                if column.head:
                    have = doc['values'].get(column.field)
                    if column.field in doc['values'] and have != cells[name]:
                        errors.append(_f('Row {n}, {column}: differs from row {first} of the '
                                         'same {noun}', n=n, column=name,
                                         first=first_row[label], noun=self.noun))
                        continue
                    doc['values'][column.field] = cells[name]
                else:
                    line[column.field] = cells[name]
            first_row.setdefault(label, n)
            rows = doc['children'][self.collection]
            if self.sequence:
                line[self.sequence] = (len(rows) + 1) * 10
            rows.append({'values': line})
        return list(documents.items())

    def refusal(self, errors: List[str]) -> Dict[str, Any]:
        # In the order of the sheet, not of the checks that found them
        errors = sorted(errors, key=_row_of)
        shown = errors[:self.max_errors]
        more = len(errors) - len(shown)
        detail = '\n'.join(shown + ([_f('... and {n} more', n=more)] if more else []))
        return {'status': 'error', 'code': 422,
                'message': (_f('The sheet has a mistake: nothing was imported') if len(errors) == 1
                            else _f('The sheet has {n} mistakes: nothing was imported', n=len(errors))),
                'data': {'detail': detail}}


def _row_of(error: str) -> int:
    """The row an error names, for sorting; errors naming none come last."""
    match = re.match(r'\D*(\d+)', error)
    return int(match.group(1)) if match else 1 << 30
