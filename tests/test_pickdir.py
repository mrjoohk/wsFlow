import os
import unittest
from unittest import mock

from helpers import TempDir  # noqa: F401 — sys.path 설정
from wsflow import pickdir


class PickDir(unittest.TestCase):
    def test_hook_returns_path_or_none(self):
        with mock.patch.dict(os.environ, {pickdir.TEST_ENV: "K:/ws/x"}):
            self.assertEqual(pickdir.pick_folder(), "K:/ws/x")
        with mock.patch.dict(os.environ, {pickdir.TEST_ENV: ""}):
            self.assertIsNone(pickdir.pick_folder())  # 취소

    def test_child_failure_is_runtime_error(self):
        # 자식 프로세스가 비정상 종료(tkinter 부재 등)하면 stderr 를 담은 RuntimeError 로 끝나야 한다.
        env = {k: v for k, v in os.environ.items() if k != pickdir.TEST_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(pickdir, "_SCRIPT", "import sys; sys.stderr.write('no tk'); sys.exit(2)"):
            with self.assertRaises(RuntimeError) as cm:
                pickdir.pick_folder("/nowhere")
            self.assertIn("no tk", str(cm.exception))

    def test_child_success_roundtrip(self):
        # 자식이 출력한 경로(한글 포함)가 그대로 돌아온다 — 인코딩 고정 확인.
        env = {k: v for k, v in os.environ.items() if k != pickdir.TEST_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(pickdir, "_SCRIPT", "import sys; sys.stdout.write('D:/작업/ws')"):
            self.assertEqual(pickdir.pick_folder(), "D:/작업/ws")


if __name__ == "__main__":
    unittest.main()
