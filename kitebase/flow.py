"""
The rules of a table, judged on every port that writes it.

A plugin registers functions on a table, for one of three stages of a write:

    @on_write('Loan')                      # check: before anything is written
    def loan_rules(ctx):
        if ctx.mode == 'insert' and too_many(ctx):
            if not ctx.confirm('many_open', _('Three loans open: lend anyway?')):
                ctx.stay()

    @on_write('Loan', 'commit')            # inside the transaction
    @on_write('Loan', 'after')             # once committed

`check` gathers: it reads, asks, refuses, and may change the values about to
be written, but writes nothing: its session refuses to flush or commit, so a
rule that tries finds out at once. `commit` runs in the transaction of the
write, after the row is in the session: what it writes commits or rolls back
with the record. `after` runs once the record is committed, and answers with
actions for the client (a ticket to print, a page to open).

A question interrupts the write. The answer goes back to the client, which
asks the person and sends the same call again with the answer added to
`answers`; the rules run again from the start and this time the question has
its reply. Nothing is kept on the server between the two calls.

The rules belong to the table, not to a form: `db` and `save_tree` both judge
them, so a call that skips the form does not skip a rule. Functions on the
same table and stage run in the order their plugins load.

A form also proposes, before anything is written: the step from one phase of a
document to the next. A proposal sees the document as it stands in the form,
header and rows, asks and refuses as a rule does, and answers with a patch the
form applies to its draft. It writes nothing:

    @endpoint('order_head', access=access.PER_TABLE)
    @proposal('PurchaseOrder')
    def order_head(ctx):
        ctx.rows['lines'] = [r for r in ctx.rows['lines'] if keep(r)]

A batch writes a document as its form would with `run_form`: the proposals of
its phases in order, then the save. It does not repeat the form's behaviour
in a second place, and what holds always stays a rule of the table.
"""
import functools
import logging
from typing import Any, Callable, Dict, List, NoReturn, Optional, Tuple

from sqlalchemy import event, inspect as sa_inspect

import kitebase
import kitebase.context
from kitebase import access
from kitebase.endpoints import CommandDispatcher
from kitebase.i18n import _f
from kitebase.outbound import to_client
from kitebase.pages import load_page
from kitebase.registry import Registry

_log = logging.getLogger('kitebase')

STAGES = ('check', 'commit', 'after')
MODES = ('insert', 'update', 'delete')

# (table, stage, the rule), under a name made of the three: a module imported
# again replaces its rules, it does not repeat them.
_RULES: Registry[Tuple[str, str, Callable]] = Registry('write_rules', 'write rule')


def on_write(table: str, stage: str = 'check'):
    """Register a rule of `table` for one stage of its writes."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}, not {stage!r}")

    def register(fn: Callable) -> Callable:
        _RULES.add(f"{table}:{stage}:{':'.join(_identity(fn))}", (table, stage, fn))
        return fn
    return register


def _identity(fn: Callable):
    code = getattr(fn, '__code__', None)
    return (code.co_filename if code else fn.__module__, fn.__qualname__)


def has_rules(table: str) -> bool:
    return any(t == table for t, _, _ in _RULES.values())


def _rules(table: str, stage: str) -> List[Callable]:
    return [fn for t, s, fn in _RULES.values() if t == table and s == stage]


# ── What a rule can say ───────────────────────────────────────────────────────

class Ask(Exception):
    """A question without its answer: the write stops and the client asks."""

    def __init__(self, question: Dict[str, Any]):
        super().__init__(question.get('text'))
        self.question = question


class Stay(Exception):
    """A refusal: nothing is written, the form stays where it is, or goes to `phase`."""

    def __init__(self, message: str, patch: Optional[Dict[str, Any]] = None,
                 phase: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.patch = patch
        self.phase = phase


class ReadOnly(RuntimeError):
    """A `check` rule tried to write: the rule is wrong, not the data."""


class Context:
    """
    What a rule sees and says, for one call. Built per request, never shared.

    `record` is the row as it will be written: on an update, the stored row
    with the changes over it; on a delete, the stored row. A `check` rule may
    change it, and the change is written. `original` is the stored row, None on
    an insert.
    """

    def __init__(self, table: str, mode: str, record: Dict[str, Any],
                 original: Optional[Dict[str, Any]], answers: Dict[str, Any]):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
        self.table = table
        self.mode = mode
        self.record = record
        self.original = original
        self.answers = answers
        self.session = None          # read-only in check, the write's in commit
        self.obj = None              # the ORM row, in commit
        self.stage = 'check'
        self.infos: List[str] = []
        self.actions: List[Dict[str, Any]] = []
        self.upload = None           # the file the client sent, after ctx.file asked for it

    # Questions: an answer present is used, an answer missing stops the write.

    def confirm(self, key: str, text: str, yes: Optional[str] = None,
                no: Optional[str] = None) -> bool:
        """A yes or no. The labels should say what each answer does."""
        if key in self.answers:
            return bool(self.answers[key])
        self._ask({'key': key, 'kind': 'confirm', 'text': text,
                   'yes': yes, 'no': no})

    def choose(self, key: str, text: str, options: List[Dict[str, Any]]) -> Any:
        """One of `options`, each `{value, label}`; the value comes back."""
        if key in self.answers:
            return self.answers[key]
        self._ask({'key': key, 'kind': 'choose', 'text': text, 'options': options})

    def form(self, key: str, view: str, values: Optional[Dict[str, Any]] = None,
             text: Optional[str] = None) -> Dict[str, Any]:
        """
        Values typed in a form: `view` names a form view of the plugins (a
        percentage, a date), `values` are its first values over the view's
        defaults. The values come back as a dict.
        """
        if key in self.answers:
            return dict(self.answers[key] or {})
        descriptor, first = form_view(view)
        self._ask({'key': key, 'kind': 'form', 'text': text, 'view': descriptor,
                   'values': {**first, **(values or {})}})

    def file(self, key: str, accept: Optional[List[str]] = None,
             text: Optional[str] = None):
        """
        A file from the person (`accept`: the extensions the picker offers).
        The client picks it and sends the same call again through the upload
        route, with the file and the answers so far; the file comes back as
        an `UploadedFile`.
        """
        if self.upload is not None:
            return self.upload
        self._ask({'key': key, 'kind': 'file', 'text': text, 'accept': accept})

    def _ask(self, question: Dict[str, Any]) -> NoReturn:
        if self.stage != 'check':
            raise RuntimeError(f"A '{self.stage}' rule cannot ask: questions belong "
                               "to check, before anything is written")
        raise Ask({k: v for k, v in question.items() if v is not None})

    def stay(self, message: str, patch: Optional[Dict[str, Any]] = None,
             phase: Optional[str] = None) -> NoReturn:
        """
        Refuse the write. `patch` changes the form's values all the same;
        `phase`, in a form with phases, is where the problem is: the form goes
        back there (the header, the rows) instead of staying on its step.
        """
        raise Stay(message, patch, phase)

    def check(self, ok: bool, text: str, key: str, severity: str = 'block') -> None:
        """
        A condition whose gravity is a setting: `block` refuses, `confirm` asks.
        """
        if ok:
            return
        if severity == 'confirm':
            if not self.confirm(key, text):
                self.stay(text)
        else:
            self.stay(text)

    def info(self, text: str) -> None:
        """A line for the person, shown with the outcome. Stops nothing."""
        self.infos.append(text)

    def action(self, **action: Any) -> None:
        """What the client does once the record is saved (after only)."""
        if self.stage != 'after':
            raise RuntimeError("Actions are answered once the record is saved: "
                               "register the rule for 'after'")
        self.actions.append(action)


def form_view(view_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    A form view of the plugins as the client shows it (references resolved,
    metadata out, labels translated), and its first values from `default`.
    """
    app = kitebase.utils.get_app()
    view = app.plugins.get(f'views.{view_id}')
    if view is None:
        raise ValueError(f"The form view '{view_id}' does not exist")
    view = to_client(app.plugins.resolve_refs(view))
    return view, {f['name']: f['default'] for f in _fields(view.get('layout') or [])
                  if 'default' in f}


def _fields(layout):
    """The fields of a layout, at any depth."""
    for node in layout:
        if not isinstance(node, dict):
            continue
        if 'name' in node:
            yield node
        for key in ('fields', 'columns', 'layout', 'pages'):
            if isinstance(node.get(key), list):
                yield from _fields(node[key])


# ── Running the stages ───────────────────────────────────────────────────────

def _forbid_writes(session) -> None:
    def no_flush(*_):
        raise ReadOnly("A check rule may read, not write: write in a 'commit' rule")

    def no_write_statement(state):
        if not state.is_select:
            raise ReadOnly("A check rule may read, not write: write in a 'commit' rule")

    event.listen(session, 'before_flush', no_flush)
    event.listen(session, 'before_commit', no_flush)
    event.listen(session, 'do_orm_execute', no_write_statement)


def row_values(obj) -> Dict[str, Any]:
    """The stored columns of an ORM row, by attribute name."""
    return {attr.key: getattr(obj, attr.key) for attr in sa_inspect(obj).mapper.column_attrs}


def judge(table: str, mode: str, record: Dict[str, Any],
          original: Optional[Dict[str, Any]], answers: Optional[Dict[str, Any]]):
    """
    Run the `check` rules. Returns `(ctx, refusal)`: a refusal is the envelope
    the endpoint answers with (a question or a refusal), None when the write
    may go on with `ctx.record`.
    """
    ctx = Context(table, mode, dict(record), original, dict(answers or {}))
    rules = _rules(table, 'check')
    if not rules:
        return ctx, None
    return ctx, _read_only(ctx, rules)


def _read_only(ctx: Context, fns: List[Callable]) -> Optional[Dict[str, Any]]:
    """Run `fns` on a session that refuses to write; the refusal envelope, or None."""
    app = kitebase.utils.get_app()
    try:
        with app.get_session() as session:
            _forbid_writes(session)
            ctx.session = session
            for fn in fns:
                fn(ctx)
    except Ask as a:
        return {'status': 'error', 'code': 409, 'message': a.question.get('text') or 'An answer is needed',
                '$flow': _flow(ctx, ask=a.question)}
    except Stay as s:
        return {'status': 'error', 'code': 422, 'message': s.message,
                '$flow': _flow(ctx, patch=s.patch, phase=s.phase)}
    finally:
        ctx.session = None
    return None


def commit(ctx: Context, session, obj) -> None:
    """Run the `commit` rules inside the write's transaction, before it commits."""
    ctx.stage, ctx.session, ctx.obj = 'commit', session, obj
    try:
        for rule in _rules(ctx.table, 'commit'):
            rule(ctx)
        session.flush()
    finally:
        ctx.session = None


def finish(ctx: Context, result: Dict[str, Any], saved: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Run the `after` rules on the committed record, and add to the endpoint's
    answer what the rules had to say. An `after` rule that fails does not undo
    the write: the person learns it, and the record stays saved.
    """
    ctx.stage, ctx.obj = 'after', None
    if saved is not None:
        ctx.record = saved
    for rule in _rules(ctx.table, 'after'):
        try:
            rule(ctx)
        except Exception:
            _log.exception('after rule %s of %s failed', getattr(rule, '__name__', rule), ctx.table)
            ctx.infos.append(_f('Saved, but a follow-up step failed: {step}',
                                step=getattr(rule, '__name__', '')))
    flow = _flow(ctx, actions=ctx.actions)
    if flow:
        result['$flow'] = flow
    return result


def _flow(ctx: Context, **parts: Any) -> Dict[str, Any]:
    flow = {k: v for k, v in parts.items() if v}
    if ctx.infos:
        flow['info'] = list(ctx.infos)
    return flow


# ── Proposals: what a form suggests before anything is written ───────────────

class Proposal(Context):
    """
    What a proposal sees: the document as the form holds it.

    `record` is the header, `rows` the rows of each collection by its id, each
    row a dict of its values with its `id` (negative for a row never written;
    a row the proposal adds has none). Change them in place, or replace a list
    whole: the form receives what differs from what it sent.

    A command on the rows also says where it was given: `collection`, the id
    of the collection; `current`, the row under the cursor; `checked`, the
    rows ticked; `visible`, the rows the column filters leave, None when
    they leave all. None and empty when the proposal is a step between phases.
    """

    def __init__(self, table: str, tree: Dict[str, Any], answers: Dict[str, Any],
                 scope: Optional[Dict[str, Any]] = None):
        root_id = tree.get('id')
        mode = 'insert' if root_id is None or (isinstance(root_id, int) and root_id < 0) else 'update'
        super().__init__(table, mode, dict(tree.get('values') or {}), None, answers)
        self.rows: Dict[str, List[Dict[str, Any]]] = {
            cid: [{**(node.get('values') or {}), 'id': node.get('id')} for node in nodes
                  if node.get('op') != 'delete']
            for cid, nodes in (tree.get('children') or {}).items()
        }
        scope = scope or {}
        self.collection: Optional[str] = scope.get('collection')
        self.current: Any = scope.get('current')
        self.checked: List[Any] = list(scope.get('checked') or [])
        visible = scope.get('visible')
        self.visible: Optional[List[Any]] = None if visible is None else list(visible)

    def chosen(self, collection: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        The rows a command works on: the ticked ones; without ticks, those the
        filters leave on screen; without filters, all of them.
        """
        rows = self.rows.get(collection or self.collection, [])
        if self.checked:
            keep = self.checked
        elif self.visible is not None:
            keep = self.visible
        else:
            return rows
        return [r for r in rows if r.get('id') in keep]

    def patch(self, values: Dict[str, Any], rows: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
        """What changed against what the form sent: values by key, collections whole."""
        changed = {k: v for k, v in self.record.items() if k not in values or values[k] != v}
        moved = {cid: [{'id': r.get('id'), 'values': {k: v for k, v in r.items() if k != 'id'}}
                       for r in current]
                 for cid, current in self.rows.items() if current != rows.get(cid, [])}
        out: Dict[str, Any] = {}
        if changed:
            out['values'] = changed
        if moved:
            out['rows'] = moved
        return out


def propose(table: str, fn: Callable, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run a proposal on the document in `data` (`tree`, in the shape of
    `save_tree`, and `answers`). The answer is the endpoint's: the patch on
    success, a question or a refusal as the rules give them.
    """
    ctx = Proposal(table, data.get('tree') or {}, dict(data.get('answers') or {}),
                   data.get('scope'))
    ctx.upload = data.get('file')
    values = dict(ctx.record)
    rows = {cid: [dict(r) for r in current] for cid, current in ctx.rows.items()}
    refusal = _read_only(ctx, [fn])
    if refusal is not None:
        return refusal
    result: Dict[str, Any] = {'status': 'success', 'data': ctx.patch(values, rows)}
    flow = _flow(ctx)
    if flow:
        result['$flow'] = flow
    return result


def proposal(table: str):
    """
    Make `fn(ctx)` the body of an endpoint that proposes changes to a document
    of `table`. Reading the table is the least a proposal needs; writing is
    the save's to check, when the document is written.
    """
    def wrap(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def body(data: Dict[str, Any]) -> Dict[str, Any]:
            access.check_table(table, 'read')
            return propose(table, fn, data)
        body.proposes = fn  # type: ignore[attr-defined]
        return body
    return wrap


# ── The form without its client: a document written as the form would ───────

def run_form(page_id: str, tree: Dict[str, Any],
             answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Write a new document through its form, without a client: each phase that
    has a judge proposes on the tree, in order, and its patch is applied as the
    client applies it; then `save_tree`, with the rules of the table.

    `tree` is the document as a form holds it, `{values, children: {cid:
    [{values}, ...]}}`, ids optional. `answers` answers every question up
    front: a question without its answer stops the run, and nothing is
    written. Calls go through the dispatcher, so they run with the permissions
    of whoever is in the context, as a client's would.

    Returns the answer of `save_tree`, with the lines the proposals had for the
    person, or the refusal of the step that stopped.
    """
    if isinstance(tree.get('id'), int) and tree['id'] >= 0:
        return {'status': 'error', 'code': 400,
                'message': 'run_form writes new documents: this one already has a key'}
    page = load_page(kitebase.utils.get_app(), page_id)
    if page is None:
        return {'status': 'error', 'code': 404, 'message': f"No page '{page_id}'"}

    seq = [0]
    doc = _with_ids(tree, seq)
    answers = dict(answers or {})
    infos: List[str] = []
    send = _sender()
    refusal = _propose_all(page, doc, answers, seq, send, infos)
    if refusal is not None:
        return refusal

    result = send({'operation': 'save_tree',
                   'parameters': {'page': page_id, 'root': _created(doc), 'answers': answers}})
    return _with_infos(result, infos)


def run_forms(page_id: str, trees: List[Dict[str, Any]],
              answers: Optional[Dict[str, Any]] = None,
              labels: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    `run_form` for many new documents written as **one** act: every document
    through the proposals of its phases first, then all of them in one
    transaction (`save_trees`), so they are all written or none is. The same
    `answers` serve every document. A refusal names its document by its
    label (the key of its rows in a sheet, say), or by its position.
    """
    from kitebase.endpoint_tree import save_trees

    page = load_page(kitebase.utils.get_app(), page_id)
    if page is None:
        return {'status': 'error', 'code': 404, 'message': f"No page '{page_id}'"}
    answers = dict(answers or {})
    names = labels or [f'#{i + 1}' for i in range(len(trees))]
    send = _sender()
    infos: List[str] = []
    roots = []
    for tree, name in zip(trees, names):
        if isinstance(tree.get('id'), int) and tree['id'] >= 0:
            return {'status': 'error', 'code': 400,
                    'message': f'{name}: run_forms writes new documents'}
        seq = [0]
        doc = _with_ids(tree, seq)
        refusal = _propose_all(page, doc, answers, seq, send, infos)
        if refusal is not None:
            refusal['message'] = f"{name}: {refusal.get('message')}"
            return refusal
        roots.append(_created(doc))

    result = save_trees(page_id, roots, answers)
    if result.get('status') != 'success':
        index = (result.get('data') or {}).get('index')
        if isinstance(index, int) and index < len(names):
            result['message'] = f"{names[index]}: {result.get('message')}"
        return result
    return _with_infos(result, infos)


def _sender() -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Calls through the dispatcher, as the caller: the access check of each
    endpoint is the one a request for it gets."""
    dispatcher = CommandDispatcher()
    return lambda command: dispatcher.call(command['operation'], command.get('parameters'))


def _propose_all(page: Dict[str, Any], doc: Dict[str, Any], answers: Dict[str, Any],
                 seq: List[int], send, infos: List[str]) -> Optional[Dict[str, Any]]:
    """The proposals of the page's phases on `doc`, in order, each patch
    applied. The refusal of the step that stopped, or None."""
    for phase in (page.get('content') or {}).get('phases') or []:
        endpoint = (phase.get('next') or {}).get('endpoint') if isinstance(phase, dict) else None
        if not endpoint:
            continue
        res = send({'operation': endpoint, 'parameters': {'tree': doc, 'answers': answers}})
        if res.get('status') != 'success':
            ask = (res.get('$flow') or {}).get('ask')
            if ask:
                # Nobody to ask: the caller learns which answer to add
                res['message'] = _f("Unanswered '{name}': {text}", name=ask['key'], text=ask.get('text', ''))
            return res
        apply_patch(doc, res.get('data') or {}, seq)
        infos += (res.get('$flow') or {}).get('info') or []
    return None


def _with_infos(result: Dict[str, Any], infos: List[str]) -> Dict[str, Any]:
    if infos:
        flow = result.setdefault('$flow', {})
        flow['info'] = infos + (flow.get('info') or [])
    return result


def apply_patch(node: Dict[str, Any], patch: Dict[str, Any], seq: List[int]) -> None:
    """A proposal's patch on a tree: the values, and each collection whole."""
    node['values'].update(patch.get('values') or {})
    for cid, rows in (patch.get('rows') or {}).items():
        apply_rows(node, cid, rows, seq)


def apply_rows(parent: Dict[str, Any], cid: str, rows: List[Dict[str, Any]],
               seq: List[int]) -> None:
    """
    Make a collection what a proposal says it should be, as the client does
    (`applyRows` in aggregate.ts): a known row takes the values, a row without
    a known id is created, a live row left out is removed (a stored one is
    marked for deletion, one never written simply goes). Rows end up in the
    proposal's order.
    """
    current = parent.setdefault('children', {}).get(cid, [])
    live = {r['id']: r for r in current if r.get('op') != 'delete'}
    kept: List[Dict[str, Any]] = []
    for patch in rows:
        row = live.pop(patch.get('id'), None) if patch.get('id') is not None else None
        if row is None:
            seq[0] += 1
            row = {'id': -seq[0], 'op': 'create', 'values': {}, 'children': {}}
        changed = {k: v for k, v in (patch.get('values') or {}).items()
                   if row['values'].get(k) != v}
        if changed:
            row['values'].update(changed)
            if not row.get('op'):
                row['op'] = 'update'
        kept.append(row)
    going = [r for r in current if r.get('op') == 'delete']
    for row in live.values():
        if isinstance(row['id'], int) and row['id'] >= 0:
            row['op'] = 'delete'
            going.append(row)
    parent['children'][cid] = kept + going


def _with_ids(node: Dict[str, Any], seq: List[int]) -> Dict[str, Any]:
    """A copy of a document with a temporary id on every node that has none."""
    node_id = node.get('id')
    if node_id is None:
        seq[0] += 1
        node_id = -seq[0]
    return {'id': node_id, 'values': dict(node.get('values') or {}),
            'children': {cid: [_with_ids(child, seq) for child in children]
                         for cid, children in (node.get('children') or {}).items()}}


def _created(node: Dict[str, Any]) -> Dict[str, Any]:
    """The save_tree payload of a document nobody has written yet."""
    return {'id': node['id'], 'op': 'create', 'values': node['values'],
            'children': {cid: [_created(child) for child in children
                               if child.get('op') != 'delete']
                         for cid, children in node['children'].items()}}
