"""I6 static preparation and explicitly submitted, scoped session observations.

No agent, skill, evidence instruction or shell command is executed. Receipts
describe submitted evidence; they are not independent attestations of a tool.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import json
from pathlib import Path
import platform
import re
import sqlite3
from uuid import uuid4

from . import context_projection as projection, context_skills as skills
from wsflow import fileio, operations
from wsflow.writer_admission import writer_method
from wsflow.readmodel import Reader

TOOLS = {'codex':('AGENTS.md','.agents/skills'), 'claude':('CLAUDE.md','.claude/skills')}
KINDS = {'unknown','static_only','agent_reported','tool_observed'}
MAX_EVIDENCE = 256 * 1024


def runtime():
    return {'host':platform.node(), 'os':platform.platform()}


def timestamp(value):
    if not isinstance(value,str):raise ValueError('관측 시각은 offset을 가진 ISO 문자열입니다')
    try:result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ValueError('관측 시각이 올바르지 않습니다') from None
    if result.tzinfo is None:raise ValueError('관측 시각에 timezone offset이 필요합니다')
    return result.astimezone(timezone.utc)


def bounded_text(value,limit,name):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or any(c in value for c in '\x00\r\n'):
        raise ValueError(name+' 형식이 올바르지 않습니다')
    return value.strip()


def _storage_checked(function):
    """Reject invalid storage before admission can create its control files."""
    @wraps(function)
    def invoke(self,*args,**kwargs):
        self._storage()
        return function(self,*args,**kwargs)
    return invoke


class Service:
    def __init__(self, registry):
        self.registry=registry
        self.context=projection.Service(registry)
        self.ops=operations.Store(str(registry.file.resolve())+'.sessions')
        self._writer_domain = self.ops._writer_domain
        self.evidence_dir=self.ops.path.parent.parent/'session-evidence'/operations.digest(str(registry.file.resolve()))

    def _storage(self):
        base=self.ops.path.parent.parent.resolve()
        for entry in [*self.registry.snapshot()['roots'],*self.registry.snapshot()['projects']]:
            if base.is_relative_to(Path(entry['path']).resolve()):
                raise operations.StateConflict('세션 자료는 사용자 ROOT/프로젝트 밖에 저장해야 합니다')
        skills.plain_path(base,Path(base.anchor))

    def _plans(self,kind):
        with self.ops.connection() as db:
            rows=db.execute('SELECT snapshot FROM plans ORDER BY rowid').fetchall() if db else []
        return [value for row in rows if (value:=json.loads(row[0])).get('kind')==kind]

    def _packet(self,pid,ident):
        if not isinstance(ident,str) or not re.fullmatch(r'P-[0-9a-f]{32}',ident):raise ValueError('서버 packet ID만 허용합니다')
        packet=self.ops.plan(ident)
        if packet.get('kind')!='session_packet' or packet.get('project_id')!=pid:
            raise operations.StateConflict('해당 프로젝트의 세션 준비 패킷이 아닙니다')
        return packet

    def inspect(self,pid,tool):
        """Read current scoped sources and managed projections without writing files."""
        if not isinstance(tool,str) or tool not in TOOLS:raise ValueError('지원하지 않는 도구입니다')
        blockers=[];warnings=[];documents=[];expected_skills=[];observations=[]
        result={'schema_version':1,'project_id':pid,'tool':tool,'cwd':None,'root_uid':None,'project_uid':None,
                'impact_project_ids':[],'scope_complete':False,'unknown_scope':['user_global','plugins','semantic_compliance','tool_loading']}
        try:
            registry,root,checkout=self.context._binding(pid)
            result.update(cwd=str(checkout),root_path=str(root),impact_project_ids=[p['id'] for p in registry['projects'] if Path(p['path']).resolve().is_relative_to(root)])
            paths=[root/'GLOBAL_RULES.md',root/'.wsflow/workspace.json',root/'.wsflow/manifest.json']
            reader=Reader([checkout,root/'skills'],paths,[Path(p['path']) for p in registry['projects'] if p['id']!=pid and not checkout.is_relative_to(Path(p['path']))])
            def read(path,ident,owner,document=False):
                skills.plain_path(path,owner)
                meta,raw=reader.read(path,ident,'common' if owner==root else 'project',boundary=owner)
                if meta['status']!='ok':blockers.append('source_unavailable:'+ident)
                if document and raw is not None:
                    raw.decode('utf-8-sig')
                    documents.append({'id':ident,'path':str(path),'resolved_path':meta['resolved_path'],'sha256':meta['sha256']})
                return raw
            def profile(path,ident,owner):
                raw=read(path,ident,owner)
                value=json.loads(raw.decode('utf-8-sig')) if raw else {}
                if not isinstance(value,dict) or value.get('schema_version')!=1:raise ValueError('profile_not_initialized:'+ident)
                return value
            rp=profile(root/'.wsflow/workspace.json','root:profile',root)
            pp=profile(checkout/'.wsflow/project.json','project:profile',checkout)
            result.update(root_uid=rp.get('root_uid'),project_uid=pp.get('project_uid'))
            if not rp.get('root_uid') or not pp.get('project_uid'):blockers.append('stable_uid_missing')
            if pp.get('root_uid')!=rp.get('root_uid'):blockers.append('root_uid_mismatch')
            if rp.get('root_path')!=str(root):blockers.append('root_binding_changed')
            if pp.get('checkout_path')!=str(checkout):blockers.append('checkout_binding_changed')
            if sum(Path(p['path']).resolve()==checkout for p in registry['projects'])!=1:blockers.append('duplicate_checkout')
            for p in registry['projects']:
                if p['id']==pid:continue
                sibling=Path(p['path'])/'.wsflow/project.json'
                if sibling.exists():
                    skills.plain_path(sibling,Path(p['path']))
                    identity=Reader(files=[sibling]);_,raw=identity.read(sibling,'sibling:identity','metadata',boundary=Path(p['path']))
                    value=json.loads(raw.decode('utf-8-sig')) if raw else None
                    if not isinstance(value,dict):blockers.append('sibling_identity_unknown')
                    elif value.get('project_uid')==pp.get('project_uid'):blockers.append('duplicate_project_uid')
                    if not identity.finish():blockers.append('sibling_identity_unknown')
            for r in registry['roots']:
                other=Path(r['path']).resolve()
                if other==root:continue
                path=other/'.wsflow/workspace.json'
                if path.exists():
                    skills.plain_path(path,other);identity=Reader(files=[path]);_,raw=identity.read(path,'root:identity','metadata',boundary=other)
                    value=json.loads(raw.decode('utf-8-sig')) if raw else None
                    if not isinstance(value,dict):blockers.append('root_identity_unknown')
                    elif value.get('root_uid')==rp.get('root_uid'):blockers.append('duplicate_root_uid')
                    if not identity.finish():blockers.append('root_identity_unknown')
            for p,rules in ((rp,'GLOBAL_RULES.md'),(pp,'PROJECT_RULES.md')):
                if p.get('rules_path')!=rules or p.get('skills_path')!='skills':blockers.append('custom_profile_requires_migration')
            entry,exposure=TOOLS[tool]
            common=read(root/'GLOBAL_RULES.md','common:rules',root,True)
            local=read(checkout/'PROJECT_RULES.md','project:rules',checkout,True)
            entry_raw=read(checkout/entry,'entrypoint:'+tool,checkout,True)
            if tool=='codex' and (checkout/'AGENTS.override.md').exists():blockers.append('entrypoint_override')
            shadow=checkout/'GLOBAL_RULES.md'
            if shadow.exists():
                shadow_raw=read(shadow,'project:common_shadow',checkout)
                if shadow_raw!=common:blockers.append('common_rules_shadowed')
            # Verify ownership pins and only this tool's selected projection bytes.
            manifests={}
            for owner in (root,checkout):
                manifests[str(owner)]=self.context._manifest(owner,blockers)
                read(owner/'.wsflow/manifest.json',('root' if owner==root else 'project')+':manifest',owner)
                rel='.wsflow/workspace.json' if owner==root else '.wsflow/project.json'
                expected=manifests[str(owner)].get('files',{}).get(rel,{})
                ident='root:profile' if owner==root else 'project:profile'
                actual=next((s.get('sha256') for s in reader.sources if s['logical_id']==ident),None)
                if not actual or expected.get('sha256')!=actual:blockers.append('managed_profile_changed:'+ident)
            manifest=manifests[str(checkout)]
            managed=manifest.get('files',{}).get(entry,{})
            if entry_raw is not None:
                _,_,block=projection.managed_block(entry_raw)
                if projection.sha(block)!=managed.get('sha256'):blockers.append('managed_entrypoint_changed')
            # Explicit canonical references remain part of the static check.
            from .context_rules import references
            refs=references(entry_raw.decode('utf-8-sig') if entry_raw else '',checkout)
            if not {root/'GLOBAL_RULES.md',checkout/'PROJECT_RULES.md'}.issubset(refs):blockers.append('canonical_references_missing')
            others=[Path(p['path']).resolve() for p in registry['projects'] if p['id']!=pid]
            catalog,errors=skills.catalog(root,checkout,others);blockers.extend(errors)
            by_id={p['skill_id']:p for p in catalog};selected=pp.get('selected_skills',[])
            if not isinstance(selected,list) or any(not isinstance(x,str) for x in selected) or len(set(selected))!=len(selected):raise ValueError('invalid_skill_selection')
            for ident in selected:
                p=by_id.get(ident)
                if not p:blockers.append('selected_skill_missing:'+ident);continue
                prefix=exposure+'/'+p['name'];target=checkout/prefix
                owned=manifest.get('packages',{}).get(prefix,{})
                current=skills.package(target,checkout)
                if owned.get('skill_id')!=ident or owned.get('tree_hash')!=p['tree_hash'] or current['tree_hash']!=p['tree_hash']:
                    blockers.append('projection_mismatch:'+ident)
                expected_skills.append({'id':ident,'name':p['name'],'source_path':p['path'],'exposed_path':str(target),
                                        'tree_hash':p['tree_hash'],'file_count':p['file_count']})
                observations.append({'source':p['path'],'source_hash':p['tree_hash'],'target':str(target),'target_hash':current['tree_hash']})
                # Recheck after all source observations to detect ordinary concurrent changes.
                if skills.package(Path(p['path']),root if p['source_scope']=='common' else checkout)['tree_hash']!=p['tree_hash']:
                    blockers.append('skill_changed_during_query')
            base=checkout/exposure
            skills.plain_path(base,checkout)
            if base.exists():
                managed_names={p['name'].casefold() for p in expected_skills}
                for child in base.iterdir():
                    if child.is_dir() and child.name.casefold() not in managed_names:
                        warnings.append('additional_tool_skill:'+child.name)
            if (checkout/'.claude/rules').exists():warnings.append('additional_claude_rules_semantics_unknown')
            if tool=='claude':
                for name in ('CLAUDE.local.md','.claude/CLAUDE.md'):
                    path=checkout/name
                    if path.exists():read(path,'additional:'+name,checkout,True)
                rule_dir=checkout/'.claude/rules';skills.plain_path(rule_dir,checkout)
                if rule_dir.is_dir():
                    for index,path in enumerate(sorted(rule_dir.rglob('*'))):
                        if index>=4000:raise ValueError('additional_rules_limit')
                        skills.plain_path(path,checkout)
                        if path.is_file() and path.suffix.lower()=='.md':read(path,'additional:'+path.relative_to(checkout).as_posix(),checkout,True)
            if tool=='codex':
                legacy=checkout/'.codex/skills';skills.plain_path(legacy,checkout)
                if legacy.is_dir():
                    for path in legacy.iterdir():
                        if any(s['name'].casefold()==path.name.casefold() for s in expected_skills):blockers.append('legacy_skill_collision:'+path.name)
            # Worktree descriptor/parent identities bind a checkout even if content matches.
            boundary={'root':projection.state(root),'checkout':projection.state(checkout),'git':projection.state(checkout/'.git')}
            gitfile=checkout/'.git'
            if gitfile.is_file():
                skills.plain_path(gitfile,checkout);text=gitfile.read_text(encoding='utf-8').strip()
                if text.startswith('gitdir:'):
                    admin=(checkout/text[7:].strip()).resolve();boundary['git_admin']=projection.state(admin);boundary['git_head']=projection.state(admin/'HEAD')
            for pending in self.context.ops.list():
                if pending.get('kind')=='context' and pending.get('owner') in (str(root),str(checkout)) and pending['status'] in ('executing','uncertain'):
                    blockers.append('context_operation_unfinished')
            if not reader.finish():blockers.append('source_changed_or_unreadable')
            observations.extend(reader.sources)
            result['binding']=boundary
        except (OSError,ValueError,KeyError,RuntimeError,TypeError,AttributeError) as exc:
            blockers.append(str(exc))
        result.update(ready=not blockers,blockers=sorted(set(blockers)),warnings=sorted(set(warnings)),expected_sources=documents,expected_skills=expected_skills)
        result['snapshot_id']=operations.digest({k:result.get(k) for k in ('project_id','tool','cwd','root_path','root_uid','project_uid','binding','blockers')} | {'sources':observations})
        return result

    @_storage_checked
    @writer_method
    def prepare(self,pid,body):
        if not isinstance(body,dict) or set(body)!={'tool','version','surface'}:raise ValueError('tool/version/surface만 제출하십시오')
        tool=body['tool'];version=bounded_text(body['version'],100,'도구 버전')
        if not isinstance(tool,str) or tool not in TOOLS or body['surface'] not in ('cli','desktop','ide'):raise ValueError('도구와 surface를 확인하십시오')
        self._storage();snapshot=self.inspect(pid,tool)
        profile={'tool':tool,'version':version,'surface':body['surface'],**runtime(),
                 'discovery_path':TOOLS[tool][1],'version_provenance':'user_declared'}
        profile['profile_id']=operations.digest(profile)
        packet={**snapshot,'kind':'session_packet','profile':profile,'recognition':'unknown','observation_kind':'static_only',
                'packet_id':None,'startup_text':'','scope_complete':False}
        if not snapshot['ready']:return packet
        text='새 세션을 다음 checkout에서 시작하세요: '+snapshot['cwd']+'\n도구: '+tool+' '+version+' ('+body['surface']+')\n'
        text+='관측 결부 snapshot_id: '+snapshot['snapshot_id']+'\nprofile_id: '+profile['profile_id']+'\n'
        text+='아래 정본/진입점을 읽고 선택 스킬 목록을 확인하세요. 파일 연결 확인은 실제 로딩/준수의 증명이 아닙니다.\n'
        text+='\n'.join(s['id']+': '+s['path']+' [sha256 '+s['sha256']+']' for s in snapshot['expected_sources'])
        text+='\n선택 스킬:\n'+'\n'.join(s['id']+': '+s['exposed_path']+' [tree '+s['tree_hash']+']' for s in snapshot['expected_skills'])
        text+='\n실제로 확인한 문서/스킬만 보고하세요. 자체 보고는 agent_reported이며 tool_observed로 바꾸지 마세요. 개인/플러그인 범위는 미확인입니다.'
        text+='\n보고 JSON 필드: schema_version=1, origin=agent_report, session_ref, captured_at(실제 관측 시각·ISO offset), snapshot_id, profile_id, cwd, observed_sources([{id,sha256}]), observed_skills([{id,tree_hash}]), notes. 미확인 항목은 배열에서 제외하세요.'
        packet['startup_text']=text
        saved=self.ops.save_plan(packet,[])
        packet.update(packet_id=saved['plan_id'],created_at=saved['created_at'])
        packet['observation_template']={'schema_version':1,'origin':'agent_report','session_ref':'실제 세션 식별자',
            'captured_at':operations.now(),'snapshot_id':packet['snapshot_id'],'profile_id':profile['profile_id'],'cwd':packet['cwd'],
            'observed_sources':[],'observed_skills':[],'notes':'실제로 확인한 출처 ID/sha256와 스킬 ID/tree_hash만 입력하세요.'}
        return packet

    @_storage_checked
    @writer_method
    def upload(self,content):
        if not isinstance(content,str) or not content or len(content.encode('utf-8'))>MAX_EVIDENCE:raise ValueError('증거는 256KiB 이하 UTF-8 텍스트입니다')
        self._storage();skills.plain_path(self.evidence_dir,self.ops.path.parent.parent)
        ident='E-'+uuid4().hex;raw=content.encode('utf-8')
        self.ops.save_plan({'kind':'session_evidence','evidence_id':ident,'sha256':projection.sha(raw),'size_bytes':len(raw)},[])
        fileio.atomic_write(self.evidence_dir/(ident+'.txt'),raw,
                            validate_handle=lambda fd:projection.validate_staging_handle(fd,self.ops.path.parent.parent))
        return {'evidence_id':ident,'sha256':projection.sha(raw),'size_bytes':len(raw)}

    def _evidence(self,ident):
        if not isinstance(ident,str) or not re.fullmatch(r'E-[0-9a-f]{32}',ident):raise ValueError('서버 evidence ID만 허용합니다')
        meta=next((p for p in self._plans('session_evidence') if p['evidence_id']==ident),None)
        if not meta:raise ValueError('등록되지 않은 증거입니다')
        path=self.evidence_dir/(ident+'.txt');skills.plain_path(path,self.ops.path.parent.parent)
        reader=Reader(files=[path]);_,raw=reader.read(path,'session:evidence','user_data',boundary=self.evidence_dir)
        if raw is None or len(raw)>MAX_EVIDENCE or projection.sha(raw)!=meta['sha256'] or not reader.finish():raise ValueError('evidence_changed')
        return meta,raw

    @_storage_checked
    @writer_method
    def observe(self,pid,body):
        if not isinstance(body,dict) or set(body)-{'packet_id','evidence_id','evidence_kind','attest_tool_output'} or not {'packet_id','evidence_id','evidence_kind'}.issubset(body):raise ValueError('packet/evidence ID와 관측 종류만 제출하십시오')
        kind=body['evidence_kind']
        if not isinstance(kind,str) or kind not in KINDS:raise ValueError('관측 종류가 올바르지 않습니다')
        if not isinstance(body.get('attest_tool_output',False),bool):raise ValueError('도구 출력 확인은 boolean입니다')
        self._storage();packet=self._packet(pid,body['packet_id']);meta,raw=self._evidence(body['evidence_id'])
        doc=json.loads(raw.decode('utf-8-sig'))
        required={'schema_version','origin','session_ref','captured_at','snapshot_id','profile_id','cwd','observed_sources','observed_skills','notes'}
        if not isinstance(doc,dict) or set(doc)!=required or type(doc['schema_version']) is not int or doc['schema_version']!=1:raise ValueError('관측 JSON schema가 올바르지 않습니다')
        expected_origin={'agent_reported':'agent_report','tool_observed':'tool_output','static_only':'static_only','unknown':'unknown'}[kind]
        if doc['origin']!=expected_origin:raise ValueError('관측 종류와 출력 origin이 다릅니다 — 자체 보고를 승격할 수 없습니다')
        if kind=='tool_observed' and body.get('attest_tool_output') is not True:raise ValueError('실제 도구 출력임을 사용자가 확인해야 합니다; 진위 독립검증은 아닙니다')
        cwd=doc['cwd']
        same_cwd=isinstance(cwd,str) and Path(cwd).is_absolute() and '..' not in Path(cwd).parts and Path(cwd).absolute()==Path(packet['cwd'])
        if doc['snapshot_id']!=packet['snapshot_id'] or doc['profile_id']!=packet['profile']['profile_id'] or not same_cwd:
            raise ValueError('관측의 snapshot/profile/cwd가 준비 패킷과 다릅니다')
        session_ref=bounded_text(doc['session_ref'],200,'세션 식별자')
        captured=timestamp(doc['captured_at'])
        if captured<timestamp(packet['created_at']) or captured>datetime.now(timezone.utc)+timedelta(minutes=5):raise ValueError('관측 시각이 준비 후/현재 시각 범위를 벗어났습니다')
        if not isinstance(doc['notes'],str) or len(doc['notes'])>20000:raise ValueError('notes는 20000자 이하 텍스트입니다')
        def checked(values,expected,hash_key):
            if not isinstance(values,list):raise ValueError('관측 출처는 배열입니다')
            allowed={p['id']:p[hash_key] for p in expected};seen=set()
            for item in values:
                if not isinstance(item,dict) or set(item)!={'id',hash_key} or not isinstance(item['id'],str) or item['id'] in seen or allowed.get(item['id'])!=item[hash_key]:
                    raise ValueError('관측 출처/해시/중복이 준비 범위와 다릅니다')
                seen.add(item['id'])
            return sorted(seen),sorted(set(allowed)-seen)
        source_ids,missing_sources=checked(doc['observed_sources'],packet['expected_sources'],'sha256')
        skill_ids,missing_skills=checked(doc['observed_skills'],packet['expected_skills'],'tree_hash')
        if kind in ('unknown','static_only') and (source_ids or skill_ids):raise ValueError('정적/미확인 기록에 실제 인식을 주장할 수 없습니다')
        scope={'sources':source_ids,'skills':skill_ids,'missing_sources':missing_sources,'missing_skills':missing_skills,
               'selected_scope_complete':not missing_sources and not missing_skills,'global_scope_complete':False}
        intent={'kind':'session_receipt','project_id':pid,'packet_id':body['packet_id'],'evidence_id':body['evidence_id'],
                'evidence_sha256':meta['sha256'],'evidence_kind':kind,'session_ref':session_ref,'captured_at':doc['captured_at'],
                'received_at':operations.now(),'verification_scope':scope,'notes':doc['notes'],
                'attest_tool_output':body.get('attest_tool_output',False),'trust':'user_submitted_unattested','independently_verified':False}
        ident='R-'+operations.digest([body['packet_id'],body['evidence_id'],kind,body.get('attest_tool_output',False)])
        try:receipt=self.ops.create(intent,ident=ident)
        except sqlite3.IntegrityError:receipt=self.ops.get(ident)
        return self._receipt(receipt,packet,self.inspect(pid,packet['profile']['tool']),self._latest(pid,packet['profile']['tool']))

    def _latest(self,pid,tool):
        packets=[p for p in self._plans('session_packet') if p['project_id']==pid and p['profile']['tool']==tool]
        return packets[-1] if packets else None

    @staticmethod
    def _stale(packet,current,latest):
        reasons=[]
        if packet['snapshot_id']!=current['snapshot_id']:reasons.append('context_changed')
        if packet['cwd']!=current.get('cwd'):reasons.append('checkout_changed')
        if not current['ready']:reasons.append('files_blocked')
        if any(packet['profile'][k]!=v for k,v in runtime().items()):reasons.append('runtime_changed')
        if latest and latest['plan_id']!=packet['plan_id']:
            reasons.append('newer_preparation')
            if latest['profile']['profile_id']!=packet['profile']['profile_id']:reasons.append('profile_changed')
        return reasons

    def _receipt(self,receipt,packet,current,latest):
        reasons=self._stale(packet,current,latest)
        try:self._evidence(receipt['evidence_id'])
        except (OSError,ValueError,operations.StateConflict):reasons.append('evidence_changed')
        kind=receipt['evidence_kind'];recognition='unknown'
        if not reasons and kind in ('agent_reported','tool_observed'):
            recognition=kind if receipt['verification_scope']['selected_scope_complete'] else 'partial'
        return {k:v for k,v in receipt.items() if k not in ('home','status','approved_at','result','error','local_recorded','kind')} | {
            'receipt_id':receipt['operation_id'],'stale_reasons':sorted(set(reasons)),'current':not reasons,'recognition':recognition,
            'profile':packet['profile'],'cwd':packet['cwd'],'snapshot_id':packet['snapshot_id'],
            'observed_sources':[s for s in packet['expected_sources'] if s['id'] in receipt['verification_scope']['sources']],
            'observed_skill_ids':receipt['verification_scope']['skills'],
            'evidence_path':str(self.evidence_dir/(receipt['evidence_id']+'.txt'))}

    def status(self,pid):
        tools={};packets=self._plans('session_packet');receipts=[o for o in self.ops.list() if o.get('kind')=='session_receipt' and o.get('project_id')==pid]
        for tool in TOOLS:
            current=self.inspect(pid,tool);latest=self._latest(pid,tool)
            by_id={p['plan_id']:p for p in packets if p['project_id']==pid and p['profile']['tool']==tool}
            views=[self._receipt(r,by_id[r['packet_id']],current,latest) for r in receipts if r['packet_id'] in by_id]
            latest_receipt=views[-1] if views else None
            packet_view=({**latest,'packet_id':latest['plan_id'],'stale_reasons':self._stale(latest,current,latest)} if latest else None)
            tools[tool]={'files':current,'packet':packet_view,'latest_receipt':latest_receipt,'receipts':views,
                         'recognition':latest_receipt['recognition'] if latest_receipt else 'unknown',
                         'independently_verified':False,'version_provenance':'user_declared','scope_complete':False}
        return {'schema_version':1,'project_id':pid,'tools':tools,'scope_complete':False}

    def dashboard(self,calendar=None):
        rows=[]
        for project in self.registry.snapshot()['projects']:
            try:state=self.status(project['id'])
            except (OSError,ValueError,sqlite3.Error):
                state={'tools':{tool:{'files':{'ready':False,'blockers':['context_status_unavailable'],'warnings':[],'snapshot_id':None},'packet':None,'latest_receipt':None,'recognition':'unknown'} for tool in TOOLS}}
            impact=state['tools']['codex']['files'].get('impact_project_ids',[])
            summary={}
            for tool,status in state['tools'].items():
                receipt=status['latest_receipt'];packet=status['packet']
                summary[tool]={'files':{k:status['files'][k] for k in ('ready','blockers','warnings','snapshot_id')},
                               'recognition':status['recognition'],'scope_complete':False,'independently_verified':False,
                               'latest_receipt':{k:receipt[k] for k in ('evidence_kind','current','stale_reasons','captured_at','session_ref','verification_scope')} if receipt else None,
                               'packet':{k:packet[k] for k in ('profile','created_at','stale_reasons')} if packet else None}
            rows.append({'id':project['id'],'alias':project.get('alias',project['id']),'tools':summary,'impact_project_ids':impact})
        return {'schema_version':1,'projects':rows,'calendar':calendar or {'state':'unknown','warnings':['캘린더 상태 미확인']},
                'captured_at':operations.now(),'observation_scope':'static_and_submitted_evidence'}
