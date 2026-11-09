"""A variant of config.yaml: `extends: config.yaml` and only what it changes.

KITEBASE_CONFIG (the CLI's --config) names it, beside config.yaml, for this
process and those it starts. A variant that changes the engine chooses it by
name, as --engine does: the default stays the base's, and so do its media.
"""
import os

import pytest

from kitebase import cli, dev
from kitebase.plugins import PluginsManager, compose_config

BASE = """\
name: t
plugins: []
db_engine: main
api: { port: 8300, prefix: kitebase }
dataview: { page_size: 100, reload_all_threshold: 5000 }
locale: en
"""

VARIANT = """\
extends: config.yaml
db_engine: demo
api: { port: 8310 }
dataview: { page_size: 1000 }
"""


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    for var in ('KITEBASE_ENGINE', 'KITEBASE_SECRETS', 'KITEBASE_ENGINES_DIR'):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / 'config.yaml').write_text(BASE)
    (tmp_path / 'config.demo.yaml').write_text(VARIANT)
    (tmp_path / 'secrets').mkdir()
    (tmp_path / 'secrets' / 'engines.yaml').write_text(
        "main: sqlite:///app.sqlite\ndemo: sqlite:///demo.sqlite\n")
    yield tmp_path
    from kitebase import secrets
    secrets.set_app_root(None)


def manager(app_dir):
    pm = PluginsManager()
    pm.load_config(app_dir / 'config.yaml')
    return pm


def test_a_variant_is_its_base_with_what_it_changes(app_dir):
    data, files = compose_config(app_dir / 'config.demo.yaml')
    assert files == [app_dir / 'config.yaml', app_dir / 'config.demo.yaml']
    assert data['dataview'] == {'page_size': 1000, 'reload_all_threshold': 5000}
    assert data['api'] == {'port': 8310, 'prefix': 'kitebase'}
    assert data['locale'] == 'en' and 'extends' not in data


def test_without_the_variable_config_yaml_is_loaded(app_dir):
    pm = manager(app_dir)
    assert pm.config_files == [app_dir / 'config.yaml']
    assert pm.config['dataview']['page_size'] == 100


def test_the_variable_loads_the_variant_instead(app_dir, monkeypatch):
    monkeypatch.setenv('KITEBASE_CONFIG', 'config.demo.yaml')
    pm = manager(app_dir)
    assert pm.config_file == app_dir / 'config.demo.yaml'
    assert pm.config['dataview']['page_size'] == 1000
    assert pm.app_root == app_dir


def test_an_engine_changed_by_a_variant_is_chosen_by_name(app_dir, monkeypatch):
    monkeypatch.setenv('KITEBASE_CONFIG', 'config.demo.yaml')
    pm = manager(app_dir)
    assert pm.default_engine == 'main'
    assert pm.engine_name == 'demo' and pm.db_engine_source == 'config.demo.yaml'
    assert pm.media_base() == app_dir / 'engines/demo'
    # --engine has the last word, the default included
    monkeypatch.setenv('KITEBASE_ENGINE', 'main')
    pm = manager(app_dir)
    assert pm.engine_name is None and pm.media_base() == app_dir
    assert pm.config['db_engine'] == 'main' and pm.db_engine_source == 'KITEBASE_ENGINE'


def test_a_variant_changes_the_engine_by_name_only(app_dir, monkeypatch):
    (app_dir / 'config.demo.yaml').write_text(VARIANT.replace('demo', 'sqlite:///other.sqlite'))
    monkeypatch.setenv('KITEBASE_CONFIG', 'config.demo.yaml')
    with pytest.raises(ValueError, match='by name'):
        manager(app_dir)


def test_one_level_of_extends(app_dir, monkeypatch):
    (app_dir / 'config.second.yaml').write_text('extends: config.demo.yaml\nlocale: it\n')
    with pytest.raises(ValueError, match='one level'):
        compose_config(app_dir / 'config.second.yaml')
    (app_dir / 'config.lost.yaml').write_text('extends: nowhere.yaml\n')
    with pytest.raises(FileNotFoundError, match='nowhere.yaml'):
        compose_config(app_dir / 'config.lost.yaml')


def test_a_variant_sits_beside_config_yaml(app_dir, monkeypatch, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp('elsewhere') / 'config.yaml'
    elsewhere.write_text(BASE)
    monkeypatch.setenv('KITEBASE_CONFIG', str(elsewhere))
    with pytest.raises(ValueError, match='beside'):
        manager(app_dir)
    monkeypatch.setenv('KITEBASE_CONFIG', 'config.missing.yaml')
    with pytest.raises(FileNotFoundError, match='config.missing.yaml'):
        manager(app_dir)


def test_dev_reads_the_port_of_the_variant(app_dir, monkeypatch):
    assert dev.read_config(app_dir)['api']['port'] == 8300
    monkeypatch.setenv('KITEBASE_CONFIG', 'config.demo.yaml')
    assert dev.read_config(app_dir)['api']['port'] == 8310


@pytest.mark.parametrize('argv', [
    ['--config', 'config.demo.yaml', 'check'],
    ['check', '--config', 'config.demo.yaml'],
    ['--config=config.demo.yaml', 'my-own-command'],
])
def test_the_option_sets_the_variable_wherever_it_is_written(argv, monkeypatch):
    if argv[-1] == 'my-own-command':
        assert cli._take_config(list(argv)) == ['my-own-command']
    else:
        cli.make_parser().parse_args(argv)
    assert os.environ['KITEBASE_CONFIG'] == 'config.demo.yaml'
