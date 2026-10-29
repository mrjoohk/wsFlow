"""Workspace operations backend with the version-1 module service contract."""
from __future__ import annotations
from contextlib import nullcontext

from wsflow.module_services import ApiError
from wsflow.runtime_activity import admitted, normalize_activity
from . import gitops
from .routes import Routes


class Backend:
    def __init__(self, context):
        self.services = context['services']
        activity = getattr(self.services, 'activity', None)
        self._activity = normalize_activity(activity)
        self._writing = normalize_activity(getattr(self.services, 'writing', None))
        self._activity_bound = activity is not None
        self.started = False

    @admitted
    def start(self):
        self.started = True
        return True

    def runtime_quiescence(self):
        return {'api': 1, 'activity_bound': self._activity_bound, 'owns_runtime': True}

    def stop(self, timeout=None):
        self.started = False
        return True

    @admitted
    def summary(self, project):
        return Routes(None, self.services)._summary(project)

    @admitted
    def dispatch(self, request, method, path, query, body):
        try:
            with (self._writing() if method != 'GET' else nullcontext()):
                return Routes(request, self.services).dispatch(method, path, query, body)
        except gitops.GitError as error:
            details = {**error.to_dict(), **{key: getattr(error, key) for key in ('plan_id', 'operation_id') if hasattr(error, key)}}
            raise ApiError(500, str(error), details) from error


def create_module(context):
    return Backend(context)
