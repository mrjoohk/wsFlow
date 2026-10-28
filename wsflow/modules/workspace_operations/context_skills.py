"""Bounded package inventory. No skill code or hooks are executed."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from wsflow.operations import digest
from wsflow.path_policy import linked, plain_path  # Compatibility exports for existing callers.


def package(path, owner):
    path = plain_path(path, owner)
    files, directories, total = {}, [], 0
    pending = [path]
    while pending:
        current = pending.pop()
        for child in sorted(current.iterdir()):
            plain_path(child, owner)
            rel = child.relative_to(path).as_posix()
            if child.is_dir():
                directories.append(rel); pending.append(child)
            elif child.is_file():
                if child.stat().st_size > 64 * 1024 * 1024 - total:
                    raise ValueError('package_limit:' + str(path))
                raw = child.read_bytes(); total += len(raw)
                if len(files) >= 4000 or total > 64 * 1024 * 1024:
                    raise ValueError('package_limit:' + str(path))
                files[rel] = hashlib.sha256(raw).hexdigest()
                if child.suffix.lower() == '.md':
                    text = raw.decode('utf-8-sig')
                    refs = re.findall(r'\]\(([^)]+)\)', text)
                    for ref in refs:
                        if '://' in ref or ref.startswith('#'):
                            continue
                        ref = ref.split('#', 1)[0].strip('<>')
                        target = (child.parent / ref).resolve()
                        if not target.is_relative_to(path.resolve()) or not target.exists():
                            raise ValueError('package_dependency:' + str(child) + ':' + ref)
            else:
                raise ValueError('package_special_file:' + str(child))
    if 'SKILL.md' not in files:
        raise ValueError('skill_entry_missing:' + str(path))
    return {'files': files, 'directories': sorted(directories),
            'tree_hash': digest({'files': files, 'directories': sorted(directories)}), 'file_count': len(files)}


def catalog(root, checkout, other_projects):
    packages, blockers = [], []
    for scope, base in (('common', root / 'skills'), ('project', checkout / 'skills')):
        try:
            plain_path(base, root if scope == 'common' else checkout)
            if not base.exists():
                continue
            for child in sorted(base.iterdir(), key=lambda p:p.name.casefold()):
                if not child.is_dir() and not linked(child):
                    continue
                reserved={'con','prn','aux','nul',*(p+str(n) for p in ('com','lpt') for n in range(1,10))}
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', child.name) or child.name.casefold() in reserved:
                    raise ValueError('invalid_skill_name:' + child.name)
                if any(child.resolve().is_relative_to(p) or p.is_relative_to(child.resolve()) for p in other_projects):
                    raise ValueError('sibling_skill_source:' + str(child))
                info = package(child, base)
                packages.append({'skill_id':scope + ':' + child.name, 'name':child.name,
                                 'source_scope':scope, 'path':str(child), **info})
        except (OSError, ValueError, UnicodeError) as exc:
            blockers.append(str(exc))
    names = set()
    for p in packages:
        key = p['name'].casefold()
        if key in names:
            blockers.append('skill_name_collision:' + key)
        names.add(key)
    return packages, blockers
