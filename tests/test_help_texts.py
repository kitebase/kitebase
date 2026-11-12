"""The translations of the guide: keys from the declarations, files kept in step
with the English, the translation or the English, never a deletion."""
from pathlib import Path

import pytest
import yaml

import kitebase.utils
from kitebase.app import App
from kitebase.help import help_merge, render_html
from kitebase.help_texts import catalog, declared_texts, diagnose, read_file, sync_help
from kitebase.i18n import use_locale
from kitebase.outbound import to_client
from kitebase.plugins import PluginsManager

BASE_MODEL = {
    'types': {'Code': {'base': 'String', 'length': 10, 'help_how': 'Letters and digits.'}},
    'tables': {'Doc': {'name': 'docs', 'help': 'A document.', 'doc': 'TECHNICAL', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'title', 'type': 'String', 'length': 60, 'help': 'What it is about.'},
        {'name': 'code', 'type': 'Code', 'help': 'The code.'},
        {'name': 'note', 'type': 'String', 'length': 60, 'help': 'A note.'},
    ]}},
}

BASE_PAGES = {
    'pages': {'doc_list': {
        'title': 'Documents',
        'help': 'The documents.',
        'content': {
            'type': 'table',
            'source': {'model': 'Doc'},
            'columns': [{'field': 'title'}, {'field': 'code'}, {'field': 'note'}],
            'navigator': {'commands': [
                {'id': 'stamp', 'label': 'Stamp', 'help': 'Stamps it.', 'scope': 'row',
                 'endpoint': 'stamp'},
            ]},
        },
    }},
    'menu_items': {'docs': {'label': 'Documents', 'action': 'stack_push', 'panel': 'doc_list'}},
}

# A plugin after the base: rewrites the English of one text and translates another
APP_MODEL = {'tables': {'Doc': {'columns': [{'name': 'note', 'help': 'A note, rewritten.'}]}}}


def write_plugin(root: Path, name: str, files, depends=()):
    plugin = root / 'plugins' / name
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump(
        {'name': name, 'version': '0.0.1', 'depends_on': list(depends)}), encoding='utf-8')
    for filename, data in files.items():
        (plugin / filename).write_text(yaml.safe_dump(data), encoding='utf-8')
    return plugin


def load(path: Path, monkeypatch) -> App:
    monkeypatch.chdir(path)
    pm = PluginsManager()
    pm.load_config('config.yaml')
    pm.load_plugins()
    pm.load_all_locales()
    app = App()
    app.calc_db(pm)
    app.dispatcher = None
    return app


@pytest.fixture
def bench(tmp_path, monkeypatch):
    base = write_plugin(tmp_path, 'base', {'model.yaml': BASE_MODEL, 'pages.yaml': BASE_PAGES})
    extra = write_plugin(tmp_path, 'extra', {'model.yaml': APP_MODEL}, depends=['base'])
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'plugins': ['plugins']}), encoding='utf-8')

    def reload():
        return load(tmp_path, monkeypatch)
    return reload, base, extra


def entries(plugin: Path, lang='it'):
    return read_file(plugin / 'help' / f'{lang}.yaml')


def translate(plugin: Path, key: str, text: str, lang='it'):
    path = plugin / 'help' / f'{lang}.yaml'
    data = read_file(path)
    data.setdefault(key, {'en': ''})[lang] = text
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')


def rewrite_english(plugin: Path, column: str, text: str):
    model = yaml.safe_load((plugin / 'model.yaml').read_text(encoding='utf-8'))
    for c in model['tables']['Doc']['columns']:
        if c['name'] == column:
            c['help'] = text
    (plugin / 'model.yaml').write_text(yaml.safe_dump(model), encoding='utf-8')


# ── Keys ────────────────────────────────────────────────────────────────────


def test_the_key_is_where_the_text_is_declared():
    keys = dict(declared_texts({**BASE_MODEL, **BASE_PAGES}))
    assert keys['tables.Doc'] == 'A document.'
    assert keys['tables.Doc.columns.title'] == 'What it is about.'
    assert keys['types.Code.help_how'] == 'Letters and digits.'
    assert keys['pages.doc_list.content.navigator.commands.stamp'] == 'Stamps it.'
    assert 'TECHNICAL' not in keys.values()


def test_metadata_keys_are_not_walked():
    assert dict(declared_texts({'pages': {'$plugin': {'help': 'no'}, 'p': {'help': 'yes'}}})) == {
        'pages.p': 'yes'}


# ── sync-help ───────────────────────────────────────────────────────────────


def test_a_language_starts_only_when_asked(bench):
    reload, base, extra = bench
    assert sync_help(reload()) == []
    sync_help(reload(), 'it')
    assert entries(base)['tables.Doc.columns.title'] == {'en': 'What it is about.', 'it': ''}
    # Each plugin keeps the texts it declares, even the ones a later plugin rewrites
    assert entries(extra) == {'tables.Doc.columns.note': {'en': 'A note, rewritten.', 'it': ''}}
    assert entries(base)['tables.Doc.columns.note']['en'] == 'A note.'


def test_english_is_not_a_translation_file(bench):
    reload, _, _ = bench
    with pytest.raises(ValueError, match='English'):
        sync_help(reload(), 'en')


def test_only_the_languages_a_plugin_has_are_kept_in_step(bench):
    reload, base, extra = bench
    translate(base, 'tables.Doc', 'Un documento.')
    results = sync_help(reload())
    assert [r.path.parent.parent.name for r in results] == ['base']
    assert entries(base)['tables.Doc'] == {'en': 'A document.', 'it': 'Un documento.'}
    assert not (extra / 'help').exists()


def test_a_changed_english_keeps_the_old_one_beside_the_translation(bench):
    reload, base, _ = bench
    sync_help(reload(), 'it')
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    rewrite_english(base, 'title', 'What the document is about.')
    rewrite_english(base, 'code', 'The code, changed.')
    result = sync_help(reload())[0]
    assert result.changed == ['tables.Doc.columns.title']
    assert entries(base)['tables.Doc.columns.title'] == {
        'en': 'What the document is about.', 'en_old': 'What it is about.', 'it': 'Di cosa parla.'}
    # Nothing translated, nothing to review
    assert entries(base)['tables.Doc.columns.code'] == {'en': 'The code, changed.', 'it': ''}


def test_en_old_stays_the_english_the_translation_was_made_on(bench):
    reload, base, _ = bench
    sync_help(reload(), 'it')
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    rewrite_english(base, 'title', 'Second.')
    sync_help(reload())
    rewrite_english(base, 'title', 'Third.')
    sync_help(reload())
    assert entries(base)['tables.Doc.columns.title']['en_old'] == 'What it is about.'


def test_rewrapping_the_english_is_not_a_change(bench):
    reload, base, _ = bench
    sync_help(reload(), 'it')
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    rewrite_english(base, 'title', 'What it\n  is   about.')
    assert sync_help(reload())[0].changed == []
    assert 'en_old' not in entries(base)['tables.Doc.columns.title']


def test_nothing_is_ever_deleted(bench):
    reload, base, _ = bench
    translate(base, 'tables.Doc.columns.gone', 'Sparito.')
    sync_help(reload())
    assert entries(base)['tables.Doc.columns.gone']['it'] == 'Sparito.'


def test_a_second_sync_writes_nothing(bench):
    reload, base, _ = bench
    sync_help(reload(), 'it')
    assert [r.written for r in sync_help(reload())] == [False, False]


def test_a_multiline_english_is_written_as_a_block(bench):
    reload, base, _ = bench
    rewrite_english(base, 'title', 'One line.\nAnother line.')
    sync_help(reload(), 'it')
    assert 'en: |-\n    One line.\n    Another line.' in (base / 'help' / 'it.yaml').read_text(encoding='utf-8')


# ── What the user reads ─────────────────────────────────────────────────────


def guide(reload, lang='it') -> str:
    app = reload()
    return render_html(app, help_merge(app, 'doc_list', lang)).html


def test_the_translation_where_there_is_one_the_english_otherwise(bench):
    reload, base, _ = bench
    sync_help(reload(), 'it')
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    translate(base, 'types.Code.help_how', 'Lettere e cifre.')
    page = guide(reload)
    assert 'Di cosa parla.' in page
    assert 'Lettere e cifre.' in page
    assert 'The code.' in page
    assert '<html lang="it">' in page
    # The words of the core, like the interface
    assert 'Comandi' in page and 'Campi' in page


def test_a_translation_a_little_behind_is_still_shown(bench):
    reload, base, _ = bench
    sync_help(reload(), 'it')
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    rewrite_english(base, 'title', 'What the document is about.')
    sync_help(reload())
    assert 'Di cosa parla.' in guide(reload)


def test_the_english_guide_reads_no_translation(bench):
    reload, base, _ = bench
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    page = guide(reload, 'en')
    assert 'What it is about.' in page and 'Di cosa parla.' not in page
    assert '<html lang="en">' in page


def test_a_plugin_that_rewrites_the_english_owns_its_translation(bench):
    reload, base, extra = bench
    translate(base, 'tables.Doc.columns.note', 'Una nota.')
    page = guide(reload)
    assert 'A note, rewritten.' in page and 'Una nota.' not in page
    translate(extra, 'tables.Doc.columns.note', 'Una nota, riscritta.')
    assert 'Una nota, riscritta.' in guide(reload)


def test_a_later_plugin_may_translate_the_text_of_an_earlier_one(bench):
    reload, base, extra = bench
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    translate(extra, 'tables.Doc.columns.title', "Di cosa parla, secondo l'app.")
    assert "Di cosa parla, secondo l'app." in guide(reload)


def test_the_long_text_in_the_language_or_in_english(bench):
    reload, base, _ = bench
    (base / 'help' / 'en').mkdir(parents=True)
    (base / 'help' / 'en' / 'doc_list.md').write_text('Read me.\n', encoding='utf-8')
    assert 'Read me.' in guide(reload)
    (base / 'help' / 'it').mkdir()
    (base / 'help' / 'it' / 'doc_list.md').write_text('Leggimi.\n', encoding='utf-8')
    page = guide(reload)
    assert 'Leggimi.' in page and 'Read me.' not in page
    assert 'Read me.' in guide(reload, 'en')


def test_a_page_on_the_web_opens_in_a_new_tab(bench):
    reload, base, _ = bench
    (base / 'help' / 'en').mkdir(parents=True)
    (base / 'help' / 'en' / 'doc_list.md').write_text(
        'See [the site](https://example.com) or [write](mailto:a@example.com).\n', encoding='utf-8')
    page = guide(reload, 'en')
    assert '<a href="https://example.com" target="_blank" rel="noopener noreferrer">' in page
    assert '<a href="mailto:a@example.com">' in page


# ── check ───────────────────────────────────────────────────────────────────


def codes(app):
    return sorted((i['code'], i['path']) for i in diagnose(app))


def test_check_is_silent_without_translation_files(bench):
    reload, _, _ = bench
    assert diagnose(reload()) == []


def test_check_tells_what_to_review_and_what_is_left_over(bench):
    reload, base, _ = bench
    sync_help(reload(), 'it', ['base'])
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    translate(base, 'tables.Doc.columns.gone', 'Sparito.')
    rewrite_english(base, 'title', 'Changed.')
    sync_help(reload())
    rewrite_english(base, 'code', 'Changed too.')
    model = yaml.safe_load((base / 'model.yaml').read_text(encoding='utf-8'))
    model['tables']['Doc']['columns'].append({'name': 'extra', 'type': 'String', 'help': 'New.'})
    (base / 'model.yaml').write_text(yaml.safe_dump(model), encoding='utf-8')
    found = {code: [i for i in diagnose(reload()) if i['code'] == code]
             for code in ('help-stale', 'help-orphan', 'help-unsynced', 'help-untranslated')}
    assert [i['path'] for i in found['help-stale']] == ['base/help/it.yaml: tables.Doc.columns.title']
    assert [i['path'] for i in found['help-orphan']] == ['base/help/it.yaml: tables.Doc.columns.gone']
    assert '1 texts' in found['help-unsynced'][0]['message']
    assert found['help-untranslated'][0]['severity'] == 'info'
    assert all(i['severity'] != 'error' for i in diagnose(reload()))


def test_a_file_that_is_not_a_mapping_is_an_error(bench):
    reload, base, _ = bench
    (base / 'help').mkdir()
    (base / 'help' / 'it.yaml').write_text('- just a list\n', encoding='utf-8')
    assert [i['code'] for i in diagnose(reload())] == ['help-file']


# ── On the way to the client ────────────────────────────────────────────────


def test_the_help_of_a_page_reaches_the_client_translated(bench, monkeypatch):
    reload, base, _ = bench
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    app = reload()
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: app)
    field = {'name': 'title', 'label': 'Title', 'help': 'What it is about.'}
    with use_locale('it'):
        assert to_client(field)['help'] == 'Di cosa parla.'
        assert to_client({'help': 'Nobody translates this.'})['help'] == 'Nobody translates this.'
    assert to_client(field)['help'] == 'What it is about.'


def test_the_catalog_is_kept_until_a_translation_changes(bench):
    reload, base, _ = bench
    translate(base, 'tables.Doc.columns.title', 'Di cosa parla.')
    app = reload()
    first = catalog(app, 'it')
    assert catalog(app, 'it') is first
    translate(base, 'tables.Doc.columns.title', 'Di che cosa parla.')
    assert catalog(app, 'it') is not first
    assert catalog(app, 'it').translate('What it is about.') == 'Di che cosa parla.'
