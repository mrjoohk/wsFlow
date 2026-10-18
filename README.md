# wsFlow — 로컬 웹 workspace + Jira 업무 기록 관리자

**wsManager**(프로젝트 폴더·git·규칙 대시보드)와 **jiraWorkFlow**(Jira 개인 업무 기록 순환)를 합친
하나의 로컬 도구. 브라우저에서 프로젝트 폴더를 고르면 그 폴더의 git 상태·규칙 준수와
**매핑된 Jira 태스크·날짜별 메모·마감·신규 태스크**를 한 화면에서 다룬다.

외부 의존 0 — Python 3.10+ 표준 라이브러리와 git CLI 만 있으면 된다. Jira 는 `urllib` 로 REST 를 직접 호출한다.

## 실행

```
python run.py            # 토큰이 붙은 주소가 브라우저로 열린다
run.cmd                  # Windows (UTF-8 콘솔 설정 포함)
python run.py --port 9000 --store D:\my\projects.json --no-browser
```

> **토큰**: 기동할 때마다 난수 토큰을 만들고 주소에 `?t=...` 로 붙인다. 127.0.0.1 바인딩만으로는
> 사용자가 연 임의 웹페이지의 fetch 를 막지 못하기 때문이다(F-001). 콘솔에 찍힌 주소로 열어야 한다.

## Jira 자격

환경변수가 우선이고, 없으면 `%APPDATA%\wsFlow\credentials.json`(그 외 OS 는 `~/.config/wsFlow/`)을 본다.
**저장소에 들어갈 수 있는 파일에는 토큰을 두지 않는다**(D-007).

```json
{
  "JIRA_EMAIL": "me@example.com",
  "JIRA_API_TOKEN": "..."
}
```

키 이름은 `email`/`user`, `api_token`/`apiToken`/`token` 도 받고 대소문자·하이픈을 무시한다.
사이트가 여럿이면 `{"sites": {"https://x.atlassian.net": { ... }}}` 형태로 나눈다.
Basic 인증이므로 **토큰만으로는 안 되고 계정 이메일이 함께 필요하다.**
토큰은 https://id.atlassian.com/manage-profile/security/api-tokens 에서 만들고 **만료일(최장 365일)이 있다.**

```
python run.py --check-jira <WF_HOME>          자격·인증·스코프·필드 경로 확인 (읽기 전용)
python run.py --check-jira <WF_HOME> --fetch  확인 후 원본을 .workflow/raw/ 에 남긴다
```

## 사용 순서

1. 헤더 **＋ 추가 ▾** → 「루트 추가」의 **찾아보기…** 로 워크스페이스 루트를 고른다. **루트 스캔…** → 등록할 폴더 체크 → **선택 등록**.
   프로젝트가 하나라도 있으면 첫 화면은 **대시보드**(「주의」 요약 + 규칙 준수 표)다.
2. 왼쪽 카드에서 프로젝트 선택 → 탭: `상태/커밋/push` · `로그` · `remote` · `규칙` · **`Jira`**. 카드의 배지(변경·↑·lock·Jira)를 누르면 그 상태를 다루는 탭으로 바로 간다.
3. **Jira 탭**은 그 프로젝트 폴더에 `.workflow/state.json` 이 있으면 **켜진 채 잠긴다**(D-004) — 설정 파일이 있는데 끄는 선택지는 두지 않는다. 없으면 꺼져 있고, 체크하거나 **설정 작성**을 누르면 설정 패널이 열린다. 탭 상단에 **Jira 자격 상태**(있음/없음·출처·파일 위치)가 먼저 보인다.
   - 자격이 있으면 탭을 열 때 **자동으로 조회**한다(읽기 전용). 「다시 조회」와 마지막 조회 시각이 옆에 있다. 배지: 목록 밖 CSU · 제목 필터로 빠짐 · CSC 미지정 · 범위 밖 · 안 올라간 파생
   - 태스크별 칸에 그날 한 일을 적으면 **● 수정됨 — 저장되지 않음** 이 뜨고, 탭·프로젝트를 오가도 입력은 유지된다. **메모 저장** → `daily/{YYMMDD}.md` 의 메모 구획만 바뀐다. 다른 곳에서 먼저 바뀌었으면 **덮지 않고** 현재 내용을 보여준다(INV-1). 날짜 변경·다시 조회·창 닫기는 미저장 입력이 있으면 먼저 묻는다
   - **마감 초안**(미저장이면 「저장하고 마감 초안」) → 승인 표 → **체크한 것만** 코멘트로 올라간다(기본은 전부 미체크). 결과는 대상별 성공/실패 표로 남고 실패는 그 자리에서 다시 올릴 수 있다. 승인 기록이 먼저 남고, **완료(Done) 전이는 하지 않는다**
   - **새 태스크 · 파생** → 확인 → 확정된 제목·필드를 보고 **승인하고 만들기**. 만들기 전에 제목이 이 폴더 범위에 들어오는지, CSC 가 붙는지, 어느 CSU 밑인지 검사한다
   - **설정 작성 / 설정 수정** → 사이트 공통값은 이미 설정된 폴더에서 가져오고, 폴더마다 다른 것만 묻는다. **미리보기 → 적용** 순서이며 값을 바꾸면 다시 미리보기해야 한다. 재실행해도 `watermark` 와 `roots` 는 보존된다. `.gitignore` 에 업무 데이터 제외가 없으면 저장 전에 멈춘다
4. **대시보드**: 「주의」 요약(lock·뒤처짐·push 대기·미커밋·remote 없음·Jira 설정 문제 — 프로젝트 이름을 누르면 해당 탭으로) + 규칙 준수 표. Jira 신호 열(범위 밖 에픽·제목 누락·CSC 미지정·안 올라간 파생·마감 누락)은 S7 에서 붙는다.
   push·Jira 쓰기·배포처럼 이 PC 밖에 쓰거나 덮어쓰는 버튼은 **빨간색**이고 확인 창이 뜬다. 헤더 **알림**에서 최근 toast·오류 전문을 다시 볼 수 있다.
5. **도구 ▾ → lock 검사… · 배포…**, **＋ 추가 ▾ → 새 프로젝트** 는 wsManager 와 같다.

## 구성

```
run.py / run.cmd              진입점
wsflow/server.py              HTTP JSON API + F-001 가드(Host·Origin·토큰)
wsflow/gitops.py              git 어댑터 (UTF-8 고정, lock 감지/정리, 파괴적 명령 없음)
wsflow/scanner.py             루트 스캔 · 규칙 준수 지표 (읽기 전용)
wsflow/store.py               projects.json
wsflow/deploy.py              배포(복사) 계획·실행
wsflow/pickdir.py             OS 폴더/파일 선택 창 (tkinter 자식 프로세스)
vendor/jira_worklog/          jiraWF 결정적 파이프라인 — 무수정 이관 (D-003, 고치지 말 것)
static/index.html             UI (단일 파일)
tests/                        unittest + jiraWF fixtures
PROJECT_RULES.md              프로젝트 바인딩 (GLOBAL Rule 10)
```

**`vendor/jira_worklog/` 는 고치지 않는다.** 원본은 `jiraWorkFlow/jiraWF/plugins/jira-worklog/scripts/` 이고,
고칠 일이 생기면 거기서 고쳐 다시 가져온다. 사본이 둘이면 그 자체가 드리프트다(D-003).

## 시험

```
python tests/run_tests.py        # logs/test_evidence/<run_id>_evidence.json 생성
python tools/graph_checks.py     # 대장 무결성 + 문서의 코드 인용
```

## 두 모체

`wsManager`(원년 2026-09-04)와 `jiraWorkFlow`(원년 2026-08-25)는 **동결되고 사라지지 않는다.**
이 저장소의 원년은 2026-09-11 이고 대장은 D-001 부터 다시 시작한다 — 유효 결정은 `PROJECT_RULES.md` §5 에
사본이 아니라 **재선언**으로 들어와 있다. `jiraWF` 는 public·단독 설치용 플러그인 저장소로 계속 살아 있다.

## 통합 목표 설계 v2와 I0·I1·I2 구현

ROOT 공통 규칙·스킬, 프로젝트 전용 지침, Codex/Claude 세션 준비와 월/주 캘린더를
[통합 목표 설계](rd/wsflow_integrated_design.md)에 연결했다. 날짜 칸에는 일정 내용과 수행 task/메모 각각의 건수,
날짜 선택 시 오른쪽에 상세를 표시한다. 수행 task는 진행 중 포함 그날 작업 기록이 있는 업무다.
직접 일정 등록·Google Calendar 조회·로컬 일정의 Google 추가 및 양방향 갱신·충돌/복구를 필수 목표로 포함한다.

[최종 통합 분석](output_docs/261016_1917_integrated_final_analysis.md), [단계별 구현 계획](rd/implementation_plan.md),
[통합 검증·수용 계획](rd/wsflow_verification_plan.md)이 다음 구현의 기준이다.
로컬 캘린더 I2는 중간 단계이고 Google 동기화 수용까지 통과한 I4가 캘린더 전체 완료 게이트다.
P-013은 목표 설계이며, P-014에서 I0의 읽기 전용 출처 조사·날짜 기록 API를 구현했다.
캘린더 화면·스킬 배치·Google 연동은 후속 단계이며 현재 제공하지 않는다.

I0 읽기 API (기존 기동 토큰 필요):

- `GET /api/v1/projects/<id>/context`: 등록 ROOT와 프로젝트의 정책·스킬 출처·해시·구조적 충돌. 실제 도구 로딩은 미확인으로 표시한다.
- `GET /api/v1/calendar/range?start=2026-10-01&end=2026-11-01`: end 제외 기간의 저장된 작업 기록·공통 메모. `project_id` 반복으로 프로젝트 선택 가능.
- `GET /api/v1/calendar/day?date=2026-10-16&start=2026-10-01&end=2026-11-01&snapshot_id=...`: 같은 기간의 선택 날짜 상세. 원본이 바뀌면 409로 재조회 요구.

API는 기존 daily만 읽고 빈 파일·새 UID를 생성하지 않는다. 실패한 집계는 0 대신 null/확인 불가로 구분한다.
시간대는 기본 Asia/Seoul이며 호스트 IANA 데이터가 필요하다. 상세 계약은 [I0 API 명세](rd/i0_api_contract.md),
구현·검증 결과는 [I0 보고서](output_docs/261016_2012_i0_implementation_report.md)에 있다.

기존 [상세 분석](review_docs/261016_1808_project_design_implementation_analysis.md),
[P-011 최종 분석](output_docs/261016_1852_final_design_analysis.md),
[개발 컨텍스트 설계](rd/workspace_context_design.md)와 [검증 계획](rd/workspace_context_verification_plan.md),
[캘린더 검토](review_docs/261016_1909_calendar_feature_review.md)는 원문 보존한다.

I1에서 메모 원문 보존·날짜별 문서 분리·동시 저장 충돌 검사를 보강했다. Jira 승인은 서버가 보관한
미리보기의 작업 UUID로 실행하며, 응답 유실은 결과 미확인으로 남겨 재전송을 차단한다.
Jira 화면의 작업 상태에서 원격 ID를 대조하거나, 확인된 성공의 로컬 기록만 복구할 수 있다.
구형 approved-only 로그는 자동 재실행하지 않는다. 기존 실행 URL은 유지하되 raw intent/items 대신
미리보기에서 받은 plan_id와 operation_id가 필요하며, 메모 PUT에는 document_id가 필요하다.

작업 DB는 기본 사용자 설정 영역의 `wsFlow/operations`에 저장한다. `WSFLOW_DATA_DIR`로 위치를 지정할 수 있으며
WF_HOME 밖이어야 한다. 앱 실행 의존은 Python 표준 라이브러리를 유지한다.
[I1 계약](rd/i1_contracts.md)과 [검증 증거](logs/test_evidence/261017_P015_validation.json)를 참조한다.
검증 명령은 Windows 출력 인코딩까지 고정하려면 `python -X utf8 tests/run_tests.py`를 사용한다.
브라우저 재현 harness는 `tests/i1_browser.cjs`이며 설치된 Playwright/Chrome 경로를
`PLAYWRIGHT_MODULE`·`PLAYWRIGHT_CHROMIUM` 환경 변수로 지정한다. 외부 API는 모두 합성 응답이다.

I2의 **캘린더**는 헤더에서 연다. 월/주·이전/오늘/다음, 프로젝트·출처 필터와 날짜별 상세를 제공한다.
날짜 칸에는 일정 시간·제목과 수행 업무/메모 건수만 표시하며, 전체 내용은 선택 날짜의 오른쪽에 나타난다.
좁은 화면에서는 상세가 아래에 놓이고 방향키로 날짜를 이동할 수 있다. 기존 대시보드가 첫 화면인 정책은 유지한다.

로컬 일정·독립 메모는 내용을 확인한 뒤 작성·수정·삭제한다. 닫거나 날짜를 이동해도 미저장 초안이 남고,
저장 충돌은 현재 저장본과 입력을 비교하도록 안내한다. 기존 daily의 업무 기록·공통 메모는 원본을 읽기만 한다.
Google 조회 연결은 I3에서, 연결한 로컬 일정의 Google 추가·양방향 갱신·충돌/복구는 I4에서 추가했다. 실제 Google 계정 수용 시험은 별도로 남아 있다.

새 `GET /api/v2/calendar/range|day`는 로컬 일정/독립 메모와 기존 기록을 함께 반환한다. I0 v1 응답 의미는 유지한다.
로컬 쓰기는 `POST /api/v2/calendar/plan`과 `/apply`의 불변 계획·버전 검사를 사용한다.
DB는 사용자 데이터 영역의 `calendar/` 아래 registry 실경로별로 분리한다. 전체/프로젝트 조회는 이전 등록의 연결을
자동으로 새 폴더에 재할당하지 않는다. [I2 계약](rd/i2_contracts.md), [구현 검증](logs/test_evidence/261017_P016_validation.json)을 참조한다.

브라우저 검증은 `tests/i2_browser.cjs`의 임시 실제 서버/SQLite와 합성 업무 데이터를 사용한다.
`WSFLOW_BROWSER_OUTPUT`로 출력 폴더를 지정하며 기본값은 실행별 별도 폴더다. I1 harness에도 같은 규칙을 적용한다.
vendor 시험 상세는 `WSFLOW_TEST_EVIDENCE_DIR` 또는 실행별 reruns 폴더에 보존한다.

I3의 **Google 연결·캘린더 선택** 패널에서 계정을 연결하고 표시할 캘린더를 선택한다.
먼저 Google Cloud에서 Calendar API를 사용 설정하고 **Desktop 앱** OAuth client JSON을 다운로드한다.
파일은 프로젝트/WF_HOME 밖의 사용자 데이터 폴더에 `google-client.json`으로 두거나
`WSFLOW_GOOGLE_CLIENT_FILE` 환경 변수로 절대 경로를 지정한다. 사용자 데이터 기본 위치는
Windows `%APPDATA%/wsFlow`, 그 외 `~/.config/wsFlow`이며 `WSFLOW_DATA_DIR`로 변경할 수 있다.
예: PowerShell에서 `$env:WSFLOW_GOOGLE_CLIENT_FILE='C:\Users\user\AppData\Roaming\wsFlow\google-client.json'` 설정 후 앱을 실행한다.
동의 화면이 테스트 상태라면 Google Cloud에서 허용한 시험 계정으로 연결한다.

**Google 연결** → **Google 브라우저 승인 열기**에서 시스템 브라우저로 계정/권한을 승인하고 앱으로 돌아온다.
읽을 캘린더를 체크하고 **선택 저장·동기화**를 누른다. `reader` 이상만 내용 조회가 가능하며
freeBusyReader는 선택할 수 없다. 기본 연결은 읽기 전용이며 캘린더 목록 읽기·일정 읽기와 계정 식별(openid/email)을 요청한다.
I4의 **Google 쓰기 권한 연결**은 일정 읽기/쓰기 scope로 브라우저 재승인을 진행한다. owner/writer 캘린더만 쓸 수 있다.
Google 원본·반복 예외의 증분 수집과 월/주 기간의 개별 일정 조회를 분리한다. 마지막 수집 시각/출처를 표시하고,
실패하면 이전 결과와 `?` 건수를 표시한다. 앱이 실행 중이면 5분마다 갱신 필요를 확인하며 수동 동기화도 가능하다.
앱을 종료하면 수집하지 않는다. **연결 해제**는 로컬 자격을 지우고 Google 권한 철회를 시도한다.
철회 미확인 안내가 나오면 Google 계정의 앱 권한에서도 확인한다. 로컬 일정/메모와 기존 mirror는 보존한다.

Google 자격은 registry별 사용자 데이터 영역의 보호 파일(Windows DPAPI / POSIX 0600)에,
mirror/cursor는 별도 SQLite에 둔다. 한 registry에서 활성 계정 하나를 선택하며 계정별 기록은 섞지 않는다.
프로젝트 필터는 연결된 프로젝트 기록만 표시하므로 프로젝트에 연결되지 않은 Google 일정은 전체 프로젝트에서 확인한다.
[I3 계약](rd/i3_contracts.md)을 참조한다. 실제 Google 계정 연결 시험은 자동 회귀에 포함하지 않는다.

I4에서 새 일정의 **저장 대상**을 로컬만 또는 선택한 Google 캘린더로 지정할 수 있다.
기존 로컬 일정은 상세의 **Google에 추가**에서 대상과 내용을 확인하고 승인한다.
연결한 일정의 수정은 로컬 저장과 함께 전송 대기로 기록한 뒤 Google에 반영한다.
Google 변경은 수동 동기화 또는 앱의 주기적 수집에서 로컬 일정에 반영한다. 왕복 조회 후에도 하나의 일정으로 표시한다.
다른 앱이 작성한 미연결 Google 일정은 읽기 전용이며 반복/참석자/특수 일정의 자동 편집은 지원하지 않는다.

**Google 연결 일정·복구**에서 전송 대기·오류·충돌·결과 미확인을 확인한다.
오류는 **같은 작업 전송**, 결과 미확인은 **원격 결과 대조**를 사용한다. 대조 전에는 재전송하지 않는다.
전송 전 저장한 같은 Google ID와 작업 marker/내용/etag로 결과를 확인하며, 미반영을 확인한 경우에만 같은 ID의 명시 재시도가 가능하다.
충돌은 로컬/Google 내용을 함께 보여주고 **Google 내용 채택** 또는 **로컬 내용 Google 반영**을 다시 확인·승인한다.
확인 후 다른 변경이 생기면 덮어쓰지 않고 다시 확인한다. 전송 중 새 입력도 보존한다.

연결 일정의 로컬 삭제는 **화면 숨김**이며 Google 원본을 삭제하지 않는다. **화면에 다시 표시**는 로컬 표시만 복구하며 오프라인에서도 가능하다.
**Google에서 삭제**는 별도 내용 확인/승인을 거친다. Google에서 삭제/취소된 일정의 보관 내용을 다시 표시해도 원격 일정을 자동 부활시키지 않는다.
그 내용을 새 Google 일정으로 쓰려면 새 일정을 작성한다. task·메모·Jira 데이터와 참석자 목록은 업로드하지 않는다.

연결 정보와 불변 outbox는 기존 `calendar/` DB에 로컬 record와 같은 트랜잭션으로 저장한다.
이미 승인된 전송 대기는 앱 실행 중 수집 후 처리하며, 오류/결과 미확인은 명시 재시도/대조를 기다린다.
인증을 해제하거나 다른 계정으로 연결해도 다른 계정의 작업을 보내지 않는다. 재시작 후 남은 전송 중 상태도 먼저 대조한다.
[I4 계약](rd/i4_contracts.md), [I4 검증 증거](logs/test_evidence/261018_P018/validation.json)를 참조한다.

검증 산출물의 재사용을 막기 위해 전체 시험의 run_id는 마이크로초까지 구분한다.
`WSFLOW_RUN_EVIDENCE_DIR`로 전체 시험 출력 폴더를, `WSFLOW_GRAPH_EVIDENCE`로 graph 검사 출력 파일을 지정할 수 있다.

I5는 프로젝트의 **규칙 → 개발 환경 준비…**에서 사용한다. **빈 폴더 생성 + 등록**은 기존처럼 빈 폴더만 만든다.
개발 환경 준비에서는 다음 순서로 변경 내용을 확인·적용한다.

1. **노출 방식 → 관리 사본**을 명시 선택한다. 기존 진입점/유효 프로필을 채택할 경우 원문 보존 체크를 선택한다.
2. **ROOT 초기화 미리보기**에서 공통 프로필·얇은 진입점과 영향 프로젝트를 확인하고 적용한다. 기존 공통 규칙은 보존한다.
3. ROOT의 `skills/<name>/` 또는 프로젝트의 `skills/<name>/`에 둔 전체 패키지 중 필요한 스킬을 선택한다.
4. **프로젝트 준비 미리보기**에서 사용자 지침 diff와 선택 패키지 변경을 확인한 뒤 적용한다.

ROOT에는 `.wsflow/workspace.json`, 프로젝트에는 `.wsflow/project.json`과 소유 해시 manifest를 둔다.
프로젝트의 `AGENTS.md`/`CLAUDE.md`에는 공통·프로젝트 정본을 참조하는 관리 블록만 추가·갱신하고,
선택한 전체 패키지를 `.agents/skills/<name>`·`.claude/skills/<name>`에 관리 사본으로 노출한다.
기존 `.codex`·`.claude`·프로젝트 규칙·Jira state는 보존한다. 같은 이름의 기존 스킬/다른 원본은 자동 병합하지 않는다.
기존 도구별 스킬을 중립 정본으로 이관하거나 커스텀 프로필 경로/이동·UID 재결합을 바꾸는 자동 마이그레이션은 제공하지 않는다.

사본을 직접 편집하거나 파일을 추가하면 다음 계획이 충돌을 표시한다. 정본을 편집한 뒤 새 계획으로 갱신한다.
선택 해제는 manifest가 소유하고 바뀌지 않은 파일만 제거하며 원본과 기존 디렉터리는 남긴다.
관리 영역은 일반 **배포**로 덮어쓸 수 없다. 일반 문서 배포는 기존 동작을 유지한다.
적용 응답이 유실되면 **같은 승인 확인**, 중단된 작업은 **계획·복구 확인 → 이 작업 명시 복구**를 사용한다.
계획·파일별 이전 bytes·실행 저널은 사용자 데이터 영역의 operations SQLite에 보존한다. 중단 복구는 남은 단계를 이어가는 방식이며,
이후 사용자 수정과 일치하지 않으면 복구도 차단한다. 원본을 지우거나 디렉터리를 통째로 교체하는 동작은 없다.

현재 backend는 명시 선택한 관리 사본이다. 링크가 불가능할 때 자동 사본으로 대체하지 않는다.
Windows에서 실제 패키지·Junction·공백·worktree를 비교했지만 실제 Codex/Claude 세션 인식은 **미검증**이다.
링크를 포함한 원본 패키지와 canonical 경로가 다른 프로필은 보존하고 적용을 차단한다.
세션 시작 패킷·실제 관측/receipt·대시보드는 I6 범위다. [I5 계약](rd/i5_contracts.md)을 참조한다.

I6의 **규칙 → 세션 준비·관측…** 또는 대시보드의 **codex/claude 준비·관측**에서 도구별로 준비한다.
사용자가 확인한 버전과 실행 환경(CLI/desktop/IDE)을 입력하고 **이 환경의 새 세션 준비**를 누른다.
앱은 checkout의 정본·진입점·선택 패키지/사본·소유 manifest를 검사하고 시작 정보를 만든다.
**시작 정보 복사** 후 해당 checkout에서 도구의 새 세션을 직접 연다. 앱은 도구/스킬/명령을 자동 실행하지 않는다.
버전은 사용자 선언이고 host/OS는 wsFlow 서버 환경이다. 설치 버전을 자동 확인한 결과가 아니다.

**관측 자료 제출**에서 실제 확인한 문서 ID/sha256와 스킬 ID/tree_hash만 JSON으로 제출한다.
시작 정보에는 snapshot/profile 식별자와 보고 필드가 포함된다. 자체 보고의 origin은 `agent_report`이며
**에이전트 보고**로 기록한다. 실제 도구 출력 원본을 구조화하여 제출할 때만 `origin=tool_output`과
**도구 출력 관측**을 사용하고 실제 출력임을 확인한다. 이 경우에도 **사용자 제출·진위 독립검증 아님**을 표시한다.
선택 스킬 목록 인식은 스킬 실행·모든 지원파일 로딩·정책 준수의 증명이 아니다.
부분 보고는 부분 관측이며 개인 전역/플러그인 범위는 미확인이다. JSON의 지시·코드는 실행하지 않는다.

관측 이력에는 당시 도구/버전/환경·시각·scope·원본 근거 경로/해시와 오래된 이유가 표시된다.
ROOT·checkout/worktree·정본/support·사본·선택·프로필·host/OS·근거가 달라지거나 새 패킷을 준비하면
이전 기록은 보존하고 현재 인식 배지는 무효화한다. 같은 버전의 새 세션에도 새 관측을 남긴다.
근거는 최대 256KiB UTF-8 JSON이며 서버가 생성한 사용자 데이터 영역에 저장한다. 임의 파일 경로를 읽지 않는다.
준비/관측은 저장소 밖 immutable SQLite 기록이며 같은 자료의 수신 재시도는 같은 receipt ID다.
입력 중인 자료는 프로젝트/도구별로 유지한다. 준비/관측 과정에서 사용자 ROOT 파일을 갱신하지 않는다.

대시보드는 **개발 환경·세션 관측**과 **캘린더 동기화**를 별도로 표시한다.
파일 연결 확인만으로 실제 도구 인식·준수 완료를 표시하지 않는다. 실제 세션 자동 수집이나
숨은 문맥 갱신은 제공하지 않는다. [I6 계약](rd/i6_contracts.md),
[I6 검증 증거](logs/test_evidence/261018_P020/validation.json)를 참조한다. P-020 당시 후속 단계였던 I7의 현재 결과는 아래에 있다.


## I7 Git 동기화와 일일 업무 순환

상태 탭의 **fetch**로 원격 추적 ref를 갱신하고 성공 시각을 확인한다. **ff-only pull**은 먼저 fetch하고 빠른 병합만 허용한다. 변경/미추적 파일·충돌·분기·detached HEAD·upstream 없음은 중단 사유를 표시한다. 자동 stash/rebase/reset은 하지 않는다. push는 기존 별도 승인 흐름을 유지한다. fetch 시각을 확인하지 않은 ↑/↓는 최신 원격 상태의 증명이 아니다.

Jira 탭의 **오늘 일일 갱신**은 해당 Jira 계정 timeZone의 오늘에 대해 changes/open/CSC 미지정 조회 → vendor 집계 → 일지 → 불변식 검사 → 검증된 파일 반영 → 최종 state 교체 순서로 실행한다. Jira에는 쓰지 않는다. 저장하지 않은 메모가 있으면 먼저 처리한다. `.workflow/batches/<UUID>/`에 원본 전체 백업·raw·집계·일지·검사·단계별 실행 기록을 남긴다. 모든 단계가 성공한 뒤 watermark를 수집 시작 시각으로 확정한다. 실패는 watermark를 유지한다.

응답을 잃었으면 **배치 기록 확인**으로 최종 state marker를 확인한다. 실행 중/중단 가능 상태를 성공으로 취급하지 않는다. marker 없는 중단은 같은 구간을 새 명시 실행으로 다시 수집한다. 파일 반영 중 실패하면 검증된 일부 파일이 남을 수 있지만 원본 백업과 미확정 경고가 남고 watermark는 보존된다. 전원 장애와 비협조 외부 편집기까지 다중 파일 원자성을 보장하지 않는다.

대시보드 **전체 업무 상태**에서 날짜를 선택하면 신규/이어서/정체·미등재 CSU·제목 제외·CSC 미지정·미등록 파생·마감 누락 의심과 최근 배치/실패/미확인을 본다. 수행 업무·메모 건수와 **캘린더 상세**는 Calendar와 같은 `daily/YYMMDD.md` 원문에서 읽는다. 배치의 예정 업무를 수행 기록으로 세지 않는다. 이 화면 조회는 Jira 수집/파일 복구/watermark 갱신을 실행하지 않는다.

신규 업무의 연결된 미리보기는 현재 사용자 accountId를 담당자로 고정하고 에픽의 권한 내 전체 제목(완료/다른 담당자/제목 필터 밖 포함)을 조회한다. 같은 업무 폴더의 로컬 예약과 실행 직전 재조회로 순번 경합을 막는다. 외부 사용자/다른 PC와 Jira의 검색 색인 지연·권한 밖 이슈에 대한 원격 순번 원자성은 제공하지 않는다. 생성 후 직접 조회와 목록 재조회를 수행하며 생성 성공과 목록 가시성을 구분한다. 가시성 미확인은 재생성 사유가 아니다. 오프라인 제목 미리보기는 유지하고 생성 전에 연결 후 다시 미리보기한다.

다른 CSC의 대괄호 접두사가 붙은 제목도 같은 에픽 순번에 포함한다. 지원은 001~999이며 미해석 접두사 뒤 숫자/4자리 번호는 명시 오류로 중단한다. 미승인·만료 계획도 예약을 유지하므로 빈 번호가 생길 수 있다. 조회 내 중복 key·판정 필드 누락/손상은 불완전 경고와 배치 중단 사유다. 손상된 배치 기록은 경고하면서 확정 watermark를 보여 준다.

`sprint_mode=active`는 legacy 값이며 스프린트를 필터링하지 않는다. `all`/미지정도 전체 스프린트 대상이다. 그 외 mode는 지원하지 않는다. 사이트별 Jira 자격은 credentials의 `sites` URL exact match로 지정한다. 환경변수/평탄 자격 파일은 공유 계정 용도다.

`python tools/check_jira_vendor.py --upstream ../jiraWorkFlow/jiraWF`로 모체 commit과 vendor 9파일을 대조한다. 모체 없이 배포된 경우 `--upstream`을 생략해 고정 manifest와 비교한다. I7 계약은 [rd/i7_contracts.md](rd/i7_contracts.md), 결과는 `logs/test_evidence/261018_P021/validation.json`이 연결하는 새 보고서를 따른다. 실제 Jira/Google 계정·Codex/Claude 인식·타 OS·CI 수용은 별도다. 실행 중인 이전 서버는 새 코드를 사용하려면 다시 시작해야 한다.
