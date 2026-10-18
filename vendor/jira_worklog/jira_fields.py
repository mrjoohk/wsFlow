#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 응답에서 Jira 필드를 관대하게 추출한다.

왜 관대해야 하는가: MCP 래퍼는 Jira REST 응답을 그대로 넘기기도 하고
평탄화하기도 한다. 필드 경로를 하나로 못박으면 첫 실행에서 조용히 빈 값이
나오고, 그것이 일지에 "변동 없음"으로 기록된다 — 가장 나쁜 실패 모드다.
그래서 여러 경로를 시도하고, **실제로 적중한 경로를 기록**해 첫 실행 후
사람이 확인할 수 있게 한다.
"""
import re

HITS = {}

# state.json 이 이 값과 다르면 스크립트는 **실행하지 않고 중단한다.**
# DESIGN 은 처음부터 그렇게 못박았지만 오래도록 문서에만 있었고, 실제로는
# 에이전트가 문서를 지키는 데 전적으로 의존했다(대장 F-014). 구스키마에
# 대고 마감까지 가면 roots 구조가 어긋난 채 티켓이 생성되는데, 그것은
# 되돌리기 가장 비싼 실패다. 그래서 여기서 기계가 막는다.
SCHEMA_VERSION = 4


def require_schema(state, where=""):
    """state.json 의 schema_version 을 확인하고, 다르면 종료 코드 2로 중단한다.

    2를 쓰는 이유: 1은 "검사 위반"(다시 돌리면 될 수도 있다)이고 2는
    "설정이 깨졌다"(사람이 고치기 전에는 뭘 해도 소용없다)이다. 대처가
    다르므로 코드로 구별한다.
    """
    import sys as _sys
    got = state.get("schema_version")
    if got == SCHEMA_VERSION:
        return
    src = f" ({where})" if where else ""
    print(f"schema_version 불일치{src}: 기대 {SCHEMA_VERSION}, 실제 {got!r}. "
          "구스키마로 계속 도는 것보다 멈추는 편이 복구 가능하다 — "
          "/wf-init 을 다시 실행해 마이그레이션하십시오.", file=_sys.stderr)
    raise SystemExit(2)


def dig(obj, *paths, default=None, tag=None):
    """dotted path 여러 개를 순서대로 시도해 첫 non-None 값을 반환."""
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            if tag:
                HITS.setdefault(tag, path)
            return cur
    if tag:
        HITS.setdefault(tag, "<none>")
    return default


def csc_names(d, csc_field):
    """이슈에서 CSC 값을 뽑아 **문자열 목록**으로 돌려준다.

    CSC 를 어디에 담느냐는 사이트마다 다르고(``csc_field``), 그에 따라 값의
    형태가 네 갈래로 갈린다. 넷을 다 흡수하지 않으면 빈 목록이 나오고, 그러면
    그 폴더는 아무것도 못 맡는다 — 조용히 전부 스코프 밖이 된다(F-038).

    ====================  =========================================
    ``csc_field``          값의 형태
    ====================  =========================================
    ``components``         ``[{"name": "..."}]``  (표준 컴포넌트)
    ``customfield_1xxxx``  ``{"value": "...", "id": "..."}``  (단일 선택)
    〃                     ``[{"value": "..."}]``  (다중 선택)
    ``labels``             ``["...", "..."]``  (문자열 배열)
    ====================  =========================================

    실측(2026-09-02, 어느 team-managed 프로젝트): 단일 선택 필드는 배열이
    아니라 단일 객체로 왔다. 배열만 가정하면 그 사이트 전체가 스코프 밖이 된다.
    """
    if not csc_field:
        return []
    raw = dig(d, f"fields.{csc_field}", csc_field, default=None, tag="csc")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for c in items:
        if isinstance(c, dict):
            n = c.get("name") or c.get("value")
        else:
            n = c
        if n:
            out.append(str(n))
    return out



# --- 이름 규약 (D-019 · D-020) ----------------------------------------
# 태스크 순번은 ``--001--``, CSC 약자는 ``[AAA]`` 다. 두 표시가 서로 다른
# 문자를 쓰는 것이 요점이다 — 대괄호는 CSC 전용이므로 제목 앞머리만 보고
# "이건 순번인가 CSC 표시인가"를 사람도 기계도 헷갈리지 않는다. 옛 규약
# 옛 2자리 대괄호 순번은 그 구별이 없어 ``[BUG]``·``title_prefix`` 와 같은 자리를
# 다투었다(D-007 을 D-019 가 supersede).
TASK_SEQ_RE = re.compile(r"^--(\d{3})--(?:\s|$)")
CSC_ABBR_RE = re.compile(r"^\[([^\]]{1,12})\]")


def task_seq(summary):
    """제목 앞머리의 ``--NNN--`` 순번을 정수로 꺼낸다. 없으면 ``None``.

    ``None`` 과 ``0`` 을 구별해야 한다 — 전자는 "번호가 없다", 후자는
    있을 수 없는 번호다. 호출자가 ``or`` 로 뭉개지 않도록 0 은 내지 않는다.
    """
    m = TASK_SEQ_RE.match((summary or "").strip())
    return int(m.group(1)) if m else None


def next_task_seq(summaries):
    """에픽 안 기존 제목들에서 다음 순번을 구한다(최댓값 + 1, 없으면 1).

    빈 자리를 메우지 않는 이유: D-007 의 불변 ID 선언이다. 지워진 번호를
    다시 쓰면 몇 달 뒤의 인용이 다른 태스크를 가리킨다.
    """
    seen = [task_seq(t) for t in (summaries or [])]
    seen = [n for n in seen if n is not None]
    return (max(seen) + 1) if seen else 1


def csc_abbr(csc_value):
    """CSC 값 ``[AAA] CSC-2 통신처리`` 에서 약자 ``ALS`` 를 꺼낸다.

    약자를 CSC 필드 옵션 값 안에 두는 것이 D-020 이다. 에픽 이름의
    접두사는 여기서 **파생**되므로 설정에 접두사 칸을 따로 두지 않는다 —
    칸이 둘이면 언젠가 서로 어긋나고, 그 어긋남이 F-029 계열의 유실이다.
    약자가 없는 CSC 값(옛 이름)이면 ``None`` 을 내고 검사는 건너뛴다.
    """
    m = CSC_ABBR_RE.match((csc_value or "").strip())
    return m.group(1).strip() if m else None


def epic_prefix(csc_value):
    """에픽 이름에 붙일 접두사 ``[AAA]``. 약자가 없으면 ``None``."""
    a = csc_abbr(csc_value)
    return f"[{a}]" if a else None

def norm_issue(d, csc_field=None):
    """Jira 이슈 dict → 이 워크플로우가 쓰는 평탄한 형태.

    ``csc_field`` 는 CSC 를 담은 필드 이름이다(state.json 의 ``csc_field``).
    넘기지 않으면 ``csc`` 는 빈 목록이 되고 CSC 판정은 하지 않는 것과 같다.
    """
    parent_key = dig(d, "fields.parent.key", "parent.key", "parentKey", tag="parent")
    return {
        "key": dig(d, "key", "issueKey", tag="key"),
        "summary": dig(d, "fields.summary", "summary", default="", tag="summary"),
        "status": dig(d, "fields.status.name", "status.name", "status",
                      default="", tag="status"),
        "status_category": dig(d, "fields.status.statusCategory.key",
                               "status.statusCategory.key", default="", tag="status_category"),
        "type": dig(d, "fields.issuetype.name", "issuetype.name", "issueType",
                    default="", tag="type"),
        "is_subtask": bool(dig(d, "fields.issuetype.subtask", "issuetype.subtask",
                               default=False, tag="is_subtask")),
        # 계층 번호: -1 파생 · 0 태스크 · 1 에픽. 이슈 유형 **이름**은 사이트
        # 언어에 따라 "Epic"·"에픽" 이 갈리므로 판정에 쓸 수 없다(F-040).
        "hierarchy_level": dig(d, "fields.issuetype.hierarchyLevel",
                               "issuetype.hierarchyLevel", default=None,
                               tag="hierarchy_level"),
        "parent_key": parent_key,
        "parent_summary": dig(d, "fields.parent.fields.summary", "parent.fields.summary",
                              "parent.summary", default="", tag="parent_summary"),
        "updated": dig(d, "fields.updated", "updated", default="", tag="updated"),
        "duedate": dig(d, "fields.duedate", "duedate", tag="duedate"),
        "priority": dig(d, "fields.priority.name", "priority.name", default="", tag="priority"),
        "labels": dig(d, "fields.labels", "labels", default=[], tag="labels"),
        "csc": csc_names(d, csc_field),
        "changelog": dig(d, "changelog.histories", "changelog", default=[], tag="changelog"),
        "comments": dig(d, "fields.comment.comments", "comments", default=[], tag="comments"),
    }


def issue_list(raw):
    """MCP 응답 최상위에서 이슈 배열을 찾아낸다.

    형태가 최소 두 갈래다. 평탄한 ``{"issues": [...]}`` 와, 커넥터가 한 겹
    감싼 ``{"issues": {"nodes": [...]}}``. 후자를 놓치면 조회는 성공했는데
    집계가 0건이 되고 일지에는 "변동 없음"으로 남는다 — 이 모듈이 막으려는
    바로 그 실패 모드이므로, 감싼 형태도 함께 본다.

    적중한 경로는 ``HITS["issue_list"]``에 남는다. 0건이 나왔을 때
    조회 문제인지 경로 문제인지 가르는 근거다.
    """
    if isinstance(raw, list):
        HITS.setdefault("issue_list", "<root list>")
        return raw
    for path in ("issues", "results", "data.issues",
                 "structuredContent.issues", "nodes"):
        v = dig(raw, path)
        if isinstance(v, list):
            HITS.setdefault("issue_list", path)
            return v
        if isinstance(v, dict) and isinstance(v.get("nodes"), list):
            HITS.setdefault("issue_list", path + ".nodes")
            return v["nodes"]
    HITS.setdefault("issue_list", "<none>")
    return []
