"""Explicit schema3→4 transition. Only state.json changes; original bytes remain backed up."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import time

from . import wfhome, writeplans
from wsflow import fileio, operations


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise operations.StateConflict('중복 JSON 키가 있습니다 — 원본을 보존하고 설정을 확인하십시오')
        result[key] = value
    return result


def _decode(data):
    def invalid(value):
        raise operations.StateConflict('유효하지 않은 JSON 숫자입니다')
    def number(text):
        value = float(text)
        if not math.isfinite(value) or Decimal(str(value)) != Decimal(text):
            raise operations.StateConflict('변환하면 정밀도가 달라지는 JSON 숫자입니다 — 원본을 보존하고 수동 확인하십시오')
        return value
    value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique, parse_constant=invalid, parse_float=number)
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 3:
        raise operations.StateConflict('schema3 설정만 보존 전환할 수 있습니다')
    if not isinstance(value.get('project_key'), str) or not value['project_key'].strip():
        raise operations.StateConflict('프로젝트 키를 확인하십시오')
    previous = datetime.fromisoformat(value.get('watermark', ''))
    if previous.tzinfo is None or not isinstance(value.get('roots', {}), dict) or not isinstance(value.get('issue_type_map'), dict):
        raise operations.StateConflict('watermark offset·roots·사이트 설정을 확인하십시오')
    return value


def preview(project, home):
    """Read source once, persist immutable plan outside WF_HOME; no Jira or home writes."""
    source = fileio.within(home, wfhome.state_path(home)).read_bytes()
    state = _decode(source)
    target = json.dumps({**state, 'schema_version': wfhome.SCHEMA_VERSION}, ensure_ascii=False, indent=2).encode('utf-8')
    saved = operations.Store(home).save_plan({
        'kind': 'schema_upgrade', 'binding': writeplans.binding(project, home),
        'before_sha256': _sha(source), 'target_sha256': _sha(target), 'target_hex': target.hex(),
        'from_schema': 3, 'to_schema': wfhome.SCHEMA_VERSION,
        'preserved_fields': sorted(k for k in state if k != 'schema_version'),
    }, [{'kind': 'schema_upgrade'}])
    return {k: saved[k] for k in ('plan_id', 'expires_at', 'from_schema', 'to_schema',
                                 'before_sha256', 'target_sha256', 'preserved_fields')} | {
        'operation_id': saved['items'][0]['operation_id'], 'changed_fields': ['schema_version']}


def _selected(project, home, plan_id, operation_id):
    store = operations.Store(home)
    plan = store.plan(plan_id)
    if plan.get('kind') != 'schema_upgrade' or plan.get('binding') != writeplans.binding(project, home):
        raise operations.StateConflict('전환 계획의 프로젝트·checkout·업무 폴더가 다릅니다')
    if len(plan['items']) != 1 or plan['items'][0]['operation_id'] != operation_id:
        raise operations.StateConflict('서버가 발급한 전환 작업 ID가 필요합니다')
    op = store.get(operation_id)
    if not op or op.get('kind') != 'schema_upgrade' or op.get('plan_id') != plan_id:
        raise operations.StateConflict('전환 작업을 찾지 못했습니다')
    backup = fileio.within(home, Path(home) / '.workflow/migrations' / operation_id / 'state.before.json')
    return store, plan, op, backup


def status(project, home, plan_id, operation_id):
    """Observe local commit/backup without recovery or writes."""
    _, plan, op, backup = _selected(project, home, plan_id, operation_id)
    current = fileio.within(home, wfhome.state_path(home)).read_bytes()
    verified = backup.is_file() and _sha(backup.read_bytes()) == plan['before_sha256']
    target = _sha(current) == plan['target_sha256']
    return {'plan_id': plan_id, 'operation_id': operation_id, 'status': op['status'],
            'committed': verified and target, 'backup_verified': verified,
            'source_unchanged': _sha(current) == plan['before_sha256'],
            'result': op.get('result'), 'backup': str(backup)}


def apply(project, home, plan_id, operation_id):
    """Approve the saved local operation; retry uses hashes/backup, never client state."""
    home = Path(home).resolve()
    with fileio.locked(home):
        store, plan, op, backup = _selected(project, home, plan_id, operation_id)
        if op['status'] == 'succeeded':
            return {**op['result'], 'reused': True}
        sp = fileio.within(home, wfhome.state_path(home))
        current = sp.read_bytes()
        current_sha = _sha(current)
        result = {'plan_id': plan_id, 'operation_id': operation_id, 'status': 'succeeded',
                  'backup': str(backup), 'before_sha256': plan['before_sha256'],
                  'target_sha256': plan['target_sha256'], 'changed_fields': ['schema_version']}
        if current_sha == plan['target_sha256']:
            if op['status'] not in ('executing', 'uncertain') or not backup.is_file() or _sha(backup.read_bytes()) != plan['before_sha256']:
                raise operations.StateConflict('전환의 원본 백업·작업 결과를 확인할 수 없습니다')
            store.finish(operation_id, 'succeeded', result)
            return {**result, 'reconciled': True}
        if current_sha != plan['before_sha256']:
            raise operations.StateConflict('계획 이후 설정이 변경됐습니다 — 변경을 보존하고 다시 미리보기하십시오')
        if time.time() > plan['expires_at']:
            raise operations.StateConflict('전환 계획이 만료됐습니다 — 다시 미리보기하십시오')
        original = _decode(current)
        target = bytes.fromhex(plan['target_hex'])
        if json.loads(target) != {**original, 'schema_version': wfhome.SCHEMA_VERSION} or _sha(target) != plan['target_sha256']:
            raise operations.StateConflict('전환 계획이 schema-only 계약과 다릅니다')
        if op['status'] in ('planned', 'approved', 'failed'):
            store.approve(operation_id)
            store.claim(operation_id)
        elif op['status'] not in ('executing', 'uncertain'):
            raise operations.StateConflict('이 전환 작업은 실행할 수 없습니다')
        try:
            if backup.exists():
                if _sha(backup.read_bytes()) != plan['before_sha256']:
                    raise operations.StateConflict('전환 백업이 변경됐습니다 — 원본을 덮지 않습니다')
            else:
                fileio.atomic_write(backup, current)
            def gate():
                if sp.read_bytes() != current or _sha(backup.read_bytes()) != plan['before_sha256']:
                    raise operations.StateConflict('전환 중 원본 또는 백업이 바뀌었습니다')
            fileio.atomic_write(sp, target, gate)
            store.finish(operation_id, 'succeeded', result)
            return result
        except Exception as exc:
            try:
                if store.get(operation_id)['status'] in ('executing', 'uncertain'):
                    store.finish(operation_id, 'uncertain', error=str(exc))
            except Exception:
                pass  # Durable state/backup remain authoritative when the operation DB is unavailable.
            raise
