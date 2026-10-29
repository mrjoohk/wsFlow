"""Event-driven acceptance checks for the host activity lease and pause boundary."""
from contextlib import contextmanager, nullcontext
import math
import threading
import unittest

from wsflow.runtime_activity import (ActivityGate, ActivityUnavailable, admitted,
                                     normalize_activity, validate_timeout)


class ActivityGateTests(unittest.TestCase):
    def start_worker(self, target):
        errors = []
        def run():
            try:
                target()
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, errors

    def finish_worker(self, worker, errors):
        worker.join(2)
        self.assertFalse(worker.is_alive(), 'worker exceeded bounded join')
        self.assertEqual(errors, [], repr(errors))

    def test_admitted_root_finishes_nested_work_after_pause_but_new_root_is_denied(self):
        gate = ActivityGate()
        entered, continue_nested, completed = (threading.Event() for _ in range(3))
        self.addCleanup(continue_nested.set)
        observations = []
        def work():
            with gate.activity():
                entered.set()
                self.assertTrue(continue_nested.wait(2), 'nested-work release timed out')
                with gate():
                    observations.append((gate.in_activity(), gate.snapshot()))
                    with self.assertRaisesRegex(ValueError, 'inner failure'):
                        with gate.activity():
                            raise ValueError('inner failure')
                self.assertTrue(gate.in_activity())
                completed.set()
            self.assertFalse(gate.in_activity())
        worker, errors = self.start_worker(work)
        self.assertTrue(entered.wait(2), 'root entry timed out')
        self.assertFalse(gate.in_activity(), 'leases must not transfer to another thread')
        self.assertFalse(gate.pause(0))
        self.assertEqual(gate.snapshot(), {'accepting': False, 'in_flight': 1})
        with self.assertRaises(ActivityUnavailable) as denied:
            with gate.activity():
                self.fail('paused gate admitted new root')
        self.assertEqual(denied.exception.reason, 'paused')
        continue_nested.set()
        self.finish_worker(worker, errors)
        self.assertTrue(completed.is_set())
        self.assertEqual(observations, [(True, {'accepting': False, 'in_flight': 1})])
        self.assertTrue(gate.pause(0))
        self.assertEqual(gate.snapshot(), {'accepting': False, 'in_flight': 0})

    def test_waiting_pause_wakes_after_last_root_finishes(self):
        gate = ActivityGate()
        entered, release, pause_started, pause_done = (threading.Event() for _ in range(4))
        self.addCleanup(release.set)
        results = []
        def work():
            with gate():
                entered.set()
                self.assertTrue(release.wait(2), 'root release timed out')
        worker, errors = self.start_worker(work)
        self.assertTrue(entered.wait(2), 'root entry timed out')
        self.assertFalse(gate.pause(0))
        def drain():
            pause_started.set()
            results.append(gate.pause(1))
            pause_done.set()
        pauser, pause_errors = self.start_worker(drain)
        self.assertTrue(pause_started.wait(2), 'pause start timed out')
        self.assertFalse(pause_done.is_set(), 'drain completed with a root still running')
        release.set()
        self.finish_worker(worker, errors)
        self.finish_worker(pauser, pause_errors)
        self.assertEqual(results, [True])
        self.assertEqual(gate.snapshot(), {'accepting': False, 'in_flight': 0})

    def test_timeout_keeps_all_existing_roots_counted_and_retry_drains(self):
        gate = ActivityGate()
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        for signal in release:
            self.addCleanup(signal.set)
        def work(index):
            with gate():
                entered[index].set()
                self.assertTrue(release[index].wait(2), 'root release timed out')
        workers = [self.start_worker(lambda index=index: work(index)) for index in range(2)]
        for signal in entered:
            self.assertTrue(signal.wait(2), 'root entry timed out')
        self.assertFalse(gate.pause(.01))
        self.assertEqual(gate.snapshot(), {'accepting': False, 'in_flight': 2})
        release[0].set()
        self.finish_worker(*workers[0])
        self.assertFalse(gate.pause(0))
        self.assertEqual(gate.snapshot(), {'accepting': False, 'in_flight': 1})
        release[1].set()
        self.finish_worker(*workers[1])
        self.assertTrue(gate.pause(.01))
        self.assertEqual(gate.snapshot(), {'accepting': False, 'in_flight': 0})

    def test_root_exception_releases_lease_and_preserves_original_error(self):
        gate = ActivityGate()
        failure = ValueError('producer failed')
        with self.assertRaises(ValueError) as observed:
            with gate():
                self.assertTrue(gate.in_activity())
                raise failure
        self.assertIs(observed.exception, failure)
        self.assertFalse(gate.in_activity())
        self.assertEqual(gate.snapshot(), {'accepting': True, 'in_flight': 0})
        self.assertTrue(gate.pause(0))

    def test_reentrant_pause_is_rejected_before_admission_changes(self):
        gate = ActivityGate()
        with gate():
            before = gate.snapshot()
            with self.assertRaises(ActivityUnavailable) as denied:
                gate.pause(0)
            self.assertEqual(denied.exception.reason, 'reentrant_pause')
            self.assertEqual(gate.snapshot(), before)
            with gate():
                self.assertEqual(gate.snapshot()['in_flight'], 1)
        self.assertTrue(gate.pause(0))

    def test_closed_gate_can_resume_and_other_host_gate_stays_independent(self):
        first, second = ActivityGate(), ActivityGate()
        self.assertTrue(first.pause())
        with second():
            self.assertEqual(second.snapshot(), {'accepting': True, 'in_flight': 1})
            with self.assertRaises(ActivityUnavailable):
                with first():
                    self.fail('first host gate admitted work')
        first.resume()
        with first():
            self.assertEqual(first.snapshot(), {'accepting': True, 'in_flight': 1})
        self.assertEqual(second.snapshot(), {'accepting': True, 'in_flight': 0})

    def test_invalid_pause_timeout_does_not_change_gate(self):
        gate = ActivityGate()
        for value in (True, False, None, '1', -1, 30.0001, math.inf, -math.inf, math.nan, 10**1000):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    gate.pause(value)
                self.assertEqual(gate.snapshot(), {'accepting': True, 'in_flight': 0})


class ActivityAdapterTests(unittest.TestCase):
    def test_timeout_accepts_exact_numeric_types_and_bounds(self):
        for value in (0, 0.0, .25, 5, 30, 30.0):
            with self.subTest(value=value):
                result = validate_timeout(value)
                self.assertIs(type(result), float)
                self.assertEqual(result, float(value))
        class IntSubclass(int):
            pass
        class FloatSubclass(float):
            pass
        for value in (True, False, IntSubclass(1), FloatSubclass(1), None, '1', [], -1,
                      30.0001, math.inf, math.nan, 10**1000):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, 'stop_timeout'):
                    validate_timeout(value, 'stop_timeout')

    def test_normalize_activity_defaults_to_nullcontext_and_preserves_factory(self):
        self.assertIs(normalize_activity(None), nullcontext)
        with normalize_activity(None)():
            pass
        gate = ActivityGate()
        self.assertIs(normalize_activity(gate), gate)
        for invalid in (False, 1, 'activity', [], nullcontext()):
            with self.subTest(value_type=type(invalid).__name__):
                with self.assertRaises(ValueError):
                    normalize_activity(invalid)

    def test_admitted_opens_factory_for_each_call_and_preserves_result_and_metadata(self):
        changes = []
        @contextmanager
        def activity():
            changes.append('enter')
            try:
                yield
            finally:
                changes.append('exit')
        class Producer:
            _activity = staticmethod(activity)
            @admitted
            def produce(self, value, *, suffix):
                """Return the original business result."""
                changes.append('effect')
                return value + suffix
        producer = Producer()
        self.assertEqual(producer.produce('a', suffix='b'), 'ab')
        self.assertEqual(producer.produce('c', suffix='d'), 'cd')
        self.assertEqual(changes, ['enter', 'effect', 'exit', 'enter', 'effect', 'exit'])
        self.assertEqual(producer.produce.__name__, 'produce')
        self.assertEqual(producer.produce.__doc__, 'Return the original business result.')
        self.assertTrue(callable(producer.produce.__wrapped__))

    def test_admitted_rejects_effect_before_entry_and_releases_after_exception(self):
        gate = ActivityGate()
        effects = []
        failure = ValueError('producer failed')
        class Producer:
            _activity = gate
            @admitted
            def produce(self):
                effects.append('effect')
                raise failure
        producer = Producer()
        self.assertTrue(gate.pause(0))
        with self.assertRaises(ActivityUnavailable):
            producer.produce()
        self.assertEqual(effects, [])
        gate.resume()
        with self.assertRaises(ValueError) as observed:
            producer.produce()
        self.assertIs(observed.exception, failure)
        self.assertEqual(effects, ['effect'])
        self.assertEqual(gate.snapshot(), {'accepting': True, 'in_flight': 0})


if __name__ == '__main__':
    unittest.main()
