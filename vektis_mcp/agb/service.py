"""Validation, orchestration and response assembly for the AGB tools.

This is the only layer that knows both the public contract (docs/tools.md) and the
source's quirks. It holds no I/O of its own: the client fetches, the parsers
read HTML, and everything here is either a pure decision or an ``await`` on the
client.

Three rules from the design are implemented literally, because getting them
wrong would make the server lie about the register:

* ``has_more`` reports only *known* additional matching rows (rows we parsed and
  dropped at the limit), while ``source_truncated`` separately reports that the
  source's 500-row render cap hid matches we never saw. Neither promises
  pagination.
* An unrecognized or inconsistent results page is a `ParseError`. Zero results
  need the source's own "no results" page; a count that disagrees with the row
  count below the cap, or a full-cap page with no count and no cap marker, is a
  source/parser failure rather than a quiet "complete" answer.
* Only a recognized absence (a 404 or the rendered not-found page) proves a
  record does not exist. Any client error propagates, so an outage can never
  surface as ``not_found`` or as a falsely unambiguous match.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .. import codes
from ..models import (
    AppliedFilters,
    Record,
    RecordResponse,
    RecordSection,
    RecordType,
    SearchResponse,
    SearchResult,
    Side,
)
from .client import AgbClient
from .constants import BASE_URL, RENDER_CAP
from .errors import InvalidInput, ParseError
from .parsers import (
    DEFAULT_SECTIONS,
    classify_record_page,
    encode_vestiging_slug,
    parse_record,
    parse_record_identity,
    parse_search,
)
from .slugs import RECORD_TYPES
from .types import ParsedSearchPage, RecordIdentity, SearchCriteria, SourcePage

__all__ = ["AgbService", "build_search_criteria", "record_url"]

logger = logging.getLogger("vektis_mcp.agb.service")

#: Detail pages live directly under this prefix: ``<prefix><type>-<code|slug>``.
RECORD_URL_PREFIX = f"{BASE_URL}/agb-register/"

#: Fields the source only honours on the organisation tab (``fieldset#tab2`` is
#: disabled on the zorgverlener tab, so a browser never sends them there).
_ORGANISATIE_ONLY = ("plaats", "postcode", "kvknummer")


# --- input normalization ----------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Trim surrounding whitespace; an empty or blank string is no criterion."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _clean_postcode(value: str | None) -> str | None:
    """Normalize to the source's own format: upper-case, no spaces (``1234AB``)."""
    trimmed = _clean(value)
    if trimmed is None:
        return None
    return "".join(trimmed.split()).upper() or None


def build_search_criteria(
    side: Side,
    query: str | None = None,
    zorgsoort: str | None = None,
    kwalificaties: Sequence[str] | None = None,
    plaats: str | None = None,
    postcode: str | None = None,
    kvknummer: str | None = None,
    include_ended: bool = False,
) -> SearchCriteria:
    """Normalize and validate search inputs before any network I/O.

    Raises `InvalidInput` for an unknown side or code, for location filters on
    the zorgverlener side, for an empty individual query, and for an
    organisation search with no criterion at all. ``limit`` and
    ``include_ended`` are deliberately not criteria: they narrow or widen a
    result set, they do not select one.
    """
    if side not in ("zorgverlener", "organisatie"):
        raise InvalidInput(f"Unknown side {side!r}; expected zorgverlener or organisatie.")

    cleaned = {
        "query": _clean(query),
        "zorgsoort": _clean(zorgsoort),
        "plaats": _clean(plaats),
        "postcode": _clean_postcode(postcode),
        "kvknummer": _clean(kvknummer),
    }

    if side == "zorgverlener":
        supplied = [name for name in _ORGANISATIE_ONLY if cleaned[name] is not None]
        if supplied:
            raise InvalidInput(
                f"{', '.join(supplied)} {'applies' if len(supplied) == 1 else 'apply'} to "
                "organisation search only; use agb_search_organisaties."
            )

    # Both checked against the chosen side's table, and the qualifications
    # against their zorgsoort parent when one was given.
    zorgsoort_code = codes.validate_zorgsoort(side, cleaned["zorgsoort"])
    kwalificatie_codes = codes.validate_kwalificaties(side, zorgsoort_code, kwalificaties)

    if side == "zorgverlener":
        if cleaned["query"] is None:
            raise InvalidInput("query is required: pass an AGB-code or a name to search for.")
    elif not any((cleaned["query"], *(cleaned[name] for name in _ORGANISATIE_ONLY), zorgsoort_code, kwalificatie_codes)):
        raise InvalidInput(
            "Organisation search needs at least one criterion: query, plaats, postcode, "
            "kvknummer, zorgsoort or kwalificaties."
        )

    return SearchCriteria(
        side=side,
        query=cleaned["query"],
        zorgsoort=zorgsoort_code,
        kwalificaties=kwalificatie_codes,
        plaats=cleaned["plaats"],
        postcode=cleaned["postcode"],
        kvknummer=cleaned["kvknummer"],
        include_ended=include_ended,
    )


def record_url(record_type: RecordType, tail: str) -> str:
    """The detail URL for one record type and one code-or-slug tail."""
    return f"{RECORD_URL_PREFIX}{record_type}-{tail}"


# --- service ---------------------------------------------------------------


class AgbService:
    """Runs one search or one exact lookup against the public AGB-register."""

    def __init__(self, client: AgbClient) -> None:
        self._client = client

    # -- search ------------------------------------------------------------

    async def search(self, criteria: SearchCriteria, limit: int) -> SearchResponse:
        """Fetch one results page and turn it into the public search response."""
        page = await self._client.search(criteria)
        parsed = parse_search(page)
        self._check_completeness(parsed)

        deduped = _dedupe_rows(parsed.rows)
        matching = [row for row in deduped if criteria.include_ended or not row.einddatum]
        results = matching[:limit]

        response = SearchResponse(
            results=results,
            returned_count=len(results),
            source_total=parsed.source_total,
            applied_filters=_applied_filters(criteria),
            has_more=len(matching) > limit,
            source_truncated=_source_truncated(parsed),
        )
        logger.info(
            "op=search side=%s rows=%d deduped=%d matching=%d returned=%d "
            "source_total=%s truncated=%s",
            criteria.side,
            parsed.raw_row_count,
            len(deduped),
            len(matching),
            response.returned_count,
            parsed.source_total,
            response.source_truncated,
        )
        return response

    @staticmethod
    def _check_completeness(parsed: ParsedSearchPage) -> None:
        """Reject pages whose count and rows cannot both be true.

        Below the render cap the source renders every match, so a count that
        differs from the rendered row count means the page was misread (or
        changed shape) — never that results were truncated. At the cap, a page
        with neither a count nor the ``#500PlusAlert`` marker carries no evidence
        either way, so it must not be reported as a complete result set.
        """
        if parsed.cap_marker:
            return
        if parsed.raw_row_count >= RENDER_CAP:
            if parsed.source_total is None:
                raise ParseError(
                    f"search page rendered {parsed.raw_row_count} rows at the {RENDER_CAP}-row "
                    "cap with no result count and no cap marker: completeness is indeterminate"
                )
            return
        if parsed.source_total is not None and parsed.source_total != parsed.raw_row_count:
            raise ParseError(
                f"search page claims {parsed.source_total} results but rendered "
                f"{parsed.raw_row_count} rows below the {RENDER_CAP}-row cap"
            )

    # -- exact record ------------------------------------------------------

    async def get_record(
        self,
        agbcode: str,
        record_type: RecordType | None = None,
        sections: Sequence[RecordSection] | None = None,
    ) -> RecordResponse:
        """Resolve one AGB-code to a record, an absence, or an ambiguity.

        Probes detail URLs directly (search is unusable for this: an onderneming
        code also matches its vestigingen). Probes run one after another through
        the paced client, so a lookup costs at most four requests (three
        types, plus the encoded vestiging form after a plain-digit absence).
        """
        code = (agbcode or "").strip()
        if not (len(code) == 8 and code.isascii() and code.isdigit()):
            raise InvalidInput(f"agbcode must be eight digits, got {agbcode!r}.")
        if record_type is not None and record_type not in RECORD_TYPES:
            raise InvalidInput(
                f"Unknown record_type {record_type!r}; expected zorgverlener, onderneming or vestiging."
            )
        wanted = list(DEFAULT_SECTIONS) if sections is None else list(sections)

        found: list[tuple[RecordIdentity, SourcePage]] = []
        for probe_type in RECORD_TYPES if record_type is None else (record_type,):
            match = await self._probe(probe_type, record_url(probe_type, code), code)
            if match is None and probe_type == "vestiging":
                # Both vestiging URL forms answer 200 without redirecting
                # (docs/agb-source-notes.md §3), so the plain-digit form is
                # enough whenever it resolves. The encoded form is only tried
                # after a *recognized* absence, in case this deployment stops
                # serving plain digits for some code ranges; a match on either
                # form is the same record, and the dedupe below keeps it one.
                match = await self._probe(
                    "vestiging",
                    record_url("vestiging", encode_vestiging_slug(code)),
                    code,
                )
            if match is not None:
                found.append(match)

        unique = _dedupe_identities(found)
        logger.info(
            "op=get_record probed=%s matches=%d",
            record_type or "all",
            len(unique),
        )
        if not unique:
            return RecordResponse(outcome="not_found")
        if len(unique) == 1:
            identity, page = unique[0]
            # The page is already in hand: sections are parsed from it, never
            # re-fetched.
            record: Record = parse_record(page, identity, wanted)
            return RecordResponse(outcome="found", record=record)
        return RecordResponse(
            outcome="ambiguous",
            candidates=[
                SearchResult(
                    record_type=identity.record_type,
                    naam=identity.naam,
                    agbcode=identity.agbcode,
                    source_url=page.url,
                )
                for identity, page in unique
            ],
        )

    async def _probe(
        self,
        record_type: RecordType,
        url: str,
        code: str,
    ) -> tuple[RecordIdentity, SourcePage] | None:
        """Fetch one candidate URL. ``None`` means *recognized* absence only.

        Every other outcome raises: a client error (network, 5xx, block,
        deadline) propagates untouched, and a page that does not verify as the
        requested code and type is a `ParseError`. Neither may be read as "this
        record does not exist".
        """
        page = await self._client.fetch_record(url)
        if classify_record_page(page) == "not_found":
            return None
        identity = parse_record_identity(page, record_type, code)
        return identity, page


# --- pure helpers ----------------------------------------------------------


def _dedupe_rows(rows: Sequence[SearchResult]) -> list[SearchResult]:
    """Drop repeated ``(record_type, agbcode)`` rows, preserving source order."""
    seen: set[tuple[str, str]] = set()
    unique: list[SearchResult] = []
    for row in rows:
        key = (row.record_type, row.agbcode)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _dedupe_identities(
    matches: Sequence[tuple[RecordIdentity, SourcePage]],
) -> list[tuple[RecordIdentity, SourcePage]]:
    """Collapse probes that resolved to the same record, keeping probe order."""
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[RecordIdentity, SourcePage]] = []
    for identity, page in matches:
        key = (identity.record_type, identity.agbcode)
        if key in seen:
            continue
        seen.add(key)
        unique.append((identity, page))
    return unique


def _source_truncated(parsed: ParsedSearchPage) -> bool:
    """Whether the render cap hid matches from us.

    The explicit ``#500PlusAlert`` marker is authoritative. The count-above-rows
    comparison is kept as a fallback, and only at the cap: below it the source
    renders everything, so a mismatch there is a parse failure (see
    `AgbService._check_completeness`), not truncation.
    """
    if parsed.cap_marker:
        return True
    return (
        parsed.source_total is not None
        and parsed.source_total > parsed.raw_row_count
        and parsed.raw_row_count >= RENDER_CAP
    )


def _applied_filters(criteria: SearchCriteria) -> AppliedFilters:
    """The normalized inputs that were actually sent, echoed back to the caller."""
    return AppliedFilters(
        query=criteria.query,
        zorgsoort=criteria.zorgsoort,
        kwalificaties=list(criteria.kwalificaties) or None,
        plaats=criteria.plaats,
        postcode=criteria.postcode,
        kvknummer=criteria.kvknummer,
        include_ended=criteria.include_ended,
    )
