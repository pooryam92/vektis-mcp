"""Async source client for the public AGB-register.

Mechanics only: pacing, transaction-scoped cookies, the Laravel CSRF token,
explicitly followed and bounded redirects, one restart on session expiry,
bounded retries with backoff, and a total deadline. Nothing here parses a page
beyond what the control flow needs; that is ``parsers.py``'s job.

Field names, encodings and status codes are the ones verified live on
2026-09-12 and recorded in ``docs/agb-source-notes.md``:

* the CSRF token is the ``_token`` *form input* (the ``<head>`` meta tag holds a
  different value and must not be used);
* the search POST answers ``302`` with an absolute ``Location`` carrying a
  ``#resultaten`` fragment, and the follow-up GET needs the transaction cookies
  because the query lives in the session;
* a bare GET of the results URL (no POST in the session) redirects back to the
  form, which is a failed transaction, not an empty result;
* ``kwalificaties[]`` is a repeated bracketed key, not a joined list;
* ``g-recaptcha-response`` is not enforced and is omitted;
* an expired token/session answers ``419`` with Laravel's "Page Expired" page;
* detail pages are sessionless and answer ``404`` for an unknown record.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import anyio
import httpx
from selectolax.parser import HTMLParser

from ..settings import Settings
from .constants import BASE_URL, FORM_URL, RESULTS_URL
from .errors import (
    AgbError,
    InvalidInput,
    ParseError,
    SessionExpired,
    SourceBlocked,
    SourceUnavailable,
)
from .pacing import Pacer
from .types import SearchCriteria, SourcePage

__all__ = ["AgbClient", "Pacer"]

logger = logging.getLogger("vektis_mcp.agb.client")

#: Hops the client is willing to follow itself; the source needs exactly one.
MAX_REDIRECTS = 3
#: First backoff step; doubles per retry and gets a little jitter on top.
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_JITTER_FRACTION = 0.25
#: Generic browser headers; nothing here identifies the tool.
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_LANGUAGE = "nl-NL,nl;q=0.9,en;q=0.8"

SOURCE_SCHEME = urlsplit(BASE_URL).scheme
SOURCE_HOST = urlsplit(BASE_URL).netloc
RECORD_PATH_PREFIX = "/agb-register/"

SESSION_EXPIRED_STATUS = 419
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
#: Laravel's expiry page; also seen as a 403 body on some deployments.
EXPIRED_MARKER = "page expired"
#: Conservative challenge markers. Deliberately excludes reCAPTCHA, which the
#: real search form mounts on every response without enforcing it.
CHALLENGE_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "cf-browser-verification",
    "__cf_chl_",
    "checking if the site connection is secure",
)


#: MCP search side -> the source's ``zorgpartijtype`` radio value.
ZORGPARTIJTYPE = {
    "zorgverlener": "zorgverlener",
    "organisatie": "onderneming,vestiging",
}


class _Transient(Exception):
    """Internal: this attempt failed in a way a fresh transaction may survive."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


def _bare(url: str) -> str:
    """Drop the fragment (and nothing else) from an absolute URL."""
    split = urlsplit(url)
    return urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))


class AgbClient:
    """Fetches form, results and detail pages from the public AGB-register.

    Every call opens its own ``httpx.AsyncClient`` so cookies and the CSRF
    token stay confined to one transaction, and closes it on success, failure
    and cancellation. The client holds no session state between calls.
    """

    def __init__(
        self,
        settings: Settings,
        pacer: Pacer,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._pacer = pacer
        self._transport = transport
        # Share the pacer's time sources so backoff and the deadline move with
        # pacing; tests inject a fake clock once and the whole client follows.
        self._clock = pacer.clock
        self._sleep = pacer.sleep

    # -- public API ----------------------------------------------------------

    async def search(self, criteria: SearchCriteria) -> SourcePage:
        """Run one search and return the rendered results page."""
        if criteria.side not in ZORGPARTIJTYPE:
            raise InvalidInput(f"unknown search side {criteria.side!r}")
        return await self._run(
            "search",
            lambda attempt, deadline: self._search_once(criteria, attempt, deadline),
        )

    async def fetch_record(self, url: str) -> SourcePage:
        """Fetch one detail page. A ``404`` is returned, not raised."""
        target = self._source_url(
            url,
            error=InvalidInput,
            what="detail URL",
            require_record_path=True,
        )
        return await self._run(
            "fetch_record",
            lambda attempt, deadline: self._fetch_record_once(target, attempt, deadline),
        )

    # -- attempt loop --------------------------------------------------------

    async def _run(
        self,
        op: str,
        transaction: Callable[[int, float], Awaitable[SourcePage]],
    ) -> SourcePage:
        """Run ``transaction`` with retries, one expiry restart and a deadline."""
        started = self._clock()
        deadline = started + self._settings.tool_deadline_seconds
        attempt = 0
        retries_used = 0
        expiry_restarts = 0
        while True:
            attempt += 1
            try:
                page = await transaction(attempt, deadline)
            except SessionExpired as exc:
                if expiry_restarts >= 1:
                    raise SourceUnavailable(
                        f"{op}: the source expired the session twice in a row "
                        f"after {attempt} attempts ({exc})"
                    ) from exc
                expiry_restarts += 1
                logger.info(
                    "op=%s event=session_expired attempt=%d restarting=1",
                    op,
                    attempt,
                )
                continue
            except _Transient as exc:
                if retries_used >= self._settings.max_retries:
                    raise SourceUnavailable(
                        f"{op}: no usable response after {attempt} attempts "
                        f"({exc.message})"
                    ) from exc
                retries_used += 1
                logger.info(
                    "op=%s event=retry attempt=%d retries_used=%d reason=%s",
                    op,
                    attempt,
                    retries_used,
                    exc.message,
                )
                await self._backoff(retries_used, exc.retry_after, deadline, op)
                continue
            logger.info(
                "op=%s event=ok attempts=%d status=%d latency_ms=%d",
                op,
                attempt,
                page.status_code,
                int((self._clock() - started) * 1000),
            )
            return page

    async def _backoff(
        self,
        retries_used: int,
        retry_after: float | None,
        deadline: float,
        op: str,
    ) -> None:
        """Sleep before the next attempt, never past the deadline."""
        if retry_after is not None:
            delay = retry_after
        else:
            base = BACKOFF_BASE_SECONDS * (2 ** (retries_used - 1))
            delay = base + random.uniform(0, base * BACKOFF_JITTER_FRACTION)
        remaining = self._remaining(deadline)
        if delay >= remaining:
            raise SourceUnavailable(
                f"{op}: the {self._settings.tool_deadline_seconds:g}s deadline leaves "
                f"no room for a {delay:.1f}s backoff"
            )
        logger.info("op=%s event=backoff seconds=%.2f", op, delay)
        await self._sleep(delay)

    # -- transactions --------------------------------------------------------

    async def _search_once(
        self,
        criteria: SearchCriteria,
        attempt: int,
        deadline: float,
    ) -> SourcePage:
        """One complete search transaction: form GET, POST, bounded redirects."""
        async with self._transaction() as client:
            form = await self._request(
                client,
                "GET",
                FORM_URL,
                op="search",
                step="form",
                attempt=attempt,
                deadline=deadline,
            )
            token = self._extract_token(form.text)
            posted = await self._request(
                client,
                "POST",
                RESULTS_URL,
                op="search",
                step="post",
                attempt=attempt,
                deadline=deadline,
                data=self.search_fields(criteria, token),
                referer=FORM_URL,
            )
            if posted.is_redirect:
                location = _bare(urljoin(str(posted.request.url), posted.headers["location"]))
                if location == FORM_URL:
                    # Spend no request on it: the form is where a session that
                    # never received our query gets sent.
                    raise SessionExpired("the search POST redirected back to the search form")
            final = await self._follow(
                client,
                posted,
                op="search",
                attempt=attempt,
                deadline=deadline,
                referer=FORM_URL,
            )
            final_url = _bare(str(final.request.url))
            if final_url == FORM_URL:
                # A bare results GET redirects to the form; reaching the form
                # means the session never carried our query.
                raise SessionExpired("the search redirected back to the search form")
            if final.status_code != 200:
                raise SourceUnavailable(
                    f"search: the results page answered {final.status_code}"
                )
            return self._page(final, final_url)

    async def _fetch_record_once(
        self,
        url: str,
        attempt: int,
        deadline: float,
    ) -> SourcePage:
        """One detail fetch. Sessionless: no form GET, no CSRF token."""
        async with self._transaction() as client:
            response = await self._request(
                client,
                "GET",
                url,
                op="fetch_record",
                step="record",
                attempt=attempt,
                deadline=deadline,
                allow_not_found=True,
            )
            final = await self._follow(
                client,
                response,
                op="fetch_record",
                attempt=attempt,
                deadline=deadline,
                allow_not_found=True,
            )
            if final.status_code not in (200, 404):
                raise SourceUnavailable(
                    f"fetch_record: the detail page answered {final.status_code}"
                )
            return self._page(final, _bare(str(final.request.url)))

    # -- form body -----------------------------------------------------------

    @staticmethod
    def search_fields(criteria: SearchCriteria, token: str) -> dict[str, object]:
        """The POST body, in the source form's document order.

        ``kwalificaties[]`` holds a list so httpx encodes it as a repeated
        bracketed key (``kwalificaties%5B%5D=0101&kwalificaties%5B%5D=0110``).
        ``g-recaptcha-response`` is omitted; the location fields are sent on the
        organisation side only, matching the browser, whose ``fieldset#tab2`` is
        disabled on the zorgverlener tab.
        """
        fields: dict[str, object] = {
            "_token": token,
            "zorgpartijtype": ZORGPARTIJTYPE[criteria.side],
            "agbcode": criteria.query or "",
            "zorgsoort": criteria.zorgsoort or "",
        }
        if criteria.kwalificaties:
            fields["kwalificaties[]"] = list(criteria.kwalificaties)
        if criteria.side == "organisatie":
            fields["plaats"] = criteria.plaats or ""
            fields["postcode"] = criteria.postcode or ""
            fields["kvknummer"] = criteria.kvknummer or ""
        return fields

    @staticmethod
    def _extract_token(html: str) -> str:
        """Read the CSRF token off the form input, never off the head meta tag."""
        node = HTMLParser(html).css_first('input[name="_token"]')
        token = (node.attributes.get("value") or "").strip() if node is not None else ""
        if not token:
            raise ParseError("the search form carried no _token input")
        return token

    # -- requests ------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _transaction(self) -> AsyncIterator[httpx.AsyncClient]:
        """A fresh client whose cookies die with it, closed whatever happens."""
        client = self._new_client()
        try:
            yield client
        finally:
            # Explicit: AsyncClient.__aexit__ does not route through aclose().
            # Shielded: when the tool call itself is cancelled (client went
            # away, notifications/cancelled), the first await in here would
            # re-raise and leave the connection to the garbage collector.
            with anyio.CancelScope(shield=True):
                await client.aclose()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(self._settings.http_timeout_seconds),
            headers={
                "User-Agent": self._settings.user_agent,
                "Accept": ACCEPT,
                "Accept-Language": ACCEPT_LANGUAGE,
                "Upgrade-Insecure-Requests": "1",
            },
            # None means httpx builds its own transport, exactly as before.
            transport=self._transport,
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        op: str,
        step: str,
        attempt: int,
        deadline: float,
        data: dict[str, object] | None = None,
        referer: str | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response:
        """One paced, host-checked, deadline-bounded request."""
        target = self._source_url(url, error=SourceUnavailable, what="request URL")
        await self._acquire_slot(deadline, op)
        headers = {"Referer": referer} if referer else None
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise self._deadline_exceeded(op)
        started = self._clock()
        try:
            async with asyncio.timeout(remaining):
                response = await client.request(method, target, data=data, headers=headers)
        except TimeoutError as exc:
            if self._remaining(deadline) <= 0:
                raise self._deadline_exceeded(op) from exc
            raise _Transient(f"{step} request exceeded the remaining budget") from exc
        except httpx.TransportError as exc:
            raise _Transient(f"{step} request failed: {type(exc).__name__}") from exc
        logger.info(
            "op=%s step=%s attempt=%d status=%d latency_ms=%d",
            op,
            step,
            attempt,
            response.status_code,
            int((self._clock() - started) * 1000),
        )
        self._classify(response, allow_not_found=allow_not_found)
        return response

    async def _follow(
        self,
        client: httpx.AsyncClient,
        response: httpx.Response,
        *,
        op: str,
        attempt: int,
        deadline: float,
        referer: str | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response:
        """Follow redirects ourselves, bounded, verifying every hop's URL."""
        hops = 0
        while response.is_redirect:
            if hops >= MAX_REDIRECTS:
                raise SourceUnavailable(
                    f"{op}: the source kept redirecting after {MAX_REDIRECTS} hops"
                )
            hops += 1
            location = response.headers.get("location")
            if not location:
                raise SourceUnavailable(
                    f"{op}: the source answered {response.status_code} without a Location"
                )
            # Verified before any I/O: absolute https URL on the Vektis host.
            target = urljoin(str(response.request.url), location)
            response = await self._request(
                client,
                "GET",
                target,
                op=op,
                step="redirect",
                attempt=attempt,
                deadline=deadline,
                referer=referer,
                allow_not_found=allow_not_found,
            )
        return response

    async def _acquire_slot(self, deadline: float, op: str) -> None:
        """Wait for the pacer, aborting rather than overrunning the deadline."""
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise self._deadline_exceeded(op)
        try:
            async with asyncio.timeout(remaining):
                await self._pacer.wait()
        except TimeoutError as exc:
            raise self._deadline_exceeded(op) from exc
        if self._remaining(deadline) <= 0:
            raise self._deadline_exceeded(op)

    # -- response classification --------------------------------------------

    def _classify(self, response: httpx.Response, *, allow_not_found: bool) -> None:
        """Map a response onto the error model; return for usable responses."""
        status = response.status_code
        if status == SESSION_EXPIRED_STATUS:
            raise SessionExpired("the source answered 419 Page Expired")
        if status == 403:
            if self._has_marker(response, EXPIRED_MARKER):
                raise SessionExpired("the source answered 403 with the Page Expired page")
            raise SourceBlocked("the source answered 403")
        if status in RETRYABLE_STATUSES:
            raise _Transient(
                f"the source answered {status}",
                retry_after=self._retry_after(response),
            )
        if status == 200:
            if self._challenge_marker(response):
                raise SourceBlocked("the source answered with a challenge page")
            return
        if response.is_redirect:
            return
        if status == 404 and allow_not_found:
            return
        raise SourceUnavailable(f"the source answered {status}")

    @staticmethod
    def _has_marker(response: httpx.Response, marker: str) -> bool:
        try:
            return marker in response.text.lower()
        except (UnicodeDecodeError, httpx.ResponseNotRead):  # pragma: no cover
            return False

    @classmethod
    def _challenge_marker(cls, response: httpx.Response) -> bool:
        if "html" not in response.headers.get("content-type", "text/html"):
            return False
        try:
            body = response.text.lower()
        except (UnicodeDecodeError, httpx.ResponseNotRead):  # pragma: no cover
            return False
        return any(marker in body for marker in CHALLENGE_MARKERS)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            seconds = float(raw.strip())
        except ValueError:
            # Only the delta-seconds form is honoured; an HTTP-date falls back
            # to the normal backoff ladder.
            return None
        return max(seconds, 0.0)

    # -- helpers -------------------------------------------------------------

    def _page(self, response: httpx.Response, url: str) -> SourcePage:
        return SourcePage(
            html=response.text,
            url=url,
            fetched_at=datetime.now(UTC),
            status_code=response.status_code,
        )

    @staticmethod
    def _source_url(
        url: str,
        *,
        error: type[AgbError],
        what: str,
        require_record_path: bool = False,
    ) -> str:
        """Check scheme and host before any I/O; return the URL sans fragment."""
        split = urlsplit(url)
        if split.scheme != SOURCE_SCHEME or split.netloc != SOURCE_HOST:
            # The message names the origin only: never the query or a code.
            raise error(
                f"{what} must be on {SOURCE_SCHEME}://{SOURCE_HOST}, "
                f"got {split.scheme or '(none)'}://{split.netloc or '(none)'}"
            )
        if require_record_path and not split.path.startswith(RECORD_PATH_PREFIX):
            raise error(f"{what} must be a path under {RECORD_PATH_PREFIX}")
        return urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))

    def _remaining(self, deadline: float) -> float:
        return deadline - self._clock()

    def _deadline_exceeded(self, op: str) -> SourceUnavailable:
        return SourceUnavailable(
            f"{op}: the {self._settings.tool_deadline_seconds:g}s deadline "
            "elapsed before the source answered"
        )
