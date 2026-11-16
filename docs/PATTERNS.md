# Patterns

Recurring requests, and the form kitebase answers them with. Each entry has the
same shape: the problem, the form chosen, an example, and what to avoid. The
reference of each key is in [PLUGIN_MODEL.md](PLUGIN_MODEL.md); this page says
which keys to reach for, and why.

An entry is here because at least two real cases use it and tests cover it. A
form still being tried is not.

---

## A board over a state column

**The problem.** "I want to see the orders (the tickets, the negotiations, the
jobs) as cards in columns, and move them from one column to the next."

**The form.** A board is not a component of its own: it is one more way of
looking at a list, beside the table and the cards. The **model** says which
column tells where a record stands; the **view** offers the board. Everything
else (the query, the rules, the search, the form a card opens, the commands)
is the list's.

The values of the state come from one of two places:

- **the code's own**, a closed set: the column's `choices`. The columns of the
  board are always the same, and the order is the order of the choices;
- **the users'**, rows of a table they edit: the column is a foreign key, and
  `order` names the column that sorts them. When the rows belong to something
  (the stages of a pipeline, the columns of a project), `within` names it: the
  board shows one value of it at a time, picked above the board.

The board takes its columns from this declaration, not from the rows loaded:
an empty column shows, and it is where a card is dropped. Dragging a card
writes the state through the `db` port, so the table's rules judge it as they
judge a form; a refusal leaves the card where it was, with the message.

**The example.** A help desk, with a status the code owns:

```yaml
# model.yaml
tables:
  Ticket:
    state: status
    columns:
      - name: status
        type: ShortStr
        default: "'new'"
        nullable: false
        choices:
          - { value: new, label: New }
          - { value: working, label: In progress }
          - { value: waiting, label: Waiting for the customer }
          - { value: solved, label: Solved }

# pages.yaml
pages:
  ticket_list:
    title: Tickets
    content:
      type: table
      allow_views: [table, kanban]
      source: { model: Ticket }
      columns:
        - field: subject
        - field: Customer.name as customer
        - field: status
      card: { title: subject, subtitle: customer }
      kanban:
        folded: [waiting]        # a narrow strip with its count; a click opens it
```

A workshop whose stages each team defines for itself:

```yaml
tables:
  Job:
    state: { column: stage_id, within: team_id, order: sequence, folded: folded }
  JobStage:                     # team_id, name, sequence, folded
    ...
```

The view adds `kanban: {sum: hours}` to head each column with a total, and the
`rules:` it opens with (only the open ones, only mine) as a starting point the
user may change.

**What to avoid.**

- **A transition endpoint** for the drag. A move is an update of the column;
  what may not happen is a write rule (`on_write`), which then holds on every
  port, the form and an import included.
- **Hard-coded stages the users must change.** If the people of the business
  rename, reorder or add stages, they are rows of a table, not `choices`.
  And the other way round: a status the code reads (to decide what is
  invoiced) belongs to the code, as `choices`.
- **Folding by id in the YAML.** Ids are data and differ from one installation
  to the next: stages kept in a table say they start folded in their own
  column (`folded`), which the users tick. `kanban.folded` is for `choices`.
- **Several values of `within` on one board.** The stages of two pipelines are
  not one set of columns; the board shows one at a time.
- **Closing by dragging.** When a state ends the record's life (won, lost,
  cancelled), make it a command, which may ask what it needs (a reason), and
  keep the board's columns for the states it passes through.
- **A board component per application.** What a board of yours adds (a ribbon
  coloured by a deadline, a total, a command) is a declaration on the view: a
  `tone` on a column, `kanban.sum`, a navigator command.
