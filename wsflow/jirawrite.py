"""Jira 쓰기 **단일 통로** — D-006.

안 A(서버가 REST 를 직접 호출)에서 이 모듈이 없으면 jiraWF 대비 순수 후퇴다.
jiraWF 는 `INV-3`("승인 기록 없는 push 는 존재하지 않는다")을 `loop_checks.py` 가
**사후에** 검사한다. 그런데 서버가 직접 쓰면 그 검사 대상 기록도 서버가 만든다 —
쓰기 뒤에 기록하는 구조라면 자기가 만든 기록을 자기가 검사하는 꼴이라 게이트가
형해화된다.

그래서 순서를 뒤집는다. **승인 기록이 먼저 있고, 그것을 찾지 못하면 호출 자체가
일어나지 않는다.** 모든 쓰기가 `execute()` 하나를 지나므로 우회 경로가 없다.

지키는 것 넷 (`worklog-close/SKILL.md` §절대 규칙 승계):

1. 승인 없이 쓰지 않는다 — `ApprovalRequired`.
2. 같은 승인으로 두 번 쓰지 않는다 — 중복 티켓은 되돌리기 비용이 가장 크다.
3. **완료(Done) 전이는 하지 않는다** — 무엇이 끝났는지는 사람만 안다. 전이 대상의
   `statusCategory` 를 실제로 조회해서 막는다. 이름(`Done`·`완료`)으로 막으면
   사이트 언어에 따라 새어 나간다.
4. 승인·결과는 `<WF_HOME>/.workflow/approvals.jsonl` 에 append-only 로 남는다.
   이 파일이 `INV-3` 의 검사 대상이며, 저장소가 아니라 업무 폴더에 있다(D-009).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from . import fileio, operations

from .jiraapi import JiraError, _send, request

KINDS = ("comment", "create_issue", "transition")
RECEIPT_PROPERTY = 'wsflow.operation'


def remote_receipt(op):
    return {'operation_id': op['operation_id'], 'payload_hash': op['payload_hash']}


class ApprovalRequired(JiraError):
    """승인 기록이 없다. **이 예외가 나면 Jira 에는 아무 일도 일어나지 않았다.**"""


class ForbiddenWrite(JiraError):
    """규칙상 하지 않기로 한 쓰기(완료 전이 등)."""


class UncertainWrite(JiraError):
    def __init__(self, message, operation_id, operation_status='uncertain'):
        super().__init__(message, 409)
        self.operation_id, self.operation_status = operation_id, operation_status

    def to_dict(self):
        return {**super().to_dict(), 'operation_id': self.operation_id,
                'operation_status': self.operation_status, 'retry_allowed': False}


def approvals_path(wf_home: str | Path) -> Path:
    return Path(wf_home) / ".workflow" / "approvals.jsonl"


def payload_hash(kind: str, target: str, payload: dict) -> str:
    """정렬된 JSON 의 sha256. 승인한 내용과 보낸 내용이 같은지 기계로 대조한다."""
    blob = json.dumps({"kind": kind, "target": target, "payload": payload},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _append(path: Path, rec: dict) -> dict:
    return fileio.append_json(path, rec)


def approve(wf_home: str | Path, kind: str, target: str, payload: dict,
            operation_id: str | None = None, site: str | None = None) -> dict:
    """사람이 승인한 사실을 **쓰기 전에** 남긴다. 반환값의 id 로 execute 를 부른다."""
    if kind not in KINDS:
        raise JiraError(f"알 수 없는 쓰기 종류: {kind!r}")
    h = payload_hash(kind, target, payload)
    with fileio.locked(wf_home):
        if site is None:
            source = Path(wf_home) / '.workflow/state.json'
            if source.is_file():
                site = (json.loads(source.read_text(encoding='utf-8-sig')).get('issue_type_map') or {}).get('site_url')
        store = operations.Store(wf_home)
        legacy = _read(approvals_path(wf_home))
        pushed = {r.get('id') for r in legacy if r.get('event') == 'pushed'}
        for record in legacy:
            if (record.get('event') == 'approved' and record.get('payload_hash') == h
                    and record.get('id') not in pushed and store.get(record.get('id')) is None):
                raise UncertainWrite('구형 승인 기록의 효과가 미확인입니다 — Jira에서 결과를 먼저 확인하십시오', record['id'], 'legacy_uncertain')
        for prior in store.list():
            if (prior.get('payload_hash') == h and prior['status'] in ('executing', 'uncertain')
                    and prior['operation_id'] != operation_id):
                raise UncertainWrite('같은 내용의 결과 미확인 작업이 있습니다 — 먼저 결과를 대조하십시오', prior['operation_id'])
        if operation_id is None:
            operation_id = store.create({'kind': kind, 'target': target, 'payload': payload,
                                         'payload_hash': h, 'site': site})['operation_id']
        op = store.get(operation_id)
        if (not op or op['kind'] != kind or op['target'] != target or op['payload'] != payload
                or site and op.get('site') and site.rstrip('/') != op['site'].rstrip('/')):
            raise ApprovalRequired('발급한 작업과 승인할 내용이 다릅니다')
        if op['status'] in ('executing', 'uncertain'):
            raise UncertainWrite('이미 진행 중이거나 결과 확인이 필요한 작업입니다', operation_id, op['status'])
        if op['status'] == 'succeeded':
            return {'id': operation_id, 'approved_at': op['approved_at']}
        op = store.approve(operation_id)
        rec = {"event": "approved", "id": operation_id, "kind": kind, "target": target,
               "payload_hash": h, "approved_at": op['approved_at']}
        if not any(r.get('event') == 'approved' and r.get('id') == operation_id for r in _read(approvals_path(wf_home))):
            _append(approvals_path(wf_home), rec)
        return rec


def find_approval(wf_home: str | Path, kind: str, target: str, payload: dict) -> dict | None:
    """승인됐고 **아직 쓰이지 않은** 기록. 없으면 None."""
    h = payload_hash(kind, target, payload)
    recs = _read(approvals_path(wf_home))
    pushed = {r.get("id") for r in recs if r.get("event") == "pushed"}
    for r in recs:
        if (r.get("event") == "approved" and r.get("payload_hash") == h
                and r.get("approved_at") and r.get("id") not in pushed):
            op = operations.Store(wf_home).get(r['id'])
            if op is None:
                # Pre-I1 approved-only logs cannot establish whether a request escaped.
                continue
            if op['status'] == 'approved':
                return r
    return None


def adf(text: str) -> dict:
    """평문 → Atlassian Document Format.

    v3 의 코멘트 본문은 평문 문자열을 받지 않는다. 문단 경계를 잃지 않도록 빈 줄이
    아닌 각 줄을 문단 하나로 옮긴다.
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()] or [""]
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": ([{"type": "text", "text": ln}] if ln else [])}
                        for ln in lines]}


def _transition_is_done(creds, base, issue_key: str, transition_id: str, send) -> bool:
    """전이 대상의 statusCategory 를 실제로 조회한다.

    이름으로 막지 않는 이유: 상태 이름은 사이트 언어에 따라 'Done'·'완료'·'해결됨'
    으로 갈리고, 그 목록을 코드에 적는 순간 새 이름 하나에 조용히 뚫린다.
    """
    data = request(creds, base, f"/rest/api/3/issue/{issue_key}/transitions", None, send)
    for t in data.get("transitions") or []:
        if str(t.get("id")) == str(transition_id):
            return ((t.get("to") or {}).get("statusCategory") or {}).get("key") == "done"
    raise JiraError(f"{issue_key} 에 전이 {transition_id} 가 없습니다")


def execute(wf_home: str | Path, creds: dict, base: str, kind: str, target: str,
            payload: dict, send=None, *, operation_id: str | None = None) -> dict:
    """**Jira 로 나가는 유일한 문.** 승인 기록이 없으면 호출조차 하지 않는다."""
    if kind not in KINDS:
        raise JiraError(f"알 수 없는 쓰기 종류: {kind!r}")
    send = send or _send
    with fileio.locked(wf_home):
        store = operations.Store(wf_home)
        appr = find_approval(wf_home, kind, target, payload)
        if operation_id is None:
            if not appr:
                pending = next((r for r in store.list() if r.get('payload_hash') == payload_hash(kind, target, payload)
                                and r['status'] in ('executing', 'uncertain')), None)
                if pending:
                    raise UncertainWrite('결과 미확인 작업을 다시 전송하지 않습니다', pending['operation_id'])
                raise ApprovalRequired('승인 기록이 없거나 이미 소비한 승인입니다')
            operation_id = appr['id']
        op = store.get(operation_id)
        if (not op or op.get('kind') != kind or op.get('target') != target or op.get('payload') != payload
                or op.get('site') and op['site'].rstrip('/') != base.rstrip('/')):
            raise ApprovalRequired('승인한 작업의 대상·본문·사이트가 다릅니다')
        records = _read(approvals_path(wf_home))
        if not any(r.get('event') == 'approved' and r.get('id') == operation_id
                   and r.get('payload_hash') == payload_hash(kind, target, payload) for r in records):
            raise ApprovalRequired('승인 로그 선기록을 확인할 수 없습니다')
        if op['status'] == 'succeeded':
            _export_result(wf_home, op)
            return op['result']
        if op['status'] in ('executing', 'uncertain'):
            raise UncertainWrite('이미 진행 중이거나 결과 미확인입니다 — 재전송하지 않습니다', operation_id, op['status'])
        if op['status'] != 'approved':
            raise ApprovalRequired('이 작업은 아직 승인되지 않았습니다')
        if kind == "comment":
            path, body = f"/rest/api/3/issue/{target}/comment", {"body": adf(payload.get("text", ""))}
        elif kind == "create_issue":
            path, body = "/rest/api/3/issue", {"fields": payload["fields"]}
        else:
            tid = payload.get("transition_id")
            if _transition_is_done(creds, base, target, tid, send):
                raise ForbiddenWrite('완료(Done) 전이는 하지 않습니다')
            path, body = f"/rest/api/3/issue/{target}/transitions", {"transition": {"id": str(tid)}}
        if kind in ('comment', 'create_issue'):
            body['properties'] = [{'key': RECEIPT_PROPERTY, 'value': remote_receipt(op)}]
        store.claim(operation_id)
        try:
            result = request(creds, base, path, body, send)
            if not isinstance(result, dict) or (kind in ('comment', 'create_issue') and not (result.get('id') or result.get('key'))):
                raise ValueError('원격 성공을 확인할 식별자가 없습니다')
        except Exception as exc:
            known_rejection = isinstance(exc, JiraError) and exc.status in (400, 401, 403, 404, 422, 429)
            status = 'failed' if known_rejection else 'uncertain'
            store.finish(operation_id, status, error=str(exc))
            if not known_rejection:
                raise UncertainWrite('원격 결과를 확정하지 못했습니다 — 결과 대조가 필요합니다', operation_id) from exc
            raise
        try:
            store.finish(operation_id, 'succeeded', result=result)
        except Exception as exc:
            # The committed claim remains non-replayable even if this update fails.
            raise UncertainWrite('원격 응답 뒤 로컬 결과 기록에 실패했습니다 — 다시 전송하지 않습니다', operation_id) from exc
        _export_result(wf_home, store.get(operation_id))
        return result


def _export_result(wf_home, op):
    try:
        if not any(r.get('event') == 'pushed' and r.get('id') == op['operation_id'] for r in _read(approvals_path(wf_home))):
            _append(approvals_path(wf_home), {'event': 'pushed', 'id': op['operation_id'],
                    'kind': op['kind'], 'target': op['target'], 'payload_hash': payload_hash(op['kind'], op['target'], op['payload']),
                    'pushed_at': _now(), 'result_key': op['result'].get('key') or op['result'].get('id')})
    except Exception as exc:
        raise UncertainWrite('성공 응답은 보관됐고 승인 로그 반영이 필요합니다 — 같은 작업으로 복구하십시오',
                             op['operation_id'], 'succeeded') from exc


def reconcile(wf_home, creds, base, operation_id, remote_id, send=None):
    """Positive evidence only: read a specific remote resource, never infer absence."""
    import re
    if not isinstance(remote_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', remote_id):
        raise ValueError('원격 comment ID 또는 issue key가 필요합니다')
    with fileio.locked(wf_home):
        store = operations.Store(wf_home); op = store.get(operation_id)
        if not op or op['status'] not in ('executing', 'uncertain'):
            raise operations.StateConflict('결과 미확인 작업만 대조할 수 있습니다')
        if op.get('site') and op['site'].rstrip('/') != base.rstrip('/'):
            raise operations.StateConflict('작업과 대조할 사이트가 다릅니다')
        if op['kind'] == 'comment':
            remote = request(creds, base, f"/rest/api/3/issue/{op['target']}/comment/{remote_id}", None, send or _send)
            if remote.get('body') != adf(op['payload']['text']):
                raise operations.StateConflict('원격 코멘트 본문이 승인한 내용과 다릅니다')
            result = {'id': str(remote.get('id') or remote_id)}
            property_path = f'/rest/api/3/comment/{remote_id}/properties/{RECEIPT_PROPERTY}'
        elif op['kind'] == 'create_issue':
            remote = request(creds, base, f'/rest/api/3/issue/{remote_id}', None, send or _send)
            def matches(expected, actual):
                if isinstance(expected, dict):
                    return isinstance(actual, dict) and all(k in actual and matches(v, actual[k]) for k, v in expected.items())
                if isinstance(expected, list):
                    return isinstance(actual, list) and all(any(matches(v, a) for a in actual) for v in expected)
                return expected == actual
            if not matches(op['payload']['fields'], remote.get('fields')) or not remote.get('key'):
                raise operations.StateConflict('원격 티켓 필드가 승인한 내용과 다릅니다')
            result = {'key': remote['key'], 'id': remote.get('id')}
            property_path = f'/rest/api/3/issue/{remote_id}/properties/{RECEIPT_PROPERTY}'
        else:
            raise operations.StateConflict('전이의 불확실한 효과는 Jira에서 직접 확인해야 합니다')
        receipt = request(creds, base, property_path, None, send or _send)
        if receipt.get('value') != remote_receipt(op):
            raise operations.StateConflict('원격 작업 식별자가 다릅니다 — 같은 본문만으로 성공을 확정하지 않습니다')
        store.finish(operation_id, 'succeeded', result=result)
        _export_result(wf_home, store.get(operation_id))
        return result


def repair(wf_home, operation_id):
    """Known success only: repair local exports without sending a Jira request."""
    with fileio.locked(wf_home):
        store = operations.Store(wf_home); op = store.get(operation_id)
        if not op or op.get('kind') not in KINDS or op['status'] != 'succeeded':
            raise operations.StateConflict('확정 성공 작업만 로컬 기록을 복구할 수 있습니다')
        _export_result(wf_home, op)
        if op['kind'] == 'create_issue' and not op['local_recorded']:
            from . import newtask
            newtask.record(wf_home, op, op['result']['key'], approved_at=op['approved_at'], operation_id=operation_id)
            store.local_recorded(operation_id)
        return op['result']


def operation_status(wf_home):
    with fileio.locked(wf_home):
        pushed = {r.get('id') for r in _read(approvals_path(wf_home)) if r.get('event') == 'pushed'}
        return [{**op, 'receipt_exported': op['operation_id'] in pushed} for op in operations.Store(wf_home).list() if op.get('kind') in KINDS]


def unpushed(wf_home: str | Path) -> list[dict]:
    """승인됐는데 아직 안 나간 것. UI 의 '대기' 목록이자 중단 복구의 근거다."""
    with fileio.locked(wf_home):
        recs = _read(approvals_path(wf_home))
        pushed = {r.get("id") for r in recs if r.get("event") == "pushed"}
        store = operations.Store(wf_home)
        return [{**r, 'status': (store.get(r['id']) or {}).get('status', 'legacy_uncertain'),
                 'retry_allowed': (store.get(r['id']) or {}).get('status') in ('approved', 'failed')}
                for r in recs if r.get("event") == "approved" and r.get("id") not in pushed]
