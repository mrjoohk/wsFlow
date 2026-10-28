"""Synthetic producer data for CI boundary tests, with no business runtime."""
import base64
from pathlib import Path

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')

def prepare_inputs(runner, root, source):
    target = root / 'ci/modules-browser-baseline.json'
    target.write_bytes((source / 'ci/modules-browser-baseline.json').read_bytes())
    for name in ('tests/modules_browser.cjs','tests/modules_fixture.py',
                 'examples/erp_demo/module.json','examples/erp_demo/backend.py','examples/erp_demo/view.js'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic source', encoding='utf-8')

def snapshot(runner, root, folder):
    folder.mkdir(parents=True, exist_ok=True)
    baseline = runner.read(root / 'ci/modules-browser-baseline.json')
    sources = runner.module_source_hashes(root)
    runner.save(folder/'result.json', dict(ok=True,cases=baseline['cases'],pageErrors=[],
        unexpectedConsoleErrors=[],unexpectedResponses=[],forbidden=[],
        fixtureStarts=8,providerAttempts=0,node='v24.14.0',browser='synthetic',playwright='1.62.1',
        fixtureRuntimes=[dict(starts=n,python=runner.platform.python_version(),executable=runner.sys.executable) for n in range(1,9)],
        source_sha256=sources,source_sha256_end=sources))
    for number in range(1,9):
        stopped=dict(event='stopped',starts=number,preserved=True,provider_attempts=0,
            shutdown={key:{'workers_stopped':True} for key in baseline['shutdown_keys'][str(number)]})
        if number==8: runner.save(folder/'shutdown.json',stopped)
        else: runner.save(folder/('restart-'+str(number+1)+'.json'),dict(starts=number+1,previous_shutdown=stopped))
    runner.save(folder/'final-state.json',dict(event='snapshot',provider_attempts=0))
    for name in baseline['screenshots']: (folder/name).write_bytes(PNG)

