"""Default discovery wrapper for the independent hand-written cases."""

try:
    from .boundary_cases import HandWrittenBoundaryCases
except ImportError:  # unittest discovery with tests/ as the start directory
    from boundary_cases import HandWrittenBoundaryCases


__all__ = ["HandWrittenBoundaryCases"]
