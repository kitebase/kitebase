"""The modules a plugin declares under `optional`: they exist only alongside
another plugin, which the application chooses whether to load.

    optional:
      reports: [phonebook]    # imported only if the app loads reports

What is covered: the other plugin absent leaves the module dormant and is not
pulled in; present, it orders first like a dependency and the module loads; a
misspelt module or a malformed declaration is an error; and on the page, a
command whose endpoint was never registered is dropped.
"""
import pytest
import yaml

from kitebase.pages import load_page, strip_meta
from kitebase.plugins import PluginsManager


def plugin(root, name, config=None, modules=()):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / 'config.yaml').write_text(yaml.safe_dump({'name': name, **(config or {})}))
    for module in modules:
        (folder / f'{module}.py').write_text('')


def load(tmp_path, monkeypatch, optional, with_reports=True):
    """An app with `partners`, declaring `optional`, and maybe `reports`."""
    plugin(tmp_path / 'plugins', 'partners', {'optional': optional},
           modules=['model', 'phonebook'])
    # In a root of its own, listed second: discovered after partners, so only
    # `optional` can order it first.
    plugin(tmp_path / 'more', 'reports', modules=['registry'])

    roots = ['plugins'] + ([{'path': 'more', 'include': ['reports']}] if with_reports else [])
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': roots}))
    monkeypatch.chdir(tmp_path)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()
    return pm


def sources(pm):
    return [f'{p.parent.name}.{p.stem}' for p in pm.get_sources()]


def codes(pm):
    return [i['code'] for i in pm.issues]


# ── The loader ────────────────────────────────────────────────────────────────

def test_without_the_other_plugin_the_module_is_dormant(tmp_path, monkeypatch):
    pm = load(tmp_path, monkeypatch, {'reports': ['phonebook']}, with_reports=False)

    assert 'reports' not in pm.plugins       # not pulled in, as depends_on would
    assert sources(pm) == ['partners.model']
    assert codes(pm) == []


def test_with_the_other_plugin_the_module_loads_after_it(tmp_path, monkeypatch):
    """Ordered like a dependency, whatever order the disk gives."""
    pm = load(tmp_path, monkeypatch, {'reports': ['phonebook']})

    assert pm.sorted == ['reports', 'partners']
    assert sorted(sources(pm)[1:]) == ['partners.model', 'partners.phonebook']
    assert sources(pm)[0] == 'reports.registry'


def test_a_plugin_the_app_does_not_include_stays_out(tmp_path, monkeypatch):
    """Found in a root but not asked for: `optional` does not pull it in."""
    root = tmp_path / 'plugins'
    plugin(root, 'partners', {'optional': {'reports': ['phonebook']}},
           modules=['model', 'phonebook'])
    plugin(root, 'reports', modules=['registry'])
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': [
        {'path': 'plugins', 'include': ['partners']}]}))
    monkeypatch.chdir(tmp_path)
    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()

    assert list(pm.plugins) == ['partners']
    assert sources(pm) == ['partners.model']


@pytest.mark.parametrize('loaded, imported', [
    (['reports'], False),
    (['reports', 'email'], True),
])
def test_a_module_under_two_plugins_needs_both(tmp_path, monkeypatch, loaded, imported):
    plugin(tmp_path / 'plugins', 'partners',
           {'optional': {'reports': ['invoice_mail'], 'email': ['invoice_mail']}},
           modules=['model', 'invoice_mail'])
    for name in ('reports', 'email'):
        plugin(tmp_path / 'more', name)
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': [
        'plugins', {'path': 'more', 'include': loaded}]}))
    monkeypatch.chdir(tmp_path)
    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()

    assert ('partners.invoice_mail' in sources(pm)) is imported
    assert codes(pm) == []


def test_a_module_the_plugin_does_not_have_is_an_error(tmp_path, monkeypatch):
    """A misspelt name would otherwise leave the real module loading always."""
    pm = load(tmp_path, monkeypatch, {'reports': ['phonbook']}, with_reports=False)

    [issue] = pm.issues
    assert issue['severity'] == 'error'
    assert issue['code'] == 'optional-unknown-module'
    assert issue['plugin'] == 'partners'
    assert "no module 'phonbook.py'" in issue['message']


@pytest.mark.parametrize('optional', [
    ['reports'],                     # a list, not a mapping
    {'reports': 'phonebook'},        # a string, not a list
    {'reports': [{'name': 'phonebook'}]},
])
def test_a_malformed_declaration_is_an_error(tmp_path, monkeypatch, optional):
    pm = load(tmp_path, monkeypatch, optional)

    assert codes(pm) == ['optional-malformed']


# ── The page ──────────────────────────────────────────────────────────────────

class FakeProcessor:
    def __init__(self, *names):
        self.endpoints = {name: None for name in names}


class FakeApp:
    def __init__(self, pm, cp):
        self.pm = pm
        self.cp = cp
        self.tables = {}


def page_with_commands(cp):
    pm = PluginsManager()
    pm.config = {}
    pm.merge_dicts({'pages': {'partner_list': {'content': {
        'type': 'table', 'source': {'model': 'Partner'},
        'navigator': {'commands': [
            {'id': 'archive', 'endpoint': 'archivable'},
            {'id': 'print', 'endpoint': 'reports.print'},
            {'id': 'refresh'},       # no endpoint: the client's own
        ]},
    }}}}, 'bench')
    page = strip_meta(load_page(FakeApp(pm, cp), 'partner_list'))
    return [c['id'] for c in page['content']['navigator']['commands']]


def test_a_command_whose_endpoint_is_not_registered_is_dropped():
    assert page_with_commands(FakeProcessor('archivable')) == ['archive', 'refresh']


def test_a_registered_endpoint_keeps_its_command():
    assert page_with_commands(FakeProcessor('archivable', 'reports.print')) == \
        ['archive', 'print', 'refresh']


def test_nothing_loaded_is_nothing_to_compare_with():
    assert page_with_commands(None) == ['archive', 'print', 'refresh']
