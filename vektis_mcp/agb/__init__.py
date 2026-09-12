"""AGB-register source access: constants, errors and internal types."""

from __future__ import annotations

from .constants import BASE_URL, CODE_LENGTH, FORM_URL, RENDER_CAP, RESULTS_URL
from .errors import (
    AgbError,
    InvalidInput,
    ParseError,
    SessionExpired,
    SourceBlocked,
    SourceUnavailable,
)
from .types import ParsedSearchPage, RecordIdentity, SearchCriteria, SourcePage

__all__ = [
    "BASE_URL",
    "CODE_LENGTH",
    "FORM_URL",
    "RENDER_CAP",
    "RESULTS_URL",
    "AgbError",
    "InvalidInput",
    "ParseError",
    "SessionExpired",
    "SourceBlocked",
    "SourceUnavailable",
    "ParsedSearchPage",
    "RecordIdentity",
    "SearchCriteria",
    "SourcePage",
]
