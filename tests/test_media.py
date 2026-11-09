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
        self.ref_sets = {}


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


# ── what an upload really is ──────────────────────────────────────────────────

from kitebase.media import UploadedFile, checked_upload, serve

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 600
JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 600
PDF = b'%PDF-1.7\n' + b'x' * 600
HTML = b'<html><script>alert(1)</script></html>'


def upload(name, data, kind='file', accept=('pdf', 'jpg', 'png', 'txt')):
    return b''.join(checked_upload(kind, accept, UploadedFile(name, chunks(data, 100))))


def test_an_upload_passes_whole_when_its_bytes_agree_with_its_name():
    assert upload('scan.png', PNG) == PNG
    assert upload('photo.JPEG', JPG) == JPG          # jpeg and jpg are one format
    assert upload('notes.txt', b'plain text') == b'plain text'


@pytest.mark.parametrize('name, data, why', [
    ('cover.png', JPG, 'is a jpg file, not png'),      # renamed
    ('invoice.pdf', HTML, 'is not a pdf file'),         # a page dressed as a PDF
    ('page.html', HTML, 'takes'),                       # not accepted at all
    ('noextension', PDF, 'takes'),
])
def test_an_upload_that_lies_about_itself_is_refused(name, data, why):
    with pytest.raises(MediaError, match=why) as e:
        upload(name, data)
    assert e.value.status == 415


def test_an_image_column_takes_raster_images_only():
    assert upload('a.png', PNG, kind='image') == PNG
    with pytest.raises(MediaError, match='image'):
        upload('a.pdf', PDF, kind='image')


# ── the GET route ─────────────────────────────────────────────────────────────

def stored(signed, tmp_path, rel, data):
    target = tmp_path / 'covers' / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return split(signed.url(f'covers:{rel}'))


def get(signed, parts, **extra):
    root, path, variant, e, s = parts
    params = {'e': e, 's': s, **({'v': variant} if variant else {})}
    return serve(signed, root, path, params, **extra)


def test_a_signed_url_serves_its_file(signed, tmp_path):
    reply = get(signed, stored(signed, tmp_path, '2026/11/a.png', PNG))
    assert reply.status == 200 and reply.path.read_bytes() == PNG
    h = reply.headers
    assert h['Content-Type'] == 'image/png'
    assert h['Content-Disposition'].startswith('inline;')
    assert h['X-Content-Type-Options'] == 'nosniff'
    assert 'sandbox' in h['Content-Security-Policy']
    assert h['Cache-Control'].startswith('private, max-age=')


def test_an_unchanged_file_answers_304(signed, tmp_path):
    parts = stored(signed, tmp_path, 'a.png', PNG)
    etag = get(signed, parts).headers['ETag']
    assert get(signed, parts, if_none_match=etag).status == 304


@pytest.mark.parametrize('name', ['page.html', 'logo.svg', 'data.xml'])
def test_what_could_run_in_the_browser_leaves_as_an_attachment(signed, tmp_path, name):
    reply = get(signed, stored(signed, tmp_path, name, HTML))
    assert reply.headers['Content-Disposition'].startswith('attachment;')


def test_a_pdf_opens_in_the_browser_viewer(signed, tmp_path):
    h = get(signed, stored(signed, tmp_path, 'a.pdf', PDF)).headers
    assert h['Content-Disposition'].startswith('inline;')
    assert 'sandbox' not in h['Content-Security-Policy']


def test_a_bad_signature_is_403_and_a_missing_file_404(signed, tmp_path):
    root, path, variant, e, s = stored(signed, tmp_path, 'a.png', PNG)
    assert get(signed, (root, 'b.png', variant, e, s)).status == 403
    assert get(signed, split(signed.url('covers:gone.png'))).status == 404


def test_without_a_cache_a_variant_is_501_never_the_original(signed, tmp_path):
    stored(signed, tmp_path, 'a.png', PNG)
    reply = get(signed, split(signed.url('covers:a.png', 'thumb')))
    assert reply.status == 501 and reply.path is None


# ── renditions ────────────────────────────────────────────────────────────────

def image_bytes(size, fmt='PNG', mode='RGB'):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new(mode, size, 'red').save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def cached(tmp_path):
    m = Media({'roots': {'covers': {'path': str(tmp_path / 'covers')},
                         'thumbs': {'path': str(tmp_path / 'thumbs')}},
               'variants': {'thumb': {'fit': '96x96'}},
               'cache': 'thumbs'})
    m.set_signing_key('the-app-secret')
    return m


def test_a_variant_is_made_in_the_cache_and_fits_its_box(cached, tmp_path):
    from PIL import Image
    stored(cached, tmp_path, '2026/11/a.png', image_bytes((800, 400)))
    reply = get(cached, split(cached.url('covers:2026/11/a.png', 'thumb')))
    assert reply.status == 200
    assert reply.path == tmp_path / 'thumbs' / 'thumb' / 'covers' / '2026/11/a.png'
    assert reply.headers['Content-Type'] == 'image/png'
    with Image.open(reply.path) as im:
        assert im.size == (96, 48)


def test_a_variant_is_made_once_and_again_when_the_original_is_newer(cached, tmp_path):
    import os
    stored(cached, tmp_path, 'a.jpg', image_bytes((300, 300), 'JPEG'))
    url = split(cached.url('covers:a.jpg', 'thumb'))
    made = get(cached, url).path.stat()
    assert get(cached, url).path.stat().st_ino == made.st_ino
    original = tmp_path / 'covers' / 'a.jpg'
    os.utime(original, (made.st_mtime + 10, made.st_mtime + 10))
    assert get(cached, url).path.stat().st_ino != made.st_ino


def test_a_png_variant_keeps_its_transparency(cached, tmp_path):
    from PIL import Image
    stored(cached, tmp_path, 'a.png', image_bytes((200, 200), 'PNG', 'RGBA'))
    with Image.open(get(cached, split(cached.url('covers:a.png', 'thumb'))).path) as im:
        assert im.mode == 'RGBA'


def test_a_variant_of_what_is_not_an_image_is_415(cached, tmp_path):
    stored(cached, tmp_path, 'a.pdf', PDF)
    assert get(cached, split(cached.url('covers:a.pdf', 'thumb'))).status == 415
    stored(cached, tmp_path, 'lies.png', PNG)        # a PNG header and nothing more
    assert get(cached, split(cached.url('covers:lies.png', 'thumb'))).status == 415


def test_an_image_over_max_pixels_is_413_before_it_is_decoded(cached, tmp_path):
    cached.max_pixels = 100 * 100
    stored(cached, tmp_path, 'big.png', image_bytes((101, 100)))
    reply = get(cached, split(cached.url('covers:big.png', 'thumb')))
    assert reply.status == 413 and reply.path is None


@pytest.mark.parametrize('config, why', [
    ({'variants': {'thumb': {'fit': 'small'}}}, 'WIDTHxHEIGHT'),
    ({'variants': {'thumb': {}}}, 'WIDTHxHEIGHT'),
    ({'variants': {'thumb': {'fit': '0x10'}}}, 'WIDTHxHEIGHT'),
    ({'cache': 'nowhere'}, 'not in media.roots'),
    ({'variants': {'thumb': {'fit': '96x96'}}, 'list_variant': 'tiny'}, 'not in media.variants'),
])
def test_variants_and_cache_are_checked_when_they_load(tmp_path, config, why):
    with pytest.raises(MediaError, match=why):
        Media({'roots': {'covers': {'path': str(tmp_path)}}, **config})


def test_check_keeps_columns_out_of_the_cache_and_asks_for_one(cached, signed):
    app = _App(cached, Book=_Table(_Col('cover', media='image', root='thumbs')))
    assert codes(app) == [('cover', 'media-cache', 'error')]
    app = _App(signed, Book=_Table(_Col('cover', media='image', root='covers')))
    assert codes(app) == [('cache', 'media-cache', 'warning')]


def test_the_client_learns_the_kind_limits_and_whether_to_save_first(media):
    from kitebase.media import column_schema
    image = column_schema(media, {'media': 'image', 'root': 'uploads', 'accept': ['png'],
                                  'max_size': '1MB', 'resize': 2048, 'length': 255})
    assert image == {'media': 'image', 'root': 'uploads', 'accept': ['png'],
                     'resize': 2048, 'max_size': 1024 ** 2, 'needs_record': False}
    assert column_schema(media, {'media': 'image', 'root': 'records'})['needs_record']
    assert column_schema(media, {'media': 'files', 'root': 'records'})['needs_record']


def test_a_generated_list_shows_images_in_the_list_variant(cached):
    from kitebase.pages import auto_list_page
    table = _Table(_Col('id'), _Col('cover', media='image', root='covers'),
                   _Col('sample', media='file', root='covers'))
    columns = lambda m: {c['field']: c.get('variant') for c in
                         auto_list_page('Book', table, m)['content']['columns']}

    assert columns(cached) == {'id': None, 'cover': None, 'sample': None}
    cached.list_variant = 'thumb'
    assert columns(cached) == {'id': None, 'cover': 'thumb', 'sample': None}


def test_check_asks_a_grid_for_an_existing_variant_of_its_images(cached):
    from kitebase.diagnostics import _check_view
    app = _App(cached, Book=_Table(_Col('title'), _Col('cover', media='image', root='covers')))

    def issues(*columns):
        found = []
        _check_view(app, {'source': {'model': 'Book'}, 'columns': list(columns)},
                    'views.books', None, found)
        return [(i['code'], i['severity']) for i in found]

    assert issues({'field': 'title'}, {'field': 'cover', 'variant': 'thumb'}) == []
    assert issues({'field': 'cover'}) == [('media-variant', 'warning')]
    assert issues({'field': 'cover', 'variant': 'huge'}) == [('media-variant', 'error')]


def test_a_files_column_stays_out_of_grids(cached):
    from kitebase.diagnostics import _check_view
    from kitebase.pages import auto_list_page
    table = _Table(_Col('name'), _Col('documents', media='files', root='covers', virtual=True))
    assert [c['field'] for c in auto_list_page('Author', table, cached)['content']['columns']] == ['name']

    found = []
    _check_view(_App(cached, Author=table),
                {'source': {'model': 'Author'}, 'columns': [{'field': 'documents'}]},
                'views.authors', None, found)
    assert [(i['code'], i['severity']) for i in found] == [('media-files', 'error')]


def test_a_listed_image_carries_the_thumb_of_the_list_variant(cached):
    from kitebase.endpoint_media import _entry
    from kitebase.media import FileEntry, parse_ref
    folder = parse_ref('covers:Author/000/1/documents/')
    app = _App(cached)
    photo = FileEntry('photo.jpg', 10, 'image/jpeg', 0.0, '')
    paper = FileEntry('paper.pdf', 10, 'application/pdf', 0.0, '')

    assert 'thumb' not in _entry(app, folder, photo)
    cached.list_variant = 'thumb'
    assert split(_entry(app, folder, photo)['thumb'])[2] == 'thumb'
    assert 'thumb' not in _entry(app, folder, paper)


def test_a_generated_form_lists_a_files_column_and_no_other_virtual(cached):
    from kitebase.pages import auto_form_page
    table = _Table(_Col('name'), _Col('label', virtual=True),
                   _Col('documents', media='files', root='covers', virtual=True))
    names = [f['name'] for f in auto_form_page('Author', table)['content']['fields']]
    assert names == ['name', 'documents']
