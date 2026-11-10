"""
Duplicate: a saved document read back as a new draft, to be finished in its form.

    commands:
      - id: duplicate
        label: Duplicate
        scope: row
        endpoint: duplicate
        params: { page: purchaseorder_form }

The record is read with the collections its page declares, as `load_tree`
reads it, and becomes a tree with no keys. What is not carried over takes
the column's default in the form, as on any new record:

- the primary key, and the link of a row to its parent;
- a column unique by itself: a copy could only be refused;
- a column that says `duplicate: false` (a timestamp, the supplier's
  reference). `duplicate: true` on a column carries it over regardless,
  except the key.

The person is asked first: "Duplicate #3?", or the options view when the
page has one, which is the question itself. `confirm: false` in the
command's params opens the draft without asking.

A page may register a duplicator, which changes the tree before the form
opens it, with an options view to ask through, as a print does, and the
first values of that view computed from the record:

    @duplicator('purchaseorder_form', options='purchaseorder_duplicate')
    def order_duplicate(tree, choices): ...

    @order_duplicate.options_values
    def order_duplicate_values(source): return {'order_date': ...}

The answer is `open_form`: nothing is written until the person saves.
"""
from typing import Any, Callable, Dict, Optional, Tuple

import kitebase
from kitebase import access
from kitebase.endpoints import endpoint
from kitebase.i18n import _, _f
from kitebase.registry import Registry, owner_of


class _Duplicator:
    def __init__(self, fn: Callable, options: Optional[str]):
        self.fn, self.options = fn, options
        self.values: Optional[Callable] = None


_DUPLICATORS: Registry[_Duplicator] = Registry('duplicators', 'duplicator of page')


def duplicator(page: str, options: Optional[str] = None):
    """
    Register what a page changes in a duplicate: `fn(tree, choices)`, the
    tree as the form will open it and the values of the options view (empty
    without one). One per page; a module reloaded replaces its own.

    `fn.options_values` registers `values(source) -> dict`, the first values
    of the options view computed from the record copied, as it was read
    (keys and all), over the view's defaults.
    """
    def register(fn: Callable) -> Callable:
        entry = _Duplicator(fn, options)
        _DUPLICATORS.add(page, entry, owner=owner_of(fn))

        def options_values(values: Callable) -> Callable:
            entry.values = values
            return values
        fn.options_values = options_values
        return fn
    return register


def carried(column) -> bool:
    """Whether a duplicate takes this column's value."""
    attrs = column.attributes
    if attrs.get('primary_key') or attrs.get('virtual'):
        return False
    if 'duplicate' in attrs:
        return bool(attrs['duplicate'])
    return not attrs.get('unique')


def duplicate_tree(page_id: str, record_id: Any
                   ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    The record as it was read, and the same as a tree with no keys: what the
    form opens. (None, None) if it is not there.
    """
    from kitebase.endpoint_tree import _link_of, _load_tree
    from kitebase.pages import page_aggregate

    app = kitebase.utils.get_app()
    aggregate = page_aggregate(app, page_id)
    access.check_table(aggregate.model, 'create')
    with app.get_session() as session:
        access.guard_session(session)
        root = _load_tree(app, session, aggregate, record_id)
    if root is None:
        return None, None

    def strip(node, model, collections, link=None):
        table = app.tables[model]
        keep = {c.name for c in table.effective_columns if carried(c)} - {link}
        children = {}
        for cid, coll in collections.items():
            children[cid] = [strip(row, coll.model, coll.collections, coll.fk)
                             for row in node.get('children', {}).get(cid, [])]
        return {'values': {k: v for k, v in node['values'].items() if k in keep},
                'children': children}

    return root, strip(root, aggregate.model, aggregate.collections)


@endpoint('duplicate', access=access.PER_TABLE)
def duplicate(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    A row of a list, opened as a new document. Parameters: `page`, the form
    of the document; `id`, the row; `confirm: false` not to ask. The first
    call asks (a confirmation, or the options view), the second carries the
    answer back with `$asked`.
    """
    page_id, record_id = data.get('page'), data.get('id')
    if not page_id or record_id is None:
        return {'status': 'error', 'code': 400, 'message': 'page and id are required'}

    custom = _DUPLICATORS.get(page_id)
    try:
        source, tree = duplicate_tree(page_id, record_id)
    except ValueError as e:
        return {'status': 'error', 'code': 400, 'message': str(e)}
    if tree is None:
        return {'status': 'error', 'code': 404, 'message': _f('No record {id}', id=record_id)}

    if not data.get('$asked'):
        again = {'action': 'endpoint', 'op': 'duplicate',
                 'params': {'page': page_id, 'id': record_id, '$asked': True}}
        if custom and custom.options:
            return _ask_options(page_id, custom, source, again)
        if data.get('confirm', True):
            return {'status': 'success',
                    'data': {**again, 'confirm': _f('Duplicate #{id}?', id=record_id)}}

    if custom:
        custom.fn(tree, {k: v for k, v in data.items() if k not in _CALL_KEYS})
    return {'status': 'success', 'data': {
        'action': 'open_form', 'page': page_id, 'draft': tree,
        'title': _f('Duplicate of #{id}', id=record_id)}}


# What reaches the endpoint besides the choices: the call, and the list's situation
_CALL_KEYS = {'page', 'id', 'confirm', '$asked', 'table', 'query_params', 'ids', 'view_ids'}


def _ask_options(page_id: str, custom: _Duplicator, source: Dict[str, Any],
                 again: Dict[str, Any]) -> Dict[str, Any]:
    """The options view as the question, its first values computed from the record."""
    from kitebase.flow import form_view
    try:
        view, values = form_view(custom.options)
    except ValueError as e:
        return {'status': 'error', 'code': 500, 'message': f"{e} (duplicate of '{page_id}')"}
    if custom.values:
        values.update(custom.values(source) or {})
    return {'status': 'success', 'data': {
        'action': 'form', 'title': _('Duplicate'), 'view': view, 'values': values,
        'then': again}}
