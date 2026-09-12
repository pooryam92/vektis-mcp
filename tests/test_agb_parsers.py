"""Tests for the pure AGB HTML parsers.

Fixtures are the synthetic reductions in `tests/fixtures/agb/`; the structures
they preserve are documented in `docs/agb-source-notes.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from vektis_mcp.agb.constants import RESULTS_URL
from vektis_mcp.agb.errors import ParseError
from vektis_mcp.agb.parsers import (
    DEFAULT_SECTIONS,
    classify_record_page,
    decode_vestiging_slug,
    encode_vestiging_slug,
    parse_record,
    parse_record_identity,
    parse_search,
)
from vektis_mcp.agb.slugs import code_from_record_url
from vektis_mcp.agb.types import RecordIdentity, SourcePage

FIXTURES = Path(__file__).parent / "fixtures" / "agb"
FETCHED_AT = datetime(2026, 9, 12, 14, 30, tzinfo=UTC)

ALL_SECTIONS = ["basisregistratie", "contact", "kwalificaties", "erkenningen", "relaties"]

DETAIL_URLS = {
    "record_zorgverlener.html": ("zorgverlener", "01999001"),
    "record_zorgverlener_ended.html": ("zorgverlener", "01999009"),
    "record_onderneming.html": ("onderneming", "01999100"),
    "record_vestiging.html": ("vestiging", "71999001"),
}


def html_page(html: str, *, url: str = RESULTS_URL, status_code: int = 200) -> SourcePage:
    """A SourcePage around literal HTML, with a fixed tz-aware fetch time."""
    return SourcePage(html=html, url=url, fetched_at=FETCHED_AT, status_code=status_code)


def fixture_page(name: str, *, url: str | None = None, status_code: int = 200) -> SourcePage:
    """A SourcePage built from a fixture file and a fixed tz-aware fetch time."""
    if url is None:
        if name in DETAIL_URLS:
            record_type, code = DETAIL_URLS[name]
            url = f"https://www.vektis.nl/agb-register/{record_type}-{code}"
        else:
            url = RESULTS_URL
    return html_page((FIXTURES / name).read_text(encoding="utf-8"), url=url, status_code=status_code)


def detail_page(name: str) -> tuple[SourcePage, str, str]:
    record_type, code = DETAIL_URLS[name]
    return fixture_page(name), record_type, code


def identity_of(name: str) -> tuple[SourcePage, RecordIdentity]:
    page, record_type, code = detail_page(name)
    return page, parse_record_identity(page, record_type, code)


def search_html(rows: str, *, heading: str | None = "1 Zoekresultaten") -> str:
    """A minimal but structurally faithful results page around `rows`."""
    head = f'<h2 class="h3 mt-4">{heading}</h2>' if heading is not None else ""
    return f"""<!DOCTYPE html><html><body><main>
    <section class="js-search-results" data-value="zorgverlener">
      <div class="visually-hidden" id="resultaten"></div>
      <div id="resultsContainer">
        {head}
        <div class="card-table-container">
          <table class="card-table table table-striped js-datatable">
            <caption class="visually-hidden">Resultaten</caption>
            <thead><tr><th>Type</th><th>Naam zorgverlener</th><th>AGB-code</th><th>Adres</th><th>Postcode</th><th>Plaats</th><th>Einddatum</th></tr></thead>
            <tbody class="card-table__body">{rows}</tbody>
          </table>
        </div>
      </div>
    </section></main></body></html>"""


def search_row(
    *,
    label: str = "Zorgverlener",
    href: str = "https://www.vektis.nl/agb-register/zorgverlener-01999001",
    code: str = "01999001",
    cells: int = 7,
) -> str:
    columns = [
        f'<td class="card-table__cell fs-5" data-search="{label}"><svg></svg></td>',
        f'<td class="card-table__cell card-table__cell--name">'
        + (f'<a href="{href}">A Testpersoon</a>' if href else "A Testpersoon")
        + "</td>",
        f'<td class="card-table__cell">{code}</td>',
        '<td class="card-table__cell">Teststraat 1</td>',
        '<td class="card-table__cell">1234AB  </td>',
        '<td class="card-table__cell card-table__cell--city">TESTSTAD</td>',
        '<td class="card-table__cell card-table__cell--date" data-order="">-</td>',
    ]
    return "<tr>" + "".join(columns[:cells]) + "</tr>"


# --- parse_search ----------------------------------------------------------


def test_search_zorgverlener_rows_and_count() -> None:
    parsed = parse_search(fixture_page("search_results_zorgverlener.html"))

    assert parsed.source_total == 4
    assert parsed.raw_row_count == 4
    assert len(parsed.rows) == 4
    assert parsed.is_empty is False
    assert parsed.cap_marker is False
    assert [row.agbcode for row in parsed.rows] == [
        "01999001",
        "01999002",
        "01999009",
        "01999010",
    ]
    assert {row.record_type for row in parsed.rows} == {"zorgverlener"}


def test_search_zorgverlener_first_row_every_field() -> None:
    row = parse_search(fixture_page("search_results_zorgverlener.html")).rows[0]

    assert row.record_type == "zorgverlener"
    assert row.naam == "A Testpersoon"
    assert row.agbcode == "01999001"
    # Individuals carry no address columns.
    assert row.adres == ""
    assert row.postcode == ""
    assert row.plaats == ""
    assert row.einddatum == ""
    assert row.source_url == "https://www.vektis.nl/agb-register/zorgverlener-01999001"


def test_search_zorgverlener_ended_rows_keep_iso_einddatum() -> None:
    rows = parse_search(fixture_page("search_results_zorgverlener.html")).rows

    assert [row.einddatum for row in rows] == ["", "", "2024-03-04", "2024-12-31"]


def test_search_organisatie_onderneming_row_every_field() -> None:
    rows = parse_search(fixture_page("search_results_organisatie.html")).rows
    row = rows[0]

    assert row.record_type == "onderneming"
    assert row.naam == "Praktijk Voorbeeld B.V."
    assert row.agbcode == "01999100"
    assert row.adres == "Teststraat 1"
    # Trailing whitespace from the source cell is normalized away.
    assert row.postcode == "1234AB"
    assert row.plaats == "TESTSTAD"
    assert row.einddatum == ""
    assert row.source_url == "https://www.vektis.nl/agb-register/onderneming-01999100"


def test_search_organisatie_vestiging_row_decodes_its_slug() -> None:
    parsed = parse_search(fixture_page("search_results_organisatie.html"))

    assert parsed.source_total == 4
    assert [row.record_type for row in parsed.rows] == [
        "onderneming",
        "vestiging",
        "onderneming",
        "vestiging",
    ]
    vestiging = parsed.rows[1]
    assert vestiging.record_type == "vestiging"
    assert vestiging.naam == "Praktijk Voorbeeld B.V."
    # The AGB-code column shows "-"; the code only exists in the URL slug.
    assert vestiging.agbcode == "71999001"
    assert vestiging.adres == "Teststraat 1"
    assert vestiging.postcode == "1234AB"
    assert vestiging.plaats == "TESTSTAD"
    assert vestiging.einddatum == ""
    assert (
        vestiging.source_url
        == "https://www.vektis.nl/agb-register/vestiging-4e7a45354f546b774d44453d"
    )
    assert parsed.rows[3].agbcode == "71999002"
    assert parsed.rows[3].einddatum == "2024-12-31"


def test_search_empty_page_is_recognized_not_guessed() -> None:
    parsed = parse_search(fixture_page("search_results_empty.html"))

    assert parsed.is_empty is True
    assert parsed.rows == []
    assert parsed.raw_row_count == 0
    assert parsed.source_total is None
    assert parsed.cap_marker is False


def test_search_capped_page_reports_marker_and_count() -> None:
    parsed = parse_search(fixture_page("search_results_capped.html"))

    assert parsed.cap_marker is True
    assert parsed.source_total == 800
    assert parsed.raw_row_count == 3
    assert parsed.is_empty is False


def test_search_count_accepts_thousands_separator() -> None:
    html = (FIXTURES / "search_results_capped.html").read_text(encoding="utf-8")

    with_dots = parse_search(html_page(html.replace("800 Zoekresultaten", "15.857 Zoekresultaten")))
    without = parse_search(html_page(html.replace("800 Zoekresultaten", "15857 Zoekresultaten")))

    assert with_dots.source_total == 15857
    assert without.source_total == 15857


def test_search_missing_heading_yields_null_total_but_keeps_rows() -> None:
    parsed = parse_search(html_page(search_html(search_row(), heading=None)))

    assert parsed.source_total is None
    assert parsed.raw_row_count == 1
    assert parsed.is_empty is False


@pytest.mark.parametrize(
    "name",
    ["search_form.html", "session_expired.html"],
)
def test_search_unrecognized_page_is_parse_error(name: str) -> None:
    with pytest.raises(ParseError):
        parse_search(fixture_page(name))


def test_search_random_html_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_search(html_page("<html><body><p>Hello</p></body></html>"))


def test_search_row_with_wrong_column_count_is_parse_error() -> None:
    with pytest.raises(ParseError, match="6 cells"):
        parse_search(html_page(search_html(search_row(cells=6))))


def test_search_row_without_detail_link_is_parse_error() -> None:
    with pytest.raises(ParseError, match="detail link"):
        parse_search(html_page(search_html(search_row(href=""))))


def test_search_row_with_undecodable_slug_is_parse_error() -> None:
    row = search_row(
        label="Vestiging",
        href="https://www.vektis.nl/agb-register/vestiging-nothexatall",
        code="-",
    )
    with pytest.raises(ParseError, match="AGB-code"):
        parse_search(html_page(search_html(row)))


def test_search_row_code_is_never_padded() -> None:
    with pytest.raises(ParseError, match="AGB-code"):
        parse_search(html_page(search_html(search_row(code="199900"))))


def test_search_row_code_contradicting_its_url_is_parse_error() -> None:
    # href addresses 01999001 while the column claims another code.
    with pytest.raises(ParseError, match="contradicts"):
        parse_search(html_page(search_html(search_row(code="01999002"))))


def test_search_row_einddatum_in_dutch_order_is_converted_to_iso() -> None:
    # Today the column renders ISO; tolerate the site's other date format too.
    row = search_row().replace('data-order="">-', 'data-order="1735603200">31-12-2024')

    assert parse_search(html_page(search_html(row))).rows[0].einddatum == "2024-12-31"


def test_search_row_with_unparseable_einddatum_is_parse_error() -> None:
    row = search_row().replace('data-order="">-', 'data-order="">31 december 2024')

    with pytest.raises(ParseError, match="einddatum"):
        parse_search(html_page(search_html(row)))


def test_search_row_type_contradicting_its_url_is_parse_error() -> None:
    row = search_row(
        label="Onderneming",
        href="https://www.vektis.nl/agb-register/zorgverlener-01999001",
    )
    with pytest.raises(ParseError, match="contradicts"):
        parse_search(html_page(search_html(row)))


# --- vestiging slugs -------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "slug"),
    [
        ("71999001", "4e7a45354f546b774d44453d"),
        ("71999002", "4e7a45354f546b774d44493d"),
        ("71004592", "4e7a45774d4451314f54493d"),
        ("01999001", "4d4445354f546b774d44453d"),
    ],
)
def test_slug_round_trip(code: str, slug: str) -> None:
    assert decode_vestiging_slug(slug) == code
    assert encode_vestiging_slug(code) == slug


def test_slug_round_trip_preserves_leading_zeros() -> None:
    slug = encode_vestiging_slug("00000001")

    assert decode_vestiging_slug(slug) == "00000001"


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "zzzz",
        "4e7a45354f546b774d44453",  # odd number of hex digits
        "4e7a",  # decodes to base64 "Nz", not eight digits
        "31323334353637383930",  # hex of plain digits, not hex(base64(...))
        "71999001",  # the plain code is not a slug
    ],
)
def test_decode_invalid_slug_is_parse_error(slug: str) -> None:
    with pytest.raises(ParseError):
        decode_vestiging_slug(slug)


@pytest.mark.parametrize("code", ["", "1999001", "019990010", "0199900a", "  "])
def test_encode_invalid_code_is_parse_error(code: str) -> None:
    with pytest.raises(ParseError):
        encode_vestiging_slug(code)


def test_code_from_record_url_accepts_both_vestiging_forms() -> None:
    plain = "https://www.vektis.nl/agb-register/vestiging-71001001"
    encoded = "https://www.vektis.nl/agb-register/vestiging-4e7a45774d4445774d44453d"

    assert code_from_record_url(plain) == "71001001"
    assert code_from_record_url(encoded) == "71001001"
    assert code_from_record_url("https://www.vektis.nl/agb-register/zoeken") == ""


# --- classify_record_page --------------------------------------------------


def test_classify_not_found_on_404() -> None:
    page = fixture_page(
        "record_not_found.html",
        url="https://www.vektis.nl/agb-register/zorgverlener-99999999",
        status_code=404,
    )

    assert classify_record_page(page) == "not_found"


def test_classify_not_found_on_rendered_marker_without_404() -> None:
    page = fixture_page(
        "record_not_found.html",
        url="https://www.vektis.nl/agb-register/zorgverlener-99999999",
        status_code=200,
    )

    assert classify_record_page(page) == "not_found"


@pytest.mark.parametrize("name", sorted(DETAIL_URLS))
def test_classify_record_on_every_detail_fixture(name: str) -> None:
    assert classify_record_page(fixture_page(name)) == "record"


@pytest.mark.parametrize("name", ["search_results_zorgverlener.html", "session_expired.html"])
def test_classify_unrecognized_page_is_parse_error(name: str) -> None:
    with pytest.raises(ParseError):
        classify_record_page(fixture_page(name))


def test_classify_does_not_infer_absence_from_a_server_error() -> None:
    with pytest.raises(ParseError):
        classify_record_page(html_page("<html><body>502 Bad Gateway</body></html>", status_code=502))


# --- parse_record_identity -------------------------------------------------


def test_identity_zorgverlener_takes_name_from_basisregistratie() -> None:
    page, record_type, code = detail_page("record_zorgverlener.html")

    identity = parse_record_identity(page, record_type, code)

    assert identity.record_type == "zorgverlener"
    assert identity.agbcode == "01999001"
    # The h1 is empty on zorgverlener pages.
    assert identity.naam == "A. Testpersoon"


def test_identity_onderneming_takes_name_from_header() -> None:
    page, record_type, code = detail_page("record_onderneming.html")

    identity = parse_record_identity(page, record_type, code)

    assert identity == RecordIdentity(
        record_type="onderneming", agbcode="01999100", naam="Praktijk Voorbeeld B.V."
    )


def test_identity_vestiging_uses_requested_code_because_page_hides_it() -> None:
    page, record_type, code = detail_page("record_vestiging.html")

    identity = parse_record_identity(page, record_type, code)

    assert identity == RecordIdentity(
        record_type="vestiging", agbcode="71999001", naam="Praktijk Voorbeeld B.V."
    )


def test_identity_vestiging_trusts_expected_code_without_url_echo() -> None:
    html = (FIXTURES / "record_vestiging.html").read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in html.splitlines() if "itemprop=\"url\"" not in line and "og:url" not in line
    )

    identity = parse_record_identity(html_page(stripped, url="https://example.invalid/"), "vestiging", "71999002")

    assert identity.agbcode == "71999002"


def test_identity_vestiging_rejects_a_contradicting_url_echo() -> None:
    page, _, _ = detail_page("record_vestiging.html")

    with pytest.raises(ParseError, match="echoes code"):
        parse_record_identity(page, "vestiging", "71999002")


@pytest.mark.parametrize(
    ("name", "wrong_type"),
    [
        ("record_zorgverlener.html", "onderneming"),
        ("record_onderneming.html", "vestiging"),
        ("record_vestiging.html", "onderneming"),
    ],
)
def test_identity_type_mismatch_is_parse_error(name: str, wrong_type: str) -> None:
    page, _, code = detail_page(name)

    with pytest.raises(ParseError, match="expected"):
        parse_record_identity(page, wrong_type, code)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["record_zorgverlener.html", "record_onderneming.html"])
def test_identity_code_mismatch_is_parse_error(name: str) -> None:
    page, record_type, _ = detail_page(name)

    with pytest.raises(ParseError, match="shows code"):
        parse_record_identity(page, record_type, "09999999")


def test_identity_ignores_the_constant_organisatie_label() -> None:
    page, _, _ = detail_page("record_zorgverlener.html")

    # Every type carries the right-hand "Organisatie" label; it must not be read
    # as the record type, so a zorgverlener page still verifies as zorgverlener.
    assert "Organisatie" in page.html
    assert parse_record_identity(page, "zorgverlener", "01999001").record_type == "zorgverlener"


def test_identity_on_a_not_found_page_is_parse_error() -> None:
    page = fixture_page("record_not_found.html", status_code=404)

    with pytest.raises(ParseError):
        parse_record_identity(page, "zorgverlener", "99999999")


def test_identity_rejects_a_code_that_is_not_eight_digits() -> None:
    page, record_type, _ = detail_page("record_zorgverlener.html")

    with pytest.raises(ParseError, match="8 digits"):
        parse_record_identity(page, record_type, "1999001")


# --- parse_record ----------------------------------------------------------


def test_record_zorgverlener_all_sections() -> None:
    page, identity = identity_of("record_zorgverlener.html")

    record = parse_record(page, identity, ALL_SECTIONS)

    assert record.record_type == "zorgverlener"
    assert record.agbcode == "01999001"
    assert record.naam == "A. Testpersoon"
    assert record.source_url == page.url
    assert record.retrieved_at == FETCHED_AT
    assert record.status.beeindigd is False
    assert record.status.start == "1988-07-01"
    assert record.status.einde == ""

    assert record.basisregistratie is not None
    assert record.basisregistratie.naam == "A. Testpersoon"
    assert record.basisregistratie.geboortenaam == "A. Testpersoon-Voorbeeld"
    assert record.basisregistratie.geslacht == "Mannelijk"
    assert record.basisregistratie.academische_titel == "Doctorandus"
    assert record.basisregistratie.handelsnamen == []

    assert record.kwalificaties is not None
    assert [(k.code, k.naam, k.start, k.einde) for k in record.kwalificaties] == [
        ("0101", "Huisarts", "1988-07-01", ""),
        ("0110", "Huisarts, Apotheekhoudend", "1990-01-01", "2020-12-31"),
    ]

    assert record.erkenningen is not None
    assert [(e.naam, e.nummer, e.start, e.einde) for e in record.erkenningen] == [
        ("RIBIZ Artsen huisartsgeneeskunde", "", "1988-07-01", ""),
        ("BIG-registratie", "12345678901", "1988-07-01", ""),
    ]

    assert record.relaties is not None
    assert len(record.relaties.vestigingen) == 1
    vestiging = record.relaties.vestigingen[0]
    assert vestiging.naam == "Praktijk Voorbeeld B.V."
    # The relation's AGB-code column is "-" for a vestiging; decoded from the link.
    assert vestiging.agbcode == "71999001"
    assert vestiging.rol == "Huisarts"
    assert vestiging.start == "2007-01-01"
    assert vestiging.einde == ""
    assert [(r.naam, r.agbcode, r.rol) for r in record.relaties.arbeidsrelaties] == [
        ("Praktijk Voorbeeld B.V.", "01999100", "Vrijgevestigd (MTO getekend)")
    ]
    assert record.relaties.zorgverleners == []
    assert record.relaties.ondernemingen == []
    assert record.relaties.onderneming is None


def test_record_zorgverlener_has_no_contact_section() -> None:
    page, identity = identity_of("record_zorgverlener.html")

    record = parse_record(page, identity, ALL_SECTIONS)

    assert record.unavailable_sections == ["contact"]
    assert record.contact is None


def test_record_ended_zorgverlener_status() -> None:
    page, identity = identity_of("record_zorgverlener_ended.html")

    record = parse_record(page, identity, ALL_SECTIONS)

    assert record.agbcode == "01999009"
    assert record.naam == "B. Testpersoon"
    assert record.status.beeindigd is True
    assert record.status.start == "1973-01-01"
    assert record.status.einde == "2024-03-04"
    assert record.basisregistratie is not None
    # An empty label is simply not rendered.
    assert record.basisregistratie.academische_titel == ""
    assert record.kwalificaties is not None
    assert record.kwalificaties[0].einde == "2024-03-04"
    assert record.relaties is not None
    assert record.relaties.vestigingen[0].einde == "2024-03-04"


def test_record_active_pages_are_not_marked_ended() -> None:
    page, identity = identity_of("record_onderneming.html")

    assert parse_record(page, identity, []).status.beeindigd is False


def test_record_onderneming_all_sections() -> None:
    page, identity = identity_of("record_onderneming.html")

    record = parse_record(page, identity, ALL_SECTIONS)

    assert record.unavailable_sections == []
    assert record.status.start == "2021-12-09"
    assert record.basisregistratie is not None
    assert record.basisregistratie.naam == "Praktijk Voorbeeld B.V."
    assert record.basisregistratie.handelsnamen == ["Praktijk Voorbeeld"]

    assert record.contact is not None
    assert [a.soort for a in record.contact.adressen] == ["Bezoekadres", "Correspondentieadres"]
    bezoek = record.contact.adressen[0]
    assert (bezoek.straat, bezoek.postcode, bezoek.plaats) == ("Teststraat 1", "1234AB", "Teststad")
    assert (bezoek.provincie, bezoek.land) == ("Testprovincie", "Nederland")
    assert record.contact.adressen[1].straat == "Postbus 1"
    assert record.contact.adressen[1].postcode == "1234CD"
    assert record.contact.email == "info@praktijkvoorbeeld.example"
    assert record.contact.telefoon == "0101234567"

    assert record.kwalificaties is not None
    assert [(k.code, k.naam) for k in record.kwalificaties] == [("0100", "Huisartsenpraktijk")]
    assert record.erkenningen is not None
    assert [(e.naam, e.nummer) for e in record.erkenningen] == [
        ("Inschrijving handelsregister", "12345678"),
        ("WTZA meldplicht", ""),
    ]


def test_record_onderneming_relation_tables_do_not_bleed_into_each_other() -> None:
    page, identity = identity_of("record_onderneming.html")

    relaties = parse_record(page, identity, ["relaties"]).relaties

    # Three relation tables share one page block; each must stay in its own list.
    assert relaties is not None
    assert [(r.naam, r.agbcode, r.adres) for r in relaties.vestigingen] == [
        ("Praktijk Voorbeeld B.V.", "71999001", "Teststraat 1 1234AB Teststad")
    ]
    assert [(r.naam, r.agbcode, r.rol) for r in relaties.zorgverleners] == [
        ("A. Testpersoon", "01999001", "Eigenaar")
    ]
    assert [(r.naam, r.agbcode, r.rol) for r in relaties.ondernemingen] == [
        ("Praktijk Voorbeeld Noord B.V.", "01999101", "Samenwerkingsverband")
    ]
    assert relaties.arbeidsrelaties == []
    assert relaties.onderneming is None
    # The vestiging relation's own address column must not land in `rol`.
    assert relaties.vestigingen[0].rol == ""


def test_record_vestiging_all_sections() -> None:
    page, identity = identity_of("record_vestiging.html")

    record = parse_record(page, identity, ALL_SECTIONS)

    assert record.record_type == "vestiging"
    assert record.agbcode == "71999001"
    assert record.unavailable_sections == []
    assert record.status.start == "1991-03-01"
    assert record.basisregistratie is not None
    assert record.basisregistratie.handelsnamen == [
        "Praktijk Voorbeeld",
        "Huisartsenpraktijk Voorbeeld",
    ]
    assert record.contact is not None
    assert len(record.contact.adressen) == 1
    assert record.contact.email == "praktijk@voorbeeld.example"
    assert record.erkenningen is not None
    # Zero-padded KvK-nummer is preserved verbatim.
    assert record.erkenningen[0].nummer == "000012345678"


def test_record_vestiging_parent_relation_is_not_duplicated_by_the_mobile_card() -> None:
    page, identity = identity_of("record_vestiging.html")

    relaties = parse_record(page, identity, ["relaties"]).relaties

    # The parent onderneming is rendered twice: a `.d-md-none` card and the
    # desktop table. Only the table is parsed.
    assert "d-md-none" in page.html
    assert relaties is not None
    assert relaties.onderneming is not None
    assert relaties.onderneming.naam == "Praktijk Voorbeeld B.V."
    assert relaties.onderneming.agbcode == "01999100"
    assert relaties.onderneming.start == "1991-03-01"
    assert relaties.onderneming.einde == ""
    assert relaties.ondernemingen == []
    assert [(r.naam, r.agbcode, r.rol) for r in relaties.zorgverleners] == [
        ("A. Testpersoon", "01999001", "Huisarts")
    ]
    assert relaties.vestigingen == []


def test_record_default_sections_leave_the_rest_none() -> None:
    page, identity = identity_of("record_onderneming.html")

    record = parse_record(page, identity, DEFAULT_SECTIONS)

    assert record.requested_sections == ["basisregistratie", "kwalificaties"]
    assert record.unavailable_sections == []
    assert record.basisregistratie is not None
    assert record.kwalificaties is not None
    assert record.contact is None
    assert record.erkenningen is None
    assert record.relaties is None


def test_record_empty_section_list_returns_identity_and_status_only() -> None:
    page, identity = identity_of("record_onderneming.html")

    record = parse_record(page, identity, [])

    assert record.requested_sections == []
    assert record.unavailable_sections == []
    assert record.basisregistratie is None
    assert record.contact is None
    assert record.kwalificaties is None
    assert record.erkenningen is None
    assert record.relaties is None
    assert record.naam == "Praktijk Voorbeeld B.V."
    assert record.status.beeindigd is False


def test_record_explicit_sections_are_deduplicated_in_order() -> None:
    page, identity = identity_of("record_onderneming.html")

    record = parse_record(page, identity, ["contact", "basisregistratie", "contact"])

    assert record.requested_sections == ["contact", "basisregistratie"]
    assert record.contact is not None
    assert record.basisregistratie is not None
    assert record.kwalificaties is None


def test_record_unknown_section_is_parse_error() -> None:
    page, identity = identity_of("record_onderneming.html")

    with pytest.raises(ParseError, match="unknown record section"):
        parse_record(page, identity, ["adresgegevens"])  # type: ignore[list-item]


def test_record_available_but_empty_section_is_an_empty_list() -> None:
    page, identity = identity_of("record_onderneming.html")
    # Heading present, zero cards: empty, not unavailable.
    emptied = page.html.replace(
        '<h3 class="h5 ">Huisartsenpraktijk 0100</h3>', '<h3 class="h5 ">KEEP</h3>'
    )
    start = emptied.index('<h3 class="h4">Mijn kwalificaties</h3>')
    end = emptied.index('<h3 class="h4">Mijn erkenningen</h3>')
    emptied = (
        emptied[:start]
        + '<h3 class="h4">Mijn kwalificaties</h3></div><div class="row g-4"></div><div>'
        + emptied[end:]
    )

    record = parse_record(html_page(emptied, url=page.url), identity, ["kwalificaties"])

    assert record.kwalificaties == []
    assert record.unavailable_sections == []


def test_record_relaties_heading_without_tables_is_empty_not_unavailable() -> None:
    identity = RecordIdentity(record_type="zorgverlener", agbcode="01999001", naam="A. Testpersoon")
    html = """<html><body><div class="container">
      <header><h2 class="h3 mb-0">Relaties</h2></header>
    </div></body></html>"""

    record = parse_record(html_page(html), identity, ["relaties"])

    assert record.unavailable_sections == []
    assert record.relaties is not None
    assert record.relaties.vestigingen == []
    assert record.relaties.zorgverleners == []
    assert record.relaties.ondernemingen == []
    assert record.relaties.arbeidsrelaties == []
    assert record.relaties.onderneming is None


def test_record_absent_requested_sections_are_parse_error() -> None:
    identity = RecordIdentity(record_type="zorgverlener", agbcode="01999001", naam="A. Testpersoon")
    with pytest.raises(ParseError, match="requested sections missing"):
        parse_record(html_page("<html><body><p>niets</p></body></html>"), identity, ALL_SECTIONS)


MALFORMED_KWALIFICATIE = """<!DOCTYPE html><html><body><main>
<div class="block-pt-lg block-pb-lg bg-primary-subtle"><div class="container">
  <h2 class="h3">Bevoegdheden</h2>
  <div class="mt-4 mt-md-8"><h3 class="h4">Mijn kwalificaties</h3></div>
  <div class="row g-4">
    <div class="col-md-6 col-lg-4"><div class="card"><div class="card-body">
      <div><h3 class="h5 ">Huisarts</h3></div>
      <dl class="mb-0"><dt class="fw-normal mb-0">Start</dt><dd class="fw-bold mb-0">01-07-1988</dd></dl>
    </div></div></div>
  </div>
</div></div></main></body></html>"""


def test_record_present_but_malformed_section_is_parse_error() -> None:
    identity = RecordIdentity(record_type="zorgverlener", agbcode="01999001", naam="A. Testpersoon")

    with pytest.raises(ParseError, match="four-digit code"):
        parse_record(html_page(MALFORMED_KWALIFICATIE), identity, ["kwalificaties"])


def test_record_section_heading_without_its_card_row_is_parse_error() -> None:
    identity = RecordIdentity(record_type="zorgverlener", agbcode="01999001", naam="A. Testpersoon")
    html = """<html><body><div class="container">
      <div class="mt-4 mt-md-8"><h3 class="h4">Mijn kwalificaties</h3></div>
    </div></body></html>"""

    with pytest.raises(ParseError, match="card row"):
        parse_record(html_page(html), identity, ["kwalificaties"])


def test_record_malformed_relation_row_is_parse_error() -> None:
    identity = RecordIdentity(record_type="onderneming", agbcode="01999100", naam="Praktijk")
    html = """<html><body><div class="container">
      <h2 class="h3 mb-0">Relaties</h2>
      <table class="card-table table table-striped">
        <caption class="visually-hidden">Bij deze onderneming werken de volgende zorgverleners</caption>
        <thead><tr><th>Naam</th><th>Rol</th><th>AGB-code</th><th>Start</th><th>Einde</th></tr></thead>
        <tbody class="card-table__body"><tr>
          <td class="card-table__cell card-table__cell--name">A. Testpersoon</td>
          <td class="card-table__cell">Eigenaar</td>
          <td class="card-table__cell">-</td>
          <td class="card-table__cell card-table__cell--date">09-12-2021</td>
          <td class="card-table__cell card-table__cell--date">-</td>
        </tr></tbody>
      </table>
    </div></body></html>"""

    with pytest.raises(ParseError, match="AGB-code"):
        parse_record(html_page(html), identity, ["relaties"])


def test_record_basisregistratie_without_a_name_is_parse_error() -> None:
    identity = RecordIdentity(record_type="onderneming", agbcode="01999100", naam="Praktijk")
    html = """<html><body><div class="container"><section>
      <h2 class="h3">Basisregistratie</h2>
      <dl><dt class="fw-normal mb-0">Handelsnaam</dt><dd class="fw-bold mb-0">Praktijk</dd></dl>
    </section></div></body></html>"""

    with pytest.raises(ParseError, match="'Naam'"):
        parse_record(html_page(html), identity, ["basisregistratie"])


# Layout drift: preserve data across cosmetic changes, reject ambiguous shapes.
def _reverse_table_columns(html: str) -> str:
    import re

    def reverse_row(match: re.Match[str]) -> str:
        cells = re.findall(r"<t[hd]\b.*?</t[hd]>", match.group(1), flags=re.S)
        return "<tr>" + "".join(reversed(cells)) + "</tr>"

    return re.sub(r"<tr\b[^>]*>(.*?)</tr>", reverse_row, html, flags=re.S)


def test_search_reordered_columns_and_removed_table_styles_preserve_results() -> None:
    page = fixture_page("search_results_zorgverlener.html")
    changed = _reverse_table_columns(page.html)
    changed = changed.replace("js-datatable", "new-table").replace("card-table__body", "new-body")
    assert parse_search(html_page(changed)).rows == parse_search(page).rows


def test_relations_reordered_columns_and_removed_caption_styles_preserve_results() -> None:
    page, identity = identity_of("record_onderneming.html")
    changed = _reverse_table_columns(page.html).replace("visually-hidden", "new-caption")
    changed = changed.replace("card-table__body", "new-body")
    assert parse_record(html_page(changed), identity, ["relaties"]).relaties == parse_record(
        page, identity, ["relaties"]
    ).relaties


@pytest.mark.parametrize("replacement", ["Unknown", "Naam zorgverlener"])
def test_search_unknown_or_duplicate_headers_fail(replacement: str) -> None:
    html = search_html(search_row()).replace("<th>Adres</th>", f"<th>{replacement}</th>")
    with pytest.raises(ParseError, match="heading"):
        parse_search(html_page(html))


def test_search_missing_headers_fail_instead_of_guessing_positions() -> None:
    import re

    html = re.sub(r"<thead>.*?</thead>", "", search_html(search_row()))
    with pytest.raises(ParseError, match="header row"):
        parse_search(html_page(html))


@pytest.mark.parametrize("section, heading", [
    ("basisregistratie", "Basisregistratie"),
    ("contact", "Contactgegevens"),
    ("kwalificaties", "Mijn kwalificaties"),
    ("erkenningen", "Mijn erkenningen"),
])
def test_renamed_requested_section_fails(section: str, heading: str) -> None:
    page, identity = identity_of("record_onderneming.html")
    html = page.html.replace(heading, "New heading")
    with pytest.raises(ParseError, match="requested sections missing"):
        parse_record(html_page(html), identity, [section])


@pytest.mark.parametrize("old, new", [
    ("card-body", "new-body"),
    ("Bij deze onderneming werken de volgende zorgverleners", "New relation caption"),
])
def test_unrecognized_content_cannot_be_reported_as_empty(old: str, new: str) -> None:
    page, identity = identity_of("record_onderneming.html")
    with pytest.raises(ParseError):
        parse_record(html_page(page.html.replace(old, new)), identity, ALL_SECTIONS)


def test_identity_code_survives_styling_changes() -> None:
    page, record_type, code = detail_page("record_onderneming.html")
    changed = html_page(page.html.replace('class="h4 mb-0"', 'class="new-code"'), url=page.url)
    assert parse_record_identity(changed, record_type, code) == parse_record_identity(page, record_type, code)


def test_contact_values_survive_styling_changes() -> None:
    page, identity = identity_of("record_onderneming.html")
    changed = page.html.replace('class="mb-2 h5"', 'class="new-label"').replace("text-nowrap", "new-value")
    assert parse_record(html_page(changed), identity, ["contact"]).contact == parse_record(
        page, identity, ["contact"]
    ).contact


def test_unrequested_malformed_cards_do_not_break_basis_lookup() -> None:
    page, identity = identity_of("record_onderneming.html")
    changed = page.html.replace('class="row g-4"', 'class="new-layout"')
    assert parse_record(html_page(changed), identity, ["basisregistratie"]).basisregistratie is not None


def test_search_cap_marker_without_a_table_is_a_parse_error() -> None:
    """The marker claims 500 rendered rows; with no table the page was misread."""
    html = (
        '<section class="js-search-results"><div id="resultsContainer">'
        '<div class="alert-warning alert" id="500PlusAlert">Er zijn meer dan 500 resultaten</div>'
        "</div></section>"
    )
    page = SourcePage(html=html, url=RESULTS_URL, fetched_at=datetime.now(UTC), status_code=200)

    with pytest.raises(ParseError, match="render-cap marker but no results table"):
        parse_search(page)
