"""
Who is asking: the context of the work in progress.

A dictionary (user, tenant, locale, op_date, whatever the application adds)
that travels with the request and is read by whoever needs to know on whose
behalf it runs: access, defaults, the rules of a write, the session.

It lives in one ContextVar, which serves both execution models. A WSGI thread
has a context of its own that lasts across the requests it serves, so whoever
sets it clears it on the way out (the Flask adapter's teardown). An ASGI server
runs a sync handler on a copy of the request's context, so what the handler
sets is discarded with the copy. A thread or a process started by hand begins
with no context: background work sets its own with `use()`.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

Context = Optional[Dict[str, Any]]

_current: ContextVar = ContextVar('kitebase_context', default=None)


def current() -> Context:
    """The context of the work in progress, or None."""
    return _current.get()


def replace(context: Context) -> None:
    """Set the context, replacing the whole value; None clears it.

    For the entry points that own the whole life of a request (the dispatcher,
    the HTTP adapters). Anything else uses `use()`, which puts back what it found.
    """
    _current.set(context)


@contextmanager
def use(context: Context) -> Iterator[Context]:
    """Set the context for the duration of the block, then restore it.

    For code that runs outside an HTTP request: background threads, batch
    scripts, server-rendered pages of a host application, where nothing else
    sets the context on the way in. Restores unconditionally, including back to
    None: leaving a context behind means the next user of that thread inherits
    an identity nobody chose, and query behaviors filter by it. The block must
    close in the context it was opened in; closing it elsewhere raises
    ValueError instead of restoring the wrong value.
    """
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)
