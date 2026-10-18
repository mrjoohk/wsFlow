"""Mutation / evidence runner (ci-evidence-automation) -- spec-driven, parallel, chunkable.

A mutant is one or more text edits (file, old -> new). For every mutant the runner copies the code
(--copy paths) to a fresh temporary directory, applies the edits there, runs the test command and records
whether the tests caught it (GLOBAL Rule 12 negative controls). The working tree is never modified.

    python mutation_runner.py SPEC --root REPO --jobs 2 --out logs/<P>_evidence/mutation.json
    python mutation_runner.py SPEC --root REPO --only R02,F0* --jobs 2          # pick by ID (glob)
    python mutation_runner.py SPEC --root REPO --changed SNAPSHOT_DIR_OR_GIT_REF   # changed functions only
    python mutation_runner.py SPEC --root REPO --out RES.json --budget 100 --timeout 60   # host time limit: repeat while exit = 3
    python mutation_runner.py SPEC --root REPO --check                          # patterns still apply? (no tests)
    python mutation_runner.py SPEC --root REPO --out RES.json --summary --strict # final gate from the merged file

SPEC is JSON ({"copy", "test", "timeout", "env", "mutants"}) or a legacy .py file whose literal top-level statements
define MUTANTS (or M) = [(name, [(file, old, new), ...]), ...]; the .py file is read up to its first runtime statement,
never executed.
Exit codes: 0 gate pass · 1 gate fail · 2 setup/usage error (bad spec, baseline tests fail, interrupted) ·
3 incomplete (--budget). Generalizes drmHWP P-041 mutate_writer6.py + static_checks.py mut. Spec format and
workflow: references/mutation_runner.md.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import contextlib
import datetime
import errno
import fnmatch
import hashlib
import json
import operator
import os
import platform
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = 2
STATUSES = ("killed", "timeout", "survived", "pattern", "invalid", "error")
ALWAYS_BAD = ("survived", "pattern", "invalid", "error")      # fail the gate in every mode
STRICT_BAD = ("timeout", "stale", "not run", "unverified")     # also fail with --strict
IGNORED_PARTS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache"}
# "collected 0 items" only at the end of the line: "collected 0 items / 1 error" is a collection error (a kill)
NO_TESTS = re.compile(r"(?m)\bRan 0 tests?\b|\bno tests ran\b|NO TESTS RAN|collected 0 items\s*$")
UT_RAN = re.compile(r"(?m)^Ran (\d+) tests? in ")
UT_LINE = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]*)\)")
PY_NAMES = {"python", "python3", "python.exe", "python3.exe"}   # not "py": the launcher takes its own arguments
# --timeout/--budget upper bound: larger is a typo; cores x it (the CPU limit) must fit rlim_t, and Windows Popen.wait
# takes a DWORD of milliseconds (about 4.29e6 s)
MAX_SECS = 1e6
# legacy .py specs (_py_spec): pure str methods; methods run on its lists/dicts/sets; names reaching module globals
STR_METHODS = {"replace", "strip", "lstrip", "rstrip", "join", "upper", "lower", "format", "split"}
DATA_METHODS = {list: {"append", "extend", "insert", "pop", "remove", "clear", "reverse", "sort"},
                dict: {"pop", "popitem", "clear", "update", "setdefault"},
                set: {"add", "discard", "remove", "pop", "clear", "update"}}
REFLECT = {"globals", "vars", "locals", "exec", "eval", "compile", "setattr", "delattr", "getattr", "__import__",
           "__builtins__", "__dict__", "modules", "__globals__", "f_globals", "f_locals", "_getframe", "currentframe",
           "stack", "getmembers", "get_objects", "get_referrers", "import_module", "__main__"}
BINOPS = {getattr(ast, k): (getattr(operator, f), getattr(operator, "i" + f)) for k, f in (     # (a op b, a op= b)
    ("Add", "add"), ("Sub", "sub"), ("Mult", "mul"), ("Div", "truediv"), ("FloorDiv", "floordiv"), ("Mod", "mod"),
    ("Pow", "pow"))}
MUTATING = set().union(*DATA_METHODS.values())
SPEC_NAMES = {"MUTANTS", "M", "COPY", "TEST", "TIMEOUT", "ENV"}         # what a legacy spec defines
# POSIX: the test command runs under a small supervisor in the same process group. The supervisor gives the test a
# CPU-time limit (inherited by its children; soft below any outside hard limit, so SIGXCPU comes before the kernel's
# SIGKILL) and kills the whole group as soon as the runner (whose pid it is given) is no longer its parent, or a wall-clock backstop
# passes -- so a hung mutant, busy or blocked, cannot outlive a runner that was killed outright (SIGKILL, a host
# ending the call). The test's exit status (or signal) is passed on unchanged.
SUPERVISOR = "\n".join([
    "import os, sys, time, signal, resource",
    "cpu, wall, parent = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])",
    "argv = sys.argv[4:]",
    "pid = os.fork()",
    "if pid == 0:",
    "    try:",
    "        hard = resource.getrlimit(resource.RLIMIT_CPU)[1]",
    "        if hard == resource.RLIM_INFINITY:",
    "            soft, hard = cpu, cpu + 5",
    "        else:",
    "            soft = max(1, min(cpu, hard - 1))",
    "        resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))",
    "        resource.setrlimit(resource.RLIMIT_CORE, (0, resource.getrlimit(resource.RLIMIT_CORE)[1]))",
    "        os.execvp(argv[0], argv)",
    "    except BaseException as e:",
    "        sys.stderr.write('mutation_runner supervisor: %s\\n' % e)",
    "        os._exit(127)",
    "deadline = time.time() + wall",
    "while True:",
    "    done, status = os.waitpid(pid, os.WNOHANG)",
    "    if done:",
    "        break",
    "    if os.getppid() != parent or time.time() > deadline:",
    "        os.killpg(0, signal.SIGKILL)",
    "    time.sleep(0.05)",
    "if os.WIFSIGNALED(status):",
    "    sig = os.WTERMSIG(status)",
    "    try:",
    "        resource.setrlimit(resource.RLIMIT_CORE, (0, resource.getrlimit(resource.RLIMIT_CORE)[1]))",
    "        signal.signal(sig, signal.SIG_DFL)",
    "    except (OSError, ValueError):",
    "        pass",
    "    os.kill(os.getpid(), sig)",
    "    time.sleep(1)",
    "os._exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1)",
])

_CHILDREN = {}                      # pid -> Popen of running test commands (killed on SIGTERM/SIGINT/SIGHUP)
_CHILD_LOCK = threading.RLock()
_STOP = threading.Event()


def _now():
    """Local time with its UTC offset -- results from hosts in different zones stay comparable."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class SpecError(Exception):
    """The spec or the setup cannot be used (abort with exit 2)."""


class PatternError(Exception):
    """A mutant's edit does not apply to the current code (status 'pattern' -- fix the spec)."""


class _Unsupported(Exception):
    pass


class _Unknown:                     # value of a name the legacy reader does not know (import, def, unreadable value)
    def __init__(self, why, func=False):
        self.why, self.func = why, func                      # why names the line; func: a def of the file


# --------------------------------------------------------------------------- spec loading
def _too_big(op, a, b):
    """True for a huge result (a typo such as 10 ** 10 ** 9 must not hang the reader)."""
    if isinstance(a, int) and isinstance(b, int):
        return (a.bit_length() * b if isinstance(op, ast.Pow) else a.bit_length() + b.bit_length()) > 10 ** 7
    s, n = (a, b) if isinstance(b, int) else (b, a)
    return isinstance(op, ast.Mult) and isinstance(n, int) and isinstance(s, (str, bytes, list, tuple)) \
        and len(s) * n > 10 ** 7


def _key(sl, env):
    sl = sl.value if type(sl).__name__ == "Index" else sl                    # Python 3.8
    if isinstance(sl, ast.Slice):
        return slice(*[None if x is None else _lit(x, env) for x in (sl.lower, sl.upper, sl.step)])
    return _lit(sl, env)


def _lit(node, env):
    """Evaluate a literal expression on the real objects of `env` (A = M shares the list, as in Python): constants,
    names read so far, tuple/list/dict/set displays, indexing/slicing, + - * / // % ** and unary -/+, STR_METHODS,
    dict.get, len(). Pure: never runs code of the spec. Anything else, or an error (M[99]), raises _Unsupported."""
    try:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and not isinstance(env.get(node.id, _Unknown("not bound")), _Unknown):
            return env[node.id]
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):                             # a new object each time
            return {ast.Tuple: tuple, ast.List: list, ast.Set: set}[type(node)](_lit(e, env) for e in node.elts)
        if isinstance(node, ast.Dict) and None not in node.keys:
            return {_lit(k, env): _lit(v, env) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
            a, b = _lit(node.left, env), _lit(node.right, env)
            if not _too_big(node.op, a, b):
                return BINOPS[type(node.op)][0](a, b)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):     # numbers (else TypeError)
            return -_lit(node.operand, env) if isinstance(node.op, ast.USub) else +_lit(node.operand, env)
        if isinstance(node, ast.JoinedStr):                  # f"{len(M)} mutants"
            return "".join(_lit(v, env) for v in node.values)
        if isinstance(node, ast.FormattedValue):             # {v!r:>8}: conversion, then a format spec read the same way
            v = {115: str, 114: repr, 97: ascii}.get(node.conversion, lambda x: x)(_lit(node.value, env))
            return format(v, _lit(node.format_spec, env) if node.format_spec else "")
        if isinstance(node, ast.Subscript):                  # K[1], W[0:3], M[0]["edits"]
            return _lit(node.value, env)[_key(node.slice, env)]
        if isinstance(node, ast.Call) and all(k.arg for k in node.keywords):
            f, args, kw = node.func, [_lit(x, env) for x in node.args], {k.arg: _lit(k.value, env) for k in node.keywords}
            if isinstance(f, ast.Name) and f.id == "len" and "len" not in env and len(args) == 1 and not kw:
                return len(args[0])
            obj = _lit(f.value, env) if isinstance(f, ast.Attribute) else None
            if isinstance(obj, str) and f.attr in STR_METHODS or isinstance(obj, dict) and f.attr == "get":
                return getattr(obj, f.attr)(*args, **kw)
    except (_Unsupported, RecursionError):
        raise
    except Exception as e:                                   # 1 / 0, {[]: 1}, M[99] ...
        raise _Unsupported("%s at line %s (%s: %s)" % (type(node).__name__, getattr(node, "lineno", "?"),
                                                       type(e).__name__, str(e)[:80]))
    if isinstance(node, ast.Name):
        raise _Unsupported("name %s at line %s (%s)" % (node.id, node.lineno, env.get(node.id, _Unknown("not bound")).why))
    raise _Unsupported("%s at line %s" % (type(node).__name__, getattr(node, "lineno", "?")))


def _shares(v, seen=None):
    """True when v is or holds a list/dict/set (a shared sub-tuple is visited once)."""
    seen = set() if seen is None else seen
    if isinstance(v, tuple) and id(v) not in seen:
        seen.add(id(v))
        return any(_shares(x, seen) for x in v)
    return isinstance(v, (list, dict, set))


def _bad(node, env):
    """What in `node` could run code of the file or reach its globals (:=, lambda, yield, await, REFLECT), else None."""
    return next((":=" if isinstance(n, ast.NamedExpr) else getattr(n, "attr", getattr(n, "id", type(n).__name__.lower()))
                for n in ast.walk(node) if isinstance(n, (ast.NamedExpr, ast.Lambda, ast.Yield, ast.YieldFrom, ast.Await))
                or isinstance(n, ast.Attribute) and n.attr in REFLECT or isinstance(n, ast.Name) and n.id in REFLECT
                and isinstance(n.ctx, ast.Load) and n.id not in env), None)


def _root(t):
    """Name an item/attribute chain starts from (M[0].get("edits")[1] -> M); a call other than .get/.setdefault (which
    return the element itself) ends it: TEST.split(), ENV.copy() and sorted(M) are new objects."""
    while isinstance(t, (ast.Subscript, ast.Attribute)) or isinstance(t, ast.Call) \
            and getattr(t.func, "attr", None) in ("get", "setdefault"):
        t = t.func.value if isinstance(t, ast.Call) else t.value
    return t.id if isinstance(t, ast.Name) else None


def _py_stmt(node, env, bound, gone):
    """Carry out one top-level statement exactly as Python would; None when read, else the kind where reading stops."""
    line = node.lineno

    def bind(name, value):
        env[name], bound[name] = value, line
        gone.pop(name, None)

    def inert(x):                            # evaluating x cannot change or capture the spec's lists/dicts/sets
        return not _bad(x, env) and not any(isinstance(n, ast.Name) and _shares(env.get(n.id)) for n in ast.walk(x))

    def local_use(x):                        # a function of the file that x uses (calling it runs code)
        return next((n.id for n in ast.walk(x) if isinstance(n, ast.Name) and isinstance(env.get(n.id), _Unknown)
                     and env[n.id].func), None)

    def leaves(t):                           # names and items a target binds (unpacking flattened)
        return [y for e in t.elts for y in leaves(e)] if isinstance(t, (ast.Tuple, ast.List)) else [t]

    def target(t):                           # None when t can be assigned exactly, else the kind where reading stops
        if isinstance(t, (ast.Tuple, ast.List)):
            return next((k for k in map(target, t.elts) if k), None)
        if isinstance(t, ast.Starred):
            return "starred assignment"
        if isinstance(t, ast.Name):
            return None
        if _root(t) not in env or isinstance(env[_root(t)], _Unknown):
            return "item or attribute assignment"            # rooted in something unknown: os.environ["X"] = "1"
        if isinstance(t, ast.Attribute):
            raise SpecError("an attribute of the spec's data cannot be set")
        return None                                          # known data: assign() reads container and key in turn

    def assign(t, v):                        # left to right, as Python (i, M[i] = ... sees the new i)
        if isinstance(t, ast.Name):
            return bind(t.id, v)
        if isinstance(t, ast.Subscript):
            return _lit(t.value, env).__setitem__(_key(t.slice, env), v)
        items = list(v)
        if len(items) != len(t.elts):
            raise SpecError("cannot unpack %d values into %d targets" % (len(items), len(t.elts)))
        for e, x in zip(t.elts, items):
            assign(e, x)

    if isinstance(node, ast.Pass) or isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        return None                                          # docstring, bare constant
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            if a.name == "*" or REFLECT & set(a.name.split(".") + (getattr(node, "module", "") or "").split(".")):
                raise SpecError("'import %s' is not supported (unknown names, or module globals)" % a.name)
            bind(a.asname or a.name.split(".")[0], _Unknown("imported at line %d" % line))
        return None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = node.args
        anns = [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if x] + [node.returns]
        try:
            [_lit(d, env) for d in a.defaults + a.kw_defaults if d is not None]
        except _Unsupported:
            return "def with default values it cannot read"
        if node.decorator_list or not all(inert(x) and not local_use(x) for x in anns if x):
            return "decorated def" if node.decorator_list else "def with annotations that run code"
        return bind(node.name, _Unknown("function defined at line %d" % line, func=True))
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        raise SpecError("'%s' is not supported" % type(node).__name__.lower())
    if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete, ast.Expr)):
        return type(node).__name__.lower().replace("classdef", "class")    # if/for/while/with/try/class ...
    if local_use(node):
        return "use of %s()" % local_use(node)               # a function of the file: calling it runs code
    if _bad(node, env):
        raise SpecError("%s is not supported" % _bad(node, env))

    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if isinstance(node, ast.AnnAssign) and not inert(node.annotation):
            raise SpecError("the annotation cannot be read")
        kind = next((k for k in map(target, targets) if k), None)
        if kind:
            return kind
        try:
            v = node.value and _lit(node.value, env)         # X: int binds nothing
        except _Unsupported as e:
            leaves = [x for t in targets for x in leaves(t)]
            if any(isinstance(x, ast.Subscript) for x in leaves) or not inert(node.value):
                raise SpecError("unsupported %s -- the value goes into, or reads, the spec's lists/dicts" % e)
            for x in leaves:
                bind(x.id, _Unknown("unsupported %s (assignment at line %d)" % (e, line)))
            return None
        for t in targets * (node.value is not None):         # A = B = v: one object, as in Python
            assign(t, v)
        return None
    if isinstance(node, ast.AugAssign):
        t = node.target
        if target(t):
            return target(t)
        cur = env[t.id] if isinstance(t, ast.Name) and t.id in env else _lit(t, env)      # may be _Unknown
        try:
            v = _lit(node.value, env)
            if type(node.op) not in BINOPS or isinstance(cur, _Unknown) or _too_big(node.op, cur, v):
                raise _Unsupported("%s at line %d" % (type(node.op).__name__, line))
        except _Unsupported as e:
            if isinstance(t, ast.Subscript) or _shares(cur) or not inert(node.value):
                raise SpecError("unsupported %s -- it changes the spec's lists/dicts" % e)
            return bind(t.id, _Unknown("unsupported %s (augmented assignment at line %d)" % (e, line)))
        return assign(t, BINOPS[type(node.op)][1](cur, v))   # in place on a list: aliases see it, as in Python
    if isinstance(node, ast.Delete):
        ts = [x for t in node.targets for x in leaves(t)]
        kind = next((k for k in map(target, ts) if k), None)
        if kind:
            return kind                                      # del os.environ["X"]: as for an assignment
        for t in ts:
            if isinstance(t, ast.Name):                      # unbinds the name only: no data changes
                if t.id not in env:
                    raise SpecError("name %s is not defined" % t.id)
                del env[t.id]
                gone[t.id] = "deleted at line %d" % line
            else:
                del _lit(t.value, env)[_key(t.slice, env)]
        return None
    call = node.value                                        # expression statement
    if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "print" and "print" not in env \
            and all(k.arg in ("sep", "end", "file", "flush") for k in call.keywords):
        try:                                                 # what it prints must be readable; file=sys.stderr is inert
            [_lit(x, env) for x in call.args + [k.value for k in call.keywords if not inert(k.value)]]
            return None                                      # print(f"{len(M)} mutants", file=sys.stderr) only reads
        except _Unsupported:
            pass
    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and all(k.arg for k in call.keywords):
        try:
            obj = _lit(call.func.value, env)
        except _Unsupported:
            obj = None
        if call.func.attr in DATA_METHODS.get(type(obj), ()):                       # in place, exactly as Python
            return getattr(obj, call.func.attr)(*[_lit(x, env) for x in call.args],
                                                **{k.arg: _lit(k.value, env) for k in call.keywords}) and None
    try:
        _lit(call, env)                                      # pure: no effect
    except _Unsupported as e:
        if not inert(call):
            raise SpecError("unsupported %s -- cannot tell what it does to the spec's lists/dicts" % e)
    return None                                              # e.g. print("x"), sys.path.insert(0, "..")


def _tail(tree, start, env):
    """Runtime code after the stop (tree.body[start:]) and the bodies of the file's defs must not change the spec
    directly: the first (line, name, (def name, def line) or None) of a top-level assignment/op=/del of a SPEC_NAMES
    name, or anywhere an item/attribute store, op=, del, DATA_METHODS call or global through a root -- a SPEC_NAMES
    name or a for variable over one (nested loops count) -- and the SPEC_NAMES rebound inside a block (P-041's
    M = M[a:b] under an argv check: ignored, named in the note). Nothing else is analysed: copies and other names are
    not followed. op= on a plain name changes data for a spec name, and for a for variable whose values in the read
    data include a list/dict/set (for name, edits in M: edits += [...])."""
    roots, rebinds = SPEC_NAMES, []

    def values(it, vals):                    # what a for over `it` yields in the read data, or None (cannot tell)
        r = _root(it)
        try:
            return [e for v in vals[r] for e in _lit(it, dict(env, **{r: v}))] if r in vals else \
                list(_lit(it, env)) if r in SPEC_NAMES else None
        except (_Unsupported, TypeError):
            return None

    def unpack(t, vs, vals):                 # the loop target onto every element, as Python unpacks it
        if isinstance(t, ast.Name):
            vals[t.id] = vs
        for k, e in enumerate(t.elts if isinstance(t, (ast.Tuple, ast.List)) else []):
            ok = vs is not None and all(isinstance(v, (list, tuple)) and len(v) == len(t.elts) for v in vs)
            unpack(e, [v[k] for v in vs] if ok and not isinstance(e, ast.Starred) else None, vals)

    def hit(n, roots, vals):
        if isinstance(n, ast.AugAssign):                     # op= on a plain name: a spec name, or a mutable for value
            r = _root(n.target) if not isinstance(n.target, ast.Name) or n.target.id in SPEC_NAMES or any(
                isinstance(v, (list, dict, set)) for v in vals.get(n.target.id) or ()) else None
        elif isinstance(n, (ast.Subscript, ast.Attribute)) and isinstance(n.ctx, (ast.Store, ast.Del)):
            r = _root(n)
        elif isinstance(n, ast.Delete):
            r = next((t.id for t in n.targets if isinstance(t, ast.Name) and t.id in SPEC_NAMES), None)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATING:
            r = _root(n.func.value)
            if r not in roots and getattr(n.func.value, "id", None) in ("list", "dict", "set") and n.args:
                r = _root(n.args[0])                         # list.append(M, x), dict.update(ENV, ...)
        else:
            r = next((x for x in n.names if x in SPEC_NAMES), None) if isinstance(n, ast.Global) else None
        return r if r in roots else None

    def scan(n, roots, vals, block, fn):     # vals: for variable -> its values; block: below a top-level statement;
        for g in [n] if isinstance(n, (ast.For, ast.AsyncFor)) else getattr(n, "generators", []):  # fn: (name, line)
            if _root(g.iter) in roots:                                                            # of the def it is in
                roots = roots | {x.id for x in ast.walk(g.target) if isinstance(x, ast.Name)}    # loop variables
                vals = dict(vals)
                unpack(g.target, values(g.iter, vals), vals)
        if hit(n, roots, vals):
            return n.lineno, hit(n, roots, vals), fn
        if block and not fn and isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in SPEC_NAMES:
            rebinds.append("%s inside a block at line %d" % (n.id, n.lineno))
        fn = (n.name, n.lineno) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
        return next(filter(None, (scan(c, roots, vals, block or isinstance(n, ast.stmt), fn)
                                  for c in ast.iter_child_nodes(n))), None)

    for s in tree.body[start:]:
        top = [x.id for t in getattr(s, "targets", [getattr(s, "target", None)]) for x in ast.walk(t or ast.Pass())
               if isinstance(x, ast.Name) and not isinstance(x.ctx, ast.Load) and x.id in SPEC_NAMES]
        top += [x.target.id for x in ast.walk(s) if isinstance(s, (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign))
                and isinstance(x, ast.NamedExpr) and x.target.id in SPEC_NAMES]                # (M := [])
        r = (s.lineno, top[0], None) if top and isinstance(s, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete,
                                                               ast.Expr)) else scan(s, roots, {}, False, None)
        if r:
            return r, rebinds
    defs = [d for d in tree.body[:start] if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return next(filter(None, (scan(d, roots, {}, True, None) for d in defs)), None), rebinds


def _py_spec(path):
    """Read a legacy .py spec without executing it: its literal top-level statements are carried out exactly, on real
    objects; the first statement that runs code stops reading (a note) -- and the code after it must not change the
    spec's data (_tail); anything else is a SpecError naming the line. Imported code is assumed not to reach into the
    spec's globals (see references/mutation_runner.md)."""
    try:
        tree = ast.parse(path.read_bytes(), str(path))      # bytes: a BOM or a PEP 263 coding line, as Python reads it
    except RecursionError:
        raise SpecError("%s: nested too deeply to read (cannot be parsed)" % path)
    except (SyntaxError, ValueError, MemoryError) as e:
        raise SpecError("%s: cannot be parsed (%s)" % (path, e))
    env, bound, gone, notes, stop = {}, {}, {}, [], ""
    for i, node in enumerate(tree.body):
        line = _lines_of(node)[0]
        try:
            kind = _py_stmt(node, env, bound, gone)
        except Exception as e:               # SpecError, an unreadable argument, Python's own error (M.remove(x)) ...
            raise SpecError("%s: line %d: %s" % (path, line, "nested too deeply to read" if isinstance(e, RecursionError)
                            else e if isinstance(e, SpecError) else "unsupported %s" % e if isinstance(e, _Unsupported)
                            else "%s: %s" % (type(e).__name__, e)))
        if not kind:
            continue
        try:
            (hit, rebinds), deep = _tail(tree, i, env), []
        except RecursionError:                               # deep runtime code is never read: not a spec error
            hit, rebinds, deep = None, [], ["tail not checked (nested too deeply)"]
        if hit and hit[2]:
            raise SpecError("%s: line %d (function %s, defined at line %d): changes %s -- it may be called after reading "
                            "stopped at line %d; move the change above line %d or write the list out"
                            % (path, hit[0], hit[2][0], hit[2][1], hit[1], line, line))
        if hit:
            raise SpecError("%s: line %d: changes %s after reading stopped at line %d ('%s') -- move it above line %d "
                            "or write the list out" % (path, hit[0], hit[1], line, kind, line))
        extra = ["the tail rebinds %s: ignored" % ", ".join(rebinds)] * bool(rebinds) + deep
        stop = ("line %d: reading stops at '%s' -- lines %d-%d are runtime code: not read, and not checked except for "
                "direct changes to MUTANTS, M, COPY, TEST, TIMEOUT, ENV%s" % (line, kind, line, tree.body[-1].end_lineno,
                                                                             " (%s)" % "; ".join(extra) if extra else ""))
        notes.append("%s %s" % (path.name, stop))
        break
    key = "MUTANTS" if "MUTANTS" in bound else "M"           # never M once MUTANTS was assigned
    muts = env.get(key)
    if not isinstance(muts, (list, tuple)):
        why = muts.why if isinstance(muts, _Unknown) else gone.get(key) or (
            "%s (line %d) is a %s" % (key, bound[key], type(muts).__name__) if key in env else stop)
        raise SpecError("%s: no top-level literal MUTANTS (or M) list%s" % (path, (" -- " + why) if why else ""))
    for w in ("COPY", "TEST", "TIMEOUT", "ENV"):
        if isinstance(env.get(w), _Unknown):                 # never fall back silently to "not given"
            raise SpecError("%s: %s cannot be read -- %s" % (path, w, env[w].why))
    return {"mutants": list(muts), "copy": env.get("COPY"), "test": env.get("TEST"),
            "timeout": env.get("TIMEOUT"), "env": env.get("ENV"), "notes": notes}


def norm_relpath(p, what):
    """Relative POSIX path inside --root ('./a/../b.py' -> 'b.py'); absolute or escaping paths are refused."""
    s = str(p).replace("\\", "/")
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        raise SpecError("%s must be a path relative to --root (got %s)" % (what, _show(p)))
    n = posixpath.normpath(s)
    if n == ".." or n.startswith("../"):
        raise SpecError("%s must stay inside --root (got %s)" % (what, _show(p)))
    return n


def _norm_edit(e, where):
    if isinstance(e, dict):
        d = dict(e)
    elif isinstance(e, (list, tuple)) and len(e) in (3, 4):
        d = {"file": e[0], "old": e[1], "new": e[2]}
        if len(e) == 4:
            d["func"] = e[3]
    else:
        raise SpecError("%s: edit must be {file, old, new[, func, count]} or (file, old, new[, func])" % where)
    for k in ("file", "old", "new"):
        if not isinstance(d.get(k), str):
            raise SpecError("%s: edit field %r must be a string" % (where, k))
    if not d["old"]:
        raise SpecError("%s: empty 'old' pattern" % where)
    if d.get("func") is not None and not isinstance(d["func"], str):
        raise SpecError("%s: 'func' must be a string" % where)
    if not isinstance(d.get("count", 1), int) or d.get("count", 1) < 1:
        raise SpecError("%s: 'count' must be a positive integer" % where)
    out = {"file": norm_relpath(d["file"], "%s: edit file" % where), "old": d["old"], "new": d["new"]}
    if d.get("func"):
        out["func"] = d["func"]
    if d.get("count", 1) != 1:
        out["count"] = d["count"]
    return out


def _norm_mutant(m, i):
    where = "mutant #%d" % i
    if isinstance(m, dict):
        mid, name, edits, test = m.get("id"), m.get("name") or "", m.get("edits"), m.get("test")
        if not mid and name:
            mid, _, name = name.partition(" ")
    elif isinstance(m, (list, tuple)) and len(m) == 2:
        mid, _, name = str(m[0]).partition(" ")
        edits, test = m[1], None
    else:
        raise SpecError("%s: must be {id, name, edits} or (\"ID name\", [edits])" % where)
    if not mid or not isinstance(mid, str):
        raise SpecError("%s: missing id" % where)
    if not isinstance(edits, (list, tuple)) or not edits:
        raise SpecError("%s (%s): no edits" % (where, mid))
    out = {"id": mid, "name": name.strip(), "edits": [_norm_edit(e, "%s (%s)" % (where, mid)) for e in edits]}
    if test:
        out["test"] = test
    return out


def load_spec(path):
    path = Path(path)
    if not path.is_file():
        raise SpecError("spec not found: %s" % path)
    if path.suffix.lower() == ".py":
        data = _py_spec(path)
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            raise SpecError("%s: bad JSON (%s)" % (path, e))
        if isinstance(data, list):
            data = {"mutants": data}
        if not isinstance(data, dict):
            raise SpecError("%s: top level must be an object or a list" % path)
    try:
        muts = [_norm_mutant(m, i) for i, m in enumerate(data.get("mutants") or [])]
        json.dumps(muts)                                     # a legacy value may contain itself: refuse it here
    except (RecursionError, ValueError, TypeError) as e:
        raise SpecError("%s: a mutant holds a value that is not plain data (%s)" % (path, e))
    if not muts:
        raise SpecError("%s: no mutants" % path)
    seen, dup = set(), set()
    for m in muts:
        if m["id"] in seen:
            dup.add(m["id"])
        seen.add(m["id"])
    if dup:
        raise SpecError("duplicate mutant ids: %s" % ", ".join(sorted(dup)))
    copy = data.get("copy")
    if isinstance(copy, str):
        copy = [copy]
    env = data.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise SpecError("%s: 'env' must map strings to strings" % path)
    return {"copy": copy, "test": data.get("test"), "timeout": data.get("timeout"), "env": env, "mutants": muts,
            "notes": data.get("notes") or []}


# --------------------------------------------------------------------------- AST anchors
def _defs(tree):
    """[(qualified name, node)] for every def/class, outermost first."""
    out = []

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + ch.name
                out.append((q, ch))
                walk(ch, q + ".")
            else:
                walk(ch, prefix)
    walk(tree, "")
    return out


def _lines_of(node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


def _parse(text):
    return ast.parse(text[1:] if text.startswith("﻿") else text)     # a BOM is on line 1: line numbers unchanged


def _find_def(text, qual, where):
    try:
        tree = _parse(text)
    except SyntaxError as e:
        raise PatternError("cannot parse %s (%s)" % (where, e.msg))
    defs = _defs(tree)
    hits = [n for q, n in defs if q == qual] or [n for q, n in defs if q.rsplit(".", 1)[-1] == qual]
    if not hits:
        raise PatternError("func %s not found in %s" % (qual, where))
    if len(hits) > 1:
        raise PatternError("func %s ambiguous in %s (%d defs)" % (qual, where, len(hits)))
    return hits[0]


def _line_starts(text):
    """Start offset of every line, counting only "\\n" as ast does (str.splitlines would also split on \\f etc.)."""
    starts, p = [0], text.find("\n")
    while p >= 0:
        starts.append(p + 1)
        p = text.find("\n", p + 1)
    return starts


def func_span(text, qual, where):
    """Character span [lo, hi) of def/class `qual` (qualified 'Class.method' or a unique simple name)."""
    s, e = _lines_of(_find_def(text, qual, where))
    starts = _line_starts(text)
    return starts[s - 1], (starts[e] if e < len(starts) else len(text))


def func_source(text, qual):
    """Source of def/class `qual`, or None when it is missing, ambiguous or the file does not parse."""
    try:
        lo, hi = func_span(text, qual, "")
    except PatternError:
        return None
    return text[lo:hi]


def enclosing_def(text, lo, hi):
    """Qualified name of the innermost def/class containing text[lo:hi]; None at module level or on error."""
    try:
        tree = _parse(text)
    except SyntaxError:
        return None
    l1, l2 = text.count("\n", 0, lo) + 1, text.count("\n", 0, max(hi - 1, lo)) + 1
    best = None
    for q, n in _defs(tree):
        s, e = _lines_of(n)
        if s <= l1 and l2 <= e and (best is None or e - s <= best[1] - best[0]):
            best = (s, e, q)
    return best[2] if best else None


# --------------------------------------------------------------------------- edits
def apply_edit(text, e):
    old, new, want = e["old"], e["new"], e.get("count", 1)
    if old == new:
        raise PatternError("no-op edit in %s (old == new)" % e["file"])
    lo, hi, where = 0, len(text), e["file"]
    if e.get("func"):
        lo, hi = func_span(text, e["func"], e["file"])
        where = "%s:%s" % (e["file"], e["func"])
    seg = text[lo:hi]
    n = seg.count(old)
    if n != want:
        raise PatternError("PATTERN x%d in %s" % (n, where) + ("" if want == 1 else " (want %d)" % want))
    return text[:lo] + seg.replace(old, new) + text[hi:]


def _read(path):
    try:
        with open(path, encoding="utf-8", newline=None) as f:     # universal newlines: CRLF files match "\n" patterns
            return f.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as e:
        raise PatternError("cannot read %s as UTF-8 (%s)" % (path, e.reason))


def apply_edits(root, mutant):
    texts = {}
    for e in mutant["edits"]:
        f = e["file"]
        if f not in texts:
            t = _read(Path(root) / f)
            if t is None:
                raise PatternError("file not found: %s" % f)
            texts[f] = t
        texts[f] = apply_edit(texts[f], e)
    return texts


def syntax_errors(texts):
    bad = []
    for f, t in texts.items():
        if f.endswith(".py"):
            try:
                compile(t[1:] if t.startswith("﻿") else t, f, "exec", dont_inherit=True)
            except SyntaxError as e:
                bad.append("%s:%s %s" % (f, e.lineno, e.msg))
    return "; ".join(bad)


# --------------------------------------------------------------------------- copies, fingerprints
def norm_copy(copy):
    """Normalized, sorted, de-duplicated --copy paths; a path inside another listed path is dropped."""
    cs = sorted({norm_relpath(c, "--copy path") for c in (copy or [])})
    if "." in cs:
        return ["."]
    return [c for c in cs if not any(c.startswith(o + "/") for o in cs if o != c)]


def _ignored(rel_parts):
    return any(p in IGNORED_PARTS for p in rel_parts) or (rel_parts and rel_parts[-1].endswith((".pyc", ".pyo")))


def _files(root, copy):
    """{relative POSIX path: Path} of every copied file -- keyed and later sorted by the POSIX string, so the
    order does not depend on the host's path sorting or on the --copy order."""
    out = {}
    for c in copy:
        p = Path(root) / c
        if p.is_file():
            out[c] = p
        elif p.is_dir():
            for x in p.rglob("*"):
                rel = x.relative_to(root)
                if x.is_file() and not _ignored(rel.parts):
                    out[rel.as_posix()] = x
        else:
            raise SpecError("--copy path not found: %s" % p)
    return out


def tree_fp(root, copy):
    """Fingerprint of the copied code (line endings normalized) -- results from another code state are 'stale'."""
    h = hashlib.sha256()
    files = _files(root, copy)
    for rel in sorted(files):
        h.update(rel.encode("utf-8") + b"\0")
        h.update(files[rel].read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()[:12]


def env_fp(spec_env, copy):
    """Where a baseline ran: interpreter, platform, host, spec env, copy set -- a stored baseline is reused only here."""
    blob = json.dumps({"python": sys.executable, "version": platform.python_version(), "platform": sys.platform,
                       "host": platform.node(), "env": spec_env or {}, "copy": copy}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def mutant_fp(m, test, spec_env=None):
    blob = json.dumps({"edits": m["edits"], "test": m.get("test") or test, "env": spec_env or {}},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def runner_fp():
    try:
        return hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]
    except OSError:
        return "?"


def inside_copy(path, root, copy):
    """The --copy entry that contains `path` (or None)."""
    p = Path(path).resolve()
    for c in copy:
        base = (Path(root) / c).resolve()
        if p == base or base in p.parents:
            return c
    return None


def cmd_key(cmd):
    return cmd if isinstance(cmd, str) else json.dumps(list(cmd))


def copy_tree(root, dst, copy):
    for c in copy:
        src, out = Path(root) / c, Path(dst) / c
        if src.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        else:
            shutil.copytree(src, out, ignore=lambda d, names: [n for n in names if n in IGNORED_PARTS
                                                               or n.endswith((".pyc", ".pyo"))])


# --------------------------------------------------------------------------- running tests
def split_cmd(cmd, windows):
    """Command string -> argv. On Windows shlex is used in non-POSIX mode (backslash paths) and surrounding
    double quotes are removed from each token -- Popen quotes the tokens again for CreateProcess."""
    if isinstance(cmd, (list, tuple)):
        return list(cmd)
    argv = shlex.split(cmd, posix=not windows)
    if windows:
        argv = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in argv]
    return argv


def _argv(cmd):
    argv = split_cmd(cmd, os.name == "nt")
    if argv and argv[0].lower() in PY_NAMES:
        argv[0] = sys.executable
    return argv


def _kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def kill_all_children():
    _STOP.set()
    for p in list(_CHILDREN.values()):          # no lock: this runs inside a signal handler
        _kill_tree(p)


def cpu_limit(timeout):
    """CPU seconds a test command may use: more than all cores can burn within the wall-clock timeout, so only a
    test that outlives its runner reaches it."""
    return min(int((os.cpu_count() or 2) * timeout) + 60, 2 ** 31 - 6)     # + 5 (hard limit) must fit any rlim_t


def _supervised(argv, timeout):
    if os.name == "nt":
        return argv
    try:
        import resource  # noqa: F401  (POSIX only)
    except ImportError:
        return argv
    return [sys.executable, "-c", SUPERVISOR, str(cpu_limit(timeout)), str(2 * timeout + 30), str(os.getpid())] + argv


def run_cmd(cmd, cwd, timeout, extra_env, log_path):
    """Run `cmd` in `cwd`; output goes to a file (a hung grandchild cannot block a pipe).
    Returns (exit code or None on timeout, output text)."""
    if _STOP.is_set():
        raise RuntimeError("interrupted")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)               # never import the real (unmutated) tree by accident
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update({k: v.replace("{tmp}", str(cwd)) for k, v in (extra_env or {}).items()})
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with open(log_path, "wb") as log:
        p = subprocess.Popen(_supervised(_argv(cmd), timeout), cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env, **kw)
        with _CHILD_LOCK:
            _CHILDREN[p.pid] = p
        try:
            if _STOP.is_set():
                _kill_tree(p)
            try:
                code = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(p)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
                code = None
        finally:
            with _CHILD_LOCK:
                _CHILDREN.pop(p.pid, None)
    return code, Path(log_path).read_text(encoding="utf-8", errors="replace")


def ran_no_tests(out, code):
    """The test command ran nothing. unittest: its last 'Ran N tests' line (tests may print nested runner output,
    e.g. in assertion messages, before it) says 0. Otherwise (pytest): 'no tests ran' / a bare 'collected 0 items'
    with exit 0 or 5 (both runners use 5 for 'nothing ran')."""
    ran = UT_RAN.findall(out)
    if ran:
        return ran[-1] == "0"
    return code in (0, 5) and bool(NO_TESTS.search(out))


def failing_tests(out):
    names = []
    for line in out.splitlines():
        m = UT_LINE.match(line)                                         # unittest: "FAIL: name (module.Class.name)"
        if m:
            names.append(("load error: " + m.group(2)) if "_FailedTest" in m.group(3) else m.group(2))
        elif line.startswith(("FAILED ", "ERROR ")) and ("::" in line or ".py" in line):   # pytest
            nid = line.split(" ", 1)[1].split(" - ")[0].strip()
            names.append(nid.split("::")[-1])
    return sorted(set(names))


class Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def run_one(m, ctx):
    t0 = time.time()
    res = {"name": m["name"], "timeout": ctx.timeout}
    base = Path(tempfile.mkdtemp(prefix="mut_%s_" % re.sub(r"\W", "_", m["id"]), dir=ctx.tmp))
    keep = False
    try:
        texts = apply_edits(ctx.root, m)
        bad = syntax_errors(texts)
        if bad:
            res.update(status="invalid", detail="syntax error after edit: " + bad)
        else:
            work = base / "w"
            copy_tree(ctx.root, work, ctx.copy)
            for f, t in texts.items():
                p = work / f
                if not p.is_file():
                    raise SpecError("edited file is not under --copy: %s" % f)
                with open(p, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(t)
            code, out = run_cmd(m.get("test") or ctx.test, work, ctx.timeout, ctx.env, base / "log.txt")
            if code is None:
                res.update(status="timeout", detail="no result in %g s (process tree killed)" % ctx.timeout)
            elif os.name != "nt" and code == -getattr(signal, "SIGXCPU", 24):
                res.update(status="timeout", detail="CPU-time limit reached (SIGXCPU)")
            elif os.name != "nt" and code == -signal.SIGKILL:
                res.update(status="error", detail="killed by SIGKILL from outside (out of memory or an outside limit) "
                                                  "-- not counted as caught")
            elif ran_no_tests(out, code):
                res.update(status="error", detail="the test command ran no tests (exit %d)" % code)
            elif code == 0:
                res.update(status="survived", detail="tests passed")
                keep = bool(ctx.keep)
            else:
                names = failing_tests(out)
                res.update(status="killed", failing=names[:10],
                           detail=("killed by " + ", ".join(names[:3]) + (" +%d" % (len(names) - 3) if len(names) > 3 else ""))
                           if names else "killed (exit %d)" % code)
    except PatternError as e:
        res.update(status="pattern", detail=str(e))
    except Exception as e:                                           # runner-side problem: rerun with --only after fixing
        res.update(status="error", detail="%s: %s" % (type(e).__name__, e))
    finally:
        if keep:
            res["kept"] = str(base / "w")
        else:
            shutil.rmtree(base, ignore_errors=True)
    res["secs"] = round(time.time() - t0, 2)
    return m["id"], res


def run_baseline(ctx, cmd):
    base = Path(tempfile.mkdtemp(prefix="mut_base_", dir=ctx.tmp))
    t0 = time.time()
    limit = max(ctx.timeout * 2, 60)
    try:
        copy_tree(ctx.root, base / "w", ctx.copy)
        code, out = run_cmd(cmd, base / "w", limit, ctx.env, base / "log.txt")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    secs = round(time.time() - t0, 2)
    if code is None:
        return False, secs, "baseline tests did not finish in %g s" % limit
    if ran_no_tests(out, code):                                # before the exit code: unittest 3.12+ exits 5 here
        return False, secs, "baseline ran no tests -- check the test command / --copy:\n" + "\n".join(out.splitlines()[-10:])
    if code != 0:
        return False, secs, "baseline tests FAIL on the unmutated copy (exit %d):\n%s" % (
            code, "\n".join(out.splitlines()[-25:]))
    return True, secs, ""


# --------------------------------------------------------------------------- selection
def select_only(muts, only):
    toks = [t.strip() for t in only.split(",") if t.strip()]
    hit, picked = set(), []
    for m in muts:
        full = (m["id"] + " " + m["name"]).strip()
        matched = [t for t in toks if fnmatch.fnmatchcase(m["id"], t) or t == full]
        hit.update(matched)
        if matched:
            picked.append(m)
    missing = [t for t in toks if t not in hit]
    if missing:
        raise SpecError("--only matched nothing for: %s" % ", ".join(missing))
    return picked


def _baseline_reader(root, baseline):
    b = Path(baseline)
    if b.is_dir():
        return lambda f: _read(b / f)
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", baseline + "^{commit}"],
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise SpecError("--changed: %s is not a directory and git is not available" % baseline)
    if probe.returncode != 0:
        raise SpecError("--changed: %s is neither a directory nor a git commit in %s" % (baseline, root))

    def read(f):
        p = subprocess.run(["git", "-C", str(root), "show", "%s:./%s" % (baseline, f)], capture_output=True, timeout=60)
        return p.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n") if p.returncode == 0 else None
    return read


def mutant_changed(m, root, read_base):
    """True when the code a mutant edits differs from the baseline: the enclosing def/class of every occurrence
    is compared; an edit at module level counts as changed when its file changed."""
    for e in m["edits"]:
        try:
            cur, base = _read(Path(root) / e["file"]), read_base(e["file"])
        except PatternError:
            return True                                              # unreadable: let the run report it
        if cur is None or base is None:
            return True
        if cur == base:
            continue
        if e.get("func"):
            quals = [e["func"]]
        else:
            if cur.count(e["old"]) != e.get("count", 1):
                return True                                          # will show up as 'pattern'
            quals, pos = [], cur.find(e["old"])
            while pos >= 0:
                quals.append(enclosing_def(cur, pos, pos + len(e["old"])))
                pos = cur.find(e["old"], pos + len(e["old"]))
        for q in quals:
            if q is None:
                return True                                          # module-level edit in a changed file
            a, b = func_source(cur, q), func_source(base, q)
            if a is None or a != b:
                return True
    return False


def parse_slice(s):
    m = re.fullmatch(r"\s*(-?\d*)\s*:\s*(-?\d*)\s*", s or "")
    if not m:
        raise SpecError("--slice must look like A:B, A: or :B (got %r)" % s)
    return slice(int(m.group(1)) if m.group(1) else None, int(m.group(2)) if m.group(2) else None)


# --------------------------------------------------------------------------- results file
def load_out(path):
    if path and Path(path).is_file():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except ValueError as e:
            raise SpecError("%s is not readable JSON (%s)" % (path, e))
        if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
            raise SpecError("%s is not a mutation_runner results file" % path)
        data.setdefault("meta", {})
        return data
    return {"meta": {}, "results": {}}


def save_out(path, data, tries=10, allow_nan=True):
    text = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=allow_nan)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("%s.%d.tmp" % (p.name, os.getpid()))
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            os.replace(str(tmp), str(p))
            return
        except PermissionError:                 # Windows: another process has the file open without delete sharing
            if i == tries - 1:
                raise
            time.sleep(0.2)


@contextlib.contextmanager
def run_lock(out_path):
    """One runner per results file at a time. The lock lives in the system temp dir (nothing to clean up in the
    repository) and is released by the OS when the process ends, even when it is killed."""
    key = hashlib.sha256(str(Path(out_path).resolve()).encode("utf-8")).hexdigest()[:16]
    lp = Path(tempfile.gettempdir()) / ("mutation_runner_%s.lock" % key)
    fh = open(lp, "a+")
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", -1),
                           getattr(errno, "EDEADLK", -1)):
                raise SpecError("another mutation_runner is writing %s right now (lock %s) -- run the calls one after "
                                "another" % (out_path, lp))
            print("warning: cannot lock %s (%s) -- do not run two runners on the same --out" % (lp, e),
                  file=sys.stderr)
        yield
    finally:
        if locked and os.name == "nt":
            try:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        fh.close()


def is_fresh(r, fp, mfp, timeout):
    """A stored result still answers for this code and this mutant definition. A timeout recorded under a smaller
    --timeout is not fresh (raising --timeout retries it). error results are fresh: rerun them with --only."""
    if not r or r.get("fp") != fp or r.get("mfp") != mfp:
        return False
    return not (r.get("status") == "timeout" and float(r.get("timeout") or 0) < timeout)


def summarize(ids, results, fp=None, mfps=None):
    lists = {s: [] for s in STATUSES + STRICT_BAD}
    for i in ids:
        r = results.get(i)
        if not r:
            lists["not run"].append(i)
            continue
        lists[r.get("status", "error")].append(i)
        if fp is not None and (r.get("fp") != fp or r.get("mfp") != (mfps or {}).get(i)):
            lists["stale"].append(i)
    return lists


def unverified(ids, results):
    """Results not backed by a passing baseline of their test command, on this code, in the environment that
    produced them (results made with --skip-baseline, or by an older runner, are unverified)."""
    return [i for i in ids if results.get(i) and not results[i].get("verified")]


def gate(lists, strict):
    bad = list(ALWAYS_BAD) + (list(STRICT_BAD) if strict else [])
    return [(k, lists[k]) for k in bad if lists.get(k)]


def print_summary(label, lists, total, strict, show_gate=True):
    parts = ["%s %d" % (k, len(lists[k])) for k in STATUSES + STRICT_BAD
             if lists.get(k) or k in ("killed", "survived")]
    print("%s: total %d · %s" % (label, total, " · ".join(parts)))
    fails = gate(lists, strict)
    if not show_gate:
        return 1 if fails else 0
    if fails:
        print("GATE: FAIL (%s)" % "; ".join("%s: %s" % (k, ", ".join(v[:12]) + (" …" if len(v) > 12 else ""))
                                           for k, v in fails))
    else:
        warn = [(k, lists[k]) for k in STRICT_BAD if lists.get(k)]
        print("GATE: PASS" + (" (warn — %s; final gate: --summary --strict)" % "; ".join(
            "%s %d" % (k, len(v)) for k, v in warn) if warn else ""))
    return 1 if fails else 0


def _install_signal_handlers():
    def handler(signum, frame):
        kill_all_children()
        print("interrupted (signal %d) -- test processes killed, results so far are saved" % signum,
              file=sys.stderr, flush=True)
        raise SystemExit(2)
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# --------------------------------------------------------------------------- main
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="mutant spec (.json, or legacy .py read without executing it)")
    ap.add_argument("--root", default=".", help="repository root the spec paths are relative to (default: .)")
    ap.add_argument("--copy", action="append", help="path (dir or file) copied for each mutant; repeatable (else spec 'copy')")
    ap.add_argument("--test", help="test command run in the copy (else spec 'test'); leading 'python' = this interpreter")
    ap.add_argument("--timeout", type=float, help="seconds per mutant before the process tree is killed (else spec, else 60)")
    ap.add_argument("--jobs", type=int, default=1, help="mutants run in parallel (use the core count; default 1)")
    ap.add_argument("--only", help="comma list of mutant IDs, globs (R0*) or full 'ID name' strings")
    ap.add_argument("--changed", metavar="BASELINE", help="only mutants whose function changed vs a snapshot dir or git ref"
                                                          " (+ mutants with no result in --out)")
    ap.add_argument("--slice", help="python slice A:B over the selected list (manual chunks; applied before --resume)")
    ap.add_argument("--out", help="results JSON; merged across runs (each mutant result replaced when it runs)")
    ap.add_argument("--resume", action="store_true", help="skip mutants that already have a fresh result in --out")
    ap.add_argument("--budget", type=float, help="stop starting mutants after about this many seconds (host call limit);"
                                                 " implies --resume; exit 3 while some remain")
    ap.add_argument("--check", action="store_true", help="only check that every selected mutant still applies (no tests)")
    ap.add_argument("--list", action="store_true", help="list the selected mutants (index, id, name, targets) and exit")
    ap.add_argument("--summary", action="store_true", help="print the merged summary of --out and exit (no runs)")
    ap.add_argument("--strict", action="store_true",
                    help="gate also fails on timeout, stale, not-run and unverified (no baseline for this code)")
    ap.add_argument("--skip-baseline", action="store_true", help="do not run the unmutated tests first")
    ap.add_argument("--keep", action="store_true", help="keep the copies of survived mutants (paths printed)")
    ap.add_argument("--tmp", help="directory for temporary copies (default: system temp)")
    ap.add_argument("--snapshot", metavar="DIR", help="copy the --copy paths to DIR as a later --changed baseline, then exit")
    ap.add_argument("--dump-spec", metavar="FILE", help="write the normalized spec as JSON (e.g. to convert a legacy .py) and exit")
    return ap


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    t_start = time.time()
    a = build_parser().parse_args(argv)
    try:
        return _main(a, t_start)
    except SpecError as e:
        print("ERROR:", e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        kill_all_children()
        print("interrupted -- test processes killed", file=sys.stderr)
        return 2
    except Exception as e:                                           # never exit 1 (= gate fail) on a crash
        print("ERROR: unexpected %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2


def _show(v):
    """v for an error message, about 80 characters at most. Python refuses the repr of an int over 4300 digits (also
    inside a container): a huge int is shown by its size, anything else that repr refuses by its type."""
    if isinstance(v, int) and not isinstance(v, bool) and v.bit_length() > 128:
        return "an integer of about %d digits" % int(v.bit_length() * 0.30103 + 1)
    try:
        text = repr(v)
    except ValueError:
        return "<a %s too large to show>" % type(v).__name__
    return text if len(text) <= 80 else text[:77] + "..."


def _secs(v, what):
    """v as seconds in (0, MAX_SECS]: an int or float (argparse gives floats); bool, str, nan, inf or a number too
    large for a float is a usage error."""
    try:
        f = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    except OverflowError:                                    # TIMEOUT = 10 ** 400
        f = None
    if f is None or not 0 < f <= MAX_SECS:                   # also refuses nan and inf
        raise SpecError("%s must be a positive number of seconds (at most %g; got %s)" % (what, MAX_SECS, _show(v)))
    return f


def _main(a, t_start):
    spec = load_spec(a.spec)
    for n in spec["notes"]:
        print("note:", n, file=sys.stderr)
    root = Path(a.root).resolve()
    muts = spec["mutants"]
    copy = norm_copy(a.copy or spec["copy"])
    test = a.test or spec["test"]
    senv = spec["env"]

    timeout = _secs(a.timeout if a.timeout is not None else spec["timeout"] if spec["timeout"] is not None else 60,
                    "--timeout / spec timeout")
    if a.budget is not None:
        _secs(a.budget, "--budget")
    if a.dump_spec:
        if spec["timeout"] is not None:
            _secs(spec["timeout"], "spec timeout")           # what the dump writes, also under --timeout
        try:
            save_out(a.dump_spec, {"copy": copy or None, "test": test, "timeout": spec["timeout"], "env": senv,
                                   "mutants": muts}, allow_nan=False)
        except ValueError as e:                              # nan/inf in a value: not JSON
            raise SpecError("cannot write %s: %s" % (a.dump_spec, e))
        print("wrote %s (%d mutants)" % (a.dump_spec, len(muts)))
        return 0
    resume = a.resume or a.budget is not None
    if (resume or a.summary) and not a.out:
        raise SpecError("--resume/--budget/--summary need --out RESULTS.json (the merged results are what the next call "
                        "goes on from)")
    if a.snapshot:
        if not copy:
            raise SpecError("nothing to copy: give --copy or spec 'copy' (dirs/files the tests need)")
        d = Path(a.snapshot)
        if inside_copy(d, root, copy):
            raise SpecError("--snapshot %s is inside the copied path %r -- put it elsewhere" % (d, inside_copy(d, root, copy)))
        if d.exists() and (not d.is_dir() or (any(d.iterdir()) and not (d / ".mutation_snapshot").is_file())):
            raise SpecError("--snapshot %s exists and is not a snapshot directory" % d)
        shutil.rmtree(d, ignore_errors=True)
        copy_tree(root, d, copy)
        sfp = tree_fp(root, copy)
        (d / ".mutation_snapshot").write_text("fp %s\n" % sfp, encoding="utf-8")
        print("snapshot %s (fp %s)" % (d, sfp))
        return 0

    running = not (a.summary or a.list or a.check)
    with (run_lock(a.out) if (a.out and running) else contextlib.nullcontext()):
        return _select_and_run(a, t_start, spec, root, muts, copy, test, timeout, senv, resume, running)


def _select_and_run(a, t_start, spec, root, muts, copy, test, timeout, senv, resume, running):
    out = load_out(a.out) if a.out else None
    if out is not None and (not test or not copy):            # e.g. --summary without the run's --test/--copy
        mt, mc = out["meta"].get("test"), out["meta"].get("copy")
        if (not test and mt) or (not copy and mc):
            print("note: using the --test/--copy recorded in %s" % a.out, file=sys.stderr)
        test = test or mt
        copy = copy or norm_copy(mc)
    sel, notes = muts, []
    if a.only:
        sel = select_only(sel, a.only)
        notes.append("only %s" % a.only)
    if a.changed:
        read_base = _baseline_reader(root, a.changed)
        sel = [m for m in sel if mutant_changed(m, root, read_base) or (out is not None and m["id"] not in out["results"])]
        notes.append("changed vs %s" % a.changed)
    if a.slice:
        sel = sel[parse_slice(a.slice)]
        notes.append("slice %s" % a.slice)

    def show(ms):
        index = {m["id"]: i for i, m in enumerate(muts)}
        for m in ms:
            tg = sorted({e["file"] + (":" + e["func"] if e.get("func") else "") for e in m["edits"]})
            print("%4d  %-8s %s  [%s]" % (index[m["id"]], m["id"], m["name"], ", ".join(tg)))

    if a.list and not resume:
        print("%d of %d mutants selected%s" % (len(sel), len(muts), (" (" + "; ".join(notes) + ")") if notes else ""))
        show(sel)
        return 0
    if a.check:
        print("mutation_runner: %d of %d mutants selected%s" % (len(sel), len(muts),
                                                                (" (" + "; ".join(notes) + ")") if notes else ""))
        bad = 0
        for m in sel:
            try:
                err = syntax_errors(apply_edits(root, m))
                if err:
                    bad += 1
                    print("%-8s invalid  %s" % (m["id"], err))
            except PatternError as e:
                bad += 1
                print("%-8s pattern  %s" % (m["id"], e))
        print("check: %d of %d mutants do not apply cleanly" % (bad, len(sel)))
        return 1 if bad else 0

    if not copy:
        raise SpecError("nothing to copy: give --copy or spec 'copy' (dirs/files the tests need)")
    for what, path in (("--out", a.out), ("--tmp", a.tmp)):
        c = path and inside_copy(path, root, copy)
        if c:
            raise SpecError("%s %s is inside the copied path %r -- it would be copied into every mutant and change the "
                            "code fingerprint; put it elsewhere or narrow --copy" % (what, path, c))
    fp = tree_fp(root, copy)
    mfps = {m["id"]: mutant_fp(m, test, senv) for m in muts}
    if out is not None:
        orphans = [k for k in out["results"] if k not in mfps]
        if orphans:
            print("note: %d results in %s are not in the spec any more (dropped on save): %s"
                  % (len(orphans), a.out, ", ".join(orphans[:10])))

    if a.summary:
        ids = [m["id"] for m in sel]
        lists = summarize(ids, out["results"], fp, mfps)
        lists["unverified"] = unverified(ids, out["results"])
        return print_summary("merged", lists, len(sel), a.strict)

    if resume:
        before = len(sel)
        sel = [m for m in sel if not is_fresh(out["results"].get(m["id"]), fp, mfps[m["id"]], timeout)]
        notes.append("resume: %d already fresh" % (before - len(sel)))
    if a.list:
        print("%d of %d mutants selected (%s)" % (len(sel), len(muts), "; ".join(notes)))
        show(sel)
        return 0
    print("mutation_runner: %d of %d mutants selected%s · fp %s" % (
        len(sel), len(muts), (" (" + "; ".join(notes) + ")") if notes else "", fp))

    missing_test = [m["id"] for m in sel if not (m.get("test") or test)]
    if missing_test:
        raise SpecError("no test command for %s: give --test or spec 'test'" % ", ".join(missing_test[:10]))
    ctx = Ctx(root=root, copy=copy, test=test, timeout=timeout, env=senv, keep=a.keep, tmp=a.tmp, jobs=max(1, a.jobs))
    if a.tmp:
        Path(a.tmp).mkdir(parents=True, exist_ok=True)
    _install_signal_handlers()
    efp = env_fp(senv, copy)
    run_res = {}
    verified = set()                   # test commands whose baseline passed here (run now or reused) for this code

    def save(final=False):
        out["results"] = {m["id"]: out["results"][m["id"]] for m in muts if m["id"] in out["results"]}
        out["meta"].update(tool="mutation_runner", version=VERSION, runner=runner_fp(), spec=str(a.spec),
                           root=str(root), copy=copy, test=test, timeout=timeout, env=senv, total=len(muts), fp=fp,
                           spec_notes=spec["notes"], updated=_now())
        try:
            save_out(a.out, out)
        except OSError as e:
            if final:
                raise
            print("warning: could not save %s (%s) -- trying again at the next save" % (a.out, e), file=sys.stderr)

    def record_run(started, elapsed):
        if out is None:
            return
        runs = out["meta"].get("runs") or []
        runs.append({"at": _now(), "selected": len(sel), "ran": started, "secs": round(elapsed, 1), "jobs": ctx.jobs,
                     "notes": notes, "runner": runner_fp(), "python": platform.python_version(),
                     "executable": sys.executable, "env": efp, "platform": sys.platform, "host": platform.node(),
                     "skip_baseline": bool(a.skip_baseline)})
        out["meta"]["runs"] = runs[-50:]
        save(final=True)

    # baselines: every distinct test command must pass on the unmutated copy and actually run tests
    baselines = out["meta"].setdefault("baselines", {}) if out is not None else {}
    base_secs = 0.0
    if sel and not a.skip_baseline:
        users = {}
        for m in sel:
            users.setdefault(cmd_key(m.get("test") or test), (m.get("test") or test, []))[1].append(m["id"])
        ran_here, last = 0, 0.0
        for key, (cmd, ids) in users.items():
            prev = baselines.get(key) or {}
            if prev.get("ok") and prev.get("fp") == fp and prev.get("env") == efp:
                base_secs = max(base_secs, float(prev.get("secs") or 0))
                verified.add(key)
                print("baseline: already passed for fp %s here (%.1f s) -- %s" % (fp, prev.get("secs") or 0, key))
                continue
            if a.budget is not None and ran_here and (time.time() - t_start) + last > a.budget:
                record_run(0, time.time() - t_start)
                print("INCOMPLETE: budget %g s reached during the baselines (%d of %d commands done, saved) -- run the "
                      "same command again" % (a.budget, len(verified), len(users)))
                return 3
            ok, secs, msg = run_baseline(ctx, cmd)
            if not ok:
                print("ERROR: %s\n(test command %s, used by %s)" % (msg, key, ", ".join(ids[:10])), file=sys.stderr)
                return 2
            ran_here, last = ran_here + 1, secs
            base_secs = max(base_secs, secs)
            verified.add(key)
            print("baseline: tests pass on the unmutated copy (%.1f s) -- %s" % (secs, key))
            baselines[key] = {"fp": fp, "env": efp, "ok": True, "secs": secs, "at": _now()}
            if out is not None:
                save()                                           # a call cut later still keeps this baseline
        if base_secs and timeout < 3 * base_secs:
            print("warning: --timeout %g s is under 3x the baseline (%.1f s) -- slow mutants may show as timeout"
                  % (timeout, base_secs))

    def on_done(mid, r):
        m = next(x for x in muts if x["id"] == mid)
        r.update(at=_now(), fp=fp, mfp=mfps[mid], verified=cmd_key(m.get("test") or test) in verified)
        run_res[mid] = r
        print("%-8s %-8s %6.1fs  %s | %s%s" % (mid, r["status"], r["secs"], r["name"][:44], r["detail"],
                                               (" | kept " + r["kept"]) if r.get("kept") else ""), flush=True)
        if out is not None:
            out["results"][mid] = r
            save()                                                   # after every mutant: a killed call loses little

    started = 0
    pending = set()
    todo = iter(sel)
    est = base_secs
    stopped_by_budget = False
    with cf.ThreadPoolExecutor(max_workers=ctx.jobs) as ex:
        while True:
            while len(pending) < ctx.jobs:
                if a.budget is not None and started > 0 and (time.time() - t_start) + est > a.budget:
                    stopped_by_budget = True
                    break
                m = next(todo, None)
                if m is None:
                    break
                pending.add(ex.submit(run_one, m, ctx))
                started += 1
            if not pending:
                break
            done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                mid, r = fut.result()
                if r["status"] in ("killed", "survived"):
                    est = max(est, r["secs"])
                on_done(mid, r)

    elapsed = time.time() - t_start
    remaining = len(sel) - started
    record_run(started, elapsed)
    slow = [(r["secs"], i) for i, r in run_res.items() if r["status"] in ("killed", "survived")]
    if slow and max(slow)[0] > 0.6 * timeout:
        s, i = max(slow)
        print("warning: slowest mutant %s took %.1f s of --timeout %g s -- keep the timeout at least 1.5x the slowest "
              "(%.0f s)" % (i, s, timeout, 1.5 * s))
    if started:
        ids = [m["id"] for m in sel[:started]]
        this = summarize(ids, run_res)
        this["unverified"] = unverified(ids, run_res)
        print_summary("this run (%.0f s)" % elapsed, this, started, a.strict, show_gate=out is None)
    else:
        print("nothing to run -- 0 mutants selected (this is not evidence)")
    if out is not None:
        scope = [m["id"] for m in (select_only(muts, a.only) if a.only else muts)]
        lists = summarize(scope, out["results"], fp, mfps)
        lists["unverified"] = unverified(scope, out["results"])
        code = print_summary("merged %s" % a.out, lists, len(scope), a.strict)
    elif started:
        code = 1 if gate(this, a.strict) else 0
    else:
        code = 1 if a.strict else 0
    if remaining > 0 and stopped_by_budget:
        print("INCOMPLETE: budget %g s reached, %d selected mutants not started -- run the same command again"
              % (a.budget, remaining))
        return 3
    return code


if __name__ == "__main__":
    sys.exit(main())
