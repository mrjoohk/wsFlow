

## P076 scanner 도구 실행 writer

UF-ST-01은 [상세 계약](261106_P076_scanner_writer_contracts.md)의 userdata 선검사→공개 진입→도구 선택/존재 검사→실제 자식/출력/timeout 처리→각 결과 필드→반환 전체를 보호한다. 기존 명령/결과 알고리즘은 유지한다. 검사기가 파일을 쓸 수 있으므로 단순 조회와 구분한다. 일반 descendant·원자 rollback·C3 완료는 범위 밖이다.

UF-ST-02는 같은 계약의 host/HTTP 선검사다. 선택적 read-only backend preflight를 최초 activity 전에 호출하고 기존 admission을 유지한다. 프로젝트 내부 userdata와 경로 정규화는 실제 ModuleHost 및 loopback HTTP로 검증한다. 서버 초기화의 기존 lock 생성까지 소급하지 않는다.


## P077 Jira 호출 보호

UF-JW-01~03의 원본은 [Jira 호출 계약](261107_P077_jira_writer_contracts.md)이다. Jira 공개 함수와 상위 계획·생성 함수는 userdata 선검사, 공통 쓰기 root, WF_HOME 잠금 순서를 적용한다. HTTP는 최초 activity 전에 검사하고 실제 업무 경로에서 현재 WF_HOME을 다시 검사한다.

직접 Jira route, closeflow 배치와 standalone 생성의 SQL·파일 끝 경계를 보강했다.

쓰기 root 안에서 검사한 WF_HOME 실제 경로를 업무 호출에 전달한다.
