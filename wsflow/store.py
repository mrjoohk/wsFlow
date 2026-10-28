"""관리 목록 저장소 — projects.json 하나가 wsFlow 가 소유하는 유일한 상태 파일이다.

{
  "roots":    [{"path": "K:/01.workspace/AI_workspace", "template": "0.PROJECT_TEMPLATE"}],
  "projects": [{"id": "hfsim", "path": "K:/.../hfSim", "alias": "hfSim", "tags": []}]
}
경로는 저장 시 정규화(절대경로, 슬래시 통일)한다. 폴더를 만들거나 지우지 않는다 — 목록만 관리한다.
"""
from __future__ import annotations

import json
import copy
import os
import re
import threading
import time
from pathlib import Path

DEFAULT_TEMPLATE_DIR = "0.PROJECT_TEMPLATE"


def norm(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve()).replace("\\", "/")


def make_id(path: str, existing: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(path).name).strip("-").lower() or "project"
    cand, i = base, 2
    while cand in existing:
        cand, i = f"{base}-{i}", i + 1
    return cand


class Store:
    def __init__(self, file: str | Path):
        self.file = Path(file)
        self._lock = threading.Lock()
        self.data = {"roots": [], "projects": []}
        if self.file.exists():
            self.data = json.loads(self.file.read_text(encoding="utf-8"))
            self.data.setdefault("roots", [])
            self.data.setdefault("projects", [])

    def _save(self) -> None:
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(6):
            try:
                tmp.replace(self.file)
                return
            except PermissionError as error:
                # A Windows reader or scanner may briefly deny delete-sharing.
                # Keep the same replacement bytes; never mask a permanent error.
                if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(.01 * 2 ** attempt)

    def snapshot(self) -> dict:
        """Consistent in-memory registry view for read models; never saves/migrates."""
        with self._lock:
            return copy.deepcopy(self.data)

    # ---- roots
    def roots(self) -> list[dict]:
        return list(self.data["roots"])

    def add_root(self, path: str, template: str | None = None) -> dict:
        p = norm(path)
        if not Path(p).is_dir():
            raise ValueError(f"디렉터리가 아닙니다: {path}")
        with self._lock:
            for r in self.data["roots"]:
                if r["path"] == p:
                    return r
            r = {"path": p, "template": template or DEFAULT_TEMPLATE_DIR}
            self.data["roots"].append(r)
            self._save()
            return r

    def remove_root(self, path: str) -> bool:
        p = norm(path)
        with self._lock:
            before = len(self.data["roots"])
            self.data["roots"] = [r for r in self.data["roots"] if r["path"] != p]
            changed = len(self.data["roots"]) != before
            if changed:
                self._save()
            return changed

    def template_dir_for(self, project_path: str) -> Path | None:
        """프로젝트가 속한 루트의 템플릿 디렉터리(없으면 None)."""
        p = norm(project_path)
        best = None
        for r in self.data["roots"]:
            if p.startswith(r["path"].rstrip("/") + "/") and (best is None or len(r["path"]) > len(best["path"])):
                best = r
        if not best:
            return None
        t = Path(best["path"]) / best.get("template", DEFAULT_TEMPLATE_DIR)
        return t if t.is_dir() else None

    # ---- projects
    def projects(self) -> list[dict]:
        return list(self.data["projects"])

    def get(self, pid: str) -> dict | None:
        return next((p for p in self.data["projects"] if p["id"] == pid), None)

    def add_project(self, path: str, alias: str | None = None, tags: list[str] | None = None) -> dict:
        p = norm(path)
        if not Path(p).is_dir():
            raise ValueError(f"디렉터리가 아닙니다: {path}")
        with self._lock:
            for pr in self.data["projects"]:
                if pr["path"] == p:
                    raise ValueError(f"이미 등록됨: {pr['id']}")
            pid = make_id(p, {pr["id"] for pr in self.data["projects"]})
            pr = {"id": pid, "path": p, "alias": alias or Path(p).name, "tags": tags or []}
            self.data["projects"].append(pr)
            self._save()
            return pr

    def create_project(self, root: str, name: str) -> dict:
        """루트 아래에 빈 폴더를 만들고 등록한다 (D-010). 템플릿 복사·git init 은 하지 않는다 — 템플릿은 배포로."""
        name = (name or "").strip()
        if not name or name in (".", "..") or any(c in name for c in '/\\:*?"<>|'):
            raise ValueError(f"폴더 이름이 올바르지 않습니다: {name!r}")
        r = norm(root)
        if not any(x["path"] == r for x in self.data["roots"]):
            raise ValueError(f"등록된 루트가 아닙니다: {root}")
        target = Path(r) / name
        if target.exists():
            raise ValueError(f"이미 존재합니다: {target}")
        target.mkdir()
        return self.add_project(target)

    def update_project(self, pid: str, alias: str | None = None, tags: list[str] | None = None) -> dict:
        with self._lock:
            pr = self.get(pid)
            if not pr:
                raise KeyError(pid)
            if alias is not None:
                pr["alias"] = alias
            if tags is not None:
                pr["tags"] = tags
            self._save()
            return pr

    def remove_project(self, pid: str) -> bool:
        with self._lock:
            before = len(self.data["projects"])
            self.data["projects"] = [p for p in self.data["projects"] if p["id"] != pid]
            changed = len(self.data["projects"]) != before
            if changed:
                self._save()
            return changed
