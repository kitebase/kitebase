"""
The roots of the merged tree, and who owns each of them.

Every `.yaml` of a plugin is merged into `pm.data`, so any top-level key becomes
a root. A root means something only to the module that reads it: the core for
`tables` or `pages`, a plugin for its own (`roles` of acl). That module says so:

    register_section('roles')
    register_section('settings', check=_check_settings)

- `descriptor=True`: the root is a tree of UI descriptors, walked by the generic
  checks of diagnostics ($ref, views, commands, collections).
- `check=`: a function `(app) -> [issue]`, run by `kitebase check` with the
  core's own checks. It is how a plugin brings its rules to `check`.
- `vocabulary=True`: the keys inside the root are checked against the core's
  vocabulary (kitebase.vocabulary). Only the core's roots: the keys of a
  plugin's root are the plugin's, and its `check=` judges them.

A root nobody registers is read by nobody: `check` warns about it, and it is
neither walked nor dumped. The registry is per process, like the validators'
(kitebase.transforms): a plugin registers at import, which happens once.
"""
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

Check = Callable[[Any], List[Dict[str, Any]]]


@dataclass(frozen=True)
class Section:
    name: str
    owner: str                      # module that registered it
    descriptor: bool = False
    check: Optional[Check] = None
    vocabulary: bool = False


_SECTIONS: Dict[str, Section] = {}


def register_section(name: str, *, descriptor: bool = False,
                     check: Optional[Check] = None, vocabulary: bool = False) -> None:
    """Declare that the calling module owns the root `name` of the merged tree.

    Two modules owning the same root would read one tree by two sets of rules:
    that is refused. The same module registering again replaces its entry.
    """
    if name.startswith('$'):
        raise ValueError(f"'{name}': `$` keys are framework metadata, not roots")
    owner = sys._getframe(1).f_globals.get('__name__', '?')
    current = _SECTIONS.get(name)
    if current is not None and current.owner != owner:
        raise ValueError(f"root '{name}' is already owned by {current.owner}; "
                         f"{owner} cannot register it too")
    _SECTIONS[name] = Section(name, owner, descriptor, check, vocabulary)


def get_section(name: str) -> Optional[Section]:
    """The registration of a root, or None if nobody owns it."""
    return _SECTIONS.get(name)


def sections() -> List[Section]:
    """Every registered root, in registration order."""
    return list(_SECTIONS.values())


# The core's roots, their keys in the core's vocabulary. tables/types/schemas
# feed model and type generation and have checks and dumpers of their own; the
# others are descriptor trees.
for _name in ('tables', 'types', 'schemas'):
    register_section(_name, vocabulary=True)
for _name in ('pages', 'views', 'menus', 'menu_items'):
    register_section(_name, descriptor=True, vocabulary=True)
