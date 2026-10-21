"""
kitebase.i18n — lightweight gettext-style translation.

Usage:
    from kitebase.i18n import _, set_locale

    set_locale('it')          # called by auth middleware from JWT context
    _('Record not found')     # → 'Record non trovato'

English is the key — no separate EN file needed.
Falls back to the key itself if no translation is found.
"""
from contextvars import ContextVar
from typing import Any, Dict

_locale: ContextVar[str] = ContextVar('locale', default='en')
_translations: Dict[str, Dict[str, str]] = {}


def set_locale(locale: str) -> None:
    """Set the locale for the current request context."""
    _locale.set(locale or 'en')


def register_translations(locale: str, data: Dict[str, str]) -> None:
    """Register a translation dictionary for a locale."""
    _translations[locale] = {**_translations.get(locale, {}), **data}


def _(key: str) -> str:
    """Translate key to the current request locale. Falls back to key."""
    locale = _locale.get()
    if locale == 'en':
        return key
    return _translations.get(locale, {}).get(key, key)


def _f(key: str, **kwargs: object) -> str:
    """Translate key and substitute {var} placeholders via str.format_map."""
    try:
        return _(key).format_map(kwargs)
    except (KeyError, ValueError):
        return key.format_map(kwargs)


# What a descriptor shows to whoever reads it, and so passes from `_()`.
LABEL_KEYS = ('label', 'title', 'placeholder')

# What a descriptor hands to the data: a column named `title` in a filter or a
# default is a value, and translating it would write another one.
DATA_KEYS = ('source', 'domain', 'defaults', 'params', 'values', 'query', 'data')


def translate_labels(obj: Any) -> Any:
    """A copy of a descriptor with its labels translated to the current locale.

    The YAML is written in English, the key of `_()`, and is translated when it
    leaves for the client, with the plugins' `locale/<lang>.py`. A choice is
    translated only when it has a `value`: the value is what the field stores,
    and the label must not be one.
    """
    if isinstance(obj, list):
        return [translate_labels(item) for item in obj]
    if not isinstance(obj, dict):
        return obj
    out: Dict[str, Any] = {}
    for key, value in obj.items():
        if key in LABEL_KEYS and isinstance(value, str):
            out[key] = _(value)
        elif key in DATA_KEYS:
            out[key] = value
        elif key == 'choices' and isinstance(value, list):
            out[key] = [translate_labels(c) if isinstance(c, dict) and 'value' in c else c
                        for c in value]
        else:
            out[key] = translate_labels(value)
    return out
