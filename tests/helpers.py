"""테스트 공용 — 임시 git 저장소 fixture."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), env=GIT_ENV, capture_output=True, encoding="utf-8",
                          errors="replace", check=True)


def make_repo(base: Path, name: str = "repo", commits: int = 1) -> Path:
    p = base / name
    p.mkdir(parents=True)
    git(p, "init", "-q", "-b", "main")
    git(p, "config", "user.name", "t")
    git(p, "config", "user.email", "t@x")
    for i in range(commits):
        (p / f"f{i}.txt").write_text(f"v{i}\n", encoding="utf-8")
        git(p, "add", "-A")
        git(p, "commit", "-q", "-m", f"c{i} 커밋")
    return p


def make_bare(base: Path, name: str = "remote.git") -> Path:
    p = base / name
    git(base, "init", "-q", "--bare", "-b", "main", str(p))
    return p


class TempDir:
    def __enter__(self):
        self._t = tempfile.TemporaryDirectory(prefix="wsm_test_")
        return Path(self._t.name)

    def __exit__(self, *a):
        self._t.cleanup()
