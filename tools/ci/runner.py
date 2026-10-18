"""Portable V0/V1 runner. Only explicit source and new evidence destinations are used."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import site
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
import zipfile

ROOTS = ('wsflow', 'static', 'tests', 'vendor', 'tools', 'ci', '.github')
IGNORED = {'.git', '__pycache__', '.pytest_cache', 'node_modules'}
MANIFEST = 'ci/source-manifest.json'
CONTEXT = ('.gitignore', 'GLOBAL_RULES.md', 'PROJECT_RULES.md', 'README.md',
           'graph/edges.csv', 'graph/checks_config.json', 'rd/evaluation_plan.md',
           'rd/v0_v1_contracts.md', 'evidence_pack/metrics.yaml')
BROWSER_COUNTS = (11, 11, 7, 11, 11, 14, 17)


def plain(path):
    if path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError('linked source refused: ' + str(path))


def new_output(root, destination):
    if destination.exists() or destination == root or any(destination.is_relative_to(root / n) for n in ROOTS):
        raise ValueError('new destination outside source roots required')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value) + b'\n')


def read(path):
    return json.loads(Path(path).read_bytes())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return data.replace(b'\r\n', b'\n')


def files(root):
    result = {}
    for name in ROOTS:
        base = root / name
        if not base.is_dir():
            raise ValueError('missing source root: ' + name)
        plain(base)
        for directory, directories, names in os.walk(base, followlinks=False):
            directories[:] = [n for n in directories if n not in IGNORED]
            for p in [*(Path(directory) / n for n in directories), *(Path(directory) / n for n in names)]:
                plain(p)
            for name in names:
                p = Path(directory) / name
                key = p.relative_to(root).as_posix()
                if key == MANIFEST or p.suffix == '.pyc':
                    continue
                result[key] = p
    for name in ('run.py', 'run.cmd'):
        p = root / name
        if not p.is_file():
            raise ValueError('missing/plain source required: ' + name)
        plain(p)
        result[name] = p
    return result


def fingerprint(items):
    return digest(encoded({n: digest(canonical(p.read_bytes())) for n, p in sorted(items.items())}))


def seal(root):
    target = root / MANIFEST
    if target.exists():
        raise ValueError('manifest already exists; make a new candidate for a changed source')
    items = files(root)
    manifest = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                'normalization': 'CRLF to LF; raw bytes retained in frozen archive',
                'fingerprint': fingerprint(items),
                'files': {n: {'sha256': digest(canonical(p.read_bytes())), 'raw_sha256': digest(p.read_bytes())}
                          for n, p in sorted(items.items())}}
    save(target, manifest)
    return manifest


def verify(root, manifest=None):
    doc = read(manifest or root / MANIFEST)
    if doc.get('schema_version') != 1 or not doc.get('files'):
        raise ValueError('unsupported/empty source manifest')
    items = files(root)
    expected = doc['files']
    if set(items) != set(expected):
        raise ValueError('source inventory mismatch')
    if any(digest(canonical(p.read_bytes())) != expected[n]['sha256'] for n, p in items.items()):
        raise ValueError('source hash mismatch')
    if fingerprint(items) != doc['fingerprint']:
        raise ValueError('source fingerprint mismatch')
    return doc


def freeze(root, destination):
    new_output(root, destination)
    doc = verify(root)
    destination.mkdir(parents=True, exist_ok=False)
    source = destination / 'source'
    source.mkdir()
    for name in ROOTS:
        (source / name).mkdir()
    selected = {**files(root), MANIFEST: root / MANIFEST}
    selected.update({n: root / n for n in CONTEXT if (root / n).is_file()})
    raw = {}
    for name, p in selected.items():
        for parent in (p, *p.parents):
            if parent == root:
                break
            plain(parent)
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
        raw[name] = digest(p.read_bytes())
    archive = destination / 'source.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for name in sorted(selected):
            z.write(source / name, name)
    if any(digest((source / n).read_bytes()) != value for n, value in raw.items()):
        raise ValueError('frozen byte copy mismatch')
    verify(source)
    result = {'source': str(source), 'fingerprint': doc['fingerprint'],
              'archive_sha256': digest(archive.read_bytes()), 'raw_files': raw}
    save(destination / 'freeze.json', result)
    return result


def g4(root):
    metrics = read(root / 'evidence_pack/metrics.yaml')  # JSON is a YAML 1.2 subset.
    if not isinstance(metrics, list) or not metrics:
        raise ValueError('Gate G4 empty metrics')
    ids = set()
    for m in metrics:
        if not isinstance(m, dict):
            raise ValueError('Gate G4 metric object required')
        source = (root / m.get('source', '')).resolve()
        if (not {'metric_id', 'value', 'threshold', 'comparison', 'status', 'unit', 'source', 'mode'} <= m.keys()
                or type(m['value']) not in (int, float) or type(m['threshold']) not in (int, float)
                or any(isinstance(m[k], float) and not math.isfinite(m[k]) for k in ('value', 'threshold'))
                or m['comparison'] not in ('eq', 'gte', 'lte') or m['status'] not in ('PASS', 'FAIL', 'WARN')
                or m['mode'] != 'PLANNED' or not source.is_relative_to(root) or not source.is_file()
                or m['metric_id'] in ids):
            raise ValueError('Gate G4 schema/provenance rejected')
        ids.add(m['metric_id'])
    return {'gate': 'G4', 'status': 'PASS', 'meaning': 'planned input schema/provenance, not V1 execution'}


def clean_env(out, logdir):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('JIRA_', 'WSFLOW_GOOGLE_', 'GOOGLE_', 'GITHUB_TOKEN', 'GH_TOKEN')) or key == 'PYTHONPATH':
            env.pop(key)
    profile = out / 'profile'
    profile.mkdir()
    config = profile / 'gitconfig'
    config.write_text('', encoding='utf-8')
    env.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(config), GIT_TERMINAL_PROMPT='0',
               APPDATA=str(profile / 'AppData'), LOCALAPPDATA=str(profile / 'Local'),
               XDG_CONFIG_HOME=str(profile / 'config'), WSFLOW_DATA_DIR=str(profile / 'userdata'),
               WSFLOW_RUN_EVIDENCE_DIR=str(logdir / 'unit'), WSFLOW_TEST_EVIDENCE_DIR=str(logdir / 'details'))
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
    return env


def command(args, cwd, log, env=None, timeout=900):
    log.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with log.open('wb') as stream:
        child = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt')
        try:
            code = child.wait(timeout=timeout)
        except BaseException as exc:
            if os.name == 'nt':
                try:
                    subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], stdout=stream, stderr=stream, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                os.killpg(child.pid, signal.SIGKILL)
            if child.poll() is None:
                child.kill()  # Only our Popen handle; fallback when taskkill cannot access the tree.
            child.wait(timeout=15)
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            code = 124
    return {'command': args, 'exit_code': code, 'seconds': round(time.monotonic() - start, 3), 'log': str(log)}


def accepted(step):
    if step['exit_code']:
        raise ValueError('command failed: ' + step['log'])


def gates(root, out, mode):
    """Execute bad inputs; the real CLI must fail, not merely print a failure."""
    out.mkdir(parents=True, exist_ok=False)
    if mode == 'source_mismatch':
        doc = read(root / MANIFEST)
        doc['files'][next(iter(doc['files']))]['sha256'] = '0' * 64
        save(out / 'wrong.json', doc)
        step = command([sys.executable, str(root / 'tools/ci/runner.py'), 'check', '--root', str(root),
                        '--manifest', str(out / 'wrong.json')], root, out / 'gate.log')
    elif mode == 'test_failure':
        fixture = out / 'fixture'
        fixture.mkdir()
        (fixture / 'test_failure.py').write_text('import unittest\nclass Failure(unittest.TestCase):\n def test_guard(self):self.fail("intentional gate probe")\n', encoding='utf-8')
        step = command([sys.executable, '-m', 'unittest', 'discover', '-s', str(fixture)], root, out / 'gate.log')
    elif mode == 'mutant_survived':
        fixture = out / 'fixture'
        fixture.mkdir()
        (fixture / 'subject.py').write_text('def value(): return False\n', encoding='utf-8')
        (fixture / 'test_subject.py').write_text('import unittest,subject\nclass Weak(unittest.TestCase):\n def test_value(self):self.assertIsInstance(subject.value(),bool)\n', encoding='utf-8')
        spec = {'copy': ['subject.py', 'test_subject.py'], 'test': ['python', '-m', 'unittest', 'discover'],
                'timeout': 30, 'mutants': [{'id': 'intentional_survivor', 'name': 'intentionally weak test',
                'edits': [{'file': 'subject.py', 'old': 'return False', 'new': 'return True'}]}]}
        save(out / 'spec.json', spec)
        step = command([sys.executable, str(root / 'tools/ci/mutation_runner.py'), str(out / 'spec.json'),
                        '--root', str(fixture), '--strict', '--out', str(out / 'mutation.json')], root, out / 'gate.log')
    else:
        raise ValueError('unknown gate probe')
    result = {'mode': mode, 'step': step, 'expected_nonzero': True,
              'source': verify(root)['fingerprint'], 'origin': 'controlled gate fixture'}
    log = (out / 'gate.log').read_text(encoding='utf-8', errors='replace')
    reason = ('source hash mismatch' in log if mode == 'source_mismatch' else
              'intentional gate probe' in log and 'FAILED (failures=1)' in log if mode == 'test_failure' else
              read(out / 'mutation.json')['results']['intentional_survivor']['status'] == 'survived')
    result['rejected'] = step['exit_code'] == 1 and reason
    save(out / 'gate.json', result)
    if not result['rejected']:
        raise ValueError('negative gate did not reject its input')
    return result


def run(root, out, expected_python, step, jobs):
    new_output(root, out)
    out.mkdir(parents=True, exist_ok=False)
    result = {'at': datetime.now(timezone.utc).isoformat(), 'status': 'executing', 'steps': [],
              'step': step, 'jobs': jobs,
              'provider': 'github_actions' if os.environ.get('GITHUB_ACTIONS') == 'true' else 'local isolated runner',
              'live_providers': False, 'python': platform.python_version(), 'platform': platform.platform()}
    logs = root / 'logs/test_evidence/ci' / uuid4().hex
    logs.mkdir(parents=True)
    try:
        result['source'] = verify(root)['fingerprint']
        result['g4'] = g4(root)
        if platform.python_version() != expected_python or sys.prefix == sys.base_prefix or site.ENABLE_USER_SITE:
            raise ValueError('expected isolated Python runtime not active')
        result['environment'] = {'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
                                 'user_site': site.ENABLE_USER_SITE, 'sqlite': sqlite3.sqlite_version,
                                 'host_timezone': list(time.tzname), 'image_os': os.environ.get('ImageOS'),
                                 'image_version': os.environ.get('ImageVersion'),
                                 'ci_run_id': os.environ.get('GITHUB_RUN_ID'), 'ci_sha': os.environ.get('GITHUB_SHA')}
        env = clean_env(out, logs)
        node = command(['node', '--version'], root, out / 'node_version.log', env, 30)
        result['steps'].append(node); accepted(node)
        result['environment']['node'] = (out / 'node_version.log').read_text(encoding='utf-8').strip()
        if (root / 'ci/node_modules').exists():
            raise ValueError('install Node dependencies outside the frozen source')
        selection = read(root / 'ci/selection.json')
        if step == 'all':
            for name, args in [('selftests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'ci/tests', '-v']),
                               ('vendor', [sys.executable, 'tools/check_jira_vendor.py', '--out', str(out / 'vendor.json')])]:
                r = command(args, root, out / (name + '.log'), env)
                result['steps'].append(r); accepted(r)
        if step in ('all', 'browser', 'mutations'):
            js = "const p=require(process.env.PLAYWRIGHT_MODULE||'playwright');(async()=>{const b=await p.chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});console.log(JSON.stringify({playwright:require((process.env.PLAYWRIGHT_MODULE||'playwright')+'/package.json').version,browser:b.version(),executable:process.env.PLAYWRIGHT_CHROMIUM||p.chromium.executablePath()}));await b.close()})().catch(e=>{console.error(e);process.exit(1)})"
            r = command(['node', '-e', js], root, out / 'browser_runtime.json', env, 60)
            result['steps'].append(r); accepted(r)
            result['environment']['browser'] = read(out / 'browser_runtime.json')
        if os.name == 'nt':
            import ctypes
            result['environment']['elevated'] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        if step in ('all', 'unit'):
            r = command([sys.executable, '-X', 'utf8', 'tests/run_tests.py'], root, out / 'unit.log', env)
            result['steps'].append(r); accepted(r)
            unit = read(next((logs / 'unit').glob('*_evidence.json')))
            if not unit['ok'] or unit['tests_run'] < 415 or unit['failures'] or unit['errors'] or unit['skipped'] > 1:
                raise ValueError('application regression gate failed')
            result['unit'] = unit
        if step in ('all', 'browser'):
            browser = []
            baseline = read(root / 'ci/browser-baseline.json')
            for n, expected in enumerate(BROWSER_COUNTS, 1):
                folder = logs / 'browser' / ('i' + str(n))
                env['WSFLOW_BROWSER_OUTPUT'] = str(folder)
                r = command(['node', f'tests/i{n}_browser.cjs'], root, out / f'browser_i{n}.log', env, 180)
                result['steps'].append(r); accepted(r)
                evidence = read(next(folder.glob('*evidence.json')))
                if len(evidence['cases']) < expected or evidence['errors'] or evidence['source_sha256'] != digest((root / 'static/index.html').read_bytes()):
                    raise ValueError('browser evidence gate failed')
                if not set(baseline[n-1]['cases']) <= set(evidence['cases']):
                    raise ValueError('historical browser case missing')
                for name, value in evidence.get('backend_hashes', {}).items():
                    path = root / (name if name.startswith('wsflow/') else 'wsflow/' + name)
                    if not path.is_file() or digest(path.read_bytes()) != value:
                        raise ValueError('browser backend freshness mismatch')
                browser.append({'stage': n, 'cases': len(evidence['cases'])})
            result['browser'] = browser
        if step in ('all', 'mutations'):
            mutations = []
            for item in selection['specs']:
                name = item['stage'];target = out / (name + '.json')
                args = [sys.executable, '-X', 'utf8', 'tools/ci/mutation_runner.py', item['spec'],
                        '--jobs', str(min(jobs, 2) if name.endswith('_ui') else jobs), '--out', str(target), '--strict']
                r = command(args, root, out / (name + '.log'), env, 3600)
                result['steps'].append(r);accepted(r)
                d = read(target)
                if len(d['results']) != item['count'] or any(x['status'] != 'killed' or not x['verified'] for x in d['results'].values()):
                    raise ValueError('mutation evidence gate failed')
                mutations.append({'stage': name, 'killed': len(d['results'])})
            result['mutations'] = mutations
        if step in ('all', 'gates'):
            result['gates'] = [gates(root, out / mode, mode) for mode in ('source_mismatch', 'test_failure', 'mutant_survived')]
        verify(root)
        result['status'] = 'PASS'
    except Exception as exc:
        result.update(status='FAIL', error=str(exc))
    finally:
        try:
            if logs.exists():
                shutil.copytree(logs, out / 'artifacts')
        except OSError as exc:
            result.update(status='FAIL', artifact_error=str(exc))
        finally:
            save(out / 'result.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('seal', 'check', 'freeze', 'run', 'gate'))
    p.add_argument('--root', type=Path, default=Path.cwd())
    p.add_argument('--out', type=Path)
    p.add_argument('--manifest', type=Path)
    p.add_argument('--expected-python', default=platform.python_version())
    p.add_argument('--step', choices=('all', 'unit', 'browser', 'mutations', 'gates'), default='all')
    p.add_argument('--jobs', type=int, default=4)
    p.add_argument('--mode', choices=('source_mismatch', 'test_failure', 'mutant_survived'))
    a = p.parse_args();root = a.root.resolve()
    if a.action in ('freeze', 'run', 'gate') and a.out is None:
        p.error('--out is required')
    if a.action == 'gate' and a.mode is None:
        p.error('--mode is required')
    if a.jobs < 1:
        p.error('--jobs must be positive')
    if a.action == 'seal':
        result = seal(root)
    elif a.action == 'check':
        result = verify(root, a.manifest)
    elif a.action == 'freeze':
        result = freeze(root, a.out.resolve())
    elif a.action == 'gate':
        result = gates(root, a.out.resolve(), a.mode)
        print(json.dumps(result));return 1  # Deliberately fail the provider job for a rejected fixture.
    else:
        result = run(root, a.out.resolve(), a.expected_python, a.step, a.jobs)
    print(json.dumps({k: result[k] for k in ('status', 'fingerprint', 'source') if k in result}, ensure_ascii=False))
    return 1 if result.get('status') == 'FAIL' else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr);raise SystemExit(1)
