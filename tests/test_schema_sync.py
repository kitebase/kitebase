"""
Schema alignment (coframe.schema_sync).

The point of these tests is the closed list: what the sync applies by itself,
what it refuses, and — the part that costs data if it is wrong — that a refused
change stays refused even when a table gets rebuilt for a different reason.

The fixtures work on plain SQLAlchemy metadata rather than on plugin YAML: what
is under test is the comparison between a database and a schema, whatever built
the schema. SQLite is the harshest dialect here (no ALTER COLUMN, so every type
change goes through a table rebuild), which makes it the right one to test on.
"""
import pytest
import sqlalchemy as sa

from coframe.schema_sync import (
    REFUSED,
    SAFE,
    SchemaOutOfDateError,
    apply_diff,
    check_on_startup,
    diff_schema,
    format_diff,
    plan_sql,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

def make_engine(tmp_path, name='t.sqlite'):
    return sa.create_engine(f'sqlite:///{tmp_path / name}')


def base_metadata():
    """The schema as the database already has it."""
    md = sa.MetaData()
    sa.Table('author', md,
             sa.Column('id', sa.Integer, primary_key=True),
             sa.Column('name', sa.String(30)))
    book = sa.Table('book', md,
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('title', sa.String(50), nullable=False),
                    sa.Column('author_id', sa.Integer, sa.ForeignKey('author.id')),
                    sa.Column('note', sa.String(10)))
    sa.Index('idx_book_title', book.c.title)
    return md


@pytest.fixture
def engine(tmp_path):
    """A database holding base_metadata() with one row in each table."""
    eng = make_engine(tmp_path)
    base_metadata().create_all(eng)
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO author (id, name) VALUES (1, 'Calvino')"))
        conn.execute(sa.text(
            "INSERT INTO book (id, title, author_id, note) "
            "VALUES (1, 'Le città invisibili', 1, 'keep')"))
    return eng


def column_info(engine, table):
    return {c['name']: c for c in sa.inspect(engine).get_columns(table)}


def by_target(diff):
    return {c.target: c for c in diff.changes}


# ── Aligned ────────────────────────────────────────────────────────────────────

def test_aligned_database_reports_nothing(engine):
    diff = diff_schema(engine, base_metadata())

    assert diff.is_aligned
    assert diff.changes == []
    assert format_diff(diff) == 'Database aligned with the schema.'


def test_tables_outside_the_schema_are_not_looked_at(engine):
    """A host application's tables, another tenant's, a plugin not loaded."""
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE legacy_orders (id INTEGER, total NUMERIC)'))

    diff = diff_schema(engine, base_metadata())

    assert diff.is_aligned


# ── The safe list ──────────────────────────────────────────────────────────────

def test_new_table_column_and_index_are_applied(engine):
    md = base_metadata()
    sa.Table('publisher', md,
             sa.Column('id', sa.Integer, primary_key=True),
             sa.Column('name', sa.String(60)))
    book = md.tables['book']
    book.append_column(sa.Column('isbn', sa.String(20)))
    sa.Index('idx_book_isbn', book.c.isbn)

    diff = diff_schema(engine, md)
    assert {c.kind for c in diff.safe} == {'add_table', 'add_column', 'add_index'}
    assert diff.refused == []

    apply_diff(engine, diff)

    assert 'publisher' in sa.inspect(engine).get_table_names()
    assert 'isbn' in column_info(engine, 'book')
    assert 'idx_book_isbn' in {i['name'] for i in sa.inspect(engine).get_indexes('book')}
    assert diff_schema(engine, md).is_aligned


def test_widening_a_string_keeps_data_indexes_and_keys(engine):
    """On SQLite this is a full table rebuild — everything must come across."""
    md = base_metadata()
    md.tables['book'].c.title.type = sa.String(200)

    diff = diff_schema(engine, md)
    assert [c.kind for c in diff.safe] == ['widen_type']

    apply_diff(engine, diff)

    assert str(column_info(engine, 'book')['title']['type']) == 'VARCHAR(200)'
    inspector = sa.inspect(engine)
    assert 'idx_book_title' in {i['name'] for i in inspector.get_indexes('book')}
    assert [fk['referred_table'] for fk in inspector.get_foreign_keys('book')] == ['author']
    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT title FROM book')).scalar() == 'Le città invisibili'
    assert diff_schema(engine, md).is_aligned


def test_relaxing_not_null_is_applied_tightening_is_not(engine):
    md = base_metadata()
    md.tables['book'].c.title.nullable = True

    diff = diff_schema(engine, md)
    assert [c.kind for c in diff.safe] == ['relax_nullable']
    apply_diff(engine, diff)
    assert column_info(engine, 'book')['title']['nullable'] is True

    # Same schema as the database now has, but asking for one column to tighten.
    md2 = base_metadata()
    md2.tables['book'].c.title.nullable = True
    md2.tables['book'].c.note.nullable = False
    refused = diff_schema(engine, md2).refused
    assert [c.kind for c in refused] == ['modify_nullable']
    assert 'may hold NULL' in refused[0].reason


# ── Columns that need a value for the rows already there ───────────────────────

def test_not_null_column_backfills_from_the_declared_default(engine):
    md = base_metadata()
    md.tables['book'].append_column(
        sa.Column('pages', sa.Integer, nullable=False, default=0))

    diff = diff_schema(engine, md)
    assert diff.safe[0].verdict == SAFE
    assert 'existing rows set to 0' in diff.safe[0].description

    apply_diff(engine, diff)

    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT pages FROM book')).scalar() == 0
    assert column_info(engine, 'book')['pages']['nullable'] is False


def test_callable_default_is_evaluated_once(engine):
    md = base_metadata()
    md.tables['book'].append_column(
        sa.Column('status', sa.String(1), nullable=False, default=lambda: 'A'))

    diff = diff_schema(engine, md)
    apply_diff(engine, diff)

    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT status FROM book')).scalar() == 'A'


def test_not_null_without_default_is_refused_when_rows_exist(engine):
    md = base_metadata()
    md.tables['book'].append_column(sa.Column('shelf', sa.String(10), nullable=False))

    diff = diff_schema(engine, md)

    assert diff.safe == []
    change = diff.refused[0]
    assert change.target == 'book.shelf'
    assert 'no value to receive' in change.reason
    assert 'UPDATE book SET shelf' in change.sql_hint


def test_not_null_without_default_is_fine_on_an_empty_table(tmp_path):
    eng = make_engine(tmp_path, 'empty.sqlite')
    base_metadata().create_all(eng)

    md = base_metadata()
    md.tables['book'].append_column(sa.Column('shelf', sa.String(10), nullable=False))

    diff = diff_schema(eng, md)
    assert [c.verdict for c in diff.changes] == [SAFE]
    apply_diff(eng, diff)
    assert diff_schema(eng, md).is_aligned


# ── The refused list ───────────────────────────────────────────────────────────

def test_dropped_column_is_reported_never_applied(engine):
    md = base_metadata()
    md.tables['book']._columns.remove(md.tables['book'].c.note)

    diff = diff_schema(engine, md)

    change = by_target(diff)['book.note']
    assert change.verdict == REFUSED
    assert change.sql_hint == 'ALTER TABLE book DROP COLUMN note;'

    apply_diff(engine, diff)
    assert 'note' in column_info(engine, 'book')


def test_a_rebuild_does_not_carry_out_a_refused_drop(engine):
    """
    The dangerous overlap: widening a column rebuilds the whole table on
    SQLite, and the rebuild must copy the columns the schema no longer
    declares instead of quietly dropping them.
    """
    md = base_metadata()
    md.tables['book'].c.title.type = sa.String(200)
    md.tables['book']._columns.remove(md.tables['book'].c.note)

    diff = diff_schema(engine, md)
    assert [c.kind for c in diff.safe] == ['widen_type']
    assert [c.kind for c in diff.refused] == ['remove_column']

    apply_diff(engine, diff)

    assert str(column_info(engine, 'book')['title']['type']) == 'VARCHAR(200)'
    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT note FROM book')).scalar() == 'keep'


def test_widening_a_decorated_string_is_still_a_widening(engine):
    """`case: upper` wraps the column in a TypeDecorator.

    It is not an instance of String, so an unwrapped comparison called a plain
    widening a decision to take by hand — and with `on_startup: error` that
    stops the server over an ALTER that SQLite cannot even run.
    """
    from coframe.db import CaseString

    md = base_metadata()
    md.tables['book'].c.title.type = CaseString(200, case='upper')

    diff = diff_schema(engine, md)

    assert [c.kind for c in diff.safe] == ['widen_type']
    assert diff.refused == []


def test_narrowing_a_decorated_string_is_still_refused(engine):
    """The unwrapping must not cost the refusal in the other direction."""
    from coframe.db import CaseString

    md = base_metadata()
    md.tables['book'].c.title.type = CaseString(10, case='upper')

    diff = diff_schema(engine, md)

    assert diff.safe == []
    assert 'longer than 10' in diff.refused[0].reason


def test_narrowing_a_string_that_does_not_fit_is_refused(engine):
    md = base_metadata()
    md.tables['book'].c.title.type = sa.String(10)

    diff = diff_schema(engine, md)

    assert diff.safe == []
    change = diff.refused[0]
    assert change.kind == 'narrow_string' and change.forceable
    assert '1 value(s) longer than 10' in change.reason
    apply_diff(engine, diff)
    assert str(column_info(engine, 'book')['title']['type']) == 'VARCHAR(50)'


# ── Decided by the data ────────────────────────────────────────────────────────

def test_narrowing_a_string_every_value_fits_is_applied(engine):
    """'Le città invisibili' is 19 characters: 20 loses nothing, so no decision."""
    md = base_metadata()
    md.tables['book'].c.title.type = sa.String(20)

    diff = diff_schema(engine, md)

    assert [c.kind for c in diff.safe] == ['narrow_string']
    assert diff.refused == []
    apply_diff(engine, diff)
    assert str(column_info(engine, 'book')['title']['type']) == 'VARCHAR(20)'
    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT title FROM book')).scalar() == 'Le città invisibili'
    assert diff_schema(engine, md).is_aligned


def test_narrowing_an_integer_every_value_fits_is_applied(engine):
    md = base_metadata()
    md.tables['book'].c.author_id.type = sa.SmallInteger()

    diff = diff_schema(engine, md)

    assert [c.kind for c in diff.safe] == ['narrow_int']
    apply_diff(engine, diff)
    assert diff_schema(engine, md).is_aligned


def test_an_integer_out_of_range_is_refused_even_forcing(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO author (id, name) VALUES (40000, 'Rodari')"))
        conn.execute(sa.text("INSERT INTO book (id, title, author_id) VALUES (2, 'Favole', 40000)"))
    md = base_metadata()
    md.tables['book'].c.author_id.type = sa.SmallInteger()

    diff = diff_schema(engine, md)

    change = by_target(diff)['book.author_id']
    assert change.verdict == REFUSED and not change.forceable
    assert '1 value(s) outside -32768..32767' in change.reason
    apply_diff(engine, diff, force=True)
    assert by_target(diff_schema(engine, md))['book.author_id'].kind == 'narrow_int'


# ── Forcing ────────────────────────────────────────────────────────────────────

def test_force_truncates_explicitly(engine):
    md = base_metadata()
    md.tables['book'].c.title.type = sa.String(10)

    apply_diff(engine, diff_schema(engine, md), force=True)

    assert str(column_info(engine, 'book')['title']['type']) == 'VARCHAR(10)'
    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT title FROM book')).scalar() == 'Le città i'
    assert diff_schema(engine, md).is_aligned


def test_force_drops_a_column_and_counts_what_it_loses(engine):
    md = base_metadata()
    md.tables['book']._columns.remove(md.tables['book'].c.note)

    diff = diff_schema(engine, md)
    change = by_target(diff)['book.note']
    assert change.forceable
    assert change.reason == 'dropping it loses 1 non-empty value(s)'

    apply_diff(engine, diff, force=True)

    assert 'note' not in column_info(engine, 'book')
    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT title FROM book')).scalar() == 'Le città invisibili'
    assert diff_schema(engine, md).is_aligned


def test_force_drops_an_index(engine):
    md = base_metadata()
    md.tables['book'].indexes.clear()

    apply_diff(engine, diff_schema(engine, md), force=True)

    assert sa.inspect(engine).get_indexes('book') == []
    assert diff_schema(engine, md).is_aligned


def test_force_leaves_what_is_not_forceable(engine):
    """Tightening a NOT NULL needs a value nobody has chosen: forcing is no answer."""
    md = base_metadata()
    md.tables['author'].c.name.nullable = False
    md.tables['book']._columns.remove(md.tables['book'].c.note)

    diff = diff_schema(engine, md)
    apply_diff(engine, diff, force=True)

    after = diff_schema(engine, md)
    assert [c.kind for c in after.refused] == ['modify_nullable']
    assert 'note' not in column_info(engine, 'book')


def test_dry_run_with_force_shows_the_truncation_and_the_drop(engine):
    md = base_metadata()
    md.tables['book'].c.title.type = sa.String(10)
    md.tables['book']._columns.remove(md.tables['book'].c.note)

    diff = diff_schema(engine, md)
    assert plan_sql(engine, diff) == ''
    sql = plan_sql(engine, diff, force=True)

    assert 'substr(book.title, 1, 10) WHERE length(book.title) > 10' in sql
    assert 'VARCHAR(10)' in sql
    copy = next(line for line in sql.splitlines() if line.startswith('INSERT INTO _alembic_tmp_book'))
    assert 'note' not in copy
    assert str(column_info(engine, 'book')['title']['type']) == 'VARCHAR(50)'


def test_table_missing_from_the_schema_is_silence(engine):
    """
    Deliberate asymmetry with the column case above: nothing tells a table
    someone removed from the YAML apart from a table belonging to a plugin
    that is not loaded, so the diff stays quiet — and the table stays put.
    """
    md = base_metadata()
    md.remove(md.tables['book'])

    diff = diff_schema(engine, md)

    assert diff.is_aligned
    apply_diff(engine, diff)
    assert 'book' in sa.inspect(engine).get_table_names()


def junction_metadata(*, surrogate_key):
    """A junction between book and author, keyed the old way or the new one."""
    md = sa.MetaData()
    sa.Table('author', md, sa.Column('id', sa.Integer, primary_key=True))
    sa.Table('book', md, sa.Column('id', sa.Integer, primary_key=True))
    columns = [
        sa.Column('book_id', sa.Integer, sa.ForeignKey('book.id'),
                  primary_key=not surrogate_key, nullable=False),
        sa.Column('author_id', sa.Integer, sa.ForeignKey('author.id'),
                  primary_key=not surrogate_key, nullable=False),
    ]
    if surrogate_key:
        columns.insert(0, sa.Column('id', sa.Integer, primary_key=True))
    sa.Table('books_authors', md, *columns)
    return md


def test_a_changed_primary_key_is_refused_and_named(tmp_path):
    """The comparison engine does not look at constraints: without this check the
    key change is invisible, and on an empty table the sync would add the column
    and then call the database aligned."""
    eng = make_engine(tmp_path, 'junction.sqlite')
    junction_metadata(surrogate_key=False).create_all(eng)

    diff = diff_schema(eng, junction_metadata(surrogate_key=True))

    key = by_target(diff)['books_authors']
    assert key.verdict == REFUSED
    assert key.description == 'primary key (book_id, author_id) -> (id)'
    apply_diff(eng, diff)
    assert sa.inspect(eng).get_pk_constraint(
        'books_authors')['constrained_columns'] == ['book_id', 'author_id']


def test_a_key_that_did_not_change_says_nothing(tmp_path):
    eng = make_engine(tmp_path, 'junction.sqlite')
    junction_metadata(surrogate_key=True).create_all(eng)

    assert diff_schema(eng, junction_metadata(surrogate_key=True)).is_aligned


# ── Dry run ────────────────────────────────────────────────────────────────────

def test_dry_run_renders_the_ddl_without_touching_the_database(engine):
    md = base_metadata()
    md.tables['book'].append_column(sa.Column('isbn', sa.String(20)))
    md.tables['book'].c.title.type = sa.String(200)

    diff = diff_schema(engine, md)
    sql = plan_sql(engine, diff)

    assert 'ALTER TABLE book ADD COLUMN isbn VARCHAR(20)' in sql
    assert 'CREATE TABLE _alembic_tmp_book' in sql       # the rebuild, spelled out
    assert 'INSERT INTO _alembic_tmp_book' in sql
    # Nothing ran: the same difference is still there.
    assert not diff_schema(engine, md).is_aligned
    assert 'isbn' not in column_info(engine, 'book')


def test_one_table_is_rebuilt_once_however_many_changes(engine):
    """
    Every alteration a table needs goes into a single pass: on SQLite each one
    would otherwise copy the whole table again.
    """
    md = base_metadata()
    md.tables['book'].c.title.type = sa.String(200)      # a widening
    md.tables['book'].append_column(                     # and a NOT NULL to lock
        sa.Column('copies', sa.Integer, nullable=False, default=1))

    diff = diff_schema(engine, md)
    assert len(diff.safe) == 2

    assert plan_sql(engine, diff).count('CREATE TABLE _alembic_tmp_book') == 1

    apply_diff(engine, diff)
    assert diff_schema(engine, md).is_aligned


def test_a_new_index_survives_a_rebuild_of_its_table(engine):
    """
    Order matters: a rebuild restores the indexes the table had when it was
    copied, so an index created before it would go down with the old table.
    """
    md = base_metadata()
    md.tables['book'].c.title.type = sa.String(200)
    md.tables['book'].append_column(sa.Column('isbn', sa.String(20)))
    sa.Index('idx_book_isbn', md.tables['book'].c.isbn)

    apply_diff(engine, diff_schema(engine, md))

    assert 'idx_book_isbn' in {i['name'] for i in sa.inspect(engine).get_indexes('book')}
    assert diff_schema(engine, md).is_aligned


def test_dry_run_does_not_write_data_either(engine):
    """
    A backfill is the one step of a sync that touches rows rather than shape:
    it has to go through the same rendering, or a dry run would edit data.
    """
    md = base_metadata()
    sa.Table('shelf', md, sa.Column('id', sa.Integer, primary_key=True))
    md.tables['book'].append_column(
        sa.Column('copies', sa.Integer, nullable=False, default=1))

    sql = plan_sql(engine, diff_schema(engine, md))

    assert 'UPDATE book SET copies=1' in sql.replace(' = ', '=')
    assert 'CREATE TABLE shelf' in sql
    assert 'shelf' not in sa.inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT COUNT(*) FROM book')).scalar() == 1
    assert 'copies' not in column_info(engine, 'book')


# ── Startup policy ─────────────────────────────────────────────────────────────

def stale_metadata():
    md = base_metadata()
    md.tables['book'].append_column(sa.Column('isbn', sa.String(20)))
    return md


def test_startup_check_stops_the_server_by_default(engine):
    with pytest.raises(SchemaOutOfDateError) as exc:
        check_on_startup(engine, stale_metadata(), 'error')

    assert 'book.isbn' in str(exc.value)
    assert 'db-sync' in str(exc.value)


def test_startup_check_can_only_warn(engine, caplog):
    check_on_startup(engine, stale_metadata(), 'warn')

    assert 'book.isbn' in caplog.text


def test_startup_check_off_does_not_even_compare(engine):
    assert check_on_startup(engine, stale_metadata(), 'off') is None


def test_startup_check_rejects_an_unknown_policy(engine):
    with pytest.raises(ValueError, match='on_startup'):
        check_on_startup(engine, base_metadata(), 'maybe')


def test_startup_check_passes_when_aligned(engine):
    assert check_on_startup(engine, base_metadata(), 'error').is_aligned


# ── The command: forcing asks first ───────────────────────────────────────────

class _AppStub:
    def __init__(self, engine):
        import logging
        from types import SimpleNamespace
        self.engine = engine
        self.pm = SimpleNamespace(logger=logging.getLogger('test'))


def _forced_schema(monkeypatch):
    import coframe.db
    from types import SimpleNamespace
    md = base_metadata()
    md.tables['book']._columns.remove(md.tables['book'].c.note)
    monkeypatch.setattr(coframe.db, 'Base', SimpleNamespace(metadata=md))
    return md


def test_db_sync_force_declined_applies_nothing(engine, monkeypatch):
    from coframe.cli import db_sync
    _forced_schema(monkeypatch)
    asked = []

    report, aligned = db_sync(_AppStub(engine), force=True,
                              confirm=lambda changes: asked.append(changes) or False)

    assert [c.target for c in asked[0]] == ['book.note']
    assert 'not confirmed' in report and not aligned
    assert 'note' in column_info(engine, 'book')


def test_db_sync_force_confirmed_applies(engine, monkeypatch):
    from coframe.cli import db_sync
    md = _forced_schema(monkeypatch)

    report, aligned = db_sync(_AppStub(engine), force=True, confirm=lambda changes: True)

    assert aligned
    assert 'note' not in column_info(engine, 'book')
    assert diff_schema(engine, md).is_aligned
