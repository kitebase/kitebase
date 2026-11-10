"""The port of the settings: the core reads a value, a plugin provides it.

What is covered: without a provider `get` answers None and a page token is taken
out, so the field it would fill stays empty, and `check` warns for each token;
with one, a token becomes its value in a mapping and in a list, the page the
client receives carries the value, and a key the provider does not have is an
error of `check`, and is taken out at runtime; a second provider is refused.
"""
import pytest

import kitebase.utils
from kitebase import settings
from kitebase.diagnostics import run_checks
from kitebase.endpoint_panels import get_page
from kitebase.plugins import PluginsManager


class Provider(settings.SettingsProvider):
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values[key]

    def has(self, key):
        return key in self.values


class FakeApp:
    def __init__(self, pm):
        self.plugins = pm
        self.tables = {}


@pytest.fixture(autouse=True)
def no_provider():
    saved = settings._PROVIDER
    settings._PROVIDER = None
    yield
    settings._PROVIDER = saved


ORDER_FORM = {'order_form': {'content': {
    'type': 'form',
    'source': {'defaults': {'default_discount': '$setting.acquisitions.default_discount',
                            'notes': 'Thanks'}},
    'layout': [{'name': 'notes'}],
}}}


def make_app(pages=ORDER_FORM):
    pm = PluginsManager()
    pm.config = {}
    pm.merge_dicts({'pages': pages}, 'shop')
    return FakeApp(pm)


def by_code(issues, code):
    return [i for i in issues if i['code'] == code]


def test_without_a_provider_there_are_no_values():
    assert settings.get('acquisitions.default_discount') is None
    defaults = {'default_discount': '$setting.acquisitions.default_discount', 'notes': 'x'}
    assert settings.resolve_tokens(defaults) == {'notes': 'x'}


def test_without_a_provider_check_warns_for_each_token():
    [issue] = by_code(run_checks(make_app()), 'setting-no-provider')
    assert issue['severity'] == 'warning'
    assert issue['path'] == 'pages.order_form.content.source.defaults.default_discount'
    assert issue['plugin'] == 'shop'


def test_a_token_becomes_its_value():
    settings.set_settings_provider(Provider({'a.x': 10, 'a.y': [1, 2]}))
    tree = {'d': {'x': '$setting.a.x', 'keep': '$record.x'}, 'l': ['$setting.a.y', 'z']}
    assert settings.resolve_tokens(tree) == {'d': {'x': 10, 'keep': '$record.x'},
                                             'l': [[1, 2], 'z']}
    assert settings.get('a.x') == 10


def test_the_client_receives_the_value(monkeypatch):
    settings.set_settings_provider(Provider({'acquisitions.default_discount': 10}))
    app = make_app()
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: app)

    result = get_page({'id': 'order_form'})

    assert result['status'] == 'success'
    assert result['data']['content']['source']['defaults'] == {
        'default_discount': 10, 'notes': 'Thanks'}
    # the merged tree keeps the token for the next request
    assert app.plugins.data['pages']['order_form']['content']['source']['defaults'][
        'default_discount'] == '$setting.acquisitions.default_discount'


def test_a_key_the_provider_does_not_have_is_an_error_and_is_taken_out():
    settings.set_settings_provider(Provider({}))
    [issue] = by_code(run_checks(make_app()), 'setting-unknown')
    assert issue['severity'] == 'error'
    with pytest.raises(KeyError):
        settings.get('acquisitions.default_discount')
    assert settings.resolve_tokens({'d': '$setting.nope'}) == {}


def test_a_second_provider_is_refused():
    settings.set_settings_provider(Provider({}))
    with pytest.raises(ValueError, match='already registered'):
        settings.set_settings_provider(Provider({}))
