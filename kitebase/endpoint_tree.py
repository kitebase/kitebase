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

Two hooks let a plugin take part in both verbs without the core knowing why
(the concurrency plugin is the first to use them):

    @before_tree_load
    def version(model, record_id):          # before the data is read
        return {'$version': ...}            # `$` keys, set on the root node

    @before_tree_write
    def check(write):                       # in the save's transaction, before the rows
        if stale(write.meta):
            raise Conflict(...)             # 409, with the tree as it is now
        return {'$version': ...}            # set on the root that is answered

A tree that leaves the server carries what the load hooks said, and a root that
comes back carries it again in its own `$` keys: the client keeps them without
reading them. A `Conflict` raised by a write hook rolls back the whole save
and answers with the tree re-read, so the client can show what changed.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import kitebase.utils
import kitebase.media
import kitebase.refsets
from kitebase import access, flow
from kitebase.access import AccessDenied
from kitebase.errors import Conflict, Invalid, NotFound, Refusal
from kitebase.endpoints import endpoint
from kitebase.endpoint_db import (pk_field, write_values, coerce_value, require_columns,
                                  refuse_duplicate_key, refuse_key_change, refuse_referenced,
                                  _table_label)
from kitebase.i18n import _f
from kitebase.pages import Aggregate, Collection, page_aggregate
from kitebase.querybuilder import DynamicQueryBuilder
from kitebase.registry import Registry

_OPS = ('create', 'update', 'delete')


# ── Hooks ───────────────────────────────────────────────────────────────────

@dataclass
class TreeWrite:
    """What a write hook sees of one tree about to be saved."""
    session: Any                     # the save's: what a hook writes commits with the tree
    model: str                       # the root's model
    id: Any                          # the root's key; on a create, its temporary id
    action: str                      # create, update or delete, as the root is written
    meta: Dict[str, Any] = field(default_factory=dict)   # the `$` keys of the root node


_LOAD_HOOKS: Registry[Callable[[str, Any], Optional[Dict[str, Any]]]] = Registry(
    'tree_load_hooks', 'load hook of trees')
_WRITE_HOOKS: Registry[Callable[[TreeWrite], Optional[Dict[str, Any]]]] = Registry(
    'tree_write_hooks', 'write hook of trees')


def _hook_name(fn: Callable) -> str:
    return f'{fn.__module__}.{fn.__qualname__}'


def before_tree_load(fn: Callable) -> Callable:
    """
    Register `fn(model, record_id)`, called when a tree is read for the client,
    before its data. It returns `$` keys for the root node, or None.
    """
    _LOAD_HOOKS.add(_hook_name(fn), fn)
    return fn


def before_tree_write(fn: Callable) -> Callable:
    """
    Register `fn(write: TreeWrite)`, called inside the save's transaction
    before any row of the tree is written, once the access to the root is
    checked: what it writes or locks comes first. It may raise `Conflict` to
    refuse the save, and returns `$` keys for the root it answers with, or None.
    """
    _WRITE_HOOKS.add(_hook_name(fn), fn)
    return fn


def _metadata(found: Optional[Dict[str, Any]], hook: Callable) -> Dict[str, Any]:
    """What a hook returned: `$` keys only, so a hook never writes over the node."""
    if not found:
        return {}
    wrong = [k for k in found if not str(k).startswith('$')]
    if wrong:
        raise ValueError(f"The tree hook {_hook_name(hook)} returned keys without '$': "
                         f"{', '.join(map(str, wrong))}")
    return dict(found)


def _merged(results) -> Dict[str, Any]:
    """The keys of every hook together; two hooks returning one key is an error,
    or the import order would choose between them in silence."""
    meta: Dict[str, Any] = {}
    owners: Dict[str, Callable] = {}
    for hook, found in results:
        for key, value in _metadata(found, hook).items():
            if key in owners:
                raise ValueError(f"The tree hooks {_hook_name(owners[key])} and "
                                 f"{_hook_name(hook)} both return '{key}'")
            owners[key] = hook
            meta[key] = value
    return meta


def _load_metadata(model: str, record_id: Any) -> Dict[str, Any]:
    return _merged((hook, hook(model, record_id)) for hook in _LOAD_HOOKS.values())


def _write_metadata(write: TreeWrite) -> Dict[str, Any]:
    return _merged((hook, hook(write)) for hook in _WRITE_HOOKS.values())


def _node_metadata(node: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in node.items() if isinstance(k, str) and k.startswith('$')}


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
            order_by: Optional[List[Any]], covered: List[str] = ()) -> List[Dict[str, Any]]:
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

    `covered` names the models whose read permission is already answered: the
    rows of a collection follow their root, and have no permission of their own.
    """
    builder = DynamicQueryBuilder(session, app.models, covered=list(covered),
                                  value_transform=kitebase.media.query_transform(app))
    query_def: Dict[str, Any] = {'table': model_name, 'resolve': True}
    if conditions:
        query_def['filters'] = {'conditions': conditions}
    if order_by:
        query_def['order_by'] = order_by
    rows = builder.execute_query(query_def, result_format='records')
    kitebase.refsets.attach(app, session, app.tables.get(model_name), rows)
    return rows


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
        # A declared sequence is the order; the key settles rows written together
        order = ([f'{coll.model}.{coll.sequence}', f'{coll.model}.{pk}'] if coll.sequence
                 else coll.order_by or [f'{coll.model}.{pk}'])
        rows = _select(app, session, coll.model, conditions, order, covered=[coll.model])

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


def _aggregate_models(aggregate: Aggregate) -> List[str]:
    """The root model and every collection model below it."""
    found = [aggregate.model]
    stack = list(aggregate.collections.values())
    while stack:
        coll = stack.pop()
        found.append(coll.model)
        stack.extend(coll.collections.values())
    return found


def _owned(app: Any, coll: Collection) -> bool:
    """Whether the rows of `coll` are parts of their parent: `owned` on the
    foreign key, or a link table. The parent answers for its parts; the rows
    of a collection that is not owned have a life, and permissions, of their
    own."""
    table = app.tables.get(coll.model)
    if table is None:
        return False
    if table.attributes.get('many_to_many'):
        return True
    column = next((c for c in table.effective_columns if c.name == coll.fk), None)
    foreign_key = column.attributes.get('foreign_key') if column is not None else None
    return isinstance(foreign_key, dict) and foreign_key.get('owned') is True


def _cascaded_models(app: Any, aggregate: Aggregate, root_node: Dict[str, Any]) -> List[str]:
    """
    The models whose rows go with a deleted row through the ORM composition,
    unseen here: every owned collection declared below a row the tree deletes.
    Their rules would be skipped, so a table with rules among them refuses
    the tree. A collection that is not owned loses only its reference.
    """
    found: List[str] = []

    def below(node_def) -> None:
        for coll in node_def.collections.values():
            if _owned(app, coll):
                found.append(coll.model)
                below(coll)

    def walk(node_def, node: Dict[str, Any]) -> None:
        for cid, rows in (node.get('children') or {}).items():
            coll = node_def.collections.get(cid)
            if coll is None or not isinstance(rows, list):
                continue  # refused with its own message when the rows are saved
            for row in rows:
                if str(row.get('op') or '').lower() == 'delete':
                    below(coll)
                walk(coll, row)

    walk(aggregate, root_node)
    if str(root_node.get('op') or '').lower() == 'delete':
        below(aggregate)
    return found


def _root_action(root_node: Dict[str, Any]) -> str:
    """What writing a tree asks of its root: a change below it modifies it."""
    return {'create': 'create', 'delete': 'delete'}.get(root_node.get('op'), 'update')


def _load_tree(app, session, aggregate: Aggregate, record_id: Any,
               covered: List[str] = ()) -> Optional[Dict[str, Any]]:
    """Load the root record and everything below it, or None if it does not exist.

    One path for the whole tree — see `_select` for why it resolves rather than
    browses. Fetching the root by key through the ORM instead would answer the same
    question, but it would serialize it differently: `serialize_model` hands back a
    `date` where the querybuilder hands back its ISO form, and two spellings of the
    same kind of value in one payload is a trap for whoever reads it.
    """
    _, db_table, pk = _table_of(app, aggregate.model)
    rows = _select(app, session, aggregate.model, [{pk: record_id}], None, covered)
    if not rows:
        return None

    root = {'id': rows[0].get(pk), 'values': rows[0], 'children': {}}
    _load_level(app, session, aggregate.model, aggregate.collections, {root['id']: root})
    return root


@endpoint('load_tree', access=access.PER_TABLE)
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
        raise Invalid('page is required')
    if 'id' not in data or data['id'] is None:
        raise Invalid('id is required')

    app = kitebase.utils.get_app()
    aggregate = page_aggregate(app, page_id)
    # The hooks may write (a marker): only for whoever may read the record
    access.check_table(aggregate.model, 'read')
    root = _read_for_client(app, aggregate, data['id'])
    if root is None:
        raise NotFound(f"No record {data['id']} in '{aggregate.model}'")

    return {'status': 'success', 'data': root, 'code': 200}


def _read_for_client(app, aggregate: Aggregate, record_id: Any,
                     covered: List[str] = ()) -> Optional[Dict[str, Any]]:
    """
    A tree as it leaves the server: the load hooks first, then the data. The
    order is the hooks' guarantee: what they say (a version) is never newer
    than the rows read after it.
    """
    meta = _load_metadata(aggregate.model, record_id)
    with app.get_session() as session:
        root = _load_tree(app, session, aggregate, record_id, covered)
    if root is not None:
        root.update(meta)
    return root


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
                raise Invalid(
                    f"'{model_name}.{key}' points at the temporary id {value}, which "
                    f"nothing in this save creates before it")
            value = id_map[value]
        resolved[key] = value
    return resolved


def _is_saved_id(node_id: Any) -> bool:
    """A key the database holds: anything but nothing, a flag, or the negative
    integer the client mints for a row not yet written. A typed key (a code)
    is a saved id as much as an assigned one."""
    if node_id is None or node_id == '' or isinstance(node_id, (bool, dict, list)):
        return False
    return not (isinstance(node_id, int) and node_id < 0)


def _require_saved_id(model_name: str, node_id: Any, op: str) -> None:
    """An update or a delete addresses a row that exists: a key, not a temporary id."""
    if not _is_saved_id(node_id):
        raise Invalid(
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
        raise NotFound(f"No record {node_id} in '{node_def.model}'")

    for key, value in inherited.items():
        current = getattr(obj, key, None)
        if current != value:
            raise Invalid(
                f"Row {node_id} of '{node_def.model}' has '{key}' = {current!r}, but "
                f"the collection it appears in belongs to {value!r} — an unchanged "
                f"row is verified, never reparented")

    return obj


def _save_node(app, session, node_def, node: Dict[str, Any],
               inherited: Dict[str, Any], defaults: Dict[str, Any],
               id_map: Dict[int, Any], deletes: List[Tuple[int, str, Any, Any, bool]],
               depth: int, judged: Dict[int, Any]) -> Optional[Any]:
    """Write one node and descend into its collections. Returns the row's real id.

    `node_def` is an `Aggregate` or a `Collection` — both carry the model and the
    collections below it, which is why the same function serves the root and every
    child. `inherited` is what the parent supplies (the foreign key, and one day
    values like the document's currency); `defaults` are stamped on creation only.
    `judged` holds the rules' contexts of the rows, by node: their `commit`
    rules run once the row and the rows below it are written.

    A row of a collection that is not owned is checked against its own
    table's row conditions, as the root is (`_check_rows` for the table's).
    """
    model_class, db_table, pk = _table_of(app, node_def.model)

    op = str(node.get('op') or '').lower()
    if op and op not in _OPS:
        raise Invalid(f"Unknown operation '{node.get('op')}' on '{node_def.model}'")

    node_id = node.get('id')

    if not op:
        # An untouched row: nothing to write, but the descent continues, because
        # a node carries an `op` only where the user acted. That is what lets the
        # buffer and the payload keep one shape — the client prunes what it did
        # not touch, and what it keeps is what it holds.
        obj = _passthrough_row(app, session, node_def, node_id, inherited)
        _save_children(app, session, node_def, node, obj, id_map, deletes, depth, judged)
        return node_id

    if op == 'delete':
        _require_saved_id(node_def.model, node_id, 'delete')
        deletes.append((depth, node_def.model, node_id, judged.get(id(node)),
                        _row_checked(app, node_def)))
        # No id to hand down, but the descent continues: a grandchild's deletion
        # is collected here and applied before this row goes.
        _save_children(app, session, node_def, node, None, id_map, deletes, depth, judged)
        return None

    values, sets = kitebase.refsets.split(db_table, dict(node.get('values') or {}))

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
            raise Invalid(
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

        require_columns(model_class, values)
        refuse_duplicate_key(session, model_class, values)
        obj = model_class(**values)
        session.add(obj)
        session.flush()                     # the key, without closing the transaction
        real_id = getattr(obj, pk)
        # The row as inserted, defaults included, must be one the user may create
        if _row_checked(app, node_def):
            access.check_row(session, model_class, real_id, 'create')
        if isinstance(node_id, int) and not isinstance(node_id, bool) and node_id < 0:
            # The map is global to the payload — that is what lets a row point at
            # a sibling created beside it — so the client allocates temporary ids
            # from one counter. Two tables reusing a number would silently graft
            # one tree onto another.
            if node_id in id_map:
                raise Invalid(
                    f"Temporary id {node_id} is used twice in this save; they are "
                    f"unique across the whole payload, not per collection")
            id_map[node_id] = real_id

    else:   # update
        _require_saved_id(node_def.model, node_id, 'update')
        obj = session.get(model_class, node_id)
        if obj is None:
            raise NotFound(f"No record {node_id} in '{node_def.model}'")
        if _row_checked(app, node_def):
            # On the row as stored: otherwise the change itself could satisfy it
            if not access.row_visible(session, model_class, node_id):
                raise NotFound(f"No record {node_id} in '{node_def.model}'")
            access.check_row(session, model_class, node_id, 'update')
        # A typed key is refused on its field, as a form shows it; an assigned
        # one is never typed, and moving it is a malformed payload
        refuse_key_change(obj, model_class, values)
        if pk in values and values[pk] != node_id:
            raise Invalid(
                f"An update on '{node_def.model}' cannot move the row from {node_id} "
                f"to {values[pk]!r}")
        for key, value in values.items():
            if key != pk and hasattr(obj, key):
                setattr(obj, key, value)
        session.flush()
        real_id = node_id

    kitebase.refsets.write(app, session, db_table, obj, sets)
    _save_children(app, session, node_def, node, obj, id_map, deletes, depth, judged)
    ctx = judged.get(id(node))
    if ctx is not None:
        # The row as written: its key, the temporary ids resolved
        ctx.record = {**ctx.record, **flow.row_values(obj)}
        flow.commit(ctx, session, obj)
    return real_id


def _row_checked(app, node_def) -> bool:
    """Whether a node's rows answer to their own table's row conditions: the
    rows of a collection that is not owned. The root is checked by _write."""
    return isinstance(node_def, Collection) and not _owned(app, node_def)


def _save_children(app, session, node_def, node: Dict[str, Any], parent: Optional[Any],
                   id_map: Dict[int, Any], deletes: List[Tuple[int, str, Any, Any, bool]],
                   depth: int, judged: Dict[int, Any]) -> None:
    """Descend into the collections of a node, refusing any the page does not declare.

    `parent` is the parent's row, None when it is being deleted. Below a row that goes away
    only deletions make sense: a row added under it would be an orphan the caller
    believed it had created.
    """
    for cid, rows in (node.get('children') or {}).items():
        coll = node_def.collections.get(cid)
        if coll is None:
            raise Invalid(
                f"'{node_def.model}' declares no collection '{cid}' on this page")
        if not isinstance(rows, list):
            raise Invalid(f"Collection '{cid}' must carry a list of rows")

        for child in rows:
            if parent is None and str(child.get('op') or '').lower() != 'delete':
                raise Invalid(
                    f"A row of '{coll.model}' hangs from a deleted row of "
                    f"'{node_def.model}': below a row that goes away, only deletions")
            inherited = ({} if parent is None
                         else {coll.fk: getattr(parent, _link_of(app, node_def.model, coll))})
            _save_node(app, session, coll, child, inherited, coll.defaults,
                       id_map, deletes, depth + 1, judged)


_MODES = {'create': 'insert', 'update': 'update', 'delete': 'delete'}


def _as_written(app, model: str, node: Dict[str, Any], mode: Optional[str]):
    """A node's row as it will be written: `(record, original, changes)`.
    The stored row with the node's values over it; on a pass-through (no
    mode) the stored row; on an insert, the values alone."""
    model_class, db_table, _ = _table_of(app, model)
    values = write_values(db_table, dict(node.get('values') or {})) if mode in ('insert', 'update') else {}
    changes = {k: coerce_value(model_class, k, v) for k, v in values.items()}
    original = None
    if mode != 'insert' and _is_saved_id(node.get('id')):
        with app.get_session() as session:
            obj = session.get(model_class, node.get('id'))
            if obj is None:
                raise NotFound(f"No record {node.get('id')} in '{model}'")
            original = flow.row_values(obj)
    return {**(original or {}), **changes}, original, changes


def _as_parent(app, model: str, node: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    """What the rows below a node see of it: its record, with its key, the
    temporary one when it is new."""
    _, _, pk = _table_of(app, model)
    return {**record, pk: record.get(pk, node.get('id'))}


def _judge_root(app, aggregate: Aggregate, root_node: Dict[str, Any], answers):
    """
    The rules of the root's table, on the root as it will be written; then
    the rules of every row the tree writes (_judge_rows). Returns
    `(ctx, rows, refusal)`, `rows` the contexts of the rows judged.

    The rows a delete takes with it through the composition are not in the
    tree, and are not judged: a table with rules among them refuses the tree.
    """
    cascaded = [m for m in _cascaded_models(app, aggregate, root_node) if flow.has_rules(m)]
    if cascaded:
        raise ValueError(f"Rows deleted with their parent are not judged by save_tree: "
                         f"{', '.join(sorted(set(cascaded)))}")

    mode = {'create': 'insert', 'delete': 'delete'}.get(root_node.get('op'), 'update')
    ctx = flow.Context(aggregate.model, mode, {}, None, {})
    ruled_root = flow.has_rules(aggregate.model)
    ruled_rows = _rows_ruled(aggregate, root_node)
    if not ruled_root and not ruled_rows:
        return ctx, {}, None

    record, original, changes = _as_written(app, aggregate.model, root_node, mode)
    if ruled_root:
        ctx, refusal = flow.judge(aggregate.model, mode, record, original, answers)
        if refusal is not None:
            return ctx, {}, refusal
        # What a rule changed is written with the rest of the root
        for key, value in ctx.record.items():
            if key in changes and changes[key] == value:
                continue
            if key not in changes and original is not None and original.get(key) == value:
                continue
            root_node.setdefault('values', {})[key] = value
        record = ctx.record

    rows: Dict[int, flow.Context] = {}
    if ruled_rows:
        refusal = _judge_rows(app, aggregate, root_node,
                              _as_parent(app, aggregate.model, root_node, record),
                              answers, rows)
        if refusal is not None:
            return ctx, rows, refusal
    return ctx, rows, None


def _rows_ruled(node_def, node: Dict[str, Any]) -> bool:
    """Whether the tree writes a row whose table has rules."""
    for cid, rows in (node.get('children') or {}).items():
        coll = node_def.collections.get(cid)
        if coll is None or not isinstance(rows, list):
            continue
        for row in rows:
            if (row.get('op') and flow.has_rules(coll.model)) or _rows_ruled(coll, row):
                return True
    return False


def _judge_rows(app, node_def, node: Dict[str, Any], parent: Dict[str, Any],
                answers, rows: Dict[int, flow.Context]) -> Optional[Dict[str, Any]]:
    """
    The rules of the rows below `node`, each on its row as it will be written,
    with `ctx.parent` the parent as it will be (its rules already judged).
    What a rule changes is written with the row; its context is kept in
    `rows`, by the node, for the `commit` and `after` rules.

    A row is judged like a single write, with two limits. Its refusal names
    its table, and carries `{collection, id}` in `$flow.row`; a patch of a
    row is not sent, the client applies patches to the form it shows. A row
    cannot ask: there is no form to put the question in, so a question is
    a refusal with its text, and the row is written from its own form.
    Rules about the rows together are rules of the root, which sees them.
    """
    for cid, children in (node.get('children') or {}).items():
        coll = node_def.collections.get(cid)
        if coll is None or not isinstance(children, list):
            continue  # refused with its own message when the rows are saved
        link = _link_of(app, node_def.model, coll)
        for child in children:
            mode = _MODES.get(str(child.get('op') or '').lower())
            ruled = mode is not None and flow.has_rules(coll.model)
            if not ruled and not child.get('children'):
                continue
            record, original, _ = _as_written(app, coll.model, child, mode)
            if mode == 'insert':
                for key, value in (coll.defaults or {}).items():
                    record.setdefault(key, value)
            if mode != 'delete':
                record[coll.fk] = parent.get(link)
            if ruled:
                before = dict(record)
                ctx, refusal = flow.judge(coll.model, mode, record, original, answers,
                                          parent=parent, parent_model=node_def.model)
                if refusal is not None:
                    return _row_refusal(refusal, coll, cid, child)
                if mode != 'delete':
                    for key, value in ctx.record.items():
                        if key not in before or before[key] != value:
                            child.setdefault('values', {})[key] = value
                rows[id(child)] = ctx
                record = ctx.record
            if child.get('children'):
                refusal = _judge_rows(app, coll, child, _as_parent(app, coll.model, child, record),
                                      answers, rows)
                if refusal is not None:
                    return refusal
    return None


def _row_refusal(refusal: Dict[str, Any], coll: Collection, cid: str,
                 node: Dict[str, Any]) -> Dict[str, Any]:
    """A row's refusal, as the tree answers it: a question becomes a refusal,
    the message names the row's table, the row is in `$flow.row`."""
    flow_part = dict(refusal.get('$flow') or {})
    flow_part.pop('patch', None)
    flow_part.pop('phase', None)
    question = flow_part.pop('ask', None)
    message = (question or {}).get('text') or refusal.get('message') or ''
    flow_part['row'] = {'collection': cid, 'id': node.get('id')}
    return {'status': 'error', 'code': 422,
            'message': _f('{table}: {message}', table=_table_label(coll.model), message=message),
            '$flow': flow_part}


def _finish_rows(rows: Dict[int, flow.Context], result: Dict[str, Any]) -> Dict[str, Any]:
    """The `after` rules of the rows, once everything is committed; what they
    and the `check` rules of the rows said joins the answer of the root."""
    infos: List[str] = []
    actions: List[Dict[str, Any]] = []
    for ctx in rows.values():
        outcome = flow.finish(ctx, {}, ctx.record if ctx.mode != 'delete' else None).get('$flow') or {}
        infos += outcome.get('info') or []
        actions += outcome.get('actions') or []
    if infos or actions:
        merged = dict(result.get('$flow') or {})
        if infos:
            merged['info'] = list(merged.get('info') or []) + infos
        if actions:
            merged['actions'] = list(merged.get('actions') or []) + actions
        result['$flow'] = merged
    return result


@endpoint('save_tree', access=access.PER_TABLE)
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
        raise Invalid('page is required')
    root_node = data.get('root')
    if not isinstance(root_node, dict):
        raise Invalid('root must be a node')

    app = kitebase.utils.get_app()
    aggregate, ctx, rows, refusal = _prepare(app, page_id, root_node, data.get('answers'))
    if refusal:
        return refusal

    id_map: Dict[int, Any] = {}
    try:
        with app.get_session() as session:
            root_id, meta = _write(app, session, aggregate, root_node, ctx, rows, id_map)
            session.commit()
            # What was just written comes back under the permission that wrote it.
            root = _load_tree(app, session, aggregate, root_id,
                              covered=_aggregate_models(aggregate)) if root_id is not None else None
    except Conflict as conflict:
        # Nothing was written: the answer is the tree as it stands now
        raise _with_fresh_tree(app, aggregate, root_node, conflict)
    if root is not None:
        root.update(meta)

    result = flow.finish(ctx, {'status': 'success',
                               'data': {'id': root_id, 'id_map': id_map, 'root': root},
                               'code': 200},
                         root['values'] if root else None)
    return _finish_rows(rows, result)


def save_trees(page_id: str, roots: List[Dict[str, Any]],
               answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Write several trees of one page in **one** transaction: all of them or
    none. Every root is judged before anything is written, so a question or a
    refusal of the rules stops the whole set; a row that fails to write takes
    the others back with it. A refusal names the tree it is about in
    `data.index`. The `after` rules run once everything is committed.

    For a batch that writes many documents as one act (a sheet imported):
    called in the process, with the permissions of the current context.
    """
    app = kitebase.utils.get_app()
    index = 0
    try:
        prepared = []
        for index, root_node in enumerate(roots):
            aggregate, ctx, rows, refusal = _prepare(app, page_id, root_node, answers)
            if refusal:
                refusal['data'] = {**(refusal.get('data') or {}), 'index': index}
                return refusal
            prepared.append((aggregate, ctx, rows, root_node))

        written = []
        with app.get_session() as session:
            metas = []
            for index, (aggregate, ctx, rows, root_node) in enumerate(prepared):
                id_map: Dict[int, Any] = {}
                root_id, meta = _write(app, session, aggregate, root_node, ctx, rows, id_map)
                written.append((root_id, id_map))
                metas.append(meta)
            session.commit()
            loaded = [_load_tree(app, session, aggregate, root_id,
                                 covered=_aggregate_models(aggregate)) if root_id is not None else None
                      for (aggregate, _, _, _), (root_id, _) in zip(prepared, written)]
        for root, meta in zip(loaded, metas):
            if root is not None:
                root.update(meta)

        infos: List[str] = []
        actions: List[Dict[str, Any]] = []
        for (_, ctx, rows, _), root in zip(prepared, loaded):
            outcome = _finish_rows(rows, flow.finish(ctx, {}, root['values'] if root else None))
            outcome = outcome.get('$flow') or {}
            infos += outcome.get('info') or []
            actions += outcome.get('actions') or []
        result: Dict[str, Any] = {
            'status': 'success', 'code': 200,
            'data': {'ids': [root_id for root_id, _ in written],
                     'id_maps': [id_map for _, id_map in written], 'roots': loaded}}
        outcome = {k: v for k, v in (('info', infos), ('actions', actions)) if v}
        if outcome:
            result['$flow'] = outcome
        return result

    except AccessDenied:
        raise
    except Refusal as e:
        return {'status': 'error', 'code': e.code, 'message': e.message,
                'data': {**(e.data or {}), 'index': index}}


def _prepare(app, page_id: str, root_node: Dict[str, Any], answers):
    """The aggregate of the page, the permissions, and the rules' judgement:
    `(aggregate, ctx, rows, refusal)`. Nothing is written."""
    aggregate = page_aggregate(app, page_id)
    access.check_table(aggregate.model, _root_action(root_node))
    _check_rows(app, aggregate, root_node)
    ctx, rows, refusal = _judge_root(app, aggregate, root_node, answers)
    return aggregate, ctx, rows, refusal


def _check_rows(app, node_def, node: Dict[str, Any]) -> None:
    """
    The permissions of the rows the tree writes. The aggregate decides for
    its parts, the rows of an owned collection: only the root is checked. A
    collection that is not owned shows rows with a life of their own (hours
    under a task), and each written row needs its own table's permission, as
    from its own form; their row conditions are checked as they are written.
    """
    for cid, rows in (node.get('children') or {}).items():
        coll = node_def.collections.get(cid)
        if coll is None or not isinstance(rows, list):
            continue
        owned = _owned(app, coll)
        for row in rows:
            op = str(row.get('op') or '').lower()
            if op in _OPS and not owned:
                access.check_table(coll.model, op)
            _check_rows(app, coll, row)


def _with_fresh_tree(app, aggregate: Aggregate, root_node: Dict[str, Any],
                     conflict: Conflict) -> Conflict:
    """A write hook's refusal, carrying the tree re-read (None if it is gone)."""
    root_id = root_node.get('id')
    fresh = None
    if _is_saved_id(root_id):
        fresh = _read_for_client(app, aggregate, root_id, covered=_aggregate_models(aggregate))
    return Conflict(conflict.message, data={**(conflict.data or {}), 'root': fresh},
                    code=conflict.code)


def _write(app, session, aggregate, root_node: Dict[str, Any], ctx,
           rows: Dict[int, Any], id_map: Dict[int, Any]) -> Tuple[Any, Dict[str, Any]]:
    """Write one judged tree in `session`, its write hooks first and its
    `commit` rules last, the rows' as each is written and the root's at the
    end; the caller commits. Returns the root's key and the hooks' `$` keys."""
    deletes: List[Tuple[int, str, Any, Any, bool]] = []
    root_class, _, _ = _table_of(app, aggregate.model)
    action = _root_action(root_node)
    # A row condition is judged on the root as stored, before the change
    root_key = root_node.get('id')
    # a malformed root is _save_node's to refuse, in its own words
    if action != 'create' and _is_saved_id(root_key) \
            and str(root_node.get('op') or 'update').lower() in _OPS:
        if not access.row_visible(session, root_class, root_key):
            raise NotFound(f"No record {root_key} in '{aggregate.model}'")
        access.check_row(session, root_class, root_key, action)

    meta = _write_metadata(TreeWrite(session, aggregate.model, root_key, action,
                                     _node_metadata(root_node)))
    root_id = _save_node(app, session, aggregate, root_node, {}, {}, id_map, deletes, 0, rows)
    # and a created root as inserted, before anything is committed
    if action == 'create':
        access.check_row(session, root_class, root_id, 'create')

    # Backwards: a grandchild before its child, a child before its parent.
    # Every row first, so that one still needed elsewhere is said by name
    # before anything goes.
    doomed = []
    for _depth, model_name, record_id, row_ctx, checked in sorted(deletes, key=lambda d: -d[0]):
        model_class, _, _ = _table_of(app, model_name)
        obj = session.get(model_class, record_id)
        if obj is None or (checked and not access.row_visible(session, model_class, record_id)):
            raise NotFound(f"No record {record_id} to delete in '{model_name}'")
        if checked:
            access.check_row(session, model_class, record_id, 'delete')
        doomed.append((obj, row_ctx))
    refuse_referenced(session, [obj for obj, _ in doomed])
    for obj, row_ctx in doomed:
        if row_ctx is not None:
            flow.commit(row_ctx, session, obj)
        session.delete(obj)

    root_obj = session.get(root_class, root_id) if root_id is not None else None
    flow.commit(ctx, session, root_obj)
    return root_id, meta
