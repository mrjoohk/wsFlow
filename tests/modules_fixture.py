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
import sys
import threading
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wsflow import auth  # noqa: E402
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
