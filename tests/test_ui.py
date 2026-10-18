"""static/index.html 의 화면 상태 규칙 — node 로 순수 함수 블록을 실행해 검사한다.

UI 는 지금까지 `node --check`(문법) 밖에 시험이 없었다. 화면 상태 규칙(프로젝트별 상태·
저장 전 초안 보존)은 서버 시험으로 잡히지 않으므로, index.html 의 ``<script id="ui-pure">``
블록(DOM 에 닿지 않는 함수만 둔다)을 그대로 node 에 먹여 검사한다. node 가 없으면 건너뛴다.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "static" / "index.html"
NODE = shutil.which("node")


def _blocks(html: str) -> list[str]:
    out, i = [], 0
    while True:
        a = html.find("<script", i)
        if a < 0:
            return out
        a = html.index(">", a) + 1
        b = html.index("</script>", a)
        out.append(html[a:b])
        i = b


def _pure(html: str) -> str:
    tag = html.index('<script id="ui-pure">')
    a = html.index(">", tag) + 1
    return html[a:html.index("</script>", a)]


def _node(js: str) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        name = f.name
    try:
        return subprocess.run([NODE, name], capture_output=True, text=True, encoding="utf-8", timeout=30)
    finally:
        Path(name).unlink(missing_ok=True)


@unittest.skipUnless(NODE, "node 없음")
class SyntaxTests(unittest.TestCase):
    def test_every_script_block_parses(self):
        html = INDEX.read_text(encoding="utf-8")
        for i, js in enumerate(_blocks(html)):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(js)
                name = f.name
            try:
                cp = subprocess.run([NODE, "--check", name], capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(cp.returncode, 0, f"script block #{i}: {cp.stderr[:400]}")
            finally:
                Path(name).unlink(missing_ok=True)


@unittest.skipUnless(NODE, "node 없음")
class PureStateTests(unittest.TestCase):
    """음성 대조: jiraFor 가 프로젝트마다 다른 객체를 주지 않거나, memoTextFor 가 draft 를
    무시하면 실패한다."""

    def run_js(self, body: str) -> str:
        js = _pure(INDEX.read_text(encoding="utf-8")) + "\n" + body
        cp = _node(js)
        self.assertEqual(cp.returncode, 0, cp.stderr[:600])
        return cp.stdout.strip()

    def test_state_is_per_project(self):
        out = self.run_js("""
            const m = new Map();
            const a = jiraFor(m, "a"), b = jiraFor(m, "b");
            a.view = {tasks: [{key: "XXX-1"}]};
            console.log(JSON.stringify([a === b, b.view, jiraFor(m, "a") === a]));
        """)
        self.assertEqual(out, "[false,null,true]")

    def test_unsaved_draft_wins_over_file_text(self):
        out = self.run_js("""
            const m = new Map(); const J = jiraFor(m, "a");
            J.memo = {text: "- XXX-1: 파일 내용", sha: "s"};
            const before = memoTextFor(J);
            J.dirty = true; J.draft = "- XXX-1: 타이핑 중";
            const during = memoTextFor(J);
            J.dirty = false; J.draft = null;
            console.log(JSON.stringify([before, during, memoTextFor(J), unsavedIds(m)]));
        """)
        self.assertEqual(out, '["- XXX-1: 파일 내용","- XXX-1: 타이핑 중","- XXX-1: 파일 내용",[]]')

    def test_unsaved_ids_lists_only_dirty_projects(self):
        out = self.run_js("""
            const m = new Map(); jiraFor(m, "a"); jiraFor(m, "b").dirty = true; jiraFor(m, "c");
            console.log(JSON.stringify(unsavedIds(m)));
        """)
        self.assertEqual(out, '["b"]')

    def test_memo_round_trip_keeps_human_lines(self):
        out = self.run_js("""
            const text = "- XXX-2: 둘\\n사람이 쓴 줄\\n- XXX-1: 하나\\n- [신규] 후보";
            const s = splitMemo(text);
            console.log(joinMemo(s.by, s.rest));
        """)
        self.assertEqual(out, "- XXX-1: 하나\n- XXX-2: 둘\n사람이 쓴 줄\n- [신규] 후보")

    def test_editing_visible_task_keeps_hidden_task_original_lines(self):
        out = self.run_js(r'''
            const original = "\n* XXX-99:  hidden original  \n- XXX-1: old\ncommon\n";
            const edited = patchMemo(original, {"XXX-1": ["changed"]}, splitMemo(original).rest);
            if (!edited.includes("* XXX-99:  hidden original  ")) throw Error("hidden line changed");
            if (!edited.includes("- XXX-1: changed")) throw Error("edit missing");
            console.log("preserved");
        ''')
        self.assertEqual(out, 'preserved')

    def test_document_state_separates_dates_and_wf_home_even_with_same_sha(self):
        out = self.run_js(r'''
            const map = new Map(), a = jiraFor(map, "a", "261017", "home"),
              b = jiraFor(map, "a", "261018", "home"), c = jiraFor(map, "a", "261017", "other");
            a.memo = b.memo = {text:"same", sha:"same"}; a.dirty = true; a.draft = "first day";
            if (a === b || a === c || b.dirty || c.dirty) throw Error("document mixed");
            console.log(JSON.stringify([unsavedIds(map), unsavedDocuments(map).length]));
        ''')
        self.assertEqual(out, '[["a"],1]')

    def test_old_response_and_foreign_day_do_not_replace_current_document(self):
        out = self.run_js(r'''
            const J = jiraFor(new Map(), "a", "261017", "home"); J.requestRevision = 2;
            if (acceptJiraResponse(J, 1, {}, {ymd:"261017",document_id:"a"})) throw Error("old response");
            if (acceptJiraResponse(J, 2, {}, {ymd:"261018",document_id:"b"})) throw Error("wrong day");
            if (!acceptJiraResponse(J, 2, {}, {ymd:"261017",document_id:"a",text:"saved"})) throw Error("current response");
            J.dirty = true; J.draft = "new input";
            acceptJiraResponse(J, 2, {}, {ymd:"261017",document_id:"a",text:"remote"});
            console.log(memoTextFor(J));
        ''')
        self.assertEqual(out, 'new input')


if __name__ == "__main__":
    unittest.main()
