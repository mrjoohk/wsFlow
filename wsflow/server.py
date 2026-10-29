"""HTTP 서버 — 표준 라이브러리 http.server 위의 얇은 JSON API + 정적 index.html.

127.0.0.1 에만 바인딩하고, 그 위에 F-004 가드를 둔다 — Host 검사 · Origin 검사 ·
기동 시 난수 토큰(`?t=` 또는 `X-Auth-Token`). 바인딩만으로는 사용자가 연 임의
웹페이지의 fetch 를 막지 못한다. 원격 접근·다중 사용자 인증은 여전히 범위 밖.

API
  POST   /api/v1/projects/<id>/writes/<git|memo>/plan {action,payload}  공통 불변 계획·작업 ID
  POST   /api/v1/projects/<id>/writes/<git|memo>/apply {plan_id,operation_id}  단일 실행
  GET    /api/v1/projects/<id>/writes/<git|memo>/operations[/<id>]  읽기 전용 목록·상태
  POST   /api/v1/projects/<id>/writes/<git|memo>/operations/<id> {}  명시 결과 대조·미실행 종료
  GET    /api/config                         루트 목록
  GET    /api/v1/modules                     모듈의 현재/재기동 적용 대기 구성
  POST   /api/v1/modules/plan                 모듈 구성 변경 미리보기
  POST   /api/v1/modules/apply {plan_id}      다음 기동에 적용할 구성 저장
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
  POST   /api/v2/calendar/plan {kind,action,payload?,id?,expected_revision?,rebind_project_ids?} 로컬 기록 불변 계획
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

Business HTTP handlers and worker lifecycles are owned by separately versioned
packages; this core owns authentication, transport, registry and dispatch.
"""
from __future__ import annotations

import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, fileio, operations, pickdir
from .builtin_modules import route_owner, WORK_CALENDAR
from .module_host import ModuleError, ModuleHost
from .module_services import ApiError, ModuleServices
from .runtime_activity import ActivityUnavailable
from .store import Store

STATIC_DIR = Path(__file__).resolve().parent.parent / 'static'


# Compatibility for callers that explicitly patched the former server imports.
# A clean core import still loads no business implementation.
def __getattr__(name):
    legacy = {'auth','calendar_query','calendar_store','calendar_view','closeflow',
              'context_inventory','context_projection','session_prep','deploy','gitops',
              'google_calendar','calendar_sync','jiraapi','jirawrite','newtask','scanner',
              'wfhome','wfinit','writeplans','workflow','wf_migrate','common_writes'}
    if name in legacy:
        import importlib
        return importlib.import_module('wsflow.' + name)
    raise AttributeError(name)


class AssetResponse:
    def __init__(self, body, content_type):
        self.body, self.content_type = body, content_type


def make_handler(store: Store, token: str, google=None, google_error=None, writer=None,
                 modules=None, stopping=None):
    supplied_runtime = modules is None and any(value is not None for value in (google, google_error, writer))
    modules = modules if modules is not None else ModuleHost(store.file)
    services = modules.services or ModuleServices(store, modules)
    modules.bind_services(services)
    if supplied_runtime and modules.builtin_active(WORK_CALENDAR):
        backend = modules.backends.get(WORK_CALENDAR)
        if backend is not None:
            try:
                dependencies = modules._active['modules'][WORK_CALENDAR].get('requires', [])
                if any(services.feature(ident) is None for ident in dependencies):
                    raise ModuleError('호출자 runtime 연결 전에 의존 모듈의 기동이 필요합니다')
                attach = getattr(backend, 'use_runtime', None)
                if not callable(attach):
                    raise ModuleError('이 업무 패키지는 호출자 runtime 연결을 지원하지 않습니다')
                attach(google, google_error, writer)
                modules.started_backends.add(WORK_CALENDAR)
            except Exception as exc:
                modules.errors[WORK_CALENDAR] = 'supplied runtime failed: ' + str(exc)
    stopping = stopping if stopping is not None else threading.Event()

    class Handler(BaseHTTPRequestHandler):
        server_version = f'wsFlow/{__version__}'
        LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1', '[::1]')

        def _json(self, obj, status: int = 200):
                    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)

        def _asset(self, response: AssetResponse):
                    self.send_response(200)
                    self.send_header("Content-Type", response.content_type)
                    self.send_header("Content-Length", str(len(response.body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(response.body)

        def _body(self) -> dict:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n == 0:
                        return {}
                    try:
                        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                    except json.JSONDecodeError:
                        raise ApiError(400, "본문이 JSON 이 아닙니다")

        def log_message(self, fmt, *args):  # 조용히
                    if "--verbose" in sys.argv:
                        super().log_message(fmt, *args)

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

        def _modules_v1(self, method, rest, query, body):
                    if not rest and method == "GET":
                        return modules.snapshot()
                    if stopping.is_set():
                        raise ApiError(503, "서버가 종료 중입니다. 다음 기동 후 다시 시도하십시오")
                    if rest == ["plan"] and method == "POST":
                        if not isinstance(body, dict):
                            raise ApiError(400, "모듈 계획은 JSON 객체입니다")
                        return modules.plan(body)
                    if rest == ["apply"] and method == "POST":
                        if not isinstance(body, dict) or set(body) != {"plan_id"}:
                            raise ApiError(400, "서버가 발행한 plan_id만 제출하십시오")
                        return modules.apply(body["plan_id"])
                    if len(rest) >= 2 and rest[1] == "api":
                        return modules.extension_request(method, rest[0], rest[2:], query, body)
                    if len(rest) >= 3 and rest[1] == "assets" and method == "GET":
                        raw, content_type = modules.asset(rest[0], "/".join(rest[2:]))
                        return AssetResponse(raw, content_type)
                    raise ApiError(404, "지원하지 않는 모듈 API입니다")

        def _project(self, project_id):
            return services.project(project_id)

        def _dispatch(self, method):
            url = urlparse(self.path)
            try:
                self._guard(url)
                if not url.path.startswith('/api/'):
                    return self._static(url.path)
                result = self._route(method, url.path, parse_qs(url.query))
                if isinstance(result, AssetResponse):
                    return self._asset(result)
                self._json(result)
            except ApiError as exc:
                self._json({'error': str(exc), **exc.extra}, exc.status)
            except ModuleError as exc:
                self._json({'error': str(exc)}, exc.status)
            except ActivityUnavailable as exc:
                self._json({'error': '호스트 업무 작업이 일시 중지되었습니다. 상태를 확인한 뒤 다시 시도하십시오',
                            'runtime_reason': exc.reason}, 503)
            except (operations.StateConflict, fileio.LockBusy) as exc:
                self._json({'error': str(exc)}, 409)
            except (ValueError, KeyError) as exc:
                self._json({'error': str(exc)}, 400)
            except Exception as exc:
                self._json({'error': f'{type(exc).__name__}: {exc}',
                            **{key:getattr(exc,key) for key in ('plan_id','operation_id') if hasattr(exc,key)}}, 500)

        def _route(self, method, path, query):
            segments = [s for s in path.split('/') if s][1:]
            control_read = method == 'GET' and (segments in (['config'], ['v1','modules']) or
                (len(segments) >= 5 and segments[:2] == ['v1','modules'] and segments[3] == 'assets'))
            if control_read:
                return self._route_admitted(method, path, query)
            with services.activity():
                return self._route_admitted(method, path, query)

        def _route_admitted(self, method, path, query):
            segments = [s for s in path.split('/') if s][1:]
            body = self._body() if method in ('POST','PUT','PATCH','DELETE') else {}
            if segments[:2] == ['v1','modules']:
                return self._modules_v1(method, segments[2:], query, body)
            owner = route_owner(method, path)
            if owner is not None:
                if stopping.is_set():
                    raise ApiError(503, '서버가 종료 중입니다. 새 모듈 작업은 시작하지 않습니다', {'module_id':owner})
                if owner in modules.errors:
                    raise ApiError(503, '이 업무 패키지의 기동을 확인할 수 없습니다', {'module_id':owner})
                if not modules.builtin_active(owner):
                    raise ApiError(409, '이 통합 기능 모듈이 비활성화되었거나 제거되었습니다', {'module_id':owner,'module_state':'inactive'})
                backend = services.feature(owner)
                if backend is None:
                    raise ApiError(503, '업무 패키지가 준비되지 않았습니다', {'module_id':owner})
                return backend.dispatch(self, method, path, query, body)
            if segments == ['config'] and method == 'GET':
                return {'version':__version__, 'roots':store.roots(), 'store':str(store.file)}
            if segments == ['projects'] and method == 'GET':
                return {'projects':[services.summary(project) for project in store.projects()]}
            if len(segments)==2 and segments[0]=='projects' and method=='GET':
                return services.summary(self._project(segments[1]))
            if segments == ['pick-folder'] and method == 'POST':
                try:
                    selected = pickdir.pick_folder(body.get('initial') or None)
                except RuntimeError as exc:
                    raise ApiError(500, str(exc)) from exc
                return {'path':selected, 'cancelled':selected is None}
            if segments == ['pick-files'] and method == 'POST':
                try:
                    selected = pickdir.pick_files(body.get('initial') or None)
                except RuntimeError as exc:
                    raise ApiError(500, str(exc)) from exc
                return {'paths':selected, 'cancelled':not selected}
            raise ApiError(404, f'no route: {method} {path}')

    return Handler


def serve(store_file: str | Path, host: str = '127.0.0.1', port: int = 8765,
          open_browser: bool = False, token: str | None = None, google_transport=None) -> ThreadingHTTPServer:
    store = Store(store_file)
    modules = ModuleHost(store.file)
    services = ModuleServices(store, modules, google_transport)
    modules.bind_services(services)
    stopping = threading.Event()
    token = token or secrets.token_urlsafe(24)

    class Server(ThreadingHTTPServer):
        def server_close(self):
            stopping.set()
            try:
                modules.close()
                participants = set(modules.backends) | set(modules.extensions) | set(modules.stop_results)
                self.module_shutdown = {ident:{'workers_stopped':modules.stop_results.get(ident,False)} for ident in participants}
                external = [modules.stop_results.get(ident, False) for ident in modules.extensions]
                self.module_shutdown['extensions'] = {'workers_stopped':all(external)}
            finally:
                super().server_close()

    httpd = Server((host, port), make_handler(store, token, modules=modules, stopping=stopping))
    httpd.module_host = modules
    httpd.module_shutdown = {}
    modules.start_backends()
    work = modules.backends.get(WORK_CALENDAR)
    httpd.google_calendar = getattr(work, 'google', None)
    httpd.calendar_writer = getattr(work, 'writer', None)
    httpd.daemon_threads = True
    httpd.wsflow_token = token
    if open_browser:
        import webbrowser
        url = f'http://{host}:{httpd.server_address[1]}/?t={token}'
        threading.Timer(0.5, lambda:webbrowser.open(url)).start()
    return httpd
