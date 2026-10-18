"""신규 태스크·파생 생성 — 사용자 요구 2 (D-018 에서 미뤄 둔 몫).

`worklog-close/SKILL.md` 의 계층 규칙과 절대 규칙을 그대로 따른다.

    과제(Epic) → 메인 태스크(root, 로컬 R-nnn) → 파생(Sub-task)

만들기 **전에** 세 가지를 검사한다. 셋 다 "만든 사람이 가장 늦게 알아채는
유실"을 막는 검사다.

1. **제목이 이 폴더 범위에 들어오는가** — `vendor/new_title.py` 를 그대로
   돌린다(하위 프로세스). 같은 판정을 여기서 다시 구현하면 그 순간 사본이
   둘이 되고, 어긋나면 티켓은 Jira 에 남는데 이 폴더 조회에서는 사라진다.
2. **CSC 를 지정했는가** — `csc_value` 가 있는 폴더인데 빠뜨리면 티켓은
   만들어지고 다음 조회에서는 사라진다(F-029). CSC 는 에픽에서 상속되지 않는다.
3. **어느 CSU 밑인가** — 후보가 1개면 그것, 여럿이면 **묻는다**(임의로 고르지
   않는다), 비어 있으면 부모 없이 만들되 **"과제 미지정"** 으로 표시한다.

만든 뒤 완료로 옮기지 않는다. 이미 끝난 일이어도 티켓만 만들고 완료 처리는
사람이 한다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from . import fileio, jirawrite, operations, wfhome

NEW_TITLE = Path(__file__).resolve().parent.parent / "vendor" / "jira_worklog" / "new_title.py"


class NewTaskError(Exception):
    pass


def confirm_title(wf_home: str | Path, summary: str, kind: str = "task",
                  epic_name: str | None = None, epic_summaries: list | None = None) -> str:
    """vendor 의 판정을 그대로 받는다. 통과하면 **확정된 제목**(순번·접두사 포함)."""
    cmd = [sys.executable, str(NEW_TITLE),
           "--state", str(wfhome.state_path(wf_home)),
           "--summary", summary, "--kind", "subtask" if kind == "subtask" else "task"]
    tmp = None
    if epic_summaries is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(list(epic_summaries), tmp, ensure_ascii=False)
        tmp.close()
        cmd += ["--epic-summaries", tmp.name]
    if epic_name:
        cmd += ["--epic-name", epic_name]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=30)
    finally:
        if tmp:
            Path(tmp.name).unlink(missing_ok=True)
    if r.returncode != 0:
        raise NewTaskError((r.stderr or r.stdout or "제목 검사에 실패했습니다").strip())
    title = (r.stdout or "").strip()
    if not title:
        raise NewTaskError("제목 검사가 빈 결과를 냈습니다")
    return title


def csc_payload(state: dict) -> dict:
    """이 폴더의 CSC 를 생성 필드 모양으로. 형태는 필드 종류마다 다르다."""
    field, value = state.get("csc_field"), state.get("csc_value")
    if not (field and value):
        return {}
    if field == "components":
        return {"components": [{"name": value}]}
    if field == "labels":
        return {"labels": [value]}
    return {field: {"value": value}}


def choose_epic(state: dict, epic_key: str | None) -> tuple[str | None, str]:
    """(부모 에픽, 경고). 여럿인데 안 골랐으면 만들지 않는다."""
    keys = list(state.get("epic_keys") or [])
    if epic_key:
        if keys and epic_key not in keys:
            raise NewTaskError(f"이 폴더가 맡은 CSU 가 아닙니다: {epic_key} (맡은 것: {keys})")
        return epic_key, ""
    if len(keys) == 1:
        return keys[0], ""
    if not keys:
        return None, "과제 미지정 — 이 폴더에 epic_keys 가 없어 부모 없이 만듭니다. 과제별 구획에 잡히지 않습니다."
    raise NewTaskError(f"어느 CSU 밑인지 고르십시오 (후보 {len(keys)}개: {keys})")


def plan(wf_home: str | Path, state: dict, *, summary: str, kind: str = "task",
         epic_key: str | None = None, epic_name: str | None = None,
         parent_key: str | None = None, epic_summaries: list | None = None) -> dict:
    """만들 내용을 확정한다. **Jira 를 건드리지 않는다.**"""
    if kind not in ('task', 'subtask', 'bug') or not isinstance(summary, str) or not summary.strip():
        raise NewTaskError('유효한 업무 종류와 비어 있지 않은 제목이 필요합니다')
    itm = state.get("issue_type_map") or {}
    warnings = []

    if kind == "subtask":
        if not parent_key:
            raise NewTaskError("파생은 부모 메인 태스크가 필요합니다")
        title = confirm_title(wf_home, summary, "subtask")
        fields = {"project": {"key": state["project_key"]},
                  "summary": title,
                  "issuetype": {"name": itm.get("subtask_type") or "Subtask"},
                  "parent": {"key": parent_key}}
        # 파생은 스코프 판정에서 면제되므로 CSC 를 넣지 않는다(하위 작업에 그 필드를 두지 않는 구성).
        return {"kind": "create_issue", "target": state["project_key"],
                "payload": {"fields": fields}, "title": title, "warnings": warnings,
                "origin": "local", "parent_key": parent_key, "task_kind": "subtask"}

    if kind == "bug":
        marker = itm.get("bug_marker") or "label:bug"
        if not parent_key:
            raise NewTaskError("문제(BUG)는 메인 태스크 아래 파생으로 만듭니다 — 부모가 필요합니다")
        title = confirm_title(wf_home, summary, "subtask")
        if marker.startswith("type:"):
            itype = marker.split(":", 1)[1]
            fields = {"project": {"key": state["project_key"]}, "summary": title,
                      "issuetype": {"name": itype}, "parent": {"key": parent_key}}
        else:
            fields = {"project": {"key": state["project_key"]},
                      "summary": title if title.startswith("[BUG]") else f"[BUG] {title}",
                      "issuetype": {"name": itm.get("subtask_type") or "Subtask"},
                      "parent": {"key": parent_key}, "labels": ["bug"]}
        return {"kind": "create_issue", "target": state["project_key"],
                "payload": {"fields": fields}, "title": fields["summary"],
                "warnings": warnings, "origin": "local", "parent_key": parent_key,
                "task_kind": "bug"}

    # --- 메인 태스크
    parent, warn = choose_epic(state, epic_key)
    if warn:
        warnings.append(warn)
    title = confirm_title(wf_home, summary, "task", epic_name, epic_summaries)
    fields = {"project": {"key": state["project_key"]}, "summary": title,
              "issuetype": {"name": (itm.get("task_types") or ["Task"])[0]}}
    if parent:
        fields["parent"] = {"key": parent}
    csc = csc_payload(state)
    if state.get("csc_value") and not csc:
        raise NewTaskError("이 폴더는 CSC 를 쓰는데 생성 필드를 만들지 못했습니다 — csc_field 를 확인하십시오")
    if csc:
        fields.update(csc)
    elif state.get("csc_value"):
        warnings.append("CSC 가 지정되지 않았습니다 — 다음 조회에서 사라집니다(F-029)")
    return {"kind": "create_issue", "target": state["project_key"],
            "payload": {"fields": fields}, "title": title, "warnings": warnings,
            "origin": "local", "epic_key": parent, "task_kind": "task"}


def create(wf_home: str | Path, creds: dict, base: str, intent: dict, send=None,
           *, operation_id: str | None = None) -> dict:
    """승인 게이트를 지나 만들고, `state.json` 에 기록한다.

    `origin: "local"` 을 남기는 이유는 INV-3 이 "승인 없이 만들어진 티켓"을
    가려낼 수 있어야 하기 때문이다 — Jira 에서 발견한 root 와 구별되어야 한다.
    """
    with fileio.locked(wf_home):
        if wfhome.read_state(wf_home).get('schema_version') != wfhome.SCHEMA_VERSION:
            raise NewTaskError('지원하지 않는 업무 state schema입니다 — 설정 복구가 필요합니다')
        if operation_id is None:
            approval = jirawrite.find_approval(wf_home, intent['kind'], intent['target'], intent['payload'])
            if not approval:
                raise jirawrite.ApprovalRequired('생성 승인 기록이 없습니다')
            operation_id = approval['id']
        kw = {"send": send} if send else {}
        res = jirawrite.execute(wf_home, creds, base, intent["kind"], intent["target"],
                                intent["payload"], operation_id=operation_id, **kw)
        key = res.get("key")
        if not key:
            raise jirawrite.UncertainWrite('생성된 원격 key를 확인해야 합니다', operation_id)
        store = operations.Store(wf_home)
        approval = store.get(operation_id)
        if not approval['local_recorded']:
            try:
                record(wf_home, intent, key, approved_at=approval['approved_at'], operation_id=operation_id)
                store.local_recorded(operation_id)
            except Exception as exc:
                raise jirawrite.UncertainWrite('원격 생성은 성공했고 로컬 기록 복구가 필요합니다', operation_id, 'succeeded') from exc
        return {"key": key, "result": res, 'operation_id': operation_id}


def record(wf_home: str | Path, intent: dict, key: str, *, approved_at: str, operation_id: str) -> dict:
    """생성 결과를 state.json 에 남긴다 — 메인이면 새 root, 파생이면 부모의 children."""
    if not approved_at or not operation_id:
        raise ValueError('생성 결과를 승인 receipt 없이 기록하지 않습니다')
    with fileio.locked(wf_home):
        sp = fileio.within(wf_home, wfhome.state_path(wf_home))
        state = json.loads(sp.read_text(encoding="utf-8-sig"))
        roots = state.setdefault("roots", {})
        existing = list(roots.values()) + [c for r in roots.values() for c in r.get('children', [])] + state.get('orphan_children', [])
        if any(r.get('jira_key') == key for r in existing):
            return state
        receipt = {'approved_at': approved_at, 'approval_id': operation_id,
                   'operation_id': operation_id, 'origin': 'local'}
        if intent.get("task_kind") in ("subtask", "bug"):
            for rid, r in roots.items():
                if r.get("jira_key") == intent.get("parent_key"):
                    r.setdefault("children", []).append(
                        {"local_id": f"{rid}.{len(r.get('children') or []) + 1}",
                         "jira_key": key, "kind": intent["task_kind"],
                         "summary": intent["title"], **receipt})
                    break
            else:
                state.setdefault("orphan_children", []).append(
                    {"jira_key": key, "parent": intent.get("parent_key"), "summary": intent["title"], **receipt})
        else:
            rid = wfhome.AGG.next_root_id(roots)
            roots[rid] = {"jira_key": key, "epic": intent.get("epic_key"),
                          "children": [], "summary": intent["title"],
                          'first_seen': approved_at[:10], 'last_seen': approved_at[:10], **receipt}
        fileio.atomic_write(sp, json.dumps(state, ensure_ascii=False, indent=1).encode('utf-8'))
        return state
