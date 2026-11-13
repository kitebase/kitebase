"""
References to a record of any table: a model name and a key, `('Partner', 12)`.

A plugin that links its rows to records it does not know (the mail sent about
an invoice, an activity on a partner) keeps the pair in a table of its own,
with no foreign key, and reads it back through these functions, which apply
the caller's permissions and record rules. The key is given as text or as it
is typed; it is coerced to the column's type here.

A name that is no model is a label: written and read, never opened. The
functions say so (False, None) rather than refuse.
"""
from typing import Any, Iterable, Optional, Tuple

from sqlalchemy import inspect

import kitebase.utils
from kitebase import access
from kitebase.endpoint_db import coerce_value
from kitebase.errors import NotFound
from kitebase.i18n import _f


def _find(session: Any, Model: Any, record_id: Any) -> Tuple[Any, Any]:
    key = inspect(Model).primary_key[0].key
    value = coerce_value(Model, key, record_id)
    return session.get(Model, value), value


def check_readable(model: str, record_id: Any) -> bool:
    """Raise unless the user reads the record: AccessDenied without the table's
    `read`, NotFound when the record is not there or a rule hides it. False for
    a name that is no model."""
    app = kitebase.utils.get_app()
    Model = app.models.get(model)
    if Model is None:
        return False
    access.check_table(model, 'read')
    with app.get_session() as session:
        row, value = _find(session, Model, record_id)
        if row is None or not access.row_visible(session, Model, value):
            raise NotFound(_f('No {model} {id}', model=model, id=record_id))
    return True


def any_readable(refs: Iterable[Tuple[str, Any]]) -> bool:
    """Whether the user reads at least one of the records: what makes a row
    linked to several records visible to whoever sees one of them."""
    for model, record_id in refs:
        try:
            if check_readable(str(model), record_id):
                return True
        except (access.AccessDenied, NotFound):
            continue
    return False


def label(model: str, record_id: Any) -> Optional[str]:
    """The record as a person names it: its display field, or `Model id` for a
    table without one. `<Model>.label` is enough, as for the label of a foreign
    key. None when the user may not read it, when it is not there, or when the
    name is no model: the caller decides what to show instead."""
    app = kitebase.utils.get_app()
    Model, table = app.models.get(model), app.tables.get(model)
    if Model is None or table is None:
        return None
    field = table.search_info['display_field']
    try:
        rule = access.check_read(model, [field] if field else [], main=True)
    except access.AccessDenied:
        return None
    with app.get_session() as session:
        row, value = _find(session, Model, record_id)
        if row is None or (rule is not None and not access.row_visible(session, Model, value)):
            return None
        text = getattr(row, field, None) if field else None
    return str(text) if text not in (None, '') else f'{model} {record_id}'
