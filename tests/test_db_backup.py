"""A backup is a snapshot the engine reads, not a copy of the file.

`cp` on a live SQLite file can be torn, and in WAL mode misses what still sits
in `-wal`. The online backup API reads through the engine and writes a file
that opens cleanly with everything committed so far — while the server runs.
Other engines are refused by name, until an application runs on one.
"""
import sqlite3
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from kitebase.cli import db_backup


def app_on(url):
    return SimpleNamespace(engine=sa.create_engine(url))


@pytest.fixture
def live_db(tmp_path):
    path = tmp_path / 'app.sqlite'
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)')
    con.execute("INSERT INTO t (name) VALUES ('first')")
    con.commit()
    yield path, con
    con.close()


def test_the_copy_holds_what_is_committed_even_before_a_checkpoint(live_db):
    path, con = live_db
    con.execute("INSERT INTO t (name) VALUES ('in the wal')")
    con.commit()
    assert path.with_name('app.sqlite-wal').exists()

    report, target = db_backup(app_on(f'sqlite:///{path}'))

    rows = sqlite3.connect(target).execute('SELECT name FROM t ORDER BY id').fetchall()
    assert rows == [('first',), ('in the wal',)]
    assert 'Backup written' in report


def test_the_default_name_sits_next_to_the_database_and_is_stamped(live_db):
    path, _ = live_db
    _, target = db_backup(app_on(f'sqlite:///{path}'))
    assert target.startswith(str(path.parent / 'app-'))
    assert target.endswith('.sqlite')


def test_a_directory_keeps_the_original_name(live_db, tmp_path):
    path, _ = live_db
    out = tmp_path / 'backups'
    out.mkdir()
    _, target = db_backup(app_on(f'sqlite:///{path}'), str(out))
    assert target == str(out / 'app.sqlite')


def test_the_database_itself_is_not_a_destination(live_db):
    path, _ = live_db
    with pytest.raises(SystemExit, match='itself'):
        db_backup(app_on(f'sqlite:///{path}'), str(path))


def test_another_engine_is_refused_by_name():
    # No driver needed to be refused: the URL alone names the engine.
    app = SimpleNamespace(engine=SimpleNamespace(url=sa.make_url('postgresql://u:p@localhost/db')))
    with pytest.raises(SystemExit, match='postgresql'):
        db_backup(app)
