# P072 raw Git 쓰기 보호 — 구현 계약·Gate G2

기존 UF-I7-01/G2와 D162의 [GT01~05 계약](261102_P069_git_writer_contract.md)을 두 공개 진입점에 적용한다. 브라우저 GUI·정상 응답·Git 옵션은 유지한다. 상위 HTTP/Service 쓰기 보호와 별도로 raw 호출도 같은 협조 domain에 참여시킨다.

| UF | 부모/입출력 | 처리·실패 계약 | 검증 소유권 |
|---|---|---|---|
| UF-GT-01 | UF-I7-01: checkout Path → 기존 fetch 결과 dict | 기존 writer_function으로 전체 fetch를 감싼다. 상위 root 재사용, 독립 호출의 call-time default_domain. admission 거절은 Git/동기화 기록 효과 전에 WriterUnavailable. Git 실행·기록 교체·반환까지 유지. 기존 Git 실패 결과와 저장 예외 전파 유지 | GT01/GT02/GT04, tests/test_git_writer.py |
| UF-GT-02 | UF-I7-01: checkout Path → 기존 pull result/status dict | 같은 보호로 초기 상태→fetch→고정 ff-only pull→최종 status를 감싼다. dirty/conflict/detached/no-upstream/non-ff 보호 유지. stash/reset/rebase/원격 push 추가 없음 | GT01/GT03/GT04/GT05, tests/test_git_writer.py 및 기존 test_i7.py |

Gate G2: 두 UF의 진입/효과/최종 반환 책임과 GT01~05를 연결했다. 읽기 status/log/sync_info는 쓰기 epoch를 올리지 않는다. 정상·반환 실패·예외의 종료, 중첩·call-time domain, 실제 로컬 bare/checkout의 refs/HEAD/index/worktree, 실행 중 자식·JSON 교체·최종 status 구간이 검증 대상이다. 범위 내 UNCOVERED/REDUNDANT 없음. G2는 설계 연결 판정이며 실행 수용과 구분한다.

두 공개 진입점은 읽기 전용 `_sync_path`/Store 구성 검사로 기존 userdata 권한을 먼저 확인한 뒤 기존 writer_function을 호출한다. userdata가 repo와 같거나 내부이면 StateConflict를 내고 업무·admission 제어 파일 모두 만들지 않는다. 그 외 실행·반환 경계는 동일하다. import 때 domain을 고정하지 않는다.

검증은 새 소유 로컬 저장소만 사용한다. gitops 본체를 fake로 대체하지 않고 실제 호출을 관찰·제한된 이벤트로 잠시 보류한다. 모듈 경계 밖 일반 Git 쓰기·외부 비협조 프로세스·전체 descendant·완전 snapshot/복구는 이 단계의 완료로 선언하지 않는다. 제품 변경 전 회귀의 실패를 보존하고 장식 제거/반환 경계 단축/보호 옵션 약화 대조를 실행한다.
