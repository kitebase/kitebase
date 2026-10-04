"""
kitebase.apptime — the timezone an application declares, and the clock read
through it.

Kitebase stores datetimes **naive**: what sits in the column is wall-clock time
with no offset. That is not a shortcoming of the storage, it is what the
storage does — SQLite drops the offset even when given an aware value, and a
naive column on PostgreSQL keeps the fields verbatim. So the meaning of those
numbers is not in the data: it is a convention, and until an application states
it the convention is whatever timezone the process happens to run in — an
ambient value read from `TZ` or `/etc/localtime`.

Ambient is the problem. A rebuilt container, a base image without `TZ`, a host
moved to another region: the clock changes and nothing else does. The
application keeps working and writes numbers that are off by an offset, in the
same column as the right ones and indistinguishable from them, because a naive
value carries nothing that says which of the two it is. There is no error to
notice, only data that turns out wrong weeks later.

Declaring `timezone:` in config.yaml turns the assumption into a stated fact:

    timezone: Europe/Rome

From then on `now()` and `today()` answer in that zone whatever the process
believes, and `check_process_timezone()` refuses to start a process whose clock
disagrees — the one moment where the mismatch is still visible. Declaring
nothing keeps the previous behaviour exactly: the process clock, unchecked.

The check compares the **clock**, not the name: `Europe/Rome` and
`Europe/Berlin` pass against each other because they hold the same offsets and
the same rules, and it is the offset that ends up in the column.

Note what this module does *not* do: it does not make the stored values
unambiguous. Two naive datetimes written under two different declared zones are
still indistinguishable. What it buys is that the declared zone lives in
config.yaml, so a change to it is a deploy — visible, versioned, dated by its
commit — instead of an afternoon nobody recorded. That matters on the single
day it is ever needed: a migration to UTC storage, which has to know which zone
applied to which rows.
"""
import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Two real zones never differ by less than fifteen minutes, so anything under
# this is the two calls straddling a tick rather than a genuine mismatch.
_TOLERANCE_SECONDS = 30

_name: Optional[str] = None
_zone: Optional[ZoneInfo] = None


def set_app_timezone(name: Optional[str]) -> None:
    """
    Declare the application's timezone, as an IANA name ('Europe/Rome').

    Called by PluginsManager.load_config from the `timezone:` entry. None or
    empty clears the declaration, which is the unconfigured default.

    Raises:
        ValueError: the name is not a zone this system knows
    """
    global _name, _zone

    if not name:
        _name, _zone = None, None
        return

    try:
        _zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(
            f"config.yaml declares timezone '{name}', which this system does "
            f"not know ({e}). It must be an IANA name such as 'Europe/Rome'; "
            f"on a slim image the tzdata package may be missing."
        ) from e
    _name = name


def app_timezone_name() -> Optional[str]:
    """The declared timezone name, or None when the app declares none."""
    return _name


def check_process_timezone() -> None:
    """
    Refuse to run if the process clock disagrees with the declared timezone.

    Nothing happens when no timezone is declared: declaring one *is* the act of
    asking for the guarantee, so an application that says nothing keeps working
    as it always did.

    Raises:
        RuntimeError: a timezone is declared and the process runs in another
    """
    if _zone is None:
        return

    drift = datetime.now() - datetime.now(_zone).replace(tzinfo=None)
    if abs(drift.total_seconds()) <= _TOLERANCE_SECONDS:
        return

    hours = drift.total_seconds() / 3600
    raise RuntimeError(
        f"Timezone mismatch: config.yaml declares '{_name}', but this process "
        f"runs in {'/'.join(dict.fromkeys(time.tzname))} — its clock is "
        f"{hours:+.2f}h off. Naive timestamps written now would be wrong by "
        f"that much, and nothing in them would say so. "
        f"Set TZ={_name} in the environment (in Docker: `environment: TZ={_name}` "
        f"in the compose service), or change `timezone:` if the declaration is "
        f"the part that is out of date."
    )


def now() -> datetime:
    """
    Current wall-clock time in the application's timezone, naive.

    Naive on purpose: it is what goes into a DateTime column, and it has to
    match what everything else in the process writes. Correct even if the
    process clock is wrong — but do not rely on that alone, because plain
    `datetime.now()` elsewhere would not be, which is what the startup check
    is there to rule out.
    """
    if _zone is None:
        return datetime.now()
    return datetime.now(_zone).replace(tzinfo=None)


def today() -> date:
    """Current date in the application's timezone.

    The organisation's today, not the machine's: on a server running in UTC
    these differ for the first hours after midnight, which is when someone
    working late gets yesterday's date on today's document.
    """
    return now().date()
