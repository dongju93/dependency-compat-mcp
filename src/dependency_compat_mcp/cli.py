"""Entry points for the two transports of 01.

Both transports serve the *same* ``MCPServer``. ``stdio`` is the local path; Streamable
HTTP is the remote one, and it runs stateless because 01 forbids inferring anything about
a request from its connection. SSE is deliberately absent - the 2026-07-28 specification
deprecates it for new implementations.

On ``stdio``, stdout is the protocol wire, so logging is pinned to stderr before anything
else happens. A stray ``print`` on stdout corrupts the session.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Final

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from dependency_compat_mcp.adapters.npm import NpmAdapter
from dependency_compat_mcp.adapters.pypi import PyPIAdapter
from dependency_compat_mcp.adapters.runtimes import RuntimeReleaseAdapter
from dependency_compat_mcp.curated.loader import (
    load_curated_pack,
    load_runtime_releases,
)
from dependency_compat_mcp.infra.http import HttpxJsonFetcher
from dependency_compat_mcp.server import build_server
from dependency_compat_mcp.service import CompatibilityService

__all__ = ["build_service", "main"]

logger = logging.getLogger(__name__)

# 01 fixes this; the SDK's default happens to agree, and it is restated so a future SDK
# default cannot move the contract silently.
MAX_REQUEST_BODY_BYTES: Final = 4 * 1024 * 1024
STREAMABLE_HTTP_PATH: Final = "/mcp"


def build_service(
    *, pack_directory: Path | None = None, runtime_table: Path | None = None
) -> CompatibilityService:
    """Load static data and wire the adapters.

    The curated pack and the runtime tables are read once, here. A schema violation raises
    and the process fails to start, which 03 prefers over serving quietly wrong evidence.
    """
    pack = load_curated_pack(pack_directory)
    table = load_runtime_releases(runtime_table)
    fetcher = HttpxJsonFetcher()
    return CompatibilityService(
        pypi=PyPIAdapter(fetcher=fetcher),
        npm=NpmAdapter(fetcher=fetcher),
        runtimes=RuntimeReleaseAdapter(table=table),
        pack=pack,
        fetcher=fetcher,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dependency-compat-mcp",
        description=(
            "MCP server answering whether two exact versions are usable together, "
            "with the official sources behind the answer."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for a local host process, http for stateless Streamable HTTP.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Bind address for --transport http. Defaults to loopback; exposing the server "
            "requires TLS and the MCP authorization spec at a trusted deployment boundary."
        ),
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    return parser


def _configure_logging(level: str) -> None:
    # stderr, always: on stdio the protocol owns stdout.
    logging.basicConfig(
        level=getattr(logging, level),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _serve(
    server: MCPServer, service: CompatibilityService, args: argparse.Namespace
) -> None:
    try:
        if args.transport == "stdio":
            await server.run_stdio_async()
        else:
            await server.run_streamable_http_async(
                host=args.host,
                port=args.port,
                streamable_http_path=STREAMABLE_HTTP_PATH,
                # No per-connection session: every request carries its own version and
                # capabilities, and nothing about a verdict belongs to a connection.
                stateless_http=True,
                max_request_body_size=MAX_REQUEST_BODY_BYTES,
                transport_security=TransportSecuritySettings(
                    # 01: Origin/Host validation and DNS-rebinding defence belong at the
                    # deployment boundary, and the server refuses to run without them.
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=[f"{args.host}:{args.port}", args.host],
                    allowed_origins=[f"http://{args.host}:{args.port}"],
                ),
            )
    finally:
        await service.aclose()


def main() -> None:
    """Console-script entry point."""
    args = _build_parser().parse_args()
    _configure_logging(args.log_level)
    service = build_service()
    server = build_server(service)
    logger.info("starting dependency-compat-mcp over %s", args.transport)
    asyncio.run(_serve(server, service, args))
