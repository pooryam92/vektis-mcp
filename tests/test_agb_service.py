"""Tests for the AGB service layer: validation, search assembly, exact records.

Every test drives the real `AgbService` over a real `AgbClient` whose only fake
is an `httpx.MockTransport`, so the full stack — pacing, the form/POST/redirect
transaction, the parsers — runs against the committed fixtures. Time is faked
through the pacer (the client borrows its clock and sleep), so retries and
backoff cost nothing.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from vektis_mcp.agb.client import AgbClient, Pacer
from vektis_mcp.agb.constants import FORM_URL, RENDER_CAP, RESULTS_URL
from vektis_mcp.agb.errors import InvalidInput, ParseError, SourceUnavailable
from vektis_mcp.agb.parsers import DEFAULT_SECTIONS, encode_vestiging_slug
from vektis_mcp.agb.service import AgbService, build_search_criteria, record_url
from vektis_mcp.agb.types import SearchCriteria
from vektis_mcp.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "agb"

def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


FORM_HTML = fixture("search_form.html")
RESULTS_ZORGVERLENER = fixture("search_results_zorgverlener.html")
RESULTS_ORGANISATIE = fixture("search_results_organisatie.html")
RESULTS_EMPTY = fixture("search_results_empty.html")
RESULTS_CAPPED = fixture("search_results_capped.html")
RECORD_ZORGVERLENER = fixture("record_zorgverlener.html")
RECORD_ZORGVERLENER_ENDED = fixture("record_zorgverlener_ended.html")
RECORD_ONDERNEMING = fixture("record_onderneming.html")
RECORD_VESTIGING = fixture("record_vestiging.html")
RECORD_NOT_FOUND = fixture("record_not_found.html")

ZV_CODE = "01999001"
ZV_URL = record_url("zorgverlener", ZV_CODE)
ON_URL = record_url("onderneming", ZV_CODE)
VEST_CODE = "71999001"
VEST_PLAIN_URL = record_url("vestiging", VEST_CODE)
VEST_SLUG_URL = record_url("vestiging", encode_vestiging_slug(VEST_CODE))


class FakeTime:
    """A monotonic clock that only advances when something sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class Source:
    """Records every request and answers through the given handler."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def build(handler, *, settings: Settings | None = None) -> tuple[AgbService, Source, FakeTime]:
    """A service over the real client, a mock transport and a zero-wait pacer."""
    source = Source(handler)
    clock = FakeTime()
    # interval 0 keeps the pacing path exercised without any waiting.
    pacer = Pacer(0.0, clock=clock.clock, sleep=clock.sleep)
    client = AgbClient(settings or Settings(), pacer, transport=httpx.MockTransport(source))
    return AgbService(client), source, clock


def searching(results_html: str):
    """The verified live search flow: form GET 200, POST 302, redirect GET 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == FORM_URL:
            return httpx.Response(200, html=FORM_HTML)
        if request.method == "POST" and url == RESULTS_URL:
            return httpx.Response(302, headers={"location": f"{RESULTS_URL}#resultaten"})
        if request.method == "GET" and url == RESULTS_URL:
            return httpx.Response(200, html=results_html)
        raise AssertionError(f"unexpected request {request.method} {url}")

    return handler


def records(pages: dict[str, tuple[int, str]]):
    """Serve detail pages by URL; anything unlisted is the rendered 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        status, html = pages.get(str(request.url), (404, RECORD_NOT_FOUND))
        return httpx.Response(status, html=html)

    return handler


# --- HTML surgery on the fixtures -------------------------------------------


def duplicate_first_row(html: str) -> str:
    """Repeat the first result row, bumping the count heading to match.

    The source does emit the same record twice when a query matches it through
    more than one path; the count then covers both rows, so the heading has to
    move with it or the completeness check (rightly) rejects the page.
    """
    opener = '<tbody class="card-table__body">'
    start = html.index(opener) + len(opener)
    end = html.index("</tr>", start) + len("</tr>")
    row = html[start:end]
    count = int(re.search(r"(\d+) Zoekresultaten", html).group(1))
    return (
        html[:end] + row + html[end:]
    ).replace(f"{count} Zoekresultaten", f"{count + 1} Zoekresultaten")


def without_cap_marker(html: str) -> str:
    return re.sub(r'<div class="alert-warning alert" id="500PlusAlert".*?</div></div></div>', "", html, flags=re.S)


ROW_TEMPLATE = """
<tr>
    <td class="card-table__cell fs-5" data-search="Zorgverlener" data-order="Zorgverlener">
        <svg class="icon icon-caregiver" role="img" aria-hidden="true"></svg>
    </td>
    <td class="card-table__cell card-table__cell--name">
        <a href="https://www.vektis.nl/agb-register/zorgverlener-{code}">{naam}</a>
    </td>
    <td class="card-table__cell">
        <a href="https://www.vektis.nl/agb-register/zorgverlener-{code}">{code}</a>
    </td>
    <td class="card-table__cell"></td>
    <td class="card-table__cell"></td>
    <td class="card-table__cell card-table__cell--city"></td>
    <td class="card-table__cell card-table__cell--date" data-order="">-</td>
</tr>
"""


def synthetic_results(row_count: int, *, count_heading: int | None, cap_marker: bool) -> str:
    """A results page with an arbitrary number of rows, built from the live shape.

    Used for the 500-row cases the committed fixtures deliberately keep small.
    """
    rows = "".join(
        ROW_TEMPLATE.format(code=f"019{index:05d}", naam=f"Testpersoon {index}") for index in range(row_count)
    )
    heading = f'<h2 class="h3 mt-4">{count_heading} Zoekresultaten</h2>' if count_heading is not None else ""
    marker = (
        '<div class="alert-warning alert" id="500PlusAlert" role="alert"><div class="d-flex gap-1">'
        "<div class=\"flex-grow-1\">Er zijn meer dan 500 resultaten: niet alle resultaten worden "
        "getoond.</div></div></div>"
        if cap_marker
        else ""
    )
    return (
        '<!DOCTYPE html><html lang="nl"><body><main>'
        '<section class="js-search-results" data-value="zorgverlener">'
        f'<div id="resultsContainer">{heading}{marker}'
        '<div class="card-table-container"><table class="card-table table table-striped js-datatable">'
        '<caption class="visually-hidden">Resultaten</caption>'
        '<thead><tr><th>Type</th><th>Naam zorgverlener</th><th>AGB-code</th><th>Adres</th><th>Postcode</th><th>Plaats</th><th>Einddatum</th></tr></thead>'
        f'<tbody class="card-table__body">{rows}</tbody></table></div>'
        "</div></section></main></body></html>"
    )


# --- build_search_criteria --------------------------------------------------


def test_criteria_trims_and_normalizes() -> None:
    criteria = build_search_criteria(
        "organisatie",
        query="  Praktijk Voorbeeld  ",
        zorgsoort=" 01 ",
        kwalificaties=[" 0100 ", "0100", ""],
        plaats="  Assen ",
        postcode=" 9403 aa ",
        kvknummer=" 12345678 ",
        include_ended=True,
    )

    assert criteria == SearchCriteria(
        side="organisatie",
        query="Praktijk Voorbeeld",
        zorgsoort="01",
        kwalificaties=("0100",),
        plaats="Assen",
        postcode="9403AA",
        kvknummer="12345678",
        include_ended=True,
    )


def test_criteria_blank_strings_are_no_criterion() -> None:
    criteria = build_search_criteria("organisatie", query="Praktijk", plaats="   ", postcode="", kvknummer=None)

    assert (criteria.plaats, criteria.postcode, criteria.kvknummer) == (None, None, None)


def test_criteria_postcode_without_letters_is_kept() -> None:
    assert build_search_criteria("organisatie", postcode="9403").postcode == "9403"


@pytest.mark.parametrize(
    "field",
    ["plaats", "postcode", "kvknummer"],
)
def test_criteria_rejects_location_filters_for_individuals(field: str) -> None:
    values = {"plaats": "Assen", "postcode": "9403AA", "kvknummer": "12345678"}

    with pytest.raises(InvalidInput, match="organisation search only"):
        build_search_criteria("zorgverlener", query="0100045", **{field: values[field]})


@pytest.mark.parametrize("query", [None, "", "   "])
def test_criteria_requires_a_query_for_individuals(query: str | None) -> None:
    with pytest.raises(InvalidInput, match="query is required"):
        build_search_criteria("zorgverlener", query=query)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"include_ended": True},
        {"query": "   ", "kwalificaties": []},
    ],
)
def test_criteria_requires_one_organisation_criterion(kwargs: dict) -> None:
    with pytest.raises(InvalidInput, match="at least one criterion"):
        build_search_criteria("organisatie", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "Praktijk"},
        {"plaats": "Assen"},
        {"postcode": "9403"},
        {"kvknummer": "12345678"},
        {"zorgsoort": "01"},
        {"kwalificaties": ["0100"]},
    ],
)
def test_criteria_accepts_any_single_organisation_criterion(kwargs: dict) -> None:
    assert build_search_criteria("organisatie", **kwargs).side == "organisatie"


def test_criteria_rejects_an_unknown_side() -> None:
    with pytest.raises(InvalidInput, match="Unknown side"):
        build_search_criteria("ziekenhuis", query="x")  # type: ignore[arg-type]


def test_criteria_rejects_a_zorgsoort_the_side_does_not_have() -> None:
    # 06 Ziekenhuizen exists on the organisation side only.
    with pytest.raises(InvalidInput, match="Unknown zorgsoort"):
        build_search_criteria("zorgverlener", query="0100045", zorgsoort="06")


def test_criteria_rejects_a_kwalificatie_outside_its_zorgsoort() -> None:
    with pytest.raises(InvalidInput, match="Unknown kwalificatie codes for zorgsoort 01"):
        build_search_criteria("zorgverlener", query="0100045", zorgsoort="01", kwalificaties=["0201"])


def test_criteria_rejects_an_unknown_kwalificatie_without_a_zorgsoort() -> None:
    with pytest.raises(InvalidInput, match="Unknown kwalificatie codes for zorgverlener"):
        build_search_criteria("zorgverlener", query="0100045", kwalificaties=["9999"])


# --- search -----------------------------------------------------------------


async def test_search_zorgverleners_drops_ended_records() -> None:
    service, source, _ = build(searching(RESULTS_ZORGVERLENER))

    response = await service.search(build_search_criteria("zorgverlener", query="01999"), limit=25)

    assert [r.agbcode for r in response.results] == ["01999001", "01999002"]
    assert response.returned_count == 2
    assert response.source_total == 4
    assert response.has_more is False
    assert response.source_truncated is False
    assert source.urls == [FORM_URL, RESULTS_URL, RESULTS_URL]


async def test_search_include_ended_keeps_every_row() -> None:
    service, _, _ = build(searching(RESULTS_ZORGVERLENER))

    response = await service.search(
        build_search_criteria("zorgverlener", query="01999", include_ended=True), limit=25
    )

    assert [r.agbcode for r in response.results] == ["01999001", "01999002", "01999009", "01999010"]
    assert [r.einddatum for r in response.results[2:]] == ["2024-03-04", "2024-12-31"]
    assert response.returned_count == 4
    assert response.has_more is False


async def test_search_limit_reports_known_extra_matches() -> None:
    service, _, _ = build(searching(RESULTS_ZORGVERLENER))

    response = await service.search(
        build_search_criteria("zorgverlener", query="01999", include_ended=True), limit=3
    )

    assert response.returned_count == 3
    assert response.has_more is True
    assert response.source_truncated is False


async def test_search_limit_equal_to_the_match_count_has_no_more() -> None:
    service, _, _ = build(searching(RESULTS_ZORGVERLENER))

    response = await service.search(build_search_criteria("zorgverlener", query="01999"), limit=2)

    assert (response.returned_count, response.has_more) == (2, False)


async def test_search_deduplicates_repeated_record_type_and_code() -> None:
    service, _, _ = build(searching(duplicate_first_row(RESULTS_ZORGVERLENER)))

    response = await service.search(build_search_criteria("zorgverlener", query="01999"), limit=25)

    assert [r.agbcode for r in response.results] == ["01999001", "01999002"]
    assert response.source_total == 5  # the source counted the duplicate; we did not


async def test_search_applied_filters_echo_the_normalized_inputs() -> None:
    service, _, _ = build(searching(RESULTS_ORGANISATIE))

    response = await service.search(
        build_search_criteria(
            "organisatie",
            query="  Praktijk ",
            zorgsoort="01",
            kwalificaties=["0100"],
            plaats=" Assen ",
            postcode="9403 aa",
            kvknummer="12345678",
            include_ended=True,
        ),
        limit=25,
    )

    assert response.applied_filters.model_dump() == {
        "query": "Praktijk",
        "zorgsoort": "01",
        "kwalificaties": ["0100"],
        "plaats": "Assen",
        "postcode": "9403AA",
        "kvknummer": "12345678",
        "include_ended": True,
    }


async def test_search_applied_filters_report_no_kwalificaties_as_null() -> None:
    service, _, _ = build(searching(RESULTS_ORGANISATIE))

    response = await service.search(
        build_search_criteria("organisatie", query="Praktijk", kwalificaties=[]), limit=25
    )

    assert response.applied_filters.kwalificaties is None
    assert response.applied_filters.include_ended is False


async def test_search_organisaties_returns_both_types_with_addresses() -> None:
    service, source, _ = build(searching(RESULTS_ORGANISATIE))

    response = await service.search(build_search_criteria("organisatie", plaats="Teststad"), limit=25)

    assert [(r.record_type, r.agbcode) for r in response.results] == [
        ("onderneming", "01999100"),
        ("vestiging", "71999001"),
    ]
    first, vestiging = response.results
    assert (first.adres, first.postcode, first.plaats) == ("Teststraat 1", "1234AB", "TESTSTAD")
    # A vestiging row shows "-" in the code column; the code comes from the slug.
    assert vestiging.source_url.endswith(f"vestiging-{encode_vestiging_slug('71999001')}")
    assert "plaats=Teststad" in source.requests[1].content.decode()


async def test_search_decodes_a_vestiging_slug_with_a_leading_zero() -> None:
    # The register's 01-range vestigingen keep their leading zero only because the
    # slug is decoded as exactly eight ASCII digits, never as an int.
    html = RESULTS_ORGANISATIE.replace(encode_vestiging_slug("71999001"), encode_vestiging_slug("01999200"))
    service, _, _ = build(searching(html))

    response = await service.search(build_search_criteria("organisatie", plaats="Teststad"), limit=25)

    assert [r.agbcode for r in response.results] == ["01999100", "01999200"]


async def test_search_empty_page_is_an_empty_result_set() -> None:
    service, _, _ = build(searching(RESULTS_EMPTY))

    response = await service.search(build_search_criteria("zorgverlener", query="00000000"), limit=25)

    assert response.results == []
    assert response.returned_count == 0
    assert response.source_total is None
    assert response.has_more is False
    assert response.source_truncated is False


async def test_search_cap_marker_sets_source_truncated() -> None:
    # The design's example, scaled to the fixture: 800 source matches, the cap
    # marker, and only a few rendered rows survive local filtering.
    service, _, _ = build(searching(RESULTS_CAPPED))

    response = await service.search(build_search_criteria("zorgverlener", query="01"), limit=25)

    assert response.source_total == 800
    assert response.source_truncated is True
    # Two active rows of three rendered: nothing more is *known*, yet matches
    # beyond the cap may still exist.
    assert (response.returned_count, response.has_more) == (2, False)


async def test_search_cap_marker_and_limit_both_report_more() -> None:
    service, _, _ = build(searching(RESULTS_CAPPED))

    response = await service.search(build_search_criteria("zorgverlener", query="01"), limit=1)

    assert (response.returned_count, response.has_more, response.source_truncated) == (1, True, True)


async def test_search_count_above_rows_at_the_cap_is_truncation() -> None:
    service, _, _ = build(
        searching(synthetic_results(RENDER_CAP, count_heading=800, cap_marker=False))
    )

    response = await service.search(build_search_criteria("zorgverlener", query="01"), limit=25)

    assert (response.source_total, response.source_truncated, response.has_more) == (800, True, True)
    assert response.returned_count == 25


async def test_search_count_mismatch_below_the_cap_is_a_parse_error() -> None:
    service, _, _ = build(searching(without_cap_marker(RESULTS_CAPPED)))

    with pytest.raises(ParseError, match="claims 800 results but rendered 3 rows"):
        await service.search(build_search_criteria("zorgverlener", query="01"), limit=25)


async def test_search_full_cap_page_without_evidence_is_indeterminate() -> None:
    service, _, _ = build(
        searching(synthetic_results(RENDER_CAP, count_heading=None, cap_marker=False))
    )

    with pytest.raises(ParseError, match="completeness is indeterminate"):
        await service.search(build_search_criteria("zorgverlener", query="01"), limit=25)


async def test_search_unrecognized_page_is_never_zero_results() -> None:
    service, _, _ = build(searching("<html><body><p>Onderhoud</p></body></html>"))

    with pytest.raises(ParseError):
        await service.search(build_search_criteria("zorgverlener", query="01"), limit=25)


async def test_search_source_failure_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    service, source, _ = build(handler)

    with pytest.raises(SourceUnavailable):
        await service.search(build_search_criteria("zorgverlener", query="01"), limit=25)
    assert len(source.requests) == 3  # the initial attempt plus two retries


# --- get_record: probes -----------------------------------------------------


async def test_get_record_with_a_type_probes_only_that_url() -> None:
    service, source, _ = build(records({ZV_URL: (200, RECORD_ZORGVERLENER)}))

    response = await service.get_record(ZV_CODE, "zorgverlener", None)

    assert response.outcome == "found"
    assert source.urls == [ZV_URL]
    assert response.record is not None
    assert (response.record.record_type, response.record.agbcode) == ("zorgverlener", ZV_CODE)
    assert response.record.naam == "A. Testpersoon"
    assert response.candidates == []


async def test_get_record_without_a_type_probes_every_candidate() -> None:
    service, source, _ = build(records({ON_URL: (200, RECORD_ONDERNEMING.replace("01999100", ZV_CODE))}))

    response = await service.get_record(ZV_CODE, None, None)

    # Plain-digit vestiging first; the encoded form is only tried after that 404.
    assert source.urls == [
        ZV_URL,
        ON_URL,
        record_url("vestiging", ZV_CODE),
        record_url("vestiging", encode_vestiging_slug(ZV_CODE)),
    ]
    assert response.outcome == "found"
    assert response.record is not None
    assert response.record.record_type == "onderneming"


async def test_get_record_recognized_404_is_absence() -> None:
    service, source, _ = build(records({}))

    response = await service.get_record("01999999", None, None)

    assert response.outcome == "not_found"
    assert response.record is None
    assert response.candidates == []
    assert len(source.urls) == 4


async def test_get_record_identity_mismatch_is_a_parse_error() -> None:
    # The page for 01999001 served under the URL for 01999002.
    service, _, _ = build(records({record_url("zorgverlener", "01999002"): (200, RECORD_ZORGVERLENER)}))

    with pytest.raises(ParseError, match="shows code '01999001', expected '01999002'"):
        await service.get_record("01999002", "zorgverlener", None)


async def test_get_record_wrong_type_page_is_a_parse_error() -> None:
    # A 200 that is the *other* type cannot be read as a match for this URL.
    service, _, _ = build(records({ON_URL: (200, RECORD_ZORGVERLENER)}))

    with pytest.raises(ParseError, match="page is a 'zorgverlener', expected 'onderneming'"):
        await service.get_record(ZV_CODE, "onderneming", None)


async def test_get_record_ambiguous_across_types() -> None:
    service, source, _ = build(
        records(
            {
                ZV_URL: (200, RECORD_ZORGVERLENER),
                ON_URL: (200, RECORD_ONDERNEMING.replace("01999100", ZV_CODE)),
            }
        )
    )

    response = await service.get_record(ZV_CODE, None, None)

    assert response.outcome == "ambiguous"
    assert response.record is None
    assert [(c.record_type, c.agbcode, c.naam, c.source_url) for c in response.candidates] == [
        ("zorgverlener", ZV_CODE, "A. Testpersoon", ZV_URL),
        ("onderneming", ZV_CODE, "Praktijk Voorbeeld B.V.", ON_URL),
    ]


async def test_get_record_vestiging_plain_form_skips_the_encoded_probe() -> None:
    service, source, _ = build(records({VEST_PLAIN_URL: (200, RECORD_VESTIGING)}))

    response = await service.get_record(VEST_CODE, "vestiging", None)

    assert source.urls == [VEST_PLAIN_URL]
    assert response.outcome == "found"
    assert response.record is not None
    assert (response.record.record_type, response.record.agbcode) == ("vestiging", VEST_CODE)


async def test_get_record_falls_back_to_the_encoded_vestiging_form() -> None:
    service, source, _ = build(records({VEST_SLUG_URL: (200, RECORD_VESTIGING)}))

    response = await service.get_record(VEST_CODE, "vestiging", None)

    assert source.urls == [VEST_PLAIN_URL, VEST_SLUG_URL]
    assert response.outcome == "found"


async def test_get_record_both_vestiging_forms_resolve_to_one_record() -> None:
    # Live, both URL forms answer 200 without redirecting. Whichever order they
    # are probed in, the lookup must stay unambiguous.
    pages = {VEST_PLAIN_URL: (200, RECORD_VESTIGING), VEST_SLUG_URL: (200, RECORD_VESTIGING)}
    service, source, _ = build(records(pages))

    response = await service.get_record(VEST_CODE, "vestiging", None)

    assert response.outcome == "found"
    assert response.candidates == []
    assert source.urls == [VEST_PLAIN_URL]


async def test_get_record_failed_probe_never_proves_absence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ZV_URL:
            return httpx.Response(503)
        return httpx.Response(404, html=RECORD_NOT_FOUND)

    service, _, _ = build(handler)

    with pytest.raises(SourceUnavailable):
        await service.get_record(ZV_CODE, None, None)


async def test_get_record_rejects_a_malformed_code() -> None:
    service, source, _ = build(records({}))

    with pytest.raises(InvalidInput, match="eight digits"):
        await service.get_record("1999", None, None)
    assert source.urls == []


async def test_get_record_rejects_an_unknown_record_type() -> None:
    service, source, _ = build(records({}))

    with pytest.raises(InvalidInput, match="Unknown record_type"):
        await service.get_record(ZV_CODE, "praktijk", None)  # type: ignore[arg-type]
    assert source.urls == []


# --- get_record: sections ---------------------------------------------------


async def test_get_record_default_sections() -> None:
    service, source, _ = build(records({ZV_URL: (200, RECORD_ZORGVERLENER)}))

    record = (await service.get_record(ZV_CODE, "zorgverlener", None)).record

    assert record is not None
    assert record.requested_sections == list(DEFAULT_SECTIONS)
    assert record.basisregistratie is not None
    assert record.kwalificaties is not None
    assert (record.contact, record.erkenningen, record.relaties) == (None, None, None)
    assert record.unavailable_sections == []
    # One fetch only: the sections come out of the page already in hand.
    assert len(source.requests) == 1


async def test_get_record_empty_section_list_is_identity_and_status_only() -> None:
    service, source, _ = build(records({ZV_URL: (200, RECORD_ZORGVERLENER_ENDED.replace("01999009", ZV_CODE))}))

    record = (await service.get_record(ZV_CODE, "zorgverlener", [])).record

    assert record is not None
    assert record.requested_sections == []
    assert record.unavailable_sections == []
    assert (record.basisregistratie, record.contact, record.kwalificaties) == (None, None, None)
    assert (record.erkenningen, record.relaties) == (None, None)
    assert record.naam == "B. Testpersoon"
    assert record.status.beeindigd is True
    assert record.status.einde == "2024-03-04"
    assert record.retrieved_at.tzinfo is not None
    assert record.source_url == ZV_URL
    assert len(source.requests) == 1


async def test_get_record_explicit_sections_report_what_the_page_lacks() -> None:
    service, source, _ = build(records({ZV_URL: (200, RECORD_ZORGVERLENER)}))

    record = (
        await service.get_record(ZV_CODE, "zorgverlener", ["contact", "relaties", "contact"])
    ).record

    assert record is not None
    # Deduplicated, order preserved; a zorgverlener page has no Contactgegevens.
    assert record.requested_sections == ["contact", "relaties"]
    assert record.unavailable_sections == ["contact"]
    assert record.contact is None
    assert record.relaties is not None
    # The related vestiging's code comes from its href, not from the "-" cell.
    assert [r.agbcode for r in record.relaties.vestigingen] == ["71999001"]
    assert len(source.requests) == 1


async def test_get_record_all_sections_on_an_onderneming() -> None:
    service, source, _ = build(records({record_url("onderneming", "01999100"): (200, RECORD_ONDERNEMING)}))

    record = (
        await service.get_record(
            "01999100",
            "onderneming",
            ["basisregistratie", "contact", "kwalificaties", "erkenningen", "relaties"],
        )
    ).record

    assert record is not None
    assert record.unavailable_sections == []
    assert record.contact is not None
    assert [a.soort for a in record.contact.adressen] == ["Bezoekadres", "Correspondentieadres"]
    assert [k.code for k in record.kwalificaties or []] == ["0100"]
    assert [e.nummer for e in record.erkenningen or []] == ["12345678", ""]
    assert record.relaties is not None
    assert len(source.requests) == 1


async def test_get_record_rejects_non_ascii_digits() -> None:
    """str.isdigit() accepts fullwidth digits; those would crash the slug encoder."""
    service, source, _ = build(records({}))

    with pytest.raises(InvalidInput, match="eight digits"):
        await service.get_record("１２３４５６７８")

    assert source.urls == []
