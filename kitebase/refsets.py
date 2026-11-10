"""A junction read and written as a value of the record: the RefSets of a table.

A table holds a RefSet for each junction end that declares `field: true`
(kitebase.schema.RefSet): the field carries the keys of the other end, in the order
the links were written. The junction rows never leave the server.
"""
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from kitebase.i18n import _, _f
from kitebase.transforms import ValidationError


def _junction_key(ref) -> str:
    """The junction's own key, which orders the links as they were written."""
    return next(col.name for col in ref.junction.effective_columns
                if col.attributes.get('primary_key'))


def attach(app: Any, session: Any, db_table: Any, rows: List[Dict[str, Any]],
           names: Optional[List[str]] = None) -> None:
    """Add the RefSets of `db_table` to `rows`, records as dicts, in place.

    All of them, or the ones `names` lists. One query per RefSet for all the
    rows: a list of a hundred books asks the junction once, not a hundred
    times. A row with no links gets an empty list.
    """
    if db_table is None or not rows:
        return
    for ref in db_table.ref_sets.values():
        if names is not None and ref.name not in names:
            continue
        keys = {row.get(ref.owner_key) for row in rows} - {None}
        linked: Dict[Any, List[Any]] = defaultdict(list)
        if keys:
            junction = app.find_model_class(ref.junction.name)
            owner = getattr(junction, ref.owner_column)
            target = getattr(junction, ref.target_column)
            query = (select(owner, target).where(owner.in_(keys))
                     .order_by(getattr(junction, _junction_key(ref))))
            for owner_value, target_value in session.execute(query):
                linked[owner_value].append(target_value)
        for row in rows:
            row[ref.name] = list(linked.get(row.get(ref.owner_key), []))


def junctions(db_table: Any) -> List[str]:
    """The junctions of `db_table`'s RefSets: read with the record, not on their own.

    A link is part of the record's value, so whoever reads the record reads it,
    to show it or to filter by it; the vocabulary it points at keeps its own
    permission.
    """
    return [ref.junction.name for ref in db_table.ref_sets.values()] if db_table is not None else []


def from_select(db_table: Any, query_def: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The RefSets a query's `select` names, and the query without them.

    The querybuilder knows nothing of RefSets: they are taken out before it
    runs and attached to its records after (`attach`). The key each one is
    read by stays selected, added when the select left it out.
    """
    select = query_def.get('select')
    if db_table is None or not db_table.ref_sets or not isinstance(select, list):
        return query_def, []
    prefix = f'{db_table.name}.'

    def ref_name(item: Any) -> Optional[str]:
        name = item.removeprefix(prefix) if isinstance(item, str) else None
        return name if name in db_table.ref_sets else None

    names = [ref_name(item) for item in select if ref_name(item)]
    if not names:
        return query_def, []
    kept = [item for item in select if not ref_name(item)]
    for name in names:
        key = db_table.ref_sets[name].owner_key
        if key not in kept and prefix + key not in kept:
            kept.append(key)
    return {**query_def, 'select': kept}, names


# ── Write ─────────────────────────────────────────────────────────────────────
#
# A write carries the whole list. The server compares it with the links that
# exist, removes the ones that are gone and adds the new ones after them, so a
# link that stays keeps its place. What the list may contain besides keys is a
# matter for a resolver: a plugin registers one for the vocabularies it knows
# (tags turn names into keys), with the protocol of a query behavior:
#
#   applies_to(target_class) -> bool
#   resolve(session, target_class, ref, values) -> list of keys
#   creates = True       optional: a value may become a new row of the target,
#                        so the widget offers to add what it does not find
#
# Without a resolver the values must be keys of the target, and exist.

_RESOLVERS: List[Any] = []


def register_resolver(resolver: Any) -> None:
    """Register a resolver for the RefSets whose target it applies to."""
    if resolver not in _RESOLVERS:
        _RESOLVERS.append(resolver)


def _resolver_for(target_class: Any) -> Any:
    return next((r for r in _RESOLVERS if r.applies_to(target_class)), None)


def field(app: Any, ref: Any) -> Dict[str, Any]:
    """A RefSet as the client sees it: a field of its own type, with its vocabulary.

    The same description in the table schema, in a written form and in a
    generated one. `create` says whether a value the vocabulary lacks can be
    added, which only the resolver knows.
    """
    target_class = app.find_model_class(ref.target.name) if app is not None else None
    resolver = _resolver_for(target_class) if target_class is not None else None
    return {'name': ref.name, 'type': 'RefSet',
            'ref_set': {'target': ref.target.name, 'field': ref.target_key,
                        'create': bool(getattr(resolver, 'creates', False))}}


def split(db_table: Any, values: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Separate the RefSet fields from the columns: the two are written apart."""
    names = db_table.ref_sets if db_table is not None else {}
    sets = {k: v for k, v in values.items() if k in names}
    return {k: v for k, v in values.items() if k not in names}, sets


def _keys(session: Any, target_class: Any, ref: Any, values: Any) -> List[Any]:
    """The keys a written list stands for, in order and each once, or ValidationError."""
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValidationError({ref.name: _('A list is expected')})
    resolver = _resolver_for(target_class)
    if resolver is not None:
        values = resolver.resolve(session, target_class, ref, values)
    keys = list(dict.fromkeys(values))

    # The database may not check the foreign key (SQLite, by default): a key
    # that does not exist would be stored and never shown.
    target_key = getattr(target_class, ref.target_key)
    found = set(session.scalars(select(target_key).where(target_key.in_(keys)))) if keys else set()
    missing = [key for key in keys if key not in found]
    if missing:
        raise ValidationError({ref.name: _f('Not found: {keys}',
                                            keys=', '.join(str(k) for k in missing))})
    return keys


def write(app: Any, session: Any, db_table: Any, record: Any, sets: Dict[str, Any]) -> None:
    """Bring the links of `record`, already flushed, to the lists in `sets`."""
    for name, values in sets.items():
        ref = db_table.ref_sets[name]
        junction = app.find_model_class(ref.junction.name)
        target_class = app.find_model_class(ref.target.name)
        keys = _keys(session, target_class, ref, values)

        owner_value = getattr(record, ref.owner_key)
        owner = getattr(junction, ref.owner_column)
        existing = {getattr(row, ref.target_column): row
                    for row in session.scalars(select(junction).where(owner == owner_value))}
        for target_value, row in existing.items():
            if target_value not in keys:
                session.delete(row)
        for key in keys:
            if key not in existing:
                session.add(junction(**{ref.owner_column: owner_value,
                                        ref.target_column: key}))
    if sets:
        session.flush()
