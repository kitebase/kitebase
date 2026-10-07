import copy
import inspect
import threading
import contextvars
from pathlib import Path
from typing import Dict, List, Any, Optional, Union, Iterator
from types import ModuleType
from contextlib import contextmanager, ExitStack
import sqlalchemy.types
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, Session
from kitebase.plugins import PluginsManager, Plugin
from kitebase.endpoints import CommandProcessor
from kitebase.utils import deep_merge


def merge_columns_by_name(base: List[Dict[str, Any]],
                          override: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge two column lists by name, keeping the order of `base` first.

    A name present in both is merged, `override` winning on the keys it states;
    a name only in `override` is appended.
    """
    # Deep copies: the merge below writes into these, and the parent type keeps
    # its own columns for the tables that use it directly.
    result = [copy.deepcopy(c) for c in base]
    by_name = {c.get('name'): c for c in result}
    for column in override:
        existing = by_name.get(column.get('name'))
        if existing is None:
            new = dict(column)
            result.append(new)
            by_name[new.get('name')] = new
        else:
            deep_merge(existing, column)
    return result


class CaseString(sqlalchemy.types.TypeDecorator):
    """String normalised to one case on its way to the database.

    Declared as `case: upper` (or `lower`) on a column or on a type; without it
    a plain String is generated, which is the neutral default.

    Normalising in the type means every write goes through it — ORM, Core, bulk
    — and so do the parameters of a query, so a lower-case search matches an
    upper-case value. The object in memory keeps what was assigned to it until
    it is refreshed; the endpoints commit and re-read, so what an API call
    returns is the normalised value.
    """

    impl = sqlalchemy.types.String
    cache_ok = True

    def __init__(self, *args: Any, case: str = 'upper', **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.case = case

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.upper() if self.case == 'upper' else value.lower()


# Attributes that describe ONE column and cannot be shared by the parts of a
# composite: its identity, its storage, and the words that name it. Everything
# else a composite column declares — `editable`, and whatever an application
# writes for itself — speaks about the whole address, so it reaches every part.
_COMPOSITE_PRIVATE = {
    'name', 'type', 'prefix', 'foreign_key', 'many_to_many', 'virtual',
    # words that name one column, never five
    'label', 'help',
    # storage and constraints: `unique: true` on a composite would otherwise
    # make each of the parts unique on its own, which nobody means
    'primary_key', 'autoincrement', 'unique', 'nullable', 'index', 'default',
    'onupdate', 'length', 'precision', 'scale', 'timezone',
}


def _shared_composite_attrs(column: Dict[str, Any]) -> Dict[str, Any]:
    """What a composite column says about all of its parts.

    Without this a composite is mute: `editable: false` on an address declared
    as `type: Address` reached nothing, because each part was built from the
    *type's* definition alone — so a form let someone type into columns meant to
    be read-only, and nothing said otherwise.

    The part's own declaration still wins: a type that says something about one
    of its columns knows more than the table that happens to use the type.
    """
    return {k: v for k, v in column.items() if k not in _COMPOSITE_PRIVATE}


class DB:
    """
    Database schema manager that handles types, tables, and columns defined in plugins.

    This class is responsible for:
    - Loading and validating all database types (both SQLAlchemy built-ins and custom types)
    - Managing table definitions and their relationships
    - Ensuring data consistency across the schema
    - Resolving type inheritance and foreign key relationships
    """

    def __init__(self) -> None:
        """
        Initialize the database schema manager.
        - self.pm: Plugin manager instance
        - self.cp: Command processor instance
        - self.types: Dictionary of all available types (built-in and custom)
        - self.tables: Dictionary of all defined tables
        - self.tables_list: Ordered list of table names
        - self.model: Module containing all models
        - self.models: Dictionary of all db models
        - self.engine: The instanced db engine
        - self.db_type: Database type: "sqlite", "postgresql", "mysql" and so on
        - self.multi_tenant_config: Multi-tenancy configuration
        - self.shared_tables: Set of tables that are shared (no tenant prefix)
        """
        self.pm: Optional[PluginsManager] = None
        self.cp: Optional[CommandProcessor] = None
        self.types: Dict[str, DbType] = {}
        self.tables: Dict[str, DbTable] = {}
        self.tables_list: List[str] = []
        self.model: Any = None
        self.models: Dict[str, Any] = {}
        self.engine: Any = None
        self.db_type: str = "unknown"
        self.multi_tenant_config: Dict[str, Any] = {}
        self.shared_tables: set = set()
        self.query_behaviors: List[Any] = []

    def add_query_behavior(self, behavior_class: Any) -> None:
        """
        Register a query behavior class.

        A behavior class must implement:
          applies_to(model_class) -> bool
          apply(model_class, query_def, query) -> query

        Called at startup after calc_db():
          app.add_query_behavior(Archivable)
        """
        self.query_behaviors.append(behavior_class)

    def calc_db(self, plugins: PluginsManager) -> None:
        """
        Build the complete database schema from plugin definitions.

        Process flow:
        1. Load all type definitions
        2. Create table structures
        3. Materialize the columns a `many_to_many:` declaration implies
        4. Process and validate column definitions
        5. register all endpoints from plugins and from package
        6. Load multi-tenant configuration

        Args:
            plugins: Instance containing all loaded plugins
        """
        self.pm = plugins
        self._calc_types()
        self._calc_tables()
        self._calc_junctions()
        self._calc_columns()
        self._calc_endpoints()
        self._load_multi_tenant_config()

    def _calc_types(self) -> None:
        """
        Process and validate all type definitions. This includes:
        1. Loading built-in SQLAlchemy types
        2. Processing custom types from plugins
        3. Resolving type inheritance hierarchies

        Raises:
            ValueError: If a type is redefined across plugins
        """
        # Load built-in SQLAlchemy types
        self.types = {}
        type_classes = [obj for name, obj in inspect.getmembers(sqlalchemy.types) if isinstance(obj, type)]

        for t in type_classes:
            try:
                py_type = t().python_type
                self.types[t.__name__] = DbType(t.__name__, "", python_type=py_type)
            except Exception:
                # Skip types that don't have a python_type equivalent
                continue

        # Process plugin-defined types
        for name in self.pm.sorted:
            plugin = self.pm.plugins[name]
            for data in plugin.data:
                types = data.get('types', {})
                for type_name, value in types.items():
                    if type_name in self.types:
                        raise ValueError(f"Type already defined: {type_name}")
                    self.types[type_name] = DbType(type_name, plugin, attributes=value)

        # Resolve inheritance relationships
        for type_name in self.types:
            self.types[type_name].resolve(self.types)

    def _calc_tables(self) -> None:
        """
        Process all table definitions from plugins.

        Handles:
        - Table creation using merged plugin data
        - Maintaining table order
        """
        self.tables = {}
        self.tables_list = []

        # Use merged data instead of individual plugin data
        # The merge handlers have already combined columns with the same name
        tables = self.pm.data.get('tables', {})
        for table_name, value in tables.items():
            # Skip metadata keys (those starting with $)
            if table_name.startswith('$'):
                continue

            # Get the plugin that defined/last modified this table
            plugin_name = value.get('$plugin', 'unknown')
            plugin = self.pm.plugins.get(plugin_name)

            table = DbTable(table_name, plugin, value, self)
            self.tables[table_name] = table
            self.tables_list.append(table_name)

    def _calc_junctions(self) -> None:
        """
        Write out the columns a `many_to_many:` declaration implies.

        The declaration names two targets and the columns that reach them, and a
        junction row is identified like any other record: those three columns are
        materialized here as ordinary definitions, before columns are resolved, so
        that everything downstream — type resolution, foreign keys,
        effective_columns, the generated model, the schema the client receives —
        sees a table like all the others. Doing it later, in the code generator,
        is what used to leave a junction with columns SQLAlchemy knew about and
        the schema layer did not: no addressable key, no fields in an auto-form.

        Two passes, so that a junction pointing at another junction does not
        depend on the order tables happen to be declared in.
        """
        schema_cfg = self.pm.config.get('schema', {}) if self.pm else {}
        pk_name = schema_cfg.get('pk_name', 'id')

        junctions = [t for t in self.tables.values() if t.attributes.get('many_to_many')]
        for table in junctions:
            table.materialize_junction_key(self, pk_name)
        for table in junctions:
            table.materialize_junction_targets(self)

    def _calc_columns(self) -> None:
        """
        Process and validate all column definitions.

        Handles:
        1. Resolving column types for composite types
        2. Processing table columns and their relationships
        3. Validating foreign key references
        4. Checking for column name duplicates

        Raises:
            ValueError: If duplicate column names are found or invalid foreign keys are referenced
        """
        # Process composite type columns (includes mixin types)
        for type_name in self.types:
            for column in self.types[type_name].attributes.get('columns', []):
                col = DbColumn(column, self)
                col.resolve(f"type: {type_name}")
                if col.attributes.get('virtual'):
                    self.types[type_name].virtual_columns.append(col)
                else:
                    self.types[type_name].columns.append(col)

        # Process table columns
        for table_name in self.tables:
            for column in self.tables[table_name]._columns:
                col = DbColumn(column, self)
                col.resolve(f"table: {table_name}")

                # Handle composite types
                if col.db_type and col.db_type.columns:
                    prefix = column.get('prefix', "")
                    shared = _shared_composite_attrs(column)
                    for type_column in col.db_type.columns:
                        composed_col = DbColumn({**shared, **type_column.attributes}, self)
                        composed_col.name = prefix + composed_col.name
                        composed_col.resolve(f"table: {table_name}")
                        self.tables[table_name].columns.append(composed_col)
                elif col.attributes.get('virtual'):
                    self.tables[table_name].virtual_columns.append(col)
                else:
                    self.tables[table_name].columns.append(col)

                # Check for duplicate column names after composite types integration
                for i, c1 in enumerate(self.tables[table_name].columns):
                    for c2 in self.tables[table_name].columns[i + 1:]:
                        if c1.name == c2.name:
                            raise ValueError(f'Duplicated column "{c1.name}" in table "{table_name}"')

            # No need for _column variable anymore
            delattr(self.tables[table_name], '_columns')

        # Resolve foreign keys and many to many relationships
        for table_name in self.tables:
            for col in self.tables[table_name].columns:
                col.resolve_foreign(f"table: {table_name}")
            self.tables[table_name].resolve_m2m(self)

    def _calc_endpoints(self) -> None:
        """
        Resolve all endpoints coming from plugins and the ones defined in package
        """
        self.cp = CommandProcessor()
        # The package's own endpoints come with the processor; this loads what
        # the plugins declare. (The previous line here passed the bare string
        # 'endpoint_db.py', which iterates as its characters and loaded
        # nothing — the built-ins were arriving by accident.)
        self.cp.resolve_endpoints(self.pm.get_sources())

    def _load_multi_tenant_config(self) -> None:
        """
        Load multi-tenancy configuration from PluginManager config.

        If 'multi_tenant' section is not present in config.yaml,
        the system works in standard single-tenant mode.
        """
        self.multi_tenant_config = self.pm.config.get('multi_tenant', {})
        self.shared_tables = set(self.multi_tenant_config.get('shared_tables', []))

    def get_table_name(self, model_name: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Get the actual table name based on model name and context.

        Supports multi-tenancy with tenant prefixes (e.g., 'data_orders', 'test_customers').

        Args:
            model_name: The model class name (e.g., "Order")
            context: User context with optional tenant_prefix

        Returns:
            Actual table name (e.g., "data_orders" or "orders")

        Example:
            >>> app.get_table_name('Order', {'tenant_prefix': 'data'})
            'data_orders'

            >>> app.get_table_name('Config', {'tenant_prefix': 'data'})
            'config'  # Config is shared, no prefix
        """
        # Get base table name from model definition
        table_def = self.tables.get(model_name)
        if not table_def:
            return None

        base_table_name = table_def.table_name

        # Check if multi-tenancy is enabled
        if not self.multi_tenant_config.get('enabled', False):
            return base_table_name

        # Check if this table is shared (no prefix)
        if model_name in self.shared_tables:
            return base_table_name

        # Apply tenant prefix if context has tenant_prefix
        if context and context.get('tenant_prefix'):
            tenant_prefix = context.get('tenant_prefix')
            return f"{tenant_prefix}_{base_table_name}"

        # Fallback to base name
        return base_table_name

    def find_model_class(self, table_name: str) -> Any:
        """
        Find the model class from the name of class,

        Args:
            table_name: the name to search

        Returns:
            The model class or None if not found
        """
        return self.models.get(table_name, None)

    def get_type_schema(self, include_builtin: bool = False) -> Dict[str, Any]:
        """
        Return all resolved types as a client-facing dict.

        Args:
            include_builtin: if True, include SQLAlchemy built-in types
                             (String, Integer, DateTime, …).
                             Default False: only plugin-defined types.

        Returns:
            { TypeName: DbType.to_client_dict() }

        The result is intentionally free of UI-specific concerns (no widget
        inference): each client applies its own rendering logic on top of
        the type metadata (inheritance chain, python_type, declared attrs).
        """
        return {
            name: t.to_client_dict(self.types)
            for name, t in self.types.items()
            if include_builtin or t.plugin != ""
        }

    def get_schema_registry(self) -> Dict[str, Any]:
        """Return all declared schemas from plugin YAML as client-facing dicts."""
        from kitebase.types import get_schema_registry
        return get_schema_registry(self.pm.data)

    def get_table_schema(self) -> Dict[str, Any]:
        """
        Return all tables with their effective_columns (real + mixin + virtual).
        Used by the client to know the full column list for each table.

        Returns:
            {
              TableName: {
                pk_fields: ['id'],           # the key, from the columns that declare it
                columns: [ {name, type, virtual, editable, label, ...} ],
                display_field: 'name',       # column to show in FK comboboxes
                search_fields: ['name'],     # columns a text search matches (ILIKE)
                search_pk: 'id',             # primary key, matched exactly (optional)
                mixins: [...],               # optional
                indexes: [{name, columns}],  # compound indexes, optional
              }
            }
        """
        result = {}
        for name, table in self.tables.items():
            cols = []
            for col in table.effective_columns:
                col_dict = {'name': col.name}
                for attr in ('type', 'label', 'virtual', 'editable', 'nullable', 'secret',
                             'default', 'deferred', 'index', 'unique', 'query_rank',
                             'granularity'):
                    if attr in col.attributes:
                        col_dict[attr] = col.attributes[attr]
                    elif hasattr(col, 'db_type') and col.db_type and attr == 'type':
                        col_dict['type'] = col.db_type.name
                fk = col.attributes.get('foreign_key')
                if fk and 'table' in fk:
                    col_dict['foreign_key'] = {'target': fk['table'].name, 'field': fk['id']}
                cols.append(col_dict)

            # The key is read from the columns that declare it — junctions
            # included, since a junction declares one like everyone else.
            pk_fields = [
                col.name for col in table.effective_columns
                if col.attributes.get('primary_key')
            ]

            table_dict: Dict[str, Any] = {
                'pk_fields': pk_fields,
                'columns': cols,
            }
            mixins = table.attributes.get('mixins', [])
            if mixins:
                table_dict['mixins'] = mixins

            # Compound indexes, as declared. What the client does with them is
            # suggest an order the database can serve without sorting everything
            # — a priority in a combo, never a restriction on what may be sorted.
            indexes = table.attributes.get('indexes', [])
            if indexes:
                table_dict['indexes'] = [
                    {'name': idx.get('name'), 'columns': idx.get('columns', [])}
                    for idx in indexes
                ]

            search = table.search_info
            if search['display_field']:
                table_dict['display_field'] = search['display_field']
            if search['search_fields']:
                table_dict['search_fields'] = search['search_fields']
            if search['search_pk']:
                table_dict['search_pk'] = search['search_pk']

            result[name] = table_dict
        return result

    def initialize_db(self, db_url: str, model: ModuleType,
                      create_all: bool = True, check_schema: bool = True) -> Any:
        """
        Initialize the database with the given connection URL, register the
        model module and build the models dictionary

        `create_all` adds the tables that do not exist yet; it never alters an
        existing one, which is what the schema check is for: it compares the
        database with the schema the plugins describe and, according to
        `migrations.on_startup` in config.yaml, stops the server ('error',
        the default), logs ('warn') or says nothing ('off').  See
        kitebase.schema_sync — the alignment itself is an explicit command.

        Args:
            db_url: Database connection URL for SQLAlchemy
            model: Module containing all models
            create_all: Create missing tables
            check_schema: Run the startup schema check (off for the CLI, which
                          needs to report the database as it actually is)

        Returns:
            The created engine instance
        """
        self.model = model
        self.models = {name: cls for name, cls in vars(self.model).items()
                       if isinstance(cls, type) and not name.startswith('_')}
        from sqlalchemy import create_engine
        engine = create_engine(self._resolve_db_url(db_url))
        if create_all:
            Base.metadata.create_all(engine)
        self.engine = engine
        self.db_type = self.get_database_type()

        if check_schema:
            from kitebase.schema_sync import check_on_startup
            policy = (self.pm.config.get('migrations') or {}).get('on_startup', 'error')
            check_on_startup(engine, Base.metadata, policy, logger=self.pm.logger)

        return engine

    def _resolve_db_url(self, db_url: str) -> str:
        """
        Anchor a file-backed database to the application directory.

        `sqlite:///data/app.sqlite` names a file, and a relative one would open
        — or silently create — a different database depending on where the
        process was started. Absolute URLs (`sqlite:////var/lib/...`), in-memory
        ones and every server-based dialect pass through untouched.

        Args:
            db_url: the URL as config.yaml declares it

        Returns:
            The URL with a relative sqlite path made absolute
        """
        prefix = 'sqlite:///'
        if not db_url.startswith(prefix) or self.pm is None:
            return db_url

        target = db_url[len(prefix):]
        if not target or target == ':memory:' or Path(target).is_absolute():
            return db_url

        return prefix + str(self.pm.resolve_path(target))

    def get_database_type(self) -> str:
        """
        Get the type of database being used.

        Returns:
            String representing database type ('sqlite', 'postgresql', 'mysql', etc.)
        """
        if not self.engine:
            return None

        connection_url = str(self.engine.url)

        if 'sqlite' in connection_url:
            return 'sqlite'
        elif 'postgresql' in connection_url or 'postgres' in connection_url:
            return 'postgresql'
        elif 'mysql' in connection_url:
            return 'mysql'
        elif 'mariadb' in connection_url:
            return 'mariadb'
        # add further db types if needed
        else:
            return 'unknown'

    @contextmanager
    def get_session(self, context: Dict[str, Any] = None) -> Iterator[Session]:
        """
        Context manager that provides a session for database operations.

        The session is automatically closed when the context is exited.
        If an exception occurs, the session is rolled back before being closed.

        Args:
            context: Optional context to apply for the life of the session

        Yields:
            SQLAlchemy session object
        """

        with ExitStack() as stack:
            # Scoped: the previous context comes back on exit whatever it was,
            # None included — see BaseApp.context().
            if context is not None:
                stack.enter_context(BaseApp.context(context))

            session_factory = sessionmaker(bind=self.engine)
            Session = scoped_session(session_factory)
            session = Session()
            try:
                yield session
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()


class DbType:
    """
    Represents a database column type, either built-in SQLAlchemy type or custom.

    Features:
    - Support for type inheritance
    - Custom attributes for extended type information
    - Automatic attribute inheritance from parent types
    """

    def __init__(self, name: str, plugin: Union[Plugin, str], attributes: Optional[Dict[str, Any]] = None,
                 python_type: Optional[object] = None) -> None:
        """
        Initialize a new type definition.

        Args:
            name: Type name
            plugin: Plugin that defines this type
            attributes: Type attributes and configuration
            python_type: Corresponding Python type
        """
        self.name: str = name
        self.plugin: Union[Plugin, str] = plugin
        self.python_type: Optional[object] = python_type
        self.attributes: Dict[str, Any] = attributes or {}
        self.inheritance: List[str] = []
        self.columns: List['DbColumn'] = []
        self.virtual_columns: List['DbColumn'] = []

    def resolve(self, types: Dict[str, 'DbType']) -> None:
        """
        Resolve type inheritance chain and merge attributes.

        Walks up the inheritance chain and merges attributes from parent types
        into the current type's attributes.

        Args:
            types: Dictionary of all available types

        Raises:
            ValueError: If an inherited type is not found
        """
        type_obj = self
        while 'base' in type_obj.attributes:
            if type_obj.name not in types:
                raise ValueError(f'Type "{type_obj.name}" declared in "{type_obj.plugin.name}" is not found')
            type_obj = types[type_obj.attributes['base']]
            self.inheritance.append(type_obj.name)
            # Child wins on conflict: only fill in keys not already set by the child.
            # deep_merge is intentionally NOT used here — its "last wins" semantics
            # is correct for plugin merging but wrong for type inheritance.
            for key, value in type_obj.attributes.items():
                if key == 'columns':
                    # A composite type inherits the columns of the one it derives
                    # from, and its own refine them by name — otherwise "child
                    # wins" would drop the parent's columns altogether, and a
                    # derived Address would have only what was added to it.
                    self.attributes['columns'] = merge_columns_by_name(
                        value, self.attributes.get('columns', []))
                elif key not in self.attributes:
                    self.attributes[key] = value

        self.python_type = type_obj.python_type

    def to_client_dict(self, all_types: Dict[str, 'DbType']) -> Dict[str, Any]:
        """
        Serialise this type for the frontend client.

        The server is intentionally agnostic about UI concerns (widgets, rendering).
        It exposes raw type metadata; each client applies its own widget mapping.

        Field-level attributes in view descriptors or model columns override
        these type-level defaults — the merge is client-side (field wins).

        Returns a dict with:
          inheritance  — resolution chain [direct_parent, grandparent, ...]
          base         — direct parent type name (first in inheritance)
          python_type  — Python type as string ('str', 'int', 'float', ...)
          builtin      — True if SQLAlchemy built-in, False if plugin-defined
          + all resolved YAML attributes: widget (if declared), label, help,
            nullable, length, precision, scale, validate, index, unique, ...
          + columns    — for composite types: sub-column list with their own attrs
        """
        result: Dict[str, Any] = {
            'inheritance': self.inheritance,
            'builtin': self.plugin == "",
        }
        if self.inheritance:
            result['base'] = self.inheritance[0]
        if self.python_type:
            try:
                result['python_type'] = self.python_type.__name__
            except AttributeError:
                pass

        # All resolved YAML attributes except structural keys already exposed above
        _skip = {'base', 'columns'}
        for key, value in self.attributes.items():
            if key not in _skip:
                result[key] = value

        # Sub-columns for composite types (TimeStamp, Address, Credentials, …)
        if self.columns:
            cols = []
            for col in self.columns:
                col_entry: Dict[str, Any] = {'name': col.name}
                for k, v in col.attributes.items():
                    if k not in ('plugin', 'name'):
                        col_entry[k] = v
                cols.append(col_entry)
            result['columns'] = cols

        return result


class DbTable:
    """
    Represents a database table with support for:
    - Multi-plugin table definitions
    - Column management
    - Table attributes and metadata
    """

    def __init__(self, name: str, plugin: Plugin, attributes: Dict[str, Any], db: 'DB' = None) -> None:
        """
        Initialize a new table definition.

        Args:
            name: Table class name
            plugin: Plugin that initially defines this table
            attributes: Table configuration and columns
            db: Parent DB instance (for mixin/type resolution)
        """
        self.db = db
        self.name: str = name
        self.table_name: str = attributes.get('name', name.lower())
        self.plugins: List[Plugin] = []
        self.attributes: Dict[str, Any] = {}
        self._columns: List[Dict[str, Any]] = []  # Temporary variable used to build columns
        self.columns: List[DbColumn] = []
        self.virtual_columns: List[DbColumn] = []
        self._effective_columns: Optional[List['DbColumn']] = None
        self._secret_columns: Optional[frozenset] = None
        self._search_info: Optional[Dict[str, Any]] = None

        self.update(attributes, plugin)

    @property
    def effective_columns(self) -> List['DbColumn']:
        """
        Full column list: real columns + mixin columns + virtual columns.
        Cached after first access. Used by serialization and schema export.
        source.py uses .columns only (no virtuals, no mixin expansion).
        """
        if self._effective_columns is None:
            cols: List[DbColumn] = list(self.columns)

            if self.db:
                # Expand mixin columns (real + virtual, inherited via Python class)
                for mixin_name in self.attributes.get('mixins', []):
                    if mixin_name in self.db.types:
                        mixin_type = self.db.types[mixin_name]
                        for col in mixin_type.columns + mixin_type.virtual_columns:
                            if not any(c.name == col.name for c in cols):
                                cols.append(col)

            # Append own virtual columns (hybrid_property, no mapped_column)
            for col in self.virtual_columns:
                if not any(c.name == col.name for c in cols):
                    cols.append(col)

            self._effective_columns = cols
        return self._effective_columns

    @property
    def secret_columns(self) -> frozenset:
        """
        Names of the columns this table never sends to a client (`secret: true`).

        Cached like effective_columns: the answer is fixed once the schema is
        built, and it is asked on every record serialised and every column a
        query names — almost always to be told the empty set.
        """
        if self._secret_columns is None:
            self._secret_columns = frozenset(
                col.name for col in self.effective_columns
                if col.attributes.get('secret'))
        return self._secret_columns

    @property
    def search_info(self) -> Dict[str, Any]:
        """
        What a text search on this table looks at.

        {
          'display_field': 'name',        # label column, or None
          'search_fields': ['name'],      # columns matched with ILIKE
          'search_pk': 'id',              # column matched exactly, or None
        }

        The same answer serves the FK combobox, the quick search box and the
        value widget of a rule on a foreign key: one text, in OR over a declared
        set of columns. Cached like effective_columns — the schema is fixed once
        built, and every keystroke of a search asks for it.

        The cascade (DATA_MODEL.md §4.4):

          [primary key, exact]  + [display fields, ILIKE] + [searchable: true, ILIKE]

        An explicit `search_fields` on the table replaces the display fields;
        the key and the `searchable` columns stay added. The key is dropped by
        `include_pk: false` on the table, or globally by
        `schema.include_pk_in_search: false`, and it only takes part when the
        table has a single-column key — a composite one has no value a user can
        type.

        Secret columns never enter: a search matching them answers whether a
        value is right, which is how a password is guessed one query at a time.
        Virtual columns stay out of what convention derives, having no column to
        compare in SQL; naming one explicitly is left to whoever knows their
        hybrid carries an SQL expression.
        """
        if self._search_info is None:
            self._search_info = self._resolve_search_info()
        return self._search_info

    def _resolve_search_info(self) -> Dict[str, Any]:
        """Resolve the search cascade once. See search_info."""
        schema_cfg = {}
        if self.db is not None and self.db.pm is not None:
            schema_cfg = self.db.pm.config.get('schema', {})
        convention = schema_cfg.get('display_field_names', ['name', 'title', 'description'])

        columns = self.effective_columns
        by_name = {col.name: col for col in columns}
        secrets = self.secret_columns

        def is_real(name: str) -> bool:
            col = by_name.get(name)
            return col is not None and not col.attributes.get('virtual', False)

        display_field = self.attributes.get('display_field')
        if not display_field:
            display_field = next((n for n in convention if n in by_name), None)

        explicit = self.attributes.get('search_fields')
        if explicit:
            fields = list(explicit) if isinstance(explicit, list) else [explicit]
        elif display_field and is_real(display_field):
            fields = [display_field]
        elif display_field:
            # A virtual display field has nothing to compare: fall back to the
            # first real string column, which is what the label is built from.
            fields = [
                col.name for col in columns
                if not col.attributes.get('virtual', False)
                and not col.attributes.get('primary_key', False)
                and getattr(col.db_type, 'python_type', None) is str
            ][:1]
        else:
            fields = []

        fields += [
            col.name for col in columns
            if col.attributes.get('searchable') and col.name not in fields
        ]
        fields = [name for name in fields if name not in secrets]

        pk_fields = [col.name for col in columns if col.attributes.get('primary_key')]
        include_pk = self.attributes.get('include_pk', schema_cfg.get('include_pk_in_search', True))
        search_pk = None
        if include_pk and len(pk_fields) == 1 and pk_fields[0] not in secrets:
            search_pk = pk_fields[0]

        return {
            'display_field': display_field,
            'search_fields': fields,
            'search_pk': search_pk,
        }

    def update(self, attributes: Dict[str, Any], plugin: Plugin) -> None:
        """
        Update table definition with additional attributes from a plugin.

        Args:
            attributes: New attributes to merge
            plugin: Plugin providing the updates
        """
        # Process columns. A table may declare none — a junction whose columns are
        # all implied by `many_to_many:` is the ordinary case.
        for column in attributes.get('columns', []):
            column['plugin'] = plugin
            self._columns.append(column)

        # Merge attributes
        deep_merge(self.attributes, attributes)

        self.plugins.append(plugin)
        # Remove processed columns from attributes
        self.attributes.pop('columns', None)

    def _declared_primary_key(self, db: DB) -> Optional[str]:
        """Name of the primary key this table declares, from a column or its type."""
        def is_key(column: Dict[str, Any]) -> bool:
            if 'primary_key' in column:
                return bool(column['primary_key'])
            declared_type = db.types.get(column.get('type'))
            return bool(declared_type and declared_type.attributes.get('primary_key'))

        for column in self._columns:
            if is_key(column):
                return column['name']
        # A mixin can carry the key too — its columns are the type's, unresolved
        # at this point but already merged along the type's own inheritance.
        for mixin_name in self.attributes.get('mixins', []):
            mixin = db.types.get(mixin_name)
            for column in (mixin.attributes.get('columns', []) if mixin else []):
                if is_key(column):
                    return column['name']
        return None

    def materialize_junction_key(self, db: DB, pk_name: str = 'id') -> None:
        """
        Give the junction a key of its own, unless it declares one.

        A junction with a key of one column is addressable like every other
        record — `db` can update or delete one row of it, a form can open it, a
        buffered collection can hold it. The pair stays unique (the index below),
        it just stops being the identity. The name comes from
        `schema.pk_name`, so an installation with another convention can say so;
        a single table out of convention declares its key instead, and then
        nothing is injected.

        The key is written before the target columns of every junction, so that
        one junction may reference another whatever the declaration order.
        """
        if self._declared_primary_key(db):
            return
        if any(col['name'] == pk_name for col in self._columns):
            raise ValueError(
                f"Junction table '{self.name}' declares a column named '{pk_name}' that is not "
                f"a primary key: it is the name the generated key would take. Either make it "
                f"the key (primary_key: true) or rename it."
            )
        self._columns.insert(0, {
            'name': pk_name, 'type': 'Integer', 'primary_key': True, 'autoincrement': True,
            'plugin': self.plugins[0] if self.plugins else None,
        })

    def materialize_junction_targets(self, db: DB) -> None:
        """
        Write the two columns that reach the targets, and the index that keeps
        one link per pair — which is what the composite key used to guarantee.

        Each column takes the *base* type of the key it points at, never the
        declared one: a key type carries `primary_key` (that is what `ID` is),
        and copying it would make these columns keys again.
        """
        m2m = self.attributes['many_to_many']
        declared = {col['name'] for col in self._columns}
        pair, injected = [], []

        for key in ('target1', 'target2'):
            target = m2m[key]
            name = target.get('column')
            reference = target.get('table')
            if not name or not isinstance(reference, str) or '.' not in reference:
                raise ValueError(
                    f"Table '{self.name}': many_to_many {key} needs a `table: Table.column` "
                    f"reference and a `column:` name")
            pair.append(name)
            if name not in declared:
                injected.append(
                    self._junction_target_column(db, name, *reference.split('.', 1)))

        # Right after the key: column order is what a generated form follows, and
        # the two columns the junction exists for come before whatever it carries.
        after_key = next(
            (i + 1 for i, col in enumerate(self._columns)
             if col['name'] == self._declared_primary_key(db)), 0)
        self._columns[after_key:after_key] = injected

        indexes = self.attributes.setdefault('indexes', [])
        if not any(list(idx.get('columns', [])) == pair for idx in indexes):
            indexes.append({
                'name': f"uq_{self.table_name}_{'_'.join(pair)}",
                'columns': pair,
                'unique': True,
                'description': 'one link per pair',
            })

    def _junction_target_column(self, db: DB, name: str,
                                target_table: str, target_key: str) -> Dict[str, Any]:
        """The foreign key column reaching one target of a junction."""
        foreign = db.tables.get(target_table)
        if foreign is None:
            raise ValueError(f"Table '{self.name}': many_to_many target table "
                             f"'{target_table}' does not exist")

        key = next((col for col in foreign._columns if col.get('name') == target_key), None)
        if key is None or not key.get('type'):
            raise ValueError(f"Table '{self.name}': many_to_many target "
                             f"'{target_table}.{target_key}' does not exist")

        resolved = db.types.get(key['type'])
        column: Dict[str, Any] = {
            'name': name,
            'type': resolved.inheritance[-1] if resolved and resolved.inheritance else key['type'],
            'nullable': False,
            'foreign_key': {'target': f'{target_table}.{target_key}'},
            'plugin': self.plugins[0] if self.plugins else None,
        }
        # Whatever shapes the key's type must shape the column pointing at it: a
        # varchar(8) key reached by an unbounded string is a comparison between
        # two different types. The column that declares it wins over the type,
        # which is the rule everywhere else.
        for arg in ('length', 'precision', 'scale', 'timezone'):
            value = key.get(arg, resolved.attributes.get(arg) if resolved else None)
            if value is not None:
                column[arg] = value
        return column

    def resolve_m2m(self, db: DB) -> None:
        """
        Resolve many-to-many relationship information.

        This method processes target tables in a many-to-many relationship,
        finding the referenced columns and their types.

        Args:
            db: Database schema manager instance

        Raises:
            ValueError: If the many-to-many relationship has invalid configuration
        """
        m2m = self.attributes.get('many_to_many', None)
        if not m2m:
            return

        def _resolve_target(target: Dict[str, Any]) -> None:
            table, id = target['table'].split('.')
            foreign = db.tables[table]
            target['table'] = foreign
            target['id'] = id

            # Find the referenced column type
            for col in foreign.columns:
                if id == col.name:
                    target['db_type'] = col.db_type
                    break
            if not target['db_type']:
                raise ValueError(f"Many to Many Column error in table: {self.name}")

        try:
            _resolve_target(m2m['target1'])
            _resolve_target(m2m['target2'])
        except Exception:
            raise ValueError(f"Many to Many error for table: {self.name}")


class DbColumn:
    """
    Represents a table or type column with support for:
    - Type resolution
    - Foreign key relationships
    - Column attributes
    """

    def __init__(self, attributes: Dict[str, Any], db: DB) -> None:
        """
        Initialize a new column.

        Args:
            attributes: Column configuration
            db: Database schema manager instance
        """
        self.db: DB = db
        self.attributes: Dict[str, Any] = attributes
        self.name: str = attributes['name']
        self.db_type: Optional[DbType] = None
        self.attr_field: Dict[str, Any] = {}
        self.attr_type: Dict[str, Any] = {}
        self.attr_other: Dict[str, Any] = {}

    def resolve(self, caller: str) -> None:
        """
        Resolve column type and relationships.

        Handles:
        - Basic type resolution
        - Foreign key relationships
        - Many-to-many relationships
        - Attribute inheritance from type definitions

        Args:
            caller: Context information for error messages

        Raises:
            ValueError: If type resolution fails or references are invalid
        """
        # Handle foreign key columns
        if 'foreign_key' in self.attributes:
            try:
                fk = self.attributes['foreign_key']
                table, id = fk['target'].split('.')
                foreign = self.db.tables[table]
                fk['table'] = foreign
                fk['id'] = id
            except Exception:
                raise ValueError(f"Foreign key for column: {self.name} in {caller} has invalid type")

            # For FK columns, split attributes NOW (before return)
            # This ensures nullable, index, etc. are captured
            field_keys = ['primary_key', 'autoincrement', 'unique', 'nullable', 'index', 'default', 'onupdate']
            type_keys = ['length', 'precision', 'scale', 'timezone']
            for key, value in self.attributes.items():
                if key in field_keys:
                    self.attr_field[key] = value
                elif key in type_keys:
                    self.attr_type[key] = value
                else:
                    self.attr_other[key] = value
            self._default_nullable()
            return

        cur_type = self.attributes['type']

        # Inherit attributes from type definition
        if cur_type in self.db.types:
            attr = self.db.types[cur_type].attributes
            for key, value in attr.items():
                if key not in self.attributes:
                    self.attributes[key] = value
            self.db_type = self.db.types[cur_type]

        # Split attributes by type (for non-FK columns, after type inheritance)
        field_keys = ['primary_key', 'autoincrement', 'unique', 'nullable', 'index', 'default', 'onupdate']
        type_keys = ['length', 'precision', 'scale', 'timezone']
        for key, value in self.attributes.items():
            if key in field_keys:
                self.attr_field[key] = value
            elif key in type_keys:
                self.attr_type[key] = value
            else:
                self.attr_other[key] = value

        self._default_nullable()

    def _default_nullable(self) -> None:
        """
        A column that says nothing about nullability is nullable.

        Written out instead of being left implicit. With the SQLAlchemy 2.0
        annotations a generated `Mapped[str]` means NOT NULL, which is the
        opposite of what the rest of the stack reads from an undeclared column:
        the schema sent to the client and the auto-form both take it as optional.

        It is also what lets plugins compose a table. A plugin adding a column to
        a shared table cannot know what the rows created by the others should put
        in it, so a column that arrives mandatory stops them from being created
        at all. Requiredness is declared — on the column, or by a type that
        carries the reason (`Description`, `Name`).

        Primary keys are left alone: they are never nullable.
        """
        if self.attr_field.get('primary_key'):
            return
        self.attr_field.setdefault('nullable', True)

    def resolve_foreign(self, caller: str) -> None:
        """
        Resolve foreign relationships. This is done after all columns resolution
        to avoid forward resolutions in case of many-to-many relationships.

        Args:
            caller: Context information for error messages

        Raises:
            ValueError: If type resolution fails or references are invalid
        """
        fk = self.attributes.get('foreign_key', None)
        if not fk:
            return
        foreign = fk['table']
        id = fk['id']

        # Find the referenced column type
        for col in foreign.columns:
            if id == col.name:
                self.db_type = col.db_type
                break
        if not self.db_type:
            raise ValueError(f"Column: {self.name} has invalid foreign reference")


class BaseApp:
    """
    Base class for all SQLAlchemy models.
    Provides access to the database schema information.

    Context management supports both threading (Flask, CLI sync)
    and asyncio (FastAPI, CLI async) execution models.
    """
    __kitebase_app__: DB = DB()

    # Dual-mode context storage
    _context_local = threading.local()  # For threading-based execution (Flask, WSGI)
    _context_var: contextvars.ContextVar = contextvars.ContextVar('app_context', default=None)  # (FastAPI, ASGI)

    @classmethod
    def get_context(cls):
        """
        Get current context (threading or asyncio aware).

        Tries async context first (FastAPI/asyncio), then falls back
        to thread-local storage (Flask/threading).

        Returns:
            Current context dictionary or None
        """
        # Try async context first (FastAPI, async CLI)
        ctx = cls._context_var.get()

        # Fallback to thread-local context (Flask, sync CLI)
        if ctx is None:
            ctx = getattr(cls._context_local, 'value', None)

        return ctx

    @classmethod
    def set_context(cls, context):
        """
        Set current context (writes to both backends for compatibility).

        Args:
            context: Context dictionary with user/tenant info
        """
        # Write to both backends for maximum compatibility
        cls._context_local.value = context
        cls._context_var.set(context)

    @classmethod
    @contextmanager
    def context(cls, context):
        """
        Set the context for the duration of the block, then restore it.

        The scoped form of set_context(), for code that runs outside an HTTP
        request — background threads, batch scripts, server-rendered pages of a
        host application — where nothing else sets the context on the way in.
        Restores unconditionally, including back to None: leaving a context
        behind means the next user of that thread inherits an identity nobody
        chose, and query behaviors filter by it.

        Args:
            context: Context dictionary with user/tenant info

        Yields:
            The context that was set
        """
        previous = cls.get_context()
        cls.set_context(context)
        try:
            yield context
        finally:
            cls.set_context(previous)

    # ==========================================
    # Model ↔ DB Definition Bridge
    # ==========================================

    @classmethod
    def get_table_definition(cls):
        """
        Get the DbTable definition for this model class.

        Returns:
            DbTable object with columns, attributes, plugins, etc.

        Example:
            >>> User.get_table_definition()
            <DbTable: User (table_name='users', columns=[...])>

            >>> table_def = User.get_table_definition()
            >>> table_def.attributes.get('label')
            'Utente'
        """
        table_name = cls.__name__
        return cls.__kitebase_app__.tables.get(table_name)

    @classmethod
    def get_column_definition(cls, column_name: str):
        """
        Get the DbColumn definition for a specific column.

        Args:
            column_name: Name of the column

        Returns:
            DbColumn object with type, attributes, constraints, etc.

        Example:
            >>> User.get_column_definition('username')
            <DbColumn: username (type=String, unique=True)>
        """
        table_def = cls.get_table_definition()
        if not table_def:
            return None

        for col in table_def.columns:
            if col.name == column_name:
                return col

        return None

    @classmethod
    def get_table_name(cls, context=None) -> str:
        """
        Get the actual table name for this model (with tenant prefix if applicable).

        Args:
            context: Optional context dict with tenant_prefix

        Returns:
            Actual table name (e.g., 'data_orders' or 'orders')

        Example:
            >>> context = {'tenant_prefix': 'data'}
            >>> Order.get_table_name(context)
            'data_orders'

            >>> Config.get_table_name(context)  # Shared table
            'config'
        """
        if context is None:
            context = cls.get_context()

        model_name = cls.__name__
        return cls.__kitebase_app__.get_table_name(model_name, context)

    @classmethod
    def get_plugins(cls) -> list:
        """
        Get list of plugins that contribute to this table.

        Returns:
            List of Plugin objects

        Example:
            >>> User.get_plugins()
            [<Plugin: base/users>, <Plugin: auth/extended_users>]
        """
        table_def = cls.get_table_definition()
        return table_def.plugins if table_def else []

    @classmethod
    def get_relationships(cls) -> dict:
        """
        Get all relationships (foreign keys and many-to-many) for this model.

        Returns:
            Dictionary with relationship metadata

        Example:
            >>> Order.get_relationships()
            {
                'foreign_keys': [
                    {'column': 'customer_id', 'target': 'Customer.id'}
                ],
                'many_to_many': {...}
            }
        """
        table_def = cls.get_table_definition()
        if not table_def:
            return {}

        relationships = {
            'foreign_keys': [],
            'many_to_many': table_def.attributes.get('many_to_many')
        }

        for col in table_def.columns:
            if 'foreign_key' in col.attributes:
                fk = col.attributes['foreign_key']
                relationships['foreign_keys'].append({
                    'column': col.name,
                    'target': f"{fk['table'].name}.{fk['id']}"
                })

        return relationships

    def get_column_value_with_metadata(self, column_name: str) -> dict:
        """
        Get column value with its metadata (instance method).

        Args:
            column_name: Name of the column

        Returns:
            Dictionary with value and metadata

        Example:
            >>> user = User(username='mario', email='mario@example.com')
            >>> user.get_column_value_with_metadata('email')
            {
                'value': 'mario@example.com',
                'column_name': 'email',
                'type': 'String',
                'label': 'Email',
                'required': True
            }
        """
        col_def = self.get_column_definition(column_name)
        if not col_def:
            return None

        return {
            'value': getattr(self, column_name, None),
            'column_name': column_name,
            'type': col_def.db_type.name if col_def.db_type else None,
            'python_type': col_def.db_type.python_type.__name__ if col_def.db_type else None,
            'label': col_def.attributes.get('label', column_name),
            'description': col_def.attributes.get('description'),
            'required': not col_def.attr_field.get('nullable', True),
            'unique': col_def.attr_field.get('unique', False),
            'primary_key': col_def.attr_field.get('primary_key', False),
        }


Base = declarative_base(cls=BaseApp)
