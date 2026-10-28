"""워크스페이스 스캐너 — 루트 아래 후보 폴더 나열, 프로젝트별 규칙 준수 지표 계산.

읽기 전용이다. 지표는 GLOBAL_RULES 가 요구하는 파일의 존재·버전·상태를 기계적으로 집계한 것이며,
"프로젝트가 잘 되고 있는가"가 아니라 "규칙이 요구하는 근거 사슬이 파일로 존재하는가"를 본다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from . import gitops

VALID_LEDGER_STATUS = {"open", "fixed", "verified", "rejected", "superseded", "deferred"}
TRACKED_TOOLS = ["GLOBAL_RULES.md", "tools/graph_checks.py", "tools/worklog/validate_worklog.py",
                 "tools/worklog/worklog_schema.py"]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist"}


def _md5(p: Path) -> str | None:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _nested_repos(root: Path, max_depth: int = 3) -> list[str]:
    found = []
    base_depth = len(root.parts)
    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        depth = len(d.parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [n for n in dirnames if n not in SKIP_DIRS]
        if depth > 0 and (d / ".git").exists():
            found.append(str(d.relative_to(root)).replace("\\", "/"))
            dirnames[:] = []
    return found


# ---------------------------------------------------------------- 루트 스캔

def scan_root(root: str | Path, registered: set[str]) -> list[dict]:
    """루트 바로 아래 디렉터리들을 후보로 나열한다."""
    rp = Path(root)
    out = []
    for child in sorted(rp.iterdir(), key=lambda c: c.name.lower()):
        if not child.is_dir() or child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        p = str(child.resolve()).replace("\\", "/")
        kind = gitops.repo_kind(child) if (child / ".git").exists() else "none"
        out.append({
            "name": child.name, "path": p, "registered": p in registered, "git": kind,
            "global_rules": (child / "GLOBAL_RULES.md").is_file(),
            "project_rules": (child / "PROJECT_RULES.md").is_file(),
            "ledger": (child / "graph" / "edges.csv").is_file(),
            "logs_jsonl": (child / "logs" / "files.jsonl").is_file(),
            "logs_xlsx": (child / "0.FilesUpdate.xlsx").is_file(),
            "parked": (child / "_PARKED.txt").is_file(),
        })
    return out


# ---------------------------------------------------------------- 규칙 지표

def ledger_summary(path: Path) -> dict:
    counts: dict[str, int] = {}
    invalid: dict[str, int] = {}
    total = 0
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                st = (row.get("status") or "").strip()
                total += 1
                counts[st] = counts.get(st, 0) + 1
                if st not in VALID_LEDGER_STATUS:
                    invalid[st] = invalid.get(st, 0) + 1
    except (OSError, csv.Error):
        return {"exists": False}
    return {"exists": True, "total": total, "counts": counts, "invalid_status": invalid}


def worklog_summary(logs: Path) -> dict:
    out = {"files_jsonl": (logs / "files.jsonl").is_file(), "prompts_jsonl": (logs / "prompts.jsonl").is_file(),
           "last_req_id": None, "prompts": 0, "files": 0, "uncommitted": 0, "unverified": 0, "bad_lines": 0}

    def read(p: Path):
        try:
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        out["bad_lines"] += 1
        except OSError:
            return

    if out["prompts_jsonl"]:
        for rec in read(logs / "prompts.jsonl"):
            out["prompts"] += 1
            if rec.get("req_id"):
                out["last_req_id"] = rec["req_id"]
    if out["files_jsonl"]:
        for rec in read(logs / "files.jsonl"):
            out["files"] += 1
            if rec.get("commit") == "미커밋":
                out["uncommitted"] += 1
            if rec.get("verify") == "검증 없음":
                out["unverified"] += 1
    return out


def tool_drift(project: Path, template: Path | None) -> list[dict]:
    rows = []
    for rel in TRACKED_TOOLS:
        mine = project / rel
        row = {"file": rel, "exists": mine.is_file(), "match": None}
        if template is not None and mine.is_file():
            tp = template / rel
            if tp.is_file():
                row["match"] = _md5(mine) == _md5(tp)
                row["size"], row["template_size"] = mine.stat().st_size, tp.stat().st_size
        rows.append(row)
    return rows


def rules_report(project_path: str | Path, template: Path | None) -> dict:
    p = Path(project_path)
    kind = gitops.repo_kind(p)
    rep = {
        "path": str(p).replace("\\", "/"),
        "git": kind,
        "global_rules": (p / "GLOBAL_RULES.md").is_file(),
        "project_rules": (p / "PROJECT_RULES.md").is_file(),
        "checks_config": (p / "graph" / "checks_config.json").is_file(),
        "ledger": ledger_summary(p / "graph" / "edges.csv"),
        "worklog": worklog_summary(p / "logs"),
        "logs_xlsx": (p / "0.FilesUpdate.xlsx").is_file(),
        "drift": tool_drift(p, template),
        "template": str(template).replace("\\", "/") if template else None,
        "markers": {
            "parked": (p / "_PARKED.txt").is_file(),
            "to_delete": (p / "_to_delete").is_dir(),
            "jira": (p / "jira_project_key_id.txt").is_file(),
            "daily": (p / "daily").is_dir(),
            "evidence_pack": (p / "evidence_pack").is_dir(),
            "claude_skills": (p / ".claude" / "skills").is_dir(),
        },
        "nested_repos": _nested_repos(p),
        "locks": gitops.find_locks(p) if kind in ("repo", "broken") else [],
        "remotes": [],
    }
    if kind == "repo":
        try:
            rep["remotes"] = gitops.remotes(p)
        except gitops.GitError:
            rep["remotes"] = []
    return rep


# ---------------------------------------------------------------- 검사 도구 실행

def run_tool(project_path: str | Path, tool: str, timeout: int = 120) -> dict:
    """프로젝트 자체의 검사기를 실행한다. 프로젝트에 파일이 없으면 실행하지 않는다(다른 버전을 섞지 않는다)."""
    p = Path(project_path)
    if tool == "validate_worklog":
        script = p / "tools" / "worklog" / "validate_worklog.py"
        args = [str(script), "logs/files.jsonl", "logs/prompts.jsonl"]
    elif tool == "graph_checks":
        script = p / "tools" / "graph_checks.py"
        args = [str(script)]
    else:
        return {"ok": False, "error": f"알 수 없는 도구: {tool}"}
    if not script.is_file():
        return {"ok": False, "error": f"{script.relative_to(p)} 없음", "returncode": None}
    try:
        cp = subprocess.run([sys.executable, *args], cwd=str(p), capture_output=True, encoding="utf-8",
                            errors="replace", timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "시간 초과", "returncode": 124}
    return {"ok": cp.returncode == 0, "returncode": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr,
            "command": " ".join(["python", *args])}
