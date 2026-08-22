"""The only place this server talks to the network.

Three rules from 01 "스키마와 보안 기준" and 03 [3] are enforced structurally here rather
than by convention:

* **The host set is hard-coded.** :func:`build_url` is the only URL constructor, it takes a
  host and pre-split path segments, and it refuses any host outside :data:`ALLOWED_HOSTS`.
  A caller cannot pass a URL through, so there is no path from user input to a request
  target. Redirects are *not* followed by the client; each hop is re-checked here.
* **Every limit is applied on the always-executed path.** Size is enforced while streaming
  (a declared ``Content-Length`` is only an early exit, never the authority), and each
  attempt runs under its own :func:`asyncio.timeout`.
* **A failed lookup is a value, not an exception** (03: ``LookupFailed``는 값이다). If a
  lookup failure escaped as an exception it would be caught somewhere up the stack and
  would never reach the decision procedure, which has an explicit branch for it.
  :class:`asyncio.CancelledError` is the single deliberate exception: cancellation is the
  caller withdrawing the question, not the registry failing to answer it.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol, assert_never
from urllib.parse import quote, urljoin, urlsplit

import httpx2

from dependency_compat_mcp.domain.errors import InvariantViolation

__all__ = [
    "ALLOWED_HOSTS",
    "DEFAULT_ATTEMPT_TIMEOUT",
    "DEFAULT_REQUEST_BUDGET",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "HttpFailed",
    "HttpNotFound",
    "HttpOk",
    "HttpResult",
    "HttpxJsonFetcher",
    "InvalidRequestTarget",
    "JsonFetcher",
    "build_url",
]

_logger: Final = logging.getLogger(__name__)

ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        # Package registries.
        "pypi.org",
        "registry.npmjs.org",
        # Runtime release existence and dates.
        "www.python.org",
        "nodejs.org",
        # Runtime support lifecycle (end-of-life) schedules.
        "peps.python.org",
        "raw.githubusercontent.com",
    }
)
"""Every host this server may open a connection to. Display-only URLs are not listed.

Each entry is the publisher of record for the facts read from it: python.org and nodejs.org
serve their own release indexes, peps.python.org serves the CPython release cycle the
devguide itself reads, and ``raw.githubusercontent.com`` serves ``nodejs/Release``'s
``schedule.json``, which is where the Node.js project publishes its support schedule.
Adding a host widens what this server will treat as fact and requires human review.
"""

MAX_RESPONSE_BYTES: Final = 5 * 1024 * 1024
"""Body ceiling. A large npm packument must not decide this server's memory use."""

DEFAULT_ATTEMPT_TIMEOUT: Final = 5.0
"""Seconds for one HTTP attempt, including reading its body."""

DEFAULT_REQUEST_BUDGET: Final = 15.0
"""Seconds for a whole tool call.

Enforced by the evidence-collection service that owns the ``asyncio.TaskGroup``, not here:
one fetcher cannot see how many lookups share the budget. Exceeding it is a
``LookupFailed`` value and therefore ``unknown / lookup_failed``, never a tool error.
"""

MAX_REDIRECTS: Final = 3
"""Redirect hops allowed. Each hop's host is re-validated against the allowlist."""

_REQUEST_HEADERS: Final[dict[str, str]] = {
    "accept": "application/json",
    "user-agent": "dependency-compat-mcp/0.1 (+https://github.com/dongju93/dependency-compat-mcp)",
}


class InvalidRequestTarget(InvariantViolation):
    """A URL was assembled for a host outside :data:`ALLOWED_HOSTS`.

    This is an :class:`~dependency_compat_mcp.domain.errors.InvariantViolation` rather than
    a lookup failure because hosts are literals in this repository: reaching it means the
    server's own code is wrong, and 03 step 7 requires such defects to surface as a tool
    error instead of hiding inside a normal ``unknown`` verdict.
    """

    __slots__ = ("host",)

    def __init__(self, host: str) -> None:
        allowed = ", ".join(sorted(ALLOWED_HOSTS))
        super().__init__(f"host {host!r} is not in the registry allowlist ({allowed}).")
        self.host = host


# --------------------------------------------------------------------------------------
# Result values
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpOk:
    """A 2xx response whose body parsed as JSON.

    ``payload`` is deliberately typed ``object``: nothing below the adapter may assume the
    registry returned the shape it documents.
    """

    url: str
    payload: object
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class HttpNotFound:
    """A 404. The distinction from :class:`HttpFailed` is what keeps 03 step 0 honest:
    "we could not look" and "it is not there" lead to different verdicts."""

    url: str


@dataclass(frozen=True, slots=True)
class HttpFailed:
    """A lookup that did not produce a document.

    ``detail`` is a **stable code**, never prose, because it is copied into
    ``SourceCheck.detail`` which 04 exposes to callers who branch on it. The codes are:
    ``timeout``, ``transport_error``, ``invalid_json``, ``response_too_large``,
    ``rate_limited``, ``unexpected_status``, ``host_not_allowed``, ``too_many_redirects``.
    """

    url: str
    detail: str


type HttpResult = HttpOk | HttpNotFound | HttpFailed


class JsonFetcher(Protocol):
    """What an adapter needs from the network, and nothing more.

    Adapters depend on this rather than on :class:`httpx2.AsyncClient` so that the whole
    adapter test suite runs against an in-memory fake and never touches a socket.
    """

    async def get_json(self, url: str) -> HttpResult: ...


# --------------------------------------------------------------------------------------
# URL construction
# --------------------------------------------------------------------------------------


def build_url(host: str, *segments: str) -> str:
    """Assemble an ``https`` URL from a hard-coded host and percent-encoded segments.

    Segments are encoded individually with ``safe="@"``: ``/`` must become ``%2F`` so a
    scoped npm name stays one path segment, while ``@`` is a legal ``pchar`` in RFC 3986
    and is what both registries expect to see unescaped.

    Raises:
        InvalidRequestTarget: when ``host`` is not in :data:`ALLOWED_HOSTS`.
    """
    if host not in ALLOWED_HOSTS:
        raise InvalidRequestTarget(host)
    path = "/".join(_quote_segment(segment) for segment in segments)
    return f"https://{host}/{path}"


def _quote_segment(segment: str) -> str:
    return quote(segment, safe="@")


def _allowed_host_of(url: str) -> str | None:
    """The host of ``url`` when the URL is https and on the allowlist, else ``None``."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return None
    host = parts.hostname
    if host is None or host.lower() not in ALLOWED_HOSTS:
        return None
    return host.lower()


# --------------------------------------------------------------------------------------
# Fetcher
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Redirect:
    """Internal: a hop that must be re-validated before it is taken."""

    location: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HttpxJsonFetcher:
    """A :class:`JsonFetcher` over ``httpx2`` with the 01/03 limits wired in.

    Args:
        client: An existing client to borrow. It must have ``follow_redirects`` disabled -
            httpx's own redirect handling would bypass the per-hop allowlist check. A
            borrowed client is *not* closed by :meth:`aclose`; whoever created it owns it.
        attempt_timeout: Wall-clock seconds for one attempt, body included.
        max_bytes: Body ceiling; the stream is abandoned as soon as it is exceeded.
        clock: Source of ``retrieved_at``. Injected so fixture tests are byte-stable -
            03 requires the same fixture to produce the same output twice.
    """

    __slots__ = ("_attempt_timeout", "_client", "_clock", "_max_bytes", "_owns_client")

    def __init__(
        self,
        *,
        client: httpx2.AsyncClient | None = None,
        attempt_timeout: float = DEFAULT_ATTEMPT_TIMEOUT,
        max_bytes: int = MAX_RESPONSE_BYTES,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if attempt_timeout <= 0:
            raise ValueError("attempt_timeout must be positive.")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive.")
        if client is not None and client.follow_redirects:
            raise InvariantViolation(
                "the registry client must not follow redirects itself; each hop's host is "
                "validated against the allowlist by HttpxJsonFetcher."
            )
        self._client = client if client is not None else _build_client(attempt_timeout)
        self._owns_client = client is None
        self._attempt_timeout = attempt_timeout
        self._max_bytes = max_bytes
        self._clock = clock

    async def get_json(self, url: str) -> HttpResult:
        """Fetch and parse ``url``, returning a failure *value* for every failure mode.

        The only exception that leaves this method is :class:`asyncio.CancelledError`.
        """
        try:
            return await self._follow(url)
        except asyncio.CancelledError:
            # Cancellation is not a lookup outcome: the caller withdrew the request, and
            # swallowing it here would keep a doomed task alive inside its TaskGroup.
            raise
        except httpx2.TimeoutException, TimeoutError:
            return HttpFailed(url=url, detail="timeout")
        except httpx2.HTTPError:
            return HttpFailed(url=url, detail="transport_error")
        except Exception:
            # A registry cannot be trusted to fail only in ways httpx models. Anything
            # else is still a failed lookup, but it is a bug signal, so it is logged.
            _logger.exception("unexpected error while fetching %s", url)
            return HttpFailed(url=url, detail="transport_error")

    async def aclose(self) -> None:
        """Close the client if this fetcher created it. A borrowed client is left alone."""
        if self._owns_client:
            await self._client.aclose()

    async def _follow(self, url: str) -> HttpResult:
        """Walk redirect hops, re-checking the allowlist before each request.

        Every result reports the *originally requested* ``url`` so that two runs against
        the same fixture compare equal even if a registry changes its redirect chain.
        """
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            if _allowed_host_of(current) is None:
                return HttpFailed(url=url, detail="host_not_allowed")
            async with asyncio.timeout(self._attempt_timeout):
                outcome = await self._attempt(current, url)
            match outcome:
                case _Redirect(location=location):
                    current = urljoin(current, location)
                case HttpOk() | HttpNotFound() | HttpFailed():
                    return outcome
                case _:  # pragma: no cover - exhaustive over the internal sum type
                    assert_never(outcome)
        return HttpFailed(url=url, detail="too_many_redirects")

    async def _attempt(
        self, request_url: str, reported_url: str
    ) -> HttpResult | _Redirect:
        async with self._client.stream(
            "GET", request_url, headers=_REQUEST_HEADERS
        ) as response:
            if response.has_redirect_location:
                return _Redirect(location=response.headers["location"])
            if response.status_code == 404:
                return HttpNotFound(url=reported_url)
            if response.status_code == 429:
                return HttpFailed(url=reported_url, detail="rate_limited")
            if not response.is_success:
                return HttpFailed(url=reported_url, detail="unexpected_status")

            oversized = HttpFailed(url=reported_url, detail="response_too_large")
            declared = response.headers.get("content-length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > self._max_bytes
            ):
                # Early exit only. The streaming check below is the authority, because a
                # server may lie about or omit Content-Length.
                return oversized

            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self._max_bytes:
                    # Leaving the context manager closes the response and stops the read.
                    return oversized

            try:
                payload = json.loads(body)
            except json.JSONDecodeError, UnicodeDecodeError:
                return HttpFailed(url=reported_url, detail="invalid_json")
            return HttpOk(url=reported_url, payload=payload, retrieved_at=self._clock())


def _build_client(attempt_timeout: float) -> httpx2.AsyncClient:
    """The default client: redirects off, small pool, per-attempt timeout."""
    return httpx2.AsyncClient(
        follow_redirects=False,
        timeout=httpx2.Timeout(attempt_timeout),
        limits=httpx2.Limits(max_connections=8, max_keepalive_connections=4),
    )
