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
"""
import functools
import logging
from typing import Any, Callable, Dict, List, NoReturn, Optional

from sqlalchemy import event, inspect as sa_inspect

import kitebase
from kitebase.i18n import _f

_log = logging.getLogger('kitebase')

STAGES = ('check', 'commit', 'after')
MODES = ('insert', 'update', 'delete')

_RULES: Dict[str, Dict[str, List[Callable]]] = {}


def on_write(table: str, stage: str = 'check'):
    """Register a rule of `table` for one stage of its writes."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}, not {stage!r}")

    def register(fn: Callable) -> Callable:
        rules = _RULES.setdefault(table, {}).setdefault(stage, [])
        # A file imported twice - reloaded, or reachable under two package
        # names from two plugin roots - replaces its rule, not repeats it
        rules[:] = [r for r in rules if _identity(r) != _identity(fn)]
        rules.append(fn)
        return fn
    return register


def _identity(fn: Callable):
    code = getattr(fn, '__code__', None)
    return (code.co_filename if code else fn.__module__, fn.__qualname__)


def has_rules(table: str) -> bool:
    return any(_RULES.get(table, {}).values())


def _rules(table: str, stage: str) -> List[Callable]:
    return _RULES.get(table, {}).get(stage, [])


# ── What a rule can say ───────────────────────────────────────────────────────

class Ask(Exception):
    """A question without its answer: the write stops and the client asks."""

    def __init__(self, question: Dict[str, Any]):
        super().__init__(question.get('text'))
        self.question = question


class Stay(Exception):
    """A refusal: nothing is written, the form stays where it is."""

    def __init__(self, message: str, patch: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.patch = patch


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

    def _ask(self, question: Dict[str, Any]) -> NoReturn:
        if self.stage != 'check':
            raise RuntimeError(f"A '{self.stage}' rule cannot ask: questions belong "
                               "to check, before anything is written")
        raise Ask({k: v for k, v in question.items() if v is not None})

    def stay(self, message: str, patch: Optional[Dict[str, Any]] = None) -> NoReturn:
        """Refuse the write. `patch` changes the form's values all the same."""
        raise Stay(message, patch)

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
        return {'status': 'error', 'code': 409, 'message': a.question['text'],
                '$flow': _flow(ctx, ask=a.question)}
    except Stay as s:
        return {'status': 'error', 'code': 422, 'message': s.message,
                '$flow': _flow(ctx, patch=s.patch)}
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
    """

    def __init__(self, table: str, tree: Dict[str, Any], answers: Dict[str, Any]):
        root_id = tree.get('id')
        mode = 'insert' if root_id is None or (isinstance(root_id, int) and root_id < 0) else 'update'
        super().__init__(table, mode, dict(tree.get('values') or {}), None, answers)
        self.rows: Dict[str, List[Dict[str, Any]]] = {
            cid: [{**(node.get('values') or {}), 'id': node.get('id')} for node in nodes
                  if node.get('op') != 'delete']
            for cid, nodes in (tree.get('children') or {}).items()
        }

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
    ctx = Proposal(table, data.get('tree') or {}, dict(data.get('answers') or {}))
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
            from kitebase import access
            access.check_table(table, 'read')
            return propose(table, fn, data)
        body.proposes = fn  # type: ignore[attr-defined]
        return body
    return wrap
