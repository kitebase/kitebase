"""
kitebase.media — where files live and how a row refers to them.

A column holds a reference `root:path`: the name of a root declared in
config.yaml and a path relative to it. The root says where the bytes are and
how new paths are laid out; the reference never names a storage backend, so
moving a root is a line of configuration and not a migration of the data.

    media:
      roots:
        uploads: { type: fs, path: ./media/uploads }                # yyyymm
        records: { type: fs, path: ./media/records, allocate: by_record }

The layout is a path template made of a closed set of placeholders, or one of
three short names for the common ones. A template proposes a path; the core
guarantees what must not depend on whoever wrote it: the path is relative,
never leaves its root, the name is sanitised, and an existing file is never
overwritten (a collision gets a numeric suffix).

Two kinds of media column:

- Image and File store a reference to one file, allocated on upload.
- Files stores nothing: its folder is computed from the record, the template
  with `{field}/` in place of `{name}`, and its files are found on disk. Its
  template may therefore use only placeholders of the record.

This module is the mechanism without HTTP: roots, references, templates, the
filesystem provider. Routes, signed URLs and upload checks build on it.
"""
import base64
import hashlib
import hmac
import os
import re
import string
import tempfile
import unicodedata
import mimetypes
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterable, List, Optional

import yaml

from kitebase import apptime


class MediaError(ValueError):
    """A media reference, template or operation the core refuses.

    `status` is the HTTP answer it becomes at a route: 400 by default, 413 for
    a file too large, 415 for one of a type the column does not take.
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── References ────────────────────────────────────────────────────────────────

_ROOT_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]*$')


@dataclass(frozen=True)
class MediaRef:
    """A parsed `root:path` reference. A folder path ends with '/'."""
    root: str
    path: str

    @property
    def is_folder(self) -> bool:
        return self.path.endswith('/')

    def __str__(self) -> str:
        return f'{self.root}:{self.path}'


def check_relative_path(path: str) -> str:
    """Return `path` if it is a safe relative path, else raise MediaError.

    Safe means: not empty, no leading '/', no backslash or NUL, no empty, '.'
    or '..' segment. A trailing '/' (a folder) is allowed.
    """
    if not path or path.startswith('/'):
        raise MediaError(f'Media path must be relative and not empty: {path!r}')
    if '\\' in path or '\x00' in path:
        raise MediaError(f'Media path holds a forbidden character: {path!r}')
    segments = path[:-1].split('/') if path.endswith('/') else path.split('/')
    for segment in segments:
        if segment in ('', '.', '..'):
            raise MediaError(f'Media path holds an empty or relative segment: {path!r}')
    return path


def parse_ref(value: str) -> MediaRef:
    """Parse and validate a `root:path` string."""
    if not isinstance(value, str) or ':' not in value:
        raise MediaError(f'Not a media reference (root:path): {value!r}')
    root, path = value.split(':', 1)
    if not _ROOT_NAME.match(root):
        raise MediaError(f'Not a root name: {root!r}')
    return MediaRef(root, check_relative_path(path))


# ── Names ─────────────────────────────────────────────────────────────────────

_UNSAFE_CHARS = re.compile(r'[^A-Za-z0-9._-]+')
_MAX_NAME = 120


def sanitize_name(name: str) -> str:
    """A file name safe on any filesystem and inside a URL.

    Only the last component of what the client sent is kept. Accents are
    folded to ASCII, anything outside letters, digits, '.', '_' and '-'
    becomes '-', and a leading dot is dropped: dot names are the core's own
    (.descriptions.yaml, .trash/). The extension is lower-cased, because the
    MIME type is read from it.
    """
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    name = _UNSAFE_CHARS.sub('-', name).strip('-.')
    name = re.sub(r'-{2,}', '-', name)
    stem, dot, ext = name.rpartition('.')
    if dot and stem:
        name = f'{stem}.{ext.lower()}'
    if not name:
        name = 'file'
    if len(name) > _MAX_NAME:
        stem, dot, ext = name.rpartition('.')
        name = (stem[:_MAX_NAME - len(ext) - 1] + '.' + ext) if dot and stem else name[:_MAX_NAME]
    return name


def mime_type(name: str) -> str:
    """The MIME type of a file, from its extension."""
    return mimetypes.guess_type(name)[0] or 'application/octet-stream'


# ── Path templates ────────────────────────────────────────────────────────────

# The closed set. Record placeholders can be computed again at any time; the
# others describe one upload.
RECORD_PLACEHOLDERS = frozenset({'model', 'field', 'id', 'id_k'})
UPLOAD_PLACEHOLDERS = frozenset({'yyyy', 'mm', 'dd', 'name'})
PLACEHOLDERS = RECORD_PLACEHOLDERS | UPLOAD_PLACEHOLDERS

NAMED_TEMPLATES = {
    'yyyymm': '{yyyy}/{mm}/{name}',
    'by_record': '{model}/{id_k}/{id}/{name}',
    'flat': '{name}',
}


@dataclass
class AllocContext:
    """What a path is computed from. `record_id` is None for an unsaved record."""
    model: str
    field: str
    record_id: Any = None
    name: str = ''
    day: Optional[date] = None


class PathTemplate:
    """A layout declared with placeholders, checked when it is built.

    `{id_k}` is `id // 1000` on three digits, always computed from the id, and
    is valid only followed by `{id}`: alone it would put a thousand records in
    one folder and mix their files. The template must end with `{name}`, the
    segment a Files column replaces with `{field}/`.
    """

    def __init__(self, source: str):
        self.source = NAMED_TEMPLATES.get(source, source)
        self.placeholders: List[str] = []
        for _, name, spec, conv in string.Formatter().parse(self.source):
            if name is None:
                continue
            if name not in PLACEHOLDERS or spec or conv:
                raise MediaError(
                    f'Unknown placeholder {{{name}}} in path template {source!r}; '
                    f'allowed: {", ".join(sorted(PLACEHOLDERS))}')
            self.placeholders.append(name)
        if not (self.source == '{name}' or self.source.endswith('/{name}')):
            raise MediaError(f'Path template {source!r} must end with {{name}}')
        if self.placeholders.count('name') != 1:
            raise MediaError(f'Path template {source!r} must hold {{name}} once')
        if 'id_k' in self.placeholders:
            k = self.placeholders.index('id_k')
            if 'id' not in self.placeholders[k + 1:]:
                raise MediaError(
                    f'Path template {source!r}: {{id_k}} must be followed by {{id}}, '
                    f'or a thousand records would share one folder')
        check_relative_path(self.source.replace('{', 'x').replace('}', 'x'))

    @property
    def needs_record(self) -> bool:
        """True when the record must be saved before a file is allocated."""
        return 'id' in self.placeholders

    @property
    def record_only(self) -> bool:
        """True when the folder can be computed from the record alone (Files)."""
        return self.needs_record and not (set(self.placeholders) - RECORD_PLACEHOLDERS - {'name'})

    def expand(self, ctx: AllocContext) -> str:
        """The relative path for a file, with `ctx.name` already sanitised."""
        return self._format(ctx, name=ctx.name)

    def folder(self, ctx: AllocContext) -> str:
        """The folder of a Files column: `{field}/` in place of `{name}`."""
        if not self.record_only:
            raise MediaError(
                f'Path template {self.source!r} cannot hold a Files column: '
                f'its folder must be computed from the record alone, with {{id}}')
        return self._format(ctx, name=_segment(ctx.field)) + '/'

    def _format(self, ctx: AllocContext, name: str) -> str:
        values: Dict[str, str] = {'name': name, 'model': _segment(ctx.model),
                                  'field': _segment(ctx.field)}
        if 'id' in self.placeholders:
            if ctx.record_id is None:
                raise MediaError('The record must be saved before a file is allocated')
            values['id'] = _segment(str(ctx.record_id))
            if 'id_k' in self.placeholders:
                if not isinstance(ctx.record_id, int):
                    raise MediaError(f'{{id_k}} needs an integer id, not {ctx.record_id!r}')
                values['id_k'] = f'{ctx.record_id // 1000:03d}'
        day = ctx.day or apptime.today()
        values.update(yyyy=f'{day.year:04d}', mm=f'{day.month:02d}', dd=f'{day.day:02d}')
        return check_relative_path(self.source.format(**values))


def _segment(value: str) -> str:
    """A placeholder value as one path segment."""
    return sanitize_name(value)


# ── Strategies written in Python ──────────────────────────────────────────────

# name -> callable(name, ctx) -> relative path. For the layouts a template
# cannot express; the core applies the same guarantees to what they return.
_STRATEGIES: Dict[str, Callable[[str, AllocContext], str]] = {}


def register_allocator(name: str, func: Callable[[str, AllocContext], str]) -> None:
    """Register a layout written in Python, named in a root as `allocate: <name>`."""
    if name in NAMED_TEMPLATES or '{' in name:
        raise MediaError(f'Allocator name {name!r} clashes with a template')
    _STRATEGIES[name] = func


# ── Provider: the filesystem ──────────────────────────────────────────────────

DESCRIPTIONS_FILE = '.descriptions.yaml'
TRASH_DIR = '.trash'


@dataclass
class FileEntry:
    """A file of a Files folder."""
    name: str
    size: int
    mime: str
    modified: float
    description: str = ''


class FsProvider:
    """A root on the local filesystem.

    Every path is resolved and checked inside the root again, symlinks
    included: the signature of a URL protects the URL, not a value written in
    the database some other way.
    """

    def __init__(self, path: Path):
        self.base = Path(path).resolve()

    def locate(self, rel: str) -> Path:
        """The absolute path of `rel`, refused if it falls outside the root."""
        check_relative_path(rel)
        target = (self.base / rel).resolve()
        if target != self.base and self.base not in target.parents:
            raise MediaError(f'Media path leaves its root: {rel!r}')
        return target

    def exists(self, rel: str) -> bool:
        return self.locate(rel).exists()

    def open(self, rel: str) -> BinaryIO:
        """The file's bytes, for the server: renderer, jobs, the GET route."""
        target = self.locate(rel)
        if not target.is_file():
            raise FileNotFoundError(rel)
        return open(target, 'rb')

    def create(self, rel: str, chunks: Iterable[bytes], max_size: Optional[int] = None) -> str:
        """Write a new file at `rel`, or at `rel` with a suffix if it is taken.

        Creation is exclusive, so two uploads of the same name never overwrite
        each other. The size limit is applied while writing: a file over it is
        removed and refused. Returns the relative path actually written.
        """
        target = self.locate(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        stem, ext = _split_ext(target.name)
        for n in range(0, 10000):
            candidate = target if n == 0 else target.with_name(f'{stem}-{n}{ext}')
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            break
        else:
            raise MediaError(f'No free name for {rel!r}')
        written = 0
        try:
            with os.fdopen(fd, 'wb') as f:
                for chunk in chunks:
                    written += len(chunk)
                    if max_size is not None and written > max_size:
                        raise MediaError(f'File larger than {max_size} bytes', 413)
                    f.write(chunk)
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        return candidate.relative_to(self.base).as_posix()

    def list(self, folder: str) -> List[FileEntry]:
        """The files of a folder, by name, with their descriptions."""
        target = self.locate(folder)
        if not target.is_dir():
            return []
        descriptions = self._read_descriptions(target)
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: p.name):
            if item.name.startswith('.') or not item.is_file():
                continue
            st = item.stat()
            entries.append(FileEntry(item.name, st.st_size, mime_type(item.name),
                                     st.st_mtime, descriptions.get(item.name, '')))
        return entries

    def trash(self, folder: str, name: str) -> None:
        """Move a file of a folder into its .trash/, never deleting it."""
        target = self._member(folder, name)
        if not target.is_file():
            raise FileNotFoundError(f'{folder}{name}')
        trash = target.parent / TRASH_DIR
        trash.mkdir(exist_ok=True)
        stem, ext = _split_ext(name)
        dest = trash / name
        n = 0
        while dest.exists():
            n += 1
            dest = trash / f'{stem}-{n}{ext}'
        target.rename(dest)
        self._write_descriptions(target.parent, {})

    def describe(self, folder: str, name: str, text: str) -> None:
        """Set the description of a file; an empty text removes it."""
        target = self._member(folder, name)
        if not target.is_file():
            raise FileNotFoundError(f'{folder}{name}')
        self._write_descriptions(target.parent, {name: text.strip()})

    def _member(self, folder: str, name: str) -> Path:
        if not folder.endswith('/'):
            raise MediaError(f'Not a folder: {folder!r}')
        # Not sanitize_name(): a file put there from outside keeps its own name.
        if not name or '/' in name or name.startswith('.'):
            raise MediaError(f'Not a file name of this folder: {name!r}')
        return self.locate(folder + name)

    @staticmethod
    def _read_descriptions(folder: Path) -> Dict[str, str]:
        path = folder / DESCRIPTIONS_FILE
        if not path.is_file():
            return {}
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise MediaError(f'{path} must map file names to text, and nothing else')
        return data

    def _write_descriptions(self, folder: Path, changes: Dict[str, str]) -> None:
        """Rewrite the descriptions atomically, dropping files no longer there."""
        data = self._read_descriptions(folder)
        data.update(changes)
        data = {k: v for k, v in data.items() if v and (folder / k).is_file()}
        path = folder / DESCRIPTIONS_FILE
        if not data:
            path.unlink(missing_ok=True)
            return
        fd, tmp = tempfile.mkstemp(dir=folder, prefix='.descriptions-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                yaml.safe_dump(dict(sorted(data.items())), f, allow_unicode=True,
                               sort_keys=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _split_ext(name: str):
    stem, dot, ext = name.rpartition('.')
    return (stem, '.' + ext) if dot and stem else (name, '')


_PROVIDERS: Dict[str, Callable[[Dict[str, Any], Callable[[str], Path]], Any]] = {
    'fs': lambda decl, resolve: FsProvider(resolve(decl['path'])),
}


# ── Roots ─────────────────────────────────────────────────────────────────────

@dataclass
class Root:
    """A named place for files, with its backend and its layout."""
    name: str
    provider: Any
    allocate: str = 'yyyymm'
    template: Optional[PathTemplate] = None
    strategy: Optional[Callable[[str, AllocContext], str]] = None

    @property
    def needs_record(self) -> bool:
        """True when uploads wait for a saved record. A Python strategy decides
        for itself: it receives `record_id`, None for a new record."""
        return self.template.needs_record if self.template else False

    def allocate_file(self, ctx: AllocContext, chunks: Iterable[bytes],
                      max_size: Optional[int] = None) -> MediaRef:
        """Store a new file and return its reference. Never overwrites."""
        ctx.name = sanitize_name(ctx.name)
        if self.template:
            rel = self.template.expand(ctx)
        else:
            assert self.strategy is not None
            rel = check_relative_path(self.strategy(ctx.name, ctx))
            if rel.endswith('/'):
                raise MediaError(f'Allocator {self.allocate!r} returned a folder: {rel!r}')
        return MediaRef(self.name, self.provider.create(rel, chunks, max_size))

    def folder(self, ctx: AllocContext) -> MediaRef:
        """The folder of a Files column for one record."""
        if not self.template:
            raise MediaError(f'Root {self.name!r}: a Files column needs a path template')
        return MediaRef(self.name, self.template.folder(ctx))

    def add_to_folder(self, folder: MediaRef, name: str, chunks: Iterable[bytes],
                      max_size: Optional[int] = None) -> MediaRef:
        """Store a new file in a Files folder. Never overwrites."""
        rel = self.provider.create(folder.path + sanitize_name(name), chunks, max_size)
        return MediaRef(self.name, rel)


_FIT = re.compile(r'^\s*(\d+)\s*x\s*(\d+)\s*$')


def _parse_fit(variant: str, value: Any) -> tuple:
    """`fit: 96x96`, the box a rendition fits in, keeping its proportions."""
    m = _FIT.match(str(value or ''))
    if not m or not all(int(n) > 0 for n in m.groups()):
        raise MediaError(f'media.variants.{variant}: fit must be WIDTHxHEIGHT, e.g. 96x96')
    return int(m.group(1)), int(m.group(2))


class Media:
    """The roots an application declared under `media:` in config.yaml."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 resolve_path: Callable[[str], Path] = Path):
        config = config or {}
        self.roots: Dict[str, Root] = {}
        for name, decl in (config.get('roots') or {}).items():
            self.roots[name] = self._build_root(name, decl or {}, resolve_path)
        # Named variants only: a size free in the URL would let anyone make the
        # server resize anything to anything.
        self.variants: Dict[str, Dict[str, Any]] = dict(config.get('variants') or {})
        self.fits: Dict[str, tuple] = {}
        for name, decl in self.variants.items():
            if not _ROOT_NAME.match(name):
                raise MediaError(f'media.variants: {name!r} is not a variant name')
            self.fits[name] = _parse_fit(name, (decl or {}).get('fit'))
        # The root renditions are written to: a declared root like the others,
        # never one a column stores in, so it can be emptied at any time.
        self.cache: Optional[str] = config.get('cache')
        if self.cache is not None:
            if self.cache not in self.roots:
                raise MediaError(f'media.cache: {self.cache!r} is not in media.roots')
            if not isinstance(self.roots[self.cache].provider, FsProvider):
                raise MediaError(f'media.cache: root {self.cache!r} must be of type fs')
        self.max_pixels: int = int(config.get('max_pixels', 40_000_000))
        self.url_days: int = int(config.get('url_days', 1))
        self._key: Optional[bytes] = None

    @staticmethod
    def _build_root(name: str, decl: Dict[str, Any], resolve_path) -> Root:
        if not _ROOT_NAME.match(name):
            raise MediaError(f'media.roots: {name!r} is not a root name')
        kind = decl.get('type', 'fs')
        if kind not in _PROVIDERS:
            raise MediaError(f'media.roots.{name}: unknown type {kind!r}')
        allocate = decl.get('allocate', 'yyyymm')
        root = Root(name, _PROVIDERS[kind](decl, resolve_path), allocate)
        if allocate in _STRATEGIES:
            root.strategy = _STRATEGIES[allocate]
        else:
            try:
                root.template = PathTemplate(allocate)
            except MediaError as e:
                raise MediaError(f'media.roots.{name}: {e}') from None
        return root

    def root(self, name: str) -> Root:
        try:
            return self.roots[name]
        except KeyError:
            raise MediaError(f'No media root named {name!r}') from None

    def open(self, ref: str) -> BinaryIO:
        """The bytes behind a reference, for code that runs on the server."""
        r = parse_ref(ref)
        return self.root(r.root).provider.open(r.path)

    def list(self, ref: str) -> List[FileEntry]:
        """The files of a folder reference."""
        r = parse_ref(ref)
        if not r.is_folder:
            raise MediaError(f'Not a folder reference: {ref!r}')
        return self.root(r.root).provider.list(r.path)


    # ── Signed URLs ───────────────────────────────────────────────────────────
    #
    # A row's media value leaves the server as a URL that carries its own
    # permission: root, path, variant and expiry under an HMAC. It is minted when
    # the row is serialised, so the permission is the one already checked reading
    # the row; an <img> can then fetch it with a plain GET, no header needed. The
    # expiry is a day, not an instant, so the URL stays the same all day and the
    # browser cache works. The URL is relative to the API prefix: the client puts
    # its own base in front.

    def set_signing_key(self, secret_key: str) -> None:
        """Derive the key URLs are signed with from the application's secret.

        Derived, not shared: a URL signature is never valid as a token, nor the
        other way round.
        """
        self._key = hmac.new(secret_key.encode(), b'kitebase.media.url', hashlib.sha256).digest()

    def _signature(self, root: str, path: str, variant: str, expires: str) -> str:
        if self._key is None:
            raise MediaError('Media URLs cannot be signed: no signing key (register the routes first)')
        message = '\n'.join((root, path, variant, expires)).encode()
        digest = hmac.new(self._key, message, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()

    def url(self, ref: str, variant: str = '', today: Optional[date] = None) -> str:
        """The signed URL of a stored reference, in a variant or as it is."""
        r = parse_ref(ref)
        self.root(r.root)
        if variant and variant not in self.variants:
            raise MediaError(f'No media variant named {variant!r}')
        day = (today or apptime.today()) + timedelta(days=self.url_days)
        expires = day.strftime('%Y%m%d')
        query = f'e={expires}&s={self._signature(r.root, r.path, variant, expires)}'
        if variant:
            query = f'v={variant}&' + query
        return f'media/{r.root}/{quote(r.path)}?{query}'

    def verify(self, root: str, path: str, variant: str, expires: str,
               signature: str, today: Optional[date] = None) -> MediaRef:
        """The reference a URL stands for, or MediaError if it is not one we signed."""
        expected = self._signature(root, path, variant, expires)
        if not hmac.compare_digest(expected, signature or ''):
            raise MediaError('Bad media signature')
        if expires < (today or apptime.today()).strftime('%Y%m%d'):
            raise MediaError('Media URL expired')
        return parse_ref(f'{root}:{path}')


# ── Columns ───────────────────────────────────────────────────────────────────

# `media:` on a column (usually through its type) is what the core reads; the
# names Image, File, Files are vocabulary of the plugins that declare them.
MEDIA_KINDS = ('image', 'file', 'files')

_SIZE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([KMG]?B)?\s*$', re.IGNORECASE)
_UNITS = {None: 1, 'B': 1, 'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3}


def parse_size(value: Any) -> Optional[int]:
    """`max_size` as bytes: an integer, or text such as '5MB'."""
    if value is None or isinstance(value, int):
        return value
    m = _SIZE.match(str(value))
    if not m:
        raise MediaError(f'Not a size: {value!r} (e.g. 500KB, 5MB)')
    return int(float(m.group(1)) * _UNITS[m.group(2) and m.group(2).upper()])


def media_columns(table: Any) -> List[Any]:
    """The media columns of a table definition."""
    return [c for c in table.effective_columns if c.attributes.get('media')]


def column_schema(media: Optional[Media], attrs: Dict[str, Any]) -> Dict[str, Any]:
    """What the client knows of a media column: its kind and its limits.

    `max_size` leaves in bytes; `needs_record` says whether an upload waits
    for the record to be saved, which only the root's layout knows. A column
    `check` would refuse leaves with what it has, and its uploads fail there.
    """
    out: Dict[str, Any] = {k: attrs[k] for k in ('media', 'root', 'accept', 'resize') if k in attrs}
    try:
        out['max_size'] = parse_size(attrs.get('max_size'))
    except MediaError:
        pass
    root = (media.roots.get(attrs.get('root')) if media else None)
    out['needs_record'] = attrs['media'] == 'files' or bool(root and root.needs_record)
    return out


def diagnose(app: Any) -> List[Dict[str, Any]]:
    """What `check` reports on the media columns, against the declared roots.

    An error is a column whose uploads cannot work: no root, an unknown one, a
    Files column that is stored or on a root whose folder depends on the
    upload. A File or Files column without `accept:` is a warning: it refuses
    every upload until it says which files it takes.
    """
    from kitebase.diagnostics import make_issue

    media = getattr(app, 'media', None) or Media()
    issues = []
    for t_name, table in (getattr(app, 'tables', None) or {}).items():
        for col in media_columns(table):
            path = f'tables.{t_name}.{col.name}'
            a = col.attributes

            def error(code: str, message: str) -> None:
                issues.append(make_issue('error', code, path, message))

            kind = a['media']
            if kind not in MEDIA_KINDS:
                error('media-kind', f"media '{kind}' is not one of {', '.join(MEDIA_KINDS)}")
                continue
            if bool(a.get('virtual')) != (kind == 'files'):
                error('media-virtual', 'a files column has no stored value and is virtual: true; '
                      'an image or file column is stored, never virtual')
            try:
                parse_size(a.get('max_size'))
            except MediaError as e:
                error('media-size', str(e))
            root_name = a.get('root')
            if not root_name:
                error('media-root', 'a media column names its root: root: <name of media.roots>')
                continue
            if root_name == media.cache:
                error('media-cache', f"root '{root_name}' is media.cache, where renditions "
                      f"are written and emptied: a column stores elsewhere")
                continue
            root = media.roots.get(root_name)
            if root is None:
                known = ', '.join(sorted(media.roots)) or 'none declared'
                error('media-root', f"root '{root_name}' is not in media.roots ({known})")
                continue
            if kind == 'files' and not (root.template and root.template.record_only):
                error('media-files-root',
                      f"root '{root_name}' ({root.allocate}) cannot hold a files column: "
                      f"its folder must come from the record alone, with {{id}} (e.g. by_record)")
            if kind != 'image' and not a.get('accept'):
                issues.append(make_issue(
                    'warning', 'media-accept', path,
                    'declare accept: [extensions]; until then every upload is refused'))
    if media.variants and media.cache is None:
        issues.append(make_issue(
            'warning', 'media-cache', 'media.cache',
            'media.variants are declared but media.cache names no root: '
            'every variant is refused until it does'))
    return issues



# ── Values in and out ─────────────────────────────────────────────────────────

def _signed(media: Media, value: Any, variant: str = '') -> Optional[str]:
    """The URL of a stored value; None when there is nothing usable to sign."""
    if not value:
        return None
    try:
        return media.url(value, variant)
    except MediaError:
        # A value the core cannot read (written by hand, a root since removed)
        # is shown as no file rather than breaking the list it is in.
        return None


def _stored_columns(table: Any) -> Dict[str, Dict[str, Any]]:
    return {c.name: c.attributes for c in media_columns(table)
            if c.attributes['media'] in ('image', 'file')}


def sign_record(app: Any, table: Any, record: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the stored references of a serialised record with their URLs."""
    media = getattr(app, 'media', None)
    if media is None or table is None:
        return record
    for name in _stored_columns(table):
        if name in record:
            record[name] = _signed(media, record[name])
    return record


def query_transform(app: Any, variants: Optional[Dict[str, str]] = None):
    """A `value_transform` for the querybuilder: media columns come out as URLs.

    `variants` maps an output column to a named variant (`{cover: thumb}`);
    the names are checked here, before anything runs.
    """
    from sqlalchemy import Column
    from sqlalchemy.sql.elements import Label

    media = getattr(app, 'media', None)
    variants = dict(variants or {})
    if media is not None:
        for variant in variants.values():
            if variant not in media.variants:
                raise MediaError(f'No media variant named {variant!r}')
    tables = {model.__table__: model for model in app.models.values()
              if hasattr(model, '__table__')}

    def transform(name: str, element: Any) -> Optional[Callable[[Any], Any]]:
        if media is None:
            return None
        while isinstance(element, Label):
            element = element.element
        if not isinstance(element, Column):
            return None
        table = element.table
        while table not in tables and hasattr(table, 'element'):
            table = table.element          # an aliased join
        model = tables.get(table)
        definition = model.get_table_definition() if model is not None else None
        if definition is None or element.key not in _stored_columns(definition):
            return None
        variant = variants.get(name, '')
        return lambda value: _signed(media, value, variant)

    return transform


def incoming(media: Media, attrs: Dict[str, Any], value: Any) -> Any:
    """What a write may put in a media column.

    None or '' clears it. A reference is kept if it names the column's root
    and a file that exists: what the upload route answered. Anything that is
    not a reference, such as the URL the client was sent and echoes back on
    save, means "unchanged" and returns KEEP. A reference to another root or
    to a missing file is an error.
    """
    if attrs['media'] == 'files':
        return KEEP
    if value is None or value == '':
        return None
    try:
        ref = parse_ref(value)
    except MediaError:
        return KEEP
    if ref.root != attrs.get('root') or ref.is_folder:
        raise MediaError(f"Not a file of root '{attrs.get('root')}': {value}")
    if not media.root(ref.root).provider.exists(ref.path):
        raise MediaError(f'No such file: {value}')
    return str(ref)


KEEP = object()


# ── What an upload really is ──────────────────────────────────────────────────

@dataclass
class UploadedFile:
    """A file arriving at the upload route: its name and its bytes, in chunks.

    Only the route builds one: a JSON payload cannot hold it, so the upload
    endpoint called through the dispatcher refuses itself.
    """
    filename: str
    chunks: Iterable[bytes]


# Spellings of one format: what the extension says and what the bytes say
# must agree up to these.
_SAME_FORMAT = {'jpeg': 'jpg', 'tif': 'tiff', 'htm': 'html'}

# Formats with no signature in their first bytes: accepted on the word of
# their extension only, and always served as an attachment.
TEXT_EXTENSIONS = frozenset({'txt', 'csv', 'tsv', 'xml', 'json', 'md', 'eml', 'log', 'yaml', 'yml'})

# Served inline: the formats a browser shows without running anything.
INLINE_TYPES = frozenset({'image/jpeg', 'image/png', 'image/gif', 'image/webp',
                          'image/avif', 'image/bmp', 'application/pdf'})

_RASTER = frozenset({'jpg', 'png', 'gif', 'webp', 'avif', 'bmp', 'tiff', 'heic'})
_HEAD = 512


def _format(ext: str) -> str:
    ext = ext.lower()
    return _SAME_FORMAT.get(ext, ext)


def checked_upload(kind: str, accept: Iterable[str], upload: UploadedFile) -> Iterable[bytes]:
    """The chunks of an upload, after its first bytes have said what it is.

    The extension must be in `accept`, and the content must be what the
    extension says: the type is read from the bytes, never from the client.
    An image column takes raster formats only, whatever `accept` says.
    Raises MediaError(415) before anything is written.
    """
    import filetype

    name = sanitize_name(upload.filename)
    _, _, ext = name.rpartition('.')
    ext = _format(ext) if '.' in name else ''
    allowed = {_format(a) for a in (accept or ())}
    if not ext or ext not in allowed:
        raise MediaError(f"'{upload.filename}': this field takes {', '.join(sorted(allowed)) or 'no file'}", 415)

    chunks = iter(upload.chunks)
    head = b''
    for chunk in chunks:
        head += chunk
        if len(head) >= _HEAD:
            break
    guess = filetype.guess(head) if head else None
    if guess is not None:
        if _format(guess.extension) != ext:
            raise MediaError(f"'{upload.filename}' is a {guess.extension} file, not {ext}", 415)
    elif ext not in TEXT_EXTENSIONS:
        raise MediaError(f"'{upload.filename}' is not a {ext} file", 415)
    if kind == 'image' and ext not in _RASTER:
        raise MediaError(f"'{upload.filename}' is not an image this field can show", 415)

    def stream():
        if head:
            yield head
        yield from chunks

    return stream()


# ── The GET route, without a framework ────────────────────────────────────────

@dataclass
class MediaReply:
    """What the GET route answers: a file to send, or a status with a message."""
    status: int
    headers: Dict[str, str]
    path: Optional[Path] = None
    message: str = ''


def serve(media: Media, root: str, path: str, params: Dict[str, str],
          if_none_match: Optional[str] = None) -> MediaReply:
    """Answer `GET media/<root>/<path>?v=&e=&s=`.

    The signature is the permission; the path is checked inside its root
    again, because the signature protects the URL, not a value written in the
    database some other way. A type that could run in the browser (HTML,
    SVG, anything not in INLINE_TYPES) leaves as an attachment, and nothing
    is ever sniffed or framed.
    """
    variant = params.get('v', '')
    try:
        ref = media.verify(root, path, variant, params.get('e', ''), params.get('s', ''))
    except MediaError as e:
        return MediaReply(403, {}, message=str(e))
    provider = media.root(ref.root).provider
    try:
        target = provider.locate(ref.path)
    except MediaError as e:
        return MediaReply(404, {}, message=str(e))
    if not target.is_file():
        return MediaReply(404, {}, message='No such file')
    if variant:
        # A variant that cannot be made is an error, never the full original
        # in place of a thumbnail.
        try:
            target = rendition(media, ref, variant, target)
        except MediaError as e:
            return MediaReply(e.status, {}, message=str(e))

    st = target.stat()
    etag = f'"{st.st_size:x}-{int(st.st_mtime):x}"'
    mime = mime_type(target.name)
    inline = mime in INLINE_TYPES
    headers = {
        'ETag': etag,
        'Cache-Control': f'private, max-age={media.url_days * 86400}',
        'X-Content-Type-Options': 'nosniff',
        # The PDF viewer of a browser does not run in a sandbox; everything
        # else gets one, and no file can load anything.
        'Content-Security-Policy': "default-src 'none'" + ('' if mime == 'application/pdf' else '; sandbox'),
        'Content-Type': mime,
        'Content-Disposition': _disposition('inline' if inline else 'attachment', target.name),
    }
    if if_none_match and etag in [t.strip() for t in if_none_match.split(',')]:
        return MediaReply(304, {k: headers[k] for k in ('ETag', 'Cache-Control')})
    return MediaReply(200, headers, path=target)


def _disposition(kind: str, name: str) -> str:
    ascii_name = name.encode('ascii', 'replace').decode('ascii').replace('"', '_').replace('?', '_')
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


# ── Renditions ────────────────────────────────────────────────────────────────

# What Pillow writes a rendition as, by the extension of the original: the
# rendition keeps the original's format and path, so its MIME type and its
# headers are those of an image like the original.
_RENDITION_FORMATS = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'gif': 'GIF',
                      'webp': 'WEBP', 'bmp': 'BMP'}


def rendition(media: Media, ref: MediaRef, variant: str, source: Path) -> Path:
    """The file of a named variant of an image, made on the first request.

    It lives in the root `media.cache` at `<variant>/<root>/<path>`, and is
    made again when the original is newer. The pixels are counted before the
    image is decoded, against `media.max_pixels`. Raises MediaError: 501 when
    the server cannot make variants (no cache root, no Pillow), 415 for an
    original that is not a raster image it can write, 413 for one too large.
    """
    if media.cache is None:
        raise MediaError('media.cache names no root: variants cannot be made', 501)
    ext = source.suffix[1:].lower()
    fmt = _RENDITION_FORMATS.get(ext)
    if fmt is None:
        raise MediaError(f"A {ext or 'file'} has no variant '{variant}': not an image", 415)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise MediaError('Variants need Pillow: install kitebase[media]', 501) from None

    target = media.root(media.cache).provider.locate(f'{variant}/{ref.root}/{ref.path}')
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    try:
        with Image.open(source) as im:
            width, height = im.size
            if width * height > media.max_pixels:
                raise MediaError(f'Image of {width}x{height} pixels is over media.max_pixels', 413)
            im.seek(0)          # an animated GIF gives its first frame
            out = ImageOps.exif_transpose(im)
            if fmt == 'JPEG' and out.mode not in ('RGB', 'L'):
                out = out.convert('RGB')
            out.thumbnail(media.fits[variant])
            target.parent.mkdir(parents=True, exist_ok=True)
            # Written aside and renamed: two requests making the same
            # rendition never serve a half-written file.
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.rendition-')
            try:
                with os.fdopen(fd, 'wb') as f:
                    out.save(f, fmt)
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
    except (Image.DecompressionBombError, Image.UnidentifiedImageError, OSError) as e:
        raise MediaError(f"Variant '{variant}' cannot be made: {e}", 415) from None
    return target
