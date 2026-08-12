"""Parsed targets: the only way an identifier reaches the decision procedure.

02 requires that ``namespace + name + version`` be fully parsed before any evaluation
runs. This module owns that boundary. :class:`CanonicalName` and :class:`ExactVersion`
have no public constructor path other than the namespace parsers below, so "an
unvalidated string flowed into the verdict" has no representation.

Two rules drive every parser here:

* **No silent correction.** A parser that canonicalises must round-trip: serialising the
  parse result has to reproduce the caller's input byte for byte. ``v22.17.0`` is an
  input error, not a quiet rewrite to ``22.17.0``.
* **No cross-ecosystem version syntax.** PEP 440 is parsed by ``packaging``; npm SemVer by
  ``node-semver``. The :class:`VersionScheme` tag rides along on the value so a later
  comparison cannot mix them by accident.
"""

import re
from dataclasses import dataclass, field
from typing import ClassVar, Final, Literal, Self, assert_never

import nodesemver
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from dependency_compat_mcp.domain.errors import InputError

__all__ = [
    "MAX_NAMESPACE_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_VERSION_LENGTH",
    "NAMESPACE_PATTERN",
    "REGISTERED_NAMESPACES",
    "REGISTERED_RUNTIME_NAMES",
    "CanonicalName",
    "ExactVersion",
    "Namespace",
    "NodeRuntimeTarget",
    "NpmTarget",
    "Pep440Version",
    "PyPITarget",
    "PythonRuntimeTarget",
    "RegistryClass",
    "RuntimeClass",
    "SemverVersion",
    "Target",
    "TargetClass",
    "TargetId",
    "VersionScheme",
    "classify",
    "name_of",
    "namespace_of",
    "parse_namespace",
    "parse_npm_name",
    "parse_pep440_version",
    "parse_pypi_name",
    "parse_semver_version",
    "parse_target",
    "scheme_of",
    "target_id",
    "version_of",
]

# --------------------------------------------------------------------------------------
# Scalar contract limits (02). Kept here so the JSON Schema and the parser cannot drift.
# --------------------------------------------------------------------------------------

MAX_NAMESPACE_LENGTH: Final = 32
MAX_NAME_LENGTH: Final = 200
MAX_VERSION_LENGTH: Final = 100
NAMESPACE_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,31}$"

type Namespace = Literal["pypi", "npm", "runtime"]
type RuntimeName = Literal["python", "node"]
type VersionScheme = Literal["pep440", "semver"]

REGISTERED_NAMESPACES: Final[frozenset[str]] = frozenset({"pypi", "npm", "runtime"})
REGISTERED_RUNTIME_NAMES: Final[frozenset[str]] = frozenset({"python", "node"})

_NAMESPACE_RE: Final = re.compile(NAMESPACE_PATTERN)
# PEP 508 name grammar, applied case-insensitively before canonicalisation.
_PYPI_NAME_RE: Final = re.compile(
    r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$"
)
# npm package name grammar: optional @scope/ prefix, url-safe, no uppercase.
_NPM_NAME_RE: Final = re.compile(
    r"^(?:@[a-z0-9\-~][a-z0-9\-._~]*/)?[a-z0-9\-~][a-z0-9\-._~]*$"
)
# npm's own limit is 214, but 02's shared 200-character cap is checked first and always
# binds, so it is not restated here: a second, unreachable bound would read like an
# enforced npm rule while never running.
_CONTROL_CHARS_RE: Final = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------------------------------
# Validated scalars
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CanonicalName:
    """A name already interpreted by its namespace's grammar.

    Only :func:`parse_target` (and the pack loader, which reuses the same parsers) can
    produce one, so no function downstream needs to re-validate a name.
    """

    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Pep440Version:
    """An exact PEP 440 release, with the original spelling preserved.

    ``raw`` is the identity. It is already canonical - the parser rejects any spelling that
    would round-trip differently - so two values are the same release exactly when their
    ``raw`` matches, and ``parsed`` is excluded from equality as a derived view.
    """

    raw: str
    parsed: Version = field(compare=False)

    scheme: ClassVar[VersionScheme] = "pep440"

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True, slots=True)
class SemverVersion:
    """An exact npm SemVer release, with the original spelling preserved.

    ``parsed`` is excluded from equality and hashing, and here that is load-bearing rather
    than tidy: ``nodesemver.SemVer`` inherits ``object.__eq__``, so comparing it would make
    two independent parses of the same string unequal. Every derived comparison - cache
    keys, set membership, "is this the same release" - would then be silently wrong, and
    only on the npm side, because ``packaging.Version`` does implement value equality.
    ``raw`` is canonical and therefore a sound identity on its own.
    """

    raw: str
    parsed: nodesemver.SemVer = field(compare=False)

    scheme: ClassVar[VersionScheme] = "semver"

    def __str__(self) -> str:
        return self.raw


type ExactVersion = Pep440Version | SemverVersion


def scheme_of(version: ExactVersion) -> VersionScheme:
    """Return the syntax a comparison must use for ``version``."""
    match version:
        case Pep440Version():
            return "pep440"
        case SemverVersion():
            return "semver"
        case _:
            assert_never(version)


# --------------------------------------------------------------------------------------
# Target sum type
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PyPITarget:
    name: CanonicalName
    version: Pep440Version


@dataclass(frozen=True, slots=True)
class NpmTarget:
    name: CanonicalName
    version: SemverVersion


@dataclass(frozen=True, slots=True)
class PythonRuntimeTarget:
    version: Pep440Version

    @property
    def name(self) -> CanonicalName:
        return CanonicalName("python")


@dataclass(frozen=True, slots=True)
class NodeRuntimeTarget:
    version: SemverVersion

    @property
    def name(self) -> CanonicalName:
        return CanonicalName("node")


type Target = PyPITarget | NpmTarget | PythonRuntimeTarget | NodeRuntimeTarget


def namespace_of(target: Target) -> Namespace:
    match target:
        case PyPITarget():
            return "pypi"
        case NpmTarget():
            return "npm"
        case PythonRuntimeTarget() | NodeRuntimeTarget():
            return "runtime"
        case _:
            assert_never(target)


def version_of(target: Target) -> ExactVersion:
    match target:
        case PyPITarget(version=version) | PythonRuntimeTarget(version=version):
            return version
        case NpmTarget(version=version) | NodeRuntimeTarget(version=version):
            return version
        case _:
            assert_never(target)


def name_of(target: Target) -> CanonicalName:
    match target:
        case PyPITarget(name=name) | NpmTarget(name=name):
            return name
        case PythonRuntimeTarget() | NodeRuntimeTarget():
            return target.name
        case _:
            assert_never(target)


@dataclass(frozen=True, slots=True)
class TargetId:
    """A versionless identity: what a claim is *about*.

    ``requires_dist`` names a package, not a release, so a claim's subject cannot carry a
    version without inventing one.
    """

    namespace: Namespace
    name: CanonicalName

    @classmethod
    def of(cls, target: Target) -> Self:
        return cls(namespace=namespace_of(target), name=name_of(target))


def target_id(target: Target) -> TargetId:
    return TargetId.of(target)


# --------------------------------------------------------------------------------------
# Rule keys (03 [2]): runtimes are keyed by name, registries are not
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryClass:
    """A registry namespace. Package names are unbounded, so the name is not part of the key."""

    namespace: Literal["pypi", "npm"]


@dataclass(frozen=True, slots=True)
class RuntimeClass:
    """A runtime. The server knows a finite set, so the name *is* part of the key."""

    name: RuntimeName


type TargetClass = RegistryClass | RuntimeClass


def classify(target: Target) -> TargetClass:
    match target:
        case PyPITarget():
            return RegistryClass("pypi")
        case NpmTarget():
            return RegistryClass("npm")
        case PythonRuntimeTarget():
            return RuntimeClass("python")
        case NodeRuntimeTarget():
            return RuntimeClass("node")
        case _:
            assert_never(target)


# --------------------------------------------------------------------------------------
# Boundary parsers
# --------------------------------------------------------------------------------------


def _reject_untrimmed(field: str, raw: str, *, maximum: int) -> None:
    if not raw:
        raise InputError("empty_value", f"{field} must not be empty.", field=field)
    if len(raw) > maximum:
        raise InputError(
            "value_too_long",
            f"{field} must be at most {maximum} characters.",
            field=field,
        )
    if raw != raw.strip():
        raise InputError(
            "surrounding_whitespace",
            f"{field} must not have leading or trailing whitespace; "
            "the server does not trim input.",
            field=field,
        )
    if _CONTROL_CHARS_RE.search(raw):
        raise InputError(
            "control_character",
            f"{field} must not contain control characters.",
            field=field,
        )


def parse_namespace(raw: str) -> Namespace:
    _reject_untrimmed("namespace", raw, maximum=MAX_NAMESPACE_LENGTH)
    if not _NAMESPACE_RE.match(raw):
        raise InputError(
            "namespace_syntax",
            f"namespace {raw!r} does not match {NAMESPACE_PATTERN}.",
            field="namespace",
        )
    if raw not in REGISTERED_NAMESPACES:
        registered = ", ".join(sorted(REGISTERED_NAMESPACES))
        raise InputError(
            "namespace_not_registered",
            f"namespace {raw!r} is not registered. Registered namespaces: {registered}.",
            field="namespace",
        )
    # Narrowed by the membership test above.
    return raw  # pyrefly: ignore[bad-return]


def parse_pypi_name(raw: str) -> CanonicalName:
    _reject_untrimmed("name", raw, maximum=MAX_NAME_LENGTH)
    if not _PYPI_NAME_RE.match(raw):
        raise InputError(
            "name_syntax", f"{raw!r} is not a valid PyPI project name.", field="name"
        )
    return CanonicalName(str(canonicalize_name(raw)))


def parse_npm_name(raw: str) -> CanonicalName:
    _reject_untrimmed("name", raw, maximum=MAX_NAME_LENGTH)
    if not _NPM_NAME_RE.match(raw):
        raise InputError(
            "name_syntax", f"{raw!r} is not a valid npm package name.", field="name"
        )
    # npm names are already their own canonical form; nothing to fold.
    return CanonicalName(raw)


def parse_runtime_name(raw: str) -> RuntimeName:
    _reject_untrimmed("name", raw, maximum=MAX_NAME_LENGTH)
    if raw not in REGISTERED_RUNTIME_NAMES:
        registered = ", ".join(sorted(REGISTERED_RUNTIME_NAMES))
        raise InputError(
            "runtime_not_registered",
            f"runtime {raw!r} is not registered. Registered runtimes: {registered}.",
            field="name",
        )
    return raw  # pyrefly: ignore[bad-return]


def parse_pep440_version(raw: str) -> Pep440Version:
    """Parse an exact PEP 440 release, rejecting anything the parser would rewrite."""
    _reject_untrimmed("version", raw, maximum=MAX_VERSION_LENGTH)
    try:
        parsed = Version(raw)
    except InvalidVersion as exc:
        raise InputError(
            "version_syntax",
            f"{raw!r} is not an exact PEP 440 version: {exc}",
            field="version",
        ) from exc
    if str(parsed) != raw:
        raise InputError(
            "version_not_canonical",
            f"{raw!r} is not written in canonical PEP 440 form ({str(parsed)!r}); "
            "the server does not normalise versions on the caller's behalf.",
            field="version",
        )
    return Pep440Version(raw=raw, parsed=parsed)


def parse_semver_version(raw: str) -> SemverVersion:
    """Parse an exact npm SemVer release, rejecting ranges and non-canonical spellings."""
    _reject_untrimmed("version", raw, maximum=MAX_VERSION_LENGTH)
    try:
        canonical = nodesemver.valid(raw, loose=False)
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise InputError(
            "version_syntax",
            f"{raw!r} is not an exact npm SemVer version.",
            field="version",
        ) from exc
    if canonical is None:
        raise InputError(
            "version_syntax",
            f"{raw!r} is not an exact npm SemVer version. Ranges, wildcards and unions "
            "are not accepted.",
            field="version",
        )
    # `nodesemver.valid` returns a SemVer object, not a string; comparing it to `raw`
    # directly would reject every well-formed version. Serialise it first - which is also
    # exactly the round-trip the no-silent-correction rule asks for.
    canonical_text = str(canonical)
    if canonical_text != raw:
        raise InputError(
            "version_not_canonical",
            f"{raw!r} is not written in canonical SemVer form ({canonical_text!r}); "
            "the server does not strip prefixes such as 'v'.",
            field="version",
        )
    return SemverVersion(raw=raw, parsed=nodesemver.make_semver(raw, loose=False))


def parse_target(namespace: str, name: str, version: str) -> Target:
    """Parse one ``TargetInput`` into the domain sum type.

    The three fields are checked in the 02 order (namespace grammar and registration,
    then the namespace's name parser, then its exact-version parser) so the reported
    error names the first field that actually failed.
    """
    parsed_namespace = parse_namespace(namespace)
    match parsed_namespace:
        case "pypi":
            return PyPITarget(
                name=parse_pypi_name(name), version=parse_pep440_version(version)
            )
        case "npm":
            return NpmTarget(
                name=parse_npm_name(name), version=parse_semver_version(version)
            )
        case "runtime":
            match parse_runtime_name(name):
                case "python":
                    return PythonRuntimeTarget(version=parse_pep440_version(version))
                case "node":
                    return NodeRuntimeTarget(version=parse_semver_version(version))
                case never:
                    assert_never(never)
        case _:
            assert_never(parsed_namespace)
