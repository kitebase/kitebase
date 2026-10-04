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
from typing import Dict

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
