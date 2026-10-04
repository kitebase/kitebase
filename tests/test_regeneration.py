"""When the generated model.py is stale.

`model.py` is generated from the plugin YAML, and regenerated when the YAML is
newer than it. The rule missed its other input: **the generator itself**. A
change to `source.py` or `db.py` produces a different model from the very same
YAML, so a `git pull` that alters the generation used to leave the old model in
place — with the new schema already applied to the database. Errors from that
mismatch point everywhere except at the cause.

The application config is the third input, and it failed the same way: adding a
shared plugin root changes the model without touching one plugin file, and a
root cloned last week is older than a model generated yesterday.
"""
import os

import yaml

from kitebase.plugins import PluginsManager, _core_timestamp


def model_file(tmp_path, age: float):
    """A generated model whose mtime is `age` seconds newer than the core."""
    path = tmp_path / 'model.py'
    path.write_text('# generated\n')
    stamp = _core_timestamp() + age
    os.utime(path, (stamp, stamp))
    return str(path)


def test_the_core_counts_among_the_inputs():
    """A manager with no plugins at all still has a timestamp: the package."""
    manager = PluginsManager()

    assert _core_timestamp() > 0
    assert manager.get_timestamp() == _core_timestamp()


def test_a_model_older_than_the_generator_is_stale(tmp_path):
    manager = PluginsManager()

    assert manager.should_regenerate(model_file(tmp_path, age=-10))


def test_a_model_newer_than_everything_is_kept(tmp_path):
    manager = PluginsManager()

    assert not manager.should_regenerate(model_file(tmp_path, age=+10))


def test_a_missing_model_is_always_generated(tmp_path):
    assert PluginsManager().should_regenerate(str(tmp_path / 'absent.py'))


def an_app(tmp_path, age: float):
    """An application whose config.yaml is `age` seconds newer than the core."""
    (tmp_path / 'plugins').mkdir()
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'name': 'a', 'plugins': ['plugins']}))
    stamp = _core_timestamp() + age
    os.utime(config, (stamp, stamp))
    return config


def test_the_application_config_counts_among_the_inputs(tmp_path):
    """Because it decides which plugins are read at all."""
    manager = PluginsManager()
    manager.load_config(an_app(tmp_path, age=+100))

    assert manager.get_timestamp() == _core_timestamp() + 100


def test_a_model_older_than_the_config_is_stale(tmp_path):
    """Declaring a shared root regenerates, though no plugin file moved."""
    manager = PluginsManager()
    manager.load_config(an_app(tmp_path, age=+100))

    assert manager.should_regenerate(model_file(tmp_path, age=+10))
