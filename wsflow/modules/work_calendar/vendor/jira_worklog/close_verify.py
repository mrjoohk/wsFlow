#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""마감이 Jira 에 쓴 것을 되읽어 의도와 대조한다.

이 스크립트가 있는 이유는 하나다. **일일 배치에는 사후 검사(INV-1~6)가
있는데 마감에는 없었다.** 마감은 되돌리기 비싼 쪽인데 검사가 없었으니
뒤집혀 있었다.

2026-09-03 첫 실 마감에서 실제로 났다: 생성 호출 하나가 잘못된 형태로
나가 설명 본문이 오염되고 **담당자가 다른 사람으로 지정**됐다. 호출은
성공으로 돌아왔고 어떤 검사도 걸리지 않았다 — 사람이 되읽어 보고서야
발견했다(F-048). 성공 응답은 "요청이 접수됐다"이지 "의도대로 됐다"가
아니다. 그 둘을 구별하는 유일한 방법이 되읽기다.

대조하는 다섯 칸은 **틀렸을 때 조용한 것들**이다. 제목·부모 에픽·CSC·
담당자·스프린트. 이 중 어느 하나가 어긋나면 티켓은 Jira 에 멀쩡히 남고
이 폴더의 다음 조회에서는 사라지거나(부모·CSC), 남의 일이 되거나
(담당자), 보드에서 안 보인다(스프린트). 전부 만든 사람이 가장 늦게
알아채는 형태다.

  close_verify.py --intent .workflow/close_intent.json \
                  --observed .workflow/raw/{stamp}_verify.json \
                  --state .workflow/state.json [--json]

종료 코드
  0  전건 일치
  1  어긋난 칸이 있다 — **사람에게 그대로 보이고 고친 뒤 다시 돌린다.**
  2  파일이 없거나 깨졌다(설정 오류). 되읽기를 제출하지 않은 경우도 여기다 —
     "대조했는데 0건"과 "대조하지 않았다"는 다르다.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jira_fields import dig, issue_list, norm_issue, require_schema  # noqa: E402


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sprint_ids(d):
    """스프린트 필드는 지나간 스프린트까지 배열로 담는다. id 만 모은다."""
    out = []
    for key in list(d.get("fields", {}) or {}):
        if not key.startswith("customfield_"):
            continue
        val = d["fields"][key]
        if isinstance(val, list) and val and isinstance(val[0], dict) \
                and "state" in val[0] and "boardId" in val[0]:
            for s in val:
                if s.get("id") is not None:
                    out.append(int(s["id"]))
    return out


def assignee_id(d):
    return dig(d, "fields.assignee.accountId", "assignee.accountId", tag="assignee")


def verify(intent, observed, csc_value):
    """의도와 관측을 칸별로 대조해 어긋난 것만 돌려준다."""
    by_key = {}
    for raw in issue_list(observed):
        k = dig(raw, "key", "issueKey")
        if k:
            by_key[k] = raw
    bad = []

    def miss(key, field, want, got):
        bad.append({"key": key, "field": field, "expected": want, "actual": got})

    for t in intent.get("tasks") or []:
        k = t["key"]
        raw = by_key.get(k)
        if raw is None:
            miss(k, "존재", "관측에 있음", "없음")
            continue
        n = norm_issue(raw, t.get("csc_field"))
        if n["summary"] != t["summary"]:
            miss(k, "제목", t["summary"], n["summary"])
        if n["parent_key"] != t.get("parent"):
            miss(k, "부모 에픽", t.get("parent"), n["parent_key"])
        want_csc = t.get("csc_value", csc_value)
        if want_csc and want_csc not in (n["csc"] or []):
            miss(k, "CSC", want_csc, n["csc"] or [])
        if t.get("assignee") and assignee_id(raw) != t["assignee"]:
            miss(k, "담당자", t["assignee"], assignee_id(raw))
        want_sp, got_sp = t.get("sprint_id"), sprint_ids(raw)
        if want_sp is not None and int(want_sp) not in got_sp:
            miss(k, "스프린트", want_sp, got_sp or "없음(백로그)")
        if want_sp is None and got_sp:
            miss(k, "스프린트", "없음(백로그)", got_sp)
        # 설명은 길어 통째로 비교하면 지적이 읽히지 않는다. 대신 **오염**만
        # 본다 — 인자 표기가 본문으로 흘러든 것이 F-048 의 실제 형태였다.
        desc = dig(raw, "fields.description", "description", default="") or ""
        if isinstance(desc, str):
            for mark in ("<parameter", "</parameter", "<antml", "</antml"):
                if mark in desc:
                    miss(k, "설명 오염", f"{mark} 없음", "호출 인자 표기가 본문에 들어갔다")
                    break

    for c in intent.get("comments") or []:
        k = c["key"]
        raw = by_key.get(k)
        if raw is None:
            miss(k, "존재", "관측에 있음", "없음")
            continue
        bodies = [x.get("body") for x in
                  (dig(raw, "fields.comment.comments", "comments", default=[]) or [])]
        if c["body"] not in bodies:
            miss(k, "코멘트 본문", "초안과 같은 본문이 달려 있음",
                 "일치하는 코멘트 없음 (잘렸거나 다르게 들어갔다)")

    for tr in intent.get("transitions") or []:
        k = tr["key"]
        raw = by_key.get(k)
        if raw is None:
            miss(k, "존재", "관측에 있음", "없음")
            continue
        got = dig(raw, "fields.status.name", "status.name", default="")
        if got != tr["status"]:
            miss(k, "상태", tr["status"], got)
    return bad


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent", required=True)
    ap.add_argument("--observed", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    for p in (a.intent, a.state):
        if not os.path.exists(p):
            print(f"파일이 없다: {p}", file=sys.stderr)
            return 2
    state = load(a.state)
    require_schema(state, a.state)
    intent = load(a.intent)

    n_intent = sum(len(intent.get(k) or []) for k in ("tasks", "comments", "transitions"))
    if not os.path.exists(a.observed):
        print(f"되읽기를 제출하지 않았다: {a.observed}\n"
              "마감이 쓴 키를 다시 조회해 저장한 뒤 이 검사를 돌리십시오. "
              "성공 응답은 '요청이 접수됐다'이지 '의도대로 됐다'가 아니다 — "
              "그 둘을 구별하는 방법은 되읽기뿐이다.", file=sys.stderr)
        return 2
    observed = load(a.observed)

    bad = verify(intent, observed, state.get("csc_value"))
    if a.json:
        print(json.dumps({"checked": n_intent, "mismatches": bad},
                         ensure_ascii=False, indent=2))
    if bad:
        if not a.json:
            print(f"close_verify: {n_intent}건 중 {len(bad)}칸이 의도와 다르다.",
                  file=sys.stderr)
            for b in bad:
                print(f"  [{b['key']}] {b['field']}\n"
                      f"      의도: {b['expected']}\n"
                      f"      실제: {b['actual']}", file=sys.stderr)
            print("\n고친 뒤 다시 조회해 이 검사를 통과시키십시오.", file=sys.stderr)
        return 1
    if not a.json:
        print(f"close_verify: {n_intent}건 전건 일치 (제목·부모·CSC·담당자·스프린트)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
