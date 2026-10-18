"""I0 context inventory for one checkout. Read-only; no agent/skill execution."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import json

from . import context_rules
from .readmodel import Reader, fingerprint, inside

MAX_PACKAGE_FILES = 4000


def _profile(reader, path, ident, owner, blockers):
    meta, raw = reader.read(path, ident, owner)
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value, dict) or value.get('schema_version') != 1:
            raise ValueError('unsupported profile')
        for key in ('root_uid', 'project_uid', 'rules_path', 'skills_path'):
            if key in value and (not isinstance(value[key], str) or not value[key].strip()):
                raise ValueError('invalid profile field')
        for key in ('selected_skills', 'policy_exceptions'):
            if key in value and not isinstance(value[key], list):
                raise ValueError('invalid profile list')
        if any(not isinstance(x, str) for x in value.get('selected_skills', [])):
            raise ValueError('invalid skill selection')
        return value
    except (ValueError, UnicodeError):
        meta.update(status='error', error='invalid_profile')
        blockers.append('invalid_profile:' + owner)
        return {}


def _relative(base, value):
    rel = Path(value)
    if not rel.parts or rel.is_absolute() or '..' in rel.parts or rel.drive or '\\' in value:
        raise ValueError('profile paths must be relative and within their owner')
    return base / rel


def _packages(reader, base, owner, warnings):
    packages = []
    try:
        if not reader.allowed(base.resolve()):
            reader.record(base, owner + ':skills', owner, 'blocked')
            return packages
        children = sorted(base.iterdir(), key=lambda p: p.name.casefold())
    except FileNotFoundError:
        return packages
    except (OSError, RuntimeError):
        reader.record(base, owner + ':skills', owner, 'error', error='catalog_unreadable')
        return packages
    for package in children:
        if not package.is_dir() and not package.is_symlink():
            continue
        files, stack, seen = [], [package], set()
        while stack:
            current = stack.pop()
            try:
                resolved = current.resolve()
                if not reader.allowed(resolved) or resolved in seen or len(seen) >= MAX_PACKAGE_FILES:
                    files.append(reader.record(current, owner + ':skill:' + package.name, owner,
                                               'blocked', error='outside_scope_or_cycle'))
                    continue
                seen.add(resolved)
                for child in sorted(current.iterdir(), key=lambda p: p.name):
                    if len(files) >= MAX_PACKAGE_FILES:
                        files.append(reader.record(current, owner + ':skill:' + package.name, owner,
                                                   'error', error='package_file_limit'))
                        stack.clear()
                        break
                    if child.is_dir():
                        stack.append(child)
                    else:
                        meta, _ = reader.read(child, owner + ':skill:' + str(child.relative_to(base)), owner)
                        files.append(meta)
            except (OSError, RuntimeError):
                files.append(reader.record(current, owner + ':skill:' + package.name, owner,
                                           'error', error='package_unreadable'))
        valid = any(Path(f['declared_path']).name == 'SKILL.md' and f['status'] == 'ok' for f in files)
        if not valid:
            warnings.append('skill_entry_missing:' + owner + ':' + package.name)
        entries = sorted((str(Path(f['declared_path']).relative_to(package)), f['status'], f['sha256']) for f in files)
        complete = valid and all(f['status'] == 'ok' for f in files)
        packages.append({'skill_id': owner + ':' + package.name, 'name': package.name,
                         'source_scope': owner, 'declared_path': str(package),
                         'tree_hash': fingerprint(entries) if complete else None,
                         'complete': complete, 'file_count': len(files)})
    return packages


def _extra_rules(reader, base, owner):
    """Observe explicit rule directories, without imports or recursive link following."""
    pending, seen = [base], set()
    present = False
    while pending:
        current = pending.pop()
        try:
            resolved = current.resolve()
            if not reader.allowed(resolved) or resolved in seen or len(seen) >= MAX_PACKAGE_FILES:
                reader.record(current, owner + ':rules', owner, 'blocked')
                continue
            seen.add(resolved)
            for child in sorted(current.iterdir()):
                if child.is_dir():
                    pending.append(child)
                elif child.suffix.lower() == '.md':
                    meta, _ = reader.read(child, owner + ':rule:' + str(child.relative_to(base)), owner)
                    present = present or meta['status'] == 'ok'
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError):
            reader.record(current, owner + ':rules', owner, 'error', error='rules_unreadable')
    return present


def inspect(project: dict, roots: list[dict], projects: list[dict] | None = None) -> dict:
    """Return schema-v1 static context for a registered project, without migration.

    Args: project/roots/projects are existing registry dictionaries (paths in OS units).
    Returns: JSON snapshot; missing UID stays None, unknown tool/global scope stays unknown.
    """
    checkout = Path(project['path']).expanduser().resolve()
    blockers, warnings = [], []
    candidates = []
    for registered in roots:
        try:
            candidate = Path(registered['path']).expanduser().resolve()
            if inside(checkout, candidate):
                candidates.append(candidate)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            warnings.append('invalid_registered_root')
    root = max(candidates, key=lambda p: len(p.parts)) if candidates else None
    if not checkout.is_dir():
        blockers.append('checkout_missing')
    if root is None:
        warnings.append('unregistered_root')
    if len(set(candidates)) > 1:
        warnings.append('nested_roots_nearest_selected')
    excluded = [Path(p['path']).expanduser().resolve() for p in projects or []
                if p['id'] != project['id'] and Path(p['path']).expanduser().resolve() != checkout]
    # Sibling project subtrees cannot be read even when a shared skill link points there.
    # An ancestor registry entry must not exclude this selected checkout itself.
    excluded = [p for p in excluded if not inside(checkout, p)]
    root_files = [root / x for x in ('.wsflow/workspace.json', 'GLOBAL_RULES.md', 'AGENTS.md', 'CLAUDE.md')] if root else []
    reader = Reader([checkout] + ([root / 'skills'] if root else []), root_files, excluded)
    pp = _profile(reader, checkout / '.wsflow/project.json', 'project:profile', 'project', blockers)
    rp = _profile(reader, root / '.wsflow/workspace.json', 'root:profile', 'common', blockers) if root else {}
    try:
        common = _relative(root, rp.get('rules_path', 'GLOBAL_RULES.md')) if root else None
        common_skills = _relative(root, rp.get('skills_path', 'skills')) if root else None
        local_rules = _relative(checkout, pp.get('rules_path', 'PROJECT_RULES.md'))
        local_skills = _relative(checkout, pp.get('skills_path', 'skills'))
    except ValueError:
        blockers.append('invalid_profile_path')
        common, common_skills = (root / 'GLOBAL_RULES.md', root / 'skills') if root else (None, None)
        local_rules, local_skills = checkout / 'PROJECT_RULES.md', checkout / 'skills'
    if common:
        reader.files.append(common)
        reader.directories.append(common_skills)
    texts = {}
    names = {'project:PROJECT_RULES.md': local_rules,
             **{'project:' + n: checkout / n for n in ('GLOBAL_RULES.md', 'AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md')}}
    if common:
        names['root:common_rules'] = common
        names.update({'root:' + n: root / n for n in ('AGENTS.md', 'CLAUDE.md')})
    for ident, path in names.items():
        meta, raw = reader.read(path, ident, 'common' if ident.startswith('root:') else 'project')
        if raw is not None:
            try:
                texts[ident] = raw.decode('utf-8-sig')
            except UnicodeError:
                meta.update(status='error', error='invalid_utf8')
    rules = context_rules.resolve(reader.sources, texts, common, checkout, pp.get('policy_exceptions', []))
    blockers.extend(rules['blockers']); warnings.extend(rules['warnings'])
    skills = _packages(reader, common_skills, 'common', warnings) if common_skills else []
    skills += _packages(reader, local_skills, 'project', warnings)
    canonical = list(skills)
    for rel in ('.agents/skills', '.codex/skills', '.claude/skills'):
        skills += _packages(reader, checkout / rel, rel, warnings)
    if _extra_rules(reader, checkout / '.claude/rules', 'project'):
        warnings.append('additional_claude_rules_semantics_unknown')
    names_seen = {}
    for skill in canonical:
        key = skill['name'].casefold()
        if key in names_seen:
            blockers.append('skill_name_collision')
        names_seen[key] = skill['skill_id']
    selected = pp.get('selected_skills', [])
    if any(s not in {k['skill_id'] for k in canonical} for s in selected):
        blockers.append('selected_skill_missing')
    if any(k['skill_id'] in selected and not k['complete'] for k in canonical):
        blockers.append('selected_skill_incomplete')
    if pp.get('root_uid') and pp['root_uid'] != rp.get('root_uid'):
        blockers.append('root_uid_mismatch')
    uid = pp.get('project_uid')
    if uid and any(p['id'] != project['id'] and p.get('project_uid') == uid for p in projects or []):
        blockers.append('duplicate_registry_project_uid')
    if not reader.finish():
        blockers.append('source_unreadable_or_changed')
    rules['ready'] = not blockers
    rules['readiness_kind'] = 'structural_only'
    git_root = None
    try:
        cp = subprocess.run(['git', '-C', str(checkout), 'rev-parse', '--show-toplevel'],
                            capture_output=True, encoding='utf-8', errors='replace', timeout=5,
                            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
        if cp.returncode == 0:
            git_root = str(Path(cp.stdout.strip()).resolve())
    except (OSError, subprocess.TimeoutExpired):
        warnings.append('git_boundary_unknown')
    return {'schema_version': 1, 'snapshot_id': str(uuid4()),
            'captured_at': datetime.now(timezone.utc).isoformat(), 'project_id': project['id'],
            'project_uid': uid, 'root_uid': rp.get('root_uid'),
            'identity_status': 'configured' if uid and rp.get('root_uid') else 'legacy',
            'checkout_path': str(checkout), 'git_root': git_root,
            'root_path': str(root) if root else None, 'tool_profile_id': None,
            'sources': reader.sources, 'source_fingerprint': fingerprint(reader.sources),
            'skills': skills, 'expected_skills': selected, 'rules': rules,
            'warnings': sorted(set(warnings)), 'blockers': sorted(set(blockers)),
            'scope_complete': False, 'observation_kind': 'static_only',
            'unknown_scope': ['tool_loading', 'user_global', 'plugins', 'semantic_compliance',
                              'duplicate_uids_in_unread_sibling_profiles']}
