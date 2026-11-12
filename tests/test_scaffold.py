"""`kitebase new` — what lands on disk, and whether it is coherent.

The scaffold's value is that what it writes runs. Most of that is proven by
running it, which the smoke test of a fresh application does; what is worth
holding here is what a run would not notice: that no placeholder survives into
a file, and that the three things which must agree — the servers written, the
extras installed and the commands printed — cannot drift apart, because they
are chosen once.
"""
import os
from pathlib import Path

import pytest
import yaml

from kitebase.scaffold import EXTRAS, RUN, SERVERS, commons_root, create_app


@pytest.fixture
def app(tmp_path):
    """Write an application, and hand back its directory."""
    def write(server="both", name="sample"):
        return create_app(name, tmp_path / server / name, server=server)
    return write


# ── What is written ──────────────────────────────────────────────────────────

def test_both_servers_by_default(app):
    target = app()
    assert (target / "server_flask.py").is_file()
    assert (target / "server_fastapi.py").is_file()


def test_the_names_are_importable(app):
    """`waitress-serve server_flask:app` needs a module name, not a filename."""
    for path in app().glob("server_*.py"):
        assert path.stem.isidentifier(), path


@pytest.mark.parametrize("server,written,absent", [
    ("flask", "server_flask.py", "server_fastapi.py"),
    ("fastapi", "server_fastapi.py", "server_flask.py"),
])
def test_asking_for_one_writes_only_that_one(app, server, written, absent):
    target = app(server)
    assert (target / written).is_file()
    assert not (target / absent).exists()


def test_a_server_that_does_not_exist_is_refused(app):
    with pytest.raises(ValueError, match="is not a server"):
        app("tornado")


def test_the_application_name_has_to_be_importable(tmp_path):
    with pytest.raises(ValueError, match="cannot be an application name"):
        create_app("My App", tmp_path / "x")


# ── What it declares ─────────────────────────────────────────────────────────

def test_the_library_comes_from_its_repository(app):
    """No index to publish to yet: the repository is where it comes from."""
    pyproject = (app() / "pyproject.toml").read_text(encoding='utf-8')
    assert "kitebase[flask,fastapi] @ git+https://github.com/" in pyproject


@pytest.mark.parametrize("server", ["flask", "fastapi", "both"])
def test_the_extras_are_the_frameworks_written(app, server):
    pyproject = (app(server) / "pyproject.toml").read_text(encoding='utf-8')
    assert f"kitebase[{EXTRAS[server]}] @" in pyproject


def test_the_wsgi_server_comes_only_with_flask(app):
    assert "waitress" in (app("flask") / "pyproject.toml").read_text(encoding='utf-8')
    assert "waitress" not in (app("fastapi") / "pyproject.toml").read_text(encoding='utf-8')


def test_the_readme_names_the_entry_points_that_exist(app):
    readme = (app("fastapi") / "README.md").read_text(encoding='utf-8')
    assert "uv run server_fastapi.py" in readme
    assert "uv run server_flask.py" not in readme


# ── Coherence ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("server", ["flask", "fastapi", "both"])
def test_no_placeholder_survives(app, server):
    """A `{{...}}` left in a file is a template that never reached a value."""
    for path in app(server).rglob("*"):
        if path.is_file():
            assert "{{" not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize("server", ["flask", "fastapi", "both"])
def test_every_choice_says_what_to_run(server):
    """The three tables are indexed by the same key, and none may lag."""
    assert server in SERVERS and server in EXTRAS and server in RUN


def test_the_files_written_are_the_ones_the_commands_name(app):
    target = app("both")
    for line in RUN["both"].splitlines():
        entry = line.split("uv run ")[1].split()[0]
        assert (target / entry).is_file()


@pytest.fixture(autouse=True)
def plugins_unloaded(tmp_path):
    """The plugins of an application loaded here leave nothing behind: their
    modules and their endpoints go, so the next application may reuse a name."""
    import sys
    from kitebase.endpoints import _ENDPOINTS
    saved, modules = _ENDPOINTS.snapshot(), set(sys.modules)
    yield
    _ENDPOINTS.restore(saved)
    for name in set(sys.modules) - modules:
        module = sys.modules[name]
        places = [getattr(module, '__file__', None) or '', *getattr(module, '__path__', [])]
        if any(str(p).startswith((str(tmp_path), str(COMMONS))) for p in places):
            del sys.modules[name]


def _checked(target, monkeypatch):
    """The issues `kitebase check` finds in the application at `target`, and the app."""
    import kitebase.utils
    from kitebase.app import App
    from kitebase.diagnostics import run_checks
    from kitebase.plugins import PluginsManager

    monkeypatch.chdir(target)
    pm = PluginsManager()
    pm.load_config(str(target / "config.yaml"))
    pm.load_plugins()
    application = App()
    application.calc_db(pm)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: application)
    issues = run_checks(application)
    return [(i['severity'], i['path']) for i in issues if i['severity'] != 'info'], application


def test_a_new_application_checks_clean(app, monkeypatch):
    """The first `kitebase check` of an application nobody has touched yet says
    nothing to fix: a warning there would teach to ignore warnings."""
    issues, application = _checked(app(), monkeypatch)
    assert issues == []
    assert {'User', 'Item'} <= set(application.tables)


def test_there_is_something_to_see_at_the_first_start(app):
    target = app()
    assert yaml.safe_load((target / "config.yaml").read_text(encoding='utf-8'))['title'] == 'Sample'
    menu = yaml.safe_load((target / "plugins/sample/menu.yaml").read_text(encoding='utf-8'))
    assert menu['menu_items']['items']['panel'] == 'item_list'
    assert "raise NotFound" in (target / "plugins/sample/endpoints.py").read_text(encoding='utf-8')
    assert "def seed(" in (target / "kite.py").read_text(encoding='utf-8')
    assert "kite.seed(" in (target / "server_flask.py").read_text(encoding='utf-8')


# ── With the shared plugins ──────────────────────────────────────────────────

COMMONS = Path(__file__).resolve().parent.parent.parent / 'commons'
needs_commons = pytest.mark.skipif(
    not (COMMONS / 'plugins' / 'users').is_dir(),
    reason=f'needs the kitebase-commons checkout at {COMMONS}')


@needs_commons
@pytest.mark.parametrize("given", [COMMONS, COMMONS / 'plugins'])
def test_the_commons_are_recognised_from_the_checkout_or_its_root(given):
    assert commons_root(given) == COMMONS / 'plugins'


def test_a_directory_without_users_is_not_the_commons(tmp_path):
    with pytest.raises(ValueError, match='kitebase-commons checkout'):
        commons_root(tmp_path)


def test_users_without_what_it_depends_on_is_refused(tmp_path):
    users = tmp_path / 'plugins' / 'users'
    users.mkdir(parents=True)
    (users / 'config.yaml').write_text('name: users\ndepends_on: [common]\n', encoding='utf-8')
    with pytest.raises(ValueError, match='common'):
        commons_root(tmp_path)


def _fake_commons(where):
    for name, deps in (('users', ['common']), ('common', [])):
        plugin = where / 'plugins' / name
        plugin.mkdir(parents=True)
        (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': name, 'depends_on': deps}), encoding='utf-8')


def test_a_relative_path_stays_relative_from_the_application(tmp_path, monkeypatch):
    _fake_commons(tmp_path / 'commons')
    monkeypatch.chdir(tmp_path)
    target = create_app("sample", Path("apps") / "sample", commons=Path("commons"))
    root = yaml.safe_load((target / "config.yaml").read_text(encoding='utf-8'))['plugins'][0]
    assert root['path'] == '../../commons/plugins'


def test_an_absolute_path_stays_absolute(tmp_path):
    _fake_commons(tmp_path / 'commons')
    target = create_app("sample", tmp_path / "sample", commons=tmp_path / "commons")
    root = yaml.safe_load((target / "config.yaml").read_text(encoding='utf-8'))['plugins'][0]
    assert root['path'] == str(tmp_path / 'commons' / 'plugins')


def test_nothing_is_written_when_the_commons_are_not_there(tmp_path):
    with pytest.raises(ValueError):
        create_app("sample", tmp_path / "sample", commons=tmp_path / "nowhere")
    assert not (tmp_path / "sample").exists()


@needs_commons
def test_with_the_commons_users_are_theirs_and_it_checks_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = create_app("sample", Path("apps") / "sample",
                        commons=Path(os.path.relpath(COMMONS, tmp_path)))
    assert not (target / "plugins" / "users").exists()
    config = yaml.safe_load((target / "config.yaml").read_text(encoding='utf-8'))
    root = config['plugins'][0]
    assert root['include'] == ['users']
    assert (target / root['path']).resolve() == COMMONS / 'plugins'

    issues, application = _checked(target, monkeypatch)
    assert issues == []
    assert {'User', 'Item'} <= set(application.tables)
