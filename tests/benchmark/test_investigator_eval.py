"""The Investigator evaluation, and the corpus it runs on.

The corpus is the instrument. If it is miscalibrated - phrased in the extractor's own
vocabulary, or annotated inconsistently - then every number it produces is decoration, so
the corpus is tested more carefully here than the evaluator is.

No test in this file makes a network call. The Gemini arm is exercised by running it, and
its results are recorded in `docs/investigator-evaluation.md`; what is checked here is the
scoring, the propagation analysis, and the corpus's own integrity.
"""

from __future__ import annotations

import pytest

from backend.agents.llm import PRIMACY_WORDS, VAGUE_WORDS
from benchmark.explanation_corpus import CORPUS, STRATA
from benchmark.investigator_eval import (
    Extraction,
    OfflineExtractor,
    run,
    score_extraction,
    summarise,
)


class TestTheCorpusIsAFairInstrument:
    """A corpus written in the extractor's own vocabulary measures nothing."""

    @pytest.mark.parametrize("case", CORPUS, ids=lambda c: c.id)
    def test_no_case_uses_the_extractors_primacy_vocabulary(self, case) -> None:
        """The whole point: primacy must be expressed the way people write it.

        If a case said "the primary driver", the keyword matcher would score it correctly
        by construction, and the evaluation would flatter a substring search into looking
        like comprehension.

        Attribution traps are exempt, and deliberately so. There the primacy word is the
        trap: the sentence contains one, and the question is whether the extractor binds it
        to the feature it actually modifies rather than to the nearest one. Removing the
        word would remove the test.
        """
        if case.stratum == "attribution-trap":
            return
        lowered = case.text.lower()
        borrowed = [word for word in PRIMACY_WORDS if word in lowered]
        assert not borrowed, (
            f"{case.id} is phrased using the extractor's own primacy words {borrowed}; "
            "rewrite it the way a person would actually say it"
        )

    @pytest.mark.parametrize("case", CORPUS, ids=lambda c: c.id)
    def test_no_vague_case_uses_the_extractors_vague_vocabulary(self, case) -> None:
        if case.stratum != "vague":
            return
        lowered = case.text.lower()
        borrowed = [word for word in VAGUE_WORDS if word in lowered]
        assert not borrowed, f"{case.id} borrows the extractor's vague words {borrowed}"

    @pytest.mark.parametrize("case", CORPUS, ids=lambda c: c.id)
    def test_every_case_records_why_its_answer_is_right(self, case) -> None:
        """Ground truth a reader cannot audit is ground truth they must take on faith."""
        assert len(case.note) > 30, f"{case.id} needs a real justification, not a label"

    @pytest.mark.parametrize("case", CORPUS, ids=lambda c: c.id)
    def test_the_annotation_is_internally_consistent(self, case) -> None:
        if case.subject is None:
            assert case.primacy is None, f"{case.id}: no driver named, so primacy cannot apply"
            assert not case.testable, f"{case.id}: no driver named, so nothing is testable"
        else:
            assert case.primacy is not None, f"{case.id}: a named driver needs a primacy call"

    def test_the_discriminating_strata_are_balanced(self) -> None:
        """primacy vs influence is the measurement that matters, so neither may dominate."""
        primacy = sum(1 for c in CORPUS if c.stratum == "faithful-primacy")
        influence = sum(1 for c in CORPUS if c.stratum == "faithful-influence")
        assert primacy == influence, (
            "an unbalanced pair lets an extractor score well by always guessing one way"
        )

    def test_the_subject_is_not_always_the_same_feature(self) -> None:
        """Otherwise 'always answer urgency_marker' scores well and means nothing."""
        named = {c.subject for c in CORPUS if c.subject}
        assert len(named) >= 3, f"the corpus only ever names {named}"

    def test_attribution_traps_cannot_be_beaten_positionally(self) -> None:
        """Some traps must resolve to the first feature mentioned and some to the second.

        Otherwise "take the last feature named" passes the whole stratum and the trap
        measures word order rather than reading comprehension. This caught exactly that:
        the first three traps all resolved to the second feature, so `trap-04` was written
        with the driver mentioned first.
        """
        aliases = {"urgency_marker": "urgency", "signal_b": "signal_b", "signal_c": "signal_c"}
        resolves_to_first = []
        for case in (c for c in CORPUS if c.stratum == "attribution-trap"):
            lowered = case.text.lower()
            positions = sorted(
                (lowered.find(alias), name)
                for name, alias in aliases.items()
                if lowered.find(alias) >= 0
            )
            assert len(positions) >= 2, f"{case.id} is not a trap: it names one feature"
            resolves_to_first.append(positions[0][1] == case.subject)
        assert any(resolves_to_first) and not all(resolves_to_first), (
            "every trap resolves the same way positionally; "
            f"first-mentioned-is-the-answer: {resolves_to_first}"
        )


class TestScoring:
    def test_a_perfect_extraction_scores_perfectly(self) -> None:
        case = next(c for c in CORPUS if c.stratum == "faithful-primacy")
        got = Extraction(subject=case.subject, primacy=True, testability=0.92)
        assert score_extraction(case, got).fully_right

    def test_primacy_is_not_scored_where_it_does_not_apply(self) -> None:
        """A vague case has no primacy answer, so neither guess may count against."""
        case = next(c for c in CORPUS if c.subject is None)
        for guess in (True, False):
            got = Extraction(subject=None, primacy=guess, testability=0.1)
            assert score_extraction(case, got).primacy_right

    def test_an_error_below_the_testability_gate_is_not_consequential(self) -> None:
        """The distinction the headline accuracy hides."""
        case = next(c for c in CORPUS if c.subject is None)
        absorbed = score_extraction(case, Extraction("urgency_marker", False, 0.10))
        assert not absorbed.fully_right
        assert not absorbed.consequential

    def test_the_same_error_above_the_gate_is_consequential(self) -> None:
        case = next(c for c in CORPUS if c.subject is None)
        acted_on = score_extraction(case, Extraction("urgency_marker", False, 0.60))
        assert not acted_on.fully_right
        assert acted_on.consequential


class TestTheOfflineArm:
    """Its measured weakness is the empirical argument for using a language model."""

    @pytest.fixture(scope="class")
    def report(self) -> dict:
        return run(OfflineExtractor())

    def test_it_runs_every_case(self, report: dict) -> None:
        assert report["cases"] == len(CORPUS)
        assert set(report["by_stratum"]) == set(STRATA)

    def test_the_keyword_matcher_fails_the_discriminating_measurement(
        self, report: dict
    ) -> None:
        """Primacy F1 near zero is the finding, and it is asserted so it cannot quietly
        improve without someone noticing that the argument for the LLM has changed."""
        assert report["primacy_f1"] < 0.5, (
            "the offline reasoner now detects primacy; docs/investigator-evaluation.md "
            f"reports that it cannot (F1 {report['primacy_f1']})"
        )

    def test_primacy_asserted_without_a_keyword_is_missed_entirely(
        self, report: dict
    ) -> None:
        assert report["by_stratum"]["faithful-primacy"]["primacy"] == 0.0

    def test_extraction_errors_reach_the_verdict(self, report: dict) -> None:
        """The decisive number: a wrong claim is not absorbed by the protocol."""
        propagation = report["propagation"]
        assert propagation["compared"] > 0
        assert propagation["p_verdict_changed_given_wrong"] > 0.0, (
            "if extraction errors stopped changing verdicts, the protocol would be robust "
            "to its own extractor - a good result, but a different claim than the one "
            "currently documented"
        )
        assert propagation["p_verdict_changed_given_right"] == 0.0


class TestTheReportIsHonest:
    def test_every_failure_is_published_with_its_reasoning(self) -> None:
        report = run(OfflineExtractor())
        expected = round((1 - report["fully_correct"]) * report["cases"])
        assert len(report["failures"]) == expected
        assert all(f["why_this_is_the_right_answer"] for f in report["failures"])

    def test_an_empty_result_set_does_not_crash_the_summary(self) -> None:
        """Division guards, checked rather than assumed."""
        scores = [
            score_extraction(case, Extraction(case.subject, bool(case.primacy), 0.92))
            for case in CORPUS
        ]
        summary = summarise(scores, "perfect")
        assert summary["propagation"]["p_verdict_changed_given_wrong"] is None
