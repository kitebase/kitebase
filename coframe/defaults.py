"""
System (framework) column defaults.

These are values Coframe itself provides — resolved at INSERT time from the
request context — that a model can opt into from YAML via a `$`-token, e.g.:

    - name: review_date
      type: Date
      default: $op_date

The source generator (coframe.source) translates the token into a reference to
the registered callable and emits the needed import into the generated model.
SQLAlchemy then calls it per insert, so it reflects the operator's current
op_date each time.

`op_date` is built in: the context carries it only when the user has set a date
(update_context), otherwise it is today, read at each call. It is a framework
value, not app policy, so it belongs here rather than in a commons plugin. Apps
may register their own system defaults via register_default() (same spirit as
add_query_behavior).
"""
from datetime import date, datetime
from typing import Callable, Optional, Set
from uuid import uuid4

from coframe import apptime
from coframe.db import BaseApp


def op_date() -> date:
    """Operational ("working") date from the request context; today if unset.

    "Today" is the organisation's, not the machine's — see coframe.apptime.
    On a server running in UTC the two differ for the first hours after
    midnight, and an op_date a day out produces documents that look right.
    """
    ctx = BaseApp.get_context() or {}
    raw = ctx.get('op_date')
    if raw:
        try:
            return date.fromisoformat(raw)
        except (ValueError, TypeError):
            pass
    return apptime.today()


def now() -> datetime:
    """Current wall-clock time in the application's timezone.

    `default: $now` rather than `default: datetime.now`: the same value, read
    through the timezone the app declares instead of the one the process
    happens to run in. It also puts the answer to "when is now" in one place,
    so a system that later moves its storage to UTC changes this function and
    not every model.yaml.
    """
    return apptime.now()


def uuid() -> str:
    """A new random key, in the canonical 36-character form (uuid4).

    A key minted on the server is what lets a record be inserted where no
    client coins one, as the terminals do: a column that must hold a unique
    key and is not editable would otherwise make the table non-insertable
    from a form. Lower case, as `str()` writes it and as the terminals send
    it, so the same key never arrives in two spellings.
    """
    return str(uuid4())


# Registry: token name (without the leading '$') -> callable used as a
# SQLAlchemy column default.
_DEFAULTS: dict = {
    'op_date': op_date,
    'now': now,
    'uuid': uuid,
}


def register_default(name: str, func: Callable) -> None:
    """Register a system default callable, referenced in YAML as `$<name>`."""
    _DEFAULTS[name] = func


def get_default(name: str) -> Optional[Callable]:
    """Return the callable for a token name, or None if unknown."""
    return _DEFAULTS.get(name)


def default_names() -> Set[str]:
    """All registered token names."""
    return set(_DEFAULTS)
