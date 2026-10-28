#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""일일 파일을 갱신하되 **사람이 쓴 진행 메모는 절대 건드리지 않는다**.

이 스크립트의 존재 이유가 그 한 줄이다. 상단 두 섹션(변동/오늘 할 일)만
새로 쓰고, 메모 마커 사이의 내용은 원문 그대로 옮긴다.
메모 해시를 실행 전후로 기록해 loop_checks.py의 INV-1이 검증한다.

  render_daily.py --agg agg.json --narrative narrative.md \
                  --out daily/260814.md --state .workflow/state.json
"""
import argparse
import hashlib
import json
import os
import re
import sys

M_START = "<!-- MEMO:START -->"
M_END = "<!-- MEMO:END -->"
MEMO_PLACEHOLDER = (
    "> 오늘 작업하면서 여기에 메모를 남기십시오. 마감 명령이 이 내용을 읽어\n"
    "> Jira 코멘트·하위 티켓 초안을 만듭니다. 이 구획은 자동으로 덮이지 않습니다.\n"
    "> Jira에 티켓이 없는 일은 `- [신규] 내용` , 파생 항목은 `- [파생] 내용` ,\n"
    "> 문제는 `- [BUG] 내용` 으로 적으면 인식됩니다.\n"
    "> 따로 정리해 둔 문서가 있으면 `- [출처] 경로` 로 적어 주십시오 — 출처는\n"
    "> 이 일지에만 남고 Jira 로는 나가지 않습니다. **경로만 적으면 마감이\n"
    "> 멈춥니다.** 무슨 일이 있었는지는 문장으로 함께 적혀야 합니다.\n"
)

CLASS_LABEL = {"new": "신규", "continuing": "이어서", "stalled": "정체"}


def _utf8_stdout():
    """Windows 콘솔 기본 코드페이지(cp949 등)에서 위반 메시지가 깨지지 않도록.

    메시지에 '→'나 '—'가 섞이면 인코딩 오류로 print 자체가 죽는다. 그러면
    사람이 봐야 할 위반 내용이 화면에 아예 안 나오고, 종료 코드만 1로 남아
    "검사에 걸렸다"와 "검사가 죽었다"를 구별할 수 없게 된다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def extract_memo(path):
    if not os.path.exists(path):
        return None
    text = open(path, encoding="utf-8").read()
    m = re.search(re.escape(M_START) + r"(.*?)" + re.escape(M_END), text, re.S)
    return m.group(1) if m else None


def h(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def render_todo(agg):
    lines = []
    today = set(agg.get("today_local_ids") or [])
    for ep in agg.get("epics", []):
        lines.append(f"\n### [CSU] {ep['epic_name']}")
        for r in ep["roots"]:
            mark = "**오늘**" if r["local_id"] in today else "대기"
            due = f" · 마감 {r['duedate']}" if r.get("duedate") else ""
            klass = CLASS_LABEL.get(r["class"], r["class"])
            extra = f" · {r['age_days']}일째" if r["class"] != "new" else ""
            lines.append(f"- [{mark}] {r['local_id']} ({r['key']}) {r['summary']}"
                         f" — {klass}{extra} · {r['status']}{due}")
            for c in r.get("children", []):
                state = c["jira_key"] if c.get("jira_key") else "**미등록**"
                kind = c.get("kind", "task")
                lines.append(f"  - {c['local_id']} [{kind}] {c.get('summary','')} — {state}")
    return "\n".join(lines) if lines else "\n(할당된 미완료 태스크가 없습니다.)"


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--agg", required=True)
    ap.add_argument("--narrative", required=True,
                    help="서브에이전트가 쓴 '지난 실행 이후 변동' 본문")
    ap.add_argument("--out", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--version", default="0.9.0")
    a = ap.parse_args()

    agg = json.load(open(a.agg, encoding="utf-8"))
    narrative = open(a.narrative, encoding="utf-8").read().strip()

    memo_before = extract_memo(a.out)
    memo = memo_before if memo_before is not None else "\n" + MEMO_PLACEHOLDER

    c = agg["counts"]
    stall_note = ""
    if c.get("stalled"):
        stall_note = (f"\n> ⚠ 정체 {c['stalled']}건 — {agg['stall_days']}일 이상 "
                      f"목록에 있으나 Jira 변동이 없습니다.\n")
    pending_note = ""
    if c.get("children_pending"):
        pending_note = (f"> ⚠ 미등록 파생 {c['children_pending']}건 — "
                        f"마감 명령으로 Jira에 올리십시오.\n")
    # 스코프가 새는 두 경로(F-029·F-030)는 콘솔에만 남기면 다음 날 사라진다.
    # 일지에 적어 두어야 사람이 나중에라도 대조할 수 있다.
    scope_note = ""
    cm = agg.get("csc_missing") or {}
    if cm.get("count"):
        scope_note += (f"> ⚠ CSC 미지정 {cm['count']}건 — "
                       f"{', '.join((cm.get('keys') or [])[:5])}. "
                       "이대로 두면 이 폴더의 조회에 잡히지 않습니다.\n")
    eu = agg.get("epic_unlisted") or {}
    if eu.get("count"):
        names = ", ".join(f"{e['key']} {e['name']}".strip()
                          for e in (eu.get("epics") or [])[:3])
        scope_note += (f"> ⚠ 미등재 CSU {eu['count']}건 — {names}. "
                       "이 폴더가 맡을 것인지 확인하십시오.\n")

    body = f"""# {agg['date']} 업무 기록

## 지난 실행 이후 변동
{stall_note}{pending_note}{scope_note}
{narrative}

## 오늘 할 일
{render_todo(agg)}

## 진행 메모
{M_START}{memo}{M_END}

---
<sub>jira-worklog v{a.version} · 생성 {agg['date']} · 원본 대조용 집계: {os.path.basename(a.agg)}</sub>
"""
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w", encoding="utf-8").write(body)

    memo_after = extract_memo(a.out)
    rec = {"file": a.out, "memo_hash_before": h(memo_before),
           "memo_hash_after": h(memo_after),
           "memo_existed_before": memo_before is not None}
    d = os.path.dirname(a.state) or "."
    os.makedirs(d, exist_ok=True)
    json.dump(rec, open(os.path.join(d, "last_render.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print(f"render: {a.out} (메모 {'보존' if memo_before is not None else '신규 생성'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
