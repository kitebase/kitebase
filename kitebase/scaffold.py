"""
kitebase.scaffold — write a new application that runs.

`kitebase new myapp` produces a directory that starts, logs in and serves its
API before a single table of the domain exists. What it writes is the shape
validated in the applications already in service, reduced to the minimum:

    myapp/
      config.yaml          plugin roots, database, api, authentication
      kite.py              loads the application, and carries the commands
      server_flask.py      the Flask process — `app` at module level
      server_fastapi.py    its twin — same routes, the other framework
      pyproject.toml       dependencies, and its own virtual environment
      plugins/myapp/       where the domain goes
      plugins/users/       the seed: a User table, so there is a way in

Both servers are written unless one is asked for (`--server flask|fastapi`):
they answer the same, and having the pair is what keeps double support a
property that is checked rather than an intention.

The `users` plugin is **not** a copy of the shared `commons` plugins: it uses
only core types and core transforms, so it tracks nothing and cannot drift
from anything. It exists to be replaced — an application that grows adds the
`commons` root and drops this directory.

No Svelte is written: an application does not own a client. It contributes UI
through the `.svelte` files of its plugins, and the generic shell builds it.
"""

import sys
from pathlib import Path
from typing import Optional

# ── Templates ─────────────────────────────────────────────────────────────────
#
# Placeholders are {{name}} and {{sources}}, replaced literally: the content is
# YAML, TOML and Python, all of which use braces for their own purposes.

CONFIG_YAML = '''# {{name}} — app-instance kitebase.
#
# Every relative path below hangs from THIS file's directory, so the
# application starts from anywhere: a service, a cron job, a command run from
# somewhere else.
name: {{name}}
version: 0.1.0
description: ""

# Plugin roots, in order. Each entry is a directory holding plugin directories,
# named as a path or as `{ path, include }` — which takes part of a root and
# leaves the rest of it inert.
#
# A path is relative to this file, or absolute: this application is free to live
# anywhere, and says where the roots it uses are. Add the shared plugins by
# replacing the line below, e.g.
#
#   plugins:
#     - path: /path/to/kitebase-commons/plugins
#       include: [common, partners]
#     - plugins
#
# Not `users`: this application has one of its own, and a name provided by two
# roots is refused.
plugins: [plugins]

db_engine: "sqlite:///data/{{name}}.sqlite"

# The timezone the stored naive datetimes are written in. Uncomment to state
# it: from then on the clock is read through it and a process whose own clock
# disagrees refuses to start. Left out, the process timezone applies and
# nothing checks it — which is fine until a container is rebuilt somewhere else.
# timezone: Europe/Rome

# The server only ever looks: if the schema the plugins describe differs from
# the database, it stops. Changing the database is an explicit command:
#   python kite.py db-check    what differs (read-only)
#   python kite.py db-sync     apply it (additions only, never a drop)
migrations:
  on_startup: error

# Who logs in, and what the token carries into the context of every write.
authentication:
  user_table: User
  username_field: username
  password_field: password
  context_fields: [id, username, email, is_active, is_admin]

api:
  prefix: "kitebase"
  port: 8300

# What kitebase is for this application. `app`: it IS the application, and its
# client owns "/" and logs people in. `admin`: it is the admin of a host that
# has its own pages, mounted under /admin/; add `login: /login` when the host
# logs people in (see kitebase.clientui).
client:
  role: app
'''

KITE_PY = '''"""{{name}} — loads the application, and carries the commands.

Two files, and the division is not cosmetic: the commands must be able to look
at the database **without** starting a server, and in service the process is
taken by a WSGI server without going through a `main()`.

    python kite.py db-check     what differs between schema and database
    python kite.py db-sync      apply it (additions only, never a drop)
    python kite.py check        validate the plugin descriptors
    python kite.py dump-table   the schema as it comes out of the merge
    python server_flask.py      start the process (development)

`model.py` is GENERATED from the YAML schema: do not edit it. Inside a plugin,
`model.py` is real code — the behaviour mixins — and is versioned.
"""
import sys
from pathlib import Path

import kitebase
import kitebase.plugins
import kitebase.source
import kitebase.utils

# Everything hangs from here, never from the current directory.
APP_DIR = Path(__file__).resolve().parent
CONFIG = APP_DIR / "config.yaml"


def setup_schema():
    """Plugins and schema — no engine, no model.py.

    Enough for the introspection commands, which look at what the application
    declares rather than at what the database holds.
    """
    plugins = kitebase.plugins.PluginsManager()
    plugins.load_config(CONFIG)
    kitebase.utils.register_standard_handlers(plugins)
    plugins.load_plugins()

    app = kitebase.utils.get_app()
    app.calc_db(plugins)

    # Query behaviours of the application go here, e.g. with the shared plugins:
    #   from common.model import Archivable
    #   app.add_query_behavior(Archivable)

    return app


def setup(generate: bool = True):
    """The schema plus model.py, regenerated when the YAML or the generator moved."""
    app = setup_schema()

    if generate and app.pm.should_regenerate("model.py"):
        print("Generating model.py ...")
        kitebase.source.Generator(app).generate(filename="model.py")

    return app, app.pm


def setup_db(create_all: bool = True, check_schema: bool = True):
    """The application with the database open. Returns (app, plugins, model).

    The entry point of the server and of any batch work: whatever runs in this
    process uses the generated models directly, never its own HTTP endpoints.
    """
    app, plugins = setup()

    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    import model  # type: ignore  # generated by setup()

    app.initialize_db(plugins.config["db_engine"], model,
                      create_all=create_all, check_schema=check_schema)
    plugins.load_all_locales()

    return app, plugins, model


def seed_admin(app, model, username: str = "admin", password: str = "admin") -> None:
    """An administrator, if the table is empty.

    Development data, not a mechanism: it exists so there is a way in the first
    time. In production the first account is created by hand and this does
    nothing, because the table is not empty.
    """
    with app.get_session() as session:
        if session.query(model.User).first():
            return
        session.add(model.User(
            name="Administrator",
            username=username,
            password=password,      # stored hashed: the column says so
            email="admin@example.com",
            is_active=True,
            is_admin=True,
        ))
        session.commit()
        print(f"Created the initial user: {username}/{password}")


if __name__ == "__main__":
    from kitebase.cli import DB_COMMANDS, make_parser, run_cli

    parser = make_parser()
    args = parser.parse_args()

    if args.command in DB_COMMANDS:
        # These look at the database as it is: no create_all, no startup check
        # — reporting the difference is the whole point.
        app, plugins, model = setup_db(create_all=False, check_schema=False)
        run_cli(app, args, output_dir=APP_DIR / "data")
    elif args.command:
        run_cli(setup_schema(), args, output_dir=APP_DIR / "data")
    else:
        parser.print_help()
        sys.exit(1)
'''

SERVER_PY = '''"""{{name}} — the Flask process.

    waitress-serve --port=8300 server_flask:app     (service)
    python server_flask.py                          (development)

`app` is at module level, so a WSGI server takes it as it is. A WSGI server
rather than `app.run()` is about HTTP hardening, not load: `app.run()` is
Werkzeug's development server and says so itself.

Kitebase is mounted as a **blueprint** even though the house is ours here. It
costs nothing today, and the day this process also serves something else —
another API, server-rendered pages — that line does not change.
"""
import os

from flask import Blueprint, Flask, jsonify

import kitebase.server_utils as srv

import kite

# ── Log ──────────────────────────────────────────────────────────────────────
# The library speaks (one line per request, the traceback on a failure); the
# process decides to listen. Always stdout — journald under systemd, the
# terminal by hand — and a rotating file when the environment names one.
srv.setup_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FILE") or None)

# ── Application ──────────────────────────────────────────────────────────────

kitebase_app, plugins, model = kite.setup_db()
kite.seed_admin(kitebase_app, model)

APP_DIR = kite.APP_DIR

# From the environment in production: changing it invalidates the tokens
# already issued, which is exactly what a key is for.
SECRET_KEY = os.environ.get("SECRET_KEY", "development-secret-key-not-for-service")

flask_app = Flask(__name__)
flask_app.config["SECRET_KEY"] = SECRET_KEY

# ── Kitebase — /kitebase/* ─────────────────────────────────────────────────────
#
# `register_flask` touches nothing outside the blueprint: no CORS, no catch-all,
# and the application's JSON provider stays as it is. It hands back the
# AuthMiddleware, for whatever else in this process lets the same person in.

kitebase_bp = Blueprint("kitebase", __name__)
auth = srv.register_flask(kitebase_bp, kitebase_app, plugins, SECRET_KEY)
flask_app.register_blueprint(kitebase_bp)

# ── The compiled client ──────────────────────────────────────────────────────
#
# Built into clientui/ by `kitebase build-client`, and mounted where `client:` in
# config.yaml says. `static/` is this application's own, on Flask's route.

if not srv.serve_client_flask(flask_app, APP_DIR, plugins.config):

    @flask_app.route("/")
    def no_client():
        return jsonify({
            "application": plugins.config.get("name"),
            "api": f"{plugins.config.get('api', {}).get('prefix', 'kitebase')}/",
            "client": "not built — run `kitebase build-client`",
        })


app = flask_app

if __name__ == "__main__":
    port = plugins.config.get("api", {}).get("port", 8300)
    print(f"\\n🚀 {plugins.config.get('name')} on http://localhost:{port}\\n")
    app.run(host="0.0.0.0", port=port)
'''

FASTAPI_SERVER_PY = '''"""{{name}} — the FastAPI process.

    uvicorn server_fastapi:app --port 8300     (service)
    python server_fastapi.py                   (development)

`app` is at module level, so an ASGI server takes it as it is. The twin of
`server_flask.py`: same four routes, registered by the same call on the other
framework, and answering byte for byte the same. Keeping the pair is what makes
double support a property that is checked rather than an intention — delete the
one you do not serve with, and drop its extra from pyproject.toml.

The bootstrap is not here: `kite.py` composes the application, so a server, a
command and a test all load the same thing.
"""
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import kitebase.server_utils as srv

import kite

# ── Log ──────────────────────────────────────────────────────────────────────
# The library speaks (one line per request, the traceback on a failure); the
# process decides to listen. Always stdout — journald under systemd, the
# terminal by hand — and a rotating file when the environment names one.
srv.setup_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FILE") or None)

# ── Application ──────────────────────────────────────────────────────────────

kitebase_app, plugins, model = kite.setup_db()
kite.seed_admin(kitebase_app, model)

# From the environment in production: changing it invalidates the tokens
# already issued, which is exactly what a key is for.
SECRET_KEY = os.environ.get("SECRET_KEY", "development-secret-key-not-for-service")

fastapi_app = FastAPI(
    title=plugins.config.get("name", "{{name}}"),
    description=plugins.config.get("description", ""),
    version=plugins.config.get("version", "0.0.0"),
)

# ── Kitebase — /kitebase/* ─────────────────────────────────────────────────────
#
# `register_fastapi` registers four routes and nothing application-wide: no
# CORS, no exception handler, no encoder of its own. An APIRouter would do as
# a target just as well, the day this process also serves something else.

srv.register_fastapi(fastapi_app, kitebase_app, plugins, SECRET_KEY)

# ── The compiled client ──────────────────────────────────────────────────────
#
# Built into clientui/ by `kitebase build-client`, and mounted where `client:` in
# config.yaml says. Last, so the API routes win.

if not srv.serve_client_fastapi(fastapi_app, kite.APP_DIR, plugins.config):

    @fastapi_app.get("/")
    def no_client():
        return JSONResponse({
            "application": plugins.config.get("name"),
            "api": f"{plugins.config.get('api', {}).get('prefix', 'kitebase')}/",
            "client": "not built — run `kitebase build-client`",
        })


app = fastapi_app

if __name__ == "__main__":
    import uvicorn

    port = plugins.config.get("api", {}).get("port", 8300)
    print(f"\\n🚀 {plugins.config.get('name')} on http://localhost:{port}")
    print(f"📖 OpenAPI docs: http://localhost:{port}/docs\\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
'''

PYPROJECT = '''# {{name}} — third-party dependencies, and its own virtual environment.
#
#   uv sync            create .venv and install
#   uv run kite.py ...  run inside it
#
# kitebase is a dependency like any other, taken from its repository — there is
# no index to publish to yet, so the repository is where it comes from. `main`
# follows the library; replace it with a tag or a commit the day an application
# in service needs to stop moving.

[project]
name = "{{name}}"
version = "0.1.0"
description = ""
requires-python = ">=3.11"

dependencies = [
{{dependencies}}]

[tool.uv]
package = false
{{sources}}'''

GITIGNORE = '''# State: database, logs, exchanged files. Not versioned — and in production
# the only thing worth backing up.
data/*
!data/.gitkeep

# The compiled client: an artifact, rebuilt from the kitebase workspace.
/clientui/

# Generated by kitebase from the plugin schema — ONLY the one at the root.
# Inside a plugin, model.py is real code (behaviour mixins) and is versioned.
/model.py

# Environment
.venv/
__pycache__/
*.pyc
'''

PLUGIN_CONFIG = '''# The plugin of this application: its schema, and its domain operations.
#
# Kitebase imports every .py in this directory and registers what it finds
# decorated with `@endpoint`. That is where the operations of the domain go —
# never in kite.py, never in a page — because from here the same function is
# reachable from the dispatcher, from another module in this process, and from
# a command, without a line of wiring.
name: {{name}}
version: 0.1.0
description: ""
depends_on: [users]
'''

PLUGIN_MODEL = '''# The schema of {{name}}, declared once and only here.
#
# A table generates its SQLAlchemy model, its CRUD endpoints and — with no
# further declaration — an auto-generated list and form. Uncomment to start:
#
# tables:
#
#   Item:
#     name: items
#     label: "Item"
#     columns:
#       - name: id
#         type: Integer
#         primary_key: true
#         autoincrement: true
#
#       - name: code
#         type: String
#         length: 20
#         unique: true
#         nullable: false
#         label: "Code"
#         searchable: true
#
#       - name: description
#         type: String
#         length: 200
#         label: "Description"
#
#       - name: quantity
#         type: Integer
#         default: 0
#         label: "Quantity"

# Replace this line — do not add a second `tables:` above it. YAML keeps the
# last of two keys with the same name and says nothing, so the tables you wrote
# would be silently discarded in favour of this empty one.
tables: {}
'''

USERS_CONFIG = '''# The seed: enough of a user table to have a way in.
#
# NOT a copy of the shared `commons` plugins — it uses only core types and core
# transforms, so it tracks nothing and cannot drift from anything. It exists to
# be replaced: an application that grows declares the `commons` root in
# config.yaml, takes `users` from there, and deletes this directory.
name: users
version: 0.1.0
description: "Minimal user table for authentication"
'''

USERS_MODEL = '''# Who operates the application — the table `authentication:` points at.
#
# `secret: true` keeps the column out of every response, out of `select *`, and
# makes a query that names it an error rather than a silent drop. `on_write`
# names the transform applied before writing: `password_hash` is built into the
# core, because the `auth` endpoint has to know how credentials are kept.

tables:

  User:
    name: users
    label: "User"
    tags: [anag]
    columns:
      - name: id
        type: Integer
        primary_key: true
        autoincrement: true

      - name: name
        type: String
        length: 60
        label: "Name"
        searchable: true

      - name: username
        type: String
        length: 30
        unique: true
        nullable: false
        label: "Username"

      - name: password
        type: String
        length: 128
        label: "Password"
        widget: password
        secret: true
        on_write: password_hash

      - name: email
        type: String
        length: 120
        label: "Email"

      - name: is_active
        type: Boolean
        default: true
        nullable: false
        label: "Active"

      - name: is_admin
        type: Boolean
        default: false
        nullable: false
        label: "Administrator"
'''

README = '''# {{name}}

Application built on [kitebase](https://github.com/kitebase/kitebase).

## Running it

    uv sync                     create .venv and install the dependencies
    uv run kite.py db-sync      create the database from the YAML schema
{{run}}

## Where things go

| where | what |
|---|---|
| `plugins/{{name}}/model.yaml` | the schema — the only definition there is |
| `plugins/{{name}}/*.py` | the domain operations, as `@endpoint` |
| `kite.py` | loads the application, carries the commands |
| `server_flask.py`, `server_fastapi.py` | compose the process — keep the one you serve with |
| `model.py` | GENERATED — do not edit |

The operations of the domain belong in the plugin, not in `kite.py` and not in a
page: kitebase imports every `.py` of a plugin directory and registers what it
finds decorated with `@endpoint`, and from there the same function is reachable
from the dispatcher, from anything else in this process, and from a command.

## The admin client

An application does not own a client: it contributes UI through the `.svelte`
files of its plugins, and the generic shell — the only re-pointable client —
builds them.

    kitebase dev              this server and the client, together, hot-reloaded
    kitebase build-client     the compiled client, into clientui/

Both need a checkout of the client repository: they look beside the kitebase
checkout, and take `$KITEBASE_UI` when it is somewhere else.

The result lands in `clientui/`, which the server mounts where `client:` in
config.yaml says: at the root with `role: app`, under /admin/ with `role: admin`.
`static/` stays this application's own.
'''

PLUGIN_MENU = '''# The menu of the admin client, and the pages it opens.
#
# A page id like `user_list` is auto-generated from the table: declaring a list
# or a form only becomes necessary when the generated one is not what you want.
# Every plugin can add its own items to a group declared elsewhere, by naming
# its parent — which is how a menu grows without anyone editing this file.

menus:
  main:
    label: {{name}}

menu_items:

  # A top-level group: no action of its own, it only holds things.
  masters: { label: Masters, icon: database, order: 10 }

  users: { label: Users, icon: users, parent: masters, order: 10, action: stack_push, panel: user_list }

  # Your own tables go here, e.g.:
  # items: { label: Items, icon: package, parent: masters, order: 20, action: stack_push, panel: item_list }
'''

# What each choice writes, and what it costs in dependencies. `both` is the
# default because the pair is what keeps the two paths honest: a divergence
# shows up on the machine that wrote them, not on the one that switches later.
# Underscores, not hyphens: `waitress-serve server_flask:app` needs a module
# name it can import, and a hyphen is not one. Symmetric names also mean
# neither framework reads as the default and the other as an afterthought.
SERVERS = {
    "flask": [("server_flask.py", SERVER_PY)],
    "fastapi": [("server_fastapi.py", FASTAPI_SERVER_PY)],
    "both": [("server_flask.py", SERVER_PY),
             ("server_fastapi.py", FASTAPI_SERVER_PY)],
}

EXTRAS = {"flask": "flask", "fastapi": "fastapi", "both": "flask,fastapi"}

# The port serves the API; until a client is built, its root says so rather
# than showing a page. Credentials belong to the API too — admin/admin.
RUN = {
    "flask": "    uv run server_flask.py      the API on http://localhost:8300",
    "fastapi": "    uv run server_fastapi.py    the API on http://localhost:8300",
    "both": ("    uv run server_flask.py      Flask   — the API on http://localhost:8300\n"
             "    uv run server_fastapi.py    FastAPI — the same four routes, /docs too"),
}

FILES = [
    ("config.yaml", CONFIG_YAML),
    ("kite.py", KITE_PY),
    ("pyproject.toml", PYPROJECT),
    (".gitignore", GITIGNORE),
    ("README.md", README),
    ("plugins/{{name}}/config.yaml", PLUGIN_CONFIG),
    ("plugins/{{name}}/model.yaml", PLUGIN_MODEL),
    ("plugins/{{name}}/menu.yaml", PLUGIN_MENU),
    ("plugins/users/config.yaml", USERS_CONFIG),
    ("plugins/users/model.yaml", USERS_MODEL),
    ("data/.gitkeep", ""),
]


# ── Writing it out ────────────────────────────────────────────────────────────

def _kitebase_source() -> str:
    """A `[tool.uv.sources]` block when kitebase is running from a checkout.

    `kitebase new` knows where the kitebase it ran from lives. If that is a source
    tree rather than an installed package, the generated project points at it in
    editable mode: the sources stay live, which is what a workstation developing
    both wants. Installed from a package, nothing is written and the dependency
    resolves normally.
    """
    import kitebase

    repo = Path(kitebase.__file__).resolve().parent.parent
    if not (repo / "pyproject.toml").is_file():
        return ""

    return (
        "\n# `kitebase new` ran from a source checkout, so this points at it in\n"
        "# editable mode: edit the library and the application sees it at once.\n"
        "# Delete this block — or run `uv sync --no-sources` — to resolve the\n"
        "# dependency the way a machine without that checkout would.\n"
        "[tool.uv.sources]\n"
        f'kitebase = {{ path = "{repo}", editable = true }}\n'
    )


def _dependencies(server: str) -> str:
    """The dependency list of a generated application.

    kitebase comes from its repository: there is no index to publish to yet, so
    naming the repository is the only way this file can state where the library
    comes from. The extras follow the servers written — an application installs
    the framework it serves with, and not the other one.
    """
    lines = [f'    "kitebase[{EXTRAS[server]}] @ '
             f'git+https://github.com/kitebase/kitebase@main",']
    if server in ("flask", "both"):
        lines += [
            "    # The WSGI server used in service. Not for throughput: `app.run()` is",
            "    # Werkzeug's development server.",
            '    "waitress",',
        ]
    return "\n".join(lines) + "\n"


def create_app(name: str, directory: Optional[Path] = None,
               force: bool = False, server: str = "both") -> Path:
    """
    Write a new application.

    Args:
        name:      application name — also the name of its plugin
        directory: where to write it (default: ./<name>)
        force:     write into a directory that already has files
        server:    which server to write — 'flask', 'fastapi' or 'both'

    Returns:
        The directory written to

    Raises:
        ValueError: if the name is not usable as a Python package name
        FileExistsError: if the directory holds files and force is False
    """
    if not name.isidentifier() or name != name.lower():
        raise ValueError(
            f"'{name}' cannot be an application name: it must be a lowercase "
            f"Python identifier (letters, digits, underscore; no leading digit) "
            f"— it names a plugin directory that gets imported.")

    target = Path(directory) if directory else Path.cwd() / name
    target = target.resolve()

    if target.exists() and any(target.iterdir()) and not force:
        raise FileExistsError(
            f"{target} is not empty. Use --force to write into it anyway.")

    if server not in SERVERS:
        raise ValueError(f"'{server}' is not a server: "
                         f"{', '.join(SERVERS)}")

    substitutions = {
        "{{name}}": name,
        "{{sources}}": _kitebase_source(),
        "{{dependencies}}": _dependencies(server),
        "{{run}}": RUN[server],
    }

    for relative, template in FILES + SERVERS[server]:
        path = target / _fill(relative, substitutions)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_fill(template, substitutions), encoding="utf-8")

    return target


def _fill(text: str, substitutions: dict) -> str:
    for placeholder, value in substitutions.items():
        text = text.replace(placeholder, value)
    return text


def print_next_steps(target: Path, name: str, server: str = "both") -> None:
    """What to type next, in the order that works."""
    where = target if not str(target).startswith(str(Path.cwd())) else \
        Path(target).relative_to(Path.cwd())
    run = RUN[server]
    print(f"""
Written: {target}

    cd {where}
    uv sync                     create .venv and install
    uv run kite.py db-sync      create the database from the YAML schema
{run}

The schema goes in plugins/{name}/model.yaml, the domain operations in
plugins/{name}/*.py as @endpoint. See README.md.

`kitebase dev` runs this server and the admin client together; `kitebase
build-client` compiles the client into clientui/.
""", file=sys.stderr)
