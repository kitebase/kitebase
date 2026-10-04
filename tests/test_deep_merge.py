"""Golden / characterization tests for kitebase.utils.deep_merge.

deep_merge is the SIMPLE recursive merge (dict-only): it mutates `a` in place,
returns None, recurses into nested dicts, and REPLACES everything else (scalars,
lists, type changes) with the value from `b`. It has NO identity-aware list
semantics — that lives in PluginsManager (see test_plugin_merge.py).

These tests pin current behaviour so future refactors are safe. If one fails,
the merge semantics changed — decide intentionally, don't just update the golden.
"""
from kitebase.utils import deep_merge


def test_returns_none_and_mutates_a_in_place():
    a = {"x": 1}
    ret = deep_merge(a, {"y": 2})
    assert ret is None                      # mutates in place, no return value
    assert a == {"x": 1, "y": 2}


def test_new_keys_are_added():
    a = {"x": 1}
    deep_merge(a, {"y": 2, "z": 3})
    assert a == {"x": 1, "y": 2, "z": 3}


def test_nested_dicts_merge_recursively():
    a = {"d": {"p": 1}}
    deep_merge(a, {"d": {"q": 2}})
    assert a == {"d": {"p": 1, "q": 2}}


def test_conflicting_scalar_b_wins():
    a = {"x": 1}
    deep_merge(a, {"x": 9})
    assert a == {"x": 9}


def test_equal_values_are_left_untouched():
    a = {"x": 1, "d": {"p": 1}}
    deep_merge(a, {"x": 1, "d": {"p": 1}})
    assert a == {"x": 1, "d": {"p": 1}}


def test_lists_are_replaced_wholesale_not_merged():
    a = {"items": [1, 2, 3]}
    deep_merge(a, {"items": [4]})
    assert a == {"items": [4]}              # NO list merge here — b replaces a


def test_type_change_dict_to_scalar_b_wins():
    a = {"x": {"p": 1}}
    deep_merge(a, {"x": 5})
    assert a == {"x": 5}


def test_type_change_scalar_to_dict_b_wins():
    a = {"x": 5}
    deep_merge(a, {"x": {"p": 1}})
    assert a == {"x": {"p": 1}}


def test_deeply_nested_merge():
    a = {"a": {"b": {"c": 1}}}
    deep_merge(a, {"a": {"b": {"d": 2}, "e": 3}})
    assert a == {"a": {"b": {"c": 1, "d": 2}, "e": 3}}


def test_new_key_shares_reference_with_b():
    # For a brand-new key, deep_merge assigns b's object directly (no deep copy):
    # a["k"] and b["k"] are the SAME object. This aliasing is load-bearing to know.
    b_val = {"p": 1}
    a = {}
    deep_merge(a, {"k": b_val})
    assert a["k"] is b_val                  # shared reference, not a copy


def test_empty_b_is_noop():
    a = {"x": 1}
    deep_merge(a, {})
    assert a == {"x": 1}
