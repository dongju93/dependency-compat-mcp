"""Process wiring: what is loaded at start-up, and what a bad start-up does.

03 wants a schema violation in the curated data to be a *start-up* failure rather than a
quiet degradation, so the wiring is worth its own tests: serving wrong evidence is worse
than not serving.
"""

from pathlib import Path

import pytest

from dependency_compat_mcp import main as package_main
from dependency_compat_mcp.cli import (
    MAX_REQUEST_BODY_BYTES,
    STREAMABLE_HTTP_PATH,
    _build_parser,
    build_service,
)
from dependency_compat_mcp.curated.loader import PackLoadError
from dependency_compat_mcp.server import build_server

FIXTURES = Path(__file__).parent / "fixtures" / "packs"


def test_the_package_root_exposes_main_without_importing_the_server() -> None:
    """`dependency_compat_mcp` must stay import-cheap and import-cycle-free.

    Putting the wiring in the root once meant that importing any submodule pulled in the
    whole server, so a single unfinished module broke every unrelated import.
    """
    assert callable(package_main)


def test_defaults_match_the_transport_contract() -> None:
    parsed = _build_parser().parse_args([])
    assert parsed.transport == "stdio"
    # 01: bind to loopback unless a deployment boundary says otherwise.
    assert parsed.host == "127.0.0.1"
    assert STREAMABLE_HTTP_PATH == "/mcp"
    assert MAX_REQUEST_BODY_BYTES == 4 * 1024 * 1024


def test_sse_is_not_an_option() -> None:
    """2026-07-28 deprecates HTTP+SSE, so the flag does not exist to be chosen by mistake."""
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--transport", "sse"])


def test_build_service_loads_the_shipped_static_data() -> None:
    service = build_service()
    assert service.pack.pack_version
    # The shipped pack is empty on purpose; the runtime tables are not.
    assert service.pack.entries == ()
    assert service.runtimes.table.pack_version


def test_a_malformed_pack_fails_at_start_up_instead_of_degrading() -> None:
    with pytest.raises(PackLoadError):
        build_service(pack_directory=FIXTURES / "missing_source_url")


def test_the_wired_server_is_usable() -> None:
    server = build_server(build_service())
    assert server.name == "dependency-compat-mcp"
    assert server.instructions
