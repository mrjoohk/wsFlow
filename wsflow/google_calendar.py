"""Google Desktop OAuth and read-only Calendar adapter (I3, stdlib only).

Secrets never enter the mirror DB or API responses. A cooperative process lock
serializes credentials and remote reads across app instances of this registry.
"""
from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, time as day_time, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import auth, calendar_query, fileio, operations

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
USER_URL = 'https://openidconnect.googleapis.com/v1/userinfo'
API_URL = 'https://www.googleapis.com/calendar/v3'
SCOPES = ('openid', 'email', 'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
          'https://www.googleapis.com/auth/calendar.events.readonly')
WRITE_SCOPE = 'https://www.googleapis.com/auth/calendar.events'
READ_ROLES = {'reader', 'writer', 'owner', 'writerWithoutPrivateAccess'}
PROFILE = {'singleEvents':'false', 'showDeleted':'true', 'maxResults':'250'}
CACHE_SECONDS = 300


def has_read_scopes(scopes):
    return SCOPES[2] in scopes and (SCOPES[3] in scopes or WRITE_SCOPE in scopes)


class GoogleError(ValueError):
    def __init__(self, status=502):
        self.status = status
        super().__init__('Google 인증/권한 확인 필요' if status in (400, 401, 403) else
                         'Google 연결 불가 — 이전 수집 결과는 보존됩니다')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method, url, data=None, headers=None):
    """Fixed HTTPS endpoints only; no redirect can forward bearer credentials."""
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname not in {
            'www.googleapis.com', 'oauth2.googleapis.com', 'openidconnect.googleapis.com'}:
        raise ValueError('허용되지 않은 Google endpoint입니다')
    headers = dict(headers or {})
    body = None
    if data is not None:
        is_json = '/calendar/v3/' in url
        body = json.dumps(data,ensure_ascii=False).encode('utf-8') if is_json else urlencode(data).encode('ascii')
        headers['Content-Type'] = 'application/json; charset=utf-8' if is_json else 'application/x-www-form-urlencoded'
    try:
        with build_opener(NoRedirect).open(Request(url, data=body, headers=headers, method=method), timeout=15) as resp:
            raw = resp.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise GoogleError(502)
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise GoogleError(502)
            return result
    except HTTPError as exc:
        raise GoogleError(exc.code) from None
    except (URLError, OSError, ValueError):
        raise GoogleError(502) from None


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')


def _protect(data, decrypt=False):
    """Windows user-bound DPAPI, no optional Python dependency."""
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buf), Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('Google 자격 보호/해제 실패 — 현재 OS 사용자로 다시 연결하십시오')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


class Credentials:
    def __init__(self, path):
        self.path = Path(path)
    def load(self):
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_bytes()
            raw = _protect(raw, True) if os.name == 'nt' else raw
            value = json.loads(raw)
            if not isinstance(value, dict) or not value.get('sub') or not value.get('access_token'):
                raise ValueError()
            return value
        except (OSError, ValueError):
            raise ValueError('Google 자격을 읽지 못했습니다 — 원본을 보존하고 다시 연결하십시오') from None
    def save(self, value):
        raw = operations.encoded(value).encode('utf-8')
        fileio.atomic_write(self.path, _protect(raw) if os.name == 'nt' else raw)
        if os.name != 'nt':
            self.path.chmod(0o600)


class MirrorStore(operations.Store):
    def __init__(self, registry):
        super().__init__(Path(registry).resolve().parent)
        self.path = self.path.parent.parent / 'google' / (operations.digest(str(Path(registry).resolve())) + '.sqlite3')
    def get_value(self, key, default=None):
        with self.connection() as db:
            if db is None:
                return copy.deepcopy(default)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='google_values'").fetchone():
                raise ValueError('Google mirror schema를 확인하십시오')
            row = db.execute('SELECT value FROM google_values WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else copy.deepcopy(default)
    def put(self, values):
        with self.connection(True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS google_values(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            for key, value in values.items():
                db.execute('INSERT INTO google_values VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (key, operations.encoded(value)))


class Service:
    def __init__(self, registry, transport=None, projects=None):
        self.registry = Path(registry).resolve()
        self.store = MirrorStore(self.registry)
        self.home = self.store.path.with_suffix('')
        self.credentials = Credentials(self.home / 'credentials.bin')
        self.transport = transport or request
        self.projects = projects or (lambda: [])
        self.mutex = threading.RLock()
        self.pending = None
        self.listener = None
        self.last_auth_error = None
        self.background_error = None
        self.after_sync = None
        self.refresh_stop = threading.Event()
        self._check_paths()

    def _check_paths(self, client=None):
        roots = [self.registry.parent]
        from . import wfhome
        for project in self.projects():
            roots.append(Path(project['path']).resolve())
            roots.append(wfhome.wf_home_for(project).resolve())
        for path in [self.store.path.resolve(), self.credentials.path.resolve()] + ([Path(client).resolve()] if client else []):
            if any(path == root or root in path.parents for root in roots):
                raise ValueError('Google 자격/DB/client 파일은 프로젝트·WF_HOME 밖에 두십시오')

    @contextmanager
    def locked(self):
        self._check_paths()
        with self.mutex, fileio.locked(self.home, timeout=5):
            yield

    def client(self):
        path = Path(os.environ.get('WSFLOW_GOOGLE_CLIENT_FILE') or self.store.path.parent.parent / 'google-client.json').expanduser().resolve()
        self._check_paths(path)
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))['installed']
            if not isinstance(value.get('client_id'), str) or not value['client_id'].endswith('.apps.googleusercontent.com'):
                raise ValueError()
            return {k:value.get(k, '') for k in ('client_id', 'client_secret')}
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('Desktop OAuth client JSON을 사용자 데이터/google-client.json 또는 WSFLOW_GOOGLE_CLIENT_FILE에 설정하십시오') from None

    def _key(self, token, kind, ident=''):
        return operations.encoded([token['sub'], kind, ident])

    def status(self):
        self._check_paths()
        configured, setup_error = True, None
        try:
            self.client()
        except ValueError as exc:
            configured, setup_error = False, str(exc)
        token = self.credentials.load()
        selected = self.store.get_value(self._key(token,'selected'), []) if token else []
        mirrors = [self.store.get_value(self._key(token,'mirror',cid), {}) for cid in selected] if token else []
        return {'configured':configured, 'setup_error':setup_error, 'connected':bool(token),
                'account':{'id':token['sub'], 'email':auth.mask_email(token.get('email',''))} if token else None,
                'scopes':token.get('scopes',[]) if token else [], 'selected_calendar_ids':selected,
                'pending':bool(self.pending and self.pending['expires_at'] > time.time()),
                'auth_error':self.last_auth_error, 'can_write':bool(token and WRITE_SCOPE in token.get('scopes',[])),
                'background_error':self.background_error,
                'last_sync':min((m.get('last_sync') or '' for m in mirrors),default='') or None,
                'complete':bool(selected) and all(m.get('complete',False) for m in mirrors),
                'state':('connected' if not selected or all(m.get('complete') for m in mirrors) else 'needs_sync') if token else 'disconnected'}

    def _token(self):
        token = self.credentials.load()
        if token is None:
            raise GoogleError(401)
        if token['expires_at'] <= time.time() + 60:
            if not token.get('refresh_token'):
                raise GoogleError(401)
            result = self.transport('POST',TOKEN_URL,{'grant_type':'refresh_token','refresh_token':token['refresh_token'],
                                    **token['client']})
            if not result.get('access_token'):
                raise GoogleError(401)
            token.update(access_token=result['access_token'], expires_at=time.time()+int(result.get('expires_in',3600)))
            if 'scope' in result and not has_read_scopes(result['scope'].split()):
                raise GoogleError(403)
            if 'scope' in result:
                token['scopes'] = result['scope'].split()
            if result.get('refresh_token'):
                token['refresh_token'] = result['refresh_token']
            self.credentials.save(token)
        return token

    def _get(self, token, path, params):
        return self.transport('GET',API_URL+path+'?'+urlencode(params),headers={'Authorization':'Bearer '+token['access_token']})

    def _pages(self, token, path, params, cursor=False):
        items, seen, page = [], set(), None
        for _ in range(100):
            if self.refresh_stop.is_set():
                raise GoogleError(503)
            result = self._get(token, path, {**params, **({'pageToken':page} if page else {})})
            batch = result.get('items',[])
            if not isinstance(batch,list) or any(not isinstance(i,dict) or not isinstance(i.get('id'),str) or not i['id'] for i in batch):
                raise GoogleError(502)
            items.extend(batch)
            page = result.get('nextPageToken')
            if not page:
                if cursor and (not isinstance(result.get('nextSyncToken'),str) or not result['nextSyncToken']):
                    raise GoogleError(502)
                return items, result.get('nextSyncToken')
            if not isinstance(page,str) or page in seen:
                raise GoogleError(502)
            seen.add(page)
        raise GoogleError(502)

    def calendars(self):
        with self.locked():
            token = self._token()
            items, _ = self._pages(token,'/users/me/calendarList',{'maxResults':'250'})
            values = [{**i, 'can_read':i.get('accessRole') in READ_ROLES and not i.get('deleted'),
                       'can_write':WRITE_SCOPE in token.get('scopes',[]) and i.get('accessRole') in {'owner','writer'}} for i in items if not i.get('deleted')]
            self.store.put({self._key(token,'calendars'):values})
            return {'calendars':values,'complete':True,'account_id':token['sub']}

    def select(self, ids):
        if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('캘린더 ID 목록을 선택하십시오')
        with self.locked():
            token = self._token()
            current = self.calendars()['calendars']
            readable = {i['id'] for i in current if i['can_read']}
            if set(ids)-readable:
                raise ValueError('읽기 권한이 있는 캘린더만 선택하십시오')
            self.store.put({self._key(token,'selected'):sorted(ids)})
            return self.status()

    def mirror(self, cid):
        token = self.credentials.load()
        return self.store.get_value(self._key(token,'mirror',cid), {}) if token else {}

    def sync(self):
        with self.locked():
            token = self._token()
            selected = self.store.get_value(self._key(token,'selected'), [])
            results = []
            try:
                readable = {c['id'] for c in self.calendars()['calendars'] if c['can_read']}
            except GoogleError as exc:
                readable = set()
            for cid in selected:
                key = self._key(token,'mirror',cid)
                previous = self.store.get_value(key,{})
                try:
                    if cid not in readable:
                        raise GoogleError(403)
                    full = not previous.get('cursor') or previous.get('profile') != PROFILE
                    params = {**PROFILE, **({'syncToken':previous['cursor']} if not full else {})}
                    try:
                        items, cursor = self._pages(token,'/calendars/'+quote(cid,safe='')+'/events',params,True)
                    except GoogleError as exc:
                        if exc.status != 410 or full:
                            raise
                        full = True
                        items, cursor = self._pages(token,'/calendars/'+quote(cid,safe='')+'/events',PROFILE,True)
                    mirror = {} if full else copy.deepcopy(previous.get('items',{}))
                    mirror.update({i['id']:i for i in items})
                    value = {'items':mirror,'cursor':cursor,'profile':PROFILE,'complete':True,
                             'generation':uuid4().hex if full else previous['generation'],
                             'last_sync':operations.now(),'error':None,'range_epoch':uuid4().hex}
                    self.store.put({key:value})
                    results.append({'calendar_id':cid,'complete':True,'last_sync':value['last_sync']})
                except GoogleError as exc:
                    self.store.put({key:{**previous,'complete':False,'error':str(exc)}})
                    results.append({'calendar_id':cid,'complete':False,'error':str(exc)})
            result = {'complete':all(r['complete'] for r in results),'calendars':results,'status':self.status()}
            if self.after_sync is not None:
                result['writes'] = self.after_sync()
            return result

    def range(self, start, end, zone='Asia/Seoul', force=False):
        first,last = calendar_query.validate_query(start,end,zone)
        with self.mutex:
            token = self.credentials.load()
            if token is None:
                return {'events':[],'sources':[],'complete':True,'connected':False}
        with self.locked():
            token = self.credentials.load()
            if token is None:
                return {'events':[],'sources':[],'complete':True,'connected':False}
            selected = self.store.get_value(self._key(token,'selected'),[])
            calendars = {i['id']:i for i in self.store.get_value(self._key(token,'calendars'),[])}
            events, sources = [], []
            auth_error = None
            try:
                token = self._token()
            except GoogleError as exc:
                auth_error = exc
            for cid in selected:
                mirror = self.store.get_value(self._key(token,'mirror',cid),{})
                cachekey = self._key(token,'range',[cid,start,end,zone])
                cache = self.store.get_value(cachekey,{})
                complete, error = False, None
                try:
                    if auth_error:
                        raise auth_error
                    if calendars.get(cid,{}).get('accessRole') not in READ_ROLES:
                        raise GoogleError(403)
                    if force or not cache or time.time()-cache.get('cached_at',0) >= CACHE_SECONDS or cache.get('epoch') != mirror.get('range_epoch'):
                        params = {'singleEvents':'true','showDeleted':'false','maxResults':'250','timeZone':zone,
                                  'timeMin':datetime.combine(first,day_time.min,ZoneInfo(zone)).isoformat(),
                                  'timeMax':datetime.combine(last,day_time.min,ZoneInfo(zone)).isoformat()}
                        items,_ = self._pages(token,'/calendars/'+quote(cid,safe='')+'/events',params)
                        converted = [self._event(i,token['sub'],cid,calendars[cid]) for i in items if i.get('status') != 'cancelled']
                        cache = {'events':converted,'cached_at':time.time(),'last_sync':operations.now(),
                                 'epoch':mirror.get('range_epoch')}
                        self.store.put({cachekey:cache})
                    complete = bool(mirror.get('complete'))
                    error = mirror.get('error')
                except (GoogleError, KeyError, TypeError, ValueError, OverflowError):
                    error = 'Google 기간 조회 확인 불가 — 이전 결과가 표시될 수 있습니다'
                events.extend(cache.get('events',[]))
                sources.append({'source_id':'google:'+operations.digest([token['sub'],cid]),'kind':'google',
                                'calendar_id':cid,'name':calendars.get(cid,{}).get('summary',cid),
                                'access_role':calendars.get(cid,{}).get('accessRole'),'can_write':False,
                                'last_sync':cache.get('last_sync'),'collection_last_sync':mirror.get('last_sync'),
                                'complete':complete,'error':error})
            unique = {e['id']:e for e in events}
            return {'events':list(unique.values()),'sources':sources,'complete':all(s['complete'] for s in sources),'connected':True}

    @staticmethod
    def _event(item, account, cid, calendar):
        if item.get('recurrence') or any(not isinstance(item.get(k,''),str) for k in ('summary','description','location')):
            raise ValueError('Google occurrence 형식이 올바르지 않습니다')
        start,end = item['start'],item['end']
        all_day = 'date' in start
        record = {'id':'G-'+operations.digest([account,cid,item['id']]),'remote_id':item['id'],'kind':'event',
                  'origin':'google','source_id':'google:'+operations.digest([account,cid]),'calendar_id':cid,
                  'calendar_name':calendar.get('summary',cid),'access_role':calendar.get('accessRole'),
                  'readonly':True,'title':item.get('summary') or '(제목 없음)','body':item.get('description',''),
                  'location':item.get('location',''),'project_ids':[],'all_day':all_day}
        if all_day:
            left,right = datetime.fromisoformat(start['date']).date(),datetime.fromisoformat(end['date']).date()
            if left >= right:
                raise ValueError()
            record.update(start_date=left.isoformat(),end_date=right.isoformat())
        else:
            left,right = datetime.fromisoformat(start['dateTime']),datetime.fromisoformat(end['dateTime'])
            if left.tzinfo is None or right.tzinfo is None or left.astimezone(timezone.utc) >= right.astimezone(timezone.utc):
                raise ValueError()
            record.update(start_at=left.isoformat(),end_at=right.isoformat(),timezone=start.get('timeZone',calendar.get('timeZone','UTC')))
        return record

    def connect(self, write=False):
        if type(write) is not bool:
            raise ValueError('쓰기 권한 선택이 올바르지 않습니다')
        with self.locked():
            client = self.client()
            self._stop_listener()
            nonce = secrets.token_urlsafe(32)
            self.store.put({'oauth_epoch':nonce})
            self.pending = {'state':secrets.token_urlsafe(32),'verifier':secrets.token_urlsafe(48),
                            'expires_at':time.time()+180,'epoch':nonce,'client':client,'write':write}
            service = self
            class Callback(BaseHTTPRequestHandler):
                def log_message(self,*args):
                    pass  # authorization code/state must never enter access logs
                def do_GET(self):
                    with service.mutex:
                        expected = '127.0.0.1:'+str(self.server.server_port)
                        url = urlsplit(self.path)
                        q = parse_qs(url.query,keep_blank_values=True)
                        pending = service.pending
                        valid = (self.headers.get('Host') == expected and url.path == '/oauth/callback' and
                                 pending is not None and pending['expires_at'] > time.time() and
                                 len(q.get('state',[])) == 1 and secrets.compare_digest(q['state'][0],pending['state']))
                        status, text = 400, 'Google 연결을 확인하지 못했습니다. 앱에서 다시 시도하십시오.'
                        if valid:
                            service.pending = None  # consume before exchanging the code
                            try:
                                with service.locked():
                                    if service.store.get_value('oauth_epoch') != pending['epoch']:
                                        raise GoogleError(401)
                                    if 'error' in q or len(q.get('code',[])) != 1 or not q['code'][0]:
                                        raise GoogleError(400)
                                    result = service.transport('POST',TOKEN_URL,{'grant_type':'authorization_code',
                                        'code':q['code'][0],'redirect_uri':pending['redirect_uri'],
                                        'code_verifier':pending['verifier'],**client})
                                    scopes = result.get('scope','').split()
                                    if not result.get('access_token') or not has_read_scopes(scopes):
                                        raise GoogleError(403)
                                    if pending['write'] and WRITE_SCOPE not in scopes:
                                        raise GoogleError(403)
                                    user = service.transport('GET',USER_URL,headers={'Authorization':'Bearer '+result['access_token']})
                                    if not isinstance(user.get('sub'),str) or not user['sub']:
                                        raise GoogleError(401)
                                    value = {'sub':user['sub'],'email':user.get('email',''),'client':client,'scopes':scopes,
                                             'access_token':result['access_token'],'refresh_token':result.get('refresh_token'),
                                             'expires_at':time.time()+int(result.get('expires_in',3600))}
                                    if service.refresh_stop.is_set():
                                        raise GoogleError(401)
                                    service.credentials.save(value)
                                    service.last_auth_error = None
                                    status,text = 200,'Google 연결 완료. 이 창을 닫고 wsFlow로 돌아가십시오.'
                            except (ValueError, OSError, KeyError, TypeError):
                                service.last_auth_error = 'Google 연결 실패 — 권한과 Desktop client를 확인하십시오'
                        raw = text.encode('utf-8')
                        self.send_response(status);self.send_header('Content-Type','text/plain; charset=utf-8')
                        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
                        self.end_headers();self.wfile.write(raw)
                        if valid:
                            stop.set()
            class Listener(HTTPServer):
                def get_request(self):
                    conn,addr = super().get_request()
                    conn.settimeout(3)
                    return conn,addr
            listener = Listener(('127.0.0.1',0),Callback)
            listener.timeout = .2
            stop = threading.Event()
            self.listener = (listener,stop)
            redirect = f'http://127.0.0.1:{listener.server_port}/oauth/callback'
            self.pending['redirect_uri'] = redirect
            def run():
                try:
                    deadline = time.time()+180
                    while not stop.is_set() and time.time() < deadline:
                        listener.handle_request()
                finally:
                    listener.server_close()
            threading.Thread(target=run,daemon=True).start()
            return {'authorization_url':AUTH_URL+'?'+urlencode({'client_id':client['client_id'],
                    'redirect_uri':redirect,'response_type':'code','scope':' '.join((*SCOPES[:3],WRITE_SCOPE) if write else SCOPES),
                    'state':self.pending['state'],'code_challenge':challenge(self.pending['verifier']),
                    'code_challenge_method':'S256','access_type':'offline','prompt':'consent select_account'}),
                    'expires_in':180}

    def _stop_listener(self):
        self.pending = None
        if self.listener:
            self.listener[1].set()
            self.listener = None

    def disconnect(self):
        with self.locked():
            self._stop_listener()
            self.store.put({'oauth_epoch':secrets.token_urlsafe(32)})
            token = self.credentials.load()
            revoked = token is None
            try:
                if token:
                    self.transport('POST',REVOKE_URL,{'token':token.get('refresh_token') or token['access_token']})
                    revoked = True
            except GoogleError:
                pass
            finally:
                self.credentials.path.unlink(missing_ok=True)
            return {'connected':False,'revoked':revoked,'warning':None if revoked else 'Google 철회 미확인 — 계정 권한 화면에서 확인하십시오'}

    def close(self):
        self.refresh_stop.set()
        self._stop_listener()

    def refresh_if_due(self):
        """Called by the app's bounded worker, independent of the visible tab."""
        status = self.status()
        if status['connected'] and status['selected_calendar_ids']:
            last = status['last_sync']
            if not last or not status['complete'] or (datetime.now(timezone.utc)-datetime.fromisoformat(last)).total_seconds() >= CACHE_SECONDS:
                return self.sync()
        return None

    def start_refresh(self):
        def run():
            while not self.refresh_stop.wait(CACHE_SECONDS):
                try:
                    self.refresh_if_due()
                    self.background_error = None
                except Exception:  # keep the worker alive; explicit UI requests report failures
                    self.background_error = 'Google 자동 수집 확인 필요 — 마지막 수집 시각을 확인하십시오'
        threading.Thread(target=run,daemon=True).start()
