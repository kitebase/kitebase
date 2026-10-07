"""kitebase dev — the processes a development session needs, started together.

A session is two processes that must agree on two things: which application
is being served, and which copy of the library is running. Started by hand
they agree only as long as nobody mistypes; started here they cannot disagree,
because each fact is read from one place.

The browser sees one origin, in development as in service: the Vite server
proxies the API to the backend on the port config.yaml declares, so neither
process needs to know about the other's port and no server enables CORS.

What the command knows by itself, and what has to be told:

  the application    the directory given, or the current one, when it holds a
                     `config.yaml` — and the API port is read from it
  the library        the package this command belongs to; when that is a source
                     checkout, the application runs against it instead of the
                     version it declares (`--src`, `$KITEBASE_SRC` to say which)
  the client         the only thing nobody can derive: `--ui`, `$KITEBASE_UI`,
                     or the workspace layout, where it sits beside the library

Nothing here belongs in production: a server started this way is the
development server of its framework. What does belong there is the artifact —
`build_client` compiles the same client into the application's `clientui/`,
which its own server mounts where `client:` in config.yaml says (kitebase.clientui).
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

import yaml

from kitebase.clientui import CLIENT_DIR

# The client repository, recognised by the one client every checkout has.
UI_MARKER = Path("apps") / "shell" / "package.json"

# Where a client checkout sits when nobody says: the workspace layout, seen
# from the library checkout and from the application.
UI_CANDIDATES = ["client", "kitebase-ui"]

# Entry points per framework, best name first. `kitebase new` writes
# `server_*.py`; the rest are what applications generated earlier have, and
# they keep working because a name changing is not a reason to rewrite an app.
SERVERS = {
    "fastapi": ["server_fastapi.py", "fastapi-server.py"],
    "flask": ["server_flask.py", "flask-server.py", "server.py"],
    None: ["server_fastapi.py", "server_flask.py",
           "fastapi-server.py", "flask-server.py", "server.py"],
}


# The script that loads an application, when config.yaml names none: what
# `kitebase new` writes.
CLI_DEFAULT = "kite.py"


class DevError(Exception):
    """Something the session needs is missing, and the message says which."""


class _Finished(Exception):
    """One of the processes ended: stop the others and return its status."""


# ── What is where ────────────────────────────────────────────────────────────

def find_app(given: Optional[str] = None) -> Path:
    """The application directory: the one given, or the current one."""
    app = Path(given).resolve() if given else Path.cwd()
    if not (app / "config.yaml").is_file():
        raise DevError(
            f"{app} holds no config.yaml — an application directory does.\n"
            f"Give one: kitebase dev /path/to/app")
    return app


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True when something already answers on that port.

    Asked before starting anything: a server that cannot bind takes the client
    down with it, and the one line saying why scrolls past between the client's
    startup and the failure of both.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((host, port)) == 0


def read_api_port(app: Path, default: int = 8300) -> int:
    """The port the application serves on, as its own config declares it."""
    return (read_config(app).get("api") or {}).get("port", default)


def find_source_checkout(given: Optional[str] = None) -> Optional[Path]:
    """The library sources to run against, if there are any.

    Explicit first, then `$KITEBASE_SRC`, then the checkout this very command
    was imported from — which is the case that matters on a workstation
    developing the library and an application at the same time. Installed as a
    package, nothing is returned and the application runs with what it declares.
    """
    for candidate in (given, os.environ.get("KITEBASE_SRC")):
        if candidate:
            path = Path(candidate).resolve()
            if not (path / "pyproject.toml").is_file():
                raise DevError(f"{path} is not a kitebase checkout (no pyproject.toml).")
            return path

    import kitebase
    repo = Path(kitebase.__file__).resolve().parent.parent
    return repo if (repo / "pyproject.toml").is_file() else None


def find_ui(given: Optional[str] = None, app: Optional[Path] = None,
            src: Optional[Path] = None) -> Path:
    """The client checkout — the one thing that cannot be derived.

    Asked for explicitly, named by `$KITEBASE_UI`, or found where the workspace
    layout puts it: beside the library checkout, or one or two levels above the
    application. When none of that holds, the error lists what was tried, so
    the answer is to point at it rather than to move it.
    """
    for candidate in (given, os.environ.get("KITEBASE_UI")):
        if candidate:
            path = Path(candidate).resolve()
            if not (path / UI_MARKER).is_file():
                raise DevError(f"{path} is not a kitebase-ui checkout (no {UI_MARKER}).")
            return path

    tried: List[Path] = []
    for base in [p for p in (src.parent if src else None,
                             app.parent if app else None,
                             app.parent.parent if app else None) if p]:
        for relative in UI_CANDIDATES:
            path = (base / relative).resolve()
            if path not in tried:
                tried.append(path)
            if (path / UI_MARKER).is_file():
                return path

    listed = "\n  ".join(str(p) for p in tried) or "(nowhere to look)"
    raise DevError(
        "No kitebase-ui checkout found. Looked in:\n  " + listed +
        "\nSay where it is: kitebase dev --ui /path/to/kitebase-ui, or set "
        "KITEBASE_UI.\nOr start the server alone: kitebase dev --no-client")


def pick_server(app: Path, framework: Optional[str] = None) -> Path:
    """The entry point to run, given the framework asked for (or any)."""
    for name in SERVERS[framework]:
        if (app / name).is_file():
            return app / name

    wanted = SERVERS[framework]
    raise DevError(
        f"{app} holds none of: {', '.join(wanted)}"
        + (f" — it has no {framework} server." if framework else ""))


def read_config(app: Path) -> dict:
    """The application's config.yaml, as a mapping."""
    return yaml.safe_load((app / "config.yaml").read_text(encoding="utf-8")) or {}


def find_cli(app: Path) -> Path:
    """The script that loads this application and carries its commands.

    Loading the application is its own business: the script composes the
    plugins, registers its query behaviours, then hands the commands to
    `kitebase.cli.run_cli`. `cli:` in config.yaml names it; `kite.py`, what
    `kitebase new` writes, needs no naming.
    """
    name = read_config(app).get("cli") or CLI_DEFAULT
    script = app / name
    if not script.is_file():
        raise DevError(
            f"{app} has no {name}, the script that loads it and carries its "
            f"commands.\nName yours in config.yaml, e.g. `cli: myapp.py`; it "
            f"ends by calling kitebase.cli.run_cli.")
    return script


def delegate(argv: Sequence[str], src: Optional[str] = None) -> int:
    """Run a command that needs the application loaded, through its own script.

    `kitebase db-check` in an application directory is `uv run kite.py db-check`
    there: one way of loading the application, reached from one command.
    """
    app = Path.cwd()
    if not (app / "config.yaml").is_file():
        raise DevError(
            f"`{argv[0]}` needs an application: run it from a directory holding "
            f"a config.yaml.")
    command = backend_command(app, find_cli(app), find_source_checkout(src), argv)
    if os.name == "posix":
        # Replaced, not spawned: Ctrl-C and the exit status are the script's own.
        os.execv(command[0], command)
    return subprocess.call(command, cwd=str(app))


# ── What to run ──────────────────────────────────────────────────────────────

def project_of(app: Path) -> Optional[Path]:
    """The Python project an application runs in: its own, or the one above it.

    The benches inside the library checkout (devtest) have no `pyproject.toml`
    of their own: they belong to the checkout, as `uv` itself would conclude
    looking upwards from their directory.
    """
    for directory in (app, *app.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return None


def backend_command(app: Path, script: Path, src: Optional[Path] = None,
                    args: Sequence[str] = ()) -> List[str]:
    """How a script of this application runs: its server, or its CLI.

    The project the application belongs to has an environment of its own, and
    `uv` is what puts it there, whatever virtual environment is active; the
    library checkout, when there is one and it is not that project already, is
    layered on top for the run only, and nothing is left installed afterwards.
    Outside any project, or without `uv`, it runs with the interpreter that is
    already here.
    """
    project = project_of(app)
    uv = shutil.which("uv")
    if not project or not uv:
        if project == app:
            raise DevError(
                f"{app} declares its own environment but uv is not installed.\n"
                f"Install uv, or run it yourself: .venv/bin/python {script.name}")
        return [sys.executable, script.name, *args]

    layered = src and src.resolve() != project.resolve()
    editable = ["--with-editable", str(src)] if layered else []
    return [uv, "run", *editable, script.name, *args]


def client_command() -> List[str]:
    """How the generic shell starts. It is told which app by the environment."""
    pnpm = shutil.which("pnpm")
    if not pnpm:
        raise DevError("pnpm is not installed — needed for the client.\n"
                       "Start the server alone: kitebase dev --no-client")
    return [pnpm, "--filter", "shell", "dev"]


def build_command(app: Path) -> List[str]:
    """How the client is compiled for one application.

    The build itself belongs to the client repository — this only says which
    application it is for, and lets that repository decide what building means.
    """
    pnpm = shutil.which("pnpm")
    if not pnpm:
        raise DevError("pnpm is not installed — needed to build the client.")
    return [pnpm, "build:app", str(app)]


# ── Running them ─────────────────────────────────────────────────────────────

def _spawn(command: List[str], cwd: Path, env: dict) -> subprocess.Popen:
    """Start a child in a process group of its own.

    Neither child is the process that does the work: `uv` runs the server,
    `pnpm` runs vite, and neither forwards a signal to what it started. Given
    its own group, a child can be stopped whole — and this is the only way the
    two paths behave alike, since when one process ends by itself there is no
    terminal signal to reach the other.
    """
    group = ({"start_new_session": True} if os.name == "posix"
             else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})
    return subprocess.Popen(command, cwd=str(cwd),
                            env={**os.environ, **env}, **group)


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    """Signal a child and everything it started; ignore what is already gone."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run(app: Optional[str] = None, framework: Optional[str] = None,
        src: Optional[str] = None, ui: Optional[str] = None,
        no_server: bool = False, no_client: bool = False) -> int:
    """Start what was asked for, and keep the two alive together.

    They live and die as one: when either process ends, the other is stopped.
    A backend that failed to start would otherwise leave a client talking to
    nothing, which looks like a bug in the application.
    """
    if no_server and no_client:
        raise DevError("Nothing to run: --no-server and --no-client together.")

    app_dir = find_app(app)
    checkout = find_source_checkout(src)
    ui_dir = find_ui(ui, app_dir, checkout) if not no_client else None

    processes: List[subprocess.Popen] = []

    if not no_server:
        server = pick_server(app_dir, framework)
        port = read_api_port(app_dir)
        if port_in_use(port):
            raise DevError(
                f"Port {port} is already in use — this application's server is "
                f"probably already running.\nStop it (Ctrl-C in its terminal), "
                f"or run the client alone: kitebase dev --no-server")
        command = backend_command(app_dir, server, checkout)
        print(f"server  {server.name}  →  http://localhost:{port}", flush=True)
        if checkout:
            print(f"        against the library at {checkout}", flush=True)
        processes.append(_spawn(command, app_dir, {}))

    if not no_client:
        print(f"client  shell  ←  {app_dir}", flush=True)
        processes.append(_spawn(client_command(), ui_dir,
                                {"KITEBASE_APP_ROOT": str(app_dir)}))

    print("Ctrl-C stops both.\n", flush=True)

    # Ctrl-C is not the only way a session ends: a closed terminal (SIGHUP) or
    # a `kill` (SIGTERM) must take the children down too, or the server stays
    # behind, holding the port. Only the first signal interrupts: the ones that
    # follow it (uv forwards what it receives) must not cut the cleanup short.
    stopping: List[int] = []

    def interrupt(signum, frame):
        if not stopping:
            stopping.append(signum)
            raise KeyboardInterrupt

    caught = [s for s in (getattr(signal, n, None)
                          for n in ("SIGINT", "SIGTERM", "SIGHUP")) if s]
    previous = {sig: signal.signal(sig, interrupt) for sig in caught}

    status = 0
    try:
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    status = code
                    raise _Finished
            time.sleep(0.2)
    except (KeyboardInterrupt, _Finished):
        pass
    finally:
        stopping.append(0)  # from here on, every signal waits for the cleanup
        for process in processes:
            _signal_group(process, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
                process.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    return status


def build_client(app: Optional[str] = None, ui: Optional[str] = None) -> int:
    """Compile the client of an application into its `clientui/`.

    The development twin of `run`: same two questions — which application, and
    where the client checkout is — answered the same way, so an application
    that can be developed can be built without learning a second set of rules.
    """
    app_dir = find_app(app)
    ui_dir = find_ui(ui, app_dir, find_source_checkout())

    print(f"client  →  {app_dir / CLIENT_DIR}", flush=True)
    return subprocess.call(build_command(app_dir), cwd=str(ui_dir))
