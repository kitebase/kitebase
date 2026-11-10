"""The engine runs a whole application with no plugin of kitebase-commons.

The core is agnostic (it is the mechanism, the commons are policy), and nothing
would notice if it stopped being: every application we run loads the commons.
So this test builds one that does not, in an interpreter of its own, where no
other test has imported a plugin: one invented plugin with two tables, a page,
a menu. The models are generated, `check` is clean, the dispatcher writes and
reads, the pages and the menu come out; and no module was loaded from outside
the core, the libraries and the application's own folder.
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

APP = textwrap.dedent('''
    import importlib.util
    import json
    import sys
    from pathlib import Path

    import yaml

    import kitebase
    import kitebase.utils
    from kitebase.app import App
    from kitebase.diagnostics import run_checks
    from kitebase.endpoints import Dispatcher
    from kitebase.plugins import PluginsManager
    from kitebase.source import Generator

    root = Path(sys.argv[1])
    plugin = root / 'plugins' / 'atelier'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump(
        {'name': 'atelier', 'version': '0.0.1', 'description': 'An invented plugin'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': {
        'Maker': {'name': 'makers', 'label': 'Maker', 'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'name', 'type': 'String', 'length': 60}]},
        'Kite': {'name': 'kites', 'label': 'Kite', 'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'model', 'type': 'String', 'length': 80},
            {'name': 'maker_id', 'type': 'Integer',
             'foreign_key': {'target': 'Maker.id'}}]},
    }}))
    (plugin / 'pages.yaml').write_text(yaml.safe_dump({'pages': {
        'kite_desk': {'title': 'Kites', 'content': {
            'type': 'table', 'source': {'model': 'Kite'},
            'columns': [{'field': 'model', 'title': 'Model'}]}},
    }}))
    (plugin / 'menu.yaml').write_text(yaml.safe_dump({
        'menus': {'main': {'label': 'Atelier'}},
        'menu_items': {
            'kites': {'label': 'Kites', 'panel': 'kite_desk'},
            'makers': {'label': 'Makers', 'panel': 'maker_list'},
        },
    }))
    (root / 'config.yaml').write_text(yaml.safe_dump({'name': 'atelier', 'plugins': ['plugins']}))

    pm = PluginsManager()
    pm.load_config(str(root / 'config.yaml'))
    pm.load_plugins()

    app = App()
    app.calc_db(pm)
    source = root / 'model.py'
    Generator(app).generate(filename=str(source))
    spec = importlib.util.spec_from_file_location('atelier_model', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app.initialize_db(f'sqlite:///{root / "atelier.sqlite"}', module, check_schema=False)
    kitebase.utils.get_app = lambda: app

    def send(op, params):
        return Dispatcher().send({'operation': op, 'parameters': params})

    out = {'issues': [i for i in run_checks(app) if i['severity'] != 'info']}
    maker = send('db', {'table': 'Maker', 'method': 'create', 'data': {'name': 'Aeolus'}})
    out['create'] = maker['status']
    send('db', {'table': 'Kite', 'method': 'create',
                'data': {'model': 'Delta', 'maker_id': maker['data']['id']}})
    rows = send('query', {'format': 'dict', 'query': {
        'table': 'Kite', 'select': ['Kite.model', 'Maker.name'],
        'joins': [{'Maker': 'Maker.id = Kite.maker_id'}]}})
    out['query'] = rows['status'], rows.get('data')
    out['pages'] = {pid: send('get_page', {'id': pid})['status']
                    for pid in ('kite_desk', 'maker_list', 'maker_form')}
    out['menu'] = send('get_menu', {})['status']

    allowed = [Path(kitebase.__file__).parent, root, Path(__file__).parent,
               Path(sys.prefix), Path(sys.base_prefix)]
    out['foreign'] = sorted(
        name for name, mod in list(sys.modules.items())
        if getattr(mod, '__file__', None)
        and not any(Path(mod.__file__).resolve().is_relative_to(p.resolve()) for p in allowed))
    print(json.dumps(out, default=str))
''')


def test_an_application_without_the_commons(tmp_path):
    script = tmp_path / 'run_app.py'
    script.write_text(APP)
    data = tmp_path / 'app'
    data.mkdir()

    done = subprocess.run(
        [sys.executable, str(script), str(data)], cwd=data, capture_output=True,
        text=True, env={'PYTHONPATH': str(REPO_ROOT), 'PATH': '/usr/bin:/bin'},
        timeout=120)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])

    assert out['issues'] == []
    assert out['create'] == 'success'
    assert out['query'] == ['success', {'headers': ['model', 'name'],
                                        'data': [['Delta', 'Aeolus']]}]
    assert out['pages'] == {'kite_desk': 'success', 'maker_list': 'success',
                            'maker_form': 'success'}
    assert out['menu'] == 'success'
    assert out['foreign'] == []
