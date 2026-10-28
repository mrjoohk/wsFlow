"""Stable identity contract shared by independently loaded write adapters."""
from pathlib import Path


def binding(project, home):
    return {'project_id': project['id'], 'checkout': str(Path(project['path']).resolve()),
            'wf_home': str(Path(home).resolve())}
