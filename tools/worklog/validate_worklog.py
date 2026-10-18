#!/usr/bin/env python3
"""작업 로그 JSONL 을 스키마와 상호 참조 규칙으로 검사한다.

    python validate_worklog.py files.jsonl prompts.jsonl

검사 항목
  - 줄마다 올바른 JSON 인가
  - 필수 필드가 모두 있는가, 타입이 맞는가
  - 일시 형식 `YYYY-MM-DD HH:MM`, 요청 ID 형식 `P-xxx`
  - `검증` 이 비어 있지 않은가  (Rule 4: 빈 칸은 규칙 위반, "검증 없음"은 명시적 선언)
  - `파일명` 에 와일드카드(*)가 없는가  (Rule 4: 행은 질의 가능해야 한다)
  - 요청 ID 가 prompts 안에서 유일하고 증가하는가
  - files 의 모든 요청 ID 가 prompts 에 존재하는가  (Rule 8: 요청 ID 는 provenance root)

종료코드: 0 = 통과, 1 = 위반 있음, 2 = 사용법 오류
"""
import json
import re
import sys

from worklog_schema import (
    FILES_SCHEMA,
    PROMPTS_SCHEMA,
    ALLOW_EMPTY_LIST,
    NEVER_BLANK,
    LEGACY_OPTIONAL,
)


def load(path):
    """[(줄번호, 레코드|None, 오류|None)] 반환."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    out.append((n, json.loads(line), None))
                except json.JSONDecodeError as e:
                    out.append((n, None, f"JSON 파싱 실패: {e}"))
    except OSError as e:
        print(f"열 수 없음: {path} — {e}")
        sys.exit(2)
    return out


def check_record(rec, schema, lineno, path, errors):
    # schema=="v1" 은 v2 컬럼 도입 전의 과거 기록이다. 그 칸이 비어 있는 것은
    # 규칙 위반이 아니라 당시에 그 칸이 없었다는 사실이므로, 존재·형식만 본다.
    legacy = rec.get("schema") == "v1"
    if legacy:
        for key in ("ts",):
            pass

    for key, (kor, kind, required, pattern) in schema.items():
        if key not in rec:
            if legacy and key in LEGACY_OPTIONAL:
                continue
            errors.append(f"{path}:{lineno}  필드 없음: {key} ({kor})")
            continue
        val = rec[key]

        if legacy and key in LEGACY_OPTIONAL:
            # 과거 기록: 값이 없어도 통과. 있으면 형식은 본다.
            if isinstance(val, list) or (isinstance(val, str) and not val.strip()):
                continue

        if kind == "list":
            if not isinstance(val, list):
                errors.append(f"{path}:{lineno}  {key}({kor})는 배열이어야 한다 — 받은 값: {type(val).__name__}")
                continue
            if key not in ALLOW_EMPTY_LIST and not val:
                errors.append(f"{path}:{lineno}  {key}({kor})가 비어 있다")
            for item in val:
                if not isinstance(item, str):
                    errors.append(f"{path}:{lineno}  {key}({kor}) 원소는 문자열이어야 한다")
                elif key == "files" and "*" in item and not rec.get("legacy_note"):
                    errors.append(
                        f"{path}:{lineno}  {key}({kor})에 와일드카드: {item!r} "
                        f"— Rule 4 는 와일드카드를 금지한다 (행이 질의 가능해야 함). "
                        f"과거 기록이라 풀 수 없으면 legacy_note 에 사유를 남긴다"
                    )
            continue

        if not isinstance(val, str):
            errors.append(f"{path}:{lineno}  {key}({kor})는 문자열이어야 한다")
            continue
        if key in NEVER_BLANK and not val.strip():
            hint = ""
            if key == "verify":
                hint = ' — 검증하지 않았다면 빈 칸이 아니라 "검증 없음" 이라고 쓴다'
            elif key == "commit":
                hint = ' — 아직이면 "미커밋" 이라고 쓴다'
            errors.append(f"{path}:{lineno}  {key}({kor})가 비어 있다{hint}")
        if pattern and val.strip() and not re.match(pattern, val):
            errors.append(f"{path}:{lineno}  {key}({kor}) 형식 오류: {val!r}")


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2

    files_path, prompts_path = argv[1], argv[2]
    errors = []

    prompts = load(prompts_path)
    files = load(files_path)

    seen = {}
    last_num = -1
    prev_rid = None
    for lineno, rec, err in prompts:
        if err:
            errors.append(f"{prompts_path}:{lineno}  {err}")
            continue
        check_record(rec, PROMPTS_SCHEMA, lineno, prompts_path, errors)
        rid = rec.get("req_id", "")
        if rec.get("schema") == "v1" and not rid:
            continue
        if isinstance(rid, str) and re.match(r"^P-\d{3,}$", rid):
            # 한 요청 안에서 왕복이 여러 번이면 같은 ID 가 연속으로 온다. 이는 정상이다.
            # 떨어져 있다가 다시 나타나는 것만 순서가 깨진 것이다.
            if rid in seen and rid != prev_rid:
                errors.append(
                    f"{prompts_path}:{lineno}  요청 ID 가 연속되지 않게 재등장: {rid} (앞서 {seen[rid]}행)"
                )
            seen[rid] = lineno
            num = int(rid.split("-")[1])
            if num < last_num:
                errors.append(f"{prompts_path}:{lineno}  요청 ID 가 역행한다: {rid}")
            last_num = max(last_num, num)
            prev_rid = rid

    for lineno, rec, err in files:
        if err:
            errors.append(f"{files_path}:{lineno}  {err}")
            continue
        check_record(rec, FILES_SCHEMA, lineno, files_path, errors)
        rid = rec.get("req_id", "")
        if rec.get("schema") == "v1" and not rid:
            continue
        if isinstance(rid, str) and rid and rid not in seen:
            # orphan_reason 은 "프롬프트 기록이 원본에 없었다" 는 사실을 명시적으로 남긴 것이다.
            # legacy_note 와 별도 필드로 둔 이유: 다른 사유의 메모가 이 검사를 대신 통과시키면 안 된다.
            if rec.get("orphan_reason"):
                continue
            errors.append(
                f"{files_path}:{lineno}  요청 ID {rid} 가 {prompts_path} 에 없다 "
                f"— 요청 ID 는 provenance root 이므로 대응하는 프롬프트 기록이 있어야 한다. "
                f"원본에 프롬프트 기록이 없었다면 orphan_reason 에 그 사실을 남긴다"
            )

    n_p = sum(1 for _, r, e in prompts if r and not e)
    n_f = sum(1 for _, r, e in files if r and not e)
    print(f"prompts: {n_p}건 · files: {n_f}건")

    if errors:
        print(f"\n위반 {len(errors)}건")
        for e in errors:
            print(f"  {e}")
        return 1

    print("통과 — 스키마·상호참조 이상 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
