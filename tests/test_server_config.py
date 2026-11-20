"""get_server_config: what the client reads at startup from config.yaml."""
import kitebase.utils
from kitebase.endpoint_db import get_server_config
from kitebase.plugins import PluginsManager


class FakeApp:
    def __init__(self, config):
        self.plugins = PluginsManager()
        self.plugins.config = config
        self.plugins.data = {}

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
        'locale': 'it', 'app_title': 'My App', 'export_max_rows': 50_000}


def test_the_export_ceiling_reaches_the_client(monkeypatch):
    """Default included: the client refuses before loading every row."""
    assert config_for(monkeypatch, {'dataview': {'export_max_rows': 2000}})['export_max_rows'] == 2000


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


def test_the_components_plugins_put_in_forms_reach_the_client(monkeypatch):
    """Keyed by id in the YAML, so two plugins add to an area; a list for the client."""
    app = FakeApp({})
    app.plugins.data = {'form_areas': {'top': {
        'concurrency.presence': None, 'notes.badge': {'tone': 'info', '$plugin': 'notes'}, '$plugin': 'x'}}}
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: app)
    assert get_server_config({})['data']['form_areas'] == {'top': [
        {'component': 'concurrency.presence', 'props': {}},
        {'component': 'notes.badge', 'props': {'tone': 'info'}}]}


def test_without_plugins_no_area_has_components(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: FakeApp({}))
    assert get_server_config({})['data']['form_areas'] == {}
