"""Workspace operations backend with the version-1 module service contract."""
from __future__ import annotations

from wsflow.module_services import ApiError
from . import gitops
from .routes import Routes


class Backend:
    def __init__(self, context):
        self.services = context['services']
        self.started = False

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.started = False
        return True

    def summary(self, project):
        return Routes(None, self.services)._summary(project)

    def dispatch(self, request, method, path, query, body):
        try:
            return Routes(request, self.services).dispatch(method, path, query, body)
        except gitops.GitError as error:
            details = {**error.to_dict(), **{key: getattr(error, key) for key in ('plan_id', 'operation_id') if hasattr(error, key)}}
            raise ApiError(500, str(error), details) from error


def create_module(context):
    return Backend(context)
