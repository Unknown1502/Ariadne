"""Measure the Investigator: does claim extraction actually work?

    python -m benchmark.investigator_eval                     # offline reasoner, free
    python -m benchmark.investigator_eval --gemini --project P   # live, ~50 calls

This closes the gap `docs/limitations.md` has been declaring without measuring. Every other
number in this repository is produced with extraction done by ``OfflineReasoner``, so
"Ariadne verifies explanations" has never been tested end to end - what has been tested is
that the verifier correctly processes claims a keyword matcher produced.

Three things are reported, in ascending order of how much they matter.

**Extraction accuracy**, per stratum. Which feature did it name, did it detect primacy, did
it recognise an untestable claim. Per-stratum because an aggregate hides the only
interesting failures - an extractor can score well overall while failing every
attribution-trap, and those are the cases where being wrong is worst.

**Primacy F1**, separately, because it is the discriminating measurement. Identical evidence
yields opposite verdicts depending on whether primacy was claimed, so an extractor that
cannot separate "the main reason" from "one of the reasons" makes the protocol's
claim-sensitivity a fiction regardless of how well it finds feature names.

**Error propagation** - the decisive analysis. For every case, the extracted claim and the
annotator's correct claim are both run through the real experiment engine and verifier, and
the verdicts are compared. This distinguishes two very different worlds:

  extraction errors that change the verdict     the LLM is a load-bearing risk
  extraction errors that do not                 the protocol is robust to its own extractor

Both are publishable and the second would be a genuinely good result. Silence is the only
outcome that is not, which is why this runs on the offline reasoner with no API key: the
measurement exists either way, and adding Gemini adds an arm rather than enabling the
experiment.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.agents.llm import GeminiClient, LLMRequest, OfflineReasoner
from backend.agents.prompts import CLAIM_COMPILER_SYSTEM, build_claim_prompt
from backend.core.enums import VerdictStatus
from backend.experiment_engine.distributions import FEATURE_NAMES
from backend.experiment_engine.runner import ExperimentRunner
from backend.verifier.verifier import verify
from benchmark.explanation_corpus import CORPUS, STRATA, ExplanationCase
from tests.factories import make_case

MIN_TESTABILITY = 0.30
"""Mirrors the verifier's gate. An extraction below this yields INCONCLUSIVE regardless of
what the experiment finds, so it is the threshold that decides whether a claim is tested."""


@dataclass(frozen=True, slots=True)
class Extraction:
    subject: str | None
    primacy: bool
    testability: float

    @property
    def testable(self) -> bool:
        return self.testability >= MIN_TESTABILITY


@dataclass
class CaseScore:
    case: ExplanationCase
    got: Extraction
    subject_right: bool
    primacy_right: bool
    testable_right: bool
    verdict_with_extracted: str | None = None
    verdict_with_truth: str | None = None

    @property
    def fully_right(self) -> bool:
        return self.subject_right and self.primacy_right and self.testable_right

    @property
    def consequential(self) -> bool:
        """A wrong extraction the testability gate does *not* absorb.

        The distinction the raw accuracy hides. An extractor that names a driver for
        "the model weighed everything it saw" but scores it 0.1 testable is wrong on paper
        and harmless in practice: the claim never reaches an experiment, and the verdict is
        INCONCLUSIVE either way. One that scores the same sentence 0.6 has manufactured a
        hypothesis out of a non-statement, and the system will go and test it.

        Only the second kind can produce a wrong verdict, so only the second kind is a
        defect in the thing this project claims to do."""
        return not self.fully_right and self.got.testable

    @property
    def verdict_changed(self) -> bool | None:
        if self.verdict_with_extracted is None or self.verdict_with_truth is None:
            return None
        return self.verdict_with_extracted != self.verdict_with_truth


class OfflineExtractor:
    """The extractor every published number in this repository actually used."""

    name = "offline-deterministic-reasoner"

    def __init__(self) -> None:
        self._reasoner = OfflineReasoner()

    def __call__(self, text: str) -> Extraction:
        response = self._reasoner.generate(
            LLMRequest(system="", user=text, task="compile_claim", context={"explanation": text})
        )
        payload = json.loads(response.text)
        subject = payload.get("subject")
        return Extraction(
            subject=None if subject in (None, "unspecified") else str(subject),
            primacy=bool(payload.get("primacy_claim", False)),
            testability=float(payload.get("testability_score", 0.0)),
        )


class GeminiExtractor:
    """Gemini through the *production* prompt path, not a prompt written for this test.

    It uses `CLAIM_COMPILER_SYSTEM` and `build_claim_prompt` - the same system prompt and
    the same user prompt the Investigator sends in a real investigation. Writing a bespoke
    prompt here would measure a prompt that never runs, and would flatter the result by
    letting the evaluation tune the thing being evaluated.
    """

    def __init__(self, *, project: str, model: str = "gemini-2.5-flash") -> None:
        self.name = f"{model} (vertex, production prompt)"
        self._client = GeminiClient(
            model=model, use_vertex=True, project=project, max_output_tokens=2048
        )

    def __call__(self, text: str) -> Extraction:
        response = self._client.generate(
            LLMRequest(
                system=CLAIM_COMPILER_SYSTEM,
                user=build_claim_prompt(
                    explanation=text,
                    decision="HIGH_PRIORITY",
                    model_id="synthetic-triage",
                    model_version="2.0.0",
                    distribution_version="baseline_2024.1",
                    available_features=list(FEATURE_NAMES),
                ),
                task="compile_claim",
                context={"explanation": text},
                temperature=0.0,
            )
        )
        payload = json.loads(response.text)
        subject = payload.get("subject")
        return Extraction(
            subject=None if subject in (None, "unspecified", "") else str(subject),
            primacy=bool(payload.get("primacy_claim", False)),
            testability=float(payload.get("testability_score", 0.0)),
        )


def score_extraction(case: ExplanationCase, got: Extraction) -> CaseScore:
    """Compare one extraction against the annotator's reading.

    Primacy is only scored where the annotator recorded an answer. On a case naming no single
    driver the question does not arise, and marking an extractor wrong for a coin-flip on an
    inapplicable field would inflate the failure rate with noise.
    """
    subject_right = got.subject == case.subject
    primacy_right = True if case.primacy is None else got.primacy == case.primacy
    return CaseScore(
        case=case,
        got=got,
        subject_right=subject_right,
        primacy_right=primacy_right,
        testable_right=got.testable == case.testable,
    )


def _verdict_for(subject: str, primacy: bool, version: str) -> str:
    """Run the real engine and verifier for a claim, and return the verdict.

    Uses the production runner and verifier rather than a reimplementation, so the
    propagation measurement reflects what the system would actually have concluded.
    """
    claim, plan = make_case(version)
    claim = claim.model_copy(
        update={
            "subject": subject,
            "primacy_claim": primacy,
            "target_variables": [subject],
            "testability_score": 0.92 if primacy else 0.55,
        }
    )
    plan = plan.model_copy(
        update={
            "claim_id": claim.id,
            "intervention": plan.intervention.model_copy(update={"variable": subject}),
        }
    )
    result = ExperimentRunner().run(plan, claim)
    return str(verify(result.evidence, claim, plan).status)


def measure_propagation(scores: list[CaseScore], version: str) -> None:
    """Fill in the verdict each claim would have produced, extracted vs correct.

    Only for cases the annotator marked testable and where a subject exists - an untestable
    claim never reaches an experiment, so there is no verdict to compare.
    """
    for score in scores:
        if not score.case.testable or score.case.subject is None:
            continue
        try:
            score.verdict_with_truth = _verdict_for(
                score.case.subject, bool(score.case.primacy), version
            )
            score.verdict_with_extracted = (
                str(VerdictStatus.INCONCLUSIVE)
                if not score.got.testable or score.got.subject is None
                else _verdict_for(score.got.subject, score.got.primacy, version)
            )
        except Exception as exc:  # noqa: BLE001 - a case that cannot run is reported, not hidden
            score.verdict_with_extracted = f"ERROR: {type(exc).__name__}: {exc}"


def _f1(tp: int, fp: int, fn: int) -> float:
    if tp == 0:
        return 0.0
    precision, recall = tp / (tp + fp), tp / (tp + fn)
    return round(2 * precision * recall / (precision + recall), 4)


def summarise(scores: list[CaseScore], extractor: str) -> dict[str, Any]:
    n = len(scores)
    tp = sum(1 for s in scores if s.case.primacy is True and s.got.primacy)
    fp = sum(1 for s in scores if s.case.primacy is not True and s.got.primacy)
    fn = sum(1 for s in scores if s.case.primacy is True and not s.got.primacy)

    compared = [s for s in scores if s.verdict_changed is not None]
    changed = [s for s in compared if s.verdict_changed]
    wrong_extraction = [s for s in compared if not s.fully_right]
    changed_given_wrong = [s for s in wrong_extraction if s.verdict_changed]
    right_extraction = [s for s in compared if s.fully_right]
    changed_given_right = [s for s in right_extraction if s.verdict_changed]

    return {
        "extractor": extractor,
        "cases": n,
        "subject_accuracy": round(sum(s.subject_right for s in scores) / n, 4),
        "primacy_accuracy": round(sum(s.primacy_right for s in scores) / n, 4),
        "testability_accuracy": round(sum(s.testable_right for s in scores) / n, 4),
        "fully_correct": round(sum(s.fully_right for s in scores) / n, 4),
        "consequential_error_rate": round(sum(s.consequential for s in scores) / n, 4),
        "absorbed_by_testability_gate": sum(
            1 for s in scores if not s.fully_right and not s.got.testable
        ),
        "primacy_f1": _f1(tp, fp, fn),
        "primacy_confusion": {"tp": tp, "fp": fp, "fn": fn},
        "by_stratum": {
            stratum: {
                "n": len(group),
                "subject": round(sum(s.subject_right for s in group) / len(group), 4),
                "primacy": round(sum(s.primacy_right for s in group) / len(group), 4),
                "fully_correct": round(sum(s.fully_right for s in group) / len(group), 4),
                "consequential": sum(s.consequential for s in group),
            }
            for stratum in STRATA
            if (group := [s for s in scores if s.case.stratum == stratum])
        },
        "propagation": {
            "compared": len(compared),
            "verdict_changed": len(changed),
            "extraction_wrong": len(wrong_extraction),
            "changed_given_extraction_wrong": len(changed_given_wrong),
            "changed_given_extraction_right": len(changed_given_right),
            "p_verdict_changed_given_wrong": (
                round(len(changed_given_wrong) / len(wrong_extraction), 4)
                if wrong_extraction else None
            ),
            "p_verdict_changed_given_right": (
                round(len(changed_given_right) / len(right_extraction), 4)
                if right_extraction else None
            ),
        },
        "failures": [
            {
                "id": s.case.id,
                "stratum": s.case.stratum,
                "text": s.case.text,
                "expected": {
                    "subject": s.case.subject,
                    "primacy": s.case.primacy,
                    "testable": s.case.testable,
                },
                "got": {
                    "subject": s.got.subject,
                    "primacy": s.got.primacy,
                    "testability": s.got.testability,
                },
                "verdict_with_truth": s.verdict_with_truth,
                "verdict_with_extracted": s.verdict_with_extracted,
                "why_this_is_the_right_answer": s.case.note,
            }
            for s in scores
            if not s.fully_right
        ],
    }


def render(report: dict[str, Any]) -> str:
    out: list[str] = [
        "# Investigator evaluation",
        "",
        f"Extractor: `{report['extractor']}`  |  cases: {report['cases']}",
        "",
        "| metric | value |",
        "|---|---|",
        f"| subject accuracy | {report['subject_accuracy']:.1%} |",
        f"| primacy accuracy | {report['primacy_accuracy']:.1%} |",
        f"| testability accuracy | {report['testability_accuracy']:.1%} |",
        f"| **fully correct** | **{report['fully_correct']:.1%}** |",
        f"| primacy F1 | {report['primacy_f1']:.3f} |",
        f"| **consequential error rate** | **{report['consequential_error_rate']:.1%}** |",
        f"| errors absorbed by the testability gate | "
        f"{report['absorbed_by_testability_gate']} |",
        "",
        "## By stratum",
        "",
        "| stratum | n | subject | primacy | fully correct | consequential errors |",
        "|---|---|---|---|---|---|",
    ]
    for stratum, row in report["by_stratum"].items():
        out.append(
            f"| {stratum} | {row['n']} | {row['subject']:.0%} | {row['primacy']:.0%} "
            f"| {row['fully_correct']:.0%} | {row['consequential']} |"
        )
    prop = report["propagation"]
    out += [
        "",
        "## Error propagation",
        "",
        "Does a wrong extraction produce a wrong verdict? This is the question that decides",
        "whether the extractor is a load-bearing risk or an absorbable one.",
        "",
        f"- verdicts compared: **{prop['compared']}**",
        f"- extractions wrong: **{prop['extraction_wrong']}**",
        f"- verdict changed: **{prop['verdict_changed']}**",
        f"- P(verdict changed | extraction wrong): **{prop['p_verdict_changed_given_wrong']}**",
        f"- P(verdict changed | extraction right): **{prop['p_verdict_changed_given_right']}**",
        "",
    ]
    if report["failures"]:
        out += ["## Every failure, with the annotator's reasoning", ""]
        for failure in report["failures"]:
            out += [
                f"### `{failure['id']}` ({failure['stratum']})",
                "",
                f"> {failure['text']}",
                "",
                f"- expected: {failure['expected']}",
                f"- got: {failure['got']}",
                f"- verdict with correct claim: `{failure['verdict_with_truth']}`",
                f"- verdict with extracted claim: `{failure['verdict_with_extracted']}`",
                f"- why: {failure['why_this_is_the_right_answer']}",
                "",
            ]
    return "\n".join(out) + "\n"


def run(extractor: Any, *, version: str = "2.0.0") -> dict[str, Any]:
    scores = [score_extraction(case, extractor(case.text)) for case in CORPUS]
    measure_propagation(scores, version)
    return summarise(scores, extractor.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate claim extraction.")
    parser.add_argument("--out", default=None, help="directory for the report")
    parser.add_argument("--gemini", action="store_true", help="run the live Gemini arm")
    parser.add_argument("--project", default="", help="GCP project for --gemini")
    parser.add_argument("--model", default="gemini-2.5-flash", help="Gemini model id")
    parser.add_argument(
        "--version", default="2.0.0",
        help="target model version for the propagation analysis (default 2.0.0, where the "
             "standing claim is true, so an extraction error has somewhere to go)",
    )
    args = parser.parse_args()

    if args.gemini:
        if not args.project:
            parser.error("--gemini needs --project")
        extractor: Any = GeminiExtractor(project=args.project, model=args.model)
    else:
        extractor = OfflineExtractor()
    report = run(extractor, version=args.version)
    text = render(report)
    print(text)
    if args.out:
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "investigator.json").write_text(json.dumps(report, indent=2), "utf-8")
        (directory / "investigator.md").write_text(text, "utf-8")


if __name__ == "__main__":
    main()
