"""설정 작성 — `/wf-init` 을 UI 로 옮긴 것(D-005, 사용자 요구 4).

## 재실행해도 안전해야 한다

`wf-init.md` 가 못박은 원칙을 그대로 지킨다 — **기존 `watermark` 와 `roots` 는
절대 지우지 않고 설정 항목만 갱신한다.** 초기화 명령이 데이터를 지우면
"뭔가 이상할 때 다시 돌려본다"는 가장 자연스러운 복구 행동이 사고가 된다.

## 사이트 공통값은 다시 묻지 않는다

`site_url`·`cloud_id`·`csc_field`·`sprint_field`·`issue_type_map` 은 사이트에
하나씩이다. 이미 설정된 폴더가 있으면 거기서 그대로 가져오고, 폴더마다 다른
것(`project_key`·`csc_value`·`epic_keys`·제목 필터)만 묻는다. 같은 값을 두 번
입력받으면 언젠가 어긋나고, 어긋난 쪽이 조용히 낡는다.

## .gitignore 를 먼저 본다

WF_HOME 이 프로젝트 폴더 안에 들어온다는 것은 `.workflow/`·`daily/` 가 그
저장소에 섞인다는 뜻이다. 제외가 없으면 **업무 기록이 커밋된다.** 그래서
설정을 쓰기 전에 검사하고, 빠진 줄을 추가할지 묻는다.

## 조회는 편의일 뿐 필수가 아니다

`discover()` 는 이슈 타입·CSC 후보·에픽을 찾아 주지만, 사이트마다 응답이
다를 수 있으므로 **한 부분이 실패해도 나머지를 돌려준다.** 사람이 값을 직접
넣는 길이 항상 열려 있어야 한다 — 조회가 안 된다고 설정을 못 하면 안 된다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import jiraapi, wfhome

# 구 /rest/api/3/issue/createmeta 는 사용 중단됐다. 프로젝트별로 나눠 부른다.
ISSUETYPES = "/rest/api/3/issue/createmeta/{key}/issuetypes"
ISSUETYPE_FIELDS = "/rest/api/3/issue/createmeta/{key}/issuetypes/{tid}"
PROJECT_SEARCH = "/rest/api/3/project/search"

SITE_KEYS = ("site_url", "cloud_id", "sprint_field", "project_style",
             "subtask_type", "bug_marker", "bug_marker_reason", "task_types", "epic_type")


def site_defaults(state: dict) -> dict:
    """이미 설정된 폴더에서 사이트 공통값만 뽑는다."""
    itm = state.get("issue_type_map") or {}
    out = {"issue_type_map": {k: itm[k] for k in SITE_KEYS if k in itm}}
    if state.get("csc_field"):
        out["csc_field"] = state["csc_field"]
    return out


def donor_states(projects: list) -> list:
    """설정 공여 후보 — 이미 `.workflow/state.json` 이 있고 사이트가 적힌 폴더."""
    out = []
    for pr in projects:
        sp = wfhome.state_path(wfhome.wf_home_for(pr))
        if not sp.is_file():
            continue
        try:
            st = json.loads(sp.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        site = (st.get("issue_type_map") or {}).get("site_url")
        if site:
            out.append({"id": pr.get("id"), "path": pr.get("path"), "site_url": site,
                        "project_key": st.get("project_key"), "defaults": site_defaults(st)})
    return out


# ------------------------------------------------------------------ 조회
def _safe(fn, bucket: dict, name: str):
    try:
        bucket[name] = fn()
    except Exception as e:  # noqa: BLE001 — 한 부분이 실패해도 나머지는 준다
        bucket[name] = None
        bucket.setdefault("errors", {})[name] = f"{type(e).__name__}: {e}"


def discover(creds: dict, base: str, project_key: str | None = None, send=None) -> dict:
    """설정에 필요한 후보들. **한 부분이 실패해도 나머지를 돌려준다.**"""
    kw = {"send": send} if send else {}
    out: dict = {}
    _safe(lambda: jiraapi.request(creds, base, PROJECT_SEARCH, None, **kw).get("values") or [],
          out, "projects")
    if not project_key:
        return out
    _safe(lambda: (jiraapi.request(creds, base, ISSUETYPES.format(key=project_key), None, **kw)
                   .get("issueTypes") or []), out, "issue_types")
    if out.get("issue_types"):
        out["csc_candidates"] = csc_candidates(creds, base, project_key, out["issue_types"], send)
    _safe(lambda: epics(creds, base, project_key, send), out, "epics")
    return out


def csc_candidates(creds: dict, base: str, project_key: str, issue_types: list, send=None) -> list:
    """CSC 를 담을 수 있는 필드 후보.

    `wf-init.md` 가 권하는 순서 그대로다 — 단일 선택 커스텀 필드가 1순위(이슈
    유형에서 필수로 걸 수 있어 F-029 미지정 유실을 생성 경로에서 막는다),
    표준 컴포넌트가 2순위(필수로 만들 수 없다), 레이블은 자유 입력이라 오타
    방어가 없어 권하지 않는다.
    """
    kw = {"send": send} if send else {}
    seen, out = set(), []
    for it in issue_types:
        if it.get("subtask") or (it.get("hierarchyLevel") or 0) != 0:
            continue
        try:
            data = jiraapi.request(creds, base,
                                   ISSUETYPE_FIELDS.format(key=project_key, tid=it.get("id")),
                                   None, **kw)
        except jiraapi.JiraError:
            continue
        for f in data.get("fields") or []:
            fid = f.get("fieldId") or f.get("key")
            if not fid or fid in seen:
                continue
            sch = f.get("schema") or {}
            allowed = f.get("allowedValues") or []
            if fid == "components":
                rank, why = 2, "표준 컴포넌트 — 필수로 만들 수 없어 미지정 유실이 남는다"
            elif fid == "labels":
                rank, why = 3, "레이블 — 자유 입력이라 오타 방어가 없다"
            elif re.fullmatch(r"customfield_\d+", fid) and sch.get("type") in ("option", "array") and allowed:
                rank, why = 1, "단일/다중 선택 커스텀 필드 — 필수로 걸면 미지정 유실을 구조적으로 막는다"
            else:
                continue
            seen.add(fid)
            out.append({"field": fid, "name": f.get("name"), "required": bool(f.get("required")),
                        "rank": rank, "why": why,
                        "values": [v.get("value") or v.get("name") for v in allowed][:50],
                        "jql": _jql_or_none(fid)})
    return sorted(out, key=lambda x: (x["rank"], x["field"]))


def _jql_or_none(fid: str):
    try:
        return jiraapi.csc_jql(fid)
    except jiraapi.JiraError:
        return None


def epics(creds: dict, base: str, project_key: str, send=None) -> list:
    """이 프로젝트의 에픽(CSU). 이름이 아니라 계층 번호로 가린다 — `is_epic` 과 같은 근거."""
    kw = {"send": send} if send else {}
    res = jiraapi.search(creds, base, f"project = {project_key} ORDER BY created DESC",
                         ["summary", "issuetype"], **kw)
    out = []
    for d in res["issues"]:
        iss = wfhome.JF.norm_issue(d)
        if wfhome.AGG.is_epic(iss):
            out.append({"key": iss["key"], "summary": iss["summary"]})
    return out


# ------------------------------------------------------------------ 쓰기
def build_state(existing: dict | None, **cfg) -> dict:
    """설정 항목만 갱신한 새 state. **watermark·roots·last_success 는 보존한다.**"""
    old = dict(existing or {})
    keep = {k: old[k] for k in ("watermark", "last_success", "roots") if k in old}
    itm = dict(old.get("issue_type_map") or {})
    itm.update(cfg.get("issue_type_map") or {})

    state = {
        **old,
        "schema_version": wfhome.SCHEMA_VERSION,
        "watermark": keep.get("watermark") or cfg.get("watermark") or "2000-01-01T00:00:00+09:00",
        "project_key": cfg["project_key"],
        "title_include": list(cfg.get("title_include") or []),
        "title_exclude": list(cfg.get("title_exclude") or []),
        "title_prefix": cfg.get("title_prefix") or None,
        "sprint_mode": cfg.get("sprint_mode") or old.get("sprint_mode") or "active",
        "transition_on_update": cfg.get("transition_on_update") or old.get("transition_on_update") or "in_progress",
        "issue_type_map": itm,
        "roots": keep.get("roots") or {},
        "csc_field": cfg.get("csc_field") or None,
        "csc_value": cfg.get("csc_value") or None,
        "epic_keys": list(cfg.get("epic_keys") or []),
    }
    if "last_success" in keep:
        state["last_success"] = keep["last_success"]
    return state


def preview(wf_home: str | Path, new_state: dict) -> dict:
    """쓰기 전에 무엇이 바뀌는지. 보존 항목은 따로 세어 보여준다."""
    sp = wfhome.state_path(wf_home)
    old = {}
    if sp.is_file():
        try:
            old = json.loads(sp.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            old = {}
    changed = {k: {"before": old.get(k), "after": v} for k, v in new_state.items()
               if k not in ("roots", "watermark", "last_success") and old.get(k) != v}
    return {"exists": bool(old), "changed": changed,
            "preserved": {"watermark": new_state.get("watermark"),
                          "roots": len(new_state.get("roots") or {})}}


def write_state(wf_home: str | Path, new_state: dict) -> str:
    from . import fileio
    sp = fileio.within(wf_home, wfhome.state_path(wf_home))
    with fileio.locked(wf_home):
        value = dict(new_state)
        if sp.is_file():
            current = wfhome.read_state(wf_home)
            value = {**current, **value}
            value.update({k: current[k] for k in ('roots', 'watermark', 'last_success', 'last_batch') if k in current})
        fileio.atomic_write(sp, json.dumps(value, ensure_ascii=False, indent=1).encode('utf-8'))
        return str(sp)


# ------------------------------------------------------------------ .gitignore (D-005)
def gitignore_report(project_path: str | Path) -> dict:
    p = Path(project_path)
    if not (p / ".git").exists():
        return {"applicable": False, "missing": [], "reason": "git 저장소가 아닙니다"}
    gi = p / ".gitignore"
    have = set()
    if gi.is_file():
        have = {ln.strip() for ln in gi.read_text(encoding="utf-8", errors="replace").splitlines()}
    missing = [n for n in wfhome.GITIGNORE_NEEDED if n not in have]
    return {"applicable": True, "missing": missing, "path": str(gi),
            "reason": "" if not missing else "업무 기록이 커밋될 수 있습니다"}


def gitignore_apply(project_path: str | Path) -> dict:
    """빠진 줄만 덧붙인다. 기존 내용은 건드리지 않는다."""
    rep = gitignore_report(project_path)
    if not rep["applicable"] or not rep["missing"]:
        return {**rep, "added": []}
    gi = Path(project_path) / ".gitignore"
    body = gi.read_text(encoding="utf-8", errors="replace") if gi.is_file() else ""
    if body and not body.endswith("\n"):
        body += "\n"
    body += "\n# jira-worklog: 업무 기록은 소스가 아니다.\n" + "\n".join(rep["missing"]) + "\n"
    gi.write_text(body, encoding="utf-8")
    return {**gitignore_report(project_path), "added": rep["missing"]}
