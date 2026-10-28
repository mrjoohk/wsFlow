"""HTTP behavior owned by the work-calendar package; auth/transport stay in core."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
import time

from wsflow import common_writes, fileio, operations
from wsflow.module_services import ApiError
from . import (auth, calendar_query, calendar_store, calendar_view, google_calendar,
               jiraapi, jirawrite, newtask, wfhome, wfinit, workflow, wf_migrate, writeplans)


class Routes:
    def __init__(self, request, services, runtime):
        self.request, self.services, self.runtime = request, services, runtime

    def _project(self, project_id):
        return self.request._project(project_id)

    def _repo(self, project_id):
        return self.request._repo(project_id)

    def dispatch(self, method, path, query, body):
        seg = [part for part in path.split('/') if part][1:]
        if seg[:2] == ['v2', 'calendar']:
            return self._calendar_v2(method, seg[2:], query, body)
        if seg[:1] == ['v1']:
            if len(seg) >= 5 and seg[1] == 'projects' and seg[3:5] == ['writes', 'memo']:
                return self._common_v1(method, seg[1:], body)
            if seg == ['v1', 'workflow', 'status'] and method == 'GET':
                day = (query.get('date') or [datetime.now().astimezone().date().isoformat()])[0]
                return self.runtime.workflow_status(day)
            return self._read_v1(method, seg[1:], query)
        if seg[:2] == ['jira', 'status'] and method == 'GET':
            return auth.status((query.get('site') or [None])[0])
        if len(seg) >= 3 and seg[0] == 'projects' and seg[2] == 'jira':
            return self._jira_route(method, self._project(seg[1]), seg[2:], query, body)
        raise ApiError(404, '지원하지 않는 업무·일정 API입니다')

    def _calendar_v2(self, method, rest, q, body):
        store = self.services.store
        google = self.runtime.google
        google_error = self.runtime.google_error
        writer = self.runtime.writer
        registry = store.snapshot()
        local = calendar_store.Store(store.file)
        if rest[:1] == ['google']:
            if google is None:
                if method == 'GET' and rest[1:] == ['status']:
                    return {'connected':False,'configured':False,'setup_error':google_error or 'Google 서비스를 사용할 수 없습니다',
                            'selected_calendar_ids':[],'pending':False,'can_write':False}
                raise ApiError(503,google_error or 'Google 서비스를 사용할 수 없습니다')
            try:
                action = rest[1:]
                if method == 'GET' and action == ['status']:
                    return google.status()
                if method == 'GET' and action == ['calendars']:
                    return google.calendars()
                if method == 'GET' and action == ['outbox']:
                    return writer.status()
                if method == 'POST' and action == ['connect'] and isinstance(body,dict) and set(body)=={'write'}:
                    return google.connect(write=body['write'])
                if method == 'POST' and action == ['write','plan']:
                    return writer.preview(body,registry['projects'])
                if method == 'POST' and action == ['write','apply']:
                    if not isinstance(body,dict) or set(body)!={'plan_id','operation_id'}:
                        raise operations.StateConflict('서버 계획 ID와 작업 ID만 제출하십시오')
                    result=writer.apply(body['plan_id'],body['operation_id'],registry['projects'])
                    if result.get('google_operation_id'):
                        result={**result,'google':writer.run(result['google_operation_id'])}
                    return result
                if method == 'POST' and action in (['outbox','run'],['outbox','reconcile']):
                    if not isinstance(body,dict) or set(body)!={'operation_id'}:
                        raise operations.StateConflict('서버 Google 작업 ID만 제출하십시오')
                    return writer.run(body['operation_id']) if action[-1]=='run' else writer.reconcile(body['operation_id'])
                if method == 'POST' and action == ['select'] and set(body) == {'calendar_ids'}:
                    return google.select(body['calendar_ids'])
                if method == 'POST' and body == {}:
                    if action == ['connect']: return google.connect()
                    if action == ['disconnect']: return google.disconnect()
                    if action == ['sync']: return google.sync()
                raise ApiError(400,'지원하지 않는 Google 요청입니다')
            except google_calendar.GoogleError as exc:
                raise ApiError(502,str(exc)) from None
        if method == 'POST' and rest in (['plan'], ['apply']):
            try:
                if rest == ['plan']:
                    if isinstance(body,dict) and 'destination' in body:
                        dest=body['destination']
                        if writer is None or not isinstance(dest,dict) or set(dest)!={'calendar_id'}:
                            raise ValueError('Google 저장 대상을 선택하십시오')
                        return local.preview({k:v for k,v in body.items() if k!='destination'},registry['projects'],
                                             google_target=writer.target(dest['calendar_id']))
                    return local.preview(body, registry['projects'])
                if not isinstance(body, dict) or set(body) != {'plan_id','operation_id'}:
                    raise operations.StateConflict('서버 계획 ID와 작업 ID만 제출하십시오')
                plan=local.plan(body['plan_id'])
                if plan.get('binding')!=local.registry or plan.get('kind')!='local_calendar' or len(plan['items'])!=1 or plan['items'][0]['operation_id']!=body['operation_id']:
                    raise calendar_store.Conflict('이 캘린더 계획의 작업 ID가 아닙니다')
                item=plan['items'][0]
                op=local.get(body['operation_id'])
                if op['status']!='succeeded':
                    if time.time()>plan['expires_at'] or op['status']!='planned':
                        raise calendar_store.Conflict('만료됐거나 사용할 수 없는 계획입니다')
                    local._verify_bindings(op,registry['projects'])
                if item.get('google_target') and op['status']!='succeeded':
                    target=item['google_target']
                    if writer is None:raise operations.StateConflict('Google 연결을 확인하십시오')
                    with google.locked():writer._authorize(target['account_id'],target['calendar_id'])
                result=local.apply(body['plan_id'], body['operation_id'], registry['projects'])
                if writer is not None and result.get('google_operation_id'):
                    result={**result,'google':writer.run(result['google_operation_id'])}
                return result
            except calendar_store.Conflict as exc:
                raise ApiError(409, str(exc), {'current':exc.current}) from exc
        if method == 'GET' and rest in (['range'], ['day']):
            from datetime import timedelta
            day = (q.get('date') or [None])[0]
            start = (q.get('start') or [day])[0]
            end = (q.get('end') or [None])[0]
            if rest == ['day']:
                selected = calendar_query.iso_date(day)
                end = end or (selected + timedelta(days=1)).isoformat()
            ids = q.get('project_id')
            if ids and set(ids) - {p['id'] for p in registry['projects']}:
                raise ApiError(404, '등록되지 않은 프로젝트입니다')
            zone = (q.get('timezone') or ['Asia/Seoul'])[0]
            calendar_query.validate_query(start,end,zone)
            remote = None
            if ids and writer is not None and (not q.get('source') or 'google' in q['source']):
                remote=writer.scope()
            if google is not None and not ids and (not q.get('source') or 'google' in q['source']):
                try:
                    remote = google.range(start,end,zone)
                except (ValueError,OSError,sqlite3.Error):
                    remote = {'events':[],'complete':False,'connected':True,
                              'sources':[{'source_id':'google','kind':'google','name':'Google',
                                          'complete':False,'last_sync':None,'error':'Google 저장소/연결 확인 필요'}]}
            result = calendar_view.query(registry['projects'], local, start, end,
                                         zone, ids, q.get('source'), google_result=remote,google_links=writer.links() if writer else None)
            expected = (q.get('snapshot_id') or [None])[0]
            if expected is not None and expected != result['snapshot_id']:
                raise ApiError(409, '캘린더 원본이 바뀌었습니다 — 기간을 다시 조회하십시오', {'snapshot_id':result['snapshot_id']})
            if rest == ['day']:
                match = next((d for d in result['days'] if d['date'] == day), None)
                if match is None:
                    raise ApiError(400, '선택 날짜가 기간 밖입니다')
                return {k:v for k,v in result.items() if k != 'days'} | {'day':match}
            return result
        raise ApiError(404, '지원하지 않는 캘린더 API입니다')


    def _jira_route(self, m: str, pr: dict, rest: list[str], q: dict, body: dict):
        info = wfhome.detect(pr)
        if len(rest) == 1 and m == "GET":
            return info

        # 설정 작성과 .gitignore 검사는 **연동이 꺼진 상태에서도** 돼야 한다.
        # 그 상태를 벗어나려고 부르는 경로이기 때문이다.
        if rest[1:2] == ["gitignore"]:
            if len(rest) == 2 and m == "GET":
                return wfinit.gitignore_report(wfinit.gitignore_target(info['wf_home']))
            if rest[2:] == ["fix"] and m == "POST":
                return wfinit.gitignore_apply(wfinit.gitignore_target(info['wf_home']))
        if rest[1:2] == ["init"]:
            return self._jira_init(m, pr, info, rest, q, body)

        if rest[1:2] == ['migration']:
            home = info['wf_home']
            if rest == ['jira', 'migration'] and m == 'GET':
                return wf_migrate.status(pr, home, q.get('plan_id', [''])[0], q.get('operation_id', [''])[0])
            if rest == ['jira', 'migration', 'plan'] and m == 'POST':
                if body != {}:raise ApiError(400, '전환 대상과 변경값은 서버가 결정합니다')
                return wf_migrate.preview(pr, home)
            if rest == ['jira', 'migration', 'apply'] and m == 'POST':
                if set(body) != {'plan_id', 'operation_id'} or any(not isinstance(v, str) or not v for v in body.values()):
                    raise ApiError(400, '서버가 발급한 계획·작업 ID만 필요합니다')
                repository = wfinit.gitignore_target(home)
                gi = wfinit.gitignore_report(repository)
                if gi['applicable'] and gi['missing']:
                    raise ApiError(409, '업무 데이터 제외 설정을 확인한 뒤 전환하십시오: '+str(repository), {'gitignore': gi})
                return wf_migrate.apply(pr, home, body['plan_id'], body['operation_id'])
            raise ApiError(404, '지원하지 않는 전환 경로입니다')

        if not info["enabled"]:
            raise ApiError(409, info["reason"], {"jira": info})
        home = info["wf_home"]
        state = wfhome.read_state(home)
        if (m != 'GET' or rest == ['jira', 'memo']) and state.get('schema_version') != wfhome.SCHEMA_VERSION:
            raise ApiError(409, '업무 schema가 지원 버전이 아닙니다 — 설정 작성으로 복구하십시오')

        if rest==['jira','batch'] and m=='GET':
            return workflow.status(home)
        if rest==['jira','batch','run'] and m=='POST':
            if body != {}:raise ApiError(400,'배치 날짜·경로·cursor는 서버가 결정합니다')
            return workflow.run(home,self._creds(info))

        if rest == ['jira', 'operations'] and m == 'GET':
            return {'operations': jirawrite.operation_status(home), 'legacy_pending': jirawrite.unpushed(home),
                    'common_writes':common_writes.Service(pr,'memo',home,memo_adapter=wfhome).list()[-10:]}
        if len(rest) == 4 and rest[:2] == ['jira', 'operations'] and rest[3] in ('reconcile', 'repair') and m == 'POST':
            with fileio.locked(home):
                state = wfhome.read_state(home)
                if state.get('schema_version') != wfhome.SCHEMA_VERSION:
                    raise operations.StateConflict('업무 schema가 바뀌었습니다 — 설정을 확인하십시오')
                pending = operations.Store(home).get(rest[2])
                if not pending or pending.get('kind') not in jirawrite.KINDS or not pending.get('plan_id'):
                    raise operations.StateConflict('서버 발급 작업을 찾지 못했습니다')
                saved = operations.Store(home).plan(pending['plan_id'])
                if saved['binding'] != writeplans.binding(pr, home) or saved['site'] != jiraapi.site_url(state).rstrip('/'):
                    raise operations.StateConflict('작업의 프로젝트 또는 사이트가 다릅니다')
                if rest[3] == 'reconcile':
                    jirawrite.reconcile(home, self._creds(info), jiraapi.site_url(state), rest[2], body.get('remote_id'))
                result = jirawrite.repair(home, rest[2])
                return {'result': result, 'operation_id': rest[2], 'status': 'succeeded'}

        if rest == ["jira", "memo"]:
            ymd = (q.get("ymd") or [body.get("ymd")] or [None])[0] or body.get("ymd")
            if not ymd:
                raise ApiError(400, "ymd 가 필요합니다")
            if m == "GET":
                return {**wfhome.read_memo(home, ymd),'common_writes':[op for op in common_writes.Service(pr,'memo',home,memo_adapter=wfhome).list() if op.get('ymd')==ymd and op['status'] in ('executing','uncertain')]}
            if m == "PUT":
                if body.get('document_id') != wfhome.memo_document_id(home, ymd):
                    raise ApiError(409, '메모의 업무 폴더·날짜 식별이 다릅니다 — 다시 여십시오')
                try:
                    return common_writes.Service(pr,'memo',home,memo_adapter=wfhome).legacy('save',{'ymd':ymd,'text':body.get('text',''),'sha':body.get('sha',''),'document_id':body['document_id']})
                except wfhome.MemoConflict as e:
                    # 덮지 않는다. 양쪽을 돌려주고 사람이 고르게 한다(INV-1).
                    raise ApiError(409, str(e), {"current_text": e.current_text,
                                                 "current_sha": e.current_sha}) from e

        # --- Jira 를 건드리지 않는 경로는 자격을 요구하지 않는다.
        # 요구하면 "자격이 없다"와 "제목이 범위 밖이다"가 같은 실패로 보인다.
        if rest == ["jira", "close", "plan"] and m == "POST":
            with fileio.locked(home):
                view = self._scope(pr, home)
                return writeplans.preview_close(pr, home, body['ymd'], view['known_keys'])
        if rest[1:3] == ["new", "plan"] and m == "POST":
            try:
                with fileio.locked(home):
                    state=wfhome.read_state(home)
                    if body.get('kind','task') not in ('task','subtask','bug') or not isinstance(body.get('summary'),str) or not body['summary'].strip():
                        raise newtask.NewTaskError('유효한 업무 종류와 비어 있지 않은 제목이 필요합니다')
                    epic,_=newtask.choose_epic(state,body.get('epic_key')) if body.get('kind','task')=='task' else (None,'')
                    try:creds=self._creds(info)
                    except ApiError as exc:
                        if exc.status!=412:raise
                        preview=writeplans.preview_new(pr,home,state,body,self._scope(pr,home,required=False))
                        preview['ready_to_create']=False
                        preview['warnings'].append('오프라인 제목 미리보기입니다. 생성하려면 Jira 계정·에픽 전체 조회 후 다시 미리보기하십시오.')
                        return preview
                    creation=jiraapi.creation_context(state,creds,epic)
                    return {**writeplans.preview_new(pr, home, state, body, self._scope(pr, home, required=False),creation=creation),'ready_to_create':True}
            except newtask.NewTaskError as e:
                # 만들기 전에 멈춘다 — 만든 뒤에는 되돌리기가 가장 비싸다.
                raise ApiError(422, str(e)) from e

        if rest == ['jira', 'close', 'apply'] and m == 'POST':
            if set(body) - {'plan_id', 'operation_ids', 'ymd', 'ack_lint'} or not body.get('plan_id'):
                raise operations.StateConflict('서버 계획과 선택 작업 ID로 승인하십시오 — 다시 미리보기해야 합니다')
            with fileio.locked(home):
                plan, chosen = writeplans.selected(pr, home, body['plan_id'], body.get('operation_ids'), 'close')
                if body.get('ymd') != plan['ymd']:
                    raise operations.StateConflict('마감 날짜가 계획과 다릅니다')
                pending = any(i['status'] != 'succeeded' for i in chosen)
                creds = self._creds(info) if pending else {}
                fresh = self._fresh_scope(pr, home, creds) if pending else {'known_keys':[]}
                if fresh.get('complete') is False:raise operations.StateConflict('Jira 조회가 불완전합니다 — 다시 조회 후 승인하십시오')
                keys = fresh['known_keys']
                return writeplans.apply_close(pr, home, body['plan_id'], body['operation_ids'], creds, keys,
                                              body.get('ack_lint') is True)
        if rest == ['jira', 'new', 'create'] and m == 'POST':
            if set(body) != {'plan_id', 'operation_id'}:
                raise operations.StateConflict('발급한 계획 ID와 작업 ID가 필요합니다 — raw intent는 승인하지 않습니다')
            with fileio.locked(home):
                saved, chosen = writeplans.selected(pr, home, body['plan_id'], [body['operation_id']], 'new')
                pending = chosen[0]['status'] != 'succeeded'
                if pending and not saved.get('creation'):raise operations.StateConflict('계정·에픽 전체 조회가 없는 오프라인 계획입니다 — 연결 후 다시 미리보기하십시오')
                creds = self._creds(info) if pending else {}
                fresh=self._fresh_scope(pr, home, creds) if pending and chosen[0].get('parent_key') else {'known_keys':[]}
                if fresh.get('complete') is False:raise operations.StateConflict('Jira 부모 업무 조회가 불완전합니다')
                keys = fresh['known_keys']
                return writeplans.apply_new(pr, home, body['plan_id'], body['operation_id'], creds, keys)

        # --- 여기서부터는 Jira 를 읽는다.
        creds = self._creds(info)
        if rest == ["jira", "tasks"] and m == "GET":
            return self._fresh_scope(pr, home, creds)
        raise ApiError(404, f"no route: {m} {'/'.join(rest)}")


    def _fresh_scope(self, project, home, creds):
        scope_cache = self.runtime.scope_cache
        scope_mutex = self.runtime.scope_mutex
        with fileio.locked(home):
            view = wfhome.live_tasks(wfhome.read_state(home), creds)
            if view.get('complete') is False:
                with scope_mutex:scope_cache.pop((project['id'],str(Path(home).resolve())),None)
                return view
            with scope_mutex:
                scope_cache[(project['id'], str(Path(home).resolve()))] = (writeplans.state_hash(home), view)
            return view


    def _scope(self, project, home, required=True):
        scope_cache = self.runtime.scope_cache
        scope_mutex = self.runtime.scope_mutex
        with scope_mutex:
            cached = scope_cache.get((project['id'], str(Path(home).resolve())))
        if cached and cached[0] == writeplans.state_hash(home):
            return cached[1]
        if required:
            raise operations.StateConflict('먼저 업무 목록을 다시 조회해야 합니다 — 클라이언트의 known_keys는 신뢰하지 않습니다')
        return None


    def _jira_init(self, m: str, pr: dict, info: dict, rest: list[str], q: dict, body: dict):
        store = self.services.store
        tail = rest[2:]
        if tail == ["donors"] and m == "GET":
            return {"donors": wfinit.donor_states(store.projects())}
        if tail == ["discover"] and m == "POST":
            site = (body.get("site") or info.get("site_url") or "").rstrip("/")
            if not site:
                raise ApiError(400, "사이트 URL 이 필요합니다 — 기존 설정에서 가져오거나 직접 넣으십시오")
            return wfinit.discover(self._creds({"site_url": site}), site, body.get("project_key"))
        if tail in (["preview"], ["apply"]) and m == "POST":
            cfg = dict(body.get("cfg") or {})
            if not cfg.get("project_key"):
                raise ApiError(400, "project_key 가 필요합니다")
            home = info["wf_home"]
            existing = None
            if wfhome.state_path(home).is_file():
                existing = wfhome.read_state(home)
            new_state = wfinit.build_state(existing, **cfg)
            pv = wfinit.preview(home, new_state)
            repository = wfinit.gitignore_target(home)
            gi = wfinit.gitignore_report(repository)
            if tail == ["preview"]:
                return {"preview": pv, "state": new_state, "gitignore": gi}
            # D-005 — 제외가 없으면 쓰기 전에 멈춘다. 고칠지는 사람이 정한다.
            if gi["applicable"] and gi["missing"] and not body.get("fix_gitignore"):
                raise ApiError(409, "업무 데이터가 .gitignore 로 제외되지 않았습니다 — "
                                    "추가에 동의하거나 직접 넣은 뒤 다시 시도하십시오",
                               {"gitignore": gi})
            if gi["applicable"] and gi["missing"]:
                gi = wfinit.gitignore_apply(repository)
            path = wfinit.write_state(home, new_state)
            return {"written": path, "preview": pv, "gitignore": gi,
                    "jira": wfhome.detect(pr)}
        raise ApiError(404, f"no route: {m} {'/'.join(rest)}")


    def _creds(self, info: dict):
        try:
            return auth.load(info.get("site_url"))
        except auth.CredentialError as e:
            raise ApiError(412, str(e), {"credentials": auth.status(info.get("site_url"))}) from e


    def _read_v1(self, method: str, seg: list[str], q: dict):
        store = self.services.store
        if method != "GET":
            raise ApiError(404, "읽기 전용 API입니다")
        registry = store.snapshot()
        if len(seg) == 2 and seg[0] == "calendar" and seg[1] in ("range", "day"):
            from datetime import timedelta
            ids = q.get("project_id")
            by_id = {p['id']: p for p in registry['projects']}
            projects = [by_id.get(pid) for pid in ids] if ids else registry['projects']
            if any(p is None for p in projects):
                raise ApiError(404, "등록되지 않은 프로젝트입니다")
            # Preserve missing projects as unknown sources rather than treating them as empty.
            projects = list({p['id']: p for p in projects}.values())
            day = (q.get("date") or [""])[0]
            start = (q.get("start") or [day])[0]
            end = (q.get("end") or [""])[0]
            if seg[1] == "day":
                selected = calendar_query.iso_date(day)
                if not end:
                    end = (selected + timedelta(days=1)).isoformat()
            result = calendar_query.query(projects, start, end,
                                          (q.get("timezone") or ["Asia/Seoul"])[0])
            expected = (q.get("snapshot_id") or [None])[0]
            if expected is not None and expected != result['snapshot_id']:
                raise ApiError(409, "날짜 기록이 바뀌었습니다 — 기간을 다시 조회하십시오",
                               {"snapshot_id": result['snapshot_id']})
            if seg[1] == "day":
                match = next((d for d in result['days'] if d['date'] == day), None)
                if match is None:
                    raise ApiError(400, "선택 날짜가 조회 기간 밖입니다")
                return {k: v for k, v in result.items() if k != 'days'} | {'day': match}
            return result
        raise ApiError(404, "no v1 route")


    def _common_v1(self,method,seg,body):
        project=self._project(seg[1]);kind=seg[3]
        if kind != 'memo':raise ApiError(400,'지원하지 않는 쓰기 종류입니다')
        home=None
        if kind=='memo':
            info=wfhome.detect(project)
            if not info['enabled']:raise ApiError(409,info['reason'])
            home=info['wf_home']
        service=common_writes.Service(project,kind,home,memo_adapter=wfhome)
        rest=seg[4:]
        if rest==['plan'] and method=='POST':
            if not isinstance(body,dict) or set(body)!={'action','payload'}:raise ApiError(400,'action/payload만 제출하십시오')
            return service.preview(body['action'],body['payload'])
        if rest==['apply'] and method=='POST':
            if not isinstance(body,dict) or set(body)!={'plan_id','operation_id'}:raise ApiError(400,'서버 계획·작업 ID만 제출하십시오')
            return service.apply(body['plan_id'],body['operation_id'])
        if rest==['operations'] and method=='GET':return {'operations':service.list()}
        if len(rest)==2 and rest[0]=='operations':
            if method=='GET':return service.status(rest[1])
            if method=='POST' and body=={}:return service.check(rest[1])
        raise ApiError(404,'지원하지 않는 공통 쓰기 API입니다')


