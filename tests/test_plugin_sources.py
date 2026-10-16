"""The Python of the plugins: loaded in dependency order, each module once."""
import yaml

from kitebase.endpoints import CommandProcessor
from kitebase.plugins import PluginsManager


def make_plugins(tmp_path, monkeypatch, plugins):
    """{name: (depends_on, {file: source})} as a root, loaded."""
    for name, (deps, files) in plugins.items():
        plugin = tmp_path / 'plugins' / name
        plugin.mkdir(parents=True)
        (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': name, 'depends_on': deps}))
        for file, source in files.items():
            (plugin / file).write_text(source)
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()
    return pm


def test_sources_come_in_dependency_order(tmp_path, monkeypatch):
    """A plugin's code may import the code of a plugin it depends on."""
    pm = make_plugins(tmp_path, monkeypatch, {
        'aaa': (['zzz'], {'aaa_code.py': ''}),
        'zzz': ([], {'zzz_code.py': ''}),
    })

    assert [p.name for p in pm.get_sources()] == ['zzz_code.py', 'aaa_code.py']


def test_a_module_imported_by_a_sibling_is_not_executed_again(tmp_path, monkeypatch):
    """`api` imports `state` before the loader reaches it: one registry, not two."""
    pm = make_plugins(tmp_path, monkeypatch, {
        'shelf2': ([], {
            'api.py': 'from shelf2.state import REGISTRY\nREGISTRY.append("api")\n',
            'state.py': 'REGISTRY = []\n',
        }),
    })

    # The order the directory listing may give: the importer first.
    sources = sorted(pm.get_sources(), key=lambda p: p.name)
    assert [p.name for p in sources] == ['api.py', 'state.py']
    CommandProcessor().resolve_endpoints(sources)

    import sys
    assert sys.modules['shelf2.api'].REGISTRY is sys.modules['shelf2.state'].REGISTRY
    assert sys.modules['shelf2.state'].REGISTRY == ['api']
