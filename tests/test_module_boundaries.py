"""Public filesystem contracts remain available without workspace features."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class ModuleBoundaryTests(unittest.TestCase):
    def test_calendar_identity_and_deploy_work_without_workspace_features(self):
        # A fresh interpreter makes this an import boundary rather than a cache check.
        script = r'''
import importlib.abc
import json
from pathlib import Path
import sys

class NoWorkspaceFeatures(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'wsflow.context_skills', 'wsflow.context_projection'}:
            raise ImportError('workspace feature unavailable: ' + fullname)
        return None

sys.meta_path.insert(0, NoWorkspaceFeatures())
from wsflow import calendar_identity, deploy

base = Path(sys.argv[1])
root = base / 'workspace'; root.mkdir()
project = root / 'project'; project.mkdir()
registry = base / 'projects.json'
projects = [{'id': 'p', 'path': str(project)}]
registry.write_text(json.dumps({'roots': [{'path': str(root)}], 'projects': projects}), encoding='utf-8')
for path, value in [
    (root / '.wsflow/workspace.json', {'schema_version': 1, 'root_uid': 'r1', 'root_path': str(root)}),
    (project / '.wsflow/project.json', {'schema_version': 1, 'project_uid': 'p1', 'root_uid': 'r1', 'checkout_path': str(project)}),
]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
original = (project / '.wsflow/project.json').read_bytes()
observed = calendar_identity.observe(projects, registry)
assert observed['p']['status'] == 'verified', observed
assert observed['p']['project_uid'] == 'p1', observed
assert (project / '.wsflow/project.json').read_bytes() == original

source = base / 'payload.txt'; source.write_text('new template', encoding='utf-8')
result = deploy.apply([str(source)], projects, 'docs', lambda _: None)
assert result['copied_total'] == 1, result
assert (project / 'docs/payload.txt').read_text(encoding='utf-8') == 'new template'
print(json.dumps({'identity': observed['p']['status'], 'copied_total': result['copied_total']}))
'''
        with tempfile.TemporaryDirectory(prefix='wsflow-module-boundary-') as directory:
            result = subprocess.run(
                [sys.executable, '-B', '-X', 'utf8', '-c', script, directory],
                cwd=ROOT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, text=True, encoding='utf-8', timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {'identity': 'verified', 'copied_total': 1})

    def test_core_and_legacy_path_apis_preserve_traversal_and_outside_rejection(self):
        from wsflow import context_skills, path_policy
        with tempfile.TemporaryDirectory(prefix='wsflow-module-policy-') as directory:
            base = Path(directory)
            owner = base / 'project'; owner.mkdir()
            for check in (path_policy.plain_path, context_skills.plain_path):
                with self.subTest(api=check.__module__):
                    missing = owner / 'missing' / 'child.txt'
                    self.assertEqual(check(missing, owner), missing.absolute())
                    with self.assertRaisesRegex(ValueError, 'parent_traversal:'):
                        check(owner / '..' / 'outside.txt', owner)
                    with self.assertRaises(ValueError):
                        check(base / 'outside.txt', owner)

    def test_protected_context_deploy_preserves_every_existing_byte(self):
        from wsflow import context_projection, deploy, path_policy
        with tempfile.TemporaryDirectory(prefix='wsflow-module-preservation-') as directory:
            base = Path(directory); project = base / 'project'; project.mkdir()
            profile = project / '.wsflow/project.json'; profile.parent.mkdir()
            profile.write_bytes(b'{"schema_version":1,"user":"preserve"}\r\n')
            user_rules = project / 'PROJECT_RULES.md'; user_rules.write_bytes(b'USER RULES\r\n')
            source = base / 'PROJECT_RULES.md'; source.write_bytes(b'REPLACEMENT\n')
            before = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob('*') if p.is_file()}
            for check in (path_policy.protected_destination, context_projection.protected_destination):
                self.assertEqual(check(user_rules, project), 'context 관리 경로는 일반 배포로 변경할 수 없습니다')
                self.assertEqual(check(profile, project), 'context profile/manifest는 개발 환경 준비로 변경하십시오')
                self.assertEqual(check(base / 'outside.txt', project), '대상 실경로가 프로젝트 밖입니다')
                self.assertIsNone(check(project / 'docs/readme.txt', project))
            projects = [{'id': 'p', 'path': str(project)}]
            plan = deploy.plan([str(source)], projects, '', lambda _: None)
            self.assertIn('context 관리 경로', plan['projects'][0]['errors'][0])
            applied = deploy.apply([str(source)], projects, '', lambda _: None)
            self.assertEqual(applied['copied_total'], 0)
            self.assertTrue(applied['results'][0]['errors'])
            after = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob('*') if p.is_file()}
            self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
