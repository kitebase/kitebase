# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-10-05

The first kitebase release: everything since 0.5.0, renamed. Server and client
(`kitebase-ui`) carry the same tag, and the tag names the pair tested together.

### Changed
- Renamed from coframe to kitebase: the package (`import kitebase`), the CLI,
  the default route prefix (`/kitebase/...`), the environment variables
  (`KITEBASE_*`) and the repositories (`github.com/kitebase/*`). Tags up to
  v0.5.0 are coframe releases.
- Installable from git: `pyproject.toml` at the repository root, extras
  `[flask]` and `[fastapi]`, neither framework the default; `uv.lock` tracked.
- The library logs and never installs a handler: the application decides what
  to listen to.
- Generated columns are nullable unless declared otherwise.

### Added
- **Plugins**: several plugin roots, each with an `include:` selection and its
  dependency closure; smart merge of lists (`$remove`, `$after`, `$before`,
  `$replace`) with golden tests of the merged tree; `$plugin` metadata.
- **UI descriptors**: `get_page` (pages generated from the schema, or written
  starting from them with `$auto`), `get_menu`, `get_type_schema`,
  `get_server_config`; collections and aggregates read and saved as one tree;
  `owned` rows; field attributes taken from the model.
- **Schema**: virtual columns and `effective_columns`, soft foreign keys,
  many-to-many and self-referencing relationships, junction tables, composite
  types, `case`, `secret`, `query_rank`, column validation with field errors,
  `timezone:` checked at startup, system defaults such as `$op_date`.
- **Queries**: `$and`, filtering through relationships, text search over the
  declared columns, date and time filters by precision, query behaviors
  (`add_query_behavior`) such as the commons' Archivable.
- **Database**: `db-check`, `db-sync` (never loses data without `--force`),
  `db-backup` while the server runs.
- **Hosting**: routes registered on an app or a Blueprint/APIRouter mounted by
  the host, one login shared with the host's session (`auth/token`,
  `host_session`), the built client served from `clientui/` where
  `client.role` says.
- **CLI**: `kitebase new`, `kitebase dev`, `kitebase build-client`, `check` with
  diagnostics and dumps; every other command is handed to the application's
  `kite.py`, including the commands the application adds.
- i18n of the server's messages, `MemorySet` for schema-aware data in memory,
  multi-tenancy groundwork, JWT renewal.

### Fixed
- The session context is restored even when there was none; a worker no longer
  returns to the pool with an identity.
- `read_file` is closed unless `allowed_dirs` names a directory.
- A filter that cannot be read is refused, not dropped; a paginated query has a
  total order.

## [0.5.0] - 2026-02-11
### Added
- FastAPI support: async endpoints next to the sync ones, with a dual-mode
  context that works under both Flask and FastAPI; a FastAPI server example in
  devtest.
- `read_file` endpoint.
- Authentication extended, with `update_context`.
### Changed
- Package restructured; `PluginsManager` merges lists through handlers.

## [0.4.0] - 2025-03-31
### Added
- Querybuilder from endpoint
- Authentication endpoint
- Utility functions
- Context information for database sessions
- Flask server to interact with Coframe
- Jupyter notebook for API testing

## [0.3.2] - 2025-03-28
### Added
- CRUD database endpoints
- Querybuilder integration
- Standalone querybuilder demo

## [0.3.0] - 2025-03-15
### Added
- Command processor support with endpoint decorators
- Centralized SQLAlchemy interaction

## [0.2.0] - 2025-03-07
### Added
- Many-to-many relationship support
- Multiple inheritance for model classes from plugin source code

## [0.1.1] - 2025-01-27
### Added
- Refactored YAML schema
- Rewritten parser according to YAML schema
- Cleaned up source with type annotations
- Rewritten source generator (missing m2m relationship and multiple inheritance)

## [0.1.0] - 2025-01-27
### Added
- First but incomplete model.py source generator
