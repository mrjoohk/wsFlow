# P038 공통 Git·메모 작업 계약

## 판단 근거와 범위

CTX-14/F032/F041의 다음 구현이다. 기존 operations.Store의 불변 plan/intent·조건부 claim과 기존 Git 어댑터·메모 CAS를 재사용하는 단일 후보가 명확하다. 새 DB/작업 상태 체계를 만들지 않는다. 실제 사용자 저장소·WF_HOME에서는 실행하지 않고 임시 Git/bare remote와 시험 WF_HOME으로 검증한다. 기존 API URL/입력/주요 결과 필드와 기존 회귀 사례는 유지한다. 새 UI 설계는 하지 않는다.

## UF-CW-01 계획

- Parent IF: 공통 쓰기/CTX-06·13·14.
- Input: project dict(id/path), kind=git|memo, action str, payload JSON object. Git 지원: add/commit/push/fetch/pull/remote_add/remote_set/remote_remove/clean_locks. Memo: save(ymd/text/sha/document_id).
- Output: plan_id/operation_id(str), expires_at(epoch seconds), 승인 대상 action/payload 및 source version 식별자. plan/작업은 사용자 데이터 DB에 저장하며 preview는 사용자 Git/daily를 변경하지 않는다.
- Git source: canonical checkout/git-dir, HEAD/refs/index/config·working files·lock metadata의 digest. 파일 콘텐츠를 읽을 수 없거나 외부로 연결된 경로는 계획을 거절한다. 상태 확인에 optional index refresh를 사용하지 않는다.
- Memo source: canonical checkout/WF_HOME·project ID, schema4 state hash, 날짜/document_id/예상 memo hash·전체 daily 원문 hash. 없는 daily는 먼저 기존 읽기 화면에서 열어야 한다. 계획은 새 daily를 생성하지 않는다.
- Edge: 옵션 주입·경로 탈출·종류별 허용되지 않은 필드·날짜 혼입·stale memo·다른 계정/프로젝트·미지원 schema 거절. 기존 memo conflict의 현재 text/sha를 유지한다.
- Verification ownership: guard-rail+chain; 정상 계획/원본 bytes·stale/잘못된 payload·만료·외부 링크 시험.

## UF-CW-02 적용·저장

- Input: server plan_id/operation_id 두 문자열만. 최초 사용자 버튼의 payload와 버전은 계획에 저장되며 적용에서 새 payload를 받지 않는다.
- Output: 기존 효과 결과 + plan_id/operation_id/status. 같은 ID 성공은 저장 결과를 재사용한다. 먼저 승인·claim을 기록하고 효과를 실행한다.
- Git은 사용자 데이터 영역의 협조 잠금, 메모는 기존 WF_HOME의 재진입 가능한 잠금을 사용한다. 실행 직전 binding·TTL·source를 다시 검사한다. 공통 코드와 기존 메모 CAS/백업·ff-only/dirty 차단을 유지한다.
- 결과를 DB에 저장하기 전에 작업별 receipt를 atomic replacement로 기록한다. 메모는 작업별 전체 daily 원문 백업도 효과 전에 저장·검사한다. DB/파일/Git의 다중 자원 원자성을 주장하지 않는다.
- executing/uncertain 상태에 대한 apply는 효과를 재실행하지 않는다. 같은 대상의 미해결 작업을 새 계획으로 우회하지 않는다. 명백히 효과 전에 실패한 경우와 실행 결과 불확실을 구분한다.
- Verification ownership: IF-acceptance; 같은 ID 동시 실행·다른 계획 ID/프로젝트·stale 파일·중단·결과 저장 유실·같은 대상 우회 금지.

## UF-CW-03 결과·복구

- GET status는 파일/원격 효과 없이 저장된 작업을 조회한다. POST check는 명시 읽기 대조와 작업 DB 정리만 한다.
- 정상 완료 receipt가 원본 plan/operation과 결부되면 DB 결과 기록을 복구한다. 메모는 원본 백업과 현재 daily 전체 target bytes를 확인해 적용 후 기록 유실을 복구할 수 있다. 늦은 외부 편집이 있으면 성공 과거 이력과 현재 문서를 구분하고 덮지 않는다.
- push는 명시 check에서 계획에 결부된 원격/ref/OID와 필요 시 upstream을 조회해 동일 결과만 확인한다. Git commit/fetch/pull 등의 native 효과 뒤 receipt까지 유실된 구간은 입증되지 않으면 uncertain을 유지한다. 자동 reset/clean/stash/rebase/원격 재전송은 하지 않는다.
- uncooperative 외부 편집/Git과 최종 검사 이후의 race는 완전 원자 보장 범위 밖이다. 다른 PC/프로세스의 원격 이력 변경과 hook 전체 효과도 단일 UUID로 원자화하지 못한다.
- Verification ownership: IF-acceptance; 정상 read-only status, receipt 누락/손상/외래 ID, memo full-file 대조·외부 편집·백업 실패, push remote readback, 입증 불가 Git 결과 유지.

## UF-CW-04 API/UI·호환

- POST `/api/v1/projects/<id>/writes/<git|memo>/plan` body `{action,payload}`.
- POST `.../apply` body `{plan_id,operation_id}`.
- GET/POST `.../operations/<operation_id>` (POST는 check). 결과 조회도 현재 project/checkout/WF_HOME binding을 확인한다.
- 기존 mutation URL도 서버가 같은 계약으로 실행한다. 기존 payload 요청은 legacy 단일 요청 호환이며 서버가 계획/작업을 생성한다. 요청 의도가 같다고 임의로 성공 UUID를 재사용하지 않는다. 불확실한 동일 대상은 새 요청으로 우회하지 못한다. 클라이언트 응답 유실 복구의 정식 지원은 사전 ID를 보관하는 새 흐름이다.
- 기존 UI의 버튼/레이아웃을 유지하고 전송 전에 ID를 보관한다. Git과 메모의 미확인 작업은 결과 조회로 연결한다. 수정 중 메모는 요청의 text/revision과 분리하고 이전 요청 결과가 최신 입력을 지우지 않는다. 새 저장 전 이전 미확인 요청을 먼저 대조한다. 브라우저 재로딩 시 서버 작업 목록에서도 ID를 찾을 수 있게 한다.
- Verification ownership: IF-acceptance; HTTP 실제 로컬 서버·guard·API ID-only·기존 URL 회귀, 실제 HTML의 날짜/동시 입력/응답 유실/초안 유지.

## Gate G2 — 연결 검토

| 책임 | UF | 연결 | 판정 |
|---|---|---|---|
| immutable 대상/payload/version | CW01→CW02 | saved plan/ID→claim | PASS |
| 하나의 실행·응답 유실 | CW02→CW03 | durable op/receipt/backup→대조 | PASS |
| 메모 CAS·날짜·사용자 원문 보존 | CW01→CW02→CW03 | full daily/memo SHA→기존 writer→readback | PASS |
| Git 효과·불확실 상태의 정직성 | CW01→CW02→CW03 | Git digest→기존 adapter→receipt/명시 readback | PASS |
| API guard·기존 입력/버튼·초안 | CW04→CW01~03 | project와 IDs/legacy adapter→response | PASS |

계약 내 UNCOVERED/REDUNDANT 없음. 이는 연결 검토이며 실제 구현·시험 수용은 별도 증거다. 남은 N2/N3/V4/V5/V7과 V3-06 USER_DEFERRED는 범위 밖이다. 일반 CI에 실제 사용자 자격을 제공하지 않는다.
