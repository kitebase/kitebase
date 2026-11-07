"""
kitebase.help_texts: the translations of the guide, one file per plugin and language.

English is written in the YAML, beside the object it explains, and stays the source.
Every other language lives in `help/<lang>.yaml` of the plugin that declares the
text, as a list of entries the tool writes and a person or a model translates:

    tables.PurchaseOrder.columns.order_date:
      en: "The date of the order. A new order starts from the working date."
      en_old: "The date of the order."      # only while the translation is behind
      it: "La data dell'ordine."

The key is where the text is declared in the tree of the plugins, before any type
is expanded: the text of a type has one key and is translated once. A list item
is named by its identity (`id`, `name`, `field`, `group`), as the merge names it.
`help` is the object itself; `help_how` adds its own name.

`sync_help` keeps the files in step with the English: it adds what is missing and,
when the English changes, keeps the previous one in `en_old`, so the diff shows
what to review. Whoever reviews removes `en_old`. Nothing is ever deleted.

What the user reads: the translation when there is one, even on an older English,
since a little behind beats another language; the English otherwise. A translation
counts only from the plugin that declares the text or one after it: a plugin that
rewrites the English of another one also owns its translation.

The long texts stay files: `help/<lang>/<page>.md`, then `help/en/<page>.md`.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

# The attributes whose text reaches the user. `doc:` is for whoever develops.
TEXT_ATTRS = ('help', 'help_how')

SOURCE = 'en'
OLD = 'en_old'

# How the merge names a list item (PluginsManager._IDENTITY_KEYS)
_IDENTITY_KEYS = ('id', 'name', 'field', 'group')

_SPACES = re.compile(r'\s+')

_HEADER = (
    '# Translations of the guide, kept in step with the English by `kitebase sync-help`.\n'
    '# Translate the value under the language; an empty one shows the English.\n'
    '# `en_old` is the English the translation was made on: review it, then remove the line.\n'
)


def normalize(text: str) -> str:
    """Spaces and line breaks do not change a text: rewrapping it is not a new English."""
    return _SPACES.sub(' ', text).strip()


def _segment(item: Any, index: int) -> str:
    if isinstance(item, dict):
        for key in _IDENTITY_KEYS:
            if isinstance(item.get(key), (str, int)):
                return str(item[key])
    return str(index)


def declared_texts(data: Any, path: Tuple[str, ...] = ()) -> Iterator[Tuple[str, str]]:
    """(key, text) of every text for the user in a tree, in the order it is written."""
    if isinstance(data, dict):
        if path:
            for attr in TEXT_ATTRS:
                text = data.get(attr)
                if isinstance(text, str) and text.strip():
                    key = '.'.join(path if attr == 'help' else path + (attr,))
                    yield key, text.strip()
        for key, value in data.items():
            if isinstance(key, str) and not key.startswith('$') and key not in TEXT_ATTRS:
                yield from declared_texts(value, path + (key,))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            yield from declared_texts(item, path + (_segment(item, index),))


def plugin_texts(plugin: Any) -> Dict[str, str]:
    """The texts a plugin declares in its own files, before the merge."""
    texts: Dict[str, str] = {}
    for data in plugin.data:
        for key, text in declared_texts(data):
            texts[key] = text
    return texts


def help_dir(plugin: Any) -> Path:
    return Path(plugin.plugin_dir) / 'help'


def read_file(path: Path) -> Dict[str, Dict[str, str]]:
    """A translation file; a missing one is empty.

    Raises:
        ValueError: if the file is not a mapping of entries
    """
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ValueError(f'{path}: expected a mapping of key: {{en: ..., <lang>: ...}}')
    return data


def languages(plugin: Any) -> List[str]:
    """The languages a plugin already translates into."""
    folder = help_dir(plugin)
    if not folder.is_dir():
        return []
    return sorted(f.stem for f in folder.glob('*.yaml') if f.stem != SOURCE)


# ── Reading ─────────────────────────────────────────────────────────────────────


class HelpCatalog:
    """The texts of one language, looked up by their current English.

    The guide reads the texts after the types are expanded and the views referenced,
    where their key is lost; the English leads back to it. Two keys with the same
    English take the first translation found."""

    def __init__(self, app: Any, lang: str):
        self.lang = lang
        self.by_text: Dict[str, str] = {}
        if lang == SOURCE:
            return
        declared: Dict[str, Tuple[int, str]] = {}
        translated: Dict[str, Tuple[int, str]] = {}
        for index, name in enumerate(app.pm.sorted):
            plugin = app.pm.plugins[name]
            for key, text in plugin_texts(plugin).items():
                declared[key] = (index, text)
            for key, entry in read_file(help_dir(plugin) / f'{lang}.yaml').items():
                value = entry.get(lang)
                if isinstance(value, str) and value.strip():
                    translated[key] = (index, value.strip())
        for key, (index, text) in declared.items():
            found = translated.get(key)
            if found is not None and found[0] >= index:
                self.by_text.setdefault(normalize(text), found[1])

    def translate(self, text: str) -> Optional[str]:
        """The translation of an English text, or None."""
        return self.by_text.get(normalize(text)) if self.by_text else None


def discourse(app: Any, page_id: str, lang: str = SOURCE) -> Optional[str]:
    """The long text of a page: the plugin that comes last wins, like a merge, and
    within it the language before the English. A translation from a plugin before
    the one that wrote the English does not count, like the short texts."""
    english: Optional[Tuple[int, Path]] = None
    translated: Optional[Tuple[int, Path]] = None
    for index, name in enumerate(app.pm.sorted):
        folder = help_dir(app.pm.plugins[name])
        if (folder / SOURCE / f'{page_id}.md').is_file():
            english = (index, folder / SOURCE / f'{page_id}.md')
        if lang != SOURCE and (folder / lang / f'{page_id}.md').is_file():
            translated = (index, folder / lang / f'{page_id}.md')
    if translated is not None and (english is None or translated[0] >= english[0]):
        return translated[1].read_text(encoding='utf-8')
    return english[1].read_text(encoding='utf-8') if english else None


# ── Writing ─────────────────────────────────────────────────────────────────────


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> Any:
    style = '|' if '\n' in value else None
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style=style)


_Dumper.add_representer(str, _str)


def dump_file(entries: Dict[str, Dict[str, str]]) -> str:
    body = yaml.dump(entries, Dumper=_Dumper, allow_unicode=True, sort_keys=False,
                     width=10 ** 6, default_flow_style=False)
    # A blank line between entries, for whoever reads the file
    lines: List[str] = []
    for line in body.splitlines():
        if lines and line and not line[0].isspace():
            lines.append('')
        lines.append(line)
    return _HEADER + '\n' + '\n'.join(lines) + '\n'


def sync_entries(texts: Dict[str, str], old: Dict[str, Dict[str, str]],
                 lang: str) -> Dict[str, Dict[str, str]]:
    """The entries of a file after the English of `texts`: in the order of the
    declarations, then whatever no declaration holds any more, untouched."""
    entries: Dict[str, Dict[str, str]] = {}
    for key, english in texts.items():
        before = old.get(key) or {}
        entry = {SOURCE: english}
        previous = before.get(SOURCE)
        if isinstance(before.get(OLD), str):
            entry[OLD] = before[OLD]
        elif isinstance(previous, str) and previous.strip() \
                and normalize(previous) != normalize(english) \
                and (before.get(lang) or '').strip():
            entry[OLD] = previous
        entry[lang] = before.get(lang) or ''
        for k, v in before.items():
            if k not in entry:
                entry[k] = v
        entries[key] = entry
    for key, entry in old.items():
        if key not in entries:
            entries[key] = entry
    return entries


@dataclass
class SyncResult:
    path: Path
    added: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    written: bool = False


def sync_help(app: Any, new_lang: Optional[str] = None,
              plugins: Optional[List[str]] = None) -> List[SyncResult]:
    """Bring the translation files of the plugins in step with their English.

    Only the languages a plugin already has, plus `new_lang`, which starts a file
    in every plugin that declares texts. `plugins` narrows the plugins touched.

    Raises:
        ValueError: if a plugin is unknown, or a file is not a mapping of entries
    """
    if new_lang == SOURCE:
        raise ValueError('English is written in the YAML, not in a translation file')
    unknown = [p for p in plugins or [] if p not in app.pm.plugins]
    if unknown:
        raise ValueError(f"Unknown plugins: {', '.join(unknown)}")
    results: List[SyncResult] = []
    for name in app.pm.sorted:
        if plugins and name not in plugins:
            continue
        plugin = app.pm.plugins[name]
        texts = plugin_texts(plugin)
        langs = languages(plugin)
        if new_lang and texts and new_lang not in langs:
            langs.append(new_lang)
        for lang in langs:
            path = help_dir(plugin) / f'{lang}.yaml'
            old = read_file(path)
            entries = sync_entries(texts, old, lang)
            result = SyncResult(path)
            for key in texts:
                if key not in old:
                    result.added.append(key)
                elif entries[key].get(OLD) and not old[key].get(OLD):
                    result.changed.append(key)
            text = dump_file(entries)
            if not path.is_file() or path.read_text(encoding='utf-8') != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
                result.written = True
            results.append(result)
    return results


# ── Checking ────────────────────────────────────────────────────────────────────


def diagnose(app: Any) -> List[Dict[str, Any]]:
    """What a translation file says about itself, only for the languages a plugin
    already has: a translation to review, an entry no text declares any more, and
    how far behind the file is. An incomplete translation is not an error."""
    from kitebase.diagnostics import make_issue
    issues: List[Dict[str, Any]] = []
    for name in app.pm.sorted:
        plugin = app.pm.plugins[name]
        texts = plugin_texts(plugin)
        for lang in languages(plugin):
            path = f'{name}/help/{lang}.yaml'
            try:
                entries = read_file(help_dir(plugin) / f'{lang}.yaml')
            except (ValueError, yaml.YAMLError) as e:
                issues.append(make_issue('error', 'help-file', path, str(e), name))
                continue
            missing = [k for k in texts if k not in entries]
            if missing:
                issues.append(make_issue(
                    'warning', 'help-unsynced', path,
                    f'{len(missing)} texts are not in the file: run kitebase sync-help', name))
            empty = [k for k in texts if k in entries and not (entries[k].get(lang) or '').strip()]
            if empty:
                issues.append(make_issue(
                    'info', 'help-untranslated', path,
                    f'{len(empty)} of {len(texts)} texts without a translation', name))
            for key, entry in entries.items():
                if key not in texts:
                    issues.append(make_issue(
                        'warning', 'help-orphan', f'{path}: {key}',
                        'no text declares this key any more: remove the entry, '
                        'or move its translation to the new key', name))
                elif entry.get(OLD):
                    issues.append(make_issue(
                        'warning', 'help-stale', f'{path}: {key}',
                        'the English changed after the translation: review it and remove en_old',
                        name))
    return issues
