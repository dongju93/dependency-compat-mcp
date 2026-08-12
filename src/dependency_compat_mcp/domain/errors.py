"""Domain-level failure types.

Two failure kinds exist and they must never be confused (04 "도구 오류와 정상 결과의 경계"):

* :class:`InputError` - the caller violated the 02 input contract. Always a tool error.
* :class:`InvariantViolation` - the server built a state its own constructors should have
  made unrepresentable. Always a tool error, never a normal ``unknown`` verdict.

Insufficient external evidence is neither: it is a normal ``unknown`` domain result and is
modelled in :mod:`dependency_compat_mcp.domain.evaluate`.
"""

__all__ = ["InputError", "InvariantViolation"]


class InputError(ValueError):
    """A tool argument violated the 02 input contract.

    Raised by the boundary parsers only. Carries a stable ``code`` so the MCP layer can
    report a machine-readable reason instead of re-deriving it from prose.
    """

    __slots__ = ("code", "field")

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


class InvariantViolation(RuntimeError):
    """A server invariant broke.

    Raised from always-executed code (never an ``assert``, which ``-O`` strips) so the
    check survives optimized runs, and converted to an MCP tool error at the boundary.
    """

    __slots__ = ()
