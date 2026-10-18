# -*- coding: utf-8 -*-
"""S5 — 매핑(D-004)·진행 메모(INV-1)·마감 계획. 실 이슈 키는 쓰지 않는다."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wsflow import closeflow, jirawrite, wfhome as W  # noqa: E402

STATE = {"schema_version": 4, "watermark": "2026-09-01T09:00:00+09:00", "project_key": "XXX",
         "csc_field": "customfield_19999", "csc_value": "[AAA] 합성",
         "epic_keys": ["XXX-100"], "title_include": [], "title_exclude": [],
         "issue_type_map": {"site_url": "https://example.atlassian.net"},
         "roots": {"R-001": {"jira_key": "XXX-11", "epic": "XXX-100", "children": [
             {"local_id": "R-001.1", "jira_key": None, "kind": "task", "summary": "안 올라간 파생"}]}}}


def issue(key, summary="s", parent="XXX-100", csc="[AAA] 합성", subtask=False, level=0):
    f = {"summary": summary, "issuetype": {"subtask": subtask, "hierarchyLevel": level},
         "status": {"name": "진행 중", "statusCategory": {"key": "indeterminate"}},
         "updated": "2026-09-10T10:00:00+09:00", "customfield_19999": {"value": csc} if csc else None}
    if parent:
        f["parent"] = {"key": parent, "fields": {"summary": "에픽"}}
    return {"key": key, "fields": f}


class Home:
    def __init__(self, state=STATE, gitignore=None, git=False):
        self._td = tempfile.TemporaryDirectory()
        self.p = Path(self._td.name)
        (self.p / ".workflow").mkdir(parents=True)
        (self.p / ".workflow" / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        if git:
            (self.p / ".git").mkdir()
        if gitignore is not None:
            (self.p / ".gitignore").write_text(gitignore, encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._td.cleanup()

    @property
    def project(self):
        return {"path": str(self.p)}


class DetectTests(unittest.TestCase):
    def test_state_file_locks_it_on(self):
        # D-004 — 설정 파일이 있으면 무조건 사용, 끄는 선택지를 두지 않는다.
        with Home() as h:
            d = W.detect(h.project)
            self.assertTrue(d["enabled"])
            self.assertTrue(d["locked"])
            self.assertTrue(d["schema_ok"])
            self.assertEqual(d["scope"]["project_key"], "XXX")

    def test_no_state_file_is_off_and_offers_setup(self):
        with tempfile.TemporaryDirectory() as d:
            got = W.detect({"path": d})
            self.assertFalse(got["enabled"])
            self.assertFalse(got["locked"])
            self.assertIn("설정 작성", got["reason"])

    def test_wrong_schema_is_enabled_but_reported(self):
        with Home(dict(STATE, schema_version=3)) as h:
            d = W.detect(h.project)
            self.assertTrue(d["enabled"])
            self.assertFalse(d["schema_ok"])
            self.assertIn("schema_version", d["reason"])

    def test_no_copy_of_scope_is_kept_in_projects_json(self):
        # 정본은 state.json 하나. detect 는 읽어서 보여줄 뿐이다.
        with Home() as h:
            self.assertNotIn("wf_home", h.project)

    def test_gitignore_gate(self):
        # D-005 — WF_HOME 이 프로젝트 안에 들어오면 제외가 없을 때 업무 데이터가 커밋된다.
        with Home(git=True, gitignore="\n".join(W.GITIGNORE_NEEDED)) as h:
            self.assertTrue(W.detect(h.project)["gitignore_ok"])
        with Home(git=True, gitignore="__pycache__/\n") as h:
            self.assertFalse(W.detect(h.project)["gitignore_ok"])
        with Home() as h:      # git 저장소가 아니면 해당 없음
            self.assertIsNone(W.detect(h.project)["gitignore_ok"])


class ClassifyTests(unittest.TestCase):
    def test_filtered_items_are_counted_not_dropped(self):
        # F-030 — 걸러낸 것을 버리면 조용한 유실이 된다. 세는 것이 유일한 방어다.
        got = W.classify([
            issue("XXX-11"),                                  # 범위 안
            issue("XXX-12", parent="XXX-999"),                # 목록 밖 CSU
            issue("XXX-13", csc=None),                        # CSC 미지정 → 범위 밖
            issue("XXX-100", parent=None, level=1),           # 에픽 자체
            issue("XXX-14", parent="XXX-11", subtask=True),   # 파생 — 면제
        ], STATE)
        self.assertEqual([i["key"] for i in got["tasks"]], ["XXX-11", "XXX-14"])
        self.assertEqual([i["key"] for i in got["epic_unlisted"]], ["XXX-12"])
        self.assertEqual([i["key"] for i in got["out_of_scope"]], ["XXX-13"])
        self.assertEqual([i["key"] for i in got["epics"]], ["XXX-100"])

    def test_title_filter_is_reported_separately(self):
        s = dict(STATE, title_include=[r"^\[SCH\]"])
        got = W.classify([issue("XXX-11", "[SCH] 맡음"), issue("XXX-12", "안 맡음")], s)
        self.assertEqual([i["key"] for i in got["tasks"]], ["XXX-11"])
        self.assertEqual([i["key"] for i in got["title_filtered"]], ["XXX-12"])

    def test_merge_roots_surfaces_unpushed_children(self):
        rows = W.merge_roots([W_norm := {"key": "XXX-11", "summary": "s"}], STATE)
        self.assertEqual(rows[0]["root_id"], "R-001")
        self.assertEqual(len(rows[0]["children_unpushed"]), 1)


class MemoTests(unittest.TestCase):
    """INV-1 — 메모는 유일하게 재생성 불가능한 원본이다. UI 도 그 책임을 진다."""

    def test_read_creates_skeleton_and_write_touches_only_the_memo_block(self):
        with Home() as h:
            m = W.read_memo(h.p, "260911")
            W.write_memo(h.p, "260911", "- XXX-11: 했음", m["sha"])
            body = (h.p / "daily" / "260911.md").read_text(encoding="utf-8")
            self.assertIn("# 260911", body)                 # 구획 밖은 그대로
            self.assertIn(W.MEMO_START, body)
            self.assertIn(W.MEMO_END, body)
            self.assertIn("- XXX-11: 했음", body)

    def test_stale_hash_is_refused_and_nothing_is_written(self):
        # 음성 대조: write_memo 의 해시 비교를 지우면 이 시험이 실패한다.
        with Home() as h:
            m = W.read_memo(h.p, "260911")
            W.write_memo(h.p, "260911", "에디터에서 쓴 줄", m["sha"])
            with self.assertRaises(W.MemoConflict) as cm:
                W.write_memo(h.p, "260911", "UI 에서 덮어쓰려는 줄", m["sha"])
            self.assertIn("에디터에서 쓴 줄", cm.exception.current_text)
            self.assertIn("에디터에서 쓴 줄", W.read_memo(h.p, "260911")["text"])

    def test_backup_is_written_before_overwrite(self):
        # WF_HOME 은 git 이 아니다 — "되돌리면 된다"가 성립하지 않는다.
        with Home() as h:
            m = W.read_memo(h.p, "260911")
            r1 = W.write_memo(h.p, "260911", "첫 내용", m["sha"])
            m2 = W.read_memo(h.p, "260911")
            r2 = W.write_memo(h.p, "260911", "둘째 내용", m2["sha"])
            self.assertIn("첫 내용", Path(r2["backup"]).read_text(encoding="utf-8"))
            self.assertTrue(Path(r1["backup"]).is_file())

    def test_lines_a_human_typed_are_not_lost_in_round_trip(self):
        text = "- XXX-11: 했음\n- [신규] 새 일\n- [출처] rd/x.md\n자유롭게 쓴 줄"
        p = W.parse_memo(text)
        back = W.serialize_memo(p["by_key"], p["marks"], p["common"])
        for needle in ("XXX-11: 했음", "[신규] 새 일", "[출처] rd/x.md", "자유롭게 쓴 줄"):
            self.assertIn(needle, back)


class CloseTests(unittest.TestCase):
    def test_plan_writes_nothing_and_separates_unknown_keys(self):
        with Home() as h:
            m = W.read_memo(h.p, "260911")
            W.write_memo(h.p, "260911", "- XXX-11: 했음\n- XXX-77: 없는 키\n- [신규] 새 일", m["sha"])
            pl = closeflow.plan(h.p, "260911", known_keys=["XXX-11"])
            self.assertEqual([i["target"] for i in pl["items"]], ["XXX-11"])
            self.assertEqual([i["target"] for i in pl["unknown_keys"]], ["XXX-77"])
            self.assertEqual(pl["deferred"], [{"mark": "[신규]", "text": "새 일"}])
            self.assertFalse(jirawrite.approvals_path(h.p).exists())   # 아무것도 쓰지 않았다

    def test_source_lines_never_go_outbound(self):
        # [출처] 는 로컬 기록으로는 값지지만 Jira 로 나가면 아무에게도 전달되지 않는다.
        with Home() as h:
            m = W.read_memo(h.p, "260911")
            W.write_memo(h.p, "260911", "- [출처] K:\\비밀\\경로.md", m["sha"])
            pl = closeflow.plan(h.p, "260911", known_keys=[])
            self.assertEqual(pl["deferred"], [])
            self.assertEqual(pl["items"], [])

    def test_local_paths_in_a_comment_are_flagged(self):
        with Home() as h:
            m = W.read_memo(h.p, "260911")
            W.write_memo(h.p, "260911", "- XXX-11: K:\\01.workspace\\어딘가.md 를 고쳤다", m["sha"])
            pl = closeflow.plan(h.p, "260911", known_keys=["XXX-11"])
            kinds = [l["kind"] for l in pl["items"][0]["lint"]]
            self.assertIn("local_ref", kinds)

    def test_apply_goes_through_the_gate(self):
        calls = []

        def send(url, headers, payload, method):
            calls.append(url)
            return {"id": "1"}

        with Home() as h:
            m = W.read_memo(h.p, "260911")
            W.write_memo(h.p, "260911", "- XXX-11: 했음", m["sha"])
            pl = closeflow.plan(h.p, "260911", known_keys=["XXX-11"])
            # 승인 없이 올리면 아무 일도 일어나지 않는다
            r = closeflow.apply(h.p, {"email": "a@b", "api_token": "t"},
                                "https://example.atlassian.net", pl["items"], send)
            self.assertEqual(calls, [])
            self.assertEqual(len(r["failed"]), 1)
            # 승인 후에는 올라간다
            closeflow.approve(h.p, pl["items"])
            r = closeflow.apply(h.p, {"email": "a@b", "api_token": "t"},
                                "https://example.atlassian.net", pl["items"], send)
            self.assertEqual(len(r["done"]), 1)
            self.assertTrue(calls[0].endswith("/rest/api/3/issue/XXX-11/comment"))


if __name__ == "__main__":
    unittest.main()
