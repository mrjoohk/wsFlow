"""Registry replacement tolerates only bounded, transient Windows sharing errors."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import errno
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from wsflow import store


@contextmanager
def deny_delete_reader(path):
    """A real Windows reader which permits reads/writes but denies replacement."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    active = [handle]
    def release():
        if active:
            if not close(active.pop()):
                raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield release
    finally:
        release()


class RegistryAtomicTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='wsflow_registry_atomic_')
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / 'projects.json'
        self.original = b'{"roots": [], "projects": []}\r\n'
        self.file.write_bytes(self.original)
        self.registry = store.Store(self.file)
        self.registry.data['projects'].append({'id': 'new', 'path': 'synthetic'})

    def denied(self, winerror):
        error = PermissionError(errno.EACCES, 'synthetic sharing violation')
        error.winerror = winerror
        return error

    def test_transient_windows_sharing_errors_retry_the_same_replacement(self):
        replace = Path.replace
        for winerror in (5, 32, 33):
            with self.subTest(winerror=winerror):
                self.file.write_bytes(self.original)
                attempts = []
                def transient(path, target):
                    attempts.append((path, target))
                    if len(attempts) < 3:
                        raise self.denied(winerror)
                    return replace(path, target)
                with mock.patch.object(store, 'os', SimpleNamespace(name='nt')), \
                     mock.patch.object(Path, 'replace', transient), \
                     mock.patch.object(store.time, 'sleep', create=True) as sleep:
                    self.registry._save()
                self.assertEqual(len(attempts), 3)
                self.assertTrue(all(row == attempts[0] for row in attempts))
                self.assertEqual(sleep.call_args_list, [mock.call(.01), mock.call(.02)])
                self.assertEqual(json.loads(self.file.read_text()), self.registry.data)

    def test_permanent_windows_denial_stops_and_preserves_original_bytes(self):
        error = self.denied(5)
        with mock.patch.object(store, 'os', SimpleNamespace(name='nt')), \
             mock.patch.object(Path, 'replace', side_effect=error) as replace, \
             mock.patch.object(store.time, 'sleep', create=True) as sleep:
            with self.assertRaises(PermissionError) as caught:
                self.registry._save()
        self.assertIs(caught.exception, error)
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(sleep.call_args_list, [mock.call(.01), mock.call(.02), mock.call(.04), mock.call(.08), mock.call(.16)])
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_other_windows_errors_are_not_retried(self):
        for error in (self.denied(87), PermissionError(errno.EACCES, 'ordinary denial'), OSError(errno.EIO, 'I/O failure')):
            with self.subTest(error=repr(error)):
                with mock.patch.object(store, 'os', SimpleNamespace(name='nt')), \
                     mock.patch.object(Path, 'replace', side_effect=error) as replace, \
                     mock.patch.object(store.time, 'sleep', create=True) as sleep:
                    with self.assertRaises(type(error)) as caught:
                        self.registry._save()
                self.assertIs(caught.exception, error)
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(self.file.read_bytes(), self.original)

    def test_non_windows_denial_is_not_retried(self):
        error = self.denied(5)
        with mock.patch.object(store, 'os', SimpleNamespace(name='posix')), \
             mock.patch.object(Path, 'replace', side_effect=error) as replace, \
             mock.patch.object(store.time, 'sleep', create=True) as sleep:
            with self.assertRaises(PermissionError) as caught:
                self.registry._save()
        self.assertIs(caught.exception, error)
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.file.read_bytes(), self.original)

    @unittest.skipUnless(os.name == 'nt', 'Windows native sharing-handle semantics')
    def test_native_reader_release_allows_the_pending_registry_replacement(self):
        replace = Path.replace
        observed = []
        with deny_delete_reader(self.file) as release:
            def first_denied(path, target):
                try:
                    return replace(path, target)
                except PermissionError as error:
                    observed.append(error.winerror)
                    # Deterministically release after the actual OS denial.
                    # Production retry must still run and complete replacement.
                    release()
                    raise
            with mock.patch.object(Path, 'replace', first_denied):
                self.registry._save()
        self.assertEqual(len(observed), 1)
        self.assertIn(observed[0], (5, 32, 33))
        self.assertEqual(json.loads(self.file.read_text()), self.registry.data)


if __name__ == '__main__':
    unittest.main()
