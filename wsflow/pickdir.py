"""폴더 선택 대화상자 — 브라우저는 보안상 선택한 폴더의 전체 경로를 주지 않으므로, 로컬 서버가
표준 라이브러리 tkinter 로 OS 네이티브 폴더 선택 창을 띄우고 경로를 돌려준다(외부 의존 0, D-001).

tkinter 는 별도 파이썬 프로세스에서 띄운다 — 서버 스레드에서 Tk 를 직접 만들면 스레드 제약과
Tk 잔류 문제가 있고, 프로세스로 격리하면 tkinter 부재도 깨끗한 오류로 끝난다.
동시 요청은 잠금으로 직렬화한다(대화상자 두 개가 겹치지 않게).

시험 훅: 환경변수 WSFLOW_PICK_RESULT 가 있으면 대화상자 없이 그 값을 돌려준다(빈 문자열 = 취소).
파일 복수 선택(pick_files)은 같은 훅을 줄바꿈 구분 목록으로 읽는다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

_LOCK = threading.Lock()
TEST_ENV = "WSFLOW_PICK_RESULT"

_SCRIPT = r'''
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:
    sys.stderr.write("tkinter 모듈이 없습니다 (Python 설치 시 tcl/tk 옵션 필요)")
    sys.exit(2)
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
initial = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
mode = sys.argv[3] if len(sys.argv) > 3 else "dir"
try:
    if mode == "files":
        p = "\n".join(filedialog.askopenfilenames(title=sys.argv[2], initialdir=initial, parent=root) or ())
    else:
        p = filedialog.askdirectory(title=sys.argv[2], initialdir=initial, mustexist=True, parent=root)
finally:
    root.destroy()
sys.stdout.write(p or "")
'''


def _run(initial: str | None, title: str, mode: str) -> str:
    if TEST_ENV in os.environ:
        return os.environ[TEST_ENV]
    with _LOCK:
        cp = subprocess.run([sys.executable, "-c", _SCRIPT, initial or "", title, mode], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=600,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if cp.returncode != 0:
        raise RuntimeError(f"선택 창을 열 수 없습니다: {(cp.stderr or '').strip()[-400:]}")
    return cp.stdout.strip()


def pick_folder(initial: str | None = None, title: str = "wsFlow — 폴더 선택") -> str | None:
    """선택한 폴더의 절대 경로. 사용자가 취소하면 None. 대화상자를 띄울 수 없으면 RuntimeError."""
    return _run(initial, title, "dir") or None


def pick_files(initial: str | None = None, title: str = "wsFlow — 파일 선택(복수)") -> list[str]:
    """선택한 파일들의 절대 경로 목록. 취소하면 []."""
    out = _run(initial, title, "files")
    return [x for x in out.splitlines() if x.strip()]
