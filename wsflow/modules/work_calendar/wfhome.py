"""WF_HOME 읽기·쓰기 — 매핑 판정(D-004)과 진행 메모(INV-1).

## 매핑

프로젝트 폴더 안에 `.workflow/state.json` 이 있으면 그 폴더가 곧 WF_HOME 이고,
Jira 연동은 **켜진 채 잠긴다**(D-004). 설정 파일이 있는데 끄는 선택지는 두지
않는다 — 그 상태는 "설정은 해 뒀는데 안 본다"는 뜻이고, 조용히 낡는 구성이다.

범위 설정의 **사본을 갖지 않는다.** `projects.json` 에는 필요할 때 폴더 밖을
가리키는 `wf_home` 만 둔다. 사본을 들면 `/wf-init` 재실행과 조용히 어긋난다.

## 진행 메모 — 여기가 이 모듈에서 제일 위험한 곳

`jiraWF/DESIGN.md` 는 메모를 **"유일하게 재생성 불가능한 원본"** 이라 부르고,
`render_daily.py` 가 그 한 줄을 지키려고 존재한다(INV-1). UI 가 메모를 쓰기
시작하면 **UI 도 같은 책임을 진다.** 세 겹으로 막는다.

1. `MEMO:START`~`MEMO:END` 구획만 치환한다. 파일 전체를 다시 쓰지 않는다.
2. 열 때 준 해시와 저장 시점의 해시가 다르면 **거부한다**(`MemoConflict`).
   에디터로 동시 편집, 배치 실행, 다른 탭 — 셋 다 실제로 일어난다.
3. 저장 전 사본을 남긴다. 배포(D-011)는 "git 으로 되돌린다"가 성립했지만
   **WF_HOME 은 git 이 아니다.** 같은 논리를 쓸 수 없다.

## 태스크별 메모

jiraWF 의 메모는 폴더 단위 하루 한 구획이지 태스크별이 아니다. 스키마를 바꾸는
대신 **규약만 얹는다** — `- KEY: 내용`. jiraWF 입장에서는 그냥 자유 텍스트이고,
초안 작성자는 집계에서 이미 그 키를 알고 있으며, **INV-4 가 그 키의 실재를
검사한다.** 규약이 틀리면 기계가 잡는다. 읽을 때의 파서는 관대하다 — 키로
시작하지 않는 줄은 버리지 않고 "공통"으로 모은다(사람이 에디터로 직접 쓴 줄).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from wsflow import fileio

VENDOR = Path(__file__).resolve().parent / "vendor" / "jira_worklog"
from .vendor_loader import load as _load_vendor

AGG = _load_vendor('aggregate')
JF = _load_vendor('jira_fields')

MEMO_START = "<!-- MEMO:START -->"
MEMO_END = "<!-- MEMO:END -->"
KEY_LINE = re.compile(r"^\s*[-*]\s*([A-Z][A-Z0-9_]*-\d+)\s*:\s*(.*)$")
MARK_LINE = re.compile(r"^\s*[-*]\s*(\[신규\]|\[파생\]|\[BUG\]|\[출처\])\s*(.*)$")
SCHEMA_VERSION = 4


class MemoConflict(Exception):
    """열었을 때와 내용이 달라졌다. **아무것도 쓰지 않았다.**"""

    def __init__(self, current_text: str, current_sha: str):
        super().__init__("메모가 다른 곳에서 바뀌었습니다 — 저장하지 않았습니다")
        self.current_text, self.current_sha = current_text, current_sha


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 매핑
def wf_home_for(project: dict) -> Path:
    return Path(project.get("wf_home") or project["path"])


def state_path(wf_home: str | Path) -> Path:
    return Path(wf_home) / ".workflow" / "state.json"


def detect(project: dict) -> dict:
    """이 프로젝트의 Jira 연동 상태. UI 체크박스가 이걸 그대로 그린다."""
    home = wf_home_for(project)
    sp = state_path(home)
    out = {"wf_home": str(home), "state_path": str(sp), "enabled": False,
           "locked": False, "schema_ok": None, "scope": None, "site_url": None,
           "gitignore_ok": None, "reason": ""}
    if not sp.is_file():
        out["reason"] = "설정 파일이 없습니다 — 설정 작성으로 만들 수 있습니다"
        return out
    # 설정 파일이 있으면 무조건 사용한다(D-004). 끄는 선택지를 두지 않는다.
    out.update(enabled=True, locked=True)
    try:
        state = json.loads(sp.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        out["reason"] = f"설정 파일을 읽지 못했습니다: {e}"
        out["schema_ok"] = False
        return out
    out["schema_ok"] = state.get("schema_version") == SCHEMA_VERSION
    out['schema_version'] = state.get('schema_version')
    out['migration_available'] = type(state.get('schema_version')) is int and state['schema_version'] == 3
    if not out["schema_ok"]:
        out["reason"] = (f"schema_version={state.get('schema_version')} — {SCHEMA_VERSION} 가 아니면 마감·배치가 멈춥니다. "
                         + ('보존형 설정 전환으로 원본 백업과 버전 변경을 미리 확인하십시오.' if out['migration_available'] else '지원 버전의 설정을 확인하십시오.'))
    out["site_url"] = (state.get("issue_type_map") or {}).get("site_url")
    out["scope"] = {"project_key": state.get("project_key"),
                    "csc_field": state.get("csc_field"), "csc_value": state.get("csc_value"),
                    "epic_keys": state.get("epic_keys") or [],
                    "title_include": state.get("title_include") or [],
                    "title_exclude": state.get("title_exclude") or []}
    from . import wfinit
    out["gitignore_ok"] = gitignore_ok(wfinit.gitignore_target(home))
    return out


GITIGNORE_NEEDED = (".workflow/", "daily/", "weekly/", "state.json", "jira_project_key_id.txt")


def gitignore_ok(project_path: str | Path) -> bool | None:
    """업무 데이터가 제외되어 있는가. git 저장소가 아니면 None(해당 없음).

    WF_HOME 이 프로젝트 폴더 안에 들어온다는 것은 `.workflow/`·`daily/` 가 그
    저장소에 섞인다는 뜻이다. 제외가 없으면 **업무 기록이 커밋된다**(D-005).
    """
    p = Path(project_path)
    if not (p / ".git").exists():
        return None
    gi = p / ".gitignore"
    if not gi.is_file():
        return False
    body = gi.read_text(encoding="utf-8", errors="replace")
    lines = {ln.strip() for ln in body.splitlines()}
    return all(n in lines for n in GITIGNORE_NEEDED)


def read_state(wf_home: str | Path) -> dict:
    return json.loads(state_path(wf_home).read_text(encoding="utf-8-sig"))


# ------------------------------------------------------------------ 태스크 목록
def classify(raw_issues: list, state: dict) -> dict:
    """조회 원본 → 이 폴더가 맡은 것과 **조용히 빠질 뻔한 것들**.

    `aggregate.py` 의 판정 함수를 그대로 쓴다(D-003). 걸러낸 것을 버리지 않고
    따로 세는 것이 핵심이다 — `epic_unlisted`·`title_filtered` 는 위반이 아니라
    **보고 대상**이고, 그 보고가 유일한 방어다(F-030).
    """
    csc_value = state.get("csc_value")
    epic_keys = state.get("epic_keys") or []
    outer, inner = AGG.scope_layers(csc_value, epic_keys)
    inc = AGG.compile_patterns(state.get("title_include") or [], "title_include")
    exc = AGG.compile_patterns(state.get("title_exclude") or [], "title_exclude")
    pk = state.get("project_key")

    out = {"tasks": [], "epics": [], "out_of_scope": [], "epic_unlisted": [], "title_filtered": []}
    for d in raw_issues:
        iss = JF.norm_issue(d, state.get("csc_field"))
        if AGG.is_epic(iss, inner or outer):
            out["epics"].append(iss)
            continue
        if not AGG.in_scope(iss, pk, csc_value, outer):
            out["out_of_scope"].append(iss)
            continue
        if not AGG.epic_match(iss, inner):
            out["epic_unlisted"].append(iss)
            continue
        if not AGG.title_match(iss, inc, exc):
            out["title_filtered"].append(iss)
            continue
        out["tasks"].append(iss)
    return out


def merge_roots(tasks: list, state: dict) -> list:
    """조회 결과에 `state.roots` 의 지역 번호·파생을 얹는다."""
    by_key = {}
    for rid, r in (state.get("roots") or {}).items():
        if r.get("jira_key"):
            by_key[r["jira_key"]] = {"root_id": rid, **r}
    out = []
    for t in tasks:
        r = by_key.get(t["key"]) or {}
        children = r.get("children") or []
        out.append({**t, "root_id": r.get("root_id"),
                    "first_seen": r.get("first_seen"), "last_seen": r.get("last_seen"),
                    "children": children,
                    "children_unpushed": [c for c in children if not c.get("jira_key")]})
    return out


# ------------------------------------------------------------------ 메모
def daily_path(wf_home: str | Path, ymd: str) -> Path:
    if not isinstance(ymd, str) or not re.fullmatch(r"[0-9]{6}", ymd):
        raise ValueError("업무 날짜는 실제 YYMMDD여야 합니다")
    datetime(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    return fileio.within(wf_home, Path(wf_home).resolve() / "daily" / f"{ymd}.md")


def memo_document_id(wf_home: str | Path, ymd: str) -> str:
    daily_path(wf_home, ymd)
    return sha(str(Path(wf_home).resolve()) + "\0" + ymd)


def _split(body: str) -> tuple[str, str, str]:
    i = body.find(MEMO_START)
    j = body.find(MEMO_END)
    if i < 0 or j < 0 or j < i:
        raise ValueError("메모 구획 표시를 찾지 못했습니다")
    return body[:i + len(MEMO_START)], body[i + len(MEMO_START):j], body[j:]


SKELETON = (f"# {{ymd}}\n\n## 진행 메모\n\n{MEMO_START}\n"
            "> 오늘 한 일을 태스크별로 적으십시오. `- KEY-123: 내용` 형태면 그 태스크의\n"
            "> 코멘트가 됩니다. 키 없이 적은 줄은 공통 메모로 남습니다.\n"
            f"{MEMO_END}\n")


def ensure_daily(wf_home: str | Path, ymd: str) -> Path:
    """없으면 **메모 구획만 있는** 파일을 만든다.

    상단 섹션(변동·오늘 할 일)은 비워 둔다 — 그것은 일일 배치가 채우는 자리이고,
    여기서 지어내면 배치가 쓴 것과 구별되지 않는다. 메모는 절대 덮지 않으므로
    나중에 배치가 돌아도 여기 쓴 내용은 그대로 남는다(INV-1).
    """
    p = daily_path(wf_home, ymd)
    with fileio.locked(wf_home):
        if not p.is_file():
            fileio.atomic_write(p, SKELETON.format(ymd=ymd).encode("utf-8"))
    return p


def read_memo(wf_home: str | Path, ymd: str) -> dict:
    daily_path(wf_home, ymd)
    with fileio.locked(wf_home):
        p = ensure_daily(wf_home, ymd)
        _, memo, _ = _split(p.read_bytes().decode("utf-8"))
        return {"path": str(p), "ymd": ymd, "document_id": memo_document_id(wf_home, ymd),
                "text": memo, "sha": sha(memo), "parsed": parse_memo(memo)}


def write_memo(wf_home: str | Path, ymd: str, text: str, expected_sha: str,
               document_id: str | None = None) -> dict:
    """메모 구획만 바꾼다. 해시가 다르면 아무것도 쓰지 않고 현재 내용을 돌려준다."""
    p = daily_path(wf_home, ymd)
    with fileio.locked(wf_home):
        if not p.is_file():
            raise ValueError(f"{p} 가 없습니다")
        original = p.read_bytes()
        head, memo, tail = _split(original.decode("utf-8"))
        cur = sha(memo)
        identity = memo_document_id(wf_home, ymd)
        if expected_sha != cur:
            raise MemoConflict(memo, cur)
        if document_id is not None and document_id != identity:
            raise MemoConflict(memo, cur)
        bak = fileio.within(wf_home, Path(wf_home).resolve() / ".workflow" / "memo_bak" / f"{ymd}_{uuid4().hex}.md")
        bak.parent.mkdir(parents=True, exist_ok=True)
        with bak.open('xb') as fh:
            import os
            fh.write(memo.encode('utf-8')); fh.flush(); os.fsync(fh.fileno())
        new = text if text.startswith("\n") else "\n" + text
        if not new.endswith("\n"):
            new += "\n"
        def check_external_edit():
            if p.read_bytes() != original:
                _, current, _ = _split(p.read_bytes().decode('utf-8'))
                raise MemoConflict(current, sha(current))
        fileio.atomic_write(p, (head + new + tail).encode('utf-8'), check_external_edit)
        return {"path": str(p), "sha": sha(new), "backup": str(bak),
                "ymd": ymd, "document_id": identity, 'text': new, 'parsed': parse_memo(new)}


def parse_memo(text: str) -> dict:
    """`- KEY: 내용` 은 태스크별로, 나머지는 버리지 않고 모은다."""
    by_key: dict[str, list[str]] = {}
    marks: list[dict] = []
    common: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith(">"):
            continue
        m = KEY_LINE.match(line)
        if m:
            by_key.setdefault(m.group(1), []).append(m.group(2).strip())
            continue
        mk = MARK_LINE.match(line)
        if mk:
            marks.append({"mark": mk.group(1), "text": mk.group(2).strip()})
            continue
        common.append(line.rstrip())
    return {"by_key": by_key, "marks": marks, "common": common}


def serialize_memo(by_key: dict, marks: list | None = None, common: list | None = None) -> str:
    """편집기의 칸들을 한 구획으로 되돌린다. 사람이 직접 쓴 줄(common)을 잃지 않는다."""
    lines = []
    for key in sorted(by_key):
        for t in by_key[key]:
            t = (t or "").strip()
            if t:
                lines.append(f"- {key}: {t}")
    for m in marks or []:
        t = (m.get("text") or "").strip()
        if t:
            lines.append(f"- {m['mark']} {t}")
    lines += [ln for ln in (common or []) if ln.strip()]
    return "\n".join(lines) + ("\n" if lines else "")


def live_tasks(state: dict, creds: dict, send=None) -> dict:
    """지금 조회해서 분류한다 — 목록 화면이 쓰는 한 덩어리.

    `open`(미완료)과 `orphans`(CSC 미지정)만 낸다. `changes` 는 watermark 를
    전진시키는 일일 배치의 몫이고, 화면을 열 때마다 돌리면 watermark 의 의미가
    흐려진다.
    """
    from . import jiraapi  # 지연 임포트 — 이 모듈의 순수 부분은 REST 없이 시험한다
    base = jiraapi.site_url(state)
    fields = jiraapi.fields_for(state)
    qs = {q["kind"]: q for q in jiraapi.build_queries(state)}
    kw = {"send": send} if send else {}

    res = jiraapi.search(creds, base, qs["open"]["jql"], fields, **kw)
    queries = [{'kind': 'open', 'pages': res['_pages'], 'truncated': res['_truncated'],
                'fields_complete': jiraapi.fields_complete(res['issues'],state)}]
    got = classify(res["issues"], state)
    got["tasks"] = merge_roots(got["tasks"], state)
    got["known_keys"] = sorted({i["key"] for i in got["tasks"] if i.get("key")})
    got["orphans"] = []
    if "orphans" in qs:
        o = jiraapi.search(creds, base, qs["orphans"]["jql"], fields, **kw)
        queries.append({'kind': 'orphans', 'pages': o['_pages'], 'truncated': o['_truncated'],
                        'fields_complete': jiraapi.fields_complete(o['issues'],state)})
        got["orphans"] = [JF.norm_issue(d, state.get("csc_field")) for d in o["issues"]]
    got["counts"] = {k: len(got[k]) for k in
                     ("tasks", "epics", "out_of_scope", "epic_unlisted", "title_filtered", "orphans")}
    got.update(complete=not any(q['truncated'] or not q['fields_complete'] for q in queries), queries=queries,
               captured_at=datetime.now(timezone.utc).isoformat())
    got['warnings'] = [] if got['complete'] else ['Jira 조회가 불완전합니다 — 아래 수치는 수집된 일부이며 전체 0건/완료로 판단할 수 없습니다.']
    from .workflow import support
    got['support'] = support(state)
    got['warnings'] += got['support']['warnings']
    if not got['support']['supported']:
        got['complete'] = False
        got['warnings'].append(got['support']['error'])
    return got
