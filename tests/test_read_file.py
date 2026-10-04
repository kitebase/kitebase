"""Tests for kitebase.endpoint_files.read_file — which files it agrees to read.

The endpoint is closed unless `read_files.allowed_dirs` names directories, and
a directory admits only what lies inside it by path components.
"""
import pytest

import kitebase.utils
from kitebase.endpoint_files import read_file
from kitebase.plugins import PluginsManager


class FakeApp:
    def __init__(self, pm):
        self.pm = pm


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """An app directory with a readable `data/`, and files around it that are not."""
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'notes.txt').write_text('inside')
    (tmp_path / 'database').mkdir()
    (tmp_path / 'database' / 'secret.txt').write_text('neighbour')
    (tmp_path / '.env').write_text('SECRET=1')
    # The process runs elsewhere: relative paths must still hang from the app.
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return tmp_path


@pytest.fixture
def configure(app_dir, monkeypatch):
    def _set(read_files=None):
        pm = PluginsManager()
        pm.app_root = app_dir
        pm.config = {} if read_files is None else {'read_files': read_files}
        monkeypatch.setattr(kitebase.utils, 'get_app', lambda: FakeApp(pm))
    return _set


def test_without_allowed_dirs_nothing_is_read(configure):
    configure()
    result = read_file({'file_path': '.env'})
    assert result['code'] == 403


def test_an_empty_list_is_closed_too(configure):
    configure({'allowed_dirs': []})
    assert read_file({'file_path': '.env'})['code'] == 403


def test_a_file_inside_an_allowed_dir_is_read(configure):
    configure({'allowed_dirs': ['data']})
    result = read_file({'file_path': 'data/notes.txt'})
    assert result['status'] == 'success', result
    assert result['data'] == 'inside'


def test_a_sibling_sharing_the_prefix_is_refused(configure):
    configure({'allowed_dirs': ['data']})
    assert read_file({'file_path': 'database/secret.txt'})['code'] == 403


def test_climbing_out_is_refused(configure):
    configure({'allowed_dirs': ['data']})
    assert read_file({'file_path': 'data/../.env'})['code'] == 403


def test_base_dir_cannot_widen_the_list(configure, app_dir):
    configure({'allowed_dirs': ['data']})
    assert read_file({'base_dir': str(app_dir), 'file_path': '.env'})['code'] == 403


def test_a_symlink_is_judged_by_its_target(configure, app_dir):
    (app_dir / 'data' / 'link.txt').symlink_to(app_dir / '.env')
    configure({'allowed_dirs': ['data']})
    assert read_file({'file_path': 'data/link.txt'})['code'] == 403
