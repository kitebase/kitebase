"""Tests for the relationship half of model generation.

A foreign key in YAML produces three things: the column, and a *pair* of Python
attributes to navigate it — `Loan.book` and `Book.loans`. Only the column exists
in SQL; the two attributes are named by the generator, and naming them from the
target table (as the first implementation did) breaks as soon as a table
references itself, or references the same target twice: both attributes land on
the same name and SQLAlchemy cannot tell which column carries the join.

The tests below check the generated text where the rule is what matters, and let
SQLAlchemy configure the mappers where the proof is that the model actually
works — the text can look plausible and still not map.
"""
import importlib.util

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers

import kitebase.utils
from kitebase.db import DB, Base
from kitebase.plugins import PluginsManager
from kitebase.source import Generator


@pytest.fixture(autouse=True)
def fresh_registry():
    """Each test maps its own classes onto the shared declarative Base."""
    yield
    Base.registry.dispose()
    Base.metadata.clear()


def build(tmp_path, monkeypatch, tables, source=None, config=None):
    """Write a one-plugin app declaring `tables`, and return the resolved schema.

    `source` is the plugin's model.py, when the test needs generated classes to
    inherit from a Python class of the same name; `config` adds keys to the app
    config.yaml.
    """
    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True, exist_ok=True)   # a test may generate twice
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': tables}))
    if source:
        (plugin / 'model.py').write_text(source)

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins'], **(config or {})}))
    monkeypatch.chdir(tmp_path)

    manager = PluginsManager()
    manager.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(manager)
    manager.load_plugins()

    db = DB()
    db.calc_db(manager)
    return db


def generate(tmp_path, monkeypatch, tables, source=None, config=None):
    """The model source generated for `tables`."""
    db = build(tmp_path, monkeypatch, tables, source, config)
    out = tmp_path / 'generated_model.py'
    Generator(db).generate(filename=str(out))
    return out.read_text()


def load(tmp_path, source):
    """Import the generated source and let SQLAlchemy configure the mappers."""
    path = tmp_path / 'generated_model.py'
    path.write_text(source)
    spec = importlib.util.spec_from_file_location('generated_model', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    configure_mappers()
    return module


def pk():
    return {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True}


def table(*columns, name=None):
    t = {'columns': [pk(), *columns]}
    if name:
        t['name'] = name
    return t


def line(source, attribute, cls=None):
    """The generated line defining `attribute`, for asserting on one statement.

    `cls` restricts the search to one class body — needed when two classes carry an
    attribute of the same name, as the two sides of a self-referential junction do.
    """
    current = None
    for text in source.splitlines():
        if text.startswith('class '):
            current = text[len('class '):].split('(')[0].strip(': ')
        if cls and current != cls:
            continue
        if text.strip().startswith(f"{attribute}:"):
            return text.strip()
    where = f" on class '{cls}'" if cls else ""
    raise AssertionError(f"no attribute '{attribute}'{where} in generated source:\n{source}")


# ── naming ────────────────────────────────────────────────────────────────────

def test_plain_foreign_key_names_the_relation_after_the_column(tmp_path, monkeypatch):
    """`book_id` -> `book` on Loan, and the plural table name back on Book."""
    source = generate(tmp_path, monkeypatch, {
        'Book': table(name='books'),
        'Loan': table({'name': 'book_id', 'foreign_key': {'target': 'Book.id'}}, name='loans'),
    })

    assert "book: Mapped['Book'] = relationship('Book', foreign_keys='Loan.book_id'" in line(source, 'book')
    assert "back_populates='loans'" in line(source, 'book')
    assert "loans: Mapped[List['Loan']] = relationship('Loan', foreign_keys='Loan.book_id'" in line(source, 'loans')
    assert "back_populates='book'" in line(source, 'loans')
    load(tmp_path, source)


def test_relation_name_cuts_at_the_last_underscore(tmp_path, monkeypatch):
    """Any suffix works — the rule is the cut, not a list of known suffixes."""
    source = generate(tmp_path, monkeypatch, {
        'Cliente': table(name='clienti'),
        'Ordine': table(
            {'name': 'codice_esterno_fk', 'foreign_key': {'target': 'Cliente.id'}}, name='ordini'),
    })

    assert 'codice_esterno' in source
    line(source, 'codice_esterno')  # relation exists under the cut name
    load(tmp_path, source)


def test_column_without_suffix_falls_back_to_the_target_table(tmp_path, monkeypatch):
    """Legacy names have nothing to cut; the target table names the relation."""
    source = generate(tmp_path, monkeypatch, {
        'Cliente': table(name='clienti'),
        'Ordine': table({'name': 'codcli', 'foreign_key': {'target': 'Cliente.id'}}, name='ordini'),
    })

    assert "cliente: Mapped['Cliente']" in line(source, 'cliente')
    load(tmp_path, source)


def test_explicit_names_win(tmp_path, monkeypatch):
    """`relation:`/`backref:` are the escape hatch for names worth choosing."""
    source = generate(tmp_path, monkeypatch, {
        'Book': table(name='books'),
        'Loan': table({'name': 'book_id', 'foreign_key': {
            'target': 'Book.id', 'relation': 'volume', 'backref': 'prestiti'}}, name='loans'),
    })

    assert "back_populates='prestiti'" in line(source, 'volume')
    assert "back_populates='volume'" in line(source, 'prestiti')
    assert 'ForeignKey' in source and 'relation=' not in source  # never a ForeignKey kwarg
    load(tmp_path, source)


# ── self reference ────────────────────────────────────────────────────────────

def test_self_reference_emits_remote_side(tmp_path, monkeypatch):
    """Both sides are the same class: only remote_side says which one is the 'one'."""
    source = generate(tmp_path, monkeypatch, {
        'Partner': table({'name': 'parent_id', 'nullable': True,
                          'foreign_key': {'target': 'Partner.id'}}, name='partners'),
    })

    assert "remote_side='Partner.id'" in line(source, 'parent')
    assert "remote_side" not in line(source, 'partners')  # the collection side must not have it
    load(tmp_path, source)


def partner_tables(first_backref=None, second_backref=None):
    """Partner with parent_id and merged_into_id, backrefs named or not."""
    parent = {'target': 'Partner.id'}
    merged = {'target': 'Partner.id'}
    if first_backref:
        parent['backref'] = first_backref
    if second_backref:
        merged['backref'] = second_backref
    return {
        'Partner': table(
            {'name': 'name', 'type': 'String', 'length': 80},
            {'name': 'parent_id', 'nullable': True, 'foreign_key': parent},
            {'name': 'merged_into_id', 'nullable': True, 'foreign_key': merged},
            name='partners'),
    }


def test_two_self_references_stay_distinct(tmp_path, monkeypatch):
    """The case that could not be generated at all: parent_id + merged_into_id."""
    source = generate(tmp_path, monkeypatch,
                      partner_tables('children', 'duplicates'))

    # Two forward attributes named from their columns, two named reverse collections
    assert "foreign_keys='Partner.parent_id'" in line(source, 'parent')
    assert "foreign_keys='Partner.merged_into_id'" in line(source, 'merged_into')
    assert "back_populates='parent'" in line(source, 'children')
    assert "back_populates='merged_into'" in line(source, 'duplicates')

    module = load(tmp_path, source)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        head = module.Partner(name='Rossi SRL')
        branch = module.Partner(name='Rossi SRL - sede', parent=head)
        dup = module.Partner(name='Rossi Srl', merged_into=head)
        session.add_all([head, branch, dup])
        session.flush()

        assert branch.parent.name == 'Rossi SRL'
        assert [p.name for p in head.children] == ['Rossi SRL - sede']
        assert [p.name for p in head.duplicates] == ['Rossi Srl']


def test_the_second_reverse_collection_must_be_named(tmp_path, monkeypatch):
    """Both reverse sides default to `partners`; nothing is renamed to make room.

    Suffixing them automatically would be the tempting fix, and it is the one thing
    that must not happen: the first foreign key may belong to another plugin, whose
    code would lose the attribute it declared without touching anything.
    """
    with pytest.raises(ValueError, match="is claimed by"):
        generate(tmp_path, monkeypatch, partner_tables())

    # Naming one of the two is enough — the other keeps the default
    source = generate(tmp_path, monkeypatch, partner_tables(second_backref='duplicates'))
    line(source, 'partners')
    line(source, 'duplicates')
    load(tmp_path, source)


def test_soft_self_reference_spells_out_the_join(tmp_path, monkeypatch):
    """No constraint to infer from, and no way to guess the direction: both needed."""
    source = generate(tmp_path, monkeypatch, {
        'Partner': table({'name': 'parent_id', 'nullable': True,
                          'foreign_key': {'target': 'Partner.id', 'constraint': False}},
                         name='partners'),
    })

    assert 'ForeignKey' not in line(source, 'parent_id')  # soft: no DB-level constraint
    assert "primaryjoin='Partner.parent_id == Partner.id'" in line(source, 'parent')
    assert "remote_side='Partner.id'" in line(source, 'parent')
    load(tmp_path, source)


# ── several paths between the same two tables ─────────────────────────────────

def test_two_foreign_keys_to_the_same_target(tmp_path, monkeypatch):
    """`ship_to_id`/`bill_to_id`: the forward names cost nothing, the reverse ones do.

    The columns differ, so the two attributes an application actually uses are
    generated with no declaration at all. Only the collections on Partner collide.
    """
    source = generate(tmp_path, monkeypatch, {
        'Partner': table(name='partners'),
        'Order': table(
            {'name': 'ship_to_id', 'foreign_key': {'target': 'Partner.id'}},
            {'name': 'bill_to_id', 'foreign_key': {
                'target': 'Partner.id', 'backref': 'billed_orders'}},
            name='orders'),
    })

    assert "foreign_keys='Order.ship_to_id'" in line(source, 'ship_to')
    assert "foreign_keys='Order.bill_to_id'" in line(source, 'bill_to')
    assert "back_populates='ship_to'" in line(source, 'orders')
    assert "back_populates='bill_to'" in line(source, 'billed_orders')
    load(tmp_path, source)


def test_foreign_keys_in_opposite_directions(tmp_path, monkeypatch):
    """Two FK paths between the same tables, one per table.

    Nothing here is ambiguous *within* a table, which is why `foreign_keys` is
    emitted unconditionally: without it SQLAlchemy refuses the join on relations
    neither table declared as special.
    """
    source = generate(tmp_path, monkeypatch, {
        'Partner': table({'name': 'default_order_id', 'nullable': True,
                          'foreign_key': {'target': 'Order.id'}}, name='partners'),
        'Order': table({'name': 'partner_id', 'nullable': True,
                        'foreign_key': {'target': 'Partner.id'}}, name='orders'),
    })

    load(tmp_path, source)  # the assertion is that configure_mappers() succeeds


# ── refusals ──────────────────────────────────────────────────────────────────

def test_relation_clashing_with_a_column_is_refused(tmp_path, monkeypatch):
    """A relationship overwriting a column in the class body must never be silent."""
    with pytest.raises(ValueError, match="both a column and"):
        generate(tmp_path, monkeypatch, {
            'Partner': table(name='partners'),
            'Order': table(
                {'name': 'partner', 'type': 'String', 'length': 20},
                {'name': 'partner_id', 'foreign_key': {'target': 'Partner.id'}},
                name='orders'),
        })


def test_two_relations_claiming_one_name_are_refused(tmp_path, monkeypatch):
    """`payment_primary` and `payment_secondary` both cut down to `payment`."""
    with pytest.raises(ValueError, match="is claimed by"):
        generate(tmp_path, monkeypatch, {
            'Payment': table(name='payments'),
            'Order': table(
                {'name': 'payment_primary', 'foreign_key': {'target': 'Payment.id'}},
                {'name': 'payment_secondary', 'foreign_key': {'target': 'Payment.id'}},
                name='orders'),
        })


def test_the_refusal_can_be_answered_with_explicit_names(tmp_path, monkeypatch):
    """The error is an instruction, so the instruction has to work."""
    source = generate(tmp_path, monkeypatch, {
        'Payment': table(name='payments'),
        'Order': table(
            {'name': 'payment_primary', 'foreign_key': {
                'target': 'Payment.id', 'relation': 'primary_payment'}},
            {'name': 'payment_secondary', 'foreign_key': {
                'target': 'Payment.id', 'relation': 'secondary_payment',
                'backref': 'orders_secondary'}},
            name='orders'),
    })

    line(source, 'primary_payment')
    line(source, 'secondary_payment')
    load(tmp_path, source)


def test_a_relationship_may_not_shadow_an_inherited_method(tmp_path, monkeypatch):
    """Generated attributes sit in the subclass body, so they win over the base class.

    A relationship taking the name of a plugin method would remove the method with
    no diagnostic at all — the model still maps, the behaviour is simply gone.
    """
    with pytest.raises(ValueError, match="would shadow"):
        generate(tmp_path, monkeypatch, {
            'Author': table(name='authors'),
            'Book': table({'name': 'author_id', 'foreign_key': {'target': 'Author.id'}},
                          name='books'),
        }, source='class Book:\n    def author(self):\n        return "shadowed"\n')


# ── many-to-many ──────────────────────────────────────────────────────────────

def junction(name, other='Author', column='author_id', **targets):
    """A junction between Book and `other`, with optional explicit names."""
    return {
        'name': name,
        'columns': [{'name': 'notes', 'type': 'String', 'length': 40, 'nullable': True}],
        'many_to_many': {
            'target1': {'table': 'Book.id', 'column': 'book_id', **targets.get('target1', {})},
            'target2': {'table': f'{other}.id', 'column': column, **targets.get('target2', {})},
        },
    }


def test_many_to_many_keeps_the_names_it_had(tmp_path, monkeypatch):
    """A single junction must generate exactly what it generated before."""
    source = generate(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors'),
    })

    assert "back_populates='author_m2m'" in line(source, 'book')       # on the junction
    assert "back_populates='book_m2m'" in line(source, 'author')       # on the junction
    assert "relationship('BookAuthor'" in line(source, 'author_m2m')   # on Book
    assert "secondary='books_authors'" in line(source, 'authors')      # on Book
    assert "back_populates='books'" in line(source, 'authors')
    load(tmp_path, source)


def test_a_junction_alone_generates_an_importable_model(tmp_path, monkeypatch):
    """The m2m branch used to get `relationship`/`List`/`ForeignKey` only as a side
    effect of some plain foreign key elsewhere in the model."""
    source = generate(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors'),
    })

    assert 'relationship' in source.split('class ')[0]   # imported, not just used
    assert 'ForeignKey' in source.split('class ')[0]
    assert 'from typing import List' in source
    load(tmp_path, source)


def test_two_junctions_on_one_pair_are_refused(tmp_path, monkeypatch):
    """Authors and reviewers over the same two tables: six names claimed twice."""
    with pytest.raises(ValueError, match="is claimed by"):
        generate(tmp_path, monkeypatch, {
            'Author': table(name='authors'),
            'Book': table(name='books'),
            'BookAuthor': junction('books_authors'),
            'BookReviewer': junction('books_reviewers'),
        })


def test_a_second_junction_names_itself(tmp_path, monkeypatch):
    """…and the first one is left alone: only the newcomer declares."""
    source = generate(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors'),
        'BookReviewer': junction(
            'books_reviewers',
            target1={'collection': 'reviewers', 'backref': 'review_rows'},
            target2={'collection': 'reviewed_books', 'backref': 'review_rows'}),
    })

    line(source, 'authors')      # untouched
    line(source, 'author_m2m')   # untouched
    line(source, 'reviewers')
    line(source, 'reviewed_books')
    load(tmp_path, source)


def test_a_junction_colliding_with_a_foreign_key_is_refused(tmp_path, monkeypatch):
    """The silent one: `Author.books` claimed by the m2m and by a plain FK.

    Both were generated, the second overwrote the first, and the model mapped —
    `Author.books` answered with the books of which the author was the main one.
    """
    with pytest.raises(ValueError, match="is claimed by"):
        generate(tmp_path, monkeypatch, {
            'Author': table(name='authors'),
            'Book': table({'name': 'main_author_id', 'nullable': True,
                           'foreign_key': {'target': 'Author.id'}}, name='books'),
            'BookAuthor': junction('books_authors'),
        })


def test_self_referential_junction(tmp_path, monkeypatch):
    """Partners linked to partners: not expressible at all before.

    The two attributes on the junction come from its columns, so they are distinct
    without being declared; the four on Partner have to be named, and the joins are
    stated because there is nothing to infer them from.
    """
    source = generate(tmp_path, monkeypatch, {
        'Partner': table({'name': 'name', 'type': 'String', 'length': 80}, name='partners'),
        'PartnerLink': {
            'name': 'partner_links',
            'columns': [{'name': 'kind', 'type': 'String', 'length': 20, 'nullable': True}],
            'many_to_many': {
                'target1': {'table': 'Partner.id', 'column': 'partner_id',
                            'collection': 'related', 'backref': 'related_rows'},
                'target2': {'table': 'Partner.id', 'column': 'related_id',
                            'collection': 'related_by', 'backref': 'related_by_rows'},
            },
        },
    })

    # On the junction, the two scalars come from the two columns and stay distinct
    assert "foreign_keys='PartnerLink.partner_id'" in line(source, 'partner', 'PartnerLink')
    assert "foreign_keys='PartnerLink.related_id'" in line(source, 'related', 'PartnerLink')
    # On Partner, the rows collections and the two joins of the shortcut
    assert "foreign_keys='PartnerLink.partner_id'" in line(source, 'related_rows', 'Partner')
    assert "foreign_keys='PartnerLink.related_id'" in line(source, 'related_by_rows', 'Partner')
    assert "primaryjoin='Partner.id == PartnerLink.partner_id'" in line(source, 'related', 'Partner')
    assert "secondaryjoin='Partner.id == PartnerLink.related_id'" in line(source, 'related', 'Partner')

    module = load(tmp_path, source)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        alfa, beta = module.Partner(name='Alfa'), module.Partner(name='Beta')
        session.add_all([alfa, beta])
        session.flush()
        session.add(module.PartnerLink(partner=alfa, related=beta, kind='fornitore'))
        session.flush()
        session.expire_all()

        assert [p.name for p in alfa.related] == ['Beta']
        assert [p.name for p in beta.related_by] == ['Alfa']
        assert [(link.related.name, link.kind) for link in alfa.related_rows] == [('Beta', 'fornitore')]


def test_self_referential_junction_must_name_its_sides(tmp_path, monkeypatch):
    """Without names, all four attributes on Partner would be two pairs of twins."""
    with pytest.raises(ValueError, match="is claimed by"):
        generate(tmp_path, monkeypatch, {
            'Partner': table(name='partners'),
            'PartnerLink': {
                'name': 'partner_links',
                'columns': [],
                'many_to_many': {
                    'target1': {'table': 'Partner.id', 'column': 'partner_id'},
                    'target2': {'table': 'Partner.id', 'column': 'related_id'},
                },
            },
        })


# ── the columns a junction is made of ─────────────────────────────────────────
#
# `many_to_many:` is sugar: the two columns that reach the targets and the key of
# the junction itself are written into the table definition (db._calc_junctions),
# before columns are resolved. They used to be written straight into the
# generated model instead, which left the junction with columns SQLAlchemy knew
# about and the schema layer did not — no addressable key, and an auto-form with
# the note but not the author.

def test_a_junction_is_a_table_like_the_others(tmp_path, monkeypatch):
    """Key, two foreign keys and a note, all of them ordinary columns."""
    db = build(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors'),
    })

    schema = db.get_table_schema()['BookAuthor']
    assert schema['pk_fields'] == ['id']
    # The two columns the junction exists for come before what it carries
    assert [col['name'] for col in schema['columns']] == ['id', 'book_id', 'author_id', 'notes']
    assert schema['columns'][1]['foreign_key'] == {'target': 'Book', 'field': 'id'}
    assert schema['columns'][2]['foreign_key'] == {'target': 'Author', 'field': 'id'}


def test_a_junction_row_can_be_reached_by_its_key(tmp_path, monkeypatch):
    """Which is the point: a row of it can be opened, updated and deleted."""
    source = generate(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors'),
    })
    module = load(tmp_path, source)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        book, author = module.Book(), module.Author()
        session.add_all([book, author])
        session.flush()
        link = module.BookAuthor(book_id=book.id, author_id=author.id, notes='curatore')
        session.add(link)
        session.flush()

        assert link.id is not None
        found = session.get(module.BookAuthor, link.id)
        found.notes = 'traduttore'
        session.flush()
        assert session.get(module.BookAuthor, link.id).notes == 'traduttore'


def test_the_pair_stays_unique(tmp_path, monkeypatch):
    """What the composite key used to guarantee is now an index that says so."""
    source = generate(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors'),
    })
    module = load(tmp_path, source)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        book, author = module.Book(), module.Author()
        session.add_all([book, author])
        session.flush()
        session.add(module.BookAuthor(book_id=book.id, author_id=author.id))
        session.flush()
        session.add(module.BookAuthor(book_id=book.id, author_id=author.id))
        with pytest.raises(IntegrityError):
            session.flush()


def test_a_junction_may_declare_no_columns_at_all(tmp_path, monkeypatch):
    """`columns:` enriches the relation; without it the junction is still a table.

    It used to raise KeyError while the plugins were loading.
    """
    db = build(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': {
            'name': 'books_authors',
            'many_to_many': {
                'target1': {'table': 'Book.id', 'column': 'book_id'},
                'target2': {'table': 'Author.id', 'column': 'author_id'},
            },
        },
    })

    schema = db.get_table_schema()['BookAuthor']
    assert schema['pk_fields'] == ['id']
    assert [col['name'] for col in schema['columns']] == ['id', 'book_id', 'author_id']


def test_a_junction_that_declares_a_key_keeps_it(tmp_path, monkeypatch):
    """The escape for a table whose key is not ours to choose — a legacy one."""
    db = build(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': {
            'name': 'books_authors',
            'columns': [{'name': 'ba_code', 'type': 'String', 'length': 8, 'primary_key': True}],
            'many_to_many': {
                'target1': {'table': 'Book.id', 'column': 'book_id'},
                'target2': {'table': 'Author.id', 'column': 'author_id'},
            },
        },
    })

    schema = db.get_table_schema()['BookAuthor']
    assert schema['pk_fields'] == ['ba_code']
    assert [col['name'] for col in schema['columns']] == ['ba_code', 'book_id', 'author_id']


def test_the_generated_key_takes_the_name_the_config_asks_for(tmp_path, monkeypatch):
    """`schema.pk_name` decides what the framework writes, never what it reads."""
    db = build(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors'),
    }, config={'schema': {'pk_name': 'oid'}})

    assert db.get_table_schema()['BookAuthor']['pk_fields'] == ['oid']


def test_a_column_named_like_the_key_but_not_the_key_is_refused(tmp_path, monkeypatch):
    """Silently it would collide with the generated one, two columns down."""
    with pytest.raises(ValueError, match="not a primary key"):
        build(tmp_path, monkeypatch, {
            'Author': table(name='authors'),
            'Book': table(name='books'),
            'BookAuthor': {
                'name': 'books_authors',
                'columns': [{'name': 'id', 'type': 'String', 'length': 8}],
                'many_to_many': {
                    'target1': {'table': 'Book.id', 'column': 'book_id'},
                    'target2': {'table': 'Author.id', 'column': 'author_id'},
                },
            },
        })


def test_the_target_column_takes_the_base_type_of_the_key_it_points_at(tmp_path, monkeypatch):
    """A key type carries `primary_key` — copying it would make these keys again."""
    source = generate(tmp_path, monkeypatch, {
        'Author': {'name': 'authors',
                   'columns': [{'name': 'code', 'type': 'String', 'length': 8,
                                'primary_key': True}]},
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors', column='author_code',
                               target2={'table': 'Author.code'}),
    })

    author_fk = line(source, 'author_code', 'BookAuthor')
    assert 'String(length=8)' in author_fk
    assert 'primary_key' not in author_fk
    assert "ForeignKey('authors.code')" in author_fk
    load(tmp_path, source)


# ── ownership ─────────────────────────────────────────────────────────────────
#
# `owned: true` says the row is a part of its parent and does not outlive it. It
# becomes an ORM cascade and never DDL: that way it reads the same on a soft key,
# it survives dialects that refuse the constraint (SQL Server rejects a
# self-referential ON DELETE CASCADE outright), and it asks nothing of a database
# already installed. See PLUGIN_MODEL § 4.5.

def owned_fk(target='Book.id', **extra):
    return {'name': 'book_id', 'type': 'Integer',
            'foreign_key': {'target': target, 'owned': True, **extra}}


def test_owned_puts_the_cascade_on_the_parent_and_not_on_the_child(tmp_path, monkeypatch):
    """It is the parent that owns the rows: the child's scalar must stay clean."""
    source = generate(tmp_path, monkeypatch, {
        'Book': table(name='books'),
        'Chapter': table(owned_fk(), name='chapters'),
    })

    assert "cascade='all, delete-orphan'" in line(source, 'chapters', 'Book')
    assert 'cascade' not in line(source, 'book', 'Chapter')
    load(tmp_path, source)


def test_a_foreign_key_that_says_nothing_carries_no_cascade(tmp_path, monkeypatch):
    source = generate(tmp_path, monkeypatch, {
        'Book': table(name='books'),
        'Loan': table({'name': 'book_id', 'type': 'Integer',
                       'foreign_key': {'target': 'Book.id'}}, name='loans'),
    })

    assert 'cascade' not in line(source, 'loans', 'Book')
    load(tmp_path, source)


def test_owned_is_a_kitebase_hint_and_never_reaches_ForeignKey(tmp_path, monkeypatch):
    """Forwarded as a kwarg it would be a TypeError at import of the model."""
    source = generate(tmp_path, monkeypatch, {
        'Book': table(name='books'),
        'Chapter': table(owned_fk(), name='chapters'),
    })

    assert "ForeignKey('books.id')" in line(source, 'book_id', 'Chapter')
    load(tmp_path, source)


def test_a_soft_key_can_be_owned_too(tmp_path, monkeypatch):
    """The point of putting this in the ORM: no constraint, same behaviour."""
    source = generate(tmp_path, monkeypatch, {
        'Book': table(name='books'),
        'Chapter': table(owned_fk(constraint=False), name='chapters'),
    })

    chapters = line(source, 'chapters', 'Book')
    assert "cascade='all, delete-orphan'" in chapters
    assert 'primaryjoin' in chapters
    assert 'ForeignKey' not in line(source, 'book_id', 'Chapter')
    load(tmp_path, source)


def test_a_self_reference_can_be_owned(tmp_path, monkeypatch):
    """The case a DDL cascade could not express on every engine."""
    source = generate(tmp_path, monkeypatch, {
        'Partner': table({'name': 'parent_id', 'type': 'Integer',
                          'foreign_key': {'target': 'Partner.id', 'owned': True}},
                         name='partners'),
    })

    assert "cascade='all, delete-orphan'" in line(source, 'partners', 'Partner')
    load(tmp_path, source)


def test_a_junction_is_owned_by_both_its_ends_without_saying_so(tmp_path, monkeypatch):
    """A junction row means nothing without either end, so it is the default."""
    source = generate(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors'),
    })

    assert "cascade='all, delete-orphan'" in line(source, 'author_m2m', 'Book')
    assert "cascade='all, delete-orphan'" in line(source, 'book_m2m', 'Author')
    # Never on the shortcut: it is viewonly, and SQLAlchemy refuses delete-orphan
    # on a relationship with `secondary` unless it is told there is a single parent.
    assert 'cascade' not in line(source, 'authors', 'Book')
    load(tmp_path, source)


def test_a_junction_target_may_derogate(tmp_path, monkeypatch):
    """For the junction that is really a historical record of one of its ends."""
    source = generate(tmp_path, monkeypatch, {
        'Author': table(name='authors'),
        'Book': table(name='books'),
        'BookAuthor': junction('books_authors', target2={'owned': False}),
    })

    assert "cascade='all, delete-orphan'" in line(source, 'author_m2m', 'Book')
    assert 'cascade' not in line(source, 'book_m2m', 'Author')
    load(tmp_path, source)
