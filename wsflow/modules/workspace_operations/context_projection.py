"""I5 preserving projections, immutable plans and explicit interrupted-write recovery.

Only managed-copy is offered. All user effects occur after plan preflight; an
interrupted multi-file apply is a recoverable saga, not an atomic transaction.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from . import context_skills as skills
from wsflow import fileio, operations
from wsflow.path_policy import protected_destination  # Compatibility export for existing callers.

BEGIN = b'<!-- WSFLOW:BEGIN v1 -->'
END = b'<!-- WSFLOW:END v1 -->'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def state(path):
    """Non-following boundary state, bytes for files, inode/realpath for dirs."""
    path = Path(path)
    if skills.linked(path):
        return {'kind':'link', 'resolved':str(path.resolve()), 'target':os.readlink(path)}
    try:
        st = path.stat()
    except FileNotFoundError:
        return {'kind':'absent', 'resolved':str(path.resolve())}
    if path.is_dir():
        return {'kind':'dir', 'resolved':str(path.resolve()), 'dev':st.st_dev, 'ino':st.st_ino,'mode':st.st_mode}
    if not path.is_file():
        raise ValueError('unsupported_file:' + str(path))
    if st.st_size > 64 * 1024 * 1024:
        raise ValueError('file_limit:' + str(path))
    return {'kind':'file', 'sha256':sha(path.read_bytes()), 'resolved':str(path.resolve()),
            'mode':st.st_mode}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def read_json(path):
    if not path.exists():
        return None
    value = json.loads(path.read_bytes().decode('utf-8-sig'))
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('invalid_profile:' + str(path))
    return value


def managed_block(raw):
    if raw.count(BEGIN) != 1 or raw.count(END) != 1:
        raise ValueError('invalid_managed_markers')
    start, end = raw.index(BEGIN), raw.index(END) + len(END)
    if end < start:
        raise ValueError('invalid_managed_markers')
    return start, end, raw[start:end]


def validate_staging_handle(fd, owner):
    """Check the actual opened staging file before any instruction bytes are written."""
    if os.name=='nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True).GetFinalPathNameByHandleW
        api.argtypes=[wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD]
        api.restype=wintypes.DWORD
        buf=ctypes.create_unicode_buffer(32768)
        size=api(msvcrt.get_osfhandle(fd),buf,len(buf),0)
        if not size or size>=len(buf):raise OSError('staging handle path unavailable')
        value=buf.value
        if value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
        elif value.startswith('\\\\?\\'):value=value[4:]
        actual=Path(value)
    elif Path('/proc/self/fd').is_dir():
        actual=Path(os.readlink('/proc/self/fd/'+str(fd)))
    else:
        raise OSError('이 호스트의 열린 파일 경계 검증을 지원하지 않습니다')
    skills.plain_path(actual,Path(owner))


class Service:
    def __init__(self, registry):
        self.registry = registry
        self.ops = operations.Store(str(registry.file.resolve()) + '.context')
        self.lock_home = self.ops.path.parent / ('context-' + operations.digest(str(registry.file.resolve())))

    def _binding(self, pid):
        registry = self.registry.snapshot()
        project = next((p for p in registry['projects'] if p['id'] == pid), None)
        if not project:
            raise ValueError('등록되지 않은 프로젝트입니다')
        checkout = Path(project['path']).absolute()
        skills.plain_path(checkout, Path(checkout.anchor))
        roots = [Path(r['path']).absolute() for r in registry['roots'] if checkout.is_relative_to(Path(r['path']).absolute())]
        if not roots:
            raise ValueError('등록 ROOT가 필요합니다')
        root = max(roots, key=lambda p:len(p.parts))
        if root == checkout or not checkout.is_dir():
            raise ValueError('독립 checkout과 ROOT가 필요합니다')
        return registry, root, checkout

    def _manifest(self, owner, blockers):
        path = owner / '.wsflow/manifest.json'
        try:
            skills.plain_path(path, owner)
            value = read_json(path)
            if value is None:
                return {'schema_version':1, 'files':{}, 'packages':{}}
            anchors = [o for o in self.ops.list() if o.get('kind') == 'context' and o.get('owner') == str(owner) and o['status']=='succeeded']
            expected = anchors[-1].get('result', {}).get('manifest_sha256') if anchors else None
            if expected != sha(path.read_bytes()):
                blockers.append('manifest_changed')
            return value
        except (ValueError, OSError) as exc:
            blockers.append(str(exc));return {'schema_version':1,'files':{},'packages':{}}

    def setup(self, pid):
        registry, root, checkout = self._binding(pid)
        others = [Path(p['path']).resolve() for p in registry['projects'] if p['id'] != pid]
        packages, blockers = skills.catalog(root, checkout, others)
        diagnostics = []
        for rel in ('.codex/skills', '.claude/skills', '.agents/skills'):
            base = checkout / rel
            if base.is_dir():
                diagnostics.append({'path':str(base), 'kind':'existing_exposure', 'policy':'preserve; no automatic adoption or merge'})
                try:
                    skills.plain_path(base,checkout)
                    for child in sorted(base.iterdir()):
                        if not child.is_dir():continue
                        info=skills.package(child,checkout)
                        matches=[p for p in packages if p['name'].casefold()==child.name.casefold()]
                        diagnostics.append({'path':str(child),'kind':'package_comparison','tree_hash':info['tree_hash'],
                                            'file_count':info['file_count'],'policy':'같은 이름 정본: '+', '.join(p['skill_id']+(' (전체 동일)' if p['tree_hash']==info['tree_hash'] else ' (내용 다름)') for p in matches)+'; 자동 병합 없음'})
                except (OSError,ValueError,UnicodeError) as exc:
                    diagnostics.append({'path':str(base),'kind':'unverified_legacy_package','policy':str(exc)})
        rp, pp = None, None
        try:
            for path in (root/'.wsflow/workspace.json',checkout/'.wsflow/project.json'):
                skills.plain_path(path,root if path.is_relative_to(root/'.wsflow') else checkout)
            rp = read_json(root/'.wsflow/workspace.json');pp = read_json(checkout/'.wsflow/project.json')
        except (OSError,ValueError) as exc:
            blockers.append(str(exc))
        impact = [p['id'] for p in registry['projects'] if Path(p['path']).resolve().is_relative_to(root)]
        common = root/'GLOBAL_RULES.md'
        if rp and common.is_file() and rp.get('rules_sha256')!=sha(common.read_bytes()):
            diagnostics.append({'path':str(common),'kind':'common_source_changed','policy':'원본 변경 영향: '+', '.join(impact)+'; 새 계획/세션 확인 필요'})
        return {'schema_version':1,'project_id':pid,'root_path':str(root),'checkout_path':str(checkout),
                'root_initialized':bool(rp),'root_profile':rp,'project_profile':pp,'catalog':packages,
                'diagnostics':diagnostics,'blockers':blockers,'impact_project_ids':impact,'supported_backends':['managed-copy'],
                'observation_kind':'static_only','tool_recognition':'unknown','scope_complete':False,
                'operations':[self._public(o) for o in self.ops.list() if o.get('project_id')==pid and o.get('kind')=='context']}

    @staticmethod
    def _public(op):
        return {k:op.get(k) for k in ('operation_id','plan_id','project_id','owner','status','result','error')}

    def preview(self, pid, body):
        if not isinstance(body,dict) or set(body)-{'kind','backend','selected_skills','adopt_existing'}:
            raise ValueError('지원하지 않는 초기화 payload')
        kind = body.get('kind','project_init')
        if kind not in ('root_init','project_init'):
            raise ValueError('invalid initialization kind')
        selected = body.get('selected_skills',[])
        if not isinstance(selected,list) or any(not isinstance(s,str) for s in selected) or len(set(selected)) != len(selected):
            raise ValueError('selected_skills는 고유 ID 배열입니다')
        if not isinstance(body.get('adopt_existing',False),bool):
            raise ValueError('adopt_existing must be boolean')
        registry, root, checkout = self._binding(pid)
        root_init = kind == 'root_init';owner = root if root_init else checkout
        blockers, warnings, guards, actions = [], [], {}, []
        if body.get('backend') != 'managed-copy':
            blockers.append('backend_not_supported')
        def guard(path):
            path = Path(path).absolute()
            value = state(path)
            if str(path) in guards and guards[str(path)] != value:
                raise operations.StateConflict('조사 중 파일이 바뀌었습니다')
            guards[str(path)] = value
            return value
        def safe(path, base=owner):
            skills.plain_path(path,base)
            for parent in (path.parent,*path.parent.parents):
                guard(parent)
                if parent == base:break
            return guard(path)
        try:
            safe(root/'.wsflow/workspace.json',root)
            safe(checkout/'.wsflow/project.json',checkout)
            rp = read_json(root/'.wsflow/workspace.json')
            pp = read_json(checkout/'.wsflow/project.json')
            if not rp and not root_init:blockers.append('root_not_initialized')
            if rp and not isinstance(rp.get('root_uid'),str):blockers.append('invalid_root_uid')
            if rp and rp.get('root_path',str(root)) != str(root):blockers.append('root_binding_changed')
            if pp and pp.get('root_uid') != (rp or {}).get('root_uid'):blockers.append('root_uid_mismatch')
            if pp and pp.get('checkout_path',str(checkout)) != str(checkout):blockers.append('checkout_binding_changed')
            if pp and not isinstance(pp.get('project_uid'),str):blockers.append('invalid_project_uid')
            if sum(Path(p['path']).resolve()==checkout.resolve() for p in registry['projects'])!=1:
                blockers.append('duplicate_checkout_registration')
            for registered in registry['roots']:
                other_root=Path(registered['path']).resolve()
                if rp and other_root!=root:
                    other_profile=other_root/'.wsflow/workspace.json'
                    skills.plain_path(other_profile,other_root)
                    other=read_json(other_profile);guard(other_profile)
                    if other and other.get('root_uid')==rp.get('root_uid'):blockers.append('duplicate_root_uid')
            # This version supports the designed canonical paths; custom layouts require migration.
            for profile, rules in ((rp,'GLOBAL_RULES.md'),(pp,'PROJECT_RULES.md')):
                if profile and (profile.get('rules_path',rules)!=rules or profile.get('skills_path','skills')!='skills'):
                    blockers.append('custom_profile_requires_migration')
            for p in registry['projects']:
                if p['id']==pid:continue
                sibling = Path(p['path'])/'.wsflow/project.json'
                if pp and sibling.is_file():
                    skills.plain_path(sibling,Path(p['path']))
                    # Only identity metadata: never read sibling rules/skill bodies.
                    other = read_json(sibling)
                    guard(sibling)
                    if other and other.get('project_uid')==pp.get('project_uid'):blockers.append('duplicate_project_uid')
            for pending in self.ops.list():
                if pending.get('kind')=='context' and pending.get('owner')==str(owner) and pending['status'] in ('executing','uncertain'):
                    blockers.append('unfinished_operation:' + pending['operation_id'])
            manifest = self._manifest(owner,blockers)
            safe(owner/'.wsflow/manifest.json')
            old = manifest.get('files',{});new = {};packages_manifest = {}
            def directory(path):
                before = safe(path)
                if before['kind']=='absent':
                    actions.append({'path':str(path),'rel':path.relative_to(owner).as_posix(),
                                    'before':before,'after':{'kind':'dir'},'directory':True,'data':None,'backup':None,'diff':None})
                elif before['kind']!='dir':raise ValueError('target_not_directory:' + str(path))
            def write(rel, data, block=False):
                path = owner/rel;before = safe(path)
                raw = path.read_bytes() if before['kind']=='file' else b''
                if before['kind'] not in ('absent','file'):raise ValueError('target_not_plain_file:' + rel)
                entry = old.get(rel)
                if block:
                    if entry:
                        start,end,current = managed_block(raw)
                        if entry.get('sha256') != sha(current):
                            blockers.append('managed_block_changed:' + rel);return
                        after = raw[:start]+data+raw[end:]
                    else:
                        if b'WSFLOW:' in raw:raise ValueError('unowned_managed_markers:' + rel)
                        if raw and not body.get('adopt_existing'):blockers.append('adoption_required:' + rel)
                        after = raw+(b'\n' if raw and not raw.endswith(b'\n') else b'')+data+b'\n'
                    new[rel]={'kind':'block','sha256':sha(data)}
                else:
                    if entry and entry.get('sha256') != sha(raw):
                        blockers.append('managed_file_changed:' + rel);return
                    if not entry and raw and raw != data and not body.get('adopt_existing'):
                        blockers.append('adoption_required:' + rel)
                    after = data
                    new[rel]={'kind':'file','sha256':sha(after)}
                if raw != after or before['kind']=='absent':
                    actions.append({'path':str(path),'rel':rel,'before':before,'after':{'kind':'file','sha256':sha(after)},
                                    'data':base64.b64encode(after).decode(),'backup':base64.b64encode(raw).decode() if before['kind']=='file' else None,
                                    'diff':{'before':raw.decode('utf-8','replace')[:12000], 'after':after.decode('utf-8','replace')[:12000]} if not '/skills/' in rel else None})
            canonical = root/'GLOBAL_RULES.md';safe(canonical,root)
            if not root_init and not canonical.is_file():blockers.append('common_rules_missing')
            if not root_init:
                shadow=checkout/'GLOBAL_RULES.md';safe(shadow)
                if shadow.is_file() and canonical.is_file():
                    if shadow.read_bytes()!=canonical.read_bytes():blockers.append('common_rules_shadowed')
                    else:warnings.append('duplicate_common_rules_copy_preserved')
            rules_rel = 'GLOBAL_RULES.md' if root_init else 'PROJECT_RULES.md'
            rules = owner/rules_rel;before = safe(rules)
            # User rules remain canonical and unowned. Create only when absent.
            if before['kind']=='absent':
                write(rules_rel, b'# Common development rules\n\nAdd workspace-wide policy here.\n' if root_init else b'# Project rules\n\nAdd project facts and explicit policy exceptions here.\n')
            elif rules_rel in old:
                # Editing a generated rules stub is expected; never overwrite it.
                new.pop(rules_rel,None)
            root_uid = (rp or {}).get('root_uid') or str(uuid4())
            profile = dict((rp if root_init else pp) or {})
            profile.update(schema_version=1,root_uid=root_uid,rules_path=rules_rel,skills_path='skills')
            if root_init:
                rule_data=canonical.read_bytes() if canonical.is_file() else b'# Common development rules\n\nAdd workspace-wide policy here.\n'
                profile.update(root_path=str(root),rules_sha256=sha(rule_data))
            else:
                profile.update(project_uid=(pp or {}).get('project_uid') or str(uuid4()), checkout_path=str(checkout),
                               selected_skills=selected,projection='managed-copy',common_rules_sha256=sha(canonical.read_bytes()) if canonical.is_file() else None)
                profile.setdefault('policy_exceptions',[])
            refs = [canonical] + ([] if root_init else [rules])
            for name in ('AGENTS.md','CLAUDE.md'):
                paths = [os.path.relpath(p,owner).replace('\\','/') for p in refs]
                if any(any(c in p for c in '\n\r`@') for p in paths):raise ValueError('unsupported_reference_path')
                if name=='AGENTS.md':
                    body_text = 'Read these canonical files before work:\n'+''.join('- `'+p+'`\n' for p in paths)
                else:
                    body_text = 'Canonical instructions (external imports may need tool approval):\n'+''.join('@'+p.replace(' ','\\ ')+'\n' for p in paths)
                block = BEGIN+b'\n'+body_text.encode()+b'Resolved owner controls writes; tool loading is unverified.\n'+END
                write(name,block,block=True)
            if not root_init:
                override=checkout/'AGENTS.override.md';safe(override)
                if override.exists():blockers.append('entrypoint_override')
                if (checkout/'.claude/CLAUDE.md').exists() or (checkout/'.claude/rules').exists():
                    warnings.append('additional_claude_instructions_semantics_unknown')
                others = [Path(p['path']).resolve() for p in registry['projects'] if p['id'] != pid]
                catalog, errors = skills.catalog(root,checkout,others);blockers.extend(errors)
                for base in (root/'skills',checkout/'skills'):
                    safe(base,root if base.is_relative_to(root/'skills') else checkout)
                    if base.is_dir():
                        guards[str(base)+'::names']={'names':sorted(p.name for p in base.iterdir())}
                by_id = {p['skill_id']:p for p in catalog}
                for rel in ('.codex/skills','.agents/skills','.claude/skills'):
                    base=checkout/rel;safe(base)
                    if not base.is_dir():continue
                    guards[str(base)+'::names']={'names':sorted(p.name for p in base.iterdir())}
                    for child in base.iterdir():
                        if not child.is_dir():continue
                        prefix=rel+'/'+child.name
                        if prefix in manifest.get('packages',{}):continue
                        if any(p['skill_id'] in selected and p['name'].casefold()==child.name.casefold() for p in catalog):
                            blockers.append('legacy_skill_collision:'+prefix)
                for prefix, expected in manifest.get('packages',{}).items():
                    location = checkout/prefix
                    current = skills.package(location,checkout) if location.is_dir() else None
                    if not current or current['tree_hash'] != expected['tree_hash']:
                        blockers.append('managed_package_changed:' + prefix)
                for ident in selected:
                    p = by_id.get(ident)
                    if not p:
                        blockers.append('selected_skill_missing:' + ident);continue
                    source = Path(p['path'])
                    guards[str(source)+'::package']={'tree_hash':p['tree_hash']}
                    for tool in ('.agents','.claude'):
                        prefix = tool+'/skills/'+p['name'];target = checkout/prefix
                        safe(target)
                        # Existing unmanaged packages are never silently replaced/adopted.
                        if target.exists() and prefix not in manifest.get('packages',{}):
                            blockers.append('unmanaged_exposure:' + prefix);continue
                        directory(target)
                        for rel in p['directories']:
                            directory(target/rel)
                        for rel in p['files']:
                            safe(source/rel,root if p['source_scope']=='common' else checkout)
                            write(prefix+'/'+rel,(source/rel).read_bytes())
                        packages_manifest[prefix]={'skill_id':ident,'tree_hash':p['tree_hash'],'source':p['path'],'backend':'managed-copy'}
            root_catalog = [p for p in skills.catalog(root,checkout,[])[0] if p['source_scope']=='common'] if root_init else catalog
            if root_init:
                common_base=root/'skills';safe(common_base,root)
                if common_base.is_dir():guards[str(common_base)+'::names']={'names':sorted(p.name for p in common_base.iterdir())}
            for p in root_catalog:
                guards[p['path']+'::package']={'tree_hash':p['tree_hash']}
            profile['catalog']=[{k:p[k] for k in ('skill_id','name','source_scope','tree_hash')} for p in root_catalog]
            write('.wsflow/workspace.json' if root_init else '.wsflow/project.json',json_bytes(profile))
            # Remove only bytes previously owned by this manifest. Keep parent dirs.
            for rel,entry in old.items():
                if rel in new or rel==rules_rel:continue
                path = owner/rel;current = safe(path)
                if entry.get('kind')!='file' or not '/skills/' in rel:
                    blockers.append('unexpected_managed_entry:' + rel);continue
                if current.get('sha256') != entry['sha256']:
                    blockers.append('managed_file_changed:' + rel);continue
                actions.append({'path':str(path),'rel':rel,'before':current,'after':{'kind':'absent'},'data':None,'backup':base64.b64encode(path.read_bytes()).decode(),'diff':None})
            output = {'schema_version':1,'owner':str(owner),'root_uid':root_uid,'files':new,'packages':packages_manifest}
            mpath = owner/'.wsflow/manifest.json';raw = json_bytes(output);prior = safe(mpath)
            if not mpath.is_file() or mpath.read_bytes()!=raw:
                actions.append({'path':str(mpath),'rel':'.wsflow/manifest.json','before':prior,'after':{'kind':'file','sha256':sha(raw)},
                                'data':base64.b64encode(raw).decode(),'backup':base64.b64encode(mpath.read_bytes()).decode() if mpath.is_file() else None,'diff':None})
            manifest_hash = sha(raw)
            # Bind checkout/worktree descriptors, and all parent directory identities.
            safe(checkout/'.git',checkout)
            git_entry = checkout/'.git'
            if git_entry.is_file():
                text = git_entry.read_text(encoding='utf-8').strip()
                if text.startswith('gitdir:'):
                    admin = (checkout/text[7:].strip()).resolve()
                    guard(admin);guard(admin/'HEAD')
        except (OSError,ValueError,KeyError) as exc:
            blockers.append(str(exc));manifest_hash = None
        impact = [p['id'] for p in registry['projects'] if Path(p['path']).resolve().is_relative_to(root)] if root_init else [pid]
        result = {'schema_version':1,'kind':kind,'project_id':pid,'owner':str(owner),'root_path':str(root),
                  'checkout_path':str(checkout),'actions':actions,'blockers':sorted(set(blockers)), 'warnings':warnings,
                  'impact_project_ids':impact,'tool_recognition':'unknown','observation_kind':'static_only','plan_id':None,'operation_id':None}
        if result['blockers']:
            return result
        for path,expected in guards.items():
            if self._guard_state(path) != expected:raise operations.StateConflict('계획 조사 중 원본 변경')
        snapshot = {**result,'registry_hash':operations.digest(registry),'guards':guards,'manifest_sha256':manifest_hash}
        saved = self.ops.save_plan(snapshot,[{'kind':'context','project_id':pid,'owner':str(owner)}])
        return {**result,'plan_id':saved['plan_id'],'operation_id':saved['items'][0]['operation_id'],'expires_at':saved['expires_at']}

    @staticmethod
    def _guard_state(key):
        if key.endswith('::names'):
            path = Path(key[:-7]);return {'names':sorted(p.name for p in path.iterdir())}
        if key.endswith('::package'):
            path = Path(key[:-9]);return {'tree_hash':skills.package(path,path)['tree_hash']}
        return state(key)

    @classmethod
    def _guard_matches(cls,path,expected,actions):
        current=cls._guard_state(path)
        if path.endswith('::names'):
            base=Path(path[:-7])
            own={Path(a['path']).name for a in actions if a.get('directory') and Path(a['path']).parent==base}
            return set(current['names'])-own==set(expected['names'])-own and set(current['names']).issubset(set(expected['names'])|own)
        return current==expected

    def status(self, pid, ident):
        op = self.ops.get(ident)
        if not op or op.get('kind')!='context' or op.get('project_id')!=pid:
            raise operations.StateConflict('해당 프로젝트의 context 작업을 찾지 못했습니다')
        saved = self.ops.plan(op['plan_id'])
        return {**self._public(op),'plan':{k:saved.get(k) for k in ('plan_id','project_id','actions','blockers','impact_project_ids','kind','owner')},'tool_recognition':'unknown'}

    @staticmethod
    def _matches(current, expected):
        return current['kind']==expected['kind'] and (current['kind'] in ('absent','dir') or current.get('sha256')==expected.get('sha256'))

    def apply(self, pid, plan_id, ident, recover=False):
        """Apply only server intent. Explicit recovery never overwrites later edits."""
        with fileio.locked(self.lock_home):
            op = self.ops.get(ident);saved = self.ops.plan(plan_id)
            if not op or op.get('kind')!='context' or op.get('project_id')!=pid or op.get('plan_id')!=plan_id or saved.get('project_id')!=pid:
                raise operations.StateConflict('계획·작업·프로젝트가 다릅니다')
            if op['status']=='succeeded':return op['result']
            continuing = op['status'] in ('executing','uncertain')
            if continuing and not recover:raise operations.StateConflict('중단된 작업은 명시 복구가 필요합니다')
            if not continuing and recover:raise operations.StateConflict('복구할 중단 작업이 없습니다')
            if not continuing and time.time()>saved['expires_at']:raise operations.StateConflict('계획이 만료되었습니다')
            registry,root,checkout = self._binding(pid)
            if operations.digest(registry)!=saved['registry_hash'] or str(checkout)!=saved['checkout_path'] or str(root)!=saved['root_path']:
                raise operations.StateConflict('등록 경계가 바뀌었습니다')
            action_paths = {a['path'] for a in saved['actions']}
            for path,expected in saved['guards'].items():
                if continuing and (path in action_paths or expected.get('kind')=='absent' and any(Path(path) in Path(a).parents for a in action_paths)):
                    continue
                matches=self._guard_matches(path,expected,saved['actions']) if continuing else self._guard_state(path)==expected
                if not matches:raise operations.StateConflict('stale-plan: '+path)
            for a in saved['actions']:
                skills.plain_path(Path(a['path']),Path(saved['owner']))
                current = state(a['path'])
                if current!=a['before'] and not (continuing and self._matches(current,a['after'])):
                    raise operations.StateConflict('복구/대상 전제가 다릅니다: '+a['rel'])
                if current['kind']=='file' and not current.get('mode',0)&0o200:
                    raise operations.StateConflict('쓰기 권한이 없습니다: '+a['rel'])
            if not continuing:
                self.ops.approve(ident);self.ops.claim(ident)
            completed = []
            try:
                for a in saved['actions']:
                    path = Path(a['path']);skills.plain_path(path,Path(saved['owner']))
                    if continuing and self._matches(state(path),a['after']):
                        completed.append(a['rel']);continue
                    def check():
                        skills.plain_path(path,Path(saved['owner']))
                        if state(path)!=a['before']:raise operations.StateConflict('쓰기 직전 대상 변경: '+a['rel'])
                        # Sources and existing parent identities remain bound throughout a saga.
                        for source,expected in saved['guards'].items():
                            if source in action_paths or expected.get('kind')=='absent' and any(Path(source) in Path(p).parents for p in action_paths):
                                continue
                            if not self._guard_matches(source,expected,saved['actions']):raise operations.StateConflict('적용 중 원본/경계 변경: '+source)
                    check()
                    if a.get('directory'):path.mkdir(parents=True,exist_ok=False)
                    elif a['data'] is None:path.unlink()
                    else:fileio.atomic_write(path,base64.b64decode(a['data']),before_replace=check,
                                             validate_handle=lambda fd:validate_staging_handle(fd,saved['owner']))
                    with self.ops.connection(True) as db:
                        self.ops._event(db,ident,'context_file_applied',{'path':a['path'],'after':a['after']})
                    completed.append(a['rel'])
                result = {'operation_id':ident,'plan_id':plan_id,'status':'succeeded','files':completed,
                          'manifest_sha256':saved['manifest_sha256'],'observation_kind':'static_only','tool_recognition':'unknown'}
                self.ops.finish(ident,'succeeded',result=result)
                return result
            except Exception as exc:
                result = {'operation_id':ident,'plan_id':plan_id,'status':'uncertain','files':completed,'error':str(exc),'recovery':'explicit_recover'}
                self.ops.finish(ident,'uncertain',result=result,error=str(exc))
                return result
