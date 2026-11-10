"""
The database an application runs on.

The one part that knows it: the engine, the generated models, and the single
place every session comes from.
"""
from contextlib import contextmanager, ExitStack
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, Optional

from sqlalchemy.orm import Session, sessionmaker

import kitebase.context
from kitebase.model_base import Base
from kitebase.plugins import PluginsManager


class Store:
    """
    The database of a running application: the engine, the models generated
    from the schema, and the sessions over them.

    - self.pm: the plugins, for the configuration and the paths
    - self.model: the module of the generated models
    - self.models: the model classes, by name
    - self.engine: the engine, once the database is open
    - self.db_type: "sqlite", "postgresql", "mysql" and so on
    """

    def __init__(self, plugins: Optional[PluginsManager] = None) -> None:
        self.pm: Optional[PluginsManager] = plugins
        self.model: Any = None
        self.models: Dict[str, Any] = {}
        self._engine: Any = None
        self._sessions: sessionmaker = sessionmaker()
        self.db_type: str = "unknown"

    @property
    def engine(self) -> Any:
        return self._engine

    @engine.setter
    def engine(self, engine: Any) -> None:
        # The factory follows the engine: one per engine, not one per session.
        self._engine = engine
        self._sessions = sessionmaker(bind=engine)

    def find_model_class(self, table_name: str) -> Any:
        """
        Find the model class from the name of class,

        Args:
            table_name: the name to search

        Returns:
            The model class or None if not found
        """
        return self.models.get(table_name, None)

    def open(self, db_url: str, model: ModuleType,
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
    def session(self, context: Dict[str, Any] = None) -> Iterator[Session]:
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
