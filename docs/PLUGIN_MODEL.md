# Coframe Plugin Model — Reference Manual

*This manual describes how plugins define, extend, and override data models in Coframe.
It is written for application developers building on top of the framework.*

*Last revised: 2026-08-17. Living document — sections marked* (planned) *or* (future) *are not yet implemented.*

Coframe is designed around a single workflow: **configure, build, deploy**.
An application developer starts from a skeleton project, writes plugins that declare
data models and UI descriptors in YAML, wires a minimal set of configuration files,
runs `pnpm build`, and ships. The framework generates the database schema, the API
endpoints, and the UI from those declarations — no boilerplate, no repetition.

The plugin model described in this manual is the mechanism that makes this possible:
multiple independent plugins contribute fragments of the same data model, and the
framework merges them into a coherent whole at startup.

---

## Table of Contents

1. [How the Plugin System Works](#1-how-the-plugin-system-works)
2. [The Merge Algorithm](#2-the-merge-algorithm) — identity keys, smart merge, leaving a hook, the `$` metadata convention
3. [Types](#3-types) — primitive, inheritance, case, composite/mixin, virtual columns, secret columns, query behaviors
4. [Tables](#4-tables)
5. [Pages and Panels](#5-pages-and-panels)
6. [Data Source](#6-data-source)
7. [Table View](#7-table-view)
8. [Form View](#8-form-view)
9. [Menu](#9-menu)
10. Other View Types *(future: kanban, cards, calendar)*
11. Locale and Help *(future)*
12. Roles and Permissions *(future)*
13. Workflows *(future)*

---

## 1. How the Plugin System Works

### 1.1 What Is a Plugin?

A **plugin** is a directory containing at least a `config.yaml` file.
Everything else in the directory is optional: additional YAML files declare data
(models, panels, types…), Python files provide endpoint handlers and business logic.

```
plugins/
  common/
    config.yaml        ← required: name, version, depends_on
    model.yaml         ← types and tables contributed by this plugin
  books/
    config.yaml
    model.yaml
    panels.yaml
    plugin.py          ← custom endpoints
    data/              ← seed data, client components, docs, and anything else
```

The `data/` subdirectory (and any other subdirectory) is ignored by the plugin
loader — it is a convention for organising plugin-owned assets that the plugin
itself is responsible for consuming.

### 1.2 Application Configuration and Plugin Paths

The application's entry point is a `config.yaml` file that lists one or more
plugin root directories:

```yaml
# config.yaml
name: mytestapp
plugins:
  - plugins           # flat plugins live here: plugins/common, plugins/users, …
  - plugins/libapp    # sub-group: plugins/libapp/library, plugins/libapp/books, …
```

Each listed path is scanned **one level deep**, without recursion: only direct
subdirectories that contain a `config.yaml` are recognised as plugins.
Directories without `config.yaml` are silently skipped.

This means that if you organise related plugins under a container folder, that
folder must be listed explicitly in `config.yaml`:

```yaml
plugins:
  - plugins           # discovers: plugins/common, plugins/users
  - plugins/vendor1   # discovers: plugins/vendor1/p1, plugins/vendor1/p2
                      # (plugins/vendor1 itself has no config.yaml — it is just a container)
```

Each listed path is also added to the Python import path at startup, so `.py`
files inside plugins can use plain `import` statements. Listing paths explicitly
gives you a precise, readable map of which directories are active for both plugin
discovery and Python imports — no implicit recursion, no surprises.

#### Choosing what a root contributes (`include`)

A root written as a bare path contributes every plugin it holds. A shared root —
a commons repository several applications draw from — is usually not wanted
whole, so an entry may instead name the plugins to take:

```yaml
plugins:
  - path: ../../commons/plugins
    include: [common, users, partners]   # these, plus whatever they depend on
  - plugins                              # short form: the whole root
```

Inclusion is **positive** on purpose. What a shared root gains over time stays
inert until an application asks for it by name; an exclusion list would do the
opposite — pulling the updated repository would hand every consumer the new
plugin, and its tables, unasked. Dependencies are *followed*, not declared:
`include: [partners]` also brings `common`, and the list survives `partners`
growing a new dependency tomorrow.

Discovery still scans every root; selection happens afterwards over the complete
picture, so an included plugin may depend on one provided by a different root.
A name found in two roots is a conflict only when both are asked for — naming
the one you want in `include` is how you pick between alternative
implementations of the same plugin, and the other stays inert. A name reached as
a *dependency* and provided by more than one root is refused: include it
explicitly from the root you mean.

### 1.3 Plugin Identity and Dependencies

Each plugin has a unique **name** (defaults to the directory name, overridable in
`config.yaml`). Plugin names must be unique across the entire application.

```yaml
# plugins/library/config.yaml
name: library
version: 1.0.0
depends_on: [common, books]
```

**Naming convention.** In collaborative or multi-vendor projects, using a
`vendor_module` prefix avoids accidental collisions between independently
developed plugins (e.g. `acme_hr`, `acme_crm`). This is a convention, not
enforced by the framework. Dot notation (`vendor.module`) should be avoided
because the dot is Python's package separator and causes import errors.

The framework resolves dependencies using a topological sort (Kahn's algorithm).
Plugins are loaded in dependency order: a base plugin is always merged before
any plugin that depends on it.

Circular dependencies and missing dependencies are caught at startup and raise
a clear error.

### 1.4 Load and Merge Sequence

When `PluginsManager.load_plugins()` is called:

1. All configured plugin paths are scanned one level deep.
2. Dependency order is computed via topological sort.
3. Each plugin's YAML files (except `config.yaml`) are loaded and merged
   into a single global `data` dict in dependency order.
4. The last writer wins for scalar values; for dicts and lists the merge
   rules described in Chapter 2 apply.

The final `data` dict is the single source of truth for the entire application.

### 1.5 Sections and the Agnostic Core

The top-level keys of `data` are called **sections** (or *roots*). The pivotal
design decision is that the merge engine is **agnostic**: it does not know what
a `table`, a `view`, or a `menu` *is*. It merges generic dicts and lists,
applying the same rules (Chapter 2) to every section. Meaning is assigned
**downstream** — by the model generator (`types`, `tables`), by the descriptor
endpoints (`get_page`, `get_menu`), by the querybuilder — never by the merge
itself.

Two consequences follow, and they are the backbone of the whole system:

- **Sections are open-ended.** Adding a new kind of descriptor — the `menus`
  section, a future `chrome` or `quickbar` — requires **no change to the merge
  engine**. You declare the section in a plugin and write the consumer that
  reads it. The diagnostics validator and dumper likewise walk every non-DB
  section uniformly, with no hardcoded list of names.
- **Each root has its own rules.** A section decides how its lists are keyed
  (identity key, § 2.2), whether it registers a custom merge handler, and how
  its resolved tree is interpreted by its consumer. `tables` are keyed by column
  `name`; `menu_items` are a flat collection assembled into a tree by `parent`;
  `pages`/`views` are flat dicts whose ids are global, addressed by a `$ref` as
  `section.id`. Same engine, different rules per root.

Sections currently defined:

| Section | Role | Merge model |
|---------|------|-------------|
| `types` | reusable column types | dict merge |
| `tables` | database tables | dict merge |
| `schemas` | non-DB typed schemas | dict merge |
| `pages` | page descriptors | dict merge |
| `views` | view descriptors | dict merge |
| `menus` | menu roots (Chapter 9) | dict merge |
| `menu_items` | menu entries, flat (Chapter 9) | dict merge |

Every section is a **flat dict keyed by id**: there is no per-plugin namespace,
which is what lets a plugin extend a page, a type or a table declared by another
one (§ 5.2), and what makes the naming convention of § 1.3 matter.

The framework's generic tooling makes exactly **one** distinction: DB/type
sections (`types`, `tables`, `schemas`) feed model generation, everything else
is a **descriptor** consumed by the UI. Beyond that split, all sections are
treated the same.

---

## 2. The Merge Algorithm

### 2.1 Dictionary Merge

When two plugins contribute to the same dict key, the merge is recursive:

- A **scalar value** (string, int, bool) in the new plugin overwrites the base.
- A **dict value** is merged recursively — existing keys are updated, new keys
  are added, nothing is deleted unless explicitly overridden.
- A **list value** follows the smart-merge rules described below.

Every merged dict receives a `$plugin` metadata key recording which plugin last
touched it. This key is for internal use and is stripped before data is sent to
the frontend.

### 2.2 List Merge — Identity Keys

Plain list concatenation (`base + new`) is correct for scalar sequences but
wrong for lists of named objects: if two plugins both provide a list of columns,
you want the second plugin to *modify* the first plugin's column, not add a
duplicate.

Coframe resolves this by detecting an **identity key** on list items.
The identity key is the first recognised key found among the items:

| Priority | Key | Typical use |
|----------|-----|-------------|
| 1st | `id` | panels, tabs, action buttons |
| 2nd | `name` | table columns, form fields |
| 3rd | `field` | view columns |
| 4th | `group` | form field groups |

If no identity key is found (pure scalar lists or unrecognised dicts),
the list falls back to **plain append without duplicates**.

Append is right for a list of values and almost never what the author meant for
a list of *nodes*: with no identity there is no way to say which one this
contribution refines, so the second plugin gets its item appended beside the
first. Where two plugins actually meet on such a list the merge says so —
`merge-unkeyed-list`, a warning, because the configuration is legitimate and
only means "nobody can extend this from outside".

The remedy is an `id`. It is why the layout containers of a form — `section`,
`tabs`, a tab page, `row`, `col`, a column of a section — accept one: without it
a derived plugin can append a second section but never refine the first.

### 2.3 Smart Merge Operations

Given that an identity key is present, the following operations are available:

#### Replace (default)

An item in the new plugin with the same identity as an existing item is
deep-merged into it — the new values override specific properties while
leaving the rest intact.

```yaml
# base plugin
columns:
  - name: price
    type: Money

# derived plugin — changes only the label
columns:
  - name: price
    label: "Sale Price"
```

Result: `{name: price, type: Money, label: "Sale Price"}`

A list property of a matched item goes back through the same door rather than
being overwritten, so the merge does not stop at the first list. It matters
because a form layout is lists all the way down — `layout → section.columns →
column.fields` — and stopping meant that refining a section lost the fields it
already had:

```yaml
# base plugin
layout:
  - id: identity
    type: section
    columns:
      - id: left
        fields: [{name: title}, {name: isbn}]

# derived plugin — renames one field and adds another
layout:
  - id: identity
    columns:
      - id: left
        fields: [{name: title, label: Titolo}, {name: price}]
```

Result: `title` keeps its type and gains the label, `isbn` stays, `price` is
appended.

#### Remove

Set `$remove: true` to delete an item from the resolved list, **or a dict entry
from a keyed section**:

```yaml
columns:
  - name: internal_code
    $remove: true

menu_items:
  books:
    $remove: true          # the derived application drops the entry
```

On a menu the usual gesture is not removal but **re-pointing** — a menu item is
a dict keyed by id, so `menu_items: {books: {panel: my_book_form}}` sends the
same entry, with its label, icon and position, somewhere else.

#### Replace an entire value (`$replace`)

Merging and starting over are both legitimate, and without a word for the second
they are indistinguishable: whatever the author meant, the machinery merged.
`$replace` lists the keys of *this* dict that the contribution supersedes.

```yaml
book_form:
  content:
    $replace: [layout]     # this layout supersedes the base's, it does not merge
    layout: [ … ]
```

It works on any value, which is why it is also the answer for a scalar list:
`order_by: [price]` in a derived plugin means `[title, price]` without it.

#### Positional insert (`$after` / `$before`)

A *new* item (one whose identity is not in the base list) can be inserted at
a specific position relative to an existing sibling.

```yaml
columns:
  - name: discount
    type: Price
    $after: price        # insert immediately after the "price" column
```

If the anchor is not found at merge time, the item is appended and a warning
is logged.

> **Note:** `$remove`, `$after`, `$before` and `$replace` are merge-time
> directives. They are consumed by the merge algorithm and never appear in the
> resolved descriptor delivered to the frontend.

### 2.4 Scalar Lists (Plain Append)

Lists whose items have no identity key — such as `toolbar`, `order_by`,
`joins`, `pass` — are merged by appending new items without duplicating
existing ones. There is no per-item removal for them; `$replace` on the
containing key supersedes the whole list.

### 2.5 Leaving a Hook: Where a Derived Plugin Can Reach

Merging composes two descriptions of the **same shape**. It cannot move a node
under a new parent — that is a tree edit, and no merge directive should try to
be one. Which means a shared plugin decides, when it writes a form, how far a
derived application can go without redeclaring it.

Two rules cover the cases that actually occur:

- **Give an `id` to any container a derived plugin might refine.** Without it,
  its list can only be appended to.
- **An inline collection goes inside a `tabs`, even when it is the only one.**
  A collection is precisely the thing a derived application wants to put a
  sibling next to — the contacts of a partner and its delivery addresses — and
  adding a page to a container that exists is a plain merge, while wrapping an
  existing node in a container that does not exist is a restructure. A
  collection reached through a **button** needs no such wrapper: the button is
  already an identified node of the layout, and a second one is appended beside
  it.

Restructuring anyway, when the hook is missing and the base plugin cannot be
changed, means republishing the piece under a name and referencing it — a
`$ref` works anywhere, including inside a layout, and sibling keys override the
resolved value. This is deliberately **not** a convention of the plugin model
today: a node declared inside a list is not addressable by path, so only
something published under a key of its own can be reused, and no case has yet
asked for it. The rules above are what keeps it unnecessary.

> **Asymmetry worth knowing:** the sibling keys of a `$ref` overwrite the
> resolved value *shallowly*, and the merge directives above do not apply there.
> `$ref` plus `columns:` therefore means "that node, with these columns".

### 2.6 Metadata Keys and the `$` Convention

Any dict key beginning with `$` is **framework metadata**, not user data.
The prefix marks a key that the framework injects or interprets, and guarantees
it can never collide with a user-defined field, column, or descriptor id. The
convention is applied consistently: wherever the framework iterates the keys of
a merged section it skips `$`-prefixed keys (`startswith('$')`); wherever it
serialises a descriptor for the frontend it strips them.

`$` tokens fall into three families that share only the sigil.

**A. Merge-time metadata** — injected or consumed while plugin data is merged.
These are part of the plugin model:

| Key | Meaning |
|-----|---------|
| `$plugin` | Attribution: which plugin last contributed this node. Injected by the merge (§ 2.1); drives default cascades and diagnostics, enables fail-loud conflict detection; stripped before the frontend. |
| `$ref` | Reference: replace this node with the object at the given path, merging sibling keys on top. Resolved server-side — define once, place many (Chapter 5). |
| `$auto` | On a page: start from the auto-generated page of that id and merge this one on top (§ 5.7). Resolved server-side with the page; never delivered. |
| `$remove` / `$after` / `$before` | Smart-merge directives (§ 2.3): delete an item — or, on a keyed section, an entry — and insert a new item at a position. Consumed by the merge; never delivered. |
| `$replace` | On a dict: the listed keys supersede the base's instead of merging with them (§ 2.3). Consumed by the merge; never delivered. |

**B. Request-time placeholders** — written in descriptors but resolved *per
request* against runtime state, not during the merge:

| Token | Meaning |
|-------|---------|
| `$props.*` | Bind to props passed via `stack.push` (§ 6.4). |
| `$trigger.*` | Bind to the payload of the event that fired the load (§ 5.5, § 6.4). |
| `$or` / `$and` | Logical operators in the `db` endpoint's filter DSL — an API-request concept, not plugin YAML. AND is the implicit default (sibling conditions are AND-ed); `$or`/`$and` are needed only to build explicit groups, e.g. `(A OR B) AND (C OR D)`. |

**C. System defaults** — a `default:` (or `onupdate:`) on a *column* whose value
is a `$` token names a callable registered in the core registry
`coframe.defaults`. The generator emits a reference to it into the model, so
SQLAlchemy calls it on every insert:

| Token | Meaning |
|-------|---------|
| `$op_date` | The operator's working date: the one the user chose (`update_context`), otherwise today, read at insert time. Built in, because the core owns it: the token carries it only when the user set one. |

An unknown token is refused at generation time, naming the ones that are
registered. Applications add their own with `register_default()`, in the same
spirit as `add_query_behavior` (§ 3.7).

```yaml
- name: review_date
  type: Date
  default: $op_date        # not `date.today` — the operator's date, per request
```

Family A shapes the descriptor tree at startup, family B is interpreted when a
request arrives, family C is resolved when a row is inserted. None of them ever
reaches the frontend as a literal `$` key: attribution is stripped, refs are
expanded, directives are consumed, placeholders are substituted, defaults are
compiled into the generated model.

---

## 3. Types

### 3.1 Purpose

The `types` section defines **reusable column types**: named bundles of
SQLAlchemy attributes, widget hints, labels, and help text. A type is
referenced by its name in a table column definition and propagates automatically
to any form or view that renders that column.

Types serve two goals:

1. **DRY data modelling** — avoid repeating `{base: String, length: 32}`
   everywhere.
2. **Semantic naming** — `type: Name` communicates intent better than
   `type: String, length: 100, index: True`, and carries UI metadata (widget,
   label, help) that forms and views inherit without extra configuration.

### 3.2 Primitive Base Types

The `base` property maps to a SQLAlchemy column type. Every class in
`sqlalchemy.types` that exposes a `python_type` is registered under its own
name, so `Float`, `Time`, `LargeBinary` and the rest are available too — the
list is not the framework's to close. The ones a data model reaches for:

| Base | SQLAlchemy type |
|------|----------------|
| `String` | `String(length)` |
| `Integer` | `Integer` |
| `Boolean` | `Boolean` |
| `Text` | `Text` |
| `Date` | `Date` |
| `DateTime` | `DateTime` |
| `Numeric` | `Numeric(precision, scale)` |
| `JSON` | `JSON` |

### 3.3 Type Inheritance

A type can extend another type using `base: <TypeName>`:

```yaml
types:

  ShortStr:
    base: String
    length: 32

  SKU:
    base: ShortStr   # inherits length: 32
    nullable: false

  Name:
    base: String
    nullable: false
    index: true
```

Inheritance is resolved at model-generation time by recursively merging
parent properties before applying the child's own properties.

A child states only what it changes: the parent fills the keys the child left
out, so `{type: UpperStr, length: 2}` on a column keeps the case of the type and
narrows the width.

A type cannot be redeclared by a second plugin — the collision raises
`Type already defined`, which is what catches two plugins reaching for the same
name. Deriving is the way to extend one.

### 3.3.1 Case Normalisation

`case: upper` (or `lower`) generates a column that normalises the value on its
way to the database:

```yaml
types:

  UpperStr:
    base: ShortStr
    case: upper

  UpperCode:
    base: UpperStr
    index: true
```

Declared on a type, every column of that type inherits it; declared on a column,
it applies to that one. Absent — or `case: neutral` — the generated column is a
plain `String`.

Normalisation happens in the column type rather than in a setter, so it covers
every write path (ORM, Core, bulk) *and* the parameters of a query: a lower-case
search matches an upper-case value without either side converting. The object in
memory keeps what was assigned to it until it is refreshed; the endpoints commit
and re-read, so an API call returns the normalised value.

Use it for identifiers whose alphabet is upper case anyway — tax codes, VAT
numbers, ISO codes — and knowing the case is fixed is what lets the code around
them stop converting.

### 3.4 Composite Types (Mixins)

A type that declares a `columns` list is a **composite type** (mixin).
It expands into multiple physical columns when applied to a table via `mixins:`.

```yaml
types:

  TimeStamp:
    columns:
      - name: created_at
        type: DateTime
        default: datetime.now()
        nullable: false
      - name: updated_at
        type: DateTime
        default: datetime.now()
        nullable: false

  Address:
    columns:
      - name: address
        type: String
        nullable: true
      - name: city
        type: String
        nullable: true
      - name: country
        type: String
        nullable: true
```

A table applies a mixin via the `mixins` key (see Chapter 4).

Composite types support a `prefix` option at application time so the same
mixin can be applied multiple times without column name collisions:

```yaml
tables:
  User:
    columns:
      - name: home_address
        type: Address
        prefix: home_     # → home_address, home_city, home_country
      - name: work_address
        type: Address
        prefix: work_     # → work_address, work_city, work_country
```

#### Deriving a composite

A composite is extended by deriving it. The derived type carries the columns of
its base, in the order the base declares them; its own list refines one of them
by name, or adds to the end:

```yaml
types:

  AddressIT:
    base: Address
    columns:
      - name: country          # refines the inherited column
        type: UpperStr
        length: 2
      - name: cadastral_code   # added after the inherited ones
        type: ShortStr
```

Refining in the derived type leaves the base untouched for the tables that use
it directly. A table then names the derived type where it wants it — including a
table declared by another plugin, since columns merge by name:

```yaml
tables:
  Partner:
    columns:
      - name: address
        type: AddressIT   # was Address, declared by the partners plugin
```

This is what keeps a shared composite minimal: it holds the shape everyone
agrees on, and an application that needs more derives it instead of widening the
one everybody else uses.

#### Mixin Python inheritance

When the plugin that defines a mixin type also contains a Python class with the
same name in its `model.py`, the generated mixin class automatically inherits
from that Python class. This is how a mixin carries both **column definitions**
(YAML) and **behaviour** (Python methods, class attributes) in a single unit.

```python
# plugins/common/model.py
class Archivable:
    _cf_archive_field = 'active'   # protocol attribute read by the querybuilder

    def archive(self):
        setattr(self, self._cf_archive_field, False)

    def unarchive(self):
        setattr(self, self._cf_archive_field, True)
```

```yaml
# plugins/common/model.yaml
types:
  Archivable:
    columns:
      - name: active
        type: Boolean
        default: true
        nullable: false
```

The framework generates:

```python
# generated model.py (do not edit)
class Archivable(plugins.common.model.Archivable):
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

class Author(Base, Archivable, ...):
    ...
```

Any table that declares `mixins: [Archivable]` inherits both the `active`
column and the `archive()`/`unarchive()` methods.

### 3.5 Virtual Columns

A column declared with `virtual: true` is **not mapped to the database**.
It represents a computed value implemented as a SQLAlchemy `@hybrid_property`
in the plugin's `model.py`.

```yaml
# books/model.yaml
tables:
  Author:
    columns:
      - name: full_name
        type: String
        virtual: true
        editable: false
        label: "Full Name"
```

```python
# books/model.py
from sqlalchemy.ext.hybrid import hybrid_property

class Author:
    @hybrid_property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @full_name.expression
    def full_name(cls):
        return cls.first_name + ' ' + cls.last_name
```

Virtual columns:

- Are **excluded** from model generation (`mapped_column` is not emitted).
- Are **included** in `effective_columns` — visible to the frontend via
  `get_server_config` and serialised in API responses.
- Are **queryable** in `select`, `filters`, and `order_by` when the
  `@expression` decorator is present (the querybuilder resolves them via
  `getattr(model_class, field_name)`).
- Are always **read-only** — setting `editable: false` is recommended to
  make the intent explicit.

Virtual columns may also be declared on mixin types, following the same rules.

### 3.6 Secret Columns and Write Transforms

A column declared `secret: true` **never travels to a client**, and one
declaring `on_write:` is rewritten on its way into the database. Usually both
are declared once on a type, so every table using it inherits the rule:

```yaml
# commons/common/model.yaml
types:
  Password:
    base: String
    widget: password
    secret: true              # never read back
    on_write: password_hash   # stored hashed
```

`secret` is enforced on every read path:

- `serialize_model` drops it, so the `db` endpoint never returns it — the
  definition is resolved from the model itself, so a caller that passes no
  table definition cannot turn into a leak;
- the query `select` drops it from `*` expansions and **refuses** a query that
  names it (an omitted column would read downstream as an empty value, which is
  the kind of answer that gets mistaken for data);
- it is not addressable in `filters` or `order_by` either, from the `db`
  endpoint or the querybuilder — an equality filter on a column that is never
  returned is a way of guessing what it holds;
- the auto-generated list and form descriptors skip it.

Two consequences on writes follow from it, and are applied by the `db` endpoint:

- an **empty value is dropped, not written**: a client cannot echo back what it
  never received, so an empty secret means "unchanged" — without this, opening a
  user and saving would wipe the password;
- `on_write` names a transform from the registry in `coframe.transforms`,
  applied before the value is stored. `password_hash` (bcrypt) is built in
  because the core is its consumer — the `auth` endpoint has to know how
  credentials were stored. Apps register their own with
  `register_write_transform()`, in the same spirit as `add_query_behavior`.

A password stored in a form predating hashing is accepted and **left as it is**:
conversion happens when a password is written, never as a side effect of a
login. An existing database — including one another system still authenticates
against — therefore keeps working, and each account converts the day its owner
changes their password, which is a wanted and visible event rather than a silent
mass migration. A stored value that looks like a digest of some *other* scheme
(`$argon2id$…`, a bare hex digest) is refused instead of taking the plaintext
path: comparing it as if it were plaintext would let anyone who read the column
log in by typing the digest itself.

> `on_write` is one-way, per column, and applied at the HTTP boundary. A
> conversion that also needs the way back — a value the client renders one way
> and the database stores another — belongs one layer below, in a SQLAlchemy
> `TypeDecorator` (see `CaseString`), which covers every consumer of the models
> rather than just HTTP requests.

### 3.7 Query Behaviors

A **query behavior** is a class that the querybuilder applies automatically
to every query on a matching model. Behaviors are registered at startup via
`app.add_query_behavior()` and are completely decoupled from the querybuilder itself.

```python
# server_fastapi.py
from plugins.common.model import Archivable
coframe_app.add_query_behavior(Archivable)
```

A behavior class implements two classmethods:

| Method | Signature | Description |
|--------|-----------|-------------|
| `applies_to` | `(cls, model_class) → bool` | Return True if behavior should apply to this model |
| `apply` | `(cls, model_class, query_def, query) → query` | Modify the SQLAlchemy query object |

The `Archivable` behavior adds `WHERE active = True` to every query on a model
that carries `_cf_archive_field`, unless the caller passes `include_archived: true`
or already filters on the archive field explicitly:

```python
api.endpoint('query', { 'table': 'Author' })
# → WHERE active = True  (implicit)

api.endpoint('query', { 'table': 'Author', 'include_archived': True })
# → no filter on active  (opt-out)

api.endpoint('query', { 'table': 'Author', 'filters': { 'active': False } })
# → WHERE active = False  (explicit — behavior defers to caller)
```

This pattern is generic: any mixin can define its own behavior and register it
at startup. Examples: tenant isolation, soft-delete with `deleted_at`, row-level
visibility rules.

### 3.8 UI Hints on Types

Types can carry UI hints that propagate automatically to form widgets:

| Property | Meaning |
|----------|---------|
| `widget` | Widget override (`password`, `email`, `textarea`, `combobox`, …) |
| `label` | Human-readable column label (used in forms and tables) |
| `help` | Tooltip / contextual help text shown in forms |

```yaml
types:
  Email:
    base: String
    widget: email
    validate: email_validator
    label: "Email Address"
```

### 3.9 Merge Behaviour for Types

`types` is a plain dict. The merge algorithm applies dict-merge semantics:

- A derived plugin can **add a new type** by declaring a new key.
- A derived plugin can **override specific properties** of an existing type
  by declaring the same key with only the properties to change.
- A derived plugin can **redefine a type entirely** by declaring it with a
  full definition — the recursive merge still applies, so only listed
  properties change.

There is no positional ordering for types (they are a dict, not a list).

---

## 4. Tables

### 4.1 Purpose

The `tables` section defines the **database schema**: one entry per SQLAlchemy
model, describing columns, foreign keys, many-to-many relationships, indexes,
and mixins.

At startup, the framework reads the merged `tables` data and **automatically
generates the SQLAlchemy model classes** — no hand-written ORM code is needed.
The generated models are used directly for database creation, migrations, and
all CRUD operations exposed by the built-in endpoints.

### 4.2 Table Definition

```yaml
tables:

  Book:
    name: books              # physical table name (defaults to snake_case of key)
    label: "Book"            # human-readable name
    help: "Library catalogue entry"
    tags: [anag]             # arbitrary tags for grouping/filtering
    mixins: [TimeStamp]      # composite types applied as column sets

    columns:
      - name: id
        type: ID             # shorthand for Integer PK autoincrement

      - name: title
        type: Description    # String, not null, index

      - name: publisher_id
        type: Integer
        nullable: true
        foreign_key:
          target: Publisher.id
          ondelete: "SET NULL"

    indexes:
      - name: idx_book_title
        columns: [title, publication_date]
        description: "Search by title and year"
```

### 4.3 Table Properties

| Property | Default | Description |
|----------|---------|-------------|
| `name` | snake_case of key | Physical SQL table name |
| `label` | key name | Display name used in UI |
| `help` | — | Description shown in admin views |
| `tags` | `[]` | Arbitrary list of category tags |
| `mixins` | `[]` | Composite types whose columns are injected |
| `display_field` | by convention | Column shown as the record's label |
| `search_fields` | `[display_field]` | Columns a text search matches — replaces the display field, see below |
| `include_pk` | `true` | Whether the key takes part in a text search |

#### What a text search looks at

One text, in OR over the columns the table declares — the primitive behind the
quick search box on a list, the lookup of an FK combobox, and the value widget
of a filter on a foreign key. Callers send the text as the `search` key of a
query and never a list of columns; the builder resolves the cascade:

```
[primary key, matched exactly] + [display field, ILIKE] + [searchable: true columns, ILIKE]
```

`search_fields` replaces the display field in that middle slot; the key and the
`searchable` columns stay added. The display field itself comes from
`display_field`, or from the first column named in `schema.display_field_names`
(app `config.yaml`, default `[name, title, description]`).

The key only takes part when the table has a single-column one and the text is a
value its type could hold — typing `42` may mean the record numbered 42 and may
equally mean a title containing 42, so it is one branch of the OR rather than a
shortcut past the others. Drop it with `include_pk: false` on the table, or
`schema.include_pk_in_search: false` for the whole app.

**Secret columns never enter**, declared or not: a search that matched one would
answer whether a value is right, which is how a password is guessed one query at
a time. Virtual columns stay out of what convention derives, having no column to
compare in SQL; naming one in `search_fields` is left to whoever knows their
hybrid carries an SQL expression. A table that declares nothing searchable
**refuses** a search instead of quietly returning every row.

### 4.4 Column Properties

| Property | Type | Description |
|----------|------|-------------|
| `name` | string | Column name (identity key for merge) |
| `type` | string | Type name from `types` section, or base type |
| `nullable` | bool | Whether the column allows NULL (default: true) |
| `default` | any | Default value, Python expression, or a `$` system default (§ 2.5) |
| `unique` | bool | Unique constraint |
| `index` | bool | Single-column index |
| `case` | string | `upper` / `lower` / `neutral` — case fixed by the column type (§ 3.3.1) |
| `virtual` | bool | Not mapped to the database — a hybrid property in `model.py` (§ 3.5) |
| `secret` | bool | Never leaves the server, on any read path (§ 3.6) |
| `on_write` | string | Registered transform applied before the value is stored (§ 3.6) |
| `editable` | bool | Client hint: the column is not writable in generated forms |
| `label` | string | UI label (overrides type-level label) |
| `help` | string | UI tooltip |
| `widget` | string | UI widget override |
| `searchable` | bool | Adds the column to what a text search matches (§ 4.3) |
| `query_rank` | string | Where the column stands when a query is built — the query editor's fields and its order combo: `top`, `normal` (default), `low`, `more` (behind "Show more"), `none` (never offered). Within a rank the declaration order holds, so a derived plugin moves a field by ranking it, without reordering the base's columns. A value off the scale is an error in `check` |
| `prefix` | string | Column name prefix when expanding a composite type |
| `foreign_key` | dict | FK definition: `target`, `relation`, `backref`, `ondelete`, `onupdate`, `constraint` (hard/soft), `owned` (§ 4.5) |
| `length` | int | String length (for String-based types) |
| `precision` / `scale` | int | Numeric precision |

### 4.5 Foreign Keys

```yaml
- name: author_id
  type: Integer
  nullable: false
  foreign_key:
    target: Author.id          # Table.column notation
    ondelete: "CASCADE"        # SQL ON DELETE action
    onupdate: "SET NULL"       # SQL ON UPDATE action
```

Any key other than `target`/`constraint`/`relation`/`backref` is forwarded
verbatim to SQLAlchemy's `ForeignKey()` (so `ondelete`, `onupdate`, … work
as-is).

#### The two navigable attributes (`relation`, `backref`)

Every foreign key also generates a **pair of Python attributes** — neither of
which exists in SQL, where only the column and its constraint live:

```python
book:  Mapped['Book']         = relationship(...)   # on Loan  — the "many-to-one"
loans: Mapped[List['Loan']]   = relationship(...)   # on Book  — the "one-to-many"
```

Their names are generated as follows:

| Side | Default name | Example |
|------|--------------|---------|
| forward (on the table holding the column) | the column, **cut at the last underscore** | `book_id` → `book`, `merged_into_id` → `merged_into`, `codice_esterno_fk` → `codice_esterno` |
| forward, column with no underscore | the referenced table | `codcli` → `cliente` |
| back (on the referenced table) | the source table name | `Book.loans` |

Both defaults depend on **one** foreign key and on nothing else, which is what
makes them safe to build on: no relationship is ever renamed because of
something declared elsewhere. Two foreign keys from one table to one target
therefore keep distinct forward names for free — the columns differ — while
their reverse collections both want the source table name, and the second one
has to be named:

```yaml
- name: ship_to_id
  foreign_key:
    target: Partner.id           # → Order.ship_to, Partner.orders

- name: bill_to_id
  foreign_key:
    target: Partner.id           # → Order.bill_to
    backref: billed_orders       # → Partner.billed_orders (without it: refused)
```

`relation:` and `backref:` override each side. Use them whenever the generated
name would be poor — `children` says more than `partners` — or when the model is
refused (below):

```yaml
- name: parent_id
  nullable: true
  foreign_key:
    target: Partner.id
    relation: parent           # Partner.parent  → the parent row
    backref: children          # Partner.children → the child rows
```

**Convention:** name foreign key columns `<something>_id`. Any suffix works, and
legacy names without one fall back to the target table, but `_id` is what the
rest of the codebase reads like.

**Refusals.** Generation stops with an error rather than let two attributes of
one class share a name — the second would silently overwrite the first, losing a
column, a relationship, or an inherited method. It happens when two foreign keys
cut down to the same name (`payment_primary` and `payment_secondary` both give
`payment`), when two reverse collections land on the same table, when a
relationship takes the name of an existing column, or the name of a method
inherited from the plugin class the model is built on. The fix is always
`relation:`/`backref:`, and the error names both claimants.

Note what generation does **not** do: rename a relationship to make room for a
new one. A plugin adding a second foreign key to a table it did not write can
never move an attribute from under the code that uses it — it declares a name
for its own relation instead, in its own YAML.

**Self-references** (`parent_id → Partner.id`) need nothing special in YAML: the
generator emits `remote_side` on the forward side, which is what tells SQLAlchemy
which end of a same-class relationship is the "one" end. Several foreign keys to
the same target likewise need nothing — `foreign_keys` is always emitted, so a
second path between two tables (in either direction, added by any plugin) can
never make an existing relationship ambiguous.

#### Hard vs soft foreign keys (`constraint`)

By default a `foreign_key` emits a real **DB-level constraint**: the database
rejects a value with no matching parent row (`IntegrityError`), and referential
integrity is guaranteed. This is the right default.

Set `constraint: false` for a **soft** foreign key — a navigable relationship
with **no DB constraint**, so the column may hold values that reference nothing
(unknown or late-bound codes, dirty imports, references into a domain not yet
modelled):

```yaml
- name: author_id
  type: Integer
  nullable: true
  foreign_key:
    target: Author.id
    constraint: false          # soft: relationship kept, no DB constraint
```

- **Hard** (default): `ForeignKey` constraint in the DDL; orphan values are
  rejected at write time — a dangling FK cannot exist by construction.
- **Soft**: no constraint. The relationship still works (`book.author` returns
  the row if it exists, `None` otherwise); the raw id is stored even when it
  points nowhere. Because SQLAlchemy can no longer infer the join, the generator
  emits it explicitly (`primaryjoin` + `foreign_keys`).

> **Note on SQLite.** SQLite does not enforce foreign-key constraints unless
> `PRAGMA foreign_keys=ON` is set per connection — which Coframe does not set
> today. So on the SQLite dev database even *hard* FKs are not currently
> enforced; the distinction bites on PostgreSQL/MySQL. Soft FKs behave the same
> everywhere: never enforced, by design.

#### Ownership (`owned`)

`owned: true` says the row is a **part** of the row it points at, and does not
outlive it. Deleting the parent deletes it, and whatever it owns in turn.

```yaml
  Chapter:
    columns:
      - name: book_id
        type: Integer
        foreign_key:
          target: Book.id
          owned: true          # a chapter is a part of its book
```

It is declared on the **child's** foreign key and never as a list on the parent:
a plugin that adds a child table then declares its own belonging without reaching
into a declaration that belongs to somebody else.

The generator turns it into `cascade='all, delete-orphan'` on the parent's
collection — never on the child's scalar, since it is the parent that owns the
rows. **Enforced by the ORM, not by the DDL**, and that is a decision rather than
a shortcut:

- it reads the same on a **soft** foreign key, where the database has no say at
  all — otherwise half the mechanism would live somewhere else;
- it survives dialects that refuse the constraint: SQL Server rejects
  `ON DELETE CASCADE` on a self-referential foreign key outright, and that is a
  shape Coframe models routinely (a partner and its contacts);
- it needs **no migration**: adding it to an installed application is a code
  change, whereas altering a constraint is a schema change `db-sync` cannot
  perform — its list is closed and its comparator does not look at constraints;
- deletions stay **observable** by SQLAlchemy events, and thus by audit and by
  query behaviors. A cascade inside the engine is invisible to the application.

The database still holds the **constraint**, which is what integrity means: no
writer, ours or anyone's, can leave a reference pointing at nothing. Ownership is
a separate statement, about meaning rather than integrity — it says which rows are
parts.

**Without `owned`** the relationship keeps SQLAlchemy's default: on deleting the
parent the children are loaded and their foreign key set to NULL. That is right
for a relation that grows in time and belongs to nobody — a loan, a review — but
note what it does: the row survives with its reference **erased**. Where the
column is `nullable: false` the delete fails instead, on a driver error.

**Ownership is not the same question as presentation.** A **collection node** in a
page (`type: collection`) says a set of rows is edited inside the parent's form,
in one buffer and one transaction; `owned` says whether those rows are parts of it.
Most collections are compositions — the lines of a document, the contacts of a
partner — but not all: the reviews of a book can be edited there and still outlive
it. The two are declared separately and neither implies the other.

Because the mistake is silent, `coframe.diagnostics` **asks** — a collection node
on a foreign key that says nothing about ownership raises a *warning*, which either
answer silences. A junction needs no answer: it is owned by both ends already.
See `docs/pending/relations.md § 18`.

### 4.6 Many-to-Many Relationships

A junction table is declared with a `many_to_many` key instead of (or in
addition to) regular columns:

```yaml
  BookAuthor:
    name: books_authors
    columns:
      - name: notes
        type: String
    many_to_many:
      target1:
        table: Book.id
        column: book_id
      target2:
        table: Author.id
        column: author_id
```

The junction is always a **table of its own**, never conjured behind the scenes.
It costs a declaration, and it buys the case that matters: a relation with data
of its own (`notes` above). Frameworks that create the junction implicitly make
you convert to an explicit one the day a column is needed — with rows already in
it.

`columns:` is what *enriches* the relation and may be left out entirely: a
junction that declares only `many_to_many:` is the ordinary case.

#### The columns a junction is made of

The declaration is sugar. What it materializes, before anything else resolves:

| Column | Why |
|--------|-----|
| the key (`id`) | a junction row is a record: it can be opened in a form, updated and deleted by key, held in a buffered collection |
| `book_id`, `author_id` | the two ends, `NOT NULL`, with their foreign key constraint and the base type of the key they point at |
| unique index on the pair | one link per pair — what a composite key used to guarantee |

They are ordinary columns from that point on: the generated model, the schema
the client receives, `db` CRUD and an auto-generated form all treat the junction
like any other table. `dump-table BookAuthor` shows them.

Two ways to decide otherwise:

- **the whole installation** — `schema.pk_name` in `config.yaml` names the
  generated key (default `id`). It decides what the framework *writes*, never
  what it reads: reading always goes through the columns that declare the key,
  so a table out of convention keeps working;
- **one table** — declare a primary key of your own in `columns:`, and nothing
  is injected. That is the escape for a legacy junction whose key is not ours
  to choose.

#### What a junction generates

Six attributes, three per side. For the junction above:

| Attribute | On | Is | Default name |
|-----------|----|----|--------------|
| `book`, `author` | `BookAuthor` | the row's two ends | the **column**, cut at the last underscore |
| `author_m2m` | `Book` | the junction rows, with their own columns | other target's class + `_m2m` |
| `authors` | `Book` | the shortcut past the junction (`viewonly`) | other target's table name |
| `book_m2m`, `books` | `Author` | the same two, mirrored | as above |

Each is renameable, on the target it belongs to:

```yaml
      target1:
        table: Book.id
        column: book_id
        relation: book             # BookAuthor.book   — on the junction
        backref: author_rows       # Book.author_rows  — the rows
        collection: authors        # Book.authors      — the shortcut
```

`relation:` and `backref:` mean exactly what they mean on a foreign key: a
junction row *is* two foreign keys. `collection:` is the third name, the one
foreign keys have no equivalent for.

#### Two junctions on the same pair, and self-referential junctions

Authors and reviewers over the same two tables is a second junction between
`Book` and `Author`. Its six default names are the same six, so it is refused
until it names them — in its own YAML, leaving the first junction untouched:

```yaml
  BookReviewer:
    name: books_reviewers
    columns:
      - name: rating
        type: Integer
    many_to_many:
      target1:
        table: Book.id
        column: book_id
        collection: reviewers      # Book.reviewers
        backref: review_rows       # Book.review_rows
      target2:
        table: Author.id
        column: author_id
        collection: reviewed_books # Author.reviewed_books
        backref: review_rows       # Author.review_rows
```

A junction whose two targets are the **same** table (partners linked to
partners) works the same way. Its two junction-side attributes are free, since
they come from the two columns, which cannot share a name; the four on the
target must be declared. The joins are generated in full (`primaryjoin` +
`secondaryjoin`), because with both columns pointing at one table there is
nothing for SQLAlchemy to infer.

#### A junction is owned by both its ends

Ownership (§ 4.5) is **on by default for both targets**, and nothing is written
in the YAML: a junction row means nothing without either end. Deleting a book
takes its `books_authors` rows and leaves the authors untouched — a cascade only
ever runs from parent to child, and the author is a *parent* of that row.

The cascade goes on the rows collection (`Book.author_m2m`) and never on the
shortcut that skips the junction (`Book.authors`), which is `viewonly` and could
not carry it anyway.

A target may derogate, for the junction that is really a historical record of one
of its ends:

```yaml
    many_to_many:
      target2:
        table: Author.id
        column: author_id
        owned: false        # the rows survive the author
```

### 4.7 Indexes

Compound indexes are declared at table level under `indexes`. Each index item
has `name` as its identity key for merge purposes.

```yaml
indexes:
  - name: idx_author_fullname
    columns: [first_name, last_name]
    unique: false
    description: "Search by full name"
```

### 4.8 Mixins

`mixins` is a list of composite type names. The framework expands each mixin
by injecting its columns into the table's column list at model-generation time.

```yaml
tables:
  LibraryUser:
    mixins: [TimeStamp, Credentials]
    columns:
      - name: id
        type: ID
      - name: name
        type: Name
```

Result: `id`, `name`, then all columns from `TimeStamp`, then all from
`Credentials` — in declaration order.

The generated SQLAlchemy class inherits from each mixin class, which in turn
inherits from the plugin's Python class if one exists (see § 3.4). This means
mixin columns, methods, and protocol attributes are all available on the table
model at runtime.

`effective_columns` — the full column list used by the API and the frontend —
includes mixin columns (real) and mixin virtual columns, in addition to the
table's own columns. Panel descriptors and `get_server_config` always reflect
the complete set.

### 4.9 Merge Behaviour for Tables

`tables` is a dict keyed by table name (the `Book`, `Author`, etc. keys).
Merge semantics:

- A plugin can **add a new table** by declaring a new key.
- A plugin can **add columns to an existing table** by declaring the same table
  key with only the new columns — the `columns` list is smart-merged by `name`.
- A plugin can **modify a column** (change label, widget, nullability…) by
  declaring an item with the same `name` and only the properties to change.
- A plugin can **remove a column** with `$remove: true`.
- A plugin can **add an index** by appending to `indexes` (smart-merged by `name`).
- Top-level table properties (`label`, `tags`, etc.) follow scalar override rules.

#### Example: extending a base table

```yaml
# base plugin (common)
tables:
  User:
    mixins: [Credentials]
    columns:
      - name: id
        type: ID
      - name: name
        type: Name

# derived plugin (hr)
tables:
  User:
    columns:
      - name: employee_id    # new column appended
        type: ShortStr
        unique: true
        nullable: true
        $after: name         # insert after "name"
      - name: department
        type: String
        length: 50
        nullable: true
```

The resulting `User` table has: `id`, `name`, `employee_id`, `department`,
plus everything from the `Credentials` mixin.

### 4.10 The `types` / `tables` Interaction

Types and tables interact through the type resolution chain:

```
column.type  →  types[name]  →  base type  →  SQLAlchemy column
```

A derived plugin that changes a type definition automatically affects **all**
columns of that type across all tables, without touching the table definitions.
This makes types a powerful customisation lever.

---

## 5. Pages and Panels

### 5.1 Two Concepts, One Hierarchy

The navigation layer is built from two distinct concepts:

- **Page** — a navigable unit pushed onto the application stack. A page has a
  title, a main `content` area and optionally one or more split `panels`.
  A page is what the user "opens".

- **Panel** — a split area within a page, loaded in response to events fired by
  the main view (a row click, a form load). A panel has a position (`right`,
  `bottom`), a size, and its own content — typically a view (see Chapter 6).

A page's `content` and each panel's content reference **views** — self-contained
data descriptors described in Chapter 6.

```
stack
 └── page: book_detail
      ├── content: book_edit_view        ← view (Chapter 6)
      └── panels:
           └── panel: authors            ← split area, pos: right
                └── content: book_authors_view   ← view (Chapter 6)
```

### 5.2 Merge Model

`pages` and `views` follow the same flat dict merge model as `types` and
`tables`. There is no namespace wrapping: a plugin that declares `book_list`
contributes to the same `book_list` key as any other plugin that declares it.

This means a derived plugin can extend any page or view declared by a base
plugin — adding a column, inserting a panel, changing a policy — using exactly
the same merge semantics described in Chapter 2.

```yaml
# base plugin (libapp) — declares the page
pages:
  book_list:
    title: Books
    content:
      $ref: "views.book_list_view"

# derived plugin (hr) — adds a column to the view
views:
  book_list_view:
    columns:
      - field: department
        title: Department
        $after: title
```

**Collision avoidance.** Two unrelated plugins that happen to declare the same
page id would be merged together, which is almost certainly wrong. The
`vendor_module` naming convention (§ 1.3) prevents this: `libapp_book_list`
and `hr_book_list` are unambiguously separate pages.

**`$ref` paths** use the standard dot-notation `section.id`:

```yaml
content:
  $ref: "views.book_list_view"
```

### 5.3 Page Definition

```yaml
pages:

  book_detail:
    title: "Book"
    breadcrumb: "$props.title"     # dynamic breadcrumb from stack props
    confirm_on_leave: true         # warn before navigating away with unsaved changes
    content:
      $ref: "views.book_edit_view"
    panels:
      - id: authors
        pos: right
        width: 320
        collapsed: false
        trigger:
          event: form_load
          pass: [id]
        $ref: "views.book_authors_view"
```

#### Page properties

| Property | Description |
|----------|-------------|
| `title` | Static title shown in the stack header |
| `breadcrumb` | Dynamic title, can reference `$props` passed via stack push |
| `confirm_on_leave` | If true, prompts the user before closing with unsaved changes |
| `content` | Main content area — inline descriptor or `$ref` to a view |
| `panels` | List of split areas (see § 5.4) |

### 5.4 Panel (Split Area)

A panel is a split area attached to its parent page or to another panel,
creating nested layouts.

```yaml
panels:
  - id: detail
    pos: right          # right | bottom | left | top
    width: 400          # used when pos is right or left (pixels)
    height: 250         # used when pos is bottom or top (pixels)
    collapsed: false    # initial collapsed state
    trigger:
      event: row_click  # what event loads this panel's content
      from: content     # which area fires the event (default: content)
      pass: [id]        # which fields to forward
    $ref: "views.loan_detail_view"
```

#### Panel properties

| Property | Default | Description |
|----------|---------|-------------|
| `id` | — | Identity key for smart merge |
| `pos` | — | Attachment position: `right`, `bottom`, `left`, `top` |
| `width` | — | Size in pixels (horizontal panels) |
| `height` | — | Size in pixels (vertical panels) |
| `collapsed` | `false` | Whether the panel starts collapsed |
| `trigger` | — | Event that loads the panel content (see § 5.5) |
| `content` | — | Inline content descriptor |
| `$ref` | — | Reference to a view or page |
| `panels` | — | Nested split areas (recursive) |

**Layout order.** Panels are rendered in declaration order: the last panel in
the list becomes the outermost split, spanning the full available space on its
axis. This controls whether a bottom panel spans below the side panel or vice
versa.

### 5.5 Triggers

A trigger defines when and how a panel loads its content — typically in
response to a user action in another area.

```yaml
trigger:
  event: row_click       # event name fired by the source area
  from: content          # source area id (default: content)
  pass: [id, title]      # fields forwarded to the panel as $trigger.*
```

Inside the panel's view, forwarded fields are accessed as `$trigger.id`,
`$trigger.title`, etc.

Common events:

| Event | Fired when |
|-------|-----------|
| `row_click` | User clicks a row in a table view |
| `form_load` | A form finishes loading a record |
| `row_select` | User selects one or more rows |

### 5.6 Merge Behaviour for Pages

`pages` is a flat dict merged by key, consistent with `types` and `tables`.
Within each page, the smart-merge rules from Chapter 2 apply to all lists:

- `panels[]` — smart-merged by `id`

A derived plugin can insert, replace, or remove a panel without redefining
the entire page descriptor.

### 5.7 Auto-generated Pages

When the frontend requests a page not declared in any plugin, the backend
generates a descriptor automatically from the table schema:

```
book_list    ← explicit YAML
Book_list    ← auto-generated from table "Book"
Book_form    ← auto-generated from table "Book"
```

Resolution order in `get_page`:
1. Exact id match in the `pages` dict
2. Auto-generate from table schema

An auto-generated page is built from `effective_columns` and nothing else.
**Secret** columns are left out of both (§ 3.6); a form also skips **virtual**
ones, shows the key read-only, marks a column `required` when it is
`nullable: false` with no default, and forwards every other column attribute
to the client, which applies the ones it knows (widget, label, `foreign_key`).

Collections (§ 4.5) are never derived: a collection is not a column, and asking
which tables hold a foreign key to this one would pull `BookAuthor`, `Loan` and
`Review` into `Book` with equal right, while only the first belongs inside it.
An aggregate is declared, or it does not exist.

**Starting from the generated page (`$auto`).** An explicit page replaces the
generated one whole, which is right for a page that differs and heavy for one
that differs by a single key. `$auto: true` on an explicit page makes the
generated page its base: the page is merged on top of it (`deep_merge`, the
same rules as between plugins), so it names only what changes and the columns
keep following the schema.

```yaml
pages:
  author_list:
    $auto: true              # the generated author_list, plus:
    content:
      navigator:             # replaces the generated `navigator: true`
        commands:
          - {id: archive, label: Archive, scope: global, key: a, endpoint: archivable}
```

A key the page declares wins, a list included: `columns:` written here is the
whole column list, not an addition to the generated one. `$auto` on an id that
no table answers to is an error, both when the page is opened and in `check`
(`auto-missing`), since such a page has no `source` of its own for any other
check to notice.

### 5.8 Lookup Mode *(planned)*

The same page can behave either as a normal navigation target or as a record
picker, depending on the runtime context. When opened with
`$ctx.mode = "lookup"`, a table view returns the selected record to the caller
instead of navigating to a detail page.

```typescript
const result = await stack.pushLookup({ page: 'author_list' });
if (result) { formData.author_id = result.id; }
```

The backend first looks for a dedicated `author_lookup` page (with reduced
columns and pre-applied filters); if not found, it falls back to the standard
list page in lookup mode.

---

## 6. Data Source

### 6.1 Purpose

A `source` block defines where a view gets its data. All view types (table,
form, and future types) share the same source structure. The `type` of the
view determines how the data is rendered; the source determines where it
comes from.

### 6.2 Source Types

#### Model source (database)

The standard source for views backed by the application database. The
querybuilder runs server-side and returns only the requested records.

```yaml
source:
  model: Book                              # table declared in `tables`
  joins:
    - Publisher: "Book.publisher_id = Publisher.id"
    - via: BookAuthor                      # M2M via junction table
      where: [BookAuthor.book_id = $trigger.id]
  order_by: [title, -publication_date]    # prefix - = descending
  id: $props.id                           # for forms: which record to load
```

Server-side filtering (`where` clauses), sorting, and joins are available
only with model sources.

#### Endpoint source (virtual)

When data comes from a custom backend endpoint rather than a direct DB query.
The endpoint returns an array; the view displays it as-is.

```yaml
source:
  endpoint: compute_invoices_for_period
  params:
    period: $props.period
```

Client-side filtering (text search, column sort) is available. Server-side
`where` / `joins` are not applicable — the data shape is owned by the
endpoint.

This source type is used for computed or aggregated data that cannot be
expressed as a simple model query: cross-period reports, multi-step
calculations, data assembled from external APIs.

#### Inline source (static array)

For prototyping, testing, or simple configuration-driven views where the
data is passed directly via props.

```yaml
source:
  data: $props.items
```

All filtering is client-side.

### 6.3 Source Properties Reference

| Property | Source type | Description |
|----------|-------------|-------------|
| `model` | model | Table name as declared in `tables` |
| `id` | model | Record id for forms — `$props.id` or `$trigger.id` |
| `joins` | model | List of join definitions (simple or M2M) |
| `order_by` | model | Default sort — list of fields, prefix `-` for descending |
| `endpoint` | endpoint | Op name passed to `api.endpoint()` |
| `params` | endpoint | Parameters forwarded to the endpoint |
| `data` | inline | Array reference, typically `$props.items` |

### 6.4 Dynamic Values (`$props` and `$trigger`)

Source properties can reference runtime values using two prefixes:

| Prefix | Origin | Typical use |
|--------|--------|-------------|
| `$props` | Stack push — values passed when navigating to the page | `id`, `title`, date range |
| `$trigger` | Event forwarded by a trigger (§ 5.5) | `id` of the clicked row |

```yaml
source:
  model: Loan
  id: $trigger.id          # loaded when row_click fires from the parent view

source:
  endpoint: get_invoices
  params:
    period: $props.period   # passed when the page is pushed onto the stack
```

### 6.5 Joins (Model Source)

#### Simple join

```yaml
joins:
  - Publisher: "Book.publisher_id = Publisher.id"
  - LibraryUser: "Loan.library_user_id = LibraryUser.id"
```

Joined fields are referenced as `Model.field` in `columns` and `order_by`.

A column's values reach the grid under one key: the alias when there is one,
otherwise the field name without its table. `Publisher.name` therefore arrives
as `name`, and in a view that also shows the book's own `name` the two are one
column to the client — one of them shows the other's values. Give it an alias:

```yaml
columns:
  - field: name
  - field: Publisher.name as publisher_name
```

The alias is kept exactly as written (case included), and `check` reports two
columns of a view that share a key (`field-key-collision`).

A left join keeps the rows with nothing to join, which an inner join drops:
`- Publisher: {type: left, "on": "Book.publisher_id = Publisher.id"}`. Quote
`"on"`: YAML reads a bare `on` as the boolean true (`check` says so,
`key-not-string`).

#### Many-to-many join

```yaml
joins:
  - via: BookAuthor                      # junction table
    where: [BookAuthor.book_id = $trigger.id]
```

The junction table resolves both FK sides automatically.

### 6.6 Filtering

With a model source, filtering is handled server-side by the querybuilder.
The toolbar `search` and `filter` actions translate user input into `where`
clauses before the query is sent.

With endpoint and inline sources, the same toolbar actions operate on the
array already in memory — no additional server round-trip occurs. The
available filter operations are limited to what can be computed client-side
(text match, range, equality).

---

## 7. Table View

### 7.1 Purpose

A table view renders a list of records as an interactive data grid. It
supports sorting, searching, filtering, row selection, and per-row actions.

```yaml
views:

  book_list_view:
    type: table
    source:
      model: Book
      joins:
        - Publisher: "Book.publisher_id = Publisher.id"
      order_by: [title]
    columns:
      - field: id
      - field: title
      - field: Publisher.name
        title: Publisher
      - field: price
        align: right
        formatter: price_color
      - field: status
    actions:
      toolbar: [add, search, filter, select, export]
      row:
        - id: edit
          action: stack_push
          page: book_detail
          pass: [id, title]
        - id: delete
          action: endpoint
          op: db
          params: {table: Book, method: delete}
          confirm: true
```

### 7.2 Column Properties

| Property | Description |
|----------|-------------|
| `field` | Field name — identity key for merge. Use `Model.field` for joined fields |
| `title` | Column header (overrides type-level label) |
| `align` | Text alignment: `left`, `center`, `right` |
| `formatter` | Frontend formatter id for custom cell rendering |
| `width` | Column width in pixels |
| `sortable` | Whether the column is sortable (default: true) |

### 7.3 Actions

#### Toolbar actions

```yaml
actions:
  toolbar: [add, search, filter, select, export]
```

| Action | Description |
|--------|-------------|
| `add` | Opens a new-record form |
| `search` | Text search bar |
| `filter` | Advanced filter panel |
| `select` | Enables multi-row checkbox selection |
| `export` | Exports visible rows to CSV/Excel |

#### Row actions

```yaml
actions:
  row:
    - id: edit
      action: stack_push     # push a page onto the stack
      page: book_detail
      pass: [id, title]      # fields forwarded as $props

    - id: delete
      action: endpoint       # call a backend endpoint directly
      op: db
      params: {table: Book, method: delete}
      confirm: true          # show confirmation dialog

    - id: preview
      action: lookup         # open page in lookup/read-only mode
      page: book_detail
      pass: [id]
```

### 7.4 Merge Behaviour

`columns[]` is smart-merged by `field`. `actions.row[]` is smart-merged by
`id`. `actions.toolbar` is a scalar list (plain append, no removal).

A derived plugin can add, modify, or remove columns and row actions without
redefining the full view.

---

## 8. Form View

### 8.1 Purpose

A form view renders a single record for viewing or editing. It is the
standard detail/edit surface in a management application.

```yaml
views:

  book_edit_view:
    type: form
    source:
      model: Book
      id: $props.id          # null → new record; int → load existing
    fields:
      - name: title
      - name: isbn
      - name: publisher_id
        widget: combobox
      - name: price
      - group: Details
        fields:
          - name: language
          - name: pages
          - name: description
            widget: textarea
    policy:
      editable: true
    actions:
      toolbar: [save, cancel]
```

### 8.2 Field Properties

| Property | Description |
|----------|-------------|
| `name` | Field name — identity key for merge |
| `widget` | Widget override (`combobox`, `textarea`, `password`, `email`, …) |
| `label` | Label override (overrides type-level label) |
| `help` | Tooltip shown next to the field |
| `readonly` | Makes this field read-only regardless of form policy |
| `lookup` | Foreign keys: `page` to pick from ("Search more…", default `{table}_list`) and `filters`, the search's initial filter (`$record.x` from the draft) — applied in the drop-down, removable rules in the list |

#### Field groups

```yaml
fields:
  - name: title
  - group: Details          # identity key for merge is `group`
    fields:
      - name: language
      - name: pages
```

A `group` creates a collapsible section. Its nested `fields[]` list is
smart-merged by `name` — a derived plugin can add fields inside a group
without redefining it.

### 8.3 Policy

```yaml
policy:
  editable: false     # entire form is read-only (detail view)
```

When `editable: false` the form renders all fields as read-only and the
toolbar typically shows only navigation actions (no save/cancel).

### 8.4 Actions

Form toolbar actions follow the same structure as table toolbar actions.
Common form actions:

| Action | Description |
|--------|-------------|
| `save` | Save the current record |
| `cancel` | Discard changes and pop the stack |
| `delete` | Delete the record and pop the stack |

### 8.5 Merge Behaviour

`fields[]` is smart-merged by `name` (or `group` for group items). Nested
`fields[]` inside groups are also smart-merged recursively.

A derived plugin can add a field to a group, change a widget, or make a
field read-only — without redefining the form.

### 8.6 Collections

A collection node edits a set of child rows inside the parent's form. Rows are
held in the form's buffer and written with the parent, in one transaction
(`save_tree`): adding a line to a new invoice is one save, not two.

```yaml
layout:
  - type: tabs
    pages:
      - id: lines_tab
        label: Lines
        count: lines              # the row count beside the label
        layout:
          - type: collection
            id: lines             # key in the buffer and in the payload
            model: InvoiceLine    # the table written
            fk: invoice_id        # the child's column that points at the parent
            form: invoice_line_form   # the page that opens one row
            domain: []            # filter of the node
            defaults: {}          # stamped on rows created here
            prefill:              # initial values of a new row, from the parent
              currency: $record.currency
            view:
              type: table
              columns: [...]
```

| Property | Description |
|----------|-------------|
| `id` | Identity key, in the buffer and in the save payload |
| `model` | The child table. A fact of persistence: the view's `source.model` is filled from it |
| `fk` | The child's column that points at the parent. Always declared, never deduced |
| `form` | The row form; defaults to `{model}_form` |
| `domain` | Rows of the child table shown here, beyond the foreign key |
| `defaults` | Values stamped on rows created here — client and server |
| `prefill` | Initial values of a new row, `$record.x` read from the parent; client only |
| `view` | How the rows look: a table view, or a `$ref` to one |

**What `fk` holds.** By default the parent's primary key. When the child's
foreign key targets another column of the parent — a uuid the child cited before
any id existed, a code — that column is the link: rows are loaded by it, and a
row created here receives it. The parent's value exists after its flush, so a
new parent and its children are still one save.

```yaml
  Report:
    columns:
      - name: visit_code
        type: String
        length: 36
        foreign_key:
          target: Visit.code      # a unique column minted by `$uuid`
```

**`defaults` and `prefill` are different statements.**

- `defaults` is the other half of `domain`: what a node filters for, it stamps
  on creation, or the new row would vanish from the grid that made it. The
  stamped fields are hidden in the row form and written by the server as well.
- `prefill` is a suggestion: when a row is added, `$record.x` is read from the
  parent's **draft** — what is on screen, saved or not — and the fields stay
  visible and editable. A value the parent does not have yet is left out. The
  server receives plain values and does nothing with `prefill`.

When both name a field, `defaults` wins: a constraint is not overridden by a
suggestion. Other `$` tokens in `prefill` (`$op_date`) are resolved as in any
form default.

Whether the rows are **parts** of the parent — deleted with it — is a separate
question, answered by `owned` on the foreign key (§ 4.5).

---

## 9. Menu

The `menus` and `menu_items` sections declare the application's navigation.
Like every other section they are merged from all plugins (§ 1.5); the tree is
assembled on demand by the `get_menu` endpoint.

### 9.1 Content vs Layout

A **generic plugin does not know the menu structure** of the app it will end up
in — hardcoding it would break the agnostic core. It declares only its own
**surfaceable entries** (content). The **app plugin owns the structure**
(layout): loading last (it `depends_on` the others), its placement decisions win
the merge.

**Opt-in by declaration.** Declaring an entry *is* the opt-in — there is no
`surface: true` flag. A plugin of pure tables or services, with no user
destinations, contributes no entries and stays invisible in the menu.

### 9.2 Flat Items + `parent`

Menu entries are a **flat, mergeable collection**; each carries a `parent`
(and an `order`). The tree is computed from the `parent` links by `get_menu`.
The consequence: **moving an entry is overriding one attribute**, not
remove-and-re-add.

```yaml
# libapp/books/menu.yaml — a content plugin declares its entries
menu_items:
  books:   { label: Books,   icon: book, parent: catalog, order: 30,
             action: stack_push, panel: book_list }
  authors: { label: Authors, icon: user, parent: catalog, order: 10,
             action: stack_push, panel: author_list }
```

A **group** is an entry with no `action`, referenced by others as their
`parent`. Its id is a deliberate hook point: several plugins can converge on the
same `parent: catalog`. Convergence is intentional, not a collision.

```yaml
# app-layout plugin — owns the structure
menu_items:
  catalog: { label: Catalog, icon: book, order: 20 }   # group / hook point
```

To relocate an entry, a later plugin overrides a single attribute:

```yaml
menu_items:
  books: { parent: archive }   # deep-merged scalar, last writer wins → moved
```

### 9.3 Multiple Roots — Each With Its Own Rules

`menus` is not one global tree but a set of **named roots**, keyed by id. Each
root is an independent entry point with its own properties — a full back-office
menu, a restricted kiosk menu, an admin menu — selected at request time by
`get_menu(id)`:

```yaml
menus:
  main:       { label: "Main menu",     home_page: hello_demo }
  production: { label: "Production",     home_page: whoisdoingwhat }
  admin:      { label: "Administration" }
```

An entry belongs to a root via its `root` attribute (defaulted by the cascade,
§ 9.5). A root may declare a `home_page` (opened by default when the menu is
shown). Authorisation *(planned)* is an **orthogonal** filter applied over
whichever root is requested — "restrict the app for a different task" is simply
`get_menu('production')`, not a separate mechanism.

### 9.4 Entry Actions

A leaf entry carries an `action` — the **same vocabulary as view row-actions**
(§ 7.3): a scalar verb plus verb-specific sibling keys, never a nested object,
so diagnostics validate menu targets with the same generic checks.

| `action` | Sibling keys | Effect |
|----------|--------------|--------|
| `stack_push` | `panel`, `props` | open a page in the active tab's stack |
| `endpoint` | `op`, `params` | invoke a backend command |

### 9.5 Default Placement — Config Cascade

`root` and `parent` default through a cascade, so a plugin states them once
instead of on every entry:

```
entry (menu_items.<id>)  →  plugin config.yaml  →  app config.yaml
```

```yaml
# app config.yaml — global fallback
menu:
  default_root: main

# plugin config.yaml — "my entries default into the catalog group"
menu:
  default_parent: catalog
```

An explicit attribute on the entry wins over the plugin default, which wins over
the app default. This is not a second opt-in — only the resolution of the
position attributes.

### 9.6 `get_menu`

`get_menu(id)` composes the tree for one root, symmetric to `get_page`:

1. **Compose** — the merged `menus` / `menu_items` (done by the loader).
2. **Resolve defaults** — fill `root` / `parent` via the cascade (§ 9.5).
3. **Auth filter** *(planned)* — recursively drop entries whose `access:` the
   caller does not satisfy (server-side, non-bypassable).
4. **Build tree** — nest by `parent`, order by `order`.
5. **Resolve `$ref`** — expand entries reused across roots.

The client receives a composed, filtered, ordered tree and does not know how it
was built — the same contract as `get_page`. **Menu is semantic, not visual:**
`get_menu` returns groups, leaves, actions, `icon`, `order`; how it renders
(sidebar tree, dropdown menubar, command bar) is Chrome's decision, and the same
root can render differently in different Chromes without changing the descriptor.

**Fallback auto-menu** *(planned).* As `get_page` auto-generates `{table}_list` /
`{table}_form` for tables with no declared page, `get_menu` will fall back to an
**auto CRUD menu over all tables** when a root is empty or undeclared: you design
the tables and get a working menu immediately, then refine by declaring explicit
`menu_items` (explicit always wins — same resolution order as `get_page`). It is
a deterministic derivation of the git-tracked `tables`, never orphan-tables
sneaking into an already-declared menu.

---

## 10. Other View Types *(future)*

Beyond `table` (Chapter 7) and `form` (Chapter 8), further view types are
planned — **kanban**, **cards**, **calendar/gantt**. They share the same
`source` block (Chapter 6) and the same merge semantics; only the rendering and
a few type-specific properties differ. A view's `type` selects the renderer, so
adding a view type is additive — no change to the plugin model.

## 11. Localization and Help *(partly implemented)*

**Localization** is implemented at the framework level (per-locale string files,
a reactive `_()` on the client, request-scoped locale on the server); its
plugin-declaration surface (per-plugin locale files, translated labels/help) is
being consolidated. **Contextual help** is designed but not yet built: a
three-level progressive disclosure (`help_short` tooltip → `help_detail` popup →
full `help_url` page), sourced from per-element Markdown files, filtered by the
user's roles, and assembled into a whole-app manual that mirrors the menu tree.

## 12. Roles and Permissions *(future)*

Role-based access control is planned as an **orthogonal filter**, not a new
mechanism: menu entries and descriptors carry an `access:` hint filtered
server-side; row-level record rules reuse the existing **query behavior** pattern
(§ 3.7) — the same lever as `Archivable`, applied to visibility. Nothing in the
core hardcodes a permission model; it is policy layered on top.

## 13. Workflows *(future)*

State machines for `transaction`-category tables: a declared set of states and
transitions that gate which fields are editable and which actions are available,
with hooks for side effects on transition. Designed to sit on top of the model
and descriptor layers, not to alter them.
