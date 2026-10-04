import inspect
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Any, Tuple
from kitebase.db import DB, DbColumn, DbTable


class ModelImportManager:
    """
    Manages imports for SQLAlchemy model generation.
    Collects and organizes different types of imports.
    """

    def __init__(self, db: DB) -> None:
        """
        Initialize import manager.

        Args:
            db: Database schema manager instance
        """
        self.db = db
        self.standard_imports: Set[str] = set()  # Python standard imports
        self.column_imports: Set[str] = set()  # SQLAlchemy Column types
        self.orm_imports: Set[str] = {"Mapped", "mapped_column"}  # SQLAlchemy ORM features
        self.kitebase_imports: Set[str] = {"Base", "BaseApp"}  # names taken from kitebase.db

        # Add configured imports from plugins
        self._add_configured_imports()

    def _add_configured_imports(self) -> None:
        """Add imports configured in plugins."""
        # Add global imports from main config
        for imp in self.db.pm.config.get('source_imports', []):
            self.standard_imports.add(imp)

        # Add imports from individual plugins
        for plugin_name, plugin_data in self.db.pm.plugins.items():
            for imp in plugin_data.config.get('source_imports', []):
                self.standard_imports.add(imp)

    def add_python_type_import(self, python_type: str) -> None:
        """
        Add import for Python type.

        Args:
            python_type: Name of Python type to import
        """
        if python_type == 'datetime':
            self.standard_imports.add("from datetime import datetime")
        elif python_type == 'date':
            self.standard_imports.add("from datetime import date")
        elif python_type == 'time':
            self.standard_imports.add("from datetime import time")
        elif python_type == 'Decimal':
            self.standard_imports.add("from decimal import Decimal")

    def add_relationship_imports(self) -> None:
        """Add imports needed for relationship definitions."""
        self.orm_imports.add("relationship")
        self.standard_imports.add("from typing import List")

    def generate_import_statements(self) -> str:
        """
        Generate formatted import statements.

        Returns:
            String with all import statements
        """
        import_statements = []

        # Add standard imports
        if self.standard_imports:
            import_statements.append('\n'.join(sorted(self.standard_imports)))
            import_statements.append('')

        # Add SQLAlchemy Column imports
        if self.column_imports:
            import_statements.append(f"from sqlalchemy import {', '.join(sorted(self.column_imports))}")

        # Add SQLAlchemy ORM imports
        if self.orm_imports:
            import_statements.append(f"from sqlalchemy.orm import {', '.join(sorted(self.orm_imports))}")

        # Add declared_attr for dynamic __tablename__
        import_statements.append('from sqlalchemy.ext.declarative import declared_attr')

        # Add Base import and kitebase utilities
        import_statements.append(f"from kitebase.db import {', '.join(sorted(self.kitebase_imports))}")
        import_statements.append('from kitebase.utils import resolve_table_name')
        import_statements.append('import kitebase')

        return '\n'.join(import_statements)


class PluginClassFinder:
    """
    Finds and imports Python classes from plugin source files
    that match table names for inheritance.
    """

    def __init__(self, plugins_manager):
        """
        Initialize the class finder with the plugins manager.

        Args:
            plugins_manager: The plugins manager instance
        """
        self.pm = plugins_manager
        self.class_map: Dict[str, List[Tuple[str, str]]] = {}  # Table name -> [(module_path, class_name)]
        self.imported_modules: Dict[str, object] = {}  # Module path -> module object

    def scan_plugin_sources(self) -> None:
        """
        Scan all plugin source files for classes that match table names.
        """
        for plugin_name, plugin in self.pm.plugins.items():
            for source_file in plugin.sources:
                self._process_source_file(source_file, plugin)

    def _process_source_file(self, source_file: Path, plugin) -> None:
        """
        Process a single source file to find relevant classes.

        Args:
            source_file: Path to the source file
            plugin: Plugin object that owns the source file
        """

        # Construct the module path relative to the plugin's root.
        # load_plugins() puts each plugin *root* on sys.path, so every plugin is
        # importable by its own folder name — regardless of where that root lives
        # on disk (this is what makes out-of-tree roots like ../commons work).
        # Deriving the module from the full filesystem path instead would produce
        # invalid names for out-of-tree roots (e.g. '..commons.common.model').
        module_path = f"{source_file.parent.name}.{source_file.stem}"

        try:
            # Import the module
            if module_path not in self.imported_modules:
                self.imported_modules[module_path] = importlib.import_module(module_path)

            module = self.imported_modules[module_path]

            # Scan for classes
            for name, obj in inspect.getmembers(module, inspect.isclass):
                # Check if class is defined in this module (not imported)
                if obj.__module__ == module_path:
                    # Add to class map for potential table matching
                    if name not in self.class_map:
                        self.class_map[name] = []
                    self.class_map[name].append((module_path, name))

        except (ImportError, AttributeError) as e:
            print(f"Error importing module {module_path}: {e}")

    def get_class_inheritance(self, table_name: str) -> List[str]:
        """
        Get the inheritance paths for a table.

        Args:
            table_name: Name of the table to find inheritance for

        Returns:
            List of fully qualified class paths for inheritance
        """
        result = []
        if table_name in self.class_map:
            for module_path, class_name in self.class_map[table_name]:
                result.append(f"{module_path}.{class_name}")
        return result


@dataclass
class ForeignKeyRelation:
    """
    One foreign key, collected while columns are generated and turned into a pair
    of relationship() attributes only once every table has been seen.

    The deferral is what makes disambiguation possible: how a relationship must be
    named — and whether the join needs spelling out — depends on the *other* foreign
    keys in the model, which are not known while a single column is being written.
    """
    table: DbTable            # table that owns the FK column
    column: str               # the FK column
    fk_table: DbTable         # referenced table
    fk_id: str                # referenced column
    soft: bool                # no DB-level constraint (see § 4.5)
    relation: Optional[str]   # explicit forward name from YAML, if given
    backref: Optional[str]    # explicit back name from YAML, if given
    owned: bool = False       # the row does not exist without its parent (see § 4.5)

    @property
    def self_referential(self) -> bool:
        return self.table.name == self.fk_table.name


@dataclass
class ManyToManyRelation:
    """
    One junction table, with its two targets.

    A junction declared explicitly (rather than conjured from a column) is what
    lets the relation carry data of its own — `BookAuthor.notes`. It costs six
    attributes instead of two: each target gets the rows of the junction and a
    shortcut to the other target, and the junction gets a scalar to each target.
    """
    junction: DbTable
    target1: Dict[str, Any]   # {table, id, column, relation?, backref?, collection?}
    target2: Dict[str, Any]


class RelationshipManager:
    """
    Manages relationships between tables for model generation.

    Every foreign key produces two Python attributes: the *forward* one on the table
    that holds the column (`Loan.book`, many-to-one) and the *back* one on the table
    referenced (`Book.loans`, one-to-many). Neither exists in SQL — only the column
    and its constraint do — so their names are free, and are chosen here.
    """

    def __init__(self) -> None:
        """Initialize relationship manager."""
        self.direct_relations: Dict[str, List[str]] = {}  # Table name -> list of relation statements
        self.back_relations: Dict[str, List[str]] = {}  # Table name -> list of backref statements
        self.foreign_keys: List[ForeignKeyRelation] = []
        self.many_to_many: List[ManyToManyRelation] = []
        self._resolved = False
        # class name -> attribute name -> what claims it. Filled while resolving,
        # then checked: two claims on one name would mean a silent overwrite.
        self._claims: Dict[str, Dict[str, str]] = {}

    def add_foreign_key_relation(self, table: DbTable, column_name: str, fk_table: DbTable,
                                 fk_id: str = 'id', soft: bool = False,
                                 relation: Optional[str] = None,
                                 backref: Optional[str] = None,
                                 owned: bool = False) -> None:
        """
        Record a foreign key. The code is emitted later, by resolve().

        Args:
            table: Table owning the foreign key column
            column_name: Current column name
            fk_table: Foreign table object
            fk_id: Referenced column on the foreign table (default 'id')
            soft: If True the column has no ForeignKey constraint — the join must
                  be stated explicitly (primaryjoin) since SQLAlchemy can no longer
                  infer it from a constraint.
            relation: Explicit name for the forward attribute (YAML `relation:`)
            backref: Explicit name for the back attribute (YAML `backref:`)
            owned: If True the row is a part of its parent and does not outlive it —
                   the parent's collection cascades the delete (YAML `owned:`)
        """
        if self._resolved:
            # Only reachable if a FK column is generated outside the table pass —
            # a mixin type, today. Silently dropping it would leave a model with a
            # column and no way to navigate it.
            raise ValueError(
                f"Foreign key '{table.name}.{column_name}' was declared after relationships "
                f"were resolved: foreign keys are not supported on mixins."
            )
        self.foreign_keys.append(ForeignKeyRelation(
            table=table, column=column_name, fk_table=fk_table, fk_id=fk_id,
            soft=soft, relation=relation, backref=backref, owned=owned,
        ))

    def add_many_to_many(self, junction: DbTable, target1: Dict[str, Any],
                         target2: Dict[str, Any]) -> None:
        """Record a junction table. The code is emitted later, by resolve()."""
        if self._resolved:
            raise ValueError(
                f"Junction table '{junction.name}' was declared after relationships were resolved."
            )
        self.many_to_many.append(ManyToManyRelation(junction, target1, target2))

    def resolve(self, inherited: Optional[Dict[str, Set[str]]] = None) -> None:
        """
        Turn the collected relations into relationship() statements.

        Runs once, after every table has been processed: a name is refused when
        another one already claims it, and that can only be known at the end.

        Args:
            inherited: Public attribute names each generated class inherits from the
                       plugin Python classes it is built on. A relationship with one
                       of those names would shadow a method without a word.
        """
        if self._resolved:
            return
        self._resolved = True

        for fk in self.foreign_keys:
            forward = fk.relation or self._cut(fk.column, fk.fk_table)
            back = fk.backref or fk.table.table_name.lower()
            self._claim(fk.table, forward, f"the foreign key '{fk.table.name}.{fk.column}'")
            self._claim(fk.fk_table, back, f"the reverse of '{fk.table.name}.{fk.column}'")
            self._emit_foreign_key(fk, forward, back)

        for m2m in self.many_to_many:
            self._resolve_many_to_many(m2m)

        self._check_columns()
        self._check_inherited(inherited or {})

    def _cut(self, column: str, fk_table: DbTable) -> str:
        """
        Name a relationship after its column: the column without its key suffix.

        `book_id` -> `book`, `merged_into_id` -> `merged_into`, and any other suffix
        works the same way (`codice_esterno_fk` -> `codice_esterno`) because the cut
        is at the last underscore, not at a known list of suffixes.

        A column with nothing to cut — a legacy `codcli` — falls back to the name of
        the referenced table, which cannot clash with the column precisely because
        the column does not follow the convention.
        """
        name, separator, _ = column.rpartition('_')
        return name if separator and name else fk_table.name.lower()

    def _claim(self, owner: DbTable, attr: str, source: str) -> None:
        """
        Reserve an attribute name on a class, or refuse the model.

        Two claims on one name mean the second definition would overwrite the first
        in the class body — a relationship silently replacing a column, or one of two
        foreign keys disappearing. Note what is *not* done here: nothing is renamed to
        make room. A generated name never changes because of something declared
        elsewhere, so a plugin can never move another plugin's attribute from under
        the code that uses it; whoever arrives second names its own relation instead.
        """
        owned = self._claims.setdefault(owner.name, {})
        if attr in owned:
            raise ValueError(
                f"Relationship name clash on '{owner.name}': '{attr}' is claimed by "
                f"{owned[attr]} and by {source}. Name one of them explicitly — "
                f"`relation:`/`backref:` on a foreign key, "
                f"`relation:`/`backref:`/`collection:` on a many-to-many target."
            )
        owned[attr] = source

    def _check_columns(self) -> None:
        """A relationship may also collide with a real column of the same class."""
        for table_name, table in self._involved_tables().items():
            owned = self._claims.get(table_name, {})
            for column in table.effective_columns:
                if column.name in owned:
                    raise ValueError(
                        f"Relationship name clash on '{table_name}': '{column.name}' is both a "
                        f"column and {owned[column.name]}. Name the relationship explicitly "
                        f"with `relation:`, `backref:` or `collection:`."
                    )

    def _check_inherited(self, inherited: Dict[str, Set[str]]) -> None:
        """
        …and with a method inherited from the plugin classes the model is built on.

        The generated attribute lives in the subclass body, so it wins: an
        `is_available()` on a plugin's `Book` would simply stop existing the day a
        relationship took its name.
        """
        for table_name, owned in self._claims.items():
            for attr in sorted(set(owned) & inherited.get(table_name, set())):
                raise ValueError(
                    f"Relationship name clash on '{table_name}': '{attr}' is {owned[attr]} and "
                    f"would shadow an attribute inherited from the plugin class of the same "
                    f"name. Name the relationship explicitly with `relation:`, `backref:` or "
                    f"`collection:`."
                )

    def _involved_tables(self) -> Dict[str, DbTable]:
        """Every table that owns or receives a generated relationship."""
        tables: Dict[str, DbTable] = {}
        for fk in self.foreign_keys:
            tables[fk.table.name] = fk.table
            tables[fk.fk_table.name] = fk.fk_table
        for m2m in self.many_to_many:
            tables[m2m.junction.name] = m2m.junction
            for target in (m2m.target1, m2m.target2):
                tables[target['table'].name] = target['table']
        return tables

    def _resolve_many_to_many(self, m2m: ManyToManyRelation) -> None:
        """Name and emit the six attributes of one junction table."""
        first = self._m2m_names(m2m.junction, m2m.target1, m2m.target2)
        second = self._m2m_names(m2m.junction, m2m.target2, m2m.target1)

        self._emit_many_to_many(m2m.junction, m2m.target1, m2m.target2, first, second)
        self._emit_many_to_many(m2m.junction, m2m.target2, m2m.target1, second, first)

    def _m2m_names(self, junction: DbTable, this: Dict[str, Any],
                   other: Dict[str, Any]) -> Tuple[str, str, str]:
        """
        The three names one side of a junction produces, and their claims.

        `relation` sits on the junction and `backref` on the target, exactly as they
        do for a foreign key — a junction row *is* two foreign keys. `collection` is
        the third thing, with no equivalent among foreign keys: the shortcut that
        skips the junction and lists the other target directly.
        """
        # Named from the column, as a foreign key is — which is also what keeps a
        # self-referential junction unambiguous, since two columns of one table
        # cannot share a name.
        relation = this.get('relation') or self._cut(this['column'], this['table'])
        rows = this.get('backref') or f"{other['table'].name.lower()}_m2m"
        collection = this.get('collection') or other['table'].table_name

        source = f"the many-to-many target '{junction.name}.{this['column']}'"
        self._claim(junction, relation, source)
        self._claim(this['table'], rows, source)
        self._claim(this['table'], collection, source)

        return relation, rows, collection

    def _emit_foreign_key(self, fk: ForeignKeyRelation, forward: str, back: str) -> None:
        """Write the two relationship() statements for one foreign key."""
        indent = " " * 4
        local = f"{fk.table.name}.{fk.column}"

        # `foreign_keys` is emitted unconditionally. SQLAlchemy can infer it whenever
        # a single FK path links the two tables, but a *second* path — added later,
        # possibly by another plugin merging a column into either table, in either
        # direction — makes the inference ambiguous and breaks relationships that no
        # one touched. Stating it keeps each table's code independent of the rest.
        common = f", foreign_keys='{local}'"

        # Without a constraint SQLAlchemy has no join condition to infer; spell it out.
        if fk.soft:
            common += f", primaryjoin='{local} == {fk.fk_table.name}.{fk.fk_id}'"

        # Self-reference: both sides are the same class, so nothing tells SQLAlchemy
        # which one is the "one" side. remote_side does.
        remote = f", remote_side='{fk.fk_table.name}.{fk.fk_id}'" if fk.self_referential else ""

        relation = self.add_relation_key(fk.table.name, self.direct_relations)
        relation.append(
            f"{indent}{forward}: Mapped['{fk.fk_table.name}'] = "
            f"relationship('{fk.fk_table.name}'{common}{remote}, back_populates='{back}')\n"
        )

        # The cascade goes on the parent's collection and never on the child's
        # scalar: it is the parent that owns the rows, not the other way round.
        # Without it SQLAlchemy's default applies — the children are loaded and
        # their foreign key set to NULL, which quietly orphans them where the
        # column is nullable and fails on a driver error where it is not.
        cascade = ", cascade='all, delete-orphan'" if fk.owned else ""

        relation = self.add_relation_key(fk.fk_table.name, self.back_relations)
        relation.append(
            f"{indent}{back}: Mapped[List['{fk.table.name}']] = "
            f"relationship('{fk.table.name}'{common}{cascade}, back_populates='{forward}')\n"
        )

    def _emit_many_to_many(self, junction: DbTable, this: Dict[str, Any], other: Dict[str, Any],
                           names: Tuple[str, str, str], other_names: Tuple[str, str, str]) -> None:
        """Write the three statements one side of a junction produces."""
        indent = " " * 4
        relation, rows, collection = names
        other_collection = other_names[2]  # the only name of the other side we need

        table = this['table']
        column = f"{junction.name}.{this['column']}"

        # On the junction: the scalar to this target.
        code = self.add_relation_key(junction.name, self.direct_relations)
        code.append(
            f"{indent}{relation}: Mapped['{table.name}'] = "
            f"relationship('{table.name}', foreign_keys='{column}', back_populates='{rows}')\n"
        )

        # On this target: the junction rows, which carry whatever the relation itself
        # has to say (a rating, a role, a date).
        #
        # Owned by default, and on both sides: a junction row means nothing without
        # either of its ends, so deleting a book takes its books_authors rows and
        # leaves the authors — the cascade only ever runs from parent to child, and
        # the author is a parent of that row, never its child. `owned: false` on a
        # target derogates, for the junction that is really a historical record.
        cascade = ", cascade='all, delete-orphan'" if this.get('owned', True) else ""

        code = self.add_relation_key(table.name, self.back_relations)
        code.append(
            f"{indent}{rows}: Mapped[List['{junction.name}']] = "
            f"relationship('{junction.name}', foreign_keys='{column}'{cascade}, "
            f"back_populates='{relation}')\n"
        )

        # On this target: the shortcut that skips the junction. Both joins are stated
        # rather than inferred, for the reason foreign_keys is (a later plugin can add
        # a path), and because a self-referential junction has no inferable join at all
        # — both of its columns point at the same table.
        code.append(
            f"{indent}{collection}: Mapped[List['{other['table'].name}']] = "
            f"relationship('{other['table'].name}', secondary='{junction.table_name}', "
            f"primaryjoin='{table.name}.{this['id']} == {column}', "
            f"secondaryjoin='{other['table'].name}.{other['id']} == "
            f"{junction.name}.{other['column']}', "
            f"back_populates='{other_collection}', viewonly=True)\n"
        )

    def add_relation_key(self, name: str, relation: Dict[str, List[str]] = {}) -> List[str]:
        if name not in relation:
            relation[name] = []
        return relation[name]


class ColumnGenerator:
    """
    Generates column definitions for SQLAlchemy models.
    Handles column types, attributes, and relationships.
    """

    def __init__(self, db: DB, imports: ModelImportManager, relationships: RelationshipManager) -> None:
        """
        Initialize column generator.

        Args:
            db: Database schema manager
            imports: Import manager
            relationships: Relationship manager
        """
        self.db = db
        self.imports = imports
        self.relationships = relationships

    def generate_column(self, column: DbColumn, table: DbTable) -> str:
        """
        Generate SQLAlchemy column definition.

        Args:
            column: Column definition
            table: Parent table\n

        Returns:
            String with the column definition
        """
        indent = " " * 4

        # Get column type information
        t = column.db_type
        py_type = t.python_type.__name__
        sa_type = t.name

        # Use the most specific type in inheritance chain
        if t.inheritance:
            sa_type = t.inheritance[-1]

        # Add Python type import if needed
        self.imports.add_python_type_import(py_type)

        # Process type arguments
        type_args = [f"{a}={column.attr_type[a]}" for a in column.attr_type]

        # `case` swaps the plain string type for the normalising one. Absent, or
        # `neutral`, nothing changes — which is what most columns want.
        case = column.attributes.get('case', 'neutral')
        if case != 'neutral':
            if case not in ('upper', 'lower'):
                raise ValueError(
                    f"Column {table.name}.{column.name}: case must be upper, lower or neutral, "
                    f"got '{case}'")
            if py_type != 'str':
                raise ValueError(
                    f"Column {table.name}.{column.name}: case applies to strings, "
                    f"not to {py_type}")
            self.imports.kitebase_imports.add('CaseString')
            sa_type = 'CaseString'
            type_args.append(f"case='{case}'")
        else:
            self.imports.column_imports.add(sa_type)

        if type_args:
            sa_type = f"{sa_type}({', '.join(type_args)})"

        # Process foreign key
        foreign = ""
        if 'foreign_key' in column.attributes:
            foreign = self._process_foreign_key(column, table)

        # Process column arguments (translate $-token system defaults)
        field_args = [
            f"{a}={self._render_field_value(a, column.attr_field[a])}"
            for a in column.attr_field
        ]
        args_str = ', '.join([""] + field_args) if field_args else ""

        # Construct final column definition
        return f"{indent}{column.name}: Mapped[{py_type}] = mapped_column({sa_type}{foreign}{args_str})\n"

    def _render_field_value(self, attr: str, value: Any) -> str:
        """
        Render a mapped_column() field-argument value.

        Values are emitted verbatim (e.g. `default=datetime.now`), except a
        `$`-token `default` — a system default like `$op_date` — which is
        translated into a reference to the registered core callable
        (kitebase.defaults) and the needed import is added. This is the seam that
        makes op_date (and any app-registered system default) usable declaratively
        from model YAML.
        """
        if attr in ('default', 'onupdate') and isinstance(value, str) and value.startswith('$'):
            from kitebase import defaults as kb_defaults
            name = value[1:]
            if kb_defaults.get_default(name) is None:
                raise ValueError(
                    f"Unknown system default '{value}' on a column: not registered "
                    f"in kitebase.defaults (known: {sorted(kb_defaults.default_names())})"
                )
            self.imports.standard_imports.add('import kitebase.defaults')
            return f'kitebase.defaults.{name}'
        return f'{value}'

    def _process_foreign_key(self, column: DbColumn, table: DbTable) -> str:
        """
        Process foreign key definition.

        Args:
            column: Column with foreign key
            table: Parent table

        Returns:
            Foreign key argument string
        """
        fk = column.attributes['foreign_key']
        fk_table = fk['table']
        fk_id = fk['id']

        # Explicit relationship names, when the generated ones would be poor or
        # ambiguous. Kitebase hints, like `constraint` — never ForeignKey kwargs.
        relation = fk.get('relation')
        backref = fk.get('backref')

        # `constraint: false` → soft FK: keep the navigable relationship but emit
        # NO DB-level FK constraint, so the column may hold values with no matching
        # parent (unknown / late-bound codes, dirty imports). See PLUGIN_MODEL § 4.5.
        soft = fk.get('constraint') is False

        # `owned: true` → the row is a part of its parent: deleting the parent
        # deletes it, and so on down. Enforced by the ORM and not by the DDL, so it
        # covers soft foreign keys too, survives dialects that refuse the constraint
        # (a self-referential ON DELETE CASCADE, say), and needs no migration on a
        # database already in place. See PLUGIN_MODEL § 4.5.
        owned = fk.get('owned') is True

        # Additional ForeignKey() arguments (ondelete, onupdate, …). `constraint`
        # and `owned` are Kitebase hints, not ForeignKey kwargs — never forward them.
        fk_args = []
        for a in fk:
            if a not in ['target', 'table', 'id', 'constraint', 'owned',
                         'relation', 'backref']:
                fk_args.append(f"{a}={fk[a]}")

        fk_args_str = f", {', '.join(fk_args)}" if fk_args else ""

        if soft:
            foreign = ""   # no ForeignKey → no DB constraint, orphan values allowed
        else:
            foreign = f", ForeignKey('{fk_table.table_name}.{fk_id}'{fk_args_str})"
            self.imports.column_imports.add('ForeignKey')

        # Record the relationship; names and join spec are decided in resolve(),
        # once the whole model is known. A column that reaches a target of a
        # junction is left out: the m2m branch emits its scalar and the rows
        # collection, and a second pair would claim the same names — the naming
        # invariant would refuse both.
        if not self._is_junction_target(column, table):
            self.relationships.add_foreign_key_relation(
                table, column.name, fk_table, fk_id, soft, relation, backref, owned
            )
        self.imports.add_relationship_imports()

        return foreign

    @staticmethod
    def _is_junction_target(column: DbColumn, table: DbTable) -> bool:
        """True when this column is one of the two a `many_to_many:` declares."""
        m2m = table.attributes.get('many_to_many')
        if not m2m:
            return False
        return column.name in (m2m['target1'].get('column'), m2m['target2'].get('column'))


class Generator:
    """
    Main class for SQLAlchemy model source code generation.
    Coordinates the generation process and writes the final output.
    """

    def __init__(self, db: DB):
        """
        Initialize the generator.

        Args:
            db: Database schema manager
        """
        self.db = db
        self.imports = ModelImportManager(db)
        self.relationships = RelationshipManager()
        self.column_generator = ColumnGenerator(db, self.imports, self.relationships)
        self.class_finder = PluginClassFinder(db.pm)
        self.class_finder.scan_plugin_sources()

        self.mixins: Set[str] = set()  # Set of mixin classes
        self.tables: Dict[str, str] = {}  # Table name -> table class code
        self.source = ""  # Complete source code

    def generate(self, filename: str = "model.py") -> None:
        """
        Generate SQLAlchemy model code and write to file.

        A relative filename is written beside the application's config.yaml,
        not in whatever directory the process happens to run from.

        Args:
            filename: Output filename, absolute or relative to the app
        """
        self._process_tables()
        self._generate_source()

        path = self.db.pm.resolve_path(filename) if self.db.pm else Path(filename)
        with open(path, 'w') as f:
            f.write(self.source)

    def _process_tables(self) -> None:
        """Process all tables and generate their class definitions."""
        # Process each table definition
        for name in self.db.tables_list:
            self._process_table(name)

        # Names and join specs can only be decided now that every relation is known
        self.relationships.resolve(self._inherited_attributes())

        # Add relationships to table definitions
        self._add_relationships_to_tables()

    def _inherited_attributes(self) -> Dict[str, Set[str]]:
        """
        Public attribute names each generated class inherits from Python.

        A generated class is built on the plugin classes of the same name and on its
        mixins, and its own attributes are written in the subclass body — so they
        shadow whatever those classes define. Collecting the names is what lets a
        relationship called `archive` be refused instead of quietly removing
        `Archivable.archive()`.
        """
        inherited: Dict[str, Set[str]] = {}
        for name in self.db.tables_list:
            table = self.db.tables[name]
            attributes: Set[str] = set()
            for source in [name, *table.attributes.get('mixins', [])]:
                for path in self.class_finder.get_class_inheritance(source):
                    module_path, class_name = path.rsplit('.', 1)
                    module = self.class_finder.imported_modules.get(module_path)
                    base = getattr(module, class_name, None) if module else None
                    if base is not None:
                        attributes.update(a for a in dir(base) if not a.startswith('_'))
            if attributes:
                inherited[name] = attributes
        return inherited

    def _process_table(self, name: str) -> None:
        """
        Process a single table and generate its class definition.

        Args:
            name: Table name
        """
        table = self.db.tables[name]

        # Collect mixins
        mixins = table.attributes.get('mixins', [])
        for mixin in mixins:
            self.mixins.add(mixin)

        # Find plugin-defined classes with the same name
        plugin_classes = self.class_finder.get_class_inheritance(name)

        # Combine base classes (Base, mixins, plugin classes)
        base_classes = ["Base"]
        base_classes.extend(mixins)
        base_classes.extend(plugin_classes)

        # Add imports for plugin classes
        for plugin_class in plugin_classes:
            module_path = plugin_class.rsplit('.', 1)[0]
            self.imports.standard_imports.add(f"import {module_path}")

        # Generate class declaration
        class_declaration = f"class {name}({', '.join(base_classes)}):\n"

        # Generate tablename declaration with declared_attr for dynamic resolution
        tablename_code = (
            "    @declared_attr\n"
            "    def __tablename__(cls):\n"
            f"        return resolve_table_name('{name}', '{table.table_name}')\n"
        )
        code = [class_declaration, tablename_code]

        # A junction's own columns are written by the loop below like all the
        # others — `db._calc_junctions` materialised them from the declaration.
        # What is left here is the navigation it implies.
        m2m = table.attributes.get('many_to_many', None)
        if m2m:
            try:
                # A junction needs these whether or not the model has plain foreign
                # keys elsewhere; they used to arrive only as a side effect of one.
                self.imports.add_relationship_imports()
                self.relationships.add_many_to_many(table, m2m['target1'], m2m['target2'])
            except (ValueError, KeyError) as e:
                print(f"Error processing many-to-many relationship in {name}: {e}")

        # Generate columns
        for column in table.columns:
            column_code = self.column_generator.generate_column(column, table)
            if column_code:
                code.append(column_code)

        # Generate compound indexes (__table_args__)
        indexes_code = self._generate_table_indexes(table)
        if indexes_code:
            code.append("\n")
            code.append(indexes_code)

        self.tables[name] = ''.join(code)

    def _generate_table_indexes(self, table: DbTable) -> str:
        """
        Generate __table_args__ for compound indexes.

        Args:
            table: Table definition

        Returns:
            String with __table_args__ definition, or empty string if no indexes
        """
        indexes = table.attributes.get('indexes', [])
        if not indexes:
            return ""

        # Add Index to imports
        self.imports.column_imports.add('Index')

        index_lines = []
        for idx in indexes:
            # Build column list
            columns = idx.get('columns', [])
            if not columns:
                continue

            name = idx.get('name', f"idx_{'_'.join(columns)}")
            cols_str = ', '.join(f"'{col}'" for col in columns)

            # Add unique flag if specified
            unique = idx.get('unique', False)
            unique_str = ', unique=True' if unique else ''

            # Trailing comma on every line, last one included: a single index
            # without it is a parenthesised expression and not a tuple, and
            # SQLAlchemy refuses __table_args__ that is not one.
            description = idx.get('description', '')
            comment = f"  # {description}" if description else ''

            index_lines.append(f"        Index('{name}', {cols_str}{unique_str}),{comment}")

        if not index_lines:
            return ""

        # Generate __table_args__ (no join with comma since we added commas manually)
        args = '\n'.join(index_lines)
        return f"    __table_args__ = (\n{args}\n    )\n"

    def _add_relationships_to_tables(self) -> None:
        """Add relationship definitions to table classes."""
        # Add direct relationships
        for table_name, relations in self.relationships.direct_relations.items():
            if relations:
                self.tables[table_name] += '\n' + ''.join(relations)

        # Add back references
        for table_name, relations in self.relationships.back_relations.items():
            if relations:
                self.tables[table_name] += '\n' + ''.join(relations)

    def _generate_mixin_classes(self) -> str:
        """
        Generate mixin class definitions.

        Returns:
            String with all mixin class definitions
        """
        code = []

        # sorted(): self.mixins is a set — iterating it raw makes the *textual*
        # order of these (independent) class blocks depend on PYTHONHASHSEED.
        # No functional impact (column order follows the MRO / bases list, not the
        # print order), but stable output is nicer to read/diff. Matches how
        # imports are already emitted (sorted()).
        for mixin in sorted(self.mixins):
            if mixin in self.db.types:
                type_def = self.db.types[mixin]

                # Inherit from plugin Python class if defined (carries methods/protocol)
                plugin_classes = self.class_finder.get_class_inheritance(mixin)
                if plugin_classes:
                    for cls in plugin_classes:
                        module_path = cls.rsplit('.', 1)[0]
                        self.imports.standard_imports.add(f"import {module_path}")
                    bases = ', '.join(plugin_classes)
                    mixin_code = [f"class {mixin}({bases}):\n"]
                else:
                    mixin_code = [f"class {mixin}:\n"]

                # Only generate real columns (virtual ones live as hybrid_property in Python)
                has_columns = False
                for column in type_def.columns:
                    column_code = self.column_generator.generate_column(column, type_def)
                    if column_code:
                        mixin_code.append(column_code)
                        has_columns = True

                if not has_columns:
                    mixin_code.append("    pass\n")

                code.append(''.join(mixin_code))

        return '\n\n'.join(code) if code else ""

    def _generate_source(self) -> None:
        """Generate the complete source code."""
        # Build body parts FIRST: mixin generation registers the imports of its
        # base classes (e.g. `import common.model` for Archivable), so it must run
        # before import statements are rendered — otherwise those imports are lost.
        helper_code = self._generate_helper_functions()
        mixin_code = self._generate_mixin_classes()

        source_parts = []

        # Add file header warning
        source_parts.append(self._generate_file_header())
        source_parts.append("")

        # Add imports (now complete — all body generators have run)
        source_parts.append(self.imports.generate_import_statements())
        source_parts.append("")
        source_parts.append("")

        # Add helper functions if any (currently empty - resolve_table_name moved to utils)
        if helper_code:
            source_parts.append(helper_code)
            source_parts.append("")
            source_parts.append("")

        # Add mixins
        if mixin_code:
            source_parts.append(mixin_code)
            source_parts.append("")

        # Add table classes
        for name in self.db.tables_list:
            source_parts.append(self.tables[name])
            source_parts.append("")

        # Add additional source code
        additional_code = self.db.pm.config.get('source_add', '')
        if additional_code:
            source_parts.append(additional_code)

        self.source = '\n'.join(source_parts)

    def _generate_file_header(self) -> str:
        """
        Generate file header warning about automatic generation.

        Returns:
            String with warning header
        """
        return '''# ============================================================================
# GENERATED FILE - DO NOT EDIT MANUALLY
# ============================================================================
# This file is automatically generated by Kitebase source generator.
# Any manual changes will be lost when the file is regenerated.
#
# To make changes:
#   1. Edit the plugin YAML files (plugins/**/model.yaml)
#   2. Regenerate this file (python devtest.py or kitebase generate)
#
# Generator: kitebase.source.Generator
# ============================================================================'''

    def _generate_helper_functions(self) -> str:
        """
        Generate helper functions for model code.

        Returns:
            String with helper functions (empty now - resolve_table_name moved to kitebase.utils)
        """
        return ""
