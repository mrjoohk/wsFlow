import unittest
from pathlib import Path

from helpers import TempDir, make_repo
from wsflow import deploy


def _tmpl_for(tmpl):
    return lambda _path: tmpl


class DeployTests(unittest.TestCase):
    def _ws(self, t: Path):
        ws = t / "ws"
        tmpl = ws / "0.PROJECT_TEMPLATE"
        (tmpl / "tools").mkdir(parents=True)
        (tmpl / "GLOBAL_RULES.md").write_text("v2\n", encoding="utf-8")
        (tmpl / "tools" / "check.py").write_text("print(1)\n", encoding="utf-8")
        (tmpl / ".git").mkdir(); (tmpl / ".git" / "HEAD").write_text("x", encoding="utf-8")
        a = make_repo(ws, "a"); b = make_repo(ws, "b")
        (a / "GLOBAL_RULES.md").write_text("v1\n", encoding="utf-8")  # 변경 대상
        (b / "tools").mkdir(); (b / "tools" / "check.py").write_text("print(1)\n", encoding="utf-8")  # 동일
        projs = [{"id": "a", "alias": "a", "path": str(a)}, {"id": "b", "alias": "b", "path": str(b)}]
        return ws, tmpl, a, b, projs

    def test_plan_classifies_and_maps_template_relpath(self):
        with TempDir() as t:
            ws, tmpl, a, b, projs = self._ws(t)
            r = deploy.plan([str(tmpl)], projs, "", _tmpl_for(tmpl))
            pa = next(p for p in r["projects"] if p["id"] == "a"); pb = next(p for p in r["projects"] if p["id"] == "b")
            self.assertEqual(pa["counts"], {"new": 1, "changed": 1, "same": 0})   # tools/check.py 신규, GLOBAL 변경
            self.assertEqual(pb["counts"], {"new": 1, "changed": 0, "same": 1})   # GLOBAL 신규, check.py 동일
            rels = {i["rel"] for i in pa["items"]}
            self.assertEqual(rels, {"GLOBAL_RULES.md", "tools/check.py"})          # 템플릿 상대경로 유지, .git 제외
            self.assertEqual(r["total"], {"new": 2, "changed": 1, "same": 1})

    def test_single_template_file_keeps_relpath(self):
        with TempDir() as t:
            ws, tmpl, a, b, projs = self._ws(t)
            r = deploy.plan([str(tmpl / "tools" / "check.py")], projs[:1], "", _tmpl_for(tmpl))
            self.assertEqual([i["rel"] for i in r["projects"][0]["items"]], ["tools/check.py"])

    def test_non_template_source_goes_to_dest_rel(self):
        with TempDir() as t:
            ws, tmpl, a, b, projs = self._ws(t)
            ext = t / "ext"; (ext / "sub").mkdir(parents=True)
            (ext / "sub" / "f.txt").write_text("f", encoding="utf-8")
            r = deploy.plan([str(ext), str(ext / "sub" / "f.txt")], projs[:1], "vendor/x", _tmpl_for(tmpl))
            rels = sorted(i["rel"] for i in r["projects"][0]["items"])
            self.assertEqual(rels, ["vendor/x/ext/sub/f.txt", "vendor/x/f.txt"])
            r2 = deploy.plan([str(ext / "sub" / "f.txt")], projs[:1], "", _tmpl_for(tmpl))
            self.assertEqual(r2["projects"][0]["items"][0]["rel"], "f.txt")      # 비우면 프로젝트 루트

    def test_rejects_escape_and_recursion(self):
        with TempDir() as t:
            ws, tmpl, a, b, projs = self._ws(t)
            r = deploy.plan([str(tmpl / "GLOBAL_RULES.md")], projs[:1], "../out", lambda _: None)
            self.assertIn("프로젝트 밖", r["projects"][0]["errors"][0]); self.assertEqual(r["projects"][0]["items"], [])
            r = deploy.plan([str(ws)], projs[:1], "", lambda _: None)             # 원본이 대상을 포함
            self.assertIn("재귀", r["projects"][0]["errors"][0])
            r = deploy.plan([str(t / "nope.txt")], projs[:1], "", lambda _: None)
            self.assertIn("원본 없음", r["projects"][0]["errors"][0])

    def test_apply_overwrites_without_backup_and_skips_same(self):
        with TempDir() as t:
            ws, tmpl, a, b, projs = self._ws(t)
            r = deploy.apply([str(tmpl)], projs, "", _tmpl_for(tmpl))
            self.assertEqual(r["copied_total"], 3)
            self.assertEqual((a / "GLOBAL_RULES.md").read_text(encoding="utf-8"), "v2\n")   # 덮어씀
            self.assertTrue((a / "tools" / "check.py").is_file()); self.assertTrue((b / "GLOBAL_RULES.md").is_file())
            self.assertFalse((a / ".git" / "HEAD").read_text(encoding="utf-8") == "x")      # 템플릿의 .git 은 안 옴
            self.assertEqual(list(a.glob("*.bak")), [])                                       # 백업 없음
            ra = next(x for x in r["results"] if x["id"] == "a"); rb = next(x for x in r["results"] if x["id"] == "b")
            self.assertEqual(sorted(ra["copied"]), ["GLOBAL_RULES.md", "tools/check.py"]); self.assertEqual(rb["skipped_same"], 1)
            # 재실행하면 전부 동일 → 0건 (음성 대조: 덮어쓰기 판정이 내용 비교가 아니면 여기서 깨진다)
            self.assertEqual(deploy.apply([str(tmpl)], projs, "", _tmpl_for(tmpl))["copied_total"], 0)


if __name__ == "__main__":
    unittest.main()
