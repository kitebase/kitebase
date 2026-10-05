"""`kitebase dev` — what it finds, and what it refuses to guess.

The command exists to make three facts agree: which application is served,
which library it runs against, and where the client is. Two of them it derives;
the third it cannot, and the value of the whole thing rests on the difference
being visible. So what is tested here is the resolution — not the processes,
which are the frameworks' own development servers and are not ours to check.
"""
import os
import socket
import sys
from pathlib import Path

import pytest
import yaml

from kitebase import dev


def write_app(directory: Path, port: int = 8300, servers=("server_fastapi.py",),
              standalone: bool = False) -> Path:
    """An application directory, reduced to what `dev` looks at."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(
        yaml.safe_dump({"name": directory.name, "api": {"port": port}}))
    for server in servers:
        (directory / server).write_text("# server\n")
    if standalone:
        (directory / "pyproject.toml").write_text("[project]\nname='x'\n")
    return directory


def write_ui(directory: Path) -> Path:
    """A client checkout, recognised by the client every one of them has."""
    (directory / "apps" / "shell").mkdir(parents=True, exist_ok=True)
    (directory / "apps" / "shell" / "package.json").write_text("{}")
    return directory


@pytest.fixture(autouse=True)
def no_inherited_environment(monkeypatch):
    """The developer's own KITEBASE_* must not decide the outcome of a test."""
    for name in ("KITEBASE_SRC", "KITEBASE_UI"):
        monkeypatch.delenv(name, raising=False)


# ── The application ──────────────────────────────────────────────────────────

def test_the_current_directory_is_the_default(tmp_path, monkeypatch):
    app = write_app(tmp_path / "myapp")
    monkeypatch.chdir(app)
    assert dev.find_app() == app


def test_a_directory_without_config_is_not_an_application(tmp_path):
    with pytest.raises(dev.DevError, match="no config.yaml"):
        dev.find_app(str(tmp_path))


def test_the_port_is_the_one_the_app_declares(tmp_path):
    assert dev.read_api_port(write_app(tmp_path / "a", port=8302)) == 8302


def test_an_app_that_declares_no_port_gets_the_default(tmp_path):
    app = tmp_path / "a"
    app.mkdir()
    (app / "config.yaml").write_text(yaml.safe_dump({"name": "a"}))
    assert dev.read_api_port(app) == 8300


def test_a_free_port_is_free(tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
    assert dev.port_in_use(port) is False


def test_a_port_someone_is_listening_on_is_in_use(tmp_path):
    """The check that keeps `kitebase dev` from starting a server that cannot bind.

    Without it the reason — one line from uvicorn — is buried between the
    client's startup and the death of both processes.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert dev.port_in_use(listener.getsockname()[1]) is True


# ── The entry point ──────────────────────────────────────────────────────────

def test_fastapi_is_preferred_when_nothing_is_asked(tmp_path):
    app = write_app(tmp_path / "a", servers=("fastapi-server.py", "server.py"))
    assert dev.pick_server(app).name == "fastapi-server.py"


def test_the_scaffold_entry_point_is_taken_when_it_is_all_there_is(tmp_path):
    app = write_app(tmp_path / "a", servers=("server.py",))
    assert dev.pick_server(app).name == "server.py"


def test_asking_for_flask_takes_the_flask_twin(tmp_path):
    app = write_app(tmp_path / "a", servers=("fastapi-server.py", "flask-server.py"))
    assert dev.pick_server(app, "flask").name == "flask-server.py"


def test_asking_for_a_framework_that_is_not_there_says_so(tmp_path):
    app = write_app(tmp_path / "a", servers=("fastapi-server.py",))
    with pytest.raises(dev.DevError, match="no flask server"):
        dev.pick_server(app, "flask")


# ── The library ──────────────────────────────────────────────────────────────

def test_the_environment_names_the_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "kitebase"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='kitebase'\n")
    monkeypatch.setenv("KITEBASE_SRC", str(checkout))
    assert dev.find_source_checkout() == checkout


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path):
    with pytest.raises(dev.DevError, match="not a kitebase checkout"):
        dev.find_source_checkout(str(tmp_path))


def test_running_from_a_checkout_is_the_default(tmp_path):
    """This test suite runs from one, which is the case it describes."""
    found = dev.find_source_checkout()
    assert found is not None and (found / "pyproject.toml").is_file()


# ── The client ───────────────────────────────────────────────────────────────

def test_the_environment_names_the_client(tmp_path, monkeypatch):
    ui = write_ui(tmp_path / "kitebase-ui")
    monkeypatch.setenv("KITEBASE_UI", str(ui))
    assert dev.find_ui() == ui


def test_the_workspace_layout_is_the_fallback(tmp_path):
    """<ws>/server and <ws>/client — the layout the workspace holds."""
    src = tmp_path / "server"
    src.mkdir()
    ui = write_ui(tmp_path / "client")
    assert dev.find_ui(app=write_app(tmp_path / "app"), src=src) == ui


def test_a_client_beside_the_application_is_found(tmp_path):
    ui = write_ui(tmp_path / "kitebase-ui")
    app = write_app(tmp_path / "myapp")
    assert dev.find_ui(app=app) == ui


def test_not_finding_it_lists_where_it_looked(tmp_path):
    app = write_app(tmp_path / "myapp")
    with pytest.raises(dev.DevError) as caught:
        dev.find_ui(app=app)
    message = str(caught.value)
    assert "KITEBASE_UI" in message and "--no-client" in message
    assert str((tmp_path / "kitebase-ui").resolve()) in message


def test_a_directory_that_is_not_a_client_is_refused(tmp_path):
    with pytest.raises(dev.DevError, match="not a kitebase-ui checkout"):
        dev.find_ui(str(tmp_path))


# ── The commands ─────────────────────────────────────────────────────────────

def test_an_app_with_its_own_environment_runs_through_uv(tmp_path):
    app = write_app(tmp_path / "a", standalone=True)
    command = dev.backend_command(app, app / "server_fastapi.py")
    assert command[1:] == ["run", "server_fastapi.py"]
    assert command[0].endswith("uv")


def test_the_library_checkout_is_layered_on_for_the_run(tmp_path):
    app = write_app(tmp_path / "a", standalone=True)
    src = tmp_path / "server"
    command = dev.backend_command(app, app / "server_fastapi.py", src)
    assert command[2:4] == ["--with-editable", str(src)]


def test_outside_any_project_it_runs_with_this_interpreter(tmp_path):
    """No pyproject.toml here or above: no environment to step into."""
    app = write_app(tmp_path / "devtest")
    command = dev.backend_command(app, app / "server_fastapi.py", tmp_path / "kitebase")
    assert command == [sys.executable, "server_fastapi.py"]


def test_a_bench_inside_the_checkout_runs_in_the_checkout_environment(tmp_path):
    """devtest has no pyproject.toml of its own: the checkout's is its project,
    and the checkout is not layered on itself."""
    checkout = tmp_path / "server"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='kitebase'\n")
    app = write_app(checkout / "devtest")
    command = dev.backend_command(app, app / "devtest.py", checkout, ["check"])
    assert command[1:] == ["run", "devtest.py", "check"]


# ── Commands that need the application loaded ────────────────────────────────

def test_the_cli_script_defaults_to_kite_py(tmp_path):
    app = write_app(tmp_path / "a", servers=("kite.py",))
    assert dev.find_cli(app) == app / "kite.py"


def test_the_cli_script_is_the_one_config_names(tmp_path):
    app = write_app(tmp_path / "a", servers=("devtest.py",))
    config = yaml.safe_load((app / "config.yaml").read_text())
    (app / "config.yaml").write_text(yaml.safe_dump({**config, "cli": "devtest.py"}))
    assert dev.find_cli(app) == app / "devtest.py"


def test_without_a_cli_script_the_error_says_how_to_name_one(tmp_path):
    app = write_app(tmp_path / "a")
    with pytest.raises(dev.DevError, match="cli: myapp.py"):
        dev.find_cli(app)


def test_a_command_is_handed_to_the_app_script(tmp_path, monkeypatch):
    app = write_app(tmp_path / "a", servers=("kite.py",), standalone=True)
    monkeypatch.chdir(app)
    ran = {}
    monkeypatch.setattr(dev.os, "execv", lambda path, argv: ran.update(argv=argv))
    monkeypatch.setattr(dev.subprocess, "call", lambda argv, cwd: ran.update(argv=argv) or 0)
    dev.delegate(["db-check"], src=None)
    assert ran["argv"][-2:] == ["kite.py", "db-check"]
    assert ran["argv"][1] == "run"


def test_a_command_only_the_app_knows_is_handed_over_too(tmp_path, monkeypatch):
    from kitebase import cli
    app = write_app(tmp_path / "a", servers=("kite.py",), standalone=True)
    monkeypatch.chdir(app)
    ran = {}
    monkeypatch.setattr(dev, "delegate", lambda argv, src=None: ran.update(argv=argv) or 0)
    with pytest.raises(SystemExit) as done:
        cli.main(["generate-terminals", "--dry-run"])
    assert done.value.code == 0
    assert ran["argv"] == ["generate-terminals", "--dry-run"]


def test_running_nothing_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(write_app(tmp_path / "a"))
    with pytest.raises(dev.DevError, match="Nothing to run"):
        dev.run(no_server=True, no_client=True)


# ── Building the client ──────────────────────────────────────────────────────

def test_the_build_names_the_application_it_is_for(tmp_path):
    app = write_app(tmp_path / "a")
    command = dev.build_command(app)
    assert command[1:] == ["build:app", str(app)]
    assert command[0].endswith("pnpm")


def test_a_command_outside_an_application_says_where_to_run_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(dev.DevError, match="`db-check` needs an application"):
        dev.delegate(["db-check"])
