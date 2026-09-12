"""Code table lookup and input validation."""

from __future__ import annotations

import pytest

from vektis_mcp.agb.errors import InvalidInput
from vektis_mcp.codes import (
    load_codes,
    lookup_codes,
    side_codes,
    validate_kwalificaties,
    validate_zorgsoort,
)


def test_load_codes_is_cached() -> None:
    assert load_codes() is load_codes()
    assert set(load_codes()) == {"zorgverlener", "organisatie"}


def test_no_filters_lists_all_zorgsoorten_in_table_order() -> None:
    response = lookup_codes("zorgverlener", None, None)
    table = side_codes("zorgverlener")

    assert response.entity_kind == "zorgverlener"
    assert [m.code for m in response.results] == list(table)
    assert {m.kind for m in response.results} == {"zorgsoort"}
    assert all(m.parent_code is None for m in response.results)
    assert response.results[0].naam == table["01"]["naam"]


def test_zorgsoort_only_lists_its_kwalificaties() -> None:
    response = lookup_codes("zorgverlener", "01", None)

    assert [m.code for m in response.results] == list(side_codes("zorgverlener")["01"]["kwalificaties"])
    assert {m.kind for m in response.results} == {"kwalificatie"}
    assert {m.parent_code for m in response.results} == {"01"}


def test_leading_zeros_are_preserved_as_strings() -> None:
    codes = [m.code for m in lookup_codes("zorgverlener", "01", None).results]

    assert "0101" in codes
    assert all(isinstance(code, str) for code in codes)
    assert lookup_codes("zorgverlener", None, None).results[0].code == "01"


def test_query_only_searches_both_levels() -> None:
    results = lookup_codes("zorgverlener", None, "huisarts").results

    assert ("zorgsoort", "01", None) in [(m.kind, m.code, m.parent_code) for m in results]
    assert ("kwalificatie", "0101", "01") in [(m.kind, m.code, m.parent_code) for m in results]


def test_query_matching_is_casefolded_on_the_label() -> None:
    lower = lookup_codes("zorgverlener", None, "HUISARTS").results
    upper = lookup_codes("zorgverlener", None, "huisarts").results

    assert [m.code for m in lower] == [m.code for m in upper]


def test_query_matches_a_code_substring() -> None:
    results = lookup_codes("organisatie", None, "0205").results

    assert [(m.kind, m.code, m.parent_code) for m in results] == [("kwalificatie", "0205", "02")]


def test_query_is_trimmed() -> None:
    assert [m.code for m in lookup_codes("organisatie", None, "  0205  ").results] == ["0205"]


def test_blank_query_behaves_like_no_query() -> None:
    assert lookup_codes("zorgverlener", None, "   ").results == lookup_codes("zorgverlener", None, None).results


def test_query_with_zorgsoort_searches_only_that_parent() -> None:
    results = lookup_codes("zorgverlener", "01", "apotheek").results

    assert [(m.code, m.parent_code) for m in results] == [("0110", "01")]


def test_unmatched_query_returns_empty_results() -> None:
    assert lookup_codes("zorgverlener", None, "zzzznotacode").results == []
    assert lookup_codes("zorgverlener", "01", "zzzznotacode").results == []


def test_unknown_zorgsoort_is_an_input_error() -> None:
    with pytest.raises(InvalidInput, match="99"):
        lookup_codes("zorgverlener", "99", None)


def test_zorgsoort_known_only_on_the_other_side_is_an_input_error() -> None:
    assert "06" in side_codes("organisatie")
    assert "06" not in side_codes("zorgverlener")

    with pytest.raises(InvalidInput, match="zorgverlener"):
        lookup_codes("zorgverlener", "06", None)


def test_side_codes_rejects_an_unknown_side() -> None:
    with pytest.raises(InvalidInput, match="entity_kind"):
        side_codes("huisarts")  # type: ignore[arg-type]


def test_validate_zorgsoort_passes_known_codes_through() -> None:
    assert validate_zorgsoort("zorgverlener", "01") == "01"
    assert validate_zorgsoort("zorgverlener", None) is None


def test_validate_kwalificaties_empty_is_no_criterion() -> None:
    assert validate_kwalificaties("zorgverlener", None, None) == ()
    assert validate_kwalificaties("zorgverlener", None, []) == ()
    assert validate_kwalificaties("zorgverlener", "01", [" ", ""]) == ()


def test_validate_kwalificaties_trims_dedupes_and_keeps_order() -> None:
    assert validate_kwalificaties("zorgverlener", "01", [" 0110", "0101", "0110"]) == ("0110", "0101")


def test_validate_kwalificaties_accepts_any_parent_without_a_zorgsoort() -> None:
    assert validate_kwalificaties("zorgverlener", None, ["0101", "0200"]) == ("0101", "0200")


def test_validate_kwalificaties_rejects_a_code_from_another_zorgsoort() -> None:
    with pytest.raises(InvalidInput, match="zorgsoort 01"):
        validate_kwalificaties("zorgverlener", "01", ["0101", "0200"])


def test_validate_kwalificaties_rejects_a_code_from_the_other_side() -> None:
    assert "0100" in side_codes("organisatie")["01"]["kwalificaties"]
    assert "0100" not in side_codes("zorgverlener")["01"]["kwalificaties"]

    with pytest.raises(InvalidInput, match="0100"):
        validate_kwalificaties("zorgverlener", None, ["0100"])


def test_validate_kwalificaties_lists_every_offending_code() -> None:
    with pytest.raises(InvalidInput) as excinfo:
        validate_kwalificaties("zorgverlener", None, ["0101", "9998", "9999"])

    assert "9998" in str(excinfo.value)
    assert "9999" in str(excinfo.value)
    assert "0101" not in str(excinfo.value)


def test_validate_kwalificaties_rejects_an_unknown_zorgsoort() -> None:
    with pytest.raises(InvalidInput, match="99"):
        validate_kwalificaties("zorgverlener", "99", ["0101"])
