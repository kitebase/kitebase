"""get_server_config: what the client reads at startup from config.yaml."""
import kitebase.utils
from kitebase.endpoint_db import get_server_config
from kitebase.plugins import PluginsManager


class FakeApp:
    def __init__(self, config):
        self.pm = PluginsManager()
        self.pm.config = config

    def get_type_schema(self, include_builtin):
        return {}

    def get_table_schema(self):
        return {}

    def get_schema_registry(self):
        return {}


def config_for(monkeypatch, config):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: FakeApp(config))
    return get_server_config({})['data']['config']


def test_the_title_is_the_one_config_declares(monkeypatch):
    assert config_for(monkeypatch, {'title': 'My App', 'locale': 'it'}) == {
        'locale': 'it', 'app_title': 'My App'}


def test_without_a_title_the_client_chooses(monkeypatch):
    assert config_for(monkeypatch, {})['app_title'] is None


def test_a_column_s_granularity_reaches_the_client(tmp_path, monkeypatch):
    """`granularity: second` on a datetime is what lists and forms both read."""
    from tests.test_codegen_relations import build, table
    db = build(tmp_path, monkeypatch, {'Visit': table(
        {'name': 'arrived', 'type': 'DateTime', 'granularity': 'second'},
        {'name': 'left', 'type': 'DateTime'},
        name='visits')})
    columns = {c['name']: c for c in db.get_table_schema()['Visit']['columns']}
    assert columns['arrived']['granularity'] == 'second'
    assert 'granularity' not in columns['left']
