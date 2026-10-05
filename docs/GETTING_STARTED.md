# Getting Started

How to put Kitebase on your machine, write an application that runs against your
own sources, and compile the client it is served with — starting from nothing but
git, Node and uv.

The criterion of this document is hard: **no step may require knowledge that is
not written here.** If you have to *know* something to get through it, that is
not a hiccup in your setup — it is a defect in this text, and worth reporting.

The road below builds a **workstation**: the three repositories on disk, and an
application that resolves the library from your working copy, so that editing the
library and using it are the same act. If you only want to see it run, one
command does that without cloning anything —
[Appendix B](#appendix-b--without-cloning-anything).

*Last revised: 2026-10-05. Walked on Linux; see [Appendix A](#appendix-a--prerequisites-by-platform)
for what is expected of macOS and Windows.*

---

## What you need

| | |
|---|---|
| **git** | also to install: dependencies come from repositories, not from an index |
| **uv** | it installs Python too, so Python is not a prerequisite of its own |
| **Node** | `^20.19` or `>=22.12`, the floor vite declares; `corepack enable` gives you pnpm |

Node is only for the client — and chapter 3 compiles one, so on this road it is
not optional. Skip it only if you will never build a client, and the API is all
you want.

Don't have them? → [Appendix A](#appendix-a--prerequisites-by-platform). Don't
want uv? → [Appendix C](#appendix-c--without-uv), and read why it is the paved
road before leaving it.

---

## 1. The workstation: three repositories

Make a directory to hold them — `kitebase` here, the name is yours — and clone
the three into it, each under the name of its role:

```bash
mkdir kitebase && cd kitebase

git clone https://github.com/kitebase/kitebase.git         server
git clone https://github.com/kitebase/kitebase-ui.git      client
git clone https://github.com/kitebase/kitebase-commons.git commons
```

The repository of the library is called `kitebase`, because that is the name
`pip install kitebase` looks for; on disk the directory says what it is. These
are the names the rest of this manual uses, and the ones the project's own
workstation has.

> **If git asks for a username**, on a repository that is public: GitHub served
> the ref advertisement and refused the fetch itself — `POST git-upload-pack` →
> `401 www-authenticate: Basic realm="GitHub"`. That is its anonymous rate limit,
> counted per IP, and it has nothing to do with permissions. Wait a few minutes,
> or clone as yourself if you have a GitHub account: `gh repo clone
> kitebase/kitebase server`, or the SSH form `git@github.com:kitebase/kitebase.git`
> once you have a key. `GIT_TERMINAL_PROMPT=0` in front of the command shows the
> real error instead of the prompt.

| directory | what it is |
|---|---|
| `server/` | the library: plugin system, model generation, dispatcher, CLI |
| `client/` | the client library and the generic shell |
| `commons/` | the shared plugins: types, mixins, the party model |

What you end up with, once chapter 2 adds an application of its own:

```
kitebase/
├── server/
├── client/
├── commons/
└── apps/
    └── myapp/          the application
```

**Nothing in the framework depends on that arrangement.** Three conveniences do:
the client finds the `devtest` bench by looking for `server/devtest` up to three
levels above itself, `kitebase dev` finds the client beside the library, and an
application that declares a shared plugin root writes the path to it — which is
short when they are close. Your applications say where they are, and can live
anywhere.

Then the environment, still from `kitebase/`:

```bash
uv venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
uv pip install -e "./server[dev]"      # the library, editable, plus the tests

cd client
pnpm install                           # the client workspace, once
cd ..
```

That venv is the workstation's: it is what gives you the `kitebase` command, and
the chapters below assume it is active. It is **not** the environment your
applications run in — each of those has a `.venv` of its own, made by `uv sync`.

`[dev]` brings pytest and both web frameworks. An application installs only the
one it serves with — `kitebase[flask]` or `kitebase[fastapi]`.

`pnpm install` sets up the whole client workspace — the UI library and the two
clients that consume it — in one go, and it is what chapter 3 compiles with. It
is only skippable by someone who will never build a client.

> **If another venv was already active**, deactivate it first: `uv pip install`
> targets `$VIRTUAL_ENV`, not the directory you are in, so it would install into
> that one. `env -u VIRTUAL_ENV uv ...` does the same job without deactivating.

**What you should see.** The `kitebase` command exists, and lists what it can do:

```bash
kitebase --help              # without the venv activated: .venv/bin/kitebase --help
```

---

## 2. Your first application

From `kitebase/`, into an `apps/` directory beside the three repositories.
**An application is free to live anywhere** — nothing looks for it, and it is
the one that says where things are: the library comes from its own environment,
and the shared plugins from a path it writes in chapter 4, relative to itself or
absolute. `apps/` here only keeps that path short and the tree easy to read.

```bash
mkdir -p apps && cd apps
kitebase new myapp
cd myapp
deactivate                  # the workstation venv has done its job
uv sync                     # this application's own .venv
```

The workstation venv was needed for one thing, `kitebase new`, and that is done:
from here an application runs in **its own** `.venv`, and `uv run` finds
everything there — the `kitebase` command included, because kitebase is a
dependency of the application too. Chapter 5 activates the workstation one again
for the library's tests.

> Leaving it active does no harm, but every `uv run` then says `VIRTUAL_ENV …
> does not match the project environment path .venv and will be ignored`. That
> is uv telling you it used the application's environment — which is the one that
> must run. The warning is the reason for the `deactivate` above, not a problem
> to solve.

`kitebase new` knows where the kitebase that ran it lives. Yours is a checkout, so
the generated `pyproject.toml` carries a block that an application created from
the published library would not have:

```toml
[tool.uv.sources]
kitebase = { path = "/…/kitebase/server", editable = true }
```

The application now resolves the library from your working copy: edit the
library, and it sees the change at once. **The line that proves it:**

```bash
uv run python -c "import kitebase; print(kitebase.__file__)"
# → /…/server/kitebase/__init__.py     a path inside your checkout
# → /…/.venv/…/site-packages/…        the published copy: the block is missing
```

`uv sync --no-sources` resolves the way a machine without that checkout would —
which is what to run before believing an application is portable.

### It runs

```bash
uv run kitebase db-sync     # creates the database from the YAML schema
uv run server_flask.py      # the API on http://localhost:8300
```

`kitebase db-sync`, like every command that needs the application loaded, is
handed to the application's own script — `kite.py`, which `kitebase new` wrote —
and run in its environment: `uv run kite.py db-sync` is the same thing, spelled
out. What each generated file is for, and why it is split that way, is in
[APPLICATION.md](APPLICATION.md).

`db-sync` prints the SQL it ran — one `CREATE TABLE users`, because an
application is born knowing only who logs into it. Then, at
<http://localhost:8300/>, the server says what it is and what it hasn't got:

```json
{"application": "myapp", "api": "kitebase/",
 "client": "not built — run `kitebase build-client`"}
```

**There is no page yet, and that is the right answer**: a client is compiled in
chapter 3. What is already complete is the API, and it answers.

**Open a second terminal now and `cd` to the application** — the server holds
the first one until Ctrl-C, and everything from here is typed in the second.
Nothing to activate in it: `uv run` finds this application's environment on its
own. In chapter 3, `kitebase dev` takes the first terminal the same way.

Log in as `admin` / `admin`, the user a generated application seeds for itself:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"admin"}' \
     http://localhost:8300/kitebase/auth/login; echo
# {"status": "success", "data": {"token": "eyJ..."}}
```

Every answer comes in that envelope, `{"status": …, "data": …}`. From here on
this manual shows only what is inside `data`. The `; echo` is there because the
server does not end its answer with a newline, and without it the prompt would
start on the same line.

Every other call carries that token, so keep it in a variable — in this same
terminal, since a shell variable belongs to the shell that set it. The chapters
below assume `$TOKEN` holds it:

```bash
TOKEN=$(curl -s -X POST -H 'Content-Type: application/json' \
        -d '{"username":"admin","password":"admin"}' \
        http://localhost:8300/kitebase/auth/login \
        | python3 -c 'import sys, json; print(json.load(sys.stdin)["data"]["token"])')

echo "$TOKEN" | cut -c1-28        # eyJhbGciOiJIUzI1NiIsInR5cCI6
```

A token lasts a day by default, so this one sees the manual out. If a call
answers `Invalid token: Not enough segments`, the variable is empty — run the
line again in the shell you are calling from, and look at what the login replies.

### The first table

Stop the server — Ctrl-C in the first terminal. It builds its model at startup
and only ever looks at the schema, so a schema about to change wants it down;
you will start it again in a moment.

Everything an application *does* lives in its plugins. Open
`plugins/myapp/model.yaml`: it holds a commented-out example and an empty
`tables: {}`. **Replace the whole file** — that line included, and not by adding
your tables above it: YAML keeps the last of two keys with the same name without
complaining, so the table you wrote would lose to the empty one and `db-sync`
would report nothing to do.

```yaml
tables:

  Book:
    name: books
    label: "Book"
    columns:
      - name: id
        type: Integer
        primary_key: true
        autoincrement: true

      - name: title
        type: String
        length: 200
        nullable: false
        label: "Title"
        searchable: true

      - name: isbn
        type: String
        length: 20
        unique: true
        label: "ISBN"

      - name: price
        type: Numeric
        label: "Price"

      - name: published_on
        type: Date
        label: "Published"
```

```bash
uv run kitebase db-check    # what differs, read-only
uv run kitebase db-sync     # apply it: whatever loses no data, never a drop
```

**What you should see.** `db-sync` names the change before making it:

```
Applicable automatically (1):
  + books                                    new table (5 columns)
```

The server only ever looks: when the schema the plugins describe differs from the
database, it refuses to start and says what differs. Changing the database is
always an explicit command.

And the table you declared is already an interface. With the server running and
a token from `auth/login`:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"id":"book_list"}' http://localhost:8300/kitebase/endpoint/get_page; echo
```

```json
{"title": "Book", "_auto": true,
 "content": {"type": "table", "source": {"model": "Book"},
             "columns": [{"field": "id"}, {"field": "title", "title": "Title"}, …],
             "navigator": true}}
```

**Nobody wrote that.** The client knows nothing about `Book`: it draws whatever
the server hands it, and what the server hands it is the JSON above — a *page
descriptor*, data rather than code. No page is declared under the id
`book_list`, so kitebase built one from the schema you had just written, and
marked it `_auto: true` so that you can tell which it is.

Two ids answer for every table, `{table}_list` and `{table}_form`. The day the
generated one is not enough — a different order of columns, a filter, a layout —
declare a page under that same id in your plugin's YAML: it is served in place of
the generated one, and nothing else changes, the client least of all. When the
change is small — a title, a command — add `$auto: true` to that page and write
only what differs: the generated page is its base (`PLUGIN_MODEL.md` § 5.7).

### And a way in

A table is reachable by the API the moment it is declared, but nothing puts it in
front of a person: that is the menu's job, and the generated one only knows about
users. `plugins/myapp/menu.yaml` ends with a commented example — replace that
line with:

```yaml
  books: { label: Books, icon: book, parent: masters, order: 20, action: stack_push, panel: book_list }
```

`panel: book_list` is the id of the page you just asked for, `parent: masters`
hangs the item under the group the file already declares, and the icon is a
[lucide](https://lucide.dev) name. Restart the server and ask what the menu is:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{}' http://localhost:8300/kitebase/endpoint/get_menu; echo
```

```json
{"label": "myapp", "id": "main", "items": [
  {"label": "Masters", "icon": "database", "children": [
    {"label": "Users", "panel": "user_list", "action": "stack_push", …},
    {"label": "Books", "panel": "book_list", "action": "stack_push", …}]}]}
```

That is what the client will draw in chapter 3, and every plugin adds its own
items to a group declared elsewhere by naming its parent — which is how a menu
grows without anyone editing one central file.

---

## 3. The client, in its two forms

An application does not own a client. It contributes interface through the
`.svelte` files of its plugins, and the shell — the only re-pointable client —
compiles them in. **That is why the shell is the norm**: what makes an
application look like itself travels in its plugins.

Both commands look for the client repository next to your kitebase checkout —
`kitebase/client`, in the layout of chapter 1 — and take
`KITEBASE_UI=/path/to/client` when it is somewhere else. `uv run` runs them
from the application's own environment, where kitebase is a dependency; with the
workstation venv active, plain `kitebase …` works too.

**In development** — two processes, two origins. **Stop the server of chapter 2
first**: `kitebase dev` starts one itself, and two cannot hold the same port —
it checks, and says so, rather than letting uvicorn fail underneath. Then, in
that same first terminal:

```bash
uv run kitebase dev
#   server  server_fastapi.py  →  http://localhost:8300
#           against the library at /…/kitebase
#   client  shell              ←  /…/myapp           http://localhost:5174
```

**Open <http://localhost:5174> and log in with `admin` / `admin`.** This is the
first page of the whole road: the menu, and the list and form of the table you
declared, drawn from the descriptors the server sends.

Those credentials are seeded by a generated application the first time it runs,
and only while the `users` table is empty — so that there is a way in, not as a
mechanism: in service the first account is made by hand and the seed does
nothing. Which also means it is a development password, and until you replace it
it is the whole security of this application.

The client is served by vite on a port of its own, but the browser never sees
two origins: vite proxies `/kitebase/*` to the backend, on the port `config.yaml`
declares, so no server enables CORS — in development as in service. Ctrl-C stops
both — and if either one dies, the other is stopped with it, because a client
talking to nothing looks like a bug in the application.

> The first run in a fresh clone warns that it *cannot find base config file
> "./.svelte-kit/tsconfig.json"*. SvelteKit writes that file while starting, so
> the warning is about something that exists a second later, and it does not
> come back.

**Compiled** — one process, one origin. Stop `kitebase dev` first, for the same
reason, then:

```bash
uv run kitebase build-client   # → clientui/, which this application's server serves
uv run server_flask.py        # http://localhost:8300 — client and API together
```

One origin, as in development — only now it is the backend that serves the
page. When something works in one form and not in the other, the difference
between the two is the first place to look.

Served this way the application answers on the network too —
`http://<this machine's address>:8300` from another machine — because the
compiled client calls whatever origin served it, and no address is baked in.

> A client of your own is possible — `client/apps/playground` is one, bound to
> the devtest bench — but it is only needed for what plugins cannot
> contribute. Today it lives inside the client repository: `@kitebase/ui` is
> consumed as a workspace package, so a client outside that checkout has no way
> to depend on it.

### What `kitebase dev` does

One command, four decisions — worth knowing, because each of them is something
you may want to make yourself:

1. **It picks the entry point.** With both servers present it takes
   `server_fastapi.py`; `--flask` asks for the other. The line it prints names
   the file it chose, which is the answer to *"which one is running?"*
2. **It starts that server in the application's own environment** (`uv run`) —
   and layers the library checkout on top for the run only, which is what the
   second line of its output says.
3. **It starts the shell** in the client repository, pointed here by
   `KITEBASE_APP_ROOT`.
4. **It keeps the two together**, and takes both down when either one stops.

The same thing by hand, in two terminals — no magic, and the way to run only one
half:

```bash
# terminal 1 — the server
uv run server_flask.py                         # or server_fastapi.py

# terminal 2 — the client, told which application it serves
cd ../../client
KITEBASE_APP_ROOT=/path/to/myapp pnpm --filter shell dev    # localhost:5174
```

`kitebase dev --no-client` and `--no-server` run one half with the other left to
you. And the compiled build has a long form too:

```bash
cd ../../client
pnpm build:app /path/to/myapp                              # → myapp/clientui/
```

---

## 4. Using what isn't yours: the shared plugins

**Stop what is running first** — the server, or `kitebase dev`. A plugin root is
read once, when a process starts: the backend builds its model from it, and the
client build derives its aliases and its component globs from the same list. Add
a root under a running process and neither notices; you get an application that
looks unchanged and a client that cannot resolve what the server now sends.

`commons` — the `kitebase-commons` repository — is not a Python dependency and
is never installed. It is a
**plugin root**, and an application reaches it by path — which is the whole
integration. In `config.yaml`, **replace the `plugins: [plugins]` line** — the
commented example above it says the same thing, but the path is only right when
the shared checkout sits where this manual put it:

```yaml
# apps/myapp/config.yaml
plugins:
  - path: ../../commons/plugins
    include: [common]
  - plugins
```

That path is relative to `config.yaml`. An application somewhere else says so —
`path: /home/you/src/kitebase/commons/plugins` works just as well, and is what a
deployment usually writes.

`include` is positive on purpose: what a shared root gains over time stays inert
until an application asks for it by name. Dependencies are followed for you —
`include: [partners]` brings `common` along without naming it.

> Not `users`: a generated application has a `users` plugin of its own, and a
> plugin name provided by two roots is refused, by name and by both paths.

### What arrives

Mostly a **vocabulary of types**. The generated application spells a column out:

```yaml
      - name: price
        type: Numeric
```

With `common` a column says what it *is*, and how it is stored is one line, in
one place. In `plugins/myapp/model.yaml`, **replace the first four columns of
`Book`** with these — the properties they had (`primary_key`, `length`,
`nullable`, `unique`) go away, because the type carries them; `published_on`
stays as it is:

```yaml
      - name: id
        type: ID              # primary key, autoincrement, labelled

      - name: title
        type: Description     # not nullable by its own definition
        label: "Title"
        searchable: true

      - name: isbn
        type: UpperCode       # indexed, normalised to upper case on write
        label: "ISBN"

      - name: price
        type: Money           # Numeric(10,2), and a currency widget in the form
        label: "Price"
```

```
$ uv run kitebase db-check
Applicable automatically (3):
  + configs                                  new table (6 columns)
  + books.isbn                               type VARCHAR(20) -> VARCHAR(32)
  + books                                    new index ix_books_isbn (isbn)
```

Three things happened that nobody asked for by hand: the shared plugin brought
its own table, the type brought its index, and the column was widened to the
width the vocabulary declares.

`uv run kitebase db-sync` applies them, and the book you may have entered in
chapter 3 is still there.

The interface follows in the same move. Start the server again and ask for the
form of that table: what the types know is already in it — with no UI written
anywhere.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"id":"book_form"}' http://localhost:8300/kitebase/endpoint/get_page; echo
```

Each field of the answer carries, among other keys, what its type declares:

```
{"name": "id",           "type": "ID",          "readonly": true}
{"name": "title",        "type": "Description", "required": true}
{"name": "isbn",         "type": "UpperCode"}
{"name": "price",        "type": "Money",       "widget": "currency", "precision": 10, "scale": 2}
{"name": "published_on", "type": "Date"}
```

---

## 5. Verifying the workstation

The proof that this setup is reproducible is everything above, done in an empty
directory, without opening any other file. In addition:

**Open a new terminal for this.** Not to be tidy: a check that passes because of
something the previous terminal happened to have — a variable, a venv, a
directory you were standing in — has verified that session, not the workstation.
Start from the workstation directory, and take the walk:

```bash
cd kitebase
source .venv/bin/activate       # for pytest and the bench: both want the library

cd server
pytest                          # the library

cd ../client
pnpm test                       # the client library

cd ../server/devtest
python server_fastapi.py        # the bench — http://localhost:8300, Ctrl-C to stop

cd ../..
```

What you are looking for is **no failures** — the counts move with the code, and
a number written here would be wrong within a month. The bench answers on 8300
with the same info JSON as chapter 2, saying no client is built: `pnpm build` in
`client` compiles one into `server/devtest/clientui/` if you want to see it.

And the proof worth twice the others, because no already-working machine can give
it: **clone the three repositories into an empty directory on a machine that has
never had them**, and do this again. That is how you find the file left out of
git, and the path that worked only thanks to something already on your disk.

---

## Appendix A — prerequisites, by platform

Only Linux has been walked end to end. The other two say what is expected.

**Debian / Ubuntu**

```bash
sudo apt install git curl
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv
sudo apt install nodejs npm && corepack enable
uv python install 3.12                              # only if you have no Python
```

**macOS**

```bash
brew install git node uv
corepack enable
```

**Windows**

Everything here is a command line; use PowerShell, and:

- activate a venv with `.venv\Scripts\activate`, not `source`;
- install uv with `winget install astral-sh.uv`, Node with `winget install OpenJS.NodeJS`;
- write paths in `config.yaml` with forward slashes — Python and Node both accept
  them, and a backslash inside a YAML string is an escape.

Nothing in this setup requires symbolic links, so Developer Mode is not needed.

---

## Appendix B — without cloning anything

To see an application run without building a workstation, one command is enough:

```bash
uvx --from "kitebase @ git+https://github.com/kitebase/kitebase" \
    kitebase new hello
cd hello
uv sync
uv run kitebase db-sync
uv run server_flask.py      # the API on http://localhost:8300
```

What you get is the same application chapter 2 builds, with one difference that
matters: it resolves kitebase from the repository at `main`, not from a checkout,
so there is nothing on your disk to edit. uv downloads the library into its cache
and into that application's environment; no copy of it is yours.

Good for a look. For anything else — a client, the shared plugins, a library you
can change — start at [chapter 1](#1-the-workstation-three-repositories). The
steps `kitebase new` prints mention `kitebase dev` and `kitebase build-client`:
both need the client repository, which only the workstation has, and without it
they say so.

---

## Appendix C — without uv

Everything except `kitebase dev` works with a plain venv:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e "./server[dev]"
```

Two things to know before choosing this road:

1. **`[tool.uv.sources]` is silently ignored by pip.** An application generated
   from a checkout declares that its dependency is your working copy; pip does
   not read that block, and resolves kitebase from `git+…@main` instead. Nothing
   warns you: you edit the library and nothing changes. Install the checkout into
   the application's environment yourself — `pip install -e /path/to/kitebase` —
   and check it with the `import kitebase; print(kitebase.__file__)` line above.
2. **`kitebase dev` requires uv**, and says so, naming what to run instead:
   `.venv/bin/python server_flask.py`, alongside `pnpm --filter shell dev`.

pnpm, on the other hand, is not optional: the client is a pnpm workspace whose
packages depend on each other with `workspace:*`, which npm cannot install.
`corepack enable` is all it takes.

---

## Appendix D — the two benches

```bash
cd server/devtest  && python server_fastapi.py           # 8300 — the library's bench
cd commons/demo    && uv run kitebase dev --no-client    # 8302 — the shared plugins
cd client          && pnpm dev                           # 5173 — playground, on devtest
```

`devtest` exercises the library, and is where a feature is demonstrated first.
`demo` has a venv of its own and declares kitebase as a dependency: it is the
reference consumer of the shared plugins and behaves like an application outside
the repository, because that is what it has to prove.

---

*Where to go next: [PLUGIN_MODEL.md](PLUGIN_MODEL.md) — how plugins declare the
data model, the UI and the menu, and how the merge composes them.
[APPLICATION.md](APPLICATION.md) — the anatomy of an application: the files
`kitebase new` writes, the bootstrap, the commands, the two servers.*
