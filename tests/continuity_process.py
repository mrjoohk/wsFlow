"""Separate-process module plan/apply fixture; no provider/service activation."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wsflow.module_host import ModuleError, ModuleHost


def main():
    fixture, mode = Path(sys.argv[1]), sys.argv[2]
    host = ModuleHost(fixture / 'registry.json')
    try:
        if mode == 'plan':
            plan = host.plan({'action':'update', 'module_id':'work-calendar', 'expected_revision':0,
                              'source_path':str(fixture / 'package'), 'trusted':True})
            value = {'plan_id':plan['plan_id'], 'ready':plan['can_apply'],
                     'fingerprint':plan['continuity']['fingerprint']}
        else:
            try:
                result = host.apply(sys.argv[3])
                value = {'status':200, 'revision':result['revision'], 'active_revision':result['active_revision']}
            except ModuleError as exc:
                value = {'status':exc.status, 'message':str(exc), 'revision':host.snapshot()['revision']}
        print(json.dumps(value, ensure_ascii=False))
    finally:
        host.close()


if __name__ == '__main__':
    main()
