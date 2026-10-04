# Kitebase

**Kitebase** is a plugin-based and data-driven framework designed to generate SQLAlchemy model source code and provide server-side infrastructure for database applications. It bridges the gap between database schema definition and application development with a flexible, extensible architecture.

## Status

**BETA SOFTWARE**: While functional for testing and development, Kitebase is still under active development. API changes may occur, and comprehensive documentation is in progress.

## Key Features

- **Plugin Architecture**: Enables collaborative development where contributors can work independently following established patterns without conflicting with each other's code.

- **Data-Driven Design**: Database schemas are defined in YAML files, combining both technical specifications and semantic information. This approach separates structure from implementation while maintaining a single source of truth.

- **Rich Metadata**: Beyond basic database schema, plugins can define UI components, validation rules, menu structures, and other application-level concerns that drive both server and client behavior.

- **Schema Agnosticism**: Applications are constructed entirely from plugins. There are no required tables or mandatory configurations, giving you complete flexibility in defining your data model.

- **REST API Infrastructure**: The built-in command processor and endpoint system provide a standardized way to expose functionality via REST, with support for JWT authentication and context-based permissions.

- **Advanced Querying**: The querybuilder component provides a JSON-based query language that can be used from client applications to construct complex SQL queries safely.

## Installation

**New here?** [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) walks the whole
road from an empty machine: the prerequisites, an application that runs, the
three repositories, the client in both its forms, and how the setup is verified.
The rest of this section is the short answer for those who only need the library.

### Prerequisites

- Python 3.11 or higher — `uv` will install one if you have none
- git: dependencies come from repositories, there is no index to publish to yet

### As a dependency

Kitebase is installed, not copied. An application declares it and pins a
version, so what is running is something the application states rather than
whatever happened to be on the machine:

```bash
uv add "kitebase[flask] @ git+https://github.com/kitebase/kitebase@v0.5.0"
# or: pip install "kitebase[flask] @ git+https://github.com/kitebase/kitebase@v0.5.0"
```

The web framework is an extra — `[flask]` or `[fastapi]` — because kitebase
imports neither at module level: you install the one you serve with.

Each application gets its own virtual environment. Starting one from nothing:

```bash
kitebase new myapp      # config.yaml, a plugin, the entry points
cd myapp && uv sync
python app.py db-sync  # create the database from the YAML schema
python server.py       # http://localhost:8300 — admin/admin
```

### For working on kitebase itself

Clone it and install in editable mode, so the sources stay live:

```bash
git clone https://github.com/kitebase/kitebase.git
cd kitebase
uv venv && uv pip install -e ".[dev]"    # or: python -m venv .venv; pip install -e ".[dev]"
pytest
```

A workstation that also builds clients and consumes the shared plugins needs the
other two repositories: see
[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) § 2.

## Usage

### Development Testing

The `devtest` directory contains examples to help you understand the framework:

1. Run the basic development test:
   ```bash
   cd devtest
   python devtest.py
   ```
   this will generate the `model.py` SQLAlchemy model and the `devtest.sqlite` database with some data

2. Start either server — the same four routes, two frameworks:
   ```bash
   python server_flask.py       # or: python server_fastapi.py
   ```

3. Open the Jupyter notebook to test API functionality:
   ```bash
   jupyter-lab server-test.ipynb
   ```

4. The querybuilder component is standalone and can work outside of
   the kitebase package. You can test it with:
   ```bash
   cd querybuilder
   python query_examples.py
   ```

### Building Your App

```bash
kitebase new myapp && cd myapp
uv sync
uv run app.py db-sync        # create the database from the YAML schema
uv run server_flask.py       # http://localhost:8300 — admin/admin
```

The schema goes in `plugins/<name>/model.yaml`, the domain operations in
`plugins/<name>/*.py` as `@endpoint`. Step by step, with the client and the
shared plugins: [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

## Architecture

Kitebase consists of several key components:

- **Plugin Manager**: Loads and organizes plugin modules
- **DB Engine**: Manages SQLAlchemy models and database interactions
- **Command Processor**: Routes requests to the appropriate endpoint functions
- **Source Generator**: Creates SQLAlchemy model code from YAML definitions
- **Querybuilder**: Translates JSON query specifications to SQLAlchemy queries
- **Flask Server**: Provides REST API access to the system

## Extending Kitebase

The system is designed to be extended through plugins. Each plugin can contain:

- YAML files defining data models
- Python modules with custom business logic
- Endpoint definitions for API access
- UI component specifications

## Web Framework Support

Currently, Kitebase includes a Flask server integration. Future versions may support Django, FastAPI, and other Python web frameworks.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
