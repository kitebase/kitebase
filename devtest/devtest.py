import sys
from datetime import datetime, timedelta
from pathlib import Path
sys.path.append("..")
import kitebase  # noqa: E402
import kitebase.plugins  # noqa: E402
from kitebase.endpoints import endpoint  # noqa: E402


@endpoint('add')
def add_numbers(data):
    a = data.get("a", 0)
    b = data.get("b", 0)

    # the context manager is working
    print(kitebase.db.BaseApp.get_context())
    return {
        "status": "success",
        "data": a + b
    }


def inspect_panels(plugins):
    """
    Inspect the merged panels/views data from plugins.
    Set a breakpoint here to explore plugins.data in the debugger.
    """
    import json

    panels = plugins.data.get('panels', {})
    views = plugins.data.get('views', {})

    print(f"\n=== PANELS ({len(panels)}) ===")
    for panel_id, panel in panels.items():
        if panel_id.startswith('$'):
            continue
        content_ref = panel.get('content', {}).get('ref', '(inline)')
        split_areas = [p['id'] for p in panel.get('panels', [])]
        print(f"  [{panel_id}]  content={content_ref}  splits={split_areas}")

    print(f"\n=== VIEWS ({len(views)}) ===")
    for view_id, view in views.items():
        if view_id.startswith('$'):
            continue
        vtype = view.get('type', '?')
        model = view.get('source', {}).get('model', '?')
        n_cols = len(view.get('columns', view.get('fields', [])))
        print(f"  [{view_id}]  type={vtype}  model={model}  fields/cols={n_cols}")

    # Simulate get_panel: use plugins.get() + resolve_refs()
    panel_id = 'book_list'
    panel = plugins.get(f"panels.{panel_id}")
    if panel:
        resolved = plugins.resolve_refs(panel)
        print(f"\n=== get_panel('{panel_id}') — refs resolved ===")
        print(json.dumps(resolved, indent=2, default=str))


# ── Shared app setup ───────────────────────────────────────────────────────────

def setup_schema():
    """
    Load plugins and compute DB schema — no DB engine, no model.py generation.
    Sufficient for introspection commands (dump-page, dump-tables, …).
    Returns the initialized app.
    """
    plugins = kitebase.plugins.PluginsManager()
    plugins.load_config("config.yaml")
    kitebase.utils.register_standard_handlers(plugins)
    plugins.load_plugins()

    app = kitebase.utils.get_app()
    app.calc_db(plugins)

    from common.model import Archivable
    app.add_query_behavior(Archivable)

    return app


def setup(generate: bool = True):
    """
    Load plugins, compute the schema, (re)generate model.py.

    The whole application up to the point where a database engine is needed.
    The servers and this harness both start from here, so what they run is the
    same application — and importing this module registers the endpoints
    declared in it.
    """
    app = setup_schema()

    if generate:
        model_file = "model.py"
        if app.pm.should_regenerate(model_file):
            print("Generating model.py ...")
            kitebase.source.Generator(app).generate(filename=model_file)
        else:
            print("model.py up to date.")

    return app, app.pm


# ── Main (existing test suite) ─────────────────────────────────────────────────

def main():

    app, plugins = setup()

    inspect_panels(plugins)

    print(plugins.export_pythonpath())

    import library.test as library_test  # type: ignore
    library_test.ok()

    # open db engine and populate empty db
    import model  # type: ignore
    app.initialize_db(plugins.config["db_engine"], model)
    plugins.load_all_locales()
    seed(app, model)

    # a query
    with app.get_session() as session:
        books = session.query(model.Book).all()
        for book in books:
            authors_names = [author.full_name for author in book.authors]
            print(f"- {book.title} by {', '.join(authors_names)}")

    # register extra endpoints
    cp = app.cp
    cp.resolve_endpoints(["devtest.py"])

    # test some endpoints
    command = {
        "operation": "add",
        "parameters": {"a": 5, "b": 3},
        "context": {"user": "me"}
    }
    result = cp.send(command)
    print(result)
    # the context manager works within the command thread
    print(kitebase.db.BaseApp.get_context())

    command = {
        "operation": "sayhello",
        "parameters": {"name": "Claudio", "lang": "en"},
        "timeout": 5
    }
    result = cp.send(command)
    print(result)

    # interact to db using endpoint
    command = {
        "operation": "books",
    }
    result = cp.send(command)
    print(result)

    command = {
        "operation": "auth",
        "parameters": {
            "username": "mrossi",
            "password": "hashed_password_here"},
    }
    result = cp.send(command)
    print(result)

    # a dynamic query
    q = {
        "from": "Book",
        "select": [
            "id",
            "title",
            "isbn",
            "publication_date",
            "Author.id as author_id",
            "Author.first_name",
            "Author.last_name",
            "Author.nationality"
        ],
        "joins": [
            {"BookAuthor": "BookAuthor.book_id = Book.id"},
            {"Author": "Author.id = BookAuthor.author_id"}
        ],
        "order_by": ["Book.title", "Author.last_name", "Author.first_name"]
    }
    command = {
        "operation": "query",
        'parameters': {
            "format": "tuples",
            "query": q
        }
    }
    result = cp.send(command)
    print(result)

    # using standard endpoints get
    command = {
        "operation": "db",
        "parameters": {
            "table": "Book",
            "method": "get",
            "start": 0,
            "limit": 10
        }
    }
    result = cp.send(command)
    print(result)

    # using standard endpoints get 1
    command = {
        "operation": "db",
        "parameters": {
            "table": "Book",
            "method": "get",
            "id": 1,
            "start": 0,
            "limit": 10
        }
    }
    result = cp.send(command)
    print(result)

    # using standard endpoints create
    command = {
        "operation": "db",
        "parameters": {
            "table": "Book",
            "method": "create",
            "data": {
                "isbn": "9788806219451",
                "title": "Le città invisibili",
                "publication_date": datetime(1972, 1, 1),
                "price": 14.90,
                "status": "A"
            }
        }
    }
    result = cp.send(command)
    print(result)
    print(kitebase.db.BaseApp.get_context())

    # get id from just created book
    book_id = result['data']['id']

    # using standard endpoints update
    command = {
        "operation": "db",
        "parameters": {
            "table": "Book",
            "method": "update",
            "id": book_id,
            "data": {
                "price": 16.50
            }
        }
    }
    result = cp.send(command)
    print(result)

    # using standard endpoints delete
    command = {
        "operation": "db",
        "parameters": {
            "table": "Book",
            "method": "delete",
            "id": book_id
        }
    }
    result = cp.send(command)
    print(result)


def seed(app, model=None):
    """
    Fill an empty database with the data the tests and the UI work on.

    Development data, not a mechanism: it runs only while the tables are empty,
    so whatever is entered from the UI is left alone. Dev credentials are
    admin/admin.

    Passwords are seeded in plaintext on purpose. Writing straight through
    SQLAlchemy skips the `on_write` transform of the endpoints, which leaves
    devtest in the state a database predating hashing is in — the one case the
    login path would otherwise never exercise here, and what the change-password
    flow will be tested against once it exists.
    """
    model = model or app.model

    with app.get_session() as session:
        if session.query(model.Book).first():
            return

        print("regenerate test data...")
        try:
            author1 = model.Author(
                first_name="Italo",
                last_name="Calvino",
                birth_date=datetime(1923, 10, 15),
                nationality="Italian"
            )
            author2 = model.Author(
                first_name="Umberto",
                last_name="Eco",
                birth_date=datetime(1932, 1, 5),
                nationality="Italian"
            )
            session.add_all([author1, author2])
            session.flush()

            book1 = model.Book(
                isbn="9788806219450",
                title="Il barone rampante",
                publication_date=datetime(1957, 1, 1),
                price=15.90,
                status="A"
            )
            book2 = model.Book(
                isbn="9788845274930",
                title="Il nome della rosa",
                publication_date=datetime(1980, 1, 1),
                price=18.50,
                status="A"
            )
            session.add_all([book1, book2])
            session.flush()

            book_author1 = model.BookAuthor(
                book_id=book1.id,
                author_id=author1.id,
                notes="Masterpiece"
            )
            session.add_all([book_author1])
            book_author2 = model.BookAuthor(
                book_id=book2.id,
                author_id=author2.id,
                notes="International bestseller"
            )
            session.add_all([book_author1, book_author2])

            # Create admin user for system
            admin_user = model.User(
                name="Admin User",
                email="admin@example.com",
                username="admin",
                password="admin",
                is_admin=True
            )
            session.add(admin_user)

            # Create library user for library operations
            library_user = model.LibraryUser(
                name="Mario Rossi",
                email="mario.rossi@library.com",
                username="mrossi",
                password="hashed_password_here",
                is_student=False
            )
            session.add(library_user)
            session.flush()

            loan1 = model.Loan(
                book_id=book1.id,
                library_user_id=library_user.id,
                borrowed_at=datetime.now(),
                due_date=datetime.now() + timedelta(days=30)
            )
            review1 = model.Review(
                book_id=book1.id,
                library_user_id=library_user.id,
                rating=5,
                comment="An italian masterpiece!"
            )
            session.add_all([loan1, review1])

            session.commit()

        except Exception as e:
            print(f"Error during tests: {e}")
            session.rollback()


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from kitebase.cli import DB_COMMANDS, make_parser, run_cli

    args = make_parser().parse_args()

    if args.command in DB_COMMANDS:
        # These look at the database as it actually is: no create_all, no
        # startup check — reporting the difference is the whole point.
        app, plugins = setup()
        import model  # type: ignore  # generated by setup()

        app.initialize_db(plugins.config["db_engine"], model,
                          create_all=False, check_schema=False)
        run_cli(app, args, output_dir=Path('data'))
    elif args.command:
        run_cli(setup_schema(), args, output_dir=Path('data'))
    else:
        main()
