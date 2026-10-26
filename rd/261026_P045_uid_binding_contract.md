# P045 F185 보존형 calendar identity 계약

판단 근거: P044의 recreated_project.json(같은ID/경로·새UID에서구일정2/2), stale_uid_plan.json(구계획실행1), 보존형UID계획 D104. 과거UID 자동추정/일괄 backfill은 금지되어 선택지가 아니며, 서버관측·불변계획·명시재연결이 단일 구현 경로다. 새 사용자자료/실계정 이관은 하지 않는다.

1. 관리 프로젝트는 등록 ROOT·checkout 경로, schema1 profile의 비어있지 않은 project/root UID, 부모결합, 등록중복·UID중복·일반파일경계와 같은 관측의 변경없음을 읽기전용 확인한다. 지침/스킬본문이나 도구 로딩은 조사하지 않는다. 오류/손상/중복은 unavailable이며 새연결을 승인하지 않는다. UID가없는 legacy 프로젝트는 path+실제디렉터리 dev/ino라는 약한 경계를 명시한 legacy_path 모드다. profile을 작성/수정하지 않는다.
2. 신규 record/계획의 프로젝트 reference는 서버에서 발급한다. 관리형은 path+root/project UID, legacy형은 path+directory identity다. 계획은 별도의 observation hash로 해당 metadata와 경계를 고정하며 apply 직전 재확인한다. 이미 성공한 operation은 먼저 기존결과를 돌려줘 응답유실 재시도를 보존한다.
3. UID결합버전이없는 미실행 local/Google 계획은 내용을 바꾸지 않고 재미리보기를 요구한다. schema/본문/revision/ID/queued/uncertain/원격ID를 읽기중 자동변경하지 않는다. 과거문자열 reference는 unverified로 보관하며 프로젝트선택집계에서 제외한다. 전체조회·날짜상세에는 보존값과 확인불가상태를 표시한다. 기존UID reference도 현재UID와다르면 unavailable이다.
4. update/delete의 유지된 연결은 과거reference를 그대로 보존한다. 본문편집·숨김은 가능하며 소속을조용히바꾸지않는다. update 요청의 `rebind_project_ids: string[]`는 payload의 연결ID이면서 기존record에 있던 ID만 허용한다. 선택한ID의 현재유효한서버identity로 재연결하는 사실을 preview에 명시하고 새승인 뒤 적용한다. 제거/신규추가도 계획에서 명시되며 기존record의 모든연결과 신규연결의 관측을 실행까지 고정한다. client UID/reference 주입은 거부한다.
5. 프로젝트연결만바뀌고 Google 전송본문이같으면 queued/uncertain의 link/outbox/intent를 건드리거나 새전송을 만들지 않는다. clean synced·pending없음·base_local_revision=current.revision이면 메타데이터저장 후 base_local_revision만 새revision으로 진행해 원격단독변경을오충돌로만들지않는다. remote ID/etag/본문은보존한다. 실제내용편집에대한 기존명시적저장/동기화계약은 유지한다. Google 계획도 읽기/권한조회 전과 로컬commit 직전에 project binding을 확인한다. 기존 성공결과 재조회는 허용한다.
6. 월/주칸은 기존 일정내용·업무/메모건수 구조를 유지한다. 확인불가소속 경고·전체조회안내·명시재연결 선택/승인표시는 기능적 보강이며 외관개선은 아니다. 자료를 조회/수정한 뒤 편집초안을 보존하는 기존행동을 유지한다.

검증: 새관리UID(동일path/id), legacy→managed, legacy目录교체, 누락/손상/ROOT변경/중복, metadata경합, 구immutable계획거부와성공캐시, 기존문자열행/본문편집/명시재연결·CAS/동시저장, HTTP토큰·클라이언트주입거부, Google연결·outbox보존, 전체 I0~I7/화면회귀·선정되돌림음성대조·깨끗한패키지/실제CI를 분리수용한다. 실제다른OS사용자·미확보실계정표본·도구인식은자동PASS하지않는다.

진척분모는 기존31요구·46수용목록을 유지한다. 새source 동결 뒤 기존14수용은 자동승계하지 않고 각근거와source를대조한다. V7-03은F185해소만으로PASS하지않으며 실제새OS환경등잔여조건을PARTIAL로남긴다. D092/D100보류·전체완료false·GUI외관개선대기를유지한다.
