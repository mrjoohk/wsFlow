# P073 OAuth 콜백 시험 완료 경계

수정 범위는 `tests/test_package_activity.py`의 기존 시험 하나다. 제품 API·활동 게이트·OAuth 처리·제한시간·합격 기준·시험 수·선정 변이는 유지한다.

## UF-PA-T01 / IF-CAL-03·모듈 실행 정지

- 입력: 합성 OAuth 전송, 실제 loopback HTTP listener, `ActivityGate`, 이미 입장한 콜백.
- 출력: 입장 후 pause는 새 호출을 거절하며 기존 콜백은 자격 저장·응답 후 활동을 해제한다. 완료 후 in_flight=0과 실제 worker 종료를 확인한다.
- 실패: HTTP 200 수신은 서버 `do_GET` 반환/컨텍스트 해제의 증거가 아니다. 클라이언트 thread join만으로 gate=0을 즉시 단정하지 않는다.
- 절차: 기존 전송 중단/입장 수/새 호출 거절/HTTP 200/자격 저장 검사를 보존하고, `gate.pause(2)`의 명시적 drain 완료를 확인한 뒤 in_flight=0·close 결과를 확인한다. 고정 sleep·제품 타이머 변경·조건부 skip은 추가하지 않는다.
- 검증: 실제 콜백의 응답 후 root 종료만 Event로 지연하는 소유 탐침에서 수정 전 실패/수정 후 성공을 확인한다. root 해제 누락은 제한 내 실패해야 한다. 두 Python의 해당 파일과 기존 runtime admission 음성 대조를 실행한다. 전체 hosted CI는 별도 수용한다.

Gate G2: PASS. 기존 callback 입장→전송→자격 저장→응답→root 종료→worker 종료 책임과 시험 관측을 연결했다. 이 계약은 시험 관측 경계의 보강이며 새로운 제품 동작 구현 계약이 아니다.
