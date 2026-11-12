from types import ModuleType
from typing import Any, Dict, List, Optional

import kitebase.access
import kitebase.media
import kitebase.refsets
from kitebase.endpoints import INHERIT, CommandDispatcher
from kitebase.model_base import BaseApp
from kitebase.plugins import PluginsManager
from kitebase.schema import DbTable, DbType, Schema
from kitebase.store import Store


class App:
    """
    The application: what its plugins declare, and the database it runs on.

    The declared part is the schema (kitebase.schema), read from the merged
    tree and then left alone; `types`, `tables` and `tables_list` are its.
    The database is the store (kitebase.store): the engine, the models
    generated from the schema and the sessions over them; `engine`, `models`
    and `get_session()` are its. Beside them, the dispatcher and the media.
    """

    def __init__(self) -> None:
        """
        - self.plugins: Plugin manager instance
        - self.schema: The declared types and tables
        - self.store: The database: engine, models, sessions
        - self.dispatcher: CommandDispatcher instance
        """
        self.plugins: Optional[PluginsManager] = None
        self.schema: Schema = Schema()
        self.dispatcher: Optional[CommandDispatcher] = None
        self.store: Store = Store()
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

    # The store, likewise.

    @property
    def engine(self) -> Any:
        return self.store.engine

    @engine.setter
    def engine(self, engine: Any) -> None:
        self.store.engine = engine

    @property
    def model(self) -> Any:
        return self.store.model

    @model.setter
    def model(self, model: Any) -> None:
        self.store.model = model

    @property
    def models(self) -> Dict[str, Any]:
        return self.store.models

    @models.setter
    def models(self, models: Dict[str, Any]) -> None:
        self.store.models = models

    @property
    def db_type(self) -> str:
        return self.store.db_type

    @db_type.setter
    def db_type(self, db_type: str) -> None:
        self.store.db_type = db_type

    def initialize_db(self, db_url: str, model: ModuleType,
                      create_all: bool = True, check_schema: bool = True) -> Any:
        """Open the database on the generated models: see Store.open()."""
        return self.store.open(db_url, model, create_all=create_all, check_schema=check_schema)

    def get_session(self, context: Optional[Dict[str, Any]] = None) -> Any:
        """A session, as a context manager: see Store.session()."""
        return self.store.session(context)

    def find_model_class(self, table_name: str) -> Any:
        """The model class of a table, by model name, or None."""
        return self.store.find_model_class(table_name)

    def get_database_type(self) -> str:
        return self.store.get_database_type()

    def call(self, operation: str, parameters: Optional[Dict[str, Any]] = None,
             context: Any = INHERIT) -> Dict[str, Any]:
        """Call an endpoint from server code, as the caller: see CommandDispatcher.call()."""
        return self.dispatcher.call(operation, parameters, context)

    def _resolve_db_url(self, db_url: str) -> str:
        return self.store._resolve_db_url(db_url)

    def add_query_behavior(self, behavior_class: Any) -> None:
        """
        Register a query behavior class.

        A behavior class must implement:
          applies_to(model_class) -> bool
          apply(model_class, query_def, query) -> query

        Called at startup after calc_db():
          app.add_query_behavior(Archivable)
        """
        if behavior_class not in self.query_behaviors:
            self.query_behaviors.append(behavior_class)

    # ── Starting up, in three stages ─────────────────────────────────────
    #
    #     app = kitebase.utils.get_app()
    #     app.declare("config.yaml")     what the plugins declare
    #     app.generate()                 model.py, when the declarations moved
    #     app.open(model)                the database
    #
    # Each is enough for some use: `check` and the inspector stop after the
    # first, a server goes through all three.

    def declare(self, config: Any = "config.yaml") -> 'App':
        """
        Stage one: the plugins named by `config`, composed, and what they
        declare: schema, media, their Python code, the dispatcher. No database
        and no generated model, which is all the introspection commands need.

        Args:
            config: Path of the application's config.yaml

        Returns:
            The app itself
        """
        plugins = PluginsManager()
        plugins.load_config(config)
        plugins.load_plugins()
        self.calc_db(plugins)
        return self

    def generate(self, filename: str = "model.py") -> bool:
        """
        Stage two: write the models, if the declarations or the generator
        changed since the file was written.

        Returns:
            True if the file was written
        """
        if not self.plugins.should_regenerate(filename):
            return False
        from kitebase.source import Generator
        Generator(self).generate(filename=filename)
        return True

    def open(self, model: ModuleType, create_all: bool = True,
             check_schema: bool = True) -> Any:
        """
        Stage three: the database the configuration names (`db_engine`), on
        the generated models, and the translations.

        Args:
            model: The module of the generated models
            create_all: Create missing tables
            check_schema: Run the startup schema check

        Returns:
            The engine
        """
        engine = self.initialize_db(self.plugins.config["db_engine"], model,
                                    create_all=create_all, check_schema=check_schema)
        self.plugins.load_all_locales()
        return engine

    def calc_db(self, plugins: PluginsManager) -> None:
        """
        Build the application from plugins already loaded: the schema, the
        media, then the Python code of the plugins and the dispatcher over it.
        `declare()` is this, starting from the path of a config.yaml.

        Args:
            plugins: Instance containing all loaded plugins
        """
        self.plugins = plugins
        self.store.plugins = plugins
        self.schema = Schema()
        self.schema.calc(plugins)
        self.media = kitebase.media.Media(self.plugins.config.get('media'), self.plugins.resolve_media_path)
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
        if self.plugins.engine_name is None or not self.media.roots:
            return
        base = self.plugins.media_base()
        if not base.exists():
            self.plugins.logger.warning(
                f"Media of engine '{self.plugins.engine_name}': {base} does not exist, "
                "file references will not resolve (db-copy --media copies them)")

    def _calc_endpoints(self) -> None:
        """
        Load the Python code of the plugins, then the dispatcher over what the
        package and the plugins registered.
        """
        self.plugins.load_modules()
        self.dispatcher = CommandDispatcher()

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
        return get_schema_registry(self.plugins.data)

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


# The application of this process, where the models and get_app() find it.
BaseApp.__kitebase_app__ = App()
