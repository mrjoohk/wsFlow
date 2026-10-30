"""DP01-03: current file bytes and whole deploy roots with stock loopback HTTP."""
import builtins
from contextlib import contextmanager
import filecmp
import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from wsflow import deploy, writer_admission
from wsflow.server import serve
from wsflow.store import Store


def tree(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}


class DeployWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wsflow-deploy-writer-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.owner = self.base/'owner'
        self.template = self.owner/'0.PROJECT_TEMPLATE'
        self.a, self.b = self.owner/'a', self.owner/'b'
        for p in (self.template/'tools', self.template/'.git', self.template/'__pycache__', self.a, self.b/'tools'):
            p.mkdir(parents=True)
        for p, raw in ((self.template/'GLOBAL_RULES.md', b'v2\n'), (self.template/'tools/check.py', b'print(1)\n'),
                       (self.template/'.git/HEAD', b'NO GIT COPY'), (self.template/'__pycache__/cache.pyc', b'NO CACHE COPY'),
                       (self.a/'GLOBAL_RULES.md', b'v1\n'), (self.b/'tools/check.py', b'print(1)\n')):
            p.write_bytes(raw)
        for i, p in enumerate((self.template/'GLOBAL_RULES.md', self.a/'GLOBAL_RULES.md', self.template/'tools/check.py', self.b/'tools/check.py')):
            os.utime(p, ns=(1700000000000000000+i*1000000000,)*2)
        self.projects = [dict(id='a', alias='a', path=str(self.a)), dict(id='b', alias='b', path=str(self.b))]
        self.userdata = self.base/'userdata'
        environment = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.userdata),
            'WSFLOW_GOOGLE_CLIENT_FILE': str(self.base/'no-client.json')})
        environment.start(); self.addCleanup(environment.stop)
        self.domain = writer_admission.default_domain()
        with self.domain.barrier(0):
            pass
        filecmp.clear_cache()
        self.addCleanup(filecmp.clear_cache)
        self.audit = {'provider_calls': 0, 'http': [], 'windows': []}

    def tearDown(self):
        destination = os.environ.get('WSFLOW_TEST_EVIDENCE_DIR')
        if destination:
            output = Path(destination)/'deploy_writer'
            output.mkdir(parents=True, exist_ok=True)
            (output/(self._testMethodName+'.json')).write_text(json.dumps(self.audit, ensure_ascii=False, indent=2), encoding='utf-8')

    def apply(self, **kwargs):
        return deploy.apply(kwargs.get('sources', [str(self.template)]), kwargs.get('projects', self.projects),
                            kwargs.get('dest', ''), kwargs.get('callback', lambda _: self.template))

    def complete(self, epoch):
        view = self.domain.observe()
        self.assertEqual(view['epoch'], epoch+2)
        self.assertIsNone(view['active'])
        with self.domain.barrier(0) as lease:
            self.assertFalse(lease.view()['snapshot_ready'])

    def exact_three(self, value):
        self.assertEqual(value['copied_total'], 3)
        self.assertEqual(value['results'][0]['copied'], ['GLOBAL_RULES.md', 'tools/check.py'])
        self.assertEqual(value['results'][1]['copied'], ['GLOBAL_RULES.md'])
        self.assertEqual(value['results'][1]['skipped_same'], 1)
        for p in (self.a, self.b):
            self.assertEqual((p/'GLOBAL_RULES.md').read_bytes(), b'v2\n')
            self.assertEqual((p/'tools/check.py').read_bytes(), b'print(1)\n')
            self.assertFalse((p/'.git').exists())
            self.assertFalse((p/'__pycache__').exists())
            self.assertEqual(list(p.rglob('*.bak')), [])
        self.assertEqual([r['failed'] for r in value['results']], [[], []])
        self.assertEqual([r['errors'] for r in value['results']], [[], []])

    @contextmanager
    def worker(self, call, releases):
        outcome = {}
        def work():
            try: outcome['value'] = call()
            except BaseException as error: outcome['error'] = error
        thread = threading.Thread(target=work, daemon=True, name='owned-deploy-writer')
        thread.start()
        try:
            yield thread, outcome
        finally:
            for release in releases: release.set()
            thread.join(12)
            self.assertFalse(thread.is_alive(), 'owned deploy worker did not finish')
        if 'error' in outcome: raise outcome['error']

    @staticmethod
    def pause(arrived, release):
        arrived.set()
        if not release.wait(10): raise TimeoutError('owned deploy hold expired')

    def held(self, epoch, thread, outcome):
        self.assertTrue(thread.is_alive())
        self.assertEqual(outcome, {})
        view = self.domain.observe()
        self.assertEqual(view['epoch'], epoch+1)
        self.assertIsNotNone(view['active'])
        with self.assertRaises(writer_admission.WriterUnavailable) as caught:
            with self.domain.barrier(0): pass
        self.assertEqual(caught.exception.reason, 'busy')
        self.audit['windows'].append(view)
        return view['active']

    def tiny(self, left, right):
        source = self.template/'same.txt'; target = self.a/'same.txt'
        source.write_bytes(left); target.write_bytes(right)
        for p in (source, target): os.utime(p, ns=(1700000000000000000,)*2)
        self.assertEqual(source.stat().st_size, target.stat().st_size)
        self.assertEqual(source.stat().st_mtime_ns, target.stat().st_mtime_ns)
        return source, target

    def test_current_bytes_override_same_stat_cache_for_plan_and_apply(self):
        source, target = self.tiny(b'ONE\n', b'ONE\n')
        args = ([str(source)], self.projects[:1], '', lambda _: self.template)
        self.assertEqual(deploy.plan(*args)['total'], {'new': 0, 'changed': 0, 'same': 1})
        source.write_bytes(b'TWO\n'); os.utime(source, ns=(1700000000000000000,)*2)
        self.assertEqual(deploy.plan(*args)['total'], {'new': 0, 'changed': 1, 'same': 0})
        value = deploy.apply(*args)
        self.assertEqual(value['copied_total'], 1)
        self.assertEqual(target.read_bytes(), b'TWO\n')
        self.assertEqual(deploy.plan(*args)['total'], {'new': 0, 'changed': 0, 'same': 1})

    def test_repeated_and_duplicate_projects_copy_once(self):
        source, target = self.tiny(b'NEW\n', b'OLD\n')
        original = deploy.shutil.copy2
        with mock.patch.object(deploy.shutil, 'copy2', wraps=original) as copy:
            value = self.apply(sources=[str(source)], projects=[self.projects[0], self.projects[0]])
            self.assertEqual(value['copied_total'], 1)
            self.assertEqual(value['results'][0]['copied'], ['same.txt'])
            self.assertEqual(value['results'][1]['copied'], [])
            self.assertEqual(value['results'][1]['skipped_same'], 1)
            self.assertEqual(copy.call_count, 1)
            self.assertEqual(self.apply(sources=[str(source)], projects=self.projects[:1])['copied_total'], 0)
            self.assertEqual(copy.call_count, 1)
        self.assertEqual(source.read_bytes(), target.read_bytes())
        self.assertEqual(target.read_bytes(), b'NEW\n')

    def test_raw_apply_refuses_before_callback_or_owner_effects(self):
        entered, release = threading.Event(), threading.Event()
        before = tree(self.owner)
        def barrier():
            with self.domain.barrier(0): self.pause(entered, release)
        callback = mock.Mock(return_value=self.template)
        with self.worker(barrier, [release]):
            self.assertTrue(entered.wait(3))
            with self.assertRaises(writer_admission.WriterUnavailable) as caught:
                self.apply(callback=callback)
            self.assertEqual(caught.exception.reason, 'busy')
            callback.assert_not_called()
            self.assertEqual(tree(self.owner), before)
        epoch = self.domain.observe()['epoch']
        self.exact_three(self.apply())
        self.complete(epoch)

    def test_noop_errors_protection_plan_and_nested_domain(self):
        for kwargs, rows in (({'projects': []}, 0), ({'sources': []}, 2),
                             ({'sources': [str(self.base/'absent')]}, 2)):
            with self.subTest(kwargs=kwargs):
                epoch = self.domain.observe()['epoch']; before = tree(self.owner)
                result = self.apply(**kwargs)
                self.assertEqual(result['copied_total'], 0)
                self.assertEqual(len(result['results']), rows)
                self.assertEqual(tree(self.owner), before)
                if 'absent' in str(kwargs): self.assertTrue(all(len(row['errors']) == 1 for row in result['results']))
                self.complete(epoch)
        before = tree(self.userdata)
        self.assertEqual(deploy.plan([str(self.template)], self.projects, '', lambda _: self.template)['total'], {'new': 2, 'changed': 1, 'same': 1})
        self.assertEqual(tree(self.userdata), before)
        epoch = self.domain.observe()['epoch']
        alternate = self.base/'alternate'
        with self.domain.writing():
            state = self.domain.observe()
            with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(alternate)}):
                self.exact_three(self.apply())
            self.assertEqual(self.domain.observe(), state)
            self.assertFalse(alternate.exists())
        self.complete(epoch)
        (self.a/'.wsflow').mkdir(); (self.a/'.wsflow/project.json').write_bytes(b'{"sentinel":1}')
        before = tree(self.owner)
        for sources, dest, callback in (([str(self.template/'GLOBAL_RULES.md')], '', lambda _: self.template),
                                        ([str(self.template/'tools/check.py')], '../escape', lambda _: None)):
            epoch = self.domain.observe()['epoch']
            value = self.apply(sources=sources, projects=self.projects[:1], dest=dest, callback=callback)
            self.assertEqual(value['copied_total'], 0); self.assertEqual(len(value['results'][0]['errors']), 1)
            self.assertEqual(tree(self.owner), before); self.complete(epoch)

    def test_copy_and_final_response_windows_keep_whole_raw_root(self):
        # Separate calls use fresh destinations without changing the original source's bytes.
        for boundary in ('copy', 'response'):
            with self.subTest(boundary=boundary):
                target = self.owner/boundary; target.mkdir()
                projects = [dict(id=boundary, path=str(target))]
                arrived, release = threading.Event(), threading.Event()
                original_copy = deploy.shutil.copy2
                copied = []
                def copy(*args, **kwargs):
                    value = original_copy(*args, **kwargs); copied.append(Path(args[1]).read_bytes())
                    if boundary == 'copy' and len(copied) == 1: self.pause(arrived, release)
                    return value
                def total(values, *args, **kwargs):
                    value = builtins.sum(values, *args, **kwargs)
                    if boundary == 'response' and inspect.currentframe().f_back.f_code.co_name == 'apply':
                        self.assertEqual(value, 2); self.pause(arrived, release)
                    return value
                epoch = self.domain.observe()['epoch']
                with mock.patch.object(deploy.shutil, 'copy2', side_effect=copy), mock.patch.object(deploy, 'sum', new=total, create=True):
                    with self.worker(lambda: self.apply(projects=projects), [release]) as (thread, outcome):
                        self.assertTrue(arrived.wait(4), outcome)
                        self.assertEqual((target/'GLOBAL_RULES.md').read_bytes(), b'v2\n')
                        self.held(epoch, thread, outcome)
                    self.assertEqual(outcome['value']['copied_total'], 2)
                self.assertEqual(copied, [b'v2\n', b'print(1)\n'])
                self.complete(epoch)

    def test_partial_mkdir_failure_and_callback_exception_release(self):
        blocker = self.a/'tools'; blocker.write_bytes(b'KEEP BLOCKER')
        epoch = self.domain.observe()['epoch']
        value = self.apply(sources=[str(self.template/'tools/check.py')])
        self.assertEqual(value['copied_total'], 0)
        self.assertEqual(len(value['results'][0]['failed']), 1)
        self.assertIn('error', value['results'][0]['failed'][0])
        self.assertEqual(value['results'][1]['skipped_same'], 1)
        self.assertEqual(blocker.read_bytes(), b'KEEP BLOCKER'); self.complete(epoch)
        # A different actual source reaches project two even after project one's mkdir failure.
        (self.template/'tools/extra.py').write_bytes(b'ACTUAL\n')
        epoch = self.domain.observe()['epoch']
        value = self.apply(sources=[str(self.template/'tools/extra.py')])
        self.assertEqual(value['copied_total'], 1)
        self.assertEqual(len(value['results'][0]['failed']), 1)
        self.assertEqual(value['results'][1]['copied'], ['tools/extra.py'])
        self.assertEqual((self.b/'tools/extra.py').read_bytes(), b'ACTUAL\n'); self.complete(epoch)
        calls = []
        def callback(path):
            calls.append(path)
            if len(calls) == 2: raise ValueError('owned_callback_second_failure')
            return self.template
        epoch = self.domain.observe()['epoch']
        with self.assertRaisesRegex(ValueError, 'owned_callback_second_failure'):
            self.apply(sources=[str(self.template/'GLOBAL_RULES.md')], callback=callback)
        self.assertEqual(calls, [str(self.a), str(self.b)])
        self.assertEqual((self.a/'GLOBAL_RULES.md').read_bytes(), b'v2\n')
        self.assertFalse((self.b/'GLOBAL_RULES.md').exists()); self.complete(epoch)

    @contextmanager
    def server(self):
        registry = self.base/'registry.json'
        registry.write_text(json.dumps({'roots': [{'path': self.owner.as_posix(), 'template': '0.PROJECT_TEMPLATE'}], 'projects': self.projects}), encoding='utf-8')
        provider = mock.Mock(side_effect=AssertionError('real provider forbidden'))
        server = serve(registry, port=0, token='deploy-test', google_transport=provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown(); server.server_close(); thread.join(4)
            self.assertFalse(thread.is_alive()); provider.assert_not_called()
            self.assertTrue(all(row['workers_stopped'] for row in server.module_shutdown.values()))

    def request(self, server, path, body):
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,
            data=json.dumps(body).encode(), method='POST', headers={'X-Auth-Token': 'deploy-test', 'Content-Type': 'application/json'})
        try: response = urllib.request.urlopen(request, timeout=9)
        except urllib.error.HTTPError as error: response = error
        with response: code, raw = response.code, response.read()
        value = json.loads(raw)
        self.audit['http'].append({'path': path, 'status': code, 'body': value})
        return code, value

    def test_http_compound_deploy_separates_function_and_transport_completion(self):
        body = {'sources': [str(self.template)], 'ids': ['a', 'b'], 'dest_rel': ''}
        with self.server() as server:
            before_plan = tree(self.owner); epoch = self.domain.observe()['epoch']
            code, plan = self.request(server, '/api/deploy/plan', body)
            self.assertEqual(code, 200); self.assertEqual(plan['total'], {'new': 2, 'changed': 1, 'same': 1})
            self.assertEqual(tree(self.owner), before_plan); self.complete(epoch)
            copy_arrived, copy_release, sum_arrived, sum_release, json_arrived, json_release = [threading.Event() for _ in range(6)]
            original_copy, original_json, original_template = deploy.shutil.copy2, server.RequestHandlerClass._json, Store.template_dir_for
            copies, callbacks = [], []
            def callback(store, path):
                callbacks.append({'path': path, 'view': self.domain.observe()})
                return original_template(store, path)
            def copy(*args, **kwargs):
                value = original_copy(*args, **kwargs); copies.append(Path(args[1]).read_bytes())
                if len(copies) == 1: self.pause(copy_arrived, copy_release)
                return value
            def total(values, *args, **kwargs):
                value = builtins.sum(values, *args, **kwargs)
                if inspect.currentframe().f_back.f_code.co_name == 'apply':
                    self.assertEqual(value, 3); self.pause(sum_arrived, sum_release)
                return value
            def respond(handler, value, status=200):
                if isinstance(value, dict) and 'copied_total' in value:
                    self.audit['before_http_serialization'] = self.domain.observe()
                    self.pause(json_arrived, json_release)
                return original_json(handler, value, status)
            epoch = self.domain.observe()['epoch']
            with mock.patch.object(Store, 'template_dir_for', new=callback), mock.patch.object(deploy.shutil, 'copy2', side_effect=copy), \
                    mock.patch.object(deploy, 'sum', new=total, create=True), mock.patch.object(server.RequestHandlerClass, '_json', new=respond):
                with self.worker(lambda: self.request(server, '/api/deploy/apply', body), [copy_release, sum_release, json_release]) as (thread, outcome):
                    self.assertTrue(copy_arrived.wait(4), outcome)
                    root = self.held(epoch, thread, outcome); self.assertEqual((self.a/'GLOBAL_RULES.md').read_bytes(), b'v2\n')
                    copy_release.set(); self.assertTrue(sum_arrived.wait(4), outcome)
                    self.assertEqual(self.held(epoch, thread, outcome), root)
                    self.assertEqual(len(copies), 3); self.assertEqual(len(callbacks), 2)
                    self.assertTrue(all(item['view']['active'] == root for item in callbacks))
                    sum_release.set(); self.assertTrue(json_arrived.wait(4), outcome)
                    self.complete(epoch); self.assertTrue(thread.is_alive()); self.assertEqual(outcome, {})
                code, value = outcome['value']; self.assertEqual(code, 200); self.exact_three(value)
            self.assertIsNone(self.audit['before_http_serialization']['active'])
            self.assertEqual(self.audit['before_http_serialization']['epoch'], epoch+2)
            self.audit['callback_roots'] = callbacks

    def test_http_busy_and_callback_error_preserve_status_body_and_partial_effect(self):
        body = {'sources': [str(self.template/'GLOBAL_RULES.md')], 'ids': ['a', 'b'], 'dest_rel': ''}
        with self.server() as server:
            before = tree(self.owner)
            with self.domain.barrier(0):
                code, value = self.request(server, '/api/deploy/apply', body)
                self.assertEqual(code, 503); self.assertIn('runtime_reason', value)
                self.assertIn('busy', value['runtime_reason']); self.assertEqual(tree(self.owner), before)
            original = Store.template_dir_for; calls = []
            def callback(store, path):
                calls.append(path)
                if len(calls) == 2: raise ValueError('owned_http_callback_failure')
                return original(store, path)
            epoch = self.domain.observe()['epoch']
            with mock.patch.object(Store, 'template_dir_for', new=callback):
                code, value = self.request(server, '/api/deploy/apply', body)
            self.assertEqual(code, 400); self.assertIn('owned_http_callback_failure', value['error'])
            self.assertEqual(len(calls), 2); self.assertEqual((self.a/'GLOBAL_RULES.md').read_bytes(), b'v2\n')
            self.assertFalse((self.b/'GLOBAL_RULES.md').exists()); self.complete(epoch)


if __name__ == '__main__': unittest.main()
