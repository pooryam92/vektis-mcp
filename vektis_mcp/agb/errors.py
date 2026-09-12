"""Domain errors for AGB source access, translated to MCP errors by the adapters."""

from __future__ import annotations


class AgbError(Exception):
    """Base class for every AGB failure."""


class InvalidInput(AgbError):
    """Caller input is unusable: unknown code, missing criterion, bad combination."""


class SourceUnavailable(AgbError):
    """The source could not be reached: network failure, timeout, exhausted 5xx/429, deadline."""


class SourceBlocked(AgbError):
    """The source refused the client: 403 or a challenge page."""


class SessionExpired(AgbError):
    """Internal: 419 or a Laravel 'Page Expired' 403. The client restarts once."""


class ParseError(AgbError):
    """The page was fetched but not understood, or its identity did not match."""
