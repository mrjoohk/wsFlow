# -*- coding: utf-8 -*-
"""S5b·S6 — 설정 작성(D-005)과 신규 태스크(요구 2). 실 이슈 키는 쓰지 않는다."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wsflow import newtask, wfhome as W, wfinit  # noqa: E402

BASE = "https://example.atlassian.net"
CREDS = {"email": "a@b", "api_token": "t"}

STATE = {"schema_version": 4, "watermark": "2026-09-01T09:00:00+09:00",
         "last_success": "2026-09-01T09:05:00+09:00",
         "project_key": "XXX", "csc_field": "customfield_19999", "csc_value": "[AAA] 합성",
         "epic_keys": ["XXX-100"], "title_include": [], "title_exclude": [], "title_prefix": None,
         "issue_type_map": {"site_url": BASE, "subtask_type": "하위 작업", "bug_marker": "label:bug",
                            "task_types": ["작업", "버그"], "sprint_field": "customfield_10020"},
         "roots": {"R-001": {"jira_key": "XXX-11", "children": [], "origin": "jira"}}}


def home(state=STATE, git=False, gitignore=None):
    td = tempfile.mkdtemp()
    p = Path(td)
    (p / ".workflow").mkdir(parents=True)
    if state is not None:
        (p / ".workflow" / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    if git:
        (p / ".git").mkdir()
    if gitignore is not None:
        (p / ".gitignore").write_text(gitignore, encoding="utf-8")
    return p


class Recorder:
    def __init__(self, by_path=None, default=None):
        self.by_path, self.default, self.calls = by_path or {}, default or {}, []

    def __call__(self, url, headers, payload, method):
        self.calls.append(url)
        for frag, resp in self.by_path.items():
            if frag in url:
                return resp
        return self.default


class BuildStateTests(unittest.TestCase):
    """재실행해도 안전해야 한다 — 초기화가 데이터를 지우면 복구 행동이 사고가 된다."""

    def test_watermark_and_roots_survive_reconfiguration(self):
        # 음성 대조: build_state 의 keep 보존을 빼면 이 시험이 실패한다.
        new = wfinit.build_state(STATE, project_key="XXX", csc_field="customfield_19999",
                                 csc_value="[BBB] 다른 CSC", epic_keys=["XXX-200"])
        self.assertEqual(new["watermark"], STATE["watermark"])
        self.assertEqual(new["last_success"], STATE["last_success"])
        self.assertEqual(new["roots"], STATE["roots"])
        self.assertEqual(new["csc_value"], "[BBB] 다른 CSC")     # 설정만 갱신
        self.assertEqual(new["epic_keys"], ["XXX-200"])

    def test_fresh_folder_gets_a_full_schema_4_state(self):
        new = wfinit.build_state(None, project_key="XXX")
        self.assertEqual(new["schema_version"], 4)
        for k in ("watermark", "roots", "issue_type_map", "csc_field", "csc_value",
                  "epic_keys", "title_include", "title_exclude", "title_prefix",
                  "sprint_mode", "transition_on_update"):
            self.assertIn(k, new)
        self.assertEqual(new["roots"], {})

    def test_preview_reports_changes_and_what_is_preserved(self):
        p = home()
        new = wfinit.build_state(STATE, project_key="XXX", csc_value="[BBB] 다름")
        pv = wfinit.preview(p, new)
        self.assertTrue(pv["exists"])
        self.assertIn("csc_value", pv["changed"])
        self.assertEqual(pv["preserved"]["roots"], 1)

    def test_write_then_detect_round_trip(self):
        p = home(state=None)
        new = wfinit.build_state(None, project_key="XXX",
                                 issue_type_map={"site_url": BASE})
        wfinit.write_state(p, new)
        d = W.detect({"path": str(p)})
        self.assertTrue(d["enabled"])
        self.assertTrue(d["schema_ok"])


class SiteReuseTests(unittest.TestCase):
    def test_site_defaults_carry_common_values_only(self):
        got = wfinit.site_defaults(STATE)
        self.assertEqual(got["csc_field"], "customfield_19999")
        self.assertEqual(got["issue_type_map"]["site_url"], BASE)
        # 폴더마다 다른 값은 공통값이 아니다 — 두 번 입력받으면 언젠가 어긋난다.
        self.assertNotIn("csc_value", got)
        self.assertNotIn("epic_keys", got)
        self.assertNotIn("project_key", got.get("issue_type_map", {}))

    def test_donor_states_finds_configured_folders(self):
        p = home()
        got = wfinit.donor_states([{"id": "a", "path": str(p)}, {"id": "b", "path": "/nope"}])
        self.assertEqual([g["id"] for g in got], ["a"])
        self.assertEqual(got[0]["site_url"], BASE)


class DiscoverTests(unittest.TestCase):
    def test_one_failing_part_does_not_kill_the_rest(self):
        # 조회는 편의일 뿐이다 — 안 된다고 설정을 못 하면 안 된다.
        class Partial(Recorder):
            def __call__(self, url, headers, payload, method):
                self.calls.append(url)
                if "project/search" in url:
                    raise RuntimeError("막힘")
                if "createmeta" in url:
                    return {"issueTypes": [{"id": "1", "name": "작업", "subtask": False, "hierarchyLevel": 0}]}
                return {"issues": [], "isLast": True}
        got = wfinit.discover(CREDS, BASE, "XXX", Partial())
        self.assertIsNone(got["projects"])
        self.assertIn("projects", got["errors"])
        self.assertEqual(len(got["issue_types"]), 1)

    def test_csc_candidates_rank_select_field_first(self):
        send = Recorder(by_path={
            "createmeta/XXX/issuetypes/1": {"fields": [
                {"fieldId": "customfield_19999", "name": "CSC", "schema": {"type": "option", "custom": "x"},
                 "allowedValues": [{"value": "[AAA] 합성"}]},
                {"fieldId": "components", "name": "컴포넌트", "schema": {"type": "array"},
                 "allowedValues": [{"name": "C1"}]},
                {"fieldId": "labels", "name": "레이블", "schema": {"type": "array"}},
                {"fieldId": "summary", "name": "요약", "schema": {"type": "string"}},
            ]}})
        got = wfinit.csc_candidates(CREDS, BASE, "XXX",
                                    [{"id": "1", "subtask": False, "hierarchyLevel": 0}], send)
        self.assertEqual([c["field"] for c in got], ["customfield_19999", "components", "labels"])
        self.assertEqual(got[0]["jql"], "cf[19999]")
        self.assertIn("구조적으로", got[0]["why"])


class GitignoreTests(unittest.TestCase):
    """D-005 — WF_HOME 이 프로젝트 안에 들어오면 제외가 없을 때 업무 기록이 커밋된다."""

    def test_missing_lines_are_named(self):
        p = home(git=True, gitignore="__pycache__/\n")
        rep = wfinit.gitignore_report(p)
        self.assertTrue(rep["applicable"])
        self.assertEqual(set(rep["missing"]), set(W.GITIGNORE_NEEDED))

    def test_apply_appends_only_and_keeps_existing(self):
        p = home(git=True, gitignore="__pycache__/\n*.pyc\n")
        got = wfinit.gitignore_apply(p)
        body = (p / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("__pycache__/", body)      # 기존 내용은 건드리지 않는다
        self.assertIn(".workflow/", body)
        self.assertEqual(wfinit.gitignore_report(p)["missing"], [])
        self.assertEqual(set(got["added"]), set(W.GITIGNORE_NEEDED))

    def test_non_repo_is_not_applicable(self):
        self.assertFalse(wfinit.gitignore_report(home())["applicable"])


class NewTaskTests(unittest.TestCase):
    def test_title_goes_through_vendor_and_prefix_is_enforced(self):
        p = home(dict(STATE, title_prefix="[SCH]", title_include=[r"^\[SCH\]"]))
        title = newtask.confirm_title(p, "지그 제작")
        self.assertTrue(title.startswith("[SCH]"))

    def test_out_of_scope_title_is_refused_before_creating(self):
        # 접두사 없는 제목으로 만들면 티켓은 남고 이 폴더 조회에서는 사라진다.
        p = home(dict(STATE, title_include=[r"^\[SCH\]"]))
        with self.assertRaises(newtask.NewTaskError):
            newtask.confirm_title(p, "아무 제목")

    def test_sequence_is_numbered_by_vendor_not_by_hand(self):
        p = home()
        title = newtask.confirm_title(p, "새 일", epic_summaries=["--003-- 먼저 한 일"])
        self.assertTrue(title.startswith("--004--"), title)

    def test_csc_is_always_attached_when_the_folder_uses_one(self):
        p = home()
        pl = newtask.plan(p, STATE, summary="새 일")
        self.assertEqual(pl["payload"]["fields"]["customfield_19999"], {"value": "[AAA] 합성"})
        self.assertEqual(pl["payload"]["fields"]["parent"], {"key": "XXX-100"})

    def test_ambiguous_epic_is_asked_not_guessed(self):
        st = dict(STATE, epic_keys=["XXX-100", "XXX-101"])
        p = home(st)
        with self.assertRaises(newtask.NewTaskError) as cm:
            newtask.plan(p, st, summary="새 일")
        self.assertIn("고르십시오", str(cm.exception))
        pl = newtask.plan(p, st, summary="새 일", epic_key="XXX-101")
        self.assertEqual(pl["payload"]["fields"]["parent"], {"key": "XXX-101"})

    def test_no_epic_keys_warns_about_unassigned(self):
        st = dict(STATE, epic_keys=[])
        pl = newtask.plan(home(st), st, summary="새 일")
        self.assertNotIn("parent", pl["payload"]["fields"])
        self.assertTrue(any("과제 미지정" in w for w in pl["warnings"]))

    def test_bug_becomes_a_subtask_with_marker(self):
        p = home()
        pl = newtask.plan(p, STATE, summary="단위 오류", kind="bug", parent_key="XXX-11")
        f = pl["payload"]["fields"]
        self.assertEqual(f["issuetype"], {"name": "하위 작업"})
        self.assertEqual(f["parent"], {"key": "XXX-11"})
        self.assertIn("bug", f["labels"])
        self.assertTrue(f["summary"].startswith("[BUG]"))

    def test_create_needs_approval_and_records_a_local_root(self):
        p = home()
        send = Recorder(default={"key": "XXX-55", "id": "55"})
        pl = newtask.plan(p, STATE, summary="새 일")
        with self.assertRaises(Exception):          # 승인 없이는 만들지 않는다
            newtask.create(p, CREDS, BASE, pl, send)
        self.assertEqual(send.calls, [])
        from wsflow import jirawrite
        jirawrite.approve(p, pl["kind"], pl["target"], pl["payload"])
        got = newtask.create(p, CREDS, BASE, pl, send)
        self.assertEqual(got["key"], "XXX-55")
        st = json.loads((p / ".workflow" / "state.json").read_text(encoding="utf-8"))
        new_root = [r for r in st["roots"].values() if r["jira_key"] == "XXX-55"][0]
        self.assertEqual(new_root["origin"], "local")   # INV-3 이 구별할 수 있어야 한다
        self.assertEqual(new_root["epic"], "XXX-100")

    def test_subtask_is_recorded_under_its_parent_root(self):
        p = home()
        send = Recorder(default={"key": "XXX-56"})
        pl = newtask.plan(p, STATE, summary="회귀 시험 추가", kind="subtask", parent_key="XXX-11")
        from wsflow import jirawrite
        jirawrite.approve(p, pl["kind"], pl["target"], pl["payload"])
        newtask.create(p, CREDS, BASE, pl, send)
        st = json.loads((p / ".workflow" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["roots"]["R-001"]["children"][0]["jira_key"], "XXX-56")


if __name__ == "__main__":
    unittest.main()
