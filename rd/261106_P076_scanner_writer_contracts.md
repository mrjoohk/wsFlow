# P076 scanner 검사기 전체 호출 보호 — UF/G2

P075의 정확한 commit1a234cea/source58f7은 CI42171502427 정상2·원본ZIP9 strict PASS다. 이 변경은 별도 사본에서 준비·검증한 뒤 부모 수용 후 정본 제품에 통합했다. 새 소스의 전체 CI 수용은 별도이며 기존 브라우저 GUI를 유지한다.

| UF | 부모·입출력 | 처리·실패 계약 | 검증 |
|---|---|---|---|
| UF-ST-01 | UF-WS-03 및 기존 rules/run route: run_tool(project_path,tool,timeout=120) → 기존 dict/예외 | read-only operations.Store의 userdata 경로 선검사 후 기존 writer_function으로 현재 root 또는 호출시점 default domain을 사용한다. 도구 경로 선택·존재 검사·실제 Python 자식·출력 수집/timeout 직접자식 종료 처리·결과 필드 구성·공개 반환 전체를 같은 협조 root에 둔다. barrier 거절 전 spawn/도구 효과0. 인수·cwd·UTF-8 환경·반환/timeout124·기존 OSError 전파 유지 | 실제 소유 C-temp 검사기의 argv/cwd/env/파일효과, live 직접자식, 각 returncode/stdout/stderr 접근 보류, 실제 timeout, launch 실패, 중첩/호출시점 domain, unknown/missing/no fallback, finish 저장 실패·자동 재실행0 및 보호 제거/부분 보호 변이 |
| UF-ST-02 | 기존 workspace Backend.dispatch 및 Handler._route → 선택적 preflight(request,method,path) | 활성 backend가 제공한 읽기 전용 hook을 최초 services.activity 전에 호출한다. hook 없는 backend는 기존 경로를 유지한다. workspace는 raw Backend.dispatch에서도 최초 activity 전에 같은 검사기를 호출한다. POST /api/projects/:id/rules/run만 프로젝트를 읽어 operations.Store 선검사하며 dispatch와 동일한 empty-segment 정규화를 사용한다. 이 hook은 실행 허가가 아니며 뒤의 activity/writing·기존 route 검사를 유지한다 | 실제 ModuleHost/Services/Backend의 same/inside userdata·canonical/중복/끝 slash 요청은 파일0/자식0. 실제 loopback HTTP는 요청 전 파일 bytes 보존·요청 중 제어 lock I/O0/자식0 및 HTTP409. 유효 host/HTTP 실제 자식 효과·단일 epoch, barrier 및 없는 프로젝트의 거절. host/HTTP hook 제거·정규화 약화 변이 |

Gate G2: PASS for these bounded isolated UFs. run_tool은 프로젝트 자체 검사기를 실행하며 검사기는 증적 파일을 쓸 수 있다. 조회용 scan/rules_report는 이 변경의 보호 대상이 아니다. 검사기의 개별 파일 변경 원자성·출력 내용의 정당성·모든 hook/일반 descendant 종료·외부 도구 writer·일관 사본/C3를 보장하지 않는다.

유효 userdata에서 unknown tool/없는 script의 기존 오류 dict를 유지한다. 이 공개 호출도 협조 root에 참여하므로 barrier 중에는 호출 전에 거절할 수 있다. userdata가 프로젝트 자체/내부인 잘못된 설정은 제어 파일 생성 전 StateConflict로 거절한다. 새로운 plan/UUID/receipt나 자동 재시도는 추가하지 않는다. root 종료는 도구 성공이나 실패한 파일 효과의 rollback이 아니다. 기존 subprocess.run의 직접자식 timeout 처리는 유지하며 전체 descendant 수거로 표시하지 않는다.

최초 서버 초기화/bind_services는 별도 기존 activity이며 lock을 만들 수 있다. UF-ST-02의 HTTP 거절 보장은 그 초기화 이전까지 소급하지 않고 해당 요청의 최초 activity/제어 I/O 전에 선검사한다는 뜻이다. 시험은 초기화된 파일의 경로·bytes를 보존하고 실제 요청 중 fileio.locked 호출0도 확인한다. raw/direct backend는 초기화 전 빈 프로젝트의 파일0을 확인한다. hook이 적용되지 않은 다른 API의 userdata 검증이나 모든 서버 기동의 무쓰기까지 일반화하지 않는다.

선검사 때문에 잘못된 userdata의 storage conflict409가 중지 상태503보다 먼저 반환될 수 있다. 이것은 실행 허가가 아니다. 유효 설정의 중지/일시중지/미기동/오류503 및 비활성409 거절과 자식0은 유지한다. hook 없음/None/비호출 값은 기존 dispatch를 유지한다. 이 hook으로 다른 API의 모든 오류 우선순위를 재정의하지 않는다.
