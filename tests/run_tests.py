#!/usr/bin/env python3
"""테스트 실행 + 기계 판독 증거 파일 생성 (GLOBAL_RULES Rule 13: 시험 실행은 evidence 를 남긴다).

python tests/run_tests.py  →  logs/test_evidence/YYMMDD_HHMM_evidence.json  (EXIT 0=전부 통과)
"""
import io
import json
import platform
import subprocess
import sys
import time
import unittest
import tempfile
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> int:
    t0 = time.time()
    buf = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    previous_data = os.environ.get('WSFLOW_DATA_DIR')
    with tempfile.TemporaryDirectory(prefix='wsflow_test_userdata_') as user_data:
        os.environ['WSFLOW_DATA_DIR'] = user_data
        try:
            result = unittest.TextTestRunner(stream=buf, verbosity=2).run(suite)
        finally:
            if previous_data is None:
                os.environ.pop('WSFLOW_DATA_DIR', None)
            else:
                os.environ['WSFLOW_DATA_DIR'] = previous_data
    out = buf.getvalue()
    sys.stdout.write(out)
    git_ver = subprocess.run(["git", "--version"], capture_output=True, encoding="utf-8", errors="replace").stdout.strip()
    ev = {
        "run_id": datetime.now().strftime("%y%m%d_%H%M%S_%f"),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "python": platform.python_version(), "platform": platform.platform(), "git": git_ver,
        "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "duration_sec": round(time.time() - t0, 2),
        "ok": result.wasSuccessful(),
        "failed_tests": [str(t) for t, _ in result.failures + result.errors],
        "skipped_tests": [f"{t}: {r}" for t, r in result.skipped],
    }
    d = Path(os.environ.get('WSFLOW_RUN_EVIDENCE_DIR') or ROOT / "logs" / "test_evidence").resolve()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ev['run_id']}_evidence.json"
    p.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nevidence: {p.relative_to(ROOT)}  ok={ev['ok']} run={ev['tests_run']} fail={ev['failures']} err={ev['errors']} skip={ev['skipped']}")
    return 0 if ev["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
