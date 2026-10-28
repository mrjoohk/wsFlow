"""Shared filesystem boundaries, independent of optional business modules.

These checks preserve the existing context and deployment policies. They observe
paths cooperatively; they do not create directories or claim OS-level isolation.
"""
from __future__ import annotations

from pathlib import Path


def linked(path):
    """Return whether a path is a symlink or a Windows reparse point."""
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def plain_path(path, owner):
    """Refuse all reparse components, including missing-path linked parents."""
    path, owner = Path(path).absolute(), Path(owner).absolute()
    if '..' in path.parts:
        raise ValueError('parent_traversal:' + str(path))
    path.relative_to(owner)
    path.resolve().relative_to(owner.resolve())
    for part in (path, *path.parents):
        if linked(part):
            raise ValueError('linked_path:' + str(part))
        if part == owner:
            break
    return path


def protected_destination(path, project):
    """General deploy must not modify configured context, even through aliases."""
    project, path = Path(project).resolve(), Path(path)
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(project)
    except (OSError, ValueError, RuntimeError):
        return '대상 실경로가 프로젝트 밖입니다'
    # Profile/manifest itself cannot be injected through legacy deployment.
    rel = relative.as_posix().casefold()
    if rel == '.wsflow' or rel.startswith('.wsflow/'):
        return 'context profile/manifest는 개발 환경 준비로 변경하십시오'
    configured = any((project / '.wsflow' / name).exists() for name in ('project.json','workspace.json','manifest.json'))
    if configured and (rel in ('agents.md','claude.md','agents.override.md','global_rules.md','project_rules.md') or
                       any(rel == p or rel.startswith(p + '/') for p in ('skills','.agents/skills','.claude/skills','.codex/skills'))):
        return 'context 관리 경로는 일반 배포로 변경할 수 없습니다'
    return None
