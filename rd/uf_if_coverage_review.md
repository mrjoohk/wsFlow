

## P076 UF-ST-01 Gate G2

[scanner 전체 호출 계약](261106_P076_scanner_writer_contracts.md)을 기존 workspace 검사 route에 연결했다. userdata 선검사·barrier·actual child·UTF8/argv/cwd/nonzero·timeout 직접자식 종료·결과 세 필드·중첩/호출시점 domain·미존재/unknown·finish 실패를 새 시험으로 매핑했다. 이 bounded UF에 UNCOVERED/REDUNDANT 없음, G2 PASS. C3와 Jira 전체 호출은 별도 후속이다.

P076 UF-ST-02 보강: 독립 검토 D1-②-01의 host 선행 admission을 read-only hook으로 처리했다. Backend.dispatch와 HTTP 최초 activity 전에 검증하며 실제 host·HTTP same/inside·정규화 입력, 정상 자식 효과 및 hook 제거 대조를 새6시험·3변이에 연결한다. G2 PASS, 본 bounded 변경의 UNCOVERED 없음. 다른 API/기동 전 복구는 후속 범위다.


## P077 Jira Gate G2

UF-JW-01~03은 [Jira 호출 계약](261107_P077_jira_writer_contracts.md)의 승인/전송/대조/복구, 상위 호출 잠금 순서, host/HTTP 경계를 회귀 15개에 연결한다. 이 범위의 G2 PASS다. C3는 후속이다.
