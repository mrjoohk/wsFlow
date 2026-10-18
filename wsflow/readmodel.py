"""Bounded, read-only source observations shared by I0 query models.

No mkdir, write, hooks, subprocesses or registry migrations are performed here.
This is a cooperative filesystem observation, not an OS sandbox or transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

MAX_SOURCE_BYTES = 4 * 1024 * 1024


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class Reader:
    """Observe explicit files/trees; reject resolved paths outside their scope."""

    def __init__(self, directories=(), files=(), excluded=()):
        # Keep logical boundaries unresolved: a link must not enlarge its own scope.
        self.directories = [Path(os.path.abspath(p)) for p in directories]
        self.files = [Path(os.path.abspath(p)) for p in files]
        self.excluded = [Path(p).resolve() for p in excluded]
        self.sources: list[dict] = []
        self._observed: list[tuple[Path, tuple, dict]] = []
        self._missing: list[tuple[Path, dict]] = []

    def allowed(self, resolved: Path) -> bool:
        return (not any(inside(resolved, p) for p in self.excluded)
                and (resolved in self.files or any(inside(resolved, p) for p in self.directories)))

    def record(self, path: Path, logical_id: str, owner: str, status: str, **extra) -> dict:
        item = dict(logical_id=logical_id, declared_path=str(path), resolved_path=None,
                    owner=owner, kind='file', status=status, sha256=None, **extra)
        self.sources.append(item)
        return item

    @staticmethod
    def _signature(st):
        # On Windows stat/fstat creation-time values can differ for a new file.
        # File identity, byte size and modification time are the content guard.
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns

    def read(self, path: Path, logical_id: str, owner: str,
             boundary: Path | None = None) -> tuple[dict, bytes | None]:
        path = Path(os.path.abspath(path))
        item = self.record(path, logical_id, owner, 'error')
        try:
            resolved = path.resolve()
            item['resolved_path'] = str(resolved)
            if not self.allowed(resolved) or (boundary is not None and not inside(resolved, boundary)):
                item['status'] = 'blocked'
                return item, None
            with resolved.open('rb') as fh:
                before = self._signature(os.fstat(fh.fileno()))
                data = fh.read(MAX_SOURCE_BYTES + 1)
                after = self._signature(os.fstat(fh.fileno()))
            if len(data) > MAX_SOURCE_BYTES:
                item.update(status='error', error='source_too_large')
                return item, None
            if before != after or after != self._signature(path.stat()) or path.resolve() != resolved:
                item.update(status='error', error='source_changed_during_read')
                return item, None
            item.update(status='ok', sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
            self._observed.append((path, after, item))
            return item, data
        except FileNotFoundError:
            item['status'] = 'missing'
            self._missing.append((path, item))
        except (OSError, RuntimeError) as e:
            item.update(status='error', error=type(e).__name__)
        return item, None

    def finish(self) -> bool:
        """Detect ordinary concurrent edits; never claim cross-file atomicity."""
        for path, signature, item in self._observed:
            try:
                if (self._signature(path.stat()) != signature
                        or str(path.resolve()) != item['resolved_path']):
                    item.update(status='error', error='source_changed_during_query')
            except (OSError, RuntimeError):
                item.update(status='error', error='source_changed_during_query')
        for path, item in self._missing:
            try:
                if path.exists():
                    item.update(status='error', error='source_changed_during_query')
            except OSError:
                item.update(status='error', error='source_changed_during_query')
        return not any(s['status'] in ('error', 'blocked') for s in self.sources)
