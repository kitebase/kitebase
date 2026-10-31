import datetime
import importlib
from pathlib import Path
from typing import List, Dict, Any, Optional, Union
from sqlalchemy import inspection


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
    Get the current DB application instance.

    This function centralizes access to the application instance,
    making it easier to change the implementation if needed.

    Returns:
        The current DB application instance
    """
    # Import here to avoid circular dependency
    from kitebase.db import Base
    return Base.__kitebase_app__


def resolve_table_name(model_name: str, base_table_name: str) -> str:
    """
    Resolve table name dynamically based on current context.

    Supports multi-tenancy with tenant prefixes (e.g., 'data_orders', 'test_customers').
    If multi-tenancy is not configured, returns the base table name.

    This function works both at import time (returns base table name) and at
    runtime (returns tenant-prefixed name if applicable).

    Args:
        model_name: The model class name (e.g., 'User', 'Order')
        base_table_name: The base table name (e.g., 'users', 'orders')

    Returns:
        Actual table name with tenant prefix if applicable

    Example:
        >>> # Standard mode (no multi-tenancy)
        >>> resolve_table_name('User', 'users')
        'users'

        >>> # Multi-tenant mode with context
        >>> BaseApp.set_context({'tenant_prefix': 'data'})
        >>> resolve_table_name('Order', 'orders')
        'data_orders'
    """
    try:
        # Try to get app instance (works at runtime)
        app = get_app()
        if app and hasattr(app, 'tables') and app.tables:
            # Import here to avoid circular dependency
            from kitebase.db import BaseApp
            context = BaseApp.get_context()
            result = app.get_table_name(model_name, context)
            if result:
                return result
    except Exception:
        # App not initialized yet (during import)
        pass

    # Fallback to base table name
    return base_table_name


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


def serialize_model(model, include_relationships=False, db_table=None):
    """
    Convert SQLAlchemy model instance to dictionary.

    Columns marked `secret: true` are left out: they must never reach a client,
    and the definition is resolved from the model itself when the caller has
    none at hand, so forgetting to pass one cannot turn into a leak. So are the
    columns of a group the current user may not read (kitebase.access).

    Args:
        model: SQLAlchemy model instance
        include_relationships: Whether to include relationship attributes
        db_table: DbTable instance to include virtual columns (optional)

    Returns:
        Dictionary representation of the model
    """
    from kitebase import access
    db_table = table_definition(model.__class__, db_table)
    secrets = secret_columns(db_table)
    if db_table is not None:
        secrets = secrets | access.unreadable_columns(db_table)

    result = {}
    # Add real + mixin columns (SQLAlchemy introspection covers both)
    for column in inspection.inspect(model.__class__).columns:
        if column.name in secrets:
            continue
        result[column.name] = getattr(model, column.name)

    # Add virtual columns (hybrid_property, not in __table__.columns)
    if db_table:
        for col in db_table.virtual_columns:
            if col.name in secrets:
                continue
            result[col.name] = getattr(model, col.name, None)

    # Optionally add relationships
    if include_relationships:
        for relationship in inspection.inspect(model.__class__).relationships:
            rel_name = relationship.key
            rel_value = getattr(model, rel_name)

            # Handle different types of relationships
            if rel_value is None:
                result[rel_name] = None
            elif isinstance(rel_value, list):
                # Many relationship - serialize IDs only
                result[rel_name] = [item.id if hasattr(item, 'id') else str(item) for item in rel_value]
            else:
                # Single relationship - serialize ID only
                result[rel_name] = rel_value.id if hasattr(rel_value, 'id') else str(rel_value)

    return result


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


def json_to_model_types(data, table_name):
    """
    Convert JSON data types to appropriate SQLAlchemy model types,
    with special handling based on database type.

    Args:
        data: Dictionary of values from JSON
        table_name: Name of the table/model

    Returns:
        Dictionary with values converted to appropriate Python types
    """

    app = get_app()
    model_class = app.find_model_class(table_name)
    if not data or not model_class or not hasattr(model_class, '__table__'):
        return data

    # Skip conversion for databases that handle JSON conversion well
    if app.db_type.lower() in ('postgresql', 'mysql', 'mariadb'):
        return data

    result = data.copy()

    for column in model_class.__table__.columns:
        column_name = column.name

        # Skip if column not in data
        if column_name not in result:
            continue

        value = result[column_name]

        # Skip None values
        if value is None:
            continue

        # Get Python type for the column
        try:
            python_type = column.type.python_type
        except NotImplementedError:
            # Some SQLAlchemy types don't have a direct Python type
            continue

        # Convert based on target type
        if python_type == datetime.date and isinstance(value, str):
            try:
                result[column_name] = datetime.date.fromisoformat(value)
            except ValueError:
                pass
        elif python_type == datetime.datetime and isinstance(value, str):
            try:
                result[column_name] = datetime.datetime.fromisoformat(value)
            except ValueError:
                pass

    return result


def register_standard_handlers(pm) -> None:
    """
    Register standard merge handlers for common data patterns.

    This function sets up handlers for merging lists that contain items
    with a 'name' field, such as table columns or type columns.

    Args:
        pm: PluginsManager instance
    """

    def merge_by_name(base_list: List[Dict[str, Any]],
                      new_list: List[Dict[str, Any]],
                      plugin) -> List[Dict[str, Any]]:
        """
        Merge two lists of dictionaries by their 'name' field.

        When items with the same 'name' are found, the attributes from
        the new item are merged into the existing item, with new values
        overwriting old ones.

        Args:
            base_list: Base list to merge into
            new_list: New list to merge from
            plugin: Plugin providing the new items

        Returns:
            Merged list with combined items
        """
        result = []

        # Copy base list items
        for item in base_list:
            result.append(item.copy())

        # Process new list items
        for new_item in new_list:
            name = new_item.get('name')

            if not name:
                # If no name, just append
                new_copy = new_item.copy()
                if '$plugin' not in new_copy:
                    new_copy['$plugin'] = plugin
                result.append(new_copy)
                continue

            # Find existing item with same name
            existing = None
            for item in result:
                if item.get('name') == name:
                    existing = item
                    break

            if existing:
                # Merge attributes (new values overwrite old ones)
                deep_merge(existing, new_item)
                # Update plugin marker
                existing['$plugin'] = plugin
            else:
                # Add new item
                new_copy = new_item.copy()
                if '$plugin' not in new_copy:
                    new_copy['$plugin'] = plugin
                result.append(new_copy)

        return result

    # Register handlers for common patterns
    pm.register_merge_handler('tables.*.columns', merge_by_name)
    pm.register_merge_handler('types.*.columns', merge_by_name)
