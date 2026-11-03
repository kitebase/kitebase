"""
The port of the settings: the values an installation changes while it runs.

The core reads a setting and resolves it in the pages; it does not know which
settings exist, where they are kept or how they are written. A plugin provides
all of that and registers itself as the provider, as acl registers the guard:

    set_settings_provider(MyProvider())

    get('acquisitions.default_discount')     # from code
    "$setting.acquisitions.default_discount" # in a page descriptor, a whole string

A page is sent with each token replaced by its value, so the browser receives
only the values the page's author put in it. Without a provider there are no
values: `get` answers None and a token is taken out, so a default it would have
given stays empty. `check` says so for every token, and says which tokens name
a key the provider does not have.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

TOKEN = '$setting.'

_log = logging.getLogger(__name__)


class SettingsProvider:
    """What a provider answers. `get` raises KeyError for a key it does not have."""

    def get(self, key: str) -> Any:
        raise KeyError(key)

    def has(self, key: str) -> bool:
        return False


# One per process, like the guard: the plugin module registers when it is imported.
_PROVIDER: Optional[SettingsProvider] = None


def set_settings_provider(provider: SettingsProvider) -> None:
    """Register the provider. One only: two would give two answers to one key."""
    global _PROVIDER
    if _PROVIDER is not None:
        raise ValueError(f"A settings provider is already registered: {type(_PROVIDER).__name__}")
    _PROVIDER = provider


def provider() -> Optional[SettingsProvider]:
    """The registered provider, or None."""
    return _PROVIDER


def get(key: str) -> Any:
    """The value of a setting; None without a provider. A key the provider does
    not have is a KeyError: a misspelt key must not read as an unset one."""
    if _PROVIDER is None:
        return None
    return _PROVIDER.get(key)


def _token_key(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.startswith(TOKEN) and len(value) > len(TOKEN):
        return value[len(TOKEN):]
    return None


def resolve_tokens(obj: Any) -> Any:
    """Replace each `$setting.<key>` string of a descriptor with the value, in
    place. A token with no value (no provider, or a key it does not have) is
    taken out of its mapping, so whatever it would have filled stays empty."""
    if isinstance(obj, dict):
        for k in list(obj):
            key = _token_key(obj[k])
            if key is None:
                resolve_tokens(obj[k])
                continue
            found, value = _lookup(key)
            if found:
                obj[k] = value
            else:
                del obj[k]
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            key = _token_key(item)
            if key is None:
                resolve_tokens(item)
            else:
                obj[i] = _lookup(key)[1]
    return obj


def _lookup(key: str) -> Tuple[bool, Any]:
    if _PROVIDER is None:
        return False, None
    try:
        return True, _PROVIDER.get(key)
    except KeyError:
        _log.error("setting '%s' is not known to the settings provider", key)
        return False, None


def _tokens(obj: Any, path: str, found: List[Tuple[str, str, Optional[str]]],
            plugin: Optional[str] = None) -> None:
    if isinstance(obj, dict):
        plugin = obj.get('$plugin', plugin)
        for k, v in obj.items():
            key = _token_key(v)
            if key is not None:
                found.append((f'{path}.{k}', key, plugin))
            else:
                _tokens(v, f'{path}.{k}', found, plugin)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            key = _token_key(item)
            if key is not None:
                found.append((f'{path}[{i}]', key, plugin))
            else:
                _tokens(item, f'{path}[{i}]', found, plugin)


def diagnose(app: Any, roots: List[str]) -> List[Dict[str, Any]]:
    """The tokens of the descriptor roots: without a provider each one gives
    nothing (a warning, the application works without the value); a key the
    provider does not have is an error, the default it names would never come."""
    from kitebase.diagnostics import make_issue

    found: List[Tuple[str, str, Optional[str]]] = []
    for root in roots:
        for item_id, item in (app.pm.data.get(root) or {}).items():
            if not item_id.startswith('$'):
                _tokens(item, f'{root}.{item_id}', found)

    issues = []
    for path, key, plugin in found:
        if _PROVIDER is None:
            issues.append(make_issue(
                'warning', 'setting-no-provider', path,
                f"'{TOKEN}{key}' gives nothing: no settings provider is loaded "
                f"(the settings plugin), so the value stays empty", plugin))
        elif not _PROVIDER.has(key):
            issues.append(make_issue(
                'error', 'setting-unknown', path,
                f"'{TOKEN}{key}': no setting '{key}' is declared", plugin))
    return issues
