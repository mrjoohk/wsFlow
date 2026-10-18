"""`--check-jira` — 토큰 값을 화면에 내지 않고 연결을 확인한다.

토큰 파일은 저장소 밖에 있고(D-007) 읽을 수 있는 사람도 사용자뿐이다. 그래서
"제대로 들어갔는지"는 **값을 보여주는 방식이 아니라 실제로 한 번 써보는 방식**으로
확인한다. 순서가 곧 실패를 가르는 순서다 — 자격 → 인증 → 스코프 → 필드 경로.

마지막 단계가 U-002 다. `jira_fields` 가 어느 경로로 값을 찾았는지(`field_paths_used`)
**첫 실조회에서 반드시 확인**해야 한다. 경로가 어긋나면 조회는 성공하는데 집계가
0건이 되고, 일지에는 "변동 없음"으로 남는다.

기본은 **읽기 전용**이다. `--fetch` 를 줄 때만 `.workflow/raw/` 에 원본을 남긴다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import auth, jiraapi

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "jira_worklog"


def _vendor():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    import jira_fields  # noqa: PLC0415
    return jira_fields


# norm_issue 가 못 찾으면 이 폴더의 판정 자체가 무너지는 경로. 나머지는 비어 있을 수 있다.
ESSENTIAL_TAGS = ("issue_list", "key", "summary", "status", "status_category", "updated", "type")


def absent_fields(issues: list, requested: list[str]) -> list[str]:
    """요청했는데 응답의 ``fields`` 에 **키 자체가 없던** 이름들.

    이 구별이 필요한 이유: ``jira_fields.dig`` 는 값이 ``None`` 이어도 ``<none>`` 으로
    기록한다. 그래서 "마감일을 아무도 안 넣었다"(정상)와 "그 필드가 응답에 아예
    없다"(요청이 틀렸거나 사이트 형태가 다르다)가 같은 표시로 나온다. 둘을 뭉뚱그려
    경고하면 정상 상태에서 매번 경고가 떠서, 진짜일 때 아무도 안 본다.
    """
    seen = set()
    for d in issues:
        f = d.get("fields")
        if isinstance(f, dict):
            seen |= set(f)
    return [k for k in requested if k not in seen]


def load_state(wf_home: str | Path) -> dict:
    p = Path(wf_home) / ".workflow" / "state.json"
    if not p.is_file():
        raise jiraapi.JiraError(f"{p} 가 없습니다 — 이 폴더는 아직 Jira 연동 대상이 아닙니다")
    return json.loads(p.read_text(encoding="utf-8-sig"))


def scope_line(state: dict) -> str:
    bits = [f"프로젝트 {state.get('project_key')}"]
    if state.get("csc_value"):
        bits.append(f"CSC {state['csc_field']} = {state['csc_value']!r}")
    if state.get("epic_keys"):
        bits.append(f"에픽 {len(state['epic_keys'])}개" + ('(JQL 에는 넣지 않음)' if state.get('csc_value') else '(바깥 경계 JQL)'))
    for k in ("title_include", "title_exclude"):
        if state.get(k):
            bits.append(f"{k} {state[k]}(JQL 에는 넣지 않음)")
    return " · ".join(bits)


def run(wf_home: str | Path, do_fetch: bool = False, out=print) -> int:
    """0 정상 · 1 실패. 사람이 읽는 보고를 out 으로 낸다."""
    try:
        state = load_state(wf_home)
        base = jiraapi.site_url(state)
    except (jiraapi.JiraError, json.JSONDecodeError) as e:
        out(f"[스코프] 실패 — {e}")
        return 1

    st = auth.status(base)
    if not st["configured"]:
        out(f"[자격] 없음 — {st['error']}")
        out(f"       파일 자리: {st['path']} (존재: {'예' if st['path_exists'] else '아니오'})")
        return 1
    out(f"[자격] 있음 — 출처 {st['source']} · 계정 {st['email']}" + (f"  ⚠ {st['warning']}" if st["warning"] else ""))

    schema = state.get("schema_version")
    if schema != 4:
        out(f"[스키마] schema_version={schema} — 4 가 아니면 마감·배치가 멈춥니다. /wf-init 필요")
        return 1
    from .workflow import support
    supported = support(state)
    for warning in supported['warnings']:out('[지원 범위] ' + warning)
    if not supported['supported']:
        out('[지원 범위] ' + supported['error'])
        return 1
    out(f"[스코프] {scope_line(state)}")
    out(f"[사이트] {base}")

    try:
        me = jiraapi.myself(auth.load(base), base)
    except jiraapi.JiraError as e:
        out(f"[인증] 실패 — {e}")
        return 1
    out(f"[인증] 성공 — {me.get('displayName')} ({me.get('emailAddress') or me.get('accountId')})")

    queries = jiraapi.build_queries(state)
    out(f"[JQL] {len(queries)}종" + (" (CSC 미지정 조회 포함 — INV-6)" if len(queries) == 3 else ""))
    for q in queries:
        out(f"   {q['kind']:8s} {q['jql']}")

    creds = auth.load(base)
    fields = jiraapi.fields_for(state)
    jf = _vendor()
    jf.HITS.clear()
    try:
        res = jiraapi.search(creds, base, queries[1]["jql"], fields)   # 'open' 으로 확인
    except jiraapi.JiraError as e:
        out(f"[조회] 실패 — {e}")
        return 1
    issues = jf.issue_list(res)
    if res.get('_truncated'):
        out('[조회] 불완전 — 페이지 한도/페이지 응답을 확인하십시오. 정상 연결 완료로 표시하지 않습니다.')
        return 1
    for d in issues:
        jf.norm_issue(d, state.get("csc_field"))
    out(f"[조회] 미완료 {len(issues)}건 · 페이지 {res['_pages']} · 응답 최상위 키 {res['_top_level_keys']}")
    out(f"[필드 경로] {json.dumps(jf.HITS, ensure_ascii=False)}")

    bad = [t for t in ESSENTIAL_TAGS if jf.HITS.get(t, "<none>") == "<none>"]
    missing = absent_fields(issues, fields) if issues else []
    empty = sorted(t for t, v in jf.HITS.items() if v == "<none>" and t not in bad)

    if not issues:
        out("   ⚠ 0건입니다. 스코프가 맞는지, 그리고 위 필드 경로를 함께 보십시오 — "
            "조회 실패와 경로 불일치는 둘 다 0건으로 보입니다(U-002).")
        return 1
    if bad:
        out(f"   ⚠ 판정에 필요한 경로를 못 찾았습니다: {bad}. 이 사이트의 응답 형태가 다릅니다(U-002).")
        return 1
    if missing:
        out(f"   ⚠ 요청했는데 응답에 키 자체가 없던 필드: {missing}. "
            "fields 목록이나 사이트 설정을 확인하십시오.")
        return 1
    if empty:
        out(f"   · 값이 비어 있던 항목: {empty} — 응답에 키는 있고 값이 null 입니다. "
            "정상입니다(아무도 그 값을 넣지 않았다는 뜻).")
    out("   ✓ 필드 경로 정상 — U-002 확인 완료.")

    if do_fetch:
        stamp = datetime.now().strftime("%y%m%d_%H%M")
        got = jiraapi.fetch(state, wf_home, stamp, creds)
        if any(q['truncated'] for q in got['queries']):
            out('[원본] 불완전한 조회입니다 — batch로 처리하거나 watermark를 전진시키지 마십시오.')
            return 1
        out(f"[원본] {stamp} — " + " · ".join(f"{q['kind']} {q['count']}건" for q in got["queries"]))
        for f in got["files"]:
            out(f"   {f}")
    else:
        out("[원본] 저장하지 않음(읽기 전용). 남기려면 --fetch 를 붙이십시오.")
    return 0
