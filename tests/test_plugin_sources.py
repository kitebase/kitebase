"""The Python of the plugins: loaded in dependency order, each module once."""
import yaml

from kitebase.plugins import PluginsManager, load_modules


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
    load_modules(sources)

    import sys
    assert sys.modules['shelf2.api'].REGISTRY is sys.modules['shelf2.state'].REGISTRY
    assert sys.modules['shelf2.state'].REGISTRY == ['api']


def test_a_module_that_does_not_load_stops_the_start(tmp_path, monkeypatch):
    """Named, with the cause: not a traceback the start runs past."""
    import pytest
    pm = make_plugins(tmp_path, monkeypatch, {
        'shelf3': ([], {'broken.py': 'from no_such_plugin.registry import report\n'}),
    })

    with pytest.raises(ImportError) as raised:
        pm.load_modules()

    message = str(raised.value)
    assert "Plugin module 'shelf3.broken'" in message
    assert "No module named 'no_such_plugin'" in message
    assert '`optional`' in message           # the likely fix, for a missing module
    import sys
    assert 'shelf3.broken' not in sys.modules


def _copies(stem: str):
    """The names a file of a plugin was imported under."""
    import sys
    return sorted(name for name, module in sys.modules.items()
                  if name.split('.')[-1] == stem and module is not None)


def test_a_relative_import_reaches_the_same_module(tmp_path, monkeypatch):
    """`from .state import ...` in a plugin is `<plugin>.state`, not a copy
    of the file under the name of the module that asked."""
    pm = make_plugins(tmp_path, monkeypatch, {
        'shelf4': ([], {
            'api4.py': 'from .state4 import REGISTRY\nREGISTRY.append("api")\n',
            'state4.py': 'REGISTRY = []\n',
        }),
    })

    pm.load_modules()

    assert _copies('state4') == ['shelf4.state4']
    assert _copies('api4') == ['shelf4.api4']


def test_a_package_that_imports_its_modules_loads_them_once(tmp_path, monkeypatch):
    """A plugin whose __init__ imports its own modules: the loader reaches the
    same files afterwards and finds them loaded, under the same names."""
    pm = make_plugins(tmp_path, monkeypatch, {
        'shelf5': ([], {
            '__init__.py': 'from kitebase import autoimport\nautoimport(__file__, __package__)\n',
            'count5.py': 'RUNS = []\nRUNS.append(__name__)\n',
        }),
    })

    pm.load_modules()

    import sys
    assert _copies('count5') == ['shelf5.count5']
    assert sys.modules['shelf5.count5'].RUNS == ['shelf5.count5']
    assert 'shelf5.__init__' not in sys.modules


def test_the_chosen_root_is_the_one_whose_code_runs(tmp_path, monkeypatch):
    """Two roots hold a plugin of one name: `include` names the root, and the
    package is made from that directory whatever sys.path would find first."""
    for root, mark in (('first', 'FIRST'), ('second', 'SECOND')):
        plugin = tmp_path / root / 'shelf6'
        plugin.mkdir(parents=True)
        (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'shelf6'}))
        (plugin / 'which6.py').write_text(f'ROOT = "{mark}"\n')
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': [
        {'path': 'first', 'include': []}, {'path': 'second', 'include': ['shelf6']}]}))
    monkeypatch.chdir(tmp_path)
    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()

    pm.load_modules()

    import sys
    assert sys.modules['shelf6.which6'].ROOT == 'SECOND'
