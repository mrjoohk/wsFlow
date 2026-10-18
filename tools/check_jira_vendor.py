"""Read-only vendor hash/provenance check; optional mother checkout comparison."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from wsflow import workflow


def check(upstream=None):
    result=workflow.vendor_status()
    if upstream:
        upstream=Path(upstream).resolve()
        manifest=json.loads(workflow.MANIFEST.read_text(encoding='utf-8'))
        scripts=upstream/manifest['upstream_scripts']
        hashes={name:hashlib.sha256((scripts/name).read_bytes()).hexdigest() for name in manifest['files']}
        cp=subprocess.run(['git','-c',f'safe.directory={upstream.as_posix()}','-C',str(upstream),'rev-parse','HEAD'],
                          capture_output=True,encoding='utf-8',timeout=15,check=True)
        result.update(mother_commit=cp.stdout.strip(),mother_identical=hashes==result['files'],
                      recorded_commit_matches=cp.stdout.strip()==result['upstream_commit'])
        result['identical']=result['identical'] and result['mother_identical'] and result['recorded_commit_matches']
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--upstream');parser.add_argument('--out')
    args=parser.parse_args();result=check(args.upstream)
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.out:
        path=Path(args.out);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text+'\n',encoding='utf-8')
    print(text);sys.exit(0 if result['identical'] else 1)
