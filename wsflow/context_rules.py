"""Structural policy diagnostics. Never infer semantic compliance/tool loading."""
from __future__ import annotations

import re
from pathlib import Path


def references(text: str, base: Path) -> set[Path]:
    """Recognize explicit Markdown/code/import path literals without following them."""
    candidates = re.findall(r'`([^`\n]+\.md)`|\]\(([^)\n]+\.md)\)|@"([^"]+\.md)"|@([^\s]+\.md)', text)
    result = set()
    for group in candidates:
        value = next(x for x in group if x).strip('<>')
        if '://' in value:
            continue
        try:
            result.add((base / value).resolve())
        except (OSError, RuntimeError):
            continue
    return result


def resolve(sources: list[dict], texts: dict[str, str], common: Path | None,
            project: Path, exceptions: list) -> dict:
    by_id = {s['logical_id']: s for s in sources}
    blockers, warnings = [], []
    common_id = 'root:common_rules' if common is not None else 'project:GLOBAL_RULES.md'
    global_source = by_id.get(common_id, {})
    if global_source.get('status') != 'ok':
        blockers.append('common_rules_missing')
    local = by_id.get('project:PROJECT_RULES.md', {})
    if local.get('status') != 'ok':
        blockers.append('project_rules_missing')
    shadow = by_id.get('project:GLOBAL_RULES.md', {})
    if common is not None and shadow.get('status') == 'ok':
        if shadow['sha256'] != global_source.get('sha256'):
            blockers.append('common_rules_shadowed')
        else:
            warnings.append('duplicate_common_rules_copy')
    if by_id.get('project:AGENTS.override.md', {}).get('status') == 'ok':
        blockers.append('entrypoint_override')
    expected = {Path(s['resolved_path']) for s in (global_source, local)
                if s.get('status') == 'ok'}
    entries = []
    for name in ('AGENTS.md', 'CLAUDE.md'):
        ident = 'project:' + name
        refs = references(texts.get(ident, ''), project)
        complete = len(expected) == 2 and expected.issubset(refs)
        entries.append({'path': str(project / name), 'canonical_refs_complete': complete,
                        'canonical_refs': sorted(str(p) for p in refs.intersection(expected)),
                        'observation_kind': 'static_only'})
        if not complete:
            blockers.append('entrypoint_refs_missing:' + name)
    valid_exceptions = []
    for exc in exceptions:
        if not isinstance(exc, dict) or not isinstance(exc.get('id'), str) or not exc['id'].strip() or not isinstance(exc.get('reason'), str) or not exc['reason'].strip():
            blockers.append('invalid_policy_exception')
        else:
            valid_exceptions.append({'id': exc['id'], 'reason': exc['reason']})
    return {'common_source': global_source.get('resolved_path'),
            'project_source': local.get('resolved_path'), 'entrypoints': entries,
            'exceptions': valid_exceptions, 'warnings': warnings, 'blockers': blockers,
            'precedence': {'project_facts': 'PROJECT_RULES', 'common_process': 'GLOBAL_RULES',
                           'semantic_conflicts': 'unknown'}, 'ready': not blockers}
