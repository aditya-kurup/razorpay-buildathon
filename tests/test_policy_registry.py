"""Regression test for the provisional policy rule registry."""

import unittest

from policy.rules import RULE_REGISTRY


class PolicyRegistryTest(unittest.TestCase):
    def test_registry_has_exact_expected_rule_ids(self) -> None:
        expected_rule_ids = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
        registered_rule_ids = tuple(
            getattr(rule, "rule_id", None) for rule in RULE_REGISTRY
        )
        self.assertEqual(registered_rule_ids, expected_rule_ids)


if __name__ == "__main__":
    unittest.main()
