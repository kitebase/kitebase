import importlib
from pathlib import Path
from typing import List, Dict, Any, Optional

from kitebase.model_base import BaseApp


def autoimport(file: str, package: str) -> None:
    """
    Automatically import all modules in the same directory of the package.

    Args:
        file: The file path of the package's __init__.py
        package: The package name to import modules from
    """
    package_dir = Path(file).resolve().parent

    for file in package_dir.glob("*.py"):
        # __main__ is what `python -m kitebase` executes, not a module of the
        # package: importing it here would load it twice under two names.
        if file.name in ("__init__.py", "__main__.py"):
            continue
        module_name = file.stem
        module = importlib.import_module(f".{module_name}", package=package)
        globals()[module_name] = module


# The keys that name an item of a list, in order of priority: the merge refines
# the item with the same value instead of appending a second one.
IDENTITY_KEYS = ('id', 'name', 'field', 'group')


def identity_key(items: list) -> Optional[str]:
    """The key that names the items of a list, or None for a list of values or
    of mappings without one. The first mapping of the list decides."""
    for item in items:
        if isinstance(item, dict):
            return next((k for k in IDENTITY_KEYS if k in item), None)
    return None


def deep_merge(a: Dict[str, Any], b: Dict[str, Any], path: Optional[List[str]] = None) -> None:
    """
    Merge dictionary b into dictionary a recursively.

    Args:
        a: Target dictionary to merge into
        b: Source dictionary to merge from
        path: Current path in the recursive merge process, used for tracking nested keys
    """
    if path is None:
        path = []
    for key in b:
        if key in a:
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                deep_merge(a[key], b[key], path + [str(key)])
            elif a[key] == b[key]:
                pass
            else:
                a[key] = b[key]
        else:
            a[key] = b[key]


def get_app():
    """
    Get the application of this process.

    This function centralizes access to the application instance,
    making it easier to change the implementation if needed.

    Returns:
        The application instance
    """
    return BaseApp.__kitebase_app__


def table_definition(model_class, db_table=None):
    """
    The DbTable definition for a model class, when one is reachable.

    Args:
        model_class: SQLAlchemy model class
        db_table: Definition already at hand, returned as is

    Returns:
        DbTable instance, or None outside a loaded kitebase app
    """
    if db_table is not None:
        return db_table
    getter = getattr(model_class, 'get_table_definition', None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def secret_columns(db_table) -> frozenset:
    """
    Names of the columns a table never sends to a client (`secret: true`).

    Declared in YAML, usually through a type (commons' `Password`), so the
    column list stays the single place that says what a table holds. The table
    definition computes the set once; this only spares every caller the check
    for a table it could not resolve.
    """
    if db_table is None:
        return frozenset()
    return db_table.secret_columns


def search_info(db_table) -> dict:
    """
    What a text search on a table looks at — see DbTable.search_info.

    Returns the empty cascade outside a loaded kitebase app, so a caller working
    with plain models is told the table is not searchable rather than crashing.
    """
    if db_table is None:
        return {'display_field': None, 'search_fields': [], 'search_pk': None}
    return db_table.search_info


def seek(table_name, filters):
    """
    Generic seek function to find a record by filters.

    Args:
        table_name: Name of the table/model to query
        filters: Dictionary of field:value pairs for filtering

    Returns:
        First matching record or None if not found
    """
    app = get_app()
    model_class = app.find_model_class(table_name)
    if not model_class:
        raise ValueError(f"Table '{table_name}' not found")

    with app.get_session() as session:
        query = session.query(model_class)

        # Apply filters
        for field, value in filters.items():
            if hasattr(model_class, field):
                query = query.filter(getattr(model_class, field) == value)
            else:
                raise ValueError(f"Field '{field}' not found in table '{table_name}'")

        # Return first match or None
        return query.first()
