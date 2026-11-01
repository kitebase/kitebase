"""
The operations on media columns that go through the dispatcher.

`media_upload` stores a file and answers its reference; it is called by the
upload route, which is the only caller able to hand it the bytes. The value
of the column is not written here: the form puts the reference in the record
and saves it, so an upload in a form that is then cancelled changes nothing
but leaves a file behind, for the GC.

A Files column has no value to save, so its operations act at once:
`media_list`, `media_describe`, `media_trash`, and `media_upload` itself.

Every operation asks what the same change to the record would ask: the
table's permission, the row's condition, the column's group.
"""
from typing import Any, Dict, Optional, Tuple

import kitebase.utils
from kitebase import access, media
from kitebase.access import AccessDenied
from kitebase.endpoint_db import pk_field
from kitebase.endpoints import endpoint
from kitebase.media import AllocContext, MediaError, UploadedFile


def _column(app: Any, data: Dict[str, Any]) -> Tuple[Any, Any, str, Dict[str, Any]]:
    """The model, its definition, the field and its attributes, or MediaError."""
    table, field = data.get('table'), data.get('field')
    model = app.models.get(table)
    definition = model.get_table_definition() if model is not None else None
    if definition is None:
        raise MediaError(f'No table {table!r}')
    attrs = next((c.attributes for c in media.media_columns(definition) if c.name == field), None)
    if attrs is None:
        raise MediaError(f'{table}.{field} is not a media column')
    return model, definition, field, attrs


def _record_id(session: Any, model: Any, definition: Any, raw: Any, action: str) -> Optional[Any]:
    """The key of the record, typed as the database has it, after the checks of
    `action` on its row. None for a record not saved yet."""
    if raw in (None, ''):
        return None
    record = session.get(model, raw)
    if record is None or not access.row_visible(session, model, raw):
        raise MediaError(f'No {model.__name__} with id {raw}', 404)
    if action != 'read':
        access.check_row(session, model, raw, action)
    return getattr(record, pk_field(definition))


def _guard_column(model: Any, definition: Any, field: str, write: bool) -> None:
    if write and field in access.unwritable_columns(definition):
        raise AccessDenied(f'{model.__name__}.{field}', f'Not allowed to change {field}')
    access.check_columns(model.__name__, [field])


def _error(e: MediaError) -> Dict[str, Any]:
    return {'status': 'error', 'message': str(e), 'code': e.status}


def _entry(app: Any, folder: media.MediaRef, entry: media.FileEntry) -> Dict[str, Any]:
    return {'name': entry.name, 'size': entry.size, 'mime': entry.mime,
            'modified': entry.modified, 'description': entry.description,
            'url': app.media.url(f'{folder}{entry.name}')}


@endpoint('media_upload', access=access.PER_TABLE)
def media_upload(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Store an uploaded file for a media column.

    Parameters (form fields of the upload route):
        table, field: the column
        id:           the record, absent for one not saved yet
        file:         the UploadedFile, from the route only

    Returns:
        Image/File: {ref, url}, the reference to save in the record
        Files:      the entry of the new file in its folder
    """
    upload = data.get('file')
    if not isinstance(upload, UploadedFile):
        return {'status': 'error', 'code': 400,
                'message': 'A file is uploaded through the upload route, not the dispatcher'}
    app = kitebase.utils.get_app()
    try:
        model, definition, field, attrs = _column(app, data)
        root = app.media.root(attrs['root'])
        kind = attrs['media']
        with app.get_session() as session:
            raw = data.get('id')
            access.check_table(model.__name__, 'update' if raw not in (None, '') else 'create')
            record_id = _record_id(session, model, definition, raw, 'update')
        _guard_column(model, definition, field, write=True)

        ctx = AllocContext(model.__name__, field, record_id, upload.filename)
        if (kind == 'files' or root.needs_record) and record_id is None:
            raise MediaError('Save the record before adding files to it')
        chunks = media.checked_upload(kind, attrs.get('accept') or (), upload)
        max_size = media.parse_size(attrs.get('max_size'))

        if kind == 'files':
            folder = root.folder(ctx)
            ref = root.add_to_folder(folder, upload.filename, chunks, max_size)
            name = ref.path.rsplit('/', 1)[-1]
            entry = next(e for e in root.provider.list(folder.path) if e.name == name)
            return {'status': 'success', 'data': _entry(app, folder, entry)}

        ref = root.allocate_file(ctx, chunks, max_size)
        return {'status': 'success', 'data': {'ref': str(ref), 'url': app.media.url(str(ref))}}
    except MediaError as e:
        return _error(e)


def _folder(data: Dict[str, Any], action: str) -> Tuple[Any, media.MediaRef]:
    """The folder of a Files column for one record, after the checks of `action`."""
    app = kitebase.utils.get_app()
    model, definition, field, attrs = _column(app, data)
    if attrs['media'] != 'files':
        raise MediaError(f'{model.__name__}.{field} is not a files column')
    access.check_table(model.__name__, action)
    with app.get_session() as session:
        record_id = _record_id(session, model, definition, data.get('id'), action)
    if record_id is None:
        raise MediaError('A record id is required')
    _guard_column(model, definition, field, write=action != 'read')
    root = app.media.root(attrs['root'])
    return root, root.folder(AllocContext(model.__name__, field, record_id))


@endpoint('media_list', access=access.PER_TABLE)
def media_list(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    The files of a Files column for one record.

    Parameters: table, field, id
    Returns: [{name, size, mime, modified, description, url}], by name
    """
    try:
        root, folder = _folder(data, 'read')
        app = kitebase.utils.get_app()
        return {'status': 'success',
                'data': [_entry(app, folder, e) for e in root.provider.list(folder.path)]}
    except MediaError as e:
        return _error(e)


@endpoint('media_describe', access=access.PER_TABLE)
def media_describe(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Set the description of a file of a Files column; an empty text removes it.

    Parameters: table, field, id, name, description
    """
    try:
        root, folder = _folder(data, 'update')
        root.provider.describe(folder.path, data.get('name') or '', data.get('description') or '')
        return {'status': 'success', 'data': None}
    except FileNotFoundError:
        return {'status': 'error', 'message': 'No such file', 'code': 404}
    except MediaError as e:
        return _error(e)


@endpoint('media_trash', access=access.PER_TABLE)
def media_trash(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Move a file of a Files column into the .trash/ of its folder.

    Parameters: table, field, id, name
    """
    try:
        root, folder = _folder(data, 'update')
        root.provider.trash(folder.path, data.get('name') or '')
        return {'status': 'success', 'data': None}
    except FileNotFoundError:
        return {'status': 'error', 'message': 'No such file', 'code': 404}
    except MediaError as e:
        return _error(e)
