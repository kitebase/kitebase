"""A database and its media are one unit.

The default engine keeps its media where they always were; any other engine,
chosen by name, keeps them in <engines_dir>/<name>/, laid out the same way.
db-copy moves the files only with --media, staged until the rows are
committed; db-backup --media writes the unit, linking what did not change.
"""
import logging
import sqlite3
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from kitebase import secrets
from kitebase.cli import db_backup, db_copy
from kitebase.app import App
from kitebase.store import Store
from kitebase.db_copy import media_roots, snapshot_files
from kitebase.plugins import PluginsManager, check_engine_name

CONFIG = """\
name: t
plugins: []
db_engine: sqlite:///app.sqlite
media:
  engines_dir: data/engines
  roots:
    books:   { type: fs, path: media/books }
    records: { type: fs, path: media/records, allocate: by_record }
    cache:   { type: fs, path: media/cache }
  cache: cache
"""


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    for var in ('KITEBASE_ENGINE', 'KITEBASE_SECRETS', 'KITEBASE_ENGINES_DIR'):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / 'config.yaml').write_text(CONFIG)
    (tmp_path / 'secrets').mkdir()
    (tmp_path / 'secrets' / 'engines.yaml').write_text(
        "demo: sqlite:///data/engines/demo/app.sqlite\n")
    yield tmp_path
    secrets.set_app_root(None)


def manager(app_dir):
    pm = PluginsManager()
    pm.load_config(app_dir / 'config.yaml')
    return pm


# -- where the media are -----------------------------------------------------

def test_the_default_engine_keeps_the_app_directory(app_dir):
    pm = manager(app_dir)
    assert pm.media_base() == app_dir
    assert pm.resolve_media_path('media/books') == app_dir / 'media/books'


def test_another_engine_mirrors_the_layout_in_its_own_folder(app_dir, monkeypatch):
    monkeypatch.setenv('KITEBASE_ENGINE', 'demo')
    pm = manager(app_dir)
    assert pm.media_base() == app_dir / 'data/engines/demo'
    assert pm.resolve_media_path('media/books') == app_dir / 'data/engines/demo/media/books'


def test_an_absolute_root_is_the_same_for_every_engine(app_dir, monkeypatch, tmp_path_factory):
    shared = tmp_path_factory.mktemp('shared')
    monkeypatch.setenv('KITEBASE_ENGINE', 'demo')
    assert manager(app_dir).resolve_media_path(str(shared)) == shared


def test_the_environment_moves_the_engines_folder(app_dir, monkeypatch, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp('datasets')
    monkeypatch.setenv('KITEBASE_ENGINES_DIR', str(elsewhere))
    assert manager(app_dir).media_base('demo') == elsewhere / 'demo'


@pytest.mark.parametrize('name', ['Demo', 'my-db', '1st', 'con', 'nul', 'com3', 'lpt9', ''])
def test_an_engine_name_must_be_a_folder_name_everywhere(name):
    with pytest.raises(ValueError, match='Engine name'):
        check_engine_name(name)


def test_another_engine_is_chosen_by_name_not_by_url(app_dir, monkeypatch):
    monkeypatch.setenv('KITEBASE_ENGINE', 'sqlite:///other.sqlite')
    with pytest.raises(ValueError, match='not a URL'):
        manager(app_dir)


def test_choosing_the_default_by_name_changes_nothing(app_dir, monkeypatch):
    (app_dir / 'config.yaml').write_text(CONFIG.replace('sqlite:///app.sqlite', 'demo'))
    monkeypatch.setenv('KITEBASE_ENGINE', 'demo')
    pm = manager(app_dir)
    assert pm.engine_name is None and pm.media_base() == app_dir


def test_a_missing_media_folder_is_said_at_startup(app_dir, monkeypatch, caplog):
    monkeypatch.setenv('KITEBASE_ENGINE', 'demo')
    fake = SimpleNamespace(plugins=manager(app_dir), media=SimpleNamespace(roots={'books': None}))
    with caplog.at_level(logging.WARNING):
        App._warn_missing_media_base(fake)
    assert "Media of engine 'demo'" in caplog.text and 'does not exist' in caplog.text


def test_the_roots_of_a_database_leave_out_the_cache_and_the_absolute_ones():
    own, shared = media_roots({'roots': {'a': {'path': 'media/a'}, 'c': {'path': 'media/c'},
                                         'x': {'path': '/srv/x'}}, 'cache': 'c'})
    assert own == [('a', 'media/a')] and shared == ['x']


# -- db-copy --media ---------------------------------------------------------

@pytest.fixture
def metadata():
    md = sa.MetaData()
    sa.Table('books', md, sa.Column('id', sa.Integer, primary_key=True),
             sa.Column('cover', sa.String(200)))
    return md


@pytest.fixture
def app(app_dir, metadata):
    pm = manager(app_dir)
    engine = sa.create_engine(f"sqlite:///{app_dir / 'app.sqlite'}")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(metadata.tables['books'].insert(),
                     [{'id': 1, 'cover': 'books:202611/a.png'}])
    (app_dir / 'media/books/202611').mkdir(parents=True)
    (app_dir / 'media/books/202611/a.png').write_bytes(b'png')
    (app_dir / 'media/cache').mkdir()
    (app_dir / 'media/cache/thumb.png').write_bytes(b'small')
    target = app_dir / 'data/engines/demo/app.sqlite'
    target.parent.mkdir(parents=True)
    metadata.create_all(sa.create_engine(f'sqlite:///{target}'))

    resolve = lambda url: Store._resolve_db_url(SimpleNamespace(plugins=pm), url)
    return SimpleNamespace(plugins=pm, engine=engine, _resolve_db_url=resolve)


def test_without_media_only_the_rows_travel_and_the_report_says_so(app, app_dir, metadata):
    report, ok = db_copy(app, 'demo', metadata=metadata)
    assert ok and 'Media not copied' in report
    assert not (app_dir / 'data/engines/demo/media').exists()


def test_with_media_the_files_follow_into_the_target_folder(app, app_dir, metadata):
    report, ok = db_copy(app, 'demo', media=True, metadata=metadata)
    demo = app_dir / 'data/engines/demo'
    assert ok, report
    assert (demo / 'media/books/202611/a.png').read_bytes() == b'png'
    assert not (demo / 'media/cache').exists()
    assert not (demo / '.kitebase-copy').exists()
    assert 'Media: 1 file(s) copied' in report


def test_target_media_with_files_need_force(app, app_dir, metadata):
    stale = app_dir / 'data/engines/demo/media/books/old.png'
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b'old')
    report, ok = db_copy(app, 'demo', media=True, metadata=metadata)
    assert not ok and "media are not empty" in report
    report, ok = db_copy(app, 'demo', media=True, force=True, metadata=metadata)
    assert ok, report
    assert not stale.exists()
    assert (stale.parent / '202611/a.png').exists()


def test_a_failed_copy_leaves_the_target_media_as_they_were(app, app_dir, metadata):
    kept = app_dir / 'data/engines/demo/media/books/kept.png'
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b'kept')
    with sa.create_engine(f"sqlite:///{app_dir / 'data/engines/demo/app.sqlite'}").begin() as c:
        c.exec_driver_sql("CREATE TRIGGER no BEFORE INSERT ON books "
                          "BEGIN SELECT RAISE(ABORT, 'refused'); END")
    report, ok = db_copy(app, 'demo', media=True, force=True, metadata=metadata)
    assert not ok and 'Copying books' in report
    assert kept.read_bytes() == b'kept'
    assert not (app_dir / 'data/engines/demo/.kitebase-copy').exists()


def test_media_need_the_target_by_name(app, app_dir, metadata):
    report, ok = db_copy(app, f"sqlite:///{app_dir / 'x.sqlite'}", media=True,
                         metadata=metadata)
    assert not ok and 'by name' in report


# -- db-backup --media -------------------------------------------------------

def test_unchanged_files_are_linked_to_the_previous_snapshot(tmp_path):
    src = tmp_path / 'src'
    (src / 'a').mkdir(parents=True)
    (src / 'a/one.png').write_bytes(b'1')
    (src / 'a/two.png').write_bytes(b'2')
    assert snapshot_files(src, tmp_path / 'b1') == (2, 0)
    (src / 'a/two.png').write_bytes(b'22')
    import os
    os.utime(src / 'a/two.png', (1, 1))
    assert snapshot_files(src, tmp_path / 'b2', tmp_path / 'b1') == (1, 1)
    assert (tmp_path / 'b2/a/one.png').stat().st_ino == (tmp_path / 'b1/a/one.png').stat().st_ino
    assert (tmp_path / 'b2/a/two.png').read_bytes() == b'22'


def test_the_backup_unit_holds_the_database_and_its_media(app, app_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp('backups')
    report, first = db_backup(app, str(out), media=True)
    first = out / first.split('/')[-1]
    assert (first / 'app.sqlite').exists()
    assert (first / 'media/books/202611/a.png').read_bytes() == b'png'
    assert not (first / 'media/cache').exists()
    rows = sqlite3.connect(first / 'app.sqlite').execute('select cover from books').fetchall()
    assert rows == [('books:202611/a.png',)]
    assert 'nothing (first backup)' in report

    import time
    time.sleep(1.1)
    report, second = db_backup(app, str(out), media=True)
    assert '0 file(s) copied, 1 linked' in report
