"""Vektis MCP server: AGB-register tools for now; see ROADMAP.md for the rest.

This module owns the public MCP surface and nothing else. The tool schemas below
are the contract (see docs/tools.md); the bodies only
normalize input, call `AgbService`, and translate domain errors into MCP tool
errors. Validated settings, the process-wide pacer, the source client and the
service are built in the FastMCP lifespan — never at import time — so importing
this module never touches the network and both transports share one pacer.

Run:
    uv run vektis-mcp
    uv run vektis-mcp --transport streamable-http --host 127.0.0.1 --port 8000

With stdio the MCP client launches this as a subprocess: stdout carries protocol
messages only and every log line goes to stderr (configured in cli.py). Over
Streamable HTTP the SDK's own runner serves the MCP endpoint at /mcp.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from . import codes
from .agb.client import AgbClient
from .agb.errors import InvalidInput, ParseError, SourceBlocked, SourceUnavailable
from .agb.pacing import Pacer
from .agb.service import AgbService, build_search_criteria
from .codes import (
    ZORGSOORT_ORGANISATIE_DESC,
    ZORGSOORT_ZORGVERLENER_DESC,
    ZorgsoortOrganisatie,
    ZorgsoortZorgverlener,
)
from .models import CodeLookupResponse, RecordResponse, RecordSection, RecordType, SearchResponse, Side
from .settings import Settings

logger = logging.getLogger("vektis_mcp.server")

#: Prefix added to the message of every source-side failure, so an MCP client can
#: tell a transient outage from a block from a layout change without parsing prose.
_ERROR_CATEGORY = {
    SourceUnavailable: "source_unavailable",
    SourceBlocked: "source_blocked",
    ParseError: "parse_error",
}


# --- lifespan ----------------------------------------------------------------


@dataclass(frozen=True)
class AppContext:
    """What one server process shares across every tool call."""

    settings: Settings
    pacer: Pacer
    service: AgbService


#: Test seam. `configure(transport=...)` installs an `httpx` transport that the
#: lifespan hands to the `AgbClient`, which is how the tool tests drive the real
#: server against fixtures. Left None in production, so the client builds its own
#: transport per transaction.
_transport_override: httpx.AsyncBaseTransport | None = None


def configure(transport: httpx.AsyncBaseTransport | None) -> None:
    """Install (or clear) the HTTP transport the next lifespan will use."""
    global _transport_override
    _transport_override = transport


#: The process-wide pacer, keyed by the event loop it was built on. Over
#: Streamable HTTP the SDK enters the lifespan once per *session*, not once per
#: process, so building the pacer inside the lifespan would hand every connected
#: client its own request budget. Keying by loop keeps one pacer per process in
#: production (one loop) while letting each test's fresh loop start clean.
_shared_pacer: tuple[asyncio.AbstractEventLoop, Pacer] | None = None


def _process_pacer(settings: Settings) -> Pacer:
    """Return the pacer every session on this event loop shares."""
    global _shared_pacer
    loop = asyncio.get_running_loop()
    if _shared_pacer is None or _shared_pacer[0] is not loop:
        _shared_pacer = (loop, Pacer.from_settings(settings))
    elif _shared_pacer[1].interval_seconds != settings.request_interval_seconds:
        logger.warning(
            "event=pacer_reused configured_interval=%.2fs active_interval=%.2fs",
            settings.request_interval_seconds,
            _shared_pacer[1].interval_seconds,
        )
    return _shared_pacer[1]


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Build settings, the shared pacer and one service for this session.

    Settings are validated here, so a misconfigured environment fails at startup
    rather than on the first tool call. Nothing in here performs I/O: the client
    opens a transaction-scoped `httpx.AsyncClient` per request instead. The
    pacer comes from `_process_pacer`, so concurrent HTTP sessions share it.
    """
    settings = Settings.from_env()
    pacer = _process_pacer(settings)
    service = AgbService(AgbClient(settings, pacer, transport=_transport_override))
    logger.info(
        "event=startup interval=%.2fs timeout=%.0fs deadline=%.0fs retries=%d",
        settings.request_interval_seconds,
        settings.http_timeout_seconds,
        settings.tool_deadline_seconds,
        settings.max_retries,
    )
    try:
        yield AppContext(settings=settings, pacer=pacer, service=service)
    finally:
        logger.info("event=shutdown")


mcp = FastMCP(
    "vektis",
    instructions=(
        "Search and read the public Vektis AGB-register. "
        "agb_search_zorgverleners finds individual care providers; "
        "agb_search_organisaties finds ondernemingen (legal entities) and vestigingen (locations). "
        "Follow up with agb_get_record for a profile and optional detail sections. "
        "When source_truncated is true, narrow with zorgsoort or organisation location filters. Search returns candidates, not confirmed identities. Use agb_lookup_codes to discover filter codes."
    ),
    streamable_http_path="/mcp",
    lifespan=lifespan,
)


def _service(ctx: Context) -> AgbService:
    """The process-wide service, from the lifespan context."""
    app: AppContext = ctx.request_context.lifespan_context
    return app.service


@contextmanager
def _translated_errors() -> Iterator[None]:
    """Map domain errors onto the MCP tool-error contract.

    Unusable input becomes a `ValueError`; a source outage, a block or a page we
    could not parse becomes a category-prefixed `ToolError`. FastMCP turns either
    into an `isError` result whose text carries our message, so the distinction is
    for readers of this module, not for the wire. What matters is that none of
    these ever returns an empty result set or a `not_found` record.
    """
    try:
        yield
    except InvalidInput as exc:
        raise ValueError(str(exc)) from exc
    except (SourceUnavailable, SourceBlocked, ParseError) as exc:
        raise ToolError(f"{_ERROR_CATEGORY.get(type(exc), 'source_error')}: {exc}") from exc


# --- search: zorgverleners ---------------------------------------------------


@mcp.tool()
async def agb_search_zorgverleners(
    ctx: Context,
    query: str = Field(min_length=1, pattern=r"\S", description="AGB-code or name. Digits match as a substring of the code, letters match the name."),
    zorgsoort: ZorgsoortZorgverlener | None = Field(None, description=ZORGSOORT_ZORGVERLENER_DESC),
    kwalificaties: list[str] | None = Field(None, description="Kwalificatie codes to filter on, e.g. ['0101']. See agb_lookup_codes."),
    include_ended: bool = Field(False, description="Keep records that have an einddatum."),
    limit: int = Field(25, ge=1, le=500),
) -> SearchResponse:
    """Search the AGB-register for individual zorgverleners (huisartsen, tandartsen, fysiotherapeuten, ...).

    Results carry initials-only names and no address; use agb_get_record for detail sections.
    """
    with _translated_errors():
        criteria = build_search_criteria(
            "zorgverlener",
            query=query,
            zorgsoort=zorgsoort,
            kwalificaties=kwalificaties,
            include_ended=include_ended,
        )
        return await _service(ctx).search(criteria, limit)


# --- search: ondernemingen en vestigingen ------------------------------------


@mcp.tool()
async def agb_search_organisaties(
    ctx: Context,
    query: str | None = Field(None, description="AGB-code or name. Digits match as a substring of the code, letters match the name. May be omitted when filtering on plaats, postcode or kvknummer."),
    zorgsoort: ZorgsoortOrganisatie | None = Field(None, description=ZORGSOORT_ORGANISATIE_DESC),
    kwalificaties: list[str] | None = Field(None, description="Kwalificatie codes to filter on, e.g. ['0100']. See agb_lookup_codes."),
    plaats: str | None = Field(None, description="City name."),
    postcode: str | None = Field(None, description="Four digits, optionally followed by two letters, e.g. '9403' or '9403AA'.", pattern=r"^[1-9][0-9]{3}\s?([A-Za-z]{2})?$"),
    kvknummer: str | None = Field(None, description="KvK-nummer, up to 12 digits.", pattern=r"^[0-9]{1,12}$"),
    include_ended: bool = Field(False, description="Keep records that have an einddatum."),
    limit: int = Field(25, ge=1, le=500),
) -> SearchResponse:
    """Search the AGB-register for ondernemingen (businesses) and vestigingen (locations).

    Require at least one non-empty query, location, KvK, zorgsoort or kwalificatie criterion.
    Results include record_type and an address when available.
    """
    with _translated_errors():
        criteria = build_search_criteria(
            "organisatie",
            query=query,
            zorgsoort=zorgsoort,
            kwalificaties=kwalificaties,
            plaats=plaats,
            postcode=postcode,
            kvknummer=kvknummer,
            include_ended=include_ended,
        )
        return await _service(ctx).search(criteria, limit)


# --- detail -------------------------------------------------------------------


@mcp.tool()
async def agb_get_record(
    ctx: Context,
    agbcode: str = Field(description="Eight-digit AGB-code.", pattern=r"^[0-9]{8}$"),
    record_type: RecordType | None = Field(
        None,
        description="zorgverlener (person), onderneming (business) or vestiging (location). "
        "Pass the type from search. If omitted, return alternatives when ambiguous; never choose the first match.",
    ),
    sections: list[RecordSection] | None = Field(
        None,
        description="Requested sections. Omitted means basisregistratie and kwalificaties; [] means identity and status only.",
    ),
) -> RecordResponse:
    """Retrieve one exact AGB registration, preserving leading zeros.

    Return found, not_found or ambiguous. For ambiguous, return candidates to retry
    with record_type. A found record includes identity, status, source_url and
    retrieved_at. Source failures are tool errors, never not_found responses.
    """
    with _translated_errors():
        return await _service(ctx).get_record(agbcode, record_type, sections)


# --- lookup -------------------------------------------------------------------


@mcp.tool()
def agb_lookup_codes(
    entity_kind: Side = Field(description="zorgverlener for people or organisatie for businesses and locations."),
    zorgsoort: str | None = Field(None, description="Optional two-digit parent zorgsoort code.", pattern=r"^[0-9]{2}$"),
    query: str | None = Field(None, description="Optional code or label substring, matched case-insensitively. Return all matches; never guess an ambiguous label."),
) -> CodeLookupResponse:
    """Discover zorgsoort and kwalificatie filter codes and labels.

    Without filters, list zorgsoorten. With zorgsoort, list its kwalificaties.
    With query alone, search both levels. With both, search kwalificaties under
    that zorgsoort. Each kwalificatie includes its parent_code. Unknown parents
    are input errors; an unmatched query returns an empty results list.
    """
    # Bundled JSON only: no lifespan context and no network needed.
    with _translated_errors():
        return codes.lookup_codes(entity_kind, zorgsoort, query)


@mcp.resource("vektis://codes/{side}")
def codes_resource(side: str) -> str:
    """Zorgsoorten and their kwalificaties for one side of the register, as JSON."""
    with _translated_errors():
        return json.dumps(codes.side_codes(side), ensure_ascii=False)  # type: ignore[arg-type]


__all__ = ["AppContext", "configure", "lifespan", "mcp"]
