"""Jira Cloud REST 어댑터 — D-002(안 A: 서버가 직접 호출).

표준 라이브러리 ``urllib`` 만 쓴다(D-001). 하는 일은 셋이다 — 자격으로 인증하고,
`state.json` 의 스코프를 JQL 로 옮기고, 응답을 **손대지 않은 채** `.workflow/raw/`
에 남긴다. 필터·집계·판정은 전부 `vendor/jira_worklog/aggregate.py` 소관이다(D-003).

### 엔드포인트

구 ``/rest/api/3/search`` 는 **Jira Cloud 에서 제거됐다.** 현행은
``/rest/api/3/search/jql`` 이고 페이지 넘김이 ``startAt`` 이 아니라
``nextPageToken`` 이다. 두 가지가 특히 다르다.

* ``fields`` 를 생략하면 **필드가 거의 오지 않는다.** 그래서 이 모듈은 항상
  명시해서 보낸다 — 생략은 "조회는 됐는데 집계가 0건"이라는, 이 워크플로우가
  가장 싫어하는 조용한 실패를 만든다.
* ``total`` 에 기대지 않는다. 있으면 기록만 하고 판단에는 쓰지 않는다.

### JQL 은 스코프의 *바깥 경계*만 옮긴다

CSC가 있으면 ``epic_keys`` 를 넣지 않고 제목 필터는 항상 **넣지 않는다**(F-030). 넣으면 목록 밖 CSU 가 응답에
아예 없어 ``epic_unlisted`` 로 셀 수조차 없고, 제목 색인 지연으로 새 이슈가 어느
폴더에도 안 잡힌다. 그 판정은 ``aggregate.py`` 가 한다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path

from . import auth

SEARCH_PATH = "/rest/api/3/search/jql"   # 구 /rest/api/3/search 는 제거됨
MYSELF_PATH = "/rest/api/3/myself"
TIMEOUT = 60
PAGE = 100
MAX_PAGES = 50                            # 폭주 방지 — 개인 범위에서 5000건이면 스코프가 틀린 것이다

# norm_issue(jira_fields.py:146) 가 읽는 필드. 생략하면 신 엔드포인트가 거의 안 준다.
FIELDS_BASE = ["summary", "status", "issuetype", "parent", "updated",
               "duedate", "priority", "labels", "comment", "assignee", "project"]


class JiraError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status, self.body = status, body

    def to_dict(self) -> dict:
        return {"error": str(self), "status": self.status, "body": self.body[:2000]}


def csc_jql(csc_field: str) -> str:
    """CSC 를 담은 필드 이름 → JQL 좌변 (worklog-daily/SKILL.md:43)."""
    if csc_field == "components":
        return "component"
    if csc_field == "labels":
        return "labels"
    m = re.fullmatch(r"customfield_(\d+)", csc_field or "")
    if m:
        return f"cf[{m.group(1)}]"
    raise JiraError(f"csc_field 를 JQL 로 옮길 수 없습니다: {csc_field!r}")


def wm_for_jql(watermark: str) -> str:
    """ISO watermark → JQL 의 ``"YYYY-MM-DD HH:MM"``.

    JQL 은 초·오프셋을 받지 않는다. 잘라내기만 하고 시간대를 바꾸지 않는 이유는
    watermark 가 이미 그 사이트에서 쓰는 지역 시각이기 때문이다 — 여기서 UTC 로
    옮기면 구간이 9시간 어긋나고, 그 유실은 조용하다.
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", watermark or "")
    if not m:
        raise JiraError(f"watermark 형식을 읽을 수 없습니다: {watermark!r}")
    return f"{m.group(1)} {m.group(2)}"


def build_queries(state: dict) -> list[dict]:
    """state.json 의 바깥 경계를 JQL 3종으로 옮긴다 (worklog-daily/SKILL.md:53-55).

    ``csc_value`` 가 있으면 **CSC 미지정 조회를 반드시 함께** 낸다 — INV-6.
    CSC 는 에픽에서 상속되지 않으므로, 이 조회가 빠지면 "0건"과 "보지 않았다"가
    구별되지 않는다.
    """
    key = state.get("project_key")
    if not key:
        raise JiraError("state.json 에 project_key 가 없습니다")
    csc_value = state.get("csc_value")
    scope = ""
    cj = ""
    if csc_value:
        cj = csc_jql(state.get("csc_field") or "")
        scope = f' AND {cj} = {literal(csc_value)}'
    elif state.get('epic_keys'):
        scope = ' AND parent in (' + ','.join(issue_key(k) for k in state['epic_keys']) + ')'
    issue_key(key + '-1')

    qs = [
        {"kind": "changes",
         "jql": f'project = {key}{scope} AND assignee = currentUser() '
                f'AND updated >= "{wm_for_jql(state.get("watermark", ""))}" ORDER BY updated DESC'},
        {"kind": "open",
         "jql": f'project = {key}{scope} AND assignee = currentUser() '
                f'AND statusCategory != Done ORDER BY updated DESC'},
    ]
    if csc_value:
        qs.append({"kind": "orphans",
                   "jql": f'project = {key} AND assignee = currentUser() '
                          f'AND {cj} IS EMPTY AND statusCategory != Done ORDER BY updated DESC'})
    return qs


def fields_for(state: dict) -> list[str]:
    out = list(FIELDS_BASE)
    for extra in (state.get("csc_field"), (state.get("issue_type_map") or {}).get("sprint_field")):
        if extra and extra not in out:
            out.append(extra)
    return out


def site_url(state: dict) -> str:
    u = (state.get("issue_type_map") or {}).get("site_url")
    if not u:
        raise JiraError("state.json 의 issue_type_map.site_url 이 없습니다")
    return u.rstrip("/")


# --------------------------------------------------------------- 전송
_HINTS = {
    401: "인증 실패 — 토큰이 만료됐거나 이메일이 다릅니다. "
         "Atlassian API 토큰은 이제 만료일이 있습니다(최장 365일).",
    403: "권한 없음 — 토큰 스코프 또는 프로젝트 권한을 확인하십시오.",
    404: f"엔드포인트를 찾을 수 없습니다 — 구 /rest/api/3/search 는 제거됐습니다. 현행은 {SEARCH_PATH} 입니다.",
    410: f"엔드포인트가 제거됐습니다 — 현행은 {SEARCH_PATH} 입니다.",
}


def _send(url: str, headers: dict, payload: bytes | None, method: str) -> dict:
    """전송만 한다. 오류 해석은 ``request`` 가 맡는다 — 전송을 갈아끼워도
    (시험의 가짜 전송을 포함해) 같은 해석을 받게 하기 위해서다."""
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def request(creds: dict, base: str, path: str, body: dict | None = None,
            send=None) -> dict:
    send = send or _send
    headers = {"Authorization": auth.basic_header(creds),
               "Accept": "application/json",
               "User-Agent": "wsFlow"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        return send(base + path, headers, payload, "POST" if body is not None else "GET")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — 본문이 없을 수 있다
            raw = ""
        hint = _HINTS.get(e.code, "")
        raise JiraError(f"HTTP {e.code} {e.reason}{' — ' + hint if hint else ''}", e.code, raw) from e
    except urllib.error.URLError as e:
        raise JiraError(f"연결 실패: {e.reason}") from e


def myself(creds: dict, base: str, send=None) -> dict:
    return request(creds, base, MYSELF_PATH, None, send)


def search(creds: dict, base: str, jql: str, fields: list[str], send=None) -> dict:
    """nextPageToken 으로 끝까지 모아 ``{"issues": [...]}`` 하나로 돌려준다.

    합친 형태는 평탄한 ``{"issues": [...]}`` 이고, 이는
    ``jira_fields.issue_list`` 가 첫 번째로 시도하는 경로다(무수정 재사용, D-003).
    """
    issues, raw_issues, token, pages, seen_keys, tokens, issue_keys = [], [], None, 0, [], set(), set()
    reasons = []
    truncated = False
    while True:
        body = {"jql": jql, "fields": fields, "maxResults": PAGE, "expand": "changelog"}
        if token:
            body["nextPageToken"] = token
        data = request(creds, base, SEARCH_PATH, body, send)
        page = data.get("issues")
        if not isinstance(page, list) or any(not isinstance(i, dict) or not isinstance(i.get('key'), str) for i in page):
            raise JiraError(f"응답에 issues 배열이 없습니다 — 최상위 키: {sorted(data)[:8]}")
        if 'isLast' in data and type(data['isLast']) is not bool:
            raise JiraError('isLast는 boolean이어야 합니다')
        if data.get('isLast') is True and data.get('nextPageToken'):
            raise JiraError('마지막 페이지에 다음 페이지 토큰이 있습니다')
        raw_issues += page
        for item in page:
            if item['key'] in issue_keys:
                truncated = True
                if 'duplicate_issue_key' not in reasons:
                    reasons.append('duplicate_issue_key')
            else:
                issue_keys.add(item['key'])
                issues.append(item)
        seen_keys = sorted(set(seen_keys) | set(data))
        pages += 1
        token = data.get("nextPageToken")
        if data.get('isLast') is True:
            break
        if token and (not isinstance(token, str) or token in tokens or pages >= MAX_PAGES):
            truncated = True
            break
        if not token:
            truncated = truncated or data.get('isLast') is False
            break
        tokens.add(token)
    return {"issues": issues, "_pages": pages, "_top_level_keys": seen_keys,
            "_truncated": truncated, '_incomplete_reasons': reasons,
            **({'_raw_issues': raw_issues} if reasons else {})}


def fetch(state: dict, wf_home: str | Path, stamp: str, creds: dict, send=None) -> dict:
    """JQL 3종을 돌려 원본을 ``.workflow/raw/{stamp}_{kind}.json`` 에 그대로 남긴다.

    **응답을 가공하지 않는다.** 이 파일이 재집계·대조의 근거이고, 없으면 INV-4
    (일지가 언급한 키가 원본에 실재하는가)가 성립하지 않는다.
    """
    base = site_url(state)
    fields = fields_for(state)
    raw_dir = Path(wf_home) / ".workflow" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = {"stamp": stamp, "queries": [], "files": []}
    for q in build_queries(state):
        res = search(creds, base, q["jql"], fields, send)
        doc = {"_provenance": {"tool": f"wsFlow jiraapi {SEARCH_PATH}", "jql": q["jql"],
                               "captured": stamp, "fields": fields,
                               "pages": res["_pages"], "top_level_keys": res["_top_level_keys"],
                               "truncated": res["_truncated"],
                               'incomplete_reasons': res['_incomplete_reasons']},
               "issues": res.get('_raw_issues', res["issues"])}
        p = raw_dir / f"{stamp}_{q['kind']}.json"
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        out["queries"].append({**q, "count": len(res["issues"]), "truncated": res["_truncated"]})
        out["files"].append(str(p))
    return out


def literal(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise JiraError('유효한 JQL 문자열이 필요합니다')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def issue_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', value):
        raise JiraError('유효한 Jira 이슈 키가 필요합니다')
    return value


def require_complete(result):
    if result.get('_truncated') is not False:
        raise JiraError('완전한 Jira 조회가 아닙니다 — 페이지 한도/반복 토큰/마지막 페이지를 확인하십시오')
    return result


def fields_complete(issues, state):
    """Absent scope/type fields are unknown; an explicit null parent is legitimate."""
    required = {'summary','status','issuetype','updated','assignee'}
    if state.get('csc_value'):
        required.add(state['csc_field'])
    for item in issues:
        f = item.get('fields')
        if not isinstance(f, dict) or not required <= f.keys():
            return False
        status, kind = f['status'], f['issuetype']
        if not isinstance(f['summary'], str) or not f['summary'].strip():
            return False
        if not isinstance(status, dict) or not isinstance(status.get('name'), str) or not isinstance(status.get('statusCategory'), dict) or not status['statusCategory'].get('key'):
            return False
        if not isinstance(kind, dict) or not isinstance(kind.get('name'), str) or not kind['name']:
            return False
        if f['assignee'] is not None and (not isinstance(f['assignee'], dict) or not f['assignee'].get('accountId')):
            return False
        try:
            if datetime.fromisoformat(f['updated'].replace('Z', '+00:00')).tzinfo is None:
                return False
        except (ValueError, TypeError, AttributeError):
            return False
        # Epic roots can omit parent; task scope needs either a known parent or explicit null.
        hierarchy = kind.get('hierarchyLevel', 0)
        if type(hierarchy) is not int:
            return False
        if state.get('epic_keys') and item.get('key') not in state['epic_keys'] and hierarchy < 1:
            if 'parent' not in f or (f['parent'] is not None and (not isinstance(f['parent'], dict) or not f['parent'].get('key'))):
                return False
    return True


def creation_context(state, creds, epic_key=None, *, send=None):
    """Current account and all visible epic titles, without personal/open/CSC filters."""
    base = site_url(state)
    me = myself(creds, base, send)
    account = me.get('accountId')
    if not isinstance(account, str) or not account:
        raise JiraError('현재 Jira 사용자 accountId를 확인하지 못했습니다')
    summaries, name = None, None
    if epic_key:
        issue_key(epic_key)
        epic = request(creds, base, f'/rest/api/3/issue/{epic_key}?fields=summary,issuetype', send=send)
        if epic.get('key') != epic_key or type(epic.get('fields', {}).get('issuetype', {}).get('hierarchyLevel')) is not int or (epic.get('fields', {}).get('issuetype', {}).get('hierarchyLevel') != 1):
            raise JiraError('선택한 부모가 확인된 Epic이 아닙니다')
        name = epic.get('fields', {}).get('summary')
        res = require_complete(search(creds, base, f'project = {state["project_key"]} AND parent = {issue_key(epic_key)} ORDER BY key', ['summary'], send))
        summaries = [i.get('fields', {}).get('summary') for i in res['issues']]
        if not isinstance(name, str) or not name.strip() or any(not isinstance(t, str) or not t.strip() for t in summaries):
            raise JiraError('에픽 이름과 전체 제목을 확인하지 못했습니다')
    return {'account_id': account, 'epic_key': epic_key, 'epic_name': name,
            'epic_summaries': summaries, 'complete': True, 'captured_at': datetime.now(timezone.utc).isoformat(),
            'scope': 'visible_epic_all_statuses_and_assignees'}


def visibility(state, creds, key, account_id, send=None):
    """A successful create and membership in the personal list are distinct facts."""
    try:
        key = issue_key(key)
        raw = request(creds, site_url(state), f'/rest/api/3/issue/{key}?fields=' + ','.join(fields_for(state)), send=send)
        from . import wfhome
        got = wfhome.classify([raw], state)
        assigned = raw.get('fields', {}).get('assignee', {}).get('accountId') == account_id
        q = next(q['jql'] for q in build_queries(state) if q['kind'] == 'open')
        res = require_complete(search(creds, site_url(state), f'key = {key} AND ({q.split(" ORDER BY")[0]})', fields_for(state), send))
        visible = assigned and any(t['key'] == key for t in got['tasks']) and any(i['key'] == key for i in res['issues'])
        return {'state': 'visible' if visible else 'not_visible', 'checked_at': datetime.now(timezone.utc).isoformat(),
                'warnings': [] if visible else ['생성은 성공했으나 개인 업무 목록 가시성이 확인되지 않았습니다. 담당자·범위·권한·검색 색인을 확인하십시오.']}
    except Exception:
        return {'state': 'unknown', 'checked_at': None, 'warnings': ['생성은 성공했으나 재조회에 실패했습니다. 같은 작업을 다시 생성하지 마십시오.']}
