"""
What the modules of the core and of the plugins register, in one shape.

A plugin extends the core by registering: an endpoint, a rule, a guard, a
default. It does so while its module is imported, when the application may not
exist yet, so the registries belong to the process, not to the application.

Two shapes cover them:

    Registry   many entries, each under a name
    Slot       one value, or the default

Both know who registered what, by module, and both answer a collision the same
way: a name belongs to the module that took it. That module registering again
replaces its own entry (a reload); another module is refused, unless the
registration says `replace=True` where the registry offers it, and then the
registry remembers whom it replaced. Nothing is overwritten in silence.

Every registry has a name and is listed by `catalog()`, which is what a tool
reads to show what is registered, and by whom.
"""
import sys
from collections.abc import MutableMapping
from typing import Any, Dict, Generic, Iterator, List, Optional, TypeVar

T = TypeVar('T')

_CATALOG: Dict[str, Any] = {}


def catalog() -> Dict[str, Any]:
    """Every registry and slot of the process, by name."""
    return dict(_CATALOG)


def _listed(name: str, registry: Any) -> None:
    if name in _CATALOG:
        raise ValueError(f"A registry named '{name}' already exists")
    _CATALOG[name] = registry


def owner_of(value: Any, depth: int = 2) -> str:
    """The module a registration comes from: the one that defines the value,
    or, for a value without a module of its own, the one that is registering."""
    module = getattr(value, '__module__', None)
    if isinstance(module, str):
        return module
    return sys._getframe(depth).f_globals.get('__name__', '?')


def caller(depth: int = 2) -> str:
    """The module calling the function that asks: who is registering, when
    the value registered may come from somewhere else."""
    return sys._getframe(depth).f_globals.get('__name__', '?')


class Collision(ValueError):
    """Two modules claim the same name of a registry."""


class Registry(MutableMapping, Generic[T]):
    """
    Entries under a name, in the order they were registered.

    Read like a dictionary. Written with `add()`; `registry[name] = value` is
    the same registration, owned by the module of the value.
    """

    def __init__(self, name: str, what: str = 'entry') -> None:
        self.name = name
        self.what = what            # one entry, in the words of an error message
        self._entries: Dict[str, T] = {}
        self._owners: Dict[str, str] = {}
        self._replaced: Dict[str, List[str]] = {}
        _listed(name, self)

    def add(self, key: str, value: T, *, owner: Optional[str] = None,
            replace: bool = False) -> T:
        owner = owner or owner_of(value)
        current = self._owners.get(key)
        if current is not None and current != owner:
            if not replace:
                raise Collision(
                    f"{self.what} '{key}' is already registered by {current}; "
                    f"{owner} cannot register it too")
            self._replaced.setdefault(key, []).append(current)
        self._entries[key] = value
        self._owners[key] = owner
        return value

    def owner(self, key: str) -> Optional[str]:
        """The module that registered `key`."""
        return self._owners.get(key)

    def replaced(self, key: str) -> List[str]:
        """The modules whose registration of `key` was replaced, oldest first."""
        return list(self._replaced.get(key, ()))

    # ── The dictionary ────────────────────────────────────────────────────

    def __getitem__(self, key: str) -> T:
        return self._entries[key]

    def __setitem__(self, key: str, value: T) -> None:
        self.add(key, value, owner=owner_of(value))

    def __delitem__(self, key: str) -> None:
        del self._entries[key]
        self._owners.pop(key, None)
        self._replaced.pop(key, None)

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"Registry({self.name!r}, {list(self._entries)})"

    # ── Tests ─────────────────────────────────────────────────────────────

    def snapshot(self) -> Any:
        """The state to hand back to `restore()`: a test registers, then undoes."""
        return dict(self._entries), dict(self._owners), {k: list(v) for k, v in self._replaced.items()}

    def restore(self, snapshot: Any) -> None:
        self._entries, self._owners, self._replaced = (dict(part) for part in snapshot)


class Slot(Generic[T]):
    """One value, or the default while nobody registered one."""

    def __init__(self, name: str, default: T, what: str = 'value') -> None:
        self.name = name
        self.what = what
        self.default = default
        self._value: T = default
        self._owner: Optional[str] = None
        _listed(name, self)

    def get(self) -> T:
        return self._value

    def set(self, value: T, *, owner: Optional[str] = None, replace: bool = False) -> None:
        owner = owner or owner_of(value)
        if self._owner is not None and self._owner != owner and not replace:
            raise Collision(
                f"{self.what} is already registered by {self._owner}; "
                f"{owner} cannot register another")
        self._value, self._owner = value, owner

    @property
    def owner(self) -> Optional[str]:
        return self._owner

    @property
    def taken(self) -> bool:
        return self._owner is not None

    def reset(self) -> None:
        """Back to the default, as if nobody had registered."""
        self._value, self._owner = self.default, None

    def __repr__(self) -> str:
        return f"Slot({self.name!r}, {self._value!r})"
