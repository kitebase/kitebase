import kitebase
import kitebase.context
from kitebase import tokens
import kitebase.transforms
import kitebase.media
import kitebase.refsets
from kitebase import flow
from kitebase import access
from kitebase.endpoints import endpoint
from kitebase.errors import Invalid, NotFound, Refusal
from kitebase.querybuilder import DynamicQueryBuilder
from kitebase.i18n import _, _f
from typing import Dict, Any
from sqlalchemy import desc, asc
from sqlalchemy import Date, DateTime, Integer, inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
import datetime


@endpoint('db', access=access.PER_TABLE)
def db_operations(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic database CRUD endpoint.

    Parameters:
        - table: Name of the table/model to operate on
        - method: Operation to perform (get, create, update, delete)
        - id: Optional ID for single record operations
        - data: Data for create/update operations
        - start: Pagination start index (default: 0)
        - limit: Pagination limit (default: 100)
        - order_by: Field to order by
        - order_dir: Direction of ordering ('asc' or 'desc')

    Returns:
        Dictionary with operation results
    """
    table_name = data.get('table')
    method = data.get('method', 'get').lower()
    if not table_name:
        raise Invalid(_('Table name is required'))

    app = kitebase.utils.get_app()
    model_class = app.find_model_class(table_name)
    if not model_class:
        raise NotFound(_f("Table '{name}' not found", name=table_name))
    db_table = app.tables.get(table_name)

    if method not in access.DB_METHODS:
        raise Invalid(_f("Unsupported method: '{method}'", method=method))
    access.check_table(model_class.__name__, access.DB_METHODS[method])

    if method == 'get':
        return handle_get(app, model_class, data, db_table)
    elif method == 'create':
        return handle_create(app, model_class, data, db_table)
    elif method == 'update':
        return handle_update(app, model_class, data, db_table)
    else:
        return handle_delete(app, model_class, data, db_table)


def pk_field(db_table) -> str:
    """Return the first PK field name for a table, defaulting to 'id'."""
    if db_table is None:
        return 'id'
    pk_cols = [col.name for col in db_table.effective_columns if col.attributes.get('primary_key')]
    return pk_cols[0] if pk_cols else 'id'


def write_values(db_table, record_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prepare incoming values for storage, following what the columns declare.

    A column of a group the user may not write is dropped first: a create gets
    its default, an update leaves it alone (kitebase.access). Then three
    rules, all from `kitebase.transforms`:
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
    unwritable = access.unwritable_columns(db_table)
    result = {}
    errors = {}
    for key, value in record_data.items():
        if key in unwritable:
            continue
        attrs = attributes.get(key, {})
        is_empty = value is None or value == ''

        if attrs.get('secret') and is_empty:
            continue

        if attrs.get('media'):
            try:
                value = kitebase.media.incoming(kitebase.utils.get_app().media, attrs, value)
            except kitebase.media.MediaError as e:
                errors[key] = str(e)
                continue
            if value is kitebase.media.KEEP:
                continue
            result[key] = value
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


def serialize_model(model, db_table=None) -> Dict[str, Any]:
    """
    A record as it leaves for the client.

    Columns marked `secret: true` are left out: they must never reach a client,
    and the definition is resolved from the model itself when the caller has
    none at hand, so forgetting to pass one cannot turn into a leak. So are the
    columns of a group the current user may not read (kitebase.access). A
    file's stored reference leaves as its signed URL (kitebase.media).

    Args:
        model: SQLAlchemy model instance
        db_table: DbTable instance, for the virtual columns (optional)
    """
    db_table = kitebase.utils.table_definition(model.__class__, db_table)
    secrets = kitebase.utils.secret_columns(db_table)
    if db_table is not None:
        secrets = secrets | access.unreadable_columns(db_table)

    # Real and mixin columns: SQLAlchemy introspection covers both
    result = {column.name: getattr(model, column.name)
              for column in sa_inspect(model.__class__).columns
              if column.name not in secrets}

    if db_table is not None:
        # Virtual columns (hybrid_property, not in __table__.columns)
        for col in db_table.virtual_columns:
            if col.name not in secrets:
                result[col.name] = getattr(model, col.name, None)
        kitebase.media.sign_record(kitebase.utils.get_app(), db_table, result)

    return result


def handle_get(app, model_class, params: Dict[str, Any], db_table=None) -> Dict[str, Any]:
    """Handle GET operations (list or single record)"""
    record_id = params.get(pk_field(db_table))
    start = int(params.get('start', 0))
    limit = int(params.get('limit', 100))
    # A filter here would be a second dialect beside the endpoint `query`:
    # refused, so a list never comes back whole when a filter was meant.
    if params.get('query'):
        raise Invalid("db get takes no filters: lists are read with the endpoint 'query'")
    order_by = params.get('order_by')
    order_dir = params.get('order_dir', 'asc')

    with app.get_session() as session:
        if record_id:
            # Get single record
            record = session.get(model_class, record_id)
            if not record or not access.row_visible(session, model_class, record_id):
                raise NotFound(_f('Record with id {id} not found', id=record_id))

            data = serialize_model(record, db_table=db_table)
            kitebase.refsets.attach(app, session, db_table, [data])
            return {"status": "success", "data": data, "code": 200}
        else:
            # List records with filtering and pagination
            query = session.query(model_class)

            # The rows the user may read, when the permission holds only on some
            rule = access.row_clause(model_class.__name__, 'read', app)
            if rule is not None:
                query = query.filter(rule)

            # Apply ordering: on a column the client could not read, the
            # order would tell what the values are.
            if order_by:
                column = getattr(model_class, order_by, None)
                if column:
                    if order_by in kitebase.utils.secret_columns(db_table):
                        raise Invalid(f"Column '{order_by}' is not sortable")
                    access.check_columns(model_class.__name__, [order_by])
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
            result = [serialize_model(record, db_table=db_table) for record in records]

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


def require_columns(model_class, values: Dict[str, Any]) -> None:
    """Refuse a new row that leaves out a column the database requires.

    A column that is NOT NULL, with no default and no key the database
    assigns, must arrive with a value: said by field before the insert, rather
    than as the database's own error, which quotes the statement and its
    values. A secret column the form does not show (a password) is reported
    here too, under its name.

    Raises:
        ValidationError: the columns left out, as {field: message}
    """
    table = getattr(model_class, '__table__', None)
    if table is None:
        return
    errors = {}
    for col in table.columns:
        if (col.nullable or col.default is not None or col.server_default is not None
                or col is table.autoincrement_column):
            continue
        if values.get(col.key) is None:
            errors[col.key] = _('A value is required')
    if errors:
        raise kitebase.transforms.ValidationError(errors)


def typed_key(model_class) -> list:
    """The primary key columns of a table whose key is typed, not assigned.

    Empty when the database assigns the key (an autoincrement): only a key
    someone types can collide with one already there, or be changed by mistake.
    """
    table = getattr(model_class, '__table__', None)
    if table is None or table.autoincrement_column is not None:
        return []
    return list(table.primary_key.columns)


def refuse_duplicate_key(session, model_class, values: Dict[str, Any]) -> None:
    """Refuse a new row whose typed key is already taken, said on the key's fields.

    The database would refuse it too, quoting the statement; here the message
    names the value, and says when the row holding it is archived, which is
    the case where whoever types the code sees no row with it.

    Raises:
        ValidationError: the key fields, as {field: message}
    """
    cols = typed_key(model_class)
    if not cols or any(values.get(c.key) is None for c in cols):
        return
    key = tuple(values[c.key] for c in cols)
    existing = session.get(model_class, key if len(key) > 1 else key[0])
    if existing is None:
        return
    shown = ', '.join(str(v) for v in key)
    message = (_f('{value} already exists, archived', value=shown)
               if getattr(existing, 'is_archived', False) is True
               else _f('{value} already exists', value=shown))
    raise kitebase.transforms.ValidationError({c.key: message for c in cols})


def refuse_key_change(record, model_class, changes: Dict[str, Any]) -> None:
    """Refuse an update that would change a typed key.

    Other rows, and other tables, refer to the row by its key: renaming it
    would leave them pointing at nothing. The form locks the field once the
    record exists; this holds for whoever calls the endpoint directly.

    Raises:
        ValidationError: the key fields the update tries to change
    """
    errors = {c.key: _('The key cannot be changed once the record exists')
              for c in typed_key(model_class)
              if c.key in changes and changes[c.key] != getattr(record, c.key)}
    if errors:
        raise kitebase.transforms.ValidationError(errors)


def handle_create(app, model_class, params: Dict[str, Any], db_table=None) -> Dict[str, Any]:
    """Handle CREATE operations"""
    record_data = params.get('data')

    if not record_data:
        raise Invalid(_('No data provided for creation'))

    try:
        record_data, sets = kitebase.refsets.split(db_table, record_data)
        record_data = write_values(db_table, record_data)
        coerced = {k: coerce_value(model_class, k, v) for k, v in record_data.items()}
        require_columns(model_class, coerced)

        # The table's rules first: a question or a refusal writes nothing
        ctx, refusal = flow.judge(model_class.__name__, 'insert', coerced, None,
                                  params.get('answers'))
        if refusal:
            return refusal
        new_record = model_class(**ctx.record)

        with app.get_session() as session:
            refuse_duplicate_key(session, model_class, ctx.record)
            session.add(new_record)
            # The row as inserted, defaults included, must be one the user may create
            session.flush()
            access.check_row(session, model_class,
                             sa_inspect(new_record).identity[0], 'create')
            kitebase.refsets.write(app, session, db_table, new_record, sets)
            flow.commit(ctx, session, new_record)
            session.commit()

            # Refresh to get generated IDs and other database defaults
            session.refresh(new_record)

            saved = serialize_model(new_record, db_table=db_table)
            kitebase.refsets.attach(app, session, db_table, [saved])
            result = {
                "status": "success",
                "data": saved,
                "message": _('Record created successfully'),
                "code": 201
            }
        return flow.finish(ctx, result, saved)
    except SQLAlchemyError as e:
        raise Invalid(f"Creation failed: {e}") from e


def handle_update(app, model_class, params: Dict[str, Any], db_table=None) -> Dict[str, Any]:
    """Handle UPDATE operations"""
    record_id = params.get(pk_field(db_table))
    if not record_id:
        raise Invalid(_('Record ID is required for updates'))

    record_data = params.get('data')
    if not record_data:
        raise Invalid(_('No data provided for update'))

    record_data, sets = kitebase.refsets.split(db_table, record_data)
    record_data = write_values(db_table, record_data)

    with app.get_session() as session:
        # Find the record
        record = session.get(model_class, record_id)
        if not record or not access.row_visible(session, model_class, record_id):
            raise NotFound(_f('Record with id {id} not found', id=record_id))
        # On the row as stored: otherwise the change itself could satisfy it
        access.check_row(session, model_class, record_id, 'update')

        changes = {k: coerce_value(model_class, k, v) for k, v in record_data.items()
                   if hasattr(record, k)}
        refuse_key_change(record, model_class, changes)
        original = flow.row_values(record)
        ctx, refusal = flow.judge(model_class.__name__, 'update', {**original, **changes},
                                  original, params.get('answers'))
        if refusal:
            return refusal

        # What the request changed, and what a rule changed on top of it
        for key, value in ctx.record.items():
            if key in changes or original.get(key) != value:
                # Skip read-only hybrid/virtual properties
                try:
                    setattr(record, key, value)
                except AttributeError:
                    pass

        try:
            session.flush()
            kitebase.refsets.write(app, session, db_table, record, sets)
            flow.commit(ctx, session, record)
            session.commit()
            saved = serialize_model(record, db_table=db_table)
            kitebase.refsets.attach(app, session, db_table, [saved])
        except SQLAlchemyError as e:
            raise Invalid(f"Update failed: {e}") from e

    return flow.finish(ctx, {"status": "success", "data": saved,
                             "message": _('Record updated successfully'), "code": 200}, saved)


def handle_delete(app, model_class, params: Dict[str, Any], db_table=None) -> Dict[str, Any]:
    """Handle DELETE operations"""
    record_id = params.get(pk_field(db_table))
    if not record_id:
        raise Invalid(_('Record ID is required for deletion'))

    with app.get_session() as session:
        # Find the record
        record = session.get(model_class, record_id)
        if not record or not access.row_visible(session, model_class, record_id):
            raise NotFound(_f('Record with id {id} not found', id=record_id))
        access.check_row(session, model_class, record_id, 'delete')

        original = flow.row_values(record)
        ctx, refusal = flow.judge(model_class.__name__, 'delete', original, original,
                                  params.get('answers'))
        if refusal:
            return refusal

        try:
            flow.commit(ctx, session, record)
            session.delete(record)
            session.commit()
        except SQLAlchemyError as e:
            raise Invalid(f"Deletion failed: {e}") from e

    return flow.finish(ctx, {"status": "success",
                             "message": _('Record deleted successfully'), "code": 200}, None)


@endpoint('query', access=access.PER_TABLE)
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
        raise Invalid(_('Query not defined'))

    # Allow top-level limit/offset as a convenience shorthand
    if 'limit' in data and 'limit' not in query_def:
        query_def = {**query_def, 'limit': data['limit']}
    if 'offset' in data and 'offset' not in query_def:
        query_def = {**query_def, 'offset': data['offset']}

    app = kitebase.utils.get_app()
    # A RefSet named in the select is read apart, as a list per record
    main_table = app.tables.get(query_def.get('table')) if isinstance(query_def, dict) else None
    query_def, sets = kitebase.refsets.from_select(main_table, query_def)
    if sets and fmt != 'records':
        raise Invalid(_f("{fields} can be read in the records format only",
                         fields=', '.join(sets)))
    with app.get_session() as session:
        builder = DynamicQueryBuilder(
            session, app.models, covered=kitebase.refsets.junctions(main_table),
            value_transform=kitebase.media.query_transform(app, data.get('variants')))
        try:
            records = builder.execute_query(query_def, result_format=fmt)
            total = builder.count_query(query_def) if want_count else None
        except Refusal:
            raise
        except ValueError as e:
            # The querybuilder names what it refuses in the query it was given
            raise Invalid(str(e)) from e
        if sets:
            kitebase.refsets.attach(app, session, main_table, records, sets)

    if want_count:
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


@endpoint('auth', access=access.AUTHENTICATED)
def authenticate(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Authenticate a user and return a context for subsequent operations.

    Parameters:
        - username: User identifier
        - password: User credential

    Returns:
        Dictionary with authentication result and context
    """
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        raise Invalid(_('Username and password are required'))

    app = kitebase.utils.get_app()
    config = app.plugins.config.get('authentication', {})
    user_table = config.get('user_table', 'User')
    name_field = config.get('username_field', 'username')
    pass_field = config.get('password_field', 'password')
    context_fields = tokens.context_fields(config, default=['id'])

    user = kitebase.utils.seek(user_table, {name_field: username})
    # Verify the password against its stored form. A value still stored the
    # old way is accepted and left alone: hashing happens when a password is
    # written, never behind the back of a login (see kitebase.transforms).
    if not user or not kitebase.transforms.verify_password(password, getattr(user, pass_field, None)):
        raise Refusal(_('Invalid credentials'), code=401)
    if hasattr(user, 'is_active') and not user.is_active:
        raise Refusal(_('Account is inactive'), code=401)

    # The context: the fields of the user table it names, and the others
    # from the request when it carries them
    user_model = app.models.get(user_table)
    user_attributes = [column.key for column in user_model.__table__.columns]
    context = {}
    for field in context_fields:
        if field in user_attributes:
            context[field] = getattr(user, field)
        else:
            context[field] = data.get(field)
    context['username'] = username

    return {
        "status": "success",
        "data": {
            "authenticated": True,
            "context": context
        },
        "code": 200
    }


@endpoint('update_context', access=access.AUTHENTICATED)
def update_context(data):
    """
    Update user context and return a new token with updated context.
    """
    current_context = kitebase.context.current()
    if not current_context or 'id' not in current_context:
        raise Refusal(_('User not authenticated'), code=401)

    # Only allow the client to set framework fields (op_date, ...) + this
    # app's custom context fields. Identity columns stay server-authoritative.
    # Same allowlist as the /auth/update_context route, and the same None
    # = remove the field.
    app = kitebase.utils.get_app()
    allowed = set(tokens.FRAMEWORK_UPDATABLE_FIELDS) | set(
        tokens.custom_context_fields(app.plugins.config)
    )
    for field in allowed:
        if field not in data:
            continue
        if data[field] is None:
            current_context.pop(field, None)
        else:
            current_context[field] = data[field]

    kitebase.context.replace(current_context)

    return {
        "status": "success",
        "data": {
            "context": current_context
        },
        "code": 200
    }


def _each_to_client(names: Dict[str, Any]) -> Dict[str, Any]:
    """A map of names with each value through the outbound gate: the names are
    data (a type or a field may be called `doc`), what they name is a descriptor."""
    from kitebase.outbound import to_client
    return {name: to_client(value) for name, value in names.items()}


@endpoint('get_server_config', access=access.AUTHENTICATED)
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
            export_max_rows: int,     # ceiling of an export (dataview.export_max_rows)
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
    from kitebase.endpoint_export import max_rows
    app = kitebase.utils.get_app()
    include_builtin = bool(data.get('include_builtin', False))
    return {
        'status': 'success',
        'data': {
            'config': {
                **app.plugins.config.get('dataview', {}),
                # Always there, default included: the client refuses before
                # loading every row, the server would refuse after.
                'export_max_rows': max_rows(),
                'locale': app.plugins.config.get('locale', 'en'),
                'app_title': app.plugins.config.get('title'),
            },
            'types': _each_to_client(app.get_type_schema(include_builtin)),
            # The column labels name the fields in filters and pickers.
            'tables': _each_to_client(app.get_table_schema()),
            'schemas': {schema_id: _each_to_client(fields)
                        for schema_id, fields in app.get_schema_registry().items()},
        },
        'code': 200,
    }
