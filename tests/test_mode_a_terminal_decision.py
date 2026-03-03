import unittest

from velora.run import _mode_a_status_for_terminal_decision


class TestModeATerminalDecision(unittest.TestCase):
    def test_finalize_success_maps_to_ready(self):
        self.assertEqual(_mode_a_status_for_terminal_decision("finalize_success"), "ready")

    def test_stop_failure_maps_to_failed(self):
        self.assertEqual(_mode_a_status_for_terminal_decision("stop_failure"), "failed")

    def test_unknown_decision_raises(self):
        with self.assertRaises(ValueError):
            _mode_a_status_for_terminal_decision("nope")


if __name__ == "__main__":
    unittest.main()
