"""PyPI JSON API to claims.

The per-release endpoint ``/pypi/{name}/{version}/json`` is used rather than the project
endpoint because 03 [3] forbids substituting a nearby release: the URL names the exact
version, PyPI answers 404 when it does not exist, and the parser additionally rejects a
body whose ``info.version`` is not the release that was asked for.

Which tier each field lands in is fixed by 03 "근거 티어" and is not a judgement call here:

* ``requires_python`` and ``requires_dist`` are tier A. Installers enforce them, so a
  violation really is "does not install".
* ``classifiers`` are tier C. They are an enumerated positive signal that cannot on its
  own support ``supported`` - and, per absolute rule 2, their *absence* proves nothing,
  which is why a missing classifier produces no claim at all rather than a negative one.

Nothing here raises on bad registry data. An entry that cannot be parsed into a
trustworthy claim is skipped, because a guessed claim would be indistinguishable from a
declared one once it reaches the decision procedure.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, assert_never

from packaging.requirements import InvalidRequirement, Requirement

from dependency_compat_mcp.adapters.protocol import (
    LookupFailed,
    ReleaseDocument,
    ReleaseLookup,
    ReleaseNotFound,
)
from dependency_compat_mcp.domain import versions
from dependency_compat_mcp.domain.claims import (
    Claim,
    Corroboration,
    Evidence,
    EvidenceId,
    Fetched,
    InstallationGate,
    NarrativeEvidence,
    SourceId,
    VersionConstraintEvidence,
    YankedInfo,
    analyse_marker,
)
from dependency_compat_mcp.domain.errors import InputError
from dependency_compat_mcp.domain.targets import (
    CanonicalName,
    Namespace,
    PyPITarget,
    Target,
    TargetId,
    parse_pep440_version,
    parse_pypi_name,
)
from dependency_compat_mcp.infra.http import (
    HttpFailed,
    HttpNotFound,
    HttpOk,
    JsonFetcher,
    build_url,
)

__all__ = ["PYPI_HOST", "PyPIAdapter"]

PYPI_HOST: Final = "pypi.org"

_PYTHON_RUNTIME: Final = TargetId(namespace="runtime", name=CanonicalName("python"))
_CLASSIFIER_PREFIX: Final = "Programming Language :: Python :: "
# Only dotted feature versions are enumerable support signals. A bare `3` says nothing
# about which 3.x releases were tested, and `3 :: Only` is a statement about Python 2.
_DOTTED_VERSION_RE: Final = re.compile(r"^\d+\.\d+$")


class PyPIAdapter:
    """Fetches and parses one PyPI release into domain values.

    Holds no state beyond its fetcher, so a single instance is safe to share across
    concurrent tool calls.
    """

    namespace: Namespace = "pypi"
    source_id: SourceId = "pypi_json"

    __slots__ = ("_fetcher",)

    def __init__(self, fetcher: JsonFetcher) -> None:
        self._fetcher = fetcher

    async def fetch_release(self, target: Target) -> ReleaseLookup:
        """Look up ``target`` and return a fully parsed document or a failure value."""
        if not isinstance(target, PyPITarget):
            return LookupFailed(target=target, detail="unsupported_namespace")

        url = release_url(target)
        result = await self._fetcher.get_json(url)
        match result:
            case HttpNotFound():
                return ReleaseNotFound(target=target)
            case HttpFailed(detail=detail):
                return LookupFailed(target=target, detail=detail)
            case HttpOk(payload=payload, retrieved_at=retrieved_at):
                return parse_release(target, payload, retrieved_at=retrieved_at)
            case _:  # pragma: no cover - exhaustive over HttpResult
                assert_never(result)


def release_url(target: PyPITarget) -> str:
    """The JSON endpoint for exactly this release."""
    return build_url(PYPI_HOST, "pypi", str(target.name), str(target.version), "json")


def project_page_url(target: PyPITarget) -> str:
    """The human-readable page for this release, used as ``evidence.url``.

    04 defines ``evidence.url`` as something the caller can open and check, so evidence
    points at the rendered project page rather than at the JSON document the server read.
    """
    return build_url(PYPI_HOST, "project", str(target.name), str(target.version)) + "/"


def parse_release(
    target: PyPITarget, payload: object, *, retrieved_at: datetime
) -> ReleaseLookup:
    """Turn a PyPI release document into claims, evidence and release facts.

    Pure: every input is a value, so the same fixture always produces the same output.
    """
    if not isinstance(payload, dict):
        return LookupFailed(target=target, detail="invalid_document")
    info = payload.get("info")
    if not isinstance(info, dict):
        # The envelope is not what the API documents. Individual *fields* may be missing
        # without complaint, but a missing `info` means we did not get a release document.
        return LookupFailed(target=target, detail="invalid_document")
    if _names_a_different_release(info, target):
        # Defence in depth for 03's "never substitute a nearby release".
        return ReleaseNotFound(target=target)

    urls = payload.get("urls")
    files: Sequence[object] = urls if isinstance(urls, list) else ()

    claims: list[Claim] = []
    evidence: list[Evidence] = []
    context = _Context(
        target=target,
        title=f"Distribution metadata for {target.name} {target.version}",
        url=project_page_url(target),
        provenance=Fetched(retrieved_at=retrieved_at),
    )

    _add_requires_python(info, context, claims, evidence)
    _add_requires_dist(info, context, claims, evidence)
    _add_classifiers(info, context, claims, evidence)

    return ReleaseDocument(
        target=target,
        released_at=_earliest_upload(files),
        yanked=_yanked_info(info, files),
        claims=tuple(claims),
        evidence=tuple(evidence),
    )


@dataclass(frozen=True, slots=True)
class _Context:
    """The per-document constants every evidence item shares."""

    target: PyPITarget
    title: str
    url: str
    provenance: Fetched


def _add_requires_python(
    info: dict[str, object],
    context: _Context,
    claims: list[Claim],
    evidence: list[Evidence],
) -> None:
    raw = info.get("requires_python")
    if not isinstance(raw, str) or not raw.strip():
        return
    identifier: EvidenceId = "pypi:requires_python"
    claims.append(
        InstallationGate(
            declared_about=_PYTHON_RUNTIME,
            # Verbatim: 04 hands this expression back to the caller, who must not have to
            # trust a re-serialisation of it.
            expression=raw,
            scheme="pep440",
            # `bounded_above` answers `None` for an unparseable specifier. Treating that
            # as "not bounded above" is the conservative reading: it keeps step 5 from
            # inferring a support statement from a range it could not analyse.
            bounded_above=versions.bounded_above(raw, "pep440") is True,
            lower_bound=versions.lower_bound(raw, "pep440"),
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
                f"Requires-Python {raw}."
            ),
            expression=raw,
            scheme="pep440",
            provenance=context.provenance,
        )
    )


def _add_requires_dist(
    info: dict[str, object],
    context: _Context,
    claims: list[Claim],
    evidence: list[Evidence],
) -> None:
    entries = info.get("requires_dist")
    if not isinstance(entries, list):
        return
    used: set[EvidenceId] = set()
    for entry in entries:
        if not isinstance(entry, str):
            continue
        try:
            requirement = Requirement(entry)
        except InvalidRequirement:
            # An entry that does not parse cannot become a trustworthy claim, and a
            # guessed one would be indistinguishable from a declared one downstream.
            continue
        if requirement.url is not None:
            # A direct reference identifies one artifact by URL, not a release range in
            # PyPI.  Its empty ``specifier`` must therefore not become an unconstrained
            # gate about every version of the named PyPI project.
            continue
        try:
            dependency = parse_pypi_name(requirement.name)
        except InputError:
            continue

        expression = str(requirement.specifier)
        identifier = _unique_id(f"pypi:requires_dist:{dependency}", used)
        marker = str(requirement.marker) if requirement.marker is not None else None
        claims.append(
            InstallationGate(
                declared_about=TargetId(namespace="pypi", name=dependency),
                # An empty specifier set is not "no constraint recorded": PEP 440 says it
                # admits every version, so it is a satisfied gate with no ceiling.
                expression=expression,
                scheme="pep440",
                bounded_above=versions.bounded_above(expression, "pep440") is True,
                lower_bound=versions.lower_bound(expression, "pep440"),
                condition=analyse_marker(marker) if marker is not None else None,
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
                # The verbatim entry keeps the marker visible, which 03 step 1 requires
                # for a claim that ends up `indeterminate`.
                substantiates=(
                    f"{context.target.name} {context.target.version} declares "
                    f"Requires-Dist: {entry}."
                ),
                expression=expression,
                scheme="pep440",
                provenance=context.provenance,
            )
        )


def _add_classifiers(
    info: dict[str, object],
    context: _Context,
    claims: list[Claim],
    evidence: list[Evidence],
) -> None:
    entries = info.get("classifiers")
    if not isinstance(entries, list):
        return
    enumerated = frozenset(_dotted_python_classifiers(entries))
    if not enumerated:
        return
    identifier: EvidenceId = "pypi:classifiers"
    claims.append(
        Corroboration(
            declared_about=_PYTHON_RUNTIME,
            enumerated_versions=enumerated,
            evidence_id=identifier,
        )
    )
    listed = ", ".join(sorted(enumerated, key=_feature_version_key))
    evidence.append(
        # Narrative, not VersionConstraintEvidence: an enumeration is not a range, and 03
        # keeps "carries no constraint" distinct from "carries an empty constraint".
        NarrativeEvidence(
            id=identifier,
            tier="C",
            source_type="registry_classifier",
            title=f"Trove classifiers for {context.target.name} {context.target.version}",
            url=context.url,
            substantiates=(
                f"{context.target.name} {context.target.version} lists Python "
                f"classifiers for {listed}."
            ),
            provenance=context.provenance,
        )
    )


def _dotted_python_classifiers(entries: Sequence[object]) -> Iterator[str]:
    for classifier in entries:
        if not isinstance(classifier, str) or not classifier.startswith(
            _CLASSIFIER_PREFIX
        ):
            continue
        remainder = classifier[len(_CLASSIFIER_PREFIX) :].strip()
        if _DOTTED_VERSION_RE.match(remainder):
            yield remainder


def _feature_version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _unique_id(base: EvidenceId, used: set[EvidenceId]) -> EvidenceId:
    """Keep ids unique when one package is required twice under different markers.

    Suffixes follow input order, so the ids are stable for a given document even though
    the response assembler renumbers them to ``evidence-N`` later.
    """
    if base not in used:
        used.add(base)
        return base
    suffix = 2
    while f"{base}#{suffix}" in used:
        suffix += 1
    identifier = f"{base}#{suffix}"
    used.add(identifier)
    return identifier


def _names_a_different_release(info: dict[str, object], target: PyPITarget) -> bool:
    declared = info.get("version")
    if not isinstance(declared, str):
        return False
    try:
        parsed = parse_pep440_version(declared)
    except InputError:
        # PyPI spelled the version in a form we cannot compare. Do not invent a mismatch.
        return False
    return parsed.parsed != target.version.parsed


def _earliest_upload(files: Sequence[object]) -> datetime | None:
    """The first moment any file of this release became available.

    The earliest upload is the release's publication instant; a later wheel added to the
    same release does not move when the release itself appeared, and 03 step 5 compares
    against that instant.
    """
    moments = [
        moment
        for moment in (
            _parse_timestamp(entry.get("upload_time_iso_8601"))
            for entry in files
            if isinstance(entry, dict)
        )
        if moment is not None
    ]
    return min(moments) if moments else None


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # PyPI publishes UTC; a naive value is normalised rather than left ambiguous, because
    # a naive datetime cannot be compared against the aware runtime release table.
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def _yanked_info(info: dict[str, object], files: Sequence[object]) -> YankedInfo | None:
    """Whether this release is withdrawn (PEP 592).

    ``info.yanked`` is the release-level flag. Files carry their own flags, so a release
    counts as yanked when every file is - a partially yanked release still has an
    installable file and is therefore not withdrawn.
    """
    if info.get("yanked") is True:
        return YankedInfo(reason=_reason(info.get("yanked_reason")))

    file_entries = [entry for entry in files if isinstance(entry, dict)]
    if file_entries and all(entry.get("yanked") is True for entry in file_entries):
        reasons = [_reason(entry.get("yanked_reason")) for entry in file_entries]
        return YankedInfo(reason=next((reason for reason in reasons if reason), None))
    return None


def _reason(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None
