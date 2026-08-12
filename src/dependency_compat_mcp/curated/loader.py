"""Load and validate the committed curated evidence pack and the runtime release table.

03 makes two decisions this module implements literally.

**Loading happens once, at start-up, and a schema violation is a start-up failure.**
"잘못된 근거로 조용히 동작하는 것보다 뜨지 않는 편이 낫다." Every rule below therefore
raises :class:`PackLoadError` rather than skipping a bad entry: a pack that half-loaded
would let the server answer with evidence nobody reviewed.

**Parsing happens here, once, and the rest of the server sees domain values.** Names and
versions go through the same namespace parsers as tool input
(:mod:`dependency_compat_mcp.domain.targets`), and ``applies_to`` goes through
:mod:`dependency_compat_mcp.domain.versions`. The pack cannot introduce a spelling that
tool input could not, so a curated entry and a caller's target are always comparable.

Pydantic does the shape checking. It is already present transitively via the MCP SDK, so
adding ``jsonschema`` would buy a second validation vocabulary for nothing - and the
domain rules that actually matter here (official host, valid range, canonical version)
are not expressible in a JSON Schema anyway.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Final, Literal, assert_never
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dependency_compat_mcp.domain.claims import (
    Claim,
    CompatibilityStatement,
    Curated,
    Evidence,
    EvidenceId,
    NarrativeEvidence,
    SourceType,
    VersionConstraintEvidence,
)
from dependency_compat_mcp.domain.targets import (
    CanonicalName,
    ExactVersion,
    Namespace,
    NodeRuntimeTarget,
    NpmTarget,
    PyPITarget,
    PythonRuntimeTarget,
    Target,
    TargetId,
    VersionScheme,
    name_of,
    namespace_of,
    parse_npm_name,
    parse_pep440_version,
    parse_pypi_name,
    parse_runtime_name,
    parse_semver_version,
    version_of,
)
from dependency_compat_mcp.domain.versions import admits, valid_expression

__all__ = [
    "OFFICIAL_HOSTS",
    "PACK_DIRECTORY",
    "RUNTIME_RELEASES_PATH",
    "ChangeCategory",
    "CuratedPack",
    "PackChange",
    "PackEntry",
    "PackLoadError",
    "PackStatement",
    "RuntimeRelease",
    "RuntimeReleaseTable",
    "change_evidence",
    "load_curated_pack",
    "load_runtime_releases",
    "parse_exact_version",
    "statement_claims",
]

type ChangeCategory = Literal[
    "breaking_change", "removal", "deprecation", "migration_required"
]
type Stance = Literal["supports", "excludes"]
type CuratedSourceType = Literal["official_support_policy", "official_release_note"]

PACK_DIRECTORY: Final = Path(__file__).resolve().parent / "pack"
RUNTIME_RELEASES_PATH: Final = Path(__file__).resolve().parent / "runtime_releases.json"

# A malformed table can produce thousands of validation errors; the first few name the
# problem, and the rest only bury the message a maintainer has to read.
_MAX_REPORTED_ERRORS: Final = 5

# Hosts a curated `source.url` may point at.
#
# 03: "url은 공식 도메인 allowlist를 만족해야 한다. 블로그, 포럼, 요약 사이트는 근거가
# 되지 않는다." The list is deliberately short and first-party: each host is either the
# ecosystem's own documentation/registry or the forge where a project publishes its own
# release notes. Aggregators, Q&A sites, forums (including discuss.python.org) and blog
# platforms are excluded on purpose - a summary of an announcement is not the
# announcement, and 04 defines evidence as something the caller can verify at the source.
#
# ADDING A HOST REQUIRES HUMAN REVIEW. It widens what the whole server will accept as
# evidence, so it belongs in the same review as the pack entry that needs it, with a note
# saying why the host is the publisher of record for that project.
OFFICIAL_HOSTS: Final[frozenset[str]] = frozenset(
    {
        # Python language and packaging, first-party
        "docs.python.org",
        "www.python.org",
        "peps.python.org",
        "devguide.python.org",
        "packaging.python.org",
        "pypi.org",
        "docs.pypi.org",
        # Node.js and npm, first-party
        "nodejs.org",
        "docs.npmjs.com",
        "www.npmjs.com",
        # Where projects publish their own release notes and migration guides
        "github.com",
        "raw.githubusercontent.com",
        # Project documentation sites owned by the projects themselves
        "docs.djangoproject.com",
    }
)


class PackLoadError(RuntimeError):
    """The committed pack or runtime table is unusable.

    Always fatal at start-up. Raised from always-executed code, never an ``assert``, so
    that an optimized run cannot boot with unvalidated evidence.
    """

    __slots__ = ()


# --------------------------------------------------------------------------------------
# Version parsing
# --------------------------------------------------------------------------------------


def parse_exact_version(scheme: VersionScheme, raw: str) -> ExactVersion:
    """Parse an exact version in ``scheme`` using the namespace parser from the spine.

    Exposed because the runtime table and the pack both need it, and because the server
    must never grow a second version parser: a pack entry has to be rejected by exactly
    the rules that reject a tool argument.
    """
    if scheme == "pep440":
        return parse_pep440_version(raw)
    return parse_semver_version(raw)


# --------------------------------------------------------------------------------------
# Wire models (pydantic) - shape only; the domain rules follow below
# --------------------------------------------------------------------------------------


class _Model(BaseModel):
    # `extra="forbid"` is the load-time half of "표현할 수 없으면 만들 수 없다": a typo'd
    # or invented field fails the build instead of being ignored.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _SourceModel(_Model):
    source_type: CuratedSourceType
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)


class _CounterpartModel(_Model):
    namespace: Namespace
    name: str = Field(min_length=1)


class _StatementModel(_Model):
    stance: Stance
    counterpart: _CounterpartModel
    expression: str = Field(min_length=1)
    scheme: VersionScheme
    source: _SourceModel


class _ChangeModel(_Model):
    category: ChangeCategory
    area: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    source: _SourceModel


class _EntryModel(_Model):
    namespace: Namespace
    name: str = Field(min_length=1)
    applies_to: str = Field(min_length=1)
    verified_against: list[str] = Field(default_factory=list)
    reviewed_at: date
    reviewed_by: str = Field(min_length=1)
    statements: list[_StatementModel] = Field(default_factory=list)
    changes: list[_ChangeModel] = Field(default_factory=list)


class _PackModel(_Model):
    pack_version: str = Field(min_length=1)
    entries: list[_EntryModel] = Field(default_factory=list)


class _RuntimeReleaseModel(_Model):
    version: str = Field(min_length=1)
    released_at: date
    eol_at: date | None = None


class _RuntimeTableModel(_Model):
    pack_version: str = Field(min_length=1)
    runtimes: dict[str, list[_RuntimeReleaseModel]]
    # Written by scripts/build_runtime_releases.py so a reviewer can see how old the
    # snapshot is. Not evidence freshness - that is `pack_version` - so nothing downstream
    # reads it, but it must be declared or `extra="forbid"` would reject the shipped file.
    generated_at: date | None = None


# --------------------------------------------------------------------------------------
# Domain values
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackStatement:
    """A reviewed official statement about what a release supports or excludes (tier B)."""

    stance: Stance
    counterpart: TargetId
    expression: str
    scheme: VersionScheme
    source_type: SourceType
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class PackChange:
    """A reviewed change note. Carries no version range, hence narrative evidence."""

    category: ChangeCategory
    area: str
    summary: str
    source_type: SourceType
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class PackEntry:
    """One reviewed pack entry, fully parsed.

    ``applies_to`` is a *range* in the namespace's own scheme, while ``verified_against``
    holds *exact* versions. 03 keeps them apart deliberately: a statement applies across
    the range, but only the enumerated versions were actually checked by a human, and the
    difference is reported to the caller as ``curated_not_verified_for_version``.
    """

    namespace: Namespace
    name: CanonicalName
    applies_to: str
    verified_against: tuple[ExactVersion, ...]
    reviewed_at: date
    reviewed_by: str
    statements: tuple[PackStatement, ...]
    changes: tuple[PackChange, ...]

    @property
    def scheme(self) -> VersionScheme:
        """The version syntax every expression in this entry is written in."""
        return _scheme_for(self.namespace, self.name)


@dataclass(frozen=True, slots=True)
class CuratedPack:
    """Every reviewed entry in the repository, keyed for lookup by target."""

    pack_version: str
    entries: tuple[PackEntry, ...]

    def lookup(self, target: Target) -> PackEntry | None:
        """The entry whose namespace and name match ``target`` and whose ``applies_to``
        admits its version, or ``None``.

        Entries are held in a deterministic order and the first admitting one wins, so
        two overlapping ranges cannot make the answer depend on file ordering.
        """
        namespace = namespace_of(target)
        name = name_of(target)
        version = version_of(target)
        for entry in self.entries:
            if entry.namespace != namespace or entry.name != name:
                continue
            if admits(entry.applies_to, entry.scheme, version) is True:
                return entry
        return None

    def verified_for(self, entry: PackEntry, target: Target) -> bool:
        """Did a human check this entry against *this exact* version?

        A ``False`` here does not withdraw the statement - 03 says to apply it anyway and
        record ``curated_not_verified_for_version`` - it only tells the caller how far the
        review actually reached.
        """
        if entry.namespace != namespace_of(target) or entry.name != name_of(target):
            return False
        wanted = str(version_of(target))
        return any(str(version) == wanted for version in entry.verified_against)


@dataclass(frozen=True, slots=True)
class RuntimeRelease:
    """One exact runtime release, with its date and the EOL of its release line.

    ``eol_at`` is ``None`` when upstream has published no day-precision date. It is never
    estimated: 03's staleness check must rest on an announced fact, and a guess that fires
    the check would manufacture a ``stale_lower_bound`` the upstream never declared.
    """

    version: ExactVersion
    released_at: date
    eol_at: date | None


@dataclass(frozen=True, slots=True)
class RuntimeReleaseTable:
    """The committed CPython and Node.js release snapshot.

    Static data by design (03 [2]): the lists change slowly and are small, and keeping
    them in the repository keeps the release-date and EOL facts out of the network path.
    """

    pack_version: str
    python: tuple[RuntimeRelease, ...]
    node: tuple[RuntimeRelease, ...]
    _index: dict[tuple[str, str], RuntimeRelease] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        index = {
            ("python", str(release.version)): release for release in self.python
        } | {("node", str(release.version)): release for release in self.node}
        object.__setattr__(self, "_index", index)

    def lookup(self, target: Target) -> RuntimeRelease | None:
        """The row for a runtime target, or ``None`` for anything else.

        Registry packages have no row here by construction, so a caller cannot accidentally
        read a runtime EOL for an npm package.
        """
        match target:
            case PythonRuntimeTarget(version=version):
                return self._index.get(("python", str(version)))
            case NodeRuntimeTarget(version=version):
                return self._index.get(("node", str(version)))
            case PyPITarget() | NpmTarget():
                return None
            case _:
                assert_never(target)


# --------------------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------------------


def _scheme_for(namespace: Namespace, name: CanonicalName) -> VersionScheme:
    """The version syntax an entry uses. ``runtime`` is split by name, as in 03 [2]."""
    match namespace:
        case "pypi":
            return "pep440"
        case "npm":
            return "semver"
        case "runtime":
            return "pep440" if name.value == "python" else "semver"
        case _:
            assert_never(namespace)


def _parse_name(namespace: Namespace, raw: str, *, where: str) -> CanonicalName:
    """Run ``raw`` through the namespace's own name parser."""
    try:
        match namespace:
            case "pypi":
                return parse_pypi_name(raw)
            case "npm":
                return parse_npm_name(raw)
            case "runtime":
                return CanonicalName(parse_runtime_name(raw))
    except ValueError as exc:
        raise PackLoadError(f"{where}: {exc}") from exc


def _check_url(url: str, *, where: str) -> None:
    """Reject anything that is not an ``https://`` URL on an allow-listed official host."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise PackLoadError(
            f"{where}: source url must use https, got {url!r}. Evidence has to be "
            "fetchable by the caller over a verified channel."
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise PackLoadError(f"{where}: source url {url!r} has no host.")
    if parts.username or parts.password:
        raise PackLoadError(f"{where}: source url {url!r} must not carry credentials.")
    if host not in OFFICIAL_HOSTS:
        raise PackLoadError(
            f"{where}: {host!r} is not an official evidence host. "
            "Blogs, forums and summary sites are not evidence; adding a host to "
            "OFFICIAL_HOSTS requires human review."
        )


def _build_statement(model: _StatementModel, *, where: str) -> PackStatement:
    _check_url(model.source.url, where=where)
    counterpart_name = _parse_name(
        model.counterpart.namespace,
        model.counterpart.name,
        where=f"{where} counterpart",
    )
    if not valid_expression(model.expression, model.scheme):
        raise PackLoadError(
            f"{where}: {model.expression!r} is not a valid {model.scheme} range."
        )
    return PackStatement(
        stance=model.stance,
        counterpart=TargetId(
            namespace=model.counterpart.namespace, name=counterpart_name
        ),
        expression=model.expression,
        scheme=model.scheme,
        source_type=model.source.source_type,
        title=model.source.title,
        url=model.source.url,
    )


def _build_change(model: _ChangeModel, *, where: str) -> PackChange:
    _check_url(model.source.url, where=where)
    return PackChange(
        category=model.category,
        area=model.area,
        summary=model.summary,
        source_type=model.source.source_type,
        title=model.source.title,
        url=model.source.url,
    )


def _build_entry(model: _EntryModel, *, where: str) -> PackEntry:
    name = _parse_name(model.namespace, model.name, where=where)
    scheme = _scheme_for(model.namespace, name)
    label = f"{where} entry {model.namespace}:{name}"

    if not valid_expression(model.applies_to, scheme):
        raise PackLoadError(
            f"{label}: applies_to {model.applies_to!r} is not a valid {scheme} range."
        )
    try:
        verified = tuple(
            parse_exact_version(scheme, raw) for raw in model.verified_against
        )
    except ValueError as exc:
        raise PackLoadError(f"{label}: verified_against: {exc}") from exc

    statements = tuple(
        _build_statement(statement, where=f"{label} statements[{index}]")
        for index, statement in enumerate(model.statements)
    )
    changes = tuple(
        _build_change(change, where=f"{label} changes[{index}]")
        for index, change in enumerate(model.changes)
    )
    return PackEntry(
        namespace=model.namespace,
        name=name,
        applies_to=model.applies_to,
        verified_against=verified,
        reviewed_at=model.reviewed_at,
        reviewed_by=model.reviewed_by,
        statements=statements,
        changes=changes,
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackLoadError(f"{path}: cannot be read: {exc}") from exc


def _validate[T: BaseModel](model: type[T], payload: str, path: Path) -> T:
    """Validate JSON *text*, not a decoded object.

    ``model_validate_json`` keeps strict mode while still accepting the only forms JSON
    has for a date or a datetime - a plain ``model_validate`` in strict mode would reject
    ``"2026-08-12"`` because it is a ``str``.
    """
    try:
        return model.model_validate_json(payload)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:_MAX_REPORTED_ERRORS]
        )
        suffix = (
            f" (+{exc.error_count() - _MAX_REPORTED_ERRORS} more)"
            if exc.error_count() > _MAX_REPORTED_ERRORS
            else ""
        )
        raise PackLoadError(
            f"{path}: does not match the pack schema: {details}{suffix}"
        ) from exc


# --------------------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------------------


def _entry_sort_key(entry: PackEntry) -> tuple[str, str, str]:
    """Total order over the loaded entries; `(namespace, name, applies_to)` is unique."""
    return (entry.namespace, entry.name.value, entry.applies_to)


def load_curated_pack(directory: Path | None = None) -> CuratedPack:
    """Load and validate every ``*.json`` pack file in ``directory``.

    An empty ``entries`` array is valid and expected: 03 states that entry count is not a
    completion criterion and that an implementer must not invent evidence to fill the
    file. What must never happen is a *malformed* entry loading silently, so every
    violation below aborts start-up.
    """
    root = PACK_DIRECTORY if directory is None else directory
    if not root.is_dir():
        raise PackLoadError(f"{root}: curated pack directory does not exist.")

    paths = sorted(root.glob("*.json"))
    if not paths:
        raise PackLoadError(
            f"{root}: contains no pack files. The pack envelope carries `pack_version`, "
            "which every piece of curated evidence and the cache key depend on."
        )

    pack_version: str | None = None
    entries: list[PackEntry] = []
    seen: dict[tuple[str, str, str], Path] = {}

    for path in paths:
        model = _validate(_PackModel, _read_text(path), path)
        if pack_version is None:
            pack_version = model.pack_version
        elif model.pack_version != pack_version:
            raise PackLoadError(
                f"{path}: pack_version {model.pack_version!r} differs from "
                f"{pack_version!r} in an earlier file. One repository state is one pack "
                "version, or provenance and cache invalidation stop meaning anything."
            )
        for index, entry_model in enumerate(model.entries):
            entry = _build_entry(entry_model, where=f"{path} entries[{index}]")
            key = (entry.namespace, entry.name.value, entry.applies_to)
            if key in seen:
                raise PackLoadError(
                    f"{path}: duplicate entry for {entry.namespace}:{entry.name} "
                    f"{entry.applies_to!r}, already defined in {seen[key]}. Which one "
                    "wins would otherwise depend on file order."
                )
            seen[key] = path
            entries.append(entry)

    assert pack_version is not None
    # Deterministic order makes `lookup` independent of the filesystem.
    ordered = tuple(sorted(entries, key=_entry_sort_key))
    return CuratedPack(pack_version=pack_version, entries=ordered)


def load_runtime_releases(path: Path | None = None) -> RuntimeReleaseTable:
    """Load and validate the committed runtime release snapshot."""
    source = RUNTIME_RELEASES_PATH if path is None else path
    model = _validate(_RuntimeTableModel, _read_text(source), source)

    unknown = set(model.runtimes) - {"python", "node"}
    if unknown:
        raise PackLoadError(
            f"{source}: unknown runtime(s) {sorted(unknown)}. Only registered runtimes "
            "have a release table."
        )
    missing = {"python", "node"} - set(model.runtimes)
    if missing:
        raise PackLoadError(
            f"{source}: missing release table for {sorted(missing)}. 03 forbids shipping "
            "a registered runtime with a partial or absent table."
        )

    tables: dict[str, tuple[RuntimeRelease, ...]] = {}
    for runtime, scheme in (("python", "pep440"), ("node", "semver")):
        rows = model.runtimes[runtime]
        if not rows:
            raise PackLoadError(f"{source}: the {runtime} release table is empty.")
        releases: list[RuntimeRelease] = []
        seen_versions: set[str] = set()
        for row in rows:
            try:
                version = parse_exact_version(scheme, row.version)
            except ValueError as exc:
                raise PackLoadError(
                    f"{source}: {runtime} {row.version!r}: {exc}"
                ) from exc
            if row.version in seen_versions:
                raise PackLoadError(
                    f"{source}: {runtime} lists {row.version!r} more than once."
                )
            seen_versions.add(row.version)
            releases.append(
                RuntimeRelease(
                    version=version, released_at=row.released_at, eol_at=row.eol_at
                )
            )
        tables[runtime] = tuple(releases)

    return RuntimeReleaseTable(
        pack_version=model.pack_version,
        python=tables["python"],
        node=tables["node"],
    )


# --------------------------------------------------------------------------------------
# Conversion to domain values
# --------------------------------------------------------------------------------------


def _evidence_id(entry: PackEntry, kind: str, index: int) -> EvidenceId:
    """Deterministic, collision-free evidence id.

    Determinism is required by 03's byte-stability test; uniqueness is required by 04's
    referential-integrity check on ``verdict_evidence_ids`` and ``notices[].evidence_ids``.
    ``(namespace, name, applies_to)`` is unique per pack, so adding the kind and the index
    within the entry makes the whole id unique.
    """
    return f"curated:{entry.namespace}:{entry.name}:{entry.applies_to}:{kind}:{index}"


def _substantiates(entry: PackEntry, statement: PackStatement) -> str:
    """A templated one-line summary. Never free prose - see 03's `summary` invariant."""
    counterpart = f"{statement.counterpart.namespace}:{statement.counterpart.name}"
    verb = "supports" if statement.stance == "supports" else "excludes"
    return (
        f"{entry.namespace}:{entry.name} {entry.applies_to} {verb} "
        f"{counterpart} {statement.expression}."
    )


def statement_claims(
    entry: PackEntry, pack_version: str
) -> tuple[tuple[Claim, ...], tuple[Evidence, ...]]:
    """Turn an entry's statements into tier-B claims and their evidence.

    Returned as a pair because the two are generated together and must stay consistent:
    every claim's ``evidence_id`` has to resolve inside the same response, and building
    them apart would let the two drift.
    """
    claims: list[Claim] = []
    evidence: list[Evidence] = []
    provenance = Curated(reviewed_at=entry.reviewed_at, pack_version=pack_version)

    for index, statement in enumerate(entry.statements):
        identifier = _evidence_id(entry, "statement", index)
        claims.append(
            CompatibilityStatement(
                declared_about=statement.counterpart,
                stance=statement.stance,
                expression=statement.expression,
                scheme=statement.scheme,
                evidence_id=identifier,
            )
        )
        evidence.append(
            VersionConstraintEvidence(
                id=identifier,
                # Curated support statements are tier B: explicit publisher statements,
                # never installer-enforced gates (03's evidence tier table).
                tier="B",
                source_type=statement.source_type,
                title=statement.title,
                url=statement.url,
                substantiates=_substantiates(entry, statement),
                expression=statement.expression,
                scheme=statement.scheme,
                provenance=provenance,
            )
        )
    return tuple(claims), tuple(evidence)


def change_evidence(entry: PackEntry, pack_version: str) -> tuple[Evidence, ...]:
    """Turn an entry's changes into narrative evidence.

    Changes carry no version range, so they are :class:`NarrativeEvidence` and produce no
    claim: a breaking-change note describes what changed, not which versions are
    compatible, and must not be able to move a verdict on its own.
    """
    provenance = Curated(reviewed_at=entry.reviewed_at, pack_version=pack_version)
    return tuple(
        NarrativeEvidence(
            id=_evidence_id(entry, "change", index),
            tier="B",
            source_type=change.source_type,
            title=change.title,
            url=change.url,
            substantiates=change.summary,
            provenance=provenance,
        )
        for index, change in enumerate(entry.changes)
    )
