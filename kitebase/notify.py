"""
What a work in progress says while it runs: the port of `notify()`.

The code that works says where it is, and whoever waits sees it:

    from kitebase.notify import notify

    notify(f'Fetching description of {book.title}')
    for i, book in enumerate(books):
        notify('Fetching descriptions', done=i, total=len(books), cancellable=True)
        ...

The core keeps no table and knows nobody who listens. A plugin registers the
notifier, the support that keeps the state where the client reads it; without
one the message goes to the log at DEBUG. `notify()` can be called always, so
the code of an endpoint or of a plugin depends on nothing to use it.

A work is a request (the dispatcher opens one around each endpoint) or what
code outside a request opens with `work()`. Its state is the last one: each
call replaces the previous, there is no queue. The notifier is written at the
first call and then at most once a second, or at once when the phase changes
between cancellable and not; in between only the last state is kept. When the
work ends the notifier is told to close it: the code that works never has to.

Cancelling is the choice of who writes the work, and it is made per phase: the
default is not cancellable, and a phase says `cancellable=True` where stopping
leaves nothing half done. A cancel asked in a phase that is not cancellable
waits for the first one that is. The cancel is found while writing: the
notifier answers whether one was asked, and `notify()` raises `Cancelled`.
What was done stays done; a work that must undo does it in `except Cancelled`.

The notifier never makes `notify()` fail: a support that does not answer costs
a message, not the work.
"""
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

from kitebase.errors import Cancelled
from kitebase.registry import Slot, caller

_log = logging.getLogger(__name__)

# The notifier is written at most this often for a work, in seconds
MIN_INTERVAL = 1.0


class Notifier:
    """What a support answers. One per process, registered by a plugin."""

    def write(self, key: str, state: Dict[str, Any]) -> bool:
        """Keep the state of the work `key`: text, done, total, cancellable.
        Return whether a cancel of the work was asked."""
        raise NotImplementedError

    def close(self, key: str) -> None:
        """The work `key` is over: what was kept of it goes."""
        raise NotImplementedError


NOTIFIER: Slot[Optional[Notifier]] = Slot('notifier', None, 'notifier')


def set_notifier(notifier: Notifier) -> None:
    """Register the notifier. One only: two would show two states of one work."""
    if NOTIFIER.taken and NOTIFIER.owner != caller():
        raise ValueError(
            f"A notifier is already registered: {type(NOTIFIER.get()).__name__}")
    NOTIFIER.set(notifier, owner=caller())


class _Work:
    """A work in progress, as `notify()` sees it."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.written = False
        self.last_write = 0.0
        self.cancellable = False
        self.cancel_asked = False


_current: ContextVar[Optional[_Work]] = ContextVar('kitebase_work', default=None)


@contextmanager
def work(key: str) -> Iterator[None]:
    """
    A work for the length of the block, closed however it ends.

    The dispatcher opens one around every endpoint, keyed by the request id;
    code outside a request opens its own. Inside a work already open (an
    endpoint calling another) the block joins it: one work, one state.
    """
    if _current.get() is not None:
        yield
        return
    w = _Work(key)
    token = _current.set(w)
    try:
        yield
    finally:
        _current.reset(token)
        notifier = NOTIFIER.get()
        if w.written and notifier is not None:
            try:
                notifier.close(w.key)
            except Exception:
                _log.warning('The notifier did not close work %s', w.key, exc_info=True)


def notify(text: str, done: Optional[int] = None, total: Optional[int] = None,
           cancellable: bool = False) -> None:
    """
    Say where the work in progress is: a line, and a counter when there is one.

    `cancellable=True` says that the work may stop here, and then a cancel
    asked raises `Cancelled`. Outside a work, or without a notifier, the
    message goes to the log at DEBUG.
    """
    w = _current.get()
    notifier = NOTIFIER.get()
    if w is None or notifier is None:
        _log.debug('notify: %s%s', text, f' ({done}/{total})' if total else '')
        return
    now = time.monotonic()
    due = (not w.written or cancellable != w.cancellable
           or now - w.last_write >= MIN_INTERVAL)
    w.cancellable = cancellable
    if due:
        state = {'text': text, 'done': done, 'total': total, 'cancellable': cancellable}
        try:
            w.cancel_asked = bool(notifier.write(w.key, state))
            w.written, w.last_write = True, now
        except Exception:
            _log.warning('The notifier did not write work %s', w.key, exc_info=True)
    if cancellable and w.cancel_asked:
        raise Cancelled('Cancelled')


def current_key() -> Optional[str]:
    """The key of the work in progress, or None outside one."""
    w = _current.get()
    return w.key if w is not None else None
