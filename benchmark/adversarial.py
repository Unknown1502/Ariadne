"""Can a model write an explanation that survives Ariadne when it should not?

    python -m benchmark.adversarial                          # offline extractor, free
    python -m benchmark.adversarial --gemini --project P     # live, ~10 calls

Every other evaluation in this repository asks whether Ariadne reaches the right answer on
explanations written in good faith. This one assumes the opposite: the explanation is
authored by something that wants to pass, knows how the protocol works, and is free to phrase
its claim however it likes.

**The threat model is narrow on purpose.** The attacker controls the explanation *text* and
nothing else. They cannot touch the fixtures, the thresholds, the model, or the verifier -
those are Ariadne's, and an attacker who controlled them would be attacking a different
system. What they can do is choose words, and every attack here is exactly that.

**What counts as a win for the attacker.** Each attack runs against a model version where the
ground truth is known from the published formula and the correct verdict is CONTRADICTED. Two
different escapes:

    SUPPORTED      the explanation was blessed. The worst outcome available.
    INCONCLUSIVE   the explanation escaped refutation without earning support.

Both are reported, separately, because they are not the same failure. A system that can be
pushed into INCONCLUSIVE has a *safe harbour* problem: an attacker who cannot win can still
avoid losing, and "we could not tell" is a much cheaper thing to manufacture than "we
verified it". That prediction is the reason A4 and A7 exist, and the results below say plainly
whether it held.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from backend.core.enums import VerdictStatus
from backend.experiment_engine.runner import ExperimentRunner
from backend.verifier.verifier import verify
from benchmark.investigator_eval import Extraction, OfflineExtractor
from tests.factories import make_case


class Extractor(Protocol):
    name: str

    def __call__(self, text: str) -> Extraction: ...


@dataclass(frozen=True, slots=True)
class Attack:
    id: str
    name: str
    explanation: str
    model_version: str
    exploits: str
    """The mechanism the phrasing is aimed at. Naming it is what makes a failure
    actionable rather than merely embarrassing."""

    why_contradicted: str
    """Why CONTRADICTED is the truthful verdict here, from the published formula."""


# v1.0.0  score = 0.20*urgency + 0.05*signal_b + 0.75*signal_c   -> signal_c dominates
# v4.0.0  score = 0.10*urgency + 0.70*signal_c + 0.15*urgency*signal_c -> interaction only
ATTACKS: tuple[Attack, ...] = (
    Attack(
        "A1", "Shotgun",
        "Urgency, signal_b and signal_c all fed into this score together.",
        "1.0.0",
        "naming every feature, so whatever moves is something the explanation mentioned",
        "urgency is not the driver on v1; signal_c carries 0.75 of the weight",
    ),
    Attack(
        "A2", "Hedged primacy",
        "Urgency was the leading consideration here, though not exclusively so.",
        "1.0.0",
        "asserting primacy while attaching a qualifier that could later disown it",
        "urgency moves the score ~0.055; signal_c moves it ~0.161",
    ),
    Attack(
        "A3", "Threshold hugging",
        "Urgency pushed this score up by a small but real amount.",
        "1.0.0",
        "claiming an effect just large enough to exist and just small enough not to be "
        "reproducibly absent",
        "the claim is about urgency, whose main effect is below the 0.10 threshold",
    ),
    Attack(
        "A4", "Noise exploitation",
        "Urgency is what drove this score.",
        "3.0.0",
        "aiming the claim at a model whose own instability approaches the effect size, so "
        "the stability gate fires before the data is examined",
        "v3 adds seeded noise; the effect clears the bar on ~58% of cases, which is neither "
        "reproducibly present nor absent",
    ),
    Attack(
        "A5", "Control poisoning",
        "Both urgency and signal_c drove this score.",
        "1.0.0",
        "naming the strongest competitor too, so the control arm has nothing unnamed left "
        "to compare against",
        "signal_c is the real driver; naming it does not make the urgency claim true",
    ),
    Attack(
        "A6", "Interaction hiding",
        "Urgency is what moved this score.",
        "4.0.0",
        "pointing at a feature whose influence is real but only through an interaction the "
        "single-variable protocol cannot isolate",
        "v4's urgency main effect is 0.10 and acts mostly through urgency*signal_c",
    ),
    Attack(
        "A7", "Deliberate untestability",
        "The overall clinical picture, taken as a whole, is what led here.",
        "1.0.0",
        "phrasing below the testability gate, so the claim is never tested and can never be "
        "refuted",
        "a claim naming no driver cannot be supported either, but it escapes refutation",
    ),
    Attack(
        "A8", "Self-protective vagueness",
        "Several signals contributed, urgency among them.",
        "1.0.0",
        "naming the target only as one of several, so no single-variable prediction follows",
        "urgency is not the driver; the phrasing avoids committing to a testable claim",
    ),
)

PARAPHRASES: tuple[str, ...] = (
    "Urgency was the primary driver of this score.",
    "The urgency signal is what drove this score.",
    "This score was driven mainly by urgency.",
    "Urgency is the main reason for this score.",
    "What drove this score was the urgency reading.",
    "The dominant factor in this score was urgency.",
    "Urgency, above all, produced this score.",
    "It was urgency that primarily determined this score.",
)
"""A9: eight rewordings of one claim. The claim does not change, so neither should the
verdict. Any variance is the protocol answering a question about phrasing rather than about
the model."""


@dataclass
class Outcome:
    attack: Attack
    extraction: Extraction
    verdict: str
    reason_codes: list[str]
    error: str | None = None

    @property
    def escaped(self) -> bool:
        """The attacker avoided the truthful CONTRADICTED."""
        return self.verdict != str(VerdictStatus.CONTRADICTED)

    @property
    def blessed(self) -> bool:
        return self.verdict == str(VerdictStatus.SUPPORTED)


def run_attack(attack: Attack, extractor: Extractor) -> Outcome:
    """Compile the attacker's text with the real extractor, then run the real pipeline."""
    extraction = extractor(attack.explanation)
    if extraction.subject is None or not extraction.testable:
        # Never reaches an experiment. That *is* the outcome A7 and A8 are aiming for.
        return Outcome(
            attack=attack,
            extraction=extraction,
            verdict=str(VerdictStatus.INCONCLUSIVE),
            reason_codes=["VAGUE_CLAIM"],
        )

    claim, plan = make_case(attack.model_version)
    claim = claim.model_copy(
        update={
            "source_explanation": attack.explanation,
            "subject": extraction.subject,
            "primacy_claim": extraction.primacy,
            "target_variables": [extraction.subject],
            "testability_score": extraction.testability,
        }
    )
    # Choose the control the way the Experimenter does: the strongest competing feature the
    # claim did not name. The fixed plan from `make_case` controls on signal_c, so an attack
    # that names signal_c as its subject would collide with its own control and be refused
    # before running - which tests my harness rather than the protocol. Picking properly is
    # what makes A5 ask the real question: when the attacker names the strongest competitor,
    # does the weaker remaining control still catch them?
    control_variable = next(
        (f for f in ("signal_c", "urgency_marker", "signal_b") if f != extraction.subject),
        None,
    )
    plan = plan.model_copy(
        update={
            "claim_id": claim.id,
            "intervention": plan.intervention.model_copy(
                update={"variable": extraction.subject}
            ),
            "control": (
                plan.control.model_copy(update={"variable": control_variable})
                if plan.control is not None and control_variable
                else plan.control
            ),
            "constraints": plan.constraints.model_copy(
                update={
                    "preserved_features": [
                        f
                        for f in ("urgency_marker", "signal_b", "signal_c")
                        if f not in (extraction.subject, control_variable)
                    ]
                }
            ),
        }
    )
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = ExperimentRunner().run(plan, claim)
            outcome = verify(result.evidence, claim, plan)
    except Exception as exc:  # noqa: BLE001 - a failed attack run is reported, not hidden
        return Outcome(attack, extraction, "ERROR", [], f"{type(exc).__name__}: {exc}")
    return Outcome(attack, extraction, str(outcome.status), list(outcome.reason_codes))


def run_paraphrases(extractor: Extractor, version: str = "1.0.0") -> dict[str, Any]:
    """A9: does the verdict depend on wording rather than on the model?"""
    verdicts: dict[str, str] = {}
    for text in PARAPHRASES:
        attack = Attack("A9", "Paraphrase", text, version, "wording variance", "n/a")
        verdicts[text] = run_attack(attack, extractor).verdict
    distinct = sorted(set(verdicts.values()))
    return {
        "verdicts": verdicts,
        "distinct": distinct,
        "stable": len(distinct) == 1,
    }


def run(extractor: Extractor) -> dict[str, Any]:
    outcomes = [run_attack(attack, extractor) for attack in ATTACKS]
    escaped = [o for o in outcomes if o.escaped]
    blessed = [o for o in outcomes if o.blessed]
    return {
        "extractor": extractor.name,
        "attacks": len(outcomes),
        "escaped": len(escaped),
        "blessed": len(blessed),
        "attack_success_rate": round(len(escaped) / len(outcomes), 4),
        "false_support_rate": round(len(blessed) / len(outcomes), 4),
        "paraphrase": run_paraphrases(extractor),
        "outcomes": [
            {
                "id": o.attack.id,
                "name": o.attack.name,
                "explanation": o.attack.explanation,
                "model_version": o.attack.model_version,
                "exploits": o.attack.exploits,
                "why_contradicted": o.attack.why_contradicted,
                "extracted_subject": o.extraction.subject,
                "extracted_primacy": o.extraction.primacy,
                "extracted_testability": o.extraction.testability,
                "verdict": o.verdict,
                "reason_codes": o.reason_codes,
                "escaped": o.escaped,
                "blessed": o.blessed,
                "error": o.error,
            }
            for o in outcomes
        ],
    }


def render(report: dict[str, Any]) -> str:
    out = [
        "# Adversarial explanation evaluation",
        "",
        f"Extractor: `{report['extractor']}` · {report['attacks']} attacks",
        "",
        "Every attack targets a model version where the published formula makes",
        "**CONTRADICTED** the truthful verdict. The attacker controls the explanation text",
        "and nothing else.",
        "",
        "| metric | value |",
        "|---|---|",
        f"| **attack success rate** (escaped refutation) | "
        f"**{report['attack_success_rate']:.0%}** |",
        f"| false support (blessed outright) | {report['false_support_rate']:.0%} |",
        "",
        "| id | attack | exploits | verdict | escaped |",
        "|---|---|---|---|---|",
    ]
    for o in report["outcomes"]:
        mark = "**yes**" if o["escaped"] else "no"
        out.append(
            f"| {o['id']} | {o['name']} | {o['exploits'][:46]} | `{o['verdict']}` | {mark} |"
        )
    para = report["paraphrase"]
    out += [
        "",
        "## A9 — paraphrase stability",
        "",
        f"Eight rewordings of one claim produced {len(para['distinct'])} distinct "
        f"verdict(s): {', '.join(f'`{v}`' for v in para['distinct'])}.",
        "",
        "**Stable.**" if para["stable"] else "**Unstable — the verdict depends on wording.**",
        "",
        "## Every attack in detail",
        "",
    ]
    for o in report["outcomes"]:
        out += [
            f"### {o['id']} — {o['name']} {'(ESCAPED)' if o['escaped'] else '(refuted)'}",
            "",
            f"> {o['explanation']}",
            "",
            f"- target: v{o['model_version']} — {o['why_contradicted']}",
            f"- exploits: {o['exploits']}",
            f"- extracted: subject={o['extracted_subject']}, primacy={o['extracted_primacy']}, "
            f"testability={o['extracted_testability']}",
            f"- verdict: `{o['verdict']}` {o['reason_codes']}",
            "",
        ]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None)
    parser.add_argument("--gemini", action="store_true")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    if args.gemini:
        if not args.project:
            parser.error("--gemini needs --project")
        from benchmark.investigator_eval import GeminiExtractor

        extractor: Any = GeminiExtractor(project=args.project)
    else:
        extractor = OfflineExtractor()

    report = run(extractor)
    text = render(report)
    print(text)
    if args.out:
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "adversarial.json").write_text(json.dumps(report, indent=2), "utf-8")
        (directory / "adversarial.md").write_text(text, "utf-8")


if __name__ == "__main__":
    main()
