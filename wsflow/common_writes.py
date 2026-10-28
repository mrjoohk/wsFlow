"""Server-owned Git/memo plans and bounded, explicit result recovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

from . import fileio, operations
from .write_context import binding


def sha(raw):return hashlib.sha256(raw).hexdigest()


class Service:
    def __init__(self, project, kind, home=None, git_adapter=None, memo_adapter=None):
        if kind not in ('git','memo'):raise ValueError('지원하지 않는 쓰기 종류입니다')
        if kind=='git' and git_adapter is None:
            from . import gitops as git_adapter
        if kind=='memo' and memo_adapter is None:
            from . import wfhome as memo_adapter
        self.git=git_adapter;self.memo=memo_adapter
        self.project=project;self.kind=kind
        self.home=Path(home if kind=='memo' else project['path']).resolve()
        self.store=operations.Store(self.home)
        self.binding={**binding(project,self.home),'write_kind':kind}
        self.lock=self.home if kind=='memo' else self.store.path.parent/'common-locks'/operations.digest(str(self.home))

    def _payload(self,action,payload):
        allowed={'add':{'paths','all'},'commit':{'message','add_all'},'push':{'remote','branch','set_upstream'},
                 'fetch':set(),'pull':set(),'remote_add':{'name','url'},'remote_set':{'name','url'},
                 'remote_remove':{'name'},'clean_locks':set()} if self.kind=='git' else {'save':{'ymd','text','sha','document_id'}}
        if action not in allowed or not isinstance(payload,dict) or set(payload)-allowed[action]:
            raise ValueError('지원하지 않는 작업 또는 payload 필드입니다')
        p=dict(payload)
        if self.kind=='git' and action!='clean_locks' and self.git.repo_kind(self.home)!='repo':
            raise ValueError('유효한 Git 저장소가 필요합니다')
        for key in ('all','add_all','set_upstream'):
            if key in p and type(p[key]) is not bool:raise ValueError('명시적인 bool 옵션이 필요합니다')
        if self.kind=='memo':
            if set(p)!=allowed[action] or any(not isinstance(v,str) for v in p.values()):raise ValueError('메모 문서·날짜·본문·SHA가 필요합니다')
            self.memo.daily_path(self.home,p['ymd'])
        if action=='commit' and (not isinstance(p.get('message'),str) or not p['message'].strip()):raise ValueError('커밋 메시지가 필요합니다')
        if action=='add' and not p.get('all'):
            if not isinstance(p.get('paths'),list) or not p['paths']:raise ValueError('스테이징할 상대 경로가 필요합니다')
            for name in p['paths']:
                if not isinstance(name,str) or not name or name.startswith('-') or Path(name).is_absolute() or '..' in Path(name).parts:
                    raise ValueError('잘못된 Git 상대 경로입니다')
                fileio.within(self.home,self.home/name)
        if action in ('push','remote_add','remote_set','remote_remove'):
            key='remote' if action=='push' else 'name';p.setdefault(key,'origin')
            if not isinstance(p[key],str) or not p[key] or p[key].startswith('-'):raise ValueError('잘못된 remote 이름입니다')
        if action in ('remote_add','remote_set') and (not isinstance(p.get('url'),str) or not p['url'] or p['url'].startswith('-')):
            raise ValueError('remote URL이 필요합니다')
        if action=='push':
            p['branch']=p.get('branch') or self.git.run_git(self.home,'symbolic-ref','--quiet','--short','HEAD').stdout.strip()
            if not isinstance(p['branch'],str) or ':' in p['branch'] or p['branch'].startswith('-'):
                raise ValueError('push는 명시한 단일 로컬 브랜치를 사용합니다')
            self.git.run_git(self.home,'check-ref-format','--branch',p['branch'])
        return p

    def _git_source(self,action=None):
        def read(*args):return self.git.run_git(self.home,*args,check=args[0] not in ('rev-parse','show-ref')).stdout
        if action=='clean_locks':
            gitdir=self.git.git_dir(self.home) if self.git.repo_kind(self.home)=='repo' else self.home/'.git'
            return dict(git_dir=str(gitdir.resolve()),locks={l['path']:[Path(l['path']).stat().st_mtime_ns,sha(Path(l['path']).read_bytes())] for l in self.git.find_locks(self.home)})
        if self.git.repo_kind(self.home)!='repo':
            return dict(broken=True,git_dir=str((self.home/'.git').resolve()),locks={l['path']:[Path(l['path']).stat().st_mtime_ns,sha(Path(l['path']).read_bytes())] for l in self.git.find_locks(self.home)})
        gitdir=self.git.git_dir(self.home).resolve()
        files={}
        names=read('ls-files','-z','--cached','--others','--exclude-standard').split('\0')
        for name in sorted(set(names)-{''}):
            path=self.home/name
            if path.is_symlink():files[name]=['link',os.readlink(path)]
            else:
                fileio.within(self.home,path)
                if not path.exists():files[name]=None
                elif not path.is_file():raise operations.StateConflict('Git 하위 모듈/특수 경로는 이 계획에서 지원하지 않습니다')
                else:
                    digest=hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
                    files[name]=[digest.hexdigest(),path.stat().st_mode]
        locks={}
        for lock in self.git.find_locks(self.home):
            path=Path(lock['path']);locks[str(path)]=[path.stat().st_mtime_ns,sha(path.read_bytes())]
        return dict(git_dir=str(gitdir),head=read('rev-parse','--verify','HEAD').strip(),
                    refs=sha(read('show-ref').encode()),index=sha((gitdir/'index').read_bytes()) if (gitdir/'index').exists() else None,
                    config=sha(read('config','--null','--list','--show-origin').encode()),files=files,locks=locks)

    def _memo_source(self,payload):
        if self.memo.read_state(self.home).get('schema_version')!=self.memo.SCHEMA_VERSION:raise operations.StateConflict('지원하지 않는 업무 schema입니다')
        path=fileio.within(self.home,self.memo.daily_path(self.home,payload['ymd']))
        raw=path.read_bytes();head,memo,tail=self.memo._split(raw.decode('utf-8'))
        if payload['sha']!=self.memo.sha(memo) or payload['document_id']!=self.memo.memo_document_id(self.home,payload['ymd']):
            raise self.memo.MemoConflict(memo,self.memo.sha(memo))
        text=payload['text'];text=text if text.startswith('\n') else '\n'+text
        if not text.endswith('\n'):text+='\n'
        return dict(state=operations.digest(self.memo.state_path(self.home).read_bytes().hex()),before_sha256=sha(raw),before_hex=raw.hex(),
                    target_sha256=sha((head+text+tail).encode('utf-8')))

    def _resource(self,payload):return 'git' if self.kind=='git' else payload['ymd']

    def _pending(self,resource,exclude=None):
        for op in self.store.list():
            if (op.get('kind')=='local_write' and op.get('write_kind')==self.kind and op.get('resource')==resource
                    and op['operation_id']!=exclude and op['status'] in ('executing','uncertain')):
                raise operations.StateConflict('미확인 작업 '+op['operation_id']+'의 결과를 먼저 확인하십시오')

    def preview(self,action,payload):
        with fileio.locked(self.lock):
            observed_at=time.time()
            payload=self._payload(action,payload);resource=self._resource(payload);self._pending(resource)
            source=self._git_source(action) if self.kind=='git' else self._memo_source(payload)
            target=None
            if action=='push':
                ref='refs/heads/'+payload['branch']
                target=dict(urls=self.git.run_git(self.home,'remote','get-url','--push','--all',payload['remote']).stdout.splitlines(),
                            ref=ref,oid=self.git.run_git(self.home,'rev-parse','--verify',ref+'^{commit}').stdout.strip())
            plan=self.store.save_plan(dict(kind='local_write',write_kind=self.kind,binding=self.binding,
                source=source,action=action,payload=payload,target=target,resource=resource,observed_at=observed_at),
                [dict(kind='local_write',write_kind=self.kind,action=action,resource=resource)])
            return {k:plan[k] for k in ('plan_id','expires_at','action','payload')} | {'operation_id':plan['items'][0]['operation_id']}

    def _selected(self,plan_id,ident):
        if not isinstance(plan_id,str) or not isinstance(ident,str) or not plan_id or not ident:
            raise operations.StateConflict('문자열 계획·작업 ID가 필요합니다')
        plan=self.store.plan(plan_id);op=self.store.get(ident)
        if (plan.get('kind')!='local_write' or plan.get('binding')!=self.binding or len(plan['items'])!=1
                or plan['items'][0]['operation_id']!=ident or not op or op.get('plan_id')!=plan_id or op.get('write_kind')!=self.kind):
            raise operations.StateConflict('계획의 종류·프로젝트·경로·작업 ID가 다릅니다')
        return plan,op

    def _paths(self,ident):
        folder=self.store.path.parent/'common-receipts'/ident
        return fileio.within(self.store.path.parent,folder/'receipt.json'),fileio.within(self.store.path.parent,folder/'before.bin')

    def _execute(self,plan):
        p=plan['payload'];action=plan['action']
        if self.kind=='memo':return self.memo.write_memo(self.home,p['ymd'],p['text'],p['sha'],p['document_id'])
        if action=='fetch':return self.git.fetch(self.home)
        if action=='pull':return self.git.pull_ff(self.home)
        if action=='push':
            ref=plan['target']['ref']
            args=['push','--no-mirror','--no-follow-tags','--recurse-submodules=no']
            if p.get('set_upstream'):args.append('-u')
            args.extend([p['remote'],ref+':'+ref])
            cp=self.git.run_git(self.home,*args,check=False)
            return dict(ok=cp.returncode==0,returncode=cp.returncode,stdout=cp.stdout,stderr=cp.stderr,command='git '+' '.join(args))
        if action=='commit':return self.git.commit(self.home,p['message'],p.get('add_all',False))
        if action=='add':self.git.add(self.home,p.get('paths'),p.get('all',False));return self.git.status(self.home)
        if action=='remote_add':self.git.remote_add(self.home,p['name'],p['url'])
        elif action=='remote_set':self.git.remote_set_url(self.home,p['name'],p['url'])
        elif action=='remote_remove':self.git.remote_remove(self.home,p['name'])
        elif action=='clean_locks':return self.git.clean_locks(self.home,min_age_sec=5+max(0,time.time()-plan['observed_at']))
        return {'remotes':self.git.remotes(self.home)}

    def _result(self,plan,op,reused=False):
        if self.kind=='memo':
            path=fileio.within(self.home,self.memo.daily_path(self.home,plan['payload']['ymd']))
            if not path.is_file():raise operations.StateConflict('저장 이후 문서가 사라졌습니다 — 자동 복원하지 않습니다')
            raw=path.read_bytes()
            if sha(raw)!=plan['source']['target_sha256']:
                _,memo,_=self.memo._split(raw.decode('utf-8'))
                raise self.memo.MemoConflict(memo,self.memo.sha(memo))
        return {**op['result'],'plan_id':plan['plan_id'],'operation_id':op['operation_id'],'status':op['status'],'reused':reused}

    def apply(self,plan_id,ident):
        with fileio.locked(self.lock):
            plan,op=self._selected(plan_id,ident)
            if op['status']=='succeeded':return self._result(plan,op,True)
            if op['status']=='failed':raise operations.StateConflict('실행하지 않거나 실패한 작업입니다 — 새 계획을 요청하십시오')
            if op['status'] in ('executing','uncertain'):raise operations.StateConflict('작업을 재전송하지 않습니다 — 같은 ID의 결과를 확인하십시오')
            try:
                self._pending(plan['resource'],ident)
                if time.time()>plan['expires_at']:raise operations.StateConflict('계획이 만료됐습니다')
                current=self._git_source(plan['action']) if self.kind=='git' else self._memo_source(plan['payload'])
                if current!=plan['source']:raise operations.StateConflict('계획 이후 원본 버전이 바뀌었습니다')
            except Exception as exc:
                self.store.reject_unexecuted(ident,str(exc))
                exc.plan_id=plan_id;exc.operation_id=ident
                raise
            self.store.approve(ident);self.store.claim(ident)
            receipt,backup=self._paths(ident);started=False
            try:
                if self.kind=='memo':
                    raw=bytes.fromhex(plan['source']['before_hex'])
                    if backup.exists():
                        if backup.read_bytes()!=raw:raise operations.StateConflict('작업 원본 백업이 바뀌었습니다')
                    else:fileio.atomic_write(backup,raw)
                started=True;result=self._execute(plan)
                outcome='uncertain' if result.get('ok') is False and plan['action']=='push' else 'failed' if result.get('ok') is False else 'succeeded'
                saved=dict(plan_id=plan_id,operation_id=ident,binding=self.binding,status=outcome,result=result)
                saved['sha256']=operations.digest(saved)
                fileio.atomic_write(receipt,operations.encoded(saved).encode('utf-8'))
                self.store.finish(ident,outcome,result)
                return self._result(plan,self.store.get(ident)) if outcome=='succeeded' else {**result,'plan_id':plan_id,'operation_id':ident,'status':outcome}
            except Exception as exc:
                exc.plan_id=plan_id;exc.operation_id=ident
                safe_failure=not started
                if started and self.kind=='git' and plan['action']!='push' and isinstance(exc,self.git.GitError) and exc.returncode!=124:
                    try:safe_failure=self._git_source(plan['action'])==plan['source']
                    except Exception:pass
                try:self.store.finish(ident,'failed' if safe_failure else 'uncertain',error=str(exc))
                except Exception:pass  # Executing remains durable if the result DB is unavailable.
                raise

    def status(self,ident):
        op=self.store.get(ident)
        if not op or not op.get('plan_id'):raise operations.StateConflict('작업을 찾을 수 없습니다')
        plan,op=self._selected(op['plan_id'],ident);_,backup=self._paths(ident)
        return dict(plan_id=plan['plan_id'],operation_id=ident,status=op['status'],action=plan['action'],
                    ymd=plan['payload'].get('ymd'),result=op.get('result'),error=op.get('error'),before_backup=str(backup),backup_exists=backup.is_file())

    def list(self):
        rows=[]
        for op in self.store.list():
            if op.get('kind')=='local_write' and op.get('write_kind')==self.kind:
                plan=self.store.plan(op['plan_id'])
                if plan.get('binding')==self.binding:rows.append(self.status(op['operation_id']))
        return rows

    def check(self,ident):
        with fileio.locked(self.lock):
            observed=self.status(ident);plan,op=self._selected(observed['plan_id'],ident)
            if op['status']=='succeeded':return {**observed,'result':self._result(plan,op,True),'reconciled':False}
            if op['status'] in ('planned','approved'):
                self.store.reject_unexecuted(ident,'실행되지 않은 계획을 종료했습니다 — 입력을 유지하고 새 계획을 요청하십시오')
                return self.status(ident)
            if op['status'] not in ('executing','uncertain'):return observed
            receipt,backup=self._paths(ident);result=None
            if receipt.is_file():
                saved=json.loads(receipt.read_text(encoding='utf-8'))
                checksum=saved.pop('sha256',None)
                if checksum!=operations.digest(saved):raise operations.StateConflict('작업 receipt가 손상됐습니다')
                if (saved.get('plan_id')!=plan['plan_id'] or saved.get('operation_id')!=ident or saved.get('binding')!=self.binding):
                    raise operations.StateConflict('작업 결과 receipt가 다릅니다')
                if saved['status']=='succeeded':result=saved['result']
            if result is None and self.kind=='memo':
                path=fileio.within(self.home,self.memo.daily_path(self.home,plan['payload']['ymd']))
                if backup.is_file() and sha(backup.read_bytes())==plan['source']['before_sha256'] and path.is_file() and sha(path.read_bytes())==plan['source']['target_sha256']:
                    _,memo,_=self.memo._split(path.read_bytes().decode('utf-8'));ymd=plan['payload']['ymd']
                    result=dict(path=str(path),text=memo,sha=self.memo.sha(memo),parsed=self.memo.parse_memo(memo),ymd=ymd,
                                document_id=self.memo.memo_document_id(self.home,ymd),backup=str(backup),backup_format='full_daily')
            if result is None and plan['action']=='push':
                target=plan['target'];p=plan['payload']
                urls=self.git.run_git(self.home,'remote','get-url','--push','--all',p['remote']).stdout.splitlines()
                remotes=[]
                if urls==target['urls']:
                    for url in urls:
                        cp=self.git.run_git(self.home,'ls-remote','--refs',url,target['ref'],check=False)
                        if cp.returncode or cp.stdout.split()!=[target['oid'],target['ref']]:break
                        remotes.append(cp.stdout)
                upstream=self.git.run_git(self.home,'rev-parse','--abbrev-ref',p['branch']+'@{upstream}',check=False).stdout.strip()
                if urls and len(remotes)==len(target['urls']) and (not p.get('set_upstream') or upstream==p['remote']+'/'+p['branch']):
                    result=dict(ok=True,command='git push (all targets readback verified)',stdout=''.join(remotes),stderr='')
            if result is None:return observed
            self.store.finish(ident,'succeeded',result)
            return {**self.status(ident),'result':self._result(plan,self.store.get(ident),True),'reconciled':True}

    def legacy(self,action,payload):
        plan=self.preview(action,payload)
        return {**self.apply(plan['plan_id'],plan['operation_id']),'legacy_single_request':True}
