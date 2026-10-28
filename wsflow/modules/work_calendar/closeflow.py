"""마감 — 진행 메모 → Jira 코멘트. **쓰기는 전부 `jirawrite.execute` 를 지난다.**

`worklog-close/SKILL.md` 의 절차를 UI 가 할 수 있는 형태로 옮겼다. 바뀐 것은
문장화 주체 하나다 — jiraWF 는 집계 파일을 서브에이전트가 문장으로 바꾸지만,
여기서는 **사람이 UI 에서 태스크별로 직접 쓴 메모가 그대로 코멘트 본문**이다.
서브에이전트의 존재 이유는 맥락 오염 차단인데, 사람이 직접 쓴 문장에는 그 위험이
없다. 대신 `draft_lint` 는 유지한다 — 로컬 경로·URL 이 코멘트로 새어 나가는 것은
누가 썼든 막아야 한다.

`plan()` 은 아무것도 쓰지 않는다. 사람이 승인할 표를 만들 뿐이고, 실제 쓰기는
승인 뒤 `apply()` 가 한다.
"""
from __future__ import annotations

from pathlib import Path

from . import jirawrite, wfhome

VENDOR = Path(__file__).resolve().parent / "vendor" / "jira_worklog"
from .vendor_loader import load as _load_vendor

LINT = _load_vendor('draft_lint')

OUTBOUND_MARKS = ("[신규]", "[파생]", "[BUG]")   # [출처] 는 로컬에만 남는다


def plan(wf_home: str | Path, ymd: str, known_keys) -> dict:
    """메모를 승인 표로 바꾼다. **Jira 를 건드리지 않는다.**

    `known_keys` 는 방금 조회한 원본에 실재하는 키다. 여기에 없는 키를 그대로
    올리면 엉뚱한 이슈에 기록이 쌓이거나 실패한다 — INV-4 가 사후에 잡는 것을
    앞당겨 **올리기 전에** 막는다.
    """
    memo = wfhome.read_memo(wf_home, ymd)
    parsed = memo["parsed"]
    known = set(known_keys or ())

    items, unknown = [], []
    for key in sorted(parsed["by_key"]):
        text = "\n".join(t for t in parsed["by_key"][key] if t.strip())
        if not text.strip():
            continue
        row = {"kind": "comment", "target": key, "payload": {"text": text},
               "lint": LINT.lint(text, key)}
        if key in known:
            items.append(row)
        else:
            unknown.append(row)

    deferred = [m for m in parsed["marks"] if m["mark"] in OUTBOUND_MARKS]
    return {
        "ymd": ymd, "memo_sha": memo["sha"], "items": items,
        "unknown_keys": unknown,
        "deferred": deferred,          # 신규·파생·BUG — 티켓 생성은 S6 소관
        "common": parsed["common"],    # 키 없이 쓴 줄 — 로컬에만 남는다
        "pending": jirawrite.unpushed(wf_home),
    }


def approve(wf_home: str | Path, items: list) -> list:
    """사람이 고른 항목만 승인 기록으로 남긴다. 여기서도 Jira 는 건드리지 않는다."""
    return [jirawrite.approve(wf_home, it["kind"], it["target"], it["payload"]) for it in items]


def apply(wf_home: str | Path, creds: dict, base: str, items: list, send=None) -> dict:
    """승인된 것만 올린다. 한 건이 실패해도 나머지를 계속하되 결과를 전부 돌려준다.

    중간에 끊겼을 때 무엇이 올라갔는지 알 수 있어야 하므로, 성공·실패를 모으고
    예외로 전체를 날리지 않는다. 기록 자체는 `approvals.jsonl` 에 남는다.
    """
    kw = {"send": send} if send else {}
    done, failed = [], []
    for it in items:
        try:
            res = jirawrite.execute(wf_home, creds, base, it["kind"], it["target"],
                                    it["payload"], **kw)
            done.append({"target": it["target"], "result": res.get("id") or res.get("key")})
        except jirawrite.JiraError as e:
            failed.append({"target": it["target"], "error": str(e)})
    return {"done": done, "failed": failed}
