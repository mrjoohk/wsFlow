"""One real Node child exercises four named UI contracts; this is not a browser."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[2]
CASES = {
    'blocks_selection_through_status_refresh_and_calendar',
    'restores_selection_admission_after_each_failure',
    'poll_preserves_existing_action_owner_and_visibility',
    'pending_polls_restore_controls_and_continue_requests',
}


class GoogleAuthRefreshTests(unittest.TestCase):
    def test_actual_ui_auth_refresh_contract(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for the actual UI contract child')
        child = subprocess.run([node, 'tests/google_auth_refresh.cjs'], cwd=SOURCE,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                               capture_output=True, text=True, encoding='utf-8', timeout=20)
        print(child.stdout, end="")
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        lines = child.stdout.splitlines()
        self.assertEqual(len(lines), 1, child.stdout)
        result = json.loads(lines[0])
        self.assertEqual(result['passed'], 4)
        self.assertEqual(len(result['cases']), 4)
        self.assertEqual(set(result['cases']), CASES)
        self.assertEqual(result['scope'], 'actual UI in Node VM with DOM/API fixtures; not a browser')


if __name__ == '__main__':
    unittest.main()
