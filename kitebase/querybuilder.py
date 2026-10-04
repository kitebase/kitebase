"""
Dynamic Query Builder for SQLAlchemy.

This module provides a flexible and powerful way to build SQLAlchemy queries from JSON objects.
It supports various SQL features including joins, filters, grouping, and ordering,
with both verbose and concise syntax options, and works with dynamic table names.
"""

import json
import re
import datetime
import decimal
from uuid import UUID
from typing import Any, Dict, List, Union, Optional, Type

from sqlalchemy import and_, or_, desc, asc, false, select, func, text, TextClause, literal_column, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.engine.row import Row
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.ext.declarative import DeclarativeMeta

from kitebase.utils import coerce_temporal, search_info, secret_columns, table_definition, temporal_condition

# The escape character LIKE patterns are built with. Postgres already reads a
# backslash this way; stating it makes every backend agree, and makes the
# escaping a caller does to a user's own '%' actually take effect.
LIKE_ESCAPE = '\\'


def _escape_like(text: str) -> str:
    """Neutralise the wildcards in text a user typed, so '50%' means '50%'."""
    return re.sub(r'([\\%_])', r'\\\1', text)


def _ordered_column_names(order_def: Optional[List[Any]]) -> List[str]:
    """The column names an ordering definition mentions, in any of its formats."""
    names: List[str] = []
    for item in order_def or []:
        if isinstance(item, dict):
            column = item.get('column')
        elif isinstance(item, list):
            column = item[0] if item else None
        else:
            column = item
        if isinstance(column, str):
            names.append(column)
    return names


def _is_aggregated(query_def: Dict[str, Any]) -> bool:
    """
    Whether the query returns groups rather than rows.

    A grouped or aggregated query has no primary key to end its ordering with —
    naming one would be a column outside the grouping — and it is not paginated
    row by row either, so it has nothing to gain from the tiebreaker.
    """
    if query_def.get('group_by') or query_def.get('having'):
        return True
    select_def = query_def.get('select') or []
    return any(isinstance(expr, str) and '(' in expr for expr in select_def)


def filters_mention(filters_def: Any, table: str, column: str) -> bool:
    """
    Whether a `filters` block conditions the main table's `column` anywhere.

    A query behavior that adds an implicit condition (Archivable's `active =
    True`) steps aside when the caller already conditions that column: what the
    caller wrote is what the caller wants. The syntax is this module's, so the
    question is answered here in every form a condition can take — a concise
    key, bare or qualified as `Table.column`, a verbose dict, and both kinds of
    group — and a behavior asks instead of parsing. A qualified mention of the
    same column on another table (`Author.active`) is not a mention of this one.
    """
    def seen(node: Any) -> bool:
        if isinstance(node, list):
            return any(seen(item) for item in node)
        if not isinstance(node, dict):
            return False
        if 'conditions' in node:
            return seen(node['conditions'])
        if 'column' in node and 'op' in node:
            return node['column'] == column and node.get('table') in (None, table)
        return any(key == column or key == f'{table}.{column}' for key in node)

    return seen(filters_def) if filters_def else False


def _secret_names(model_class) -> set:
    """Names of the columns of a model that are never sent to a client."""
    return secret_columns(table_definition(model_class))


def _refuse_secret(model_class, col_name: str) -> None:
    """
    Stop a query that names a column the table never sends.

    Refused rather than dropped: a select that silently loses a column would
    read as an empty value downstream, which is the kind of answer that gets
    mistaken for data.

    Raises:
        ValueError: if the column is marked `secret: true`
    """
    if col_name in _secret_names(model_class):
        table_name = getattr(model_class, '__name__', '?')
        raise ValueError(f"Column '{table_name}.{col_name}' is not readable")


class DynamicQueryBuilder:
    """
    Main query builder that coordinates specialized builders to construct a complete query from JSON or dictionary.

    This class acts as a facade for the query building process, delegating specialized tasks
    to dedicated builder classes (SelectBuilder, JoinBuilder, etc.) and assembling the final query.

    Query Format:
    -------------
    The query can be defined either as a JSON string or a Python dictionary with the following structure:

    {
        "from": "ModelName",           # Main table/model name (required)
        "table": "ModelName",          # Alternative to "from" (optional)

        "select": [                    # Columns to select (optional, defaults to all columns)
            "column_name",             # Column from the main table
            "Model.column_name",       # Column from a specific model
            "function(column_name)",   # Function on a column (e.g., "count(id)")
            "expr as alias",           # Column or expression with alias
            "*",                       # All columns from the main table
            "Model.*"                  # All columns from a specific model
        ],

        "joins": [                     # Table joins (optional)
            {                          # Verbose format
                "table": "ModelName",
                "type": "inner|left",  # Join type (optional, defaults to "inner")
                "on": {                # Join condition
                    "left_table": "ModelName1",
                    "left_column": "column_name1",
                    "right_table": "ModelName2",
                    "right_column": "column_name2"
                }
            },
            {                          # Concise format
                "ModelName": "Model1.column1 = Model2.column2"
            },
            {                          # Dictionary format
                "ModelName": {
                    "type": "left",
                    "on": "Model1.column1 = Model2.column2"
                }
            }
        ],

        "filters": {                   # WHERE conditions (optional)
            "conditions": [
                {                      # Verbose format
                    "table": "ModelName",
                    "column": "column_name",
                    "op": "eq|ne|gt|ge|lt|le|like|ilike|in|notin|isnull|isnotnull|between",
                    "value": value
                },
                {                      # Concise format
                    "column_name": value  # Implicit "eq" operator
                },
                {                      # Concise format with explicit operator
                    "Model.column_name": ["operator", value]
                },
                {                      # Operator symbols format
                    "column_name": ["=", value]  # Symbols like =, !=, >, >=, <, <=
                },
                {                      # Nested conditions with AND (default)
                    "conditions": [
                        {"column1": value1},
                        {"column2": value2}
                    ]
                },
                {                      # Nested conditions with OR
                    "op": "or",
                    "conditions": [
                        {"column1": value1},
                        {"column2": value2}
                    ]
                }
            ]
        },

        "search": "rossi",             # Text search (optional) — see below

        "group_by": [                  # GROUP BY clause (optional)
            "column_name",
            "Model.column_name",
            "expression"
        ],

        "having": {                    # HAVING clause (optional, same format as "filters")
            "conditions": [
                {"function(column_name)": ["operator", value]}
            ]
        },

        "order_by": [                  # ORDER BY clause (optional)
            "column_name",             # Ascending order by default
            ["column_name", "asc|desc"],  # Explicit direction
            {                          # Verbose format
                "column": "column_name",
                "direction": "asc|desc"
            },
            {                          # With table specification
                "table": "ModelName",
                "column": "column_name",
                "direction": "desc"
            }
        ],

        "limit": number,               # LIMIT clause (optional)
        "offset": number,              # OFFSET clause (optional)

        "resolve": true                # Absolute lookup (optional) — see below
    }

    The "resolve" flag
    ------------------
    `resolve: true` means "I am resolving a value that is already stored
    somewhere, tell me what it is" — as opposed to "propose the values that are
    acceptable now". It is an *absolute* lookup: registered query behaviors are
    not applied at all.

    It exists because the two questions have different answers. A document
    saved last year points at a partner who has since been archived, or who
    lost the role the picklist filters on: the picklist must not offer that
    partner any more, but the document must still display their name. Without
    the flag the implicit filter also hits the lookup by primary key and the
    field renders blank.

    Filtering the picklist is the job of the behaviors and of the view's
    `domain`; neither must ever reach the resolution of a stored value.

    The "search" key
    ----------------
    One text, in OR over the columns the table declares as searchable — the
    cascade of DATA_MODEL.md §4.4, resolved from the table definition, so the
    caller sends what the user typed and never a list of columns. The primary
    key joins the OR as one branch matched exactly, when the text is a value its
    type could hold.

    It is a key of its own rather than one more condition group because it is
    ANDed with the filters: it can only narrow them, and the two are cleared
    independently. A table that declares nothing searchable refuses the search
    instead of quietly returning every row.

    Examples:
    ---------
    Example 1: Basic query with filters
    ```
    {
        "from": "Product",
        "select": ["product_id", "product_name", "unit_price"],
        "filters": {
            "conditions": [
                {"discontinued": 0},
                {"unit_price": [">", 20]}
            ]
        },
        "order_by": ["unit_price"],
        "limit": 10
    }
    ```

    Example 2: Join query with aggregation
    ```
    {
        "from": "OrderDetail",
        "select": [
            "Product.product_name",
            "sum(OrderDetail.quantity) as total_sold",
            "sum(OrderDetail.quantity * OrderDetail.unit_price) as revenue"
        ],
        "joins": [
            {"Product": "Product.product_id = OrderDetail.product_id"}
        ],
        "group_by": ["Product.product_name"],
        "order_by": [["revenue", "desc"]],
        "limit": 5
    }
    """

    def __init__(self, session: Session, models: Dict[str, Type[DeclarativeMeta]]) -> None:
        """
        Initialize the builder with a SQLAlchemy session and a dictionary of models.

        Args:
            session: SQLAlchemy session for executing queries
            models: Dictionary with model names as keys and model classes as values
        """
        self.session = session
        self.models = models
        self.engine = session.get_bind() if session else None

    def build_query(self, query_def: Union[Dict[str, Any], str]) -> Select:
        """
        Build a SQLAlchemy query from a JSON string or a dictionary.

        Args:
            query_def: Either a JSON string or a dictionary defining the query structure

        Returns:
            SQLAlchemy Select object representing the query

        Raises:
            ValueError: If the query definition is invalid
        """
        # Convert JSON string to dictionary if needed
        if isinstance(query_def, str):
            try:
                query_def = json.loads(query_def)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON: {str(e)}")

        # Ensure we have a dictionary
        if not isinstance(query_def, dict):
            raise ValueError(f"Query definition must be a JSON string or a dictionary, got {type(query_def)}")

        # Get the main table (supports both 'from' and 'table' as synonyms)
        main_table = None
        if 'from' in query_def:
            main_table = query_def['from']
        elif 'table' in query_def:
            main_table = query_def['table']

        if not main_table:
            raise ValueError("Query definition must contain a 'from' or 'table' field")

        if main_table not in self.models:
            raise ValueError(f"Model not found: {main_table}")

        # Initialize specialized builders
        select_builder = SelectBuilder(self.models, main_table, self.engine)
        join_builder = JoinBuilder(self.models)
        filter_builder = FilterBuilder(self.models, select_builder)
        order_builder = OrderBuilder(self.models, select_builder)

        # Determine the columns to select - if 'select' is absent, pass None or [] to use default behavior
        select_def = query_def.get('select')
        columns_to_select = select_builder.build_select_columns(select_def)

        # Start the query with the specified columns
        query = select(*columns_to_select)

        # Add the main table
        query = query.select_from(self.models[main_table])

        # Apply joins
        if 'joins' in query_def:
            query = join_builder.apply_joins(query, query_def['joins'])

        # Apply registered query behaviors (e.g. Archivable auto-filter).
        #
        # `resolve: true` skips them all: the caller is resolving an already
        # stored value, not proposing selectable ones (see the "resolve" flag
        # in the class docstring).
        #
        # INVARIANT — query behaviors are *visibility* filters, not
        # *authorization*. `resolve` comes from the client and bypasses every
        # behavior, so a behavior must never be the thing that keeps a user
        # away from records they may not see. Enforce authorization elsewhere;
        # if it ever has to live here, this loop is where the two kinds of
        # behavior must start being told apart.
        main_model = self.models[main_table]
        if not query_def.get('resolve'):
            # Lazy import: keeps querybuilder usable standalone without kitebase stack
            try:
                import kitebase.utils
                behaviors = kitebase.utils.get_app().query_behaviors
            except Exception:
                behaviors = []
            for behavior in behaviors:
                if behavior.applies_to(main_model):
                    query = behavior.apply(main_model, query_def, query)

        # Apply filters (WHERE)
        if 'filters' in query_def:
            query = filter_builder.apply_filters(query, query_def['filters'])

        # Apply the text search over the table's declared search fields.
        # A key of its own rather than one more condition group: it is ANDed
        # with whatever the filters say, so it can only narrow them, and the
        # two are cleared independently — emptying the search box leaves the
        # user's rules alone.
        search_text = str(query_def.get('search') or '').strip()
        if search_text:
            query = query.where(filter_builder.build_search(main_model, search_text))

        # Apply grouping (GROUP BY)
        if 'group_by' in query_def:
            group_columns = []
            for group_item in query_def['group_by']:
                if isinstance(group_item, str):
                    # First convert model references to table names
                    group_item = select_builder._process_column_references(group_item)
                    # Then handle database-specific functions like EXTRACT
                    group_item = select_builder._apply_database_specific_replacements(group_item)

                    if "(" in group_item and ")" in group_item:
                        # Functional expression
                        group_columns.append(text(group_item))
                    else:
                        # Simple column
                        try:
                            group_columns.append(select_builder._get_column(group_item))
                        except (ValueError, AttributeError):
                            # For complex expressions or non-columns, use text()
                            group_columns.append(text(group_item))

            if group_columns:
                query = query.group_by(*group_columns)

        # Apply group filters (HAVING)
        if 'having' in query_def:
            query = filter_builder.apply_having(query, query_def['having'])

        # Apply ordering
        if 'order_by' in query_def:
            query = order_builder.apply_ordering(query, query_def['order_by'])

        # A paginated query needs a total order, or a page is not a well-defined
        # slice of anything: rows tied on the ordering column can come back in a
        # different order on the next execution, so one is read twice and another
        # never. The key ends the ordering and settles every tie.
        if ('limit' in query_def or 'offset' in query_def) and not _is_aggregated(query_def):
            query = order_builder.append_key_tiebreaker(
                query, main_model, query_def.get('order_by'))

        # Apply limit/offset
        if 'limit' in query_def:
            query = query.limit(query_def['limit'])

        if 'offset' in query_def:
            query = query.offset(query_def['offset'])

        return query

    def execute_query(self, query_def: Union[Dict[str, Any], str], result_format: str = 'default') -> Any:
        """
        Build and execute a query, returning the results in the specified format.

        Args:
            query_def: Either a JSON string or a dictionary defining the query
            result_format: Format of the results:
                - 'default': Returns the raw result rows
                - 'dict': Returns a dictionary with 'headers' and 'data'
                - 'records': Returns a list of dictionaries (row as dict)
                - 'tuples': Returns a tuple (headers, data)
                - 'json': Returns a JSON string representation
                - 'csv': Returns a CSV string representation
                - 'cursor': Returns the result cursor for streaming iteration

        Returns:
            Query results in the requested format or a result proxy for streaming iteration
        """
        query = self.build_query(query_def)
        result = self.session.execute(query)

        # If streaming mode is requested, return the result proxy directly
        if result_format == 'cursor':
            return result

        # Get column headers
        headers = result.keys()

        if result_format == 'dict':
            # Return a dictionary with 'headers' and 'data'
            return {
                'headers': list(headers),
                'data': self._prepare_data(result.all())
            }
        elif result_format == 'records':
            # Return a list of dictionaries (similar to pandas.DataFrame.to_dict('records'))
            return [dict(zip(headers, self._prepare_row(row))) for row in result.all()]
        elif result_format == 'tuples':
            # Return a tuple (headers, data) where data is a list of tuples
            return (list(headers), self._prepare_data(result.all()))
        elif result_format == 'json':
            # Return a JSON string directly
            data = {
                'headers': list(headers),
                'data': self._prepare_data(result.all())
            }
            return json.dumps(data, cls=JSONEncoder)
        elif result_format == 'csv':
            # Return a CSV string
            return self.to_csv(result)
        else:
            # Default behavior: return only the data
            return result.all()

    def count_query(self, query_def: Union[Dict[str, Any], str]) -> int:
        """
        Count total rows for a query definition, ignoring limit/offset.
        Works correctly with GROUP BY queries by wrapping in a subquery.

        Args:
            query_def: Either a JSON string or a dictionary defining the query

        Returns:
            Total row count (int)
        """
        if isinstance(query_def, str):
            try:
                query_def = json.loads(query_def)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON: {str(e)}")

        # Strip limit/offset so the count covers all matching rows
        count_def = {k: v for k, v in query_def.items() if k not in ('limit', 'offset')}
        inner = self.build_query(count_def)
        count_q = select(func.count()).select_from(inner.subquery())
        return self.session.execute(count_q).scalar() or 0

    def _prepare_data(self, data: List[Any]) -> List[List[Any]]:
        """
        Prepare data for JSON serialization, converting problematic types.

        Args:
            data: List of query result rows

        Returns:
            List of prepared rows
        """
        return [self._prepare_row(row) for row in data]

    def _prepare_row(self, row: Any) -> List[Any]:
        """
        Prepare a single row for JSON serialization.
        Converts problematic types to serializable types.

        Args:
            row: A query result row

        Returns:
            List of prepared values
        """
        if isinstance(row, Row):
            # If it's a SQLAlchemy Row object, convert it to tuple
            row = tuple(row)

        # Convert row values
        return [self._convert_value(val) for val in row]

    def _convert_value(self, value: Any) -> Any:
        """
        Convert a single value to a JSON-serializable type.

        Args:
            value: The value to convert

        Returns:
            Converted value
        """
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        elif isinstance(value, decimal.Decimal):
            return float(value)
        elif isinstance(value, UUID):
            return str(value)
        elif isinstance(value, bytes):
            try:
                return value.decode('utf-8')
            except UnicodeDecodeError:
                return str(value)  # Fallback to string representation
        return value

    def to_json(self, data: Any, structured: bool = False, root_name: str = 'results') -> str:
        """
        Convert any result to JSON.

        Args:
            data: Data to convert to JSON
            structured: If True, generates more structured JSON with metadata
            root_name: Name of the root element when using structured format

        Returns:
            JSON string
        """
        if structured:
            # Create a structured JSON with metadata
            current_time = datetime.datetime.now().isoformat()

            if isinstance(data, dict) and 'headers' in data and 'data' in data:
                # Already in our dict format
                structured_data = {
                    root_name: data['data'],
                    'metadata': {
                        'columns': data['headers'],
                        'timestamp': current_time,
                        'row_count': len(data['data']),
                    }
                }
            elif isinstance(data, (list, tuple)) and all(isinstance(i, (list, tuple, dict)) for i in data):
                # List of rows
                structured_data = {
                    root_name: data,
                    'metadata': {
                        'timestamp': current_time,
                        'row_count': len(data),
                    }
                }
            else:
                # Fallback for other data types
                structured_data = {
                    root_name: data,
                    'metadata': {
                        'timestamp': current_time
                    }
                }

            return json.dumps(structured_data, cls=JSONEncoder)
        else:
            # Simple JSON conversion
            return json.dumps(data, cls=JSONEncoder)

    def to_csv(self, result: Any, delimiter: str = ',', include_headers: bool = True) -> str:
        """
        Convert query results to CSV format.

        Args:
            result: SQLAlchemy result object or result proxy
            delimiter: CSV delimiter character
            include_headers: Whether to include column headers as the first row

        Returns:
            CSV formatted string
        """
        import io
        import csv

        # Get headers if not already fetched
        headers = result.keys() if hasattr(result, 'keys') else []

        # Create a string buffer for CSV writing
        output = io.StringIO()
        writer = csv.writer(output, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)

        # Write headers if requested
        if include_headers and headers:
            writer.writerow(headers)

        # Write data rows
        for row in result:
            # Convert any non-serializable objects
            processed_row = [self._convert_value(val) for val in row]
            writer.writerow(processed_row)

        # Get the CSV string
        csv_string = output.getvalue()
        output.close()

        return csv_string

    def get_query_headers(self, query_def: Union[Dict[str, Any]]) -> List[str]:
        """
        Return only the column headers that would be returned by the query
        without actually executing the full query.

        Args:
            query_def: Either a JSON string or a dictionary defining the query

        Returns:
            List of column names
        """
        query = self.build_query(query_def)
        # Execute a query with LIMIT 0 to get only the structure without data
        limited_query = query.limit(0)
        result = self.session.execute(limited_query)
        return list(result.keys())

    def get_sql(self, query_def: Union[Dict[str, Any]]) -> str:
        """
        Return the SQL string corresponding to the query.
        Useful for debugging and analysis.

        Args:
            query_def: Either a JSON string or a dictionary defining the query

        Returns:
            SQL string
        """
        query = self.build_query(query_def)
        return str(query.compile(compile_kwargs={"literal_binds": True}))


class SelectBuilder:
    """
    Handles column selection and aggregation functions with SQLite compatibility.

    This class is responsible for parsing and building the SELECT clause of the query,
    including handling column references, functions, aliases, and complex expressions.
    """

    def __init__(self, models: Dict[str, Type[DeclarativeMeta]],
                 main_table: str, engine: Optional[Engine] = None) -> None:
        """
        Initialize the SelectBuilder.

        Args:
            models: Dictionary of table name to model class mappings
            main_table: Name of the main table in the query
            engine: SQLAlchemy engine for database-specific adaptations
        """
        self.models = models
        self.main_table = main_table
        self.aliases: Dict[str, Any] = {}  # Dictionary to track defined aliases
        self.engine = engine  # Reference to SQLAlchemy engine for detecting database type

    def build_select_columns(self, select_def: Optional[List[str]]) -> List[Any]:
        """
        Build the list of columns to select from the JSON definition.
        Supports syntax for aggregate functions, aliases, and complex expressions.
        Properly expands * and Table.* into individual columns.

        Args:
            select_def: List of column definitions or None

        Returns:
            List of SQLAlchemy column objects
        """
        columns = []
        self.aliases = {}  # Reset aliases

        # If select is missing or empty, expand to all columns of the main table
        if not select_def:
            main_model = self.models[self.main_table]
            return self._expand_model_columns(main_model)

        for col_expr in select_def:
            # Handle asterisk selectors
            if col_expr == "*":
                # Expand to all columns from the main table
                main_model = self.models[self.main_table]
                columns.extend(self._expand_model_columns(main_model))
                continue
            elif col_expr.endswith(".*"):
                # Expand to all columns from a specific table
                table_name = col_expr[:-2]  # Remove the ".*" part
                model = self._get_model(table_name)
                if model:
                    columns.extend(self._expand_model_columns(model))
                    continue
                else:
                    raise ValueError(f"Model not found for wildcard selection: {table_name}")

            # Apply database-specific replacements if needed
            col_expr = self._apply_database_specific_replacements(col_expr)

            # Check if it contains an alias: the last " as ", and kept in its
            # case — it is the key of the column in every record, and the
            # client finds it by the same rule, exactly as written
            # (dataview.query extractFieldKey).
            as_idx = col_expr.lower().rfind(" as ")
            if as_idx != -1:
                expr = col_expr[:as_idx]
                alias = col_expr[as_idx + 4:].strip()

                # Build the expression
                column = self._parse_expression(expr)

                # Label the expression based on its type
                if isinstance(column, TextClause):
                    # For text() expressions we need a different approach
                    labeled_column = literal_column(str(column)).label(alias)
                else:
                    # For normal columns and functions we can use label directly
                    labeled_column = column.label(alias)

                # Save the alias for future reference (e.g., in ordering)
                self.aliases[alias] = labeled_column

                columns.append(labeled_column)
            else:
                # No alias
                column = self._parse_expression(col_expr)
                columns.append(column)

        return columns

    def _get_model(self, name: str) -> Optional[Type[DeclarativeMeta]]:
        """
        Find a model by name or table name using various lookup strategies.

        Args:
            name: Model name or table name

        Returns:
            Model class if found, None otherwise
        """
        # Try direct lookup
        if name in self.models:
            return self.models[name]

        # Try case-insensitive model name lookup (e.g., "Customer" vs "customer")
        name_lower = name.lower()
        for model_name, model_class in self.models.items():
            if model_name.lower() == name_lower:
                return model_class

        # Try to find by actual table name
        for model_name, model_class in self.models.items():
            tablename = getattr(model_class, '__tablename__', None)
            if tablename == name:
                return model_class

        # Nothing found
        return None

    def _expand_model_columns(self, model_class):
        """
        Expand a model class into a list of all its columns.

        Columns marked `secret: true` are left out: a wildcard is a request for
        "what this table holds", not a way around what it never sends.

        Args:
            model_class: SQLAlchemy model class

        Returns:
            List of column objects for the model
        """
        # Get table metadata to access individual columns
        if hasattr(model_class, '__table__') and hasattr(model_class.__table__, 'columns'):
            secrets = _secret_names(model_class)
            return [getattr(model_class, column.key)
                    for column in model_class.__table__.columns
                    if column.key not in secrets]

        # Fallback for models without __table__ attribute
        return [model_class]

    def _apply_database_specific_replacements(self, expr: str) -> str:
        """
        Apply database-specific replacements for non-standard functions.

        Args:
            expr: SQL expression string

        Returns:
            Modified expression string with database-specific adaptations
        """
        # First, process model references to replace Model.column with tablename.column
        expr = self._process_column_references(expr)

        # Then check if we're using SQLite
        is_sqlite = self._is_sqlite_database()

        if is_sqlite:
            # Convert EXTRACT(YEAR FROM date) to strftime('%Y', date)
            expr = self._replace_extract_for_sqlite(expr)

        return expr

    def _is_sqlite_database(self) -> bool:
        """
        Determine if we're using a SQLite database.

        Returns:
            True if using SQLite, False otherwise
        """
        if self.engine is None:
            # If we don't have an engine, assume SQLite as the safest case for compatibility
            return True

        # Check the driver name or database URL
        return 'sqlite' in str(self.engine.url).lower()

    def _replace_extract_for_sqlite(self, expr: str) -> str:
        """
        Replace EXTRACT(YEAR FROM date) with strftime('%Y', date) for SQLite.
        Processes an expression where model references have already been replaced.

        Args:
            expr: SQL expression string with resolved table names

        Returns:
            Modified expression string with SQLite function syntax
        """
        # Pattern to detect EXTRACT(YEAR FROM ...) and similar
        extract_pattern = r'EXTRACT\s*\(\s*(YEAR|MONTH|DAY|HOUR|MINUTE|SECOND|QUARTER)\s+FROM\s+([^)]+)\s*\)'

        def replace_extract(match: re.Match) -> str:
            date_part = match.group(1).upper()
            date_expr = match.group(2).strip()

            format_map = {
                'YEAR': '%Y',
                'MONTH': '%m',
                'DAY': '%d',
                'HOUR': '%H',
                'MINUTE': '%M',
                'SECOND': '%S',
                'QUARTER': None  # Special case, handled below
            }

            if date_part == 'QUARTER':
                # SQLite doesn't have a direct format for quarter, so we calculate (month-1)/3+1
                return f"(cast(strftime('%m', {date_expr}) as integer) - 1) / 3 + 1"

            if date_part in format_map:
                return f"strftime('{format_map[date_part]}', {date_expr})"

            # For unsupported parts, leave unchanged
            return match.group(0)

        # Apply the replacement
        return re.sub(extract_pattern, replace_extract, expr, flags=re.IGNORECASE)

    def _parse_expression(self, expr: str) -> Any:
        """
        Parse an expression that can be a simple column, a function,
        or a complex expression with operations and nested parentheses.

        Args:
            expr: SQL expression string

        Returns:
            SQLAlchemy expression object
        """
        expr = expr.strip()

        # Case 1: Simple column expression (e.g., "Model.column")
        if "(" not in expr:
            return self._get_column(expr)

        # Case 2: Simple SQL function (e.g., "count(Model.id)")
        if expr.count("(") == 1 and ")" in expr and not any(op in expr for op in ["+", "-", "*", "/"]):
            # Handle function with simple argument
            func_name, arg = expr.split("(", 1)
            arg = arg.rstrip(")")

            # Process the argument in case it's a column reference
            processed_arg = self._process_column_references(arg)

            func_expr = f"{func_name}({processed_arg})"
            return self._build_function(func_expr)

        # Case 3: Complex expression (e.g., "sum(Model.col * Model.col2 * (1 - Model.col3))")
        # Replace all model.column references with real tablename.column
        processed_expr = self._process_column_references(expr)

        # Use text() for the literal SQL expression
        return text(processed_expr)

    def _process_column_references(self, expr: str) -> str:
        """
        Replace model.column references in a complex expression with actual table names.

        Args:
            expr: SQL expression string

        Returns:
            Processed expression with correct table references
        """
        # Find all possible column references (pattern: Model.column)
        column_refs = re.findall(r'([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)', expr)

        # Create a copy of the original expression that we'll modify
        processed_expr = expr

        # Process each reference
        for model_name, col_name in column_refs:
            if model_name in self.models:
                model_class = self.models[model_name]
                _refuse_secret(model_class, col_name)

                # Get the actual current tablename (works with dynamic tablenames)
                actual_tablename = getattr(model_class, '__tablename__', None)

                if actual_tablename and hasattr(model_class, col_name):
                    # Replace Model.column with actual_tablename.column
                    old_ref = f"{model_name}.{col_name}"
                    new_ref = f"{actual_tablename}.{col_name}"
                    processed_expr = processed_expr.replace(old_ref, new_ref)

        return processed_expr

    def _get_column(self, col_expr: str) -> Any:
        """
        Get a column object from the specified string.
        Supports both model names and table names for looking up the column.

        Args:
            col_expr: Column expression string

        Returns:
            SQLAlchemy column object

        Raises:
            ValueError: If the model is not found
        """
        if "." in col_expr:
            # "table.column" or "model.column" format
            table_or_model_name, col_name = col_expr.split(".", 1)

            # Direct lookup by model name
            if table_or_model_name in self.models:
                return self._column_of(self.models[table_or_model_name], col_name)

            # Try case-insensitive model name lookup (e.g., "Customer" vs "customer")
            for model_name, model_class in self.models.items():
                if model_name.lower() == table_or_model_name.lower():
                    return self._column_of(model_class, col_name)

            # Try to find by actual table name (for cases where the table name is used directly)
            for model_name, model_class in self.models.items():
                tablename = getattr(model_class, '__tablename__', None)
                if tablename == table_or_model_name:
                    return self._column_of(model_class, col_name)

            # If we get here, we couldn't find a matching model
            raise ValueError(f"Model or table not found: {table_or_model_name}")
        else:
            # Use the main table (no prefix specified)
            return self._column_of(self.models[self.main_table], col_expr)

    def _column_of(self, model_class, col_name: str) -> Any:
        """Resolve a column of a model, refusing the ones never sent to a client."""
        _refuse_secret(model_class, col_name)
        return getattr(model_class, col_name)

    def _build_function(self, func_expr: str) -> Any:
        """
        Build a function expression from a string.
        Example: "count(users.id)" -> func.count(users.id)

        Args:
            func_expr: Function expression string

        Returns:
            SQLAlchemy function expression
        """
        # Extract function name and argument
        func_name, remainder = func_expr.split("(", 1)
        arg_expr = remainder.rsplit(")", 1)[0].strip()
        func_name = func_name.lower().strip()

        # Check if the argument is a simple column or a complex expression
        if any(op in arg_expr for op in ["+", "-", "*", "/"]) or arg_expr.count("(") > 0:
            # For complex arguments, use text()
            arg = text(self._process_column_references(arg_expr))
            return getattr(func, func_name)(arg)
        else:
            # For simple arguments, get the column
            try:
                column = self._get_column(arg_expr)
                return getattr(func, func_name)(column)
            except (ValueError, AttributeError):
                # If it's not a valid column, use text()
                return getattr(func, func_name)(text(arg_expr))

    def get_alias(self, name: str) -> Optional[Any]:
        """
        Return the aliased column, if it exists.
        Useful for ordering by alias.

        Args:
            name: Alias name

        Returns:
            Aliased column object or None if not found
        """
        return self.aliases.get(name)


class JoinBuilder:
    """
    Handles table joins with support for concise syntax.

    This class manages JOIN operations, supporting both verbose and concise join definitions
    with various condition formats.
    """

    def __init__(self, models: Dict[str, Type[DeclarativeMeta]]) -> None:
        """
        Initialize the JoinBuilder.

        Args:
            models: Dictionary of table name to model class mappings
        """
        self.models = models

    def apply_joins(self, query: Select, joins_def: List[Dict[str, Any]]) -> Select:
        """
        Apply joins to the query. Supports both verbose and concise syntax.

        Args:
            query: SQLAlchemy query object
            joins_def: List of join definitions

        Returns:
            Modified query with joins applied

        Raises:
            ValueError: If join format is invalid or 'on' condition is missing
        """
        if not joins_def:
            return query

        for join_def in joins_def:
            # Handle various join formats
            if isinstance(join_def, dict):
                if 'table' in join_def:
                    # Verbose format: { table: X, on: '...', type: 'inner' }
                    table_name = join_def['table']
                    join_type = join_def.get('type', 'inner').lower()
                    if 'on' not in join_def:
                        raise ValueError(
                            f"Join '{table_name}' is missing the 'on' condition. "
                            f"Use: `- {table_name}: 'SourceTable.fk_col = {table_name}.pk_col'`"
                        )
                    join_condition = self._parse_join_condition(join_def['on'])

                elif len(join_def) == 1:
                    # Concise format: {"orders": {"type": "left", "on": "users.id = orders.user_id"}}
                    # or {"orders": "users.id = orders.user_id"}
                    table_name = list(join_def.keys())[0]
                    join_value = join_def[table_name]

                    if isinstance(join_value, dict):
                        join_type = join_value.get('type', 'inner').lower()
                        join_condition = self._parse_join_condition(join_value['on'])
                    else:
                        # Even more concise: {"orders": "users.id = orders.user_id"}
                        join_type = 'inner'
                        join_condition = self._parse_join_condition(join_value)
                else:
                    raise ValueError(f"Invalid join format: {join_def}")
            elif isinstance(join_def, str):
                raise ValueError(
                    f"Join '{join_def}' is missing the condition. "
                    f"Use: `- {join_def}: 'SourceTable.fk_col = {join_def}.pk_col'`"
                )
            else:
                raise ValueError(f"Invalid join format: {join_def}")

            # Verify the table exists
            if table_name not in self.models:
                raise ValueError(f"Model for join not found: {table_name}")

            join_model = self.models[table_name]

            # Apply the join
            if join_type == 'left':
                query = query.outerjoin(join_model, join_condition)
            else:  # join_type == 'inner'
                query = query.join(join_model, join_condition)

        return query

    def _parse_join_condition(self, condition_def: Union[Dict[str, str], str]) -> ClauseElement:
        """
        Process the join condition in different formats:
        1. Verbose: {"left_table": "users", "left_column": "id", "right_table": "orders", "right_column": "user_id"}
        2. Concise dictionary: {"users.id": "orders.user_id"}
        3. Concise string: "users.id = orders.user_id"

        Args:
            condition_def: Join condition definition

        Returns:
            SQLAlchemy clause element for the join condition

        Raises:
            ValueError: If condition format is invalid
        """
        # Case 1: Verbose format
        if isinstance(condition_def, dict) and all(k in condition_def for k in
                                                   ["left_table", "left_column", "right_table", "right_column"]):
            left_table = condition_def['left_table']
            left_column = condition_def['left_column']
            right_table = condition_def['right_table']
            right_column = condition_def['right_column']

            if left_table not in self.models or right_table not in self.models:
                raise ValueError("Table not found for join condition")

            left_model = self.models[left_table]
            right_model = self.models[right_table]

            return getattr(left_model, left_column) == getattr(right_model, right_column)

        # Case 2: Concise dictionary format with a single key
        elif isinstance(condition_def, dict) and len(condition_def) == 1:
            left_expr = list(condition_def.keys())[0]
            right_expr = condition_def[left_expr]

            return self._build_condition_from_expressions(left_expr, right_expr)

        # Case 3: String format
        elif isinstance(condition_def, str):
            if "=" in condition_def:
                left_expr, right_expr = condition_def.split("=", 1)
                left_expr = left_expr.strip()
                right_expr = right_expr.strip()

                return self._build_condition_from_expressions(left_expr, right_expr)
            else:
                raise ValueError(f"Join condition in string format must contain '=': {condition_def}")

        # Unsupported case
        raise ValueError(f"Invalid join condition format: {condition_def}")

    def _build_condition_from_expressions(self, left_expr: str, right_expr: str) -> ClauseElement:
        """
        Build a join condition from two expressions in "table.column" format.

        Args:
            left_expr: Left side expression
            right_expr: Right side expression

        Returns:
            SQLAlchemy clause element for the join condition

        Raises:
            ValueError: If expressions are invalid
        """
        # Parse the table.column expressions
        if "." not in left_expr or "." not in right_expr:
            raise ValueError(f"Join expressions must be in 'table.column' format: {left_expr}, {right_expr}")

        left_table, left_column = left_expr.split(".", 1)
        right_table, right_column = right_expr.split(".", 1)

        if left_table not in self.models or right_table not in self.models:
            raise ValueError(f"Table not found for join condition: {left_table}, {right_table}")

        left_model = self.models[left_table]
        right_model = self.models[right_table]

        return getattr(left_model, left_column) == getattr(right_model, right_column)


class FilterBuilder:
    """
    Handles filters (WHERE and HAVING) with support for concise syntax.

    This class is responsible for building filter conditions, supporting both
    verbose and concise syntax, and handling complex nested conditions.
    """

    def __init__(self, models: Dict[str, Type[DeclarativeMeta]], select_builder: SelectBuilder) -> None:
        """
        Initialize the FilterBuilder.

        Args:
            models: Dictionary of table name to model class mappings
            select_builder: SelectBuilder instance for column and function handling
        """
        self.models = models
        self.select_builder = select_builder
        self.main_table = select_builder.main_table

        # Mapping of symbolic operators to textual operators
        self.operator_map = {
            "=": "eq",
            "==": "eq",
            "!=": "ne",
            "<>": "ne",
            ">": "gt",
            ">=": "ge",
            "<": "lt",
            "<=": "le",
            "in": "in",
            "not in": "notin",
            "like": "like",
            "ilike": "ilike",
            "is null": "isnull",
            "is not null": "isnotnull",
            "between": "between"
        }

    def apply_filters(self, query: Select, filters_def: Dict[str, Any]) -> Select:
        """
        Apply WHERE filters to the query.

        `filters` holds its conditions under `conditions`, and a shape this
        cannot read is refused rather than ignored: a filter that silently does
        not filter answers with the whole table, and the caller cannot tell that
        from a query that legitimately matched everything. The endpoint `db` has
        a filter DSL of its own — flat, and under another key — so writing one
        where the other is expected is the mistake this catches.

        An empty `filters` stays silent: it says nothing, and nothing is what it
        means.

        Args:
            query: SQLAlchemy query object
            filters_def: Filter definitions

        Returns:
            Modified query with filters applied

        Raises:
            ValueError: if filters_def says something this cannot read
        """
        self._require_conditions(filters_def, 'filters')
        if not filters_def:
            return query

        filter_conditions = self._build_filter_conditions(filters_def['conditions'])
        if filter_conditions is not None:
            query = query.where(filter_conditions)

        return query

    @staticmethod
    def _require_conditions(spec: Any, key: str) -> None:
        """Refuse a filter block whose conditions cannot be found."""
        if not spec:
            return
        if not isinstance(spec, dict):
            raise ValueError(
                f"'{key}' must be an object holding 'conditions', "
                f"not {type(spec).__name__}"
            )
        if 'conditions' not in spec:
            raise ValueError(
                f"'{key}' has no 'conditions' — expected "
                f"{{\'{key}\': {{\'conditions\': [{{\'field\': value}}]}}}}, "
                f"got the keys {sorted(spec)}. The endpoint `db` takes flat "
                f"filters under \'query\' instead; the two are not interchangeable."
            )

    def apply_having(self, query: Select, having_def: Dict[str, Any]) -> Select:
        """
        Apply HAVING filters for aggregations.

        Args:
            query: SQLAlchemy query object
            having_def: HAVING filter definitions

        Returns:
            Modified query with HAVING filters applied
        """
        self._require_conditions(having_def, 'having')
        if not having_def:
            return query

        having_conditions = self._build_filter_conditions(having_def['conditions'])
        if having_conditions is not None:
            query = query.having(having_conditions)

        return query

    def build_search(self, model_class: Type[DeclarativeMeta], text: str) -> ClauseElement:
        """
        One text, in OR over the columns the table declares as searchable.

        The primitive behind the quick search box, the FK combobox and the value
        widget of a rule on a foreign key: the caller sends what the user typed
        and never has to know which columns that means, which is what keeps the
        three of them the same mechanism (DATA_MODEL.md §4.4).

        The key takes part as *one branch of the OR*, matched exactly, and only
        when the text is a value its type could hold. Typing 42 may well mean the
        record numbered 42 and may equally mean a title with 42 in it, and
        hiding the second is an answer nobody asked for.

        Wildcards the user typed are escaped, so searching for '50%' looks for
        that and not for everything.

        Args:
            model_class: the main model of the query
            text: what the user typed, already stripped

        Returns:
            A clause to AND with the rest of the query

        Raises:
            ValueError: if the table declares nothing to search
        """
        info = search_info(table_definition(model_class))
        table_name = getattr(model_class, '__name__', self.main_table)

        if not info['search_fields'] and not info['search_pk']:
            raise ValueError(
                f"Table '{table_name}' has no searchable columns: declare "
                f"search_fields, or searchable: true on a column"
            )

        pattern = f"%{_escape_like(text)}%"
        branches = [
            self._build_filter_conditions({field: ['ilike', pattern]})
            for field in info['search_fields']
        ]
        branches = [b for b in branches if b is not None]

        pk_value = self._as_key_value(model_class, info['search_pk'], text)
        if pk_value is not None:
            branches.append(getattr(model_class, info['search_pk']) == pk_value)

        # Searchable only by key, and the text is not one: no row can match, and
        # saying so is not the same as dropping the search and returning all.
        if not branches:
            return false()

        return or_(*branches)

    @staticmethod
    def _as_key_value(model_class: Type[DeclarativeMeta], pk: Optional[str], text: str) -> Any:
        """
        The text as a value of the key column, or None when it cannot be one.

        A key the user cannot type — a date, a float — never takes part: the
        branch would either raise or match by accident.
        """
        if not pk:
            return None
        try:
            python_type = inspect(model_class).columns[pk].type.python_type
        except (KeyError, NotImplementedError, AttributeError):
            return None

        if python_type is str:
            return text
        if python_type is int:
            try:
                return int(text)
            except ValueError:
                return None
        if python_type is UUID:
            try:
                return UUID(text)
            except ValueError:
                return None
        return None

    def _build_filter_conditions(self, conditions: Any) -> Optional[ClauseElement]:
        """
        Build filter conditions recursively, supporting both verbose and concise syntax.

        Args:
            conditions: Filter condition definitions

        Returns:
            SQLAlchemy clause element for the filter conditions or None if no valid conditions
        """
        if not conditions:
            return None

        # Case 1: Verbose - dictionary with explicit fields
        if isinstance(conditions, dict) and all(k in
                                                ['table', 'column', 'op'] for k in conditions.keys() if k != 'value'):
            return self._build_verbose_filter(conditions)

        # Case 2: Concise - dictionary with column key and operator/value
        elif isinstance(conditions, dict) and not ('conditions' in conditions and 'op' in conditions):
            return self._build_concise_filter(conditions)

        # Case 3: Group of conditions with default AND or explicit OR
        elif isinstance(conditions, list):
            return self._build_condition_group(conditions)

        # Case 4: Explicit AND/OR group of conditions
        elif isinstance(conditions, dict) and 'conditions' in conditions:
            op = and_
            if 'op' in conditions and conditions['op'].lower() == 'or':
                op = or_

            subconditions = []
            for condition in conditions['conditions']:
                parsed = self._build_filter_conditions(condition)
                if parsed is not None:
                    subconditions.append(parsed)

            if not subconditions:
                return None

            return op(*subconditions)

        return None

    def _build_verbose_filter(self, condition: Dict[str, Any]) -> ClauseElement:
        """
        Build a filter using the original verbose syntax.

        Args:
            condition: Verbose filter condition definition

        Returns:
            SQLAlchemy clause element for the filter
        """
        table_name = condition.get('table')
        column_name = condition['column']
        op = condition['op'].lower()
        value = condition.get('value')

        # A path is only a path when it is not already qualified by a table
        if table_name is None:
            relation = self._build_relation_filter(column_name, op, value)
            if relation is not None:
                return relation

        # Get the column
        column = self._get_column_from_names(table_name, column_name)

        # Apply the operator
        return self._apply_operator(column, op, value)

    def _build_concise_filter(self, condition: Dict[str, Any]) -> ClauseElement:
        """
        Build a filter using concise syntax.
        Example: {"users.username": ["like", "%john%"]} or {"amount": [">", 100]}

        Args:
            condition: Concise filter condition definition

        Returns:
            SQLAlchemy clause element for the filter

        Raises:
            ValueError: If condition format is invalid
        """
        # Single filter
        if len(condition) != 1:
            raise ValueError("Concise condition must have exactly one key")

        column_expr = list(condition.keys())[0]
        value_def = list(condition.values())[0]

        # Check if value_def is a list [operator, value]
        if not isinstance(value_def, list):
            # Assume "=" as default operator
            op = "eq"
            value = value_def
        else:
            op = value_def[0]
            value = value_def[1:]
            if len(value) == 1:
                value = value[0]  # Unpack if it's a single value

        # Normalize the operator
        if op in self.operator_map:
            op = self.operator_map[op]

        # A relationship path is not a column: it is a predicate on another table
        relation = self._build_relation_filter(column_expr, op, value)
        if relation is not None:
            return relation

        # Get the column
        column = self._get_column_from_expr(column_expr)

        # Apply the operator
        return self._apply_operator(column, op, value)

    def _build_condition_group(self, conditions: List[Any]) -> Optional[ClauseElement]:
        """
        Build a group of conditions (default AND).

        Args:
            conditions: List of condition definitions

        Returns:
            SQLAlchemy clause element for the condition group or None if no valid conditions
        """
        # Check if the first elements define an operator
        op = and_
        start_idx = 0

        if len(conditions) >= 1 and isinstance(conditions[0], str) and conditions[0].lower() == 'op':
            if len(conditions) >= 2 and conditions[1].lower() == 'or':
                op = or_
            start_idx = 2

        # Build the conditions
        filter_conditions = []
        for condition in conditions[start_idx:]:
            parsed_condition = self._build_filter_conditions(condition)
            if parsed_condition is not None:
                filter_conditions.append(parsed_condition)

        if not filter_conditions:
            return None

        return op(*filter_conditions)

    def _get_column_from_expr(self, column_expr: str) -> Any:
        """
        Get a column object from an expression (e.g., 'users.username' or 'amount').

        Args:
            column_expr: Column expression string

        Returns:
            SQLAlchemy column object
        """
        if "(" in column_expr and ")" in column_expr:
            # Function (e.g., "count(users.id)")
            return self.select_builder._build_function(column_expr)

        if "." in column_expr:
            # "table.column" format
            table_name, col_name = column_expr.split(".", 1)
            return self._get_column_from_names(table_name, col_name)
        else:
            # Use the main table
            return self._get_column_from_names(self.main_table, column_expr)

    def _get_column_from_names(self, table_name: Optional[str], column_name: str) -> Any:
        """
        Get a column object from table name and column name.

        Args:
            table_name: Table name or None to use main table
            column_name: Column name

        Returns:
            SQLAlchemy column object

        Raises:
            ValueError: If model for filter not found
        """
        if table_name is None:
            table_name = self.main_table

        if table_name not in self.models:
            # A dotted name has two readings, and a wrong guess about which one
            # was meant sends the reader looking in the wrong place: say both.
            main = self.models.get(self.main_table)
            known = sorted(inspect(main).relationships.keys()) if main is not None else []
            raise ValueError(
                f"'{table_name}' is neither a model nor a relationship of "
                f"{getattr(main, '__name__', self.main_table)} "
                f"(relationships: {', '.join(known) or 'none'})"
            )

        model_class = self.models[table_name]
        # Filtering and ordering address a column just as a select does: a
        # column the table never sends must not be reachable from either, or a
        # filter on it turns into a way of guessing its value.
        _refuse_secret(model_class, column_name)
        return getattr(model_class, column_name)

    def _walk_relation_path(self, expr: str) -> Optional[tuple]:
        """
        Read a dotted expression as a walk over relationships, or decline it.

        Declines — returns None — for anything that is not one: a bare column, or
        the `Table.column` form the rest of the builder already understands. The
        two share the dot, so the first segment decides: a relationship of the
        main table opens a path, a model name does not.

        Returns:
            (chain, target_model, column_name), where chain is the list of
            (owner_model, attribute, is_collection) to walk, or None

        Raises:
            ValueError: if a name is both a model and a relationship, or if a
                segment names neither
        """
        if '.' not in expr:
            return None

        model = self.models.get(self.main_table)
        if model is None:
            return None

        head = expr.split('.', 1)[0]
        relationships = inspect(model).relationships
        if head not in relationships:
            return None
        if head in self.models:
            raise ValueError(
                f"'{head}' is both a model and a relationship of {model.__name__}: "
                f"the path '{expr}' is ambiguous"
            )

        segments = expr.split('.')
        chain = []
        for segment in segments[:-1]:
            relationships = inspect(model).relationships
            if segment not in relationships:
                raise ValueError(
                    f"'{segment}' is not a relationship of {model.__name__} in path '{expr}'"
                )
            chain.append((model, segment, relationships[segment].uselist))
            model = relationships[segment].mapper.class_

        column_name = segments[-1]
        if not hasattr(model, column_name):
            raise ValueError(f"'{model.__name__}' has no column '{column_name}' in path '{expr}'")

        return chain, model, column_name

    def _build_relation_filter(self, expr: str, op: str, value: Any) -> Optional[ClauseElement]:
        """
        Build a predicate that reaches another table through a relationship.

        The direction of each step decides its translation, and the reason is the
        cardinality of the result rather than taste. A step towards *one* — a
        foreign key — cannot multiply rows, so it becomes a correlated `has()`.
        A step towards *many* would: joining an author to their books returns
        that author once per book, which inflates counts, shifts pagination and
        makes "authors who never published with X" inexpressible. It becomes an
        `any()`, an EXISTS that asks whether such a row exists without bringing
        it into the result.

        Returns None when the expression is not a path, so the caller falls back
        to its ordinary column handling.
        """
        walk = self._walk_relation_path(expr)
        if walk is None:
            return None

        chain, target, column_name = walk

        # A secret column stays unreachable however long the road to it
        _refuse_secret(target, column_name)

        condition = self._apply_operator(getattr(target, column_name), op, value)
        for owner, attribute, is_collection in reversed(chain):
            related = getattr(owner, attribute)
            condition = related.any(condition) if is_collection else related.has(condition)
        return condition

    def _apply_operator(self, column: Any, op: str, value: Any) -> ClauseElement:
        """
        Apply the appropriate operator to the column with the specified value.

        Args:
            column: SQLAlchemy column object
            op: Operator name
            value: Value to compare against

        Returns:
            SQLAlchemy clause element for the operation

        Raises:
            ValueError: If operator is not supported or between operator is used incorrectly
        """
        period = temporal_condition(column, op, value)
        if period is not None:
            return period
        value = coerce_temporal(column, value)
        if op == 'eq':
            return column == value
        elif op == 'ne':
            return column != value
        elif op == 'lt':
            return column < value
        elif op == 'le':
            return column <= value
        elif op == 'gt':
            return column > value
        elif op == 'ge':
            return column >= value
        elif op == 'like':
            return column.like(value, escape=LIKE_ESCAPE)
        elif op == 'ilike':
            return column.ilike(value, escape=LIKE_ESCAPE)
        elif op == 'in':
            return column.in_(value)
        elif op == 'notin':
            return ~column.in_(value)
        elif op == 'isnull':
            return column.is_(None)
        elif op == 'isnotnull':
            return column.isnot(None)
        elif op == 'between':
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("The 'between' operator requires two values")
            return column.between(value[0], value[1])
        else:
            raise ValueError(f"Unsupported filter operator: {op}")


class OrderBuilder:
    """
    Handles result ordering with support for aliases and concise syntax.

    This class manages the ORDER BY clause, supporting multiple ordering formats
    and handling of aliased columns and expressions.
    """

    def __init__(self, models: Dict[str, Type[DeclarativeMeta]], select_builder: SelectBuilder) -> None:
        """
        Initialize the OrderBuilder.

        Args:
            models: Dictionary of table name to model class mappings
            select_builder: SelectBuilder instance for column and alias handling
        """
        self.models = models
        self.select_builder = select_builder
        self.main_table = select_builder.main_table

    def apply_ordering(self, query: Select, order_def: List[Any]) -> Select:
        """
        Apply ordering to the query. Supports multiple formats and alias usage.

        Args:
            query: SQLAlchemy query object
            order_def: Ordering definitions

        Returns:
            Modified query with ordering applied

        Raises:
            ValueError: If ordering format is invalid
        """
        if not order_def:
            return query

        for order_item in order_def:
            if isinstance(order_item, dict):
                # Dictionary formats
                query = self._apply_dict_ordering(query, order_item)
            elif isinstance(order_item, list):
                # Array format [column, direction]
                if len(order_item) not in [1, 2]:
                    raise ValueError(f"Array ordering definition must have 1 or 2 elements: {order_item}")

                column_expr = order_item[0]
                direction = order_item[1].lower() if len(order_item) == 2 else 'asc'

                column = self._get_column_or_alias(column_expr)
                query = self._apply_direction(query, column, direction)
            elif isinstance(order_item, str):
                # Simple string format
                column = self._get_column_or_alias(order_item)
                query = self._apply_direction(query, column, 'asc')
            else:
                raise ValueError(f"Invalid ordering format: {order_item}")

        return query

    def append_key_tiebreaker(self, query: Select, model_class: Type[DeclarativeMeta],
                              order_def: Optional[List[Any]]) -> Select:
        """
        End the ordering with the primary key, so a page is a well-defined slice.

        A column with repeated values does not decide an order between the rows
        that share one, and the database is free to return them differently on
        each execution — which is exactly what pagination does, one query per
        page. With two books both titled "1984", the one that came last on page
        one can come first on page two: the reader sees it twice and never sees
        the other, with nothing to signal it.

        Adding the key makes the order *total*: no two rows are tied, so every
        page is a slice of one and the same sequence. It costs nothing to
        evaluate — the key is indexed by definition — and it also settles what
        was already undecided, since two rows that look identical now always
        come back in the same order.

        Args:
            query: the query, with its declared ordering already applied
            model_class: the main model, whose key is the tiebreaker
            order_def: the ordering already applied, to avoid naming a column twice

        Returns:
            The query, ordered by the key after everything else
        """
        named = {name.split('.')[-1] for name in _ordered_column_names(order_def)}
        try:
            key_columns = [c.name for c in inspect(model_class).primary_key]
        except Exception:
            return query

        for name in key_columns:
            if name in named:
                continue
            column = getattr(model_class, name, None)
            if column is not None:
                query = query.order_by(asc(column))
        return query

    def _apply_dict_ordering(self, query: Select, order_item: Dict[str, Any]) -> Select:
        """
        Apply ordering from dictionary format.

        Args:
            query: SQLAlchemy query object
            order_item: Dictionary ordering definition

        Returns:
            Modified query with ordering applied

        Raises:
            ValueError: If dictionary does not have required keys or model not found
        """
        if 'column' not in order_item:
            raise ValueError("Dictionary ordering definition must have 'column'")

        column_expr = order_item['column']
        direction = order_item.get('direction', 'asc').lower()

        # If there's a table, use table.column, otherwise use column directly
        if 'table' in order_item:
            table_name = order_item['table']
            if table_name not in self.models:
                raise ValueError(f"Model for ordering not found: {table_name}")
            column = getattr(self.models[table_name], column_expr)
        else:
            # Use direct expression (could be "table.column", "column", or an alias)
            column = self._get_column_or_alias(column_expr)

        return self._apply_direction(query, column, direction)

    def _get_column_or_alias(self, column_expr: str) -> Any:
        """
        Get a column or alias object from an expression.
        Supports: "column", "table.column", "function(column)", "alias"

        Args:
            column_expr: Column expression string

        Returns:
            SQLAlchemy column object or text expression

        Raises:
            ValueError: If model not found
        """
        # First check if the expression is a known alias
        alias_column = self.select_builder.get_alias(column_expr)
        if alias_column is not None:
            return alias_column

        # If not an alias, proceed as before
        if "(" in column_expr and ")" in column_expr:
            # Function (e.g., "count(users.id)")
            return self.select_builder._parse_expression(column_expr)

        if "." in column_expr:
            # "table.column" format
            table_name, col_name = column_expr.split(".", 1)
            if table_name not in self.models:
                raise ValueError(f"Model not found: {table_name}")
            return getattr(self.models[table_name], col_name)
        else:
            # Could be a column of the main table or an untracked alias
            try:
                return getattr(self.models[self.main_table], column_expr)
            except AttributeError:
                # If column not found, assume it's an untracked column alias
                # Use SQLAlchemy's text notation
                from sqlalchemy import text
                return text(column_expr)

    def _apply_direction(self, query: Select, column: Any, direction: str) -> Select:
        """
        Apply ordering direction.

        Args:
            query: SQLAlchemy query object
            column: Column to order by
            direction: Direction ('asc' or 'desc')

        Returns:
            Modified query with ordering applied
        """
        if direction == 'desc':
            return query.order_by(desc(column))
        else:  # direction == 'asc'
            return query.order_by(asc(column))


class JSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that handles common SQLAlchemy data types
    that are not natively JSON serializable.
    """
    def default(self, obj: Any) -> Any:
        """
        Convert non-serializable types to JSON-serializable types.

        Args:
            obj: Object to serialize

        Returns:
            JSON-serializable representation of the object
        """
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        elif isinstance(obj, datetime.date):
            return obj.isoformat()
        elif isinstance(obj, datetime.time):
            return obj.isoformat()
        elif isinstance(obj, decimal.Decimal):
            return float(obj)
        elif isinstance(obj, UUID):
            return str(obj)
        elif isinstance(obj, bytes):
            return obj.decode('utf-8')
        elif isinstance(obj, Row):
            # Convert Row to dictionary
            return dict(obj)
        return super(JSONEncoder, self).default(obj)
