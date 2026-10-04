"""The way Archivable reaches the user: one command, a menu, two verbs.

A view declares one navigator command pointing at `archivable`, and the client
sends the whole situation — which table, the active row, the ticked rows, the
query params in force. The answer is a `choose` menu built here, and every
entry carries what to do next ready-made: the three views set a query param
the behavior already honours, the verb on the row is a call to `archive` or
`unarchive` with its arguments filled in. The client knows none of it: it shows
a list, and does what the picked entry says.

Which verb appears is decided here too — only the one that applies to the
record, and none without a row — which is what a server-driven popup means
without any machinery on the client's side.
"""
import kitebase.utils
from kitebase.endpoints import endpoint
from kitebase.i18n import _, _f
from kitebase.utils import search_info, table_definition


def _archivable_model(data):
    """The model named by `table`, refused when it is not Archivable."""
    app = kitebase.utils.get_app()
    table = data.get('table')
    model = app.models.get(table) if table else None
    if model is None:
        return None, {'status': 'error', 'code': 400,
                      'message': _f("Unknown table '{table}'", table=table)}
    if not hasattr(model, '_kb_archive_field'):
        return None, {'status': 'error', 'code': 400,
                      'message': _f("'{table}' is not Archivable", table=table)}
    return model, None


def _label(record, model):
    """What to call a record in a menu entry: its display field, else its key."""
    info = search_info(table_definition(model))
    field = info.get('display_field') or info.get('search_pk') or 'id'
    return str(getattr(record, field, ''))


@endpoint('archivable')
def archivable(data):
    model, error = _archivable_model(data)
    if error:
        return error
    params = data.get('query_params') or {}
    archived = params.get('archived')
    table = data['table']

    def view(label, value):
        return {'label': label, 'current': archived == value,
                'then': {'action': 'set_query_params', 'params': {'archived': value}}}

    options = [
        view(_('Only active'), None),
        view(_('Only archived'), 'only'),
        view(_('Active and archived'), 'all'),
    ]

    ids = data.get('ids') or []
    app = kitebase.utils.get_app()
    if ids:
        n = len(ids)
        options.append({
            'label': _f('Archive {n} selected', n=n),
            'then': {'action': 'endpoint', 'op': 'archive', 'params': {'table': table, 'ids': ids},
                     'confirm': _f('Archive {n} records?', n=n)},
        })
        options.append({
            'label': _f('Restore {n} selected', n=n),
            'then': {'action': 'endpoint', 'op': 'unarchive', 'params': {'table': table, 'ids': ids}},
        })
    elif data.get('id') is not None:
        with app.get_session() as session:
            record = session.get(model, data['id'])
            if record is not None:
                verb = 'unarchive' if record.is_archived else 'archive'
                text = _('Restore "{name}"') if record.is_archived else _('Archive "{name}"')
                options.append({
                    'label': text.format(name=_label(record, model)),
                    'then': {'action': 'endpoint', 'op': verb,
                             'params': {'table': table, 'id': data['id']}},
                })

    return {'status': 'success', 'data': {'action': 'choose', 'options': options}}


def _flip(data, archive: bool):
    model, error = _archivable_model(data)
    if error:
        return error
    ids = data.get('ids') or ([data['id']] if data.get('id') is not None else [])
    if not ids:
        return {'status': 'error', 'code': 400, 'message': _('No record given')}

    app = kitebase.utils.get_app()
    touched = []
    with app.get_session() as session:
        for record_id in ids:
            record = session.get(model, record_id)
            if record is None or record.is_archived == archive:
                continue
            record.archive() if archive else record.unarchive()
            touched.append(record_id)
        session.commit()

    if not touched:
        message = _('Already archived') if archive else _('Already active')
    elif archive:
        message = _f('{n} archived', n=len(touched))
    else:
        message = _f('{n} restored', n=len(touched))
    # The keys the view should look at again: it re-asks its own query for
    # them and learns whether they still belong to what it shows.
    return {'status': 'success', 'data': {'message': message, 'touched': touched}}


@endpoint('archive')
def archive(data):
    """Hide the given records from the default views; nothing is deleted."""
    return _flip(data, True)


@endpoint('unarchive')
def unarchive(data):
    """Bring archived records back into the default views."""
    return _flip(data, False)
