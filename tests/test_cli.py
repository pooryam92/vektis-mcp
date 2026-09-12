"""CLI argument parsing. The server itself is never started here."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator

import pytest

from vektis_mcp.cli import build_parser, main


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    """Restore root logging after a test that lets main() reconfigure it."""
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_defaults() -> None:
    args = build_parser().parse_args([])

    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.log_level == "INFO"


def test_explicit_arguments() -> None:
    args = build_parser().parse_args(
        ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9001", "--log-level", "debug"]
    )

    assert args.transport == "streamable-http"
    assert args.host == "0.0.0.0"
    assert args.port == 9001
    assert args.log_level == "DEBUG"


def test_unknown_transport_is_rejected() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--transport", "carrier-pigeon"])

    assert excinfo.value.code == 2


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0


def test_stdio_run_is_invoked_with_logging_on_stderr(
    monkeypatch: pytest.MonkeyPatch, isolated_logging: None
) -> None:
    from vektis_mcp import server

    calls: list[str] = []
    monkeypatch.setattr(server.mcp, "run", lambda transport: calls.append(transport))

    assert main(["--log-level", "WARNING"]) == 0
    assert calls == ["stdio"]
    assert logging.getLogger().level == logging.WARNING
    assert all(getattr(h, "stream", sys.stderr) is sys.stderr for h in logging.getLogger().handlers)


def test_streamable_http_sets_host_and_port(
    monkeypatch: pytest.MonkeyPatch, isolated_logging: None
) -> None:
    from vektis_mcp import server

    calls: list[str] = []
    monkeypatch.setattr(server.mcp, "run", lambda transport: calls.append(transport))
    # Setting the current values makes monkeypatch restore them after the test.
    monkeypatch.setattr(server.mcp.settings, "host", server.mcp.settings.host)
    monkeypatch.setattr(server.mcp.settings, "port", server.mcp.settings.port)
    monkeypatch.setattr(server.mcp.settings, "transport_security", server.mcp.settings.transport_security)

    assert main(["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9001"]) == 0
    assert calls == ["streamable-http"]
    assert (server.mcp.settings.host, server.mcp.settings.port) == ("0.0.0.0", 9001)


def test_streamable_http_keeps_localhost_protection_for_loopback(
    monkeypatch: pytest.MonkeyPatch, isolated_logging: None
) -> None:
    from vektis_mcp import server

    monkeypatch.setattr(server.mcp, "run", lambda transport: None)
    monkeypatch.setattr(server.mcp.settings, "host", server.mcp.settings.host)
    monkeypatch.setattr(server.mcp.settings, "port", server.mcp.settings.port)
    monkeypatch.setattr(server.mcp.settings, "transport_security", server.mcp.settings.transport_security)

    assert main(["--transport", "streamable-http", "--host", "127.0.0.1"]) == 0

    security = server.mcp.settings.transport_security
    assert security is not None and security.enable_dns_rebinding_protection is True


def test_streamable_http_drops_localhost_only_allowlist_for_other_binds(
    monkeypatch: pytest.MonkeyPatch, isolated_logging: None
) -> None:
    """FastMCP fixed a localhost-only Host allowlist at construction; keeping it
    on a 0.0.0.0 bind would answer every remote request with 421."""
    from vektis_mcp import server

    monkeypatch.setattr(server.mcp, "run", lambda transport: None)
    monkeypatch.setattr(server.mcp.settings, "host", server.mcp.settings.host)
    monkeypatch.setattr(server.mcp.settings, "port", server.mcp.settings.port)
    monkeypatch.setattr(server.mcp.settings, "transport_security", server.mcp.settings.transport_security)

    assert main(["--transport", "streamable-http", "--host", "0.0.0.0"]) == 0

    assert server.mcp.settings.transport_security is None


def test_httpx_request_lines_are_kept_out_of_info_logs(
    monkeypatch: pytest.MonkeyPatch, isolated_logging: None
) -> None:
    """httpx logs full URLs (and so AGB-codes) at INFO; the CLI silences that."""
    from vektis_mcp import server

    monkeypatch.setattr(server.mcp, "run", lambda transport: None)

    assert main(["--log-level", "INFO"]) == 0

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
