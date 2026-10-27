"""Reader lexical compatibility and live scope/content guard regressions."""
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
import random
import subprocess
import tempfile
import unittest
from unittest import mock

from wsflow import readmodel


def old_inside(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class ScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.file = self.home / 'note.md'
        self.file.write_bytes(b'first')

    def test_inside_matches_original_windows_lexical_boundaries(self):
        rng = random.Random(20261027)
        roots = [r'C:\work', r'c:\WORK', r'D:\work', r'\\server\share\work']
        suffixes = ['', '/x', '/x/y', '/..', '/../sibling', '/한글', 'space', 'flow']
        for _ in range(1000):
            p = PureWindowsPath(rng.choice(roots) + rng.choice(suffixes))
            parent = PureWindowsPath(rng.choice(roots) + rng.choice(suffixes))
            with self.subTest(path=str(p), parent=str(parent)):
                self.assertEqual(readmodel.inside(p, parent), old_inside(p, parent))

    def test_directory_membership_matches_original_with_prefix_siblings(self):
        cases = [self.home, self.file, self.root / 'home2' / 'note.md', self.root,
                 self.home / 'nested' / '..' / 'note.md']
        reader = readmodel.Reader([self.home], excluded=[self.home / 'nested'])
        for p in cases:
            expected = (not any(old_inside(p, x) for x in reader.excluded)
                        and any(old_inside(p, x) for x in reader.directories))
            self.assertEqual(reader.allowed(p), expected, str(p))

    def test_directories_are_live_on_append_replace_and_clear(self):
        reader = readmodel.Reader()
        self.assertFalse(reader.allowed(self.file))
        reader.directories.append(str(self.home))
        self.assertTrue(reader.allowed(self.file))
        reader.directories = [self.root / 'elsewhere']
        self.assertFalse(reader.allowed(self.file))
        reader.directories = [self.home]
        self.assertTrue(reader.allowed(self.file))
        reader.directories.clear()
        self.assertFalse(reader.allowed(self.file))

    def test_explicit_files_are_live_and_do_not_grant_child_access(self):
        reader = readmodel.Reader()
        reader.files.append(self.file)
        self.assertTrue(reader.allowed(self.file))
        self.assertFalse(reader.allowed(self.file / 'child'))
        reader.files = [self.home / 'other.md']
        self.assertFalse(reader.allowed(self.file))
        reader.files.append(str(self.file))  # Preserve existing Path-list membership semantics.
        self.assertFalse(reader.allowed(self.file))
        reader.files = [self.file]
        reader.files.clear()
        self.assertFalse(reader.allowed(self.file))

    def test_exclusions_are_live_and_override_explicit_files(self):
        reader = readmodel.Reader([self.home], [self.file])
        self.assertTrue(reader.allowed(self.file))
        reader.excluded.append(str(self.file))
        self.assertFalse(reader.allowed(self.file))
        reader.excluded = [self.home]
        self.assertFalse(reader.allowed(self.file))
        reader.excluded.clear()
        self.assertTrue(reader.allowed(self.file))

    def test_disallowed_and_boundary_paths_are_not_opened(self):
        reader = readmodel.Reader([self.home])
        outside = self.root / 'outside.md'
        outside.write_bytes(b'private')
        with mock.patch.object(Path, 'open', side_effect=AssertionError('must not open')):
            item, data = reader.read(outside, 'outside', 'test')
            self.assertEqual(item['status'], 'blocked')
            self.assertIsNone(data)
            item, data = reader.read(self.file, 'boundary', 'test', boundary=self.home / 'other')
            self.assertEqual(item['status'], 'blocked')
            self.assertIsNone(data)
        self.assertFalse(reader.finish())

    def test_exclusion_change_is_seen_by_the_next_read(self):
        reader = readmodel.Reader([self.home])
        item, data = reader.read(self.file, 'first', 'test')
        self.assertEqual((item['status'], data), ('ok', b'first'))
        reader.excluded.append(self.home)
        with mock.patch.object(Path, 'open', side_effect=AssertionError('must not open')):
            item, data = reader.read(self.file, 'second', 'test')
        self.assertEqual((item['status'], data), ('blocked', None))

    def test_success_hash_and_finish_content_change(self):
        reader = readmodel.Reader([self.home])
        item, data = reader.read(self.file, 'id', 'owner')
        self.assertEqual(data, b'first')
        self.assertEqual(item['sha256'], hashlib.sha256(data).hexdigest())
        self.assertTrue(reader.finish())
        self.file.write_bytes(b'new contents')
        self.assertFalse(reader.finish())
        self.assertEqual(item['error'], 'source_changed_during_query')

    def test_replacement_with_same_size_and_mtime_is_detected(self):
        reader = readmodel.Reader([self.home])
        item, _ = reader.read(self.file, 'id', 'owner')
        before = self.file.stat()
        replacement = self.home / 'replacement'
        replacement.write_bytes(b'other')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.file)
        self.assertFalse(reader.finish())
        self.assertEqual(item['status'], 'error')

    def test_missing_created_before_finish_is_detected(self):
        reader = readmodel.Reader([self.home])
        missing = self.home / 'missing.md'
        item, data = reader.read(missing, 'missing', 'owner')
        self.assertEqual((item['status'], data), ('missing', None))
        missing.write_bytes(b'new')
        self.assertFalse(reader.finish())
        self.assertEqual(item['error'], 'source_changed_during_query')

    def test_read_size_limit_is_enforced(self):
        with mock.patch.object(readmodel, 'MAX_SOURCE_BYTES', 4):
            reader = readmodel.Reader([self.home])
            item, data = reader.read(self.file, 'large', 'owner')
        self.assertIsNone(data)
        self.assertEqual(item['error'], 'source_too_large')
        self.assertFalse(reader.finish())

    def test_changed_fstat_during_read_discards_bytes(self):
        original = os.fstat
        calls = []
        def changing(fd):
            st = original(fd)
            calls.append(st)
            if len(calls) == 1:
                self.file.write_bytes(b'changed during read')
            return st
        with mock.patch.object(readmodel.os, 'fstat', side_effect=changing):
            reader = readmodel.Reader([self.home])
            item, data = reader.read(self.file, 'changing', 'owner')
        self.assertIsNone(data)
        self.assertEqual(item['error'], 'source_changed_during_read')
        self.assertFalse(reader.finish())

    def test_path_replaced_after_close_discards_observed_bytes(self):
        replacement = self.home / 'replacement'
        replacement.write_bytes(b'other')
        real_open = Path.open
        @contextmanager
        def swapped(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as stream:
                yield stream
            os.replace(replacement, path)
        with mock.patch.object(Path, 'open', swapped):
            reader = readmodel.Reader([self.home])
            item, data = reader.read(self.file, 'swap', 'owner')
        self.assertIsNone(data)
        self.assertEqual(item['error'], 'source_changed_during_read')

    def junction(self, path, target):
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(path), str(target)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        else:
            path.symlink_to(target, target_is_directory=True)

    def linked_pair(self):
        twin = self.root / 'twin'
        twin.mkdir()
        os.link(self.file, twin / self.file.name)
        self.assertTrue(os.path.samefile(self.file, twin / self.file.name))
        link = self.root / 'logical'
        self.junction(link, self.home)
        return link, twin

    def retarget(self, link, target):
        self.assertEqual(link.parent, self.root)
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()  # Remove the junction itself, leaving its target intact.
        self.junction(link, target)

    def test_logical_directory_root_is_not_expanded_to_link_target(self):
        link = self.root / 'declared'
        self.junction(link, self.home)
        reader = readmodel.Reader([link])
        with mock.patch.object(Path, 'open', side_effect=AssertionError('must not open')):
            item, data = reader.read(link / self.file.name, 'linked-root', 'owner')
        self.assertEqual((item['status'], data), ('blocked', None))

    def test_read_rechecks_resolved_path_even_when_inode_is_unchanged(self):
        link, twin = self.linked_pair()
        real_open = Path.open
        @contextmanager
        def retargeted(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as stream:
                yield stream
            self.retarget(link, twin)
        with mock.patch.object(Path, 'open', retargeted):
            reader = readmodel.Reader([self.root])
            item, data = reader.read(link / self.file.name, 'retarget', 'owner')
        self.assertIsNone(data)
        self.assertEqual(item['error'], 'source_changed_during_read')

    def test_finish_rechecks_resolved_path_even_when_inode_is_unchanged(self):
        link, twin = self.linked_pair()
        reader = readmodel.Reader([self.root])
        item, data = reader.read(link / self.file.name, 'retarget', 'owner')
        self.assertEqual(data, b'first')
        self.assertTrue(reader.finish())
        self.retarget(link, twin)
        self.assertFalse(reader.finish())
        self.assertEqual(item['error'], 'source_changed_during_query')


if __name__ == '__main__':
    unittest.main()
