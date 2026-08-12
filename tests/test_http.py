"""Contract tests for the only networked module.

01 item 5 asks for contract tests on timeout, response size, allowed hosts, malformed JSON
and rate limiting; 03 [3] adds "a failed lookup is a value" and "redirects never leave the
allowlist". Every case below is one of those, driven through ``httpx2.MockTransport`` so
the suite never opens a socket.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, override

import httpx2
import pytest

from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.infra.http import (
    ALLOWED_HOSTS,
    HttpFailed,
    HttpNotFound,
    HttpOk,
    HttpxJsonFetcher,
    InvalidRequestTarget,
    build_url,
)

FIXED_NOW = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
PYPI_URL = "https://pypi.org/pypi/sample-project/5.2.1/json"

type Handler = Callable[[httpx2.Request], Any]


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive one coroutine to completion.

    Used instead of an async pytest plugin so the suite adds no dependency; each test owns
    its event loop and therefore cannot leak a task into the next one.
    """
    return asyncio.run(coroutine)


def make_fetcher(handler: Handler, **kwargs: Any) -> HttpxJsonFetcher:
    """A fetcher over an in-memory transport. Pass ``clock=None`` for the real clock."""
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), follow_redirects=False
    )
    kwargs.setdefault("clock", lambda: FIXED_NOW)
    if kwargs["clock"] is None:
        del kwargs["clock"]
    return HttpxJsonFetcher(client=client, **kwargs)


def constant(response: httpx2.Response) -> Handler:
    return lambda _request: response


class ChunkStream(httpx2.AsyncByteStream):
    """A body of unknown length, so only the streaming size check can catch it."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._count = count

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._count):
            yield self._chunk


# --------------------------------------------------------------------------------------
# build_url
# --------------------------------------------------------------------------------------


def test_build_url_percent_encodes_each_segment() -> None:
    assert (
        build_url("pypi.org", "pypi", "sample-project", "5.2.1", "json")
        == "https://pypi.org/pypi/sample-project/5.2.1/json"
    )


def test_build_url_keeps_a_scoped_npm_name_in_one_segment() -> None:
    # The slash must be escaped or the registry would read it as two path segments; the
    # `@` is a legal pchar and npm expects it unescaped.
    assert (
        build_url("registry.npmjs.org", "@example-scope/sample-helper")
        == "https://registry.npmjs.org/@example-scope%2Fsample-helper"
    )


def test_build_url_escapes_traversal_attempts() -> None:
    assert build_url("pypi.org", "pypi", "../../etc/passwd") == (
        "https://pypi.org/pypi/..%2F..%2Fetc%2Fpasswd"
    )


def test_build_url_rejects_a_host_outside_the_allowlist() -> None:
    with pytest.raises(InvalidRequestTarget) as caught:
        build_url("example.invalid", "anything")
    assert caught.value.host == "example.invalid"
    # A server-side defect, so it must reach the MCP boundary as a tool error.
    assert isinstance(caught.value, InvariantViolation)


def test_allowlist_is_exactly_the_two_registries() -> None:
    assert frozenset({"pypi.org", "registry.npmjs.org"}) == ALLOWED_HOSTS


# --------------------------------------------------------------------------------------
# Successful lookups
# --------------------------------------------------------------------------------------


def test_get_json_parses_the_body_and_stamps_the_injected_clock() -> None:
    fetcher = make_fetcher(
        constant(httpx2.Response(200, json={"info": {"version": "5.2.1"}}))
    )
    result = run(fetcher.get_json(PYPI_URL))
    assert result == HttpOk(
        url=PYPI_URL, payload={"info": {"version": "5.2.1"}}, retrieved_at=FIXED_NOW
    )


def test_get_json_sends_a_json_accept_header() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    run(make_fetcher(handler).get_json(PYPI_URL))
    assert seen[0].headers["accept"] == "application/json"
    assert "dependency-compat-mcp" in seen[0].headers["user-agent"]


# --------------------------------------------------------------------------------------
# Status handling
# --------------------------------------------------------------------------------------


def test_404_is_absence_not_failure() -> None:
    fetcher = make_fetcher(
        constant(httpx2.Response(404, json={"message": "Not Found"}))
    )
    assert run(fetcher.get_json(PYPI_URL)) == HttpNotFound(url=PYPI_URL)


def test_429_is_reported_as_rate_limited() -> None:
    fetcher = make_fetcher(constant(httpx2.Response(429, text="slow down")))
    assert run(fetcher.get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="rate_limited"
    )


@pytest.mark.parametrize("status", [400, 403, 500, 503])
def test_other_non_2xx_statuses_share_one_code(status: int) -> None:
    fetcher = make_fetcher(constant(httpx2.Response(status, text="nope")))
    assert run(fetcher.get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="unexpected_status"
    )


def test_malformed_json_is_a_failure_value() -> None:
    fetcher = make_fetcher(constant(httpx2.Response(200, content=b'{"info": {')))
    assert run(fetcher.get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="invalid_json"
    )


def test_non_utf8_body_is_a_failure_value() -> None:
    fetcher = make_fetcher(constant(httpx2.Response(200, content=b"\xff\xfe\x00")))
    assert run(fetcher.get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="invalid_json"
    )


# --------------------------------------------------------------------------------------
# Size limit
# --------------------------------------------------------------------------------------


def test_declared_content_length_over_the_ceiling_short_circuits() -> None:
    fetcher = make_fetcher(
        constant(httpx2.Response(200, content=b"x" * 64)), max_bytes=16
    )
    assert run(fetcher.get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="response_too_large"
    )


def test_streaming_body_is_abandoned_once_the_ceiling_is_passed() -> None:
    # No Content-Length is available here, so the streaming check is the only defence.
    response = httpx2.Response(200, stream=ChunkStream(b"x" * 32, count=100))
    assert "content-length" not in response.headers
    fetcher = make_fetcher(constant(response), max_bytes=64)
    assert run(fetcher.get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="response_too_large"
    )


def test_a_body_exactly_at_the_ceiling_is_accepted() -> None:
    fetcher = make_fetcher(
        constant(httpx2.Response(200, content=b'"abcd"')), max_bytes=6
    )
    assert run(fetcher.get_json(PYPI_URL)) == HttpOk(
        url=PYPI_URL, payload="abcd", retrieved_at=FIXED_NOW
    )


# --------------------------------------------------------------------------------------
# Redirects and the allowlist
# --------------------------------------------------------------------------------------


def test_a_redirect_inside_the_allowlist_is_followed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/pypi/sample-project/5.2.1/json":
            return httpx2.Response(
                301,
                headers={
                    "location": "https://pypi.org/pypi/sample-project/5.2.1/json/"
                },
            )
        return httpx2.Response(200, json={"redirected": True})

    result = run(make_fetcher(handler).get_json(PYPI_URL))
    # The originally requested URL is reported, so two runs compare equal even if the
    # registry changes its redirect chain.
    assert result == HttpOk(
        url=PYPI_URL, payload={"redirected": True}, retrieved_at=FIXED_NOW
    )


def test_a_redirect_off_the_allowlist_is_refused_before_the_hop() -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        return httpx2.Response(
            302, headers={"location": "https://evil.example/payload.json"}
        )

    assert run(make_fetcher(handler).get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="host_not_allowed"
    )
    assert requested == [PYPI_URL]


def test_a_redirect_downgrading_to_http_is_refused() -> None:
    handler = constant(
        httpx2.Response(
            302, headers={"location": "http://pypi.org/pypi/sample/1.0/json"}
        )
    )
    assert run(make_fetcher(handler).get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="host_not_allowed"
    )


def test_a_redirect_loop_is_bounded() -> None:
    hops: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hops.append(str(request.url))
        return httpx2.Response(302, headers={"location": PYPI_URL})

    assert run(make_fetcher(handler).get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="too_many_redirects"
    )
    assert len(hops) == 4  # the first attempt plus MAX_REDIRECTS hops


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/pypi/sample/1.0/json",
        "http://pypi.org/pypi/sample/1.0/json",
        "https://pypi.org.evil.example/pypi/sample/1.0/json",
        "file:///etc/passwd",
    ],
)
def test_a_url_off_the_allowlist_is_never_requested(url: str) -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - guard
        requested.append(str(request.url))
        return httpx2.Response(200, json={})

    assert run(make_fetcher(handler).get_json(url)) == HttpFailed(
        url=url, detail="host_not_allowed"
    )
    assert requested == []


def test_a_client_that_follows_redirects_is_rejected_at_construction() -> None:
    # httpx's own redirect handling would bypass the per-hop allowlist check.
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(constant(httpx2.Response(200))),
        follow_redirects=True,
    )
    with pytest.raises(InvariantViolation):
        HttpxJsonFetcher(client=client)


# --------------------------------------------------------------------------------------
# Timeouts, transport errors and cancellation
# --------------------------------------------------------------------------------------


def test_a_slow_response_becomes_a_timeout_value() -> None:
    async def handler(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json={})  # pragma: no cover - never reached

    fetcher = make_fetcher(handler, attempt_timeout=0.01)
    assert run(fetcher.get_json(PYPI_URL)) == HttpFailed(url=PYPI_URL, detail="timeout")


def test_a_transport_error_becomes_a_failure_value() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    assert run(make_fetcher(handler).get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="transport_error"
    )


def test_an_unmodelled_error_still_does_not_escape() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise ValueError("a registry surprised us")

    assert run(make_fetcher(handler).get_json(PYPI_URL)) == HttpFailed(
        url=PYPI_URL, detail="transport_error"
    )


def test_cancellation_propagates_instead_of_becoming_a_failure_value() -> None:
    """Cancellation is the caller withdrawing the question, not the registry failing.

    Swallowing it would keep a doomed task alive inside its ``TaskGroup``.
    """

    async def scenario() -> None:
        started = asyncio.Event()

        async def handler(_request: httpx2.Request) -> httpx2.Response:
            started.set()
            await asyncio.sleep(30)
            return httpx2.Response(200, json={})  # pragma: no cover - never reached

        fetcher = make_fetcher(handler, attempt_timeout=30)
        task = asyncio.create_task(fetcher.get_json(PYPI_URL))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())


# --------------------------------------------------------------------------------------
# Lifecycle and construction
# --------------------------------------------------------------------------------------


def test_the_default_clock_is_an_aware_utc_instant() -> None:
    # `retrieved_at` is compared against release tables that are aware; a naive value
    # could not be compared at all.
    fetcher = make_fetcher(constant(httpx2.Response(200, json={})), clock=None)
    result = run(fetcher.get_json(PYPI_URL))
    assert isinstance(result, HttpOk)
    assert result.retrieved_at.tzinfo is UTC


def test_a_borrowed_client_is_not_closed() -> None:
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(constant(httpx2.Response(200, json={}))),
        follow_redirects=False,
    )
    fetcher = HttpxJsonFetcher(client=client)
    run(fetcher.aclose())
    assert not client.is_closed


def test_an_owned_client_is_closed() -> None:
    fetcher = HttpxJsonFetcher()
    run(fetcher.aclose())
    assert fetcher._client.is_closed


@pytest.mark.parametrize("kwargs", [{"attempt_timeout": 0}, {"max_bytes": 0}])
def test_non_positive_limits_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HttpxJsonFetcher(**kwargs)
