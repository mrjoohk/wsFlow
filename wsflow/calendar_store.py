"""I2 local calendar records and immutable plans in a user-data SQLite DB.

Reuses I1's transactions, immutable intents and audit; a local effect and its
operation result commit together. Reading an absent store creates no files.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import operations
from .calendar_query import iso_date
from .wfhome import wf_home_for


class Conflict(operations.StateConflict):
    def __init__(self, message, current=None):
        super().__init__(message)
        self.current = current


def business_date(value, *, end=False):
    parsed = iso_date(value)
    if not '2000-01-01' <= value <= ('2100-01-01' if end else '2099-12-31'):
        raise ValueError('업무 날짜의 지원 연도는 2000~2099입니다')
    return parsed


def instant(value, zone, *, end=False):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('시각에는 날짜와 UTC offset이 필요합니다')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() != parsed.astimezone(zone).utcoffset():
        raise ValueError('시각의 UTC offset과 IANA 시간대가 다릅니다')
    business_date(parsed.date().isoformat(), end=end)
    if parsed.date().isoformat() == '2100-01-01' and parsed.time().isoformat() != '00:00:00':
        raise ValueError('지원 기간의 종료 경계는 2100-01-01 자정입니다')
    return parsed


def normalize(kind, payload, allowed_ids):
    """Validate JSON event/note; return canonical fields, dates and offset times."""
    if not isinstance(payload, dict) or kind not in ('event', 'note'):
        raise ValueError('로컬 일정 또는 메모가 필요합니다')
    allowed = ({'title', 'body', 'location', 'project_ids', 'all_day', 'start_date', 'end_date', 'start_at', 'end_at', 'timezone'}
               if kind == 'event' else {'date', 'body', 'project_ids'})
    if set(payload) - allowed:
        raise ValueError('지원하지 않는 로컬 기록 필드입니다')
    links = payload.get('project_ids', [])
    if not isinstance(links, list) or any(not isinstance(i, str) or i not in allowed_ids for i in links):
        raise ValueError('등록된 프로젝트 연결을 선택하십시오')
    value = {'project_ids': sorted(set(links))}
    for key, limit in [('body', 65536)] + ([('title', 500), ('location', 2000)] if kind == 'event' else []):
        text = payload.get(key, '')
        if not isinstance(text, str) or len(text) > limit:
            raise ValueError('문자열 길이 또는 형식이 올바르지 않습니다')
        value[key] = text
    if kind == 'note':
        if not value['body'].strip():
            raise ValueError('메모 본문이 비어 있습니다')
        value['date'] = business_date(payload.get('date')).isoformat()
        return value
    if not value['title'].strip() or type(payload.get('all_day')) is not bool:
        raise ValueError('일정 제목과 종일 여부가 필요합니다')
    value['all_day'] = payload['all_day']
    if value['all_day']:
        if set(payload) & {'start_at', 'end_at', 'timezone'}:
            raise ValueError('종일 일정에는 날짜만 사용합니다')
        start, end = business_date(payload.get('start_date')), business_date(payload.get('end_date'), end=True)
        value.update(start_date=start.isoformat(), end_date=end.isoformat())
    else:
        if set(payload) & {'start_date', 'end_date'}:
            raise ValueError('시간 일정에는 offset 시각을 사용합니다')
        name = payload.get('timezone')
        if not isinstance(name, str):
            raise ValueError('IANA 시간대가 필요합니다')
        try:
            zone = ZoneInfo(name)
        except (ValueError, KeyError):
            raise ValueError('사용할 수 없는 IANA 시간대입니다') from None
        start, end = instant(payload.get('start_at'), zone), instant(payload.get('end_at'), zone, end=True)
        value.update(start_at=start.isoformat(), end_at=end.isoformat(), timezone=name)
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end <= start:
        raise ValueError('종료는 시작보다 뒤여야 합니다 (종료 경계 제외)')
    return value


class Store(operations.Store):
    def __init__(self, registry):
        self.registry = str(Path(registry).resolve())
        super().__init__(Path(registry).resolve().parent)
        self.path = self.path.parent.parent / 'calendar' / (operations.digest(self.registry) + '.sqlite3')

    def connection(self, write=False):
        from contextlib import contextmanager
        @contextmanager
        def connect():
            with super(Store, self).connection(write) as db:
                if write:
                    db.execute('CREATE TABLE IF NOT EXISTS calendar_records(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
                yield db
        return connect()

    def records(self):
        with self.connection() as db:
            rows = db.execute('SELECT data FROM calendar_records ORDER BY id').fetchall() if db else []
        return [json.loads(row[0]) for row in rows]

    def _storage_boundary(self, projects):
        for project in projects:
            for root in (Path(project['path']).resolve(), wf_home_for(project).resolve()):
                if self.path.resolve().is_relative_to(root):
                    raise Conflict('캘린더 DB는 프로젝트와 WF_HOME 밖에 있어야 합니다')

    @staticmethod
    def _bindings(projects, ids):
        return {i: next((str(Path(p['path']).resolve()) for p in projects if p['id'] == i), None) for i in ids}

    def preview(self, request, projects, google_target=None):
        self._storage_boundary(projects)
        if not isinstance(request, dict) or set(request) - {'action','kind','id','expected_revision','payload'}:
            raise ValueError('계획 요청 필드가 올바르지 않습니다')
        action, kind = request.get('action'), request.get('kind')
        if action not in ('create', 'update', 'delete') or kind not in ('event', 'note'):
            raise ValueError('지원하지 않는 기록/작업 종류입니다')
        if google_target is not None and (action != 'create' or kind != 'event'):
            raise ValueError('Google 저장 대상은 새 일정 작성에만 지정하십시오')
        current = None
        if action == 'create':
            if request.get('id') is not None or request.get('expected_revision') is not None:
                raise ValueError('작성 ID는 서버에서 발급합니다')
            ident = ('E-' if kind == 'event' else 'N-') + uuid4().hex
            expected = None
        else:
            ident, expected = request.get('id'), request.get('expected_revision')
            current = next((r for r in self.records() if r['id'] == ident and r['kind'] == kind), None)
            if not current or current['deleted'] or type(expected) is not int or expected != current['revision']:
                raise Conflict('저장본 버전이 다릅니다 — 입력을 보존하고 현재 내용을 확인하십시오', current)
        allowed = {p['id'] for p in projects} | set((current or {}).get('project_ids', []))
        payload = ({k:v for k,v in current.items() if k not in {'id','kind','revision','deleted','origin','sync_status','created_at','updated_at','project_references'}}
                   if action == 'delete' else normalize(kind, request.get('payload'), allowed))
        bindings = self._bindings(projects, payload['project_ids'])
        previous_refs = (current or {}).get('project_references', {})
        item = {'kind': kind, 'action': action, 'target': ident, 'payload': payload, 'expected_revision': expected,
                'project_bindings': bindings,
                'project_references': {i:previous_refs.get(i, bindings[i]) for i in payload['project_ids']}}
        if google_target is not None:
            item['google_target'] = google_target
        plan = self.save_plan({'kind':'local_calendar', 'binding':self.registry}, [item])
        return {'schema_version':2, 'plan_id':plan['plan_id'], 'operation_id':plan['items'][0]['operation_id'],
                'expires_at':plan['expires_at'], 'item':plan['items'][0]}

    def apply(self, plan_id, operation_id, projects):
        self._storage_boundary(projects)
        plan = self.plan(plan_id)
        if plan.get('binding') != self.registry or plan.get('kind') != 'local_calendar' or len(plan['items']) != 1 or plan['items'][0]['operation_id'] != operation_id:
            raise Conflict('이 캘린더 계획의 작업 ID가 아닙니다')
        with self.connection(True) as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if not row:
                raise Conflict('작업을 찾지 못했습니다')
            op = self._decode(row)
            if op['status'] == 'succeeded':
                return op['result']
            if time.time() > plan['expires_at'] or op['status'] != 'planned':
                raise Conflict('만료됐거나 사용할 수 없는 계획입니다 — 다시 확인하십시오')
            if op['project_bindings'] != self._bindings(projects, op['payload']['project_ids']):
                raise Conflict('프로젝트 연결이 바뀌었습니다 — 다시 확인하십시오')
            row = db.execute('SELECT data FROM calendar_records WHERE id=?', (op['target'],)).fetchone()
            current = json.loads(row[0]) if row else None
            if (op['action'] == 'create' and current is not None) or (op['action'] != 'create' and
                    (not current or current['deleted'] or current['revision'] != op['expected_revision'])):
                raise Conflict('저장본이 바뀌었습니다 — 입력을 보존하고 현재 내용을 확인하십시오', current)
            stamp = operations.now()
            claimed = db.execute("UPDATE operations SET status='executing',approved_at=? WHERE id=? AND status='planned'", (stamp, operation_id)).rowcount
            if claimed != 1:
                raise Conflict('이미 사용한 작업입니다')
            self._event(db, operation_id, 'approved'); self._event(db, operation_id, 'executing')
            record = {**op['payload'], 'id':op['target'], 'kind':op['kind'], 'revision':(current or {}).get('revision',0)+1,
                      'deleted':op['action']=='delete', 'origin':'local', 'sync_status':'local_only',
                      'project_references':op['project_references'],
                      'created_at':(current or {}).get('created_at',stamp), 'updated_at':stamp}
            db.execute('INSERT INTO calendar_records(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (record['id'], operations.encoded(record)))
            result = {'schema_version':2, 'operation_id':operation_id, 'record':record}
            from . import calendar_sync
            queued = calendar_sync.on_local_save(db,current,record,op)
            if queued:
                result['google_operation_id'] = queued
            db.execute("UPDATE operations SET status='succeeded',result=?,local_recorded=1 WHERE id=?", (operations.encoded(result), operation_id))
            self._event(db, operation_id, 'succeeded', {'before':current, 'after':record})
        return result
