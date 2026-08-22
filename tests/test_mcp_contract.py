"""The MCP surface, checked against 02 and 04 through a real in-memory client.

Reading the models is not enough here. What a caller sees is whatever the SDK advertises
and returns, so these tests go through ``Client(server)`` and assert on the wire shapes:
the published ``inputSchema``, the published ``outputSchema``, the structured result, and
which failures arrive as tool errors rather than as an ``unknown`` verdict.
"""

import json
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp_types import TextContent

from dependency_compat_mcp.descriptions import (
    CHECK_COMPATIBILITY_DESCRIPTION,
    GET_COMPATIBILITY_CONTEXT_DESCRIPTION,
    REQUIRED_ELEMENTS,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.server import PROTOCOL_VERSION, build_server
from tests.conftest import FakeFetcher, build_service, pypi_release, pypi_url

DJANGO = pypi_release("Django", "5.2", requires_python=">=3.10,<3.14")


@pytest.fixture
def server() -> MCPServer:
    fetcher = FakeFetcher(payloads={pypi_url("django", "5.2"): DJANGO})
    return build_server(build_service(fetcher))


def _tool(tools: list[Any], name: str) -> Any:
    return next(tool for tool in tools if tool.name == name)


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
    return node


# --------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_in_memory_client_is_served_the_protocol_01_fixed(
    server: MCPServer,
) -> None:
    """The in-memory leg of 01's three completion criteria, pinned to a revision.

    ``Client`` picks the protocol by probing, so this asserts what the other tests in this
    file are implicitly running on. Without it they would keep passing on whatever the SDK
    negotiated, which is how a 2025 connection stayed invisible here before.
    """
    async with Client(server) as client:
        assert client.protocol_version == PROTOCOL_VERSION


def test_the_server_refuses_to_build_if_the_sdk_widens_the_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new revision in a future SDK is 01's decision, not a silent one.

    ``server/discover`` advertises the SDK's list while the middleware enforces the pinned
    constant. Letting those drift would advertise a revision the server then refuses, so the
    disagreement is a start-up failure, for the reason this server prefers throughout:
    better a server that will not boot than one serving a
    contract it does not honour.
    """
    monkeypatch.setattr(
        "dependency_compat_mcp.server.MODERN_PROTOCOL_VERSIONS",
        (PROTOCOL_VERSION, "2027-01-01"),
    )
    fetcher = FakeFetcher(payloads={})
    with pytest.raises(InvariantViolation, match=PROTOCOL_VERSION):
        build_server(build_service(fetcher))


# --------------------------------------------------------------------------------------
# tools/list
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_the_two_documented_tools_are_advertised(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert sorted(tool.name for tool in tools) == [
        "check_compatibility",
        "get_compatibility_context",
    ]


@pytest.mark.anyio
async def test_no_resources_or_prompts_are_advertised(server: MCPServer) -> None:
    """01: this server publishes no documents of its own, so it advertises none.

    Advertising a capability that is not implemented would enlarge the state space both
    sides have to handle for nothing.
    """
    async with Client(server) as client:
        assert client.server_capabilities is not None
        assert client.server_capabilities.resources is None
        assert client.server_capabilities.prompts is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("check_compatibility", ["subject", "counterpart"]),
        ("get_compatibility_context", ["target"]),
    ],
)
async def test_input_schema_matches_02(
    server: MCPServer, tool_name: str, arguments: list[str]
) -> None:
    async with Client(server) as client:
        schema = _tool((await client.list_tools()).tools, tool_name).input_schema

    assert schema["type"] == "object"
    assert sorted(schema["required"]) == sorted(arguments)
    assert sorted(schema["properties"]) == sorted(arguments)
    # 02 rejects undefined fields at the top level too, and the advertised schema has to
    # say so or a caller cannot know before trying.
    assert schema["additionalProperties"] is False

    for argument in arguments:
        target = _resolve(schema, schema["properties"][argument])
        assert target["additionalProperties"] is False
        assert sorted(target["required"]) == ["name", "namespace", "version"]
        assert (
            target["properties"]["namespace"]["pattern"] == r"^[a-z][a-z0-9_-]{0,31}$"
        )
        assert target["properties"]["namespace"]["maxLength"] == 32
        assert target["properties"]["name"]["maxLength"] == 200
        assert target["properties"]["version"]["maxLength"] == 100
        for field in ("namespace", "name", "version"):
            assert target["properties"][field]["minLength"] == 1


@pytest.mark.anyio
async def test_check_output_schema_is_the_three_variant_sum_type(
    server: MCPServer,
) -> None:
    async with Client(server) as client:
        schema = _tool(
            (await client.list_tools()).tools, "check_compatibility"
        ).output_schema

    assert schema is not None
    assert schema["discriminator"]["propertyName"] == "verdict"
    assert sorted(schema["discriminator"]["mapping"]) == [
        "supported",
        "unknown",
        "unsupported",
    ]

    variants = {
        name: definition
        for name, definition in schema["$defs"].items()
        if name.endswith("Result")
    }
    supported = variants["SupportedResult"]
    unknown = variants["UnknownResult"]
    # 04: reason and verdict_evidence_ids never coexist, and the schema is where that is
    # guaranteed rather than merely intended.
    assert "verdict_evidence_ids" in supported["required"]
    assert "reason" not in supported["properties"]
    assert "reason" in unknown["required"]
    assert "verdict_evidence_ids" not in unknown["properties"]
    assert supported["properties"]["verdict_evidence_ids"]["minItems"] == 1

    # Only the open verdict can name what left it open.
    assert "decision_causes" in unknown["properties"]
    assert "decision_causes" not in supported["properties"]
    causes = schema["$defs"]["ConditionalClaimOut"]
    # A cause's condition is the two-marker union, not the wider condition union: a
    # conditional claim guarded by nothing in particular has no schema to express it.
    assert causes["properties"]["condition"]["$ref"].endswith("MarkerGuardOut")
    # The published enums are the domain's own closed sets, reached by $ref rather than
    # restated in the model - so the schema cannot drift from what the server produces.
    assert sorted(schema["$defs"]["GuardKind"]["enum"]) == [
        "environment_marker",
        "extra_marker",
    ]
    assert sorted(schema["$defs"]["UnprovenKind"]["enum"]) == [
        "claim_outside_range",
        "lifecycle_unavailable",
        "open_upper_bound",
        "stale_lower_bound",
        "tier_c_only",
        "uncomparable_claim",
    ]
    assert (
        schema["$defs"]["UnprovenClaimOut"]["properties"]["evidence_ids"]["minItems"]
        == 1
    )


@pytest.mark.anyio
async def test_a_source_check_names_the_release_it_was_made_for(
    server: MCPServer,
) -> None:
    """Without the target, two lookups of one registry collapse into one unreadable row."""
    async with Client(server) as client:
        schema = _tool(
            (await client.list_tools()).tools, "check_compatibility"
        ).output_schema

    check = schema["$defs"]["SourceCheckOut"]
    for field in ("source", "target", "role", "required", "outcome"):
        assert field in check["required"], field
    assert sorted(schema["$defs"]["LookupRole"]["enum"]) == [
        "declared_about",
        "declaring",
    ]


@pytest.mark.anyio
async def test_a_context_constraint_publishes_its_condition(server: MCPServer) -> None:
    async with Client(server) as client:
        schema = _tool(
            (await client.list_tools()).tools, "get_compatibility_context"
        ).output_schema

    constraint = schema["$defs"]["ConstraintOut"]
    assert "condition" in constraint["required"]
    condition = schema["$defs"]["ConditionOut"]
    assert condition["discriminator"]["propertyName"] == "kind"
    assert sorted(condition["discriminator"]["mapping"]) == [
        "decided_marker",
        "environment_marker",
        "extra_marker",
        "unconditional",
    ]


@pytest.mark.anyio
async def test_context_output_schema_is_the_two_variant_sum_type(
    server: MCPServer,
) -> None:
    async with Client(server) as client:
        schema = _tool(
            (await client.list_tools()).tools, "get_compatibility_context"
        ).output_schema

    assert schema is not None
    assert schema["discriminator"]["propertyName"] == "availability"
    for name in ("ContextAvailableResult", "ContextUnknownResult"):
        variant = schema["$defs"][name]
        assert "constraints" in variant["required"]
        assert "sources_checked" in variant["required"]
        # The curated tier is gone, and so is the field that described how deep it went.
        assert "depth" not in variant["properties"]
        assert "changes" not in variant["properties"]
    assert "reason" in schema["$defs"]["ContextUnknownResult"]["required"]
    assert "reason" not in schema["$defs"]["ContextAvailableResult"]["properties"]


@pytest.mark.anyio
async def test_relation_is_a_sum_type_with_no_invented_declaring(
    server: MCPServer,
) -> None:
    async with Client(server) as client:
        schema = _tool(
            (await client.list_tools()).tools, "check_compatibility"
        ).output_schema

    unsupported = schema["$defs"]["UnsupportedRelationOut"]
    for absent in ("rule", "direction", "declaring", "declared_about"):
        assert absent not in unsupported["properties"]


@pytest.mark.anyio
async def test_descriptions_are_the_pinned_constants(server: MCPServer) -> None:
    """02 makes the description part of the interface, so it is compared, not sampled."""
    async with Client(server) as client:
        tools = (await client.list_tools()).tools

    assert (
        _tool(tools, "check_compatibility").description
        == CHECK_COMPATIBILITY_DESCRIPTION
    )
    assert (
        _tool(tools, "get_compatibility_context").description
        == GET_COMPATIBILITY_CONTEXT_DESCRIPTION
    )
    for tool in tools:
        lowered = (tool.description or "").lower()
        for element in REQUIRED_ELEMENTS:
            assert any(probe in lowered for probe in element.probes), (
                f"{tool.name} description is missing the {element.name} element"
            )


# --------------------------------------------------------------------------------------
# tools/call
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_structured_output_is_returned_unwrapped(server: MCPServer) -> None:
    """`RootModel` keeps the response at the top level instead of nesting it under `result`."""
    async with Client(server) as client:
        result = await client.call_tool(
            "check_compatibility",
            {
                "subject": {"namespace": "pypi", "name": "django", "version": "5.2"},
                "counterpart": {
                    "namespace": "runtime",
                    "name": "python",
                    "version": "3.13.0",
                },
            },
        )

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["verdict"] == "supported"
    assert "result" not in structured
    # The text block must be the same facts, not a second, looser telling of them - so
    # it is compared whole rather than sampled at one key.
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text) == structured


@pytest.mark.anyio
async def test_every_diagnostic_array_is_present_even_when_empty(
    server: MCPServer,
) -> None:
    """04: "absent field" and "checked, nothing found" must not look the same."""
    async with Client(server) as client:
        result = await client.call_tool(
            "check_compatibility",
            {
                "subject": {"namespace": "pypi", "name": "django", "version": "5.2"},
                "counterpart": {
                    "namespace": "runtime",
                    "name": "python",
                    "version": "3.13.0",
                },
            },
        )
    structured = result.structured_content
    for key in ("evidence", "notices", "limitations", "sources_checked"):
        assert key in structured
        assert isinstance(structured[key], list)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        pytest.param(
            {
                "subject": {"namespace": "pypi", "name": "django", "version": "5.2"},
                "counterpart": {
                    "namespace": "runtime",
                    "name": "python",
                    "version": "3.13.0",
                },
                "repository_path": "/workspace/project",
            },
            "an undefined top-level field",
            id="undefined-top-level-field",
        ),
        pytest.param(
            {
                "subject": {
                    "namespace": "pypi",
                    "name": "django",
                    "version": "5.2",
                    "extra": 1,
                },
                "counterpart": {
                    "namespace": "runtime",
                    "name": "python",
                    "version": "3.13.0",
                },
            },
            "an undefined nested field",
            id="undefined-nested-field",
        ),
        pytest.param(
            {"subject": {"namespace": "pypi", "name": "django", "version": "5.2"}},
            "a missing required argument",
            id="missing-argument",
        ),
        pytest.param(
            {
                "subject": {"namespace": "maven", "name": "guava", "version": "33.0"},
                "counterpart": {
                    "namespace": "runtime",
                    "name": "python",
                    "version": "3.13.0",
                },
            },
            "an unregistered namespace",
            id="unregistered-namespace",
        ),
        pytest.param(
            {
                "subject": {"namespace": "runtime", "name": "ruby", "version": "3.3.0"},
                "counterpart": {
                    "namespace": "runtime",
                    "name": "python",
                    "version": "3.13.0",
                },
            },
            "an unregistered runtime",
            id="unregistered-runtime",
        ),
        pytest.param(
            {
                "subject": {
                    "namespace": "pypi",
                    "name": "django",
                    "version": ">=5.0,<6.0",
                },
                "counterpart": {
                    "namespace": "runtime",
                    "name": "python",
                    "version": "3.13.0",
                },
            },
            "a version range",
            id="version-range",
        ),
        pytest.param(
            {
                "subject": {"namespace": "npm", "name": "react", "version": "v19.1.1"},
                "counterpart": {
                    "namespace": "runtime",
                    "name": "node",
                    "version": "22.17.0",
                },
            },
            "a non-canonical version the server refuses to rewrite",
            id="non-canonical-version",
        ),
    ],
)
async def test_contract_violations_are_tool_errors_not_unknown_verdicts(
    server: MCPServer, arguments: dict[str, Any], reason: str
) -> None:
    """04's boundary: a bad request fails, a hard question returns `unknown`."""
    async with Client(server) as client:
        result = await client.call_tool("check_compatibility", arguments)

    assert result.is_error is True, f"{reason} must be a tool error"
    assert result.structured_content is None


@pytest.mark.anyio
async def test_an_unanswerable_question_is_a_normal_result(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "check_compatibility",
            {
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
        )

    assert result.is_error is False
    assert result.structured_content["verdict"] == "unknown"
    assert result.structured_content["reason"] == "relation_not_supported"


@pytest.mark.anyio
async def test_context_tool_round_trips(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "get_compatibility_context",
            {"target": {"namespace": "pypi", "name": "django", "version": "5.2"}},
        )

    assert result.is_error is False
    structured = result.structured_content
    assert structured["availability"] == "available"
