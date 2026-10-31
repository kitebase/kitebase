# devtest

The sandbox application of the library: a small book catalogue with loans,
members and reviews, where every feature is tried and shown before an
application uses it. It is a regular application (`docs/APPLICATION.md`),
with `devtest.py` in the place of `kite.py`.

## The shared plugins

`devtest` takes `common`, `users` and `acl` from the kitebase-commons repository, by
path, as any application does (`config.yaml`, `plugins:`): it expects the
checkout beside this one, in the layout of `docs/GETTING_STARTED.md`

```
kitebase/
├── server/      this repository, devtest inside
├── client/
└── commons/     kitebase-commons
```

What devtest adds is in its own plugins, as differences: `devtypes` derives the
types it needs (`SKU`, `Price`) from the shared ones, and `libapp/library` adds
columns to `User` and methods to its own tables. The shared plugins are not
copied and not edited here; a change to them is made in kitebase-commons, and
moves the golden of the merged tree (`tests/golden/devtest.json`).

## Users and access control

`acl` is on, with the roles of the library in `plugins/libapp/library/acl.yaml`
and one user per role; the password is the username. The home page lists them
with what each is meant to see, and the login page says whom to try.

| user | role | sees |
|---|---|---|
| `admin` | administrator | everything: not a role, it passes every check |
| `catalog` | Cataloguer | the catalogue; no loans, no members |
| `desk` | Loans desk | members and loans; books read only, other tables by their label |
| `librarian` | Librarian | cataloguer and loans desk together, plus the reviews and the Playground demos |
| `stats` | Statistics | Loan statistics, by book and by month; the loan list answers 403, it shows the members' names; no Playground group |

A database made before them gets them with `devtest.seed_staff`; a new one has
them from the seed. `kitebase check` lists what the roles leave uncovered.

## Running it

From this directory, in the environment of the `server` repository:

```bash
uv run devtest.py            # generate model.py, seed the database, print a smoke test
uv run server_fastapi.py     # the API on http://localhost:8300
uv run server_flask.py       # the same routes, on Flask
```

`devtest.py` is the bootstrap both servers import: it loads the plugins,
regenerates `model.py` from the YAML schema and fills an empty
`devtest.sqlite` with the sample data, so the tests and the two servers always
run the same application. Run on its own, it also prints the merged panels and
a few queries.

It carries the library commands as well (`cli: devtest.py` in `config.yaml`):

```bash
uv run kitebase db-check           # what differs between the database and the schema
uv run kitebase db-sync --dry-run  # the DDL it would run
uv run kitebase db-sync            # apply it: adds only, never drops
```

## The API

Every server of the library exposes the same routes, under the `prefix` of
`config.yaml` (`kitebase` here):

```
GET  /kitebase/info                    public: name, title, sign-in text
POST /kitebase/auth/login              username and password -> JWT
POST /kitebase/auth/update_context     a new JWT with the context changed
POST /kitebase/endpoint/{op}           the dispatcher: every other operation
POST /kitebase/download/{op}           an operation that answers with a file
```

`db`, `query`, `get_page` and the rest are operations of the dispatcher, not
routes of their own. A new one is a function with the `@endpoint` decorator,
in a plugin or, as `add`, in `devtest.py` itself; nothing is added to the servers.

## The client

`pnpm dev` in the `client` repository opens the playground on this
application; `pnpm build` puts the compiled shell in `clientui/`, which both
servers then serve at `/`.
