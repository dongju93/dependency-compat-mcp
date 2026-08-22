"""The MCP boundary: tool registration, error translation, transports.

This layer knows about MCP and about the public contract models; it knows nothing about
PyPI, npm or the decision procedure. Its whole job is to turn a request into parsed
domain values, hand them to the service, and turn the outcome into either a structured
result or an explicit tool error.

Three things here are not defaults and are worth the reading time:

* **Only one protocol revision is served.** The SDK serves the pre-2026 `initialize`
  handshake alongside the revision 01 fixed, and picks between them from the client's first
  frame - silently, without an error. `reject_legacy_protocol` refuses every request that is
  not on `PROTOCOL_VERSION`, so what `server/discover` advertises is also all this server
  will answer.
* **The top-level arguments object is closed by middleware.** 02 rejects undefined fields,
  including its own example, ``repository_path``. The SDK enforces ``extra="forbid"`` for
  nested models but builds the top-level arguments model itself, so the check lives in an
  always-executed middleware rather than in a schema the SDK would overwrite. The advertised
  ``inputSchema`` is patched to match, so ``tools/list`` and runtime behaviour agree.
* **``unknown`` never travels as an error.** Only contract violations and broken server
  invariants become ``ToolError``; a request the server could not answer comes back as a
  perfectly ordinary structured result.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.context import HandlerResult, ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import (
    UNSUPPORTED_PROTOCOL_VERSION,
    CallToolResult,
    TextContent,
    UnsupportedProtocolVersionErrorData,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import ValidationError

from dependency_compat_mcp import __version__
from dependency_compat_mcp.contracts.inputs import TargetInput
from dependency_compat_mcp.contracts.outputs import (
    CheckCompatibilityResult,
    GetCompatibilityContextResult,
)
from dependency_compat_mcp.descriptions import (
    CHECK_COMPATIBILITY_DESCRIPTION,
    GET_COMPATIBILITY_CONTEXT_DESCRIPTION,
    SERVER_INSTRUCTIONS,
)
from dependency_compat_mcp.domain.errors import InputError, InvariantViolation
from dependency_compat_mcp.service import CompatibilityService

__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOL_ARGUMENT_NAMES",
    "build_server",
    "reject_legacy_protocol",
    "reject_unknown_arguments",
]

logger = logging.getLogger(__name__)

SERVER_NAME: Final = "dependency-compat-mcp"
SERVER_VERSION: Final = __version__

# The one revision this server implements (01). Written out rather than read from the SDK
# so a release that widens `MODERN_PROTOCOL_VERSIONS` cannot quietly widen what this server
# answers to; `_pin_protocol_version` turns that drift into a start-up failure.
PROTOCOL_VERSION: Final = "2026-07-28"

# The single source of truth for "which top-level keys does each tool accept".
TOOL_ARGUMENT_NAMES: Final[dict[str, frozenset[str]]] = {
    "check_compatibility": frozenset({"subject", "counterpart"}),
    "get_compatibility_context": frozenset({"target"}),
}


# --------------------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------------------


@contextmanager
def _as_tool_error() -> Iterator[None]:
    """Map the two tool-error classes onto ``ToolError`` and let everything else through.

    ``InputError`` is a contract violation by the caller; ``InvariantViolation`` (raised by
    assembly, including from a model validator) is a defect in this server. Both are errors.
    Insufficient evidence is neither and never reaches here - it is a normal result.
    """
    try:
        yield
    except InputError as exc:
        raise ToolError(f"invalid input ({exc.code}): {exc}") from exc
    except InvariantViolation as exc:
        logger.exception("server invariant violated")
        raise ToolError(f"server invariant violated: {exc}") from exc
    except ValidationError as exc:
        # A response model rejecting its own assembly is an invariant violation too: the
        # constructors should have made that state unreachable.
        logger.exception("assembled response violated its own contract")
        raise ToolError(f"server invariant violated: {exc}") from exc


# --------------------------------------------------------------------------------------
# Boundary middleware
# --------------------------------------------------------------------------------------


async def reject_legacy_protocol(
    ctx: ServerRequestContext[Any, Any],
    call_next: Any,
) -> HandlerResult:
    """Serve ``PROTOCOL_VERSION`` and nothing else (01).

    01 fixes one revision, but the SDK keeps the pre-2026 ``initialize`` handshake reachable
    beside it and routes to whichever the client opens with - on stdio from the first frame,
    on Streamable HTTP from the ``MCP-Protocol-Version`` header. Neither route errors: an
    unrecognised version offered to the handshake is counter-offered the newest handshake
    revision, and a request with no version header defaults to one. Without this middleware a
    2025 client is quietly served the 2025 protocol by a server whose ``server/discover``
    says it speaks only 2026-07-28.

    One check covers both transports because every request - ``initialize`` included -
    reaches the handler through this chain, and ``ctx.protocol_version`` is already the
    connection's negotiated revision by then. ``initialize`` commits its negotiation only
    after the chain returns, so refusing here leaves no half-open legacy connection behind.

    Unlike `reject_unknown_arguments` this *raises*, and deliberately: the caller is not
    holding a tool result the server declined to fill in, it is speaking a protocol this
    server does not implement. -32022 with the supported list is what a client needs in order
    to reconnect correctly, and it is the same code the SDK itself answers with.
    """
    if ctx.protocol_version == PROTOCOL_VERSION:
        return await call_next(ctx)

    # On the handshake the connection still carries the SDK's seeded default, so the version
    # the caller actually asked for is in the params - report that, not the placeholder.
    requested = ctx.protocol_version
    if ctx.method == "initialize" and isinstance(ctx.params, dict):
        proposed = ctx.params.get("protocolVersion")
        if isinstance(proposed, str):
            requested = proposed
    logger.warning("refused a request on protocol %s", requested)
    raise MCPError(
        code=UNSUPPORTED_PROTOCOL_VERSION,
        message=(
            f"this server implements MCP {PROTOCOL_VERSION} only; carry the per-request "
            "protocol envelope in `params._meta` instead of opening an initialize handshake"
        ),
        data=UnsupportedProtocolVersionErrorData(
            supported=[PROTOCOL_VERSION], requested=requested
        ).model_dump(mode="json"),
    )


async def reject_unknown_arguments(
    ctx: ServerRequestContext[Any, Any],
    call_next: Any,
) -> HandlerResult:
    """Close the top-level arguments object, which the SDK leaves open.

    Runs before validation, lookup and the handler, so a typo or an out-of-contract field
    such as ``repository_path`` cannot be silently ignored (02 "오류가 되는 입력").

    It *returns* a failed ``CallToolResult`` rather than raising. The SDK turns an
    exception raised inside a tool into exactly that shape, and a nested undefined field is
    already rejected that way; raising here instead would make one class of contract
    violation arrive as a JSON-RPC error and its sibling as a tool result. A model caller
    would have to handle two failure shapes for the same mistake.
    """
    if ctx.method == "tools/call" and isinstance(ctx.params, dict):
        name = ctx.params.get("name")
        arguments = ctx.params.get("arguments")
        allowed = TOOL_ARGUMENT_NAMES.get(name) if isinstance(name, str) else None
        if allowed is not None and isinstance(arguments, dict):
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                message = (
                    f"invalid input (unknown_argument): {name} does not accept "
                    f"{', '.join(unknown)}. "
                    f"Accepted arguments: {', '.join(sorted(allowed))}."
                )
                return CallToolResult(
                    content=[TextContent(type="text", text=message)], is_error=True
                )
    return await call_next(ctx)


def _close_input_schema(server: MCPServer, tool_name: str) -> None:
    """Advertise the closure the middleware enforces, so schema and behaviour agree."""
    tool = server._tool_manager.get_tool(tool_name)
    if tool is None:  # pragma: no cover - registration just happened
        raise InvariantViolation(f"tool {tool_name!r} was not registered")
    tool.parameters["additionalProperties"] = False


def _inline_root_ref(server: MCPServer, tool_name: str) -> None:
    """Publish the output union at the root instead of behind a ``$ref``.

    ``RootModel`` makes pydantic emit ``{"$ref": "#/$defs/CheckResult", "$defs": {...}}``.
    That is valid JSON Schema, but the discriminator - the single most useful thing about
    this response - is then one dereference away, and 01 forbids relying on ``$ref``
    resolution behaviour a client may not implement. Hoisting the referenced schema into
    the root keeps ``$defs`` intact for the nested types while putting ``oneOf`` and
    ``discriminator`` where a reader looks first.
    """
    tool = server._tool_manager.get_tool(tool_name)
    if tool is None:  # pragma: no cover - registration just happened
        raise InvariantViolation(f"tool {tool_name!r} was not registered")
    schema = tool.fn_metadata.output_schema
    if schema is None:  # pragma: no cover - both tools return a model
        raise InvariantViolation(f"tool {tool_name!r} publishes no output schema")

    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return
    definitions = schema.get("$defs", {})
    name = reference.rsplit("/", 1)[-1]
    root = definitions.get(name)
    if not isinstance(root, dict):  # pragma: no cover - pydantic always emits the def
        raise InvariantViolation(f"output schema of {tool_name!r} has a dangling $ref")

    del schema["$ref"]
    # `title`/`description` already on the wrapper stay; the union's own keys win.
    schema.update({key: value for key, value in root.items() if key != "title"})
    # `outputSchema.type` is required on the wire, and a bare `oneOf` has none: without
    # this the tool list fails to serialise over stdio and Streamable HTTP while still
    # passing an in-memory client, which skips result serialisation entirely. Every
    # variant of both unions is an object, so declaring it is a statement of fact.
    schema["type"] = "object"


def _pin_protocol_version() -> None:
    """Fail at build time if the SDK stops treating ``PROTOCOL_VERSION`` as the only modern one.

    ``server/discover`` advertises the SDK's list while `reject_legacy_protocol` enforces the
    pinned constant, so the two have to be the same set or the server would advertise a
    revision it refuses. A new revision in a future SDK is a decision for 01 to make, and
    this turns it into a start-up failure rather than a contradiction served to clients.
    """
    if tuple(MODERN_PROTOCOL_VERSIONS) != (PROTOCOL_VERSION,):
        raise InvariantViolation(
            f"the SDK now serves {list(MODERN_PROTOCOL_VERSIONS)} as current protocol "
            f"revisions, but 01 fixes this server at {PROTOCOL_VERSION}"
        )


def _withdraw_unimplemented_capabilities(server: MCPServer) -> None:
    """Stop advertising Resources and Prompts.

    01 is explicit: a feature that is not implemented is left out of the advertised
    capabilities rather than mimicked with an empty implementation. The SDK registers
    ``prompts/*`` and ``resources/*`` handlers unconditionally, and it derives capabilities
    from the presence of those handlers, so an empty prompt list would be advertised as a
    real Prompts capability. Removing the handlers makes the advertisement honest and makes
    the methods answer METHOD_NOT_FOUND, which is what a server without them should say.
    """
    handlers = server._lowlevel_server._request_handlers
    for method in (
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/read",
        "resources/templates/list",
    ):
        handlers.pop(method, None)


# --------------------------------------------------------------------------------------
# Server construction
# --------------------------------------------------------------------------------------


def build_server(service: CompatibilityService) -> MCPServer:
    """Register the two tools of 02 against ``service`` on the protocol of 01.

    Only Tools are advertised. Resources and Prompts are deliberately absent (01): this
    server publishes no documents of its own - every fact in a response is fetched from an
    official source for that request - and a deterministic rule engine has nothing to
    prompt about.
    """
    _pin_protocol_version()
    server = MCPServer(
        name=SERVER_NAME,
        title="Dependency compatibility",
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        # Outermost first: a request on a protocol this server does not implement is refused
        # before anything reads its arguments.
        middleware=[reject_legacy_protocol, reject_unknown_arguments],
    )

    @server.tool(
        name="check_compatibility",
        title="Check compatibility of two exact versions",
        description=CHECK_COMPATIBILITY_DESCRIPTION,
    )
    async def check_compatibility(
        subject: TargetInput, counterpart: TargetInput
    ) -> CheckCompatibilityResult:
        with _as_tool_error():
            parsed_subject = subject.parse()
            parsed_counterpart = counterpart.parse()
            return await service.check_compatibility(parsed_subject, parsed_counterpart)

    @server.tool(
        name="get_compatibility_context",
        title="Get compatibility context for one exact version",
        description=GET_COMPATIBILITY_CONTEXT_DESCRIPTION,
    )
    async def get_compatibility_context(
        target: TargetInput,
    ) -> GetCompatibilityContextResult:
        with _as_tool_error():
            parsed_target = target.parse()
            return await service.get_compatibility_context(parsed_target)

    for tool_name in TOOL_ARGUMENT_NAMES:
        _close_input_schema(server, tool_name)
        _inline_root_ref(server, tool_name)
    _withdraw_unimplemented_capabilities(server)

    return server
