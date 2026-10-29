"""UF-WA01 observable file, thread and owned-process admission contracts."""
from contextlib import ExitStack
import errno
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from wsflow import writer_admission as wa
from wsflow.runtime_activity import ActivityUnavailable


class WriterAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.control = self.base / 'control'
        self.domain = wa.WriterDomain(self.control)

    def state(self, domain=None):
        return json.loads((domain or self.domain).state_path.read_text(encoding='utf-8'))

    def seed(self, domain=None):
        domain = domain or self.domain
        with domain.barrier() as lease:
            lease.view()
        return self.state(domain)

    def assert_unready(self, view):
        for name in ('snapshot_ready', 'activation_ready', 'coverage_complete'):
            self.assertIs(view[name], False)

    def worker(self, target):
        errors = []
        def run():
            try:
                target()
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def finish(self, thread, errors):
        thread.join(3)
        self.assertFalse(thread.is_alive(), 'owned thread exceeded watchdog')
        self.assertEqual(errors, [])

    def actor(self, mode, effect):
        return subprocess.Popen(
            [getattr(sys, '_base_executable', sys.executable),
             str(Path(__file__).with_name('writer_actor.py')), mode,
             str(self.control), str(effect)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def reap(self, process):
        if process.poll() is None:
            process.kill()
        return process.communicate(timeout=4)

    def run_actor(self, mode, effect):
        process = self.actor(mode, effect)
        try:
            output, error = process.communicate(timeout=4)
            self.assertEqual(error, '')
            return process.returncode, output
        finally:
            self.reap(process)

    def test_unavailable_is_activity_unavailable(self):
        error = wa.WriterUnavailable('busy')
        self.assertIsInstance(error, ActivityUnavailable)
        self.assertEqual(error.reason, 'busy')

    def test_observe_missing_is_read_only_and_unready(self):
        view = self.domain.observe()
        self.assertEqual(view['status'], 'missing')
        self.assertFalse(self.control.exists())
        self.assert_unready(view)

    def test_read_only_activity_never_initializes_or_changes_state(self):
        with self.domain.activity():
            self.assertTrue(self.domain.in_activity())
            self.assertFalse(self.domain.state_path.exists())
        self.assertFalse(self.domain.in_activity())
        self.seed()
        before = self.domain.state_path.read_bytes()
        with self.domain.activity():
            with wa.WriterDomain(self.control / '.').activity():
                self.assertEqual(self.domain.state_path.read_bytes(), before)
        self.assertEqual(self.domain.state_path.read_bytes(), before)

    def test_begin_end_have_one_uuid_and_two_epochs(self):
        with self.domain.writing():
            begun = self.state()
            self.assertEqual(begun['epoch'], 1)
            self.assertEqual(begun['active']['pid'], os.getpid())
            self.assertEqual(len(begun['active']['root_id']), 32)
        ended = self.state()
        self.assertEqual(ended['domain_id'], begun['domain_id'])
        self.assertEqual(ended['epoch'], 2)
        self.assertIsNone(ended['active'])
        self.assertEqual(ended['interrupted_count'], 0)
        with self.domain.barrier() as lease:
            self.assertEqual(lease.view()['epoch'], 2)
            self.assert_unready(lease.view())

    def test_nested_write_is_retained_until_outer_activity_returns(self):
        with self.domain.activity():
            with self.domain.writing():
                begun = self.state()
                with wa.WriterDomain(self.control).writing():
                    self.assertEqual(self.state(), begun)
            self.assertEqual(self.state(), begun)
            with self.assertRaises(wa.WriterUnavailable):
                with self.domain.barrier(0):
                    self.fail('barrier entered admitted activity')
        self.assertEqual(self.state()['epoch'], 2)

    def test_business_exception_records_end_without_business_success_claim(self):
        failure = ValueError('producer failed')
        with self.assertRaises(ValueError) as error:
            with self.domain.writing():
                raise failure
        self.assertIs(error.exception, failure)
        self.assertEqual(self.state()['epoch'], 2)
        self.assertIsNone(self.state()['active'])
        with self.domain.barrier() as lease:
            self.assert_unready(lease.view())

    def test_business_oserror_is_preserved_and_lock_startup_oserror_is_structured(self):
        failure = OSError('business failure')
        with self.assertRaises(OSError) as error:
            with self.domain.writing():
                raise failure
        self.assertIs(error.exception, failure)
        with patch.object(wa.fileio, 'locked', side_effect=OSError('lock startup failed')):
            with self.assertRaises(wa.WriterUnavailable) as error:
                with self.domain.activity(0):
                    self.fail('lock failure admitted')
        self.assertEqual(error.exception.reason, 'control_unavailable')
        self.assertFalse(self.domain.in_activity())

    def test_business_fileio_lockbusy_identity_is_preserved_for_activity_and_writing(self):
        for factory in (self.domain.activity, self.domain.writing):
            failure = wa.fileio.LockBusy('business lock failed')
            with self.subTest(factory=factory.__name__):
                with self.assertRaises(wa.fileio.LockBusy) as error:
                    with factory():
                        raise failure
                self.assertIs(error.exception, failure)
                self.assertFalse(self.domain.in_activity())

    def test_reentrance_refuses_before_effect_and_barrier_view_expires(self):
        with self.domain.barrier() as lease:
            before = self.domain.state_path.read_bytes()
            for factory in (self.domain.activity, self.domain.writing, self.domain.barrier):
                with self.subTest(factory=factory.__name__):
                    with self.assertRaises(wa.WriterUnavailable) as error:
                        with factory(0):
                            self.fail('reentrant effect reached')
                    self.assertEqual(error.exception.reason,
                                     'reentrant_barrier' if factory.__name__ == 'barrier'
                                     else 'reentrant_activity')
            self.assertEqual(self.domain.state_path.read_bytes(), before)
        with self.assertRaises(wa.WriterUnavailable):
            lease.view()
        with self.domain.barrier() as fresh:
            with self.assertRaises(wa.WriterUnavailable):
                lease.view()
            self.assert_unready(fresh.view())

    def test_barrier_view_cannot_be_used_by_another_thread(self):
        outcomes = []
        with self.domain.barrier() as lease:
            def foreign():
                self.assertFalse(self.domain.in_activity())
                try:
                    lease.view()
                except wa.WriterUnavailable as error:
                    outcomes.append(error.reason)
            self.finish(*self.worker(foreign))
            self.assertEqual(len(outcomes), 1)
            self.assert_unready(lease.view())

    def test_explicit_other_domain_nesting_is_refused_without_second_effect(self):
        other = wa.WriterDomain(self.base / 'other')
        with self.domain.activity():
            for factory in (other.activity, other.writing, other.barrier):
                with self.assertRaises(wa.WriterUnavailable) as error:
                    with factory(0):
                        self.fail('second domain admitted')
                self.assertEqual(error.exception.reason, 'different_domain')
        self.assertFalse(other.control_dir.exists())

    def test_other_domain_is_independent_in_another_thread(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hold():
            with self.domain.barrier():
                entered.set()
                self.assertTrue(release.wait(2))
        thread, errors = self.worker(hold)
        try:
            self.assertTrue(entered.wait(2))
            other = wa.WriterDomain(self.base / 'other')
            with other.writing(0):
                self.assertEqual(self.state(other)['epoch'], 1)
            self.assertEqual(self.state(other)['epoch'], 2)
        finally:
            release.set()
            self.finish(thread, errors)

    def test_threads_serialize_complete_promoted_root_and_bound_wait(self):
        entered, release, attempted, second_entered = (threading.Event() for _ in range(4))
        self.addCleanup(release.set)
        order = []
        def first():
            with self.domain.activity():
                with self.domain.writing():
                    order.append('first_effect')
                entered.set()
                self.assertTrue(release.wait(2))
                order.append('first_terminal')
        first_worker = self.worker(first)
        self.assertTrue(entered.wait(2))
        def second():
            attempted.set()
            with wa.WriterDomain(self.control).writing(1):
                second_entered.set()
                order.append('second_effect')
        second_worker = self.worker(second)
        try:
            self.assertTrue(attempted.wait(2))
            began = time.monotonic()
            with self.assertRaises(wa.WriterUnavailable) as error:
                with self.domain.barrier(.03):
                    self.fail('running root lost lease')
            self.assertEqual(error.exception.reason, 'busy')
            self.assertLess(time.monotonic() - began, 1)
            self.assertFalse(second_entered.is_set())
        finally:
            release.set()
            self.finish(*first_worker)
            self.finish(*second_worker)
        self.assertEqual(order, ['first_effect', 'first_terminal', 'second_effect'])
        self.assertEqual(self.state()['epoch'], 4)

    def test_process_before_effect_refusal_then_success_after_barrier(self):
        effect = self.base / 'effect'
        with self.domain.barrier():
            before = self.domain.state_path.read_bytes()
            code, output = self.run_actor('write', effect)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output), {'status': 'refused', 'reason': 'busy'})
            self.assertFalse(effect.exists())
            self.assertEqual(self.domain.state_path.read_bytes(), before)
        code, output = self.run_actor('write', effect)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {'status': 'complete'})
        self.assertEqual(effect.read_text(encoding='utf-8'), 'write')
        self.assertEqual(self.state()['epoch'], 2)

    def test_abrupt_process_exit_preserves_unknown_and_later_writer_cannot_clear_it(self):
        code, output = self.run_actor('crash', self.base / 'effect')
        self.assertEqual((code, output), (91, ''))
        interrupted = self.state()
        self.assertIsNotNone(interrupted['active'])
        self.assertEqual(interrupted['epoch'], 1)
        with self.assertRaises(wa.WriterUnavailable):
            with self.domain.barrier(.2):
                self.fail('crash marker cleared')
        with self.domain.writing(.2):
            resumed = self.state()
            self.assertEqual(resumed['interrupted_count'], 1)
            self.assertEqual(resumed['last_interrupted'], interrupted['active']['root_id'])
            self.assertNotEqual(resumed['active']['root_id'], interrupted['active']['root_id'])
        with self.domain.writing(.2):
            self.assertEqual(self.state()['interrupted_count'], 1)
        self.assertEqual(self.state()['epoch'], 5)
        with self.assertRaises(wa.WriterUnavailable):
            with self.domain.barrier(0):
                self.fail('ordinary root resolved unknown outcome')

    def test_owned_child_kill_releases_os_lock_but_not_active_marker(self):
        effect = self.base / 'ready'
        process = self.actor('hold', effect)
        self.addCleanup(lambda: self.reap(process))
        deadline = time.monotonic() + 3
        while not effect.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(effect.exists(), 'owned child did not enter before watchdog')
        with self.assertRaises(wa.WriterUnavailable) as error:
            with self.domain.barrier(.02):
                self.fail('child writer lease was bypassed')
        self.assertEqual(error.exception.reason, 'busy')
        self.reap(process)
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(wa.WriterUnavailable) as error:
            with self.domain.barrier(.2):
                self.fail('killed root was accepted')
        self.assertEqual(error.exception.reason, 'unresolved')
        with self.domain.writing(.2):
            self.assertEqual(self.state()['interrupted_count'], 1)

    def test_invalid_timeout_refuses_before_filesystem_effect(self):
        for value in (True, False, -1, 30.001, math.nan, math.inf, '1', None):
            for name in ('activity', 'writing', 'barrier'):
                with self.subTest(value=value, name=name):
                    with self.assertRaises(ValueError):
                        with getattr(self.domain, name)(value):
                            self.fail('invalid timeout admitted')
        self.assertFalse(self.control.exists())

    def test_valid_timeout_endpoints_and_exact_numeric_types(self):
        class Number(int):
            pass
        with self.assertRaises(ValueError):
            with self.domain.activity(Number(1)):
                self.fail('numeric subclass admitted')
        with self.domain.activity(0):
            pass
        with self.domain.writing(30.0):
            pass

    def test_malformed_and_oversized_state_refuse_and_observe_without_repair(self):
        self.control.mkdir()
        for data in (b'null', b'[]', b'{', b'\xff', b' ' * 16385):
            with self.subTest(data=data[:10]):
                self.domain.state_path.write_bytes(data)
                observation = self.domain.observe()
                self.assertEqual(observation['status'], 'invalid')
                self.assert_unready(observation)
                for factory in (self.domain.writing, self.domain.barrier):
                    with self.assertRaises(wa.WriterUnavailable):
                        with factory(0):
                            self.fail('invalid metadata admitted effect')
                self.assertEqual(self.domain.state_path.read_bytes(), data)

    def test_strict_state_schema_rejects_boolean_bounds_uuid_shape_and_extra_keys(self):
        valid = self.seed()
        changes = [
            {'schema_version': True}, {'schema_version': 2}, {'epoch': True},
            {'epoch': -1}, {'epoch': 2**63}, {'domain_id': 'f' * 31},
            {'domain_id': 'F' * 32}, {'interrupted_count': False},
            {'interrupted_count': -1}, {'last_interrupted': 'bad'},
            {'active': {'root_id': '0' * 32, 'pid': True}},
            {'active': {'root_id': '0' * 32, 'pid': 0}},
            {'active': {'root_id': '0' * 32, 'pid': 1, 'extra': 2}},
            {'active': {'root_id': 'broken', 'pid': 1}}, {'extra': 0},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.domain.state_path.write_text(json.dumps(dict(valid, **change)), encoding='utf-8')
                with self.assertRaises(wa.WriterUnavailable):
                    with self.domain.writing(0):
                        self.fail('invalid schema admitted')
                self.assertEqual(self.domain.observe()['status'], 'invalid')

    def test_duplicate_json_fields_and_epoch_exhaustion_are_refused(self):
        valid = self.seed()
        raw = json.dumps(valid)
        duplicate = raw[:-1] + ', "epoch": 0}'
        self.domain.state_path.write_text(duplicate, encoding='utf-8')
        with self.assertRaises(wa.WriterUnavailable):
            with self.domain.barrier(0):
                self.fail('duplicate accepted')
        for epoch in (2**63 - 2, 2**63 - 1):
            valid['epoch'] = epoch
            self.domain.state_path.write_text(json.dumps(valid), encoding='utf-8')
            with self.assertRaises(wa.WriterUnavailable):
                with self.domain.writing(0):
                    self.fail('epoch overflow admitted')
            self.assertEqual(self.state()['epoch'], epoch)
        valid['epoch'] = 2**63 - 3
        self.domain.state_path.write_text(json.dumps(valid), encoding='utf-8')
        with self.domain.writing(0):
            self.assertEqual(self.state()['epoch'], 2**63 - 2)
        self.assertEqual(self.state()['epoch'], 2**63 - 1)

    def test_interrupted_counter_overflow_is_refused_before_effect(self):
        state = self.seed()
        state.update(interrupted_count=2**63 - 1, last_interrupted='2' * 32,
                     active={'root_id': '3' * 32, 'pid': 1})
        self.domain.state_path.write_text(json.dumps(state), encoding='utf-8')
        before = self.domain.state_path.read_bytes()
        with self.assertRaises(wa.WriterUnavailable) as error:
            with self.domain.writing(0):
                self.fail('interrupted counter overflow admitted effect')
        self.assertEqual(error.exception.reason, 'state_exhausted')
        self.assertEqual(self.domain.state_path.read_bytes(), before)

    def test_end_persistence_failure_retains_unknown_marker_and_releases_lease(self):
        real_write = wa.fileio.atomic_write
        def fail_end(path, data, *args, **kwargs):
            if json.loads(data)['active'] is None:
                raise OSError('injected end persistence failure')
            return real_write(path, data, *args, **kwargs)
        with patch.object(wa.fileio, 'atomic_write', fail_end):
            with self.assertRaises(wa.WriterUnavailable):
                with self.domain.writing():
                    (self.base / 'effect').write_bytes(b'done')
        self.assertFalse(self.domain.in_activity())
        self.assertIsNotNone(self.state()['active'])
        with self.assertRaises(wa.WriterUnavailable):
            with self.domain.barrier(0):
                self.fail('failed end was accepted')
        with self.domain.writing(0):
            self.assertEqual(self.state()['interrupted_count'], 1)
        self.assertIsNone(self.state()['active'])
        with self.assertRaises(wa.WriterUnavailable):
            with self.domain.barrier(0):
                self.fail('later success erased end failure')

    def test_begin_persistence_failure_prevents_business_effect_and_does_not_leak_root(self):
        with patch.object(wa.fileio, 'atomic_write', side_effect=OSError('begin failed')):
            with self.assertRaises(wa.WriterUnavailable):
                with self.domain.writing():
                    self.fail('business effect ran without marker')
        self.assertFalse(self.domain.in_activity())
        with self.domain.writing(0):
            self.assertEqual(self.state()['epoch'], 1)

    def test_end_failure_after_replace_persists_unknown_even_when_active_was_cleared(self):
        real_write = wa.fileio.atomic_write
        failed = []
        def fail_after_replace(path, data, *args, **kwargs):
            result = real_write(path, data, *args, **kwargs)
            if json.loads(data)['active'] is None and not failed:
                failed.append(True)
                raise OSError('injected failure after replacement')
            return result
        with patch.object(wa.fileio, 'atomic_write', fail_after_replace):
            with self.assertRaises(wa.WriterUnavailable):
                with self.domain.writing():
                    pass
        self.assertEqual(self.state()['interrupted_count'], 1)
        self.assertIsNone(self.state()['active'])
        with self.assertRaises(wa.WriterUnavailable):
            with self.domain.barrier(0):
                self.fail('uncertain replacement authorized barrier')
        code, output = self.run_actor('write', self.base / 'later-effect')
        self.assertEqual((code, json.loads(output)), (0, {'status': 'complete'}))
        self.assertEqual(self.state()['interrupted_count'], 1)

    def test_completion_verifies_root_cookie_domain_and_epoch(self):
        for field, replacement in (('epoch', 7), ('domain_id', '1' * 32), ('active', None)):
            domain = wa.WriterDomain(self.base / field)
            with self.subTest(field=field):
                with self.assertRaises(wa.WriterUnavailable):
                    with domain.writing():
                        state = self.state(domain)
                        state[field] = replacement
                        domain.state_path.write_text(json.dumps(state), encoding='utf-8')
                with self.assertRaises(wa.WriterUnavailable):
                    with domain.barrier(0):
                        self.fail('mismatched end authorized a barrier')

    def test_end_ack_loss_and_compensation_failure_keep_local_unknown_latch(self):
        real_write=wa.fileio.atomic_write;faults=[]
        def fail_end_and_compensation(path,raw,*args,**kwargs):
            state=json.loads(raw)
            if state['active'] is not None:
                return real_write(path,raw,*args,**kwargs)
            if state['interrupted_count']:
                faults.append('compensation')
                raise OSError('compensation unavailable')
            real_write(path,raw,*args,**kwargs)
            faults.append('end_ack')
            raise OSError('end acknowledgement lost')
        with patch.object(wa.fileio,'atomic_write',fail_end_and_compensation):
            with self.assertRaises(wa.WriterUnavailable):
                with self.domain.writing():
                    (self.base/'effect').write_bytes(b'complete')
        self.assertEqual(faults,['end_ack','compensation'])
        self.assertEqual((self.base/'effect').read_bytes(),b'complete')
        self.assertIsNone(self.state()['active'])
        self.assertEqual(self.state()['interrupted_count'],0)
        peer=wa.WriterDomain(self.control)
        self.assertTrue(peer.observe()['local_unknown'])
        with self.assertRaises(wa.WriterUnavailable) as error:
            with peer.barrier(0):self.fail('unknown completion granted a barrier')
        self.assertEqual(error.exception.reason,'unresolved')
        with peer.writing(0):pass
        self.assertEqual(self.state()['interrupted_count'],1)
        with self.assertRaises(wa.WriterUnavailable):
            with peer.barrier(0):self.fail('later write erased the unknown latch')

    def test_observation_remains_bounded_and_available_during_writer(self):
        with self.domain.writing():
            view = self.domain.observe()
            self.assertEqual(view['status'], 'valid')
            self.assertEqual(view['epoch'], 1)
            self.assertIsNotNone(view['active'])
            self.assert_unready(view)

    def test_barrier_view_is_an_independent_acquisition_time_observation(self):
        with self.domain.barrier() as lease:
            view = lease.view()
            view['epoch'] = 700
            view['snapshot_ready'] = True
            view['active'] = {'root_id': '9' * 32, 'pid': 1}
            fresh = lease.view()
            self.assertEqual(fresh['epoch'], 0)
            self.assertIsNone(fresh['active'])
            self.assert_unready(fresh)

    def test_read_only_activity_with_corrupt_metadata_cannot_promote_to_effect(self):
        self.control.mkdir()
        self.domain.state_path.write_bytes(b'corrupt')
        with self.domain.activity():
            self.assertEqual(self.domain.observe()['status'], 'invalid')
            with self.assertRaises(wa.WriterUnavailable):
                with self.domain.writing():
                    self.fail('corrupt metadata admitted effect')
        self.assertEqual(self.domain.state_path.read_bytes(), b'corrupt')

    def test_decorators_bind_stores_and_free_function_reuses_current_domain(self):
        domain = self.domain
        calls = []
        @wa.writer_function
        def free():
            calls.append(domain.in_activity())
        class Store:
            def __init__(self, owner):
                self._writer_domain = owner
            @wa.writer_method
            def write(self):
                free()
                return 'ok'
        store = Store(domain)
        with patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.base / 'unused')}):
            self.assertEqual(store.write(), 'ok')
            self.assertEqual(calls, [True])
            self.assertFalse((self.base / 'unused').exists())
        self.assertEqual(self.state()['epoch'], 2)
        with domain.barrier():
            with self.assertRaises(wa.WriterUnavailable):
                free()
        with domain.activity():
            with self.assertRaises(wa.WriterUnavailable):
                Store(wa.WriterDomain(self.base / 'other')).write()
        self.assertFalse((self.base / 'other').exists())

    def test_free_function_default_domain_is_resolved_at_call(self):
        @wa.writer_function
        def free(value):
            return value + 1
        for name in ('first', 'second'):
            with patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.base / name)}):
                default = wa.default_domain()
                self.assertEqual(default.control_dir, (self.base / name / 'writer-admission').resolve())
                self.assertEqual(free(4), 5)
                self.assertEqual(self.state(default)['epoch'], 2)

    def test_default_domain_appdata_and_home_fallbacks_match_userdata_policy(self):
        with patch.dict(os.environ, {'APPDATA': str(self.base)}, clear=True):
            self.assertEqual(wa.default_domain().control_dir,
                             (self.base / 'wsFlow' / 'writer-admission').resolve())
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, 'home', return_value=self.base):
                self.assertEqual(wa.default_domain().control_dir,
                                 (self.base / '.config/wsFlow/writer-admission').resolve())

    def test_state_link_policy_refuses_without_reading_or_modifying_target(self):
        self.control.mkdir()
        target = self.base / 'elsewhere'
        target.write_bytes(b'untouched')
        try:
            self.domain.state_path.symlink_to(target)
        except (OSError, NotImplementedError):
            # Native Windows symlink privilege is unavailable on some machines.
            # This fallback verifies only the policy boundary, not a real link.
            import wsflow.path_policy as policy
            original = policy.linked
            def linked(path):
                return Path(path) == self.domain.state_path or original(path)
            with patch.object(policy, 'linked', linked):
                with self.assertRaises(wa.WriterUnavailable):
                    with self.domain.writing(0):
                        self.fail('linked state policy admitted')
                self.assertEqual(self.domain.observe()['status'], 'invalid')
        else:
            with self.assertRaises(wa.WriterUnavailable):
                with self.domain.writing(0):
                    self.fail('linked state admitted')
            self.assertEqual(self.domain.observe()['status'], 'invalid')
        self.assertEqual(target.read_bytes(), b'untouched')

    def test_metadata_windows_sharing_retry_keeps_encoded_bytes_and_one_effect(self):
        real_write = wa.fileio.atomic_write
        for winerror in (5, 32, 33):
            domain = wa.WriterDomain(self.base / str(winerror))
            self.seed(domain)
            attempts, effects = [], []
            failure = PermissionError(errno.EACCES, 'transient metadata sharing denial')
            failure.winerror = winerror
            def transient(path, data):
                attempts.append(data)
                if len(attempts) < 3:
                    raise failure
                return real_write(path, data)
            with self.subTest(winerror=winerror):
                with patch.object(wa, 'os', SimpleNamespace(name='nt', getpid=os.getpid)), \
                     patch.object(wa.fileio, 'atomic_write', transient), \
                     patch.object(wa.time, 'sleep') as sleep:
                    with domain.writing():
                        effects.append('once')
                self.assertEqual(effects, ['once'])
                self.assertEqual(len(attempts), 4)  # Three begin attempts, one end.
                self.assertTrue(all(data is attempts[0] for data in attempts[:3]))
                self.assertEqual(sleep.call_args_list, [call(.01), call(.02)])
                self.assertEqual(self.state(domain)['epoch'], 2)
                self.assertIsNone(self.state(domain)['active'])
                with domain.barrier() as lease:
                    self.assertEqual(lease.view()['interrupted_count'], 0)

    def test_metadata_permanent_windows_denial_is_bounded_before_effect(self):
        self.seed()
        before = self.domain.state_path.read_bytes()
        effects = []
        failure = PermissionError(errno.EACCES, 'persistent metadata sharing denial')
        failure.winerror = 5
        with patch.object(wa, 'os', SimpleNamespace(name='nt', getpid=os.getpid)), \
             patch.object(wa.fileio, 'atomic_write', side_effect=failure) as write, \
             patch.object(wa.time, 'sleep') as sleep:
            with self.assertRaises(wa.WriterUnavailable) as caught:
                with self.domain.writing():
                    effects.append('unexpected')
        self.assertEqual(caught.exception.reason, 'persistence_failed')
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(write.call_count, 6)
        encoded = write.call_args_list[0].args[1]
        self.assertTrue(all(row.args[1] is encoded for row in write.call_args_list))
        self.assertEqual(sleep.call_args_list,
                         [call(.01), call(.02), call(.04), call(.08), call(.16)])
        self.assertAlmostEqual(sum(row.args[0] for row in sleep.call_args_list), .31)
        self.assertEqual(effects, [])
        self.assertEqual(self.domain.state_path.read_bytes(), before)
        self.assertFalse(self.domain.in_activity())
        with self.domain.barrier() as lease:
            self.assertEqual(lease.view()['interrupted_count'], 0)

    def test_metadata_other_error_codes_and_types_do_not_retry(self):
        self.seed()
        before = self.domain.state_path.read_bytes()
        other_code = PermissionError(errno.EACCES, 'other Windows error')
        other_code.winerror = 87
        other_type = OSError(errno.EIO, 'generic I/O failure')
        other_type.winerror = 5
        failures = (other_code, PermissionError(errno.EACCES, 'no Windows code'),
                    other_type, ValueError('invalid write'), RuntimeError('write failed'))
        for failure in failures:
            with self.subTest(error=repr(failure)):
                with patch.object(wa, 'os', SimpleNamespace(name='nt', getpid=os.getpid)), \
                     patch.object(wa.fileio, 'atomic_write', side_effect=failure) as write, \
                     patch.object(wa.time, 'sleep') as sleep:
                    with self.assertRaises(wa.WriterUnavailable) as caught:
                        with self.domain.writing():
                            self.fail('unretryable metadata failure admitted an effect')
                self.assertIs(caught.exception.__cause__, failure)
                self.assertEqual(write.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(self.domain.state_path.read_bytes(), before)

    def test_metadata_non_windows_permission_errors_do_not_retry(self):
        self.seed()
        before = self.domain.state_path.read_bytes()
        for winerror in (5, 32, 33):
            failure = PermissionError(errno.EACCES, 'non-Windows denial')
            failure.winerror = winerror
            with self.subTest(winerror=winerror):
                with patch.object(wa, 'os', SimpleNamespace(name='posix', getpid=os.getpid)), \
                     patch.object(wa.fileio, 'atomic_write', side_effect=failure) as write, \
                     patch.object(wa.time, 'sleep') as sleep:
                    with self.assertRaises(wa.WriterUnavailable) as caught:
                        with self.domain.writing():
                            self.fail('non-Windows denial admitted an effect')
                self.assertIs(caught.exception.__cause__, failure)
                self.assertEqual(write.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(self.domain.state_path.read_bytes(), before)

    @unittest.skipUnless(os.name == 'nt', 'Windows native sharing-handle semantics')
    def test_metadata_native_begin_sharing_retry_registers_project_once(self):
        from test_registry_atomic import deny_delete_reader
        from wsflow import store
        self.seed()
        project = self.base / 'real_project'
        project.mkdir()
        registry = store.Store(self.base / 'projects.json')
        registry._writer_domain = self.domain
        real_write = wa.fileio.atomic_write
        observed, attempts = [], []
        with deny_delete_reader(self.domain.state_path) as release:
            def observe_native(path, data):
                attempts.append(data)
                try:
                    return real_write(path, data)
                except PermissionError as error:
                    observed.append(error.winerror)
                    release()
                    raise
            with patch.object(wa.fileio, 'atomic_write', observe_native), \
                 patch.object(registry, '_reload', wraps=registry._reload) as reload, \
                 patch.object(registry, '_save', wraps=registry._save) as save:
                registered = registry.add_project(str(project))
        self.assertEqual(reload.call_count, 1)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(len(observed), 1)
        self.assertIn(observed[0], (5, 32, 33))
        self.assertEqual(len(attempts), 3)
        self.assertIs(attempts[0], attempts[1])
        self.assertEqual(json.loads(registry.file.read_text(encoding='utf-8'))['projects'],
                         [registered])
        with self.domain.barrier() as lease:
            self.assertEqual(lease.view()['epoch'], 2)
            self.assertIsNone(lease.view()['active'])
            self.assertEqual(lease.view()['interrupted_count'], 0)
        self.assertFalse(self.domain.observe()['local_unknown'])

    @unittest.skipUnless(os.name == 'nt', 'Windows native sharing-handle semantics')
    def test_metadata_native_end_retry_preserves_duplicate_business_error(self):
        from test_registry_atomic import deny_delete_reader
        from wsflow import store
        self.seed()
        project = self.base / 'real_project'
        project.mkdir()
        registry = store.Store(self.base / 'projects.json')
        registry._writer_domain = self.domain
        registered = registry.add_project(str(project))
        before = registry.file.read_bytes()
        real_write = wa.fileio.atomic_write
        attempts, observed, releases, business_errors = [], [], [], []
        producer_code = store.Store.add_project.__wrapped__.__code__
        previous_trace = sys.gettrace()
        def trace(frame, event, arg):
            if event == 'exception' and frame.f_code is producer_code and isinstance(arg[1], ValueError):
                business_errors.append(arg[1])
            return trace
        with ExitStack() as handles:
            def observe_native(path, data):
                attempts.append(data)
                if json.loads(data)['active'] is None and not releases:
                    releases.append(handles.enter_context(deny_delete_reader(self.domain.state_path)))
                try:
                    return real_write(path, data)
                except PermissionError as error:
                    observed.append(error.winerror)
                    releases[0]()
                    raise
            with patch.object(wa.fileio, 'atomic_write', observe_native), \
                 patch.object(registry, '_reload', wraps=registry._reload) as reload, \
                 patch.object(registry, '_save', wraps=registry._save) as save:
                try:
                    sys.settrace(trace)
                    with self.assertRaises(ValueError) as caught:
                        registry.add_project(str(project))
                finally:
                    sys.settrace(previous_trace)
        self.assertEqual(len(business_errors), 1)
        self.assertIs(caught.exception, business_errors[0])
        self.assertIn(registered['id'], str(caught.exception))
        self.assertEqual(reload.call_count, 1)
        save.assert_not_called()
        self.assertEqual(len(observed), 1)
        self.assertIn(observed[0], (5, 32, 33))
        self.assertEqual(len(attempts), 3)
        self.assertIs(attempts[1], attempts[2])
        self.assertEqual(registry.file.read_bytes(), before)
        with self.domain.barrier() as lease:
            self.assertEqual(lease.view()['epoch'], 4)
            self.assertIsNone(lease.view()['active'])
            self.assertEqual(lease.view()['interrupted_count'], 0)
        self.assertFalse(self.domain.observe()['local_unknown'])


if __name__ == '__main__':
    unittest.main()
