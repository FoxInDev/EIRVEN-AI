from __future__ import annotations

import unittest

from eirven_ai.resilience import AdaptiveRecovery


class AdaptiveRecoveryTests(unittest.TestCase):
    def test_four_failures_switch_strategy(self) -> None:
        recovery = AdaptiveRecovery(attempts_per_strategy=4, max_strategy_changes=2)
        for attempt in range(3):
            directive = recovery.record_failure(signature=f"a{attempt}", reason="no change")
            self.assertEqual(directive.action, "continue")
        directive = recovery.record_failure(signature="a3", reason="no change")
        self.assertEqual(directive.action, "switch_strategy")
        self.assertEqual(recovery.strategy_generation, 1)
        self.assertEqual(recovery.attempts_in_strategy, 0)

    def test_uncertain_commit_never_retries(self) -> None:
        recovery = AdaptiveRecovery()
        directive = recovery.record_failure(completed=True, verified=False, reason="bubble hidden")
        self.assertEqual(directive.action, "stop_uncertain_commit")
        self.assertEqual(recovery.attempts_in_strategy, 0)

    def test_round_trip_state(self) -> None:
        recovery = AdaptiveRecovery()
        recovery.record_failure(signature="x", reason="first")
        restored = AdaptiveRecovery.from_dict(recovery.to_dict())
        self.assertEqual(restored.strategy_generation, recovery.strategy_generation)
        self.assertEqual(restored.failed_signatures, ["x"])


if __name__ == "__main__":
    unittest.main()
