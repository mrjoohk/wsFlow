"""Bounded C2 preflight only; no business imports, providers or original SQLite opens.

Temporary SQLite clones are analysis inputs, never backups or atomic snapshots.
The cooperative Reader/path checks do not serialize non-cooperating writers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import datetime

from .path_policy import plain_path
from .readmodel import Reader, MAX_SOURCE_BYTES, fingerprint

FORMATS = {'work-calendar-v1', 'workspace-operations-v1', 'module-data-v1'}
BUILTINS = {'work-calendar', 'workspace-operations'}
MAX_PROJECTS = 64
MAX_ROOTS = 64
MAX_FILES = 2048
MAX_EXTERNAL_FILES = 256
MAX_DESCRIPTORS = 1024
MAX_READ_BYTES = 64 * 1024 * 1024  # Initial observation AND final byte recheck.
MAX_ROWS = 100000
OP_PENDING = {'planned', 'approved', 'executing', 'uncertain'}
OP_TERMINAL = {'succeeded', 'failed'}  # operations.Store.finish/reject_unexecuted
OUTBOX_PENDING = {'queued', 'syncing', 'uncertain', 'error', 'conflict'}
OUTBOX_TERMINAL = {'synced', 'cancelled'}  # calendar_sync state machine
SECRET_NAME = re.compile(r'credential|secret|token|password|oauth.?client', re.I)
LIMITS = [f'per_file_bytes={MAX_SOURCE_BYTES}', f'total_read_bytes={MAX_READ_BYTES} (includes final byte recheck)',
          f'projects={MAX_PROJECTS}', f'roots={MAX_ROOTS}', f'files={MAX_FILES}',
          f'external_namespace_entries={MAX_EXTERNAL_FILES}', f'descriptors={MAX_DESCRIPTORS}',
          f'sqlite_rows_per_table={MAX_ROWS}',
          'preflight_only: no migration, rollback, writer admission or activation guarantee',
          'SQLite clones are temporary analysis copies, not backups or atomic snapshots',
          'cooperative observations; no OS atomic file-set snapshot or serialization of non-cooperating writers',
          'credentials are references only; their bytes are never opened or hashed']


def validate_contract(value) -> dict:
    if value is None:
        return {}
    if (not isinstance(value, dict) or set(value) != {'version', 'preserves', 'pending_policy'}
            or type(value.get('version')) is not int or value['version'] != 1
            or value.get('pending_policy') != 'block'):
        raise ValueError('잘못된 data_compatibility 계약입니다')
    values = value['preserves']
    if (not isinstance(values, list) or any(not isinstance(v, str) or v not in FORMATS for v in values)
            or len(values) != len(set(values))):
        raise ValueError('지원하지 않거나 중복된 자료 format입니다')
    return {'version': 1, 'preserves': list(values), 'pending_policy': 'block'}


def _digest(value):
    # Matches operations.digest: JSON STRING encoding matters for storage names.
    return fingerprint(value)


def _modules(value):
    return value.get('modules', value)


def _canonical_set(values):
    """Stable JSON ordering for observation collections whose order has no role."""
    unique = {json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')): value
              for value in values}
    return [unique[key] for key in sorted(unique)]


def _canonical_stores(stores):
    return _canonical_set([{**store, 'owners': sorted(set(store['owners'])),
                            'sources': _canonical_set(store['sources'])} for store in stores])


def _affected(current, proposed, action):
    if action not in {'update', 'replace', 'restore', 'install'}:
        return []
    changed = set()
    # Pinning identical bundled bytes into the managed cache is not a code
    # transition. Digest identifies those bytes independently of cache location.
    code = ('package_digest', 'version', 'entrypoint')
    for ident in set(current) | set(proposed):
        old, new = current.get(ident, {}), proposed.get(ident, {})
        if not (old.get('installed') or new.get('installed')):
            continue
        if (any(old.get(k) != new.get(k) for k in code)
                or old.get('installed', False) != new.get('installed', False)
                or (action == 'restore' and new.get('installed') and new.get('enabled') and not old.get('enabled'))):
            changed.add(ident)
    # Consumers may hold references to the changed implementation, even when their
    # own package digest is unchanged. Use both graphs for removed dependencies.
    while True:
        before = set(changed)
        for modules in (current, proposed):
            for ident, record in modules.items():
                if record.get('installed') and set(record.get('requires', [])) & changed:
                    changed.add(ident)
        if before == changed:
            return sorted(changed)


class _Audit:
    def __init__(self):
        self.reader = Reader()
        self.stores = []
        self.blockers = []
        self.read_bytes = 0
        self.initial = {}
        self.directories = {}
        self.references = {}
        self.operations = {}
        self.sqlite_seen = set()
        self.approvals = []
        self.file_count = 0

    def block(self, code, store, message):
        value = {'code': code, 'store_id': store['store_id'] if isinstance(store, dict) else store, 'message': message}
        if value not in self.blockers:
            self.blockers.append(value)
        if isinstance(store, dict):
            store['status'] = 'blocked'

    def store(self, path, owners, role, format_id, kind='file'):
        path = Path(path).absolute()
        for store in self.stores:
            if store['path'] == str(path):
                store['owners'] = sorted(set(store['owners']) | set(owners))
                return store
        if len(self.stores) >= MAX_DESCRIPTORS:
            raise ValueError('descriptor_limit')
        store = {'store_id': role + ':' + hashlib.sha256(str(path).encode()).hexdigest()[:20],
                 'path': str(path), 'owners': sorted(owners), 'role': role, 'format_id': format_id,
                 'kind': kind, 'status': 'absent', 'present': False, 'counts': {}, 'pending_count': 0, 'sources': []}
        self.stores.append(store)
        return store

    @staticmethod
    def boundary(path):
        # Validate ALL ancestors, not just a linked directory declared as owner.
        return plain_path(path, Path(path.anchor))

    def read(self, path, store):
        path = Path(path).absolute()
        if path in self.initial:
            info, raw, previous = self.initial[path]
            if info not in store['sources']:
                store['sources'].append(info)
            if raw is not None:
                store.update(present=True)
                if store['status'] != 'blocked':
                    store['status'] = 'observed'
            return raw
        self.file_count += 1
        if self.file_count > MAX_FILES:
            self.block('file_limit', store, '파일 관측 상한을 초과했습니다')
            return None
        try:
            self.boundary(path)
            info_stat = path.stat() if path.exists() else None
            if info_stat is not None and not stat.S_ISREG(info_stat.st_mode):
                self.block('unsupported_source', store, '일반 파일이 아닌 자료는 해석할 수 없습니다')
                return None
            size = info_stat.st_size if info_stat is not None else 0
            if size > MAX_SOURCE_BYTES or self.read_bytes + size * 2 > MAX_READ_BYTES:
                self.block('read_limit', store, '파일/총 읽기 상한을 초과했습니다')
                return None
            self.reader.files.append(path)
            info, raw = self.reader.read(path, store['store_id'], ','.join(store['owners']), boundary=Path(path.anchor))
            self.read_bytes += len(raw) if raw is not None else 0
            self.initial[path] = (info, raw, store)
            store['sources'].append(info)
            if info['status'] == 'ok':
                store.update(present=True)
                if store['status'] != 'blocked':
                    store['status'] = 'observed'
            elif info['status'] != 'missing':
                self.block('unreadable_source', store, '원본을 완전히 관측하지 못했습니다')
            return raw
        except (OSError, ValueError, RuntimeError):
            self.block('path_or_read_error', store, '경계·링크·읽기 상태를 확인하지 못했습니다')
            return None

    def reference(self, path, owners, role='credential-reference'):
        store = self.store(path, owners, role, 'reference-only', 'reference')
        try:
            self.boundary(Path(store['path']))
            signature = self._stat(Path(store['path']))
            self.references[store['path']] = (signature, store)
            store.update(present=signature is not None, status='reference_only')
        except (OSError, ValueError, RuntimeError):
            self.block('reference_boundary', store, '자격 참조 경계를 확인하지 못했습니다')
        return store

    @staticmethod
    def _stat(path):
        try:
            st = path.stat()
            return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode]
        except FileNotFoundError:
            return None

    def listing(self, path, store, external=False):
        path = Path(path)
        try:
            self.boundary(path)
            if not path.exists():
                self.directories[str(path)] = (None, store)
                return []
            if not path.is_dir():
                raise ValueError('not_directory')
            entries = []
            cap = MAX_EXTERNAL_FILES if external else MAX_FILES
            # os.walk does not follow directory links. Every discovered component
            # is checked before stat or any body read.
            for parent, dirs, files in os.walk(path, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
                for name in sorted(dirs + files):
                    item = Path(parent) / name
                    self.boundary(item)
                    entries.append((str(item.relative_to(path)), self._stat(item)))
                    if len(entries) > cap:
                        raise ValueError('namespace_limit')
            result = sorted(entries)
            self.directories[str(path)] = ((self._stat(path), result), store)
            if result:
                store.update(present=True)
                if store['status'] != 'blocked':
                    store['status'] = 'observed'
            return [path / rel for rel, sig in result if sig and not (sig[4] & 0o170000) == 0o040000]
        except (OSError, ValueError, RuntimeError):
            self.block('namespace_unknown', store, '자료 namespace의 링크·형식·관측 상한을 확인하십시오')
            return []

    def tree(self, path, owners, role, format_id, opaque=False):
        store = self.store(path, owners, role, format_id, 'namespace')
        files = self.listing(path, store, external=opaque)
        store['counts']['files'] = len(files)
        if opaque:
            if store['present']:
                self.block('opaque_data', store, '비어 있지 않은 외부 모듈 자료는 명시적 migrator 계약이 필요합니다')
            return store
        workflow_marker = None
        batch_receipts = []
        for file in files:
            if any(SECRET_NAME.search(part) for part in file.relative_to(Path(path)).parts):
                self.reference(file, owners)
                continue
            if (role not in {'canonical-skills', 'projected-skills'}
                    and file.suffix.lower() not in {'.json', '.jsonl', '.md', '.txt', '.bak', '.lock'}
                    and not (role == 'common-receipts' and file.name == 'before.bin')):
                self.block('unknown_format', store, '알 수 없는 업무 자료 형식입니다')
                continue
            raw = self.read(file, store)
            if raw is None:
                continue
            if role in {'canonical-skills', 'projected-skills'}:
                # context_skills.package accepts non-Markdown assets as bytes,
                # including arbitrary JSON containers and binary contents. Their
                # preservation observation does not imply a business-state schema.
                continue
            if file.suffix.lower() in {'.json', '.jsonl'}:
                try:
                    values = [json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()] if file.suffix == '.jsonl' else [json.loads(raw.decode('utf-8-sig'))]
                    if any(not isinstance(value, dict) for value in values):
                        raise ValueError('object_required')
                    if role == 'workflow':
                        if file.name in {'state.json','state.before.json'} and (type(values[0].get('schema_version')) is not int or values[0]['schema_version'] not in {3, 4}):
                            raise ValueError('workflow_schema')
                        if file == Path(path) / 'state.json':
                            workflow_marker = (values[0].get('last_batch') or {}).get('run_id')
                        if file.name == 'receipt.json' and file.parent.parent == Path(path) / 'batches':
                            receipt = values[0]
                            if (type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1
                                    or not isinstance(receipt.get('run_id'), str)
                                    or receipt.get('status') not in {'executing','failed','succeeded'}
                                    or datetime.fromisoformat(receipt.get('created_at', '').replace('Z','+00:00')).tzinfo is None):
                                raise ValueError('batch_schema')
                            batch_receipts.append(receipt)
                    elif role == 'common-receipts' and file.name == 'receipt.json':
                        receipt = dict(values[0]); checksum = receipt.pop('sha256', None)
                        if (set(receipt) != {'plan_id','operation_id','binding','status','result'}
                                or receipt.get('status') not in {'succeeded','failed','uncertain'}
                                or receipt.get('operation_id') != file.parent.name
                                or checksum != _digest(receipt)):
                            raise ValueError('common_receipt_schema')
                except (ValueError, UnicodeError, IndexError, AttributeError, TypeError):
                    self.block('unknown_schema', store, '업무 자료 schema를 확인하지 못했습니다')
        if role == 'workflow':
            pending = sum(value['status'] == 'executing' and value['run_id'] != workflow_marker for value in batch_receipts)
            store['counts']['running_or_interrupted_batches'] = pending
            store['pending_count'] += pending
            if pending:
                self.block('pending_batch', store, '진행 중이거나 중단된 배치 작업을 확인해야 합니다')
        return store

    def json_file(self, path, owners, role, format_id, schema=None):
        store = self.store(path, owners, role, format_id)
        raw = self.read(path, store)
        if raw is None:
            return None, store
        try:
            value = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(value, dict) or (schema is not None and (type(value.get('schema_version')) is not int or value['schema_version'] != schema)):
                raise ValueError('schema')
            if role == 'git-fetch-journal' and (set(value) != {'last_fetch_at','last_attempt','last_error'}
                    or any(v is not None and not isinstance(v, str) for v in value.values())):
                raise ValueError('git_journal_schema')
            return value, store
        except (UnicodeError, ValueError):
            self.block('unknown_schema', store, '자료 JSON/schema를 확인하지 못했습니다')
            return None, store

    def sqlite(self, path, owners, role, format_id):
        store = self.store(path, owners, role, format_id, 'sqlite')
        # A shared home may be referenced by multiple registered checkouts.
        # Merge ownership through store() first, then observe/count its DB once.
        if store['path'] in self.sqlite_seen:
            return store
        self.sqlite_seen.add(store['path'])
        parts = {suffix: self.read(Path(str(path) + suffix), store) for suffix in ('', '-wal', '-shm')}
        if parts[''] is None:
            if any(parts[suffix] is not None for suffix in ('-wal', '-shm')):
                self.block('orphan_sqlite_sidecar', store, '본체 없는 SQLite sidecar가 있습니다')
            return store
        try:
            with tempfile.TemporaryDirectory(prefix='wsflow-continuity-sqlite-') as folder:
                clone = Path(folder) / 'observed.sqlite3'
                for suffix, raw in parts.items():
                    if raw is not None:
                        Path(str(clone) + suffix).write_bytes(raw)
                # Only an owned temporary clone is ever opened by SQLite. Normal
                # WAL recovery is essential: immutable=1 would miss pending WAL.
                with closing(sqlite3.connect(str(clone))) as db:
                    db.execute('PRAGMA trusted_schema=OFF')
                    db.execute('PRAGMA query_only=ON')
                    if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                        raise ValueError('integrity')
                    self._sqlite_status(db, store)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            self.block('unknown_sqlite_schema', store, 'SQLite 사본의 schema/상태를 완전히 확인하지 못했습니다')
        return store

    def _sqlite_status(self, db, store):
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('user_version')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'plans':{'id','snapshot'}, 'operations':{'id','intent','status','approved_at','result','error','local_recorded'},
                    'events':{'sequence','operation_id','status','detail','at'}}
        optional = {}
        if store['role'] == 'calendar':
            required['calendar_records'] = {'id','data'}
            optional = {'calendar_links':{'local_id','account_id','calendar_id','remote_id','data'},
                        'calendar_outbox':{'id','local_id','intent','state','result','error'}}
            if set(optional) & tables:
                required.update(optional)
        elif store['role'] == 'google-mirror':
            required['google_values'] = {'key','value'}
        if tables - (set(required) | {'sqlite_sequence'}):
            raise ValueError('unknown_table')
        for table, columns in required.items():
            if table not in tables or {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')} != columns:
                raise ValueError('missing_or_changed_table')
            count = db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
            if count > MAX_ROWS:
                raise ValueError('row_limit')
            store['counts'][table] = count
        # Validate persisted JSON containers in SQLite without selecting their
        # business text. SQLite's JSON parser only operates on the owned clone.
        json_columns = {'plans':['snapshot'], 'operations':['intent'], 'events':['detail']}
        if 'calendar_records' in tables:
            json_columns['calendar_records'] = ['data']
        if 'calendar_links' in tables:
            json_columns['calendar_links'] = ['data']
            json_columns['calendar_outbox'] = ['intent']
        if 'google_values' in tables:
            json_columns['google_values'] = ['value']
        for table, columns in json_columns.items():
            for column in columns:
                if db.execute('SELECT count(*) FROM ' + table + ' WHERE NOT json_valid(' + column + ')').fetchone()[0]:
                    raise ValueError('invalid_json')
                if table != 'google_values' and db.execute('SELECT count(*) FROM ' + table + " WHERE json_type(" + column + ") != 'object'").fetchone()[0]:
                    raise ValueError('invalid_json_container')
        if db.execute("SELECT count(*) FROM operations WHERE json_type(intent,'$.kind') IS NOT 'text' OR local_recorded NOT IN (0,1)").fetchone()[0]:
            raise ValueError('invalid_operation_metadata')
        rows = db.execute("SELECT id,status,local_recorded,json_extract(intent,'$.kind') FROM operations").fetchall()
        effect_rows = rows
        if store['role'] == 'session-operations':
            # Session receipts are immutable submitted observations. The actual
            # producer stores these as planned but has no approve/claim path.
            passive = [row for row in rows if row[3] == 'session_receipt' and row[1] == 'planned']
            store['counts']['passive_session_receipts'] = len(passive)
            effect_rows = [row for row in rows if not (row[3] == 'session_receipt' and row[1] == 'planned')]
            if effect_rows:
                self.block('unknown_session_operation', store, '알 수 없는 세션 작업 종류나 상태가 있습니다')
        statuses = {}
        for ident, status, local, kind in effect_rows:
            statuses[status] = statuses.get(status, 0) + 1
        if any(status not in OP_PENDING | OP_TERMINAL for status in statuses):
            self.block('unknown_operation_state', store, '알 수 없는 작업 상태가 있습니다')
        store['counts']['operation_states'] = {str(k): v for k, v in statuses.items() if k in OP_PENDING | OP_TERMINAL}
        store['pending_count'] += sum(n for state, n in statuses.items() if state in OP_PENDING)
        if store['pending_count']:
            self.block('pending_operations', store, '미처리 작업이 있어 코드 전환을 예약할 수 없습니다')
        # Extract only kind, never select operation payload/result text. Jira
        # create success still needs local memo recording; every Jira success
        # requires its append-only pushed export (checked after all stores).
        self.operations[store['path']] = {ident: (status, local, kind) for ident, status, local, kind in rows}
        unrecorded = sum(status == 'succeeded' and kind == 'create_issue' and local != 1 for _, status, local, kind in rows)
        store['counts']['unrecorded_local_success'] = unrecorded
        if unrecorded:
            self.block('unrecorded_success', store, '성공 작업의 로컬 기록 복구가 필요합니다')
        if 'calendar_outbox' in tables:
            states = dict(db.execute('SELECT state,count(*) FROM calendar_outbox GROUP BY state'))
            store['counts']['outbox_states'] = {str(k): v for k, v in states.items() if k in OUTBOX_PENDING | OUTBOX_TERMINAL}
            if any(state not in OUTBOX_PENDING | OUTBOX_TERMINAL for state in states):
                self.block('unknown_outbox_state', store, '알 수 없는 Google outbox 상태가 있습니다')
            pending = sum(n for state, n in states.items() if state in OUTBOX_PENDING)
            store['pending_count'] += pending
            if pending:
                self.block('pending_outbox', store, 'Google 전송 대기·미확인 작업이 있습니다')

    def legacy(self, home, owners, op_path):
        store = self.store(home / '.workflow/approvals.jsonl', owners, 'legacy-approvals', 'work-calendar-v1')
        raw = self.read(Path(store['path']), store)
        if raw is not None:
            try:
                rows = [json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
                if any(not isinstance(row, dict) or row.get('event') not in {'approved','pushed'} or not isinstance(row.get('id'), str) or not row['id'] for row in rows):
                    raise ValueError('legacy_format')
                self.approvals.append((store, rows, str(op_path)))
                store['counts']['events'] = len(rows)
            except (ValueError, UnicodeError):
                self.block('unknown_legacy_approval', store, '구형 승인 기록의 형식을 확인하지 못했습니다')
        else:
            self.approvals.append((store, [], str(op_path)))

    def reconcile_legacy(self):
        for store, rows, op_path in self.approvals:
            pushed = {row['id'] for row in rows if row['event'] == 'pushed'}
            ops = self.operations.get(op_path, {})
            unknown = sum(row['event'] == 'approved' and row['id'] not in pushed and row['id'] not in ops for row in rows)
            missing = sum(status == 'succeeded' and kind in {'comment','create_issue','transition'} and ident not in pushed for ident, (status, local, kind) in ops.items())
            store['counts'].update(legacy_uncertain=unknown, missing_success_export=missing)
            store['pending_count'] = unknown + missing
            if unknown:
                self.block('legacy_uncertain', store, '구형 승인 작업의 원격 효과를 확인해야 합니다')
            if missing:
                self.block('unrecorded_success', store, '성공 작업의 승인 로그 반영이 필요합니다')

    def finish(self):
        # Re-read bytes through the same Reader; stat-only rechecks alone cannot
        # notice an equal-size edit whose mtime was restored by a writer.
        for path, (before, raw, store) in list(self.initial.items()):
            try:
                self.boundary(path)
                if self.read_bytes + (len(raw) if raw is not None else 0) > MAX_READ_BYTES:
                    raise ValueError('read_limit')
                after, current = self.reader.read(path, store['store_id'], ','.join(store['owners']), boundary=Path(path.anchor))
                self.read_bytes += len(current) if current is not None else 0
                if (after['status'], after.get('sha256')) != (before['status'], before.get('sha256')):
                    raise ValueError('changed')
            except (OSError, ValueError, RuntimeError):
                self.block('source_changed', store, '검사 중 업무 원본이 변경되었습니다')
        before_dirs = dict(self.directories)
        for path, (before, store) in before_dirs.items():
            self.listing(Path(path), store, external=store['format_id'] == 'module-data-v1')
            if self.directories.get(path, (None,))[0] != before:
                self.block('source_changed', store, '검사 중 자료 파일 집합이 변경되었습니다')
        for path, (before, store) in self.references.items():
            try:
                self.boundary(Path(path))
                if before != self._stat(Path(path)):
                    raise ValueError('changed')
            except (OSError, ValueError, RuntimeError):
                self.block('source_changed', store, '검사 중 자격 참조 상태가 변경되었습니다')
        if not self.reader.finish():
            for path, (info, raw, store) in self.initial.items():
                if info['status'] not in {'ok','missing'}:
                    self.block('source_changed', store, '원본 관측이 변경되었거나 불완전합니다')


def audit_transition(registry: Path, host_root: Path, current: dict, proposed: dict,
                     action: str, module_id: str | None) -> dict:
    old, new = _modules(current), _modules(proposed)
    affected = _affected(old, new, action)
    audit = _Audit()
    if affected:
        try:
            registry, host_root = Path(registry).absolute(), Path(host_root).absolute()
            owners = sorted(set(affected) & BUILTINS)
            projects, roots = [], []
            if owners:
                config, registry_store = audit.json_file(registry, owners, 'registry', 'registry-v1')
                if config is None and registry_store['status'] == 'absent':
                    config = {'projects':[], 'roots':[]}
                if config is None:
                    config = {}
                projects, roots = config.get('projects', []), config.get('roots', [])
                if (not isinstance(projects, list) or not isinstance(roots, list)
                        or len(projects) > MAX_PROJECTS or len(roots) > MAX_ROOTS
                        or any(not isinstance(p, dict) or not isinstance(p.get('path'), str) or not Path(p['path']).is_absolute() for p in projects + roots)
                        or any(not isinstance(p.get('id'), str) for p in projects)):
                    audit.block('registry_limit_or_schema', registry_store, '등록 목록 형식·프로젝트 수 상한을 확인하십시오')
                    projects, roots = [], []
                elif len({p['id'] for p in projects}) != len(projects):
                    audit.block('registry_identity', registry_store, '중복 프로젝트 등록입니다')
                declared_base = Path(os.environ.get('WSFLOW_DATA_DIR') or (Path(os.environ.get('APPDATA') or Path.home() / '.config') / 'wsFlow')).expanduser().absolute()
                audit.boundary(declared_base)
                base = declared_base.resolve()
                for item in roots:
                    audit.reference(Path(item['path']), owners, 'root-boundary')
                    audit.json_file(Path(item['path']) / '.wsflow/workspace.json', owners, 'root-identity', 'identity-v1', 1)
                    if 'workspace-operations' in owners:
                        _context_owner(audit, Path(item['path']), owners, root=True)
                homes = set()
                for project in projects:
                    checkout = Path(project['path']).absolute()
                    home = Path(project.get('wf_home') or checkout).absolute()
                    audit.reference(checkout, owners, 'project-boundary')
                    audit.reference(home, owners, 'home-boundary')
                    audit.json_file(checkout / '.wsflow/project.json', owners, 'project-identity', 'identity-v1', 1)
                    for path in sorted({checkout, home}, key=str):
                        resolved = path.resolve()
                        op = base / 'operations' / (_digest(str(resolved)) + '.sqlite3')
                        audit.sqlite(op, owners, 'shared-operations', 'shared-operations-v1')
                    if 'workspace-operations' in owners:
                        audit.json_file(base / 'operations/git' / (_digest(str(checkout.resolve())) + '.json'), owners, 'git-fetch-journal', 'workspace-operations-v1')
                        _context_owner(audit, checkout, owners)
                    if 'work-calendar' in owners and str(home) not in homes:
                        homes.add(str(home))
                        audit.tree(home / '.workflow', owners, 'workflow', 'work-calendar-v1')
                        audit.tree(home / 'daily', owners, 'daily', 'work-calendar-v1')
                        audit.tree(home / 'weekly', owners, 'weekly', 'work-calendar-v1')
                        audit.legacy(home, owners, base / 'operations' / (_digest(str(home.resolve())) + '.sqlite3'))
                reg = str(registry.resolve())
                if 'work-calendar' in owners:
                    audit.sqlite(base / 'calendar' / (_digest(reg) + '.sqlite3'), owners, 'calendar', 'work-calendar-v1')
                    audit.sqlite(base / 'google' / (_digest(reg) + '.sqlite3'), owners, 'google-mirror', 'work-calendar-v1')
                    audit.reference(base / 'google' / _digest(reg) / 'credentials.bin', owners)
                    audit.reference(Path(os.environ.get('APPDATA') or os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'wsFlow/credentials.json', owners)
                    audit.reference(Path(os.environ.get('WSFLOW_GOOGLE_CLIENT_FILE') or base / 'google-client.json').expanduser().absolute(), owners, 'oauth-client-reference')
                if 'workspace-operations' in owners:
                    for suffix, role in (('.context','context-operations'), ('.sessions','session-operations')):
                        audit.sqlite(base / 'operations' / (_digest(str(Path(reg + suffix).resolve())) + '.sqlite3'), owners, role, 'workspace-operations-v1')
                    audit.tree(base / 'session-evidence' / _digest(reg), owners, 'session-evidence', 'workspace-operations-v1')
                for op_path, rows in audit.operations.items():
                    if next((s['role'] for s in audit.stores if s['path'] == op_path), '') != 'shared-operations':
                        continue
                    for ident, (status, local, kind) in rows.items():
                        if kind != 'local_write':
                            continue
                        if not isinstance(ident, str) or not re.fullmatch(r'A-[0-9a-f]{32}', ident):
                            audit.block('unknown_operation_format', 'shared-operations', '공통 작업 식별자 형식을 확인하십시오')
                            continue
                        audit.tree(base / 'operations/common-receipts' / ident, owners, 'common-receipts', 'shared-operations-v1')
                audit.reconcile_legacy()
            for ident in affected:
                # Includes builtin namespaces: bundled code currently does not
                # store here, but replacement packages can use context.data_dir.
                audit.tree(host_root / 'data' / ident, [ident], 'module-data', 'module-data-v1', opaque=True)
            audit.finish()
            for ident in affected:
                target = new.get(ident, {})
                if not target.get('installed'):
                    continue  # different-ID replacement retains old data, no adoption
                try:
                    contract = validate_contract(target.get('data_compatibility'))
                except ValueError:
                    audit.block('invalid_contract', 'module:' + ident, '대상 자료 보존 계약이 잘못되었습니다')
                    continue
                if ident in BUILTINS:
                    relevant = [s for s in audit.stores if ident in s['owners'] and s['present']
                                and s['role'] not in {'registry','root-boundary','project-boundary','home-boundary'}]
                    if relevant and ident + '-v1' not in contract.get('preserves', []):
                        audit.block('preservation_contract_required', 'module:' + ident, '기존 builtin 자료의 포맷 보존 선언이 필요합니다')
                if old.get(ident, {}).get('installed') and old[ident].get('data_schema_version') != target.get('data_schema_version'):
                    audit.block('data_schema_change', 'module:' + ident, '자료 schema 자동 전환은 지원하지 않습니다')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            audit.block('audit_unknown', 'audit', '자료 범위나 상태를 완전히 확인하지 못했습니다')
    stores = _canonical_stores(audit.stores)
    blockers = _canonical_set(audit.blockers)
    report = {'schema_version':1, 'scope':'preflight_only', 'ready':not blockers,
              'affected_modules':affected, 'stores':stores, 'blockers':blockers,
              'pending_count':sum(store['pending_count'] for store in stores), 'limits':list(LIMITS)}
    report['fingerprint'] = fingerprint({'affected_modules':affected, 'stores':stores,
                                         'blockers':blockers, 'directories':{path:value[0] for path,value in audit.directories.items()},
                                         'references':{path:value[0] for path,value in audit.references.items()},
                                         'contracts':{ident:new.get(ident, {}).get('data_compatibility') for ident in affected}})
    return report


def _context_owner(audit, owner, owners, root=False):
    """Observe canonical/projected references without running context producers."""
    profile, source = audit.json_file(owner / ('.wsflow/workspace.json' if root else '.wsflow/project.json'),
                                      owners, 'root-identity' if root else 'project-identity', 'identity-v1', 1)
    profile = profile or {}
    manifest, manifest_source = audit.json_file(owner / '.wsflow/manifest.json', owners, 'context-manifest', 'workspace-operations-v1', 1)

    def relative(value, source):
        if (not isinstance(value, str) or not value or Path(value).is_absolute()
                or '..' in Path(value).parts or ':' in value or '\\' in value):
            audit.block('context_reference_boundary', source, 'context 참조 경로가 소유 범위를 벗어납니다')
            return None
        return owner / value

    rules = relative(profile.get('rules_path', 'GLOBAL_RULES.md' if root else 'PROJECT_RULES.md'), source)
    skills = relative(profile.get('skills_path', 'skills'), source)
    for path in [owner / 'AGENTS.md', owner / 'CLAUDE.md'] + ([rules] if rules else []):
        if any(SECRET_NAME.search(part) for part in path.relative_to(owner).parts):
            audit.reference(path, owners)
        else:
            audit.read(path, audit.store(path, owners, 'context-source', 'workspace-operations-v1'))
    if skills:
        audit.tree(skills, owners, 'canonical-skills', 'workspace-operations-v1')
    for directory in ('.agents/skills', '.claude/skills'):
        audit.tree(owner / directory, owners, 'projected-skills', 'workspace-operations-v1')
    if manifest is not None:
        files = manifest.get('files')
        if not isinstance(files, dict):
            audit.block('unknown_schema', manifest_source, 'context manifest files 형식을 확인하십시오')
        else:
            if len(files) > MAX_FILES:
                audit.block('file_limit', manifest_source, 'context 파일 상한을 초과했습니다')
                return
            for name in files:
                path = relative(name, manifest_source)
                if path is None:
                    continue
                if any(SECRET_NAME.search(part) for part in path.relative_to(owner).parts):
                    audit.reference(path, owners)
                else:
                    audit.read(path, audit.store(path, owners, 'context-projection', 'workspace-operations-v1'))
