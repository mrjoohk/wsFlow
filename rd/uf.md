# I0 함수 계약 — P-014

P-018 I4의 UF-I4-01~07과 Google 쓰기/복구 계약은 `rd/i4_contracts.md`를 따른다. 이전 단계 계약은 보존한다.

P-017 I3의 UF-I3-01~06 및 Google API 계약은 `rd/i3_contracts.md`를 따른다. 기존 I0/I1/I2 계약은 각 단계 원문을 유지한다.

통합 목표의 I0만 분해한다. CTX/CAL 전체 구현 계약이나 I1 이후의 승인 완료를 뜻하지 않는다. 현재 사용자 지시는 I0 구현을 승인한다.

## UF-01-01 — 경계 안의 원본 읽기
- Parent IF: IF-CTX-01, IF-CAL-01
- Goal: 허용한 파일만 읽고 경로·해시·오류를 반환한다.
- I/O Contract: Input 파일 Path 한 개와 허용 파일/디렉터리 Path 목록; Output JSON metadata 한 개와 bytes 또는 None. 경로는 절대경로, 크기는 byte, sha256은 hex 문자열이다.
- Algorithm Summary: 최종 실경로를 허용 범위와 대조한 뒤 읽고 전후 stat을 비교한다. 오류·누락·경계 위반은 구분한다.
- Edge Cases: 없는 파일은 missing; 외부 링크/순환은 blocked; 읽기 거부·변경 중 파일은 error. 생성·실행·저장은 하지 않는다.
- Verification Plan: Ownership: guard-rail+chain; Unit Verification: tests/test_i0.py의 경계/입력 검사; Chain Verification: 동일 파일 트리·mtime 전후 비교 및 HTTP 조회 검사.
- Evidence Pack Fields: scenario_id, source_hash, expected, observed, pass, duration_sec.

## UF-01-02 — 선택 프로젝트 컨텍스트 조사
- Parent IF: IF-CTX-01
- Goal: 등록 프로젝트와 소속 ROOT의 정책·스킬 출처를 조사한다.
- I/O Contract: Input project dict, roots list[dict], projects list[dict]; Output schema_version=1 ContextSnapshot dict. UUID snapshot_id, captured_at(UTC offset), owner·declared/resolved path·sha/status를 갖는다.
- Algorithm Summary: 경로 경계로 가장 구체적인 등록 ROOT를 고르고 고정 위치/명시 프로필만 읽는다. 스킬 패키지는 정렬된 상대경로와 지원 파일 해시를 결합한다.
- Edge Cases: UID 미설정은 legacy; 복제 registry UID/ROOT 불일치는 blocker; 형제 프로젝트 본문·임의 Markdown 참조는 읽지 않음. 도구 전역/실제 세션 범위는 unknown.
- Verification Plan: Ownership: IF-acceptance; Unit Verification: N/A; Chain Verification: tests/test_i0.py ContextTests.
- Evidence Pack Fields: snapshot_id, source_fingerprint, warnings, blockers, pass.

## UF-02-01 — 구조적 정책 해석
- Parent IF: IF-CTX-02
- Goal: 공통·전용 정본과 진입점의 명시 참조·가려짐·예외를 진단한다.
- I/O Contract: Input metadata와 조사 중 읽은 정책 text의 내부 dict; Output rules dict(정본, entrypoint 참조, exceptions, warnings, blockers, ready bool). 본문은 응답에 복사하지 않는다.
- Algorithm Summary: 명시 경로 참조만 정본 실경로와 대조하고 다른 GLOBAL 사본·override·동명 스킬을 보고한다. 자연어 의미 준수나 도구 로딩을 확정하지 않는다.
- Edge Cases: 독립 Git 경계 밖 ROOT의 자동 상속 가정 금지; 참조 누락/접근 불가 시 ready=false; 예외에는 ID와 근거 필수.
- Verification Plan: Ownership: IF-acceptance; Unit Verification: N/A; Chain Verification: tests/test_i0.py ContextTests.
- Evidence Pack Fields: rule_sources, reference_results, observation_kind, pass.

## UF-03-01 — 날짜 질의 검증
- Parent IF: IF-CAL-01
- Goal: ISO 업무 날짜·범위·시간대를 검증한다.
- I/O Contract: Input start/end YYYY-MM-DD(str), end exclusive, timezone IANA str; Output date 두 개와 검증된 timezone 이름. 1~62일, 업무 날짜 2000~2099(legacy YYMMDD 충돌 방지), 마지막 exclusive end 2100-01-01 허용.
- Algorithm Summary: 정규 형식과 실제 날짜, 유한 기간, ZoneInfo 가용성을 검증한다. 업무 날짜를 UTC로 이동하지 않는다.
- Edge Cases: 경로 문자열/불가능 날짜/역전·과대 범위는 ValueError; 시간대 데이터 미설치는 명시 오류; 윤일 허용.
- Verification Plan: Ownership: guard-rail+chain; Unit Verification: tests/test_i0.py CalendarTests 입력 검사; Chain Verification: HTTP 400 및 날짜별 집계.
- Evidence Pack Fields: query, timezone_available, expected, observed, pass.

## UF-03-02 — 순수 날짜 기록 집계
- Parent IF: IF-CAL-01
- Goal: 기존 daily의 저장된 태스크 작업 기록과 공통 메모를 날짜별로 집계한다.
- I/O Contract: Input 등록 projects list[dict]와 검증할 날짜 질의; Output schema_version=1 dict(snapshot_id content hash, query, sources, days). day별 tasks/memos list, task_count/memo_count int 또는 null, complete bool. event_count는 미구현 null이다.
- Algorithm Summary: real WF_HOME별로 한 번 읽고 프로젝트 연결을 유지한다. 사이트+키로 고유 task를 합치고 공통/후보 메모 묶음은 원본별 한 건이다. 원본 상태·질의 hash로 snapshot을 만든다.
- Edge Cases: 빈/없는 daily는 확인된 0; 현재 목록에 없는 키도 유지; marker 손상·권한 거부·소스 부재는 unknown(null). 다중 기록 한 건, task 기록은 메모 건수에서 제외.
- Verification Plan: Ownership: IF-acceptance; Unit Verification: N/A; Chain Verification: tests/test_i0.py CalendarTests와 ReadApiTests. 기존 편집용 read_memo는 호출하지 않는다.
- Evidence Pack Fields: source_hash, snapshot_id, daily_counts, complete, pass.

## UF-04-01 — 버전 있는 읽기 API
- Parent IF: IF-CTX-06, IF-CAL-01
- Goal: 기존 API 의미를 유지하며 위 읽기 모델을 HTTP로 제공한다.
- I/O Contract: GET /api/v1/projects/<id>/context; GET /api/v1/calendar/range?start=&end=&timezone=&project_id=; GET /api/v1/calendar/day?date=&start=&end=&timezone=&project_id=&snapshot_id=. Output 위 JSON, invalid query 400, 미등록 ID 404, 오래된 snapshot 409. day의 start/end를 range와 같게 주면 같은 snapshot을 대조한다.
- Algorithm Summary: 기존 Host/Origin/token guard 뒤에서 등록 목록만 사용하고 새 모델로 연결한다. day는 range의 같은 결과에서 선택한다.
- Edge Cases: 비 GET은 404; 복수 project_id는 선택 집합, 없으면 전체; range snapshot 불일치는 재조회 요구. 임의 경로 인자·네트워크 쓰기·새 UID 저장 금지.
- Verification Plan: Ownership: IF-acceptance; Unit Verification: N/A; Chain Verification: tests/test_i0.py ReadApiTests 및 기존 tests/run_tests.py 전체 회귀.
- Evidence Pack Fields: route, HTTP_status, legacy_response, filesystem_before_after, pass.

판단 근거: I0는 이미 정한 정본과 레거시 메모 문법을 직접 읽는 단일 후보가 명확하다. 새로운 저장 backend나 링크 방식의 후보 비교·선택은 I1/I5에 남긴다. 함수는 기존 프로젝트 구조와 unittest 관례를 따른다.

## P-015 I1 확장

I0 블록은 그대로 보존한다. I1의 함수 계약과 입출력·검증 소유권은 `rd/i1_contracts.md`가 정본이며 해당 문서의 Gate G2 연결 검토를 완료한 범위에서 구현한다.

## P-016 I2 확장

I2의 함수 I/O·실패·검증 소유권은 `rd/i2_contracts.md`의 UF-I2-01~05와 Gate G2가 정본이다. 기존 I0/I1 계약을 보존한다.
# I5 증분 UF

P-019: [i5_contracts.md](i5_contracts.md)의 I5-U1~U5를 따른다. I0~I4 블록은 보존한다.

# I6 증분 UF

P-020: [i6_contracts.md](i6_contracts.md)의 I6-U1~U5를 따른다. 기존 블록은 보존한다.

# I7 증분 계약

P-021의 UF-I7-01~05와 Gate G2는 [i7_contracts.md](i7_contracts.md)에 둔다. 기존 단계 계약은 보존한다.



## P038 공통 Git·메모 쓰기 확장

`rd/common_write_contracts.md`의 UF-CW-01~04와 Gate G2가 정본이다. 계획→claim→효과/receipt→조회/복구→API/UI의 연결을 검토했으며 범위 내 UNCOVERED/REDUNDANT 없음. 기존 블록을 보존하고 실행 검증은 별도 증거로 구분한다.

## P072 raw Git 쓰기 보호

UF-GT-01/02와 Gate G2는 [261103_P072_git_writer_contracts.md](261103_P072_git_writer_contracts.md)를 따른다. 기존 UF-I7-01의 전체 실행 경계를 확장하고 정상 응답·기존 Git 보호를 유지한다.

## P073 OAuth 완료 관측

UF-PA-T01과 Gate G2는 [261104_P073_oauth_completion_contract.md](261104_P073_oauth_completion_contract.md)를 따른다. 기존 콜백 시험의 클라이언트 응답과 서버 활동 종료 관측을 연결한다.

## P074 로컬 Git 전체 호출

UF-GL-01~03과 Gate G2는 [261105_P074_git_local_writer_contracts.md](261105_P074_git_local_writer_contracts.md)를 따른다. 기존 UF-I7-01의 로컬 config/index/commit/lock 공개 호출을 같은 협조 쓰기 보호에 연결한다.

## P075 Git push 전체 호출

UF-GP-01/02와 Gate G2는 [261106_P075_push_writer_contracts.md](261106_P075_push_writer_contracts.md)를 따른다. raw push 전체 호출과 기존 Service의 원격 결과/재전송 차단 검증을 구분한다.
