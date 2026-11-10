"""The roots of the merged tree have an owner: the module that reads them.

What is covered: a module registers a root and may register it again, a second
module may not; `$` keys are not roots; `check` warns about a root nobody owns,
naming the plugins that declare it, and neither walks nor dumps it; a root
registered without `descriptor` is not walked either; the owner's `check=` runs
with the core's checks.
"""
import pytest

from kitebase import sections, vocabulary
from kitebase.diagnostics import _descriptor_sections, owned_sections, run_checks
from kitebase.plugins import PluginsManager
from kitebase.sections import get_section, register_section


class FakeApp:
    def __init__(self, pm):
        self.plugins = pm
        self.tables = {}


@pytest.fixture(autouse=True)
def registry():
    """Each test registers on a copy, so the process registry stays as it was."""
    saved = sections._SECTIONS.snapshot()
    yield
    sections._SECTIONS.restore(saved)


def register_from(module, name, **kwargs):
    """Call register_section as if from another module."""
    code = compile(f"register_section({name!r}, **kwargs)", module, 'exec')
    exec(code, {'__name__': module, 'register_section': register_section,
                'kwargs': kwargs})


def by_code(issues, code):
    return [i for i in issues if i['code'] == code]


def test_the_core_owns_its_roots():
    for name in ('tables', 'types', 'schemas'):
        assert not get_section(name).descriptor
    for name in ('pages', 'views', 'menus', 'menu_items'):
        assert get_section(name).descriptor
    assert get_section('pages').owner == 'kitebase.sections'


def test_the_owner_may_register_again_another_module_may_not():
    register_from('acme.reader', 'gadgets')
    register_from('acme.reader', 'gadgets', descriptor=True)
    assert get_section('gadgets').descriptor
    with pytest.raises(ValueError, match='already registered by acme.reader'):
        register_from('other.reader', 'gadgets')


def test_a_dollar_key_is_not_a_root():
    with pytest.raises(ValueError):
        register_section('$plugin')


def test_a_root_nobody_owns_is_a_warning_and_is_not_walked():
    pm = PluginsManager()
    pm.merge_dicts({'gadgets': {'g': {'action': 'stack_push', 'panel': 'nowhere'}}},
                   'shop')
    issues = run_checks(FakeApp(pm))

    unowned = by_code(issues, 'section-unowned')
    assert [(i['severity'], i['path'], i['plugin']) for i in unowned] == \
        [('warning', 'gadgets', 'shop')]
    assert by_code(issues, 'panel-missing') == []
    assert owned_sections(pm) == []


def test_an_owned_root_without_descriptor_is_not_walked():
    register_from('acme.reader', 'gadgets')
    pm = PluginsManager()
    pm.merge_dicts({'gadgets': {'g': {'action': 'stack_push', 'panel': 'nowhere'}}},
                   'shop')
    issues = run_checks(FakeApp(pm))

    assert by_code(issues, 'section-unowned') == []
    assert by_code(issues, 'panel-missing') == []
    assert owned_sections(pm) == ['gadgets']
    assert _descriptor_sections(pm) == []


def test_a_descriptor_root_is_walked():
    register_from('acme.reader', 'gadgets', descriptor=True)
    pm = PluginsManager()
    pm.merge_dicts({'gadgets': {'g': {'action': 'stack_push', 'panel': 'nowhere'}}},
                   'shop')
    issues = run_checks(FakeApp(pm))

    assert [i['path'] for i in by_code(issues, 'panel-missing')] == ['gadgets.g.panel']


def test_the_owner_brings_its_checks():
    def check(app):
        return [{'severity': 'error', 'code': 'gadget-bad', 'path': 'gadgets.g',
                 'message': 'bad gadget', 'plugin': 'acme'}
                for k, g in app.plugins.data['gadgets'].items()
                if not k.startswith('$') and g.get('bad')]

    register_from('acme.reader', 'gadgets', check=check)
    pm = PluginsManager()
    pm.merge_dicts({'gadgets': {'g': {'bad': True}}}, 'shop')

    assert [i['path'] for i in by_code(run_checks(FakeApp(pm)), 'gadget-bad')] == \
        ['gadgets.g']


def test_the_check_of_a_root_not_declared_does_not_run():
    calls = []
    register_from('acme.reader', 'gadgets', check=lambda app: calls.append(1) or [])
    run_checks(FakeApp(PluginsManager()))
    assert calls == []


def test_the_vocabulary_judges_only_the_cores_roots():
    """A plugin's root keeps its own keys, dot or not; the core's still answer
    for a typo. A root nobody owns has its warning, not one per key."""
    register_from('acme.reader', 'gadgets')
    pm = PluginsManager()
    pm.merge_dicts({
        'gadgets': {'accounting': {'label': 'Accounting', 'items': [{'name': 'x'}]}},
        'widgets': {'w': {'colour': 'red'}},
        'pages': {'p': {'titel': 'Typo'}},
    }, 'shop')
    unknown = [i['path'] for i in by_code(run_checks(FakeApp(pm)), 'key-unknown')]

    assert unknown == ['pages.p.titel']


def test_a_plugins_root_is_read_with_the_plugins_vocabulary(tmp_path, monkeypatch):
    """The grammar of a root is its owner's: a typo in it is named, with the
    file the key would go into; the update writes there, not in the core's."""
    own = tmp_path / 'acme' / 'vocabulary.yaml'
    own.parent.mkdir()
    own.write_text(vocabulary.render({'gadgets.*': ['items', 'label'],
                                      'gadgets.*.items[]': ['name', 'type']}))
    register_from('acme.reader', 'gadgets', vocabulary=own)
    pm = PluginsManager()
    pm.merge_dicts({'gadgets': {'g': {'label': 'G', 'items': [{'name': 'x', 'tipe': 'Integer'}]}}},
                   'shop')

    [issue] = by_code(run_checks(FakeApp(pm)), 'key-unknown')
    assert issue['path'] == 'gadgets.g.items[0].tipe'
    assert "did you mean 'type'" in issue['message']
    assert 'acme/vocabulary.yaml' in issue['message']

    core_before = vocabulary.VOCABULARY_FILE.read_text()
    monkeypatch.setenv('KITEBASE_VOCABULARY_UPDATE', '1')
    run_checks(FakeApp(pm))
    assert 'tipe' in vocabulary.load(own)['gadgets.*.items[]']
    assert vocabulary.VOCABULARY_FILE.read_text() == core_before
