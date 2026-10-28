"""배포 — 원본 폴더/파일(복수)을 체크한 프로젝트들에 복사한다 (D-011).

정책(사용자 결정, P-006):
  - 항상 덮어쓰기, 백업 없음. 실행 전 `plan()` 이 프로젝트별 신규/변경/동일 목록을 보여준다.
  - 대상 경로: 원본이 그 프로젝트 루트의 템플릿 디렉터리(0.PROJECT_TEMPLATE) 아래면 **같은 상대경로**,
    아니면 `dest_rel`(기본 = 프로젝트 루트) 아래 원본 이름.
  - 원본 폴더 안의 .git / __pycache__ 는 복사하지 않는다. 대상이 프로젝트 밖으로 나가는 경로(`..`)는 거부한다.
"""
from __future__ import annotations

import filecmp
import shutil
from pathlib import Path
from typing import Callable

from wsflow.path_policy import protected_destination

SKIP_DIRS = {".git", "__pycache__"}


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _iter_files(src: Path):
    """원본이 파일이면 (파일, 상대경로=이름), 폴더면 하위 파일 전부 (파일, 폴더 기준 상대경로)."""
    if src.is_file():
        yield src, Path(src.name)
        return
    for p in sorted(src.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(src).parts):
            continue
        if p.is_file():
            yield p, p.relative_to(src)


def resolve_dest_base(src: Path, project: Path, template_dir: Path | None, dest_rel: str) -> Path:
    """원본(파일/폴더)이 프로젝트 안에서 놓일 위치(파일이면 그 파일 경로, 폴더면 그 폴더 경로)."""
    if template_dir is not None and _inside(src, template_dir):
        rel = src.resolve().relative_to(template_dir.resolve())
    else:
        rel = Path(dest_rel.strip().strip("/\\")) / src.name if dest_rel and dest_rel.strip() else Path(src.name)
    dest = project / rel
    if not _inside(dest, project):
        raise ValueError(f"대상이 프로젝트 밖입니다: {rel}")
    return dest


def _entries(sources: list[str], project: dict, template_dir: Path | None, dest_rel: str) -> dict:
    proj = Path(project["path"])
    items, errors = [], []
    for s in sources:
        src = Path(s)
        if not src.exists():
            errors.append(f"원본 없음: {s}")
            continue
        try:
            base = resolve_dest_base(src, proj, template_dir, dest_rel)
        except ValueError as e:
            errors.append(str(e))
            continue
        if src.is_dir() and _inside(proj, src):
            errors.append(f"원본 폴더가 대상 프로젝트를 포함합니다(재귀 복사 거부): {s}")
            continue
        for f, rel in _iter_files(src):
            dest = base if src.is_file() else base / rel
            conflict = protected_destination(dest, proj)
            if conflict:
                errors.append(f"{dest}: {conflict}")
                continue
            if dest.exists():
                status = "same" if filecmp.cmp(f, dest, shallow=False) else "changed"
            else:
                status = "new"
            if src.is_file() and f.resolve() == dest.resolve():
                status = "same"
            items.append({"src": str(f), "dest": str(dest), "rel": str(dest.relative_to(proj)).replace("\\", "/"),
                          "status": status, "size": f.stat().st_size})
    counts = {k: sum(1 for i in items if i["status"] == k) for k in ("new", "changed", "same")}
    return {"id": project["id"], "alias": project.get("alias", project["id"]), "path": str(proj),
            "items": items, "counts": counts, "errors": errors}


def plan(sources: list[str], projects: list[dict], dest_rel: str,
         template_for: Callable[[str], Path | None]) -> dict:
    out = [_entries(sources, p, template_for(p["path"]), dest_rel) for p in projects]
    return {"projects": out,
            "total": {k: sum(p["counts"][k] for p in out) for k in ("new", "changed", "same")}}


def apply(sources: list[str], projects: list[dict], dest_rel: str,
          template_for: Callable[[str], Path | None]) -> dict:
    """plan 과 같은 계산을 다시 하고 new/changed 만 복사한다(항상 덮어쓰기, 백업 없음)."""
    results = []
    for p in projects:
        e = _entries(sources, p, template_for(p["path"]), dest_rel)
        copied, failed = [], []
        for it in ([] if e['errors'] else e["items"]):
            if it["status"] == "same":
                continue
            try:
                conflict = protected_destination(it['dest'], p['path'])
                if conflict:
                    raise ValueError(conflict)
                Path(it["dest"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(it["src"], it["dest"])
                copied.append(it["rel"])
            except (OSError, ValueError) as ex:
                failed.append({"rel": it["rel"], "error": str(ex)})
        results.append({"id": e["id"], "alias": e["alias"], "copied": copied, "skipped_same": e["counts"]["same"],
                        "failed": failed, "errors": e["errors"]})
    return {"results": results, "copied_total": sum(len(r["copied"]) for r in results)}
