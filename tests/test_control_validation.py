from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from banking_streamlit_app import STREAMLIT_SCENARIOS, _scenario_failures
from banking_streamlit_app_otel import STREAMLIT_SCENARIOS as OTEL_STREAMLIT_SCENARIOS
from banking_streamlit_app_otel_splunk_ao import (
    STREAMLIT_EXAMPLES as OTEL_SPLUNK_STREAMLIT_EXAMPLES,
)
from banking_streamlit_app_splunk_ao import STREAMLIT_EXAMPLES as SPLUNK_STREAMLIT_EXAMPLES
from setup_controls import control_specs


class ScenarioValidationTests(unittest.TestCase):
    scenario = {
        "expected_status": "blocked",
        "expected_steers": 0,
        "expected_verified_2fa": False,
        "expected_control": "demo-deny-risky-transfer-composite",
        "expected_min_control_spans": 2,
    }
    base_result = {
        "status": "blocked",
        "steering_history": [],
        "final_transfer": {"verified_2fa": False},
        "answer": "blocked by demo-deny-risky-transfer-composite",
    }

    def test_prompt_injection_scenario_remains_available(self) -> None:
        scenario = STREAMLIT_SCENARIOS["Luna prompt-injection deny"]

        self.assertIn("Ignore previous instructions", scenario["prompt"])
        self.assertEqual(scenario["expected_status"], "blocked")
        self.assertEqual(
            scenario["expected_control"],
            "demo-observe-luna-transfer-request",
        )

    def test_prompt_injection_scenario_is_available_in_every_streamlit_app(self) -> None:
        scenario_name = "Luna prompt-injection deny"
        scenario_maps = (
            STREAMLIT_SCENARIOS,
            OTEL_STREAMLIT_SCENARIOS,
            SPLUNK_STREAMLIT_EXAMPLES,
            OTEL_SPLUNK_STREAMLIT_EXAMPLES,
        )

        for scenario_map in scenario_maps:
            with self.subTest(scenario_map=id(scenario_map)):
                self.assertIn(scenario_name, scenario_map)

    def test_logger_result_fails_without_local_or_persisted_control_spans(self) -> None:
        failures = _scenario_failures(
            self.scenario,
            {
                **self.base_result,
                "control_span_count": 0,
                "persisted_control_span_count": 0,
                "persisted_control_names": [],
            },
        )

        self.assertIn("control spans=0, expected at least 2", failures)
        self.assertIn("persisted control spans=0, expected at least 2", failures)

    def test_logger_result_requires_expected_name_in_persisted_records(self) -> None:
        failures = _scenario_failures(
            self.scenario,
            {
                **self.base_result,
                "control_span_count": 2,
                "persisted_control_span_count": 2,
                "persisted_control_names": [],
            },
        )

        self.assertEqual(
            failures,
            ["control telemetry did not identify 'demo-deny-risky-transfer-composite'"],
        )

    def test_otel_result_requires_raw_and_typed_control_spans(self) -> None:
        failures = _scenario_failures(
            self.scenario,
            {
                **self.base_result,
                "raw_control_events": 2,
                "typed_control_spans": 0,
                "persisted_control_names": [],
            },
        )

        self.assertIn("persisted typed control spans=0, expected at least 2", failures)


class LunaConfigurationTests(unittest.TestCase):
    def test_luna_control_is_omitted_without_real_scorer_id(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GALILEO_LUNA_SCORER_ID", None)
            names = [name for name, _ in control_specs()]

        self.assertNotIn("demo-observe-luna-transfer-request", names)

    def test_luna_control_uses_environment_scorer_id(self) -> None:
        with patch.dict(os.environ, {"GALILEO_LUNA_SCORER_ID": "actual-scorer-id"}):
            specs = dict(control_specs())

        luna_config = specs["demo-observe-luna-transfer-request"]["condition"]["evaluator"][
            "config"
        ]
        self.assertEqual(luna_config["scorer_id"], "actual-scorer-id")


if __name__ == "__main__":
    unittest.main()
