"""The installed distribution metadata is the only runtime version source.

Release automation changes the version in ``pyproject.toml`` and rebuilds the installed
metadata. Keeping the package and MCP server versions derived from that metadata makes a
partially updated release unrepresentable instead of relying on release-time discipline.
"""

from importlib.metadata import version

from dependency_compat_mcp import __version__
from dependency_compat_mcp.server import SERVER_VERSION


def test_runtime_versions_match_installed_distribution_metadata() -> None:
    distribution_version = version("dependency-compat-mcp")

    assert __version__ == distribution_version
    assert distribution_version == SERVER_VERSION
