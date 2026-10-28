"""Load unchanged CLI vendor files inside this package generation's namespace.

The upstream scripts use flat sibling imports and adjust sys.path for CLI use.
An in-process package must resolve those siblings locally without changing the
process import path or sharing another installed generation's module cache.
"""
from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import sys
import threading
import types

_ROOT = Path(__file__).resolve().parent / 'vendor' / 'jira_worklog'
_NAMES = {path.stem for path in _ROOT.glob('*.py')}
_LOCK = threading.RLock()


def load(name):
    if name not in _NAMES:
        raise ImportError('Unknown owned vendor module: ' + name)
    qualified = __package__ + '._vendor_' + name
    with _LOCK:
        if qualified in sys.modules:
            return sys.modules[qualified]
        path = _ROOT / (name + '.py')
        spec = importlib.util.spec_from_file_location(qualified, path)
        module = importlib.util.module_from_spec(spec)
        # Only the unchanged vendor script sees this private path list. All
        # other sys attributes retain the interpreter's ordinary values.
        local_sys = types.ModuleType('sys')
        local_sys.__dict__.update(vars(sys))
        local_sys.path = list(sys.path)

        def owned_import(target, globals=None, locals=None, fromlist=(), level=0):
            if level == 0 and target in _NAMES:
                return load(target)
            if level == 0 and target == 'sys':
                return local_sys
            return builtins.__import__(target, globals, locals, fromlist, level)

        module.__builtins__ = dict(vars(builtins), __import__=owned_import)
        sys.modules[qualified] = module
        try:
            exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
        except BaseException:
            sys.modules.pop(qualified, None)
            raise
        return module
