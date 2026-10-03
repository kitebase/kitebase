"""Page descriptors, and the tree a page declares.

Two things live here, and the second is the reason the first left the endpoint
module: resolving a page by id, and walking the resolved descriptor for the
collection nodes it carries.

**The seam.** A save arrives with a page id and never with a tree of tables: the
server loads its *own* descriptor to learn which collections exist, in which
table, and through which foreign key. If the tree came in the payload, the client
would be naming the tables to write, and anyone could name any of them.

The walk has two consumers — `get_page`, which fills `view.source.model` from the
node that declares it, and the tree endpoints, which validate every operation
against the map it returns. One function, so the two cannot diverge: what the
client renders and what the server accepts come from the same reading of the same
descriptor.
"""
import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from coframe.utils import deep_merge

_JSON_SCALARS = (str, int, float, bool, type(None))

COLLECTION = 'collection'
BUTTON = 'button'


def strip_meta(obj: Any) -> Any:
    """Remove $plugin metadata keys and non-JSON-serializable objects.

    The last step before a descriptor leaves for a consumer — the client, or the
    CLI dump. Everything that reads a descriptor as data runs it, so what one sees
    is what the other gets.
    """
    if isinstance(obj, dict):
        return {k: strip_meta(v) for k, v in obj.items() if k != '$plugin'}
    if isinstance(obj, list):
        return [strip_meta(item) for item in obj]
    if isinstance(obj, _JSON_SCALARS):
        return obj
    # Drop anything else (DbTable, DbColumn, etc.) that can't be JSON-serialized
    return None


# ── Collection nodes ────────────────────────────────────────────────────────

@dataclass
class Collection:
    """A collection node: a child table and the column that ties it to its parent.

    `fk` is not a filter that gets read, it is a value the framework writes, so
    it is always declared — never deduced. `defaults` are stamped on rows created
    here, which is the other half of `domain`: what you filter for, you stamp at
    creation, or the row vanishes from the view that made it.
    """
    id: str
    model: str
    fk: str
    form: str
    domain: Any = None
    defaults: Dict[str, Any] = field(default_factory=dict)
    order_by: Optional[List[Any]] = None
    collections: Dict[str, 'Collection'] = field(default_factory=dict)


@dataclass
class Aggregate:
    """What a page allows to be written: a root table and its collection tree."""
    page: str
    model: str
    collections: Dict[str, Collection] = field(default_factory=dict)


def _where(node: Dict[str, Any], page_id: str) -> str:
    """Locate a bad node for an error message, naming the plugin when known."""
    plugin = node.get('$plugin')
    return f"page '{page_id}'" + (f" (plugin '{plugin}')" if plugin else '')


def _collect_nodes(obj: Any, found: List[Dict[str, Any]]) -> None:
    """Gather collection nodes from a content descriptor, at any nesting depth.

    A node may sit anywhere the layout allows — directly, or inside a `section`,
    `row`, `col` or `tabs`. Its own `view:` is not descended into: a collection
    nested under a collection is declared in the *row form*, not in the grid that
    presents it.
    """
    if isinstance(obj, list):
        for item in obj:
            _collect_nodes(item, found)
        return
    if not isinstance(obj, dict):
        return
    if obj.get('type') == COLLECTION:
        found.append(obj)
        return
    for value in obj.values():
        _collect_nodes(value, found)


def _face_pages(obj: Any, found: List[str]) -> None:
    """Gather the pages a button opens as another face of the *same* record.

    A button either opens something of this record or calls an endpoint
    (relations.md §19.1). When what it opens is a form named by id, that form may
    declare collections of its own — and they belong to this record, at this level,
    not one step down. The walk stops inside a collection node for the same reason
    `_collect_nodes` does: what hangs under a collection is the row form's business.
    """
    if isinstance(obj, list):
        for item in obj:
            _face_pages(item, found)
        return
    if not isinstance(obj, dict):
        return
    if obj.get('type') == COLLECTION:
        return
    if obj.get('type') == BUTTON:
        opens = obj.get('opens')
        if isinstance(opens, dict) and opens.get('type') == 'form' and opens.get('page'):
            found.append(opens['page'])
        return
    for value in obj.values():
        _face_pages(value, found)


def resolve_collections(page: Dict[str, Any], page_id: str) -> Dict[str, Collection]:
    """Validate the collection nodes of a resolved page, and complete them in place.

    `view.source.model` is filled from the node's `model:`, because the table is a
    fact of *persistence* and belongs where writing is decided — not deduced from
    a descriptor of *presentation*, which may be a `$ref` to a shared view or be
    replaced tomorrow. A view that declares a different model is an error naming
    both, never a silent merge.

    Runs after `resolve_refs` (so a view pulled in by `$ref` gets the value on its
    own expanded copy) and before metadata is stripped (so an error can still name
    the plugin).
    """
    nodes: List[Dict[str, Any]] = []
    _collect_nodes(page.get('content'), nodes)

    result: Dict[str, Collection] = {}
    for node in nodes:
        where = _where(node, page_id)

        cid = node.get('id')
        if not cid:
            raise ValueError(f"A collection node in {where} has no 'id'")
        if cid in result:
            raise ValueError(f"Duplicate collection id '{cid}' in {where}")

        model = node.get('model')
        if not model:
            raise ValueError(f"Collection '{cid}' in {where} declares no 'model'")

        fk = node.get('fk')
        if not fk:
            raise ValueError(
                f"Collection '{cid}' in {where} declares no 'fk' — the column that "
                f"points at the parent is written by the framework, so it is never deduced")

        view = node.get('view')
        order_by = None
        if isinstance(view, dict):
            source = view.setdefault('source', {})
            declared = source.get('model')
            if declared and declared != model:
                raise ValueError(
                    f"Collection '{cid}' in {where} writes to '{model}' but its view "
                    f"reads '{declared}'")
            source['model'] = model
            order_by = source.get('order_by')

        result[cid] = Collection(
            id=cid,
            model=model,
            fk=fk,
            # Lowercased, because that is how the convention is spelled on the
            # other side (a list asks for `{model}_form` in lower case) and a
            # declared page is looked up by exact id: spelling it two ways means
            # a declared `partner_form` is found from a list and missed from a
            # collection, which falls back to the auto-form without a word.
            form=node.get('form') or f'{model.lower()}_form',
            domain=node.get('domain'),
            defaults=node.get('defaults') or {},
            order_by=order_by,
        )

    return result


# ── Auto-generated pages ────────────────────────────────────────────────────

def auto_list_page(table_name: str, table: Any) -> Dict[str, Any]:
    """
    Auto-generate a minimal list page descriptor for a table.

    Convention: requested as '{table_name}_list' (e.g. 'author_list').
    Includes all non-secret effective_columns as table columns.
    FK columns are shown as raw id fields (no join auto-resolve).

    A junction table needs no special case: it carries a key of its own like
    every other table, so its rows are addressable and add/delete mean something.
    """
    columns = []
    for col in table.effective_columns:
        if col.attributes.get('secret'):
            continue
        entry: Dict[str, Any] = {'field': col.name}
        label = col.attributes.get('label')
        if label:
            entry['title'] = label
        columns.append(entry)

    return {
        'title': table_name,
        '_auto': True,
        'content': {
            'type': 'table',
            'source': {'model': table_name},
            'columns': columns,
            'navigator': True,
        },
    }


def field_from_column(col: Any) -> Dict[str, Any]:
    """A form field as the column describes it: what a form knows before it says anything.

    Every attribute passes through, so the client applies what it knows (label,
    type, widget, foreign key...); only the DbTable object inside `foreign_key`
    is replaced by its name. The generated form is a list of these, and a
    written form gets them underneath what it declares.
    """
    attrs = col.attributes
    entry: Dict[str, Any] = {'name': col.name}
    for k, v in attrs.items():
        if k == 'foreign_key':
            fk_table = v.get('table')
            if fk_table:
                entry[k] = {'target': fk_table.name, 'field': v.get('id', 'id')}
            else:
                entry[k] = {fk_k: fk_v for fk_k, fk_v in v.items() if fk_k != 'table'}
        elif isinstance(v, _JSON_SCALARS) or isinstance(v, (list, dict)):
            entry[k] = v

    # Derived client hints not present in raw attributes
    if attrs.get('primary_key'):
        entry['readonly'] = True
    if attrs.get('nullable') is False and attrs.get('default') is None:
        entry['required'] = True
    return entry


def auto_form_page(table_name: str, table: Any) -> Dict[str, Any]:
    """
    Auto-generate a form page descriptor for a table.

    Convention: '{table_name}_form' (e.g. 'book_form').
    PK columns are included as read-only. Secret and virtual columns are skipped.
    All column attributes are forwarded to the client (which applies what it knows).
    The only exception is foreign_key['table'] which is a non-serializable DbTable object.

    Collections are not derived here: an auto-form comes from the columns, and a
    collection is not a column. Deducing one would mean asking which tables hold a
    FK to this one — and `Book` would pull in `BookAuthor`, `Loan` and `Review`
    with equal right, while only the first belongs inside the book.
    """
    fields = [field_from_column(col) for col in table.effective_columns
              if not col.attributes.get('secret') and not col.attributes.get('virtual')]

    return {
        'title': table_name,
        '_auto': True,
        'content': {
            'type': 'form',
            'source': {'model': table_name},
            'fields': fields,
            'policy': {'editable': True},
            'actions': {'toolbar': ['save', 'cancel']},
        },
    }


def resolve_auto_page(app: Any, page_id: str) -> Optional[Dict[str, Any]]:
    """
    Try to auto-generate a page from a conventional id.

    Supported patterns:
      {table_name}_list  →  auto list view for that table
      {table_name}_form  →  auto form descriptor for that table

    Table name matching is case-insensitive (e.g. 'author_list' → 'Author').
    Returns None if no matching table is found.
    """
    for suffix, builder in (('_list', auto_list_page), ('_form', auto_form_page)):
        if page_id.endswith(suffix):
            base = page_id[:-len(suffix)]
            for t_name, t_obj in app.tables.items():
                if t_name.lower() == base.lower():
                    return builder(t_name, t_obj)
    return None


# ── Page resolution ─────────────────────────────────────────────────────────

AUTO = '$auto'


def _on_auto(app: Any, page_id: str, page: Dict[str, Any]) -> Dict[str, Any]:
    """A page declaring `$auto: true` starts from the generated one and merges on top.

    For the page that is almost the generated one: a command in the navigator,
    a title, with the columns still derived from the schema. Declared, never
    inferred — an explicit page without `$auto` replaces the generated one whole.

    Raises:
        ValueError: if no generated page answers to that id
    """
    base = resolve_auto_page(app, page_id)
    if base is None:
        raise ValueError(f"Page '{page_id}' declares {AUTO}, but no table "
                         f"answers to that id")
    base.pop('_auto', None)
    overlay = copy.deepcopy(page)
    overlay.pop(AUTO, None)
    deep_merge(base, overlay)
    return base


def _column_label(app: Any, model: Optional[str], field_expr: Any) -> Optional[str]:
    """The label a model declares for a list column's field, or None.

    `inizio` is a column of the view's model; `Operatore.nome as operatore` is
    a column of the joined one, and the alias does not change whose label it is.
    """
    if not isinstance(field_expr, str):
        return None
    name = field_expr.split(' as ')[0].strip()
    if '.' in name:
        model, name = name.split('.', 1)
    table = app.tables.get(model) if model else None
    if table is None:
        return None
    for col in table.effective_columns:
        if col.name == name:
            return col.attributes.get('label')
    return None


def _fill_titles(app: Any, obj: Any, model: Optional[str] = None) -> None:
    """Give every list column without a `title` the label its model declares.

    The generated list does it already; a written one should not have to repeat
    in YAML what the model says once. Walks the whole page, collection grids
    included: their model is the node's `model:`, filled into the view by
    `resolve_collections`, which runs first.
    """
    if isinstance(obj, list):
        for item in obj:
            _fill_titles(app, item, model)
        return
    if not isinstance(obj, dict):
        return
    source = obj.get('source')
    if isinstance(source, dict) and source.get('model'):
        model = source['model']
    is_table = obj.get('type') == 'table' and isinstance(obj.get('columns'), list)
    if is_table:
        for col in obj['columns']:
            if isinstance(col, dict) and 'title' not in col:
                label = _column_label(app, model, col.get('field'))
                if label:
                    col['title'] = label
    for key, value in obj.items():
        # A form's `columns` are groups of fields and may hold collections; a
        # table's are what was just filled.
        if not (is_table and key == 'columns'):
            _fill_titles(app, value, model)


def _fill_fields(app: Any, obj: Any, model: Optional[str] = None) -> None:
    """Put under every field of a written form the attributes its column declares.

    Column, then type, then field: the declared field wins, the model fills
    what it leaves unsaid (`UI_DATAFORM.md`). Without it a written form shows
    `operatore_id` with a number in a text box where the generated one shows
    "Operatore" in a lookup. Stops at a collection node: its grid and its row
    form belong to its own model.
    """
    if isinstance(obj, list):
        for item in obj:
            _fill_fields(app, item, model)
        return
    if not isinstance(obj, dict) or obj.get('type') == COLLECTION:
        return
    source = obj.get('source')
    if isinstance(source, dict) and source.get('model'):
        model = source['model']
    name = obj.get('name')
    table = app.tables.get(model) if model else None
    if isinstance(name, str) and table is not None and 'filler' not in obj:
        col = next((c for c in table.effective_columns if c.name == name), None)
        if col is not None and not col.attributes.get('secret'):
            declared = dict(obj)
            obj.clear()
            obj.update(field_from_column(col))
            obj.update(declared)
            return
    for value in obj.values():
        _fill_fields(app, value, model)


def _resolve(app: Any, page_id: str):
    """Return (page, collection map), or (None, {}) if no page answers to that id."""
    page = app.pm.get(f'pages.{page_id}')
    if page is not None:
        resolved = app.pm.resolve_refs(page)
        if resolved.get(AUTO):
            resolved = _on_auto(app, page_id, resolved)
        collections = resolve_collections(resolved, page_id)
        _fill_titles(app, resolved)
        content = resolved.get('content')
        if isinstance(content, dict) and content.get('type', 'form') == 'form':
            _fill_fields(app, content)
        return resolved, collections

    auto = resolve_auto_page(app, page_id)
    return auto, {}


def load_page(app: Any, page_id: str) -> Optional[Dict[str, Any]]:
    """Return a page descriptor with `$ref`s resolved and collection nodes completed.

    Explicit pages first, then the auto-generated fallback. Returns None when no
    page answers to that id — the caller decides whether that is a 404 or a
    refused save.
    """
    return _resolve(app, page_id)[0]


def page_aggregate(app: Any, page_id: str, _seen: frozenset = frozenset()) -> Aggregate:
    """Build the tree of tables a page allows to be written.

    Recursive: a collection's row form may declare collections of its own, so the
    tree spans pages. Recursion stops at a page already on the path — the contacts
    of a partner are partners, and expanding that forever would describe nothing.
    A payload that goes deeper is refused by name at the level where the map ends.

    A row form that does not exist stops the descent too, without complaint: which
    page opens a row is a question of interface, and a form nobody has written yet
    cannot be a reason to refuse a save.

    Raises:
        ValueError: if the page does not exist, or declares no root model
    """
    page, collections = _resolve(app, page_id)
    if page is None:
        raise ValueError(f"Page not found: '{page_id}'")

    content = page.get('content') or {}
    model = (content.get('source') or {}).get('model')
    if not model:
        raise ValueError(f"Page '{page_id}' names no model to write")

    seen = _seen | {page_id}
    collections = _with_faces(app, content, collections, page_id, seen)
    aggregate = Aggregate(page=page_id, model=model, collections=collections)

    for coll in aggregate.collections.values():
        if coll.form not in seen:
            coll.collections = _collections_below(app, coll.form, seen)

    return aggregate


def _with_faces(app: Any, content: Dict[str, Any], collections: Dict[str, Collection],
                page_id: str, seen: frozenset) -> Dict[str, Collection]:
    """Add the collections declared by the faces this page opens behind a button.

    They merge at **this** level because a face is the same record seen with other
    fields — not a child. A face that names a collection this page already declares
    is refused: two nodes with one id would make the write contract ambiguous, and
    the payload names collections by id.
    """
    result = dict(collections)
    faces: List[str] = []
    _face_pages(content, faces)

    for ref in faces:
        if ref in seen:
            continue
        for cid, coll in _face_collections(app, ref, seen).items():
            if cid in result:
                raise ValueError(
                    f"Duplicate collection id '{cid}': page '{page_id}' and the face "
                    f"'{ref}' it opens both declare it")
            result[cid] = coll
    return result


def _face_collections(app: Any, page_id: str, seen: frozenset) -> Dict[str, Collection]:
    """The collections of a face, including those of the faces *it* opens."""
    page, collections = _resolve(app, page_id)
    if page is None:
        return {}
    return _with_faces(app, page.get('content') or {}, collections,
                       page_id, seen | {page_id})


def _collections_below(app: Any, page_id: str, seen: frozenset) -> Dict[str, Collection]:
    """The collections a row form declares, or none if there is no form to read.

    Absence is tolerated; a form that exists and is malformed still raises, because
    that one is a mistake somebody can fix.
    """
    page, _ = _resolve(app, page_id)
    if page is None:
        return {}
    if not ((page.get('content') or {}).get('source') or {}).get('model'):
        return {}
    return page_aggregate(app, page_id, seen).collections
