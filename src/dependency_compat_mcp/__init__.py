"""dependency-compat-mcp: answer whether two exact versions are usable together.

The package root reads only its installed distribution metadata. Loading it must not pull
in the MCP SDK or the HTTP client, because every submodule import would
then pay for the whole server - and, more importantly, an import cycle in the wiring would
surface as a mysterious failure in an unrelated module. The console script's work lives
in :mod:`dependency_compat_mcp.cli` and is imported only when it is actually run.
"""

from importlib.metadata import version

__all__ = ["main"]

__version__ = version("dependency-compat-mcp")


def main() -> None:
    """Console-script entry point (``dependency-compat-mcp``)."""
    from dependency_compat_mcp.cli import main as _main

    _main()
