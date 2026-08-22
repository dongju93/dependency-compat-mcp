"""Entry points for the two transports of 01.

Both transports serve the *same* ``MCPServer``. ``stdio`` is the local path; Streamable
HTTP is the remote one, and it runs stateless because 01 forbids inferring anything about
a request from its connection. SSE is deliberately absent - the 2026-07-28 specification
deprecates it for new implementations.

Neither transport is configured to pick a protocol revision, because neither can: the SDK
keeps the pre-2026 handshake era reachable on both, chosen by the client's opening frame on
``stdio`` and by the version header over HTTP. `server.reject_legacy_protocol` refuses that
era for both at the one point they share.

On ``stdio``, stdout is the protocol wire, so logging is pinned to stderr before anything
else happens. A stray ``print`` on stdout corrupts the session.
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from dependency_compat_mcp.adapters.npm import NpmAdapter
from dependency_compat_mcp.adapters.pypi import PyPIAdapter
from dependency_compat_mcp.adapters.runtimes import RuntimeReleaseAdapter
from dependency_compat_mcp.infra.http import HttpxJsonFetcher
from dependency_compat_mcp.server import build_server
from dependency_compat_mcp.service import CompatibilityService

__all__ = ["build_service", "main"]

logger = logging.getLogger(__name__)

# 01 fixes this; the SDK's default happens to agree, and it is restated so a future SDK
# default cannot move the contract silently.
MAX_REQUEST_BODY_BYTES: Final = 4 * 1024 * 1024
STREAMABLE_HTTP_PATH: Final = "/mcp"


@dataclass(frozen=True, slots=True)
class _PublicBaseUrl:
    """A deployment URL whose syntax and security-relevant parts are already parsed."""

    origin: str
    authority: str


type _DeploymentRuntime = Literal["local", "cloud_run"]


def _deployment_runtime() -> _DeploymentRuntime:
    """Recognise Cloud Run only from the complete platform-provided identity."""
    cloud_run_identity = ("K_SERVICE", "K_REVISION", "K_CONFIGURATION")
    if all(os.environ.get(name) for name in cloud_run_identity):
        return "cloud_run"
    return "local"


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _parse_public_base_url(value: str) -> _PublicBaseUrl:
    """Parse the trusted reverse-proxy origin once instead of re-checking it later."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("public base URL is malformed") from exc

    if parsed.scheme != "https":
        raise argparse.ArgumentTypeError("public base URL must use https")
    if parsed.username is not None or parsed.password is not None:
        raise argparse.ArgumentTypeError("public base URL must not contain userinfo")
    if parsed.hostname is None:
        raise argparse.ArgumentTypeError("public base URL must contain a hostname")
    if port is not None and not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "public base URL port must be between 1 and 65535"
        )
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError(
            "public base URL must be an origin without a path, query, or fragment"
        )

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise argparse.ArgumentTypeError(
            "public base URL hostname is not valid IDNA"
        ) from exc
    labels = hostname.split(".")
    if len(hostname) > 253 or any(
        not label
        or len(label) > 63
        or not label[0].isalnum()
        or not label[-1].isalnum()
        or any(not (character.isalnum() or character == "-") for character in label)
        for label in labels
    ):
        raise argparse.ArgumentTypeError("public base URL hostname is invalid")

    # RFC 6454 serialises the default HTTPS port without ``:443``; match what an
    # external proxy will put in both Host and Origin even if the operator wrote it.
    authority = hostname if port in (None, 443) else f"{hostname}:{port}"
    return _PublicBaseUrl(origin=f"https://{authority}", authority=authority)


def _transport_security(args: argparse.Namespace) -> TransportSecuritySettings:
    public_base_url: _PublicBaseUrl | None = args.public_base_url
    if public_base_url is not None:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_base_url.authority],
            allowed_origins=[public_base_url.origin],
        )
    if args.deployment_runtime == "cloud_run":
        # Cloud Run terminates TLS and validates the public ingress host before the
        # request reaches this container. Its runtime contract does not expose the
        # generated service URL, so exact application-level allowlisting is impossible
        # without an operator-supplied value. Trust only the structurally recognised
        # Cloud Run boundary; local and other deployments keep the defence enabled.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{args.host}:{args.port}", args.host],
        allowed_origins=[f"http://{args.host}:{args.port}"],
    )


def build_service() -> CompatibilityService:
    """Wire the adapters over one shared HTTP client.

    Nothing is loaded from the repository. Every fact this server answers with - a package
    release, a runtime release, an end-of-life date - is read from its official source when
    a request needs it, so start-up has no evidence state to validate and no snapshot that
    could be out of date by the time it is served.
    """
    fetcher = HttpxJsonFetcher()
    return CompatibilityService(
        pypi=PyPIAdapter(fetcher=fetcher),
        npm=NpmAdapter(fetcher=fetcher),
        runtimes=RuntimeReleaseAdapter(fetcher=fetcher),
        fetcher=fetcher,
    )


def _build_parser() -> argparse.ArgumentParser:
    deployment_runtime = _deployment_runtime()
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
        default="0.0.0.0" if deployment_runtime == "cloud_run" else "127.0.0.1",
        help=(
            "Bind address for --transport http. Defaults to Cloud Run's public bind "
            "address when detected, otherwise loopback."
        ),
    )
    parser.add_argument(
        "--port",
        type=_parse_port,
        default=os.environ.get("PORT", "8000"),
        help="Bind port for HTTP; defaults to the Cloud Run PORT value, then 8000.",
    )
    parser.add_argument(
        "--public-base-url",
        type=_parse_public_base_url,
        default=os.environ.get("MCP_PUBLIC_BASE_URL"),
        help=(
            "External HTTPS origin used for Host and Origin validation behind a trusted "
            "reverse proxy; defaults to MCP_PUBLIC_BASE_URL."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    parser.set_defaults(deployment_runtime=deployment_runtime)
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
                # 01: Origin/Host validation and DNS-rebinding defence stay in-process
                # except when the structurally detected Cloud Run ingress owns them.
                transport_security=_transport_security(args),
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
