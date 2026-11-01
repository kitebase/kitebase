"""The media core without HTTP: references, path templates, the fs provider."""
from datetime import date

import pytest

from kitebase.media import (AllocContext, Media, MediaError, PathTemplate,
                            parse_ref, register_allocator, sanitize_name)


def chunks(data: bytes, size: int = 4):
    for i in range(0, len(data), size):
        yield data[i:i + size]


@pytest.fixture
def media(tmp_path):
    return Media({'roots': {
        'uploads': {'type': 'fs', 'path': str(tmp_path / 'uploads')},
        'records': {'type': 'fs', 'path': str(tmp_path / 'records'), 'allocate': 'by_record'},
        'plain':   {'type': 'fs', 'path': str(tmp_path / 'plain'), 'allocate': '{model}/{id}/{name}'},
    }})


# ── references ────────────────────────────────────────────────────────────────

def test_a_reference_is_a_root_name_and_a_relative_path():
    ref = parse_ref('uploads:2026/09/cover.jpg')
    assert (ref.root, ref.path, ref.is_folder) == ('uploads', '2026/09/cover.jpg', False)
    assert parse_ref('records:Author/000/12/documents/').is_folder


@pytest.mark.parametrize('value', [
    'cover.jpg',                    # no root
    'uploads:/etc/passwd',          # absolute
    'uploads:../secret',            # traversal
    'uploads:a/../../b',
    'uploads:a//b',
    'uploads:a\\b',
    'uploads:',
    '../x:a.jpg',                   # not a root name
])
def test_references_that_could_leave_a_root_are_refused(value):
    with pytest.raises(MediaError):
        parse_ref(value)


# ── names ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, clean', [
    ('Copertina finale.JPG', 'Copertina-finale.jpg'),
    ('../../etc/passwd', 'passwd'),
    ('C:\\Users\\x\\perché.pdf', 'perche.pdf'),
    ('.descriptions.yaml', 'descriptions.yaml'),
    ('', 'file'),
])
def test_names_are_sanitised(raw, clean):
    assert sanitize_name(raw) == clean


# ── templates ─────────────────────────────────────────────────────────────────

def test_named_templates_are_short_names():
    assert PathTemplate('by_record').source == '{model}/{id_k}/{id}/{name}'
    assert PathTemplate('yyyymm').source == '{yyyy}/{mm}/{name}'


@pytest.mark.parametrize('source, why', [
    ('{model}/{user}/{name}', 'Unknown placeholder'),
    ('{model}/{id!r}/{name}', 'Unknown placeholder'),
    ('{model}/{id}', 'must end with {name}'),
    ('{name}/{name}', 'must end with {name}|once'),
    ('{model}/{id_k}/{name}', 'followed by {id}'),
    ('/{model}/{name}', 'relative'),
])
def test_a_template_is_checked_when_it_is_built(source, why):
    with pytest.raises(MediaError, match=why):
        PathTemplate(source)


def test_id_k_is_computed_from_the_id():
    t = PathTemplate('by_record')
    assert t.expand(AllocContext('Customer', 'logo', 12, 'logo.png')) == 'Customer/000/12/logo.png'
    assert t.expand(AllocContext('Customer', 'logo', 12345, 'logo.png')) == 'Customer/012/12345/logo.png'


def test_a_template_with_the_id_waits_for_a_saved_record():
    t = PathTemplate('{model}/{id}/{name}')
    assert t.needs_record and not PathTemplate('yyyymm').needs_record
    with pytest.raises(MediaError, match='saved'):
        t.expand(AllocContext('Book', 'cover', None, 'a.jpg'))


def test_dates_are_those_of_the_upload():
    t = PathTemplate('{model}/{yyyy}/{mm}{dd}/{name}')
    ctx = AllocContext('Product', 'sheet', None, 'art15.pdf', day=date(2026, 11, 1))
    assert t.expand(ctx) == 'Product/2026/1101/art15.pdf'


def test_a_files_folder_puts_the_field_in_place_of_the_name():
    ctx = AllocContext('Author', 'documents', 12)
    assert PathTemplate('by_record').folder(ctx) == 'Author/000/12/documents/'


@pytest.mark.parametrize('source', ['yyyymm', 'flat', '{model}/{id}/{yyyy}/{name}'])
def test_a_files_folder_needs_a_template_of_the_record_alone(source):
    with pytest.raises(MediaError, match='Files'):
        PathTemplate(source).folder(AllocContext('Author', 'documents', 12))


# ── allocation ────────────────────────────────────────────────────────────────

def test_a_file_is_allocated_and_read_back(media):
    root = media.root('uploads')
    ctx = AllocContext('Book', 'cover', None, 'My Cover.JPG', day=date(2026, 9, 3))
    ref = root.allocate_file(ctx, chunks(b'jpeg bytes'))
    assert str(ref) == 'uploads:2026/09/My-Cover.jpg'
    with media.open(str(ref)) as f:
        assert f.read() == b'jpeg bytes'


def test_a_collision_gets_a_suffix_and_never_overwrites(media):
    root = media.root('uploads')
    refs = [root.allocate_file(AllocContext('Book', 'cover', None, 'a.jpg', day=date(2026, 9, 3)),
                               chunks(bytes([n]))) for n in range(3)]
    assert [r.path for r in refs] == ['2026/09/a.jpg', '2026/09/a-1.jpg', '2026/09/a-2.jpg']
    assert [media.open(str(r)).read() for r in refs] == [b'\x00', b'\x01', b'\x02']


def test_a_file_over_the_limit_is_refused_while_writing(media, tmp_path):
    root = media.root('uploads')
    ctx = AllocContext('Book', 'cover', None, 'big.jpg', day=date(2026, 9, 3))
    with pytest.raises(MediaError, match='larger'):
        root.allocate_file(ctx, chunks(b'x' * 100), max_size=10)
    assert not (tmp_path / 'uploads/2026/09/big.jpg').exists()


def test_a_python_strategy_gets_the_same_guarantees(tmp_path):
    register_allocator('test_escape', lambda name, ctx: f'../../{name}')
    register_allocator('test_fixed', lambda name, ctx: f'fixed/{name}')
    with pytest.raises(MediaError):
        Media({'roots': {'r': {'path': str(tmp_path), 'allocate': 'test_escape'}}}) \
            .root('r').allocate_file(AllocContext('M', 'f', 1, 'a.txt'), [b'x'])
    root = Media({'roots': {'r': {'path': str(tmp_path), 'allocate': 'test_fixed'}}}).root('r')
    first = root.allocate_file(AllocContext('M', 'f', 1, 'a.txt'), [b'x'])
    second = root.allocate_file(AllocContext('M', 'f', 1, 'a.txt'), [b'y'])
    assert (first.path, second.path) == ('fixed/a.txt', 'fixed/a-1.txt')


def test_a_root_with_a_bad_template_does_not_load(tmp_path):
    with pytest.raises(MediaError, match='media.roots.bad'):
        Media({'roots': {'bad': {'path': str(tmp_path), 'allocate': '{model}/{id_k}/{name}'}}})


def test_a_symlink_out_of_the_root_is_refused(media, tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text('secret')
    base = tmp_path / 'uploads'
    base.mkdir()
    (base / 'link.txt').symlink_to(outside)
    with pytest.raises(MediaError, match='leaves its root'):
        media.open('uploads:link.txt')


# ── Files folders ─────────────────────────────────────────────────────────────

def test_a_files_folder_is_found_on_disk(media, tmp_path):
    root = media.root('records')
    folder = root.folder(AllocContext('Author', 'documents', 12))
    assert str(folder) == 'records:Author/000/12/documents/'
    assert media.list(str(folder)) == []

    root.add_to_folder(folder, 'Contratto firmato.pdf', [b'%PDF'])
    # a file put there from outside shows up by itself
    (tmp_path / 'records/Author/000/12/documents/scan 01.png').write_bytes(b'png')

    entries = media.list(str(folder))
    assert [(e.name, e.mime, e.size) for e in entries] == [
        ('Contratto-firmato.pdf', 'application/pdf', 4),
        ('scan 01.png', 'image/png', 3),
    ]


def test_two_files_columns_of_a_record_have_two_folders(media):
    root = media.root('plain')
    todo = root.folder(AllocContext('Task', 'todo', 7))
    done = root.folder(AllocContext('Task', 'done', 7))
    assert (todo.path, done.path) == ('Task/7/todo/', 'Task/7/done/')


def test_descriptions_live_beside_the_files(media, tmp_path):
    root = media.root('records')
    folder = root.folder(AllocContext('Author', 'documents', 12))
    root.add_to_folder(folder, 'id-front.jpg', [b'a'])
    root.add_to_folder(folder, 'permit.pdf', [b'b'])
    provider = root.provider

    provider.describe(folder.path, 'permit.pdf', 'Residence permit, expires 2027')
    provider.describe(folder.path, 'id-front.jpg', "Identity card, front")
    assert [(e.name, e.description) for e in media.list(str(folder))] == [
        ('id-front.jpg', 'Identity card, front'),
        ('permit.pdf', 'Residence permit, expires 2027'),
    ]

    provider.trash(folder.path, 'permit.pdf')
    assert [e.name for e in media.list(str(folder))] == ['id-front.jpg']
    disk = tmp_path / 'records/Author/000/12/documents'
    assert (disk / '.trash/permit.pdf').exists()
    assert 'permit' not in (disk / '.descriptions.yaml').read_text()

    provider.describe(folder.path, 'id-front.jpg', '')
    assert not (disk / '.descriptions.yaml').exists()


def test_descriptions_hold_text_and_nothing_else(media, tmp_path):
    root = media.root('records')
    folder = root.folder(AllocContext('Author', 'documents', 12))
    root.add_to_folder(folder, 'a.pdf', [b'a'])
    (tmp_path / 'records/Author/000/12/documents/.descriptions.yaml').write_text(
        'a.pdf: {text: x, expires: 2027}\n')
    with pytest.raises(MediaError, match='nothing else'):
        media.list(str(folder))


@pytest.mark.parametrize('name', ['../x.pdf', '.descriptions.yaml', '', 'a/b.pdf'])
def test_folder_operations_name_one_file_of_the_folder(media, name):
    root = media.root('records')
    folder = root.folder(AllocContext('Author', 'documents', 12))
    with pytest.raises(MediaError):
        root.provider.trash(folder.path, name)


# ── check ─────────────────────────────────────────────────────────────────────

class _Col:
    def __init__(self, name, **attributes):
        self.name, self.attributes = name, attributes


class _Table:
    def __init__(self, *cols):
        self.effective_columns = list(cols)


class _App:
    def __init__(self, media, **tables):
        self.media, self.tables = media, tables


def codes(app):
    from kitebase.media import diagnose
    return sorted((i['path'].split('.')[-1], i['code'], i['severity']) for i in diagnose(app))


def test_check_accepts_the_columns_of_the_bench(media):
    app = _App(media, Book=_Table(
        _Col('cover', media='image', root='uploads'),
        _Col('sample', media='file', root='uploads', accept=['pdf'], max_size='5MB'),
        _Col('documents', media='files', root='records', virtual=True, accept=['pdf']),
        _Col('title'),
    ))
    assert codes(app) == []


def test_check_reports_what_breaks_an_upload(media):
    app = _App(media, Book=_Table(
        _Col('a', media='picture', root='uploads'),
        _Col('b', media='image'),
        _Col('c', media='image', root='nowhere'),
        _Col('d', media='files', root='uploads', virtual=True, accept=['pdf']),
        _Col('e', media='files', root='records', accept=['pdf']),
        _Col('f', media='image', root='uploads', max_size='lots'),
        _Col('g', media='file', root='uploads'),
    ))
    assert codes(app) == [
        ('a', 'media-kind', 'error'),
        ('b', 'media-root', 'error'),
        ('c', 'media-root', 'error'),
        ('d', 'media-files-root', 'error'),
        ('e', 'media-virtual', 'error'),
        ('f', 'media-size', 'error'),
        ('g', 'media-accept', 'warning'),
    ]


def test_sizes_read_as_bytes():
    from kitebase.media import parse_size
    assert [parse_size(v) for v in (None, 100, '500KB', '5MB', '1.5 mb')] == \
        [None, 100, 512000, 5242880, 1572864]


# ── signed URLs ───────────────────────────────────────────────────────────────

from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, unquote

DAY = date(2026, 11, 1)


@pytest.fixture
def signed(tmp_path):
    m = Media({'roots': {'covers': {'path': str(tmp_path / 'covers')}},
               'variants': {'thumb': {'fit': '96x96'}}})
    m.set_signing_key('the-app-secret')
    return m


def split(url):
    parts = urlsplit(url)
    _, root, path = parts.path.split('/', 2)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    return root, unquote(path), q.get('v', ''), q['e'], q['s']


def test_a_url_carries_root_path_variant_and_expiry(signed):
    url = signed.url('covers:2026/11/my cover.jpg', 'thumb', today=DAY)
    assert url.startswith('media/covers/2026/11/my%20cover.jpg?v=thumb&e=20261102&s=')
    ref = signed.verify(*split(url), today=DAY)
    assert str(ref) == 'covers:2026/11/my cover.jpg'


def test_the_url_is_the_same_all_day(signed):
    assert signed.url('covers:a.jpg', today=DAY) == signed.url('covers:a.jpg', today=DAY)


@pytest.mark.parametrize('change', [
    lambda r, p, v, e, s: (r, '2026/11/other.jpg', v, e, s),     # another file
    lambda r, p, v, e, s: (r, p, '', e, s),                      # the original instead of the thumb
    lambda r, p, v, e, s: (r, p, v, '20991231', s),               # a longer life
    lambda r, p, v, e, s: (r, p, v, e, s[:-2] + 'xx'),            # a forged signature
])
def test_a_changed_url_is_refused(signed, change):
    parts = split(signed.url('covers:2026/11/a.jpg', 'thumb', today=DAY))
    with pytest.raises(MediaError, match='signature'):
        signed.verify(*change(*parts), today=DAY)


def test_an_expired_url_is_refused(signed):
    parts = split(signed.url('covers:a.jpg', today=DAY))
    signed.verify(*parts, today=DAY + timedelta(days=1))
    with pytest.raises(MediaError, match='expired'):
        signed.verify(*parts, today=DAY + timedelta(days=2))


def test_another_secret_signs_differently(signed, tmp_path):
    other = Media({'roots': {'covers': {'path': str(tmp_path)}}})
    other.set_signing_key('another-secret')
    with pytest.raises(MediaError, match='signature'):
        other.verify(*split(signed.url('covers:a.jpg', today=DAY)), today=DAY)


def test_only_named_variants_are_signed(signed):
    with pytest.raises(MediaError, match='variant'):
        signed.url('covers:a.jpg', '2000x2000')


def test_nothing_is_signed_without_a_key(tmp_path):
    with pytest.raises(MediaError, match='signing key'):
        Media({'roots': {'covers': {'path': str(tmp_path)}}}).url('covers:a.jpg')


# ── serialisation and writes ──────────────────────────────────────────────────

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base

from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class StubTable:
    def __init__(self, *cols):
        self.effective_columns = list(cols)
        self.virtual_columns = []

    @property
    def secret_columns(self):
        return frozenset()


BOOK = StubTable(_Col('id', primary_key=True), _Col('title'),
                 _Col('cover', media='image', root='covers'))


class Book(Base):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    cover = Column(String)

    @classmethod
    def get_table_definition(cls):
        return BOOK


@pytest.fixture
def library(signed):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Book(id=1, title='1984', cover='covers:2026/11/a.jpg'),
                         Book(id=2, title='Emma', cover=None),
                         Book(id=3, title='Odd', cover='not a reference')])
        session.commit()
        yield session, SimpleNamespace(media=signed, models={'Book': Book})


def test_a_query_sends_urls_never_references(library):
    from kitebase.media import query_transform
    session, app = library
    builder = DynamicQueryBuilder(session, app.models,
                                  value_transform=query_transform(app, {'cover': 'thumb'}))
    rows = builder.execute_query({'table': 'Book', 'select': ['id', 'title', 'cover'],
                                  'order_by': ['id']}, result_format='records')
    assert rows[0]['cover'].startswith('media/covers/2026/11/a.jpg?v=thumb&')
    assert [r['cover'] for r in rows[1:]] == [None, None]
    assert [r['title'] for r in rows] == ['1984', 'Emma', 'Odd']


def test_a_query_asks_only_for_named_variants(library):
    from kitebase.media import query_transform
    _, app = library
    with pytest.raises(MediaError, match='variant'):
        query_transform(app, {'cover': 'huge'})


def test_a_record_sends_urls_never_references(library):
    from kitebase.media import sign_record
    _, app = library
    record = sign_record(app, BOOK, {'id': 1, 'title': '1984', 'cover': 'covers:2026/11/a.jpg'})
    assert record['cover'].startswith('media/covers/2026/11/a.jpg?e=')


@pytest.fixture
def writes(library, monkeypatch, tmp_path):
    from kitebase import utils
    from kitebase.endpoint_db import write_values
    _, app = library
    monkeypatch.setattr(utils, 'get_app', lambda: app)
    (tmp_path / 'covers/2026/11').mkdir(parents=True)
    (tmp_path / 'covers/2026/11/new.jpg').write_bytes(b'x')
    return lambda value: write_values(BOOK, {'title': 'T', 'cover': value})


def test_a_write_takes_the_reference_the_upload_gave(writes):
    assert writes('covers:2026/11/new.jpg') == {'title': 'T', 'cover': 'covers:2026/11/new.jpg'}


def test_a_write_clears_with_nothing(writes):
    assert writes(None)['cover'] is None
    assert writes('')['cover'] is None


def test_the_url_sent_back_on_save_leaves_the_value_alone(writes, signed):
    assert writes(signed.url('covers:2026/11/a.jpg')) == {'title': 'T'}


@pytest.mark.parametrize('value', ['records:2026/11/new.jpg', 'covers:2026/11/missing.jpg',
                                   'covers:2026/11/'])
def test_a_write_refuses_a_file_that_is_not_there(writes, value):
    from kitebase.transforms import ValidationError
    with pytest.raises(ValidationError):
        writes(value)
