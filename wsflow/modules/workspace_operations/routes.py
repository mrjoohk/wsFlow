"""Workspace HTTP implementation, loaded through the business-module backend."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from wsflow import operations
from wsflow.builtin_modules import WORKSPACE_OPERATIONS
from wsflow.module_services import ApiError
from . import context_inventory, context_projection, deploy, gitops, scanner, session_prep


class Routes:
    """Per-request adapter; request objects are never stored in a shared backend."""
    def __init__(self, request, services, runtime=None):
        self.request, self.services, self.runtime = request, services, runtime

    def _project(self, pid):
        return self.request._project(pid)

    def _repo(self, pid):
        project = self._project(pid)
        kind = gitops.repo_kind(project['path'])
        if kind != 'repo':
            raise ApiError(409, 'git 저장소가 아닙니다' if kind == 'none' else '.git 이 손상되었습니다(빈 디렉터리 등)', {'git': kind})
        return project['path']

    def _writes(self, project, kind='git', home=None):
        from wsflow import common_writes
        return common_writes.Service(project, kind, home, git_adapter=gitops)

    def dispatch(self, method, path, query, body):
        seg = [part for part in path.split('/') if part][1:]
        if seg[:1] == ['v1']:
            rest = seg[1:]
            if len(rest) >= 4 and rest[0] == 'projects' and rest[2] == 'writes' and rest[3] == 'git':
                return self._common_v1(method, rest, body)
            if 'session' in rest or rest == ['context', 'dashboard']:
                return self._session_v1(method, rest, body)
            if 'context' in rest and (method == 'POST' or rest[-1] == 'setup' or 'operations' in rest):
                return self._context_v1(method, rest, query, body)
            return self._read_v1(method, rest, query)
        return self._general(method, path, seg, query, body)

    def _general(self, method, path, seg, query, body):
        store = self.services.store
        if seg == ['roots']:
            if method == 'POST':
                return store.add_root(body.get('path', ''), body.get('template'))
            if method == 'DELETE':
                return {'removed': store.remove_root(body.get('path', ''))}
        if seg == ['projects', 'new'] and method == 'POST':
            return store.create_project(body.get('root', ''), body.get('name', ''))
        if seg[:1] == ['deploy'] and len(seg) == 2 and method == 'POST':
            sources = [str(x) for x in (body.get('sources') or []) if str(x).strip()]
            ids = body.get('ids') or []
            if not sources:
                raise ApiError(400, '배포 원본이 없습니다')
            if not ids:
                raise ApiError(400, '대상 프로젝트를 선택하세요')
            targets = [self._project(ident) for ident in ids]
            missing = [project['alias'] for project in targets if not Path(project['path']).is_dir()]
            if missing:
                raise ApiError(400, f"경로가 없는 프로젝트: {', '.join(missing)}")
            fn = deploy.plan if seg[1] == 'plan' else deploy.apply if seg[1] == 'apply' else None
            if fn is None:
                raise ApiError(404, f'no route: {method} {path}')
            return fn(sources, targets, body.get('dest_rel') or '', store.template_dir_for)
        if seg == ['scan'] and method == 'GET':
            root = (query.get('root') or [''])[0]
            if not root or not Path(root).is_dir():
                raise ApiError(400, 'root 가 디렉터리가 아닙니다')
            registered = {project['path'] for project in store.projects()}
            return {'root': root, 'items': scanner.scan_root(root, registered)}
        if seg == ['projects'] and method == 'POST':
            return store.add_project(body.get('path', ''), body.get('alias'), body.get('tags'))
        if seg == ['locks'] and method == 'GET':
            return {'locks': [{'id': project['id'], 'alias': project['alias'], 'locks': gitops.find_locks(project['path'])}
                              for project in store.projects() if Path(project['path'], '.git').exists()]}
        if seg == ['locks', 'clean'] and method == 'POST':
            selected = set(body.get('ids') or [])
            result = []
            for project in store.projects():
                if selected and project['id'] not in selected:
                    continue
                if not Path(project['path'], '.git').exists():
                    continue
                result.append({'id': project['id'], **self._writes(project).legacy('clean_locks', {})})
            return {'results': result}
        if len(seg) >= 2 and seg[0] == 'projects':
            pid, rest = seg[1], seg[2:]
            if not rest:
                if method == 'PATCH':
                    self._project(pid)
                    return store.update_project(pid, body.get('alias'), body.get('tags'))
                if method == 'DELETE':
                    return {'removed': store.remove_project(pid)}
                if method == 'GET':
                    return self._summary(self._project(pid))
            return self._project_route(method, pid, rest, query, body)
        raise ApiError(404, f'no route: {method} {path}')

    def _session_v1(self, method, seg, body):
        store = self.services.store
        service = session_prep.Service(store)
        if len(seg) == 4 and seg[0] == 'projects' and seg[2] == 'session':
            self._project(seg[1]); action = seg[3]
            if action == 'status' and method == 'GET':
                return service.status(seg[1])
            if action == 'prepare' and method == 'POST':
                return service.prepare(seg[1], body)
            if action == 'evidence' and method == 'POST':
                if not isinstance(body, dict) or set(body) != {'content'}:
                    raise ApiError(400, '파일 경로 대신 evidence content만 제출하십시오')
                return service.upload(body['content'])
            if action == 'observations' and method == 'POST':
                return service.observe(seg[1], body)
        if seg == ['context', 'dashboard'] and method == 'GET':
            day = datetime.now().astimezone().date().isoformat()
            work = self.services.feature('work-calendar')
            if work is None:
                calendar = {'state': 'module_inactive', 'warnings': ['업무·일정 통합 모듈이 비활성 상태입니다'],
                            'outbox_counts': {}, 'last_sync': None}
                workflow = {'module_inactive': True, 'projects': [], 'date': day}
            else:
                try:
                    calendar = work.calendar_status()
                except Exception:
                    calendar = {'state': 'unknown', 'warnings': ['캘린더 상태 조회 실패 — 캘린더에서 다시 확인하십시오'],
                                'outbox_counts': {}, 'last_sync': None}
                workflow = work.workflow_status(day)
            result = service.dashboard(calendar=calendar)
            result['workflow'] = workflow
            return result
        raise ApiError(404, '지원하지 않는 session API입니다')

    def _read_v1(self, method, seg, query):
        if method != 'GET':
            raise ApiError(404, '읽기 전용 API입니다')
        registry = self.services.store.snapshot()
        if len(seg) == 3 and seg[0] == 'projects' and seg[2] == 'context':
            project = next((project for project in registry['projects'] if project['id'] == seg[1]), None)
            if project is None:
                raise ApiError(404, '등록되지 않은 프로젝트입니다')
            return context_inventory.inspect(project, registry['roots'], registry['projects'])
        raise ApiError(404, 'no v1 route')

    def _common_v1(self, method, seg, body):
        project = self._project(seg[1])
        if seg[3] != 'git':
            raise ApiError(400, '지원하지 않는 쓰기 종류입니다')
        service = self._writes(project)
        rest = seg[4:]
        if rest == ['plan'] and method == 'POST':
            if not isinstance(body, dict) or set(body) != {'action', 'payload'}:
                raise ApiError(400, 'action/payload만 제출하십시오')
            return service.preview(body['action'], body['payload'])
        if rest == ['apply'] and method == 'POST':
            if not isinstance(body, dict) or set(body) != {'plan_id', 'operation_id'}:
                raise ApiError(400, '서버 계획·작업 ID만 제출하십시오')
            return service.apply(body['plan_id'], body['operation_id'])
        if rest == ['operations'] and method == 'GET':
            return {'operations': service.list()}
        if len(rest) == 2 and rest[0] == 'operations':
            if method == 'GET':
                return service.status(rest[1])
            if method == 'POST' and body == {}:
                return service.check(rest[1])
        raise ApiError(404, '지원하지 않는 공통 쓰기 API입니다')

    def _context_v1(self, method, seg, query, body):
        store = self.services.store
        modules = self.services.module_host
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


    def _summary(self, pr: dict) -> dict:
        store = self.services.store
        modules = self.services.module_host
        exists = Path(pr["path"]).is_dir()
        if not modules.builtin_active(WORKSPACE_OPERATIONS):
            return {**pr, "exists": exists, "git": "unavailable", "branch": None,
                    "ahead": 0, "behind": 0, "dirty": 0, "has_remote": False,
                    "locks": 0, "parked": False, "workspace_module_inactive": True}
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
        store = self.services.store
        modules = self.services.module_host
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
                return self._writes(pr,'git').legacy('clean_locks',{})

        repo = self._repo(pid)
        if r0 in ('fetch','pull') and len(rest)==1 and m=='POST':
            if body != {}:raise ApiError(400,'Git 동기화는 옵션 없이 고정 명령으로 실행합니다')
            return self._writes(self._project(pid),'git').legacy(r0,{})
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
                    return self._writes(self._project(pid),'git').legacy('remote_add',body)
            elif len(rest) == 2:
                if m == "PUT":
                    return self._writes(self._project(pid),'git').legacy('remote_set',{**body,'name':rest[1]})
                if m == "DELETE":
                    return self._writes(self._project(pid),'git').legacy('remote_remove',{'name':rest[1]})
        if r0 == "push" and m == "POST":
            return self._writes(self._project(pid),'git').legacy('push',body)
        if r0 == "add" and m == "POST":
            return self._writes(self._project(pid),'git').legacy('add',body)
        if r0 == "commit" and len(rest) == 1 and m == "POST":
            if not body.get('message','').strip():raise gitops.GitError('커밋 메시지가 비어 있습니다')
            return self._writes(self._project(pid),'git').legacy('commit',body)
        raise ApiError(404, f"no route: {m} /api/projects/{pid}/{'/'.join(rest)}")


