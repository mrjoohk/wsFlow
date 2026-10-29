"""Work-calendar lifecycle and HTTP entrypoint for the business module package."""
from __future__ import annotations

import threading
from contextlib import nullcontext

from wsflow.module_services import ApiError
from wsflow.runtime_activity import admitted, normalize_activity
from . import calendar_store, calendar_sync, google_calendar, jirawrite, newtask, wfhome, workflow
from .routes import Routes


class Service:
    def __init__(self, context):
        self.services = context['services']
        activity = getattr(self.services, 'activity', None)
        self._activity = normalize_activity(activity)
        self._writing = normalize_activity(getattr(self.services, 'writing', None))
        self._activity_bound = activity is not None
        self.google = None
        self.google_error = None
        self.writer = None
        self.scope_cache = {}
        self.scope_mutex = threading.Lock()
        self.started = False
        self.closing = False
        self.owns_runtime = True

    @admitted
    def use_runtime(self, google, google_error=None, writer=None):
        """Attach a caller-owned runtime for the legacy make_handler API.

        Constructing an HTTP handler must never start provider workers. The
        caller remains responsible for this explicitly supplied runtime.
        """
        self.google, self.google_error, self.writer = google, google_error, writer
        self.owns_runtime = False
        self.started = True

    @admitted
    def start(self):
        """Start this package's local worker after the active generation is bound."""
        if self.closing:
            raise ValueError('종료한 업무·일정 모듈은 같은 프로세스에서 다시 기동하지 않습니다')
        if self.started:
            return
        store = self.services.store
        try:
            self.google = google_calendar.Service(
                store.file, transport=self.services.google_transport,
                projects=lambda: store.snapshot()['projects'], activity=self._activity)
        except ValueError as exc:
            self.google_error = str(exc)
        self.writer = calendar_sync.Engine(calendar_store.Store(store.file), self.google, activity=self._activity) if self.google is not None else None
        if self.writer is not None:
            self.google.after_sync = self.writer.after_sync
        if self.google is not None:
            self.google.start_refresh()
        self.started = True

    def runtime_quiescence(self):
        return {'api': 1, 'activity_bound': self._activity_bound, 'owns_runtime': self.owns_runtime}

    def stop(self, timeout=None):
        """Stop workers locally; never disconnect or revoke external credentials."""
        self.closing = True
        if self.owns_runtime and self.google is not None:
            return self.google.close() if timeout is None else self.google.close(timeout=timeout)
        return True

    @admitted
    def dispatch(self, request, method, path, query, body):
        try:
            with (self._writing() if method != 'GET' else nullcontext()):
                return Routes(request, self.services, self).dispatch(method, path, query, body)
        except wfhome.MemoConflict as exc:
            raise ApiError(409, str(exc), {'current_text': exc.current_text,
                                         'current_sha': exc.current_sha}) from exc
        except jirawrite.JiraError as exc:
            raise ApiError(exc.status if exc.status and 400 <= exc.status <= 599 else 502,
                           str(exc), exc.to_dict()) from exc
        except newtask.NewTaskError as exc:
            raise ApiError(422, str(exc)) from exc

    @admitted
    def workflow_status(self, day):
        return workflow.overview(self.services.store.snapshot()['projects'], day)

    @admitted
    def calendar_status(self):
        """Read-only dashboard contribution; no other module's implementation import."""
        result = {'state': 'unknown', 'warnings': [], 'outbox_counts': {}, 'last_sync': None}
        try:
            if self.google is None:
                result['warnings'] = [self.google_error or 'Google 상태를 확인할 수 없습니다']
            else:
                status = self.google.status()
                result.update(last_sync=status.get('last_sync'), state='ok' if status.get('connected') else 'disconnected')
                result['warnings'] = [str(status[key]) for key in ('auth_error', 'background_error') if status.get(key)]
                if status.get('connected') and status.get('selected_calendar_ids') and not status.get('complete'):
                    result['warnings'].append('Google 조회 동기화 확인 필요')
                if self.writer is not None:
                    for operation in self.writer.status()['operations']:
                        state = operation['state']
                        result['outbox_counts'][state] = result['outbox_counts'].get(state, 0) + 1
                    if any(result['outbox_counts'].get(state) for state in ('error', 'uncertain', 'conflict')):
                        result['warnings'].append('Google 연결 일정의 오류/충돌/결과 미확인 확인 필요')
                if result['warnings']:
                    result['state'] = 'attention'
        except Exception:
            result.update(state='unknown', warnings=['캘린더 상태 조회 실패 — 캘린더에서 다시 확인하십시오'])
        return result


def create_module(context):
    return Service(context)
