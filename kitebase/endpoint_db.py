import kitebase
import kitebase.server_utils as server_utils
import kitebase.transforms
from kitebase.endpoints import endpoint
from kitebase.querybuilder import DynamicQueryBuilder
from kitebase.i18n import _, _f
from typing import Dict, Any, Optional
from sqlalchemy import and_, or_, desc, asc
from sqlalchemy import Date, DateTime, Integer, inspect as sa_inspect
import datetime


@endpoint('db')
def db_operations(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic database CRUD endpoint.

    Parameters:
        - table: Name of the table/model to operate on
        - method: Operation to perform (get, create, update, delete)
        - id: Optional ID for single record operations
        - data: Data for create/update operations
        - query: Query parameters for filtering (dict with field:value pairs)
        - start: Pagination start index (default: 0)
        - limit: Pagination limit (default: 100)
        - order_by: Field to order by
        - order_dir: Direction of ordering ('asc' or 'desc')

    Returns:
        Dictionary with operation results
    """
    try:
        # Validate required parameters
        table_name = data.get('table')
        method = data.get('method', 'get').lower()

        if not table_name:
            return {"status": "error", "message": _('Table name is required'), "code": 400}

        # Get database app and model class
        app = kitebase.utils.get_app()
        model_class = app.find_model_class(table_name)

        if not model_class:
            return {"status": "error", "message": _f("Table '{name}' not found", name=table_name), "code": 404}

        db_table = app.tables.get(table_name)

        # Execute the requested method
        if method == 'get':
            return handle_get(app, model_class, data, db_table)
        elif method == 'create':
            return handle_create(app, model_class, data, db_table)
        elif method == 'update':
            return handle_update(app, model_class, data, db_table)
        elif method == 'delete':
            return handle_delete(app, model_class, data, db_table)
        else:
            return {"status": "error", "message": _f("Unsupported method: '{method}'", method=method), "code": 400}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e), "code": 500}


def pk_field(db_table) -> str:
    """Return the first PK field name for a table, defaulting to 'id'."""
    if db_table is None:
        return 'id'
    pk_cols = [col.name for col in db_table.effective_columns if col.attributes.get('primary_key')]
    return pk_cols[0] if pk_cols else 'id'


def write_values(db_table, record_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prepare incoming values for storage, following what the columns declare.

    Three rules, all from `kitebase.transforms`:
    - a `secret` column arriving empty is dropped — it is never read back, so a
      client cannot echo it, and an empty value means "leave it alone" rather
      than "clear it";
    - a column with `validate` gets its value checked against that rule, and
      every value that fails is reported, by field, before anything is written;
    - a column with `on_write` gets its value passed through that transform, so
      what reaches the database is the stored form (a hash, a canonical code).

    Raises:
        ValidationError: the values that break their rule, as {field: message}
        ValueError: if a column names a validator or a transform nobody registered
    """
    if db_table is None:
        return dict(record_data)

    attributes = {col.name: col.attributes for col in db_table.effective_columns}
    result = {}
    errors = {}
    for key, value in record_data.items():
        attrs = attributes.get(key, {})
        is_empty = value is None or value == ''

        if attrs.get('secret') and is_empty:
            continue

        validator_name = attrs.get('validate')
        if validator_name and not is_empty:
            validator = kitebase.transforms.get_validator(validator_name)
            if validator is None:
                raise ValueError(
                    f"Column '{key}' names an unknown validator: '{validator_name}'")
            problem = validator(value, record_data)
            if problem:
                errors[key] = problem
                continue

        transform_name = attrs.get('on_write')
        if transform_name and not is_empty:
            transform = kitebase.transforms.get_write_transform(transform_name)
            if transform is None:
                raise ValueError(
                    f"Column '{key}' names an unknown write transform: '{transform_name}'")
            value = transform(value)

        result[key] = value

    if errors:
        raise kitebase.transforms.ValidationError(errors)

    return result


def handle_get(app, model_class, params: Dict[str, Any], db_table=None) -> Dict[str, Any]:
    """Handle GET operations (list or single record)"""
    record_id = params.get(pk_field(db_table))
    start = int(params.get('start', 0))
    limit = int(params.get('limit', 100))
    query_filters = params.get('query', {})
    order_by = params.get('order_by')
    order_dir = params.get('order_dir', 'asc')

    with app.get_session() as session:
        if record_id:
            # Get single record
            record = session.get(model_class, record_id)
            if not record:
                return {"status": "error", "message": _f('Record with id {id} not found', id=record_id), "code": 404}

            return {
                "status": "success",
                "data": kitebase.utils.serialize_model(record, db_table=db_table),
                "code": 200
            }
        else:
            # List records with filtering and pagination
            query = session.query(model_class)

            # Apply filters
            if query_filters:
                filter_conditions = build_filters(model_class, query_filters)
                # `is not None`: a SQLAlchemy clause has no defined truth value
                # (bool() raises), so a plain `if filter_conditions:` would crash.
                if filter_conditions is not None:
                    query = query.filter(filter_conditions)

            # Apply ordering
            if order_by:
                column = getattr(model_class, order_by, None)
                if column:
                    if order_dir.lower() == 'desc':
                        query = query.order_by(desc(column))
                    else:
                        query = query.order_by(asc(column))

            # Get total count (before pagination)
            total_count = query.count()

            # Apply pagination
            query = query.offset(start).limit(limit)

            # Execute query and serialize results
            records = query.all()
            result = [kitebase.utils.serialize_model(record, db_table=db_table) for record in records]

            return {
                "status": "success",
                "data": {
                    "records": result,
                    "total": total_count,
                    "start": start,
                    "limit": limit
                },
                "code": 200
            }


def coerce_value(model_class, key: str, value: Any) -> Any:
    """Coerce string values to the Python type expected by the SQLAlchemy column."""
    if not isinstance(value, str) or value == '':
        return value
    try:
        mapper = sa_inspect(model_class)
        col = mapper.columns.get(key)
        if col is None:
            return value
        col_type = col.type
        if isinstance(col_type, DateTime):
            return datetime.datetime.fromisoformat(value)
        if isinstance(col_type, Date):
            return datetime.date.fromisoformat(value)
        if isinstance(col_type, Integer):
            return int(value)
    except Exception:
        pass
    return value


def _invalid(error) -> Dict[str, Any]:
    """A write refused by a column's rule: the errors go back by field."""
    return {"status": "error", "code": 400, "message": _('Some values are not valid'),
            "data": {"errors": error.errors}}


def handle_create(app, model_class, params: Dict[str, Any], db_table=None) -> Dict[str, Any]:
    """Handle CREATE operations"""
    record_data = params.get('data')

    if not record_data:
        return {"status": "error", "message": _('No data provided for creation'), "code": 400}

    # Create new instance
    try:
        record_data = write_values(db_table, record_data)
        coerced = {k: coerce_value(model_class, k, v) for k, v in record_data.items()}
        new_record = model_class(**coerced)

        with app.get_session() as session:
            session.add(new_record)
            session.commit()

            # Refresh to get generated IDs and other database defaults
            session.refresh(new_record)

            return {
                "status": "success",
                "data": kitebase.utils.serialize_model(new_record, db_table=db_table),
                "message": _('Record created successfully'),
                "code": 201
            }
    except kitebase.transforms.ValidationError as e:
        return _invalid(e)
    except Exception as e:
        return {"status": "error", "message": f"Creation failed: {str(e)}", "code": 400}


def handle_update(app, model_class, params: Dict[str, Any], db_table=None) -> Dict[str, Any]:
    """Handle UPDATE operations"""
    record_id = params.get(pk_field(db_table))
    if not record_id:
        return {"status": "error", "message": _('Record ID is required for updates'), "code": 400}

    record_data = params.get('data')
    if not record_data:
        return {"status": "error", "message": _('No data provided for update'), "code": 400}

    try:
        record_data = write_values(db_table, record_data)
    except kitebase.transforms.ValidationError as e:
        return _invalid(e)

    with app.get_session() as session:
        # Find the record
        record = session.get(model_class, record_id)
        if not record:
            return {"status": "error", "message": f"Record with id {record_id} not found", "code": 404}

        # Update record attributes (skip read-only hybrid/virtual properties)
        for key, value in record_data.items():
            if hasattr(record, key):
                try:
                    setattr(record, key, coerce_value(model_class, key, value))
                except AttributeError:
                    pass

        try:
            session.commit()
            return {
                "status": "success",
                "data": kitebase.utils.serialize_model(record, db_table=db_table),
                "message": _('Record updated successfully'),
                "code": 200
            }
        except Exception as e:
            session.rollback()
            return {"status": "error", "message": f"Update failed: {str(e)}", "code": 400}


def handle_delete(app, model_class, params: Dict[str, Any], db_table=None) -> Dict[str, Any]:
    """Handle DELETE operations"""
    record_id = params.get(pk_field(db_table))
    if not record_id:
        return {"status": "error", "message": _('Record ID is required for deletion'), "code": 400}

    with app.get_session() as session:
        # Find the record
        record = session.get(model_class, record_id)
        if not record:
            return {"status": "error", "message": f"Record with id {record_id} not found", "code": 404}

        try:
            session.delete(record)
            session.commit()
            return {
                "status": "success",
                "message": _('Record deleted successfully'),
                "code": 200
            }
        except Exception as e:
            session.rollback()
            return {"status": "error", "message": f"Deletion failed: {str(e)}", "code": 400}


# build_filters spells some operators differently from the querybuilder
_PERIOD_OPS = {'neq': 'ne', 'gte': 'ge', 'lte': 'le'}


def build_filters(model_class, query_filters: Dict[str, Any]) -> Optional[Any]:
    """
    Build SQLAlchemy filter conditions from query parameters.

    Supports:
    - Exact match: {"field": value}
    - Operators: {"field__op": value} where op can be:
      - eq: Equal
      - neq: Not equal
      - gt: Greater than
      - gte: Greater than or equal
      - lt: Less than
      - lte: Less than or equal
      - like: LIKE pattern
      - ilike: Case-insensitive LIKE
      - in: IN a list of values
      - between: Between two values (provide [min, max] list)
    - Logical OR:  {"$or":  [{condition1}, {condition2}, ...]}
    - Logical AND: {"$and": [{group1}, {group2}, ...]}

    AND is the *implicit* default: sibling keys in a filter dict are AND-ed
    together. The explicit "$and" is only needed to AND whole sub-groups —
    e.g. (A OR B) AND (C OR D), which the flat form can't express because a
    dict has at most one "$or" key. Its list of sub-filters is built
    recursively and folded into the surrounding AND.
    """
    if not query_filters:
        return None

    # A column the table never sends is not filterable either: an equality
    # filter on one is a way of guessing the value it would not return.
    secrets = kitebase.utils.secret_columns(kitebase.utils.table_definition(model_class))

    conditions = []

    # Handle special $or operator
    if "$or" in query_filters:
        or_conditions = []
        for or_filter in query_filters["$or"]:
            or_condition = build_filters(model_class, or_filter)
            if or_condition is not None:
                or_conditions.append(or_condition)

        if or_conditions:
            conditions.append(or_(*or_conditions))

        # Remove $or from further processing
        query_filters = {k: v for k, v in query_filters.items() if k != "$or"}

    # Handle special $and operator — each sub-group is built recursively and
    # added to `conditions`, which are AND-ed together below (symmetric to $or).
    if "$and" in query_filters:
        for and_filter in query_filters["$and"]:
            and_condition = build_filters(model_class, and_filter)
            if and_condition is not None:
                conditions.append(and_condition)

        # Remove $and from further processing
        query_filters = {k: v for k, v in query_filters.items() if k != "$and"}

    # Process standard filters
    for key, value in query_filters.items():
        if '__' in key:
            field, operator = key.split('__', 1)
        else:
            field, operator = key, 'eq'

        if not hasattr(model_class, field):
            continue

        if field in secrets:
            raise ValueError(f"Column '{field}' is not filterable")

        column = getattr(model_class, field)
        period = kitebase.utils.temporal_condition(
            column, _PERIOD_OPS.get(operator, operator), value)
        if period is not None:
            conditions.append(period)
            continue
        value = kitebase.utils.coerce_temporal(column, value)

        if operator == 'eq':
            conditions.append(column == value)
        elif operator == 'neq':
            conditions.append(column != value)
        elif operator == 'gt':
            conditions.append(column > value)
        elif operator == 'gte':
            conditions.append(column >= value)
        elif operator == 'lt':
            conditions.append(column < value)
        elif operator == 'lte':
            conditions.append(column <= value)
        elif operator == 'like':
            conditions.append(column.like(value))
        elif operator == 'ilike':
            conditions.append(column.ilike(value))
        elif operator == 'in':
            conditions.append(column.in_(value))
        elif operator == 'between':
            # Value should be a list/tuple with exactly 2 elements [min, max]
            if isinstance(value, (list, tuple)) and len(value) == 2:
                conditions.append(column.between(value[0], value[1]))

    if conditions:
        return and_(*conditions)
    return None


@endpoint('query')
def db_query(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a query in DynamicQueryBuilder format

    Parameters:
        - format: The desired format for the result from the ones provided by execute_query method
        - query: Dict for DynamicQueryBuilder
        - count (bool, default false): if true, also return total_count (runs a COUNT subquery)
        - limit (int, optional): shorthand — merged into query dict if not already set there
        - offset (int, optional): shorthand — merged into query dict if not already set there

    Returns:
        - count=false: { status, data: [...] }
        - count=true:  { status, data: { records: [...], total: int, offset: int, limit: int|null } }
    """
    fmt = data.get("format", "tuples")
    query_def = data.get("query")
    want_count = bool(data.get("count", False))

    if not query_def:
        return {"status": "error", "message": _('Query not defined'), "code": 400}

    # Allow top-level limit/offset as a convenience shorthand
    if 'limit' in data and 'limit' not in query_def:
        query_def = {**query_def, 'limit': data['limit']}
    if 'offset' in data and 'offset' not in query_def:
        query_def = {**query_def, 'offset': data['offset']}

    try:
        app = kitebase.utils.get_app()
        with app.get_session() as session:
            builder = DynamicQueryBuilder(session, app.models)
            records = builder.execute_query(query_def, result_format=fmt)

            if want_count:
                total = builder.count_query(query_def)
                return {
                    "status": "success",
                    "data": {
                        "records": records,
                        "total": total,
                        "offset": query_def.get('offset', 0),
                        "limit": query_def.get('limit'),
                    }
                }

            return {"status": "success", "data": records}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e), "code": 500}


@endpoint('auth')
def authenticate(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Authenticate a user and return a context for subsequent operations.

    Parameters:
        - username: User identifier
        - password: User credential

    Returns:
        Dictionary with authentication result and context
    """
    try:
        username = data.get('username')
        password = data.get('password')

        if not username or not password:
            return {
                "status": "error",
                "message": _('Username and password are required'),
                "code": 400
            }

        # Get authentication configuration
        app = kitebase.utils.get_app()
        config = app.pm.config.get('authentication', {})
        user_table = config.get('user_table', 'User')
        name_field = config.get('username_field', 'username')
        pass_field = config.get('password_field', 'password')
        context_fields = config.get('context_fields', ['id'])

        # Find the user
        user = kitebase.utils.seek(user_table, {name_field: username})

        if not user:
            return {
                "status": "error",
                "message": _('Invalid credentials'),
                "code": 401
            }

        # Verify the password against its stored form. A value still stored the
        # old way is accepted and left alone: hashing happens when a password is
        # written, never behind the back of a login (see kitebase.transforms).
        if not kitebase.transforms.verify_password(password, getattr(user, pass_field, None)):
            return {
                "status": "error",
                "message": _('Invalid credentials'),
                "code": 401
            }

        # Check if user is active
        if hasattr(user, 'is_active') and not user.is_active:
            return {
                "status": "error",
                "message": _('Account is inactive'),
                "code": 401
            }

        # Build context with selected fields from the User table
        # and handle custom fields automatically
        context = {}

        # Get all attributes available on the user model
        user_model = app.models.get(user_table)
        user_attributes = [column.key for column in user_model.__table__.columns]

        for field in context_fields:
            if field in user_attributes:
                # Standard field from User table
                context[field] = getattr(user, field)
            else:
                # Custom field not in User table
                # Check if this field was provided in the request
                if field in data:
                    context[field] = data[field]
                else:
                    # Otherwise set to None
                    context[field] = None

        context['username'] = username

        return {
            "status": "success",
            "data": {
                "authenticated": True,
                "context": context
            },
            "code": 200
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e), "code": 500}


@endpoint('update_context')
def update_context(data):
    """
    Update user context and return a new token with updated context.
    """
    try:
        # Get current context
        current_context = kitebase.db.BaseApp.get_context()

        # Verify user is authenticated
        if not current_context or 'id' not in current_context:
            return {
                "status": "error",
                "message": _('User not authenticated'),
                "code": 401
            }

        # Only allow the client to set framework fields (op_date, ...) + this
        # app's custom context fields. Identity columns stay server-authoritative.
        # Same allowlist as the /auth/update_context route, and the same None
        # = remove the field.
        app = kitebase.utils.get_app()
        allowed = set(server_utils.FRAMEWORK_UPDATABLE_FIELDS) | set(
            server_utils.custom_context_fields(app.pm.config)
        )
        for field in allowed:
            if field not in data:
                continue
            if data[field] is None:
                current_context.pop(field, None)
            else:
                current_context[field] = data[field]

        # Update the context in the current thread
        kitebase.db.BaseApp.set_context(current_context)

        return {
            "status": "success",
            "data": {
                "context": current_context
            },
            "code": 200
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e), "code": 500}


@endpoint('get_server_config')
def get_server_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return server configuration and type registry for client-side use.

    Single startup call — replaces the former get_type_schema endpoint.

    Parameters:
        include_builtin (bool, default false) — include SQLAlchemy built-in types

    Response data:
        {
          config: {
            page_size: int,           # global DataView page size default
            locale: str,              # config.yaml `locale`
            app_title: str | None,    # config.yaml `title`, the name the chrome shows
            # (future) page_size_by_type: { reference, master, transaction, log }
            # Any other client-relevant keys from config.yaml dataview section
          },
          types: { TypeName: { inheritance, base, python_type, builtin,
                               ...yaml_attrs, columns? } }
        }

    The server exposes raw type metadata without widget inference.
    Each client (web, mobile, desktop, …) applies its own rendering logic.
    Field-level attrs from view descriptors override type-level defaults (client-side).
    """
    try:
        app = kitebase.utils.get_app()
        include_builtin = bool(data.get('include_builtin', False))
        return {
            'status': 'success',
            'data': {
                'config': {
                    **app.pm.config.get('dataview', {}),
                    'locale': app.pm.config.get('locale', 'en'),
                    'app_title': app.pm.config.get('title'),
                },
                'types': app.get_type_schema(include_builtin),
                'tables': app.get_table_schema(),
                'schemas': app.get_schema_registry(),
            },
            'code': 200,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'status': 'error', 'message': str(e), 'code': 500}
