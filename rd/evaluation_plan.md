# I0~I7 이후 검증·실사용 수용 계획 v3 — P-022

## Metadata

- Source: `rd/wsflow_integrated_design.md`, `rd/wsflow_verification_plan.md`, I0~I7 계약, `logs/test_evidence/261018_P022/analysis_facts.json`.
- Domain: 로컬 업무 허브의 데이터 보존·외부 API 정합성·개발 도구 관측·재현성 검증. ML 모델 평가는 아니다.
- Date: 2026-10-18 (Asia/Seoul).
- 현재성: P-022는 계획만 작성한 이력이다. **P-023에서 V0/V1 9개를 명시 Windows/NTFS 프로파일로 수용했고 V2~V7의37개는 NOT_RUN**이다. 원46개 기준/분모는 유지한다. 현재 실행 결과는 `logs/test_evidence/261018_P023/validation.json`과 새 V0/V1 보고서를 따른다. 실계정/실제 도구는 이번에 실행하지 않았다.
- 정본: Stage 8의 현재 실행 기준은 본 문서다. 기존 v2 검증 계획과 I0~I7 계약은 원문·요구·과거 근거로 보존한다. 새 계획이 원 요구를 축소하지 않는다.
- P-022 당시 기준 소스: head `30238b912c487c6fec1f00f91be6c7befe4261d0`, 미커밋 소스 fingerprint `109e7c3f5a08`. commit만으로 현재 구현을 재현할 수 없으므로 V0에서 전체 소스 manifest와 함께 동결한다.

## 판단 근거

로컬 최신 회귀는 415시험/실패0/오류0/권한 skip1, Chrome82, I7 핵심·화면 음성 대조45+9다. 이는 API/도구 실서비스 수용과 같지 않다. 다음 작업은 기능 확장보다 재현 가능한 기준 확보, 실제 읽기 계약 확인, 한정된 외부 효과와 복구, 실제 도구 관측 순서가 적절하다. 이번 계획은 기존 판정·저장 모델을 재사용하는 단일 검증 경로이며 새 알고리즘/backend 후보를 선택하지 않는다.

V0 → V1을 기반으로 V2(Jira 읽기)·V3(Google 읽기)·V6(도구)을 분리해 진행할 수 있다. V4는 V3 뒤, V5는 V2 뒤이며 둘 다 V1의 필수 로컬 게이트를 통과해야 한다. V7은 앞선 필수 결과를 모아 실사용 후보를 판정한다. 이 의존 순서는 병렬 실행이나 외부 쓰기를 지금 승인한다는 뜻이 아니다.

## Metrics by Task

평가 과제는 “선언한 환경·대상에서 필수 수용 시나리오를 증거로 입증하기” 하나다. Primary는 아래 한 개만 사용한다. 안전 조건은 평균으로 상쇄하지 않는 별도 종료 게이트다.

| Category | Metric·방향 | Baseline | Target | Stretch | Unit |
|---|---|---|---|---|---|
| Primary | 필수 수용 시나리오 증거 확보율 ↑ | 0/46=0%: 이번 V0~V7 공식 수용 실행 0건 | 46/46=100% PASS | 46/46를 독립 환경에서 2회 연속 재현 | % |
| Secondary | 승인 밖 쓰기·다른 대상 변경 ↓ | P-021 합성/로컬 검증의 관측 위반 0; 실계정은 미측정 | 0 | 0 | 건 |
| Secondary | 유실·표시 중복·잘못된 최신/완료 판정 ↓ | P-021 해당 대조의 관측 위반 0; 새 실제 데이터는 미측정 | 0 | 0 | 건 |
| Secondary | 보호 대상 hash 불일치·cursor 교차 전진 ↓ | P-021 보호1074파일 불일치0; 이번 baseline2377파일은 종료 시 대조 | 0 | 0 | 건 |
| Secondary | 신선도/정상 baseline을 확인한 선정 변이 검출률 ↑ | I7 소스 기준54/54=100%; 이전 단계 전체 선정 목록은 아직 재검증하지 않음 | V0 확정 목록의100%, timeout/생존/stale/미실행/미검증0 | 같은 목록을2회 재현 | % |

증거 확보율의 0%는 기능 성공률이 0%라는 뜻이 아니다. 현재 실계정 정확도·도구 실제 인식률은 unknown이다. 시나리오 내부의 모든 필수 하위 조건/환경이 통과해야 그 시나리오를 1건 PASS로 센다. SKIP·UNKNOWN·PARTIAL은 분자에 넣지 않는다. 필수46개와 승인한 환경/대상을 V0에서 잠그며 실패 후 분모를 줄이지 않는다. 지원 범위 변경은 원 요구와 차이를 먼저 명시하고 변경 결정으로 추적한다.

**Acceptance Criteria:** Given V0에서 동결한 소스·환경·대상과 아래 데이터셋, When V1~V7의 승인된 검증을 수행하면, Then 필수46개가 모두 PASS이고 안전 위반0, 변이100% 검출, 설명 없는 skip0이어야 한다. 예상 capability skip은 이유와 영향 요구를 기록하고 그 미관측 능력까지 수용하지 않는다.

**Evaluation Dataset:** 다음 고정 profile을 V0 manifest에 넣는다.

- D-LOCAL: 기존 I0~I7 fixture, 실제 세 스킬 패키지의 P-019 조사 근거, 공백 경로·서로 다른 독립 checkout·worktree·동명/외부 의존·사용자 사후 편집. 원본은 읽고 변형은 전용 복사본에서 수행한다.
- D-JIRA: 별도 시험 프로젝트/에픽. 완료·다른 담당자·다른 CSC prefix·제목 필터 밖·부모 누락/없음 대조를 포함한 최소5개 업무. 두 번째 사이트를 확보하지 못하면 실제 다사이트 수용은 UNKNOWN으로 남긴다. 페이지 시험은 승인된 좁은 JQL과 작은 페이지 크기의 읽기 하네스로2페이지 이상 관측할 수 있으며, 이 통제 조건을 기록한다. 대량 티켓 생성으로 페이지를 만들지 않는다.
- D-GOOGLE: 전용 시험 계정/별도 Desktop client, 선택 writer 캘린더 A, 미선택 B, 접근 가능한 readonly R. 사전 준비한 종일·자정 종료·여러 날·반복 master/이동/취소·DST 경계와 일반 일정. 준비에 필요한 원격 생성도 별도 효과 목록에 포함한다.
- D-TOOLS: 공통 정본과 A/B 전용 정본, 선택/미선택 스킬 및 supporting file에 서로 다른 비밀 아닌 표식을 둔 시험 ROOT. Codex CLI와 Claude Code CLI 각각 A/B·새 세션3회, 최소12세션. desktop/IDE는 실제 선택한 surface별로 별도 행을 추가한다. 그 결과를 CLI에서 추정하지 않는다.
- D-OPS: 실제 규모를 확인해 비밀을 제거한 daily/패키지 표본, 합성 일정100/1000/10000개, 프로젝트1/10/50개, 기간7/42/62일. 수치 profile은 부하 진단용이며 실제 프로젝트 자료를 보았다는 주장이 아니다.

## V0 — 소스·요구·증거 기준 고정 (4개)

| ID | 실행·확인 | 통과 기준 |
|---|---|---|
| V0-01 | 깨끗한 후보 checkout 또는 소유 파일만 포함한 동결 패키지, 전체 source manifest·dirty diff·runtime 버전·TZ/Git/브라우저 확보 | 현행 manifest와 불일치0. 사용자 선행 변경을 무단 포함/폐기0. commit만 같다는 이유로 수용0 |
| V0-02 | CTX15/CAL16의 책임·지원 제한·실제 관측·하위 시나리오 연결 잠금 | 31개 요구 누락/중복 ID0. 부분 구현과 미실행 수용을 분리 |
| V0-03 | CTX-14의 Git 직접 실행과 공통 작업 ID 계약 차이(F-032)를 검토 | Git도 확장할지 별도 지원 계약으로 둘지 명시 결정1건. 원 요구의 미충족을 조용히 PASS 처리0. 확장을 고르면 구현+회귀 후 관련 수용 재개 |
| V0-04 | 단계별 핵심/화면 변이와 산출 경로 manifest, 과거 증거 보호 목록 확정 | 각 선정 변이에 요구/시험/정상 baseline이 연결됨100%. 과거 경로 재사용0. P-016 역사적 덮어쓰기 예외를 은폐0 |

V0-03은 문서 선택만으로 원래 CTX-14 전체 충족을 증명하지 않는다. 지원 범위로 분리하기로 하면 원래 넓은 요구의 GAP와 변경 근거를 유지하고, 해당 범위의 전체 수용 표시는 별도로 남긴다.

## V1 — 깨끗한 환경·CI 수용 (5개)

P-022 당시에는 CI 설정/실행 증거가 없었다. P-023의 `.github/workflows/verify.yml`과 `tools/ci/runner.py`로 실제 실행했다. 최종 source/정상2·통제실패3·artifact별 수용은 `logs/test_evidence/261018_P023/validation.json`을 따른다. 원 기준과 선언 환경·제품 alias 한계를 유지한다.

| ID | 실행·확인 | 통과 기준 |
|---|---|---|
| V1-01 | Windows 일반 사용자·Python3.10 계열에서 신규 후보 전체 회귀와 I1~I7 Chrome 실행 | 기준415시험 누락0, 실패/오류0, Chrome82개 이상 기존 ID 보존. 추가/대체 시험은 변경 근거 기록 |
| V1-02 | Windows·Python3.14.3 기준 환경에서 동일 검증 | V1-01과 동일. 다른 Python3.11~3.13까지 검증했다고 확대하지 않음 |
| V1-03 | V0 선정 I0~I7 변이를 현재 소스에서 정상 baseline 후 실행, vendor hash 게이트 | 검출100%, 살아남음/timeout/pattern/invalid/stale/미검증0. 모체 없으면 고정 manifest 검사는 수행하고 모체 비교는 별도 UNKNOWN |
| V1-04 | managed-copy의 일반 권한, 공백 경로/worktree, IANA TZ, 파일 잠금/교체·권한 기능 확인 | 지원 경로 위반0. WinError1314 symlink skip 최대 기존1건은 기능 범위와 함께 명시; Junction 성공을 symlink/도구 로딩 성공으로 승격0 |
| V1-05 | 불일치 소스/실패 시험/미검출 변이로 CI 게이트를 고의 실패시키고 artifact 검증 | 실패 job3종 각각 비정상 종료. 현재 소스/시험의 정확한 hash·run별 증거 업로드100%, 자격·업무 본문 유출0 |

CI는 live secrets 없이 합성 provider만 사용한다. Python3.10/3.14.3, Node·Playwright·Chrome, TZ 데이터·Git과 파일 시스템을 pin한다. Windows 외 Ubuntu/Python3.14 탐색 job은 별도 qualification이며 최초 지원 선언/필수46개에 포함하지 않는다. POSIX 자격0600·OS 잠금·링크 차이를 확인한 뒤 지원 매트릭스를 넓힌다. 전체 Python/OS 지원을 두 환경 통과로 일반화하지 않는다.

CI 증거 경로는 job/run/OS/Python별로 분리한다. `WSFLOW_RUN_EVIDENCE_DIR`, `WSFLOW_TEST_EVIDENCE_DIR`, `WSFLOW_GRAPH_EVIDENCE`, `WSFLOW_BROWSER_OUTPUT`을 지정하고 Playwright/runtime 경로를 기록한다. workspace 밖 evidence 경로를 가정하지 말고 runner의 상대 경로 제약을 확인한다. `safe.directory=*` 전역 설정을 추가하지 않고 필요한 fixture 경로만 실행 환경에 한정한다. 재실행은 이전 결과를 덮지 않는다.

## V2 — 실제 Jira 읽기·일일 수집 계약 (6개)

앱의 Jira REST 경로로 수행한다. 연결된 MCP가 읽기에 성공했다는 사실로 wsFlow 어댑터를 수용하지 않는다. 원격 업무 쓰기는 하지 않으며 배치는 복사한 시험 WF_HOME에만 결과를 기록한다.

| ID | 실행·확인 | 통과 기준 |
|---|---|---|
| V2-01 | 사이트별 exact 자격·myself accountId/timeZone 및 다른 사이트 미일치 | 최소2 named site에서 계정 기대값 일치100%, 잘못된 자격 fallback0. 사이트가1개면 다사이트 부분은 UNKNOWN |
| V2-02 | CSC 있는/없는 JQL, 에픽 완료·다른 담당자·다른 prefix/필터 밖 제목 수집 | 사전 표본5개 이상 key 집합 일치100%, 임의 제목 JQL 필터0 |
| V2-03 | 실제 fields 응답·parent 생략/null·status/type/updated/assignee·Epic hierarchy 확인 | 형식별 최소1개 관측·스키마 적합성 확인. 정상 응답을 unknown으로만 남기는 경우도 결함/지원 차이로 기록하고 전체 성공 처리0 |
| V2-04 | 실제2페이지 이상 수집과 종료 token/isLast 관측 | 고유 key 누락/중복 집계0. 잘린/중복/모순 응답의 실패 및 shadow watermark 전진0. 통제 변형은 실제 공급자 원래 응답과 구분 |
| V2-05 | 계정 TZ/offset의 경계 수집 및 staged batch | 날짜/JQL 경계 기대값 일치100%, 7단계 정상 완료, 메모 byte 손실0. 실패 단계별 watermark 전진0 |
| V2-06 | 읽기 권한/인증 실패와 검색 반영 지연 측정 | 실패를0·완전 성공으로 표시0, 이전 상태 보존. 지연은 최소10회 관측 분포와 환경 기록; 2분 내 전부 반영된다고 가정0 |

권한 오류/종료 타입/잘림의 모든 형태가 실서비스에서 자연 발생하지 않으면 실제 read contract와 통제된 fault replay를 분리해 기록한다. 후자의 PASS를 “실제 서버가 해당 오류를 발생시켰다”로 쓰지 않는다. 실제 읽기의 parent 생략 등에서 차이가 나오면 보충 조회·완전성 판정의 개선안을 먼저 제안한다.

## V3 — 실제 Google 연결·선택·증분 읽기 (7개)

원격 event는 이 단계에서 수정하지 않는다. OAuth 연결/해제·동의·revoke는 자격 상태를 바꾸는 별도 효과로 실행 대상에 기록한다.

| ID | 실행·확인 | 통과 기준 |
|---|---|---|
| V3-01 | 전용 Desktop client의 브라우저 OAuth·읽기 동의/취소·PKCE/state/callback | 연결1회·취소1회 정상, state 재사용/잘못된 callback 대조 효과0, 비밀 로그0 |
| V3-02 | 실제 calendarList·A/R 선택·B 미선택, accessRole/scope 표시 | 선택 집합 일치100%, readonly를쓰기 가능으로 표시0, 다른 계정/캘린더 혼입0 |
| V3-03 | collection2페이지 이상과 마지막 syncToken | 모든 페이지 전 cursor 확정0, key 집합/최종 cursor 경계 일치100% |
| V3-04 | 사전 준비/승인된 외부 변경의 incremental 수신 및 선택 기간 변경 | 변경/취소 누락0, 월/주 전환으로 collection profile/cursor 혼용0 |
| V3-05 | 종일/자정/여러 날/반복 이동·취소/DST occurrence | 각 형식 최소1개, 날짜·종료 제외·취소 미표시·요약/상세 건수 불일치0 |
| V3-06 | refresh·권한 상실·연결 해제·다시 연결·재시작 | local/outbox/타 계정 상태 유실0, 권한 없는 전송0, 마지막 성공/불완전 표시 정확 |
| V3-07 | 시험 mirror 사본의 무효 token으로 410 조건 확인 및 전체 재수집 | 실제410 관측과 통제 fault 결과를 분리. 새 세대 완성 전 mirror 교체0, local/pending/ID 손실0. 실제410 미관측이면 live 하위 항목 UNKNOWN |

Desktop client 유형/API 활성화/동의 화면의 테스트 사용자·조직 제한·허용 scope와 refresh 정책은 실제 Cloud 설정에서 기록한다. 설치형 앱의 권한 확장은 기존 토큰에 조용히 덧붙이지 않고 쓰기용 전체 동의를 확인한다. [Google Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)

410의 공식 설명은 원격 event mirror 재수집 근거다. 이를 local event/outbox까지 비우는 작업으로 해석하지 않는다. [Google 증분 수집](https://developers.google.com/workspace/calendar/api/guides/sync)

## V4 — 실제 Google 쓰기·양방향·불확실 복구 (8개)

V3와 필수 로컬 게이트 후, 전용 A와 exact event ID를 효과 목록으로 검토한 범위에서만 수행한다. 미선택 B·readonly R은 음성 대조 대상이다.

| ID | 실행·확인 | 통과 기준 |
|---|---|---|
| V4-01 | 새 직접 일정의 Google 저장·기존 local event 추가 | 각각1개, remote ID 전송 전 저장100%, 왕복 표시 중복0 |
| V4-02 | wsFlow의 연결 일정 수정 | 현재 etag 조건부 갱신, 대상 외 필드/일정 변경0 |
| V4-03 | Google UI/승인된 외부 수정 → 증분 수집 → local pull | local revision/관리 필드 기대 일치100%, 프로젝트 연결 유실0 |
| V4-04 | 양쪽 수정·오래된 etag·명시 local/remote 해결 | conflict 양쪽 보존100%, 확인 없는 overwrite0; 두 해결 경로 각각1회 |
| V4-05 | 실제 원격 성공 뒤 응답 수신 경계 유실·재시작·reconcile | 같은 ID로 대조, 추가 원격 객체/불확실 재전송0. 수신 유실은 통제 장애로 명시 |
| V4-06 | 화면 숨김/표시·명시 Google 삭제·삭제 후 보관 복구·미지원 event | 숨김 DELETE0, 승인한 exact ID만 삭제, 삭제 일정 자동 부활0, 반복/참석자/특수 event 변경0 |
| V4-07 | 계정·선택·scope/role 변경과 전송 중 새 local 편집 | 다른 계정/캘린더 전송0, 최신 입력 손실0, 작업 intent 임의 변경0 |
| V4-08 | offline/error/queued/syncing/uncertain·재시작·새 mirror 세대 | 상태별 전송/대조 규칙 일치100%, local/outbox·Jira watermark 손실/교차 전진0 |

실제 ID 충돌/412·response loss는 시험용 클라이언트 경계에서 통제할 수 있다. 원격 요청이 실제 수행됐는지 서버 조회로 확인하고 실제 공급자 오류와 주입 오류를 구분한다. TLS 검증 해제·범용 프록시로 자격을 노출하는 방식을 쓰지 않는다. insert는 수정의 If-Match와 같은 조건부 버전 연산이 아니므로 고정 ID와 확인을 사용한다. [Google 버전 검사](https://developers.google.com/workspace/calendar/api/guides/version-resources)

## V5 — 실제 Jira 승인 쓰기·대조·로컬 복구 (5개)

| ID | 실행·확인 | 통과 기준 |
|---|---|---|
| V5-01 | 지정 Epic의 실제 현재 담당자·전체 순번 입력으로 신규1개 생성 | payload/assignee/parent/순번 일치100%, 생성/검색 가시성 결과 구분, 검색 미확인 때문에 재생성0 |
| V5-02 | 메모→마감 초안→선택 승인→시험 issue comment | 승인한 항목만 효과, operation property/내용 대조 가능, 비승인/Done 전이0 |
| V5-03 | 원격 성공 뒤 수신/로컬 결과 기록 유실·reconcile/repair | operation UUID별 원격 효과1회, 확인 전 재전송0, 로컬 복구의 원격 쓰기0 |
| V5-04 | stale plan/다른 계정·범위·동시 예약·외부 순번 경합 | 클라이언트 새 payload/바뀐 대상 전송0. 권한·색인 밖 순번을 전역 유일 보장으로 표시0 |
| V5-05 | legacy approved-only·불일치 marker·읽기 실패 | 자동 성공/재승인0, 수동 확인 대상 유지, 원본 승인 JSONL/state 보존 |

Jira operation property는 암호학적 증명이 아니다. 대조 가능한 권한과 실제 API 응답 형식을 확인한다. 기존 본문 일치만으로 성공을 추측하지 않는다. 검색이 즉시 반영된다고 가정하지 않는다. [Atlassian Search and Reconcile](https://developer.atlassian.com/cloud/jira/platform/search-and-reconcile/)

## V6 — 실제 Codex·Claude 세션 수용 (6개)

프로필 버전은 사용자 선언이고 receipt는 수동 제출이므로, 동일 환경의 실제 도구 출력/진단 자료와 기대 파일 그래프를 함께 대조한다. 자연어 “읽었다” 응답만으로 tool observation을 증명하지 않는다.

| ID | 실행·확인 | 통과 기준 |
|---|---|---|
| V6-01 | Codex CLI 새 세션 A/B의 cwd/공통·전용 지침 | 각3세션, 기대 출처 관측100%, B 표식/경로의 A 혼입0, 확인 불가 범위 unknown |
| V6-02 | Claude Code CLI에서 동일 검증, 외부 import 승인/거부와 공백 경로 | 각3세션, 참조 해석/거부 표시 정확, 거부를 ready/read로 표시0 |
| V6-03 | 선택/미선택 스킬·supporting file·동명/도구 예약 이름 | 선택 패키지 기대 집합 일치100%, 미선택을 wsFlow가 노출0. 도구 예약 이름으로 로딩이 안 되면 파일 확인 성공과 분리 |
| V6-04 | 개인·전역·plugin 등 도구별 추가 범위 | 관측한 출처만 기록, 미관측을 scope_complete=true로 승격0. 전역 정책 준수/격리 전체 증명 주장0 |
| V6-05 | 공통/사본/support·tool profile·checkout/worktree/새 준비 변경 | 기존 receipt stale 판정 누락0, 예전 완료 배지 재사용0 |
| V6-06 | 실제 출력 증거 제출·부분 관측·자체 보고·근거 변경·독립 대조 | origin/kind·당시 hash/시간/binding 일치100%, 자체 보고 승격0, 제출 진위의 독립검증 여부를 명시 |

최소12세션은 V6-01/02의 합계이고 V6-03~06에 같은 세션을 재사용할 수 있다. 허용하는 실제 tool/version/surface를 행별로 고정한다. desktop/IDE 추가 시험의 결과가 없으면 해당 surface는 UNKNOWN이다.

[Codex AGENTS 문서](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [스킬 탐색 위치](https://learn.chatgpt.com/docs/build-skills), [Claude import](https://code.claude.com/docs/en/memory), [Claude 스킬](https://code.claude.com/docs/en/skills)을 실행일 버전에서 다시 확인한다. 도구 문서의 지원과 해당 호스트의 실제 인식은 별도 근거다. 현재 스킬 backend인 managed-copy의 파일 일치로 이 단계를 대체하지 않는다.

## V7 — 운영·성능·복구·최종 실사용 후보 (5개)

| ID | 실행·확인 | 통과 기준 |
|---|---|---|
| V7-01 | D-OPS 읽기·월/주·상세·context 조사 성능 | 각 profile30회, cold/warm p50/p95·peak RSS·I/O·호스트 기록100%. 불완전/오류를 빠른 성공으로 분류0 |
| V7-02 | DB/파일 백업·정상 종료/실행 중 중단·시험 복원 | backup2세트/복원2회, 사용자 본문·버전·pending·receipt/작업 ID 손실0, 복원 과정 원격 재전송0 |
| V7-03 | registry/ROOT/checkout 이동·UID 중복·user-data 경로 변경 | 자동 잘못된 재결합0, 구 데이터 보존, 지원하지 않는 이동은 명시 blocked/unknown. Google 자격은 새 OS 사용자에서 재인증 경계 확인 |
| V7-04 | Git·Jira batch·메모·Google·context를 연결한 날짜별 E2E | 선택 날짜·snapshot 건수 불일치0, 단계 실패 watermark·다른 cursor 전진0, 초안/사용자 파일 손실0 |
| V7-05 | 승인된 소규모 pilot와 수용 검토 | 영업일5일, 사용자 지정 프로젝트2개 이상, unresolved/uncertain 목록 매일 확인. 데이터 손실/승인 밖 효과0, 필수46항목 증거100% 및 남은 제한 명시 |

V7-01은 우선 측정 게이트다. 초기 SLO 제안은 일정1000/프로젝트10/42일의 warm 로컬 표시 p95≤2초, cold≤5초(Stretch 각각1/3초)다. 아직 측정 baseline이 없으므로 제품 보장/출시 PASS 기준으로 확정하지 않는다. 첫 측정 후 실제 규모·호스트와 함께 SLO를 사전에 확정하고, 실패한 값을 숨기기 위한 사후 기준 변경은 하지 않는다. 네트워크 시간과 순수 로컬 시간을 분리한다. 장기 raw/receipt/예약의 보존·압축 정책은 미구현이며 원본을 삭제하는 부하 시험을 하지 않는다.

실제 정전·SMB/NFS·악의적 비협조 편집의 모든 race는 별도 qualification이다. V7 통과로 다중 파일/외부 API 전체 원자성이나 원격 exactly-once를 주장하지 않는다.

## REQ / UF Mapping

전체31개 ID와 구현/증거/남은 범위의 기계 목록은 `logs/test_evidence/261018_P022/requirements_status.json`, 사람이 읽는 현황은 새 최종 분석 보고서의 요구표다.

| REQ 범위 | UF/IF 근거 | 연결 시나리오 |
|---|---|---|
| CTX-01~03·05·12 | I0 계약/context inventory·I5-U1~3·I6-U1/4 | V0-02,V1-04,V6-01~05,V7-03 |
| CTX-04·07·10 | I5-U1~5 | V1-03~04,V6-03,V7-02~03 |
| CTX-06·14 | I1 계획/claim·I4-U2~6·I5-U2~3; Git GAP F-032 | V0-03,V4-01~08,V5-01~05,V7-02/04 |
| CTX-08~09·11 | I6-U1~5 | V6-01~06 |
| CTX-13·15 | I0 API·I7 UF01~05 | V1-01~05,V2-01~06,V7-04 |
| CAL-01~05·11·16 | I0 날짜/I2 UI/store | V1-01~03,V3-05,V7-01/04 |
| CAL-06·10·12 | I3-U1~6/I4-U5 | V3-01~07,V4-01/03,V7-04 |
| CAL-07~09·13~15 | I4-U1~7/I3 token/mirror | V3-06~07,V4-01~08,V7-02/04 |

## Evidence Pack Connection

후속 각 실행은 새 run_id 폴더에 `manifest.json`, `scenarios.jsonl`, `metrics.json`, `hashes.json`, `environment.json`, redacted provider 결과·tool 관측·CI job URL/상태·재현 명령을 보존한다. 이번 P-022의 문서 검증 증거와 섞지 않는다.

필수 필드: req_id/scenario_id/run_id, code commit+dirty manifest, timestamp+offset, exact tool/version/surface/host/Python/OS/TZ/Git/DB/browser, expected/observed, PASS/FAIL/SKIP/UNKNOWN/PARTIAL, fault_origin(real_provider/controlled_injection/synthetic), account/calendar/issue의 비밀 아닌 별칭, 승인 범위·effect ID 참조, source/target revision·etag의 hash, cursor 전후 hash·complete, duration, evidence 경로/sha256, 보호 hash 차이 및 정리 결과.

실제 업무 본문·토큰·OAuth code/refresh·API Authorization·원시 session 로그는 저장소에 복사하지 않는다. 허용된 사용자 데이터 영역에 제한 보관하고 redacted 출력/해시만 참조한다. request path/query에 포함된 토큰도 마스킹한다. 앱 token·Google token·Jira credentials를 서로 대체하지 않는다.

외부 효과를 실행하기 전 대상 sheet에서 계정·캘린더/프로젝트·exact parent/issue/event·허용 create/update/delete·정리 방법·상한을 구체화한다. 초기 제안 상한은 Google 생성 event10개/쓰기40회, Jira issue5개/comment5개이며 V0에서 실제 대상과 함께 확정한다. 효과가 불확실하면 상한 이내여도 재전송하지 않고 대조한다. Google 정리는 생성한 exact ID만 별도 명시 삭제; Jira는 Done 전이를 앱 시험/정리 수단으로 사용하지 않고 소유자가 시험 자료를 확인·정리한다. 이미 승인된 exact 범위는 반복 승인하지 않으며, 새 대상/범위 확대는 별도 결정이다.

실제 provider write가 없는 V1 CI와 live smoke를 분리한다. 일반 PR job에 사용자 credentials를 제공하지 않는다. 신규 CI/하네스·수정이 필요하면 다음 코드 구현 요청에 별도 변경·회귀·음성 대조를 연결한다. P-022는 그러한 파일을 만들지 않았으며 P-023의 후속 실행 요청에서 필요한 CI/하네스를 추가했다.

## 종료 판정과 실패 라우팅

V0~V7의 필수46개 통과는 **명시한 환경·계정·scope의 실사용 후보**를 뜻한다. 원 요구 GAP(F-032), 실측 불가 범위나 안전 조건 위반이 남으면 “전체 설계 완료”로 표현하지 않는다. actual provider 미관측 항목을 합성 PASS로 대체하지 않는다. FAIL은 발견 대장과 최소 재현/교정 계획, UNKNOWN/SKIP은 필요한 계정/권한/관측 수단과 재실행 조건에 연결한다. 코드를 고쳐야 하면 그때 별도 구현을 요청받아 진행한다.

P-022의 첫 권장 범위였던 **V0+V1**은 P-023에서 명시 프로파일로 수용했다. 다음 범위는 **V2·V3·V6**이다. 실행 가능한 동결본과 CI 증거 기반을 먼저 만들고, 사용자 시험 자격/도구가 준비된 뒤 V2/V3/V6를 수용한다. 이 문서의 통과 수치는 목표이며 이번에 새로 실행한 기능 결과가 아니다.


## P-023 실행 overlay — 기준은 보존하고 결과를 연결

최종 결과는 `output_docs/261018_1825_v0_v1_verification_report.md`와 `logs/test_evidence/261018_P023/acceptance_scenarios.json`이다. V0/V1은9/9 PASS, 전체 필수46개는9/46이며 나머지37개는 NOT_RUN이다. 위 Metrics 표의0/46은 P-022 baseline이고 현재 관측은 P-023 metrics.json이다. 선정208변이를 네 프로파일에서 각각 fresh baseline 후 수용했으며 모든 코드/요구를 완전히 검증한 coverage로 확대하지 않는다.

실행 source commit `01b64dbd5098da1ca811b29fd34840acb8116c35`, normalized fingerprint `f55919f751535e9b3f2705aa80d2326989dca5d9d938ca2bc7b72b46bf232717`, `frozen7/freeze.json` raw archive, 각 env/job/artifact hash를 연결한다. 지원은 canonical full path NTFS의 선언 Windows/Python 프로파일이다. 제품 short alias F-042와 부하 잠금 F-039는 미수용 한계, CTX-14는 F-032/F-041로 PARTIAL 유지다. 실제 계정/도구·운영 수용 기준과 기존 승인 범위 규칙은 유지한다.

## P-024 V2 실행 overlay·잔여 검증 조건

새 결과는 `output_docs/261018_1957_v2_verification_report.md`, 46개/31요구 overlay는 `logs/test_evidence/261018_P024/acceptance_scenarios.json` 및 `requirements_status.json`이다. 기존 기준/이전 결과는 수정하지 않는다. V2-04/05만 현재 한 계정 Etc/GMT·schema4 시험 WF_HOME에서 PASS, 나머지4개 PARTIAL이다. 전체11 PASS/4 PARTIAL/31 NOT_RUN이며, 제품 코드/기존 시험/vendor/CI는 동일 source다. 실제 provider와 fault·clock·PAGE 변형을 구분한 P024 증거를 따른다.

사용자가 추가 사이트/지정 표본 없음으로 답했으므로 V2-01 다사이트는 UNKNOWN 유지한다. V2-02는 현재15개 표본의5개 기대집합 대조 성공 외 실제 CSC 양성·완료 Epic이 필요하다. V2-03은 실제 parent:null과 추가 hierarchy/custom-field 형식을 확보한다. V2-06의 기존 이슈10회 가시성은 새 변경 색인 지연이 아니므로 독립 변경 시각·최소10회 관측 분포·관측 기한을 사전 고정해 재검증한다. 원격 쓰기 없는 현재 범위로 준비하지 못한 입력을 성공으로 바꾸지 않는다.

F-044의 schema3 운영 전환은 기존 wfinit.build_state를 재사용한 사본 성공과 구분하여 계획/사전대조·백업·업무/roots/watermark 보존·복구를 검증한다. F-045는 DST 시작/종료·반시간 이동·gap/fold에서 UTC 경과시간 기준과 독립 대조하고 실제 Jira JQL 해석을 확인한다. 현재 제품은 수정하지 않았으며 DST 수용을 선언하지 않는다. F-046/047의 하네스 labels 범위·부분 보고·외부 deadline은 새 live 재사용 전에 보완한다. 원 기준 숫자를 줄여 미관측 조건을 없애지 않는다.

판단 근거: P024 실제 읽기82회·표본15·23페이지/46키·7단계 shadow 및 memo bytes·정확한 실패 단계의14장애·회귀415·제품45/하네스18변이를 연결했다. 독립 검토 후 하네스만 보강했고 현재 실제 프로파일의 범위와 새 제품 발견을 분리했다. V3/V6 및 이후 V4/V5/V7 검증 순서·원격 효과 승인 경계는 유지한다.


## P-025 F044/F045 보강 결과

평가정본은 `logs/test_evidence/261018_P025/validation.json`과 `output_docs/261018_2318_f044_f045_implementation_report.md`다. 이전수용평가는당시소스의이력으로보존한다. 이번은원격업무쓰기0/실계정재호출0의격리로컬수정검증이다.

F044: unknown/None/roots/watermark/last_batch값과 byte backup, client조작/TTL/binding/stale/CAS/동시적용/쓰기전후실패/DB응답유실/status행렬을실측한다. 실제schema3사본+기존raw의오프라인replay도7단계/메모bytes/원본13파일유지로대조했다. F045: UTC정수독립oracle700×4offset과DST/반시간/자정6배치, 별도1,500시각/7,500대조를통과했다. 현지감산/overlap/분내림/단위/offset/인용등보호제거를검출했다.

전체Python432(F0/E0/skip1), browser82+5, CI자체15, 최종선택변이79(기존I7 45/신규32/UI2)와baseline/source/spec/runner hash를대조한다. 초기실패·생존·패턴오류는모두보존하며잘못된setup을killed로집계하지않는다. 최종R03패턴한정은제품/시험무변경bridge와새동결변이실행으로검증한다. 기존CI선택208전체나실제CI를이번에실행했다는주장은하지않는다.

남은수용: 새 epoch JQL의공급자해석·실계정동일키/페이지/UTC경계·DST계정표본, 독립변경시각이있는색인지연, 실제원본전환, 새source CI. V2다사이트/CSC양성/완료Epic/parent:null 및F046/047조건은유지한다. 미관측은UNKNOWN/NOT_RUN으로남기고P024의11/46을새소스수용률로재사용하지않는다.
