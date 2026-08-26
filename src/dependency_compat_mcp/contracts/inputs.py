"""The public tool input models (02).

The JSON Schema in 02 is the contract; these models exist to express it. Where the SDK's
convenience and the document disagree, the document wins - hence the explicit
``pattern``/``minLength``/``maxLength`` and ``extra="forbid"`` rather than whatever a
looser Python signature would have produced.

Note that ``extra="forbid"`` here covers the *nested* object only. The top-level argument
object is guarded by :mod:`dependency_compat_mcp.server`'s boundary middleware, because
the SDK builds the top-level arguments model itself.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from dependency_compat_mcp.domain.targets import (
    MAX_NAME_LENGTH,
    MAX_NAMESPACE_LENGTH,
    MAX_VERSION_LENGTH,
    NAMESPACE_PATTERN,
    Target,
    parse_target,
)

__all__ = ["TargetInput"]


class TargetInput(BaseModel):
    """One comparison target: ``namespace + name + exact version``."""

    model_config = ConfigDict(extra="forbid")

    namespace: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_NAMESPACE_LENGTH,
            pattern=NAMESPACE_PATTERN,
            description=(
                "Name space that decides how name and version are interpreted. "
                "Registered values: pypi, npm, runtime."
            ),
        ),
    ]
    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_NAME_LENGTH,
            description=(
                "Package or runtime name, spelled with the grammar of its namespace. "
                "Under runtime only python and node are registered."
            ),
        ),
    ]
    version: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_VERSION_LENGTH,
            description=(
                "One exact release. Ranges, wildcards and unions such as '>=3.10,<3.14', "
                "'^19' or '1.x' are rejected. Runtime targets require a complete release "
                "including the patch component, for example python 3.13.7 or node 22.11.0. "
                "When a runtime is known only as a release line such as 3.13 or 22, that "
                "is a question about a line rather than a release: call "
                "get_compatibility_context on the package instead, which returns the "
                "declared constraint verbatim for the line to be compared against."
            ),
        ),
    ]

    def parse(self) -> Target:
        """Promote this DTO to the domain sum type, or raise ``InputError``."""
        return parse_target(self.namespace, self.name, self.version)
