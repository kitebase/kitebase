"""
The state column of a table: where a record stands, which a board shows as columns.

A table declares it at its top level, and the kanban view of its lists reads it:

    Order:
      state: status                  # the values: the column's own `choices`

    Opportunity:
      state:
        column: stage_id             # the values: the rows of the table it points to
        within: pipeline_id          # ...those of the record's pipeline
        order: sequence              # their order, left to right
        folded: folded               # a column that starts folded

With `choices` the values are the code's: a closed set, always the same. With
a foreign key they are rows the users write, and `within` names a column both
tables have: the board shows one value of it at a time, the stages of one
pipeline and its opportunities. `order` and `folded` are columns of the table
pointed to.

The values come from the declaration and not from the rows loaded, so an
empty column shows, and it is where a card is dropped.
"""
from typing import Any, Dict, List, Optional

KEYS = ('column', 'within', 'order', 'folded')


def declared(table: Any) -> Optional[Dict[str, Any]]:
    """The `state` of a table as a dict, the short form expanded; None without one."""
    state = table.attributes.get('state')
    if isinstance(state, str):
        return {'column': state}
    if isinstance(state, dict) and isinstance(state.get('column'), str):
        return {k: state[k] for k in KEYS if isinstance(state.get(k), str)}
    return None


def state_info(table: Any) -> Optional[Dict[str, Any]]:
    """What the client needs to draw the board: the declaration, and the table
    the values are rows of (`target`) when they are."""
    state = declared(table)
    if state is None:
        return None
    col = next((c for c in table.effective_columns if c.name == state['column']), None)
    fk = col.attributes.get('foreign_key') if col is not None else None
    if fk and 'table' in fk:
        state['target'] = fk['table'].name
    return state


def make_issue(severity: str, code: str, path: str, message: str) -> Dict[str, Any]:
    """An issue in the format of kitebase.diagnostics, which sits above this module."""
    return {'severity': severity, 'code': code, 'path': path, 'message': message,
            'plugin': None}


def diagnose(app: Any) -> List[Dict[str, Any]]:
    """A state that names what is not there gives a board with no columns:
    said here, by name, rather than as an empty view."""
    issues: List[Dict[str, Any]] = []
    for name, table in (getattr(app, 'tables', None) or {}).items():
        if (getattr(table, 'attributes', None) or {}).get('state') is None:
            continue
        path = f'tables.{name}.state'
        state = declared(table)
        if state is None:
            issues.append(make_issue(
                'warning', 'state-form', path,
                "state is a column name, or {column, within, order, folded}"))
            continue
        cols = {c.name: c for c in table.effective_columns}
        col = cols.get(state['column'])
        if col is None:
            issues.append(make_issue(
                'warning', 'state-column', f'{path}.column',
                f"'{state['column']}' is not a column of {name}, so its lists have no board"))
            continue
        fk = col.attributes.get('foreign_key')
        target = fk['table'] if fk and 'table' in fk else None
        if target is None:
            if not isinstance(col.attributes.get('choices'), list):
                issues.append(make_issue(
                    'warning', 'state-values', f'{path}.column',
                    f"'{state['column']}' has neither choices nor a foreign key: "
                    f"the board would not know its columns"))
            for key in ('within', 'order', 'folded'):
                if key in state:
                    issues.append(make_issue(
                        'warning', 'state-key', f'{path}.{key}',
                        f"'{key}' applies to a state whose values are rows of a "
                        f"table; '{state['column']}' takes them from its choices"))
            continue
        target_cols = {c.name for c in target.effective_columns}
        within = state.get('within')
        if within and (within not in cols or within not in target_cols):
            issues.append(make_issue(
                'warning', 'state-within', f'{path}.within',
                f"'{within}' must be a column of both {name} and {target.name}"))
        for key in ('order', 'folded'):
            if key in state and state[key] not in target_cols:
                issues.append(make_issue(
                    'warning', 'state-key', f'{path}.{key}',
                    f"'{state[key]}' is not a column of {target.name}"))
    return issues


def check_board(table: Any, kanban: Any, path: str, plugin: Optional[str]) -> List[Dict[str, Any]]:
    """The `kanban` of a view on `table`: its `folded` values are values of the
    state's choices. Values that are rows of a table are data, which differ
    from one installation to the next: they say it in their own `folded`
    column, never in the YAML."""
    folded = kanban.get('folded') if isinstance(kanban, dict) else None
    if folded is None:
        return []
    issue = make_issue('warning', 'kanban-folded', f'{path}.kanban.folded', '')
    issue['plugin'] = plugin
    state = declared(table)
    if state is None:
        issue['message'] = 'the table declares no state, so the view has no board to fold'
        return [issue]
    col = next((c for c in table.effective_columns if c.name == state['column']), None)
    choices = col.attributes.get('choices') if col is not None else None
    if not isinstance(choices, list):
        issue['message'] = (f"the values of '{state['column']}' are rows of a table, which "
                            f"differ by installation: fold them with the column the state "
                            f"names as 'folded'")
        return [issue]
    known = {str(c.get('value')) for c in choices if isinstance(c, dict)}
    unknown = [v for v in (folded if isinstance(folded, list) else [folded]) if str(v) not in known]
    if not unknown:
        return []
    issue['message'] = (f"{', '.join(map(str, unknown))} not among the choices of "
                        f"'{state['column']}': {', '.join(sorted(known))}")
    return [issue]

