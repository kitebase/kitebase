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
"""
from typing import Any, Dict, Iterable, Optional, Set

# What an endpoint declares with `@endpoint(name, access=...)`, besides a
# permission code. `$` marks them as framework values, never a permission.
PER_TABLE = '$per_table'           # the endpoint checks each table it touches
AUTHENTICATED = '$authenticated'   # any logged-in user may call it
UNDECLARED = None                  # nothing declared: the guard decides

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


def check_endpoint(endpoint: str, access: Optional[str]) -> None:
    """The dispatcher's check, before an endpoint runs."""
    if not _guard().allows_endpoint(endpoint, access, _context()):
        raise AccessDenied(access if isinstance(access, str) else endpoint)


def tables_read(statement: Any, models: Dict[str, Any],
                covered: Iterable[str] = ()) -> Set[str]:
    """
    The models a finished statement reads, by name.

    Walks the statement itself rather than the query definition: joins,
    the EXISTS of a filter through a relation and the subqueries of an
    expression all end up there, whatever spelling brought them in. Tables
    that map to no model are left out; `covered` names models whose rows an
    already checked permission answers for (the collections of an aggregate).
    """
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.schema import Table

    by_table = {getattr(m, '__table__', None): name for name, m in models.items()}
    found: Set[str] = set()
    for element in visitors.iterate(statement):
        if isinstance(element, Table):
            name = by_table.get(element)
            if name is not None:
                found.add(name)
    return found - set(covered)
