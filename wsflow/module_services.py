"""Host API v1 available to trusted business packages.

This facade carries core registry and module discovery. Domain implementations
are owned by packages, including their workers, exceptions and write adapters.
"""
from pathlib import Path
from .runtime_activity import admitted, normalize_activity


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.extra = status, extra or {}


class ModuleServices:
    api_version = 1

    def __init__(self, store, module_host, google_transport=None):
        self.store = store
        self.module_host = module_host
        self.google_transport = google_transport
        self._activity = normalize_activity(getattr(module_host, 'activity', None))
        self._writing = normalize_activity(getattr(module_host, 'writer_activity', None))

    def activity(self):
        """Enter this host's complete synchronous business activity."""
        return self._activity()

    def writing(self):
        """Promote a complete host root before its first cooperative write."""
        return self._writing()

    def feature(self, module_id):
        if not self.module_host.builtin_active(module_id) or module_id not in self.module_host.started_backends:
            return None
        return self.module_host.backends.get(module_id)

    def project(self, project_id):
        project = self.store.get(project_id)
        if not project:
            raise ApiError(404, f'등록되지 않은 프로젝트: {project_id}')
        if not Path(project['path']).is_dir():
            raise ApiError(410, f"경로가 사라졌습니다: {project['path']}")
        return project

    @admitted
    def summary(self, project):
        workspace = self.feature('workspace-operations')
        if workspace is not None:
            return workspace.summary(project)
        return {**project, 'exists': Path(project['path']).is_dir(), 'git': 'unavailable',
                'branch': None, 'ahead': 0, 'behind': 0, 'dirty': 0,
                'has_remote': False, 'locks': 0, 'parked': False,
                'workspace_module_inactive': True}
