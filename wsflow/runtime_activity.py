"""Cooperative activity admission for one host's synchronous runtime boundary.

An admitted thread can finish nested work after pause. This gate does not fence
standalone writers or other hosts; lifecycle owners decide whether resume is safe.
"""
from contextlib import contextmanager, nullcontext
from functools import wraps
import math
import threading
import time


class ActivityUnavailable(RuntimeError):
    """An activity or control operation was refused for the given reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_timeout(value, name='timeout') -> float:
    """Validate a finite timeout in seconds, inclusive range [0, 30].

    Args:
        value: An exact int or float; booleans and numeric subclasses are invalid.
        name: Parameter name included in the error message.
    Returns:
        The timeout as a float, in seconds.
    Raises:
        ValueError: If the type, finiteness or bounds are invalid.
    """
    if type(value) not in (int, float) or not 0 <= value <= 30 or not math.isfinite(value):
        raise ValueError(name + ' must be a finite int or float between 0 and 30 seconds')
    return float(value)


def normalize_activity(callback):
    """Return a context factory, using nullcontext for a standalone service.

    Args:
        callback: None or a callable returning an activity context manager.
    Returns:
        nullcontext for None, otherwise the original callable.
    Raises:
        ValueError: If callback is neither None nor callable.
    """
    if callback is None:
        return nullcontext
    if not callable(callback):
        raise ValueError('activity must be None or a callable context factory')
    return callback


def admitted(function):
    """Keep the complete synchronous method call inside self._activity()."""
    @wraps(function)
    def invoke(self, *args, **kwargs):
        with self._activity():
            return function(self, *args, **kwargs)
    return invoke


class ActivityGate:
    """Count root leases and pause new entries for one independent host.

    Same-thread nesting belongs to its existing root. A timeout leaves admission
    closed and existing leases intact; pause can be retried or resume requested.
    """

    def __init__(self, around=None):
        self._around = normalize_activity(around)
        self._condition = threading.Condition()
        self._local = threading.local()
        self._accepting = True
        self._in_flight = 0

    @contextmanager
    def activity(self):
        """Admit one synchronous activity and release its lease in finally.

        Raises:
            ActivityUnavailable: With reason 'paused' for a new closed-gate root.
        """
        with self._condition:
            depth = getattr(self._local, 'depth', 0)
            if not depth and not self._accepting:
                raise ActivityUnavailable('paused')
            if not depth:
                self._in_flight += 1
            self._local.depth = depth + 1
        try:
            # Only the outer root owns an external context. Its lifetime covers
            # every nested call, including cleanup and terminal-state writes.
            with (self._around() if not depth else nullcontext()):
                yield
        finally:
            with self._condition:
                self._local.depth -= 1
                if not self._local.depth:
                    self._in_flight -= 1
                    self._condition.notify_all()

    def __call__(self):
        """Return an activity context manager for an injected context factory."""
        return self.activity()

    def in_activity(self) -> bool:
        """Return whether the current thread owns an activity on this gate."""
        return bool(getattr(self._local, 'depth', 0))

    def pause(self, timeout=5) -> bool:
        """Close admission and wait at most timeout seconds for root leases.

        Args:
            timeout: Finite exact int or float, in seconds, range [0, 30].
        Returns:
            True when no roots remain, False when the deadline expires.
        Raises:
            ValueError: If timeout is invalid, before admission changes.
            ActivityUnavailable: With reason 'reentrant_pause' if this thread
                owns a lease; admission remains unchanged.
        """
        timeout = validate_timeout(timeout)
        if self.in_activity():
            raise ActivityUnavailable('reentrant_pause')
        deadline = time.monotonic() + timeout
        with self._condition:
            self._accepting = False
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def resume(self):
        """Reopen admission; the lifecycle owner enforces stop-related policy."""
        with self._condition:
            self._accepting = True

    def snapshot(self) -> dict:
        """Return {accepting: bool, in_flight: nonnegative root count}."""
        with self._condition:
            return {'accepting': self._accepting, 'in_flight': self._in_flight}
