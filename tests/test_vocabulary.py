"""The vocabulary of the merged YAML (kitebase/vocabulary.py, vocabulary.yaml).

A key nobody reads is dropped without a word: `nulable: false` leaves a column
nullable and says nothing. The vocabulary is what the applications we run use,
path by path, and `check` warns about a key outside it.
"""
from pathlib import Path

from kitebase import vocabulary as v
from tests.test_merge_golden import APPS, merged_tree

VOCAB = {
    'tables.*': ['columns', 'label'],
    'tables.*.columns[]': ['name', 'type', 'nullable', 'label'],
    'pages.*': ['content'],
    'pages.*.content': ['source'],
    'pages.*.content.source': ['model', 'defaults', 'joins'],
    'pages.*.content.source.defaults': v.OPEN,
    'pages.*.content.source.joins[]': v.OPEN,
    'pages.*.content.source.joins[].*': ['on', 'type'],
}


def issues(data):
    return [(i['path'], i['message']) for i in v.check(data, VOCAB)]


def test_a_typo_is_named_with_what_it_probably_meant():
    data = {'tables': {'Book': {'columns': [{'name': 'title', 'nulable': False}]}}}
    [(path, message)] = issues(data)
    assert path == 'tables.Book.columns[0].nulable'
    assert "'nulable' is not a known key here (did you mean 'nullable'?)" in message


def test_a_key_far_from_every_known_one_has_no_suggestion():
    [(_, message)] = issues({'tables': {'Book': {'columns': [{'name': 'x', 'zzz': 1}]}}})
    assert 'did you mean' not in message


def test_an_application_s_key_and_the_metadata_pass():
    data = {'tables': {'Book': {'$plugin': 'lib', 'columns': [
        {'name': 'title', 'myapp.owner': 'legacy', '$after': 'id'}]}}}
    assert issues(data) == []


def test_the_keys_of_an_open_map_are_data():
    data = {'pages': {'book_form': {'content': {'source': {
        'model': 'Book', 'defaults': {'published': '$op_date', 'anything': 1},
        'joins': [{'Author': {'type': 'left', 'on': 'Book.author_id = Author.id'}}]}}}}}
    assert issues(data) == []


def test_beneath_an_open_map_the_vocabulary_holds_again():
    data = {'pages': {'p': {'content': {'source': {
        'joins': [{'Author': {'tipe': 'left'}}]}}}}}
    [(path, message)] = issues(data)
    assert path == 'pages.p.content.source.joins[0].Author.tipe'
    assert "did you mean 'type'" in message


def test_the_update_adds_and_never_removes(tmp_path: Path):
    path = tmp_path / 'vocabulary.yaml'
    path.write_text(v.render({'tables.*': ['label', 'unused'], 'pages.*.props': v.OPEN}))

    added = v.update({'tables': {'Book': {'label': 'Books', 'help': 'x'}},
                      'pages': {'p': {'props': {'free': 1}}}}, path)

    assert added == 2          # tables.*: help; pages.*: props
    assert v.load(path) == {'tables.*': ['help', 'label', 'unused'],
                            'pages.*': ['props'], 'pages.*.props': v.OPEN}


def test_what_yaml_would_misread_is_written_so_it_reads_back():
    """`on` is a boolean to YAML when plain: a join's `on` must stay a string."""
    text = v.render({'pages.*.content.source.joins[].*': ['type', 'on']})
    assert "'on'" in text
    assert v.load_text(text) == {'pages.*.content.source.joins[].*': ['on', 'type']}


def test_the_shipped_vocabulary_covers_devtest(monkeypatch):
    """devtest is one of the applications it was made from: no key unknown."""
    data = merged_tree(APPS['devtest'], monkeypatch)
    assert [i['path'] for i in v.check(data, v.load())] == []


def test_a_server_start_writes_the_warnings_in_the_log(monkeypatch, caplog):
    """In development the typo is in the terminal at the next restart, without
    running `kitebase check`; the info lines stay in `check`."""
    import logging
    from kitebase import diagnostics
    from kitebase.server_utils import report_checks

    monkeypatch.setattr(diagnostics, 'run_checks', lambda app: [
        {'severity': 'warning', 'code': 'key-unknown', 'path': 'tables.Book.columns[1].nulable',
         'message': "did you mean 'nullable'?", 'plugin': 'lib'},
        {'severity': 'info', 'code': 'merge-overlap', 'path': 'pages.x.title',
         'message': 'overridden', 'plugin': 'lib'},
    ])
    with caplog.at_level(logging.WARNING, logger='kitebase'):
        report_checks(object())
    text = caplog.text
    assert 'key-unknown tables.Book.columns[1].nulable' in text
    assert 'merge-overlap' not in text
    assert '1 to look at' in text
