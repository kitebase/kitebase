"""Access control: the check every data port calls, and the guard behind it.

The core owns the ports and names the permissions; a policy plugin decides.
Every port asks `check(permission)` — `Book.read` for each table a query
touches, `Book.update` before a write, the permission an endpoint declares —
and the answer comes from the guard a policy plugin registered with
`set_guard()` when its module loaded. Without one the `OpenGuard` answers yes
to everything, so an application that does not enable access control behaves
as if none existed.

The core never names a role, a user flag or an administrator: those belong to
the guard. What it guarantees is that no port reaches the data without asking.

Permission codes: a model in CamelCase followed by an action (`Book.read`,
`Book.create`, `Book.update`, `Book.delete`); permissions of plugins in lower
case with the plugin's prefix (`reports.run`).

Code that reads for the user rather than for the system — a report loop —
runs on a session given `guard_session()`: every SELECT it executes, lazy
loads of relations included, asks `<Model>.read` on each table it reads. The
rest of the server (defaults, triggers, validators) works for the system and
is not checked; inside a guarded session `with system():` marks a read the
code means to make beyond the user's permissions.

`Book.label` is the narrow side of reading: the key, the label and the search
columns of a table (`label_columns`), what a foreign key needs to show "Il nome
della rosa" instead of 42 and a combobox to find it. A query that touches only
those columns of a table asks `Book.label` when `Book.read` is refused; any
other column, in the select, a filter, an ordering or a join, needs `Book.read`.

Columns can be narrower than their table: a column of a group
(`column_group: pricing`) asks `pricing.read` to be read and `pricing.write`
to be written. Without `read` the column is left out of a wildcard select and
of a serialised record, and naming it anywhere in a query is refused; without
`write` a value sent for it is dropped, so a create gets the default and an
update leaves it alone. The form that sends the whole record, read-only
fields included, still saves.

Rows can be narrower than their table: the guard may hold a table permission
only where a condition holds (`Guard.conditions`), in the querybuilder's
filter syntax, with `$ctx.<field>` standing for a value of the user's
context. A query applies the condition of `read` to its main table; another
table it reads with a conditional `read` is refused, unless only its label is
read and the label is held without condition. By key, a row outside the
condition of `read` does not exist (404), and one outside the condition of
the action is refused (403), judged on the row before the change; a created
row must satisfy the condition of `create` as it was inserted. The rows of a
collection follow their root, as they do for the table permissions.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, FrozenSet, Iterable, Iterator, List, Optional, Set, Tuple

# What an endpoint declares with `@endpoint(name, access=...)`, besides a
# permission code. `$` marks them as framework values, never a permission.
PER_TABLE = '$per_table'           # the endpoint checks each table it touches
AUTHENTICATED = '$authenticated'   # any logged-in user may call it
UNDECLARED = None                  # nothing declared: the guard decides

# The permissions every table has, derived from the model.
TABLE_ACTIONS = ('read', 'create', 'update', 'delete', 'label')

# The permissions every column group has: `pricing.read`, `pricing.write`.
COLUMN_ACTIONS = ('read', 'write')

# The action each `db` method needs on its table.
DB_METHODS = {'get': 'read', 'create': 'create', 'update': 'update', 'delete': 'delete'}


class AccessDenied(Exception):
    """A port refused a permission. The dispatcher answers it with a 403;
    `message` says what was refused in the caller's words."""

    def __init__(self, permission: str, message: Optional[str] = None) -> None:
        self.permission = permission
        super().__init__(message or f"Not allowed: {permission}")


class Guard:
    """
    The policy behind `check()`. A subclass answers `allows()`; the endpoint
    rules are written once here in terms of it.
    """

    def bind(self, app: Any) -> None:
        """Called once the app's plugins are merged: where to read the policy from."""

    def allows(self, permission: str, context: Optional[Dict[str, Any]]) -> bool:
        raise NotImplementedError

    def allows_undeclared(self, endpoint: str, context: Optional[Dict[str, Any]]) -> bool:
        """An endpoint that declared no access: code that may reach the data unchecked."""
        raise NotImplementedError

    def conditions(self, permission: str,
                   context: Optional[Dict[str, Any]]) -> Optional[List[Any]]:
        """
        For a permission `allows()` grants: None when it holds on every row,
        or the conditions it holds under, any one of them enough. Each is a
        filter in the querybuilder's syntax: a mapping of column to value
        (all must hold) or a list of conditions. Only asked of the four row
        actions of a table, `read`, `create`, `update` and `delete`.
        """
        return None

    def diagnose(self, app: Any) -> List[Dict[str, Any]]:
        """What `check` should report about the policy, as diagnostics issues:
        the list of what is left to cover before production."""
        return []

    def allows_endpoint(self, endpoint: str, access: Optional[str],
                        context: Optional[Dict[str, Any]]) -> bool:
        if access in (PER_TABLE, AUTHENTICATED):
            return True
        if access is UNDECLARED:
            return self.allows_undeclared(endpoint, context)
        return self.allows(access, context)


class OpenGuard(Guard):
    """The guard of an application without access control: everything is allowed."""

    def allows(self, permission: str, context: Optional[Dict[str, Any]]) -> bool:
        return True

    def allows_undeclared(self, endpoint: str, context: Optional[Dict[str, Any]]) -> bool:
        return True


OPEN = OpenGuard()

# One per process, like the other registries a plugin module fills when it is
# imported: the module runs once, whatever number of apps load it.
_GUARD: Guard = OPEN


def set_guard(guard: Guard) -> None:
    """
    Register the guard every port asks. One only: two policies have no obvious
    composition, so a second registration is an error, not a replacement.
    """
    global _GUARD
    if _GUARD is not OPEN:
        raise ValueError(f"A guard is already registered: {type(_GUARD).__name__}")
    _GUARD = guard


def bind(app: Any) -> None:
    """Hand the app to the guard; the core calls it at the end of calc_db."""
    _GUARD.bind(app)


def diagnose(app: Any) -> List[Dict[str, Any]]:
    """The guard's issues for `check`."""
    return _GUARD.diagnose(app)


def _guard() -> Guard:
    return _GUARD


def _context() -> Optional[Dict[str, Any]]:
    from kitebase.db import BaseApp
    return BaseApp.get_context()


def can(permission: str) -> bool:
    """Whether the current user holds `permission`."""
    return _guard().allows(permission, _context())


def check(permission: str) -> None:
    """Raise AccessDenied unless the current user holds `permission`."""
    if not can(permission):
        raise AccessDenied(permission)


def check_table(model_name: str, action: str) -> None:
    """`check('<Model>.<action>')`."""
    check(f'{model_name}.{action}')


def can_call(endpoint: str) -> bool:
    """Whether the current user may call `endpoint`, as the dispatcher would decide."""
    from kitebase.endpoints import _ACCESS
    return _guard().allows_endpoint(endpoint, _ACCESS.get(endpoint, UNDECLARED), _context())


def declared(value: Any) -> List[str]:
    """The permissions an `access:` in YAML asks for: one code, or a list of them all."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


# ── Sessions that read for the user ─────────────────────────────────────────

_SYSTEM: ContextVar[bool] = ContextVar('kitebase_access_system', default=False)


@contextmanager
def system() -> Iterator[None]:
    """Read beyond the user's permissions on a guarded session: a choice of the
    code, written where it is made, never a default."""
    token = _SYSTEM.set(True)
    try:
        yield
    finally:
        _SYSTEM.reset(token)


def guard_session(session: Any) -> Any:
    """
    Check every SELECT `session` executes: `<Model>.read` on each table the
    statement reads. The ORM loads whole rows, so the label is not enough.
    Lazy and eager loads of relations go through the same event, which is
    what makes this the guard of code that reads freely, like a report loop.
    """
    from sqlalchemy import event

    def check_statement(state: Any) -> None:
        if not state.is_select or _SYSTEM.get():
            return
        import kitebase.utils
        models = kitebase.utils.get_app().models
        for model_name in sorted(columns_read(state.statement, models)):
            check_table(model_name, 'read')

    event.listen(session, 'do_orm_execute', check_statement)
    return session


def check_endpoint(endpoint: str, access: Optional[str]) -> None:
    """The dispatcher's check, before an endpoint runs."""
    if not _guard().allows_endpoint(endpoint, access, _context()):
        raise AccessDenied(access if isinstance(access, str) else endpoint)


def columns_read(statement: Any, models: Dict[str, Any], covered: Iterable[str] = (),
                 extra: Iterable[Tuple[Any, str]] = ()) -> Dict[str, Set[str]]:
    """
    The models a finished statement reads, by name, each with the names of the
    columns it reads of them.

    Walks the statement itself rather than the query definition: joins,
    the EXISTS of a filter through a relation and the subqueries of an
    expression all end up there, whatever spelling brought them in. Tables
    that map to no model are left out; `covered` names models whose rows an
    already checked permission answers for (the collections of an aggregate).

    A column inside textual SQL is not in the statement as a column: `extra`
    gives those, as (model class, column name), from the expressions the
    builder checked. A table read with no column at all (a count) has an
    empty set.
    """
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.schema import Column, Table

    by_table = {getattr(m, '__table__', None): name for name, m in models.items()}
    by_class = {m: name for name, m in models.items()}
    found: Dict[str, Set[str]] = {}

    def base_table(element: Any) -> Any:
        # An alias or a subquery stands in front of the table it selects from
        while element is not None and not isinstance(element, Table):
            element = getattr(element, 'element', None)
        return element

    for element in visitors.iterate(statement):
        if isinstance(element, Table):
            name = by_table.get(element)
            if name is not None:
                found.setdefault(name, set())
        elif isinstance(element, Column):
            name = by_table.get(base_table(element.table))
            if name is not None:
                found.setdefault(name, set()).add(element.name)
    for model, column in extra:
        name = by_class.get(model)
        if name is not None:
            found.setdefault(name, set()).add(column)
    for name in covered:
        found.pop(name, None)
    return found


def label_columns(model_name: str, app: Any = None) -> FrozenSet[str]:
    """
    The columns `<Model>.label` opens: the primary key, the label and the
    search columns, as `search_info` resolves them. A virtual label stands
    for columns this cannot name, so it opens nothing beyond the search ones.
    """
    if app is None:
        import kitebase.utils
        app = kitebase.utils.get_app()
    table = app.tables.get(model_name)
    if table is None:
        return frozenset()
    info = table.search_info
    names = {col.name for col in table.effective_columns
             if col.attributes.get('primary_key')}
    names.update(info['search_fields'])
    if info['display_field']:
        names.add(info['display_field'])
    return frozenset(names - set(table.secret_columns))


def check_read(model_name: str, columns: Iterable[str], main: bool = False) -> Optional[List[Any]]:
    """
    Reading `columns` of a model: `<Model>.read`, or `<Model>.label` when
    every column is a label column. The refusal names `read`, the permission
    that would have answered.

    Returns the row condition the read holds under, for the `main` table of
    a query, which the caller applies; None when every row may be read. A
    conditional read of any other table is refused: the condition would have
    to go into its join, and outside the main table nothing applies it.
    """
    permission = f'{model_name}.read'
    if can(permission):
        rule = row_conditions(model_name, 'read')
        if rule is None:
            return None
    else:
        rule = None
    if (set(columns) <= label_columns(model_name) and can(f'{model_name}.label')):
        return None
    if rule is not None and main:
        return rule
    if rule is not None:
        raise AccessDenied(permission, f"Not allowed: {permission} holds only on some "
                                       f"rows, and {model_name} is not the table queried")
    raise AccessDenied(permission)


# ── Row conditions ──────────────────────────────────────────────────────────

ROW_ACTIONS = ('read', 'create', 'update', 'delete')
CTX_PREFIX = '$ctx.'


def _bind_context(value: Any, permission: str, context: Dict[str, Any]) -> Any:
    """`$ctx.<field>` replaced by the context's value. A field that is missing,
    or null, refuses: a null in a security condition can come to mean no
    condition at all."""
    if isinstance(value, str) and value.startswith(CTX_PREFIX):
        field = value[len(CTX_PREFIX):]
        if context.get(field) is None:
            raise AccessDenied(permission, f"Not allowed: {permission} holds under a "
                                           f"condition on '{field}', which the context "
                                           f"does not carry")
        return context[field]
    if isinstance(value, list):
        return [_bind_context(v, permission, context) for v in value]
    if isinstance(value, dict):
        return {k: _bind_context(v, permission, context) for k, v in value.items()}
    return value


def _as_conditions(where: Any) -> List[Any]:
    """A `where` as one AND group: a mapping is one condition per column."""
    if isinstance(where, dict):
        return [{k: v} for k, v in where.items()]
    if isinstance(where, list):
        return list(where)
    raise ValueError(f"A row condition is a mapping or a list, not {where!r}")


def row_conditions(model_name: str, action: str) -> Optional[List[Any]]:
    """
    The condition the current user holds `<Model>.<action>` under, as one
    querybuilder condition group with the context's values in place; None
    when the permission holds on every row. Asked after the permission
    itself: it does not say whether the user holds it.
    """
    if action not in ROW_ACTIONS:
        return None
    permission = f'{model_name}.{action}'
    context = _context() or {}
    wheres = _guard().conditions(permission, context)
    if not wheres:
        return None
    return ['op', 'or'] + [_as_conditions(_bind_context(w, permission, context))
                           for w in wheres]


def row_clause(model_name: str, action: str, app: Any = None) -> Any:
    """`row_conditions` compiled for the model: a clause, or None."""
    rule = row_conditions(model_name, action)
    if rule is None:
        return None
    return compile_conditions(model_name, rule, app)


def compile_conditions(model_name: str, rule: List[Any], app: Any = None) -> Any:
    from kitebase.querybuilder import condition_clause
    if app is None:
        import kitebase.utils
        app = kitebase.utils.get_app()
    return condition_clause(app.models, model_name, rule)


def _row_matches(session: Any, model_class: Any, record_id: Any, clause: Any) -> bool:
    from sqlalchemy import inspect, literal, select
    key = inspect(model_class).primary_key[0]
    statement = (select(literal(1)).select_from(model_class)
                 .where(key == record_id).where(clause))
    return session.execute(statement).first() is not None


def row_visible(session: Any, model_class: Any, record_id: Any) -> bool:
    """Whether the current user's `read` reaches the row with this key. A row
    outside it is not found, rather than refused: it does not exist for them."""
    clause = row_clause(model_class.__name__, 'read')
    return clause is None or _row_matches(session, model_class, record_id, clause)


def check_row(session: Any, model_class: Any, record_id: Any, action: str) -> None:
    """Raise AccessDenied unless the row with this key satisfies the condition
    of `action`. Before a change, on the row as stored; after a create, on
    the row as inserted (flushed, not committed)."""
    clause = row_clause(model_class.__name__, action)
    if clause is not None and not _row_matches(session, model_class, record_id, clause):
        permission = f'{model_class.__name__}.{action}'
        raise AccessDenied(permission, f"Not allowed: {permission} on this record")


# ── Column groups ───────────────────────────────────────────────────────────

def column_groups(model: Any, app: Any = None) -> Dict[str, str]:
    """The columns of a model that belong to a group, with their group. The
    model by name, or its table definition."""
    if not isinstance(model, str):
        return getattr(model, 'column_groups', None) or {}
    if app is None:
        import kitebase.utils
        app = kitebase.utils.get_app()
    # an app with no table definitions (a bare query builder) has no groups
    table = (getattr(app, 'tables', None) or {}).get(model)
    return table.column_groups if table is not None else {}


def _refused(model: Any, action: str, app: Any = None) -> FrozenSet[str]:
    groups = column_groups(model, app)
    if not groups:
        return frozenset()
    allowed = {g: can(f'{g}.{action}') for g in set(groups.values())}
    return frozenset(c for c, g in groups.items() if not allowed[g])


def unreadable_columns(model: Any, app: Any = None) -> FrozenSet[str]:
    """The columns of a model (name or definition) the current user may not read."""
    return _refused(model, 'read', app)


def unwritable_columns(model: Any, app: Any = None) -> FrozenSet[str]:
    """The columns of a model (name or definition) the current user may not write."""
    return _refused(model, 'write', app)


def check_columns(model_name: str, columns: Iterable[str]) -> None:
    """Reading `columns` of a model: `<group>.read` for those in a group."""
    groups = column_groups(model_name)
    for column in sorted(set(columns) & set(groups)):
        permission = f'{groups[column]}.read'
        if not can(permission):
            raise AccessDenied(permission,
                               f"Not allowed: {permission} ({model_name}.{column})")
