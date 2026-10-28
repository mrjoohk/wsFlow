"""Compatibility alias for the workspace-operations package implementation."""
import sys as _sys
from .modules.workspace_operations import session_prep as _implementation

_sys.modules[__name__] = _implementation
