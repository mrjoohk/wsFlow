# I0 UF→IF 범위 검토 — P-014 / Gate G2

P-018 I4: `rd/i4_contracts.md` Gate G2에서 권한→서버 계획→outbox→원격 대조→pull/dedup→충돌 해결→UI 책임을 연결했다. I4 범위 내 누락/중복 없음. 실행 검증/실계정 수용과 구분한다.

P-017 I3: `rd/i3_contracts.md`의 Gate G2로 OAuth/보호 자격→계정/선택→mirror/cursor→occurrence→snapshot/UI 책임을 연결했다. 범위 내 누락/중복 없음. 이전 단계 판정과 실행 검증/실계정 수용은 구분한다.

사용자가 지시한 I0의 계약 분해·연결 검토다. `rd/uf.md`가 함수 계약 정본이며 통합 요구의 I1 이후 부분은 이 검토의 대상이 아니다.

| IF / I0 수용 책임 | 담당 UF | I/O 연결 | 판정 |
|---|---|---|---|
| IF-CTX-01: 출처·경로·owner·hash·무변경 | UF-01-01, UF-01-02 | rooted read metadata → ContextSnapshot | PASS |
| IF-CTX-02: ROOT/전용 정본·명시 참조·구조 충돌 | UF-01-02, UF-02-01 | 조사 text 내부 입력 → rules JSON | PASS |
| IF-CTX-06: legacy ID/API 호환 | UF-04-01 | Store 등록 dict → 신규 versioned 응답, 기존 응답 유지 | PASS |
| IF-CAL-01: 유효 날짜·고유 task·공통 메모·실패/0 구분 | UF-01-01, UF-03-01, UF-03-02 | ISO 질의 → 기존 파일 bytes → 날짜별 records/counts | PASS |
| 기간·날짜 상세 snapshot 대조 | UF-03-02, UF-04-01 | range 결과의 snapshot/day → HTTP day 결과 | PASS |

모든 I0 수용 책임에 함수와 검증 경로가 연결되어 있다. 책임 중복은 없다. UTF-8 bytes→text 해석은 파일 읽기 경계에서 명시적으로 한다. ContextSnapshot의 UUID와 날짜 데이터 snapshot의 content hash는 목적이 다르며 서로 대입하지 않는다.

이 판정은 함수 계약의 연결에 대한 것이고 시험 통과/기능 완료 선언이 아니다. 실제 구현 결과는 후속 P-014 보고서와 evidence로 검증한다. I1 이후 쓰기·노출·실제 도구 관측·Google 동기화는 이 게이트의 통과 범위가 아니다.

## P-015 I1 범위

I1은 `rd/i1_contracts.md`의 별도 UF-I1-01~09와 Gate G2 표로 검토했다. 메모→계획→승인/claim→원격 결과→state 어댑터의 타입·식별·실패 상태가 연결되어 있다. 범위 내 누락과 중복 없음. I0만 검토했던 위 표와 I1 검토를 구분한다.

## P-016 I2 범위

`rd/i2_contracts.md`의 별도 Gate G2에서 로컬 데이터 검증→계획/버전 저장→순수 집계→월/주·같은 snapshot 상세→문서별 초안의 연결을 검토했다. I2 책임 내 누락/중복 없음. 실행 검증은 후속 P-016 evidence로 구분한다.
# I5 증분 coverage

P-019: [i5_contracts.md](i5_contracts.md)의 U1→U2→U3, deploy guard U4, API/UI U5가 IF-CTX-01~04 해당 범위를 연결한다. G2 PASS. I6/I7 책임은 이번 완료 범위에 포함하지 않는다.

# I6 증분 coverage

P-020: [i6_contracts.md](i6_contracts.md)의 U1→U2→U3→U4와 U5 adapter가 IF-CTX-05의 I6 범위를 연결한다. G2 PASS. 파일 연결·수동 관측의 신뢰 수준을 구분하고 I7/실제 도구 전수 수용은 미완료다.

# I7 증분 coverage

P-021: [i7_contracts.md](i7_contracts.md)의 UF01→05, UF02→03→04→05가 Git/Jira 일일 순환·조회/생성·전체 상태 책임을 연결한다. G2 PASS. 실제 Jira/Google 계정·실제 도구·타 OS·CI 수용은 별도다.



## P038 공통 Git·메모 쓰기 확장

`rd/common_write_contracts.md`의 UF-CW-01~04와 Gate G2가 정본이다. 계획→claim→효과/receipt→조회/복구→API/UI의 연결을 검토했으며 범위 내 UNCOVERED/REDUNDANT 없음. 기존 블록을 보존하고 실행 검증은 별도 증거로 구분한다.

## P072 raw Git 쓰기 보호

[261103_P072_git_writer_contracts.md](261103_P072_git_writer_contracts.md)의 UF-GT-01/02를 UF-I7-01 및 GT01~05에 연결했다. 진입 전 거절·실제 Git/기록/최종 상태·중첩/실패·사용자 bytes 보존 책임이 연결되며 범위 내 UNCOVERED/REDUNDANT 없음. Gate G2 PASS(설계 연결, 실행 검증 별도).

## P073 OAuth 완료 관측

[261104_P073_oauth_completion_contract.md](261104_P073_oauth_completion_contract.md)의 UF-PA-T01은 기존 모듈 pause/콜백 완료 책임을 같은 시험에 연결한다. Gate G2 PASS, 범위 내 누락/중복 없음. 제품 변경과 전체 CI 수용은 별도다.
