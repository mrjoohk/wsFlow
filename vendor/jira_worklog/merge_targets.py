#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""병합 후보 목록을 만든다 — 새 티켓을 만드는 대신 기존 태스크에 붙일 때 고를 대상.

마감 3단계에서 사람이 "이건 새 티켓이 아니라 저 태스크에 붙일 일이다"라고
판단할 수 있어야 한다. 그러려면 고를 목록이 있어야 하는데, 오늘 집계 파일에는
**이 폴더 범위의 내 미완료 root**만 들어 있다. 에픽 안의 완료된 태스크나
다른 폴더가 맡은 태스크는 거기 없으므로, 마감이 따로 조회해 이 스크립트로
추려낸다.

목록을 눈대중으로 만들지 않는 이유는 집계와 같다 — 범위 안팎과 완료 여부를
사람이 매번 다시 판단하면 틀린다. 여기서 한 번 표시해 두면 3단계에서는
고르기만 하면 된다.

  merge_targets.py --state .workflow/state.json --issues .workflow/raw/xxx_epic.json
  merge_targets.py --state ... --issues ... --json

기본 출력은 사람이 볼 표, `--json`은 세션이 그대로 쓸 구조다.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aggregate import (compile_patterns, epic_match, in_scope,  # noqa: E402
                       scope_layers, title_match)  # noqa: E402
from jira_fields import issue_list, norm_issue, require_schema  # noqa: E402


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--issues", required=True, help="에픽 조회 MCP 응답 원본")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    with open(a.state, encoding="utf-8") as f:
        state = json.load(f)
    require_schema(state, a.state)
    with open(a.issues, encoding="utf-8") as f:
        raw = json.load(f)

    project_key = state.get("project_key")
    csc_field = state.get("csc_field")
    csc_value = state.get("csc_value")
    epic_keys = list(state.get("epic_keys") or [])
    outer_epics, inner_epics = scope_layers(csc_value, epic_keys)
    include = compile_patterns(state.get("title_include") or [], "title_include")
    exclude = compile_patterns(state.get("title_exclude") or [], "title_exclude")

    rows = []
    for iss in (norm_issue(i, csc_field) for i in issue_list(raw)):
        if not iss["key"]:
            continue
        # 파생은 병합 대상이 아니다. 진행 코멘트는 메인 태스크에 남긴다
        # (계층 규칙). 파생에 붙이면 그 기록은 일지의 어느 구획에도 안 뜬다.
        if iss["is_subtask"]:
            continue
        scoped = in_scope(iss, project_key, csc_value, outer_epics) and \
            epic_match(iss, inner_epics) and title_match(iss, include, exclude)
        rows.append({
            "key": iss["key"],
            "summary": iss["summary"],
            "status": iss["status"],
            "done": iss["status_category"] == "done",
            "in_scope": scoped,
            "updated": iss["updated"],
        })

    # 범위 안을 먼저, 그 안에서 미완료를 먼저, 그다음 최근 갱신 순.
    # 갱신 역순은 문자열이라 뒤집을 수 없으므로 먼저 정렬한 뒤,
    # 안정 정렬로 그룹을 잡는다.
    rows.sort(key=lambda r: r["updated"] or "", reverse=True)
    rows.sort(key=lambda r: (not r["in_scope"], r["done"]))
    for i, r in enumerate(rows, 1):
        r["index"] = i

    if a.json:
        json.dump({"epic_keys": epic_keys, "csc_value": csc_value,
                   "count": len(rows), "targets": rows},
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    if not rows:
        print("병합 후보가 없다. 에픽 조회 결과가 비었거나 전부 파생이다.")
        return 0

    print(f"병합 후보 {len(rows)}건 "
          f"(CSC {csc_value or '(미지정)'} / "
          f"에픽 {', '.join(epic_keys) if epic_keys else '(전체)'})")
    print(f"  {'#':>3}  {'키':<12} {'범위':<6} {'상태':<10} 제목")
    for r in rows:
        mark = "안" if r["in_scope"] else "밖"
        print(f"  {r['index']:>3}  {r['key']:<12} {mark:<6} "
              f"{(r['status'] or '')[:10]:<10} {r['summary']}")
    out_cnt = sum(1 for r in rows if not r["in_scope"])
    if out_cnt:
        print(f"\n  '밖' {out_cnt}건은 이 폴더가 맡지 않는 태스크다. 고를 수는 있으나, "
              "코멘트만 남고 이 폴더의 일지에는 나타나지 않는다.")
    done_cnt = sum(1 for r in rows if r["done"])
    if done_cnt:
        print(f"  완료된 태스크 {done_cnt}건이 포함되어 있다. 붙일 수는 있으나 "
              "다음 일일 배치의 미완료 조회에는 잡히지 않는다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
