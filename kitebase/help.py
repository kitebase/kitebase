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
from typing import Any, Dict, Iterator, List, Optional, Set

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape
from markdown_it import MarkdownIt

from kitebase.media import MediaError, parse_size
from kitebase.pages import BUTTON, COLLECTION, LIST_TYPES, field_from_column, load_page, lookup_page

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
        parts.append(f'up to {size / 1024 / 1024:g} MB')
    return ', '.join(parts)


def _discourse(app: Any, page_id: str) -> Optional[str]:
    """The discursive text of a page: the plugin that comes last wins, like a merge."""
    for name in reversed(app.pm.sorted):
        path = Path(app.pm.plugins[name].plugin_dir) / 'help' / 'en' / f'{page_id}.md'
        if path.is_file():
            return path.read_text(encoding='utf-8')
    return None


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

    def __init__(self, app: Any, root: str, doc: HelpDoc):
        self.app = app
        self.root = root
        self.doc = doc
        self.menu = _menu_pages(app)
        self.seen_tables: Set[str] = set()
        self.seen_fields: Set[str] = set()
        self.grids: List[Dict[str, Any]] = []

    # ── Screens ─────────────────────────────────────────────────────────────

    def screen(self, page_id: str, group: Optional[HelpGroup] = None,
               skip: Optional[str] = None) -> None:
        """Walk a screen: as the record itself, or inside `group` when it only details
        a part of it. `help_compose: false` leaves its written text and nothing else."""
        if page_id in self.doc.screens:
            return
        if page_id != self.root and page_id in self.menu:
            if page_id not in self.doc.see_also:
                self.doc.see_also.append(page_id)
            return
        page = load_page(self.app, page_id)
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
        self.text(texts, _discourse(self.app, page_id))
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
            page = load_page(self.app, page_id) or {}
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
            page = load_page(self.app, page_id) or {}
            if _model_of(page.get('content') or {}) == model:
                return page_id
        return f'{model.lower()}_list'

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
                head, _, rest = name.partition('.')
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


def help_merge(app: Any, page_id: str) -> HelpDoc:
    """The guide of a page, as data.

    Raises:
        ValueError: if no page answers to that id
    """
    page = load_page(app, page_id)
    if page is None:
        raise ValueError(f"No page '{page_id}'")
    title = _menu_pages(app).get(page_id) or page.get('title') or _humanize(page_id)
    doc = HelpDoc(root=page_id, title=title)
    merge = _Merge(app, page_id, doc)
    merge.screen(page_id)
    merge.grid_columns()
    merge.prune()
    return doc


# ── Render ──────────────────────────────────────────────────────────────────

_LINK = re.compile(r'\[\[([^\]\n]+)\]\]')


class _Links:
    """What a link points to: an anchor when the object is in the document, the
    file beside it otherwise. Every link carries `data-help`, for a client to
    intercept. A target that does not exist stays text, with a warning."""

    def __init__(self, app: Any, doc: HelpDoc):
        self.app = app
        self.doc = doc
        self.warnings: List[str] = []
        self.external: Set[str] = set()

    def resolve(self, ref: str):
        """(href, default text) of a reference, or None."""
        told = next((f for f in self.doc.all_fields() if f.key == ref), None)
        if told is not None:
            return f'#field-{ref}', told.label
        head, _, rest = ref.partition('.')
        table = self.app.tables.get(head)
        if table is not None:
            label = head
            if rest:
                column = next((c for c in table.effective_columns if c.name == rest), None)
                if column is None:
                    return None
                label = column.attributes.get('label') or _humanize(rest)
            return self.page_href(f'{head.lower()}_list'), label
        page = load_page(self.app, ref)
        if page is None:
            return None
        return self.page_href(ref), page.get('title') or _humanize(ref)

    def page_href(self, page_id: str) -> str:
        if page_id in self.doc.screens:
            return f"#{self.doc.anchors.get(page_id, 'top')}"
        self.external.add(page_id)
        return f'{page_id}.html'

    def anchor(self, ref: str, text: Optional[str] = None) -> str:
        target = self.resolve(ref)
        if target is None:
            self.warnings.append(f"Link to '{ref}': no such page, table or column")
            return html.escape(text or ref)
        href, default = target
        return (f'<a href="{html.escape(href, quote=True)}" '
                f'data-help="{html.escape(ref, quote=True)}">{html.escape(text or default)}</a>')


def _markdown(links: _Links) -> MarkdownIt:
    md = MarkdownIt('commonmark', {'html': False})

    def rule(state, silent):
        match = _LINK.match(state.src, state.pos)
        if match is None:
            return False
        if not silent:
            state.push('help_link', '', 0).content = match.group(1)
        state.pos = match.end()
        return True

    def render(self, tokens, idx, options, env):
        ref, _, text = tokens[idx].content.partition('|')
        return links.anchor(ref.strip(), text.strip() or None)

    md.inline.ruler.before('emphasis', 'help_link', rule)
    md.add_render_rule('help_link', render)
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
    return Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(['html']))


def render_html(app: Any, doc: HelpDoc) -> RenderedHelp:
    """The guide as a standalone HTML page."""
    links = _Links(app, doc)
    md = _markdown(links)

    def commands(items: List[HelpCommand]) -> List[Dict[str, str]]:
        return [{'label': c.label, 'html': md.render(c.text)} for c in items]

    def group(g: HelpGroup) -> Dict[str, Any]:
        return {
            'id': g.id,
            'title': g.title,
            'texts': [md.render(t) for t in g.texts],
            'fields': [{
                'id': f'field-{f.key}',
                'label': links.anchor(f.link, f.label) if f.link else html.escape(f.label),
                'html': md.render(f.text) if f.text else '',
                'limits': f.limits,
            } for f in g.fields],
            'commands': commands(g.commands),
        }

    def item(i: HelpItem) -> Dict[str, Any]:
        if i.ref:
            title = links.anchor(i.ref, i.title)
        elif i.anchor:
            title = f'<a href="#{html.escape(i.anchor, quote=True)}">{html.escape(i.title)}</a>'
        else:
            title = html.escape(i.title)
        return {'title': title, 'html': md.render(i.text) if i.text else '',
                'commands': commands(i.commands)}

    parts = [{'id': key, 'title': title, 'items': [item(i) for i in doc.parts[key]]}
             for key, title in _PART_TITLES.items() if doc.parts.get(key)]

    index: List[Dict[str, Any]] = []
    if doc.commands:
        index.append({'id': 'commands', 'title': 'Commands', 'subs': []})
    if doc.groups:
        index.append({'id': 'fields', 'title': 'Fields',
                      'subs': [{'id': g.id, 'title': g.title} for g in doc.groups if g.title]})
    index.extend({'id': p['id'], 'title': p['title'], 'subs': []} for p in parts)
    if doc.how:
        index.append({'id': 'filling-in', 'title': _HOW_TITLE, 'subs': []})

    env = _environment(app)
    menu = _menu_pages(app)
    config = getattr(app.pm, 'config', None) or {}
    page = env.get_template('skeleton.html').render(
        app_title=config.get('title') or config.get('name') or '',
        title=doc.title,
        style=env.loader.get_source(env, 'style.css')[0],
        intro=[md.render(t) for t in doc.intro],
        index=index,
        commands=commands(doc.commands),
        groups=[group(g) for g in doc.groups],
        parts=parts,
        how_title=_HOW_TITLE,
        how=[{'title': h.title, 'html': md.render(h.text)} for h in doc.how],
        see_also=[links.anchor(p, menu.get(p)) for p in doc.see_also],
    )
    return RenderedHelp(page, doc.warnings + links.warnings, sorted(links.external))
