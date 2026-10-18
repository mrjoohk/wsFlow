"""HTTP 서버 — 표준 라이브러리 http.server 위의 얇은 JSON API + 정적 index.html.

127.0.0.1 에만 바인딩하고, 그 위에 F-004 가드를 둔다 — Host 검사 · Origin 검사 ·
기동 시 난수 토큰(`?t=` 또는 `X-Auth-Token`). 바인딩만으로는 사용자가 연 임의
웹페이지의 fetch 를 막지 못한다. 원격 접근·다중 사용자 인증은 여전히 범위 밖.

API
  GET    /api/config                         루트 목록
  GET    /api/v1/projects/<id>/context        I0 읽기 전용 정책·스킬 출처 진단
  GET    /api/v1/projects/<id>/context/setup  I5 카탈로그·보존 진단·작업 상태
  POST   /api/v1/projects/<id>/context/plan   I5 프로젝트 준비 불변 계획
  POST   /api/v1/roots/context/plan           I5 명시 ROOT 초기화 계획 (project_id)
  GET    /api/v1/context/operations/<id>      I5 계획·작업 확인 (project_id query)
  POST   /api/v1/context/operations/<id>/apply|recover  I5 plan_id/project_id만 승인
  GET    /api/v1/projects/<id>/session/status  I6 파일/패킷/수동 관측·stale
  POST   /api/v1/projects/<id>/session/prepare|evidence|observations  I6 준비·자료·기록
  GET    /api/v1/context/dashboard            I6 개발 환경과 별도 캘린더 경고
  GET    /api/v1/workflow/status?date=YYYY-MM-DD  I7 저장된 업무 신호 + 같은 날짜 Calendar 원본
  GET    /api/v1/calendar/range?start=&end=&timezone=&project_id=  기존 날짜 기록 (end 제외)
  GET    /api/v1/calendar/day?date=&start=&end=&snapshot_id=      같은 범위 snapshot의 날짜 상세
  GET    /api/v2/calendar/range|day?start=&end=&timezone=&project_id=&source=  I2 로컬 일정/메모 + 기존 기록
  POST   /api/v2/calendar/plan {kind,action,payload?,id?,expected_revision?}   로컬 기록 불변 계획
  POST   /api/v2/calendar/apply {plan_id,operation_id}                        선택 작업의 로컬 원자 적용
  POST   /api/roots        {path, template?}  루트 추가
  DELETE /api/roots        {path}             루트 제거
  GET    /api/scan?root=   루트 아래 후보 폴더
  GET    /api/projects     등록 목록(+요약)
  POST   /api/projects     {path, alias?, tags?}
  PATCH  /api/projects/<id> {alias?, tags?}
  DELETE /api/projects/<id>
  GET    /api/projects/<id>/status | /log?n= | /commit/<hash> | /remotes | /rules | /locks
  POST   /api/projects/<id>/remotes {name,url} | PUT /remotes/<name> {url} | DELETE /remotes/<name>
  POST   /api/projects/<id>/push   {remote?, branch?, set_upstream?}
  POST   /api/projects/<id>/fetch | /pull {}  I7 fetch --all / 고정 ff-only pull
  POST   /api/projects/<id>/add    {paths?[], all?}
  POST   /api/projects/<id>/commit {message, add_all?}
  POST   /api/projects/<id>/rules/run {tool}
  POST   /api/projects/<id>/locks/clean
  GET    /api/locks        전체 등록 저장소의 lock
  POST   /api/locks/clean  {ids?[]}  전체(또는 지정) 정리
  POST   /api/pick-folder  {initial?}  OS 폴더 선택 창을 띄워 경로 반환 (취소 시 path=null)
  POST   /api/pick-files   {initial?}  OS 파일 선택 창(복수) → {paths[]}
  POST   /api/projects/new {root, name}  루트 아래 빈 폴더 생성 + 등록 (D-010)
  POST   /api/deploy/plan  {sources[], ids[], dest_rel?}  배포 미리보기: 프로젝트별 신규/변경/동일 (D-011)
  POST   /api/deploy/apply {sources[], ids[], dest_rel?}  배포 실행(항상 덮어쓰기, 백업 없음)
  GET    /api/jira/status?site=  Jira 자격 설정 여부 (토큰 값은 담지 않는다 — D-007)
  GET    /api/projects/<id>/jira              연동 상태 (설정 파일 있으면 켜진 채 잠김 — D-004)
  GET    /api/projects/<id>/jira/tasks        지금 조회해 분류한 태스크 목록 + 조용한 유실 신호
  GET    /api/projects/<id>/jira/batch        I7 실행 기록·최종 state marker (읽기 전용)
  POST   /api/projects/<id>/jira/batch/run {}  I7 명시 일일 순환 (Jira 원격 쓰기 없음)
  POST   /api/projects/<id>/jira/migration/plan {} schema3 보존 전환 계획
  POST   /api/projects/<id>/jira/migration/apply {plan_id,operation_id} 원본 백업·schema-only 전환
  GET    /api/projects/<id>/jira/migration?plan_id=&operation_id= 로컬 전환 결과 확인
  GET    /api/projects/<id>/jira/memo?ymd=    진행 메모 (sha 동봉)
  PUT    /api/projects/<id>/jira/memo {ymd, text, sha, document_id}   문서/sha 불일치는 409 (INV-1)
  POST   /api/projects/<id>/jira/close/plan  {ymd}  서버 조회 범위의 승인 표
  POST   /api/projects/<id>/jira/close/apply {ymd,plan_id,operation_ids,ack_lint}  서버 계획 승인 후 실행
  GET    /api/projects/<id>/jira/gitignore            업무 데이터 제외 검사 (D-005)
  POST   /api/projects/<id>/jira/gitignore/fix        빠진 줄만 덧붙인다
  GET    /api/projects/<id>/jira/init/donors          사이트 공통값을 가져올 수 있는 폴더
  POST   /api/projects/<id>/jira/init/discover {site, project_key?}  프로젝트·이슈타입·CSC 후보·에픽
  POST   /api/projects/<id>/jira/init/preview  {cfg}  무엇이 바뀌는지 (watermark·roots 는 보존)
  POST   /api/projects/<id>/jira/init/apply    {cfg, fix_gitignore?}  설정 쓰기
  POST   /api/projects/<id>/jira/new/plan   {summary, kind, epic_key?, parent_key?}  만들 내용 확정
  POST   /api/projects/<id>/jira/new/create {plan_id,operation_id}  서버 계획 승인 후 생성 (D-006)
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (__version__, auth, calendar_query, calendar_store, calendar_view, closeflow, context_inventory, context_projection, session_prep,
               deploy, fileio, gitops, google_calendar, calendar_sync, jiraapi, jirawrite, newtask, operations,
               pickdir, scanner, wfhome, wfinit, writeplans, workflow, wf_migrate)
from .store import Store

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.extra = status, extra or {}


def make_handler(store: Store, token: str, google=None, google_error=None, writer=None):
    scope_cache = {}
    scope_mutex = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"wsFlow/{__version__}"

        # ---- helpers
        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n == 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise ApiError(400, "본문이 JSON 이 아닙니다")

        def _project(self, pid: str) -> dict:
            pr = store.get(pid)
            if not pr:
                raise ApiError(404, f"등록되지 않은 프로젝트: {pid}")
            if not Path(pr["path"]).is_dir():
                raise ApiError(410, f"경로가 사라졌습니다: {pr['path']}")
            return pr

        def _repo(self, pid: str) -> str:
            pr = self._project(pid)
            kind = gitops.repo_kind(pr["path"])
            if kind != "repo":
                raise ApiError(409, "git 저장소가 아닙니다" if kind == "none" else ".git 이 손상되었습니다(빈 디렉터리 등)",
                               {"git": kind})
            return pr["path"]

        def log_message(self, fmt, *args):  # 조용히
            if "--verbose" in sys.argv:
                super().log_message(fmt, *args)

        # ---- dispatch
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # ---- F-004 가드
        #
        # 127.0.0.1 바인딩은 *외부에서 오는 연결*만 막는다. 사용자가 연 임의의
        # 웹페이지에서 나가는 fetch 는 그대로 도달한다 — 브라우저가 응답을 못 읽을
        # 뿐 요청은 실행된다. commit/push/배포, 나아가 Jira 쓰기가 그렇게 실행되면
        # 되돌리기 비용이 크다. 그래서 세 겹으로 막는다.
        LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

        def _guard(self, url):
            # (1) Host — DNS 리바인딩 방어. 공격자 도메인이 127.0.0.1 로 해석돼도
            #     Host 헤더에는 그 도메인이 남는다.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host and host not in self.LOCAL_HOSTS:
                raise ApiError(403, f"허용되지 않은 Host: {host}")
            # (2) Origin — 있으면 반드시 자기 자신이어야 한다. 브라우저는 위조할 수
            #     없고, 없는 경우(curl 등)는 (3) 이 막는다.
            origin = self.headers.get("Origin")
            if origin:
                o = urlparse(origin).hostname
                if o not in self.LOCAL_HOSTS:
                    raise ApiError(403, f"허용되지 않은 Origin: {origin}")
            # (3) 토큰 — 기동 시 난수. 다른 출처의 페이지는 이 값을 읽을 수 없다.
            if url.path.startswith("/api/"):
                got = self.headers.get("X-Auth-Token") or (parse_qs(url.query).get("t") or [""])[0]
                if not secrets.compare_digest(got, token):
                    raise ApiError(403, "토큰이 없거나 올바르지 않습니다 — 콘솔에 표시된 주소로 여십시오")

        def _dispatch(self, method: str):
            url = urlparse(self.path)
            try:
                self._guard(url)
                if not url.path.startswith("/api/"):
                    return self._static(url.path)
                result = self._route(method, url.path, parse_qs(url.query))
                self._json(result)
            except ApiError as e:
                self._json({"error": str(e), **e.extra}, e.status)
            except gitops.GitError as e:
                self._json(e.to_dict(), 500)
            except jirawrite.JiraError as e:
                self._json(e.to_dict(), e.status if e.status and 400 <= e.status <= 599 else 502)
            except (operations.StateConflict, fileio.LockBusy) as e:
                self._json({'error': str(e)}, 409)
            except newtask.NewTaskError as e:
                self._json({'error': str(e)}, 422)
            except (ValueError, KeyError) as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001 — 로컬 도구, 원인을 그대로 보여준다
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _static(self, path: str):
            if path in ("/", "/index.html"):
                f = STATIC_DIR / "index.html"
                if not f.is_file():
                    raise ApiError(404, "static/index.html 없음")
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            raise ApiError(404, "not found")

        # ---- routes
        def _route(self, m: str, path: str, q: dict):
            seg = [s for s in path.split("/") if s][1:]  # drop 'api'
            body = self._body() if m in ("POST", "PUT", "PATCH", "DELETE") else {}
            if seg[:2] == ['v2', 'calendar']:
                return self._calendar_v2(m, seg[2:], q, body)
            if seg[:1] == ["v1"]:
                if seg==['v1','workflow','status'] and m=='GET':
                    from datetime import datetime
                    return workflow.overview(store.snapshot()['projects'], (q.get('date') or [datetime.now().astimezone().date().isoformat()])[0])
                if 'session' in seg or seg==['v1','context','dashboard']:
                    return self._session_v1(m,seg[1:],body)
                if 'context' in seg and (m=='POST' or seg[-1]=='setup' or 'operations' in seg):
                    return self._context_v1(m,seg[1:],q,body)
                return self._read_v1(m, seg[1:], q)
            if seg[:2] == ["jira", "status"] and m == "GET":
                return auth.status((q.get("site") or [None])[0])

            if seg == ["config"] and m == "GET":
                return {"version": __version__, "roots": store.roots(), "store": str(store.file)}

            if seg == ["roots"]:
                if m == "POST":
                    return store.add_root(body.get("path", ""), body.get("template"))
                if m == "DELETE":
                    return {"removed": store.remove_root(body.get("path", ""))}

            if seg == ["pick-folder"] and m == "POST":
                try:
                    p = pickdir.pick_folder(body.get("initial") or None)
                except RuntimeError as e:
                    raise ApiError(500, str(e))
                return {"path": p, "cancelled": p is None}

            if seg == ["pick-files"] and m == "POST":
                try:
                    paths = pickdir.pick_files(body.get("initial") or None)
                except RuntimeError as e:
                    raise ApiError(500, str(e))
                return {"paths": paths, "cancelled": not paths}

            if seg == ["projects", "new"] and m == "POST":
                return store.create_project(body.get("root", ""), body.get("name", ""))

            if seg[:1] == ["deploy"] and len(seg) == 2 and m == "POST":
                sources = [str(x) for x in (body.get("sources") or []) if str(x).strip()]
                ids = body.get("ids") or []
                if not sources:
                    raise ApiError(400, "배포 원본이 없습니다")
                if not ids:
                    raise ApiError(400, "대상 프로젝트를 선택하세요")
                targets = [self._project(i) for i in ids]
                missing = [t["alias"] for t in targets if not Path(t["path"]).is_dir()]
                if missing:
                    raise ApiError(400, f"경로가 없는 프로젝트: {', '.join(missing)}")
                fn = deploy.plan if seg[1] == "plan" else deploy.apply if seg[1] == "apply" else None
                if fn is None:
                    raise ApiError(404, f"no route: {m} {path}")
                return fn(sources, targets, body.get("dest_rel") or "", store.template_dir_for)

            if seg == ["scan"] and m == "GET":
                root = (q.get("root") or [""])[0]
                if not root or not Path(root).is_dir():
                    raise ApiError(400, "root 가 디렉터리가 아닙니다")
                registered = {p["path"] for p in store.projects()}
                return {"root": root, "items": scanner.scan_root(root, registered)}

            if seg == ["projects"]:
                if m == "GET":
                    return {"projects": [self._summary(p) for p in store.projects()]}
                if m == "POST":
                    return store.add_project(body.get("path", ""), body.get("alias"), body.get("tags"))

            if seg == ["locks"] and m == "GET":
                return {"locks": [{"id": p["id"], "alias": p["alias"], "locks": gitops.find_locks(p["path"])}
                                  for p in store.projects() if Path(p["path"], ".git").exists()]}
            if seg == ["locks", "clean"] and m == "POST":
                ids = set(body.get("ids") or [])
                out = []
                for p in store.projects():
                    if ids and p["id"] not in ids:
                        continue
                    if not Path(p["path"], ".git").exists():
                        continue
                    out.append({"id": p["id"], **gitops.clean_locks(p["path"])})
                return {"results": out}

            if len(seg) >= 2 and seg[0] == "projects":
                pid, rest = seg[1], seg[2:]
                if not rest:
                    if m == "PATCH":
                        self._project(pid)
                        return store.update_project(pid, body.get("alias"), body.get("tags"))
                    if m == "DELETE":
                        return {"removed": store.remove_project(pid)}
                    if m == "GET":
                        return self._summary(self._project(pid))
                return self._project_route(m, pid, rest, q, body)

            raise ApiError(404, f"no route: {m} {path}")

        def _calendar_v2(self, method, rest, q, body):
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
                    item=plan['items'][0]
                    if item.get('google_target') and local.get(body['operation_id']) and local.get(body['operation_id'])['status']!='succeeded':
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

        def _session_v1(self, method, seg, body):
            service=session_prep.Service(store)
            if len(seg)==4 and seg[0]=='projects' and seg[2]=='session':
                self._project(seg[1]);action=seg[3]
                if action=='status' and method=='GET':return service.status(seg[1])
                if action=='prepare' and method=='POST':return service.prepare(seg[1],body)
                if action=='evidence' and method=='POST':
                    if not isinstance(body,dict) or set(body)!={'content'}:raise ApiError(400,'파일 경로 대신 evidence content만 제출하십시오')
                    return service.upload(body['content'])
                if action=='observations' and method=='POST':return service.observe(seg[1],body)
            if seg==['context','dashboard'] and method=='GET':
                calendar={'state':'unknown','warnings':[],'outbox_counts':{},'last_sync':None}
                try:
                    if google is None:
                        calendar['warnings']=[google_error or 'Google 상태를 확인할 수 없습니다']
                    else:
                        status=google.status()
                        calendar.update(last_sync=status.get('last_sync'),state='ok' if status.get('connected') else 'disconnected')
                        calendar['warnings']=[str(status[k]) for k in ('auth_error','background_error') if status.get(k)]
                        if status.get('connected') and status.get('selected_calendar_ids') and not status.get('complete'):
                            calendar['warnings'].append('Google 조회 동기화 확인 필요')
                        if writer is not None:
                            for op in writer.status()['operations']:
                                state=op['state'];calendar['outbox_counts'][state]=calendar['outbox_counts'].get(state,0)+1
                            if any(calendar['outbox_counts'].get(s) for s in ('error','uncertain','conflict')):
                                calendar['warnings'].append('Google 연결 일정의 오류/충돌/결과 미확인 확인 필요')
                        if calendar['warnings']:calendar['state']='attention'
                except Exception:
                    calendar.update(state='unknown',warnings=['캘린더 상태 조회 실패 — 캘린더에서 다시 확인하십시오'])
                from datetime import datetime
                result=service.dashboard(calendar=calendar)
                result['workflow']=workflow.overview(store.snapshot()['projects'],datetime.now().astimezone().date().isoformat())
                return result
            raise ApiError(404,'지원하지 않는 session API입니다')

        def _context_v1(self, method, seg, query, body):
            service = context_projection.Service(store)
            if len(seg)==4 and seg[0]=='projects' and seg[2]=='context':
                self._project(seg[1])
                if seg[3]=='setup' and method=='GET':return service.setup(seg[1])
                if seg[3]=='plan' and method=='POST':
                    if body.get('kind','project_init')!='project_init':raise ApiError(400,'ROOT 초기화는 ROOT 전용 경로를 사용하십시오')
                    return service.preview(seg[1],body)
            if seg==['roots','context','plan'] and method=='POST':
                if body.get('kind')!='root_init':raise ApiError(400,'ROOT 초기화 kind를 명시하십시오')
                pid=body.get('project_id');self._project(pid)
                return service.preview(pid,{k:v for k,v in body.items() if k!='project_id'})
            if len(seg) in (3,4) and seg[:2]==['context','operations']:
                if len(seg)==3 and method=='GET':
                    pid=(query.get('project_id') or [None])[0];self._project(pid)
                    return service.status(pid,seg[2])
                if len(seg)==4 and method=='POST' and seg[3] in ('apply','recover'):
                    if set(body)!={'plan_id','project_id'}:raise operations.StateConflict('서버 plan ID와 project ID만 제출하십시오')
                    self._project(body['project_id'])
                    return service.apply(body['project_id'],body['plan_id'],seg[2],recover=seg[3]=='recover')
            raise ApiError(404,'지원하지 않는 context API입니다')

        def _read_v1(self, method: str, seg: list[str], q: dict):
            if method != "GET":
                raise ApiError(404, "읽기 전용 API입니다")
            registry = store.snapshot()
            if len(seg) == 3 and seg[0] == "projects" and seg[2] == "context":
                project = next((p for p in registry['projects'] if p['id'] == seg[1]), None)
                if project is None:
                    raise ApiError(404, "등록되지 않은 프로젝트입니다")
                return context_inventory.inspect(project, registry['roots'], registry['projects'])
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

        def _summary(self, pr: dict) -> dict:
            exists = Path(pr["path"]).is_dir()
            kind = gitops.repo_kind(pr["path"]) if exists else "missing"
            out = {**pr, "exists": exists, "git": kind, "branch": None, "ahead": 0, "behind": 0, "dirty": 0,
                   "has_remote": False, "locks": 0, "parked": exists and Path(pr["path"], "_PARKED.txt").is_file()}
            if kind == "repo":
                try:
                    st = gitops.status(pr["path"])
                    out.update(branch=st["branch"], ahead=st["ahead"], behind=st["behind"], upstream=st["upstream"],
                               dirty=len(st["staged"]) + len(st["unstaged"]) + len(st["untracked"]))
                    out["has_remote"] = bool(gitops.remotes(pr["path"]))
                    out['sync'] = st['sync']
                except gitops.GitError as e:
                    out["error"] = str(e)
            if kind in ("repo", "broken"):
                out["locks"] = len(gitops.find_locks(pr["path"]))
            return out

        def _project_route(self, m: str, pid: str, rest: list[str], q: dict, body: dict):
            r0 = rest[0]
            if r0 == "rules":
                pr = self._project(pid)
                if len(rest) == 1 and m == "GET":
                    return scanner.rules_report(pr["path"], store.template_dir_for(pr["path"]))
                if rest == ["rules", "run"] and m == "POST":
                    return scanner.run_tool(pr["path"], body.get("tool", ""))
            if r0 == "locks":
                pr = self._project(pid)
                if len(rest) == 1 and m == "GET":
                    return {"locks": gitops.find_locks(pr["path"])}
                if rest == ["locks", "clean"] and m == "POST":
                    return gitops.clean_locks(pr["path"])

            if r0 == "jira":
                return self._jira_route(m, self._project(pid), rest, q, body)

            repo = self._repo(pid)
            if r0 in ('fetch','pull') and len(rest)==1 and m=='POST':
                if body != {}:raise ApiError(400,'Git 동기화는 옵션 없이 고정 명령으로 실행합니다')
                return gitops.fetch(repo) if r0=='fetch' else gitops.pull_ff(repo)
            if r0 == "status" and m == "GET":
                return gitops.status(repo)
            if r0 == "log" and m == "GET":
                n = min(int((q.get("n") or ["50"])[0]), 500)
                ref = (q.get("ref") or [None])[0]
                return {"commits": gitops.log(repo, n, ref)}
            if r0 == "commit" and len(rest) == 2 and m == "GET":
                if not re.fullmatch(r"[0-9a-fA-F]{4,40}", rest[1]):
                    raise ApiError(400, "커밋 해시 형식이 아닙니다")
                return gitops.show_files(repo, rest[1])
            if r0 == "remotes":
                if len(rest) == 1:
                    if m == "GET":
                        return {"remotes": gitops.remotes(repo)}
                    if m == "POST":
                        gitops.remote_add(repo, body.get("name", ""), body.get("url", ""))
                        return {"remotes": gitops.remotes(repo)}
                elif len(rest) == 2:
                    if m == "PUT":
                        gitops.remote_set_url(repo, rest[1], body.get("url", ""))
                        return {"remotes": gitops.remotes(repo)}
                    if m == "DELETE":
                        gitops.remote_remove(repo, rest[1])
                        return {"remotes": gitops.remotes(repo)}
            if r0 == "push" and m == "POST":
                return gitops.push(repo, body.get("remote") or "origin", body.get("branch"), bool(body.get("set_upstream")))
            if r0 == "add" and m == "POST":
                gitops.add(repo, body.get("paths"), bool(body.get("all")))
                return gitops.status(repo)
            if r0 == "commit" and len(rest) == 1 and m == "POST":
                return gitops.commit(repo, body.get("message", ""), bool(body.get("add_all")))
            raise ApiError(404, f"no route: {m} /api/projects/{pid}/{'/'.join(rest)}")

        # ---- Jira (D-002 · D-004 · D-006)
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
                return {'operations': jirawrite.operation_status(home), 'legacy_pending': jirawrite.unpushed(home)}
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
                    return wfhome.read_memo(home, ymd)
                if m == "PUT":
                    if body.get('document_id') != wfhome.memo_document_id(home, ymd):
                        raise ApiError(409, '메모의 업무 폴더·날짜 식별이 다릅니다 — 다시 여십시오')
                    try:
                        return wfhome.write_memo(home, ymd, body.get("text", ""), body.get("sha", ""),
                                                 document_id=body['document_id'])
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
            with fileio.locked(home):
                view = wfhome.live_tasks(wfhome.read_state(home), creds)
                if view.get('complete') is False:
                    with scope_mutex:scope_cache.pop((project['id'],str(Path(home).resolve())),None)
                    return view
                with scope_mutex:
                    scope_cache[(project['id'], str(Path(home).resolve()))] = (writeplans.state_hash(home), view)
                return view

        def _scope(self, project, home, required=True):
            with scope_mutex:
                cached = scope_cache.get((project['id'], str(Path(home).resolve())))
            if cached and cached[0] == writeplans.state_hash(home):
                return cached[1]
            if required:
                raise operations.StateConflict('먼저 업무 목록을 다시 조회해야 합니다 — 클라이언트의 known_keys는 신뢰하지 않습니다')
            return None

        def _jira_init(self, m: str, pr: dict, info: dict, rest: list[str], q: dict, body: dict):
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

    return Handler


def serve(store_file: str | Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False,
          token: str | None = None, google_transport=None) -> ThreadingHTTPServer:
    store = Store(store_file)
    token = token or secrets.token_urlsafe(24)
    google,google_error = None,None
    try:
        google = google_calendar.Service(store.file,transport=google_transport,projects=lambda:store.snapshot()['projects'])
    except ValueError as exc:
        google_error = str(exc)
    class Server(ThreadingHTTPServer):
        def server_close(self):
            if google is not None:
                google.close()
            super().server_close()
    writer=calendar_sync.Engine(calendar_store.Store(store.file),google) if google is not None else None
    if writer is not None:google.after_sync=writer.after_sync
    httpd = Server((host, port), make_handler(store, token, google, google_error,writer))
    httpd.google_calendar = google
    httpd.calendar_writer = writer
    if google is not None:
        google.start_refresh()
    httpd.daemon_threads = True
    httpd.wsflow_token = token  # 호출자(run.py·시험)가 주소를 만들 수 있도록
    if open_browser:
        import webbrowser
        url = f"http://{host}:{httpd.server_address[1]}/?t={token}"
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    return httpd
