# P077 Jira 쓰기 호출 보호

P076 commit 970845dd의 CI 42180885512에서 두 정상 프로파일과 원본 ZIP 9개를 수용했다. 기존 브라우저에서 호출하는 마감, 생성, 결과 대조, 로컬 복구를 대상으로 한다.

| UF | 호출과 결과 | 수용 기준 |
|---|---|---|
| UF-JW-01 | jirawrite.approve, execute, reconcile, repair의 기존 인수와 결과 | userdata의 WF_HOME 포함 여부를 읽기 전용으로 검사한 뒤 공통 쓰기 root를 잡는다. 업무 폴더 잠금, 승인/결과 JSONL, SQL, 전송 및 대조, 반환까지 같은 root를 유지한다. 배리어 거절 시 파일과 전송에 효과가 없어야 한다. |
| UF-JW-02 | writeplans의 마감·생성 계획 및 적용, newtask의 생성·결과 기록 | standalone 진입에서도 공통 쓰기 root를 WF_HOME 잠금보다 먼저 잡는다. 기존 중첩 호출은 한 root를 공유한다. 생성 결과의 state.json 반영과 기록 표시를 포함한다. closeflow 배치는 항목별 결과를 같은 root에서 모은다. |
| UF-JW-03 | work-calendar backend와 인증된 HTTP의 Jira 계획·적용·복구 요청 | P076의 선택적 읽기 전용 preflight를 재사용한다. 최초 host activity 전 현재 프로젝트의 WF_HOME을 검사한다. 실제 실행에는 기존 activity와 writer admission을 적용한다. 잘못된 설정은 direct backend에서 제어 파일 0, HTTP에서는 초기 파일 보존 및 요청 중 lock I/O 0이다. |

실제 SQLite와 업무 파일로 승인 선기록, 응답 유실, 미확정 실행의 재전송 거절, receipt 대조, 로컬 기록 재실행의 멱등성을 확인한다. 원격 효과는 소유 합성 전송 경계에서만 검증한다. 응답 손실 이후 성공 여부는 기존 계약대로 실제 receipt 확인에 의존한다.

HTTP 선검사는 기존 초기화 lock을 소급 변경하지 않는다. 읽기 전용 상태 조회 및 대상 Jira 경로 밖의 기존 동작은 이 계약의 범위 밖이다. C3 일관 사본과 장애 복구는 후속 단계다.

G2 PASS. 세 UF의 진입, 잠금 순서, 효과 및 반환 경계와 실패 경로를 아래 회귀 시험에 연결한다.

검사와 admission 사이에 프로젝트 연결이 바뀌면 실제 요청 경로에서 현재 WF_HOME을 다시 검사한다. 이때 선행 admission 메타데이터는 생길 수 있으며 변경된 WF_HOME 잠금이나 Jira 전송에는 진입하지 않는다.

호스트 쓰기 콜백이 없는 직접 라우트도 같은 배리어에 참여한다. 생성에서는 합성 POST와 실제 state.json 및 local_recorded 반영이 같은 root에 속한다.

기존 Jira 소켓의 읽기 시간 제한은 요청 전체의 총시간 한도가 아니다. 느린 응답이 이어지면 쓰기 root가 계속 열려 배리어가 진입을 거절할 수 있다. 전체 시간 한도와 응답 유실의 처리 정책은 C3 중단·복구 설계에서 다룬다.

쓰기 root를 확보한 뒤 WF_HOME 별칭이 가리키는 실제 경로를 검사한다. 통과한 실제 경로를 업무 잠금과 후속 호출에 전달한다. 쓰기 root 획득 전에 Windows junction이 사용자 데이터의 상위 폴더로 바뀌면 업무 잠금이나 Jira 전송 전에 거절한다. 실제 경로 자체를 외부 프로세스가 교체하는 경우는 별도 복구 범위다.
