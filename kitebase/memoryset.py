"""
MemorySet — typed in-memory dataset.

Server-side collection of typed rows with schema awareness.
Can be used purely server-side (batch, import, report) or round-tripped
through a client (wizard preview → confirm pattern).
"""

from __future__ import annotations
from typing import Any

from kitebase.types import PRIMITIVE_DEFAULTS


class MemorySet:
    """
    Typed in-memory list of rows, defined by a schema.

    Schema format (from plugin schemas: section):
        { field_name: {'type': 'Decimal', 'precision': 10, ...} }

    Shorthand strings are already normalised to dicts by get_schema_registry().
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = schema
        self._rows: list[dict[str, Any]] = []

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, schema_id: str) -> 'MemorySet':
        """Build from a schema declared in plugin YAML schemas: section."""
        import kitebase.utils
        app = kitebase.utils.get_app()
        registry = app.get_schema_registry()
        if schema_id not in registry:
            raise KeyError(f"MemorySet schema '{schema_id}' not found")
        return cls(registry[schema_id])

    @classmethod
    def from_dict(cls, schema: dict[str, Any]) -> 'MemorySet':
        """Build from an inline schema dict (no YAML lookup needed)."""
        return cls(schema)

    # ── Row operations ────────────────────────────────────────────────────────

    def _default_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for field, info in self._schema.items():
            type_name = info.get('type', 'String') if isinstance(info, dict) else str(info)
            row[field] = PRIMITIVE_DEFAULTS.get(type_name)
        return row

    def add(self, **kwargs) -> dict[str, Any]:
        """
        Append a row and return it as a mutable dict reference.

        Missing fields receive schema defaults. The returned dict is the live
        row inside the MemorySet — direct mutations are reflected immediately:

            row = ms.add()
            row['code'] = 'C001'
            row['balance'] = 1500.0
        """
        row = self._default_row()
        row.update(kwargs)
        self._rows.append(row)
        return row

    def get(self, idx: int, field: str | None = None) -> Any:
        """Return row dict at idx, or a specific field value."""
        row = self._rows[idx]
        return row if field is None else row.get(field)

    def set(self, idx: int, field: str, value: Any) -> None:
        """Set a field value on an existing row."""
        self._rows[idx][field] = value

    def reload_data(self, rows: list[dict[str, Any]]) -> 'MemorySet':
        """Replace rows keeping the current schema. Useful after client roundtrip."""
        self._rows = [dict(r) for r in rows]
        return self

    def sort(self, field: str, ascending: bool = True) -> 'MemorySet':
        """Sort rows in place by a field value. None values always sort last."""
        nones = [r for r in self._rows if r.get(field) is None]
        valued = [r for r in self._rows if r.get(field) is not None]
        valued.sort(key=lambda r: r[field], reverse=not ascending)
        self._rows = valued + nones
        return self

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    # ── Serialization → client ────────────────────────────────────────────────

    def to_list(self) -> list[dict[str, Any]]:
        """Return rows as plain dicts for JSON response."""
        return [dict(row) for row in self._rows]

    def to_schema(self) -> dict[str, Any]:
        """Return schema for the client (DataView/DataForm data_schema)."""
        return dict(self._schema)

    # ── Reconstruction from client ────────────────────────────────────────────

    @classmethod
    def from_list(cls, rows: list[dict[str, Any]], schema_id: str) -> 'MemorySet':
        """Reconstruct from rows sent back by the client."""
        ms = cls.from_yaml(schema_id)
        ms._rows = [dict(r) for r in rows]
        return ms

    # ── Selection ─────────────────────────────────────────────────────────────

    def selected(self) -> 'MemorySet':
        """Return a new MemorySet with only rows where _selected is truthy."""
        ms = MemorySet(self._schema)
        ms._rows = [r for r in self._rows if r.get('_selected')]
        return ms

    # ── Validation ────────────────────────────────────────────────────────────

    def validate_row(self, row: dict[str, Any], db) -> list[str]:
        """
        Validate a single row dict against schema constraints.

        Only schema fields are checked — extra columns in the row are ignored.
        Returns a list of error messages (empty = valid).
        Can be called on any dict, not just rows already in _rows.
        """
        import kitebase.utils
        app = kitebase.utils.get_app()
        errors: list[str] = []

        for field, info in self._schema.items():
            if not isinstance(info, dict) or info.get('type') != 'FK':
                continue
            value = row.get(field)
            if value is None:
                continue
            table_name = info.get('table')
            if not table_name:
                continue
            model = app.models.get(table_name)
            db_table = app.tables.get(table_name)
            if not model or not db_table:
                continue
            pk = db_table.pk_fields[0] if db_table.pk_fields else 'id'
            if not db.query(model).filter(getattr(model, pk) == value).first():
                errors.append(f"{field} = {value!r} not found in {table_name}")

        return errors

    def validate(self, db) -> list[str]:
        """
        Validate all rows against schema constraints.
        Returns a list of error messages (empty list = valid).
        Extra columns in each row are ignored — only schema fields are checked.
        """
        errors: list[str] = []
        for i, row in enumerate(self._rows):
            for msg in self.validate_row(row, db):
                errors.append(f"Row {i + 1}: {msg}")
        return errors
