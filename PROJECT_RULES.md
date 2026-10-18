# PROJECT_RULES.md — wsFlow (프로젝트 바인딩)

> GLOBAL_RULES.md Rule 10이 요구하는 프로젝트 귀속 파일. **프로젝트 특정 사실은 전부 여기에 있고, GLOBAL_RULES.md에는 없다.**
> 우선순위: 프로젝트 사실은 본 파일 > 과거 리뷰독. 프로세스 규율(로그·ID·검증)은 GLOBAL_RULES가 우선.
> 작성: 2026-09-11 (P-001)

## 1. 프로젝트 정체

- **대상**: wsFlow — **wsManager**(로컬 웹 workspace 관리자)와 **jiraWorkFlow**(Jira 개인 업무 기록 순환)를 합친 단일 로컬 도구. 브라우저에서 프로젝트 폴더를 고르면 그 폴더의 git 상태·규칙 준수와 **매핑된 Jira 태스크·날짜별 메모·마감·신규 태스크**를 한 화면에서 다룬다.
- **두 모체와의 관계**: 두 저장소는 **동결**되고 사라지지 않는다.
  - `wsManager` (원년 2026-09-04) — 코드를 이관했다. 유효 결정은 §5 에 **재선언**했다(사본이 아니라 재선언 — 정본은 이 파일이다).
  - `jiraWorkFlow/jiraWF` (원년 2026-08-25) — 결정적 파이프라인 스크립트를 `vendor/jira_worklog/` 에 **무수정 이관**했다. 그 저장소는 public·단독 설치용으로 계속 살아 있으며, `vendor/` 를 고칠 일이 생기면 **거기서 고치고 다시 가져온다**(D-003).
- **구조**: 브라우저(단일 `static/index.html`) → HTTP JSON API(`wsflow/server.py`, 표준 라이브러리 `http.server`, 127.0.0.1 + F-001 가드) → git 어댑터 · 규칙 스캐너 · 관리 목록(`projects.json`) · 배포 · **Jira REST 어댑터** · **쓰기 단일 통로**.
- **범위 외**: 원격 배포·다중 사용자·앱 사용자 계정 인증(로컬 전용). 폴더·파일 삭제. 파괴적 git 명령. git init·GitHub 저장소 생성. **Jira 완료(Done) 전이**·자동 구현·할 일 유형 분류·팀 롤업·Jira 양방향 자동 동기화·git 커밋 자동 수집 — 마지막 다섯은 `jiraWF/DESIGN.md` §하지 않는 것을 그대로 승계한다. 융합했다고 풀리지 않는다. Google 외부 계정 OAuth와 일정 동기화는 I3/I4에서 로컬·합성 provider 범위를 구현했고 실계정 수용은 미검증이다. 현재 통합 상태는 §19를 따른다.
- **언어/빌드**: Python 코드의 import는 표준 라이브러리, Git은 git CLI. Calendar/Jira IANA 시간대에는 시스템 DB 또는 tzdata가 필요하며 Windows는 tzdata 설치를 전제한다. Node/Playwright/browser는 시험 전용이다. Python3.10은 EOL 호환 시험용. 빌드 없음. 시험은 `python tests/run_tests.py`, 실행은 `python run.py` 또는 `run.cmd`.

## 2. 정본 문서 (canonical documents)

| 역할 | 파일 | 비고 |
|---|---|---|
| 융합 설계 정본 | `review_docs/260911_1350_wsmanager_jiraworkflow_fusion_review.md` (wsManager 저장소) | P-007 검토·결정. §9·§10 이 이 프로젝트의 출발점 |
| 워크플로 설계 정본 | `jiraWorkFlow/jiraWF/DESIGN.md` | 무엇을 보장하고 무엇을 하지 않는가. `vendor/` 코드의 근거 |
| API 정본 | `wsflow/server.py` 모듈 docstring | 라우트 표. 코드가 정본이며 문서는 코드를 인용한다 |
| Jira 조회 정본 | `wsflow/jiraapi.py` | 엔드포인트·JQL 3종·페이지 넘김·필드 목록 |
| Jira 쓰기 정본 | `wsflow/jirawrite.py` | **유일한 쓰기 통로.** 승인 선기록·재사용 차단·완료 전이 거부 |
| WF_HOME 정본 | `wsflow/wfhome.py` | 매핑 판정·태스크 분류·진행 메모(INV-1 보호) |
| 설정 작성 정본 | `wsflow/wfinit.py` | 재실행 안전·사이트 공통값 재사용·`.gitignore` 게이트 |
| 티켓 생성 정본 | `wsflow/newtask.py` | 제목·CSC·CSU 검사 후 승인 게이트 경유 생성 |
| 범위 설정 정본 | 각 `WF_HOME/.workflow/state.json` | 프로젝트 키·CSC·에픽·제목 필터·`issue_type_map`. **wsFlow 는 사본을 갖지 않는다**(D-004) |
| 발견·결정 대장 | `graph/edges.csv` | F-/D-/M- 상태의 단일 정본 (GLOBAL Rule 11) |
| 기계 검사 설정 | `graph/checks_config.json` | 본 프로젝트 귀속 질의 (GLOBAL Rule 14) |
| 작업 로그 정본 | `logs/files.jsonl`, `logs/prompts.jsonl` | append-only, `schema:"v2"` |
| 시험 증거 | `logs/test_evidence/<run_id>_evidence.json` | 문서는 수치 대신 이 경로를 인용 |
| 통합 목표 설계 v2 | `rd/wsflow_integrated_design.md` | P-013 당시 목표 원문 보존. 이후 I0/I1/I2 결과는 §11~13과 단계 계획 참조 |
| 통합 검증·수용 계획 v2 보존본 | `rd/wsflow_verification_plan.md` | CTX/CAL 원 요구·기존 시나리오 보존. 후속 실행은 v3 평가 계획 우선 |
| 단계별 구현 계획 v2 | `rd/implementation_plan.md` | I0~I7의 순서·의존·종료 게이트. 이전 S/K 단계 순서를 대체 |
| 최종 통합 분석 v2 보존본 | `output_docs/261016_1917_integrated_final_analysis.md` | P-013 시점의 설계 대비 분석·후속 구현 기준 원문 보존 |
| 현행 통합 분석 v3 | `output_docs/261018_1715_i0_i7_final_analysis.md` | P-022 I0~I7 로컬 구현·증거·잔여 GAP/실사용 수용 분석 |
| 현행 Stage 8 검증·수용 계획 v3 | `rd/evaluation_plan.md` | V0~V7 필수46개·실계정/실제 도구/CI/운영 수용의 실행 정본; 계획/NOT_RUN |
| I0 구현 API 계약 | `rd/i0_api_contract.md` | P-014. 버전 있는 읽기 API·집계·scope·실패·호환성 계약 |
| 함수/검토 계약 | `rd/uf.md`, `rd/uf_if_coverage_review.md` | I0 블록 보존, I1/I2 별도 계약과 연결 검토 참조 |
| I0 구현 결과 | `output_docs/261016_2012_i0_implementation_report.md` | P-014 현재 구현·로컬 검증 결과. P-013 보고서를 덮어쓰지 않음 |
| P-011 개발 컨텍스트 설계 보존본 | `rd/workspace_context_design.md` | 원 요구·세부 계약 유지. 현재 목표·순서는 P-013 통합 문서가 우선 |
| P-011 검증·수용 계획 보존본 | `rd/workspace_context_verification_plan.md` | 과거 근거. 새 요구/발견/단계 추적은 통합 검증 계획 참조 |
| P-010 상세 분석 보존본 | `review_docs/261016_1808_project_design_implementation_analysis.md` | 원문 보존. P-011 보존 해시 근거는 `logs/test_evidence/261016_P011_context_inventory.json` |
| P-011 최종 분석 보존본 | `output_docs/261016_1852_final_design_analysis.md` | 원문 보존. P-013이 새 최종 보고서 |
| P-012 캘린더 검토 보존본 | `review_docs/261016_1909_calendar_feature_review.md` | CAL-01~CAL-16 원 요구 유지. P-013에서 정식 목표에 통합 |
| V0/V1 수용 정본 | `output_docs/261018_1825_v0_v1_verification_report.md` | P-023 최종 source·선언 환경·실제 CI·잔여 GAP. P-022 분석은 이력 보존 |
| V0/V1 동결·실행 계약 | `rd/v0_v1_contracts.md` | G2/G4·Git 지원 범위·격리/신선도·CI 수용 |

## 3. 원년 (ledger epoch)

- **원년: 2026-09-11** — 저장소 생성일 = 원년. **소급 등록하지 않는다.** 두 모체의 대장(`wsManager/graph/edges.csv`, `jiraWorkFlow/graph/edges.csv`)은 각자 자리에 이력으로 남는다.
- P-ID는 P-001부터. 발견·결정은 발생 즉시 대장에 등록한다.

## 4. 도메인 불변식

런타임 불변식 **INV-1~6** 은 `vendor/jira_worklog/loop_checks.py` 가 배치 실행 중에 검사한다(정본: `jiraWF/DESIGN.md` §런타임 불변식). 이관과 함께 그대로 승계한다.

| ID | 불변식 | 검사 주체 |
|---|---|---|
| INV-1 | 일일 배치는 진행 메모 구획을 덮지 않는다 | `loop_checks.py` + **wsFlow 의 메모 편집기도 이 책임을 진다**(해시 낙관적 잠금·사본, S5) |
| INV-2 | watermark 는 단조 증가한다 | `loop_checks.py` |
| INV-3 | 승인 기록 없는 push 는 존재하지 않는다 | `loop_checks.py` + **`<WF_HOME>/.workflow/approvals.jsonl` 선기록 강제**(D-006·D-015). 검사가 아니라 **순서**로 막는다 — 기록을 못 찾으면 REST 호출이 일어나지 않는다 |
| INV-4 | 일지가 언급한 이슈 키는 원본에 실재한다 | `loop_checks.py` |
| INV-5 | 수집 범위가 그 폴더의 스코프와 일치한다 | `loop_checks.py` |
| INV-6 | CSC 를 쓰는 폴더는 CSC 미지정 조회를 함께 제출한다 | `loop_checks.py` |

wsFlow 고유 불변식:

| ID | 불변식 | 현재 상태 |
|---|---|---|
| INV-W1 | 상태 변경 API 는 토큰 없이 실행되지 않는다 | `tests/test_server.py::GuardTests` (음성 대조 확인) |
| INV-W2 | 문서가 인용한 코드 원문(`file:line` + 인용문)은 실재한다 | `checks_config.json` `quote_exists` |
| INV-W3 | `vendor/jira_worklog/` 의 파일은 `jiraWF` 원본과 바이트 동일하다 | P-021 `tools/check_jira_vendor.py` 및 배치 manifest hash gate (U-005) |
| INV-W4 | 메모 저장은 해시가 어긋나면 **아무것도 쓰지 않는다** | `tests/test_wfhome.py::MemoTests::test_stale_hash_is_refused_and_nothing_is_written` (음성 대조 확인) |
| INV-W5 | 걸러낸 항목(목록 밖 CSU·제목 필터·CSC 미지정)은 버리지 않고 센다 | `tests/test_wfhome.py::ClassifyTests` |
| INV-W6 | 설정 재작성은 `watermark`·`roots` 를 보존한다 | `tests/test_wfinit.py::BuildStateTests` (음성 대조 확인) |
| INV-W7 | 범위 밖 제목으로는 티켓을 만들지 않는다 | `tests/test_wfinit.py::NewTaskTests` — 판정은 `vendor/new_title.py` |

## 5. 유효 결정 대장 요약 (정본은 graph/edges.csv)

| ID | 결정 | 출처 |
|---|---|---|
| D-001 | 스택은 Python 표준 라이브러리 + 단일 HTML (외부 의존 0). REST 도 `urllib` 로 한다 | wsManager D-001 재선언 |
| D-002 | **Jira 접근은 wsFlow 서버가 REST 로 직접 한다**(안 A). MCP 경유·에이전트 경유가 아니다 | wsManager D-012 승계 |
| D-003 | 결정적 파이프라인(`aggregate`·`render_daily`·`loop_checks`·`memo_intake`·`new_title`·`close_verify`·`merge_targets`·`draft_lint`·`jira_fields`)은 `jiraWF` 에서 **무수정** 이관하고 `vendor/jira_worklog/` 에 격리한다. 여기를 고치면 사본이 둘이 되므로, 고칠 일은 `jiraWF` 에서 고쳐 다시 가져온다 | 신규 |
| D-004 | **WF_HOME 판정은 프로젝트 폴더 안의 `.workflow/state.json` 존재로 한다.** 있으면 그 프로젝트는 Jira 연동 대상이고 UI 체크박스는 **켜진 채 잠긴다**. 없으면 꺼짐이며 체크하면 설정 작성으로 간다. 폴더 밖 WF_HOME 은 `projects.json` 의 `wf_home` 경로 오버라이드로만 쓴다. **범위 설정의 사본은 갖지 않는다** — 정본은 `state.json` | 신규(사용자 요구 3). wsManager D-013 을 실사용에 맞게 대체 |
| D-005 | **설정 작성 기능은 `.gitignore` 검사를 반드시 동반한다.** `.workflow/`·`daily/`·`weekly/`·`state.json`·`jira_project_key_id.txt` 가 제외되어 있지 않으면 설정을 쓰기 전에 경고하고 추가를 제안한다 | 신규(사용자 요구 4) |
| D-006 | **모든 Jira 쓰기는 단일 통로를 지난다.** 그 통로가 `logs/approvals.jsonl` 에 먼저 기록하지 않으면 예외로 막는다. 승인 없는 생성은 되돌리기가 가장 비싸다 | 신규. 안 A 의 필수 부품 |
| D-007 | API 토큰은 **저장소에 들어갈 수 있는 어떤 파일에도 쓰지 않는다.** `JIRA_EMAIL`/`JIRA_API_TOKEN` 환경변수 우선, 없으면 `%APPDATA%\wsFlow\credentials.json`. 서버 응답에 토큰을 싣지 않는다 | 신규 |
| D-008 | lock 정리는 git 프로세스 부재 + 5초 이상 경과일 때만. `objects/` 아래 lock 은 제외 | wsManager D-006 재선언 |
| D-009 | 커밋 메시지는 UTF-8(BOM 없음) 임시 파일 + `git commit -F` | wsManager D-005 재선언 |
| D-010 | 폴더·파일 선택은 서버가 tkinter 를 자식 프로세스로 띄워 경로를 반환 | wsManager D-007 재선언 |
| D-011 | 배포 = 원본 폴더/파일(복수) → 체크한 프로젝트에 복사. 템플릿 아래는 같은 상대경로, 그 외는 지정 하위경로. 항상 덮어쓰기·백업 없음, 실행 전 미리보기 | wsManager D-011 재선언 |
| D-012 | 새 프로젝트 = 선택 루트 아래 빈 폴더 생성 + 등록만 | wsManager D-010 재선언 |
| D-013 | 다중 PC 동기화 = 저장소 단위 fetch → `pull --ff-only` → push. non-ff 는 표시만 | wsManager D-008 재선언 |
| D-014 | 워크스페이스 루트는 복수 허용. 템플릿 기준은 루트별 `0.PROJECT_TEMPLATE` | wsManager D-003 재선언 |
| D-015 | 승인 기록은 `<WF_HOME>/.workflow/approvals.jsonl` — 저장소가 아니라 업무 폴더. append-only, `approved`/`pushed` 두 이벤트 | 신규(D-006 의 경로 확정) |
| D-017 | 태스크별 메모는 스키마를 바꾸지 않고 규약만 얹는다 — `- KEY: 내용`. 키로 시작하지 않는 줄은 원문 그대로 보존 | 신규 |
| D-018 | 마감 UI 는 코멘트까지. 신규·파생·BUG 는 메모에 남기고 '티켓 후보'로 보고(S6) | 신규 |
| D-019 | 목록 화면은 `open`·`orphans` 만 조회. `changes` 는 watermark 를 전진시키는 배치의 몫 | 신규 |
| D-020 | 설정 작성은 재실행해도 안전 — `watermark`·`last_success`·`roots` 보존, 설정 항목만 갱신 | wf-init 원칙 승계 |
| D-021 | 사이트 공통값은 이미 설정된 폴더에서 가져오고 다시 묻지 않는다 | 신규 |
| D-022 | 설정 조회는 편의일 뿐 — 한 부분이 실패해도 나머지를 주고, 직접 입력이 항상 가능 | 신규 |
| D-023 | 신규 티켓 제목 검사는 `vendor/new_title.py` 를 하위 프로세스로 그대로 돌린다 | 신규 |
| D-016 | 완료(Done) 전이는 전이 대상의 `statusCategory` 를 조회해 막는다. **상태 이름으로 막지 않는다** — 이름은 사이트 언어에 따라 갈리고 목록을 적는 순간 새 이름 하나에 뚫린다 | 신규 |

## 6. 형식 오버라이드 (GLOBAL Rule 5에 대한)

- 코드 중심 프로젝트이므로 **표를 포함한 분석·검토 문서도 `.md` 를 허용**한다. 도형이 있으면 `.html`.

## 7. 증거 규약

- 시험 실행 증거: `logs/test_evidence/<YYMMDD_HHMM>_evidence.json`. 문서는 수치 대신 이 경로를 인용한다.
- 음성 대조(Rule 12): 수정에는 "되돌리면 실패하는 시험"이 따른다. F-001 의 음성 대조는 `server.py` 의 `self._guard(url)` 호출을 제거했을 때 `GuardTests` 3건이 실패하는 것으로 확인한다(2026-09-11 확인).
- **업무 데이터는 이 저장소에 들어오지 않는다.** 실 이슈 키·`daily/`·`.workflow/` 는 `.gitignore` 로 막고, 문서에도 옮겨 적지 않는다(`jiraWF/.github/workflows/guard.yml` 의 취지 승계).
- 코드 인용은 `file:line` + 원문.

## 8. 알려진 미해결 전제

- **U-001** Windows 에서 `_git_running()` 은 `tasklist` 출력에 의존한다. tasklist 가 없는 환경에서는 보수적으로 "실행 중"으로 보고 lock 정리를 건너뛴다. (wsManager U-1 승계)
- **U-002** MCP 응답 필드 경로가 환경마다 다르다 — REST 도 마찬가지일 수 있다. `jira_fields.py` 가 여러 경로를 시도하고 적중 경로를 `HITS` 에 남긴다. ~~**첫 실조회에서 반드시 확인한다**~~ → **해소(M-001, 2026-09-11).** 실사이트 첫 실조회에서 모든 판정 경로가 MCP 시절과 동일하게 해석됐다 — 증거 `logs/test_evidence/260911_1555_u002_field_paths.json`. 확인 수단은 `python run.py --check-jira <WF_HOME>` 의 `[필드 경로]` 줄이며, 사이트가 늘면 그 사이트에서 다시 확인한다. (jiraWF U-002 승계)
- **U-006** 엔드포인트는 `/rest/api/3/search/jql` 이다 — 구 `/rest/api/3/search` 는 제거됐다(F-002). 생성 메타도 마찬가지로 구 `/rest/api/3/issue/createmeta` 가 사용 중단돼 `/createmeta/{key}/issuetypes` 와 `/issuetypes/{tid}` 로 나뉘었다(F-006). **새 엔드포인트를 쓸 때는 현행 여부를 먼저 확인한다** — 두 번 겪었다. `total` 은 신 엔드포인트가 주지 않는다 — 실측 응답 최상위 키는 `isLast`·`issues` 뿐이다(M-001). 판단에 쓰지 않는다. `MAX_PAGES=50` 을 넘으면 `_truncated` 로 표시만 하고 멈춘다 — 개인 범위에서 5000건이면 스코프가 틀린 것이다.
- **U-003** P-021에서 지원 경계를 확정했다. `active`는 과거 저장값을 유지하는 legacy 메타데이터이며 **스프린트 필터가 아니다**. 미지정/`all`도 스프린트 제한 없이 조회한다. 다른 mode는 배치/완전 조회 판정을 차단한다. 화면·selfcheck·배치에 지원 제한을 표시한다.
- **U-004** P-021: `sites` 자격은 요청 사이트 URL exact match이며 다른 이름의 유일 사이트로 fallback하지 않는다. 환경변수/평탄 파일은 공유 계정, 사이트별 계정은 `sites`로 지정한다. 생성 계획과 실행에서 해당 사이트의 current accountId를 확인한다. 실제 다사이트 계정 수용은 별도다.
- **U-005** P-021: `tools/jira_vendor_manifest.json`, `tools/check_jira_vendor.py`가 무수정 9파일·전체 Python tree hash·모체 commit을 대조한다. 배치도 manifest hash 불일치를 차단한다. 모체/기존 vendor는 변경하지 않았다.

## 9. P-011 목표 설계 확장 — 미구현

- 아래는 보존한 P-011의 설계 기준이다. 현재 통합 목표·검증·단계의 정본은 §10을 따른다.
- 목표: ROOT의 공통 규칙·스킬 정본과 프로젝트 전용 지침을 Codex/Claude 진입점·선택 스킬 노출·세션 준비·출처 검사로 연결한다. Git·Jira 개인 로컬 도구라는 기존 범위를 유지한다.
- P-011 당시 정본: `rd/workspace_context_design.md` 및 `rd/workspace_context_verification_plan.md`. 설계 결정 D-027~D-034는 목표에 대한 결정이며 구현 검증 완료 상태가 아니다.
- 현재 실행 규칙은 이 절을 추가했다고 바뀌지 않는다. 기존 프로젝트의 GLOBAL_RULES·스킬 복사본·진입점·등록 데이터는 후속 마이그레이션 계획에서만 전환한다. 현재 ROOT 공통 파일이나 도구 설정을 변경할 권한을 본 문서만으로 확대하지 않는다.
- 공통/프로젝트 정본과 도구별 노출 위치를 분리한다. 파일 존재, 출처 해석, 실제 세션 관측은 서로 다른 상태다. 지침 문구가 filesystem/sandbox 격리를 대신한다고 주장하지 않는다.
- P-010의 F-014~F-027은 그대로 유지한다. 설계 대응을 붙인 것은 수정 완료가 아니며 구현·음성 대조·검증 증거 뒤에만 상태를 전환한다.
- 일반 파일 배포 D-011과 새 context 관리 파일 변경 계약은 구분한다. 신규 관리 경로 보호·보존·복구 정책의 구현 전에는 기존 배포 동작을 바꿨다고 표시하지 않는다.

## 10. P-013 통합 목표 설계 v2 — I0·I1·I2 구현 현황은 §11~13

- 현재 목표 정본은 `rd/wsflow_integrated_design.md`, 검증 정본은 `rd/wsflow_verification_plan.md`, 구현 순서 정본은 `rd/implementation_plan.md`다. P-010/P-011/P-012 보고서와 이전 설계는 원문 보존한다.
- CTX-01~CTX-15를 유지하고 CAL-01~CAL-16을 정식 목표에 통합한다. 월/주 날짜 칸은 일정 내용과 task/메모 각각의 건수, 날짜 선택은 우측 상세다. 수행 task는 진행 중 포함 그날 저장된 작업 기록이 있는 고유 업무다.
- 직접 일정 등록·Google 조회·로컬 일정의 Google 추가·연결한 일정의 양방향 갱신·충돌/복구를 포함한다. 로컬 캘린더 I2는 중간 결과이며 Google 수용까지 통과한 I4가 캘린더 전체 완료 게이트다.
- Google OAuth는 외부 일정 접근 승인으로서 앱 사용자 로그인과 구분한다. Jira 양방향 자동 동기화 금지·완료 전이 금지·자격의 저장소 밖 보관·vendor 무수정 이관 원칙은 유지한다.
- 기존 daily와 Jira state는 원래 위치가 정본이다. 새 local event/note와 Google mirror/outbox는 원본·파생·미반영 편집을 구분하고 Google cursor와 Jira watermark는 공유하지 않는다.
- 이전 S0~S4·K0~K3의 실행 순서는 I0~I7로 대체한다. D-038~D-041·M-004는 목표 추적이며 구현 완료 표시가 아니다. P-013 당시 F-014~F-027의 상태는 유지했으며 이후 구현 결과는 §11·12를 따른다.
- P-013 문서 통합 당시 앱·시험·도구·vendor 코드, 실제 ROOT/스킬 배치, 외부 계정·일정·Jira 데이터는 변경하지 않았다. 보존/문서 검사 증거는 `logs/test_evidence/261016_P013_validation.json`이다.

## 11. P-014 I0 구현 현황

- 신규 `GET /api/v1/projects/<id>/context`와 `GET /api/v1/calendar/range|day`를 제공한다. 기존 URL/ID/응답·편집 UI는 유지한다. 기존 Host/Origin/토큰 가드 뒤에서 동작한다.
- 컨텍스트는 정적 출처·해시·명시 참조·구조 충돌·스킬 패키지를 조사한다. UID는 읽기 중 부여하지 않는다. 형제 본문·임의 참조를 수집하거나 스킬을 실행하지 않는다. 세션 전체 scope_complete=false다.
- 날짜 API는 기존 daily의 저장된 기록만 읽고 실제 일자를 검증한다. 같은 업무의 하루 중복·중복 WF_HOME를 제거하며 상태/현재 목록으로 과거 기록을 버리지 않는다. 기존 편집용 read_memo의 파일 생성 동작은 그대로 두고 새 조회에서 호출하지 않는다.
- 아직 구현하지 않은 event·독립 note·Google 범위는 coverage=false로 표시한다. P-014 당시 I1~I7은 미착수였으며, 이후 I1 결과는 §12를 따른다. 새 I0 결정의 로컬 검증과 CI 검증은 구분한다.
- 상세 계약은 `rd/i0_api_contract.md`, 증거는 `logs/test_evidence/261016_P014_i0_validation.json`이다. 이전 보고서/목표 설계와 vendor는 해시 기준 보존한다.

## 12. P-015 I1 구현 현황

- 함수·실행/복구 계약의 정본은 `rd/i1_contracts.md`다. I1-A~D를 구현하며 F-015~F-023은 회귀·음성 대조에 근거한 fixed다. CI는 미실행이므로 verified로 올리지 않는다. 결과는 `logs/test_evidence/261017_P015_validation.json`이 연결하는 새 I1 보고서에 보존한다.
- 메모는 프로젝트·실 WF_HOME·날짜별 문서 ID와 revision으로 관리한다. 전체 원문을 보유하고 편집된 키만 바꾼다. WF_HOME의 협조 잠금·동일 디렉터리 교체·UUID 백업을 사용한다. 잠금을 따르지 않는 외부 편집기와 전원 장애까지 원자성을 확장하지 않는다.
- SQLite 작업 DB는 WF_HOME 밖의 `WSFLOW_DATA_DIR` 또는 사용자 설정 영역에 둔다. UUID와 본문 hash를 분리하고, 계획·intent는 불변이며 전송 전 조건부 claim을 영속화한다. JSONL과 DB·Jira는 별개 저장 계층이며 승인 로그는 기존 형식으로 선기록/결과 내보내기를 유지한다.
- 실행 API는 서버 발급 계획·선택 ID만 받는다. 원본/설정/검사 정책/만료·현재 범위·schema와 경고 확인을 검증한다. 기존 URL은 유지하되 raw payload 자동 승인은 거부한다. 메모 PUT에는 해당 문서의 document_id가 필요하다.
- executing/uncertain은 재전송하지 않는다. 원격 ID의 본문/필드와 작업 UUID/hash를 읽어 대조한다. 확정 성공의 로컬 기록 복구는 원격 재전송 없이 수행한다. 구형 approved-only 기록은 수동 확인 대상으로 보존한다.
- I0 API·Git·기존 조회/설정 기능과 vendor 원문을 유지한다. F-014·F-024~F-027 및 상위 CTX/CAL 목표는 완료 처리하지 않는다. P-015 당시 다음 단계는 I2였으며 현재 결과는 §13을 따른다. Google 쓰기는 I4의 별도 수용 범위다.

## 13. P-016 I2 구현 현황

- I2 계약은 `rd/i2_contracts.md`, 결과는 `logs/test_evidence/261017_P016_validation.json`이 연결하는 새 보고서다. 월/주·날짜 우측 상세·로컬 일정/독립 메모를 구현한다. I3~I7과 Google 전체 수용은 미완료다.
- I0 v1은 보존하고 새 v2 calendar API에서 로컬 기록을 합성한다. 같은 snapshot에서 건수와 상세를 계산하며 조회는 DB/daily를 만들지 않는다. 실패 건수는 null/확인 필요로 표시한다.
- 사용자 데이터 영역의 registry별 SQLite를 사용한다. I1의 불변 계획·작업 ID/감사를 재사용하고 로컬 효과와 결과는 단일 DB 트랜잭션으로 기록한다. 기존 Jira DB·WF_HOME·승인 JSONL은 로컬 캘린더 쓰기 대상이 아니다.
- 로컬 record ID/revision·삭제 tombstone을 보존한다. 서버 계획과 수정 전 revision을 검증하고 같은 작업 재전송은 cached 결과다. 프로젝트 연결은 legacy ID와 당시 실경로를 함께 보유하여 ID 재사용 시 예전 기록을 새 폴더에 섞지 않는다.
- 날짜 칸에는 일정 시간/제목·overflow와 업무/메모 각각의 건수만 표시한다. 우측 상세에는 모든 일정·기존 작업 기록·독립/공통 메모와 출처를 표시한다. 미저장 초안은 문서에 묶고 이동/늦은 응답으로 덮지 않는다. UI 표시 시간대는 Asia/Seoul이다.
- 이 단계의 로컬 완료와 CI·실계정 검증은 구분한다. 기존 보고서/목표 설계/vendor는 해시로 보존한다. 레거시 시험 재실행의 고정 브라우저/vendor 출력 경로 재사용은 새 보고서의 이력에 명시하며 이후 출력을 요청/실행별로 분리한다.

## 14. P-017 I3 구현 현황

- I3 계약은 `rd/i3_contracts.md`, 구현 검증은 `logs/test_evidence/261018_P017/validation.json`이다. Google OAuth·계정별 캘린더 선택·고정 profile 증분 mirror와 유한 occurrence 조회를 제공한다. 기존 v1/로컬 계획/메모/Jira 흐름을 유지한다.
- Desktop PKCE·state/TTL/단회 loopback을 사용하고 기존 앱 API 가드를 유지한다. 최소 읽기 scope와 openid/email만 요청한다. 자격은 프로젝트/WF_HOME 밖의 Windows DPAPI 또는 POSIX 0600 보호 파일이며 DB/응답/접근 로그에 넣지 않는다.
- 수집 전체 페이지 성공 후 mirror/cursor를 확정한다. 410/조건 변경은 해당 mirror 새 세대의 전체 수집 성공 후 교체한다. local·pending·매핑 자료는 지우지 않는다. occurrence 실패는 이전 결과와 null 건수로 표시한다.
- 앱 worker는 실행 중 5분마다 갱신 필요를 확인한다. 수동 동기화/last_sync/권한/연결 해제를 제공한다. Google 일정은 읽기 전용이며 원격 쓰기/양방향/충돌 해결은 I4다. 단일 활성 계정이며 계정별 mirror/선택은 분리한다.
- 실제 Google 계정/client 정책·동의 화면·실제 provider recurrence는 unknown이다. 로컬 합성 provider·실제 Chrome/loopback/DB/DPAPI 시험과 구분하며 CI/다른 OS는 검증하지 않았다. 기존 보고서와 검증 산출물은 P-017 baseline의 byte hash로 보존한다.

## 15. P-018 I4 구현 현황

- I4 계약은 `rd/i4_contracts.md`, 검증은 `logs/test_evidence/261018_P018/validation.json`이다. 새 일정의 로컬만/Google 대상 선택·기존 로컬 일정 추가·연결 일정의 양방향 갱신·충돌/응답 유실 복구를 구현한다. 이전 보고서/검증 산출물은 원문 byte hash로 보존한다.
- 명시 쓰기 재승인에서 calendar.events를 요청하고 현재 계정/선택 캘린더/owner·writer/scope를 전송 직전에 확인한다. 기존 읽기 연결/v1·Git/Jira·메모/로컬 저장 흐름은 유지한다. 외부 미연결 Google 일정과 반복/참석자/특수 일정의 자동 편집은 지원하지 않는다.
- local record/link/immutable outbox/audit는 같은 calendar DB tx로 확정한다. 고정 Google ID·작업 UUID/hash를 전송 전에 기록하고 조건부 claim 후 PATCH/DELETE에 If-Match를 사용한다. 외부 효과와 DB의 분산 원자성은 주장하지 않는다.
- syncing/uncertain은 재전송하지 않고 원격 ID/marker/관리 필드/etag를 읽어 대조한다. 오류는 명시 재시도하며 승인된 queued만 앱 수집 후 자동 실행한다. 계정/선택 변경은 다른 대상으로 전송하지 않는다. marker는 상관관계 근거이며 서명이나 공급자의 별도 중복 방지 제약이 아니다.
- remote 단독 변경은 local revision을 증가시키고, 양쪽 변경은 모두 보존하는 conflict다. 전송 중 새 입력은 후속 queue에 남긴다. 충돌 해결은 현재 local revision/remote etag를 확인한 새 서버 계획으로 승인한다. Google 삭제 뒤 로컬 보관 내용을 표시/편집해도 자동 부활시키지 않는다.
- 로컬 숨김/다시 표시는 Google 삭제와 분리한다. Google 삭제는 별도 승인이다. task/메모/Jira/참석자 목록을 업로드하지 않는다. CAL 전체의 로컬 합성 수용과 실제 Google 계정 수용은 구분한다. 실제 계정/CI/다른 OS는 unknown이며 I5~I7/CTX 전체 목표는 미완료다.
# P-019 I5 구현 현황

I5의 정본은 `rd/i5_contracts.md`, 검증 결과는 `logs/test_evidence/261018_P019/validation.json` 및 그 파일이 연결한 신규 보고서다. 기존 보고서·검증 산출물은 보존한다. ROOT와 프로젝트 초기화는 별도 불변 계획이며 기존 빈 폴더 생성은 유지한다. 실제 사용자 ROOT/프로젝트의 초기화를 이 구현 작업에서 수행하지 않는다.

선택 스킬 전체 패키지는 명시 managed-copy로 `.agents/skills`·`.claude/skills`에 노출한다. 기존 진입점 사용자 bytes·기존 규칙·Jira state·기존 `.codex/.claude`를 보존하며 이름 충돌/변경된 관리 bytes/잘못된 링크·UID·checkout·만료/변경된 계획은 차단한다. 일반 배포 D-011은 관리 context 경로를 우회하지 못한다. 원본 정본의 편집과 외부 에이전트의 OS 쓰기 권한은 별도 책임이다.

파일별 백업·저널과 명시 중단 복구를 제공하지만 다중 파일 원자성이나 비협조 프로세스의 모든 race 방지를 주장하지 않는다. 패키지의 Markdown 상대 링크와 파일 경계를 조사하며 임의 스크립트의 동적 의존성을 실행·추론하지 않는다. 세션 실제 인식/receipt/대시보드 I6, Git/Jira I7, 실제 Google 계정 수용과 CI는 미완료다.

## P-020 I6 구현 현황

정본은 `rd/i6_contracts.md`, 결과는 `logs/test_evidence/261018_P020/validation.json`이 연결한 신규 보고서다. 기존 보고서·검증·사용자 스킬 파일을 보존한다. 도구별 정적 검사·시작 패킷·구조화 관측 자료/receipt·freshness·개발 환경 대시보드와 별도 캘린더 경고를 구현한다.

도구 버전/surface는 사용자 선언이며 host/OS는 서버 환경에 결부한다. 실제 도구 실행·설치 버전 검사·숨은 문맥 읽기·정책 준수 검증을 수행하지 않는다. unknown/static_only/agent_reported/tool_observed와 부분 scope를 구분한다. tool_observed도 사용자가 제출한 tool_output 자료이며 진위 독립검증이 아니다. 자료는 고정 schema/크기/서버 생성 경로로 제한하고 코드/지시를 실행하지 않는다.

ROOT/checkout/worktree·원본/사본/support·소유 profile/manifest·선택·버전/host/OS·새 준비·근거 변경 후 기존 관측은 보존하고 현재 완료로 재사용하지 않는다. 새 사용자 데이터 기록만 작성하며 사용자 ROOT/프로젝트 파일은 읽기만 한다. I6 로컬 합성 관측 검증과 실제 Codex/Claude 세션 수용은 구분한다. I7·실제 Google 계정·CI·다른 OS 수용은 후속 범위다.


## 18. P-021 I7 구현 경계

`rd/i7_contracts.md`와 `logs/test_evidence/261018_P021/validation.json`을 따른다. 기존 보고서·검증 산출물은 보존한다. Git은 명시 fetch 시각/고정 ff-only pull, Jira는 명시 일일 순환·최종 state marker·실패 watermark 유지·단계 증거를 제공한다. Google cursor와 Jira watermark는 분리한다. 전체 업무 상태는 Calendar의 날짜 원본을 재사용하며 예정 업무를 수행 기록으로 세지 않는다. 조회 완전성/현재 사용자 담당/생성 후 가시성/에픽 전체 제목·로컬 예약·실행 재검사를 추가한다. 지원하지 않는 스프린트 의미와 자격 매핑은 성공으로 표시하지 않는다.

판단 근거: D-065~069. 단일 기존 vendor/협조 잠금/atomic replacement 후보를 재사용해 별도 집계/문장 생성기를 만들지 않았다. 명시 배치는 Jira 읽기 전용이다. 원본 state/daily/aggregate 전체 백업 후 stage tree에서 검사하며 watermark와 run proof marker를 마지막 단일 state 교체로 확정한다. 다중 파일 전체 원자성·비협조 외부 편집과의 마지막 race·전원 장애·무제한 검색 색인 지연·외부 사용자/다른 PC의 순번 CAS는 보장하지 않는다. stage receipt의 일시 PermissionError는 제한 재시도하며 영구 오류는 실패다. 실제 계정/도구/CI/타 OS 및 CTX/CAL 전체 실사용 수용은 로컬 합성 검증과 구분한다.


## 19. P-022 I0~I7 통합 상태와 후속 검증 정본

현재 최종 분석은 `output_docs/261018_1715_i0_i7_final_analysis.md`, 후속 검증 실행 정본은 `rd/evaluation_plan.md`다. 기존 통합 목표/검증 v2와 단계별 계약·보고서는 원문 이력으로 유지한다. I0~I7의 로컬 계약 범위 구현·회귀는 연결됐지만 CTX-14의 Git 공통 plan/operation 경계는 부분(F-032)이며 실계정·실제 도구·CI/다른 OS 수용은 미완료다. 이전 단계의 미착수/미구현 표시는 그 요청 시점의 기록이고 현재 상태를 대체하지 않는다.

P-022는 코드·시험·도구·vendor·기존 보고서/검증 산출물을 변경하지 않는다. 기존2377파일 보호 기준과 과거 P-016 고정 출력 재실행 예외를 구분한다. V0~V7 필수46개는 계획/NOT_RUN이며 현재415시험·Chrome82·I7 음성 대조54의 기존 로컬 결과와 혼동하지 않는다. 원격 효과·CI/하네스 구현과 사용자 ROOT 적용은 후속 요청에서 구체적 범위를 정한다.


## 20. P-023 V0+V1 현재 수용 상태

현재 검증 결과 정본은 `output_docs/261018_1825_v0_v1_verification_report.md`, 실행 증거는 `logs/test_evidence/261018_P023/validation.json`이다. P-022 분석·이전 단계별 보고서/증거는 당시 원문 이력으로 보존한다. V0/V1의9개는 명시 Windows/NTFS canonical full path 프로파일에서 PASS이며 전체46개 중 나머지37개는 NOT_RUN이다. 원 CTX-14는 Git F-032와 일일 메모 F-041의 공통 plan/operation UUID 차이 때문에 PARTIAL을 유지한다. 제품 short alias 직접 입력 F-042와 느린 부하 잠금 F-039는 deferred다.

최종 source commit `01b64dbd5098da1ca811b29fd34840acb8116c35`, fingerprint `f55919f751535e9b3f2705aa80d2326989dca5d9d938ca2bc7b72b46bf232717`. 별도 승인된 공개 codex 브랜치에서 실제 CI 정상2개와 의도 실패3개의 원인/아티팩트를 대조했다. 원격 main·원 작업 폴더 HEAD/index·사용자 파일·기존2396파일을 유지했다. 애플리케이션·기존 시험·vendor는 수정하지 않았다. 최종 각 프로파일의415/15/82/208 수치는 새 evidence 정본을 따른다.

판단 근거: D-075~078/M-014 및 `rd/v0_v1_contracts.md`. CI가 통과해도 실계정 Google/Jira와 실제 Codex/Claude 세션 수용을 대신하지 않는다. V2/V3/V6 이후 V4/V5/V7 순서를 유지한다. 이번 snapshot의 보조 안내 문서는 freeze 당시 이력이며 본 현재 링크 갱신은 실행 소스 fingerprint 밖 문서 변경이다.

## 21. P-024 V2 읽기·일일 수집 수용 상태

현재 결과 정본은 `output_docs/261018_1957_v2_verification_report.md`, 증거는 `logs/test_evidence/261018_P024/validation.json`, 계약은 `rd/v2_execution_contracts.md`다. 이전 보고서/검증 산출물은 원문 보존한다. 제품·기존 시험·vendor·CI 소스는 동일 동결 fingerprint이며 새 하네스와 문서만 추가/보강했다. 읽기82회와 별도 schema4 WF_HOME에서 수행했고 원격 업무 쓰기·원본 설정 migration·공개 게시를 하지 않았다.

V2-04/05는 현재 UTC 계정·schema4 shadow 프로파일에서 PASS, V2-01/02/03/06은 PARTIAL이다. 원46개는11 PASS/4 PARTIAL/31 NOT_RUN. source fingerprint만 같다는 이유로 실계정 표본 조건을 생략하지 않는다. schema3 운영 전환(F-044), DST의 경과시간 계산/실제 gap·fold 수용(F-045), 하네스의 비labels oracle/부분 오류 보고(F-046/047)는 남은 경계다. 하네스 판정/보호 시험은 F-048~050에 기록한다. 기존 F-032/039/041/042 상태는 유지한다.

판단 근거: D-079/M-015, 실제 공급자/통제 replay의 분리·사전 표본/키 대조·정확한 단계/최종 state/메모 bytes·독립 검토를 연결한다. 하네스 source·초기 생존 변이·강화 후 fresh 결과를 모두 보존한다. 원래 schema3을 자동 고치거나 이번 UTC 결과로 DST/두 번째 사이트·색인 지연을 통과시켜서는 안 된다.


## 22. P-025 F044/F045 보강 결과

현재 보강 정본: `output_docs/261018_2318_f044_f045_implementation_report.md`, `review_docs/261018_2318_f044_f045_delta_audit.md`, `logs/test_evidence/261018_P025/validation.json`, 계약 `rd/v2_fixes_contracts.md`. F044/F045는 시험 WF_HOME의 보존형 전환·UTC 산술 범위에서 검증했다. 실제 사용자 원본은 schema3 그대로 보존하며 원격 업무 쓰기는0이다. 기존 보고서/증거는 수정하지 않는다. D080/D081/M016은 보존 전환·절대시각 조회·근거 연결을 정의한다.

F051~058은 두 독립3관점 회차와 비독립 고침 확인으로 보강했다. 성공 작업 cache는 과거 실행, status.committed는 현재 hash 대조이며 외부 변경을 덮지 않는다. 정밀 값 보존 불가 JSON은 미리보기에서 거부한다. Git 제외는 실제 WF_HOME의 근접 경계를 기준으로 모든 관련 동선에서 일치시킨다.

최종 회귀432(F0/E0/skip1), browser82+5, CI자체15, 선택변이79개. 최종 source와 회귀 source의 차이는 R03 패턴 한정1파일뿐이며 bridge로 제품/시험 bytes 동등성을 확인하고 최종 source의 변이는 재실행했다. 신규spec을 실제 CI가 이미 실행했다는 뜻이 아니다. 이전46개 수용 수치는 이전 source의 이력이며 새소스에 자동 승계하지 않는다. 후속 실계정 epoch·CI·V2표본 수용 및 F032/039/041/042·F046/047은 유지한다.
