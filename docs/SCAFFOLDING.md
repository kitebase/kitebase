# The Anatomy of an Application

*What a Kitebase application is made of: the files, the bootstrap sequence they
share, and the servers. To walk the road instead — from an empty machine to an
application with a compiled client — see
[GETTING_STARTED.md](GETTING_STARTED.md); for how plugins declare the data model
and the UI, [PLUGIN_MODEL.md](PLUGIN_MODEL.md).*

> **The files below are written for you by `kitebase new`.** This document
> explains what they are, and is worth reading when one of them has to change.

*Last revised: 2026-07-12; re-framed 2026-09-02.*

---

## What an app is

A Kitebase **application** is a directory with three things you write, plus the
plugins that carry the actual model and UI:

```
myapp/
  config.yaml        ← what the app is: plugins, db, api, auth
  myapp.py           ← CLI / dev harness: build schema, generate, run commands
  server.py          ← a FastAPI (or Flask) server exposing the API
  model.py           ← GENERATED from the plugins (do not edit; gitignored)
  plugins/           ← the app's own plugins (see PLUGIN_MODEL.md)
```

Everything the app *does* lives in the plugins; these three files just wire the
framework to a database, a CLI, and an HTTP server. All three share the same
short bootstrap sequence (below).

---

## The bootstrap sequence

This is the heart of every entry point — CLI harness and server alike run it:

```python
import kitebase, kitebase.plugins, kitebase.utils, kitebase.source

plugins = kitebase.plugins.PluginsManager()
plugins.load_config("config.yaml")             # read config.yaml
kitebase.utils.register_standard_handlers(plugins)  # smart-merge list handlers
plugins.load_plugins()                         # discover + merge all plugin YAML

app = kitebase.utils.get_app()
app.calc_db(plugins)                           # build the schema from merged data

# Optional: commons query behaviors (e.g. Archivable) — the core stays agnostic
from common.model import Archivable
app.add_query_behavior(Archivable)

import model                                   # the GENERATED module
app.initialize_db(plugins.config["db_engine"], model)  # engine + create tables
plugins.load_all_locales()                     # per-plugin translations
```

`register_standard_handlers` must run **before** `load_plugins` (it registers the
list merge handlers). `calc_db` turns the merged plugin data into SQLAlchemy
models; `model.py` is regenerated from it (see below), so it is a build artifact,
not source — keep it gitignored.

> **Library path.** The app must be able to `import kitebase`. In-tree apps add the
> library to `sys.path` (`sys.path.append("..")`); out-of-root apps reach it with
> the matching relative depth (`sys.path.append("../..")`). Once done, plugin `.py`
> files import by plain name because each plugin root is on the path too.

---

## `config.yaml`

The single file that says what the app is. Minimal annotated version:

```yaml
name: myapp
version: 0.0.1
description: "My application"
author: "You"
log_file: "myapp.log"

# Plugin roots — each scanned one level deep (see PLUGIN_MODEL.md § 1.2).
# A root may live outside the app tree (shared commons).
plugins: [../plugins/commons, plugins]

db_engine: "sqlite:///myapp.sqlite"   # any SQLAlchemy URL

api:
  prefix: "kitebase"          # all routes under /kitebase/*
  endpoint_prefix: "endpoint" # dispatcher → POST /kitebase/endpoint/{op}
  port: 8300

locale: it                   # default 'en' = no translation

authentication:
  user_table: User
  username_field: username
  password_field: password
  context_fields: [id, email, is_active, is_admin]  # copied into the JWT
  jwt_expiration_hours: 24
```

Optional sections (`schema`, `dataview`, `read_files`, `source_add`, …) have sane
defaults; add them only to override.

---

## `myapp.py` — CLI / dev harness

Two entry points: a light `setup_schema()` that stops after `calc_db` (enough for
the introspection commands — no DB engine needed), and a full `main()` that opens
the database and runs the app. The `__main__` block dispatches to the CLI when a
command is given, otherwise runs `main()`:

```python
from pathlib import Path
import kitebase, kitebase.plugins, kitebase.utils, kitebase.source

def setup_schema():
    """Load plugins + build schema. No DB engine — enough for CLI introspection."""
    plugins = kitebase.plugins.PluginsManager()
    plugins.load_config("config.yaml")
    kitebase.utils.register_standard_handlers(plugins)
    plugins.load_plugins()
    app = kitebase.utils.get_app()
    app.calc_db(plugins)
    return app

def main():
    app = setup_schema()
    if app.pm.should_regenerate("model.py"):      # regenerate only when YAML changed
        kitebase.source.Generator(app).generate(filename="model.py")
    import model
    app.initialize_db(app.pm.config["db_engine"], model)
    app.pm.load_all_locales()
    # … your smoke test / seed data / run logic …

if __name__ == "__main__":
    from kitebase.cli import make_parser, run_cli
    args = make_parser().parse_args()
    if args.command:
        run_cli(setup_schema(), args, output_dir=Path("data"))
    else:
        main()
```

CLI commands provided by `kitebase.cli`:

| Command | Purpose |
|---------|---------|
| `dump-page <id> [--auto] [--raw]` | Emit a page descriptor YAML (explicit or auto-generated) |
| `dump-table [table…]` | Effective table schema after merge (columns, PK, mixins) |
| `dump-types [--include-builtin]` | Type registry as an inheritance tree |
| `check [--dump PATH]` | Validate merged descriptors; optionally write the full effective-state JSON |

```bash
python myapp.py               # build + run main()
python myapp.py check         # validate the merged model
python myapp.py dump-table Book -o -   # print Book's effective schema
```

> These four are today's commands — all **introspection / validation**, read-only
> over the merged model. The CLI is expected to grow substantially: schema
> **migrations** (Alembic-based restructuring), **multi-tenant** operations (copy,
> delete, data scaffolding between tenants), and **backup**. *(planned)*

---

## The server (framework-agnostic)

A server is just the bootstrap at module load, then routes. The reusable logic —
JWT auth, request-context handling, the command dispatch — lives in
`kitebase.server_utils` (`AuthMiddleware`, `handle_generic_endpoint`), which is
**framework-agnostic**. So each server file is thin route glue over the same core;
only the web framework's syntax differs. Kitebase ships two today, **FastAPI** and
**Flask**.

Whichever you pick, the shape is identical:

- two dedicated routes — `POST /{prefix}/auth/login` and `…/auth/update_context` —
  because they mint JWTs;
- **one dispatcher** — `POST /{prefix}/{endpoint}/{op}` — for *everything else*
  (`db`, `query`, `get_page`, `get_menu`, custom endpoints…), delegating to
  `srv.handle_generic_endpoint`;
- the built Svelte client, from `<app>/clientui/`, mounted by `srv.serve_client_flask`
  / `srv.serve_client_fastapi` where `client:` in config.yaml says — at the root
  with `role: app` (kitebase is the application), under `/admin/` with `role: admin`
  (kitebase is the admin of a host). One origin either way, and `static/` stays the
  application's own. See `kitebase/clientui.py`.

**FastAPI:**

```python
import kitebase.server_utils as srv
from fastapi import FastAPI, Request
# … the bootstrap sequence (build plugins, calc_db, initialize_db) …
auth = srv.AuthMiddleware(plugins.config, SECRET_KEY)
app = FastAPI()

@app.post(f"{api}/auth/login")
async def login(request: Request): ...

@app.post(f"{api}/{ep}/{{operation}}")           # the dispatcher
async def endpoint(operation: str, request: Request):
    return srv.handle_generic_endpoint(command_processor, operation, ...)
```

**Flask** — same bootstrap, same `server_utils`, Flask glue instead:

```python
import kitebase.server_utils as srv
from flask import Flask, request, jsonify
# … the same bootstrap sequence …
auth = srv.AuthMiddleware(plugins.config, SECRET_KEY)
app = Flask(__name__)

@app.route(f"{api}/auth/login", methods=["POST"])
def login(): ...

@app.route(f"{api}/{ep}/<operation>", methods=["POST"])   # the dispatcher
def endpoint(operation):
    result = srv.handle_generic_endpoint(command_processor, operation, ...)
    return jsonify(result), result.get("status_code", 200)
```

### Launching

The server file ends with an embedded launch, so **`python server.py` just runs
it** — the simplest thing in development (it reads `port` from `config.yaml`):

```python
# FastAPI
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=plugins.config["api"].get("port", 8300))
# Flask
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=plugins.config["api"].get("port", 8300))
```

```bash
python server.py                       # dev: direct, embedded launch (both frameworks)
uvicorn server:app --reload            # dev with autoreload (FastAPI)
uvicorn server:app --workers 4         # production: process supervisor / scaling
gunicorn server:app                    # production (Flask)
```

For development you don't need the process-supervisor layer (`uvicorn`/`gunicorn`
as a separate command) — `python server.py` is enough; it matters under load, for
workers and autoreload.

### Server-agnostic by design → future backends

Because the core is exposed through `server_utils` and not tied to any web
framework, adding a **third** backend is just new route glue — no change to the
model, the endpoints, or the dispatch. **Django** is a natural next fit: it slots
cleanly into the agnostic scheme, and could be used not only as a full server but
as a way to **bolt a Kitebase admin panel onto an existing structured Django app**,
reusing its models/auth. *(Not implemented — a direction the architecture leaves
open.)*

---

## Plugins (in brief)

The app's behavior lives in plugins — a plugin is just a directory with a
`config.yaml` and some YAML/Python. The app's own plugins sit under `plugins/`;
shared ones (a `commons` root) can live outside the app tree and be listed in
`config.yaml`. That is all you need here — the full model (types, tables, pages,
views, menus, the merge algorithm) is the subject of
[PLUGIN_MODEL.md](PLUGIN_MODEL.md).

```
plugins/
  mymodule/
    config.yaml      ← name, version, depends_on
    model.yaml       ← types + tables
    panels.yaml      ← pages + views
    menu.yaml        ← menu entries
    plugin.py        ← custom endpoints (optional)
```

---

## The client (Svelte) *(evolving)*

The frontend is a SvelteKit application (Svelte 5 + TypeScript + Tailwind 4 +
bits-ui) that talks to the API through one unified client (`api.login`,
`api.endpoint`). It is **descriptor-driven**: `get_page`, `get_menu`, and the type
schema tell it *what* to render; the components decide *how*. The developer rarely
writes UI — the same data-driven principle as the backend.

`client/svelte/` is a **pnpm workspace**:

```
client/svelte/
  packages/kitebase-ui/   ← @kitebase/ui — the reusable component library (thin)
  apps/devtest/          ← sandbox / demonstrator app
  apps/shell/            ← the Chrome shell app
```

The library stays thin on purpose: full-stack plugins can ship their own `.svelte`
components, so complexity lives in the plugins, not in `@kitebase/ui`. A separate
dev tool, `client/inspector/`, renders the effective merged state (`dump_app`) and
is deliberately independent of `@kitebase/ui`.

This layer is **under active development** — treat the specifics as moving; the
stable contract is the API (the dispatcher + descriptors) it consumes.

---

## Running the whole thing

```bash
# backend
python myapp.py            # build schema, (re)generate model.py, seed/smoke test
python server.py           # run the API (FastAPI or Flask); uvicorn/gunicorn for load

# frontend (Svelte client — pnpm workspace, per app)
cd client/svelte && pnpm install
pnpm --filter shell dev    # dev server for the chosen app (shell / devtest)
pnpm build:app <app>       # production build, into <app>/clientui/, mounted by the backend
```
