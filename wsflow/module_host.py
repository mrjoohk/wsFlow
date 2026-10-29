"""Trusted local business modules, with durable plans applied on the next startup.

Module removal never deletes business data or revokes provider credentials. An
alternative implementation uses its own data namespace; schema migration and
adopting another implementation's data require a future explicit contract.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import threading
import time
from uuid import uuid4

from . import fileio, operations, module_continuity
from .path_policy import plain_path
from .runtime_activity import ActivityGate, ActivityUnavailable, admitted, validate_timeout

HOST_API = 1
MAX_FILES = 2000
MAX_BYTES = 16 * 1024 * 1024
ID = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


class ModuleError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _builtin(ident, name, capabilities):
    return dict(id=ident, name=name, version='1.0.0', builtin=True,
                installed=True, enabled=True, settings={}, capabilities=capabilities,
                requires=[], data_schema_version=1, host_api=HOST_API, ui={},
                config_schema={'type': 'object', 'properties': {}, 'additionalProperties': False})


BUILTINS = {
    'work-calendar': _builtin('work-calendar', '업무·일정 통합', ['work-items', 'calendar']),
    'workspace-operations': _builtin('workspace-operations', '워크스페이스 운영', ['workspace', 'development-context']),
}
PACKAGE_NAMES = {'work-calendar': 'work_calendar', 'workspace-operations': 'workspace_operations'}


def _bundled_record(ident):
    source = Path(__file__).parent / 'modules' / PACKAGE_NAMES[ident]
    try:
        inspected = inspect_package(source, builtin_id=ident)
    except ModuleError as exc:
        # Isolate a broken shipped package while keeping core management and the
        # other package available for an explicitly trusted repair/update.
        record = copy.deepcopy(BUILTINS[ident])
        record.update(schema_version=1, entrypoint='backend.py:create_module',
                      source_kind='bundled', package_digest=None, package_error=str(exc))
        return record
    record = copy.deepcopy(inspected['manifest'])
    record.update(builtin=True, installed=True, enabled=True, settings={},
                  source_kind='bundled', package_digest=inspected['digest'])
    return record


def _default():
    return {'schema_version': 1, 'revision': 0,
            'modules': {ident: _bundled_record(ident) for ident in BUILTINS}, 'applied_plans': []}


def _settings(schema, values):
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        raise ModuleError('설정 schema는 object여야 합니다')
    properties = schema.get('properties', {})
    if not isinstance(properties, dict) or not isinstance(values, dict):
        raise ModuleError('설정은 JSON object여야 합니다')
    required = schema.get('required', [])
    if not isinstance(required, list) or any(not isinstance(k, str) for k in required):
        raise ModuleError('잘못된 설정 required 목록입니다')
    if set(required) - set(values):
        raise ModuleError('필수 설정이 없습니다: ' + ', '.join(sorted(set(required) - set(values))))
    if set(values) - set(properties):
        raise ModuleError('선언되지 않은 설정 필드입니다')
    for key, value in values.items():
        field = properties[key]
        if not isinstance(field, dict) or field.get('secret') or field.get('sensitive') or field.get('writeOnly'):
            raise ModuleError('비밀값은 모듈 일반 설정에 저장할 수 없습니다')
        if re.search(r'password|token|secret|api.?key', key, re.I) and not key.endswith('_ref'):
            raise ModuleError('자격 원문 대신 별도 credential 참조가 필요합니다')
        kind = field.get('type')
        valid = ((kind == 'string' and isinstance(value, str)) or
                 (kind == 'boolean' and type(value) is bool) or
                 (kind == 'integer' and type(value) is int) or
                 (kind == 'number' and type(value) in (int, float)))
        if not valid or ('enum' in field and value not in field['enum']):
            raise ModuleError('설정 형식/허용값이 잘못되었습니다: ' + key)
        if isinstance(value, str) and len(value) > 4096:
            raise ModuleError('설정 문자열이 너무 깁니다')
    return copy.deepcopy(values)


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ModuleError('잘못된 모듈 상대 경로입니다')
    p = Path(value)
    if p.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise ModuleError('모듈 경로가 패키지 밖으로 나갑니다')
    return p


def _manifest(value, builtin_id=None):
    allowed = {'schema_version', 'id', 'name', 'version', 'host_api', 'data_schema_version',
               'capabilities', 'requires', 'config_schema', 'entrypoint', 'ui', 'data_compatibility'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ModuleError('지원하지 않는 모듈 manifest 필드입니다')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ModuleError('지원하지 않는 manifest schema입니다')
    if not isinstance(value.get('id'), str) or not ID.fullmatch(value['id']):
        raise ModuleError('잘못된 모듈 ID입니다')
    if value['id'] in BUILTINS and value['id'] != builtin_id:
        raise ModuleError('기본 모듈 ID는 외부 패키지가 사용할 수 없습니다')
    if not isinstance(value.get('name'), str) or not 1 <= len(value['name']) <= 120:
        raise ModuleError('모듈 이름이 필요합니다')
    if not isinstance(value.get('version'), str) or not VERSION.fullmatch(value['version']):
        raise ModuleError('모듈 버전은 숫자 3성분이어야 합니다')
    if type(value.get('host_api')) is not int or value['host_api'] != HOST_API:
        raise ModuleError('호스트 API 버전이 호환되지 않습니다')
    if type(value.get('data_schema_version')) is not int or value['data_schema_version'] < 1:
        raise ModuleError('잘못된 데이터 schema 버전입니다')
    for key in ('requires', 'capabilities'):
        items = value.get(key, [])
        if not isinstance(items, list) or any(not isinstance(x, str) or not ID.fullmatch(x) for x in items) or len(set(items)) != len(items):
            raise ModuleError('잘못된 ' + key + ' 목록입니다')
    entry = value.get('entrypoint', '')
    if not isinstance(entry, str) or entry.count(':') != 1:
        raise ModuleError('entrypoint는 backend.py:factory 형식이어야 합니다')
    filename, factory = entry.split(':')
    if _relative(filename).suffix != '.py' or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', factory):
        raise ModuleError('잘못된 Python entrypoint입니다')
    ui = value.get('ui', {})
    if not isinstance(ui, dict) or set(ui) - {'entry', 'label', 'mode'}:
        raise ModuleError('잘못된 UI 기여 항목입니다')
    if ui and (_relative(ui.get('entry')).suffix != '.js' or not isinstance(ui.get('label'), str)):
        raise ModuleError('UI entry와 label이 필요합니다')
    if ui.get('mode', 'module') not in ('module', 'classic') or (ui.get('mode') == 'classic' and builtin_id != value['id']):
        raise ModuleError('classic 화면은 기본 업무 패키지에만 허용합니다')
    schema = value.get('config_schema', {'type': 'object', 'properties': {}, 'additionalProperties': False})
    if not isinstance(schema, dict) or set(schema) - {'type', 'properties', 'required', 'additionalProperties'}:
        raise ModuleError('지원하지 않는 설정 schema입니다')
    if schema.get('additionalProperties', False) is not False:
        raise ModuleError('추가 설정 필드는 허용하지 않습니다')
    if not isinstance(schema.get('properties', {}), dict):
        raise ModuleError('잘못된 설정 properties입니다')
    for field in schema.get('properties', {}).values():
        if not isinstance(field, dict) or set(field) - {'type', 'enum', 'secret', 'sensitive', 'writeOnly', 'description'}:
            raise ModuleError('지원하지 않는 설정 필드 schema입니다')
        if field.get('type') not in ('string', 'integer', 'number', 'boolean'):
            raise ModuleError('지원하지 않는 설정 필드 형식입니다')
        if field.get('secret') or field.get('sensitive') or field.get('writeOnly'):
            raise ModuleError('비밀 설정은 별도 credential 계약이 필요합니다')
    result = copy.deepcopy(value)
    if 'data_compatibility' in value:
        if value['data_compatibility'] is None:
            raise ModuleError('자료 호환성 선언은 object여야 합니다')
        try:
            result['data_compatibility'] = module_continuity.validate_contract(value['data_compatibility'])
        except ValueError as exc:
            raise ModuleError('잘못된 자료 호환성 선언: ' + str(exc)) from exc
    result.update(requires=value.get('requires', []), capabilities=value.get('capabilities', []),
                  config_schema=schema, ui=ui)
    return result


def inspect_package(source, builtin_id=None):
    """Read bounded regular files and hash them without executing package code."""
    source = Path(source).absolute()
    try:
        plain_path(source, Path(source.anchor))
        if not source.is_dir():
            raise ModuleError('모듈 패키지 폴더가 없습니다')
        files, total = {}, 0
        pending = [source]
        while pending:
            current = pending.pop()
            for child in sorted(current.iterdir()):
                plain_path(child, source)
                rel = child.relative_to(source).as_posix()
                _relative(rel)
                if child.name == '__pycache__':
                    continue
                if child.is_dir():
                    pending.append(child)
                elif child.is_file():
                    if len(files) >= MAX_FILES or child.stat().st_size > MAX_BYTES - total:
                        raise ModuleError('모듈 패키지 크기/파일 수 상한을 넘었습니다')
                    raw = child.read_bytes()
                    total += len(raw)
                    if total > MAX_BYTES:
                        raise ModuleError('모듈 패키지 byte 상한을 넘었습니다')
                    files[rel] = hashlib.sha256(raw).hexdigest()
                else:
                    raise ModuleError('특수 파일은 모듈 패키지에 넣을 수 없습니다')
        manifest_path = source / 'module.json'
        if 'module.json' not in files:
            raise ModuleError('module.json이 없습니다')
        manifest = _manifest(json.loads(manifest_path.read_text(encoding='utf-8-sig')), builtin_id=builtin_id)
        required = [manifest['entrypoint'].split(':')[0]]
        if manifest['ui']:
            required.append(manifest['ui']['entry'])
        if any(p not in files for p in required):
            raise ModuleError('선언한 entry 파일이 패키지에 없습니다')
        return {'manifest': manifest, 'files': files, 'digest': operations.digest(files), 'bytes': total}
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, ModuleError):
            raise
        raise ModuleError('모듈 패키지 검사 실패: ' + str(exc)) from exc


def _validate_modules(modules):
    if not isinstance(modules, dict):
        raise ModuleError('모듈 구성이 object가 아닙니다')
    for ident, record in modules.items():
        if not isinstance(ident, str) or not ID.fullmatch(ident) or not isinstance(record, dict) or record.get('id') != ident:
            raise ModuleError('잘못된 저장 모듈 ID입니다')
        if any(type(record.get(k)) is not bool for k in ('installed', 'enabled', 'builtin')):
            raise ModuleError('잘못된 저장 모듈 상태입니다')
        if record['enabled'] and not record['installed']:
            raise ModuleError('설치되지 않은 모듈은 활성화할 수 없습니다')
        if record['builtin']:
            if ident not in BUILTINS or not set(BUILTINS[ident]['capabilities']).issubset(record.get('capabilities', [])):
                raise ModuleError('기본 모듈 선언이 변경되었습니다')
        declaration = {k: record[k] for k in ('schema_version', 'id', 'name', 'version', 'host_api', 'data_schema_version', 'capabilities', 'requires', 'config_schema', 'entrypoint', 'ui')}
        if 'data_compatibility' in record:
            declaration['data_compatibility'] = record['data_compatibility']
        _manifest(declaration, builtin_id=ident if record['builtin'] else None)
        _settings(record['config_schema'], record.get('settings', {}))
    visiting, visited = set(), set()
    def visit(ident):
        if ident in visiting:
            raise ModuleError('모듈 의존성이 순환합니다')
        if ident in visited:
            return
        visiting.add(ident)
        for dependency in modules[ident].get('requires', []):
            r = modules.get(dependency)
            if not r or not r['installed'] or not r['enabled']:
                raise ModuleError('필수 모듈이 활성 상태가 아닙니다: ' + dependency)
            visit(dependency)
        visiting.remove(ident)
        visited.add(ident)
    for ident, record in modules.items():
        if record['installed'] and record['enabled']:
            visit(ident)


class ModuleHost:
    """One immutable active generation and a separately revisioned desired state."""
    def __init__(self, registry):
        self._activity = ActivityGate()
        self._runtime_control = threading.Lock()
        self._runtime_view = threading.Lock()
        self._runtime_phase = 'running'
        self._stop_started = False
        self._stop_verified = False
        self.registry = str(Path(registry).resolve())
        base = Path(os.environ.get('WSFLOW_DATA_DIR') or (Path(os.environ.get('APPDATA') or Path.home() / '.config') / 'wsFlow')).expanduser().absolute()
        self.root = base / 'modules' / operations.digest(self.registry)
        self.state_path = self.root / 'state.json'
        self.errors, self.extensions, self.backends = {}, {}, {}
        self.stop_results = {}
        self.started_backends = set()
        self.services = None
        self.configuration_error = None
        self.storage_warning = None
        try:
            try:
                self._check_storage()
            except ModuleError as exc:
                if self.state_path.exists():
                    raise
                # Legacy apps may use userdata below a broad registered parent.
                # Default builtins need no module storage and must keep working;
                # new configuration writes still require a safe location.
                self.storage_warning = str(exc)
            self._active = _default() if self.storage_warning else self._read()
        except (ModuleError, OSError, ValueError, KeyError, TypeError) as exc:
            self.configuration_error = str(exc)
            self._active = {'schema_version': 1, 'revision': 0, 'modules': {}, 'applied_plans': []}
        loaded = set()
        def load(ident):
            if ident in loaded:
                return
            record = self._active['modules'][ident]
            if record.get('package_error'):
                self.errors[ident] = record['package_error']
                loaded.add(ident)
                return
            if not record['installed'] or not record['enabled']:
                return
            for dependency in record.get('requires', []):
                load(dependency)
            loaded.add(ident)
            try:
                if any(dependency in self.errors for dependency in record.get('requires', [])):
                    raise ModuleError('필수 모듈의 기동이 실패했습니다')
                if not record['builtin']:
                    self._load(record)
            except Exception as exc:
                self.errors[ident] = type(exc).__name__ + ': ' + str(exc)
        for ident in self._active['modules']:
            load(ident)

    def _path(self, rel):
        path = self.root / _relative(rel)
        plain_path(path, self.root)
        return path

    def _check_storage(self):
        """Use registered domain boundaries; registry.parent may also contain userdata."""
        plain_path(self.root, Path(self.root.anchor))
        registry = Path(self.registry)
        data = json.loads(registry.read_text(encoding='utf-8')) if registry.is_file() else {}
        owners = [Path(r['path']).resolve() for r in data.get('roots', [])]
        for project in data.get('projects', []):
            owners.append(Path(project['path']).resolve())
            if project.get('wf_home'):
                owners.append(Path(project['wf_home']).resolve())
        if any(self.root.resolve().is_relative_to(owner) for owner in owners):
            raise ModuleError('모듈 저장소는 등록 ROOT·프로젝트·WF_HOME 밖이어야 합니다', 409)

    def _read(self):
        plain_path(self.state_path, self.root)
        if not self.state_path.exists():
            return _default()
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or type(state.get('schema_version')) is not int or state['schema_version'] != 1 or type(state.get('revision')) is not int or state['revision'] < 0:
            raise ModuleError('저장 모듈 구성이 손상되었거나 호환되지 않습니다', 409)
        if not isinstance(state.get('applied_plans'), list):
            raise ModuleError('저장 모듈 계획 이력이 잘못되었습니다', 409)
        # P054 builtin records had no package metadata. Adopt the shipped v1
        # package in memory while preserving registration, settings and revision.
        for ident, record in state.get('modules', {}).items():
            if ident in BUILTINS and record.get('builtin') and 'entrypoint' not in record:
                if record.get('version') != '1.0.0' or record.get('data_schema_version') != 1:
                    raise ModuleError('구형 기본 모듈 버전을 자동 변환할 수 없습니다', 409)
                packaged = _bundled_record(ident)
                packaged.update(installed=record['installed'], enabled=record['enabled'], settings=record.get('settings', {}))
                state['modules'][ident] = packaged
        _validate_modules(state.get('modules'))
        return state

    def active(self, ident):
        r = self._active['modules'].get(ident, {})
        return bool(not self.configuration_error and ident not in self.errors and r.get('installed') and r.get('enabled'))

    def builtin_active(self, ident):
        return self.active(ident) and self._active['modules'][ident]['builtin']

    def snapshot(self):
        try:
            desired = self._read() if not self.configuration_error else self._active
            error = self.configuration_error
        except (ModuleError, OSError, ValueError, KeyError, TypeError) as exc:
            desired, error = self._active, str(exc)
        rows = []
        for ident in sorted(set(desired['modules']) | set(self._active['modules'])):
            record = copy.deepcopy(desired['modules'].get(ident) or self._active['modules'][ident])
            active = self._active['modules'].get(ident)
            record.update(active=self.active(ident), active_version=active['version'] if active and self.active(ident) else None,
                          active_ui=copy.deepcopy(active.get('ui', {})) if active else {},
                          pending=record != active, pending_action='재시작 대기' if record != active else None,
                          error=self.errors.get(ident))
            rows.append(record)
        return {'schema_version': 1, 'revision': desired['revision'], 'active_revision': self._active['revision'],
                'restart_required': desired['modules'] != self._active['modules'], 'configuration_error': error,
                'storage_warning': self.storage_warning,
                'modules': rows, 'catalog': [_bundled_record(ident) for ident in BUILTINS],
                'runtime_activity': self.runtime_snapshot()}

    def activity(self):
        """Admit one host-mediated synchronous operation, including nested work."""
        return self._activity()

    def runtime_snapshot(self):
        with self._runtime_view:
            # Read admission before releasing the lifecycle observation lock;
            # a later verified stop cannot be combined with stale open admission.
            activity = self._activity.snapshot()
            return {'schema_version': 1, 'scope': 'host_mediated_runtime', **activity,
                    'phase': self._runtime_phase, 'stop_verified': self._stop_verified,
                    'stop_results': dict(self.stop_results), 'snapshot_ready': False}

    def _runtime_state(self, phase, *, started=None, verified=None):
        with self._runtime_view:
            self._runtime_phase = phase
            if started is not None:
                self._stop_started = started
            if verified is not None:
                self._stop_verified = verified

    def _stop_observation(self, ident, complete):
        with self._runtime_view:
            self.stop_results[ident] = complete
        if complete:
            self.started_backends.discard(ident)

    def _quiescence_participants(self):
        """Trust only this generation's current stock, owned activity protocol."""
        if self.configuration_error or self.services is None or getattr(self.services, 'module_host', None) is not self:
            raise ModuleError('호스트 작업 경계를 확인할 수 없습니다', 409)
        participants = []
        for ident in self._dependency_order():
            record = self._active['modules'][ident]
            if (ident not in BUILTINS or not record.get('builtin') or
                    record.get('package_digest') != _bundled_record(ident)['package_digest']):
                raise ModuleError('이 패키지의 정지 계약은 아직 지원하지 않습니다: ' + ident, 409)
            backend = self.backends.get(ident)
            if backend is None or (ident not in self.started_backends and not self._stop_started):
                raise ModuleError('모듈 기동 상태를 확인할 수 없습니다: ' + ident, 409)
            try:
                contract = backend.runtime_quiescence()
            except Exception as exc:
                raise ModuleError('모듈 작업 경계를 확인할 수 없습니다: ' + ident, 409) from exc
            if (not isinstance(contract, dict) or type(contract.get('api')) is not int or contract['api'] != 1 or
                    contract.get('activity_bound') is not True or contract.get('owns_runtime') is not True):
                raise ModuleError('호스트가 소유한 작업 정지를 확인할 수 없습니다: ' + ident, 409)
            participants.append((ident, backend))
        return participants

    def quiesce(self, drain_timeout=5, stop_timeout=5):
        """Drain admitted work and verify owned stock workers; no snapshot grant.

        Timeouts are seconds in [0, 30]. A drain timeout starts no stop. Once
        stop begins this host remains closed, including failures; retry can
        re-observe completion but resume requires a new host generation.
        """
        drain_timeout = validate_timeout(drain_timeout, 'drain_timeout')
        stop_timeout = validate_timeout(stop_timeout, 'stop_timeout')
        if self._activity.in_activity():
            raise ActivityUnavailable('reentrant_pause')
        if not self._runtime_control.acquire(blocking=False):
            raise ModuleError('호스트 정지 제어가 진행 중입니다', 409)
        try:
            if self._runtime_phase == 'closed':
                raise ModuleError('종료한 호스트는 새 프로세스에서 다시 기동하십시오', 409)
            participants = self._quiescence_participants()
            self._runtime_state('draining', verified=False)
            if not self._activity.pause(drain_timeout):
                return self.runtime_snapshot()
            # An admitted operation may change runtime ownership while draining.
            # Validate the final participants before starting any stop operation.
            participants = self._quiescence_participants()
            self._runtime_state('stopping', started=True)
            deadline = time.monotonic() + stop_timeout
            complete = True
            for ident, backend in reversed(participants):
                try:
                    stopped = backend.stop(timeout=max(0, deadline - time.monotonic())) is True
                    if stopped and self.errors.get(ident, '').startswith('stop failed:'):
                        self.errors.pop(ident, None)
                except Exception as exc:
                    self.errors[ident] = 'stop failed: ' + str(exc)
                    stopped = False
                self._stop_observation(ident, stopped)
                complete = complete and stopped
            self._runtime_state('stopped' if complete else 'stop_failed', verified=complete)
            return self.runtime_snapshot()
        finally:
            self._runtime_control.release()

    def resume_activity(self):
        """Cancel a pre-stop pause; a stopped/failed-stop generation cannot resume."""
        if not self._runtime_control.acquire(blocking=False):
            raise ModuleError('호스트 정지 제어가 진행 중입니다', 409)
        try:
            if self._stop_started or self._runtime_phase == 'closed':
                raise ModuleError('worker 정지 후에는 새 프로세스에서 다시 기동하십시오', 409)
            self._activity.resume()
            self._runtime_state('running', verified=False)
            return self.runtime_snapshot()
        finally:
            self._runtime_control.release()

    def _loaded_package(self, record):
        if record.get('source_kind') == 'bundled' and record['builtin']:
            path = Path(__file__).parent / 'modules' / PACKAGE_NAMES[record['id']]
        else:
            path = self._path(record['package_path'])
        inspection = inspect_package(path, builtin_id=record['id'] if record['builtin'] else None)
        if inspection['digest'] != record['package_digest']:
            raise ModuleError('설치된 패키지가 변경되었습니다', 409)
        return path

    def _dependency_order(self):
        """Enabled dependencies precede their consumers, independent of JSON order."""
        ordered, visited = [], set()
        def visit(ident):
            if ident in visited:
                return
            visited.add(ident)
            record = self._active['modules'][ident]
            if not record['installed'] or not record['enabled']:
                return
            for dependency in record.get('requires', []):
                visit(dependency)
            ordered.append(ident)
        for ident in self._active['modules']:
            visit(ident)
        return ordered

    @admitted
    def bind_services(self, services):
        """Bind core API v1 without starting any package-owned worker."""
        if self.services is not None:
            return
        self.services = services
        for ident in self._dependency_order():
            record = self._active['modules'][ident]
            if not self.builtin_active(ident):
                continue
            try:
                if any(dependency in self.errors for dependency in record.get('requires', [])):
                    raise ModuleError('필수 모듈의 기동이 실패했습니다')
                source = self._loaded_package(record)
                if record['package_digest'] == _bundled_record(ident)['package_digest']:
                    filename = record['entrypoint'].split(':')[0]
                    module_name = str(Path(filename).with_suffix('')).replace('\\', '.').replace('/', '.')
                    module = importlib.import_module('wsflow.modules.' + PACKAGE_NAMES[ident] + '.' + module_name)
                else:
                    name = 'wsflow_business_' + record['package_digest']
                    filename, _ = record['entrypoint'].split(':')
                    spec = importlib.util.spec_from_file_location(name, source / filename,
                                                                submodule_search_locations=[str(source)])
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[name] = module
                    spec.loader.exec_module(module)
                context = {'services': services, 'module_id': ident, 'settings': copy.deepcopy(record['settings']),
                           'data_dir': str(self._path('data/' + ident)), 'host_api_version': HOST_API}
                backend = getattr(module, record['entrypoint'].split(':')[1])(context)
                if not callable(getattr(backend, 'dispatch', None)):
                    raise ModuleError('업무 패키지 dispatch 계약이 없습니다')
                self.backends[ident] = backend
            except Exception as exc:
                self.errors[ident] = type(exc).__name__ + ': ' + str(exc)

    @admitted
    def start_backends(self):
        for ident in self._dependency_order():
            if not self.active(ident):
                continue
            try:
                record = self._active['modules'][ident]
                if any(dependency in self.errors for dependency in record.get('requires', [])):
                    raise ModuleError('필수 모듈의 기동이 실패했습니다')
                if ident not in self.backends or ident in self.started_backends:
                    continue
                backend = self.backends[ident]
                start = getattr(backend, 'start', None)
                if start is not None:
                    start()
                self.started_backends.add(ident)
            except Exception as exc:
                self.errors[ident] = 'start failed: ' + str(exc)
                try:
                    self.backends[ident].stop()
                except Exception:
                    pass

    def _load(self, record):
        source = self._loaded_package(record)
        filename, factory_name = record['entrypoint'].split(':')
        name = 'wsflow_extension_' + record['package_digest']
        spec = importlib.util.spec_from_file_location(name, source / filename,
                                                    submodule_search_locations=[str(source)])
        if spec is None or spec.loader is None:
            raise ModuleError('확장 entrypoint를 불러올 수 없습니다')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        context = {'module_id': record['id'], 'settings': copy.deepcopy(record['settings']),
                   'data_dir': str(self._path('data/' + record['id'])), 'host_api_version': HOST_API}
        service = getattr(module, factory_name)(context)
        if not callable(getattr(service, 'handle', None)):
            raise ModuleError('모듈 handle 계약이 없습니다')
        self.extensions[record['id']] = service

    @admitted
    def plan(self, body):
        if self.configuration_error:
            raise ModuleError('손상 구성을 자동 초기화하지 않습니다: ' + self.configuration_error, 409)
        allowed = {'action', 'module_id', 'expected_revision', 'source_path', 'trusted', 'settings', 'target_revision', 'data_adoption'}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ModuleError('지원하지 않는 모듈 계획 필드입니다')
        if type(body.get('expected_revision')) is not int:
            raise ModuleError('expected_revision 정수가 필요합니다')
        self._check_storage()
        with fileio.locked(self.root):
            current = self._read()
            if current['revision'] != body['expected_revision']:
                raise ModuleError('모듈 구성이 바뀌었습니다. 다시 미리보기하세요', 409)
            modules = copy.deepcopy(current['modules'])
            action, ident = body.get('action'), body.get('module_id')
            package, source = None, None
            if action == 'restore':
                revision = body.get('target_revision')
                if type(revision) is not int or revision < 0:
                    raise ModuleError('복구할 구성 revision이 필요합니다')
                if revision == self._active['revision']:
                    modules = copy.deepcopy(self._active['modules'])
                else:
                    history = self._path('history/' + str(revision) + '.json')
                    if not history.is_file():
                        raise ModuleError('복구할 구성 이력이 없습니다', 404)
                    modules = json.loads(history.read_text(encoding='utf-8'))['modules']
            elif action in ('install', 'update', 'replace'):
                if action == 'install' and ident in BUILTINS and not body.get('source_path'):
                    if modules.get(ident, {}).get('installed'):
                        raise ModuleError('이미 설치된 모듈입니다', 409)
                    modules[ident] = _bundled_record(ident)
                else:
                    if body.get('trusted') is not True:
                        raise ModuleError('선택한 로컬 코드 패키지의 신뢰 확인이 필요합니다')
                    if not isinstance(body.get('source_path'), str) or not Path(body['source_path']).is_absolute():
                        raise ModuleError('패키지 폴더 절대 경로가 필요합니다')
                    source = str(Path(body['source_path']).absolute())
                    builtin_id = ident if ident in BUILTINS and action in ('update', 'install') else None
                    package = inspect_package(source, builtin_id=builtin_id)
                    record = copy.deepcopy(package['manifest'])
                    newid = record['id']
                    old = modules.get(ident)
                    if action == 'update':
                        if not old or not old['installed'] or newid != ident:
                            raise ModuleError('갱신은 설치된 모듈의 동일 ID여야 합니다')
                        if tuple(map(int, record['version'].split('.'))) <= tuple(map(int, old['version'].split('.'))):
                            raise ModuleError('갱신 버전은 현재 버전보다 높아야 합니다. 이전 구성은 복구를 사용하십시오')
                        if record['data_schema_version'] != old['data_schema_version']:
                            raise ModuleError('자동 데이터 schema 변환은 지원하지 않습니다', 409)
                        settings = body.get('settings', old['settings'])
                    else:
                        if modules.get(newid, {}).get('installed'):
                            raise ModuleError('이미 설치된 모듈 ID입니다', 409)
                        settings = body.get('settings', {})
                    if action == 'replace':
                        if not old or not old['installed'] or newid == ident:
                            raise ModuleError('다른 구현으로 교체할 설치 대상이 필요합니다')
                        if body.get('data_adoption', False) is not False:
                            raise ModuleError('다른 구현의 업무 데이터 자동 이관은 지원하지 않습니다', 409)
                        if not set(old['capabilities']).issubset(record['capabilities']):
                            raise ModuleError('교체 모듈의 업무 capability가 부족합니다', 409)
                        modules[ident].update(installed=False, enabled=False)
                    elif action == 'install' and ident not in (None, '', newid):
                        raise ModuleError('선택 ID와 패키지 ID가 다릅니다')
                    record.update(builtin=newid in BUILTINS, installed=True, enabled=True, source_kind='managed',
                                  settings=_settings(record['config_schema'], settings),
                                  package_digest=package['digest'],
                                  package_path='packages/' + newid + '/' + record['version'] + '-' + package['digest'])
                    modules[newid] = record
                    ident = ident if action == 'replace' else newid
            elif action in ('enable', 'disable', 'uninstall', 'configure'):
                if not isinstance(ident, str) or ident not in modules or not modules[ident]['installed']:
                    raise ModuleError('설치된 모듈이 아닙니다', 404)
                record = modules[ident]
                if action == 'configure':
                    record['settings'] = _settings(record['config_schema'], body.get('settings'))
                elif action == 'uninstall':
                    record.update(installed=False, enabled=False)
                else:
                    record['enabled'] = action == 'enable'
            else:
                raise ModuleError('지원하지 않는 모듈 변경입니다')
            _validate_modules(modules)
            continuity = module_continuity.audit_transition(
                Path(self.registry), self.root, current['modules'], modules, action, ident)
            plan = {'plan_id': 'MP-' + uuid4().hex, 'action': action, 'module_id': ident,
                    'expected_revision': current['revision'], 'modules': modules,
                    'source_path': source, 'package': package,
                    'restart_required': modules != self._active['modules'],
                    'data_adoption': False, 'data_preserved': True,
                    'continuity': continuity, 'can_apply': continuity['ready'],
                    'changes': [{'id': key, 'before': current['modules'].get(key), 'after': modules.get(key)}
                                for key in sorted(set(modules) | set(current['modules'])) if modules.get(key) != current['modules'].get(key)]}
            fileio.atomic_write(self._path('plans/' + plan['plan_id'] + '.json'), _bytes(plan))
            return copy.deepcopy(plan)

    def _check_continuity(self, current, plan):
        """Re-observe before reservation; this is not a startup transaction."""
        fresh = module_continuity.audit_transition(
            Path(self.registry), self.root, current['modules'], plan['modules'],
            plan['action'], plan['module_id'])
        previous = plan.get('continuity')
        if fresh['affected_modules']:
            if not isinstance(previous, dict) or not previous.get('ready') or plan.get('can_apply') is not True:
                raise ModuleError('자료 사전검사가 없거나 차단된 계획입니다. 새로 미리보기하십시오', 409)
            if not fresh['ready']:
                reasons = ' · '.join(item['message'] for item in fresh['blockers'][:3])
                raise ModuleError('미처리 작업 또는 확인 불가 자료 때문에 예약할 수 없습니다: ' + reasons, 409)
            if fresh['fingerprint'] != previous.get('fingerprint'):
                raise ModuleError('미리보기 이후 업무 자료나 연결이 바뀌었습니다. 새로 미리보기하십시오', 409)
        return fresh

    def _stage_package(self, source, inspected, record):
        destination = self._path(record['package_path'])
        if not destination.exists():
            staging = self._path('staging/' + uuid4().hex)
            staging.mkdir(parents=True)
            for rel in inspected['files']:
                data = (Path(source) / rel).read_bytes()
                if hashlib.sha256(data).hexdigest() != inspected['files'][rel]:
                    raise ModuleError('복사 중 패키지가 바뀌었습니다', 409)
                fileio.atomic_write(staging / _relative(rel), data)
            if inspect_package(staging, builtin_id=record['id'] if record['builtin'] else None) != inspected:
                raise ModuleError('staging 패키지가 일치하지 않습니다', 409)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(destination)
        self._loaded_package(record)

    def _pin_bundled(self, record):
        source = self._loaded_package(record)
        inspected = inspect_package(source, builtin_id=record['id'])
        pinned = copy.deepcopy(record)
        pinned.update(source_kind='managed', package_path='packages/' + record['id'] + '/' + record['version'] + '-' + record['package_digest'])
        self._stage_package(source, inspected, pinned)
        return pinned

    @admitted
    def apply(self, plan_id):
        if self.configuration_error:
            raise ModuleError('손상 구성은 자동 변경하지 않습니다', 409)
        if not isinstance(plan_id, str) or not re.fullmatch(r'MP-[a-f0-9]{32}', plan_id):
            raise ModuleError('잘못된 모듈 plan ID입니다')
        self._check_storage()
        with fileio.locked(self.root):
            current = self._read()
            if plan_id in current['applied_plans']:
                return self.snapshot()
            path = self._path('plans/' + plan_id + '.json')
            if not path.is_file():
                raise ModuleError('모듈 계획이 없습니다', 404)
            plan = json.loads(path.read_text(encoding='utf-8'))
            if current['revision'] != plan['expected_revision']:
                raise ModuleError('계획 이후 모듈 구성이 바뀌었습니다', 409)
            _validate_modules(plan['modules'])
            self._check_continuity(current, plan)
            if plan['package']:
                manifest_id = plan['package']['manifest']['id']
                actual = inspect_package(plan['source_path'], builtin_id=manifest_id if manifest_id in BUILTINS else None)
                if actual != plan['package']:
                    raise ModuleError('미리보기 이후 패키지가 바뀌었습니다', 409)
                record = plan['modules'][actual['manifest']['id']]
                self._stage_package(plan['source_path'], actual, record)
            for ident, record in list(plan['modules'].items()):
                if record['installed'] and record['enabled'] and record.get('source_kind') == 'bundled':
                    plan['modules'][ident] = self._pin_bundled(record)
                if record['installed'] and record['enabled']:
                    self._loaded_package(plan['modules'][ident])
            history = self._path('history/' + str(current['revision']) + '.json')
            before = None
            if not history.exists():
                before = copy.deepcopy(current)
                for ident, record in list(before['modules'].items()):
                    if record['installed'] and record.get('source_kind') == 'bundled':
                        try:
                            before['modules'][ident] = self._pin_bundled(record)
                        except ModuleError:
                            # A damaged installation must still be removable.
                            # Restoring that damaged version will fail integrity checks.
                            pass
            self._check_continuity(current, plan)
            if before is not None:
                fileio.atomic_write(history, _bytes(before))
            after = {'schema_version': 1, 'revision': current['revision'] + 1,
                     'modules': plan['modules'], 'applied_plans': [*current['applied_plans'], plan_id]}
            fileio.atomic_write(self.state_path, _bytes(after))
            return self.snapshot()

    @admitted
    def extension_request(self, method, ident, rest, query, body):
        if ident in self.errors:
            raise ModuleError('모듈 기동에 실패했습니다: ' + ident, 503)
        if not self.active(ident):
            raise ModuleError('모듈이 활성 상태가 아닙니다', 409)
        if ident in self.backends and method == 'GET' and rest == ['package']:
            record = self._active['modules'][ident]
            return {k: copy.deepcopy(record[k]) for k in ('id', 'version', 'package_digest', 'capabilities')}
        service = self.extensions.get(ident) or self.backends.get(ident)
        if service is None:
            raise ModuleError('외부 확장 API가 없습니다', 404)
        if not callable(getattr(service, 'handle', None)):
            raise ModuleError('이 모듈의 확장 API가 없습니다', 404)
        return service.handle(method, rest, query, body)

    def asset(self, ident, rel):
        if ident in self.errors:
            raise ModuleError('모듈 기동에 실패했습니다: ' + ident, 503)
        if not self.active(ident):
            raise ModuleError('활성 외부 모듈 자산이 아닙니다', 409)
        record = self._active['modules'][ident]
        source = self._loaded_package(record)
        path = source / _relative(rel)
        plain_path(path, source)
        if path.suffix not in ('.js', '.css', '.svg', '.png', '.jpg', '.webp') or not path.is_file():
            raise ModuleError('제공하지 않는 모듈 자산입니다', 404)
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or 'application/octet-stream'

    def close(self, drain_timeout=5):
        """Drain before legacy cleanup; legacy stop values are not strict proof."""
        drain_timeout = validate_timeout(drain_timeout, 'drain_timeout')
        if not self._runtime_control.acquire(blocking=False):
            return False
        try:
            if self._activity.in_activity():
                return False
            self._runtime_state('draining')
            if not self._activity.pause(drain_timeout):
                return False
            self._runtime_state('stopping', started=True, verified=False)
            complete = True
            services = {**self.extensions, **self.backends}
            for ident in reversed(self._dependency_order()):
                if ident not in services:
                    continue
                try:
                    stop = getattr(services[ident], 'stop', None)
                    stopped = not callable(stop) or stop() is not False
                except Exception as exc:
                    self.errors[ident] = 'stop failed: ' + str(exc)
                    stopped = False
                self._stop_observation(ident, stopped)
                complete = complete and stopped
            self._runtime_state('closed' if complete else 'stop_failed', verified=False)
            return complete
        finally:
            self._runtime_control.release()
