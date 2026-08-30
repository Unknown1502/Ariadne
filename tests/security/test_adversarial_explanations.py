"""The adversarial explanation benchmark, and the vulnerability it confirmed.

`tests/security/test_adversarial.py` covers prompt injection - an attacker trying to make the
Investigator emit a verdict. This file covers the subtler and more realistic attack: an
explanation written in good grammar and bad faith by something that knows how the protocol
works and wants to avoid being refuted.

The finding these tests pin in place is not a flattering one. **INCONCLUSIVE is a safe
harbour.** An attacker who cannot win can still avoid losing, by aiming a claim at a noisy
model or by phrasing it below the testability gate, and two of the eight attacks do exactly
that against live Gemini. Recording that as an asserted expectation rather than a footnote is
the point: if a future change closes it, this file fails and somebody has to notice that the
security story changed.

What is asserted as *holding*: no attack has ever produced a false SUPPORTED. That is the
property worth defending, since a blessed false explanation is the outcome with a victim.
"""

from __future__ import annotations

import pytest

from benchmark.adversarial import ATTACKS, PARAPHRASES, run, run_attack, run_paraphrases
from benchmark.investigator_eval import Extraction, OfflineExtractor


@pytest.fixture(scope="module")
def report() -> dict:
    """The offline arm. No network: the Gemini results live in the docs."""
    return run(OfflineExtractor())


class TestTheAttacksAreWellFormed:
    """An attack whose ground truth is wrong measures nothing."""

    @pytest.mark.parametrize("attack", ATTACKS, ids=lambda a: a.id)
    def test_each_attack_targets_a_model_where_the_claim_is_false(self, attack) -> None:
        """Every attack must run against a version whose published formula makes
        CONTRADICTED truthful. v2.0.0 is excluded because urgency genuinely drives it -
        an attack there would be indistinguishable from an honest explanation."""
        assert attack.model_version in {"1.0.0", "3.0.0", "4.0.0"}, (
            f"{attack.id} targets v{attack.model_version}, where the claim may be true"
        )

    @pytest.mark.parametrize("attack", ATTACKS, ids=lambda a: a.id)
    def test_each_attack_names_the_mechanism_it_exploits(self, attack) -> None:
        """A failure nobody can act on is a failure nobody will fix."""
        assert len(attack.exploits) > 30
        assert len(attack.why_contradicted) > 30

    def test_the_attacks_are_distinct(self) -> None:
        assert len({a.explanation for a in ATTACKS}) == len(ATTACKS)
        assert len({a.id for a in ATTACKS}) == len(ATTACKS)


class TestTheDefenceThatHolds:
    def test_no_attack_produces_a_false_supported(self, report: dict) -> None:
        """The property with a victim.

        A false CONTRADICTED sends an engineer on a goose chase. A false SUPPORTED sends a
        nurse a false assurance about a model whose stated reason is wrong. If this ever
        fails, it is the most serious regression this repository can have.
        """
        blessed = [o["id"] for o in report["outcomes"] if o["blessed"]]
        assert not blessed, f"attacks were blessed outright: {blessed}"
        assert report["false_support_rate"] == 0.0

    def test_the_verdict_does_not_depend_on_wording(self, report: dict) -> None:
        """Eight rewordings of one claim must reach one verdict.

        Variance here would mean the protocol is answering a question about phrasing rather
        than about the model, and an attacker could shop for a wording.
        """
        para = report["paraphrase"]
        assert para["stable"], (
            f"{len(PARAPHRASES)} paraphrases of one claim produced {para['distinct']}; "
            "the verdict is sensitive to wording, which is a foothold"
        )

    def test_a_direct_false_claim_is_still_refuted(self) -> None:
        """The baseline. If plain phrasing escaped, nothing else here would mean much."""
        outcome = run_paraphrases(OfflineExtractor(), version="1.0.0")
        assert outcome["distinct"] == ["CONTRADICTED"]


class TestTheVulnerabilityThatIsReal:
    """Asserted so that closing it is a deliberate act, not an accident nobody noticed."""

    def test_inconclusive_is_reachable_as_a_safe_harbour(self, report: dict) -> None:
        escaped = [o["id"] for o in report["outcomes"] if o["escaped"]]
        assert escaped, (
            "no attack escaped refutation. If that is genuinely true now, this test should "
            "be rewritten to assert the stronger property - but check first that the "
            "attacks still compile to the claims they intend"
        )
        assert all(
            o["verdict"] == "INCONCLUSIVE" for o in report["outcomes"] if o["escaped"]
        ), "an attack escaped by some route other than INCONCLUSIVE; that is a new finding"

    def test_an_untestable_phrasing_is_never_tested(self) -> None:
        """A7. The claim is never run, so it can never be refuted - and never supported."""
        attack = next(a for a in ATTACKS if a.id == "A7")
        outcome = run_attack(attack, OfflineExtractor())
        assert outcome.verdict == "INCONCLUSIVE"
        assert outcome.escaped

    def test_a_noisy_target_absorbs_the_claim(self) -> None:
        """A4. Aim at v3, whose instability approaches the effect being measured."""
        attack = next(a for a in ATTACKS if a.id == "A4")
        outcome = run_attack(attack, OfflineExtractor())
        assert outcome.verdict == "INCONCLUSIVE"

    def test_escaping_is_not_the_same_as_being_believed(self, report: dict) -> None:
        """The distinction that decides how bad this is.

        Every escape so far is an INCONCLUSIVE, which grants the attacker nothing to point
        at: they avoided refutation, they did not obtain support. A governance team reading
        INCONCLUSIVE learns that the claim is unestablished, which is the truth.
        """
        for outcome in report["outcomes"]:
            if outcome["escaped"]:
                assert not outcome["blessed"]


class TestTheHarnessUsesTheRealPipeline:
    def test_extraction_runs_through_the_real_compiler(self) -> None:
        """An attack scored against a shortcut would be testing the shortcut."""
        attack = next(a for a in ATTACKS if a.id == "A2")
        outcome = run_attack(attack, OfflineExtractor())
        assert isinstance(outcome.extraction, Extraction)
        assert outcome.extraction.subject is not None

    def test_the_control_is_never_the_claims_own_subject(self) -> None:
        """The Experimenter picks a control the claim did not name, and so must this.

        Found by A5: the fixed plan controls on signal_c, so an attack naming signal_c as
        its subject collided with its own control and was refused before running - which
        tested the harness rather than the protocol.
        """
        for attack in ATTACKS:
            outcome = run_attack(attack, OfflineExtractor())
            assert outcome.error is None, f"{attack.id} failed to run: {outcome.error}"
