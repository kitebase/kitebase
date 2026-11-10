"""
The schema an application declares: types, tables, columns, reference sets.

Read from the merged tree of the plugins and then left alone. Nothing here
knows a database: no engine, no session, no generated model. That is what lets
`check`, the inspector and the tests build a schema without opening anything.
"""
import copy
import inspect
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Union

import sqlalchemy.types

from kitebase.plugins import Plugin, PluginsManager
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


class Schema:
    """
    The types and the tables of an application, as its plugins declare them.

    - self.pm: the plugins the schema is read from
    - self.types: every available type, built-in and declared
    - self.tables: every declared table, by model name
    - self.tables_list: the model names, in declaration order
    """

    def __init__(self) -> None:
        self.pm: Optional[PluginsManager] = None
        self.types: Dict[str, DbType] = {}
        self.tables: Dict[str, DbTable] = {}
        self.tables_list: List[str] = []

    def calc(self, plugins: PluginsManager) -> None:
        """
        Build the schema from the plugin definitions:

        1. Load all type definitions
        2. Create table structures
        3. Materialize the columns a `many_to_many:` declaration implies
        4. Process and validate column definitions
        5. Resolve the reference sets
        """
        self.pm = plugins
        self._calc_types()
        self._calc_tables()
        self._calc_junctions()
        self._calc_columns()
        self._calc_ref_sets()

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
        # The types of the core itself, declared like the built-in ones
        self.types['CodeSet'] = DbType('CodeSet', "", python_type=list)

        # Process plugin-defined types
        for name in self.pm.sorted:
            plugin = self.pm.plugins[name]
            for data in plugin.data:
                types = data.get('types', {})
                for type_name, value in types.items():
                    if type_name in self.types:
                        raise ValueError(f"Type already defined: {type_name}")
                    # A copy, as for the tables: see _calc_tables.
                    self.types[type_name] = DbType(type_name, plugin,
                                                   attributes=copy.deepcopy(value))

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

            # A copy: resolution writes into the definitions (inherited type
            # attributes, the table a foreign key points at), and the merged
            # tree and the plugins' sources stay what the plugins declared.
            table = DbTable(table_name, plugin, copy.deepcopy(value), self)
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

    def _calc_ref_sets(self) -> None:
        """Give each junction end that declares `field: true` its RefSet.

        The one place that decides whether a junction can be read as a value: a
        column of its own that nothing fills by itself (one without a `default`,
        such as a note on the pair) would be dropped by every write of the field,
        so it is refused here, at startup, rather than lost later.
        """
        for junction in self.tables.values():
            m2m = junction.attributes.get('many_to_many')
            if not m2m:
                continue
            for this_key, other_key in (('target1', 'target2'), ('target2', 'target1')):
                this, other = m2m[this_key], m2m[other_key]
                if not this.get('field'):
                    continue
                where = f"Table '{junction.name}': many_to_many {this_key}"
                name = this.get('collection')
                if not name:
                    raise ValueError(f"{where} declares `field: true` without a `collection:`, "
                                     f"which is the name the field takes")
                pair = {this['column'], other['column']}
                carried = [col.name for col in junction.effective_columns
                           if col.name not in pair
                           and not col.attributes.get('primary_key')
                           and not col.attributes.get('virtual')
                           and 'default' not in col.attributes]
                if carried:
                    raise ValueError(
                        f"{where} declares `field: true`, but the junction rows carry "
                        f"{', '.join(carried)}: writing the field as a list of keys would "
                        f"lose them. Edit the rows as a collection instead")
                owner = this['table']
                owner.ref_sets[name] = RefSet(
                    name=name, owner=owner, owner_key=this['id'], junction=junction,
                    owner_column=this['column'], target_column=other['column'],
                    target=other['table'], target_key=other['id'])


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


@dataclass(frozen=True)
class RefSet:
    """A junction seen from one of its ends as a value: the keys of the other end.

    Declared with `field: true` on a junction target, it gives that end a field
    holding the list of keys it is linked to, read and written as one value of the
    record (`Book.tags == [3, 5, 12]`). The junction rows stay a detail of the
    server. Only a junction whose rows say nothing beyond the pair can be one:
    anything else they carried would be lost on the first write.
    """
    name: str               # the field on `owner`, which is the collection's name
    owner: 'DbTable'        # the end that holds the field
    owner_key: str          # its column the junction points at
    junction: 'DbTable'
    owner_column: str       # the junction column pointing at `owner`
    target_column: str      # the junction column pointing at `target`
    target: 'DbTable'
    target_key: str         # the column of `target` whose values the field holds


class DbTable:
    """
    Represents a database table with support for:
    - Multi-plugin table definitions
    - Column management
    - Table attributes and metadata
    """

    def __init__(self, name: str, plugin: Plugin, attributes: Dict[str, Any], db: 'Schema' = None) -> None:
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
        # The junctions this table sees as a field (`field: true` on its end)
        self.ref_sets: Dict[str, RefSet] = {}
        self._effective_columns: Optional[List['DbColumn']] = None
        self._secret_columns: Optional[frozenset] = None
        self._column_groups: Optional[Dict[str, str]] = None
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
    def column_groups(self) -> Dict[str, str]:
        """
        The columns that belong to a group (`column_group: pricing`), by name.

        A group is what access control grants on columns: `pricing.read` shows
        them, `pricing.write` lets them change (kitebase.access). Columns
        outside any group follow their table.
        """
        if self._column_groups is None:
            self._column_groups = {
                col.name: col.attributes['column_group'] for col in self.effective_columns
                if col.attributes.get('column_group')}
        return self._column_groups

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

    def _declared_columns(self, db: 'Schema') -> Iterator[Dict[str, Any]]:
        """The column definitions of this table before resolution: its own, then its mixins'.

        A mixin's columns are the type's, unresolved at this point but already
        merged along the type's own inheritance.
        """
        yield from self._columns
        for mixin_name in self.attributes.get('mixins', []):
            mixin = db.types.get(mixin_name)
            yield from (mixin.attributes.get('columns', []) if mixin else [])

    def stored_column(self, name: str) -> Optional['DbColumn']:
        """A resolved column of this table that the database stores, its own or a mixin's."""
        for col in self.columns:
            if col.name == name:
                return col
        for mixin_name in self.attributes.get('mixins', []):
            mixin = self.db.types.get(mixin_name) if self.db else None
            for col in (mixin.columns if mixin else []):
                if col.name == name:
                    return col
        return None

    def _declared_primary_key(self, db: 'Schema') -> Optional[str]:
        """Name of the primary key this table declares, from a column or its type."""
        def is_key(column: Dict[str, Any]) -> bool:
            if 'primary_key' in column:
                return bool(column['primary_key'])
            declared_type = db.types.get(column.get('type'))
            return bool(declared_type and declared_type.attributes.get('primary_key'))

        return next((col['name'] for col in self._declared_columns(db) if is_key(col)), None)

    def materialize_junction_key(self, db: 'Schema', pk_name: str = 'id') -> None:
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

    def materialize_junction_targets(self, db: 'Schema') -> None:
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

    def _junction_target_column(self, db: 'Schema', name: str,
                                target_table: str, target_key: str) -> Dict[str, Any]:
        """The foreign key column reaching one target of a junction."""
        foreign = db.tables.get(target_table)
        if foreign is None:
            raise ValueError(f"Table '{self.name}': many_to_many target table "
                             f"'{target_table}' does not exist")

        key = next((col for col in foreign._declared_columns(db)
                    if col.get('name') == target_key), None)
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

    def resolve_m2m(self, db: 'Schema') -> None:
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
            col = foreign.stored_column(id)
            target['db_type'] = col.db_type if col else None
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

    def __init__(self, attributes: Dict[str, Any], db: 'Schema') -> None:
        """
        Initialize a new column.

        Args:
            attributes: Column configuration
            db: Database schema manager instance
        """
        self.db: 'Schema' = db
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
        col = foreign.stored_column(id)
        if col:
            self.db_type = col.db_type
        if not self.db_type:
            raise ValueError(f"Column: {self.name} has invalid foreign reference")
