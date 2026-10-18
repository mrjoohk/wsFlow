# -*- coding: utf-8 -*-
"""S3 — REST 어댑터. 실조회 없이 JQL 구성·페이지 넘김·오류 대응을 고정한다.

실 이슈 키는 쓰지 않는다(PROJECT_RULES §7). 전부 합성값이다.
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "jira_worklog"))

from wsflow import auth, jiraapi  # noqa: E402
import jira_fields  # noqa: E402  (vendor, 무수정)

STATE = {
    "schema_version": 4,
    "watermark": "2026-08-28T20:58:00+09:00",
    "project_key": "XXX",
    "csc_field": "customfield_19999",
    "csc_value": "[AAA] 합성 CSC",
    "epic_keys": ["XXX-128", "XXX-129"],
    "title_include": ["^\\[SCH\\]"],
    "issue_type_map": {"site_url": "https://example.atlassian.net/",
                       "sprint_field": "customfield_10020"},
}


def fake_send(pages):
    """pages: 응답 dict 목록. 호출 기록을 calls 에 남긴다."""
    calls = []

    def send(url, headers, payload, method):
        calls.append({"url": url, "headers": headers, "method": method,
                      "body": json.loads(payload.decode("utf-8")) if payload else None})
        return pages[min(len(calls) - 1, len(pages) - 1)]

    send.calls = calls
    return send


class JqlTests(unittest.TestCase):
    def test_three_queries_match_the_skill_spec(self):
        qs = {q["kind"]: q["jql"] for q in jiraapi.build_queries(STATE)}
        self.assertEqual(qs["changes"],
            'project = XXX AND cf[19999] = "[AAA] 합성 CSC" AND assignee = currentUser() '
            'AND updated >= "2026-08-28 20:58" ORDER BY updated DESC')
        self.assertEqual(qs["open"],
            'project = XXX AND cf[19999] = "[AAA] 합성 CSC" AND assignee = currentUser() '
            'AND statusCategory != Done ORDER BY updated DESC')
        self.assertEqual(qs["orphans"],
            'project = XXX AND assignee = currentUser() AND cf[19999] IS EMPTY '
            'AND statusCategory != Done ORDER BY updated DESC')

    def test_inv6_orphans_query_is_mandatory_when_csc_is_used(self):
        # 음성 대조: build_queries 에서 orphans 분기를 지우면 이 시험이 실패한다.
        kinds = [q["kind"] for q in jiraapi.build_queries(STATE)]
        self.assertIn("orphans", kinds)

    def test_no_csc_means_no_orphans_query(self):
        s = dict(STATE, csc_value=None, csc_field=None)
        kinds = [q["kind"] for q in jiraapi.build_queries(s)]
        self.assertEqual(kinds, ["changes", "open"])

    def test_epic_keys_and_title_filter_never_reach_jql(self):
        # F-030 — 넣으면 목록 밖 CSU 를 셀 수조차 없게 된다.
        for q in jiraapi.build_queries(STATE):
            self.assertNotIn("XXX-128", q["jql"])
            self.assertNotIn("SCH", q["jql"])
            self.assertNotIn("summary", q["jql"])

    def test_csc_jql_forms(self):
        self.assertEqual(jiraapi.csc_jql("components"), "component")
        self.assertEqual(jiraapi.csc_jql("labels"), "labels")
        self.assertEqual(jiraapi.csc_jql("customfield_10282"), "cf[10282]")
        with self.assertRaises(jiraapi.JiraError):
            jiraapi.csc_jql("무엇인가")

    def test_watermark_keeps_local_time(self):
        # UTC 로 옮기면 구간이 9시간 어긋나고 그 유실은 조용하다.
        self.assertEqual(jiraapi.wm_for_jql("2026-09-03T14:22:00+09:00"), "2026-09-03 14:22")
        with self.assertRaises(jiraapi.JiraError):
            jiraapi.wm_for_jql("어제")


class SearchTests(unittest.TestCase):
    def test_fields_are_always_explicit(self):
        # 신 엔드포인트는 fields 를 생략하면 거의 주지 않는다 — "조회는 됐는데 0건".
        send = fake_send([{"issues": [], "isLast": True}])
        jiraapi.search({"email": "a@b", "api_token": "t"}, "https://example.atlassian.net",
                       "project = XXX", jiraapi.fields_for(STATE), send)
        body = send.calls[0]["body"]
        self.assertIn("fields", body)
        self.assertIn("summary", body["fields"])
        self.assertIn("customfield_19999", body["fields"])   # csc_field
        self.assertIn("customfield_10020", body["fields"])   # sprint_field
        self.assertEqual(body["expand"], "changelog")
        self.assertTrue(send.calls[0]["url"].endswith(jiraapi.SEARCH_PATH))

    def test_pagination_follows_next_page_token(self):
        send = fake_send([
            {"issues": [{"key": "XXX-1"}], "nextPageToken": "p2"},
            {"issues": [{"key": "XXX-2"}], "isLast": True},
        ])
        res = jiraapi.search({"email": "a@b", "api_token": "t"}, "https://example.atlassian.net",
                             "project = XXX", ["summary"], send)
        self.assertEqual([i["key"] for i in res["issues"]], ["XXX-1", "XXX-2"])
        self.assertEqual(res["_pages"], 2)
        self.assertEqual(send.calls[1]["body"]["nextPageToken"], "p2")
        self.assertIsNone(send.calls[0]["body"].get("nextPageToken"))

    def test_merged_shape_is_what_vendor_reads(self):
        # D-003 의 근거 — 합친 결과를 vendor 가 무수정으로 해석해야 한다.
        send = fake_send([{"issues": [{"key": "XXX-1"}], "isLast": True}])
        res = jiraapi.search({"email": "a@b", "api_token": "t"}, "https://example.atlassian.net",
                             "project = XXX", ["summary"], send)
        jira_fields.HITS.clear()
        self.assertEqual(len(jira_fields.issue_list(res)), 1)
        self.assertEqual(jira_fields.HITS["issue_list"], "issues")

    def test_missing_issues_array_is_an_error_not_zero(self):
        send = fake_send([{"values": []}])
        with self.assertRaises(jiraapi.JiraError) as cm:
            jiraapi.search({"email": "a@b", "api_token": "t"}, "https://example.atlassian.net",
                           "project = XXX", ["summary"], send)
        self.assertIn("issues", str(cm.exception))

    def test_http_errors_carry_a_usable_hint(self):
        def boom(code):
            def send(url, headers, payload, method):
                raise urllib.error.HTTPError(url, code, "x", {}, None)
            return send
        for code, needle in ((401, "만료"), (404, jiraapi.SEARCH_PATH), (410, jiraapi.SEARCH_PATH)):
            with self.assertRaises(jiraapi.JiraError) as cm:
                jiraapi.request({"email": "a@b", "api_token": "t"},
                                "https://example.atlassian.net", "/x", None, boom(code))
            self.assertIn(needle, str(cm.exception))
            self.assertEqual(cm.exception.status, code)


class FetchTests(unittest.TestCase):
    def test_raw_is_written_untouched_with_provenance(self):
        send = fake_send([{"issues": [{"key": "XXX-1", "fields": {"summary": "s"}}], "isLast": True}])
        with tempfile.TemporaryDirectory() as d:
            out = jiraapi.fetch(STATE, d, "260911_1500", {"email": "a@b", "api_token": "t"}, send)
            self.assertEqual(len(out["files"]), 3)          # changes · open · orphans
            doc = json.loads(Path(out["files"][0]).read_text(encoding="utf-8"))
            self.assertEqual(doc["issues"][0]["key"], "XXX-1")
            self.assertIn("jql", doc["_provenance"])
            self.assertIn("fields", doc["_provenance"])


class SelfCheckTests(unittest.TestCase):
    def test_scope_line_marks_what_never_enters_jql(self):
        from wsflow import selfcheck
        line = selfcheck.scope_line(STATE)
        self.assertIn("XXX", line)
        self.assertIn("JQL 에는 넣지 않음", line)   # epic_keys · title_include

    def test_null_value_is_not_a_missing_field(self):
        """F-004 — dig 는 값이 null 이어도 <none> 으로 적는다.

        '아무도 마감일을 안 넣었다'(정상)와 '그 필드가 응답에 없다'(문제)를 뭉뚱그려
        경고하면 정상 상태에서 매번 경고가 뜨고, 진짜일 때 아무도 보지 않는다.
        음성 대조: absent_fields 를 HITS 기반 판정으로 되돌리면 첫 단언이 실패한다.
        """
        from wsflow import selfcheck
        present_but_null = [{"key": "XXX-1", "fields": {"summary": "s", "duedate": None}}]
        self.assertEqual(selfcheck.absent_fields(present_but_null, ["summary", "duedate"]), [])
        key_absent = [{"key": "XXX-1", "fields": {"summary": "s"}}]
        self.assertEqual(selfcheck.absent_fields(key_absent, ["summary", "duedate"]), ["duedate"])

    def test_missing_state_is_reported_not_crashed(self):
        from wsflow import selfcheck
        with tempfile.TemporaryDirectory() as d:
            msgs = []
            self.assertEqual(selfcheck.run(d, out=msgs.append), 1)
            self.assertTrue(any("state.json" in m for m in msgs))


class AuthTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in (auth.ENV_EMAIL, auth.ENV_TOKEN, "APPDATA", "XDG_CONFIG_HOME")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _dir(self, d, payload):
        os.environ.pop(auth.ENV_EMAIL, None)
        os.environ.pop(auth.ENV_TOKEN, None)
        os.environ.pop("APPDATA", None)
        os.environ["XDG_CONFIG_HOME"] = d
        p = Path(d) / "wsFlow" / "credentials.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def test_env_wins_over_file(self):
        with tempfile.TemporaryDirectory() as d:
            self._dir(d, {"email": "file@x", "api_token": "ft"})
            os.environ[auth.ENV_EMAIL] = "env@x"
            os.environ[auth.ENV_TOKEN] = "et"
            c = auth.load()
            self.assertEqual((c["email"], c["api_token"], c["source"]), ("env@x", "et", "env"))

    def test_flat_and_sites_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            self._dir(d, {"email": "a@x", "api_token": "t1"})
            self.assertEqual(auth.load()["api_token"], "t1")
            self._dir(d, {"sites": {"https://example.atlassian.net/":
                                    {"email": "b@x", "apiToken": "t2"}}})
            self.assertEqual(auth.load("https://example.atlassian.net")["api_token"], "t2")

    def test_env_style_keys_in_file_are_accepted(self):
        """F-003 — 환경변수 이름을 그대로 파일에 적는 것이 가장 자연스러운 읽기다.

        음성 대조: _EMAIL_KEYS/_TOKEN_KEYS 에서 jira_* 별칭을 빼면 이 시험이 실패한다.
        """
        with tempfile.TemporaryDirectory() as d:
            self._dir(d, {"JIRA_EMAIL": "a@x", "JIRA_API_TOKEN": "t9"})
            c = auth.load()
            self.assertEqual((c["email"], c["api_token"]), ("a@x", "t9"))

    def test_key_names_are_matched_case_and_hyphen_insensitively(self):
        with tempfile.TemporaryDirectory() as d:
            self._dir(d, {"Jira-Email": "a@x", "apiToken": "t10"})
            self.assertEqual(auth.load()["api_token"], "t10")

    def test_unknown_keys_report_key_names_never_values(self):
        with tempfile.TemporaryDirectory() as d:
            self._dir(d, {"mail": "a@x", "secret": "DONOTLEAK"})
            with self.assertRaises(auth.CredentialError) as cm:
                auth.load()
            msg = str(cm.exception)
            self.assertIn("mail", msg)        # 키 이름은 알려준다
            self.assertIn("secret", msg)
            self.assertNotIn("DONOTLEAK", msg)  # 값은 절대 싣지 않는다
            self.assertIn("인식할 수 있는 키가 없습니다", msg)

    def test_absent_file_and_wrong_keys_say_different_things(self):
        # 둘을 한 문장으로 뭉치면 파일을 제대로 써 놓고도 원인을 못 찾는다.
        with tempfile.TemporaryDirectory() as d:
            os.environ.pop(auth.ENV_EMAIL, None)
            os.environ.pop(auth.ENV_TOKEN, None)
            os.environ.pop("APPDATA", None)
            os.environ["XDG_CONFIG_HOME"] = d
            with self.assertRaises(auth.CredentialError) as absent:
                auth.load()
            self._dir(d, {"mail": "a@x"})
            with self.assertRaises(auth.CredentialError) as wrong:
                auth.load()
            self.assertNotEqual(str(absent.exception), str(wrong.exception))
            self.assertIn("만드십시오", str(absent.exception))

    def test_missing_token_says_email_is_also_required(self):
        with tempfile.TemporaryDirectory() as d:
            self._dir(d, {"email": "a@x"})
            with self.assertRaises(auth.CredentialError) as cm:
                auth.load()
            self.assertIn("토큰", str(cm.exception))

    def test_status_never_leaks_the_token(self):
        # D-007 — 서버 응답에 토큰이 실리는 경로를 아예 만들지 않는다.
        with tempfile.TemporaryDirectory() as d:
            self._dir(d, {"email": "austin@example.com", "api_token": "SUPERSECRET"})
            st = auth.status()
            self.assertNotIn("SUPERSECRET", json.dumps(st, ensure_ascii=False))
            self.assertTrue(st["configured"])
            self.assertEqual(st["email"], "a*****@example.com")


if __name__ == "__main__":
    unittest.main()
