"""Reading and writing an aggregate — a record and the collections it owns.

The client sends a page id and a list of operations; the page's descriptor says
which tables those operations may touch (`kitebase.pages`). Nothing here trusts
the payload for a table name, and that is the whole point of the seam: the tree
is a fact of the application, not of the request.

Two verbs, symmetric, and both recursive from the start — a function that calls
itself costs no more than one that does not, and grandchildren then work the day
a form shows them:

- **load** walks down by level, one query per collection per level with
  `IN (parent ids)`, and stitches the rows in memory. Four levels of three
  collections stay a dozen queries.
- **save** walks down by node, because the ids of new rows do not exist until
  they are written: the parent, then `flush()` which assigns its key without
  closing the transaction, and only then do the children have a value for their
  foreign key. Deletions run the other way — grandchild, child, parent — collected
  during the descent and applied by decreasing depth, because our foreign keys may
  be soft and the database cascades nothing there.

One `commit`, at the top. A failure at depth three leaves nothing written
anywhere.

Temporary ids are negative integers, and the rule is one line: *any* negative
value in a foreign key column is resolved through `id_map` before writing. That
costs three lines and gives references between siblings created in the same
transaction for free.

A node carries an `op` only where the user acted. Without one it is a
**pass-through** — an untouched row that is in the payload because something below
it changed, and the way down runs through it. Nothing is written, the row is
verified against the parent it claims, and the descent continues. That is what
keeps `load_tree`'s answer and `save_tree`'s request one shape.

`db` stays what it is — a single record, one call, its own transaction. Whether
this becomes the only write path is a decision for the day the client moves.
"""
from typing import Any, Dict, List, Optional, Tuple

import kitebase.utils
from kitebase.endpoints import endpoint
from kitebase.endpoint_db import pk_field, write_values, coerce_value
from kitebase.pages import Aggregate, Collection, page_aggregate
from kitebase.querybuilder import DynamicQueryBuilder

_OPS = ('create', 'update', 'delete')


def _conditions_of(domain: Any) -> List[Any]:
    """Normalize a domain to a list of querybuilder conditions.

    `domain` shares the querybuilder syntax with `filters`, so it may be a bare
    condition, a list of them, or `{conditions: [...]}`. A wrapper that carries
    its own boundary — `{op: 'or', conditions: [...]}` — is one condition, and
    unwrapping it would spill its branches into the surrounding AND.
    """
    if domain is None or domain == '' or domain == [] or domain == {}:
        return []
    if isinstance(domain, list):
        return list(domain)
    if isinstance(domain, dict):
        if 'conditions' in domain and 'op' not in domain:
            inner = domain['conditions']
            return list(inner) if isinstance(inner, list) else [inner]
        return [domain]
    raise ValueError(f"A domain must be a condition or a list of them, got {type(domain).__name__}")


def _table_of(app: Any, model_name: str):
    """Return (model class, table definition, pk name) for a model, or raise."""
    model_class = app.find_model_class(model_name)
    if model_class is None:
        raise ValueError(f"Table '{model_name}' not found")
    db_table = app.tables.get(model_name)
    return model_class, db_table, pk_field(db_table)


def _link_of(app: Any, parent_model: str, coll: Collection) -> str:
    """The column of the parent that a collection's `fk` holds.

    The one its foreign key targets on the parent's table — a unique code or a
    uuid the child already cited before the parent was written — and otherwise
    the parent's primary key. Either way the value exists after the parent's
    flush, so creating parent and children together is one gesture.
    """
    _, parent_table, pk = _table_of(app, parent_model)
    child_table = app.tables.get(coll.model)
    for col in (child_table.effective_columns if child_table else []):
        if col.name != coll.fk:
            continue
        target = col.attributes.get('foreign_key') or {}
        if target.get('table') is parent_table and target.get('id'):
            return target['id']
    return pk


# ── Load ────────────────────────────────────────────────────────────────────

def _select(app, session, model_name: str, conditions: List[Any],
            order_by: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Run one query and return its rows as dicts.

    Goes through the querybuilder rather than the ORM because a collection's
    `domain` is written in querybuilder syntax, and because the `*` expansion
    there already drops the columns a table declares secret.

    `resolve: true` on every query of the tree, root and collections alike. The
    flag means "I am resolving something already stored, not proposing what is
    acceptable now", and an aggregate somebody opened by key is exactly that: its
    contents are stored facts, not a picklist. Under a behavior that scopes a list
    — Archivable — a browse list rightly hides archived rows, while a record must
    open whole: half an aggregate is not a smaller aggregate, it is a wrong one,
    and a row invisible in the only place it can be edited can never be restored.
    The view's `domain` still applies: that is a filter, not a behavior.
    """
    builder = DynamicQueryBuilder(session, app.models)
    query_def: Dict[str, Any] = {'table': model_name, 'resolve': True}
    if conditions:
        query_def['filters'] = {'conditions': conditions}
    if order_by:
        query_def['order_by'] = order_by
    return builder.execute_query(query_def, result_format='records')


def _load_level(app, session, parent_model: str, collections: Dict[str, Collection],
                parents: Dict[Any, Dict[str, Any]]) -> None:
    """Attach one level of children to `parents` ({parent id: node}), then recurse."""
    if not parents or not collections:
        return

    for cid, coll in collections.items():
        _, db_table, pk = _table_of(app, coll.model)
        link = _link_of(app, parent_model, coll)
        by_link = {node['values'].get(link): node for node in parents.values()}
        by_link.pop(None, None)
        conditions = [{coll.fk: ['in', list(by_link)]}] + _conditions_of(coll.domain)
        rows = _select(app, session, coll.model, conditions,
                       coll.order_by or [f'{coll.model}.{pk}'])

        # The key exists even when empty: a collection with no rows is a fact,
        # and the client should not have to tell it from a collection it forgot.
        for node in parents.values():
            node['children'].setdefault(cid, [])

        children: Dict[Any, Dict[str, Any]] = {}
        for row in rows:
            parent = by_link.get(row.get(coll.fk))
            if parent is None:      # a row the domain let through under another parent
                continue
            child = {'id': row.get(pk), 'values': row, 'children': {}}
            parent['children'][cid].append(child)
            children[child['id']] = child

        _load_level(app, session, coll.model, coll.collections, children)


def _load_tree(app, session, aggregate: Aggregate, record_id: Any) -> Optional[Dict[str, Any]]:
    """Load the root record and everything below it, or None if it does not exist.

    One path for the whole tree — see `_select` for why it resolves rather than
    browses. Fetching the root by key through the ORM instead would answer the same
    question, but it would serialize it differently: `serialize_model` hands back a
    `date` where the querybuilder hands back its ISO form, and two spellings of the
    same kind of value in one payload is a trap for whoever reads it.
    """
    _, db_table, pk = _table_of(app, aggregate.model)
    rows = _select(app, session, aggregate.model, [{pk: record_id}], None)
    if not rows:
        return None

    root = {'id': rows[0].get(pk), 'values': rows[0], 'children': {}}
    _load_level(app, session, aggregate.model, aggregate.collections, {root['id']: root})
    return root


@endpoint('load_tree')
def load_tree(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Load a record and the collections its page declares, as one tree.

    Parameters:
        page: page id — the descriptor that declares the tree
        id:   primary key of the root record

    Returns:
        { status, data: {id, values, children: {collection_id: [node, ...]}}, code }

    Every node has the same shape, at any depth. It is the shape `save_tree`
    accepts, minus the `op` the client adds when the user changes something.
    """
    page_id = data.get('page')
    if not page_id:
        return {'status': 'error', 'message': 'page is required', 'code': 400}
    if 'id' not in data or data['id'] is None:
        return {'status': 'error', 'message': 'id is required', 'code': 400}

    app = kitebase.utils.get_app()
    try:
        aggregate = page_aggregate(app, page_id)
        with app.get_session() as session:
            root = _load_tree(app, session, aggregate, data['id'])
    except ValueError as e:
        return {'status': 'error', 'message': str(e), 'code': 400}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'status': 'error', 'message': str(e), 'code': 500}

    if root is None:
        return {'status': 'error',
                'message': f"No record {data['id']} in '{aggregate.model}'", 'code': 404}

    return {'status': 'success', 'data': root, 'code': 200}


# ── Save ────────────────────────────────────────────────────────────────────

def _resolve_temp_ids(model_name: str, db_table, values: Dict[str, Any],
                      id_map: Dict[int, Any]) -> Dict[str, Any]:
    """Replace negative foreign key values with the ids their rows just received."""
    if db_table is None:
        return values

    fks = {col.name for col in db_table.effective_columns
           if col.attributes.get('foreign_key')}

    resolved = {}
    for key, value in values.items():
        if key in fks and isinstance(value, int) and not isinstance(value, bool) and value < 0:
            if value not in id_map:
                raise ValueError(
                    f"'{model_name}.{key}' points at the temporary id {value}, which "
                    f"nothing in this save creates before it")
            value = id_map[value]
        resolved[key] = value
    return resolved


def _require_saved_id(model_name: str, node_id: Any, op: str) -> None:
    """An update or a delete addresses a row that exists: a real, positive key."""
    if not isinstance(node_id, int) or isinstance(node_id, bool) or node_id < 0:
        raise ValueError(
            f"A {op} on '{model_name}' needs the id of a saved row, got {node_id!r}")


def _passthrough_row(app, session, node_def, node_id: Any,
                     inherited: Dict[str, Any]) -> Any:
    """Verify an untouched row and hand its key down to its children.

    A node without an `op` writes nothing: it is in the payload only because
    something below it changed, and the path to a grandchild runs through it. That
    is why the row is *checked* instead of written — the foreign key the parent
    supplies is never applied here, so without the check a row belonging to another
    parent would let children be grafted onto an aggregate nobody opened.
    """
    _require_saved_id(node_def.model, node_id, 'pass-through')

    model_class, _, _ = _table_of(app, node_def.model)
    obj = session.get(model_class, node_id)
    if obj is None:
        raise ValueError(f"No record {node_id} in '{node_def.model}'")

    for key, value in inherited.items():
        current = getattr(obj, key, None)
        if current != value:
            raise ValueError(
                f"Row {node_id} of '{node_def.model}' has '{key}' = {current!r}, but "
                f"the collection it appears in belongs to {value!r} — an unchanged "
                f"row is verified, never reparented")

    return obj


def _save_node(app, session, node_def, node: Dict[str, Any],
               inherited: Dict[str, Any], defaults: Dict[str, Any],
               id_map: Dict[int, Any], deletes: List[Tuple[int, str, Any]],
               depth: int) -> Optional[Any]:
    """Write one node and descend into its collections. Returns the row's real id.

    `node_def` is an `Aggregate` or a `Collection` — both carry the model and the
    collections below it, which is why the same function serves the root and every
    child. `inherited` is what the parent supplies (the foreign key, and one day
    values like the document's currency); `defaults` are stamped on creation only.
    """
    model_class, db_table, pk = _table_of(app, node_def.model)

    op = str(node.get('op') or '').lower()
    if op and op not in _OPS:
        raise ValueError(f"Unknown operation '{node.get('op')}' on '{node_def.model}'")

    node_id = node.get('id')

    if not op:
        # An untouched row: nothing to write, but the descent continues, because
        # a node carries an `op` only where the user acted. That is what lets the
        # buffer and the payload keep one shape — the client prunes what it did
        # not touch, and what it keeps is what it holds.
        obj = _passthrough_row(app, session, node_def, node_id, inherited)
        _save_children(app, session, node_def, node, obj, id_map, deletes, depth)
        return node_id

    if op == 'delete':
        _require_saved_id(node_def.model, node_id, 'delete')
        deletes.append((depth, node_def.model, node_id))
        # No id to hand down, but the descent continues: a grandchild's deletion
        # is collected here and applied before this row goes.
        _save_children(app, session, node_def, node, None, id_map, deletes, depth)
        return None

    values = dict(node.get('values') or {})

    # Defaults fill what the row leaves unsaid: they are the other half of the
    # domain, and a row created under a filter must satisfy it.
    if op == 'create':
        for key, value in (defaults or {}).items():
            values.setdefault(key, value)

    # Before anything compares foreign keys: a child buffered under a brand-new
    # parent carries the parent's temporary id, and that is the value the parent
    # has just turned into a real one.
    values = _resolve_temp_ids(node_def.model, db_table, values, id_map)

    # What the parent writes is not negotiable. Silence would be a reparenting
    # the caller believed it had performed.
    for key, value in inherited.items():
        if key in values and values[key] != value:
            raise ValueError(
                f"A row of '{node_def.model}' sets '{key}' to {values[key]!r}, but the "
                f"collection it belongs to writes {value!r} there")
        values[key] = value

    values = write_values(db_table, values)
    values = {k: coerce_value(model_class, k, v) for k, v in values.items()}

    if op == 'create':
        # A key that the database assigns is not sent; one the user types (a code,
        # a short id) is, and stays.
        if pk in values and (values[pk] is None
                             or (isinstance(values[pk], int) and values[pk] < 0)):
            del values[pk]

        obj = model_class(**values)
        session.add(obj)
        session.flush()                     # the key, without closing the transaction
        real_id = getattr(obj, pk)
        if isinstance(node_id, int) and not isinstance(node_id, bool) and node_id < 0:
            # The map is global to the payload — that is what lets a row point at
            # a sibling created beside it — so the client allocates temporary ids
            # from one counter. Two tables reusing a number would silently graft
            # one tree onto another.
            if node_id in id_map:
                raise ValueError(
                    f"Temporary id {node_id} is used twice in this save; they are "
                    f"unique across the whole payload, not per collection")
            id_map[node_id] = real_id

    else:   # update
        _require_saved_id(node_def.model, node_id, 'update')
        if pk in values and values[pk] != node_id:
            raise ValueError(
                f"An update on '{node_def.model}' cannot move the row from {node_id} "
                f"to {values[pk]!r}")

        obj = session.get(model_class, node_id)
        if obj is None:
            raise ValueError(f"No record {node_id} in '{node_def.model}'")
        for key, value in values.items():
            if key != pk and hasattr(obj, key):
                setattr(obj, key, value)
        session.flush()
        real_id = node_id

    _save_children(app, session, node_def, node, obj, id_map, deletes, depth)
    return real_id


def _save_children(app, session, node_def, node: Dict[str, Any], parent: Optional[Any],
                   id_map: Dict[int, Any], deletes: List[Tuple[int, str, Any]],
                   depth: int) -> None:
    """Descend into the collections of a node, refusing any the page does not declare.

    `parent` is the parent's row, None when it is being deleted. Below a row that goes away
    only deletions make sense: a row added under it would be an orphan the caller
    believed it had created.
    """
    for cid, rows in (node.get('children') or {}).items():
        coll = node_def.collections.get(cid)
        if coll is None:
            raise ValueError(
                f"'{node_def.model}' declares no collection '{cid}' on this page")
        if not isinstance(rows, list):
            raise ValueError(f"Collection '{cid}' must carry a list of rows")

        for child in rows:
            if parent is None and str(child.get('op') or '').lower() != 'delete':
                raise ValueError(
                    f"A row of '{coll.model}' hangs from a deleted row of "
                    f"'{node_def.model}': below a row that goes away, only deletions")
            inherited = ({} if parent is None
                         else {coll.fk: getattr(parent, _link_of(app, node_def.model, coll))})
            _save_node(app, session, coll, child, inherited, coll.defaults,
                       id_map, deletes, depth + 1)


@endpoint('save_tree')
def save_tree(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Write a record and the collections its page declares, in one transaction.

    Parameters:
        page: page id — the descriptor that declares what may be written
        root: the root node
              {op: create|update|delete, id, values: {...},
               children: {collection_id: [node, ...]}}

    A node's `id` is the primary key for `update` and `delete`, and a negative
    temporary id for `create`. The client sends the operations it performed, not
    a before/after pair: a row deleted and re-inserted is indistinguishable from
    a modified one once it has been reduced to a difference.

    A node with no `op` is a pass-through: an untouched row on the way to a
    descendant that did change. The root normally carries `update` even when its
    own fields did not move — the optimistic lock will want that touch (see
    `relations.md §8`) — but an intermediate row has no such reason to be written.

    Returns:
        { status, data: {id, id_map, root}, code }

    `id_map` maps each temporary id to the key its row received (JSON turns the
    keys into strings), and `root` is the tree re-read after the commit, so the
    client sees what the database actually holds — defaults, stamps and all.
    """
    page_id = data.get('page')
    if not page_id:
        return {'status': 'error', 'message': 'page is required', 'code': 400}

    root_node = data.get('root')
    if not isinstance(root_node, dict):
        return {'status': 'error', 'message': 'root must be a node', 'code': 400}

    app = kitebase.utils.get_app()
    try:
        aggregate = page_aggregate(app, page_id)

        id_map: Dict[int, Any] = {}
        deletes: List[Tuple[int, str, Any]] = []

        with app.get_session() as session:
            root_id = _save_node(app, session, aggregate, root_node, {}, {},
                                 id_map, deletes, 0)

            # Backwards: a grandchild before its child, a child before its parent.
            for _depth, model_name, record_id in sorted(deletes, key=lambda d: -d[0]):
                model_class, _, _ = _table_of(app, model_name)
                obj = session.get(model_class, record_id)
                if obj is None:
                    raise ValueError(f"No record {record_id} to delete in '{model_name}'")
                session.delete(obj)

            session.commit()

            root = _load_tree(app, session, aggregate, root_id) if root_id is not None else None

        return {'status': 'success',
                'data': {'id': root_id, 'id_map': id_map, 'root': root},
                'code': 200}

    except ValueError as e:
        return {'status': 'error', 'message': str(e), 'code': 400}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'status': 'error', 'message': str(e), 'code': 500}
