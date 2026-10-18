#!/usr/bin/env python3
"""작업 로그 JSONL 스키마 — files.jsonl / prompts.jsonl 공용 정의.

GLOBAL_RULES.md Rule 4(파일 생성 로그), Rule 8(프롬프트·응답 로그)을 기계가 검사할 수
있는 형태로 옮긴 것이다. 규칙이 이미 "빈 칸은 규칙 위반, '검증 없음'은 명시적 선언"이라고
말하므로, 그 구분을 코드로 강제한다.

JSON 키는 영문이다. jq 질의와 도구 처리에서 안전하고, 한글 컬럼명은 아래 표로 대응한다.
"""

TS_RE = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"
REQ_RE = r"^P-\d{3,}$"
BASIS_RE = r"^[FDM]-[A-Za-z0-9_]+$"

# Rule 4 — 파일 생성 로그
FILES_SCHEMA = {
    "ts":        ("일시",      "str",   True,  TS_RE),
    "files":     ("파일명",     "list",  True,  None),
    "summary":   ("요청 요약",   "str",   True,  None),
    "req_id":    ("요청 ID",    "str",   True,  REQ_RE),
    "basis_ids": ("근거 ID",    "list",  True,  None),   # 빈 배열 허용 (해당 원장 항목 없음)
    "verify":    ("검증",       "str",   True,  None),   # 빈 문자열 금지. "검증 없음"은 허용
    "commit":    ("커밋 ID",    "str",   True,  None),   # "미커밋" 허용
}

# Rule 8 — 프롬프트·응답 로그
PROMPTS_SCHEMA = {
    "ts":       ("일시",           "str",  True, TS_RE),
    "req_id":   ("요청 ID",        "str",  True, REQ_RE),
    "prompt":   ("요청 프롬프트",    "str",  True, None),
    "response": ("응답/결과/대처",   "str",  True, None),
    "outputs":  ("산출물 경로",      "list", True, None),  # 빈 배열 허용
}

# 빈 값을 허용하는 필드 — 그 자체가 의미 있는 선언이므로 채워져 있어야 한다
ALLOW_EMPTY_LIST = {"basis_ids", "outputs"}

# 값이 비어 있으면 규칙 위반인 필드
NEVER_BLANK = {"ts", "req_id", "summary", "verify", "commit", "prompt", "response"}

# schema == "v1" 인 과거 기록에서 비어 있어도 되는 필드.
# v2 컬럼(요청 ID·근거 ID·검증·커밋 ID) 도입 전 행이므로 당시엔 그 칸이 없었다.
# 없는 값을 지어내 채우지 않는다 — 존재한 적 없는 provenance 를 만드는 일이다.
LEGACY_OPTIONAL = {"req_id", "basis_ids", "verify", "commit", "outputs", "response"}
