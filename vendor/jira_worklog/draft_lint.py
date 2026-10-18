#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""초안의 본문이 '문서'가 되어 가는지, 읽는 사람이 못 여는 것을 가리키는지 본다.

Jira 태스크 설명과 코멘트는 팀에 사실을 공유하는 자리다. 그런데 재료가
많으면 초안은 자연스럽게 목차와 표를 갖춘 보고서로 자란다. 그렇게 자란
본문은 읽는 사람이 인과를 다시 찾아내야 하고, 대개 아무도 안 읽는다.

더 조용한 실패가 하나 더 있다. 초안은 **작성자의 로컬 작업 폴더에서** 나오므로
`daily/`나 `foo.py:120` 같은 위치를 자연스럽게 인용한다. 작성자에게는 가장
정확한 표현이지만 팀원은 그 경로를 열 수 없다. 그 문장은 읽는 사람에게
아무것도 전달하지 않으면서 자리만 차지하고, 몇 달 뒤에는 작성자도 못 연다.

문체는 기계가 판정할 수 없다. 대신 **문서로 자랐다는 표시**와 **못 여는 것을
가리킨다는 표시**는 셀 수 있다 — 머리말, 표, 길이, 로컬 참조. 이 넷을 세어
승인 전에 사람에게 보인다.

길이 상한은 설명과 코멘트가 다르다. 설명은 "이 태스크가 무엇인가"라 한
덩어리면 족하고, 코멘트는 현황 공유라 판단 근거가 되는 수치를 담아야 한다.

  draft_lint.py --draft .workflow/draft.json
  draft_lint.py --draft ... --json

종료 코드
  0  지적 없음
  1  지적 있음 — **중단이 아니라 경고다.** 승인 표에 함께 보이고, 사람이
     그대로 승인할 수 있다. 문체는 판단이지 규칙 위반이 아니기 때문이다.
  2  파일을 읽을 수 없음 (설정 오류)
"""
import argparse
import json
import re
import sys

MAX_CHARS = 600            # 코멘트 — 이보다 길면 대개 배경 설명이 들어와 있다
MAX_DESC_CHARS = 300       # 설명 — 인과 사슬 한 덩어리면 족하다
HEADING = re.compile(r"^#{1,6}\s", re.M)
TABLE_SEP = re.compile(r"^\s*\|?[\s:-]*\|[\s:|-]*$", re.M)
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)

# URL 은 검사 전에 걷어낸다. 공유 저장소의 PR·커밋·문서 링크는 팀원이 열 수
# 있으므로 로컬 참조가 아니고, 그 안의 `README.md` 같은 조각이 아래 패턴에
# 걸리면 정작 권장하는 표현이 지적당한다.
URL = re.compile(r"https?://\S+")

# 팀원이 자기 자리에서 열 수 없는 것을 가리키는 표시.
# 구분자는 ``/`` 와 ``\\`` 를 모두 본다. 문자 클래스 안에서 ``[\/]`` 는
# 이스케이프된 ``/`` 하나일 뿐이라 역슬래시를 놓쳤고, 이 팀의 경로는 대부분
# ``K:\\...`` 형태라 검사가 통째로 비켜 갔다(F-044).
SEP = r"[\\/]"
LOCAL_REF = [
    ("절대경로", re.compile(r"[A-Za-z]:" + SEP + r"|~" + SEP
                        + r"|(?<![\w~])/(?:mnt|home|Users|tmp)/")),
    ("작업폴더", re.compile(r"(?<![\w/\\])(?:daily|weekly|logs|rd|scripts|tests|"
                        r"fixtures|review_docs|output_docs|\.workflow)" + SEP)),
    ("파일명", re.compile(r"(?<![\w/])[\w.-]+\.(?:md|py|json|jsonl|csv|tsv|txt|ya?ml|"
                       r"ipynb|xlsx|docx|pptx|sh|bat|log)(?![A-Za-z0-9_.-])")),
    ("줄번호", re.compile(r"[\w.-]+\.[A-Za-z]{1,6}:L?\d+|(?<![\w])L\d{1,6}(?:-L?\d{1,6})?(?![A-Za-z0-9_])")),
    ("문서지시", re.compile(r"(?:위|아래|앞|해당|그)\s*(?:문서|파일|절)|§|(?<![\d])\d+\s*절")),
    # 대장 ID(D-007 · F-094 · M-01 · NC-17 · INV-4 · R-003). 로컬 대장
    # graph/edges.csv 와 state.json 에만 있으므로 팀원은 무엇을 가리키는지
    # 알 수 없다. 경로와 달리 형태가 Jira 키를 닮아 더 나쁘다 — 읽는 사람이
    # 열어볼 수 있다고 **믿고** 찾다가 없다는 것을 알게 된다(F-046).
    # Jira 키(PROJ-123)와 겹치지 않도록 접두사를 못박아 둔다.
    ("대장ID", re.compile(r"(?<![\w-])(?:D|F|M|NC|INV|R|UF|IF|REQ)-\d{1,4}(?![\w-])")),
]


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def local_refs(text):
    """팀원이 못 여는 위치 표현을 찾아 원문 조각으로 돌려준다.

    지적은 조각까지 보여야 쓸모가 있다 — "로컬 참조 3건"만으로는 어느 문장을
    고쳐야 하는지 사람이 다시 찾아야 한다.
    """
    body = URL.sub(" ", text)
    hits = []
    for _, rx in LOCAL_REF:
        for m in rx.finditer(body):
            frag = m.group(0).strip()
            if frag and frag not in hits:
                hits.append(frag)
    # 패턴이 겹치면 같은 자리가 두 번 나온다("foo.py"와 "foo.py:120").
    # 긴 쪽만 남긴다 — 사람이 고칠 자리는 하나다.
    return [f for f in hits if not any(f != o and f in o for o in hits)]


def lint(text, where, limit=MAX_CHARS):
    """한 덩어리 본문에서 '문서로 자란 표시'와 '못 여는 참조'를 센다."""
    out = []
    if not text:
        return out
    heads = HEADING.findall(text)
    if heads:
        out.append({"where": where, "kind": "heading", "count": len(heads),
                    "hint": "머리말(##)은 목차가 필요할 만큼 길다는 뜻이다. "
                            "인과 문장으로 펴거나 상세 문서로 링크한다."})
    rows = TABLE_ROW.findall(text)
    if TABLE_SEP.search(text) or len(rows) >= 2:
        out.append({"where": where, "kind": "table", "count": len(rows),
                    "hint": "표는 값을 나열할 뿐 인과를 말하지 않는다. "
                            "중요한 수치만 문장에 넣고 나머지는 근거 경로로."})
    if len(text) > limit:
        out.append({"where": where, "kind": "length", "count": len(text),
                    "hint": f"{limit}자를 넘는다. 배경 설명이 섞였는지 본다."})
    refs = local_refs(text)
    if refs:
        shown = ", ".join(refs[:5]) + (" 외" if len(refs) > 5 else "")
        out.append({"where": where, "kind": "local_ref", "count": len(refs),
                    "samples": refs, "hint":
                    f"팀원이 열 수 없는 위치를 가리킨다 ({shown}). 경로 대신 "
                    "대상 이름으로 쓰거나, 공유 저장소 링크·Jira 키로 바꾼다."})
    return out


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        with open(a.draft, encoding="utf-8") as f:
            draft = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"초안을 읽을 수 없다: {a.draft} — {exc}", file=sys.stderr)
        return 2

    findings = []
    for i, r in enumerate(draft.get("new_roots") or []):
        tag = r.get("ref") or r.get("local_id") or f"new_roots[{i}]"
        findings += lint(r.get("description"), f"{tag} 설명", MAX_DESC_CHARS)
    for i, c in enumerate(draft.get("comments") or []):
        tag = c.get("jira_key") or c.get("local_id") or f"comments[{i}]"
        findings += lint(c.get("body"), f"{tag} 코멘트", MAX_CHARS)
    for i, c in enumerate(draft.get("children") or []):
        tag = c.get("local_id") or c.get("summary") or f"children[{i}]"
        findings += lint(c.get("description"), f"{tag} 설명", MAX_DESC_CHARS)

    if a.json:
        json.dump({"count": len(findings), "findings": findings},
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 1 if findings else 0

    if not findings:
        print("draft_lint: 지적 없음")
        return 0

    print(f"draft_lint: {len(findings)}건 — 승인 전에 확인하십시오 (중단 아님)")
    for f in findings:
        print(f"  [{f['kind']}] {f['where']} ({f['count']}) — {f['hint']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
