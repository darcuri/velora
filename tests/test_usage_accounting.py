from types import SimpleNamespace
import unittest

from velora.acpx import AcpUsage
from velora.run import _accumulate_acpx_usage


def _result_with_used(used: int, *, model_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(usage=AcpUsage(used=used, model_id=model_id))


class TestUsageAccounting(unittest.TestCase):
    def test_first_seen_session_value_sets_baseline_without_charge(self):
        request = {"history": {"tokens_used_estimate": 0, "cost_usd_estimate": 0.0}}
        delta = _accumulate_acpx_usage(
            request,
            session_name="coord-run-1",
            result=_result_with_used(1200),
            actor="coordinator",
        )

        hist = request["history"]
        self.assertEqual(delta, 0)
        self.assertEqual(hist["tokens_used_estimate"], 0)
        self.assertEqual(hist["session_usage_baselines"]["coord-run-1"], 1200)
        self.assertEqual(hist["session_usage"]["coord-run-1"], 1200)
        self.assertEqual(hist["session_usage_deltas"]["coord-run-1"], 0)

    def test_subsequent_session_updates_charge_only_delta(self):
        request = {"history": {"tokens_used_estimate": 0, "cost_usd_estimate": 0.0}}
        _accumulate_acpx_usage(
            request,
            session_name="coord-run-1",
            result=_result_with_used(1200),
            actor="coordinator",
        )
        delta = _accumulate_acpx_usage(
            request,
            session_name="coord-run-1",
            result=_result_with_used(1235),
            actor="coordinator",
        )

        hist = request["history"]
        self.assertEqual(delta, 35)
        self.assertEqual(hist["tokens_used_estimate"], 35)
        self.assertEqual(hist["session_usage_deltas"]["coord-run-1"], 35)
        self.assertEqual(hist["coordinator_tokens_used_estimate"], 35)

    def test_counter_reset_rebaselines_without_inventing_charge(self):
        request = {"history": {"tokens_used_estimate": 0, "cost_usd_estimate": 0.0}}
        _accumulate_acpx_usage(
            request,
            session_name="worker-run-1",
            result=_result_with_used(200),
            actor="worker",
            branch="velora/task123",
        )
        _accumulate_acpx_usage(
            request,
            session_name="worker-run-1",
            result=_result_with_used(260),
            actor="worker",
            branch="velora/task123",
        )
        delta = _accumulate_acpx_usage(
            request,
            session_name="worker-run-1",
            result=_result_with_used(80),
            actor="worker",
            branch="velora/task123",
        )

        hist = request["history"]
        self.assertEqual(delta, 0)
        self.assertEqual(hist["tokens_used_estimate"], 60)
        self.assertEqual(hist["session_usage_baselines"]["worker-run-1"], 80)
        self.assertEqual(hist["session_usage_deltas"]["worker-run-1"], 60)

    def test_worker_usage_is_attributed_to_branch_estimate(self):
        request = {"history": {"tokens_used_estimate": 0, "cost_usd_estimate": 0.0}}
        _accumulate_acpx_usage(
            request,
            session_name="worker-run-1",
            result=_result_with_used(500),
            actor="worker",
            branch="velora/task123",
        )
        _accumulate_acpx_usage(
            request,
            session_name="worker-run-1",
            result=_result_with_used(520),
            actor="worker",
            branch="velora/task123",
        )
        _accumulate_acpx_usage(
            request,
            session_name="coord-run-1",
            result=_result_with_used(900),
            actor="coordinator",
        )
        _accumulate_acpx_usage(
            request,
            session_name="coord-run-1",
            result=_result_with_used(930),
            actor="coordinator",
        )

        hist = request["history"]
        self.assertEqual(hist["worker_tokens_used_estimate"], 20)
        self.assertEqual(hist["worker_tokens_by_branch_estimate"]["velora/task123"], 20)
        self.assertEqual(hist["coordinator_tokens_used_estimate"], 30)
        self.assertEqual(hist["tokens_used_estimate"], 50)


if __name__ == "__main__":
    unittest.main()
