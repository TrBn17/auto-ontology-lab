from __future__ import annotations

import re

from lab.table_select import exclusion_regex, select_tables


def _fk(child: str, parent: str) -> dict:
    return {"from": {"table": child}, "to": {"table": parent}}


PROFILE = {
    "tables": {
        "public.orders": {"row_count": 5},
        "public.customers": {"row_count": 0},
        "public.regions": {"row_count": 0},
        "public.unused": {"row_count": 0},
        "public.unused_child": {"row_count": 0},
    },
    "foreign_keys": [
        _fk("public.orders", "public.customers"),
        _fk("public.customers", "public.regions"),
        _fk("public.unused_child", "public.unused"),
    ],
}


def test_keeps_tables_with_data_and_every_parent_they_need() -> None:
    selection = select_tables(PROFILE)
    assert selection.with_data == {"public.orders"}
    assert selection.required_parents == {"public.customers", "public.regions"}
    assert selection.dropped == {"public.unused", "public.unused_child"}


def test_a_child_of_a_kept_table_is_not_kept_when_empty() -> None:
    profile = {
        "tables": {"public.a": {"row_count": 1}, "public.b": {"row_count": 0}},
        "foreign_keys": [_fk("public.b", "public.a")],
    }
    assert select_tables(profile).dropped == {"public.b"}


def test_foreign_key_cycles_terminate() -> None:
    profile = {
        "tables": {"public.a": {"row_count": 1}, "public.b": {"row_count": 0}},
        "foreign_keys": [_fk("public.a", "public.b"), _fk("public.b", "public.a")],
    }
    assert select_tables(profile).kept == {"public.a", "public.b"}


def test_regex_drops_exactly_the_unselected_tables() -> None:
    pattern = re.compile(exclusion_regex(select_tables(PROFILE)) or "")
    assert pattern.search("public.unused")
    assert not pattern.search("public.unused_extra")
    assert not pattern.search("public.orders")


def test_regex_keeps_the_user_exclusion_and_is_none_when_nothing_is_dropped() -> None:
    selection = select_tables({"tables": {"public.a": {"row_count": 1}}, "foreign_keys": []})
    assert exclusion_regex(selection) is None
    assert exclusion_regex(selection, "^tmp_") == "(?:^tmp_)"
