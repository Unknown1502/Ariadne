"""Benchmark regression tests.

The benchmark is the project's headline evidence, so it runs in CI rather than only when
somebody remembers to. A silent drop from 14/14 to 13/14 would otherwise reach a judge
before it reached us.

The ablations are asserted too. If removing the control arm ever stops costing accuracy,
either the control has become decorative or the case that isolates it has stopped working -
both are things to find out immediately.
"""

from __future__ import annotations

import pytest

from backend.core.enums import VerdictStatus
from benchmark.cases import CASES, expected_distribution
from benchmark.run_benchmark import (
    CONFIGURATIONS,
    build_report,
    compute_metrics,
    render_markdown,
    run_case,
)


@pytest.fixture(scope="module")
def report():
    return build_report()


class TestCaseDesign:
    def test_no_single_verdict_dominates_the_suite(self) -> None:
        # An all-CONTRADICTED suite would make an always-contradict system look perfect.
        spread = expected_distribution()
        assert len(spread) >= 3
        assert max(spread.values()) < sum(spread.values()) * 0.6

    def test_every_case_documents_why_its_answer_is_correct(self) -> None:
        for case in CASES:
            assert len(case.rationale) > 60, case.id
            assert case.description, case.id

    def test_case_ids_are_unique(self) -> None:
        ids = [case.id for case in CASES]
        assert len(ids) == len(set(ids))

    def test_all_three_verdicts_are_represented(self) -> None:
        expected = {case.expected for case in CASES if case.expected}
        assert expected == set(VerdictStatus)


class TestFullConfiguration:
    def test_every_case_is_answered_correctly(self, report) -> None:
        metrics = report["metrics"]["full"]
        failures = [r["case_id"] for r in report["results"]["full"] if not r["correct"]]
        assert failures == [], f"full configuration failed: {failures}"
        assert metrics["verdict_accuracy"] == 1.0

    def test_no_false_supports(self, report) -> None:
        # The dangerous error: blessing an explanation that should not be.
        assert report["metrics"]["full"]["false_support"] == 0

    def test_no_false_contradictions(self, report) -> None:
        assert report["metrics"]["full"]["false_contradiction"] == 0

    def test_inconclusive_is_perfectly_calibrated(self, report) -> None:
        assert report["metrics"]["full"]["inconclusive_calibration"] == 1.0

    def test_expected_reason_codes_are_produced(self, report) -> None:
        by_id = {r["case_id"]: r for r in report["results"]["full"]}
        for case in CASES:
            if not case.expected_reason_codes:
                continue
            observed = set(by_id[case.id]["reason_codes"])
            missing = set(case.expected_reason_codes) - observed
            assert not missing, f"{case.id} did not report {missing}"


class TestAblationsEarnTheirPlace:
    def test_removing_the_control_costs_accuracy(self, report) -> None:
        full = report["metrics"]["full"]["verdict_accuracy"]
        ablated = report["metrics"]["no-control"]["verdict_accuracy"]
        assert ablated < full, "the control arm no longer changes any verdict"

    def test_removing_the_control_causes_a_false_support(self, report) -> None:
        assert report["metrics"]["no-control"]["false_support"] >= 1

    def test_removing_the_validity_gate_causes_false_contradictions(self, report) -> None:
        # This is the mechanism that stops a weak probe reading as a refutation.
        assert report["metrics"]["no-validity"]["false_contradiction"] >= 1
        assert report["metrics"]["full"]["false_contradiction"] == 0

    def test_the_floor_reference_matches_the_benchmark_mix_it_restates(self, report) -> None:
        """`assume-faithful` is the constant SUPPORTED, so this is an arithmetic identity.

        It is asserted as one deliberately. The number used to be quoted as evidence that
        trusting a model is costly, which it is not: it is a restatement of how many cases
        this benchmark declares non-SUPPORTED. Pinning it to that computation keeps the two
        from drifting apart, so the report can never present it as a measurement again.
        """
        from benchmark.cases import CASES

        reach_verdict = [c for c in CASES if c.expected is not None]
        not_supported = sum(1 for c in reach_verdict if str(c.expected) != "SUPPORTED")

        floor = report["metrics"]["assume-faithful"]
        assert floor["false_support"] == not_supported, (
            "the floor reference should be wrong on exactly the non-SUPPORTED cases"
        )
        assert floor["false_support_rate"] == pytest.approx(
            not_supported / len(reach_verdict), abs=0.01
        )

    def test_every_configuration_beats_the_floor(self, report) -> None:
        """The only legitimate use of the floor: nothing may score below "always say yes"."""
        floor = report["metrics"]["assume-faithful"]["verdict_accuracy"]
        for name in ("full", "no-control", "no-validity"):
            assert report["metrics"][name]["verdict_accuracy"] > floor


class TestReliabilityScenarios:
    def test_all_reliability_scenarios_pass(self, report) -> None:
        failures = [
            name for name, outcome in report["reliability"].items() if not outcome["passed"]
        ]
        assert failures == [], f"reliability failures: {failures}"

    def test_duplicate_events_are_covered(self, report) -> None:
        assert report["reliability"]["duplicate-event"]["passed"]

    def test_crash_recovery_is_covered(self, report) -> None:
        assert report["reliability"]["worker-crash-recovery"]["passed"]


class TestReproducibility:
    @pytest.mark.parametrize("configuration", CONFIGURATIONS)
    def test_running_a_case_twice_gives_the_same_answer(self, configuration: str) -> None:
        case = CASES[0]
        first = run_case(case, configuration)
        second = run_case(case, configuration)
        assert first.observed == second.observed
        assert first.effect_size == second.effect_size

    def test_metrics_are_a_pure_function_of_results(self, report) -> None:
        from benchmark.run_benchmark import CaseResult

        results = [CaseResult(**row) for row in report["results"]["full"]]
        assert compute_metrics(results) == compute_metrics(results)


class TestReportHonesty:
    def test_the_report_states_its_limitations(self, report) -> None:
        text = " ".join(report["limitations"]).lower()
        assert "invented features" in text
        assert "not a language model" in text
        assert "policy choice" in text

    def test_the_report_publishes_the_ground_truth_formulas(self, report) -> None:
        # A reader has to be able to check the expected answers by hand.
        formulas = [model["formula"] for model in report["target_models"]]
        assert len(formulas) == 4
        assert all("score =" in formula for formula in formulas)

    def test_the_report_records_versions_and_seed(self, report) -> None:
        configuration = report["configuration"]
        assert configuration["verifier_version"]
        assert configuration["policy_version"]
        assert configuration["seed"]

    def test_markdown_renders_without_error(self, report) -> None:
        markdown = render_markdown(report)
        assert "# Ariadne Benchmark Report" in markdown
        assert "## Limitations" in markdown
        assert "assume-faithful" in markdown


class TestTheDocumentedSpreadIsReal:
    """`docs/evaluation.md` publishes the benchmark's verdict mix. It had drifted.

    The doc claimed 3 SUPPORTED / 4 CONTRADICTED / 5 INCONCLUSIVE / 2 NO_VERDICT while the
    suite actually held 2 / 4 / 6 / 2. Harmless on its own — except that this exact
    distribution is what determines the `assume-faithful` row, so a stale copy of it would
    make the report's own explanation of that number wrong.
    """

    def test_evaluation_md_states_the_actual_distribution(self) -> None:
        import pathlib
        import re

        from benchmark.cases import CASES

        actual: dict[str, int] = {}
        for case in CASES:
            key = "NO_VERDICT" if case.expected is None else str(case.expected)
            actual[key] = actual.get(key, 0) + 1

        doc = (
            pathlib.Path(__file__).resolve().parents[2] / "docs" / "evaluation.md"
        ).read_text(encoding="utf-8")
        stated = re.search(r"Expected verdict spread: \*\*(.+?)\.\*\*", doc)
        assert stated, "docs/evaluation.md should publish the expected verdict spread"

        for count, verdict in re.findall(r"(\d+) ([A-Z_]+)", stated.group(1)):
            assert actual.get(verdict) == int(count), (
                f"docs/evaluation.md says {count} {verdict}; the suite has "
                f"{actual.get(verdict, 0)}"
            )
        assert sum(actual.values()) == len(CASES)
