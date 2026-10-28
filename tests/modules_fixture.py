"""Persistent, isolated real-server fixture for module browser acceptance.

The stdin control protocol restarts the actual HTTP server against the same
synthetic registry/userdata.  No provider credentials or business data are read.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import sys
import threading
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wsflow import auth, operations  # noqa: E402
from wsflow.server import serve  # noqa: E402
from wsflow.store import Store  # noqa: E402


def main():
    base = Path(sys.argv[1]).absolute()
    base.mkdir(parents=True, exist_ok=False)
    workspace = base / 'workspace'
    project = workspace / 'Project Alpha'
    project.mkdir(parents=True)
    (project / 'USER.txt').write_text('SYNTHETIC USER ORIGINAL\n', encoding='utf-8')
    (project / 'PROJECT_RULES.md').write_text('# Synthetic project policy\n', encoding='utf-8')
    registry = base / 'registry' / 'projects.json'
    registry.parent.mkdir()
    store = Store(registry)
    store.add_root(workspace)
    record = store.add_project(project, 'Synthetic Alpha')
    preserved = [project / 'USER.txt', project / 'PROJECT_RULES.md', registry]
    initial = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in preserved}
    os.environ['WSFLOW_DATA_DIR'] = str(base / 'userdata')
    os.environ.pop('WSFLOW_GOOGLE_CLIENT_FILE', None)
    for key in tuple(os.environ):
        if key.startswith('JIRA_'):
            os.environ.pop(key)
    attempts = []

    def business_snapshot():
        """Hash owned synthetic business bytes; module plans/packages are separate."""
        files = set(preserved)
        for directory in (workspace, *(base / 'userdata' / name for name in
                                      ('operations', 'calendar', 'google', 'session-evidence'))):
            if directory.exists():
                files.update(path for path in directory.rglob('*') if path.is_file())
        return {str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(files)}

    continuity_operation = None

    def continuity_control(command):
        """Fixture stdin only: no HTTP route, real account, or provider operation."""
        nonlocal continuity_operation
        if command['action'] == 'continuity_prepare':
            package = base / 'continuity-work-calendar-v1_1'
            shutil.copytree(ROOT / 'wsflow/modules/work_calendar', package,
                            ignore=shutil.ignore_patterns('__pycache__'))
            manifest_path = package / 'module.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['version'] = '1.1.0'
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            result = {'event': 'continuity_prepared', 'source_path': str(package)}
        else:
            state = command.get('state')
            op_store = operations.Store(project)
            if not op_store.path.resolve().is_relative_to(base.resolve()):
                raise ValueError('continuity operation must stay in owned fixture userdata')
            if state == 'seed':
                if continuity_operation is not None:
                    raise ValueError('continuity probe already exists')
                continuity_operation = op_store.create({'kind': 'browser_continuity_probe',
                                                        'synthetic': True})['operation_id']
            elif state == 'finish':
                if continuity_operation is None:
                    raise ValueError('continuity probe does not exist')
                op_store.approve(continuity_operation)
                op_store.claim(continuity_operation)
                op_store.finish(continuity_operation, 'succeeded', {'synthetic': True})
            elif state != 'check':
                raise ValueError('unknown continuity probe state')
            result = {'event': 'continuity_operation', 'operation_id': continuity_operation,
                      'operation_store': str(op_store.path),
                      'operation_status': (op_store.get(continuity_operation) or {}).get('status')
                      if continuity_operation else None}
        return {**result, 'business_sha256': business_snapshot(),
                'provider_attempts': len(attempts),
                'preserved': all(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == initial[str(path)]
                                 for path in preserved)}

    def no_provider(*args, **kwargs):
        attempts.append({'method': str(args[0]) if args else 'unknown'})
        raise RuntimeError('provider access forbidden in module browser fixture')

    httpd = None
    thread = None
    starts = 0

    def start():
        nonlocal httpd, thread, starts
        httpd = serve(registry, '127.0.0.1', 0, google_transport=no_provider)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        starts += 1
        return {'event': 'ready', 'url': 'http://127.0.0.1:' + str(httpd.server_address[1]),
                'token': httpd.wsflow_token, 'starts': starts, 'project_id': record['id'],
                'python': platform.python_version(), 'executable': sys.executable,
                'registry': str(registry), 'base': str(base), 'provider_attempts': len(attempts)}

    def stop():
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            thread.join(5)
        return {'event': 'stopped', 'starts': starts,
                'shutdown': getattr(httpd, 'module_shutdown', {}),
                'provider_attempts': len(attempts),
                'preserved': all(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == initial[str(path)]
                                 for path in preserved)}

    with mock.patch.object(auth, 'cred_path', return_value=base / 'absent-credentials.json'), \
            mock.patch.object(auth, 'load', side_effect=RuntimeError('no fixture credentials')):
        try:
            print(json.dumps(start()), flush=True)
            for raw in sys.stdin:
                command = json.loads(raw)
                if command.get('action') == 'restart':
                    previous = stop()
                    result = start()
                    result['previous_shutdown'] = previous
                    print(json.dumps(result), flush=True)
                elif command.get('action') == 'snapshot':
                    print(json.dumps({'event': 'snapshot', 'modules': httpd.module_host.snapshot(),
                                      'provider_attempts': len(attempts)}), flush=True)
                elif command.get('action') in ('continuity_prepare', 'continuity_operation'):
                    print(json.dumps(continuity_control(command)), flush=True)
                elif command.get('action') == 'stop':
                    print(json.dumps(stop()), flush=True)
                    httpd = None
                    break
                else:
                    raise ValueError('unknown fixture control action')
        finally:
            if httpd is not None:
                stop()


if __name__ == '__main__':
    main()
