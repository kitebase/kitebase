from pathlib import Path
from typing import Dict, List, Any, Optional, Iterator
from types import ModuleType
from contextlib import contextmanager, ExitStack
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker, Session
import kitebase.access
import kitebase.context
import kitebase.media
import kitebase.refsets
from kitebase.plugins import PluginsManager
from kitebase.endpoints import CommandProcessor
from kitebase.schema import (  # noqa: F401
    DbColumn, DbTable, DbType, RefSet, Schema, merge_columns_by_name,
)
from kitebase.sqltypes import (  # noqa: F401
    CODESET_DELIMITER, CaseString, CodeSet, codeset_codes, codeset_text,
)


class DB:
    """
    The application: what its plugins declare, and the database it runs on.

    The declared part is the schema (kitebase.schema), read from the merged
    tree and then left alone; `types`, `tables` and `tables_list` are its.
    The rest is what runs: the dispatcher, the media, the engine, the models
    generated from the schema and the sessions over them.
    """

    def __init__(self) -> None:
        """
        - self.pm: Plugin manager instance
        - self.schema: The declared types and tables
        - self.cp: Command processor instance
        - self.model: Module containing all models
        - self.models: Dictionary of all db models
        - self.engine: The instanced db engine
        - self.db_type: Database type: "sqlite", "postgresql", "mysql" and so on
        """
        self.pm: Optional[PluginsManager] = None
        self.schema: Schema = Schema()
        self.cp: Optional[CommandProcessor] = None
        self.model: Any = None
        self.models: Dict[str, Any] = {}
        self._engine: Any = None
        self._sessions: sessionmaker = sessionmaker()
        self.db_type: str = "unknown"
        self.query_behaviors: List[Any] = []
        self.media: Optional[kitebase.media.Media] = None

    # The schema, by the names everything reads it with.

    @property
    def types(self) -> Dict[str, DbType]:
        return self.schema.types

    @types.setter
    def types(self, types: Dict[str, DbType]) -> None:
        self.schema.types = types

    @property
    def tables(self) -> Dict[str, DbTable]:
        return self.schema.tables

    @tables.setter
    def tables(self, tables: Dict[str, DbTable]) -> None:
        self.schema.tables = tables

    @property
    def tables_list(self) -> List[str]:
        return self.schema.tables_list

    @tables_list.setter
    def tables_list(self, names: List[str]) -> None:
        self.schema.tables_list = names

    @property
    def engine(self) -> Any:
        return self._engine

    @engine.setter
    def engine(self, engine: Any) -> None:
        # The factory follows the engine: one per engine, not one per session.
        self._engine = engine
        self._sessions = sessionmaker(bind=engine)

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
        Build the application from the plugin definitions: the schema, the
        media, then the Python code of the plugins and the dispatcher over it.

        Args:
            plugins: Instance containing all loaded plugins
        """
        self.pm = plugins
        self.schema = Schema()
        self.schema.calc(plugins)
        self.media = kitebase.media.Media(self.pm.config.get('media'), self.pm.resolve_media_path)
        self._warn_missing_media_base()
        self._calc_endpoints()
        # After the endpoints: a policy plugin registers its guard on import.
        kitebase.access.bind(self)

    def _warn_missing_media_base(self) -> None:
        """
        An engine other than the default whose media folder does not exist
        still runs: the rows are there, the files they refer to are not, so the
        references do not resolve. Said once at startup, not discovered later.
        """
        if self.pm.engine_name is None or not self.media.roots:
            return
        base = self.pm.media_base()
        if not base.exists():
            self.pm.logger.warning(
                f"Media of engine '{self.pm.engine_name}': {base} does not exist, "
                "file references will not resolve (db-copy --media copies them)")

    def _calc_endpoints(self) -> None:
        """
        Load the Python code of the plugins, then the dispatcher over what the
        package and the plugins registered.
        """
        self.pm.load_modules()
        self.cp = CommandProcessor()

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
                             'granularity', 'choices_endpoint'):
                    if attr in col.attributes:
                        col_dict[attr] = col.attributes[attr]
                    elif hasattr(col, 'db_type') and col.db_type and attr == 'type':
                        col_dict['type'] = col.db_type.name
                fk = col.attributes.get('foreign_key')
                if fk and 'table' in fk:
                    col_dict['foreign_key'] = {'target': fk['table'].name, 'field': fk['id']}
                if col.attributes.get('media'):
                    col_dict.update(kitebase.media.column_schema(self.media, col.attributes))
                cols.append(col_dict)
            cols.extend(kitebase.refsets.field(self, ref) for ref in table.ref_sets.values())

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
        url = self._resolve_db_url(db_url)
        self._log_engine(db_url, url)
        engine = create_engine(url)
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

        A name instead of a URL (no `://`) is an entry of secrets/engines.yaml,
        read here and nowhere else, so the credentials never sit in the config.

        Args:
            db_url: the URL or the engine name, as config.yaml declares it

        Returns:
            The URL with a relative sqlite path made absolute
        """
        if not db_url:
            raise ValueError("No database engine: config.yaml declares no 'db_engine'")
        if '://' not in db_url:
            from kitebase import secrets
            resolved = secrets.get('engines', db_url)
            if not isinstance(resolved, str) or '://' not in resolved:
                raise ValueError(
                    f"Engine '{db_url}' in {secrets.root() / 'engines.yaml'} "
                    "must be a database URL")
            db_url = resolved

        prefix = 'sqlite:///'
        if not db_url.startswith(prefix) or self.pm is None:
            return db_url

        target = db_url[len(prefix):]
        if not target or target == ':memory:' or Path(target).is_absolute():
            return db_url

        return prefix + str(self.pm.resolve_path(target))

    def _log_engine(self, declared: str, url: str) -> None:
        """
        One line at startup: which engine, and who chose it. A wrong engine
        shows here rather than as data that is missing. The password never
        appears: a name is printed as is, a URL masked.
        """
        if self.pm is None:
            return
        from sqlalchemy.engine import make_url
        masked = make_url(url).render_as_string(hide_password=True)
        source = getattr(self.pm, 'db_engine_source', 'config.yaml')
        if '://' in declared:
            self.pm.logger.info(f"Database engine: {masked} (from {source})")
        else:
            from kitebase import secrets
            self.pm.logger.info(
                f"Database engine: {declared} = {masked} "
                f"({secrets.root() / 'engines.yaml'}, chosen by {source})")

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
            # None included — see kitebase.context.use().
            if context is not None:
                stack.enter_context(kitebase.context.use(context))

            session = self._sessions()
            try:
                yield session
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()


class BaseApp:
    """
    Base class for all SQLAlchemy models.
    Provides access to the database schema information.

    The context of the request is kitebase.context: the three methods below
    are aliases of its verbs.
    """
    __kitebase_app__: DB = DB()

    @classmethod
    def get_context(cls):
        return kitebase.context.current()

    @classmethod
    def set_context(cls, context):
        kitebase.context.replace(context)

    @classmethod
    def context(cls, context):
        return kitebase.context.use(context)

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
