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

`Book.label` is the narrow side of reading: the key, the label and the search
columns of a table (`label_columns`), what a foreign key needs to show "Il nome
della rosa" instead of 42 and a combobox to find it. A query that touches only
those columns of a table asks `Book.label` when `Book.read` is refused; any
other column, in the select, a filter, an ordering or a join, needs `Book.read`.
"""
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

# What an endpoint declares with `@endpoint(name, access=...)`, besides a
# permission code. `$` marks them as framework values, never a permission.
PER_TABLE = '$per_table'           # the endpoint checks each table it touches
AUTHENTICATED = '$authenticated'   # any logged-in user may call it
UNDECLARED = None                  # nothing declared: the guard decides

# The permissions every table has, derived from the model.
TABLE_ACTIONS = ('read', 'create', 'update', 'delete', 'label')

# The action each `db` method needs on its table.
DB_METHODS = {'get': 'read', 'create': 'create', 'update': 'update', 'delete': 'delete'}


class AccessDenied(Exception):
    """A port refused a permission. The dispatcher answers it with a 403."""

    def __init__(self, permission: str) -> None:
        self.permission = permission
        super().__init__(f"Not allowed: {permission}")


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


def check_read(model_name: str, columns: Iterable[str]) -> None:
    """
    Reading `columns` of a model: `<Model>.read`, or `<Model>.label` when
    every column is a label column. The refusal names `read`, the permission
    that would have answered.
    """
    if can(f'{model_name}.read'):
        return
    if set(columns) <= label_columns(model_name) and can(f'{model_name}.label'):
        return
    raise AccessDenied(f'{model_name}.read')
