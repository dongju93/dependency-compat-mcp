"""Both transports of 01, over their real wire formats.

These exist because the in-memory client is not a substitute for them. It skips result
serialisation, so a response that no real client could ever receive still passes there -
which is exactly how a missing ``outputSchema.type`` survived nineteen green contract
tests. 01 lists the in-memory check, the stdio run and the ``/mcp`` endpoint as three
separate completion criteria for this reason.
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from dependency_compat_mcp.server import build_server
from tests.conftest import FakeFetcher, build_service, pypi_release, pypi_url

PROTOCOL_VERSION = "2026-07-28"
# A revision the SDK still knows how to serve, used to prove this server no longer does.
LEGACY_VERSION = "2025-06-18"
# 2026-07-28 makes each request self-contained: version, capabilities and identity ride in
# `_meta` rather than being inferred from the connection. That is what lets the server run
# stateless, so both probes below stamp them on every call.
#
# This envelope also picks the protocol era, which is why neither probe opens with
# `initialize`. The SDK serves the handshake era too and lets the client's first frame
# decide; a handshake opens a *legacy* connection even when it carries this envelope, and an
# unrecognised version there is counter-offered the newest handshake revision rather than
# refused. A probe that handshakes therefore proves the 2025 protocol works and says nothing
# about the one 01 fixed.
REQUEST_META: dict[str, Any] = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "probe", "version": "1"},
}
DJANGO_PAYLOADS = {
    pypi_url("django", "5.2"): pypi_release(
        "Django", "5.2", requires_python=">=3.10,<3.14"
    )
}

CHECK_ARGUMENTS = {
    "subject": {"namespace": "pypi", "name": "django", "version": "5.2"},
    "counterpart": {"namespace": "runtime", "name": "python", "version": "3.13.0"},
}


# --------------------------------------------------------------------------------------
# stdio
# --------------------------------------------------------------------------------------


class StdioProbe:
    """A minimal hand-rolled client.

    Deliberately not the SDK client: the point is to see the bytes a third-party host
    would see, including that stdout carries protocol frames and nothing else.
    """

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process

    def send(self, message: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        assert self._process.stdout is not None
        line = self._process.stdout.readline()
        assert line, "server closed stdout"
        return json.loads(line)

    def request(
        self, identifier: int, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": method,
                "params": {**(params or {}), "_meta": REQUEST_META},
            }
        )
        return self.receive()


@pytest.fixture
def stdio_probe(tmp_path: Path) -> Iterator[StdioProbe]:
    # stderr goes to a file, never a pipe: on stdio the server logs to stderr, and an
    # undrained pipe would deadlock the process the moment the buffer filled.
    log = (tmp_path / "server.err").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "dependency_compat_mcp", "--transport", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
        bufsize=1,
    )
    # No handshake and no `notifications/initialized`: at 2026-07-28 there is no
    # connection-level state to establish, so the first tool call is also the first frame.
    try:
        yield StdioProbe(process)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
        log.close()


@pytest.mark.slow
def test_stdio_serves_only_the_protocol_01_fixed(stdio_probe: StdioProbe) -> None:
    """`server/discover` is how a 2026-07-28 server states its versions (01).

    It gets its own test because the failure it guards is silent rather than red: the SDK
    serves the handshake era as well, and a client that opens with `initialize` is quietly
    counter-offered an older revision. Every assertion below would still pass one protocol
    revision too old, which is exactly what this file exists to rule out.
    """
    response = stdio_probe.request(1, "server/discover")
    assert "error" not in response, response
    assert response["result"]["supportedVersions"] == [PROTOCOL_VERSION]


@pytest.mark.slow
def test_stdio_refuses_the_initialize_handshake(stdio_probe: StdioProbe) -> None:
    """01 fixes one revision, so the pre-2026 handshake is an error, not a fallback.

    Sent through ``send``/``receive`` rather than ``request`` on purpose: a handshake that
    carried the modern ``_meta`` envelope would not be a handshake, and it is precisely the
    frame without one that the SDK would otherwise serve at a 2025 revision.
    """
    stdio_probe.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1"},
            },
        }
    )
    error = stdio_probe.receive()["error"]
    assert error["code"] == -32022  # UNSUPPORTED_PROTOCOL_VERSION
    # The client is told what to reconnect with; a bare refusal would strand it.
    assert error["data"] == {
        "supported": [PROTOCOL_VERSION],
        "requested": LEGACY_VERSION,
    }


@pytest.mark.slow
def test_stdio_lists_both_tools_over_the_wire(stdio_probe: StdioProbe) -> None:
    response = stdio_probe.request(2, "tools/list")
    assert "error" not in response, response
    names = sorted(tool["name"] for tool in response["result"]["tools"])
    assert names == ["check_compatibility", "get_compatibility_context"]
    for tool in response["result"]["tools"]:
        # The wire schema requires this and the in-memory path never checks it.
        assert tool["outputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False


@pytest.mark.slow
def test_stdio_returns_a_structured_result(stdio_probe: StdioProbe) -> None:
    response = stdio_probe.request(
        3,
        "tools/call",
        {
            "name": "check_compatibility",
            "arguments": {
                "subject": {
                    "namespace": "runtime",
                    "name": "python",
                    "version": "3.13.0",
                },
                "counterpart": {
                    "namespace": "runtime",
                    "name": "node",
                    "version": "22.17.0",
                },
            },
        },
    )
    structured = response["result"]["structuredContent"]
    assert structured["verdict"] == "unknown"
    assert structured["reason"] == "relation_not_supported"


@pytest.mark.slow
def test_stdio_does_not_serve_the_capabilities_it_never_advertised(
    stdio_probe: StdioProbe,
) -> None:
    """01: an unimplemented feature is left out, not faked with an empty list."""
    response = stdio_probe.request(4, "prompts/list")
    assert response["error"]["code"] == -32601  # METHOD_NOT_FOUND
    response = stdio_probe.request(5, "resources/list")
    assert response["error"]["code"] == -32601


# --------------------------------------------------------------------------------------
# Streamable HTTP
# --------------------------------------------------------------------------------------

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": PROTOCOL_VERSION,
}


def _headers(method: str, name: str | None = None, **extra: str) -> dict[str, str]:
    headers = {**BASE_HEADERS, "mcp-method": method}
    if name is not None:
        headers["mcp-name"] = name
    return {**headers, **extra}


def _body(
    identifier: int, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": method,
        "params": {**(params or {}), "_meta": REQUEST_META},
    }


@pytest.fixture
def http_app() -> Starlette:
    service = build_service(FakeFetcher(payloads=DJANGO_PAYLOADS))
    return build_server(service).streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:8000"],
            allowed_origins=["http://127.0.0.1:8000"],
        ),
    )


@pytest.mark.anyio
async def test_http_serves_the_same_tools_and_results(http_app: Starlette) -> None:
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=http_app),
            base_url="http://127.0.0.1:8000",
        ) as client,
        http_app.router.lifespan_context(http_app),
    ):
        listing = await client.post(
            "/mcp", headers=_headers("tools/list"), json=_body(1, "tools/list")
        )
        assert listing.status_code == 200
        assert sorted(t["name"] for t in listing.json()["result"]["tools"]) == [
            "check_compatibility",
            "get_compatibility_context",
        ]

        called = await client.post(
            "/mcp",
            headers=_headers("tools/call", "check_compatibility"),
            json=_body(
                2,
                "tools/call",
                {"name": "check_compatibility", "arguments": CHECK_ARGUMENTS},
            ),
        )
        assert called.status_code == 200
        assert called.json()["result"]["structuredContent"]["verdict"] == "supported"

        context = await client.post(
            "/mcp",
            headers=_headers("tools/call", "get_compatibility_context"),
            json=_body(
                3,
                "tools/call",
                {
                    "name": "get_compatibility_context",
                    "arguments": {
                        "target": {
                            "namespace": "pypi",
                            "name": "django",
                            "version": "5.2",
                        }
                    },
                },
            ),
        )
        assert context.status_code == 200
        assert context.json()["result"]["structuredContent"]["depth"] == "registry_only"


@pytest.mark.anyio
async def test_http_rejects_an_undefined_argument_the_same_way_stdio_does(
    http_app: Starlette,
) -> None:
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=http_app),
            base_url="http://127.0.0.1:8000",
        ) as client,
        http_app.router.lifespan_context(http_app),
    ):
        response = await client.post(
            "/mcp",
            headers=_headers("tools/call", "check_compatibility"),
            json=_body(
                4,
                "tools/call",
                {
                    "name": "check_compatibility",
                    "arguments": {**CHECK_ARGUMENTS, "repository_path": "/workspace"},
                },
            ),
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True


async def _post(app: Starlette, **kwargs: Any) -> Any:
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client,
        app.router.lifespan_context(app),
    ):
        return await client.post("/mcp", **kwargs)


@pytest.mark.anyio
async def test_http_refuses_a_handshake_era_version_header(http_app: Starlette) -> None:
    """On this transport the version header is what picks the era, so it is what is tested.

    The SDK routes a handshake-era value here to its pre-2026 transport, which answers the
    handshake instead of refusing it. 01 admits one revision, and the header naming another
    has to be an error rather than a downgrade.
    """
    response = await _post(
        http_app,
        headers={**BASE_HEADERS, "MCP-Protocol-Version": LEGACY_VERSION},
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == -32022  # UNSUPPORTED_PROTOCOL_VERSION
    assert error["data"] == {
        "supported": [PROTOCOL_VERSION],
        "requested": LEGACY_VERSION,
    }


@pytest.mark.anyio
async def test_http_refuses_a_request_that_states_no_protocol_at_all(
    http_app: Starlette,
) -> None:
    """The quiet case: no header, no envelope.

    The SDK reads an absent header as the spec's default-absent revision and serves the
    pre-2026 transport, so a client that simply says nothing would be answered on a protocol
    this server does not implement. The exact default is the SDK's to pick; that it is not
    the one 01 fixed is the whole assertion.
    """
    response = await _post(
        http_app,
        headers={
            key: value
            for key, value in BASE_HEADERS.items()
            if key != "MCP-Protocol-Version"
        },
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == -32022  # UNSUPPORTED_PROTOCOL_VERSION
    assert error["data"]["supported"] == [PROTOCOL_VERSION]
    assert error["data"]["requested"] != PROTOCOL_VERSION


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("header", "expected_status"),
    [
        ({"Origin": "http://evil.example"}, 403),
        ({"Host": "evil.example"}, 421),
    ],
    ids=["foreign-origin", "forged-host"],
)
async def test_http_refuses_dns_rebinding_attempts(
    http_app: Starlette, header: dict[str, str], expected_status: int
) -> None:
    """01 puts Origin/Host validation at the deployment boundary; this proves it is on."""
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=http_app),
            base_url="http://127.0.0.1:8000",
        ) as client,
        http_app.router.lifespan_context(http_app),
    ):
        response = await client.post(
            "/mcp",
            headers=_headers("tools/list", **header),
            json=_body(5, "tools/list"),
        )
    assert response.status_code == expected_status
