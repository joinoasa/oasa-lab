"""Independent checker for evidence bundles. Standard library only; see docs/checker.md."""

from .core import CHECKER_VERSION, check_bundle, format_result, write_check

__all__ = ["CHECKER_VERSION", "check_bundle", "format_result", "write_check"]
