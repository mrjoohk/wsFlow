# P075 Git push 전체 호출 보호 — UF/G2

기준선은 P074 commit9cd7de2/source1c7의 CI42163049148 정상2·원본ZIP9 strict PASS다. 브라우저 GUI와 기존 Git 명령/결과는 유지한다. 실제 외부 저장소·계정은 사용하지 않고 소유 C-temp의 checkout/bare만 사용한다.

| UF | 입출력·부모 | 처리·실패 계약 | 검증 |
|---|---|---|---|
| UF-GP-01 | UF-I7-01: raw push(repo,remote,branch,set_upstream) → 기존 dict/예외 | 기존 `_git_writer`로 userdata 선검사→현재/호출시점 domain→입력 검증→실제 push 자식→결과 필드 구성→반환 전체를 보호. admission 거절 전 Git/원격 효과0. 기존 인수·명령·GitError/비정상 반환 유지. 자동 재호출 없음 | 실제 receive-pack 자식과 결과 필드 경계, 원격 refs/로컬 upstream, barrier·잘못된 userdata·오류·중첩/호출시점 domain·보호 제거/약화 대조 |
| UF-GP-02 | 기존 UF-CW-01~04/Service.preview·apply·check의 push | 기존 writer_method 전체 보호·불변 plan/claim·receipt 선기록·SQL 종료·명시 readback 및 executing/uncertain 재전송 차단을 회귀 검증. 이 경로의 제품 변경 없음 | 실제 원격 효과 후 receipt 보류, receipt 후 SQL 저장 유실, receipt 없는 응답 유실→재적용/우회 차단→명시 readback, 기존 다중주소/OID/upstream 회귀 |

두 경로는 구분한다. 브라우저의 Service 경로는 고정 ref/옵션과 작업 ID를 가진다. legacy raw 함수는 기존 저수준 Git 어댑터이며 신규 plan/UUID/receipt를 만들지 않는다. raw의 결과 유실을 Service의 durable uncertain 기록으로 포장하지 않는다. wrapper는 자동 재전송하지 않으며 raw 오류 이후 원격 결과를 단정하지 않는다.

root 종료는 협조 실행의 종료 기록이지 원격 rollback/성공 판정이 아니다. 비정상 push나 응답 유실은 원격이 바뀌었을 수 있다. 기존 Service는 readback으로 모든 계획 대상의 URL/ref/OID와 필요한 upstream을 확인하며 미입증 상태를 유지한다. 일반 외부 writer·Git hook/SSH의 모든 descendant·원격 다중자원 원자성·C3 snapshot은 이번 수용 범위 밖이다.

Gate G2: PASS. raw 진입/실행/결과 구성과 기존 Service의 승인/claim→효과→receipt→SQL→조회/복구의 책임을 위 시험에 연결했다. 범위 내 누락/중복 없음. Service는 기존 구현을 유지·회귀 확인하며 새 코드로 세지 않는다. scanner/Jira 전체 호출과 C3~6는 후속이다.

결과 구성은 returncode뿐 아니라 stdout/stderr 접근까지 각각 독립 Event로 보류한다. returncode까지만 root에 포함한 부분 보호도 음성 대조에서 검출해야 한다. 시험 보강이며 제품 알고리즘 변경은 아니다.
