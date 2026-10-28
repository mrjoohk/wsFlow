"""Compatibility alias for the workspace-operations package implementation."""
import sys as _sys
from .modules.workspace_operations import context_rules as _implementation

_sys.modules[__name__] = _implementation
