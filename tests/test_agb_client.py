"""Tests for the AGB source client: pacing, sessions, redirects, retries.

Every test drives the real client through ``httpx.MockTransport`` with a
stateful handler that records the request sequence, so the form body, the
cookies and the hop order are asserted on the wire. Time is faked through the
pacer (the client borrows the pacer's clock and sleep), so backoff and deadline
tests run instantly.
"""

from __future__ import annotations

import anyio
import asyncio
from pathlib import Path

import httpx
import pytest

from vektis_mcp.agb.client import MAX_REDIRECTS, AgbClient, Pacer
from vektis_mcp.agb.constants import FORM_URL, RESULTS_URL
from vektis_mcp.agb.errors import (
    InvalidInput,
    ParseError,
    SourceBlocked,
    SourceUnavailable,
)
from vektis_mcp.agb.types import SearchCriteria
from vektis_mcp.settings import DEFAULT_USER_AGENT, Settings

FIXTURES = Path(__file__).parent / "fixtures" / "agb"
FIXTURE_TOKEN = "TESTFORMTOKEN000000000000000000000000000"
RECORD_URL = "https://www.vektis.nl/agb-register/zorgverlener-01999001"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def form_html(token: str = FIXTURE_TOKEN) -> str:
    return fixture("search_form.html").replace(FIXTURE_TOKEN, token)


RESULTS_HTML = fixture("search_results_zorgverlener.html")
EXPIRED_HTML = fixture("session_expired.html")


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
        await asyncio.sleep(0)  # stay a real suspension point


class Source:
    """Records every request and answers through the given handler."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request, len(self.requests))

    @property
    def trail(self) -> list[tuple[str, str]]:
        return [(r.method, str(r.url)) for r in self.requests]

    def bodies(self, method: str = "POST") -> list[str]:
        return [r.content.decode() for r in self.requests if r.method == method]


def build(
    handler,
    *,
    settings: Settings | None = None,
    interval: float = 1.0,
) -> tuple[AgbClient, Source, FakeTime]:
    source = Source(handler)
    clock = FakeTime()
    pacer = Pacer(interval, clock=clock.clock, sleep=clock.sleep)
    client = AgbClient(
        settings or Settings(),
        pacer,
        transport=httpx.MockTransport(source),
    )
    return client, source, clock


def happy(
    *,
    token: str = FIXTURE_TOKEN,
    results: str = RESULTS_HTML,
    cookie: str = "sess-1",
):
    """The verified live flow: form GET 200, POST 302, redirect GET 200."""

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == FORM_URL:
            return httpx.Response(
                200,
                html=form_html(token),
                headers={"set-cookie": f"vektis_session={cookie}; Path=/; Secure; HttpOnly"},
            )
        if request.method == "POST" and url == RESULTS_URL:
            return httpx.Response(302, headers={"location": f"{RESULTS_URL}#resultaten"})
        if request.method == "GET" and url == RESULTS_URL:
            return httpx.Response(200, html=results)
        raise AssertionError(f"unexpected request {request.method} {url}")

    return handler


ZORGVERLENER = SearchCriteria(
    side="zorgverlener",
    query="0100045",
    zorgsoort="01",
    kwalificaties=("0101", "0110"),
)
ORGANISATIE = SearchCriteria(
    side="organisatie",
    zorgsoort="01",
    plaats="Assen",
    postcode="9403",
)


# -- happy path --------------------------------------------------------------


async def test_search_happy_path_follows_form_post_redirect():
    client, source, _ = build(happy())

    page = await client.search(ZORGVERLENER)

    assert source.trail == [
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
        ("GET", RESULTS_URL),
    ]
    assert page.html == RESULTS_HTML
    assert page.url == RESULTS_URL  # the #resultaten fragment is dropped
    assert page.status_code == 200
    assert page.fetched_at.tzinfo is not None
    assert page.fetched_at.utcoffset().total_seconds() == 0


async def test_search_post_body_encoding_and_headers():
    client, source, _ = build(happy())

    await client.search(ZORGVERLENER)

    post = source.requests[1]
    assert post.content.decode() == (
        f"_token={FIXTURE_TOKEN}"
        "&zorgpartijtype=zorgverlener"
        "&agbcode=0100045"
        "&zorgsoort=01"
        "&kwalificaties%5B%5D=0101"
        "&kwalificaties%5B%5D=0110"
    )
    assert post.headers["content-type"] == "application/x-www-form-urlencoded"
    assert post.headers["referer"] == FORM_URL
    for request in source.requests:
        assert request.headers["user-agent"] == DEFAULT_USER_AGENT
        assert request.headers["user-agent"].startswith("Mozilla/5.0 (")
        assert "vektis" not in request.headers["user-agent"].lower()
        assert request.headers["accept"].startswith("text/html,")
        assert request.headers["accept-language"] == "nl-NL,nl;q=0.9,en;q=0.8"
        assert request.headers["upgrade-insecure-requests"] == "1"
    assert "g-recaptcha-response" not in post.content.decode()


async def test_search_organisatie_sends_location_fields_and_both_tab_value():
    client, source, _ = build(happy())

    await client.search(ORGANISATIE)

    assert source.bodies() == [
        f"_token={FIXTURE_TOKEN}"
        "&zorgpartijtype=onderneming%2Cvestiging"
        "&agbcode="
        "&zorgsoort=01"
        "&plaats=Assen"
        "&postcode=9403"
        "&kvknummer="
    ]


async def test_search_zorgverlener_omits_location_fields():
    client, source, _ = build(happy())

    await client.search(SearchCriteria(side="zorgverlener", query="0100045"))

    body = source.bodies()[0]
    assert body == f"_token={FIXTURE_TOKEN}&zorgpartijtype=zorgverlener&agbcode=0100045&zorgsoort="
    for absent in ("plaats", "postcode", "kvknummer", "kwalificaties"):
        assert absent not in body


async def test_search_rejects_unknown_side_before_any_request():
    client, source, _ = build(happy())

    with pytest.raises(InvalidInput):
        await client.search(SearchCriteria(side="beide"))  # type: ignore[arg-type]
    assert source.requests == []


# -- cookies and isolation ---------------------------------------------------


async def test_form_cookies_ride_along_on_the_post_and_redirect():
    client, source, _ = build(happy(cookie="sess-42"))

    await client.search(ZORGVERLENER)

    assert "cookie" not in source.requests[0].headers
    assert source.requests[1].headers["cookie"] == "vektis_session=sess-42"
    assert source.requests[2].headers["cookie"] == "vektis_session=sess-42"


async def test_concurrent_searches_do_not_share_cookies_or_tokens():
    forms = 0
    pairs: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        nonlocal forms
        url = str(request.url)
        if request.method == "GET" and url == FORM_URL:
            forms += 1
            return httpx.Response(
                200,
                html=form_html(f"TOKEN{forms}"),
                headers={"set-cookie": f"vektis_session=sess{forms}; Path=/; Secure"},
            )
        if request.method == "POST":
            body = request.content.decode()
            token = body.split("&", 1)[0].removeprefix("_token=")
            pairs.append((token, request.headers.get("cookie")))
            return httpx.Response(302, headers={"location": f"{RESULTS_URL}#resultaten"})
        return httpx.Response(200, html=RESULTS_HTML)

    client, source, _ = build(handler)

    pages = await asyncio.gather(
        client.search(ZORGVERLENER),
        client.search(ORGANISATIE),
    )

    assert [p.status_code for p in pages] == [200, 200]
    assert forms == 2
    assert len(pairs) == 2
    # Every POST carries exactly the cookie issued by its own form GET.
    assert sorted(pairs) == [
        ("TOKEN1", "vektis_session=sess1"),
        ("TOKEN2", "vektis_session=sess2"),
    ]
    # Two different criteria, two different transactions, no crossover.
    posted_bodies = source.bodies()
    assert len(set(posted_bodies)) == 2
    for body, (_, cookie) in zip(posted_bodies, pairs, strict=True):
        other = "sess2" if "sess1" in (cookie or "") else "sess1"
        assert other not in (cookie or "")


# -- token -------------------------------------------------------------------


async def test_missing_token_is_a_parse_error():
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(200, html="<form class='js-agb-search-form'></form>")

    client, source, _ = build(handler)

    with pytest.raises(ParseError):
        await client.search(ZORGVERLENER)
    assert source.trail == [("GET", FORM_URL)]


async def test_token_comes_from_the_form_input_not_the_head_meta():
    client, source, _ = build(happy())

    await client.search(ZORGVERLENER)

    body = source.bodies()[0]
    assert FIXTURE_TOKEN in body
    assert "TESTCSRFMETATOKEN" in form_html()  # the decoy really is in the page
    assert "TESTCSRFMETATOKEN" not in body


# -- session expiry ----------------------------------------------------------


def expiring_handler(*, expiries: int, status: int = 419, body: str = EXPIRED_HTML):
    seen = {"posts": 0}

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == FORM_URL:
            return httpx.Response(200, html=form_html(f"TOKEN{n}"))
        if request.method == "POST":
            seen["posts"] += 1
            if seen["posts"] <= expiries:
                return httpx.Response(status, html=body)
            return httpx.Response(302, headers={"location": f"{RESULTS_URL}#resultaten"})
        return httpx.Response(200, html=RESULTS_HTML)

    return handler


async def test_419_on_post_restarts_the_whole_transaction_once():
    client, source, _ = build(expiring_handler(expiries=1))

    page = await client.search(ZORGVERLENER)

    assert page.status_code == 200
    assert source.trail == [
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
        ("GET", RESULTS_URL),
    ]
    # A fresh token per transaction, so the restart is a real new session.
    assert source.bodies()[0] != source.bodies()[1]


async def test_two_expiries_end_as_source_unavailable():
    client, source, _ = build(expiring_handler(expiries=2))

    with pytest.raises(SourceUnavailable, match="twice"):
        await client.search(ZORGVERLENER)
    assert source.trail == [
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
    ]


async def test_403_with_page_expired_body_is_treated_as_expiry():
    client, source, _ = build(expiring_handler(expiries=1, status=403))

    page = await client.search(ZORGVERLENER)

    assert page.status_code == 200
    assert len(source.requests) == 5


async def test_plain_403_is_blocked_without_a_restart():
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.method == "GET" and str(request.url) == FORM_URL:
            return httpx.Response(200, html=form_html())
        return httpx.Response(403, html="<html><body>Forbidden</body></html>")

    client, source, _ = build(handler)

    with pytest.raises(SourceBlocked):
        await client.search(ZORGVERLENER)
    assert len(source.requests) == 2


async def test_challenge_page_on_200_is_blocked():
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(200, html="<html><title>Just a moment...</title></html>")

    client, source, _ = build(handler)

    with pytest.raises(SourceBlocked):
        await client.search(ZORGVERLENER)
    assert len(source.requests) == 1


async def test_redirect_back_to_the_form_is_treated_as_expiry_then_unavailable():
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.method == "GET" and str(request.url) == FORM_URL:
            return httpx.Response(200, html=form_html(f"TOKEN{n}"))
        return httpx.Response(302, headers={"location": FORM_URL})

    client, source, _ = build(handler)

    with pytest.raises(SourceUnavailable, match="twice"):
        await client.search(ZORGVERLENER)
    # Two transactions, and no request is wasted on the form redirect target.
    assert source.trail == [
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
    ]


# -- redirects ---------------------------------------------------------------


async def test_redirect_loop_beyond_the_bound_is_unavailable():
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == FORM_URL:
            return httpx.Response(200, html=form_html())
        return httpx.Response(302, headers={"location": f"{RESULTS_URL}#resultaten"})

    client, source, _ = build(handler)

    with pytest.raises(SourceUnavailable, match="redirect"):
        await client.search(ZORGVERLENER)
    # form GET + POST + exactly MAX_REDIRECTS followed hops, then it stops.
    assert len(source.requests) == 2 + MAX_REDIRECTS
    assert MAX_REDIRECTS == 3


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/agb-register/zoeken/resultaten",
        "http://www.vektis.nl/agb-register/zoeken/resultaten",
        "https://vektis.nl.evil.example/agb-register/zoeken/resultaten",
    ],
)
async def test_redirect_off_the_source_origin_is_never_followed(location: str):
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.method == "GET" and str(request.url) == FORM_URL:
            return httpx.Response(200, html=form_html())
        return httpx.Response(302, headers={"location": location})

    client, source, _ = build(handler)

    with pytest.raises(SourceUnavailable, match="https://www.vektis.nl"):
        await client.search(ZORGVERLENER)
    assert [str(r.url) for r in source.requests] == [FORM_URL, RESULTS_URL]
    for request in source.requests:
        assert request.url.scheme == "https"
        assert request.url.host == "www.vektis.nl"


# -- retries -----------------------------------------------------------------


def failing_handler(*, status: int, failures: int, headers: dict[str, str] | None = None):
    seen = {"posts": 0}

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == FORM_URL:
            return httpx.Response(200, html=form_html())
        if request.method == "POST":
            seen["posts"] += 1
            if seen["posts"] <= failures:
                return httpx.Response(status, html="busy", headers=headers or {})
            return httpx.Response(302, headers={"location": f"{RESULTS_URL}#resultaten"})
        return httpx.Response(200, html=RESULTS_HTML)

    return handler


async def test_503_then_200_retries_the_whole_transaction_with_backoff():
    client, source, clock = build(failing_handler(status=503, failures=1), interval=0.0)

    page = await client.search(ZORGVERLENER)

    assert page.status_code == 200
    assert source.trail == [
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
        ("GET", FORM_URL),
        ("POST", RESULTS_URL),
        ("GET", RESULTS_URL),
    ]
    assert len(clock.sleeps) == 1
    assert 0.5 <= clock.sleeps[0] <= 0.625  # base 0.5 s plus bounded jitter


async def test_429_retry_after_is_honoured_exactly():
    client, _, clock = build(
        failing_handler(status=429, failures=1, headers={"retry-after": "2"}),
        interval=0.0,
    )

    page = await client.search(ZORGVERLENER)

    assert page.status_code == 200
    assert clock.sleeps == [2.0]


async def test_backoff_doubles_and_then_exhausts():
    settings = Settings(max_retries=2)
    client, source, clock = build(
        failing_handler(status=502, failures=99),
        settings=settings,
        interval=0.0,
    )

    with pytest.raises(SourceUnavailable, match="502"):
        await client.search(ZORGVERLENER)

    assert len(source.bodies()) == settings.max_retries + 1
    assert len(clock.sleeps) == settings.max_retries
    assert clock.sleeps[1] > clock.sleeps[0]


async def test_transport_errors_are_retried_then_reported():
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client, source, _ = build(handler, settings=Settings(max_retries=1), interval=0.0)

    with pytest.raises(SourceUnavailable, match="ConnectError"):
        await client.search(ZORGVERLENER)
    assert len(source.requests) == 2


async def test_other_4xx_reports_its_status():
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.method == "GET" and str(request.url) == FORM_URL:
            return httpx.Response(200, html=form_html())
        return httpx.Response(422, html="nope")

    client, source, _ = build(handler)

    with pytest.raises(SourceUnavailable, match="422"):
        await client.search(ZORGVERLENER)
    assert len(source.requests) == 2


# -- deadline ----------------------------------------------------------------


async def test_deadline_counts_pacing_time():
    # Three paced requests at 2 s apart need 4 s of waiting; the budget is 3 s.
    settings = Settings(http_timeout_seconds=1, tool_deadline_seconds=3)
    client, source, clock = build(happy(), settings=settings, interval=2.0)

    with pytest.raises(SourceUnavailable, match="deadline"):
        await client.search(ZORGVERLENER)

    assert source.trail == [("GET", FORM_URL), ("POST", RESULTS_URL)]
    assert clock.sleeps == [2.0, 2.0]


async def test_deadline_refuses_a_backoff_it_cannot_afford():
    settings = Settings(http_timeout_seconds=1, tool_deadline_seconds=1.2)
    client, _, clock = build(
        failing_handler(status=503, failures=99, headers={"retry-after": "30"}),
        settings=settings,
        interval=0.0,
    )

    with pytest.raises(SourceUnavailable, match="deadline"):
        await client.search(ZORGVERLENER)
    assert clock.sleeps == []  # never slept past the budget


# -- pacing ------------------------------------------------------------------


async def test_pacer_spaces_every_request_start():
    client, source, clock = build(happy(), interval=1.5)

    await client.search(ZORGVERLENER)

    assert len(source.requests) == 3
    assert clock.sleeps == [1.5, 1.5]  # N requests, N-1 waits
    assert clock.now == 1000.0 + 2 * 1.5


async def test_pacer_paces_across_clients_and_operations():
    client, source, clock = build(happy(), interval=1.0)

    def record(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(200, html=fixture("record_zorgverlener.html"))

    await client.search(ZORGVERLENER)
    # Same pacer, a brand new transaction-scoped client: still paced.
    source._handler = record  # noqa: SLF001 - swap the script, keep the recorder
    await client.fetch_record(RECORD_URL)

    assert len(source.requests) == 4
    assert clock.sleeps == [1.0, 1.0, 1.0]


async def test_pacer_releases_its_lock_when_a_waiter_is_cancelled():
    blocked = asyncio.Event()
    released = asyncio.Event()

    async def sleep(seconds: float) -> None:
        released.set()
        await blocked.wait()

    pacer = Pacer(1.0, clock=lambda: 1000.0, sleep=sleep)
    await pacer.wait()  # claims the first slot, no sleep

    waiter = asyncio.create_task(pacer.wait())
    await released.wait()
    assert pacer.locked

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not pacer.locked

    # The next waiter can still take the lock.
    pacer.sleep = lambda seconds: asyncio.sleep(0)
    await asyncio.wait_for(pacer.wait(), 1)
    assert not pacer.locked


async def test_pacer_rejects_a_negative_interval():
    with pytest.raises(ValueError):
        Pacer(-1.0)


async def test_pacer_from_settings_uses_the_configured_interval():
    pacer = Pacer.from_settings(Settings(request_interval_seconds=2.5))
    assert pacer.interval_seconds == 2.5


# -- fetch_record ------------------------------------------------------------


async def test_fetch_record_makes_a_single_sessionless_get():
    html = fixture("record_zorgverlener.html")

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, html=html)

    client, source, _ = build(handler)

    page = await client.fetch_record(RECORD_URL)

    assert source.trail == [("GET", RECORD_URL)]
    assert page.html == html
    assert page.url == RECORD_URL
    assert page.status_code == 200
    assert FORM_URL not in [str(r.url) for r in source.requests]


async def test_fetch_record_returns_404_pages_as_a_source_page():
    html = fixture("record_not_found.html")

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(404, html=html)

    client, source, _ = build(handler)

    page = await client.fetch_record(
        "https://www.vektis.nl/agb-register/zorgverlener-99999999"
    )

    assert page.status_code == 404
    assert page.html == html
    assert len(source.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://www.vektis.nl/agb-register/zorgverlener-01999001",
        "https://evil.example/agb-register/zorgverlener-01999001",
        "https://www.vektis.nl.evil.example/agb-register/zorgverlener-01999001",
        "https://www.vektis.nl/something-else",
        "ftp://www.vektis.nl/agb-register/zorgverlener-01999001",
        "/agb-register/zorgverlener-01999001",
        "",
    ],
)
async def test_fetch_record_rejects_unusable_urls_before_any_io(url: str):
    client, source, _ = build(happy())

    with pytest.raises(InvalidInput):
        await client.fetch_record(url)
    assert source.requests == []


async def test_fetch_record_drops_the_fragment_and_keeps_the_query():
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(200, html="<html></html>")

    client, source, _ = build(handler)

    page = await client.fetch_record(f"{RECORD_URL}?x=1#top")

    assert page.url == f"{RECORD_URL}?x=1"
    assert source.trail == [("GET", f"{RECORD_URL}?x=1")]


async def test_fetch_record_retries_a_5xx_and_restarts_on_expiry():
    seen = {"n": 0}

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(503, html="busy")
        if seen["n"] == 2:
            return httpx.Response(419, html=EXPIRED_HTML)
        return httpx.Response(200, html="<html>ok</html>")

    client, source, _ = build(handler, interval=0.0)

    page = await client.fetch_record(RECORD_URL)

    assert page.status_code == 200
    assert len(source.requests) == 3


async def test_fetch_record_follows_one_bounded_redirect():
    target = "https://www.vektis.nl/agb-register/vestiging-71999001"

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if n == 1:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, html="<html>ok</html>")

    client, source, _ = build(handler)

    page = await client.fetch_record(
        "https://www.vektis.nl/agb-register/vestiging-4e7a45354f546b774d44453d"
    )

    assert page.url == target
    assert len(source.requests) == 2


# -- client lifecycle --------------------------------------------------------


@pytest.fixture
def closes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = httpx.AsyncClient.aclose

    async def counting_aclose(self: httpx.AsyncClient) -> None:
        calls.append(1)
        await original(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", counting_aclose)
    return calls


async def test_client_is_closed_on_success(closes: list[int]):
    client, _, _ = build(happy())

    await client.search(ZORGVERLENER)

    assert len(closes) == 1


async def test_every_restarted_transaction_closes_its_client(closes: list[int]):
    client, _, _ = build(expiring_handler(expiries=2))

    with pytest.raises(SourceUnavailable):
        await client.search(ZORGVERLENER)

    assert len(closes) == 2


async def test_client_is_closed_on_error(closes: list[int]):
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(403, html="<html>Forbidden</html>")

    client, _, _ = build(handler)

    with pytest.raises(SourceBlocked):
        await client.search(ZORGVERLENER)

    assert len(closes) == 1


async def test_client_is_closed_and_the_pacer_freed_on_cancellation(closes: list[int]):
    blocked = asyncio.Event()
    waiting = asyncio.Event()

    async def sleep(seconds: float) -> None:
        waiting.set()
        await blocked.wait()

    source = Source(happy())
    pacer = Pacer(1.0, clock=lambda: 1000.0, sleep=sleep)
    client = AgbClient(Settings(), pacer, transport=httpx.MockTransport(source))

    task = asyncio.create_task(client.search(ZORGVERLENER))
    await waiting.wait()  # the form GET is done; the POST is waiting on the pacer
    assert len(source.requests) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(closes) == 1
    assert not pacer.locked


# -- logging hygiene ---------------------------------------------------------


async def test_logs_carry_mechanics_but_never_query_token_or_html(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level("INFO", logger="vektis_mcp.agb.client")
    client, _, _ = build(expiring_handler(expiries=1))

    await client.search(ZORGVERLENER)

    messages = [record.getMessage() for record in caplog.records]
    joined = "\n".join(messages)
    assert any("op=search step=form" in m for m in messages)
    assert any("event=session_expired" in m for m in messages)
    assert any("event=ok" in m and "latency_ms=" in m for m in messages)
    for secret in (
        ZORGVERLENER.query,
        "0101",
        FIXTURE_TOKEN,
        "TOKEN1",
        "vektis_session",
        "Zoekresultaten",
    ):
        assert secret not in joined


class ClosingTransport(httpx.MockTransport):
    """A transport whose close has a checkpoint, like a real connection pool."""

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.closed = False

    async def aclose(self) -> None:
        await anyio.sleep(0)
        self.closed = True


async def _cancel_mid_request(cancel) -> ClosingTransport:
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        in_flight.set()
        await release.wait()
        return httpx.Response(200, html="<html></html>")

    transport = ClosingTransport(handler)
    client = AgbClient(Settings(), Pacer(0.0), transport=transport)
    await cancel(client.search(ZORGVERLENER), in_flight)
    return transport


async def test_transaction_close_survives_anyio_cancellation():
    """The MCP SDK cancels a tool call through anyio; close must still finish."""

    async def cancel(call, in_flight: asyncio.Event) -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: call)
            await in_flight.wait()
            group.cancel_scope.cancel()

    transport = await _cancel_mid_request(cancel)

    assert transport.closed


async def test_transaction_close_survives_native_cancellation():
    async def cancel(call, in_flight: asyncio.Event) -> None:
        task = asyncio.create_task(call)
        await in_flight.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    transport = await _cancel_mid_request(cancel)

    assert transport.closed
