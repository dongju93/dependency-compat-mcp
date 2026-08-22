"""Tool description text, treated as part of the interface (02 "도구 설명 텍스트 계약").

The input schema decides whether a call is *valid*; the description decides whether the
caller makes it at all. Because the primary caller here is a language model, these strings
are not decoration - they are the only place the following four things are stated:

1. the single question the tool answers,
2. when to call it and when not to,
3. what argument order means, and where reverse reading is allowed,
4. that ``unknown`` is a normal result rather than a failure.

They live as module-level constants for one reason: a contract test has to compare them
against what ``tools/list`` actually publishes, and text duplicated at the registration
site would drift from the text reviewed here.

02 also fixes what must stay *out*: a supported-package list or coverage number (it goes
stale on every pack release; ``depth`` and ``limitations`` report coverage per response),
example JSON (the schema already carries it, and length buries the part used to decide
whether to call), and performance claims (they do not inform that decision).

:data:`REQUIRED_ELEMENTS` exists so that "all four elements are present, in order" is a
machine-checked property instead of a review convention.
"""

from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "CHECK_COMPATIBILITY_DESCRIPTION",
    "GET_COMPATIBILITY_CONTEXT_DESCRIPTION",
    "REQUIRED_ELEMENTS",
    "SERVER_INSTRUCTIONS",
    "DescriptionElement",
]


SERVER_INSTRUCTIONS: Final[str] = (
    "Answers compatibility questions about exact, already-pinned releases of PyPI "
    "packages, npm packages, and the Python and Node runtimes. Every answer is derived "
    "from registry metadata and human-reviewed source packs, so each one carries the "
    "sources it rests on. Use check_compatibility for a two-sided question about a "
    "named pair, and get_compatibility_context to pull one release's declared "
    "constraints and reviewed changes so you can compare them with code you already "
    "hold. The server never receives a repository, a file, or a search term, and it "
    "resolves neither ranges nor dependency graphs; a resolver is the right tool for "
    "those. An unknown result states that current evidence does not settle the "
    "question; decision_causes names what left it open, and sources_checked lists every "
    "lookup with the exact release it was made for."
)


CHECK_COMPATIBILITY_DESCRIPTION: Final[str] = (
    "Does one exact release work with another exact release, and on what evidence? "
    "Call this once both sides are pinned: a PyPI or npm release, or a Python or Node "
    "runtime. Do not call it with a version range or file contents; range "
    "resolution and codebase reading are out of scope. Argument order is meaning. Under "
    "the pypi -> pypi requires_dist and npm -> npm dependencies rules, order fixes the "
    "declaring side, so swapping arguments asks another question. For pypi with "
    "runtime:python and npm with runtime:node the kind of target fixes the declaring "
    "side, so either order reads the same; relation.direction reports which way it was "
    "read. A verdict of unknown is a normal result: the claim is not provable from "
    "current evidence. It is not an error and not a reason to retry: decision_causes "
    "names why it stayed open, and limitations and sources_checked carry the next "
    "thing to check."
)


GET_COMPATIBILITY_CONTEXT_DESCRIPTION: Final[str] = (
    "What does one exact release declare and state about compatibility? Call this when a "
    "single target is pinned and you want its declared constraints and reviewed changes. "
    "Do not call it with a version range, a repository path, or source text; the server "
    "never sees a codebase and compares nothing for you. This tool takes one target, so "
    "there is no argument order to get wrong; order matters only in check_compatibility, "
    "where it fixes the declaring side for the pypi -> pypi and npm -> npm rules, the "
    "kind of target fixes it for the runtime rules, and relation.direction reports how "
    "the pair was read. Each constraint carries a condition field: unconditional, an "
    "environment marker, or a selected extra. An availability of unknown is a normal "
    "result: current evidence proves nothing here. Not an error and not a reason to "
    "retry: limitations and sources_checked carry the next thing to check."
)


@dataclass(frozen=True, slots=True)
class DescriptionElement:
    """One mandated element of a tool description, plus how to detect it.

    ``probes`` are lowercase substrings, because a contract test can only check presence
    and relative position, not intent. They are deliberately phrase-level: single words
    such as "order" would also match unrelated prose and make the check meaningless.
    """

    name: Literal["question", "when_to_call", "argument_order", "unknown_semantics"]
    probes: tuple[str, ...]


REQUIRED_ELEMENTS: Final[tuple[DescriptionElement, ...]] = (
    DescriptionElement(name="question", probes=("?", "exact release")),
    DescriptionElement(
        name="when_to_call",
        probes=("call this", "do not call it", "version range", "codebase"),
    ),
    DescriptionElement(
        name="argument_order",
        probes=("order", "declaring side", "relation.direction", "kind of target"),
    ),
    DescriptionElement(
        name="unknown_semantics",
        probes=(
            "unknown is a normal result",
            "not an error",
            "retry",
            "limitations",
            "sources_checked",
        ),
    ),
)
