"""Cooperative, exclusive writer roots in one configured userdata domain.

Only participants using this protocol are fenced. Epochs count admission and
completion, not business generations. A barrier never grants snapshot or
activation readiness. Filesystem I/O itself has no hard wall-clock bound.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import threading
import time
import uuid

from . import fileio
from .path_policy import plain_path
from .runtime_activity import ActivityUnavailable, validate_timeout

_STATE_LIMIT = 16384
_MAX_INTEGER = 2**63 - 1
_guard = threading.Lock()
_mutexes = {}
_uncertain = {}
_local = threading.local()
_FIELDS = {'schema_version', 'domain_id', 'epoch', 'active',
           'interrupted_count', 'last_interrupted'}


class WriterUnavailable(ActivityUnavailable):
    """Refuse an admission, control or metadata operation before authorization."""


def _uuid_hex(value):
    return (type(value) is str and len(value) == 32 and
            all(character in '0123456789abcdef' for character in value))


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= _MAX_INTEGER


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate metadata field')
        result[key] = value
    return result


def _validate_state(state):
    if (type(state) is not dict or set(state) != _FIELDS or
            type(state['schema_version']) is not int or state['schema_version'] != 1 or
            not _uuid_hex(state['domain_id']) or not _integer(state['epoch']) or
            not _integer(state['interrupted_count']) or
            (state['last_interrupted'] is not None and not _uuid_hex(state['last_interrupted']))):
        raise ValueError('invalid writer metadata')
    if bool(state['interrupted_count']) != (state['last_interrupted'] is not None):
        raise ValueError('invalid interrupted marker')
    active = state['active']
    if active is not None and (type(active) is not dict or
                               set(active) != {'root_id', 'pid'} or
                               not _uuid_hex(active['root_id']) or
                               not _integer(active['pid'], 1)):
        raise ValueError('invalid active marker')
    return state


def _new_state():
    return {'schema_version': 1, 'domain_id': uuid.uuid4().hex, 'epoch': 0,
            'active': None, 'interrupted_count': 0, 'last_interrupted': None}


def _view(state, status, **extra):
    result = {'scope': 'cooperative_writer_domain', 'status': status,
              'snapshot_ready': False, 'activation_ready': False,
              'coverage_complete': False}
    if state is not None:
        # Return independent JSON values; callers cannot edit the held state.
        result.update(json.loads(json.dumps(state)))
    result.update(extra)
    return result


class _Root:
    def __init__(self, domain, kind):
        self.domain, self.kind = domain, kind
        self.thread = threading.get_ident()
        self.token = object()
        self.live = True
        self.state = None


class _BarrierLease:
    """Opaque observation valid only in its owning lexical/thread context."""
    __slots__ = ('__owner', '__token')

    def __init__(self, owner):
        self.__owner, self.__token = owner, owner.token

    def view(self):
        """Return an unready cooperative observation or refuse an expired token."""
        owner = self.__owner
        if (not owner.live or owner.thread != threading.get_ident() or
                getattr(_local, 'root', None) is not owner or self.__token is not owner.token):
            raise WriterUnavailable('stale_lease')
        return _view(owner.state, 'valid')


class WriterDomain:
    """Serialize cooperative roots using a canonical control-directory identity.

    Args:
        control_dir: Path to this userdata domain's admission metadata directory.
            Canonical path aliases share one process mutex and OS lock.
    """

    def __init__(self, control_dir):
        self.control_dir = Path(os.path.normcase(str(Path(control_dir).expanduser().resolve())))
        self.state_path = self.control_dir / 'state.json'
        self._key = str(self.control_dir)
        with _guard:
            self._mutex = _mutexes.setdefault(self._key, threading.Lock())

    def _plain(self):
        plain_path(self.control_dir, self.control_dir)
        plain_path(self.control_dir / '.workflow' / 'wsflow.lock', self.control_dir)
        plain_path(self.state_path, self.control_dir)

    def _read(self):
        try:
            plain_path(self.state_path, self.control_dir)
            try:
                with self.state_path.open('rb') as stream:
                    data = stream.read(_STATE_LIMIT + 1)
            except FileNotFoundError:
                return None
            if len(data) > _STATE_LIMIT:
                raise ValueError('oversized writer metadata')
            return _validate_state(json.loads(data.decode('utf-8'), object_pairs_hook=_strict_pairs))
        except (OSError, UnicodeError, ValueError, RuntimeError) as error:
            raise WriterUnavailable('invalid_state') from error

    def _write(self, state):
        try:
            _validate_state(state)
            data = (json.dumps(state, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
            if len(data) > _STATE_LIMIT:
                raise ValueError('oversized writer metadata')
            plain_path(self.state_path, self.control_dir)
            for attempt in range(6):
                try:
                    fileio.atomic_write(self.state_path, data)
                    break
                except PermissionError as error:
                    if (os.name != 'nt' or
                            getattr(error, 'winerror', None) not in (5, 32, 33) or
                            attempt == 5):
                        raise
                    time.sleep(.01 * 2 ** attempt)
        except (OSError, ValueError, RuntimeError) as error:
            raise WriterUnavailable('persistence_failed') from error

    @contextmanager
    def _locked(self, timeout):
        deadline = time.monotonic() + timeout
        if not self._mutex.acquire(timeout=timeout):
            raise WriterUnavailable('busy')
        try:
            try:
                self._plain()
            except (OSError, ValueError, RuntimeError) as error:
                raise WriterUnavailable('invalid_state') from error
            with ExitStack() as locks:
                try:
                    locks.enter_context(fileio.locked(
                        self.control_dir, max(0, deadline - time.monotonic())))
                except fileio.LockBusy as error:
                    raise WriterUnavailable('busy') from error
                except (OSError, ValueError, RuntimeError) as error:
                    raise WriterUnavailable('control_unavailable') from error
                yield
        finally:
            self._mutex.release()

    def in_activity(self):
        """Return whether this thread owns a domain activity or barrier lease."""
        root = getattr(_local, 'root', None)
        return bool(root is not None and
                    root.domain._key == self._key and root.live)

    def _nested(self, kind):
        root = getattr(_local, 'root', None)
        if root is None:
            return None
        if root.domain._key != self._key:
            raise WriterUnavailable('different_domain')
        if kind == 'barrier':
            raise WriterUnavailable('reentrant_barrier')
        if root.kind == 'barrier':
            raise WriterUnavailable('reentrant_activity')
        return root

    @contextmanager
    def activity(self, timeout=5):
        """Hold a domain lease without durable mutation until promoted to writing.

        Args:
            timeout: Finite exact int/float seconds in [0, 30], lock-wait budget.
        Raises:
            ValueError: Invalid timeout, before admission or filesystem effects.
            WriterUnavailable: Busy domain, reentrance, or invalid control paths.
        """
        timeout = validate_timeout(timeout)
        if self._nested('activity') is not None:
            yield
            return
        with self._locked(timeout):
            root = _Root(self, 'activity')
            _local.root = root
            try:
                yield
            finally:
                try:
                    if root.state is not None:
                        self._finish(root)
                finally:
                    root.live = False
                    del _local.root

    def _begin(self, root):
        state = self._read() or _new_state()
        if state['epoch'] > _MAX_INTEGER - 2:
            raise WriterUnavailable('state_exhausted')
        with _guard:
            uncertain = _uncertain.get(self._key)
        lost = []
        if state['active'] is not None:
            lost.append(state['active']['root_id'])
        if (uncertain is not None and uncertain not in lost and
                uncertain != state['last_interrupted']):
            lost.append(uncertain)
        if state['interrupted_count'] > _MAX_INTEGER - len(lost):
            raise WriterUnavailable('state_exhausted')
        for cookie in lost:
            state['interrupted_count'] += 1
            state['last_interrupted'] = cookie
        state['epoch'] += 1
        state['active'] = {'root_id': uuid.uuid4().hex, 'pid': os.getpid()}
        self._write(state)
        root.state = state
        with _guard:
            _uncertain.pop(self._key, None)

    def _retain_failure(self, root):
        """Keep a conservative local latch and, when possible, durable unknown."""
        cookie = root.state['active']['root_id']
        with _guard:
            _uncertain[self._key] = cookie
        try:
            state = self._read()
            if (state is not None and state['domain_id'] == root.state['domain_id'] and
                    state['active'] is None and state['last_interrupted'] != cookie and
                    state['interrupted_count'] < _MAX_INTEGER):
                state['interrupted_count'] += 1
                state['last_interrupted'] = cookie
                self._write(state)
        except WriterUnavailable:
            # Failed storage cannot promise power-loss durability. A successfully
            # persisted begin marker and the live-process latch remain refused.
            pass

    def _finish(self, root):
        try:
            state = self._read()
            expected = root.state
            if (state is None or state['domain_id'] != expected['domain_id'] or
                    state['epoch'] != expected['epoch'] or state['active'] != expected['active'] or
                    state['interrupted_count'] != expected['interrupted_count'] or
                    state['last_interrupted'] != expected['last_interrupted']):
                raise WriterUnavailable('changed_state')
            state['active'] = None
            state['epoch'] += 1
            self._write(state)
        except WriterUnavailable:
            self._retain_failure(root)
            raise

    @contextmanager
    def writing(self, timeout=5):
        """Promote before effects; retain the marker through the outer activity.

        Completion records only the root's end, including on business exceptions.
        Interrupted outcomes remain latched; no recovery/reset operation exists.
        """
        with self.activity(timeout):
            root = _local.root
            if root.state is None:
                self._begin(root)
            yield

    @contextmanager
    def barrier(self, timeout=5):
        """Hold the same exclusive lease and yield a lexical/thread-bound view.

        Refuses active or unresolved roots, corrupt metadata and reentrance.
        The view captures admission metadata at acquisition, not current business
        generations. It remains an observation even while the lease is held.
        """
        timeout = validate_timeout(timeout)
        self._nested('barrier')
        with self._locked(timeout):
            state = self._read()
            with _guard:
                uncertain = self._key in _uncertain
            if uncertain or (state is not None and
                             (state['active'] is not None or state['interrupted_count'])):
                raise WriterUnavailable('unresolved')
            if state is None:
                state = _new_state()
                self._write(state)
            root = _Root(self, 'barrier')
            root.state = state
            _local.root = root
            try:
                yield _BarrierLease(root)
            finally:
                root.live = False
                del _local.root

    def observe(self):
        """Read <=16 KiB without admission wait, initialization or authorization.

        Missing and invalid metadata are explicit. The observation may race a
        writer and is neither a business snapshot nor a reusable barrier permit.
        """
        try:
            state = self._read()
        except WriterUnavailable as error:
            return _view(None, 'invalid', reason=error.reason)
        with _guard:
            uncertain = self._key in _uncertain
        return _view(state, 'missing' if state is None else 'valid', local_unknown=uncertain)


def default_domain():
    """Resolve the configured userdata admission domain at each call."""
    configured = os.environ.get('WSFLOW_DATA_DIR')
    base = (Path(configured) if configured else
            Path(os.environ.get('APPDATA') or Path.home() / '.config') / 'wsFlow')
    return WriterDomain(base / 'writer-admission')


def writer_method(function):
    """Keep a bound method's complete return inside self._writer_domain.writing."""
    @wraps(function)
    def invoke(self, *args, **kwargs):
        with self._writer_domain.writing():
            return function(self, *args, **kwargs)
    return invoke


def writer_function(function):
    """Use the current cooperative domain, or resolve the default at call time."""
    @wraps(function)
    def invoke(*args, **kwargs):
        root = getattr(_local, 'root', None)
        domain = root.domain if root is not None else default_domain()
        with domain.writing():
            return function(*args, **kwargs)
    return invoke
