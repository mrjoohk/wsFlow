#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""런타임 불변식 검사 (INV-1~5).

INV-4가 이 파일의 존재 이유다: 서브에이전트가 일지를 쓰면서 이슈 키를
지어내거나 잘못 옮기면 사람 눈에 띄지 않고, 몇 달 뒤 일지를 되짚을 때야
발견되며 그때는 원인을 재구성할 수 없다. 원본 대조가 유일한 방어선이다.

  loop_checks.py --daily daily/260814.md --agg .workflow/agg/260814.json \
                 --state .workflow/state.json [--evidence logs/xxx.json]

종료 코드 0 = 통과, 1 = 위반.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jira_fields import require_schema  # noqa: E402

KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
LOCAL_RE = re.compile(r"\bR-\d{3}(?:\.\d+)?\b")


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


def load(p, d=None):
    return json.load(open(p, encoding="utf-8")) if p and os.path.exists(p) else d


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", required=True)
    ap.add_argument("--agg", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--evidence")
    a = ap.parse_args()

    v = []
    agg = load(a.agg, {}) or {}
    state = load(a.state, {}) or {}
    require_schema(state, a.state)
    workdir = os.path.dirname(a.state) or "."

    # INV-1 — 사람이 쓴 진행 메모가 보존되었는가
    rec = load(os.path.join(workdir, "last_render.json"), {}) or {}
    if rec.get("memo_existed_before") and \
            rec.get("memo_hash_before") != rec.get("memo_hash_after"):
        v.append("[INV-1] 진행 메모가 변경되었다 "
                 f"({rec.get('memo_hash_before')} → {rec.get('memo_hash_after')}). "
                 "일일 배치는 메모 구획을 덮으면 안 된다.")

    # INV-2 — watermark 단조 증가
    hist_path = os.path.join(workdir, "watermark_history.json")
    hist = load(hist_path, []) or []
    wm = state.get("watermark")
    if wm:
        if hist and wm < hist[-1]:
            v.append(f"[INV-2] watermark 후퇴: {hist[-1]} → {wm}. "
                     "후퇴는 중복 수집 또는 구간 유실을 뜻한다.")
        if not hist or hist[-1] != wm:
            hist.append(wm)
            json.dump(hist[-200:], open(hist_path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)

    # INV-3 — 승인 기록 없는 push 없음
    # 파생뿐 아니라 마감이 새로 만든 **메인 태스크**도 대상이다. Jira에서
    # 발견한 root(origin != "local")는 애초에 push한 적이 없으므로 제외한다.
    for lid, root in (state.get("roots") or {}).items():
        if root.get("origin") == "local" and root.get("jira_key")                 and not root.get("approved_at"):
            v.append(f"[INV-3] {lid} 가 {root['jira_key']} 로 새로 생성되었으나 "
                     "승인 기록(approved_at)이 없다.")
        for c in root.get("children") or []:
            if c.get("jira_key") and not c.get("approved_at"):
                v.append(f"[INV-3] {c.get('local_id', lid)} 가 {c['jira_key']} 로 "
                         "push 되었으나 승인 기록(approved_at)이 없다.")

    # INV-4 — 일지가 언급한 이슈 키가 원본 집계에 실재하는가
    # 범위 밖이라 보고만 한 키(제목 필터·미등재 CSU·컴포넌트 미지정)도
    # 원본에 실재한다. INV-4 는 서브에이전트의 환각을 잡는 검사이지 "이
    # 폴더가 맡는가"를 보는 검사가 아니므로 함께 known 에 넣는다.
    known = set(agg.get("all_issue_keys") or []) | set(agg.get("reported_keys") or [])
    for r in (state.get("roots") or {}).values():
        if r.get("jira_key"):
            known.add(r["jira_key"])
        if r.get("epic"):
            known.add(r["epic"])
        for c in r.get("children") or []:
            if c.get("jira_key"):
                known.add(c["jira_key"])
    text = open(a.daily, encoding="utf-8").read() if os.path.exists(a.daily) else ""
    # 진행 메모는 사람이 쓴 원문이므로 검사 대상에서 제외한다.
    text = re.sub(r"<!-- MEMO:START -->.*?<!-- MEMO:END -->", "", text, flags=re.S)
    mentioned = set(KEY_RE.findall(text)) - {"XXX-0"}
    ghosts = sorted(k for k in mentioned if k not in known)
    if ghosts:
        v.append(f"[INV-4] 일지가 원본에 없는 이슈 키를 언급한다: {', '.join(ghosts)}. "
                 "서브에이전트의 환각 또는 오타일 수 있다.")

    # INV-5 — 수집 범위가 이 폴더의 설정과 일치하는가
    # 폴더가 여럿일 때 각자 다른 프로젝트·에픽을 맡는다. JQL에서 에픽 필터를
    # 빠뜨리면 옆 폴더가 맡은 태스크까지 끌어오고, 그 상태로 마감까지 가면
    # 같은 태스크에 하위 티켓이 두 번 생긴다. 되돌리기 비용이 큰 쪽이므로
    # 조용히 넘기지 않는다.
    scope = agg.get("scope")
    if scope is None:
        v.append("[INV-5] 집계 파일에 스코프 기록이 없다. 구버전 aggregate.py로 "
                 "만들어졌을 수 있다 — 다시 집계하십시오.")
    else:
        want = (state.get("project_key"), state.get("csc_field"),
                state.get("csc_value"), list(state.get("epic_keys") or []),
                state.get("title_include") or [], state.get("title_exclude") or [])
        got = (scope.get("project_key"), scope.get("csc_field"),
               scope.get("csc_value"), list(scope.get("epic_keys") or []),
               scope.get("title_include") or [], scope.get("title_exclude") or [])
        if want != got:
            v.append(f"[INV-5] 집계가 현재 설정과 다른 스코프로 만들어졌다: "
                     f"설정 {want} vs 집계 {got}. 설정을 바꾼 뒤 다시 집계하지 "
                     "않았을 수 있다.")
        oos = agg.get("out_of_scope") or {}
        if oos.get("count"):
            keys = ", ".join((oos.get("keys") or [])[:5])
            v.append(f"[INV-5] 스코프 밖 이슈 {oos['count']}건이 수집되었다: {keys}. "
                     "조회 JQL에 CSC 필터가 빠졌을 수 있다 — 이대로 마감하면 "
                     "다른 폴더와 하위 티켓이 중복 생성된다.")

    # INV-6 — CSC 미지정 조회를 함께 제출했는가 (F-029)
    # CSC 는 에픽에서 상속되지 않는다. UI 에서 CSC 없이 만든 태스크는 그
    # 순간부터 이 폴더의 조회에 안 잡히고, 만든 사람이 가장 늦게 알아챈다.
    # 조회를 빠뜨리면 "0건"과 "보지 않았다"가 구별되지 않으므로 문서가 아니라
    # 여기서 막는다 — 문서가 요구하는 게이트는 기계가 든다.
    # (CSC 필드를 필수로 걸어 둔 프로젝트에서는 실무상 0건이지만, 필수는 생성
    #  화면에만 걸리고 API 생성·일괄 편집으로는 우회되므로 조회는 유지한다.)
    if state.get("csc_value"):
        cm = agg.get("csc_missing") or {}
        if not cm.get("checked"):
            v.append("[INV-6] CSC 미지정 조회(<csc_field> IS EMPTY)를 제출하지 "
                     "않았다. aggregate.py 에 --orphans 를 함께 넘기십시오 — "
                     "이 조회가 없으면 '0건'과 '보지 않았다'가 구별되지 않는다.")

    ev = {
        "tool": "loop_checks",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "daily": a.daily,
        "checked": {"INV-1": "memo_preserved", "INV-2": "watermark_monotonic",
                    "INV-3": "push_has_approval", "INV-4": "issue_keys_exist",
                    "INV-5": "collection_within_scope",
                    "INV-6": "csc_orphans_queried"},
        "scope": agg.get("scope"),
        "keys_mentioned": sorted(mentioned),
        "keys_known": len(known),
        "violations": v,
        "exit_code": 1 if v else 0,
    }
    out = a.evidence or os.path.join(workdir, "loop_checks_last.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    json.dump(ev, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    for m in v:
        print("  위반: " + m)
    print(f"loop_checks: 위반 {len(v)}건 · evidence {out}")
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main())
