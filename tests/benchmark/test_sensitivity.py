"""The threshold plateau is a published claim, so it is checked like one.

`PREREGISTRATION.md` admits the thresholds were not pre-registered and offers a sensitivity
analysis instead: the result is flat across 0.08-0.12, so the conclusion does not balance on
the exact value of 0.10. That argument is only worth anything while it stays true, and it is
exactly the sort of claim that rots silently when a case is added or a rule changes.

The second test is the more important one. It asserts that the reproducibility threshold is
*insensitive* - that this benchmark provides no evidence for 0.80 over anything else in the
range. That is an admission, and pinning an admission in place matters more than pinning a
flattering number, because the incentive to let it drift is much stronger.
"""

from __future__ import annotations

import pathlib

import pytest

from benchmark.sensitivity import REPRODUCIBILITY, sweep

REPO = pathlib.Path(__file__).resolve().parents[2]


class TestTheEffectThresholdPlateau:
    @pytest.mark.parametrize("threshold", [0.08, 0.09, 0.10, 0.11, 0.12])
    def test_accuracy_holds_across_the_published_plateau(self, threshold: float) -> None:
        """A tuned parameter collapses when moved. This one does not."""
        scored = sweep("min_effect_threshold", (threshold,), uniform=False)
        assert scored[f"{threshold:g}"] == 14, (
            f"PREREGISTRATION.md claims 14/14 across 0.08-0.12; at {threshold} it is "
            f"{scored[f'{threshold:g}']}/14"
        )

    @pytest.mark.parametrize("threshold", [0.04, 0.20])
    def test_the_plateau_has_edges(self, threshold: float) -> None:
        """A plateau with no edges is a claim that the parameter does nothing at all.

        The edges are what make the middle meaningful, so they are asserted too - if these
        started passing at 14/14 the sensitivity argument would need rewriting, not quietly
        widening.
        """
        scored = sweep("min_effect_threshold", (threshold,), uniform=False)
        assert scored[f"{threshold:g}"] < 14


class TestTheReproducibilityThresholdIsUnjustified:
    def test_it_is_flat_across_its_whole_range(self) -> None:
        """The admission in PREREGISTRATION.md section 4, kept honest.

        If a future case ever lands near the reproducibility boundary this test fails, and
        the right response is to *delete the admission* because the benchmark has finally
        earned an opinion about 0.80 - not to widen the assertion.
        """
        scored = sweep("reproducibility_threshold", REPRODUCIBILITY, uniform=True)
        assert len(set(scored.values())) == 1, (
            "the reproducibility threshold now changes the result; PREREGISTRATION.md "
            f"section 4 says it cannot. Scores: {scored}"
        )


class TestThePreregistrationSaysWhatItMustSay:
    """The document's value is entirely in the concessions. Guard those, not the prose."""

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return (REPO / "PREREGISTRATION.md").read_text(encoding="utf-8")

    def test_it_admits_the_thresholds_were_not_preregistered(self, text: str) -> None:
        assert "were not pre-registered" in text

    def test_it_names_the_threshold_the_benchmark_cannot_justify(self, text: str) -> None:
        assert "cannot justify" in text
        assert "reproducibility threshold" in text.lower()

    def test_it_declares_a_primary_metric_in_advance(self, text: str) -> None:
        assert "Primary metric: false-support rate" in text

    def test_it_commits_to_reporting_null_results(self, text: str) -> None:
        assert "Negative and null results are reported" in text
