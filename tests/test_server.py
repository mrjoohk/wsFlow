import json
import os
import threading
import unittest
import urllib.error
import urllib.request

from helpers import TempDir, make_bare, make_repo
from wsflow.server import serve


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = TempDir()
        cls.t = cls._td.__enter__()
        cls.httpd = serve(cls.t / "projects.json", "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.token = cls.httpd.wsflow_token
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._td.__exit__(None, None, None)

    def call(self, method, path, body=None, expect=200):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-Auth-Token": self.token}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                self.assertEqual(r.status, expect)
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")  # 한 번만 읽는다 — 두 번째 read() 는 빈 문자열
            self.assertEqual(e.code, expect, raw)
            return json.loads(raw or "{}")

    def test_pick_folder_route(self):
        from unittest import mock
        from wsflow import pickdir
        with mock.patch.dict(os.environ, {pickdir.TEST_ENV: str(self.t)}):
            r = self.call("POST", "/api/pick-folder", {"initial": ""})
            self.assertEqual((r["path"], r["cancelled"]), (str(self.t), False))
        with mock.patch.dict(os.environ, {pickdir.TEST_ENV: ""}):
            r = self.call("POST", "/api/pick-folder", {})
            self.assertEqual((r["path"], r["cancelled"]), (None, True))
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != pickdir.TEST_ENV}, clear=True), \
             mock.patch.object(pickdir, "_SCRIPT", "import sys; sys.stderr.write('no tk'); sys.exit(2)"):
            r = self.call("POST", "/api/pick-folder", {}, expect=500)
            self.assertIn("no tk", r.get("error", ""))

    def test_new_project_and_deploy_routes(self):
        from unittest import mock
        from wsflow import pickdir
        t = self.t; ws = t / "ws2"; ws.mkdir()
        tmpl = ws / "0.PROJECT_TEMPLATE"; tmpl.mkdir(); (tmpl / "R.md").write_text("r", encoding="utf-8")
        self.call("POST", "/api/roots", {"path": str(ws)})
        p = self.call("POST", "/api/projects/new", {"root": str(ws), "name": "fresh"})
        self.assertTrue((ws / "fresh").is_dir()); self.assertEqual(p["id"], "fresh")
        self.call("POST", "/api/projects/new", {"root": str(ws), "name": "fresh"}, expect=400)       # 중복
        self.call("POST", "/api/projects/new", {"root": str(ws), "name": "a/b"}, expect=400)         # 구분자
        self.call("POST", "/api/projects/new", {"root": str(t / "notroot"), "name": "x"}, expect=400)  # 미등록 루트
        with mock.patch.dict(os.environ, {pickdir.TEST_ENV: f"{tmpl / 'R.md'}\n{tmpl / 'R.md'}"}):
            r = self.call("POST", "/api/pick-files", {})
            self.assertEqual(len(r["paths"]), 2); self.assertFalse(r["cancelled"])
        self.call("POST", "/api/deploy/plan", {"sources": [], "ids": [p["id"]]}, expect=400)
        self.call("POST", "/api/deploy/plan", {"sources": [str(tmpl)], "ids": []}, expect=400)
        plan = self.call("POST", "/api/deploy/plan", {"sources": [str(tmpl)], "ids": [p["id"]]})
        self.assertEqual(plan["total"], {"new": 1, "changed": 0, "same": 0})
        res = self.call("POST", "/api/deploy/apply", {"sources": [str(tmpl)], "ids": [p["id"]]})
        self.assertEqual(res["copied_total"], 1); self.assertEqual((ws / "fresh" / "R.md").read_text(encoding="utf-8"), "r")
        self.call("DELETE", f"/api/projects/{p['id']}")

    def test_flow(self):
        t = self.t
        ws = t / "ws"
        ws.mkdir()
        repo = make_repo(ws, "alpha")
        (ws / "plain").mkdir()
        bare = make_bare(t)

        self.assertIn("roots", self.call("GET", "/api/config"))
        self.call("POST", "/api/roots", {"path": str(ws)})
        self.call("POST", "/api/roots", {"path": str(t / "missing")}, expect=400)
        scan = self.call("GET", f"/api/scan?root={urllib.request.quote(str(ws))}")
        self.assertEqual({i["name"] for i in scan["items"]}, {"alpha", "plain"})

        pr = self.call("POST", "/api/projects", {"path": str(repo)})
        self.assertEqual(pr["id"], "alpha")
        self.call("POST", "/api/projects", {"path": str(repo)}, expect=400)  # 중복
        plain = self.call("POST", "/api/projects", {"path": str(ws / "plain")})
        lst = self.call("GET", "/api/projects")["projects"]
        self.assertEqual({p["id"]: p["git"] for p in lst}, {"alpha": "repo", "plain": "none"})

        # git 없는 프로젝트는 409, 규칙 탭은 됨
        self.call("GET", f"/api/projects/{plain['id']}/status", expect=409)
        self.assertFalse(self.call("GET", f"/api/projects/{plain['id']}/rules")["global_rules"])
        self.call("GET", "/api/projects/nope/status", expect=404)

        st = self.call("GET", "/api/projects/alpha/status")
        self.assertEqual(st["branch"], "main")
        self.assertEqual(len(self.call("GET", "/api/projects/alpha/log?n=1")["commits"]), 1)
        self.call("GET", "/api/projects/alpha/commit/zz", expect=400)

        # add / commit
        (repo / "새 파일.txt").write_text("x\n", encoding="utf-8")
        st = self.call("POST", "/api/projects/alpha/add", {"paths": ["새 파일.txt"]})
        self.assertEqual([x["path"] for x in st["staged"]], ["새 파일.txt"])
        c = self.call("POST", "/api/projects/alpha/commit", {"message": "웹에서 커밋"})
        self.assertTrue(c["ok"])
        self.assertEqual(self.call("GET", "/api/projects/alpha/log?n=1")["commits"][0]["subject"], "웹에서 커밋")
        self.call("POST", "/api/projects/alpha/commit", {"message": ""}, expect=500)

        # remote / push
        self.call("POST", "/api/projects/alpha/remotes", {"name": "origin", "url": str(bare)})
        self.assertEqual(self.call("GET", "/api/projects/alpha/remotes")["remotes"][0]["name"], "origin")
        res = self.call("POST", "/api/projects/alpha/push", {"remote": "origin", "branch": "main", "set_upstream": True})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.call("GET", "/api/projects/alpha/status")["upstream"], "origin/main")
        self.call("PUT", "/api/projects/alpha/remotes/origin", {"url": str(bare) + "2"})
        self.call("DELETE", "/api/projects/alpha/remotes/origin")
        self.assertEqual(self.call("GET", "/api/projects/alpha/remotes")["remotes"], [])

        # locks
        (repo / ".git" / "index.lock").write_text("")
        locks = self.call("GET", "/api/locks")["locks"]
        self.assertEqual(sum(len(x["locks"]) for x in locks), 1)
        self.assertEqual(self.call("GET", "/api/projects/alpha/locks")["locks"][0]["age_sec"] >= 0, True)
        # 갓 생긴 lock 은 보존
        res = self.call("POST", "/api/locks/clean", {"ids": ["alpha"]})
        if not res["results"][0]["reason"].startswith("git 프로세스"):
            self.assertEqual(res["results"][0]["removed"], [])
        (repo / ".git" / "index.lock").unlink()

        # 규칙 도구 실행 (파일 없음)
        self.assertFalse(self.call("POST", "/api/projects/alpha/rules/run", {"tool": "graph_checks"})["ok"])

        # 수정 / 제거
        self.assertEqual(self.call("PATCH", "/api/projects/alpha", {"alias": "A"})["alias"], "A")
        self.assertTrue(self.call("DELETE", "/api/projects/alpha")["removed"])
        self.call("GET", "/api/projects/alpha", expect=404)

        # 정적 페이지
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            self.assertIn(b"wsFlow", r.read())


if __name__ == "__main__":
    unittest.main()


class GuardTests(unittest.TestCase):
    """F-004 — 127.0.0.1 바인딩만으로는 사용자가 연 임의 웹페이지의 fetch 를 막지 못한다.

    음성 대조: server.py 의 ``self._guard(url)`` 호출을 지우면 아래 셋이 전부 실패한다.
    """

    @classmethod
    def setUpClass(cls):
        cls._td = TempDir()
        cls.t = cls._td.__enter__()
        cls.httpd = serve(cls.t / "projects.json", "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.token = cls.httpd.wsflow_token
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._td.__exit__(None, None, None)

    def _raw(self, headers, path="/api/config", method="GET", data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.read()
            return e.code

    def test_api_without_token_is_forbidden(self):
        self.assertEqual(self._raw({}), 403)

    def test_api_with_wrong_token_is_forbidden(self):
        self.assertEqual(self._raw({"X-Auth-Token": "not-the-token"}), 403)

    def test_foreign_origin_is_forbidden(self):
        # 토큰이 맞아도 Origin 이 남의 페이지면 막는다(다중 방어).
        self.assertEqual(self._raw({"X-Auth-Token": self.token,
                                    "Origin": "https://evil.example"}), 403)

    def test_own_origin_is_allowed(self):
        self.assertEqual(self._raw({"X-Auth-Token": self.token,
                                    "Origin": f"http://127.0.0.1:{self.port}"}), 200)

    def test_token_in_query_is_allowed(self):
        # 주소창으로 직접 여는 경우(?t=) 도 통과해야 한다.
        self.assertEqual(self._raw({}, path=f"/api/config?t={self.token}"), 200)

    def test_index_needs_no_token(self):
        # 페이지 자체는 토큰 없이 열려야 한다 — 그 페이지가 ?t= 를 읽어 API 를 부른다.
        self.assertEqual(self._raw({}, path="/"), 200)


class JiraStatusRouteTests(GuardTests):
    """/api/jira/status 는 설정 여부만 준다 — 토큰 값이 응답에 실리는 경로를 만들지 않는다(D-007)."""

    def test_status_shape_has_no_token(self):
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/jira/status",
                                     headers={"X-Auth-Token": self.token})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
        data = json.loads(body)
        self.assertIn("configured", data)
        self.assertNotIn("api_token", body)
        self.assertNotIn("password", body)

    def test_status_still_needs_the_guard(self):
        self.assertEqual(self._raw({}, path="/api/jira/status"), 403)


class JiraRouteTests(GuardTests):
    """S5 라우트 — 매핑 판정(D-004)과 메모 보호(INV-1)가 HTTP 경계까지 살아 있는가."""

    def _mk(self, name, with_state=True):
        import json as _json
        d = self.t / name
        d.mkdir(parents=True, exist_ok=True)
        if with_state:
            (d / ".workflow").mkdir(exist_ok=True)
            (d / ".workflow" / "state.json").write_text(_json.dumps({
                "schema_version": 4, "project_key": "XXX", "watermark": "2026-09-01T09:00:00+09:00",
                "csc_field": None, "csc_value": None, "epic_keys": [],
                "issue_type_map": {"site_url": "https://example.atlassian.net"}, "roots": {},
            }, ensure_ascii=False), encoding="utf-8")
        return d

    def _api(self, method, path, body=None, expect=200):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-Auth-Token": self.token}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            self.assertEqual(e.code, expect, raw)
            return json.loads(raw or "{}")

    def _register(self, d):
        return self._api("POST", "/api/projects", {"path": str(d)})["id"]

    def test_project_without_state_is_off(self):
        pid = self._register(self._mk("plain", with_state=False))
        info = self._api("GET", f"/api/projects/{pid}/jira")
        self.assertFalse(info["enabled"])
        self.assertIn("설정 작성", info["reason"])

    def test_project_with_state_is_locked_on(self):
        pid = self._register(self._mk("withjira"))
        info = self._api("GET", f"/api/projects/{pid}/jira")
        self.assertTrue(info["enabled"])
        self.assertTrue(info["locked"])
        self.assertTrue(info["schema_ok"])

    def test_memo_round_trip_and_conflict_is_409(self):
        pid = self._register(self._mk("memo"))
        m = self._api("GET", f"/api/projects/{pid}/jira/memo?ymd=260911")
        self._api("PUT", f"/api/projects/{pid}/jira/memo",
                  {"ymd": "260911", "text": "- XXX-1: 했음", "sha": m["sha"], 'document_id': m['document_id']})
        again = self._api("GET", f"/api/projects/{pid}/jira/memo?ymd=260911")
        self.assertIn("XXX-1", again["text"])
        self.assertEqual(again["parsed"]["by_key"]["XXX-1"], ["했음"])
        # 오래된 sha 로 저장하면 409 이고 파일은 그대로다 — 덮지 않는다.
        err = self._api("PUT", f"/api/projects/{pid}/jira/memo",
                        {"ymd": "260911", "text": "덮어쓰기", "sha": m["sha"], 'document_id': m['document_id']}, expect=409)
        self.assertIn("current_text", err)
        self.assertIn("XXX-1", self._api("GET", f"/api/projects/{pid}/jira/memo?ymd=260911")["text"])

    def test_jira_routes_need_the_guard_too(self):
        pid = self._register(self._mk("guarded"))
        self.assertEqual(self._raw({}, path=f"/api/projects/{pid}/jira"), 403)

    def test_no_state_file_refuses_memo_with_409(self):
        pid = self._register(self._mk("nostate", with_state=False))
        err = self._api("GET", f"/api/projects/{pid}/jira/memo?ymd=260911", expect=409)
        self.assertIn("jira", err)


class JiraInitRouteTests(JiraRouteTests):
    """S5b — 설정 작성은 **연동이 꺼진 상태에서도** 돼야 한다. 그 상태를 벗어나려고 부르는 경로다."""

    def test_preview_works_before_any_state_exists(self):
        pid = self._register(self._mk("fresh", with_state=False))
        r = self._api("POST", f"/api/projects/{pid}/jira/init/preview",
                      {"cfg": {"project_key": "XXX",
                               "issue_type_map": {"site_url": "https://example.atlassian.net"}}})
        self.assertFalse(r["preview"]["exists"])
        self.assertEqual(r["state"]["schema_version"], 4)
        self.assertEqual(r["state"]["roots"], {})

    def test_apply_then_detect_is_locked_on(self):
        d = self._mk("applied", with_state=False)
        pid = self._register(d)
        r = self._api("POST", f"/api/projects/{pid}/jira/init/apply",
                      {"cfg": {"project_key": "XXX",
                               "issue_type_map": {"site_url": "https://example.atlassian.net"}}})
        self.assertTrue(r["jira"]["enabled"])
        self.assertTrue(r["jira"]["locked"])
        self.assertTrue((d / ".workflow" / "state.json").is_file())

    def test_reconfigure_preserves_watermark_and_roots(self):
        d = self._mk("again")          # state 가 이미 있다
        pid = self._register(d)
        before = json.loads((d / ".workflow" / "state.json").read_text(encoding="utf-8"))
        before["roots"] = {"R-001": {"jira_key": "XXX-11", "children": []}}
        before["watermark"] = "2026-09-05T10:00:00+09:00"
        (d / ".workflow" / "state.json").write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
        self._api("POST", f"/api/projects/{pid}/jira/init/apply",
                  {"cfg": {"project_key": "XXX", "csc_value": "[BBB] 바뀜",
                           "issue_type_map": {"site_url": "https://example.atlassian.net"}}})
        after = json.loads((d / ".workflow" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(after["watermark"], before["watermark"])
        self.assertEqual(after["roots"], before["roots"])
        self.assertEqual(after["csc_value"], "[BBB] 바뀜")

    def test_apply_refuses_when_gitignore_would_leak_work_data(self):
        d = self._mk("leaky", with_state=False)
        (d / ".git").mkdir()
        (d / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        pid = self._register(d)
        cfg = {"cfg": {"project_key": "XXX",
                       "issue_type_map": {"site_url": "https://example.atlassian.net"}}}
        err = self._api("POST", f"/api/projects/{pid}/jira/init/apply", cfg, expect=409)
        self.assertIn("gitignore", err)
        self.assertFalse((d / ".workflow" / "state.json").is_file())   # 쓰지 않았다
        # 동의하면 빠진 줄을 덧붙이고 진행한다
        r = self._api("POST", f"/api/projects/{pid}/jira/init/apply", {**cfg, "fix_gitignore": True})
        self.assertEqual(r["gitignore"]["missing"], [])
        self.assertIn("__pycache__/", (d / ".gitignore").read_text(encoding="utf-8"))

    def test_gitignore_report_route(self):
        pid = self._register(self._mk("gi", with_state=False))
        self.assertFalse(self._api("GET", f"/api/projects/{pid}/jira/gitignore")["applicable"])

    def test_new_plan_refuses_out_of_scope_title_with_422(self):
        import json as _json
        d = self._mk("scoped", with_state=False)
        (d / ".workflow").mkdir(exist_ok=True)
        (d / ".workflow" / "state.json").write_text(_json.dumps({
            "schema_version": 4, "project_key": "XXX", "watermark": "2026-09-01T09:00:00+09:00",
            "csc_field": None, "csc_value": None, "epic_keys": [],
            "title_include": ["^\\[SCH\\]"], "title_exclude": [], "title_prefix": None,
            "issue_type_map": {"site_url": "https://example.atlassian.net", "task_types": ["작업"]},
            "roots": {},
        }, ensure_ascii=False), encoding="utf-8")
        pid = self._register(d)
        from unittest import mock
        from wsflow import auth
        with mock.patch.object(auth,'load',side_effect=auth.CredentialError('synthetic offline')):
            err = self._api("POST", f"/api/projects/{pid}/jira/new/plan",
                            {"summary": "접두사 없는 제목"}, expect=422)
            self.assertIn("error", err)
            ok = self._api("POST", f"/api/projects/{pid}/jira/new/plan", {"summary": "[SCH] 맞는 제목"})
        self.assertFalse(ok['ready_to_create'])
        self.assertEqual(ok["payload"]["fields"]["summary"], "[SCH] 맞는 제목")
