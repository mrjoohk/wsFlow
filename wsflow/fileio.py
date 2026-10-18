"""Cooperative WF_HOME locking and durable single-file replacement (stdlib only)."""
from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_locks = {}
_guard = threading.Lock()
_local = threading.local()


class LockBusy(ValueError):
    pass


def within(home, path):
    home, path = Path(home).resolve(), Path(path)
    try:
        path.resolve().relative_to(home)
    except (ValueError, OSError, RuntimeError):
        raise ValueError('WF_HOME 밖으로 연결된 쓰기 경로입니다') from None
    return path


@contextmanager
def locked(home, timeout=5):
    home = Path(home).resolve()
    key = str(home)
    with _guard:
        mutex = _locks.setdefault(key, threading.RLock())
    if not mutex.acquire(timeout=timeout):
        raise LockBusy('다른 작업이 업무 파일을 사용 중입니다 — 잠시 후 다시 시도하십시오')
    depths = getattr(_local, 'depths', None)
    if depths is None:
        depths = _local.depths = {}
    if depths.get(key, 0):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
            mutex.release()
        return
    fh = None
    acquired = False
    try:
        path = within(home, home / '.workflow/wsflow.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open('a+b')
        if fh.seek(0, 2) == 0:
            fh.write(b'0'); fh.flush(); os.fsync(fh.fileno())
        deadline = time.monotonic() + timeout
        while True:
            try:
                fh.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy('다른 프로세스가 업무 파일을 사용 중입니다') from None
                time.sleep(.02)
        depths[key] = 1
        yield
    finally:
        depths.pop(key, None)
        try:
            if acquired:
                fh.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            try:
                if fh is not None:
                    fh.close()
            finally:
                mutex.release()


def atomic_write(path, data: bytes, before_replace=None, validate_handle=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, 'wb') as fh:
            if validate_handle:
                validate_handle(fh.fileno())
            fh.write(data); fh.flush(); os.fsync(fh.fileno())
        if before_replace:
            before_replace()
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def append_json(path, record):
    import json
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')
        fh.flush(); os.fsync(fh.fileno())
    return record
