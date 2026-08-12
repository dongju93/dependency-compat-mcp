"""npm registry packument to claims.

The registry has no per-version JSON endpoint, so the whole packument is fetched and
``versions[<exact version>]`` is selected by exact key. Nothing else is accepted: no
dist-tag, no nearest match. An unpublished version - one still listed in ``time`` but gone
from ``versions`` - is :class:`ReleaseNotFound`, because the release genuinely cannot be
installed any more.

The tier split here is the one 03 argues for explicitly:

* ``dependencies`` and ``peerDependencies`` are tier A. A resolver enforces them.
* ``engines.node`` is tier **B**, not A. npm only *warns* on a mismatch unless
  ``engine-strict`` is set, and 02 decided the server is never told the caller's npm
  configuration. Calling it a gate would let the server assert an install failure it
  cannot know about, so it is parsed as the publisher's stated support range instead.

npm has no yank concept, so ``yanked`` is always ``None``. Unpublishing removes the
version rather than marking it, which is why it maps to absence and not to a notice.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, assert_never

from dependency_compat_mcp.adapters.protocol import (
    LookupFailed,
    ReleaseDocument,
    ReleaseLookup,
    ReleaseNotFound,
)
from dependency_compat_mcp.domain import versions
from dependency_compat_mcp.domain.claims import (
    Claim,
    CompatibilityStatement,
    Evidence,
    EvidenceId,
    Fetched,
    InstallationGate,
    SourceId,
    VersionConstraintEvidence,
)
from dependency_compat_mcp.domain.errors import InputError
from dependency_compat_mcp.domain.targets import (
    CanonicalName,
    Namespace,
    NpmTarget,
    Target,
    TargetId,
    parse_npm_name,
)
from dependency_compat_mcp.infra.http import (
    HttpFailed,
    HttpNotFound,
    HttpOk,
    JsonFetcher,
    build_url,
)

__all__ = ["NPM_REGISTRY_HOST", "NpmAdapter"]

NPM_REGISTRY_HOST: Final = "registry.npmjs.org"

_NODE_RUNTIME: Final = TargetId(namespace="runtime", name=CanonicalName("node"))

# Dependency sections that describe a registry package, in the order they are parsed.
_DEPENDENCY_SECTIONS: Final[tuple[str, ...]] = ("dependencies", "peerDependencies")

# Specifier prefixes that name something other than a registry range. node-semver rejects
# most of them anyway, but naming them documents the intent: this adapter only reasons
# about versions that come from the same registry it just read.
_NON_REGISTRY_PREFIXES: Final[tuple[str, ...]] = (
    "file:",
    "link:",
    "portal:",
    "workspace:",
    "npm:",
    "git:",
    "git+",
    "github:",
    "gitlab:",
    "bitbucket:",
    "http:",
    "https:",
)


class NpmAdapter:
    """Fetches a packument and parses exactly one of its versions."""

    namespace: Namespace = "npm"
    source_id: SourceId = "npm_registry"

    __slots__ = ("_fetcher",)

    def __init__(self, fetcher: JsonFetcher) -> None:
        self._fetcher = fetcher

    async def fetch_release(self, target: Target) -> ReleaseLookup:
        """Look up ``target`` and return a fully parsed document or a failure value."""
        if not isinstance(target, NpmTarget):
            return LookupFailed(target=target, detail="unsupported_namespace")

        result = await self._fetcher.get_json(packument_url(target))
        match result:
            case HttpNotFound():
                return ReleaseNotFound(target=target)
            case HttpFailed(detail=detail):
                return LookupFailed(target=target, detail=detail)
            case HttpOk(payload=payload, retrieved_at=retrieved_at):
                return parse_packument(target, payload, retrieved_at=retrieved_at)
            case _:  # pragma: no cover - exhaustive over HttpResult
                assert_never(result)


def packument_url(target: NpmTarget) -> str:
    """The packument endpoint. A scoped name stays one segment: ``@scope%2Fpkg``."""
    return build_url(NPM_REGISTRY_HOST, str(target.name))


def package_page_url(target: NpmTarget) -> str:
    """The human-readable page used as ``evidence.url``.

    ``www.npmjs.com`` is intentionally **not** in ``ALLOWED_HOSTS``: this URL is only ever
    displayed to the caller, never fetched, so it needs no connection permission.
    """
    return f"https://www.npmjs.com/package/{target.name}/v/{target.version}"


def parse_packument(
    target: NpmTarget, payload: object, *, retrieved_at: datetime
) -> ReleaseLookup:
    """Select ``target``'s exact version from a packument and parse it. Pure."""
    if not isinstance(payload, dict):
        return LookupFailed(target=target, detail="invalid_document")
    version_map = payload.get("versions")
    if not isinstance(version_map, dict):
        return LookupFailed(target=target, detail="invalid_document")

    manifest = version_map.get(str(target.version))
    if not isinstance(manifest, dict):
        # Absent, or present but not an object. Either way this release is unusable, and
        # an entry left in `time` (an unpublish) does not make it available again.
        return ReleaseNotFound(target=target)

    claims: list[Claim] = []
    evidence: list[Evidence] = []
    context = _Context(
        target=target,
        title=f"Package manifest for {target.name} {target.version}",
        url=package_page_url(target),
        provenance=Fetched(retrieved_at=retrieved_at),
    )

    _add_engines_node(manifest, context, claims, evidence)
    for section in _DEPENDENCY_SECTIONS:
        _add_dependencies(manifest, section, context, claims, evidence)

    return ReleaseDocument(
        target=target,
        released_at=_released_at(payload, target),
        # npm has no yank: a withdrawn version is unpublished, which is absence.
        yanked=None,
        claims=tuple(claims),
        evidence=tuple(evidence),
    )


@dataclass(frozen=True, slots=True)
class _Context:
    """The per-document constants every evidence item shares."""

    target: NpmTarget
    title: str
    url: str
    provenance: Fetched


def _add_engines_node(
    manifest: Mapping[str, object],
    context: _Context,
    claims: list[Claim],
    evidence: list[Evidence],
) -> None:
    engines = manifest.get("engines")
    if not isinstance(engines, dict):
        return
    raw = engines.get("node")
    if not isinstance(raw, str) or not raw.strip():
        return

    identifier: EvidenceId = "npm:engines.node"
    claims.append(
        CompatibilityStatement(
            declared_about=_NODE_RUNTIME,
            # `engines` only ever states what the publisher supports; npm has no field for
            # an exclusion, so the stance is fixed rather than derived.
            stance="supports",
            expression=raw,
            scheme="semver",
            evidence_id=identifier,
        )
    )
    evidence.append(
        VersionConstraintEvidence(
            id=identifier,
            tier="B",
            source_type="registry_metadata",
            title=context.title,
            url=context.url,
            substantiates=(
                f"{context.target.name} {context.target.version} declares "
                f'engines.node "{raw}".'
            ),
            expression=raw,
            scheme="semver",
            provenance=context.provenance,
        )
    )


def _add_dependencies(
    manifest: Mapping[str, object],
    section: str,
    context: _Context,
    claims: list[Claim],
    evidence: list[Evidence],
) -> None:
    entries = manifest.get(section)
    if not isinstance(entries, dict):
        return
    # Sorted so the output does not depend on the registry's key order.
    for raw_name in sorted(key for key in entries if isinstance(key, str)):
        raw_range = entries[raw_name]
        if not isinstance(raw_range, str):
            continue
        if _is_non_registry_specifier(raw_range):
            continue
        if not versions.valid_expression(raw_range, "semver"):
            # Dist-tags (`latest`), aliases and shorthands are not ranges; a claim built
            # from one could not be compared against an exact version.
            continue
        try:
            dependency = parse_npm_name(raw_name)
        except InputError:
            continue

        identifier: EvidenceId = f"npm:{section}:{dependency}"
        claims.append(
            InstallationGate(
                declared_about=TargetId(namespace="npm", name=dependency),
                expression=raw_range,
                scheme="semver",
                bounded_above=versions.bounded_above(raw_range, "semver") is True,
                lower_bound=versions.lower_bound(raw_range, "semver"),
                # npm dependency ranges carry no environment condition. Optionality lives
                # in a separate section, which this adapter does not read.
                condition=None,
                evidence_id=identifier,
            )
        )
        evidence.append(
            VersionConstraintEvidence(
                id=identifier,
                tier="A",
                source_type="registry_metadata",
                title=context.title,
                url=context.url,
                substantiates=(
                    f"{context.target.name} {context.target.version} declares "
                    f'{section}["{dependency}"] = "{raw_range}".'
                ),
                expression=raw_range,
                scheme="semver",
                provenance=context.provenance,
            )
        )


def _is_non_registry_specifier(raw_range: str) -> bool:
    """Is this specifier something other than a registry version range?"""
    candidate = raw_range.strip()
    if candidate.startswith(_NON_REGISTRY_PREFIXES):
        return True
    # `user/repo` is GitHub shorthand; no SemVer range contains a slash.
    return "/" in candidate


def _released_at(payload: Mapping[str, object], target: NpmTarget) -> datetime | None:
    """When this exact version was published, from the packument's ``time`` map."""
    times = payload.get("time")
    if not isinstance(times, dict):
        return None
    raw = times.get(str(target.version))
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )
