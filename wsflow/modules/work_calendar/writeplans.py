"""Server-owned I1 plans: immutable payload, source versions, explicit selection."""
from __future__ import annotations

import time
import re
from pathlib import Path

from . import closeflow, jiraapi, jirawrite, newtask, wfhome
from wsflow import fileio, operations
from wsflow.write_context import binding


def state_hash(home):
    return operations.digest(wfhome.state_path(home).read_bytes().hex())


def policy_hash():
    return operations.digest([(p.name, p.read_bytes().hex()) for p in
                              [newtask.NEW_TITLE, closeflow.VENDOR / 'draft_lint.py']])


def _source(project, home, kind, ymd=None):
    state = wfhome.read_state(home)
    if state.get('schema_version') != wfhome.SCHEMA_VERSION:
        raise operations.StateConflict('업무 schema가 지원 버전이 아닙니다')
    result = {'binding': binding(project, home), 'kind': kind,
              'state_hash': state_hash(home), 'site': (state.get('issue_type_map') or {}).get('site_url', '').rstrip('/'),
              'policy_hash': policy_hash(), 'ymd': ymd}
    if ymd:
        result['memo_sha'] = wfhome.read_memo(home, ymd)['sha']
    return result


def preview_close(project, home, ymd, known_keys):
    with fileio.locked(home):
        raw = closeflow.plan(home, ymd, known_keys)
        items = [{**it, 'payload_hash': jirawrite.payload_hash(it['kind'], it['target'], it['payload'])} for it in raw['items']]
        snapshot = operations.Store(home).save_plan(_source(project, home, 'close', ymd), items)
        return {**raw, 'plan_id': snapshot['plan_id'], 'items': snapshot['items'], 'expires_at': snapshot['expires_at']}


def _unprefix(title, state):
    prefix = (state.get('title_prefix') or '').strip()
    text = title[len(prefix):].strip() if prefix and title.startswith(prefix + ' ') else title
    text = re.sub(r'^(?:\[[^\]\r\n]+\]\s*)+', '', text)
    # Do not silently allocate 001 when another visible scope uses a numbered title.
    if re.search(r'--\d+--', text) and wfhome.JF.task_seq(text) is None:
        raise operations.StateConflict('기존 에픽 제목의 순번 형식을 확인하지 못했습니다 — 지원 형식은 접두사 뒤 --001--~--999--입니다')
    if re.match(r'^--\d{4,}--', text):
        raise operations.StateConflict('기존 에픽 순번이 지원 범위 001~999를 초과했습니다')
    return text


def _reserved(home, epic_key, state):
    return [_unprefix(op.get('title', ''), state) for op in operations.Store(home).list()
            if op.get('kind') == 'create_issue' and op.get('epic_key') == epic_key and op.get('task_kind') == 'task']


def preview_new(project, home, state, request, trusted_view=None, *, creation=None):
    with fileio.locked(home):
        state = wfhome.read_state(home)
        params = {k: request.get(k) for k in ('summary', 'kind', 'epic_key', 'parent_key')}
        params['kind'] = params['kind'] or 'task'
        view = trusted_view or {'tasks': [], 'known_keys': []}
        if trusted_view is not None and trusted_view.get('complete') is False:
            raise operations.StateConflict('불완전한 목록으로 생성 범위를 승인하지 않습니다')
        if params['kind'] in ('subtask', 'bug') and params['parent_key'] not in view['known_keys']:
            raise operations.StateConflict('부모 업무를 서버에서 확인할 수 없습니다 — 먼저 업무 목록을 조회하십시오')
        allowed_epics = set(state.get('epic_keys') or []) | {t.get('parent_key') for t in view['tasks']}
        if params['epic_key'] and params['epic_key'] not in allowed_epics:
            raise operations.StateConflict('이 폴더에서 확인한 과제가 아닙니다')
        params['epic_summaries'] = ([t['summary'] for t in view['tasks'] if not params['epic_key'] or t.get('parent_key') == params['epic_key']]
                                    if trusted_view is not None else None)
        if creation is not None:
            if creation.get('complete') is not True or not creation.get('account_id'):
                raise operations.StateConflict('현재 계정과 에픽 전체 제목 조회가 필요합니다')
            if params['kind'] == 'task':
                parent, _ = newtask.choose_epic(state, params['epic_key'])
                if creation['epic_key'] != parent:
                    raise operations.StateConflict('에픽 조회 대상이 생성 계획과 다릅니다')
                params['epic_key'] = parent
                params['epic_name'] = creation.get('epic_name')
                params['epic_summaries'] = ([_unprefix(t, state) for t in creation['epic_summaries']] + _reserved(home, parent, state)
                                            if parent else None)
                if params['epic_summaries'] is not None and wfhome.JF.next_task_seq(params['epic_summaries']) > 999:
                    raise operations.StateConflict('지원 순번 001~999를 초과했습니다')
        raw = newtask.plan(home, state, **params)
        if creation is not None:
            raw['payload']['fields']['assignee'] = {'accountId': creation['account_id']}
        item = {**raw, 'payload_hash': jirawrite.payload_hash(raw['kind'], raw['target'], raw['payload'])}
        snapshot = operations.Store(home).save_plan({**_source(project, home, 'new'), 'request': params, 'creation': creation}, [item])
        return {**snapshot['items'][0], 'plan_id': snapshot['plan_id'], 'expires_at': snapshot['expires_at']}


def selected(project, home, plan_id, ids, kind):
    store = operations.Store(home)
    plan = store.plan(plan_id)
    if plan['binding'] != binding(project, home) or plan['kind'] != kind or plan['home'] != str(Path(home).resolve()):
        raise operations.StateConflict('계획의 프로젝트·checkout·업무 폴더가 다릅니다')
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
        raise operations.StateConflict('계획에서 선택한 작업 ID가 필요합니다')
    allowed = {i['operation_id']: i for i in plan['items']}
    if any(i not in allowed for i in ids):
        raise operations.StateConflict('계획에 없는 작업을 승인할 수 없습니다')
    chosen = [store.get(i) for i in ids]
    pending = [i for i in chosen if i['status'] != 'succeeded']
    current = wfhome.read_state(home)
    if current.get('schema_version') != wfhome.SCHEMA_VERSION:
        raise operations.StateConflict('업무 schema가 바뀌었습니다')
    site = (current.get('issue_type_map') or {}).get('site_url', '').rstrip('/')
    if site != plan['site']:
        raise operations.StateConflict('사이트가 바뀌었습니다 — 다시 미리보기하십시오')
    if pending:
        if time.time() > plan['expires_at']:
            raise operations.StateConflict('계획이 만료됐습니다 — 다시 미리보기하십시오')
        if plan['state_hash'] != state_hash(home) or plan['policy_hash'] != policy_hash():
            raise operations.StateConflict('설정 또는 검사 정책이 바뀌었습니다 — 다시 미리보기하십시오')
        if plan.get('ymd') and plan['memo_sha'] != wfhome.read_memo(home, plan['ymd'])['sha']:
            raise operations.StateConflict('메모가 바뀌었습니다 — 다시 마감 초안을 만드십시오')
    return plan, chosen


def apply_close(project, home, plan_id, ids, creds, trusted_keys, ack_lint=False):
    with fileio.locked(home):
        plan, items = selected(project, home, plan_id, ids, 'close')
        for item in items:
            if item['status'] == 'succeeded':
                continue
            if item['target'] not in trusted_keys:
                raise operations.StateConflict('현재 서버 조회 범위에 없는 업무입니다')
            if closeflow.LINT.lint(item['payload']['text'], item['target']) and ack_lint is not True:
                raise operations.StateConflict('검사 경고를 확인한 뒤 명시적으로 승인하십시오')
        done, failed = [], []
        for item in items:
            ident = item['operation_id']
            try:
                if item['status'] != 'succeeded':
                    jirawrite.approve(home, item['kind'], item['target'], item['payload'], ident, plan['site'])
                result = jirawrite.execute(home, creds, plan['site'], item['kind'], item['target'], item['payload'], operation_id=ident)
                done.append({'target': item['target'], 'result': result.get('id'), 'operation_id': ident})
            except (jirawrite.JiraError, operations.StateConflict) as exc:
                op = operations.Store(home).get(ident)
                failed.append({'target': item['target'], 'error': str(exc), 'operation_id': ident,
                               'status': op['status'], 'retry_allowed': op['status'] == 'failed'})
        return {'done': done, 'failed': failed, 'plan_id': plan_id}


def apply_new(project, home, plan_id, ident, creds, trusted_keys):
    with fileio.locked(home):
        plan, items = selected(project, home, plan_id, [ident], 'new')
        item = items[0]
        if item['status'] != 'succeeded':
            current = wfhome.read_state(home)
            expected = newtask.plan(home, current, **plan['request'])
            creation = plan.get('creation')
            if creation:
                fresh = jiraapi.creation_context(current, creds, creation['epic_key'])
                if fresh['account_id'] != creation['account_id'] or fresh.get('epic_name') != creation.get('epic_name'):
                    raise operations.StateConflict('계정 또는 에픽이 바뀌었습니다 — 다시 미리보기하십시오')
                if creation['epic_key']:
                    seq = wfhome.JF.task_seq(_unprefix(item['title'], current))
                    remote_next = wfhome.JF.next_task_seq([_unprefix(t, current) for t in fresh['epic_summaries']])
                    if seq is None or remote_next > seq:
                        raise operations.StateConflict('에픽 순번이 이미 사용되었거나 바뀌었습니다 — 다시 미리보기하십시오')
                expected['payload']['fields']['assignee'] = {'accountId': creation['account_id']}
            if expected['payload'] != item['payload'] or expected['target'] != item['target'] or item['kind'] != 'create_issue':
                raise operations.StateConflict('최종 제목·필드·범위 검사가 계획과 다릅니다')
            if item.get('parent_key') and item['parent_key'] not in trusted_keys:
                raise operations.StateConflict('부모 업무가 현재 범위에 없습니다')
            jirawrite.approve(home, item['kind'], item['target'], item['payload'], ident, plan['site'])
        result = newtask.create(home, creds, plan['site'], item, operation_id=ident)
        if plan.get('creation'):
            result['visibility'] = (jiraapi.visibility(wfhome.read_state(home), creds, result['key'], plan['creation']['account_id'])
                                    if creds else {'state': 'unknown', 'warnings': ['이미 생성된 작업입니다. 가시성은 업무 목록에서 다시 조회하십시오.']})
        return result
