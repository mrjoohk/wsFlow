# -*- coding: utf-8 -*-
"""S4 — 쓰기 단일 통로(D-006). 실 이슈 키는 쓰지 않는다(PROJECT_RULES §7)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wsflow import jirawrite as W  # noqa: E402

CREDS = {"email": "a@b", "api_token": "t"}
BASE = "https://example.atlassian.net"


class Recorder:
    """가짜 전송 — 호출을 기록하고 정해진 응답을 돌려준다."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, url, headers, payload, method):
        self.calls.append({"url": url, "method": method,
                           "body": json.loads(payload.decode("utf-8")) if payload else None})
        return self.responses.pop(0) if self.responses else {"key": "XXX-9", "id": "9"}


class GateTests(unittest.TestCase):
    """이 클래스 전체가 D-006 의 음성 대조다 — execute() 의 승인 조회를 제거하면
    test_no_approval_means_no_call 과 test_same_approval_cannot_push_twice 가 실패한다."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_no_approval_means_no_call(self):
        send = Recorder()
        with self.assertRaises(W.ApprovalRequired):
            W.execute(self.home, CREDS, BASE, "comment", "XXX-1", {"text": "안녕"}, send)
        self.assertEqual(send.calls, [])          # Jira 에 아무 일도 일어나지 않았다

    def test_approved_write_goes_through_and_is_recorded(self):
        send = Recorder()
        W.approve(self.home, "comment", "XXX-1", {"text": "안녕"})
        W.execute(self.home, CREDS, BASE, "comment", "XXX-1", {"text": "안녕"}, send)
        self.assertEqual(len(send.calls), 1)
        self.assertTrue(send.calls[0]["url"].endswith("/rest/api/3/issue/XXX-1/comment"))
        events = [json.loads(l) for l in W.approvals_path(self.home).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["approved", "pushed"])
        self.assertEqual(events[0]["id"], events[1]["id"])

    def test_same_approval_cannot_push_twice(self):
        # 중복 티켓·중복 코멘트는 되돌리기 비용이 가장 크다.
        send = Recorder()
        W.approve(self.home, "create_issue", "XXX", {"fields": {"summary": "s"}})
        W.execute(self.home, CREDS, BASE, "create_issue", "XXX", {"fields": {"summary": "s"}}, send)
        with self.assertRaises(W.ApprovalRequired):
            W.execute(self.home, CREDS, BASE, "create_issue", "XXX", {"fields": {"summary": "s"}}, send)
        self.assertEqual(len(send.calls), 1)

    def test_changed_payload_invalidates_the_approval(self):
        # 승인한 내용과 보내는 내용이 다르면 승인이 아니다.
        send = Recorder()
        W.approve(self.home, "comment", "XXX-1", {"text": "원문"})
        with self.assertRaises(W.ApprovalRequired):
            W.execute(self.home, CREDS, BASE, "comment", "XXX-1", {"text": "몰래 바꾼 문장"}, send)
        self.assertEqual(send.calls, [])

    def test_unpushed_lists_what_is_waiting(self):
        W.approve(self.home, "comment", "XXX-1", {"text": "a"})
        W.approve(self.home, "comment", "XXX-2", {"text": "b"})
        W.execute(self.home, CREDS, BASE, "comment", "XXX-1", {"text": "a"}, Recorder())
        self.assertEqual([r["target"] for r in W.unpushed(self.home)], ["XXX-2"])


class DoneTransitionTests(unittest.TestCase):
    """완료 전이는 하지 않는다 — 이름이 아니라 statusCategory 로 막는다."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def _transitions(self):
        return {"transitions": [
            {"id": "11", "name": "진행 중", "to": {"statusCategory": {"key": "indeterminate"}}},
            {"id": "31", "name": "완료", "to": {"statusCategory": {"key": "done"}}},
        ]}

    def test_done_transition_is_refused_even_when_approved(self):
        send = Recorder([self._transitions()])
        W.approve(self.home, "transition", "XXX-1", {"transition_id": "31"})
        with self.assertRaises(W.ForbiddenWrite):
            W.execute(self.home, CREDS, BASE, "transition", "XXX-1", {"transition_id": "31"}, send)
        # 전이 목록 조회 1회뿐 — 전이 자체는 보내지 않았다.
        self.assertEqual([c["method"] for c in send.calls], ["GET"])

    def test_in_progress_transition_is_allowed(self):
        send = Recorder([self._transitions(), {}])
        W.approve(self.home, "transition", "XXX-1", {"transition_id": "11"})
        W.execute(self.home, CREDS, BASE, "transition", "XXX-1", {"transition_id": "11"}, send)
        self.assertEqual([c["method"] for c in send.calls], ["GET", "POST"])
        self.assertEqual(send.calls[1]["body"], {"transition": {"id": "11"}})

    def test_unknown_transition_is_an_error(self):
        send = Recorder([self._transitions()])
        W.approve(self.home, "transition", "XXX-1", {"transition_id": "99"})
        with self.assertRaises(W.JiraError):
            W.execute(self.home, CREDS, BASE, "transition", "XXX-1", {"transition_id": "99"}, send)


class AdfTests(unittest.TestCase):
    def test_comment_body_is_adf_not_plain_text(self):
        # v3 는 평문 문자열을 받지 않는다.
        send = Recorder()
        home = tempfile.mkdtemp()
        W.approve(home, "comment", "XXX-1", {"text": "첫 줄\n둘째 줄"})
        W.execute(home, CREDS, BASE, "comment", "XXX-1", {"text": "첫 줄\n둘째 줄"}, send)
        body = send.calls[0]["body"]["body"]
        self.assertEqual(body["type"], "doc")
        self.assertEqual(len(body["content"]), 2)
        self.assertEqual(body["content"][1]["content"][0]["text"], "둘째 줄")


if __name__ == "__main__":
    unittest.main()
