"""F065: real subprocess acknowledgement, rejection, deadline and cleanup."""
import json
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[2]

class FixtureProtocolTests(unittest.TestCase):
    def check_mode(self, mode):
        result=subprocess.run(['node','tests/i4_fixture_checks.cjs',mode],cwd=ROOT,
                              capture_output=True,timeout=35)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout),{'mode':mode,'status':'PASS'})
    def test_actual_fixture_acknowledges_fifty_controls(self): self.check_mode('actual')
    def test_real_provider_edit_error_is_rejected(self): self.check_mode('provider_rejection')
    def test_wrong_id_cannot_complete_pending_command(self): self.check_mode('wrong_id')
    def test_malformed_reply_fails_and_stops_child(self): self.check_mode('malformed')
    def test_rejected_command_is_not_acknowledged(self): self.check_mode('rejected')
    def test_missing_reply_times_out_and_poisoned_channel_stops(self): self.check_mode('timeout')
    def test_startup_exit_is_observed(self): self.check_mode('startup')
    def test_startup_without_configuration_times_out(self): self.check_mode('startup_timeout')
    def test_acknowledged_stop_without_exit_times_out_and_kills(self): self.check_mode('shutdown_timeout')
    def test_deadlines_reject_pending_and_allow_completed(self): self.check_mode('bounds')

if __name__=='__main__':unittest.main()
