import csv
import json
import unittest
from pathlib import Path

from helpers import TempDir, make_repo
from wsflow import scanner
from wsflow.store import Store, norm


class StoreTests(unittest.TestCase):
    def test_roots_and_projects(self):
        with TempDir() as t:
            s = Store(t / "projects.json")
            (t / "ws").mkdir()
            r = s.add_root(t / "ws")
            self.assertEqual(r["path"], norm(t / "ws"))
            self.assertEqual(s.add_root(t / "ws"), r)  # 중복 추가는 멱등
            with self.assertRaises(ValueError):
                s.add_root(t / "nope")
            p1 = make_repo(t / "ws", "Alpha")
            pr = s.add_project(p1)
            self.assertEqual(pr["id"], "alpha")
            self.assertEqual(pr["alias"], "Alpha")
            with self.assertRaises(ValueError):
                s.add_project(p1)  # 이미 등록
            # 표기만 다른 이름("Alpha!" → id "alpha") → id 충돌 회피. 대소문자 차이("alpha")는
            # Windows(대소문자 무시 FS)에서 같은 폴더가 되어 FileExistsError 가 나므로 쓰지 않는다(F-002).
            (t / "ws" / "Alpha!").mkdir()
            pr2 = s.add_project(t / "ws" / "Alpha!")
            self.assertEqual(pr2["id"], "alpha-2")
            s.update_project("alpha", alias="A", tags=["x"])
            self.assertEqual(s.get("alpha")["tags"], ["x"])
            # 재로드 후에도 유지
            s2 = Store(t / "projects.json")
            self.assertEqual(len(s2.projects()), 2)
            self.assertTrue(s2.remove_project("alpha"))
            self.assertFalse(s2.remove_project("alpha"))
            self.assertTrue(s2.remove_root(t / "ws"))

    def test_template_dir_for(self):
        with TempDir() as t:
            s = Store(t / "p.json")
            ws = t / "ws"
            (ws / "0.PROJECT_TEMPLATE").mkdir(parents=True)
            (ws / "proj").mkdir()
            s.add_root(ws)
            self.assertEqual(s.template_dir_for(ws / "proj"), ws / "0.PROJECT_TEMPLATE")
            self.assertIsNone(s.template_dir_for(t))  # 루트 밖


class ScannerTests(unittest.TestCase):
    def _project(self, t: Path, name="proj", git=False):
        p = make_repo(t, name) if git else (t / name)
        p.mkdir(exist_ok=True)
        (p / "GLOBAL_RULES.md").write_text("G\n", encoding="utf-8")
        (p / "PROJECT_RULES.md").write_text("P\n", encoding="utf-8")
        (p / "graph").mkdir()
        with (p / "graph" / "edges.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "type", "severity", "title", "location", "status", "fix_ref", "test_ref", "source_doc", "notes"])
            w.writerow(["F-001", "finding", "high", "t", "l", "open", "", "", "", ""])
            w.writerow(["F-002", "finding", "high", "t", "l", "fixed", "", "", "", ""])
            w.writerow(["F-003", "finding", "high", "t", "l", "closed", "", "", "", ""])  # pLLM 사례: 비허용 상태값
        (p / "logs").mkdir()
        (p / "logs" / "prompts.jsonl").write_text(json.dumps({"ts": "2026-09-04 10:00", "req_id": "P-007", "prompt": "x", "response": "y", "outputs": [], "schema": "v2"}, ensure_ascii=False) + "\n", encoding="utf-8")
        (p / "logs" / "files.jsonl").write_text(
            json.dumps({"ts": "2026-09-04 10:00", "files": ["a"], "summary": "s", "req_id": "P-007", "basis_ids": [], "verify": "검증 없음", "commit": "미커밋", "schema": "v2"}, ensure_ascii=False) + "\n"
            + json.dumps({"ts": "2026-09-04 10:01", "files": ["b"], "summary": "s", "req_id": "P-007", "basis_ids": [], "verify": "t", "commit": "abc", "schema": "v2"}, ensure_ascii=False) + "\n"
            + "not json\n", encoding="utf-8")
        return p

    def test_rules_report(self):
        with TempDir() as t:
            tpl = t / "0.PROJECT_TEMPLATE"
            tpl.mkdir()
            (tpl / "GLOBAL_RULES.md").write_text("G\n", encoding="utf-8")
            (tpl / "tools").mkdir()
            (tpl / "tools" / "graph_checks.py").write_text("print(1)\n", encoding="utf-8")
            p = self._project(t, git=True)
            (p / "tools").mkdir()
            (p / "tools" / "graph_checks.py").write_text("print(2)\n", encoding="utf-8")  # 드리프트
            (p / "_PARKED.txt").write_text("", encoding="utf-8")
            make_repo(p, "nested")  # 중첩 저장소
            r = scanner.rules_report(p, tpl)
            self.assertEqual(r["git"], "repo")
            self.assertTrue(r["global_rules"] and r["project_rules"])
            self.assertEqual(r["ledger"]["counts"], {"open": 1, "fixed": 1, "closed": 1})
            self.assertEqual(r["ledger"]["invalid_status"], {"closed": 1})
            w = r["worklog"]
            self.assertEqual((w["last_req_id"], w["prompts"], w["files"], w["uncommitted"], w["unverified"], w["bad_lines"]), ("P-007", 1, 2, 1, 1, 1))
            drift = {d["file"]: d for d in r["drift"]}
            self.assertTrue(drift["GLOBAL_RULES.md"]["match"])
            self.assertFalse(drift["tools/graph_checks.py"]["match"])
            self.assertIsNone(drift["tools/worklog/validate_worklog.py"]["match"])
            self.assertTrue(r["markers"]["parked"])
            self.assertEqual(r["nested_repos"], ["nested"])

    def test_scan_root(self):
        with TempDir() as t:
            p = self._project(t, "proj", git=True)
            (t / "data").mkdir()
            (t / ".hidden").mkdir()
            (t / "broken" / ".git").mkdir(parents=True)
            items = {i["name"]: i for i in scanner.scan_root(t, {str(p.resolve()).replace("\\", "/")})}
            self.assertNotIn(".hidden", items)
            self.assertTrue(items["proj"]["registered"] and items["proj"]["project_rules"] and items["proj"]["git"] == "repo")
            self.assertEqual(items["data"]["git"], "none")
            self.assertEqual(items["broken"]["git"], "broken")

    def test_run_tool_missing_and_present(self):
        with TempDir() as t:
            p = self._project(t)
            self.assertFalse(scanner.run_tool(p, "graph_checks")["ok"])
            (p / "tools").mkdir()
            (p / "tools" / "graph_checks.py").write_text("import sys; print('ok 한글'); sys.exit(0)\n", encoding="utf-8")
            res = scanner.run_tool(p, "graph_checks")
            self.assertTrue(res["ok"])
            self.assertIn("ok 한글", res["stdout"])
            self.assertFalse(scanner.run_tool(p, "nope")["ok"])


if __name__ == "__main__":
    unittest.main()
