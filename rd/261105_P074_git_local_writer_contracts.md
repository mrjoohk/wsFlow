# P074 로컬 Git 변경 전체 호출 보호 — UF/G2

P073 CI41850282622의 sourceb952/commit897b7fa를 기준선으로 삼는다. 기존 브라우저 GUI와 Git 알고리즘·옵션·응답은 유지한다. 원격 push·검사 도구·Jira 전체 호출·C3는 후속이다.

| UF | 부모·입출력 | 전체 실행 계약 | 검증 |
|---|---|---|---|
| UF-GL-01 | UF-I7-01: repo/name/url → remote_add/set_url/remove의 기존 None | 읽기 전용 userdata 검사 후 현재 domain 재사용 또는 call-time domain으로 전체 Git config/refs 변경과 반환을 보호한다. 잘못된 userdata는 제어 파일 없이 거절한다. 정상 실행의 기존 이름 검증/GitError 의미를 유지한다 | 실제 C-temp Git config·refs, barrier 거절6, 실행결과 보류·중첩·독립 domain·기존 회귀 |
| UF-GL-02 | UF-I7-01: repo/paths/all_/message/add_all → 기존 None 또는 commit dict | index·objects/refs·UTF-8 임시 메시지 생성/삭제 시도·최종 HEAD 조회·응답까지 하나의 writer root. Git 실패와 정리 실패의 기존 처리 유지. root 완료는 Git rollback을 의미하지 않음 | 실제 index/HEAD/message bytes, 임시 정리와 HEAD 반환 보류, 실패 시 정리, entry 및 Git-only 약화 변이 |
| UF-GL-03 | UF-I7-01: repo/min_age_sec → clean_locks 기존 dict | lock 조사·프로세스 존재 확인·정해진 나이 비교·삭제/skip·반환까지 같은 root. 기존 age/objects 제외/Git 실행 중 거절 유지 | 소유 old/fresh/objects lock, 삭제 직전 Event, 합성 외부 프로세스 검사, 실제 read-only git_dir |

`_git_writer.invoke`는 기존 repo 인자 뒤의 positional/keyword를 원형대로 전달하도록 확장한다. 기존 fetch/pull의 읽기 전용 Store 검사와 같은 협조 writer_function을 재사용한다. 개별 공개 함수 본문은 바꾸지 않고 장식만 추가한다. 순수 조회와 run_git 전체에는 장식하지 않는다.

정상 userdata에서 기존 입력 오류는 기존 GitError로 끝나며 업무 파일을 바꾸지 않는다. 입력 검증은 기존 본문에 있으므로 입장한 실패 호출도 제어 epoch의 시작/종료를 기록할 수 있다. userdata 오류·barrier 거절과 입력 오류가 동시에 있을 때는 선행 userdata/admission 거절이 우선한다. 잘못된 인수의 exception 우선순위를 별도 API로 확장하지 않는다.

기존 commit은 임시 unlink의 OSError를 무시한다. 이 단계는 삭제 시도와 해당 예외 처리까지 root를 유지하지만 삭제 성공을 보증하지 않는다. 같은 경우 HEAD가 바뀌고 ok=True·임시 메시지가 남는 기존 결과도 유지한다.

Gate G2: PASS. 세 UF의 진입/효과/정리/최종 반환 책임이 테스트와 기존 UF-I7-01에 연결된다. 범위 내 누락/중복 없음. G2는 설계 연결이며 실행 수용이 아니다. 외부 비협조 writer·모든 descendant·다중 domain 원자성·완전 snapshot·원격 rollback은 보장하지 않는다.
