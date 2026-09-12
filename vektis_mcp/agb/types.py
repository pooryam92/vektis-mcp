"""Internal value objects passed between the client, the parsers and the service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..models import RecordType, SearchResult, Side


@dataclass(frozen=True)
class SearchCriteria:
    """Normalized, validated search inputs. Location fields are organisatie-only."""

    side: Side
    query: str | None = None
    zorgsoort: str | None = None
    kwalificaties: tuple[str, ...] = ()
    plaats: str | None = None
    postcode: str | None = None
    kvknummer: str | None = None
    include_ended: bool = False


@dataclass(frozen=True)
class SourcePage:
    """One fetched page: its HTML, the URL it ended on, and when it was fetched."""

    html: str
    url: str
    fetched_at: datetime
    status_code: int


@dataclass(frozen=True)
class ParsedSearchPage:
    """Parsed search results plus the evidence needed to judge completeness."""

    rows: list[SearchResult]
    source_total: int | None
    raw_row_count: int
    is_empty: bool
    cap_marker: bool


@dataclass(frozen=True)
class RecordIdentity:
    """Identity read off a detail page, checked against what was requested."""

    record_type: RecordType
    agbcode: str
    naam: str
