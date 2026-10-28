"""I4 linked calendar writes. Immutable outbox + fixed IDs + conditional remote effects.

Local record/link/outbox/audit commit together. No DB transaction spans HTTP.
Uncertain effects are reconciled by GET, never blindly sent again.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

from . import calendar_store, google_calendar as google
from wsflow import operations


def ensure(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS calendar_links(local_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,calendar_id TEXT NOT NULL,remote_id TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(account_id,calendar_id,remote_id))',
        'CREATE TABLE IF NOT EXISTS calendar_outbox(id TEXT PRIMARY KEY,local_id TEXT NOT NULL,intent TEXT NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT)',
        "CREATE TRIGGER IF NOT EXISTS calendar_intent_immutable BEFORE UPDATE OF intent ON calendar_outbox BEGIN SELECT RAISE(ABORT,'immutable calendar intent'); END;",
        "CREATE TRIGGER IF NOT EXISTS calendar_outbox_keep BEFORE DELETE ON calendar_outbox BEGIN SELECT RAISE(ABORT,'preserve calendar outbox'); END;",
    ):
        db.execute(sql)


def links_in(db):
    if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='calendar_links'").fetchone():
        return []
    return [json.loads(r[0]) for r in db.execute('SELECT data FROM calendar_links ORDER BY local_id')]


def save_link(db, link):
    db.execute('INSERT INTO calendar_links VALUES(?,?,?,?,?) ON CONFLICT(local_id) DO UPDATE SET data=excluded.data',
               (link['local_id'],link['account_id'],link['calendar_id'],link['remote_id'],operations.encoded(link)))


def audit(db, ident, state, detail=None):
    db.execute('INSERT INTO events(operation_id,status,detail,at) VALUES(?,?,?,?)',
               (ident,state,operations.encoded(detail or {}),operations.now()))


def body(record):
    value = {'summary':record['title'],'description':record['body'],'location':record['location']}
    if record['all_day']:
        value.update(start={'date':record['start_date']},end={'date':record['end_date']})
    else:
        value.update(start={'dateTime':record['start_at'],'timeZone':record['timezone']},
                     end={'dateTime':record['end_at'],'timeZone':record['timezone']})
    return value


def semantic(value):
    def instant(part):
        if 'date' in part:
            return ['date',part['date']]
        return ['instant',datetime.fromisoformat(part['dateTime'].replace('Z','+00:00')).astimezone(timezone.utc).isoformat()]
    return [value.get('summary',''),value.get('description',''),value.get('location',''),
            instant(value['start']),instant(value['end'])]


def supported(remote):
    if remote.get('recurrence') or remote.get('recurringEventId') or remote.get('attendees') or remote.get('eventType','default') != 'default':
        raise operations.StateConflict('반복·참석자·특수 일정은 Google에서 직접 확인하십시오')


def queue(db, link, record, action=None):
    if link.get('remote_deleted'):
        link.update(state='conflict',conflict={'local':record,'remote':None,'reason':'Google 원본이 삭제됐습니다. 보관 내용을 유지하거나 새 일정으로 작성하십시오','at':operations.now()})
        save_link(db,link);audit(db,record['id'],'conflict',link['conflict'])
        return None
    pending = db.execute('SELECT state FROM calendar_outbox WHERE id=?',(link.get('pending'),)).fetchone()
    if pending and pending[0] in {'syncing','uncertain'}:
        return None
    if pending and pending[0] in {'queued','error','conflict'}:
        db.execute("UPDATE calendar_outbox SET state='cancelled' WHERE id=?",(link['pending'],))
        audit(db,link['pending'],'cancelled',{'reason':'superseded by approved revision'})
    ident = 'GOP-'+uuid4().hex
    intent = {k:link[k] for k in ('local_id','account_id','calendar_id','remote_id')}
    intent.update(action=action or ('update' if link.get('base_etag') else 'create'),local_revision=record['revision'],
                  base_etag=link.get('base_etag'),base_remote=link.get('base_remote'),payload=body(record),operation_id=ident)
    intent['payload_hash'] = operations.digest(semantic(intent['payload']))
    db.execute('INSERT INTO calendar_outbox VALUES(?,?,?,?,NULL,NULL)',(ident,record['id'],operations.encoded(intent),'queued'))
    link.update(pending=ident,state='queued',conflict=None)
    save_link(db,link);audit(db,ident,'queued',{'local_revision':record['revision'],'action':intent['action']})
    return ident


def on_local_save(db, current, record, op):
    """Called within I2's local commit; notes never produce a Google effect."""
    if record['kind'] != 'event':
        return None
    existing = next((l for l in links_in(db) if l['local_id']==record['id']),None)
    target = op.get('google_target')
    if not existing and target is None:
        return None
    if existing and current and current['deleted']==record['deleted'] and body(current)==body(record):
        if existing['state']=='synced' and not existing.get('pending') and existing['base_local_revision']==current['revision']:
            existing['base_local_revision']=record['revision'];save_link(db,existing)
        return None
    ensure(db)
    link = existing or {**target,'local_id':record['id'],'state':'local_only','base_etag':None,
                        'base_remote':None,'base_local_revision':record['revision'],'hidden':False,'pending':None,'conflict':None}
    if record['deleted']:
        link['hidden'] = True
        pending = db.execute('SELECT state FROM calendar_outbox WHERE id=?',(link.get('pending'),)).fetchone()
        if pending and pending[0] in {'queued','error'}:
            db.execute("UPDATE calendar_outbox SET state='cancelled' WHERE id=?",(link['pending'],))
            audit(db,link['pending'],'cancelled',{'reason':'local hide'})
            link.update(pending=None,state='synced' if link.get('base_etag') else 'local_only')
        save_link(db,link)
        return None
    if link['state']=='conflict':
        save_link(db,link)
        return None
    return queue(db,link,record)


class Engine:
    def __init__(self, local, service):
        self.local,self.google = local,service

    def links(self):
        with self.local.connection() as db:
            return links_in(db)

    def scope(self):
        """Account/selection identifiers for linked project filters, without HTTP."""
        token=self.google.credentials.load()
        selected=self.google.store.get_value(self.google._key(token,'selected'),[]) if token else []
        return {'events':[],'sources':[{'source_id':'google:'+operations.digest([token['sub'],cid]),'kind':'google'} for cid in selected],
                'complete':True,'connected':bool(token)}

    def operation(self, ident):
        with self.local.connection() as db:
            if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='calendar_outbox'").fetchone():
                raise operations.StateConflict('서버가 발급한 Google 작업이 아닙니다')
            row = db.execute('SELECT * FROM calendar_outbox WHERE id=?',(ident,)).fetchone()
        if row is None:
            raise operations.StateConflict('서버가 발급한 Google 작업이 아닙니다')
        return {'id':row['id'],'intent':json.loads(row['intent']),'state':row['state'],
                'result':json.loads(row['result']) if row['result'] else None,'error':row['error']}

    def status(self):
        token = self.google.credentials.load()
        links = [l for l in self.links() if token and l['account_id']==token['sub']]
        ids = {l['local_id'] for l in links}
        with self.local.connection() as db:
            rows = db.execute('SELECT id FROM calendar_outbox ORDER BY rowid DESC').fetchall() if db and ids else []
        return {'links':[{**l,'local':next((r for r in self.local.records() if r['id']==l['local_id']),None)} for l in links],
                'operations':[o for r in rows if (o:=self.operation(r[0]))['intent']['local_id'] in ids]}

    def _authorize(self, account, calendar, write=True):
        token = self.google._token()
        if token['sub'] != account:
            raise operations.StateConflict('연결 계정이 바뀌었습니다 — 원래 계정으로 다시 연결하십시오')
        selected = self.google.store.get_value(self.google._key(token,'selected'),[])
        if calendar not in selected:
            raise operations.StateConflict('선택한 캘린더가 아닙니다')
        calendars = self.google.calendars()['calendars']
        found = next((c for c in calendars if c['id']==calendar),None)
        if found is None or not found['can_read'] or (write and not found['can_write']):
            raise operations.StateConflict('Google 쓰기 권한과 캘린더 권한을 확인하십시오')
        return token

    def target(self, calendar):
        if not isinstance(calendar,str) or not calendar:
            raise ValueError('저장할 캘린더를 선택하십시오')
        with self.google.locked():
            token = self.google._token();self._authorize(token['sub'],calendar)
            return {'account_id':token['sub'],'calendar_id':calendar,'remote_id':uuid4().hex}

    def _remote(self, token, link):
        try:
            return self.google._get(token,'/calendars/'+quote(link['calendar_id'],safe='')+'/events/'+quote(link['remote_id'],safe=''),{})
        except google.GoogleError as exc:
            if exc.status in (404,410):
                return None
            raise

    def preview(self, request, projects):
        self.local._storage_boundary(projects)
        if not isinstance(request,dict) or set(request)-{'action','local_id','expected_revision','calendar_id','resolution','remote_etag'}:
            raise ValueError('Google 계획 요청 필드가 올바르지 않습니다')
        action = request.get('action')
        record = next((r for r in self.local.records() if r['id']==request.get('local_id') and r['kind']=='event'),None)
        if record is None or type(request.get('expected_revision')) is not int or record['revision']!=request['expected_revision']:
            raise calendar_store.Conflict('일정 버전이 다릅니다',record)
        link = next((l for l in self.links() if l['local_id']==record['id']),None)
        with self.google.locked():
            if action=='publish':
                if link or record['deleted']:
                    raise operations.StateConflict('이미 연결됐거나 숨긴 일정입니다')
                target = self.target(request.get('calendar_id'));remote=None
            elif action=='show' and link:
                target={k:link[k] for k in ('account_id','calendar_id','remote_id')};remote=link.get('base_remote')
            elif action in {'delete','resolve'} and link:
                token=self._authorize(link['account_id'],link['calendar_id'])
                target={k:link[k] for k in ('account_id','calendar_id','remote_id')}
                remote=self._remote(token,link)
                if action=='delete':
                    if not link.get('base_etag') or remote is None:
                        raise operations.StateConflict('원격 상태를 먼저 동기화·대조하십시오')
                    if link.get('pending') and self.operation(link['pending'])['state'] in {'syncing','uncertain'}:
                        raise operations.StateConflict('진행 중인 작업의 원격 결과를 먼저 대조하십시오')
                    supported(remote)
                if action=='resolve':
                    if link['state']!='conflict' or request.get('resolution') not in {'local','remote'}:
                        raise operations.StateConflict('명시적으로 해결할 충돌을 선택하십시오')
                    etag=(remote or {}).get('etag')
                    if request.get('remote_etag')!=etag:
                        raise operations.StateConflict('원격 충돌 내용이 바뀌었습니다 — 다시 동기화하십시오')
                    if request['resolution']=='local':
                        if remote is None or remote.get('status')=='cancelled':
                            raise operations.StateConflict('삭제된 원격 일정은 자동 부활시키지 않습니다')
                        supported(remote)
            else:
                raise ValueError('지원하지 않는 Google 일정 작업입니다')
            item={'action':action,'target':record['id'],'expected_revision':record['revision'],
                  'destination':target,'remote':remote,'resolution':request.get('resolution'),
                  'identity_version':calendar_store.calendar_identity.VERSION,'bound_project_ids':record['project_ids'],
                  'project_bindings':self.local._bindings(projects,record['project_ids'])}
            plan=self.local.save_plan({'kind':'google_write','binding':self.local.registry},[item])
            return {'plan_id':plan['plan_id'],'operation_id':plan['items'][0]['operation_id'],
                    'expires_at':plan['expires_at'],'item':plan['items'][0]}

    def apply(self, plan_id, operation_id, projects):
        self.local._storage_boundary(projects)
        plan=self.local.plan(plan_id)
        if plan.get('kind')!='google_write' or plan.get('binding')!=self.local.registry or len(plan['items'])!=1 or plan['items'][0]['operation_id']!=operation_id:
            raise operations.StateConflict('이 Google 계획의 작업 ID가 아닙니다')
        op=self.local.get(operation_id)
        if op['status']=='succeeded':return op['result']
        if op['status']!='planned' or time.time()>plan['expires_at']:
            raise operations.StateConflict('만료됐거나 사용할 수 없는 계획입니다')
        self.local._verify_bindings(op, projects)
        dest=op['destination']
        with self.google.locked():
            if op['action']=='show':
                remote=op.get('remote')
            else:
                token=self._authorize(dest['account_id'],dest['calendar_id'])
                remote=self._remote(token,dest) if op['action']!='publish' else None
                if (remote or {}).get('etag')!=(op.get('remote') or {}).get('etag'):
                    raise operations.StateConflict('계획 후 Google 일정이 바뀌었습니다 — 다시 확인하십시오')
            with self.local.connection(True) as db:
                ensure(db)
                self.local._verify_bindings(op, projects)
                current=self._record(db,op['target'])
                if current is None or current['revision']!=op['expected_revision'] or op['project_bindings']!=self.local._bindings(projects,current['project_ids']):
                    raise calendar_store.Conflict('계획 후 로컬 일정이 바뀌었습니다',current)
                changed=db.execute("UPDATE operations SET status='executing' WHERE id=? AND status='planned'",(operation_id,)).rowcount
                if changed!=1:raise operations.StateConflict('이미 승인된 작업입니다')
                link=next((l for l in links_in(db) if l['local_id']==op['target']),None)
                if op['action']=='publish':
                    if link:raise operations.StateConflict('이미 Google에 연결된 일정입니다')
                    link={**dest,'local_id':current['id'],'base_etag':None,'base_remote':None,
                          'base_local_revision':current['revision'],'state':'local_only','pending':None,'conflict':None,'hidden':False}
                    queued=queue(db,link,current)
                elif op['action']=='delete':
                    link.update(base_etag=remote['etag'],base_remote=remote)
                    queued=queue(db,link,current,'delete')
                    if queued is None:raise operations.StateConflict('전송 중인 작업을 먼저 대조하십시오')
                elif op['action']=='show':
                    current={**current,'deleted':False,'revision':current['revision']+1,'updated_at':operations.now()}
                    db.execute('UPDATE calendar_records SET data=? WHERE id=?',(operations.encoded(current),current['id']))
                    link.update(hidden=False,base_local_revision=current['revision']);save_link(db,link);queued=None
                else:
                    if link['pending']:
                        state=db.execute('SELECT state FROM calendar_outbox WHERE id=?',(link['pending'],)).fetchone()
                        if state and state[0] in {'syncing','uncertain'}:raise operations.StateConflict('먼저 원격 결과를 대조하십시오')
                        db.execute("UPDATE calendar_outbox SET state='cancelled' WHERE id=? AND state IN ('queued','error','conflict')",(link['pending'],))
                    link.update(base_etag=(remote or {}).get('etag'),base_remote=remote,conflict=None,pending=None,state='synced',
                                remote_deleted=remote is None or remote.get('status')=='cancelled')
                    if op['resolution']=='remote':
                        current=self._adopt(db,current,remote)
                        link.update(base_local_revision=current['revision'],hidden=current['deleted'])
                        save_link(db,link);queued=None
                    else:
                        queued=queue(db,link,current)
                result={'record':current,'google_operation_id':queued,'operation_id':operation_id}
                db.execute("UPDATE operations SET status='succeeded',result=?,local_recorded=1 WHERE id=?",(operations.encoded(result),operation_id))
                audit(db,operation_id,'succeeded',{'action':op['action'],'link':link})
            return result

    @staticmethod
    def _record(db, ident):
        row=db.execute('SELECT data FROM calendar_records WHERE id=?',(ident,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _adopt(db,current,remote):
        before=copy.deepcopy(current)
        if remote is None or remote.get('status')=='cancelled':
            value={**current,'deleted':True}
        else:
            supported(remote)
            normalized=google.Service._event(remote,'unused','unused',{})
            fields={k:normalized[k] for k in ('title','body','location','all_day','start_date','end_date','start_at','end_at','timezone') if k in normalized}
            payload=calendar_store.normalize('event',{**fields,'project_ids':current['project_ids']},set(current['project_ids']))
            value={**{k:v for k,v in current.items() if k not in {'start_date','end_date','start_at','end_at','timezone'}},**payload,'deleted':False}
        value.update(revision=current['revision']+1,updated_at=operations.now())
        db.execute('UPDATE calendar_records SET data=? WHERE id=?',(operations.encoded(value),current['id']))
        audit(db,current['id'],'google_pulled',{'before':before,'after':value})
        return value

    def _state(self, ident, state, remote=None, error=None):
        with self.local.connection(True) as db:
            db.execute('UPDATE calendar_outbox SET state=?,result=?,error=? WHERE id=?',
                       (state,operations.encoded(remote) if remote is not None else None,error,ident))
            intent=self.operation(ident)['intent']
            link=next(l for l in links_in(db) if l['local_id']==intent['local_id'])
            if link.get('pending')==ident:
                link['state']=state;save_link(db,link)
            audit(db,ident,state,{'error':error})
        return self.operation(ident)

    def _conflict(self, ident, link, remote, reason):
        with self.local.connection(True) as db:
            current=self._record(db,link['local_id'])
            # Re-read mapping so a concurrent local save is represented in both snapshots.
            link=next(l for l in links_in(db) if l['local_id']==link['local_id'])
            affected = link.get('pending') or ident
            link.update(state='conflict',conflict={'local':current,'remote':remote,'reason':reason,'at':operations.now()})
            save_link(db,link)
            if affected:
                db.execute("UPDATE calendar_outbox SET state='conflict',result=?,error=? WHERE id=?",(operations.encoded(remote),reason,affected))
            audit(db,ident or link['local_id'],'conflict',link['conflict'])
        return self.operation(ident) if ident else link

    @staticmethod
    def matches(intent, remote):
        if not remote or remote.get('status')=='cancelled' or remote.get('id')!=intent['remote_id'] or not isinstance(remote.get('etag'),str) or not remote['etag']:
            return False
        private=remote.get('extendedProperties',{}).get('private',{})
        try:
            return (private.get('wsflowOperation')==intent['operation_id'] and private.get('wsflowLocal')==intent['local_id']
                    and private.get('wsflowPayload')==intent['payload_hash'] and semantic(remote)==semantic(intent['payload']))
        except (ValueError,KeyError,TypeError):
            return False

    def _complete(self, ident, remote):
        intent=self.operation(ident)['intent']
        with self.local.connection(True) as db:
            link=next(l for l in links_in(db) if l['local_id']==intent['local_id'])
            record=self._record(db,intent['local_id'])
            db.execute("UPDATE calendar_outbox SET state='synced',result=?,error=NULL WHERE id=?",(operations.encoded(remote),ident))
            link.update(base_etag=(remote or {}).get('etag'),base_remote=remote,base_local_revision=intent['local_revision'],
                        pending=None,state='synced',conflict=None,remote_deleted=intent['action']=='delete')
            if intent['action']=='delete':
                if record['revision']==intent['local_revision']:
                    record=self._adopt(db,record,None);link.update(hidden=True,base_local_revision=record['revision'])
                else:
                    link.update(state='conflict',conflict={'local':record,'remote':None,'reason':'삭제 전송 중 새 로컬 입력','at':operations.now()})
            save_link(db,link);audit(db,ident,'synced',{'remote_id':intent['remote_id']})
            if intent['action']!='delete' and record['revision']!=intent['local_revision'] and not record['deleted']:
                if semantic(body(record))==semantic(intent['payload']):
                    link['base_local_revision']=record['revision'];save_link(db,link)
                else:
                    queue(db,link,record)
        # Invalidate only this provider's view cache; a crash here leaves a safe stale view.
        token=self.google.credentials.load()
        if token and token['sub']==intent['account_id']:
            try:
                key=self.google._key(token,'mirror',intent['calendar_id']);mirror=self.google.store.get_value(key,{})
                self.google.store.put({key:{**mirror,'range_epoch':uuid4().hex}})
            except (ValueError,OSError,sqlite3.Error):
                pass  # known success stays durable; the next successful sync refreshes this cache
        return self.operation(ident)

    def run(self, ident):
        with self.google.locked():
            op=self.operation(ident);intent=op['intent']
            if op['state'] not in {'queued','error'}:
                return op
            try:
                token=self._authorize(intent['account_id'],intent['calendar_id'])
                link=next(l for l in self.links() if l['local_id']==intent['local_id'])
                if link.get('pending')!=ident:
                    return self._state(ident,'cancelled',error='더 새로운 승인으로 대체된 작업입니다')
                if link['state']=='conflict':
                    return self._state(ident,'conflict',error='충돌 양쪽을 먼저 확인하십시오')
                remote=self._remote(token,intent) if intent['action']!='create' else None
                if intent['action']!='create':
                    if remote is None or remote.get('status')=='cancelled' or remote.get('etag')!=intent['base_etag']:
                        return self._conflict(ident,link,remote,'전송 전 Google 버전 변경')
                    supported(remote)
                with self.local.connection(True) as db:
                    claimed=db.execute("UPDATE calendar_outbox SET state='syncing',error=NULL WHERE id=? AND state IN ('queued','error')",(ident,)).rowcount
                    if claimed!=1:raise operations.StateConflict('이미 전송되거나 대조가 필요한 작업입니다')
                    link['state']='syncing';save_link(db,link);audit(db,ident,'syncing')
                payload=copy.deepcopy(intent['payload'])
                props=copy.deepcopy((intent.get('base_remote') or {}).get('extendedProperties',{}))
                props.setdefault('private',{}).update(wsflowOperation=ident,wsflowLocal=intent['local_id'],wsflowPayload=intent['payload_hash'])
                payload['extendedProperties']=props
                url=google.API_URL+'/calendars/'+quote(intent['calendar_id'],safe='')+'/events'
                headers={'Authorization':'Bearer '+token['access_token']}
                if intent['action']=='create':
                    payload['id']=intent['remote_id'];method='POST'
                else:
                    method='DELETE' if intent['action']=='delete' else 'PATCH'
                    url+='/'+quote(intent['remote_id'],safe='');headers['If-Match']=intent['base_etag']
                result=self.google.transport(method,url+'?sendUpdates=none',None if method=='DELETE' else payload,headers)
                if method!='DELETE' and not self.matches(intent,result):
                    return self._state(ident,'uncertain',error='원격 응답의 ID/내용 대조 필요')
                return self._complete(ident,None if method=='DELETE' else result)
            except google.GoogleError as exc:
                current=self.operation(ident)
                if current['state']=='syncing':
                    if exc.status in (409,412):
                        self._state(ident,'uncertain',error='Google 버전/ID 대조 필요')
                        return self.reconcile(ident)
                    return self._state(ident,'error' if exc.status in (400,401,403,404,429) else 'uncertain',error=str(exc))
                return self._state(ident,'error',error=str(exc))
            except operations.StateConflict as exc:
                current=self.operation(ident)
                if current['state']=='syncing':
                    return self._state(ident,'uncertain',error='전송 후 로컬 기록 확인 필요')
                if current['state'] in {'queued','error'}:
                    return self._state(ident,'error',error=str(exc))
                return current

    def reconcile(self, ident):
        with self.google.locked():
            op=self.operation(ident);intent=op['intent']
            if op['state'] not in {'syncing','uncertain'}:
                return op
            token=self._authorize(intent['account_id'],intent['calendar_id'],write=False)
            remote=self._remote(token,intent)
            link=next(l for l in self.links() if l['local_id']==intent['local_id'])
            if intent['action']=='delete' and (remote is None or remote.get('status')=='cancelled'):
                return self._complete(ident,None)
            if self.matches(intent,remote):
                return self._complete(ident,remote)
            if (intent['action']=='create' and remote is None) or (intent['action']!='create' and remote and remote.get('etag')==intent['base_etag'] and semantic(remote)==semantic(intent['base_remote'])):
                return self._state(ident,'error',error='미반영 상태 확인 — 같은 ID로 명시 재시도할 수 있습니다')
            return self._conflict(ident,link,remote,'불확실 결과 대조에서 다른 Google 내용 발견')

    def pull(self):
        with self.google.locked():
            token=self.google.credentials.load()
            if not token:return []
            selected=self.google.store.get_value(self.google._key(token,'selected'),[])
            results=[]
            for link in self.links():
                if link['account_id']!=token['sub'] or link['calendar_id'] not in selected:
                    continue
                mirror=self.google.mirror(link['calendar_id'])
                if not mirror.get('complete'):continue
                pending=self.operation(link['pending']) if link.get('pending') else None
                if pending and pending['state'] in {'syncing','uncertain'}:continue
                remote=mirror.get('items',{}).get(link['remote_id'])
                if (remote or {}).get('etag')==link.get('base_etag'):continue
                # A full mirror may omit a deleted event. A never-sent create is not a remote deletion.
                if remote is None and not link.get('base_etag'):continue
                with self.local.connection(True) as db:
                    record=self._record(db,link['local_id'])
                    dirty=record['revision']!=link['base_local_revision'] or (pending and pending['state'] in {'queued','error'})
                    if dirty and not link['hidden']:
                        conflict=True
                    else:
                        try:
                            if not link['hidden']:record=self._adopt(db,record,remote)
                            elif remote and remote.get('status')!='cancelled':supported(remote)
                            link.update(base_etag=(remote or {}).get('etag'),base_remote=remote,
                                        base_local_revision=record['revision'],state='synced',hidden=record['deleted'],conflict=None,
                                        remote_deleted=remote is None or remote.get('status')=='cancelled')
                            save_link(db,link);conflict=False;results.append(link['local_id'])
                        except (ValueError,KeyError):
                            conflict=True
                if conflict:self._conflict(link.get('pending'),link,remote,'로컬/Google 변경 비교 필요')
            return results

    def after_sync(self):
        self.pull()
        sent=[]
        for op in self.status()['operations']:
            if op['state']=='queued':sent.append(self.run(op['id']))
            if len(sent)>=20:break
        return {'sent':len(sent)}
