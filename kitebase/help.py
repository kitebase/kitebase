"""
kitebase.help: the guide of a menu entry, composed from what the plugins declare.

Whoever sees a menu entry wants one page from there on, not one per screen. So
`help_merge` starts from the page of the entry and follows the screens it can
reach, and the guide reads in the order the user meets them: a section for each
screen, one for each phase of a form, with the fields and the commands found
there. Every field is told once, whichever screens show it. A foreign key ends
the walk and stays a link, and a page with a menu entry of its own becomes a
reference.

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

from kitebase.pages import BUTTON, COLLECTION, LIST_TYPES, field_from_column, load_page, lookup_page

_TEMPLATES = Path(__file__).parent / 'help_template'

# Keys of a descriptor that hold other nodes.
_CHILDREN = ('layout', 'columns', 'fields', 'pages', 'tabs', 'children')

# How a kind of field is filled in, the same everywhere: said once in a guide.
_HOW_TO_FILL = {
    'foreign_key': (
        'Choosing from another table',
        'Type to search and pick with the arrow keys and Enter. '
        '*Search more...*, at the end of the list, opens the whole table.',
    ),
    'date': (
        'Dates',
        'Type the date, or pick it from the calendar.',
    ),
}


@dataclass
class HelpField:
    key: str
    label: str
    text: str
    link: Optional[str] = None


@dataclass
class HelpCommand:
    label: str
    text: str


@dataclass
class HelpSection:
    """A screen, or a part of one: subsections never hold subsections."""
    id: str
    title: str
    parent: Optional['HelpSection'] = None
    texts: List[str] = field(default_factory=list)
    fields: List[HelpField] = field(default_factory=list)
    commands: List[HelpCommand] = field(default_factory=list)
    subs: List['HelpSection'] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.texts or self.fields or self.commands or self.subs)


@dataclass
class HelpDoc:
    root: str
    title: str
    intro: List[str] = field(default_factory=list)
    sections: List[HelpSection] = field(default_factory=list)
    how: List[str] = field(default_factory=list)
    see_also: List[str] = field(default_factory=list)
    screens: List[str] = field(default_factory=list)
    # The section each screen landed in
    anchors: Dict[str, str] = field(default_factory=dict)

    def all_sections(self) -> Iterator[HelpSection]:
        for section in self.sections:
            yield section
            yield from section.subs

    def all_fields(self) -> Iterator[HelpField]:
        for section in self.all_sections():
            yield from section.fields


def _humanize(name: str) -> str:
    return name.replace('_', ' ').strip().capitalize()


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

    def screen(self, page_id: str, into: Optional[HelpSection] = None,
               skip: Optional[str] = None) -> None:
        """Walk a screen: into a section of its own, or into `into` when it only
        details the place it is reached from, like the form of a row."""
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

        section = into
        if section is None:
            section = HelpSection(f'page-{page_id}', page.get('title') or _humanize(page_id))
            self.doc.sections.append(section)
        self.doc.anchors[page_id] = section.id

        content = page.get('content') or {}
        model = (content.get('source') or {}).get('model')
        self.table_text(model, self.doc.intro if page_id == self.root else section.texts)
        self.text(section.texts, page.get('help'))
        self.text(section.texts, _discourse(self.app, page_id))

        self.content(content, model, section, skip)
        for panel in page.get('panels') or []:
            label = panel.get('label')
            self.content(panel, (panel.get('source') or {}).get('model') or model,
                         self.sub(section, label) if label else section)

    def text(self, texts: List[str], text: Any) -> None:
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())

    def table_text(self, model: Optional[str], texts: List[str]) -> None:
        table = self.app.tables.get(model) if model else None
        if table is not None and model not in self.seen_tables:
            self.seen_tables.add(model)
            self.text(texts, table.attributes.get('help'))

    def sub(self, section: HelpSection, title: str) -> HelpSection:
        """The part of a screen with this title: a tab called like its phase is that phase."""
        if section.title == title:
            return section
        owner = section.parent or section
        if owner.title == title:
            return owner
        for sub in owner.subs:
            if sub.title == title:
                return sub
        sub = HelpSection(f'{owner.id}-{len(owner.subs) + 1}', title, parent=owner)
        owner.subs.append(sub)
        return sub

    def content(self, node: Dict[str, Any], model: Optional[str], section: HelpSection,
                skip: Optional[str] = None) -> None:
        if node.get('type') in LIST_TYPES:
            self.grid(node, model, section)
            return
        phases = node.get('phases')
        if phases:
            self.phases(node, phases, model, section, skip)
        else:
            self.walk(node, model, section, skip)

    def phases(self, node: Dict[str, Any], phases: List[Dict[str, Any]],
               model: Optional[str], section: HelpSection, skip: Optional[str]) -> None:
        """A form with phases reads in the order of its phases, not of its layout,
        where a footer may well stand before the rows."""
        by_id = {n['id']: n for n in node.get('layout') or [] if isinstance(n, dict) and n.get('id')}
        placed: Set[str] = set()
        for phase in phases:
            label = phase.get('label')
            where = self.sub(section, label) if label else section
            for node_id in phase.get('nodes') or []:
                if node_id in by_id:
                    placed.add(node_id)
                    self.walk(by_id[node_id], model, where, skip)
        for node_id, rest in by_id.items():
            if node_id not in placed:
                self.walk(rest, model, section, skip)

    def walk(self, node: Any, model: Optional[str], section: HelpSection,
             skip: Optional[str] = None) -> None:
        if isinstance(node, list):
            for item in node:
                self.walk(item, model, section, skip)
            return
        if not isinstance(node, dict):
            return
        kind = node.get('type')
        if kind == COLLECTION:
            self.collection(node, section)
            return
        if kind == BUTTON:
            self.button(node, section)
            return
        if isinstance(node.get('field'), str):
            return
        if isinstance(node.get('label'), str) and any(k in node for k in _CHILDREN):
            section = self.sub(section, node['label'])
        if isinstance(node.get('name'), str) and 'filler' not in node:
            self.form_field(node, model, section, skip)
            return
        for key in _CHILDREN:
            self.walk(node.get(key), model, section, skip)

    def collection(self, node: Dict[str, Any], section: HelpSection) -> None:
        model = node.get('model')
        self.table_text(model, section.texts)
        self.grid(node.get('view') or {}, model, section, follow=False)
        form = node.get('form') or (f'{str(model).lower()}_form' if model else None)
        if form:
            self.screen(form, into=section, skip=node.get('fk'))

    def button(self, node: Dict[str, Any], section: HelpSection) -> None:
        self.command(node, section)
        opens = node.get('opens')
        if not isinstance(opens, dict):
            return
        if opens.get('type') == COLLECTION:
            label = node.get('label')
            self.collection(opens, self.sub(section, label) if label else section)
        elif isinstance(opens.get('page'), str):
            self.screen(opens['page'])

    def grid(self, view: Dict[str, Any], model: Optional[str], section: HelpSection,
             follow: bool = True) -> None:
        self.grids.append({'view': view, 'model': model, 'section': section})
        navigator = view.get('navigator')
        if isinstance(navigator, dict):
            for command in navigator.get('commands') or []:
                self.command(command, section)
        if follow and navigator and model:
            form = navigator.get('form_id') if isinstance(navigator, dict) else None
            self.screen(form or f'{model.lower()}_form')

    def command(self, command: Any, section: HelpSection) -> None:
        if isinstance(command, dict) and isinstance(command.get('help'), str):
            label = command.get('label') or _humanize(str(command.get('id', '')))
            section.commands.append(HelpCommand(label, command['help'].strip()))

    def form_field(self, node: Dict[str, Any], model: Optional[str], section: HelpSection,
                   skip: Optional[str]) -> None:
        name = node['name']
        if name == skip:
            return
        if not node.get('readonly'):
            self.how(node)
        self.add_field(f'{model or "form"}.{name}', node, section)

    def how(self, node: Dict[str, Any]) -> None:
        kind = None
        if node.get('foreign_key'):
            kind = 'foreign_key'
        elif self.is_date(node):
            kind = 'date'
        if kind and kind not in self.doc.how:
            self.doc.how.append(kind)

    def is_date(self, node: Dict[str, Any]) -> bool:
        type_name = node.get('type')
        visited: Set[str] = set()
        while isinstance(type_name, str) and type_name not in visited:
            if type_name in ('Date', 'DateTime'):
                return True
            visited.add(type_name)
            declared = self.app.pm.get(f'types.{type_name}')
            type_name = declared.get('base') if isinstance(declared, dict) else None
        return False

    def add_field(self, key: str, attrs: Dict[str, Any], section: HelpSection,
                  label: Optional[str] = None) -> None:
        if key in self.seen_fields:
            return
        self.seen_fields.add(key)
        text = attrs.get('help')
        if not isinstance(text, str) or not text.strip():
            return
        section.fields.append(HelpField(
            key=key,
            label=attrs.get('label') or label or _humanize(attrs.get('name', key)),
            text=text.strip(),
            link=lookup_page(attrs),
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
                if not isinstance(expr, str):
                    continue
                name = expr.split(' as ')[0].strip()
                head, _, rest = name.partition('.')
                if rest and head in self.app.tables and head != grid['model']:
                    continue
                name = rest if rest and head == grid['model'] else head
                if name in columns:
                    self.add_field(f"{grid['model']}.{name}", field_from_column(columns[name]),
                                   grid['section'], column.get('title'))

    def prune(self) -> None:
        """A screen with nothing to tell leaves no heading behind."""
        for section in self.doc.sections:
            section.subs = [s for s in section.subs if not s.is_empty()]
        self.doc.sections = [s for s in self.doc.sections if not s.is_empty()]
        kept = {s.id for s in self.doc.all_sections()}
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

    def view(section: HelpSection) -> Dict[str, Any]:
        return {
            'id': section.id,
            'title': section.title,
            'texts': [md.render(t) for t in section.texts],
            'fields': [{
                'id': f'field-{f.key}',
                'label': links.anchor(f.link, f.label) if f.link else html.escape(f.label),
                'html': md.render(f.text),
            } for f in section.fields],
            'commands': [{'label': c.label, 'html': md.render(c.text)} for c in section.commands],
            'subs': [view(s) for s in section.subs],
        }

    env = _environment(app)
    menu = _menu_pages(app)
    config = getattr(app.pm, 'config', None) or {}
    page = env.get_template('skeleton.html').render(
        app_title=config.get('title') or config.get('name') or '',
        title=doc.title,
        style=env.loader.get_source(env, 'style.css')[0],
        intro=[md.render(t) for t in doc.intro],
        sections=[view(s) for s in doc.sections],
        how=[{'title': _HOW_TO_FILL[k][0], 'html': md.render(_HOW_TO_FILL[k][1])} for k in doc.how],
        see_also=[links.anchor(p, menu.get(p)) for p in doc.see_also],
    )
    return RenderedHelp(page, links.warnings, sorted(links.external))
