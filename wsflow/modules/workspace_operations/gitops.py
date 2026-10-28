"""git 어댑터 — subprocess 로 git CLI 를 호출한다. 셸을 거치지 않고, 입출력은 UTF-8 로 고정한다.

설계 원칙
- 파괴적 명령(reset/clean/checkout -- 등)은 제공하지 않는다. 지원 범위: status, log, show, remote, push, add, commit.
- Windows PowerShell 에서 한글 커밋 메시지가 깨지는 문제(ai_skills/github_push_template.ps1:49-51 의 우회)를
  `git commit -F <utf-8 파일>` 로 근본 처리한다.
- `.git/index.lock` 잔류(에이전트 세션이 삭제 권한 없이 git 을 돌릴 때 발생)를 감지·정리한다.
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from wsflow import fileio, operations

GIT_TIMEOUT = 120  # push 등 네트워크 명령 상한(초)
_ENV_BASE = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}


class GitError(RuntimeError):
    def __init__(self, message: str, returncode: int = 1, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def to_dict(self) -> dict:
        return {"error": str(self), "returncode": self.returncode, "stdout": self.stdout, "stderr": self.stderr}


def run_git(repo: str | Path, *args: str, timeout: int = GIT_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess:
    """git 을 repo 에서 실행한다. 출력은 UTF-8, 경로 quoting 은 끈다(한글 파일명 그대로)."""
    cmd = ["git", "-c", "core.quotepath=false", "-c", "i18n.logOutputEncoding=utf-8", *args]
    env = {**os.environ, **_ENV_BASE}
    try:
        cp = subprocess.run(cmd, cwd=str(repo), stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
                            env=env, timeout=timeout)
    except FileNotFoundError:
        raise GitError("git 실행 파일을 찾을 수 없습니다 (PATH 확인)")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} 이(가) {timeout}초 안에 끝나지 않았습니다", 124)
    if check and cp.returncode != 0:
        raise GitError(f"git {' '.join(args)} 실패 (exit {cp.returncode})", cp.returncode, cp.stdout, cp.stderr)
    return cp


# ---------------------------------------------------------------- 저장소 판별

def repo_kind(path: str | Path) -> str:
    """'repo' | 'broken' (.git 이 있으나 저장소 아님, 예: 빈 .git 디렉터리) | 'none'."""
    p = Path(path)
    if not (p / ".git").exists():
        return "none"
    cp = run_git(p, "rev-parse", "--is-inside-work-tree", check=False, timeout=15)
    return "repo" if cp.returncode == 0 and cp.stdout.strip() == "true" else "broken"


def git_dir(path: str | Path) -> Path:
    cp = run_git(path, "rev-parse", "--git-dir", timeout=15)
    d = Path(cp.stdout.strip())
    return d if d.is_absolute() else Path(path) / d


# ---------------------------------------------------------------- status / log

def status(repo: str | Path) -> dict:
    """`git status --porcelain=v2 --branch` 를 구조화한다.

    `--no-optional-locks`: status 가 인덱스를 갱신하며 만드는 `.git/index.lock` 을 만들지 않는다 —
    대시보드 폴링이 대상 저장소에 lock 을 남기는 것을 막는다(F-003, claude-code#11005 의 회피책과 동일).
    """
    cp = run_git(repo, "--no-optional-locks", "status", "--porcelain=v2", "--branch", "--untracked-files=all",
                 timeout=60)
    out = {"branch": None, "upstream": None, "ahead": 0, "behind": 0, "detached": False,
           "staged": [], "unstaged": [], "untracked": [], "conflicted": [], "stashes": 0}
    for line in cp.stdout.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
            out["branch"] = None if head == "(detached)" else head
            out["detached"] = head == "(detached)"
        elif line.startswith("# branch.upstream "):
            out["upstream"] = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            a, b = line[len("# branch.ab "):].split()
            out["ahead"], out["behind"] = int(a[1:]), int(b[1:])
        elif line.startswith("1 ") or line.startswith("2 "):
            parts = line.split(" ", 8 if line[0] == "1" else 9)
            xy = parts[1]
            path = parts[-1]
            if line[0] == "2":  # rename: "orig\tnew" 형태
                path = path.split("\t")[-1]
            if xy[0] != ".":
                out["staged"].append({"code": xy[0], "path": path})
            if xy[1] != ".":
                out["unstaged"].append({"code": xy[1], "path": path})
        elif line.startswith("u "):
            out["conflicted"].append(line.split(" ", 10)[-1])
        elif line.startswith("? "):
            out["untracked"].append(line[2:])
    st = run_git(repo, "stash", "list", check=False, timeout=30)
    out["stashes"] = len([l for l in st.stdout.splitlines() if l.strip()]) if st.returncode == 0 else 0
    out["clean"] = not (out["staged"] or out["unstaged"] or out["untracked"] or out["conflicted"])
    out["sync"] = sync_info(repo)
    return out


def _sync_path(repo):
    return operations.Store(repo).path.parent / 'git' / (operations.digest(str(Path(repo).resolve())) + '.json')


def sync_info(repo):
    """Read last successful explicit fetch. Status never fetches or creates files."""
    path = _sync_path(repo)
    if not path.is_file():
        return {'last_fetch_at': None, 'last_attempt': None, 'last_error': None}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'last_fetch_at': None, 'last_attempt': None, 'last_error': 'fetch 기록을 읽지 못했습니다'}


def _fetch(repo):
    previous = sync_info(repo)
    try:
        if not remotes(repo):
            result = {'ok': False, 'reason': 'no_remote', 'error': 'remote가 없습니다', 'command': None}
        else:
            cp = run_git(repo, 'fetch', '--all', check=False)
            result = {'ok': cp.returncode == 0, 'returncode': cp.returncode,
                      'stdout': cp.stdout, 'stderr': cp.stderr, 'command': 'git fetch --all'}
    except GitError as exc:
        result = {**exc.to_dict(), 'ok': False, 'command': 'git fetch --all'}
    at = operations.now()
    record = {'last_fetch_at': at if result['ok'] else previous.get('last_fetch_at'),
              'last_attempt': at, 'last_error': None if result['ok'] else result.get('stderr') or result.get('error')}
    fileio.atomic_write(_sync_path(repo), json.dumps(record, ensure_ascii=False).encode('utf-8'))
    return {**result, 'sync': record}


def fetch(repo):
    """Explicit remote-ref refresh, serialized outside the user's checkout."""
    with fileio.locked(_sync_path(repo).parent):
        return _fetch(repo)


def pull_ff(repo):
    """Fetch then fixed ff-only pull; never stash, rebase, merge or reset user work."""
    with fileio.locked(_sync_path(repo).parent):
        st = status(repo)
        reason = ('conflict' if st['conflicted'] else 'detached' if st['detached'] else
                  'no_upstream' if not st['upstream'] else 'dirty' if not st['clean'] else None)
        if reason:
            return {'ok': False, 'reason': reason, 'status': st, 'command': None}
        fetched = _fetch(repo)
        if not fetched['ok']:
            return {**fetched, 'reason': 'fetch_failed'}
        st = status(repo)
        if st['ahead'] and st['behind']:
            return {'ok': False, 'reason': 'non_ff', 'status': st, 'command': None, 'sync': fetched['sync']}
        if not st['clean']:
            return {'ok': False, 'reason': 'dirty', 'status': st, 'command': None, 'sync': fetched['sync']}
        args = ['-c', 'merge.autoStash=false', '-c', 'rebase.autoStash=false',
                'pull', '--ff-only', '--no-rebase', '--no-autostash']
        cp = run_git(repo, *args, check=False)
        return {'ok': cp.returncode == 0, 'reason': None if cp.returncode == 0 else 'pull_failed',
                'returncode': cp.returncode, 'stdout': cp.stdout, 'stderr': cp.stderr,
                'command': 'git ' + ' '.join(args), 'status': status(repo), 'sync': fetched['sync']}


_LOG_SEP = "\x1f"


def log(repo: str | Path, n: int = 50, ref: str | None = None) -> list[dict]:
    fmt = _LOG_SEP.join(["%H", "%h", "%an", "%ad", "%s", "%D"])
    args = ["log", f"-n{int(n)}", f"--format={fmt}", "--date=iso-strict"]
    if ref:
        args.append(ref)
    cp = run_git(repo, *args, check=False, timeout=60)
    if cp.returncode != 0:  # 커밋이 0개인 저장소 등
        return []
    rows = []
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        h, short, an, ad, s, refs = (line.split(_LOG_SEP) + [""] * 6)[:6]
        rows.append({"hash": h, "short": short, "author": an, "date": ad, "subject": s,
                     "refs": [r.strip() for r in refs.split(",") if r.strip()]})
    return rows


def show_files(repo: str | Path, commit: str) -> dict:
    cp = run_git(repo, "show", "--stat=200", "--format=%H%n%an%n%ad%n%B%n--END--", "--date=iso-strict", commit, timeout=60)
    head, _, stat = cp.stdout.partition("--END--")
    lines = head.split("\n", 3)
    body = lines[3] if len(lines) > 3 else ""
    files = [l.strip() for l in stat.splitlines() if "|" in l]
    return {"hash": lines[0], "author": lines[1] if len(lines) > 1 else "", "date": lines[2] if len(lines) > 2 else "",
            "message": body.strip(), "files": files}


# ---------------------------------------------------------------- remote

def remotes(repo: str | Path) -> list[dict]:
    cp = run_git(repo, "remote", "-v", timeout=30)
    table: dict[str, dict] = {}
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, url, kind = parts[0], parts[1], parts[2].strip("()")
        table.setdefault(name, {"name": name, "fetch": None, "push": None})[kind] = url
    return list(table.values())


def remote_add(repo: str | Path, name: str, url: str) -> None:
    _check_name(name)
    run_git(repo, "remote", "add", name, url, timeout=30)


def remote_set_url(repo: str | Path, name: str, url: str) -> None:
    _check_name(name)
    run_git(repo, "remote", "set-url", name, url, timeout=30)


def remote_remove(repo: str | Path, name: str) -> None:
    _check_name(name)
    run_git(repo, "remote", "remove", name, timeout=30)


def _check_name(name: str) -> None:
    if not name or name.startswith("-") or any(c.isspace() for c in name):
        raise GitError(f"잘못된 remote 이름: {name!r}")


# ---------------------------------------------------------------- push / add / commit

def push(repo: str | Path, remote: str = "origin", branch: str | None = None, set_upstream: bool = False) -> dict:
    _check_name(remote)
    args = ["push"]
    if set_upstream:
        args.append("-u")
    args.append(remote)
    if branch:
        if branch.startswith("-"):
            raise GitError(f"잘못된 브랜치 이름: {branch!r}")
        args.append(branch)
    cp = run_git(repo, *args, check=False)
    return {"ok": cp.returncode == 0, "returncode": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr,
            "command": "git " + " ".join(args)}


def add(repo: str | Path, paths: list[str] | None = None, all_: bool = False) -> None:
    if all_:
        run_git(repo, "add", "-A", timeout=120)
        return
    if not paths:
        raise GitError("스테이징할 경로가 없습니다")
    for p in paths:
        if p.startswith("-"):
            raise GitError(f"잘못된 경로: {p!r}")
    run_git(repo, "add", "--", *paths, timeout=120)


def commit(repo: str | Path, message: str, add_all: bool = False) -> dict:
    """메시지를 UTF-8(BOM 없음) 임시 파일로 써서 `git commit -F` 한다 — 콘솔 인코딩과 무관하게 한글이 보존된다."""
    if not message or not message.strip():
        raise GitError("커밋 메시지가 비어 있습니다")
    if add_all:
        run_git(repo, "add", "-A", timeout=120)
    fd, tmp = tempfile.mkstemp(prefix="wsm_commit_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(message.strip() + "\n")
        cp = run_git(repo, "commit", "-F", tmp, check=False, timeout=120)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if cp.returncode != 0:
        raise GitError("git commit 실패", cp.returncode, cp.stdout, cp.stderr)
    head = run_git(repo, "rev-parse", "HEAD", timeout=15).stdout.strip()
    return {"ok": True, "hash": head, "stdout": cp.stdout}


# ---------------------------------------------------------------- index.lock

def _git_running() -> bool:
    """git 프로세스가 돌고 있으면 True. 확인 불가 시 True(보수적)."""
    try:
        if sys.platform.startswith("win"):
            cp = subprocess.run(["tasklist", "/FI", "IMAGENAME eq git.exe", "/NH"], capture_output=True,
                                encoding="utf-8", errors="replace", timeout=15)
            return "git.exe" in cp.stdout.lower()
        cp = subprocess.run(["pgrep", "-x", "git"], capture_output=True, timeout=15)
        return cp.returncode == 0
    except Exception:
        return True


def find_locks(repo: str | Path) -> list[dict]:
    """저장소의 .git 안 *.lock 파일(index.lock, HEAD.lock, refs/**.lock 등)을 나열한다."""
    try:
        gd = git_dir(repo)
    except GitError:
        gd = Path(repo) / ".git"
        if not gd.is_dir():
            return []
    now = time.time()
    found = []
    for lock in gd.rglob("*.lock"):
        if "objects" in lock.parts:  # 객체 팩 lock 은 대상 아님
            continue
        try:
            st = lock.stat()
        except OSError:
            continue
        found.append({"path": str(lock), "age_sec": int(now - st.st_mtime), "size": st.st_size})
    return found


def clean_locks(repo: str | Path, min_age_sec: int = 5) -> dict:
    """git 프로세스가 없고 lock 이 min_age_sec 보다 오래됐을 때만 삭제한다."""
    locks = find_locks(repo)
    if not locks:
        return {"removed": [], "skipped": [], "reason": "lock 없음"}
    if _git_running():
        return {"removed": [], "skipped": [l["path"] for l in locks], "reason": "git 프로세스 실행 중 — 건너뜀"}
    removed, skipped = [], []
    for l in locks:
        if l["age_sec"] < min_age_sec:
            skipped.append(l["path"])
            continue
        try:
            os.unlink(l["path"])
            removed.append(l["path"])
        except OSError as e:
            skipped.append(f"{l['path']} ({e.strerror})")
    return {"removed": removed, "skipped": skipped, "reason": ""}
