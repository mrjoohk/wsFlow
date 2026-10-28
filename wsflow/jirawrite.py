"""Compatibility import for the work-calendar package; implementation lives there."""
import sys as _sys
from .modules.work_calendar import jirawrite as _implementation
_sys.modules[__name__] = _implementation
