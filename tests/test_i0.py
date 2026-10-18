"""I0 acceptance: source isolation, saved work records, guarded additive APIs."""
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from wsflow import calendar_query, context_inventory
from wsflow.server import serve
from wsflow.readmodel import Reader


def tree(path):
    return {p.relative_to(path).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in path.rglob('*') if p.is_file()}


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def daily(home, day, text):
    put(home / 'daily' / f'{day}.md', '# Work\n<!-- MEMO:START -->\n' + text + '\n<!-- MEMO:END -->\n')


def state(home, site='https://example.atlassian.net'):
    put(home / '.workflow/state.json', json.dumps({'schema_version': 4, 'issue_type_map': {'site_url': site}}))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.p = self.root / 'project'; self.p.mkdir()
        self.project = {'id': 'legacy-id', 'path': str(self.p)}
        put(self.root / 'GLOBAL_RULES.md', 'common rules')
        put(self.p / 'PROJECT_RULES.md', 'project facts')

    def inspect(self):
        return context_inventory.inspect(self.project, [{'path': str(self.root)}], [self.project])

    def test_registered_root_global_is_valid_without_project_copy_and_no_write(self):
        before = tree(self.root)
        r = self.inspect()
        self.assertEqual(tree(self.root), before)
        self.assertEqual(r['project_id'], 'legacy-id')
        self.assertIsNone(r['project_uid'])
        self.assertEqual(r['identity_status'], 'legacy')
        self.assertEqual(r['rules']['common_source'], str(self.root / 'GLOBAL_RULES.md'))
        self.assertNotIn('common_rules_missing', r['blockers'])
        self.assertFalse(r['scope_complete'])
        self.assertEqual(r['observation_kind'], 'static_only')

    def test_explicit_refs_and_policy_conflict_are_structural_only(self):
        for entry in ('AGENTS.md', 'CLAUDE.md'):
            put(self.p / entry, '`../GLOBAL_RULES.md`\n`PROJECT_RULES.md`')
        r = self.inspect()
        self.assertTrue(r['rules']['ready'])
        put(self.p / 'GLOBAL_RULES.md', 'different local copy')
        self.assertIn('common_rules_shadowed', self.inspect()['blockers'])
        put(self.p / 'AGENTS.override.md', 'override')
        self.assertIn('entrypoint_override', self.inspect()['blockers'])

    def test_nearest_root_boundary_and_sibling_content_not_read(self):
        sibling = self.root / 'sibling'; sibling.mkdir()
        put(sibling / 'GLOBAL_RULES.md', 'SIBLING SECRET')
        put(self.p / 'AGENTS.md', '@../sibling/GLOBAL_RULES.md')
        r = self.inspect()
        self.assertNotIn('SIBLING SECRET', json.dumps(r))
        self.assertFalse(any(str(sibling) in s.get('resolved_path', '') for s in r['sources']))
        near = self.root / 'nested'; near.mkdir()
        nested = near / 'p'; nested.mkdir()
        roots = [{'path': str(self.root)}, {'path': str(near)}]
        got = context_inventory.inspect({'id': 'n', 'path': str(nested)}, roots)
        self.assertEqual(got['root_path'], str(near))

    def test_profile_uids_exceptions_and_mismatch_are_not_persisted(self):
        put(self.root / '.wsflow/workspace.json', json.dumps({'schema_version': 1, 'root_uid': 'root-1'}))
        put(self.p / '.wsflow/project.json', json.dumps({'schema_version': 1, 'project_uid': 'p-1', 'root_uid': 'wrong', 'policy_exceptions': [{'id': 'EX-1', 'reason': 'Reviewed exception'}]}))
        before = tree(self.root)
        r = self.inspect()
        self.assertEqual(r['project_uid'], 'p-1')
        self.assertIn('root_uid_mismatch', r['blockers'])
        self.assertEqual(r['rules']['exceptions'][0]['id'], 'EX-1')
        self.assertEqual(tree(self.root), before)

    def test_skill_hash_includes_supporting_files_and_case_collision(self):
        put(self.root / 'skills' / 'example' / 'SKILL.md', 'skill')
        put(self.root / 'skills' / 'example' / 'references' / 'guide.md', 'v1')
        r = self.inspect()
        first = r['skills'][0]['tree_hash']
        put(self.root / 'skills' / 'example' / 'references' / 'guide.md', 'v2')
        self.assertNotEqual(self.inspect()['skills'][0]['tree_hash'], first)
        put(self.p / 'skills' / 'EXAMPLE' / 'SKILL.md', 'project skill')
        self.assertIn('skill_name_collision', self.inspect()['blockers'])

    def test_external_link_is_reported_without_reading_target(self):
        outside = self.root / 'outside.md'; put(outside, 'must not read')
        link = self.p / 'GLOBAL_RULES.md'
        try:
            link.symlink_to(outside)
        except OSError as e:
            self.skipTest(f'symlink unavailable: {e}')
        r = self.inspect()
        source = next(s for s in r['sources'] if s['logical_id'] == 'project:GLOBAL_RULES.md')
        self.assertEqual(source['status'], 'blocked')
        self.assertIsNone(source['sha256'])

    def test_outside_file_and_read_permission_error_are_explicit(self):
        outside = self.root / 'private.md'; put(outside, 'not allowed')
        reader = Reader([self.p])
        meta, raw = reader.read(outside, 'outside', 'project')
        self.assertEqual(meta['status'], 'blocked'); self.assertIsNone(raw)
        target = self.p / 'PROJECT_RULES.md'
        with mock.patch.object(Path, 'open', side_effect=PermissionError('denied')):
            meta, raw = reader.read(target, 'denied', 'project')
        self.assertEqual(meta['status'], 'error'); self.assertIsNone(raw)

    def test_windows_junction_outside_scope_is_blocked(self):
        if os.name != 'nt':self.skipTest('Windows junction fixture')
        target = self.root / 'sibling'; put(target / 'SKILL.md', 'private sibling')
        link = self.p / 'skills' / 'linked'; link.parent.mkdir()
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        command = f'New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} | Out-Null'
        result = subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True)
        if result.returncode:self.skipTest('Junction unavailable on test filesystem')
        r = self.inspect()
        self.assertIn('source_unreadable_or_changed', r['blockers'])
        self.assertIsNone(next(s for s in r['skills'] if s['name'] == 'linked')['tree_hash'])

    def test_invalid_profile_does_not_expand_scope_and_extra_rules_are_observed(self):
        put(self.root / '.wsflow/workspace.json', json.dumps({'schema_version':1, 'skills_path':'.'}))
        put(self.p / '.claude/rules/check.md', 'extra local rule')
        r = self.inspect()
        self.assertIn('invalid_profile_path', r['blockers'])
        self.assertIn('additional_claude_rules_semantics_unknown', r['warnings'])
        self.assertTrue(any(s['declared_path'].endswith('check.md') for s in r['sources']))

    def test_missing_checkout_and_invalid_root_are_diagnostic(self):
        r = context_inventory.inspect({'id':'lost', 'path':str(self.p / 'missing')}, [{'path':None}])
        self.assertIn('checkout_missing', r['blockers'])
        self.assertIn('invalid_registered_root', r['warnings'])
        self.assertFalse(r['rules']['ready'])

    def test_selected_package_without_entry_is_not_ready(self):
        put(self.p / 'skills/incomplete/references/info.md', 'support without SKILL.md')
        put(self.p / '.wsflow/project.json', json.dumps({'schema_version':1, 'selected_skills':['project:incomplete']}))
        self.assertIn('selected_skill_incomplete', self.inspect()['blockers'])


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); state(self.home)
        self.project = {'id': 'a', 'path': str(self.home)}

    def query(self, projects=None, start='2026-10-16', end='2026-10-18', timezone='Asia/Seoul'):
        return calendar_query.query(projects or [self.project], start, end, timezone)

    def test_missing_daily_does_not_create_file_and_is_known_zero(self):
        before = tree(self.home)
        r = self.query()
        self.assertEqual(tree(self.home), before)
        self.assertEqual(r['days'][0]['task_count'], 0)
        self.assertEqual(r['days'][0]['memo_count'], 0)
        self.assertTrue(r['days'][0]['complete'])
        self.assertIsNone(r['days'][0]['event_count'])

    def test_saved_work_includes_historical_keys_and_deduplicates_each_day(self):
        daily(self.home, '261016', '- XXX-1: in progress\n- XXX-1: second record\n- XXX-2: \n> guidance\n- [신규] new idea\ncommon note')
        daily(self.home, '261017', '- XXX-1: continued')
        before = tree(self.home)
        r = self.query()
        self.assertEqual([d['task_count'] for d in r['days']], [1, 1])
        self.assertEqual([d['memo_count'] for d in r['days']], [1, 0])
        self.assertEqual(r['days'][0]['tasks'][0]['key'], 'XXX-1')
        self.assertEqual(len(r['days'][0]['tasks'][0]['records']), 2)
        self.assertEqual(tree(self.home), before)

    def test_duplicate_wf_home_does_not_double_count_and_keeps_links(self):
        daily(self.home, '261016', '- XXX-1: work\ncommon')
        alias = {'id': 'b', 'path': str(self.home / 'checkout'), 'wf_home': str(self.home)}
        r = self.query([self.project, alias])
        self.assertEqual(r['days'][0]['task_count'], 1)
        self.assertEqual(r['days'][0]['memo_count'], 1)
        self.assertEqual(r['days'][0]['tasks'][0]['project_ids'], ['a', 'b'])

    def test_site_and_key_identity_across_homes(self):
        other = self.home / 'other'; state(other)
        daily(self.home, '261016', '- XXX-1: first')
        daily(other, '261016', '- XXX-1: second')
        pr = {'id': 'b', 'path': str(other)}
        self.assertEqual(self.query([self.project, pr])['days'][0]['task_count'], 1)
        state(other, 'https://different.atlassian.net')
        self.assertEqual(self.query([self.project, pr])['days'][0]['task_count'], 2)

    def test_bad_markers_and_unreadable_sources_are_unknown_not_zero(self):
        put(self.home / 'daily/261016.md', 'damaged markers')
        r = self.query()
        self.assertIsNone(r['days'][0]['task_count'])
        self.assertFalse(r['days'][0]['complete'])
        missing = {'id': 'x', 'path': str(self.home / 'missing')}
        self.assertIsNone(self.query([missing])['days'][0]['memo_count'])

    def test_denied_read_is_unknown_and_no_legacy_editor_is_called(self):
        daily(self.home, '261016', '- XXX-1: work')
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path.name == '261016.md':raise PermissionError('denied')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', open_file), mock.patch('wsflow.wfhome.read_memo', side_effect=AssertionError('must not create daily')):
            r = self.query()
        self.assertIsNone(r['days'][0]['task_count'])
        self.assertFalse(r['days'][0]['complete'])

    def test_missing_file_created_during_observation_invalidates_snapshot(self):
        reader = Reader([self.home])
        path = self.home / 'new.md'
        reader.read(path, 'missing', 'project')
        put(path, 'created by another process')
        self.assertFalse(reader.finish())
        self.assertEqual(reader.sources[0]['error'], 'source_changed_during_query')

    def test_malformed_state_does_not_produce_a_confirmed_zero(self):
        put(self.home / '.workflow/state.json', '{broken')
        r = self.query()
        self.assertIsNone(r['days'][0]['task_count'])
        self.assertFalse(r['complete'])
        self.assertTrue(any(s.get('error') == 'invalid_state' for s in r['observations']))

    def test_snapshot_changes_only_with_query_or_source_changes(self):
        daily(self.home, '261016', '- XXX-1: work')
        a = self.query(); b = self.query()
        self.assertEqual(a['snapshot_id'], b['snapshot_id'])
        daily(self.home, '261016', '- XXX-1: changed')
        self.assertNotEqual(self.query()['snapshot_id'], a['snapshot_id'])

    def test_invalid_dates_ranges_and_timezone_are_rejected(self):
        for start, end in [('2026-02-30', '2026-03-02'), ('../state', '2026-10-17'), ('2026-10-16', '2026-10-16'), ('2026-01-01', '2026-10-16'), ('1999-12-31', '2000-01-02'), ('2026-1-01', '2026-01-03')]:
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.query(start=start, end=end)
        with self.assertRaises(ValueError):self.query(timezone='not/a-zone')
        self.assertEqual(len(self.query(start='2024-02-29', end='2024-03-01')['days']), 1)


class ReadApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.p = self.home / 'p'; self.p.mkdir(); state(self.p)
        registry = self.home / 'projects.json'
        put(registry, json.dumps({'roots': [{'path': str(self.home)}], 'projects': [{'id': 'old', 'path': str(self.p), 'alias': 'Original', 'tags': []}]}))
        self.server = serve(registry, '127.0.0.1', 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)

    def call(self, route, params=None, token=True, method='GET', expected=200):
        url = f'http://127.0.0.1:{self.server.server_address[1]}' + route
        if params:url += '?' + urllib.parse.urlencode(params, doseq=True)
        headers = {'X-Auth-Token': self.server.wsflow_token} if token else {}
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method), timeout=10)
        except urllib.error.HTTPError as e:r = e
        with r:
            self.assertEqual(r.status, expected)
            return json.loads(r.read().decode('utf-8'))

    def test_guarded_new_routes_keep_registry_and_legacy_rules_unchanged(self):
        before = tree(self.home)
        self.call('/api/v1/projects/old/context', token=False, expected=403)
        self.call('/api/v1/calendar/range', token=False, expected=403)
        self.call('/api/v1/projects/old/context')
        self.assertIn('global_rules', self.call('/api/projects/old/rules'))
        self.assertEqual(self.call('/api/projects/old')['id'], 'old')
        self.assertEqual(tree(self.home), before)

    def test_range_and_day_share_snapshot_and_stale_requests_conflict(self):
        daily(self.p, '261016', '- XXX-1: work')
        params = {'start': '2026-10-16', 'end': '2026-10-18', 'project_id': 'old'}
        r = self.call('/api/v1/calendar/range', params)
        d = self.call('/api/v1/calendar/day', {**params, 'date': '2026-10-16', 'snapshot_id': r['snapshot_id']})
        self.assertEqual(d['snapshot_id'], r['snapshot_id'])
        self.assertEqual(d['day'], r['days'][0])
        daily(self.p, '261016', '- XXX-1: changed')
        self.call('/api/v1/calendar/day', {**params, 'date': '2026-10-16', 'snapshot_id': r['snapshot_id']}, expected=409)
        self.call('/api/v1/calendar/range', {**params, 'project_id': 'unknown'}, expected=404)
        self.call('/api/v1/calendar/range', {'start': '../../x', 'end': '2026-10-17'}, expected=400)
        self.call('/api/v1/projects/old/context', method='POST', expected=404)
