# V0+V1 계약·기준 동결·CI 검증 — P-023

요청: V0+V1 실행. 과거 보고서/증거·기존 사용자 파일 및 애플리케이션 동작을 보존한다. 사용자가 지정한 `https://github.com/mrjoohk/wsFlow`의 별도 `codex/v0-v1-*` 브랜치를 사용하고 원격 main·기존 LICENSE와 원 작업 폴더의 Git 상태를 보존한다.

## 지원 결정과 판단 근거

Git은 기존 명시 fetch/pull/add/commit/push API를 유지한다. fetch 시각과 ff-only·dirty/non-ff/충돌 차단은 지원한다. Git의 서버 보관 불변 plan/operation UUID·durable claim·응답 유실 결과 대조/repair는 지원하지 않는다. Git ref 동작을 Jira 코멘트의 비멱등 효과와 동일하다고 가정하지 않는다. **CTX-14의 원 요구 GAP F-032는 남는다.** 이번 결정은 미충족을 숨기지 않고 검증 후보의 지원 범위를 확정한 것이며 원 요구를 삭제하지 않는다. Git 작업 엔진 확장은 별도 구현·수용이 필요하다.

판단 근거: 요청은 차이·지원 범위 정리와 기준/CI 검증이며, 기존 기능을 보존하면서 미검증 Git 실행 엔진을 동시에 추가하면 기준이 바뀐다. 기존 실행 경로를 그대로 검증하는 단일 명확 후보를 선택한다. 새 알고리즘/backend 우열 실험은 하지 않는다.

## UF·I/O·검증 소유권

| UF | 입력 → 출력 | 책임·검증 |
|---|---|---|
| V01-U1 source | 명시 allowlist·현재 코드 → normalized SHA256 manifest, raw archive/hash, 별도 candidate | .git/사용자 .codex/.claude/자격/원본 업무 제외. 새 목적지에만 생성. 원본 바이트 손실0·소스 누락/추가/변경 감지 |
| V01-U2 execute | 동결 source + Python/runtime·고유 출력 → command exit/log/env/test/browser 결과 | 사용자 site/자격/env를 분리한 venv·별도 checkout. 자식 시간 제한·own process tree 정리. 과거 산출 경로 재사용0 |
| V01-U3 mutations | I0~I7 고정 변이 목록 → 정상 baseline/각 mutant/strict 결과 | 이전 spec 보존, 현재 코드에 적용되는 새 spec·기존 ID 대응. stale/생존/timeout/무시험 baseline을 PASS로 승격0 |
| V01-U4 gates | manifest·실제 실패 unittest·실제 생존 mutant → gate 비정상 종료 | 각 실패 원인별 실행 증거3종. fixture 입력임을 명시하며 제품 결함으로 세지 않음 |
| V01-U5 CI | 동일 runner·고정 dependency·workflow → provider job/아티팩트 또는 미실행 사유 | Python3.10/3.14, vendor·415회귀/82화면·변이·환경/범위. 로컬 runner를 hosted CI로 표시0. 외부 live 자격 사용0 |

chain: U1의 manifest → U2/U3 실행·신선도 → U4 게이트 → U5 provider 수용. **Gate G2 PASS**: 책임/입출력 연결 판정이며 실행 성공 선언이 아니다.

## 동결·증거 정책

실행 코드·시험·CI spec/runner/workflow는 LF 정규화 hash와 전체 파일 목록으로 잠근다. archive는 원래 바이트를 유지하고 raw hash도 기록한다. 출력·런타임·node_modules·.git는 코드 fingerprint에서 제외한다. 후보는 별도 Git 저장소/깨끗한 clone으로 만들 수 있으며 사용자 원 저장소의 index/branch/commit을 변경하지 않는다. 미커밋 구현은 main HEAD와 별개 snapshot commit으로 식별한다.

CI evidence pack의 JSON subset YAML은 최소 metric_id/value/threshold/comparison/status/unit 및 계획/증거 provenance를 검사한다. 과거 P021 baseline과 새 V1 실측은 분리한다. Gate G4는 schema/provenance 검사이며 실계정/CI 성공 판정이 아니다.

Windows Python3.10은 최소 호환성 시험용이며 운영 권장 버전으로 신규 배포하지 않는다. 3.14.3 기준과3.10.11 호환 런타임을 사용자 전역 설치/설정 변경 없이 분리한다. Python 코드는 표준 라이브러리를 import하지만 IANA 시간대를 쓰는 Calendar/Jira 기능에는 시스템 시간대 DB 또는 tzdata 배포 데이터가 필요하다. Windows 검증은 tzdata를 명시 설치한다. Node/Playwright/브라우저는 시험 전용이며 이 시간대 데이터 전제와 구분한다.

최종 수용 정본은 새 P-023 validation과 보고서다. 원격 CI 권한/저장소가 없으면 구성·로컬 재현만 보고하고 V1의 provider 실행/아티팩트 수용을 UNKNOWN/PARTIAL로 둔다. 이전 F/D/M 상태를 실제 CI verified로 소급 올리지 않는다.

CI 검증 브랜치의 push는 정상 회귀2개와 의도한 실패 게이트3개를 실행한다. 후자에는 continue-on-error를 쓰지 않으므로 workflow 전체는 failure가 예상된다. 수용 판정은 정상2개 success, 통제 주입3개 failure 및 각 gate.json의 정확한 원인·artifact를 대조한다. 일반 pull_request는 의도한 실패 job을 실행하지 않는다. 비밀·업무 데이터가 없는 allowlist만 게시한다.

독립 delta 검토에서 도구의 환경 변경 후 cache 재사용과 전체 skip baseline의 거짓 승격을 재현했다. 기존 스킬 원본은 보존하고 `tools/ci/mutation_runner.py`의 CI 사본만 보강한다. 이 사본은 매번 새 결과 파일을 요구하며 resume/budget/summary·기존 out 재사용을 지원하지 않는다. 최종 신선도는 동결 manifest·새 baseline/실행 결과·source 재검사로 확인한다. baseline의 실행된 본문 수가0이면 거부한다. 변경 provenance는 `ci/runner_provenance.json`에 기록한다.

명령 제한·중단은 taskkill의 해당 PID tree와 직접 Popen handle fallback을 사용한다. Windows 제한 호스트가 tree 정리를 거부한 관측과 프로세스 관리가 허용된 로컬/hosted 실행을 구분한다. 임의 외부 프로세스·Windows 모든 보안 경계의 손자 프로세스 정리를 보장하지 않는다. 실패·artifact 복사 오류는 result.json에 남긴다.

V1의 Windows 시험 임시 디렉터리는 실제 NTFS의 canonical full path를 사용한다. hosted 기본 TEMP의 RUNNER~1 별칭에서 기존415시험 중 경로 문자열/Reader 경계5건이 실패한 결과는 보존한다. child TEMP/TMP를 `Path(tempfile.gettempdir()).resolve()`로 명시하고 원본 시험을 전부 다시 실행한다. 제품 Reader가 별칭 경계를 직접 입력받을 때 정상 파일을 차단하는 범위는 별도 미해결 한계이며 이를 고쳤다고 주장하지 않는다. 등록 경로를 실경로로 처리하는 표준 경로와 alias 입력 전체 수용을 구분한다.
