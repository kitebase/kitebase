"""
The base class of the generated models.
"""
from typing import Any

from sqlalchemy.orm import declarative_base

import kitebase.context


class BaseApp:
    """
    Base class for all SQLAlchemy models.
    Provides access to the database schema information.

    The context of the request is kitebase.context: the three methods below
    are aliases of its verbs.
    """
    # The application these models belong to: one per process, set by
    # kitebase.db when it creates it.
    __kitebase_app__: Any = None

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
