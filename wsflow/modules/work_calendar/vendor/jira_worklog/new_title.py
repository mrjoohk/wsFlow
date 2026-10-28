#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""새로 만들 티켓의 제목을 확정하고, 그 제목이 이 폴더의 범위에 들어오는지 검사한다.

이 스크립트가 있는 이유는 하나다. 제목 필터로 폴더를 나눠 쓰는 경우,
접두사 없는 제목으로 티켓을 만들면 **만든 그 순간부터 이 폴더의 조회에
안 잡힌다.** 티켓은 Jira에 남아 있는데 일지에서는 사라지고, `roots`에는
다시는 갱신되지 않는 항목이 남는다. 사람이 알아채기 가장 어려운 형태의
유실이므로, 만들기 전에 멈춘다.

파생(Sub-task)은 제목 규약을 따르지 않는다 — 부모 아래에 붙으므로
필터 대상이 아니다. `--kind subtask`는 접두사를 붙이지 않고 통과시킨다.

번호도 여기서 붙인다. `--NNN--` 순번의 채번 주체가 정해져 있지 않으면
마감(담당자)과 /wbs-import(PM)가 같은 에픽에 각자 번호를 매겨 겹친다(F-031).
`--epic-summaries` 로 그 에픽의 기존 제목을 넘기면 최댓값+1을 붙인다.
빈 번호는 메우지 않는다 — 지워진 번호를 재사용하면 몇 달 뒤의 인용이
다른 태스크를 가리킨다(D-007 의 불변 ID).

`--epic-name` 을 넘기면 그 에픽이 이 폴더의 CSC 인지도 본다. CSC 약자는
CSC 필드 값 안에 있고(D-020) 에픽 이름은 거기서 파생되므로, 접두사가
어긋난 에픽 아래에 태스크를 만드는 것은 옆 CSC 에 티켓을 흘리는 것과 같다.

  new_title.py --state .workflow/state.json --summary "케이블 재고 확인"
  new_title.py --state .workflow/state.json --summary "..." --kind subtask
  new_title.py --state ... --summary "..." --epic-summaries epic_titles.json \
               --epic-name "[AAA] 프로토콜 연동"

통과하면 확정된 제목을 stdout에 한 줄로 출력하고 0으로 끝난다.
이 폴더가 맡지 못할 제목이면 2로 끝난다(설정 오류와 같은 코드다 —
어느 쪽이든 사람이 고치기 전에는 진행하면 안 된다).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aggregate import compile_patterns, title_match  # noqa: E402
from jira_fields import (require_schema, task_seq, next_task_seq,  # noqa: E402
                         epic_prefix)


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
    ap.add_argument("--summary", required=True)
    ap.add_argument("--kind", default="task", choices=["task", "subtask"])
    ap.add_argument("--epic-summaries",
                    help="대상 에픽의 기존 태스크 제목 목록(JSON 배열 또는 줄바꿈 텍스트). "
                         "넘기면 --NNN-- 순번을 여기서 채번한다.")
    ap.add_argument("--epic-name", help="대상 에픽 이름 — CSC 약자 접두사를 대조한다.")
    a = ap.parse_args()

    with open(a.state, encoding="utf-8") as f:
        state = json.load(f)
    require_schema(state, a.state)

    summary = a.summary.strip()
    if not summary:
        print("제목이 비어 있다.", file=sys.stderr)
        return 2

    if a.kind == "subtask":
        print(summary)
        return 0

    # --- CSC 약자 대조 (D-020) ---------------------------------------
    want = epic_prefix(state.get("csc_value"))
    if want and a.epic_name:
        if not a.epic_name.strip().startswith(want):
            print(
                f"이 에픽은 이 폴더의 CSC 가 아니다: {a.epic_name!r}\n"
                f"  csc_value    = {state.get('csc_value')!r}\n"
                f"  기대 접두사  = {want}\n"
                "여기에 태스크를 만들면 옆 CSC 로 티켓이 넘어가고, 이 폴더의 "
                "다음 조회에서는 사라진다. 에픽을 다시 고르십시오.",
                file=sys.stderr,
            )
            return 2

    # --- 순번 채번 (D-019 · F-031) -------------------------------------
    if task_seq(summary) is not None:
        print(
            f"제목에 순번이 이미 붙어 있다: {summary!r}\n"
            "순번은 이 스크립트가 붙인다 — 손으로 붙이면 같은 에픽에 같은 "
            "번호가 둘 생긴다. 번호를 떼고 다시 시도하십시오.",
            file=sys.stderr,
        )
        return 2

    if a.epic_summaries:
        try:
            raw = open(a.epic_summaries, encoding="utf-8").read()
        except OSError as e:
            print(f"--epic-summaries 를 읽지 못했다: {e}", file=sys.stderr)
            return 2
        try:
            loaded = json.loads(raw)
        except ValueError:
            loaded = [ln for ln in raw.splitlines() if ln.strip()]
        if isinstance(loaded, dict):
            loaded = loaded.get("summaries") or loaded.get("issues") or []
        existing = []
        for it in loaded:
            existing.append(it.get("summary", "") if isinstance(it, dict) else str(it))
        summary = "--%03d-- %s" % (next_task_seq(existing), summary)

    prefix = (state.get("title_prefix") or "").strip()
    if prefix and not summary.startswith(prefix):
        # 이미 다른 대괄호 표시가 붙어 있으면 접두사가 겹쳐 찍힌다.
        # 그 자체로 틀린 것은 아니지만([PFD] [긴급] ... 은 멀쩡하다) 다른
        # 폴더의 마커를 그대로 들고 온 경우일 수 있으므로 짚어둔다.
        # 막지는 않는다 — 확정 제목은 승인 표에서 사람이 보고 고칠 수 있다.
        if summary.startswith("["):
            print(f"주의: 이미 대괄호 표시가 붙은 제목에 {prefix} 를 덧붙인다. "
                  "다른 폴더의 마커가 아닌지 확인하십시오.", file=sys.stderr)
        summary = f"{prefix} {summary}"

    include = compile_patterns(state.get("title_include") or [], "title_include")
    exclude = compile_patterns(state.get("title_exclude") or [], "title_exclude")
    if not title_match({"summary": summary, "is_subtask": False}, include, exclude):
        print(
            f"이 제목은 이 폴더가 맡지 못한다: {summary!r}\n"
            f"  title_prefix  = {state.get('title_prefix')!r}\n"
            f"  title_include = {state.get('title_include')}\n"
            f"  title_exclude = {state.get('title_exclude')}\n"
            "이대로 만들면 티켓은 Jira에 남고 이 폴더의 일지에서는 사라진다. "
            "접두사나 필터를 맞춘 뒤 다시 시도하십시오.",
            file=sys.stderr,
        )
        return 2

    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
