#!/usr/bin/env python3
"""테스트 실행 + 기계 판독 증거 파일 생성 (GLOBAL_RULES Rule 13: 시험 실행은 evidence 를 남긴다).

python tests/run_tests.py  →  logs/test_evidence/YYMMDD_HHMM_evidence.json  (EXIT 0=전부 통과)
"""
import io
from contextlib import contextmanager
import faulthandler
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


@contextmanager
def _unit_diagnostics(directory, interval=120):
    """Optionally record partial progress; only main's completed evidence is a result."""
    if not directory:
        yield None, lambda event, **fields: None
        return
    folder = Path(directory).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'progress.jsonl').open('x', encoding='utf-8') as progress, \
            (folder / 'tracebacks.log').open('xb') as stacks:
        started = time.monotonic()

        def emit(event, observed_at=None, **fields):
            observed_at = time.monotonic() if observed_at is None else observed_at
            progress.write(json.dumps(dict(event=event, at=time.time(),
                elapsed_seconds=round(observed_at - started, 6), **fields)) + '\n')
            progress.flush()

        class ObservedResult(unittest.TextTestResult):
            def startTest(self, test):
                super().startTest(test)
                self._case_started = time.monotonic()
                emit('start', test=test.id(), tests_run=self.testsRun, observed_at=self._case_started)

            def stopTest(self, test):
                try:
                    stopped = time.monotonic()
                    emit('stop', test=test.id(), seconds=round(stopped - self._case_started, 6),
                         tests_run=self.testsRun, failures=len(self.failures), errors=len(self.errors),
                         skipped=len(self.skipped), observed_at=stopped)
                finally:
                    super().stopTest(test)

        emit('suite_start', pid=os.getpid(), stack_interval_seconds=interval)
        faulthandler.dump_traceback_later(interval, repeat=True, file=stacks)
        try:
            yield ObservedResult, emit
        except BaseException as exc:
            emit('suite_aborted', error_type=type(exc).__name__)
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run(resultclass, emit) -> int:
    t0 = time.time()
    buf = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    previous_data = os.environ.get('WSFLOW_DATA_DIR')
    with tempfile.TemporaryDirectory(prefix='wsflow_test_userdata_') as user_data:
        os.environ['WSFLOW_DATA_DIR'] = user_data
        try:
            result = unittest.TextTestRunner(stream=buf, verbosity=2, resultclass=resultclass).run(suite)
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
    display = p.relative_to(ROOT) if p.is_relative_to(ROOT) else p
    print(f"\nevidence: {display}  ok={ev['ok']} run={ev['tests_run']} fail={ev['failures']} err={ev['errors']} skip={ev['skipped']}")
    emit('suite_complete', tests_run=ev['tests_run'], failures=ev['failures'], errors=ev['errors'],
         skipped=ev['skipped'], ok=ev['ok'], evidence=str(p))
    return 0 if ev["ok"] else 1


def main() -> int:
    with _unit_diagnostics(os.environ.get('WSFLOW_UNIT_DIAGNOSTICS')) as (resultclass, emit):
        return _run(resultclass, emit)


if __name__ == "__main__":
    sys.exit(main())
