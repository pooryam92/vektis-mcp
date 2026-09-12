"""Command line entry point. Stdio by default; Streamable HTTP on request."""

from __future__ import annotations

import argparse
import logging
import sys

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def build_parser() -> argparse.ArgumentParser:
    """Parser for the vektis-mcp console script."""
    parser = argparse.ArgumentParser(prog="vektis-mcp", description="MCP server for the public Vektis AGB-register.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="stdio for a client-launched subprocess (default), streamable-http to serve over HTTP.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for streamable-http (default: %(default)s).")
    parser.add_argument("--port", type=int, default=8000, help="Port for streamable-http (default: %(default)s).")
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        type=str.upper,
        default="INFO",
        help="Log level; logs always go to stderr (default: %(default)s).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Configure stderr logging, then run the server on the chosen transport."""
    args = build_parser().parse_args(argv)

    # stdout carries MCP protocol messages on stdio, so every log line goes to stderr.
    logging.basicConfig(
        level=args.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )

    # httpx logs every request line with its full URL at INFO, which would put
    # looked-up AGB-codes in the log; the client logs its own redacted lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from . import server  # imported late so stdout stays clean and import errors surface here

    if args.transport == "stdio":
        server.mcp.run(transport="stdio")
    else:
        configure_http(server.mcp, args.host, args.port, args.log_level)
        server.mcp.run(transport="streamable-http")
    return 0


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def configure_http(mcp: object, host: str, port: int, log_level: str) -> None:
    """Point the FastMCP settings at the requested bind address.

    FastMCP decides its DNS-rebinding protection from the host it was
    *constructed* with (127.0.0.1), which allows only localhost ``Host``
    headers. Assigning ``settings.host`` later leaves that allowlist in place,
    so a server bound to another address would answer every request with 421.
    For a non-loopback bind we do what FastMCP itself does when constructed
    with that host: leave transport security unset.
    """
    settings = mcp.settings  # type: ignore[attr-defined]
    settings.host = host
    settings.port = port
    settings.log_level = log_level
    if host not in LOOPBACK_HOSTS:
        settings.transport_security = None
