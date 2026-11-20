"""Contract ownership, reflection, and removal of old concrete import paths."""

from __future__ import annotations

import unittest
from typing import get_type_hints

import pi_agent_py
import pi_agent_py.api as api
import pi_agent_py.ports as ports
import pi_agent_py.providers.runtime as model_runtime
from pi_agent_py.runtime.session_spec import SessionSpec, SessionSpecResolver
from pi_agent_py.testing import FakeProvider


class PortContractTests(unittest.TestCase):
    def test_contracts_have_one_definition_and_old_locations_are_removed(self) -> None:
        for name in ("ProviderCandidate", "AttemptFailure", "RetryDecision"):
            with self.subTest(name=name):
                contract = getattr(ports, name)
                self.assertEqual(contract.__module__, "pi_agent_py.ports")
                self.assertIs(getattr(pi_agent_py, name), contract)
                self.assertFalse(hasattr(model_runtime, name))
        self.assertFalse(hasattr(model_runtime, "RetryAction"))
        self.assertFalse(hasattr(ports, "SessionSpecResolver"))
        self.assertIs(api.SessionSpecResolver, SessionSpecResolver)

    def test_public_annotations_resolve_without_type_checking_imports(self) -> None:
        self.assertIs(get_type_hints(ports.ProviderCandidate)["provider"], ports.ModelProvider)
        self.assertIs(get_type_hints(ports.RetryPolicy.decide)["failure"], ports.AttemptFailure)
        self.assertIs(get_type_hints(ports.RetryPolicy.decide)["return"], ports.RetryDecision)
        self.assertIs(get_type_hints(ports.BackoffStrategy.delay_seconds)["failure"], ports.AttemptFailure)
        get_type_hints(ports.RoutePolicy.select)
        self.assertIs(get_type_hints(SessionSpecResolver.resolve)["return"], SessionSpec)

    def test_moved_values_keep_validation_and_safe_defaults(self) -> None:
        provider = FakeProvider([])
        candidate = ports.ProviderCandidate("fake", provider)
        self.assertIs(candidate.provider, provider)
        self.assertTrue(candidate.supports_tools)
        self.assertEqual(candidate.purposes, {"agent_turn", "compaction"})
        for options in ({"name": " "}, {"purposes": frozenset()}, {"state_owners": frozenset({" "})}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ports.ProviderCandidate(**({"name": "fake", "provider": provider} | options))
        with self.assertRaises(ValueError):
            ports.RetryDecision("retry_same", " ")


if __name__ == "__main__":
    unittest.main()
