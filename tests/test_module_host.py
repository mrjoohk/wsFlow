"""Local package lifecycle contracts, with real files and restart activation."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wsflow.module_host import ModuleError, ModuleHost


class ModuleHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wsflow-module-host-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / 'workspace'; self.workspace.mkdir()
        self.registry = self.workspace / 'projects.json'
        self.registry.write_text(json.dumps({'roots': [{'path': str(self.workspace)}], 'projects': []}), encoding='utf-8')
        patch = mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.base / 'userdata')})
        patch.start(); self.addCleanup(patch.stop)
        self.hosts = []
        self.host = self.open_host()

    def open_host(self):
        host = ModuleHost(self.registry)
        self.hosts.append(host); self.addCleanup(host.close)
        return host

    def package(self, module_id='erp-sample', version='1.0.0', **manifest_changes):
        folder = self.base / 'packages' / (module_id + '-' + version)
        folder.mkdir(parents=True)
        marker = self.base / (module_id + '-activation.json')
        stopped = self.base / (module_id + '-stopped.txt')
        manifest = {
            'schema_version': 1, 'id': module_id, 'name': 'ERP 모형',
            'version': version, 'host_api': 1, 'data_schema_version': 1,
            'capabilities': ['erp-view'], 'requires': [],
            'config_schema': {'type': 'object', 'properties': {'company': {'type': 'string'}}, 'additionalProperties': False},
            'entrypoint': 'backend.py:create_module',
            'ui': {'entry': 'view.js', 'label': 'ERP 모형'},
            **manifest_changes,
        }
        (folder / 'module.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        code = '''import json
from pathlib import Path

def create_module(context):
    marker = Path(MARKER)
    previous = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {'calls': 0}
    marker.write_text(json.dumps({'calls': previous['calls'] + 1, 'settings': context['settings']}), encoding='utf-8')
    class Module:
        def handle(self, method, segments, query, body):
            return {'module_id': context['module_id'], 'settings': context['settings'], 'method': method,
                    'segments': segments, 'query': query, 'body': body, 'data_dir': str(context['data_dir'])}
        def stop(self):
            Path(STOPPED).write_text('stopped', encoding='utf-8')
    return Module()
'''.replace('MARKER', repr(str(marker))).replace('STOPPED', repr(str(stopped)))
        (folder / 'backend.py').write_text(code, encoding='utf-8')
        (folder / 'view.js').write_text('export function mount(container, api) { container.textContent = "ERP 모형"; }\n', encoding='utf-8')
        return folder, marker, stopped

    def plan(self, action, module_id=None, **extra):
        body = {'action': action, 'expected_revision': self.host.snapshot()['revision'], **extra}
        if module_id is not None:
            body['module_id'] = module_id
        return self.host.plan(body)

    def apply(self, plan):
        return self.host.apply(plan['plan_id'])

    def install(self, folder, module_id='erp-sample'):
        return self.apply(self.plan('install', module_id, source_path=str(folder), trusted=True))

    def assert_error(self, callback, statuses=(400, 409)):
        with self.assertRaises(ModuleError) as raised:
            callback()
        self.assertIn(raised.exception.status, statuses)
        return raised.exception

    def test_default_snapshot_is_readonly_and_both_builtins_are_active(self):
        before = self.registry.read_bytes()
        snapshot = self.host.snapshot()
        self.assertEqual(snapshot['revision'], 0)
        self.assertTrue(self.host.builtin_active('work-calendar'))
        self.assertTrue(self.host.builtin_active('workspace-operations'))
        self.assertFalse((self.host.root / 'state.json').exists())
        self.assertEqual(self.registry.read_bytes(), before)

    def test_configuration_is_deferred_idempotent_and_other_builtin_survives(self):
        plan = self.plan('uninstall', 'work-calendar')
        before = copy.deepcopy(plan)
        first = self.apply(plan)
        self.assertEqual(plan, before)
        revision = self.host.snapshot()['revision']
        self.assertEqual(revision, 1)
        second = self.apply(plan)
        self.assertEqual(second, first)
        self.assertEqual(self.host.snapshot()['revision'], revision)
        self.assertTrue(self.host.builtin_active('work-calendar'))
        restarted = self.open_host()
        self.assertFalse(restarted.builtin_active('work-calendar'))
        self.assertTrue(restarted.builtin_active('workspace-operations'))

    def test_two_preview_plans_cannot_overwrite_newer_configuration(self):
        work = self.plan('disable', 'work-calendar')
        workspace = self.plan('disable', 'workspace-operations')
        self.apply(work)
        self.assert_error(lambda: self.apply(workspace), statuses=(409,))
        restarted = self.open_host()
        self.assertFalse(restarted.builtin_active('work-calendar'))
        self.assertTrue(restarted.builtin_active('workspace-operations'))

    def test_uninstall_preserves_registry_user_records_and_source_package(self):
        folder, marker, _ = self.package()
        self.install(folder)
        record = self.workspace / '.workflow/state.json'; record.parent.mkdir()
        record.write_bytes(b'{"watermark":"keep","pending":"uncertain"}\r\n')
        sentinel = self.base / 'userdata/google/mirror.sqlite3'; sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b'user mirror/outbox bytes')
        before = {p: p.read_bytes() for p in (record, sentinel, self.registry, folder / 'backend.py')}
        self.apply(self.plan('uninstall', 'erp-sample'))
        self.assertFalse(marker.exists())
        self.open_host()
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_inspection_and_apply_do_not_execute_factory_until_restart(self):
        folder, marker, stopped = self.package()
        plan = self.plan('install', 'erp-sample', source_path=str(folder), trusted=True)
        self.assertFalse(marker.exists())
        self.apply(plan)
        self.assertFalse(marker.exists())
        restarted = self.open_host()
        response = restarted.extension_request('GET', 'erp-sample', ['health'], {}, {})
        self.assertEqual(response['module_id'], 'erp-sample')
        self.assertEqual(response['segments'], ['health'])
        self.assertEqual(json.loads(marker.read_text())['calls'], 1)
        restarted.close()
        self.assertTrue(stopped.is_file())

    def test_external_configuration_is_validated_and_activated_on_restart(self):
        folder, marker, _ = self.package()
        self.install(folder)
        self.apply(self.plan('configure', 'erp-sample', settings={'company': 'A'}))
        self.assertFalse(marker.exists())
        restarted = self.open_host()
        response = restarted.extension_request('POST', 'erp-sample', ['echo'], {'x': ['1']}, {'safe': True})
        self.assertEqual(response['settings'], {'company': 'A'})
        self.assertEqual(response['body'], {'safe': True})
        self.assertEqual(response['query'], {'x': ['1']})
        self.assertTrue(Path(response['data_dir']).is_relative_to(self.host.root))
        self.assert_error(lambda: self.plan('configure', 'erp-sample', settings={'company': 3}))
        self.assert_error(lambda: self.plan('configure', 'erp-sample', settings={'unexpected': 'value'}))

    def test_external_install_requires_explicit_trust_and_absolute_source(self):
        folder, marker, _ = self.package()
        self.assert_error(lambda: self.plan('install', 'erp-sample', source_path=str(folder)))
        self.assert_error(lambda: self.plan('install', 'erp-sample', source_path='relative/package', trusted=True))
        self.assertFalse(marker.exists())
        self.assertEqual(self.host.snapshot()['revision'], 0)

    def test_manifest_reserves_builtin_ids_and_rejects_unsupported_contracts(self):
        cases = [
            {'id': 'work-calendar'}, {'host_api': 99}, {'entrypoint': '../backend.py:create_module'},
            {'ui': {'entry': '../outside.js', 'label': 'bad'}}, {'capabilities': ['same', 'same']},
            {'requires': ['missing-module']}, {'requires': ['erp-self']},
        ]
        for index, change in enumerate(cases):
            with self.subTest(change=change):
                module_id = 'erp-self' if change.get('requires') == ['erp-self'] else 'erp-invalid-' + str(index)
                folder, marker, _ = self.package(module_id, **change)
                self.assert_error(lambda: self.plan('install', change.get('id', module_id), source_path=str(folder), trusted=True))
                self.assertFalse(marker.exists())

    def test_package_changed_after_preview_is_rejected_without_configuration_commit(self):
        folder, marker, _ = self.package()
        plan = self.plan('install', 'erp-sample', source_path=str(folder), trusted=True)
        (folder / 'backend.py').write_text('raise RuntimeError("mutated")\n', encoding='utf-8')
        self.assert_error(lambda: self.apply(plan), statuses=(409,))
        self.assertEqual(self.host.snapshot()['revision'], 0)
        self.assertFalse(marker.exists())

    def test_schema_changing_update_is_rejected_instead_of_silent_data_adoption(self):
        folder, _, _ = self.package()
        self.install(folder)
        updated, _, _ = self.package(version='2.0.0', data_schema_version=2)
        self.assert_error(lambda: self.plan('update', 'erp-sample', source_path=str(updated), trusted=True))
        self.assertEqual(self.host.snapshot()['revision'], 1)

    def test_builtin_can_be_reinstalled_without_a_source_package(self):
        self.apply(self.plan('uninstall', 'work-calendar'))
        self.apply(self.plan('install', 'work-calendar'))
        restarted = self.open_host()
        self.assertTrue(restarted.builtin_active('work-calendar'))

    def test_restore_reserves_a_new_revision_and_does_not_change_running_configuration(self):
        self.apply(self.plan('disable', 'work-calendar'))
        self.apply(self.plan('disable', 'workspace-operations'))
        before = self.host.snapshot()['revision']
        restored = self.plan('restore', target_revision=0)
        self.apply(restored)
        self.assertGreater(self.host.snapshot()['revision'], before)
        restarted = self.open_host()
        self.assertTrue(restarted.builtin_active('work-calendar'))
        self.assertTrue(restarted.builtin_active('workspace-operations'))

    def test_corrupt_state_is_preserved_and_not_automatically_reinitialized(self):
        self.host.root.mkdir(parents=True, exist_ok=True)
        state_file = self.host.root / 'state.json'
        original = b'{broken state bytes\r\n'
        state_file.write_bytes(original)
        restarted = self.open_host()
        self.assertFalse(restarted.builtin_active('work-calendar'))
        self.assertFalse(restarted.builtin_active('workspace-operations'))
        self.assertTrue(restarted.snapshot()['configuration_error'])
        self.assertEqual(state_file.read_bytes(), original)

    def test_missing_or_disabled_extension_cannot_dispatch(self):
        self.assert_error(lambda: self.host.extension_request('GET', 'not-installed', [], {}, {}), statuses=(404, 409, 503))
        folder, _, _ = self.package()
        self.install(folder)
        self.apply(self.plan('disable', 'erp-sample'))
        restarted = self.open_host()
        self.assert_error(lambda: restarted.extension_request('GET', 'erp-sample', [], {}, {}), statuses=(404, 409, 503))

    def test_returned_plan_mutation_cannot_change_the_saved_plan(self):
        plan = self.plan('uninstall', 'work-calendar')
        plan['modules']['workspace-operations']['enabled'] = False
        self.apply(plan)
        restarted = self.open_host()
        self.assertFalse(restarted.builtin_active('work-calendar'))
        self.assertTrue(restarted.builtin_active('workspace-operations'))

    def test_package_update_changes_version_only_after_restart(self):
        folder, marker, _ = self.package()
        self.install(folder)
        active = self.open_host()
        self.assertEqual(json.loads(marker.read_text())['calls'], 1)
        updated, _, _ = self.package(version='2.0.0')
        plan = self.plan('update', 'erp-sample', source_path=str(updated), trusted=True)
        self.apply(plan)
        self.assertEqual(json.loads(marker.read_text())['calls'], 1)
        row = next(row for row in active.snapshot()['modules'] if row['id'] == 'erp-sample')
        self.assertEqual((row['active_version'], row['version']), ('1.0.0', '2.0.0'))
        restarted = self.open_host()
        row = next(row for row in restarted.snapshot()['modules'] if row['id'] == 'erp-sample')
        self.assertEqual((row['active_version'], row['version']), ('2.0.0', '2.0.0'))
        self.assertEqual(json.loads(marker.read_text())['calls'], 2)

    def test_other_implementation_replaces_capability_without_adopting_old_data(self):
        folder, marker, _ = self.package('work-alternative', capabilities=['work-items', 'calendar'])
        self.assert_error(lambda: self.plan('replace', 'work-calendar', source_path=str(folder), trusted=True, data_adoption=True), statuses=(409,))
        plan = self.plan('replace', 'work-calendar', source_path=str(folder), trusted=True, data_adoption=False)
        self.apply(plan)
        self.assertFalse(marker.exists())
        self.assertTrue(self.host.builtin_active('work-calendar'))
        restarted = self.open_host()
        self.assertFalse(restarted.builtin_active('work-calendar'))
        self.assertTrue(restarted.active('work-alternative'))
        self.assertTrue(restarted.builtin_active('workspace-operations'))
        result = restarted.extension_request('GET', 'work-alternative', [], {}, {})
        self.assertTrue(Path(result['data_dir']).is_relative_to(self.host.root / 'data' / 'work-alternative'))

    def test_replacement_requires_the_old_business_capabilities(self):
        folder, marker, _ = self.package()
        self.assert_error(lambda: self.plan('replace', 'work-calendar', source_path=str(folder), trusted=True, data_adoption=False), statuses=(409,))
        self.assertFalse(marker.exists())
        self.assertEqual(self.host.snapshot()['revision'], 0)

    def test_active_dependency_cannot_be_disabled_or_uninstalled(self):
        folder, _, _ = self.package(requires=['workspace-operations'])
        self.install(folder)
        self.assert_error(lambda: self.plan('disable', 'workspace-operations'))
        self.assert_error(lambda: self.plan('uninstall', 'workspace-operations'))
        self.assertEqual(self.host.snapshot()['revision'], 1)

    def test_assets_are_bounded_to_active_verified_public_package_files(self):
        folder, _, _ = self.package()
        self.install(folder)
        self.assert_error(lambda: self.host.asset('erp-sample', 'view.js'), statuses=(409,))
        restarted = self.open_host()
        content, mime = restarted.asset('erp-sample', 'view.js')
        self.assertEqual(content, (folder / 'view.js').read_bytes())
        self.assertIn('javascript', mime)
        self.assert_error(lambda: restarted.asset('erp-sample', '../outside.js'))
        self.assert_error(lambda: restarted.asset('erp-sample', 'backend.py'), statuses=(404,))
        # A modified installed asset must be rejected rather than served as verified.
        row = next(row for row in restarted.snapshot()['modules'] if row['id'] == 'erp-sample')
        installed = restarted.root / row['package_path'] / 'view.js'
        installed.write_text('export const modified = true;\n', encoding='utf-8')
        self.assert_error(lambda: restarted.asset('erp-sample', 'view.js'), statuses=(409,))

    def test_schema_declared_secret_fields_and_inline_credentials_are_rejected(self):
        folder, marker, _ = self.package(config_schema={'type': 'object', 'properties': {'credential': {'type': 'string', 'secret': True}}, 'additionalProperties': False})
        self.assert_error(lambda: self.plan('install', 'erp-sample', source_path=str(folder), trusted=True))
        self.assertFalse(marker.exists())
        safe, _, _ = self.package('erp-credentials', config_schema={'type': 'object', 'properties': {'api_token': {'type': 'string'}}, 'additionalProperties': False})
        self.install(safe, 'erp-credentials')
        self.assert_error(lambda: self.plan('configure', 'erp-credentials', settings={'api_token': 'must-not-be-saved'}))
        self.assertNotIn('must-not-be-saved', (self.host.root / 'state.json').read_text(encoding='utf-8'))

    def test_failed_factory_keeps_management_and_other_builtins_available(self):
        folder, _, _ = self.package()
        (folder / 'backend.py').write_text('raise RuntimeError("factory import failed")\n', encoding='utf-8')
        self.install(folder)
        restarted = self.open_host()
        self.assertFalse(restarted.active('erp-sample'))
        self.assertTrue(restarted.builtin_active('work-calendar'))
        self.assertTrue(restarted.builtin_active('workspace-operations'))
        row = next(row for row in restarted.snapshot()['modules'] if row['id'] == 'erp-sample')
        self.assertIn('factory import failed', row['error'])

    def test_symlinked_package_entries_are_not_followed(self):
        folder, marker, _ = self.package()
        outside = self.base / 'outside.js'; outside.write_text('outside private content', encoding='utf-8')
        try:
            (folder / 'link.js').symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('symlink unavailable: ' + str(exc))
        self.assert_error(lambda: self.plan('install', 'erp-sample', source_path=str(folder), trusted=True))
        self.assertFalse(marker.exists())

    def test_legacy_readonly_defaults_survive_unsafe_storage_without_configuration(self):
        with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.workspace / 'userdata')}):
            invalid = self.open_host()
            self.assertIsNone(invalid.snapshot()['configuration_error'])
            self.assertTrue(invalid.snapshot()['storage_warning'])
            self.assertTrue(invalid.builtin_active('work-calendar'))
            self.assertTrue(invalid.builtin_active('workspace-operations'))
            self.assert_error(lambda: invalid.plan({'action': 'disable', 'module_id': 'work-calendar', 'expected_revision': 0}), statuses=(409,))
            self.assert_error(lambda: invalid.apply('MP-' + '0' * 32), statuses=(409,))
            self.assertFalse((invalid.root / 'state.json').exists())

    def test_existing_module_state_in_unsafe_storage_is_preserved_and_fails_closed(self):
        self.apply(self.plan('disable', 'work-calendar'))
        state = (self.host.root / 'state.json').read_bytes()
        with mock.patch.dict(os.environ, {'WSFLOW_DATA_DIR': str(self.workspace / 'userdata')}):
            unconfigured = self.open_host()
            unconfigured.root.mkdir(parents=True, exist_ok=True)
            state_file = unconfigured.root / 'state.json'; state_file.write_bytes(state)
            invalid = self.open_host()
            self.assertTrue(invalid.snapshot()['configuration_error'])
            self.assertFalse(invalid.builtin_active('work-calendar'))
            self.assertFalse(invalid.builtin_active('workspace-operations'))
            self.assertEqual(state_file.read_bytes(), state)

    def test_pending_ui_update_keeps_serving_the_current_active_entry(self):
        folder, _, _ = self.package()
        self.install(folder)
        active = self.open_host()
        updated, _, _ = self.package(version='2.0.0', ui={'entry': 'view-v2.js', 'label': 'ERP 새 화면'})
        (updated / 'view.js').rename(updated / 'view-v2.js')
        self.apply(self.plan('update', 'erp-sample', source_path=str(updated), trusted=True))
        row = next(row for row in active.snapshot()['modules'] if row['id'] == 'erp-sample')
        self.assertEqual(row['active_ui']['entry'], 'view.js')
        self.assertEqual(row['ui']['entry'], 'view-v2.js')
        content, _ = active.asset('erp-sample', row['active_ui']['entry'])
        self.assertEqual(content, (folder / 'view.js').read_bytes())
        self.assert_error(lambda: active.asset('erp-sample', row['ui']['entry']), statuses=(404,))
        restarted = self.open_host()
        row = next(row for row in restarted.snapshot()['modules'] if row['id'] == 'erp-sample')
        self.assertEqual(row['active_ui']['entry'], 'view-v2.js')
        content, _ = restarted.asset('erp-sample', row['active_ui']['entry'])
        self.assertEqual(content, (updated / 'view-v2.js').read_bytes())

    def test_tampered_installed_package_can_be_disabled_or_removed_without_data_loss(self):
        for action in ('disable', 'uninstall'):
            with self.subTest(action=action):
                ident = 'erp-tampered-' + action
                folder, marker, _ = self.package(ident)
                self.install(folder, ident)
                active = self.open_host()
                response = active.extension_request('GET', ident, [], {}, {})
                data_file = Path(response['data_dir']) / 'pending.json'; data_file.parent.mkdir(parents=True, exist_ok=True)
                original = b'{"operation_id":"A-existing","status":"uncertain"}\r\n'
                data_file.write_bytes(original)
                row = next(row for row in active.snapshot()['modules'] if row['id'] == ident)
                installed = active.root / row['package_path'] / 'backend.py'
                installed.write_text('raise RuntimeError("tampered installed package")\n', encoding='utf-8')
                source_bytes = (folder / 'backend.py').read_bytes()
                marker_before = marker.read_bytes()
                self.apply(self.plan(action, ident))
                restarted = self.open_host()
                self.assertFalse(restarted.active(ident))
                self.assertTrue(restarted.builtin_active('work-calendar'))
                self.assertTrue(restarted.builtin_active('workspace-operations'))
                self.assertEqual(data_file.read_bytes(), original)
                self.assertEqual((folder / 'backend.py').read_bytes(), source_bytes)
                self.assertEqual(marker.read_bytes(), marker_before)

    def test_stop_property_failure_does_not_interrupt_other_extension_cleanup(self):
        broken, _, _ = self.package('a-broken-stop')
        (broken / 'backend.py').write_text('''class BrokenStop:
    def handle(self, method, segments, query, body):
        return {'status': 'ready'}
    @property
    def stop(self):
        raise RuntimeError('stop property failed')

def create_module(context):
    return BrokenStop()
''', encoding='utf-8')
        good, _, stopped = self.package('z-good-stop')
        self.install(broken, 'a-broken-stop')
        self.install(good, 'z-good-stop')
        active = self.open_host()
        self.assertTrue(active.active('a-broken-stop'))
        self.assertTrue(active.active('z-good-stop'))
        self.assertFalse(stopped.exists())
        self.assertFalse(active.close())
        row = next(row for row in active.snapshot()['modules'] if row['id'] == 'a-broken-stop')
        self.assertIn('stop property failed', row['error'])
        self.assertEqual(stopped.read_text(encoding='utf-8'), 'stopped')


if __name__ == '__main__':
    unittest.main()
