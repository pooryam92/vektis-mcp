"""The real FastMCP server, exercised through real MCP sessions.

These tests start `vektis_mcp.server.mcp` itself — lifespan, tool schemas, error
translation and all — and talk to it as a client would. The only fake is the
`httpx` transport the lifespan picks up from `server.configure()`, so the source
client, pacer and parsers are the production ones.

Three transports are covered: the SDK's in-memory pair (most tests), the
Streamable HTTP runner on an ephemeral port, and a real `uv run vektis-mcp`
subprocess over stdio, which also proves stdout carries nothing but MCP frames.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
# `streamablehttp_client` is the deprecated alias of this in mcp 1.30.0.
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from vektis_mcp import server
from vektis_mcp.agb.constants import FORM_URL, RESULTS_URL
from vektis_mcp.agb.parsers import encode_vestiging_slug
from vektis_mcp.models import RecordResponse, SearchResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "agb"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


FORM_HTML = fixture("search_form.html")
RESULTS_ZORGVERLENER = fixture("search_results_zorgverlener.html")
RECORD_ZORGVERLENER = fixture("record_zorgverlener.html")
RECORD_ONDERNEMING = fixture("record_onderneming.html")
RECORD_NOT_FOUND = fixture("record_not_found.html")

ZV_CODE = "01999001"
RECORD_BASE = "https://www.vektis.nl/agb-register/"

#: The public input schemas. Hardcoded on purpose: these property names are the
#: contract an MCP client sees, so a refactor must not quietly rename one.
EXPECTED_PROPERTIES = {
    "agb_search_zorgverleners": ["include_ended", "kwalificaties", "limit", "query", "zorgsoort"],
    "agb_search_organisaties": [
        "include_ended",
        "kvknummer",
        "kwalificaties",
        "limit",
        "plaats",
        "postcode",
        "query",
        "zorgsoort",
    ],
    "agb_get_record": ["agbcode", "record_type", "sections"],
    "agb_lookup_codes": ["entity_kind", "query", "zorgsoort"],
}


@pytest.fixture(autouse=True)
def fast_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the inter-request pacing: the mock transport needs no politeness."""
    monkeypatch.setenv("VEKTIS_REQUEST_INTERVAL_SECONDS", "0")


def searching(results_html: str):
    """The verified live search flow, over the mock transport."""

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


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"the tool must not reach the network ({request.url})")


@asynccontextmanager
async def connected(handler=no_network) -> AsyncIterator[ClientSession]:
    """An initialized MCP session against the real server over in-memory streams.

    `server.configure` is the documented test seam: it parks an `httpx` transport
    that the next lifespan hands to its `AgbClient`.
    """
    server.configure(httpx.MockTransport(handler))
    try:
        async with create_connected_server_and_client_session(server.mcp) as session:
            yield session
    finally:
        server.configure(None)


def text_of(result: types.CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, types.TextContent))


# --- discovery ---------------------------------------------------------------


async def test_list_tools_keeps_the_published_schemas() -> None:
    async with connected() as session:
        listed = await session.list_tools()

    assert [tool.name for tool in listed.tools] == list(EXPECTED_PROPERTIES)
    for tool in listed.tools:
        assert sorted(tool.inputSchema["properties"]) == EXPECTED_PROPERTIES[tool.name], tool.name
        # The injected Context parameter must never leak into the schema.
        assert "ctx" not in tool.inputSchema["properties"]
    required = {tool.name: tool.inputSchema.get("required") for tool in listed.tools}
    assert required == {
        "agb_search_zorgverleners": ["query"],
        "agb_search_organisaties": None,
        "agb_get_record": ["agbcode"],
        "agb_lookup_codes": ["entity_kind"],
    }


# --- search ------------------------------------------------------------------


async def test_search_zorgverleners_returns_structured_content() -> None:
    async with connected(searching(RESULTS_ZORGVERLENER)) as session:
        result = await session.call_tool("agb_search_zorgverleners", {"query": "01999", "limit": 3})

    assert result.isError is False
    assert result.structuredContent is not None
    response = SearchResponse.model_validate(result.structuredContent)
    assert [r.agbcode for r in response.results] == ["01999001", "01999002"]
    assert (response.returned_count, response.source_total) == (2, 4)
    assert (response.has_more, response.source_truncated) == (False, False)
    assert response.applied_filters.query == "01999"
    assert response.applied_filters.kwalificaties is None


async def test_search_organisaties_accepts_location_filters() -> None:
    html = fixture("search_results_organisatie.html")
    async with connected(searching(html)) as session:
        result = await session.call_tool(
            "agb_search_organisaties", {"plaats": " Teststad ", "postcode": "1234 ab"}
        )

    response = SearchResponse.model_validate(result.structuredContent)
    assert [r.record_type for r in response.results] == ["onderneming", "vestiging"]
    assert response.applied_filters.postcode == "1234AB"
    assert response.applied_filters.plaats == "Teststad"


async def test_search_organisaties_without_a_criterion_is_a_tool_error() -> None:
    async with connected() as session:
        result = await session.call_tool("agb_search_organisaties", {})

    assert result.isError is True
    assert "at least one criterion" in text_of(result)
    assert result.structuredContent is None


async def test_unknown_kwalificatie_is_a_tool_error() -> None:
    async with connected() as session:
        result = await session.call_tool(
            "agb_search_zorgverleners", {"query": "01999", "kwalificaties": ["9999"]}
        )

    assert result.isError is True
    assert "Unknown kwalificatie codes" in text_of(result)


async def test_source_failure_is_a_tool_error_not_an_empty_result() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with connected(failing) as session:
        result = await session.call_tool("agb_search_zorgverleners", {"query": "01999"})

    assert result.isError is True
    message = text_of(result)
    assert "source_unavailable" in message
    assert result.structuredContent is None


async def test_unparseable_page_is_a_tool_error() -> None:
    async with connected(searching("<html><body>Onderhoud</body></html>")) as session:
        result = await session.call_tool("agb_search_zorgverleners", {"query": "01999"})

    assert result.isError is True
    assert "parse_error" in text_of(result)


# --- get_record --------------------------------------------------------------


async def test_get_record_found() -> None:
    pages = {f"{RECORD_BASE}zorgverlener-{ZV_CODE}": (200, RECORD_ZORGVERLENER)}
    async with connected(records(pages)) as session:
        result = await session.call_tool(
            "agb_get_record", {"agbcode": ZV_CODE, "record_type": "zorgverlener"}
        )

    response = RecordResponse.model_validate(result.structuredContent)
    assert response.outcome == "found"
    assert response.record is not None
    assert (response.record.agbcode, response.record.naam) == (ZV_CODE, "A. Testpersoon")
    assert response.record.requested_sections == ["basisregistratie", "kwalificaties"]
    assert response.candidates == []


async def test_get_record_not_found() -> None:
    async with connected(records({})) as session:
        result = await session.call_tool("agb_get_record", {"agbcode": "01999999"})

    response = RecordResponse.model_validate(result.structuredContent)
    assert result.isError is False
    assert response.outcome == "not_found"
    assert response.record is None
    assert response.candidates == []


async def test_get_record_ambiguous_across_types() -> None:
    pages = {
        f"{RECORD_BASE}zorgverlener-{ZV_CODE}": (200, RECORD_ZORGVERLENER),
        f"{RECORD_BASE}onderneming-{ZV_CODE}": (200, RECORD_ONDERNEMING.replace("01999100", ZV_CODE)),
    }
    async with connected(records(pages)) as session:
        result = await session.call_tool("agb_get_record", {"agbcode": ZV_CODE})

    response = RecordResponse.model_validate(result.structuredContent)
    assert response.outcome == "ambiguous"
    assert response.record is None
    assert [c.record_type for c in response.candidates] == ["zorgverlener", "onderneming"]


async def test_get_record_sections_can_be_emptied() -> None:
    pages = {f"{RECORD_BASE}zorgverlener-{ZV_CODE}": (200, RECORD_ZORGVERLENER)}
    async with connected(records(pages)) as session:
        result = await session.call_tool(
            "agb_get_record",
            {"agbcode": ZV_CODE, "record_type": "zorgverlener", "sections": []},
        )

    response = RecordResponse.model_validate(result.structuredContent)
    assert response.record is not None
    assert response.record.requested_sections == []
    assert response.record.basisregistratie is None


async def test_get_record_reports_unavailable_sections() -> None:
    pages = {f"{RECORD_BASE}zorgverlener-{ZV_CODE}": (200, RECORD_ZORGVERLENER)}
    async with connected(records(pages)) as session:
        result = await session.call_tool(
            "agb_get_record",
            {"agbcode": ZV_CODE, "record_type": "zorgverlener", "sections": ["contact", "relaties"]},
        )

    response = RecordResponse.model_validate(result.structuredContent)
    assert response.record is not None
    assert response.record.unavailable_sections == ["contact"]
    assert response.record.relaties is not None


async def test_get_record_probe_failure_is_a_tool_error() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with connected(failing) as session:
        result = await session.call_tool("agb_get_record", {"agbcode": ZV_CODE})

    assert result.isError is True
    assert "source_unavailable" in text_of(result)
    assert "not_found" not in text_of(result)


async def test_get_record_vestiging_resolves_from_the_slug_url() -> None:
    code = "71999001"
    pages = {f"{RECORD_BASE}vestiging-{encode_vestiging_slug(code)}": (200, fixture("record_vestiging.html"))}
    async with connected(records(pages)) as session:
        result = await session.call_tool(
            "agb_get_record", {"agbcode": code, "record_type": "vestiging"}
        )

    response = RecordResponse.model_validate(result.structuredContent)
    assert response.outcome == "found"
    assert response.record is not None
    assert response.record.agbcode == code


# --- lookup and resource -----------------------------------------------------


async def test_lookup_codes_needs_no_network() -> None:
    async with connected() as session:  # any request would fail the test
        listing = await session.call_tool("agb_lookup_codes", {"entity_kind": "zorgverlener"})
        narrowed = await session.call_tool(
            "agb_lookup_codes", {"entity_kind": "zorgverlener", "zorgsoort": "01"}
        )
        searched = await session.call_tool(
            "agb_lookup_codes", {"entity_kind": "organisatie", "query": "huisarts"}
        )

    assert listing.isError is False
    kinds = {match["kind"] for match in listing.structuredContent["results"]}
    assert kinds == {"zorgsoort"}
    assert {match["code"] for match in narrowed.structuredContent["results"]} == {
        "0101",
        "0102",
        "0103",
        "0110",
    }
    assert all(
        "huisarts" in match["naam"].casefold() or "huisarts" in match["code"]
        for match in searched.structuredContent["results"]
    )


async def test_lookup_codes_unknown_zorgsoort_is_a_tool_error() -> None:
    async with connected() as session:
        result = await session.call_tool(
            "agb_lookup_codes", {"entity_kind": "zorgverlener", "zorgsoort": "99"}
        )

    assert result.isError is True
    assert "Unknown zorgsoort" in text_of(result)


async def test_codes_resource_reads() -> None:
    async with connected() as session:
        result = await session.read_resource(types.AnyUrl("vektis://codes/zorgverlener"))

    assert len(result.contents) == 1
    table = json.loads(result.contents[0].text)  # type: ignore[union-attr]
    assert table["01"]["naam"] == "Huisartsen"
    assert table["01"]["kwalificaties"]["0101"] == "Huisarts"


async def test_codes_resource_rejects_an_unknown_side() -> None:
    async with connected() as session:
        with pytest.raises(Exception, match="Unknown entity_kind"):
            await session.read_resource(types.AnyUrl("vektis://codes/ziekenhuis"))


# --- streamable http ---------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_streamable_http_transport_lists_tools() -> None:
    uvicorn = pytest.importorskip("uvicorn", reason="the SDK's HTTP runner needs uvicorn")

    port = free_port()
    server.configure(httpx.MockTransport(no_network))
    config = uvicorn.Config(
        server.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    http = uvicorn.Server(config)
    serving = asyncio.create_task(http.serve())
    try:
        for _ in range(100):
            if http.started:
                break
            await asyncio.sleep(0.05)
        if not http.started:
            pytest.skip("the HTTP runner did not come up in time")

        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool("agb_lookup_codes", {"entity_kind": "organisatie"})
    finally:
        http.should_exit = True
        await serving
        server.configure(None)

    assert [tool.name for tool in listed.tools] == list(EXPECTED_PROPERTIES)
    assert result.isError is False


# --- stdio subprocess --------------------------------------------------------


async def test_stdio_subprocess_speaks_only_mcp_on_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`uv run vektis-mcp` must start on real env defaults with stdout clean.

    Nothing here mocks the transport, so this also proves the lifespan performs
    no network I/O at startup. Any banner, warning or log line on stdout would
    break the client's JSON-RPC framing and fail `initialize` below; logs are
    checked to be on stderr instead.
    """
    if shutil.which("uv") is None:  # pragma: no cover - depends on the sandbox
        pytest.skip("uv is not on PATH")
    # Real defaults: no VEKTIS_* overrides at all.
    monkeypatch.delenv("VEKTIS_REQUEST_INTERVAL_SECONDS", raising=False)

    errlog = tmp_path / "stderr.log"
    params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", str(PROJECT_ROOT), "vektis-mcp"],
        cwd=str(PROJECT_ROOT),
    )
    with errlog.open("w", encoding="utf-8") as stream:
        async with stdio_client(params, errlog=stream) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = await session.list_tools()

    assert init.serverInfo.name == "vektis"
    assert [tool.name for tool in listed.tools] == list(EXPECTED_PROPERTIES)
    # The startup log line proves logging went to stderr, not stdout.
    assert "event=startup" in errlog.read_text(encoding="utf-8")


# --- pacing is per process, not per session ----------------------------------


async def test_lifespans_on_one_loop_share_the_pacer() -> None:
    """Over Streamable HTTP the SDK enters the lifespan once per session, so a
    pacer built inside it would give each connected client its own budget."""
    async with server.lifespan(server.mcp) as first, server.lifespan(server.mcp) as second:
        assert first.pacer is second.pacer
