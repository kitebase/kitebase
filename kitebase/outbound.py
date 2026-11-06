"""
kitebase.outbound — what a descriptor becomes on its way to the client.

Every port that sends a descriptor (a page, a menu item, a type, the columns of
a table, the view of a dialog) passes it through `to_client`, and one table says
what happens to each key on the way:

    label    translated to the reader's locale with `_()`
    data     handed to the data as written, never translated: a column named
             `title` in a filter or a default is a value, and translating it
             would write another one
    meta     framework metadata, dropped
    server   written for whoever develops, dropped: the client never needs it,
             and the boundary is the server, since a client can be modified by
             whoever runs it

A key the table does not name is kept, and walked. The ports decide nothing: a
new port cannot forget a step, and a key that must stay on the server is one
line here, or one `set_policy` from the plugin that owns it.

`to_client` takes a descriptor, never a map of names. Under `tables`, `types`
or `schemas` the keys are names, and a table called `doc` is not the key `doc`:
a port holding such a map calls `to_client` on each value.
"""
from typing import Any, Dict

from kitebase.i18n import _

LABEL = 'label'
DATA = 'data'
META = 'meta'
SERVER = 'server'
POLICIES = (LABEL, DATA, META, SERVER)

_policies: Dict[str, str] = {
    'label': LABEL,
    'title': LABEL,
    'placeholder': LABEL,
    'source': DATA,
    'domain': DATA,
    'defaults': DATA,
    'params': DATA,
    'values': DATA,
    'query': DATA,
    'data': DATA,
    '$plugin': META,
    'doc': SERVER,
    'help_compose': SERVER,
    'help_embed': SERVER,
    'help_how': SERVER,
}

_JSON_SCALARS = (str, int, float, bool, type(None))


def set_policy(key: str, policy: str) -> None:
    """Declare what happens to `key` on its way to the client."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy '{policy}' for '{key}': one of {', '.join(POLICIES)}")
    _policies[key] = policy


def policy_of(key: str) -> Any:
    """The policy of `key`, or None when the table does not name it."""
    return _policies.get(key)


def to_client(obj: Any) -> Any:
    """A copy of `obj` as the client receives it.

    What cannot travel as JSON (a table or column object left in a descriptor)
    becomes None. A choice's label is translated only when the choice has a
    `value`: the value is what the field stores, and the label must not be one.
    """
    if isinstance(obj, list):
        return [to_client(item) for item in obj]
    if not isinstance(obj, dict):
        return obj if isinstance(obj, _JSON_SCALARS) else None
    out: Dict[str, Any] = {}
    for key, value in obj.items():
        policy = _policies.get(key)
        if policy in (META, SERVER):
            continue
        if policy == LABEL and isinstance(value, str):
            out[key] = _(value)
        elif policy == DATA:
            out[key] = _as_data(value)
        elif key == 'choices' and isinstance(value, list):
            out[key] = [to_client(c) if isinstance(c, dict) and 'value' in c else _as_data(c)
                        for c in value]
        else:
            out[key] = to_client(value)
    return out


def _as_data(obj: Any) -> Any:
    """Data as written: nothing translated, only metadata and non-JSON dropped."""
    if isinstance(obj, list):
        return [_as_data(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _as_data(v) for k, v in obj.items() if _policies.get(k) != META}
    return obj if isinstance(obj, _JSON_SCALARS) else None
