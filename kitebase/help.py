"""
kitebase.help: the guide of a menu entry, composed from what the plugins declare.

Whoever sees a menu entry wants one page from there on: what the table means, what
can be done on the list, what the fields of the record mean. So `help_merge` starts
from the page of the entry and follows the screens it can reach, and every field is
told once, whichever screens show it.

What depends on the record weighs less. A table whose rows belong to the record
(`owned`), or that only links two tables, is told with it; any other has a guide of
its own and gets a line here, with the commands this view adds to it. A panel says
what it depends on through its trigger. A foreign key stays a link, and a page with
a menu entry of its own becomes a reference. `help_embed` on a node overrides the
rule, and `help_compose: false` on a page replaces the composition with its text:
for what the YAML cannot tell, like a wizard driven by an endpoint.

Only `help:` and the discursive `help/en/<page>.md` of the plugins are read here.
`doc:` is written for whoever develops, and never reaches the guide.

`render_html` turns the result into a standalone page. Texts are markdown with raw
HTML disabled; they fill the skeleton as values and are never read as templates.
An application replaces a template by name from the folder `help_templates:` of
its config.yaml names, and reaches the original as `core/<name>`.
"""
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape
from markdown_it import MarkdownIt

from kitebase import help_texts
from kitebase.i18n import _, _f, use_locale
from kitebase.media import MediaError, parse_size
from kitebase.pages import (BUTTON, COLLECTION, LIST_TYPES, can_open, field_from_column, load_page,
                            lookup_page, restrict, restrict_columns)

_TEMPLATES = Path(__file__).parent / 'help_template'

# Keys of a descriptor that hold other nodes.
_CHILDREN = ('layout', 'columns', 'fields', 'pages', 'tabs', 'children')

# Where a part that depends on the record stands, and what its heading says.
INSIDE = 'inside'
SELECTING = 'selecting'
BESIDE = 'beside'
_PART_TITLES = {
    INSIDE: 'Inside the form',
    SELECTING: 'When you select a row',
    BESIDE: 'Beside the list',
}

_HOW_TITLE = 'Filling in the fields'

# How the types of the core are filled in, which no YAML declares: they exist even in
# an application without the shared plugins. `help_how` says the same for any other.
_BUILTIN_HOW = {
    'FK': (
        'Choosing from another table',
        'Type to search and pick with the arrow keys and Enter. '
        '*Search more...*, at the end of the list, opens the whole table.',
    ),
    'Date': ('Dates', 'Type the date, or pick it from the calendar.'),
    'DateTime': ('Dates', 'Type the date, or pick it from the calendar.'),
}


@dataclass
class HelpField:
    key: str
    label: str
    text: str
    link: Optional[str] = None
    # What the server accepts in this field, from its own declaration
    limits: str = ''


@dataclass
class HelpHow:
    key: str
    title: str
    text: str


@dataclass
class HelpCommand:
    label: str
    text: str


@dataclass
class HelpGroup:
    """Fields of the record, or of a table that is part of it, under one title.
    The group without a title holds what the form does not gather under any."""
    id: str
    title: Optional[str] = None
    texts: List[str] = field(default_factory=list)
    fields: List[HelpField] = field(default_factory=list)
    commands: List[HelpCommand] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.texts or self.fields or self.commands)


@dataclass
class HelpItem:
    """Something that depends on the record: a line, a reference to where its own
    fields are told, and the commands available right here."""
    title: str
    text: Optional[str] = None
    ref: Optional[str] = None
    anchor: Optional[str] = None
    commands: List[HelpCommand] = field(default_factory=list)


@dataclass
class HelpDoc:
    root: str
    title: str
    lang: str = help_texts.SOURCE
    # Composed for the current user: only what they may open, as they see it
    for_user: bool = False
    intro: List[str] = field(default_factory=list)
    commands: List[HelpCommand] = field(default_factory=list)
    groups: List[HelpGroup] = field(default_factory=list)
    # Where each dependent part stands: INSIDE, SELECTING or BESIDE
    parts: Dict[str, List[HelpItem]] = field(default_factory=dict)
    how: List[HelpHow] = field(default_factory=list)
    see_also: List[str] = field(default_factory=list)
    screens: List[str] = field(default_factory=list)
    # Where each screen is told in the page
    anchors: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def all_fields(self) -> Iterator[HelpField]:
        for group in self.groups:
            yield from group.fields


def _humanize(name: str) -> str:
    return name.replace('_', ' ').strip().capitalize()


def _limits(attrs: Dict[str, Any]) -> str:
    """The files a media field takes: `pdf, jpg, up to 20 MB`."""
    if not attrs.get('media'):
        return ''
    parts = [str(a) for a in attrs.get('accept') or []]
    try:
        size = parse_size(attrs.get('max_size'))
    except MediaError:
        size = None
    if size:
        parts.append(_f('up to {size} MB', size=f'{size / 1024 / 1024:g}'))
    return ', '.join(parts)


def _menu_pages(app: Any) -> Dict[str, str]:
    """The pages with a menu entry of their own, and the label of that entry."""
    items = app.pm.get('menu_items') or {}
    return {item['panel']: item.get('label', '') for item in items.values()
            if isinstance(item, dict) and isinstance(item.get('panel'), str)}


def _model_of(node: Dict[str, Any]) -> Optional[str]:
    source = node.get('source')
    return source.get('model') if isinstance(source, dict) else None


class _Merge:
    """One walk: the screens already seen and the fields already told."""

    def __init__(self, app: Any, root: str, doc: HelpDoc, told: Set[str] = frozenset()):
        self.app = app
        self.root = root
        self.doc = doc
        # Screens another card of the manual already tells
        self.told = told
        self.menu = _menu_pages(app)
        self.seen_tables: Set[str] = set()
        self.seen_fields: Set[str] = set()
        self.grids: List[Dict[str, Any]] = []

    # ── Screens ─────────────────────────────────────────────────────────────

    def opens(self, page_id: str) -> bool:
        return not self.doc.for_user or can_open(self.app, page_id)

    def load(self, page_id: str) -> Optional[Dict[str, Any]]:
        """A page as the reader sees it: for a user, None when they may not open
        it, and without what `get_page` would take out of it."""
        if not self.opens(page_id):
            return None
        page = load_page(self.app, page_id)
        if page is not None and self.doc.for_user:
            restrict(self.app, page)
            restrict_columns(self.app, page)
        return page

    def screen(self, page_id: str, group: Optional[HelpGroup] = None,
               skip: Optional[str] = None) -> None:
        """Walk a screen: as the record itself, or inside `group` when it only details
        a part of it. `help_compose: false` leaves its written text and nothing else."""
        if page_id in self.doc.screens:
            return
        if page_id != self.root and (page_id in self.menu or page_id in self.told):
            if page_id not in self.doc.see_also and self.opens(page_id):
                self.doc.see_also.append(page_id)
            return
        page = self.load(page_id)
        if page is None:
            return
        self.doc.screens.append(page_id)

        content = page.get('content') or {}
        model = _model_of(content)
        texts = group.texts if group else self.doc.intro
        by_hand = page.get('help_compose') is False
        if not by_hand:
            self.table_text(model, texts)
        before = len(texts)
        self.text(texts, page.get('help'))
        self.text(texts, help_texts.discourse(self.app, page_id, self.doc.lang))
        if by_hand:
            if len(texts) == before:
                self.doc.warnings.append(
                    f"Page '{page_id}' declares help_compose: false and has no text")
            return

        is_list = content.get('type') in LIST_TYPES
        self.doc.anchors[page_id] = group.id if group else ('top' if is_list else 'fields')
        if is_list:
            self.list_screen(content, model)
        elif content.get('phases'):
            self.phases(content, content['phases'], model, group, skip)
        else:
            self.walk(content, model, group, skip)
        self.panels(page, model, is_list)

    def list_screen(self, view: Dict[str, Any], model: Optional[str]) -> None:
        """The list the entry opens, and the forms it leads to: `navigator.form_id` or
        `{model}_form` like the client, and whatever page a row action names, since
        that is the screen the user lands on."""
        self.grids.append({'view': view, 'model': model, 'group': None})
        self.doc.commands.extend(self.local_commands(view))
        navigator = view.get('navigator')
        if navigator and model:
            form = navigator.get('form_id') if isinstance(navigator, dict) else None
            self.screen(form or f'{model.lower()}_form')
        actions = view.get('actions')
        for action in (actions.get('row') or []) if isinstance(actions, dict) else []:
            if isinstance(action, dict) and isinstance(action.get('panel'), str):
                self.screen(action['panel'])

    def phases(self, node: Dict[str, Any], phases: List[Dict[str, Any]], model: Optional[str],
               group: Optional[HelpGroup], skip: Optional[str]) -> None:
        """A form with phases reads in the order of its phases, not of its layout,
        where a footer may well stand before the rows."""
        by_id = {n['id']: n for n in node.get('layout') or [] if isinstance(n, dict) and n.get('id')}
        placed: Set[str] = set()
        for phase in phases:
            label = phase.get('label')
            where = self.group(label) if label else group
            for node_id in phase.get('nodes') or []:
                if node_id in by_id:
                    placed.add(node_id)
                    self.walk(by_id[node_id], model, where, skip)
        for node_id, rest in by_id.items():
            if node_id not in placed:
                self.walk(rest, model, group, skip)

    def panels(self, page: Dict[str, Any], model: Optional[str], is_list: bool) -> None:
        """What stands beside the screen, placed by what it depends on: the trigger
        says it, and without one it only stands there."""
        for panel in page.get('panels') or []:
            trigger = panel.get('trigger')
            event = trigger.get('event') if isinstance(trigger, dict) else None
            if event == 'row_click':
                part = SELECTING
            elif event == 'form_load' or not is_list:
                part = INSIDE
            else:
                part = BESIDE
            for node in panel.get('tabs') or [panel]:
                self.dependent(node, model, part)

    # ── Texts ───────────────────────────────────────────────────────────────

    def text(self, texts: List[str], text: Any) -> None:
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())

    def table_text(self, model: Optional[str], texts: List[str]) -> None:
        table = self.app.tables.get(model) if model else None
        if table is not None and model not in self.seen_tables:
            self.seen_tables.add(model)
            self.text(texts, table.attributes.get('help'))

    def group(self, title: Optional[str]) -> HelpGroup:
        for group in self.doc.groups:
            if group.title == title:
                return group
        group = HelpGroup(f'fields-{len(self.doc.groups) + 1}' if title else 'fields-0', title)
        if title:
            self.doc.groups.append(group)
        else:
            self.doc.groups.insert(0, group)
        return group

    # ── The form ────────────────────────────────────────────────────────────

    def walk(self, node: Any, model: Optional[str], group: Optional[HelpGroup],
             skip: Optional[str] = None) -> None:
        if isinstance(node, list):
            for item in node:
                self.walk(item, model, group, skip)
            return
        if not isinstance(node, dict):
            return
        kind = node.get('type')
        if kind == COLLECTION:
            self.collection(node, group)
            return
        if kind == BUTTON:
            self.button(node, model, group)
            return
        if kind in LIST_TYPES:
            self.dependent(node, model, INSIDE, group)
            return
        model = _model_of(node) or model
        if isinstance(node.get('field'), str):
            return
        if isinstance(node.get('label'), str) and any(k in node for k in _CHILDREN):
            group = self.group(node['label'])
        if isinstance(node.get('name'), str) and 'filler' not in node:
            self.form_field(node, model, group, skip)
            return
        for key in _CHILDREN:
            self.walk(node.get(key), model, group, skip)

    def is_part(self, node: Dict[str, Any], model: Optional[str], fk: Optional[str]) -> bool:
        """Whether a table shown inside the record is told with it. It is when its rows
        belong to the record (`owned`), or when it only links two tables; any other
        table has a life of its own, and a guide of its own. `help_embed` overrides."""
        explicit = node.get('help_embed')
        if isinstance(explicit, bool):
            return explicit
        table = self.app.tables.get(model) if model else None
        if table is None:
            return False
        if table.attributes.get('many_to_many'):
            return True
        column = next((c for c in table.effective_columns if c.name == fk), None)
        foreign_key = column.attributes.get('foreign_key') if column is not None else None
        return isinstance(foreign_key, dict) and foreign_key.get('owned') is True

    def collection(self, node: Dict[str, Any], group: Optional[HelpGroup],
                   title: Optional[str] = None, text: Optional[str] = None) -> None:
        model = node.get('model')
        fk = node.get('fk')
        title = title or (group.title if group else None) or _humanize(str(node.get('id') or model))
        view = node.get('view') or {}
        if not self.is_part(node, model, fk):
            self.item(INSIDE, title, text or node.get('help'), model, view)
            return
        part = self.group(title)
        self.text(part.texts, text)
        self.table_text(model, part.texts)
        self.grids.append({'view': view, 'model': model, 'group': part})
        part.commands.extend(self.local_commands(view))
        form = node.get('form') or (f'{str(model).lower()}_form' if model else None)
        if form:
            self.screen(form, part, skip=fk)

    def button(self, node: Dict[str, Any], model: Optional[str], group: Optional[HelpGroup]) -> None:
        """A page on the same model is another face of the record, told with it; one
        on another model, or with a menu entry of its own, is a reference."""
        title = node.get('label') or _humanize(str(node.get('id', '')))
        text = node.get('help')
        opens = node.get('opens')
        if isinstance(opens, dict) and opens.get('type') == COLLECTION:
            self.collection(opens, group, title, text)
        elif isinstance(opens, dict) and isinstance(opens.get('page'), str):
            page_id = opens['page']
            page = self.load(page_id)
            if page is None:
                return
            if _model_of(page.get('content') or {}) == model and page_id not in self.menu:
                face = self.group(title)
                self.text(face.texts, text)
                self.screen(page_id, face)
            else:
                self.add_item(INSIDE, HelpItem(title, text.strip() if text else None, ref=page_id))
        elif isinstance(text, str) and text.strip():
            self.add_item(INSIDE, HelpItem(title, text.strip()))

    def dependent(self, node: Dict[str, Any], model: Optional[str], part: str,
                  group: Optional[HelpGroup] = None) -> None:
        """A view that depends on the record: a panel, a tab, a table inside the form."""
        own = _model_of(node)
        label = node.get('label')
        title = label or (group.title if group else None) or _humanize(str(node.get('id') or own or ''))
        text = node.get('help')
        if own and self.is_part(node, own, None):
            target = self.group(title)
            self.table_text(own, target.texts)
            self.text(target.texts, text)
            self.grids.append({'view': node, 'model': own, 'group': target})
            target.commands.extend(self.local_commands(node))
        elif own and own == model:
            self.walk({k: node.get(k) for k in _CHILDREN if k != 'columns' or node.get('type') not in LIST_TYPES},
                      model, None)
            self.add_item(part, HelpItem(title, text.strip() if isinstance(text, str) else None,
                                         anchor='fields', commands=self.local_commands(node)))
        elif own:
            self.item(part, title, text, own, node)
        elif label or text:
            self.add_item(part, HelpItem(title, text.strip() if isinstance(text, str) else None))

    def item(self, part: str, title: str, text: Optional[str], model: Optional[str],
             view: Dict[str, Any]) -> None:
        """A table with a life of its own: what it is, where its fields are told, and
        the commands this view adds to it."""
        table = self.app.tables.get(model) if model else None
        if not text and table is not None:
            text = table.attributes.get('help')
        self.add_item(part, HelpItem(title, text.strip() if isinstance(text, str) else None,
                                     ref=self.home(model), commands=self.local_commands(view)))

    def add_item(self, part: str, item: HelpItem) -> None:
        items = self.doc.parts.setdefault(part, [])
        for known in items:
            if known.title == item.title:
                known.text = known.text or item.text
                known.ref = known.ref or item.ref
                known.commands.extend(c for c in item.commands if c not in known.commands)
                return
        items.append(item)

    def home(self, model: Optional[str]) -> Optional[str]:
        """Where a table is told: the page of a menu entry on it, or its generated list."""
        if not model:
            return None
        for page_id in self.menu:
            page = self.load(page_id) or {}
            if _model_of(page.get('content') or {}) == model:
                return page_id
        generated = f'{model.lower()}_list'
        return generated if self.opens(generated) else None

    def local_commands(self, view: Dict[str, Any]) -> List[HelpCommand]:
        found: List[HelpCommand] = []
        navigator = view.get('navigator')
        actions = view.get('actions')
        declared = list((navigator.get('commands') or []) if isinstance(navigator, dict) else [])
        declared += list((actions.get('row') or []) if isinstance(actions, dict) else [])
        for command in declared:
            if isinstance(command, dict) and isinstance(command.get('help'), str):
                label = command.get('label') or _humanize(str(command.get('id', '')))
                found.append(HelpCommand(label, command['help'].strip()))
        return found

    # ── Fields ──────────────────────────────────────────────────────────────

    def form_field(self, node: Dict[str, Any], model: Optional[str], group: Optional[HelpGroup],
                   skip: Optional[str]) -> None:
        name = node['name']
        if name == skip:
            return
        if not node.get('readonly'):
            self.how(node)
        self.add_field(f'{model or "form"}.{name}', node, group)

    def how(self, node: Dict[str, Any]) -> None:
        """How this field is filled in: the nearest `help_how` from the field up the
        chain of its types, down to the types of the core. A foreign key is of type
        FK, whatever column holds it."""
        own = node.get('help_how')
        own = own.strip() if isinstance(own, str) and own.strip() else None
        builtin = 'FK' if node.get('foreign_key') else None
        type_name = node.get('type')
        visited: Set[str] = set()
        while isinstance(type_name, str) and type_name not in visited:
            visited.add(type_name)
            if type_name in _BUILTIN_HOW:
                builtin = builtin or type_name
                break
            declared = self.app.pm.get(f'types.{type_name}')
            if not isinstance(declared, dict):
                break
            text = declared.get('help_how')
            if isinstance(text, str) and text.strip() and own in (None, text.strip()):
                self.add_how(type_name, declared.get('label') or type_name, text.strip())
                return
            type_name = declared.get('base')
        if own:
            self.add_how(node['name'], node.get('label') or _humanize(node['name']), own)
        elif builtin:
            self.add_how(builtin, *_BUILTIN_HOW[builtin])

    def add_how(self, key: str, title: str, text: str) -> None:
        if not any(h.text == text for h in self.doc.how):
            self.doc.how.append(HelpHow(key, title, text))

    def add_field(self, key: str, attrs: Dict[str, Any], group: Optional[HelpGroup],
                  label: Optional[str] = None) -> None:
        """Every field the user sees is listed, with or without a text: whoever looks
        for one must find it, even to learn there is nothing more to say. The key of
        the table says nothing to the user, unless somebody wrote that it does."""
        if key in self.seen_fields:
            return
        self.seen_fields.add(key)
        text = attrs.get('help')
        text = text.strip() if isinstance(text, str) else ''
        if attrs.get('primary_key') and not text:
            return
        (group or self.group(None)).fields.append(HelpField(
            key=key,
            label=attrs.get('label') or label or _humanize(attrs.get('name', key)),
            text=text,
            link=lookup_page(attrs),
            limits=_limits(attrs),
        ))

    def grid_columns(self) -> None:
        """After the forms, so that a field is told where the form asks for it. Only
        the columns of the model count: `book_id.title` shows the foreign key
        `book_id`, and a column from a join belongs to another table."""
        for grid in self.grids:
            table = self.app.tables.get(grid['model']) if grid['model'] else None
            if table is None:
                continue
            columns = {c.name: c for c in table.effective_columns}
            for column in grid['view'].get('columns') or []:
                expr = column.get('field') if isinstance(column, dict) else None
                if not isinstance(expr, str) or column.get('visible') is False:
                    continue
                name = expr.split(' as ')[0].strip()
                head, _sep, rest = name.partition('.')
                if rest and head in self.app.tables and head != grid['model']:
                    continue
                name = rest if rest and head == grid['model'] else head
                attrs = columns[name].attributes if name in columns else {}
                if name in columns and not (attrs.get('secret') or attrs.get('hidden')):
                    self.add_field(f"{grid['model']}.{name}", field_from_column(columns[name]),
                                   grid['group'], column.get('title'))

    def prune(self) -> None:
        """A part with nothing to tell leaves no heading behind."""
        self.doc.groups = [g for g in self.doc.groups if not g.is_empty()]
        kept = {g.id for g in self.doc.groups} | {'top'}
        if self.doc.groups:
            kept.add('fields')
        self.doc.anchors = {p: a for p, a in self.doc.anchors.items() if a in kept}


def help_merge(app: Any, page_id: str, lang: str = help_texts.SOURCE,
               for_user: bool = False, told: Set[str] = frozenset()) -> HelpDoc:
    """The guide of a page, as data. The long texts are read in `lang`; the short
    ones stay English here and are translated by `render_html`.

    `for_user` composes it for the current user, as `get_page` would show each
    screen: a screen they may not open is skipped, a reference to it is dropped,
    and a link to it stays text. Without it the guide is complete, as the command
    line writes it. The caller checks that the root itself may be opened.

    `told` are the screens other cards of the manual already tell: here they are
    a reference, like a page with a menu entry of its own.

    Raises:
        ValueError: if no page answers to that id
    """
    page = load_page(app, page_id)
    if page is None:
        raise ValueError(f"No page '{page_id}'")
    title = _menu_pages(app).get(page_id) or page.get('title') or _humanize(page_id)
    doc = HelpDoc(root=page_id, title=title, lang=lang, for_user=for_user)
    with use_locale(lang):
        merge = _Merge(app, page_id, doc, told)
        merge.screen(page_id)
        merge.grid_columns()
        merge.prune()
    return doc


def guide_root(app: Any, stack: List[str]) -> str:
    """The page whose guide tells the top of a stack of pages, as the user opened
    them: going down while the page below counts the one above among its screens.
    The form of a book, a face of it, its rows stay in the guide of the books; the
    publishers opened from a foreign key have a guide of their own."""
    root = stack[-1]
    for below in reversed(stack[:-1]):
        try:
            screens = help_merge(app, below, for_user=True).screens
        except ValueError:
            break
        if root not in screens:
            break
        root = below
    return root


def focus_anchor(doc: HelpDoc, focus: Optional[str]) -> str:
    """Where the guide opens: on a field (`Book.title`), on a screen, or at the top."""
    if focus and any(f.key == focus for f in doc.all_fields()):
        return f'field-{focus}'
    if focus and focus in doc.anchors:
        return doc.anchors[focus]
    return 'top'


# ── Render ──────────────────────────────────────────────────────────────────

_LINK = re.compile(r'\[\[([^\]\n]+)\]\]')
# A page on the web leaves the guide: inside the frame of the app it would take its place
_WEB = re.compile(r'https?://', re.IGNORECASE)


def target_page(app: Any, ref: str) -> Optional[str]:
    """The page whose guide tells a reference: the page itself, or the list of a
    table for `Table` and `Table.column`. None when nothing answers to it."""
    head, _sep, rest = ref.partition('.')
    table = app.tables.get(head)
    if table is not None:
        if rest and not any(c.name == rest for c in table.effective_columns):
            return None
        return f'{head.lower()}_list'
    return ref if load_page(app, ref) is not None else None


class _Links:
    """What a link points to: an anchor when the object is told in one of the
    cards of the page, the file beside it otherwise, and nothing beside a manual,
    which is one file. Every link carries `data-help`, for a client to intercept.
    A target that does not exist stays text, with a warning; one the user may not
    open stays text, silently."""

    def __init__(self, app: Any, cards: List['_Card'], beside: bool,
                 appendices: Set[str] = frozenset()):
        self.app = app
        self.cards = cards
        self.beside = beside
        # The appendices in the page: `[[appendix/<name>]]` leads to them
        self.appendices = appendices
        self.for_user = any(c.doc.for_user for c in cards)
        self.warnings: List[str] = []
        self.external: Set[str] = set()

    def inside(self, page_id: str) -> Optional[str]:
        for card in self.cards:
            if page_id in card.doc.screens:
                return f"#{card.prefix}{card.doc.anchors.get(page_id, 'top')}"
        return None

    def resolve(self, ref: str):
        """(href, default text) of a reference; href None when it leads nowhere
        the reader can go; None when nothing answers to it."""
        for card in self.cards:
            told = next((f for f in card.doc.all_fields() if f.key == ref), None)
            if told is not None:
                return f'#{card.prefix}field-{ref}', _(told.label)
        page_id = target_page(self.app, ref)
        if page_id is None:
            return None
        head, _sep, rest = ref.partition('.')
        table = self.app.tables.get(head)
        if table is not None:
            column = next((c for c in table.effective_columns if c.name == rest), None)
            label = (column.attributes.get('label') or _humanize(rest)) if column is not None else head
        else:
            label = (load_page(self.app, ref) or {}).get('title') or _humanize(ref)
        href = self.inside(page_id)
        if href is None and self.for_user and not can_open(self.app, page_id):
            href = None
        elif href is None and self.beside:
            self.external.add(page_id)
            href = f'{page_id}.html'
        return href, _(label)

    def anchor(self, ref: str, text: Optional[str] = None) -> str:
        if ref.startswith('appendix/'):
            name = ref.partition('/')[2]
            if name in self.appendices:
                return f'<a href="#appendix-{html.escape(name, quote=True)}">{html.escape(text or name)}</a>'
            return html.escape(text or name)
        target = self.resolve(ref)
        if target is None:
            self.warnings.append(f"Link to '{ref}': no such page, table or column")
            return html.escape(text or ref)
        href, default = target
        if href is None:
            return html.escape(text or default)
        return (f'<a href="{html.escape(href, quote=True)}" '
                f'data-help="{html.escape(ref, quote=True)}">{html.escape(text or default)}</a>')


def _markdown(links: _Links) -> MarkdownIt:
    md = MarkdownIt('commonmark', {'html': False}).enable('table')

    def rule(state, silent):
        match = _LINK.match(state.src, state.pos)
        if match is None:
            return False
        if not silent:
            state.push('help_link', '', 0).content = match.group(1)
        state.pos = match.end()
        return True

    def render(self, tokens, idx, options, env):
        ref, _sep, text = tokens[idx].content.partition('|')
        return links.anchor(ref.strip(), text.strip() or None)

    def link_open(self, tokens, idx, options, env):
        if _WEB.match(tokens[idx].attrGet('href') or ''):
            tokens[idx].attrSet('target', '_blank')
            tokens[idx].attrSet('rel', 'noopener noreferrer')
        return self.renderToken(tokens, idx, options, env)

    md.inline.ruler.before('emphasis', 'help_link', rule)
    md.add_render_rule('help_link', render)
    md.add_render_rule('link_open', link_open)
    return md


@dataclass
class RenderedHelp:
    html: str
    warnings: List[str]
    external: List[str]


def _environment(app: Any) -> Environment:
    """The templates of the application over the ones of the core, which stay
    reachable as `core/<name>` for whoever only wants to extend them.

    Raises:
        ValueError: if the folder `help_templates:` names does not exist
    """
    core = FileSystemLoader(_TEMPLATES)
    loaders: List[Any] = [PrefixLoader({'core': core}), core]
    declared = (getattr(app.pm, 'config', None) or {}).get('help_templates')
    if declared:
        folder = Path(app.pm.app_root) / declared
        if not folder.is_dir():
            raise ValueError(f"help_templates: no folder '{folder}'")
        loaders.insert(0, FileSystemLoader(folder))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(['html']))
    # The words of the skeleton, in the language of the guide
    env.globals['_'] = _
    return env


# ── The manual ──────────────────────────────────────────────────────────────


@dataclass
class ManualSection:
    """A node of the menu: a group with its introduction, or an entry with the
    card of its page. An entry whose page another card already tells points to it."""
    id: str
    title: str
    depth: int
    texts: List[str] = field(default_factory=list)
    # The page the entry opens
    page: Optional[str] = None
    doc: Optional[HelpDoc] = None
    ref: Optional[str] = None


@dataclass
class Appendix:
    id: str
    title: str
    text: str


@dataclass
class HelpManual:
    title: str
    lang: str
    intro: List[str] = field(default_factory=list)
    sections: List[ManualSection] = field(default_factory=list)
    appendices: List[Appendix] = field(default_factory=list)
    # The cards of the pages the others link to, which no menu entry opens
    others: List[HelpDoc] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def docs(self) -> List[HelpDoc]:
        return [s.doc for s in self.sections if s.doc is not None] + self.others


def _texts_of(doc: HelpDoc) -> Iterator[str]:
    yield from doc.intro
    for group in doc.groups:
        yield from group.texts
        yield from (f.text for f in group.fields)
        yield from (c.text for c in group.commands)
    yield from (c.text for c in doc.commands)
    for items in doc.parts.values():
        for item in items:
            if item.text:
                yield item.text
            yield from (c.text for c in item.commands)
    yield from (h.text for h in doc.how)


def _targets(app: Any, doc: HelpDoc) -> List[str]:
    """The pages a card leads to: its references and the links in its texts."""
    refs = list(doc.see_also)
    refs += [i.ref for items in doc.parts.values() for i in items if i.ref]
    refs += [f.link for f in doc.all_fields() if f.link]
    for text in _texts_of(doc):
        refs += [m.group(1).partition('|')[0].strip() for m in _LINK.finditer(text)]
    pages = []
    for ref in refs:
        page_id = target_page(app, ref)
        if page_id and page_id not in pages:
            pages.append(page_id)
    return pages


def _appendix(app: Any, name: str, lang: str) -> Optional[Appendix]:
    """An appendix written by hand: its title is its first heading."""
    text = help_texts.discourse(app, f'appendix/{name}', lang)
    if not text:
        return None
    lines = text.strip().splitlines()
    title = _humanize(name)
    if lines and lines[0].startswith('# '):
        title = lines[0][2:].strip()
        lines = lines[1:]
    return Appendix(f'appendix-{name}', title, '\n'.join(lines).strip())


def help_manual(app: Any, lang: str = help_texts.SOURCE, for_user: bool = False,
                root: Optional[str] = None, cards: bool = True) -> HelpManual:
    """The manual: the tree of the menu with the card of every entry, the texts
    of the application and of its groups, the appendices, and at the end the cards
    of the pages the others lead to that no entry opens. A screen is told once, in
    the first card that reaches it; the others point to it.

    `for_user` keeps what the current user may open, as the guide does; the
    appendices are for everyone. Without `cards` it is the head of the manual:
    the texts and the tree, each entry leading to its own guide, for the home.
    """
    from kitebase.endpoint_menu import _app_default_root, menu_tree

    config = getattr(app.pm, 'config', None) or {}
    manual = HelpManual(title=config.get('title') or config.get('name') or '', lang=lang)
    told: Set[str] = set()
    with use_locale(lang):
        tree = menu_tree(app, root or _app_default_root(app), for_user) or {}
        intro = help_texts.discourse(app, 'index', lang)
        if intro:
            manual.intro.append(intro.strip())

        def walk(nodes: List[Dict[str, Any]], depth: int) -> None:
            for node in nodes:
                section = ManualSection(id=f"menu-{node['id']}", title=str(node.get('label') or node['id']),
                                        depth=depth)
                if isinstance(node.get('help'), str) and node['help'].strip():
                    section.texts.append(node['help'].strip())
                section.texts += [t.strip() for t in help_texts.discourses(app, f"menu.{node['id']}", lang)]
                panel = node.get('panel')
                if isinstance(panel, str) and load_page(app, panel) is not None:
                    section.page = panel
                    if cards and panel in told:
                        section.ref = panel
                    elif cards:
                        section.doc = help_merge(app, panel, lang, for_user, told)
                        told.update(section.doc.screens)
                manual.sections.append(section)
                walk(node.get('children') or [], depth + 1)

        walk(tree.get('items') or [], 1)

    for name in help_texts.appendix_names(app, lang):
        appendix = _appendix(app, name, lang)
        if appendix is not None:
            manual.appendices.append(appendix)

    # What the cards lead to and no entry opens, until nothing new is reached
    pending = [p for doc in manual.docs() for p in _targets(app, doc)] if cards else []
    while pending:
        page_id = pending.pop(0)
        if page_id in told or (for_user and not can_open(app, page_id)):
            continue
        doc = help_merge(app, page_id, lang, for_user, told)
        told.update(doc.screens)
        manual.others.append(doc)
        pending += _targets(app, doc)

    for doc in manual.docs():
        manual.warnings += doc.warnings
    return manual


# ── Render ──────────────────────────────────────────────────────────────────


@dataclass
class _Card:
    doc: HelpDoc
    # Before every id of the card: nothing in a guide, the root in a manual
    prefix: str
    # The level of its title; its parts go one below, and so on
    level: int


def _card(card: _Card, links: _Links, md: MarkdownIt, tr: Callable[[str], str],
          index: bool) -> Dict[str, Any]:
    """The values of one card for the template, its ids already prefixed."""
    doc, p = card.doc, card.prefix

    def text(value: str) -> str:
        return md.render(tr(value))

    def commands(items: List[HelpCommand]) -> List[Dict[str, str]]:
        return [{'label': _(c.label), 'html': text(c.text)} for c in items]

    def group(g: HelpGroup) -> Dict[str, Any]:
        return {
            'id': p + g.id,
            'title': _(g.title) if g.title else None,
            'texts': [text(t) for t in g.texts],
            'fields': [{
                'id': f'{p}field-{f.key}',
                'label': links.anchor(f.link, _(f.label)) if f.link else html.escape(_(f.label)),
                'html': text(f.text) if f.text else '',
                'limits': f.limits,
            } for f in g.fields],
            'commands': commands(g.commands),
        }

    def item(i: HelpItem) -> Dict[str, Any]:
        label = _(i.title)
        if i.ref:
            title = links.anchor(i.ref, label)
        elif i.anchor:
            title = f'<a href="#{html.escape(p + i.anchor, quote=True)}">{html.escape(label)}</a>'
        else:
            title = html.escape(label)
        return {'title': title, 'html': text(i.text) if i.text else '',
                'commands': commands(i.commands)}

    parts = [{'id': p + key, 'title': _(title), 'items': [item(i) for i in doc.parts[key]]}
             for key, title in _PART_TITLES.items() if doc.parts.get(key)]

    contents: List[Dict[str, Any]] = []
    if index:
        if doc.commands:
            contents.append({'id': p + 'commands', 'title': _('Commands'), 'subs': []})
        if doc.groups:
            contents.append({'id': p + 'fields', 'title': _('Fields'),
                             'subs': [{'id': p + g.id, 'title': _(g.title)} for g in doc.groups if g.title]})
        contents.extend({'id': part['id'], 'title': part['title'], 'subs': []} for part in parts)
        if doc.how:
            contents.append({'id': p + 'filling-in', 'title': _(_HOW_TITLE), 'subs': []})

    menu = _menu_pages(links.app)
    level = card.level
    return {
        'p': p,
        'h': [min(level + n, 6) for n in range(4)],
        'title': _(doc.title),
        'intro': [text(t) for t in doc.intro],
        'index': contents,
        'commands': commands(doc.commands),
        'groups': [group(g) for g in doc.groups],
        'parts': parts,
        'how_title': _(_HOW_TITLE),
        'how': [{'title': _(h.title), 'html': text(h.text)} for h in doc.how],
        'see_also': [links.anchor(page, _(menu[page]) if menu.get(page) else None)
                     for page in doc.see_also],
    }


def _page(app: Any, lang: str, title: str, values: Dict[str, Any]) -> str:
    env = _environment(app)
    config = getattr(app.pm, 'config', None) or {}
    return env.get_template('skeleton.html').render(
        lang=lang,
        app_title=_(config.get('title') or config.get('name') or ''),
        title=title,
        style=env.get_template('style.css').render(),
        **values,
    )


def _translator(app: Any, lang: str) -> Callable[[str], str]:
    catalog = help_texts.catalog(app, lang)
    # A text no plugin translates may be one of the core, translated like the interface
    return lambda text: catalog.translate(text) or _(text)


def render_html(app: Any, doc: HelpDoc) -> RenderedHelp:
    """The guide as a standalone HTML page, in the language of the document: the
    texts from the translations of the plugins, the labels like the interface."""
    with use_locale(doc.lang):
        card = _Card(doc, '', 1)
        links = _Links(app, [card], beside=True)
        md = _markdown(links)
        values = _card(card, links, md, _translator(app, doc.lang), index=True)
        page = _page(app, doc.lang, _(doc.title), {'card': values, 'manual': None})
    return RenderedHelp(page, doc.warnings + links.warnings, sorted(links.external))


def render_manual(app: Any, manual: HelpManual, today: Optional[str] = None) -> RenderedHelp:
    """The manual as one standalone HTML file: every link an anchor inside it.
    The head of a manual composed without cards leads to the guides instead."""
    from kitebase.apptime import today as app_today

    with use_locale(manual.lang):
        tr = _translator(app, manual.lang)
        cards = [_Card(s.doc, f'{s.doc.root}-', s.depth + 1) for s in manual.sections if s.doc is not None]
        cards += [_Card(doc, f'{doc.root}-', 2) for doc in manual.others]
        head = not cards
        links = _Links(app, cards, beside=head,
                       appendices={a.id.partition('-')[2] for a in manual.appendices})
        md = _markdown(links)
        by_root = {c.doc.root: c for c in cards}

        def text(value: str) -> str:
            return md.render(tr(value))

        sections = []
        for s in manual.sections:
            sections.append({
                'id': s.id,
                'h': min(s.depth + 1, 6),
                'title': _(s.title),
                'texts': [text(t) for t in s.texts],
                'card': _card(by_root[s.doc.root], links, md, tr, index=False) if s.doc else None,
                'ref': links.anchor(s.ref) if s.ref else None,
                'link': links.anchor(s.page, _(s.title)) if head and s.page else None,
            })

        def toc(depth: int, start: int) -> List[Dict[str, Any]]:
            nodes, i = [], start
            while i < len(manual.sections) and manual.sections[i].depth >= depth:
                s = manual.sections[i]
                if s.depth == depth:
                    anchor = f'{s.doc.root}-top' if s.doc else s.id
                    nodes.append({'id': anchor, 'title': _(s.title), 'subs': toc(depth + 1, i + 1)})
                i += 1
            return nodes

        version = (getattr(app.pm, 'config', None) or {}).get('version')
        values = {
            'card': None,
            'manual': {
                'head': head,
                'date': today or app_today().isoformat(),
                'version': version,
                'intro': [text(t) for t in manual.intro],
                'toc': toc(1, 0),
                'sections': sections,
                'appendices': [{'id': a.id, 'title': a.title, 'html': text(a.text)}
                               for a in manual.appendices],
                'others': [_card(by_root[d.root], links, md, tr, index=False) for d in manual.others],
            },
        }
        page = _page(app, manual.lang, manual.title if head else _('Manual'), values)
    return RenderedHelp(page, manual.warnings + links.warnings, sorted(links.external))
