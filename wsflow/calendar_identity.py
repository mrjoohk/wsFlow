"""Read-only project identity for local calendar associations, never an adoption."""
from __future__ import annotations

import json
from pathlib import Path

from . import context_skills
from .readmodel import Reader, fingerprint

VERSION = 1


def observe(projects, registry):
    """Observe identity metadata only; legacy paths have an explicit weaker mode."""
    if not projects:
        return {}
    reader = Reader();cache = {};directories = {}

    def document(path, owner):
        path = Path(path).absolute()
        if path not in cache:
            context_skills.plain_path(path, owner)
            reader.files.append(path)
            info, raw = reader.read(path, str(path), 'identity', boundary=Path(owner))
            if info['status'] == 'missing':
                value = None
            elif info['status'] != 'ok':
                raise ValueError('identity_metadata_unavailable')
            else:
                value = json.loads(raw.decode('utf-8-sig'))
                if not isinstance(value, dict):
                    raise ValueError('identity_metadata_invalid')
            cache[path] = value
        return cache[path]

    def directory(path):
        path = Path(path).absolute()
        context_skills.plain_path(path, Path(path.anchor))
        st = path.stat()
        if not path.is_dir():
            raise ValueError('identity_directory_unavailable')
        value = {'path':str(path.resolve()),'dev':st.st_dev,'ino':st.st_ino}
        directories[str(path)] = value
        return value

    def profile(value):
        if value is not None and (type(value.get('schema_version')) is not int or value['schema_version'] != 1):
            raise ValueError('identity_profile_invalid')
        return value

    def uid(value, key):
        text = value.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            raise ValueError('identity_uid_invalid')
        return text

    result = {};profiles = {};roots = {};failure = None
    try:
        registry = Path(registry).absolute()
        config = document(registry, registry.parent) or {}
        registered_roots = config.get('roots', [])
        if not isinstance(registered_roots, list):
            raise ValueError('identity_registry_invalid')
        if len({p['id'] for p in projects}) != len(projects):
            raise ValueError('duplicate_project_registration')
        if len({Path(r['path']).resolve() for r in registered_roots}) != len(registered_roots):
            raise ValueError('duplicate_root_registration')
        for item in registered_roots:
            root = Path(item['path']).absolute()
            roots[str(root)] = profile(document(root/'.wsflow/workspace.json', root))
        for project in projects:
            checkout = Path(project['path']).absolute()
            profiles[project['id']] = profile(document(checkout/'.wsflow/project.json', checkout))
        for project in projects:
            pid = project['id'];checkout = Path(project['path']).absolute()
            value = {'version':VERSION,'path':str(checkout.resolve()),'status':'unavailable'}
            result[pid] = value
            try:
                boundary = directory(checkout)
                pp = profiles[pid]
                if pp is None:
                    value.update(status='legacy_path',directory=boundary)
                    continue
                if sum(Path(p['path']).resolve() == checkout.resolve() for p in projects) != 1:
                    raise ValueError('duplicate_checkout_registration')
                candidates = [Path(p) for p in roots if checkout.is_relative_to(Path(p)) and checkout != Path(p)]
                if not candidates:
                    raise ValueError('identity_root_unavailable')
                root = max(candidates, key=lambda p:len(p.parts));directory(root)
                rp = roots[str(root)]
                if rp is None:
                    raise ValueError('identity_root_unavailable')
                project_uid, root_uid = uid(pp,'project_uid'), uid(rp,'root_uid')
                if pp.get('checkout_path') != str(checkout) or rp.get('root_path') != str(root):
                    raise ValueError('identity_path_changed')
                if pp.get('root_uid') != root_uid:
                    raise ValueError('identity_parent_changed')
                if sum(p is not None and p.get('project_uid') == project_uid for p in profiles.values()) != 1:
                    raise ValueError('duplicate_project_uid')
                if sum(r is not None and r.get('root_uid') == root_uid for r in roots.values()) != 1:
                    raise ValueError('duplicate_root_uid')
                value.update(status='verified',project_uid=project_uid,root_uid=root_uid,root_path=str(root.resolve()))
            except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
                value.update(status='unavailable',reason=str(exc))
        if not reader.finish():
            raise ValueError('identity_changed_during_query')
        for path, before in list(directories.items()):
            if directory(path) != before:
                raise ValueError('identity_changed_during_query')
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        failure = str(exc)
    observation = fingerprint([reader.sources, directories])
    for project in projects:
        value = result.setdefault(project['id'], {'version':VERSION,'path':str(Path(project['path']).absolute())})
        if failure:
            value.update(status='unavailable',reason=failure)
        value['observation_id'] = observation
    return result


def reference(identity):
    """Persist only historical association; observations pin plans separately."""
    return {k:v for k,v in identity.items() if k != 'observation_id'}


def link_status(saved, current):
    if not isinstance(saved, dict) or saved.get('version') != VERSION:
        return 'unverified'
    if not current or current.get('status') not in {'verified','legacy_path'}:
        return 'unavailable'
    return saved['status'] if saved == reference(current) else 'unavailable'
