"""Golden / characterization tests for PluginsManager's config-merge machinery:
merge_dicts / _recursive_merge / _merge_lists / _detect_identity_key.

This is the RICH merge used to compose plugin YAML: it returns a new dict, tracks
key provenance via `$plugin` tags and a history map, and merges lists with
identity-aware semantics ($remove / $after / $before) — distinct from the simple
utils.deep_merge (see test_deep_merge.py).

These pin the *current* behaviour (quirks included) so refactors are safe. A
failure means the merge semantics changed — decide intentionally.
"""
import pytest

from kitebase.plugins import PluginsManager


@pytest.fixture
def pm():
    return PluginsManager()


# --------------------------------------------------------------------------- #
# _detect_identity_key
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("items, expected", [
    ([{"id": 1}], "id"),
    ([{"name": "n"}], "name"),
    ([{"field": "f"}], "field"),
    ([{"group": "g"}], "group"),
    ([{"id": 1, "name": "n"}], "id"),          # priority: id before name
    ([{"name": "n", "field": "f"}], "name"),   # priority: name before field
    ([{"foo": 1}], None),                       # dict without a recognised key
    (["a", "b"], None),                         # pure scalar list
    ([], None),                                 # empty list
    ([42, {"id": 1}], "id"),                    # first *dict* item decides
])
def test_detect_identity_key(pm, items, expected):
    assert pm._detect_identity_key(items) == expected


# --------------------------------------------------------------------------- #
# _merge_lists — plain (no identity key) → append with equality-dedup
# --------------------------------------------------------------------------- #

def test_plain_string_list_dedup(pm):
    assert pm._merge_lists(["x", "y"], ["y", "z"], "p", "k") == ["x", "y", "z"]


def test_plain_int_list_dedup(pm):
    assert pm._merge_lists([1, 2], [2, 3], "p", "k") == [1, 2, 3]


def test_plain_unkeyed_dict_list_dedup_by_equality(pm):
    # No identity key → plain path; {'foo':1} deduped by full equality, no $plugin added.
    out = pm._merge_lists([{"foo": 1}], [{"foo": 1}, {"bar": 2}], "p", "k")
    assert out == [{"foo": 1}, {"bar": 2}]


# --------------------------------------------------------------------------- #
# _merge_lists — smart (identity key present)
# --------------------------------------------------------------------------- #

def test_smart_same_identity_overwrites_props_and_tags_plugin(pm):
    out = pm._merge_lists([{"id": "a", "v": 1}], [{"id": "a", "v": 9}], "p2", "k")
    assert out == [{"id": "a", "v": 9, "$plugin": "p2"}]


def test_smart_new_identity_appends_and_base_untouched(pm):
    # Untouched base items are NOT re-tagged; only the incoming item gets $plugin.
    out = pm._merge_lists([{"id": "a", "v": 1}], [{"id": "b", "v": 2}], "p2", "k")
    assert out == [{"id": "a", "v": 1}, {"id": "b", "v": 2, "$plugin": "p2"}]


def test_smart_remove_drops_item(pm):
    out = pm._merge_lists(
        [{"id": "a"}, {"id": "b"}], [{"id": "a", "$remove": True}], "p2", "k")
    assert out == [{"id": "b"}]


def test_smart_after_inserts_following_anchor(pm):
    out = pm._merge_lists(
        [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        [{"id": "x", "$after": "a"}], "p", "k")
    assert [i["id"] for i in out] == ["a", "x", "b", "c"]
    assert out[1] == {"id": "x", "$plugin": "p"}   # directive stripped, plugin tagged


def test_smart_before_inserts_preceding_anchor(pm):
    out = pm._merge_lists(
        [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        [{"id": "y", "$before": "b"}], "p", "k")
    assert [i["id"] for i in out] == ["a", "y", "b", "c"]


def test_smart_after_takes_precedence_over_before(pm):
    out = pm._merge_lists(
        [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        [{"id": "z", "$after": "a", "$before": "c"}], "p", "k")
    assert [i["id"] for i in out] == ["a", "z", "b", "c"]   # $after wins


def test_smart_missing_anchor_appends_at_end(pm):
    out = pm._merge_lists(
        [{"id": "a"}], [{"id": "x", "$after": "nope"}], "p", "k")
    assert [i["id"] for i in out] == ["a", "x"]


def test_smart_merge_by_name_when_no_id(pm):
    out = pm._merge_lists([{"name": "a", "v": 1}], [{"name": "a", "v": 2}], "p", "k")
    assert out == [{"name": "a", "v": 2, "$plugin": "p"}]


# --------------------------------------------------------------------------- #
# merge_dicts / _recursive_merge — provenance & structure
# --------------------------------------------------------------------------- #

def test_new_scalar_key_has_no_plugin_tag(pm):
    assert pm.merge_dicts({"a": 1}, "p1") == {"a": 1}


def test_new_dict_key_gets_plugin_tag(pm):
    assert pm.merge_dicts({"d": {"x": 1}}, "p1") == {"d": {"x": 1, "$plugin": "p1"}}


def test_provenance_stays_with_original_definer(pm):
    pm.merge_dicts({"d": {"x": 1}}, "p1")
    data = pm.merge_dicts({"d": {"y": 2}}, "p2")
    assert data == {"d": {"x": 1, "$plugin": "p1", "y": 2}}   # $plugin stays p1


def test_overlapping_scalar_last_writer_wins(pm):
    pm.merge_dicts({"a": 1}, "p1")
    assert pm.merge_dicts({"a": 2}, "p2") == {"a": 2}


def test_incompatible_types_raise_typeerror(pm):
    pm.merge_dicts({"a": 1}, "p1")
    with pytest.raises(TypeError):
        pm.merge_dicts({"a": [1]}, "p2")


def test_new_dict_list_key_tags_each_item(pm):
    data = pm.merge_dicts({"items": [{"id": "a"}]}, "p1")
    assert data["items"] == [{"id": "a", "$plugin": "p1"}]


def test_list_merge_through_merge_dicts(pm):
    pm.merge_dicts({"items": [{"id": "a", "v": 1}]}, "p1")
    data = pm.merge_dicts({"items": [{"id": "a", "v": 9}, {"id": "b", "v": 2}]}, "p2")
    assert data["items"] == [
        {"id": "a", "v": 9, "$plugin": "p2"},
        {"id": "b", "v": 2, "$plugin": "p2"},
    ]


# --------------------------------------------------------------------------- #
# history tracking
# --------------------------------------------------------------------------- #

def test_history_tracks_definers_per_key_path(pm):
    pm.merge_dicts({"d": {"x": 1}}, "p1")
    pm.merge_dicts({"d": {"y": 2}}, "p2")
    assert pm.history["d"] == ["p1", "p2"]
    assert pm.history["d.x"] == ["p1"]
    assert pm.history["d.y"] == ["p2"]


# --------------------------------------------------------------------------- #
# custom merge handlers
# --------------------------------------------------------------------------- #

def test_exact_merge_handler_overrides_list_merge(pm):
    pm.register_merge_handler("items", lambda base, new, plugin: ["CUSTOM"])
    pm.merge_dicts({"items": [{"id": "a"}]}, "p1")
    data = pm.merge_dicts({"items": [{"id": "b"}]}, "p2")
    assert data["items"] == ["CUSTOM"]


def test_wildcard_merge_handler_matches(pm):
    pm.register_merge_handler("tables.*.columns", lambda base, new, plugin: ["W"])
    pm.merge_dicts({"tables": {"Book": {"columns": [{"id": "a"}]}}}, "p1")
    data = pm.merge_dicts({"tables": {"Book": {"columns": [{"id": "b"}]}}}, "p2")
    assert data["tables"]["Book"]["columns"] == ["W"]


# --------------------------------------------------------------------------- #
# unkeyed lists — the contribution that cannot target anything
# --------------------------------------------------------------------------- #

def codes(pm, code):
    return [i for i in pm.issues if i['code'] == code]


def test_a_second_plugin_on_an_unkeyed_list_is_warned(pm):
    """Appending is what happens; saying so is what was missing."""
    pm.merge_dicts({"layout": [{"type": "section", "fields": ["a"]}]}, "base")
    pm.merge_dicts({"layout": [{"type": "section", "fields": ["b"]}]}, "ext")

    warnings = codes(pm, 'merge-unkeyed-list')
    assert len(warnings) == 1
    assert warnings[0]['path'] == 'layout' and warnings[0]['plugin'] == 'ext'


def test_declaring_an_unkeyed_list_alone_is_not_a_warning(pm):
    """One plugin, one list: there is nothing to target and nothing to say."""
    pm.merge_dicts({"layout": [{"type": "section"}]}, "base")

    assert codes(pm, 'merge-unkeyed-list') == []


def test_an_identified_list_is_not_warned(pm):
    pm.merge_dicts({"layout": [{"id": "main", "type": "section"}]}, "base")
    pm.merge_dicts({"layout": [{"id": "main", "label": "Main"}]}, "ext")

    assert codes(pm, 'merge-unkeyed-list') == []


# --------------------------------------------------------------------------- #
# nested lists — the merge no longer stops at the first one
# --------------------------------------------------------------------------- #

def test_a_list_inside_an_identified_item_is_merged_not_replaced(pm):
    """A form layout is lists all the way down; stopping at the first lost them."""
    pm.merge_dicts({"layout": [
        {"id": "main", "columns": [{"id": "left",
                                    "fields": [{"name": "title"}, {"name": "isbn"}]}]}]}, "base")
    data = pm.merge_dicts({"layout": [
        {"id": "main", "columns": [{"id": "left",
                                    "fields": [{"name": "title", "label": "Titolo"},
                                               {"name": "price"}]}]}]}, "ext")

    fields = data["layout"][0]["columns"][0]["fields"]
    assert [f["name"] for f in fields] == ["title", "isbn", "price"]
    assert fields[0]["label"] == "Titolo"


def test_a_nested_unkeyed_list_is_warned_with_its_full_path(pm):
    pm.merge_dicts({"layout": [{"id": "main", "rows": [{"a": 1}]}]}, "base")
    pm.merge_dicts({"layout": [{"id": "main", "rows": [{"b": 2}]}]}, "ext")

    paths = [i['path'] for i in pm.issues if i['code'] == 'merge-unkeyed-list']
    assert paths == ['layout[main].rows']


# --------------------------------------------------------------------------- #
# $replace — starting over, said out loud
# --------------------------------------------------------------------------- #

def test_replace_supersedes_a_list_instead_of_merging_it(pm):
    pm.merge_dicts({"form": {"layout": [{"id": "a"}, {"id": "b"}]}}, "base")
    data = pm.merge_dicts({"form": {"$replace": ["layout"], "layout": [{"id": "c"}]}}, "ext")

    assert data["form"]["layout"] == [{"id": "c"}]


def test_replace_works_on_a_scalar_list_too(pm):
    """`order_by: [price]` in a derived plugin used to mean `[title, price]`."""
    pm.merge_dicts({"source": {"order_by": ["title"]}}, "base")
    data = pm.merge_dicts({"source": {"$replace": ["order_by"], "order_by": ["price"]}}, "ext")

    assert data["source"]["order_by"] == ["price"]


def test_replace_never_reaches_the_consumer(pm):
    pm.merge_dicts({"form": {"layout": [{"id": "a"}]}}, "base")
    data = pm.merge_dicts({"form": {"$replace": ["layout"], "layout": []}}, "ext")

    assert "$replace" not in data["form"]


def test_replace_wants_a_list_of_names(pm):
    with pytest.raises(TypeError, match=r'\$replace'):
        pm.merge_dicts({"form": {"$replace": "layout", "layout": []}}, "ext")


# --------------------------------------------------------------------------- #
# $remove on a dict value — the menu entry a derived app drops
# --------------------------------------------------------------------------- #

def test_remove_drops_a_dict_entry(pm):
    pm.merge_dicts({"menu_items": {"books": {"label": "Books"},
                                   "authors": {"label": "Authors"}}}, "base")
    data = pm.merge_dicts({"menu_items": {"books": {"$remove": True}}}, "ext")

    assert [k for k in data["menu_items"] if not k.startswith('$')] == ["authors"]


def test_remove_does_not_travel_as_data(pm):
    """It used to be an ordinary key: shipped to the client, and inert."""
    data = pm.merge_dicts({"menu_items": {"ghost": {"$remove": True}}}, "ext")

    assert "ghost" not in data["menu_items"]
    assert not any(k.startswith('$remove') for k in data["menu_items"])


def test_redefining_an_entry_still_merges(pm):
    pm.merge_dicts({"menu_items": {"books": {"label": "Books", "panel": "book_list"}}}, "base")
    data = pm.merge_dicts({"menu_items": {"books": {"panel": "book_form_myapp"}}}, "ext")

    assert data["menu_items"]["books"]["label"] == "Books"
    assert data["menu_items"]["books"]["panel"] == "book_form_myapp"


def test_an_unkeyed_item_survives_a_merge_into_its_list(pm):
    """`- filler:` belongs to nobody and is positional: it used to be dropped.

    A dict with no identity inside an otherwise-identified list was skipped when
    building the merge index, so the first contribution from a second plugin
    silently removed it — a form column losing its line break, with no warning.
    """
    pm.merge_dicts({"fields": [{"name": "title"}, {"filler": None}, {"name": "price"}]}, "base")
    data = pm.merge_dicts({"fields": [{"name": "price", "label": "Prezzo"}]}, "ext")

    assert data["fields"] == [
        {"name": "title", "$plugin": "base"},
        {"filler": None, "$plugin": "base"},
        {"name": "price", "label": "Prezzo", "$plugin": "ext"},
    ]


# --------------------------------------------------------------------------- #
# expand_shorthand — applied as a plugin's YAML is read, before any merge
# --------------------------------------------------------------------------- #

def test_a_bare_string_in_a_fields_list_is_the_field_of_that_name():
    from kitebase.plugins import expand_shorthand

    data = expand_shorthand({"fields": ["kind", {"name": "roles", "width": "25%"}]})

    assert data["fields"] == [{"name": "kind"}, {"name": "roles", "width": "25%"}]


def test_the_shorthand_reaches_every_depth_and_leaves_other_lists_alone():
    from kitebase.plugins import expand_shorthand

    data = expand_shorthand({
        "pages": {"p": {"layout": [{"columns": [{"fields": ["a", {"filler": None}]}]}]}},
        "order_by": ["name"],
        "columns": ["not a field list"],
    })

    column = data["pages"]["p"]["layout"][0]["columns"][0]
    assert column["fields"] == [{"name": "a"}, {"filler": None}]
    assert data["order_by"] == ["name"]
    assert data["columns"] == ["not a field list"]


def test_a_field_written_short_is_still_addressable_by_the_merge(pm):
    """The reason the expansion happens before the merge and not on the client.

    The merge indexes a list by the identity of its items; a bare string carries
    none, so a form written short would be appended to instead of refined.
    """
    from kitebase.plugins import expand_shorthand

    pm.merge_dicts(expand_shorthand({"fields": ["title", "price"]}), "base")
    data = pm.merge_dicts({"fields": [{"name": "language", "$after": "title"}]}, "ext")

    assert [f["name"] for f in data["fields"]] == ["title", "language", "price"]
