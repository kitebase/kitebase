"""Reserved values by name, and the engine chosen by name.

The secrets folder holds one YAML file per kind; a name in `db_engine` (or in
KITEBASE_ENGINE, which --engine sets) is an entry of secrets/engines.yaml,
read when the engine opens and never copied into the configuration.
"""
import logging
from types import SimpleNamespace

import pytest

from kitebase import secrets
from kitebase.cli import make_parser
from kitebase.store import Store
from kitebase.plugins import PluginsManager


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    monkeypatch.delenv('KITEBASE_SECRETS', raising=False)
    monkeypatch.delenv('KITEBASE_ENGINE', raising=False)
    (tmp_path / 'secrets').mkdir()
    (tmp_path / 'secrets' / 'engines.yaml').write_text(
        "local: sqlite:///data/other.sqlite\n"
        "pg: postgresql+psycopg://kitebase:s3cret@localhost:5432/app\n"
        "broken: [not, a, url]\n")
    (tmp_path / 'config.yaml').write_text(
        "name: t\nplugins: []\ndb_engine: sqlite:///data/app.sqlite\n")
    secrets.set_app_root(tmp_path)
    yield tmp_path
    secrets.set_app_root(None)


def manager(app_dir):
    pm = PluginsManager()
    pm.load_config(app_dir / 'config.yaml')
    return pm



# -- the folder ---------------------------------------------------------------

def test_an_entry_is_read_by_kind_and_name(app_dir):
    assert secrets.get('engines', 'local') == 'sqlite:///data/other.sqlite'
    assert secrets.names('engines') == ['local', 'pg', 'broken']


def test_a_missing_name_says_where_it_looked_and_what_is_there(app_dir):
    with pytest.raises(secrets.SecretError) as err:
        secrets.get('engines', 'prod')
    assert 'engines.yaml' in str(err.value)
    assert 'local, pg, broken' in str(err.value)


def test_a_missing_kind_is_an_error_to_get_and_empty_to_list(app_dir):
    with pytest.raises(secrets.SecretError, match='smtp'):
        secrets.get('smtp', 'relay')
    assert secrets.names('smtp') == []


def test_the_folder_moves_with_the_environment(app_dir, tmp_path_factory, monkeypatch):
    elsewhere = tmp_path_factory.mktemp('etc')
    (elsewhere / 'smtp.yaml').write_text("relay: {host: mail.example.com}\n")
    monkeypatch.setenv('KITEBASE_SECRETS', str(elsewhere))
    assert secrets.get('smtp', 'relay') == {'host': 'mail.example.com'}


# -- the engine ---------------------------------------------------------------

def resolve(pm, declared):
    return Store._resolve_db_url(SimpleNamespace(pm=pm), declared)


def test_a_url_in_config_is_used_as_before(app_dir):
    pm = manager(app_dir)
    assert pm.config['db_engine'] == 'sqlite:///data/app.sqlite'
    assert pm.db_engine_source == 'config.yaml'
    assert resolve(pm, pm.config['db_engine']) == f"sqlite:///{app_dir / 'data/app.sqlite'}"


def test_a_name_is_resolved_and_a_relative_sqlite_path_anchored(app_dir):
    pm = manager(app_dir)
    assert resolve(pm, 'local') == f"sqlite:///{app_dir / 'data/other.sqlite'}"
    assert resolve(pm, 'pg').startswith('postgresql+psycopg://kitebase:s3cret@')


def test_an_entry_that_is_not_a_url_is_refused(app_dir):
    with pytest.raises(ValueError, match="'broken'"):
        resolve(manager(app_dir), 'broken')


def test_the_environment_chooses_by_name_and_the_config_keeps_no_secret(app_dir, monkeypatch):
    monkeypatch.setenv('KITEBASE_ENGINE', 'pg')
    pm = manager(app_dir)
    assert pm.config['db_engine'] == 'pg'
    assert pm.db_engine_source == 'KITEBASE_ENGINE'
    assert 's3cret' not in repr(pm.config)


def test_the_cli_option_is_the_environment_variable(app_dir, monkeypatch):
    args = make_parser().parse_args(['db-check', '--engine', 'pg'])
    assert args.engine == 'pg'
    import os
    assert os.environ['KITEBASE_ENGINE'] == 'pg'
    assert manager(app_dir).config['db_engine'] == 'pg'


def test_the_log_line_names_the_engine_and_hides_the_password(app_dir, monkeypatch, caplog):
    monkeypatch.setenv('KITEBASE_ENGINE', 'pg')
    pm = manager(app_dir)
    url = resolve(pm, 'pg')
    with caplog.at_level(logging.INFO):
        Store._log_engine(SimpleNamespace(pm=pm), 'pg', url)
    line = caplog.text
    assert 'Database engine: pg' in line
    assert 'KITEBASE_ENGINE' in line
    assert 's3cret' not in line
