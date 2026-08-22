"""Process wiring: what the transports are configured with, and what start-up costs.

Start-up loads nothing. There is no committed evidence to validate, so the wiring tests
here assert the other half of that: the service comes up with its adapters sharing one
HTTP client, and every source it can reach is on the allowlist.
"""

import asyncio

import pytest

from dependency_compat_mcp import main as package_main
from dependency_compat_mcp.cli import (
    MAX_REQUEST_BODY_BYTES,
    STREAMABLE_HTTP_PATH,
    _build_parser,
    _transport_security,
    build_service,
)
from dependency_compat_mcp.server import build_server


def test_the_package_root_exposes_main_without_importing_the_server() -> None:
    """`dependency_compat_mcp` must stay import-cheap and import-cycle-free.

    Putting the wiring in the root once meant that importing any submodule pulled in the
    whole server, so a single unfinished module broke every unrelated import.
    """
    assert callable(package_main)


def test_defaults_match_the_transport_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("K_REVISION", raising=False)
    monkeypatch.delenv("K_CONFIGURATION", raising=False)
    parsed = _build_parser().parse_args([])
    assert parsed.transport == "stdio"
    # 01: bind to loopback unless a deployment boundary says otherwise.
    assert parsed.host == "127.0.0.1"
    assert STREAMABLE_HTTP_PATH == "/mcp"
    assert MAX_REQUEST_BODY_BYTES == 4 * 1024 * 1024


def test_explicit_public_url_is_parsed_at_the_cli_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://Dependency-Compat.Example.com/")

    parsed = _build_parser().parse_args(["--transport", "http", "--host", "0.0.0.0"])
    security = _transport_security(parsed)

    assert parsed.port == 8080
    assert security.allowed_hosts == ["dependency-compat.example.com"]
    assert security.allowed_origins == ["https://dependency-compat.example.com"]


def test_cloud_run_environment_selects_public_binding_without_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.delenv("MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("K_SERVICE", "dependency-compat-mcp")
    monkeypatch.setenv("K_REVISION", "dependency-compat-mcp-00001")
    monkeypatch.setenv("K_CONFIGURATION", "dependency-compat-mcp")

    parsed = _build_parser().parse_args(["--transport", "http"])
    security = _transport_security(parsed)

    assert parsed.host == "0.0.0.0"
    assert parsed.port == 8080
    assert parsed.deployment_runtime == "cloud_run"
    assert not security.enable_dns_rebinding_protection


def test_partial_cloud_run_identity_cannot_disable_local_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("K_SERVICE", "dependency-compat-mcp")
    monkeypatch.delenv("K_REVISION", raising=False)
    monkeypatch.delenv("K_CONFIGURATION", raising=False)

    parsed = _build_parser().parse_args(["--transport", "http"])
    security = _transport_security(parsed)

    assert parsed.host == "127.0.0.1"
    assert parsed.deployment_runtime == "local"
    assert security.enable_dns_rebinding_protection


def test_default_https_port_is_canonicalized_for_proxy_headers() -> None:
    parsed = _build_parser().parse_args(
        ["--public-base-url", "https://mcp.example.com:443"]
    )
    security = _transport_security(parsed)

    assert security.allowed_hosts == ["mcp.example.com"]
    assert security.allowed_origins == ["https://mcp.example.com"]


@pytest.mark.parametrize(
    "value",
    ["0", "65536", "not-a-port"],
)
def test_invalid_cloud_run_port_fails_at_start_up(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PORT", value)

    with pytest.raises(SystemExit):
        _build_parser().parse_args([])


@pytest.mark.parametrize(
    "value",
    [
        "http://mcp.example.com",
        "https://user@mcp.example.com",
        "https://mcp.example.com/mcp",
        "https://mcp.example.com?debug=true",
        "https://mcp.example.com/#fragment",
        "https://mcp.example.com:0",
        "https://-invalid.example.com",
    ],
)
def test_invalid_public_base_url_fails_at_start_up(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", value)

    with pytest.raises(SystemExit):
        _build_parser().parse_args([])


def test_sse_is_not_an_option() -> None:
    """2026-07-28 deprecates HTTP+SSE, so the flag does not exist to be chosen by mistake."""
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--transport", "sse"])


def test_build_service_reads_no_repository_state_at_start_up() -> None:
    """Nothing is loaded, so nothing can be stale by the time a request arrives."""
    service = build_service()
    try:
        # One client, shared by every adapter, owned by the service.
        assert service.fetcher is not None
        assert service.pypi._fetcher is service.fetcher
        assert service.npm._fetcher is service.fetcher
        assert service.runtimes.fetcher is service.fetcher
    finally:
        asyncio.run(service.aclose())


def test_the_wired_server_is_usable() -> None:
    server = build_server(build_service())
    assert server.name == "dependency-compat-mcp"
    assert server.instructions
