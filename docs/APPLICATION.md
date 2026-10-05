# The Anatomy of an Application

*What a Kitebase application is made of, and why: the files, the bootstrap they
share, the commands and the servers. This is the reference; the tutorial, from an
empty machine to an application with a compiled client, is
[GETTING_STARTED.md](GETTING_STARTED.md). How plugins declare the data model and
the UI is [PLUGIN_MODEL.md](PLUGIN_MODEL.md).*

> **The files below are written for you by `kitebase new`.** This document
> explains what they are, and is worth reading when one of them has to change.

*Last revised: 2026-10-05, against the output of `kitebase new` in 0.6.0.*

---

## What an app is

A Kitebase **application** is a directory with a few files that wire the
framework to a database, a command line and an HTTP server, plus the plugins
that carry the actual model and UI:

```
myapp/
  config.yaml          what the app is: plugin roots, database, api, auth, client
  kite.py              loads the application, and carries the commands
  server_flask.py      the Flask process
  server_fastapi.py    the FastAPI process, its twin
  pyproject.toml       the dependencies, and the app's own virtual environment
  model.py             GENERATED from the plugins: do not edit, not versioned
  plugins/myapp/       the app's own plugin: schema, menu, domain operations
  plugins/users/       the users table, so there is someone to log in
  data/                the state: database, logs, dumps. Not versioned
  clientui/            the compiled admin client, when built. Not versioned
```

Everything the app *does* lives in the plugins. The two servers are twins on
purpose: they answer byte for byte the same, and keeping both is what makes
double framework support a checked property. Delete the one you do not serve
with, and drop its extra from `pyproject.toml`.

---

## `kite.py`: the bootstrap and the commands

The division between `kite.py` and the servers is not cosmetic: the commands
must be able to look at the database **without** starting a server, and in
service the process is taken by a WSGI or ASGI server without going through a
`main()`. So the bootstrap lives in `kite.py`, and a server, a command and a test
all load the same thing.

```python
APP_DIR = Path(__file__).resolve().parent      # everything hangs from here
CONFIG = APP_DIR / "config.yaml"

def setup_schema():
    """Plugins and schema: no engine, no model.py."""
    plugins = kitebase.plugins.PluginsManager()
    plugins.load_config(CONFIG)
    kitebase.utils.register_standard_handlers(plugins)   # before load_plugins
    plugins.load_plugins()                               # discover + merge
    app = kitebase.utils.get_app()
    app.calc_db(plugins)                                 # schema from the merge
    # app.add_query_behavior(Archivable)                 # behaviours go here
    return app

def setup(generate=True):
    """The schema plus model.py, regenerated when the YAML or the generator moved."""

def setup_db(create_all=True, check_schema=True):
    """The application with the database open: (app, plugins, model)."""

def seed_admin(app, model, username="admin", password="admin"):
    """An administrator if the table is empty: development data, not a mechanism."""
```

Three levels, because not every caller needs everything: `check` and the
`dump-*` commands stop at `setup_schema()`, which needs no database; the
`db-*` commands open the database without creating tables or checking the
schema, because reporting the difference is their whole point; the servers take
`setup_db()`, which stops at startup if the schema and the database differ.

`model.py` is a build artifact: `setup()` regenerates it when a plugin YAML,
`config.yaml` or the generator itself changed. Inside a plugin, a `model.py` is
real code (behaviour mixins) and is versioned.

### The commands

From the application directory, `uv run kitebase <command>`. The `kitebase`
command carries what needs no application (`new`, `dev`, `build-client`) and
hands every other command to `kite.py`, run in the application's own
environment. `uv run kite.py <command>` is the same thing, longer.

| Command | Purpose |
|---------|---------|
| `check [--dump PATH]` | Validate the merged descriptors; optionally write the full effective state as JSON |
| `dump-page <id> [--auto] [--raw]` | A page descriptor, declared or generated |
| `dump-table [Table ...]` | The effective table schema after the merge |
| `dump-types [--include-builtin]` | The type registry as an inheritance tree |
| `db-check` | What differs between the schema and the database (read-only; exits 1 if anything does) |
| `db-sync [--dry-run] [--force]` | Apply what loses no data; `--force` also drops and truncates |
| `db-backup [dest]` | A consistent copy of a SQLite database, with the server running |
| `dev` | This server and the admin client together, reloading |
| `build-client` | The admin client compiled into `clientui/` |

Dumps go to `data/` unless `-o` says otherwise (`-o -` prints).

**An application adds its own commands** in `kite.py`, on the same parser, and
dispatches them before handing the rest to `run_cli`; `kitebase <its command>`
reaches them like any other:

```python
parser = make_parser()
p = parser.commands.add_parser("import-items", help="...")
p.add_argument("--dry-run", action="store_true")
args = parser.parse_args()

if args.command == "import-items":
    app, plugins, model = setup_db()
    result = app.cp.send({"operation": "import_items",
                          "parameters": {"dry_run": args.dry_run}})
elif args.command in DB_COMMANDS:
    ...
```

The command calls an endpoint of the plugin in process: the operation lives in
the plugin, and the command only says what happened.

---

## `config.yaml`

The single file that says what the app is. Every relative path in it hangs from
its own directory, so the application starts from anywhere.

```yaml
name: myapp
version: 0.1.0

# Plugin roots, in order: a path, or { path, include } to take part of a root.
plugins:
  - path: /path/to/kitebase-commons/plugins
    include: [common, partners]
  - plugins

db_engine: "sqlite:///data/myapp.sqlite"   # any SQLAlchemy URL

timezone: Europe/Rome      # stated: a process on another clock refuses to start

migrations:
  on_startup: error        # the server looks; `kitebase db-sync` changes

authentication:
  user_table: User
  username_field: username
  password_field: password
  context_fields: [id, username, email, is_active, is_admin]   # into the JWT

api:
  prefix: "kitebase"       # routes under /kitebase/*
  port: 8300

client:
  role: app                # `app`: the client owns "/"; `admin`: under /admin/
```

Optional sections (`schema`, `dataview`, `read_files`, `password_policy`,
`locale`, ...) have sane defaults; add them only to override. A name provided by
two plugin roots is refused: the scaffold's own `users` excludes the one in the
shared plugins.

---

## The servers

A server is the bootstrap at module load, then one call that registers the
routes. The reusable logic (JWT auth, request context, the dispatch) lives in
`kitebase.server_utils` and is framework-agnostic; `register_flask` and
`register_fastapi` put the same routes on either framework:

- `POST /{prefix}/auth/login` and `.../auth/update_context`, because they mint
  JWTs;
- **one dispatcher**, `POST /{prefix}/endpoint/{op}`, for everything else
  (`db`, `query`, `get_page`, `get_menu`, the plugins' own endpoints);
- `GET /{prefix}/info`: the application, and the `client:` section that tells
  the client who logs people in;
- with `host_session=`, also `POST /{prefix}/auth/token`, for an application
  that already logs people in with its own pages and wants one login only.

They register nothing application-wide (no CORS, no exception handler), and
take an app or a Blueprint/APIRouter, so a host application can mount kitebase
next to its own routes.

```python
import kitebase.server_utils as srv
import kite

srv.setup_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FILE") or None)

kitebase_app, plugins, model = kite.setup_db()
kite.seed_admin(kitebase_app, model)
SECRET_KEY = os.environ.get("SECRET_KEY", "development-secret-key-not-for-service")

app = Flask(__name__)
srv.register_flask(app, kitebase_app, plugins, SECRET_KEY)
srv.serve_client_flask(app, kite.APP_DIR, plugins.config)    # clientui/, if built
```

The library logs one line per request and never installs a handler:
`setup_logging` is the process deciding to listen, on stdout and, if asked, on a
rotating file. The compiled client is mounted where `client:` says, last, so the
API routes win.

### Launching

```bash
uv run server_flask.py                          # development, embedded launch
uv run server_fastapi.py                        # the same; OpenAPI docs on /docs
waitress-serve --port=8300 server_flask:app     # service (Flask)
uvicorn server_fastapi:app --port 8300          # service (FastAPI)
```

`app` is at module level for exactly this reason: in service a WSGI or ASGI
server takes it as it is. Not for throughput: `app.run()` is Werkzeug's
development server.

---

## Plugins (in brief)

A plugin is a directory with a `config.yaml` and some YAML and Python. The
application's own sits under `plugins/`; shared ones (the kitebase-commons root)
are listed in `config.yaml`. Kitebase imports every `.py` of a plugin and
registers what is decorated with `@endpoint`: that is where the operations of
the domain go, because from there the same function is reachable from the
dispatcher, from another module in the process and from a command.

```
plugins/myapp/
  config.yaml      name, version, depends_on
  model.yaml       types and tables
  pages.yaml       pages and views, when the generated ones are not enough
  menu.yaml        menu entries
  *.py             endpoints and behaviour mixins
```

A table gets, with no further declaration, its model, its CRUD through `db`,
and a generated list and form (`item_list`, `item_form`). The full model is the
subject of [PLUGIN_MODEL.md](PLUGIN_MODEL.md).

---

## The client

The admin client is the generic **shell** of `kitebase-ui` (SvelteKit, Svelte 5,
TypeScript, Tailwind 4), pointed at this application. It is descriptor-driven:
`get_page`, `get_menu` and the type schema say *what* to render, the components
decide *how*. An application does not own a client: it contributes `.svelte`
components from its plugins, and the shell compiles them.

```bash
uv run kitebase dev             # server and client together, reloading
uv run kitebase build-client    # the compiled client, into clientui/
```

Both look for the `kitebase-ui` checkout beside the library (`client/` or
`kitebase-ui/`), or where `KITEBASE_UI` says. The browser talks only to Vite,
which forwards `/kitebase/*` to the backend: one origin, as in service.

---

## From here

The steps, from an empty machine to this application running with its client,
are the tutorial: [GETTING_STARTED.md](GETTING_STARTED.md). Login in development
is `admin` / `admin`, created by `seed_admin` on an empty table.
