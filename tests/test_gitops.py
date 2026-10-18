import time
import unittest
from pathlib import Path

from helpers import TempDir, git, make_bare, make_repo
from wsflow import gitops


class RepoKind(unittest.TestCase):
    def test_kinds(self):
        with TempDir() as t:
            r = make_repo(t)
            self.assertEqual(gitops.repo_kind(r), "repo")
            (t / "plain").mkdir()
            self.assertEqual(gitops.repo_kind(t / "plain"), "none")
            (t / "broken" / ".git").mkdir(parents=True)  # stockTrader 사례: 빈 .git 디렉터리
            self.assertEqual(gitops.repo_kind(t / "broken"), "broken")


class Status(unittest.TestCase):
    def test_status_shapes(self):
        with TempDir() as t:
            r = make_repo(t)
            st = gitops.status(r)
            self.assertEqual(st["branch"], "main")
            self.assertTrue(st["clean"])
            (r / "f0.txt").write_text("changed\n", encoding="utf-8")
            (r / "새파일.txt").write_text("x\n", encoding="utf-8")
            st = gitops.status(r)
            self.assertEqual([x["path"] for x in st["unstaged"]], ["f0.txt"])
            self.assertEqual(st["untracked"], ["새파일.txt"])  # core.quotepath=false 로 한글 그대로
            gitops.add(r, ["f0.txt"])
            st = gitops.status(r)
            self.assertEqual([x["path"] for x in st["staged"]], ["f0.txt"])
            self.assertFalse(st["clean"])

    def test_status_uses_no_optional_locks(self):
        # 음성 대조(F-003): --no-optional-locks 를 빼면 실패한다. 실제 lock 생성은 순간적이어서
        # 호출 인자로 검증한다 — 옵션은 하위 명령(status) 앞의 전역 옵션이어야 한다.
        from unittest import mock
        with TempDir() as t:
            r = make_repo(t)
            real = gitops.subprocess.run
            seen = []
            def spy(cmd, **kw):
                seen.append(cmd); return real(cmd, **kw)
            with mock.patch.object(gitops.subprocess, "run", spy):
                gitops.status(r)
            cmd = next(c for c in seen if "status" in c)  # status() 는 뒤에 `stash list` 도 부른다
            self.assertIn("--no-optional-locks", cmd)
            self.assertLess(cmd.index("--no-optional-locks"), cmd.index("status"))

    def test_add_rejects_option_like_path(self):
        with TempDir() as t:
            r = make_repo(t)
            with self.assertRaises(gitops.GitError):
                gitops.add(r, ["--all"])


class Log(unittest.TestCase):
    def test_log_and_show(self):
        with TempDir() as t:
            r = make_repo(t, commits=3)
            rows = gitops.log(r, n=2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["subject"], "c2 커밋")
            self.assertIn("HEAD -> main", rows[0]["refs"])
            d = gitops.show_files(r, rows[0]["hash"])
            self.assertEqual(d["message"], "c2 커밋")
            self.assertTrue(any("f2.txt" in f for f in d["files"]))

    def test_log_empty_repo(self):
        with TempDir() as t:
            r = make_repo(t, commits=0)
            self.assertEqual(gitops.log(r), [])


class Commit(unittest.TestCase):
    def test_commit_utf8_message_roundtrip(self):
        with TempDir() as t:
            r = make_repo(t)
            (r / "a.txt").write_text("a\n", encoding="utf-8")
            msg = "feat: 한글 메시지\n\n본문 두 번째 줄 — 대시와 “따옴표”"
            res = gitops.commit(r, msg, add_all=True)
            self.assertTrue(res["ok"])
            body = git(r, "log", "-1", "--format=%B").stdout.strip()
            self.assertEqual(body, msg)  # -F 파일 경유로 콘솔 인코딩과 무관하게 보존

    def test_commit_empty_message_rejected(self):
        with TempDir() as t:
            r = make_repo(t)
            with self.assertRaises(gitops.GitError):
                gitops.commit(r, "   ")


class Remote(unittest.TestCase):
    def test_remote_crud_and_push(self):
        with TempDir() as t:
            r = make_repo(t)
            bare = make_bare(t)
            self.assertEqual(gitops.remotes(r), [])
            gitops.remote_add(r, "origin", str(bare))
            rem = gitops.remotes(r)
            self.assertEqual(rem[0]["name"], "origin")
            self.assertEqual(rem[0]["push"], str(bare))
            res = gitops.push(r, "origin", "main", set_upstream=True)
            self.assertTrue(res["ok"], res)
            st = gitops.status(r)
            self.assertEqual(st["upstream"], "origin/main")
            self.assertEqual(st["ahead"], 0)
            gitops.remote_set_url(r, "origin", str(bare) + "x")
            self.assertTrue(gitops.remotes(r)[0]["fetch"].endswith("x"))
            gitops.remote_remove(r, "origin")
            self.assertEqual(gitops.remotes(r), [])

    def test_push_without_remote_fails_cleanly(self):
        with TempDir() as t:
            r = make_repo(t)
            res = gitops.push(r, "origin", "main")
            self.assertFalse(res["ok"])
            self.assertNotEqual(res["returncode"], 0)

    def test_bad_remote_name(self):
        with TempDir() as t:
            r = make_repo(t)
            with self.assertRaises(gitops.GitError):
                gitops.remote_add(r, "--upload-pack=x", "u")


class Locks(unittest.TestCase):
    """음성 대조(Rule 12): lock 감지가 없으면 두 테스트 다 실패한다."""

    def test_find_and_clean(self):
        with TempDir() as t:
            r = make_repo(t)
            self.assertEqual(gitops.find_locks(r), [])
            lock = r / ".git" / "index.lock"
            lock.write_text("", encoding="utf-8")
            old = time.time() - 60
            import os
            os.utime(lock, (old, old))
            found = gitops.find_locks(r)
            self.assertEqual([Path(x["path"]).name for x in found], ["index.lock"])
            self.assertGreaterEqual(found[0]["age_sec"], 50)
            res = gitops.clean_locks(r, min_age_sec=5)
            if res["reason"].startswith("git 프로세스"):
                self.skipTest("호스트에서 git 프로세스가 실행 중")
            self.assertEqual([Path(x).name for x in res["removed"]], ["index.lock"])
            self.assertFalse(lock.exists())

    def test_fresh_lock_is_kept(self):
        with TempDir() as t:
            r = make_repo(t)
            lock = r / ".git" / "index.lock"
            lock.write_text("", encoding="utf-8")  # 방금 생김 → 진행 중일 수 있으니 보존
            res = gitops.clean_locks(r, min_age_sec=5)
            if res["reason"].startswith("git 프로세스"):
                self.skipTest("호스트에서 git 프로세스가 실행 중")
            self.assertEqual(res["removed"], [])
            self.assertTrue(lock.exists())

    def test_broken_git_dir_locks(self):
        with TempDir() as t:
            (t / "b" / ".git").mkdir(parents=True)
            (t / "b" / ".git" / "index.lock").write_text("")
            self.assertEqual(len(gitops.find_locks(t / "b")), 1)


if __name__ == "__main__":
    unittest.main()
