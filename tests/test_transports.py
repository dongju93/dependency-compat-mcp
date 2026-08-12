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
                "params": params or {},
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
    probe = StdioProbe(process)
    try:
        handshake = probe.request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1"},
            },
        )
        assert "result" in handshake, handshake
        probe.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        yield probe
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
        log.close()


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
# 2026-07-28 makes each request self-contained: version, capabilities and identity ride in
# `_meta` rather than being inferred from the connection. That is what lets the server run
# stateless, so the probe sends them on every call.
REQUEST_META: dict[str, Any] = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "probe", "version": "1"},
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
