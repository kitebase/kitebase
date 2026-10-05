# Kitebase

**Kitebase** is a data-driven framework for database applications. You declare
the schema and the interface in YAML; kitebase generates the SQLAlchemy models,
serves them through one REST dispatcher, and a Svelte client builds the
interface from the descriptors the server sends. Everything an application has
comes from plugins, composed by a deep merge, in the tradition of plugin-based
systems such as Odoo, Drupal, WordPress and Eclipse.

The name is the idea: the library is the base that holds the string, and each
application is a kite, free to fly its own way while staying tied to it. In an
application directory, the script that loads it is called `kite.py`.

It runs as an application of its own, or as the admin backend inside an existing
Flask or FastAPI application.

## Status

Version 0.x, under active development: APIs may still change. The first real
application built on it is on its way to production.

## Try it

With [uv](https://docs.astral.sh/uv/), and nothing cloned:

```bash
uvx --from "kitebase @ git+https://github.com/kitebase/kitebase" kitebase new hello
cd hello
uv sync
uv run kitebase db-sync      # create the database from the YAML schema
uv run server_flask.py       # the API on http://localhost:8300, admin/admin
```

The admin client, the shared plugins and a library you can edit need the
workstation: [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) walks the whole
road from an empty machine, and every step of it has been tried.

## The three repositories

| repository | on disk | what it is |
|---|---|---|
| [kitebase](https://github.com/kitebase/kitebase) | `server/` | the library: plugins, model generation, dispatcher, CLI |
| [kitebase-ui](https://github.com/kitebase/kitebase-ui) | `client/` | the client library and the generic shell (Svelte 5) |
| [kitebase-commons](https://github.com/kitebase/kitebase-commons) | `commons/` | shared plugins: a vocabulary of types, mixins, the party model |

## How it works

- **Plugins.** An application is a list of plugin roots in `config.yaml`. Each
  plugin brings YAML (tables, pages, menus) and Python (endpoints, behaviours),
  and the plugins are merged in dependency order: a plugin extends another
  without editing it.
- **The schema.** Tables are declared in `model.yaml`, with types that say what
  a column is (`Money`, `UpperCode`), not only how it is stored. The models are
  generated from it, and `kitebase db-sync` aligns the database without ever
  dropping data.
- **The interface is data.** Pages, forms and menus are descriptors the server
  sends; every table has a list and a form before anyone writes one, and a page
  declared in YAML replaces or extends the generated one.
- **One dispatcher.** Everything except login goes through
  `POST /kitebase/endpoint/{op}`: the built-in CRUD and queries, and the
  operations a plugin declares with `@endpoint`.
- **The querybuilder.** A JSON query language the client can use to ask for
  joins, filters and aggregations, translated to SQLAlchemy on the server.
- **The client.** The shell is generic: it draws what the descriptors say, and
  compiles in the `.svelte` components the plugins bring. `kitebase dev` runs it
  against an application; `kitebase build-client` compiles it into the
  application, which then serves client and API from one origin.
- **Two frameworks.** The same routes on Flask and on FastAPI; an application
  installs the one it serves with, `kitebase[flask]` or `kitebase[fastapi]`.

## Working on kitebase itself

```bash
mkdir kitebase && cd kitebase
git clone https://github.com/kitebase/kitebase.git         server
git clone https://github.com/kitebase/kitebase-ui.git      client
git clone https://github.com/kitebase/kitebase-commons.git commons

cd server
uv sync --all-extras         # the library, editable, with the tests and both frameworks
uv run pytest
cd devtest
uv run kitebase dev          # the library's bench, with its client
```

GETTING_STARTED § 1 explains the layout, and § 5 how a workstation is verified.

## Documentation

- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md): from an empty machine to an
  application with its client and the shared plugins.
- [docs/PLUGIN_MODEL.md](docs/PLUGIN_MODEL.md): how plugins declare the data
  model, the interface and the menu, and how the merge composes them.
- [docs/SCAFFOLDING.md](docs/SCAFFOLDING.md): the anatomy of an application,
  its bootstrap, entry points and servers.
- [CHANGELOG.md](CHANGELOG.md): one entry per release.

## License

MIT, see [LICENSE](LICENSE).
