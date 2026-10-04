"""`kitebase new` — what lands on disk, and whether it is coherent.

The scaffold's value is that what it writes runs. Most of that is proven by
running it, which the smoke test of a fresh application does; what is worth
holding here is what a run would not notice: that no placeholder survives into
a file, and that the three things which must agree — the servers written, the
extras installed and the commands printed — cannot drift apart, because they
are chosen once.
"""
from pathlib import Path

import pytest

from kitebase.scaffold import EXTRAS, RUN, SERVERS, create_app


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
    pyproject = (app() / "pyproject.toml").read_text()
    assert "kitebase[flask,fastapi] @ git+https://github.com/" in pyproject


@pytest.mark.parametrize("server", ["flask", "fastapi", "both"])
def test_the_extras_are_the_frameworks_written(app, server):
    pyproject = (app(server) / "pyproject.toml").read_text()
    assert f"kitebase[{EXTRAS[server]}] @" in pyproject


def test_the_wsgi_server_comes_only_with_flask(app):
    assert "waitress" in (app("flask") / "pyproject.toml").read_text()
    assert "waitress" not in (app("fastapi") / "pyproject.toml").read_text()


def test_the_readme_names_the_entry_points_that_exist(app):
    readme = (app("fastapi") / "README.md").read_text()
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
