#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""진행 메모를 초안 작성자에게 넘길 형태로 가른다.

이 스크립트가 있는 이유는 둘이다.

**하나 — 가리키기만 한 메모를 그냥 지나치지 않는다.** 사람은 하루를 정리해
문서로 남겨 놓고, 메모에는 그 문서의 경로만 적는다. 그것이 가장 자연스러운
동작이다. 그런데 초안 작성자는 그 문서를 열지 않고(메모 원문과 집계 파일만
받는다), 열었더라도 경로는 코멘트에 쓸 수 없다 — 팀원이 못 여는 위치이기
때문이다. 그래서 그 줄은 조용히 ``unclear`` 로 빠지고, 그날 한 일은 Jira 에
한 글자도 남지 않는다. 사람은 적었다고 믿고 있으므로 가장 늦게 알아챈다.
여기서 종료 코드 2 로 멈춰 **문서를 읽어 메모로 옮기고 사람에게 확인받는
단계**를 강제한다.

**둘 — 출처는 로컬에만 남긴다.** ``[출처]`` 줄은 이 폴더의 기록으로는 값지지만
Jira 코멘트로 나가면 아무에게도 전달되지 않는다. 초안 작성자에게 넘길 페이로드
에서 아예 빼내어, 규칙을 기억하는 대신 **볼 수 없게** 만든다.

  memo_intake.py --daily daily/260903.md --out .workflow/memo_payload.md
  memo_intake.py --daily daily/260903.md --json

종료 코드
  0  실질 내용이 있다 — 페이로드를 썼다. 마감을 계속한다.
  1  메모가 비었다(플레이스홀더뿐) — 마감할 재료가 없다. 정상 종료 경로다.
  2  가리키기만 했다 — 출처나 경로만 있고 내용이 없다. 그 문서를 읽어 메모에
     옮기고 **사용자 확인을 받은 뒤** 다시 돌린다.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from draft_lint import LOCAL_REF, URL  # noqa: E402
from render_daily import MEMO_PLACEHOLDER  # noqa: E402

M_START = "<!-- MEMO:START -->"
M_END = "<!-- MEMO:END -->"
SOURCE_MARK = "[출처]"
DRAFT_MARKS = ("[신규]", "[파생]", "[BUG]")

# 이만큼도 안 남으면 "내용"이 아니라 "가리킴"이다. 경로와 링크를 걷어낸 뒤
# 남는 글자로 센다 — 조사·기호만 남는 줄을 내용으로 세면 이 검사가 무력해진다.
MIN_SUBSTANCE = 8


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _norm(line):
    """앞머리의 인용 표시(``>``)와 목록 표시를 떼고 남는 알맹이."""
    t = line.strip()
    while t.startswith(">"):
        t = t[1:].lstrip()
    return re.sub(r"^[-*]\s*", "", t).strip()


# 플레이스홀더는 **글자로** 알아본다. 인용 표시(``>``)로 알아보면 안 된다 —
# 플레이스홀더가 인용문이라 사람이 그 아래에 이어 쓰면 편집기가 ``>`` 를
# 물려주고, 그러면 사람이 쓴 줄을 통째로 버린다(F-045). 실제로 첫 사용자가
# ``> [출처] ...`` 로 적었고 마감은 "메모가 비어 있다" 고 답했다.
PLACEHOLDER_LINES = {_norm(l) for l in MEMO_PLACEHOLDER.splitlines() if _norm(l)}


def memo_body(text):
    """일지에서 메모 구획 안쪽만 꺼낸다. 표시가 없으면 빈 문자열."""
    i = text.find(M_START)
    j = text.find(M_END)
    if i < 0 or j < 0 or j < i:
        return ""
    return text[i + len(M_START):j]


# 구분자를 품은 낱말은 통째로 위치 표현으로 본다. draft_lint 의 LOCAL_REF 는
# 경로의 **머리**(``K:\\``, ``review_docs/``)를 짚어 사람에게 보여주는 것이
# 목적이라 꼬리가 남는데, 여기서는 "이 줄에 내용이 있는가"를 재야 하므로
# 남은 꼬리까지 걷어내야 한다.
PATHISH = re.compile(r"(?<!\S)\S*[\\/]\S*(?!\S)")


def substance(line):
    """위치 표현을 걷어낸 뒤 남는 글자 수. '가리킴'과 '내용'을 가르는 잣대.

    URL 도 걷어낸다. 공유 링크는 팀원이 열 수 있으므로 코멘트에 남겨도
    되지만, **링크만 있는 줄**은 경로만 있는 줄과 똑같이 아무것도 말하지
    않는다 — 무슨 일이 있었는지는 사람이 적어야 한다.
    """
    body = URL.sub(" ", line)
    body = PATHISH.sub(" ", body)
    for _, rx in LOCAL_REF:
        body = rx.sub(" ", body)
    return len(re.sub(r"[^0-9A-Za-z가-힣]", "", body))


def split_memo(text):
    """메모를 출처 · 내용 · 가리키기만 한 줄로 가른다.

    플레이스홀더(``>`` 로 시작)는 사람이 쓴 것이 아니므로 셈에서 뺀다.
    """
    sources, content, pointers = [], [], []
    for raw in memo_body(text).splitlines():
        line = raw.strip()
        stripped = _norm(line)
        if not stripped or stripped in PLACEHOLDER_LINES:
            continue
        if stripped.startswith(SOURCE_MARK):
            target = stripped[len(SOURCE_MARK):].strip()
            if target:
                sources.append(target)
            continue
        # 표시가 붙은 줄은 그 표시를 뗀 나머지로 실질을 잰다.
        rest = stripped
        for m in DRAFT_MARKS:
            if rest.startswith(m):
                rest = rest[len(m):].strip()
                break
        if substance(rest) < MIN_SUBSTANCE:
            pointers.append(line)
            continue
        content.append(raw.rstrip())
    return {"sources": sources, "content": content, "pointers": pointers}


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", required=True)
    ap.add_argument("--out", help="초안 작성자에게 넘길 페이로드 경로 "
                                  "(출처·가리킴 줄이 빠진 메모)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.daily):
        print(f"일지가 없다: {a.daily}. 일일 배치를 먼저 돌리십시오.", file=sys.stderr)
        return 2
    text = open(a.daily, encoding="utf-8").read()
    if M_START not in text or M_END not in text:
        print(f"메모 구획 표시를 찾지 못했다: {a.daily}. "
              "render_daily.py 가 만든 파일이 맞는지 확인하십시오.", file=sys.stderr)
        return 2

    r = split_memo(text)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))

    if not (r["sources"] or r["content"] or r["pointers"]):
        if not a.json:
            print("메모가 비어 있다 — 마감할 재료가 없다. "
                  "일지에 적으시거나 오늘 한 일을 말씀해 주십시오.")
        return 1

    if not r["content"]:
        pointed = r["sources"] + r["pointers"]
        if not a.json:
            print("메모가 **가리키기만** 했다 — 내용이 없다:\n  "
                  + "\n  ".join(pointed[:5])
                  + "\n\n초안 작성자는 이 문서를 열지 않고(메모와 집계만 받는다), "
                    "열었더라도 경로는 코멘트에 쓸 수 없다 — 팀원이 못 여는 "
                    "위치이기 때문이다. 이대로 마감하면 오늘 한 일이 Jira 에 "
                    "한 글자도 남지 않는다.\n"
                    "가리킨 문서를 읽어 **오늘 한 일에 해당하는 부분만** 메모에 "
                    "옮기고, 옮긴 내용을 사용자에게 보여 확인받은 뒤 다시 "
                    "돌리십시오. 옮기는 것은 요약이지 해석이 아니다.",
                  file=sys.stderr)
        return 2

    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(r["content"]) + "\n")

    if not a.json:
        extra = f" · 출처 {len(r['sources'])}건(로컬 기록 전용)" if r["sources"] else ""
        skipped = f" · 가리킴 {len(r['pointers'])}줄 제외" if r["pointers"] else ""
        print(f"memo_intake: 내용 {len(r['content'])}줄{extra}{skipped}"
              + (f" → {a.out}" if a.out else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
