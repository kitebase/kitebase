# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - 0.6.0
### Changed
- Renamed from coframe to kitebase: the package (`import kitebase`), the
  default route prefix (`/kitebase/...`), the environment variables
  (`KITEBASE_*`) and the repository (`github.com/kitebase/kitebase`). Tags up
  to v0.5.0 are coframe releases.

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
