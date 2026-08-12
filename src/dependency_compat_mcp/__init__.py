"""dependency-compat-mcp: answer whether two exact versions are usable together.

The package root stays free of imports on purpose. Loading it must not pull in the MCP
SDK, the HTTP client or the curated pack, because every submodule import would then pay
for the whole server - and, more importantly, an import cycle in the wiring would surface
as a mysterious failure in an unrelated module. The console script's work lives in
:mod:`dependency_compat_mcp.cli` and is imported only when it is actually run.
"""

__all__ = ["main"]

__version__ = "0.1.0"


def main() -> None:
    """Console-script entry point (``dependency-compat-mcp``)."""
    from dependency_compat_mcp.cli import main as _main

    _main()
