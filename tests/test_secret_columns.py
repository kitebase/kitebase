"""Secret columns and write transforms — the `secret` / `on_write` pair.

`secret: true` was already honoured by the descriptor generators but nothing
enforced it on the data path, so a password column travelled back to any
authenticated client. Covered here: the read paths that must drop it
(serialize_model, the query select), the write rules that follow from it
(an empty secret means "unchanged", `on_write` decides the stored form), and
password verification, including the legacy plaintext ramp and the digest of an
unknown scheme that must NOT be compared literally.

Uses standalone SQLAlchemy models plus stub table definitions: the rules read
column attributes, so they need the shape of a DbTable, not a loaded app.
"""
import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

from kitebase import transforms
from kitebase.endpoint_db import write_values, build_filters
from kitebase.querybuilder import SelectBuilder
from kitebase.utils import secret_columns, serialize_model

Base = declarative_base()


class StubColumn:
    """The part of a DbColumn these rules read."""

    def __init__(self, name, **attributes):
        self.name = name
        self.attributes = attributes


class StubTable:
    """The part of a DbTable these rules read, including its cached secret set."""

    def __init__(self, columns, virtual_columns=()):
        self.effective_columns = columns
        self.virtual_columns = list(virtual_columns)

    @property
    def secret_columns(self) -> frozenset:
        return frozenset(col.name for col in self.effective_columns
                         if col.attributes.get('secret'))


def user_table():
    return StubTable([
        StubColumn('id', primary_key=True),
        StubColumn('username'),
        StubColumn('password', secret=True, on_write='password_hash'),
    ])


class User(Base):
    __tablename__ = 'user'
    id = Column(Integer, primary_key=True)
    username = Column(String)
    password = Column(String)

    @classmethod
    def get_table_definition(cls):
        """Stands in for the kitebase model ↔ DbTable bridge."""
        return user_table()


# ── Password verification ────────────────────────────────────────────────────

def test_hash_and_verify_roundtrip():
    stored = transforms.hash_password('s3cret')

    assert stored != 's3cret'
    assert transforms.is_hashed(stored)
    assert transforms.verify_password('s3cret', stored)
    assert not transforms.verify_password('wrong', stored)


def test_hash_is_salted():
    assert transforms.hash_password('same') != transforms.hash_password('same')


def test_password_over_bcrypt_limit_is_refused():
    """Truncating at 72 bytes would make two different passwords equivalent."""
    with pytest.raises(ValueError):
        transforms.hash_password('x' * 73)


def test_legacy_plaintext_is_accepted_and_left_alone():
    """A stored plaintext still logs in — and stays as it is.

    Hashing happens when a password is written, so a database another system
    also reads keeps working until someone changes their password on purpose.
    """
    assert transforms.verify_password('admin', 'admin')
    assert not transforms.verify_password('wrong', 'admin')


def test_foreign_digest_is_never_compared_literally():
    """A digest this code cannot verify must fail, not become the password.

    Comparing it as plaintext would let anyone who read the column log in by
    typing the digest itself.
    """
    md5 = '5f4dcc3b5aa765d61d8327deb882cf99'
    argon2 = '$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$hash'
    sha512_crypt = '$6$rounds=5000$salt$hash'

    for stored in (md5, argon2, sha512_crypt):
        assert not transforms.verify_password(stored, stored)
        assert not transforms.verify_password('password', stored)


def test_missing_password_or_stored_value_fails():
    assert not transforms.verify_password('', 'admin')
    assert not transforms.verify_password('admin', None)
    assert not transforms.verify_password('admin', '')


# ── Registry ─────────────────────────────────────────────────────────────────

def test_password_hash_is_registered():
    assert transforms.get_write_transform('password_hash') is transforms.hash_password
    assert 'password_hash' in transforms.transform_names()
    assert transforms.get_write_transform('nope') is None


def test_apps_can_register_their_own_transform():
    transforms.register_write_transform('shout', str.upper)
    try:
        assert transforms.get_write_transform('shout')('hi') == 'HI'
    finally:
        transforms._TRANSFORMS.pop('shout', None)


# ── Read path ────────────────────────────────────────────────────────────────

def test_secret_columns_reads_the_definition():
    assert secret_columns(user_table()) == {'password'}
    assert secret_columns(None) == set()


def test_serialize_model_drops_secret_columns():
    record = User(id=1, username='admin', password='stored-hash')

    result = serialize_model(record, db_table=user_table())

    assert result == {'id': 1, 'username': 'admin'}


def test_serialize_model_resolves_the_definition_when_not_given():
    """A caller that forgets to pass db_table must not turn into a leak."""
    record = User(id=1, username='admin', password='stored-hash')

    assert 'password' not in serialize_model(record)


def select_builder():
    return SelectBuilder({'User': User}, 'User')


def selected_names(columns):
    return [getattr(col, 'key', getattr(col, 'name', None)) for col in columns]


def test_query_wildcard_expands_without_secret_columns():
    columns = select_builder().build_select_columns(['*'])

    assert selected_names(columns) == ['id', 'username']


def test_query_with_no_select_expands_without_secret_columns():
    columns = select_builder().build_select_columns(None)

    assert selected_names(columns) == ['id', 'username']


def test_query_naming_a_secret_column_is_refused():
    """Refused, not dropped: a missing column would read as an empty value."""
    builder = select_builder()

    for expr in ('password', 'User.password', 'max(User.password)'):
        with pytest.raises(ValueError, match='not readable'):
            builder.build_select_columns([expr])


def test_query_still_selects_ordinary_columns():
    columns = select_builder().build_select_columns(['User.username'])

    assert selected_names(columns) == ['username']


def test_filtering_on_a_secret_column_is_refused():
    """A filter on a column that is never returned is a way of guessing it."""
    with pytest.raises(ValueError, match='not filterable'):
        build_filters(User, {'password': 'guess'})

    with pytest.raises(ValueError, match='not filterable'):
        build_filters(User, {'password__like': 'a%'})


def test_filtering_on_ordinary_columns_still_works():
    assert build_filters(User, {'username': 'admin'}) is not None


# ── Write path ───────────────────────────────────────────────────────────────

def test_write_values_hashes_a_secret_on_the_way_in():
    result = write_values(user_table(), {'username': 'admin', 'password': 's3cret'})

    assert result['username'] == 'admin'
    assert result['password'] != 's3cret'
    assert transforms.verify_password('s3cret', result['password'])


def test_write_values_drops_an_empty_secret():
    """Saving a form that never showed the password must not clear it."""
    for empty in ('', None):
        assert write_values(user_table(), {'username': 'admin', 'password': empty}) \
            == {'username': 'admin'}


def test_write_values_keeps_other_empty_values():
    """Only secrets get the "empty means unchanged" reading."""
    assert write_values(user_table(), {'username': ''}) == {'username': ''}


def test_write_values_without_a_definition_passes_through():
    data = {'username': 'admin', 'password': 'plain'}

    assert write_values(None, data) == data


def test_write_values_refuses_an_unknown_transform():
    table = StubTable([StubColumn('code', on_write='does_not_exist')])

    with pytest.raises(ValueError, match='does_not_exist'):
        write_values(table, {'code': 'x'})
