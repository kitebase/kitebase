# Kitebase Testing and Flask Server

Under the `devtest` directory you will find useful scripts written to test
Kitebase library.

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
| `librarian` | Librarian | cataloguer and loans desk together, plus the reviews |
| `stats` | Statistics | Loan statistics, by book and by month; the loan list answers 403, it shows the members' names |

A database made before them gets them with `devtest.seed_staff`; a new one has
them from the seed. `kitebase check` lists what the roles leave uncovered.

## Testing

The `devtest.py` script, starts Kitebase and if needed regenerate the source
code for `model.py` and the database `devtest.sqlite`.

The perform some tests on the kitebase system.

### Usage

```bash
python devtest.py
```

## Flask Server

The `server_flask.py` script provides a Flask-based API server for interacting
with Kitebase.

Please read the main README.md file.

### Features

- RESTful API for Kitebase functionality
- Authentication with JWT tokens
- CRUD operations for all database models
- Dynamic query builder support

### Setup and Installation

#### Prerequisites

- Python 3.7+
- Required dependencies (see requirements.txt)

#### Installation

Read the main README.md file.

#### Configuration

The server is configured via `config.yaml`. Key sections include:

- Basic application info (name, version, etc.)
- Database connection settings
- Authentication configuration

### Usage

#### Starting the Server

```bash
python flask_server.py
```

The server will start on port 5000 by default.

### API Endpoints

#### Authentication

- `POST /api/auth/login`: Authenticate with username and password

#### Database Operations

- `GET /api/db/<table>`: Get all records from a table
- `GET /api/db/<table>/<id>`: Get a single record by ID
- `POST /api/db/<table>`: Create a new record
- `PUT /api/db/<table>/<id>`: Update a record
- `DELETE /api/db/<table>/<id>`: Delete a record

#### Query Builder

- `POST /api/query`: Execute a dynamic query

#### Generic Endpoint

- `POST /api/endpoint/<operation>`: Call any Kitebase operation
readmet user profile
- `GET /api/users/me`: Alias for profile

#### Testing with Jupyter Notebook

The included Jupyter notebook (`server-test.ipynb`) demonstrates how to interact with the API. It covers:

1. Authentication and token management
2. Basic CRUD operations
3. Advanced queries with the query builder
4. Using the generic endpoint
5. Error handling
6. Batch operations
7. Filtering and aggregations
8. Working with relationships

### Extending the Server

#### Adding New Endpoints

To add a new endpoint, update the Flask server (`flask_server.py`) with your new route.

#### Adding Kitebase Endpoints

To add new Kitebase endpoints, create a Python file with the `@endpoint` decorator and place it in your plugins directory.

