"""
The refusals an endpoint raises, and the answer each one becomes.

A refusal is an answer to the request, not a failure of the server: the
dispatcher turns it into the error envelope with its code, logs one line
without a traceback, and the client shows the message. Anything else an
endpoint raises is a failure, answered with a 500 and its traceback.

    raise NotFound(_f('No page {id}', id=page_id))
    raise Invalid(_('Some values are not valid'), data={'errors': errors})

| class         | code | when |
|---------------|------|------|
| `Invalid`     | 400  | the request is wrong: a missing parameter, a value a rule refuses |
| `AccessDenied`| 403  | a permission is missing (`kitebase.access`) |
| `NotFound`    | 404  | what the request names is not there, or not visible to the caller |
| `Conflict`    | 409  | the request is right but the state does not allow it now |
| `Unavailable` | 503  | something the server depends on does not answer |

`code` can be set on a single refusal when the HTTP answer is more precise
than its class (413 for a file too large is an `Invalid`). `data` reaches the
client in the envelope, for what the refusal has to say beyond its message:
the field errors of a form.
"""
from typing import Any, Optional


class Refusal(Exception):
    """An answer the request gets instead of its result."""

    code = 400

    def __init__(self, message: str, data: Any = None, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.data = data
        if code is not None:
            self.code = code

    @property
    def message(self) -> str:
        """What the person reads."""
        return str(self)


class Invalid(Refusal, ValueError):
    """The request is wrong: a parameter is missing, a value breaks a rule."""

    code = 400


class NotFound(Refusal, LookupError):
    """What the request names is not there, or the caller may not see it."""

    code = 404


class Conflict(Refusal):
    """The request is right, but the state of the data does not allow it now."""

    code = 409


class Unavailable(Refusal):
    """Something the server depends on does not answer."""

    code = 503
