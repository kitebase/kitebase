"""Rows from one database into another, through the column types.

Both sides must be aligned with the schema and the target empty; tables go in
foreign-key order, a key to the same table is filled by a second pass, and the
counts are checked before the commit. Proven SQLite -> SQLite here; the same
code runs against PostgreSQL, where the sequences are set as well.
"""
from datetime import date

import pytest
import sqlalchemy as sa

from kitebase.db_copy import CopyError, copy_database, plan


@pytest.fixture
def metadata():
    md = sa.MetaData()
    sa.Table('authors', md,
             sa.Column('id', sa.Integer, primary_key=True),
             sa.Column('name', sa.String(50), nullable=False),
             sa.Column('born', sa.Date),
             sa.Column('active', sa.Boolean, nullable=False),
             sa.Column('extra', sa.JSON))
    sa.Table('books', md,
             sa.Column('id', sa.Integer, primary_key=True),
             sa.Column('title', sa.String(80), nullable=False),
             sa.Column('author_id', sa.Integer, sa.ForeignKey('authors.id'), nullable=False),
             sa.Column('sequel_of', sa.Integer, sa.ForeignKey('books.id')))
    return md


def engine_at(path, metadata, create=True):
    engine = sa.create_engine(f'sqlite:///{path}')

    @sa.event.listens_for(engine, 'connect')
    def _fk(dbapi, _):
        dbapi.execute('PRAGMA foreign_keys=ON')

    if create:
        metadata.create_all(engine)
    return engine


@pytest.fixture
def source(tmp_path, metadata):
    engine = engine_at(tmp_path / 'source.sqlite', metadata)
    authors, books = metadata.tables['authors'], metadata.tables['books']
    with engine.begin() as conn:
        conn.execute(authors.insert(), [
            {'id': 1, 'name': 'Ada', 'born': date(1815, 12, 10), 'active': True,
             'extra': {'tags': ['math']}},
            {'id': 7, 'name': 'Bob', 'born': None, 'active': False, 'extra': None},
        ])
        # The sequel comes first and its key points forward: no row order
        # inserts these three as they are, with the foreign keys enforced.
        conn.execute(books.insert(), [
            {'id': 1, 'title': 'Second', 'author_id': 1, 'sequel_of': None},
            {'id': 2, 'title': 'First', 'author_id': 1, 'sequel_of': None},
            {'id': 3, 'title': 'Other', 'author_id': 7, 'sequel_of': None},
        ])
        conn.execute(books.update().where(books.c.id == 1).values(sequel_of=2))
    return engine


@pytest.fixture
def target(tmp_path, metadata):
    return engine_at(tmp_path / 'target.sqlite', metadata)


def rows(engine, table):
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(sa.select(table).order_by(table.c.id))]


def test_every_row_arrives_with_its_values(source, target, metadata):
    report = copy_database(source, target, metadata)
    for table in metadata.tables.values():
        assert rows(target, table) == rows(source, table)
    assert report.counts == {'authors': 2, 'books': 3}
    ada = rows(target, metadata.tables['authors'])[0]
    assert ada['born'] == date(1815, 12, 10) and ada['active'] is True
    assert ada['extra'] == {'tags': ['math']}


def test_tables_go_in_key_order_and_a_self_reference_waits(metadata):
    ordered, deferred = plan(metadata)
    assert [t.name for t in ordered] == ['authors', 'books']
    assert [c.name for fkc in deferred['books'] for c in fkc.columns] == ['sequel_of']


def test_a_target_with_rows_is_refused_unless_truncated(source, target, metadata):
    copy_database(source, target, metadata)
    with pytest.raises(CopyError, match='not empty'):
        copy_database(source, target, metadata)
    copy_database(source, target, metadata, truncate=True)
    assert len(rows(target, metadata.tables['books'])) == 3


def test_a_target_not_aligned_is_refused_and_left_alone(source, tmp_path, metadata):
    bare = engine_at(tmp_path / 'bare.sqlite', metadata, create=False)
    with pytest.raises(CopyError, match='target differs from the schema'):
        copy_database(source, bare, metadata)
    assert sa.inspect(bare).get_table_names() == []


def test_the_dry_run_counts_and_writes_nothing(source, target, metadata):
    report = copy_database(source, target, metadata, dry_run=True)
    assert report.counts == {'authors': 2, 'books': 3}
    assert rows(target, metadata.tables['authors']) == []
    assert 'would be copied' in report.format('a', 'b')


def test_a_failure_halfway_leaves_the_target_empty(source, target, metadata, tmp_path):
    with target.begin() as conn:
        conn.exec_driver_sql('CREATE TRIGGER no_other BEFORE INSERT ON books '
                             "WHEN NEW.title = 'Other' BEGIN SELECT RAISE(ABORT, 'refused'); END")
    with pytest.raises(CopyError, match='Copying books'):
        copy_database(source, target, metadata)
    assert rows(target, metadata.tables['authors']) == []


def test_the_same_database_on_both_sides_is_refused(source, metadata):
    with pytest.raises(CopyError, match='same database'):
        copy_database(source, source, metadata)


def test_a_cycle_through_a_not_null_key_is_named(tmp_path):
    md = sa.MetaData()
    sa.Table('a', md, sa.Column('id', sa.Integer, primary_key=True),
             sa.Column('b_id', sa.Integer, sa.ForeignKey('b.id', use_alter=True), nullable=False))
    sa.Table('b', md, sa.Column('id', sa.Integer, primary_key=True),
             sa.Column('a_id', sa.Integer, sa.ForeignKey('a.id'), nullable=False))
    src = engine_at(tmp_path / 's.sqlite', md)
    dst = engine_at(tmp_path / 'd.sqlite', md)
    with pytest.raises(CopyError, match='NOT NULL'):
        copy_database(src, dst, md)
