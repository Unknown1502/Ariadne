"""The demo script's story must be true.

`run_demo.py` is the four-minute narrative: v1's explanation fails, v2's holds, v3 is
genuinely ambiguous, v4 fails again, a distribution shift makes the claim untestable, and
duplicate deliveries plus a mid-experiment crash change nothing. It is the script a reviewer
watches and the baseline the recorded demo is rehearsed against.

It was also the largest untested module in the repository. CI executed it and checked only
that it exited zero — so every number it printed was unasserted. A regression that flipped
v2 from SUPPORTED to CONTRADICTED, or quietly stopped suppressing duplicates, would have
left the script printing a *different story* with a passing build, and the first person to
notice would have been whoever was watching the recording.

That is the same failure species this repository keeps finding in itself: something
described in prose that no test holds to its description. So these tests read the script's
actual stdout and assert the narrative beats, and — because "every number printed is
computed during the run, nothing is hardcoded" is itself a claim in the module docstring —
they also check the printed figures against the ledger rows they are supposed to come from.

Runs at reduced repetitions for speed; the verdicts are stable because the clock is manual
and the fixtures are drawn from a declared distribution.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
import sys
from pathlib import Path

import pytest

from backend.core.enums import VerdictStatus
from backend.scripts import run_demo
from backend.scripts.run_demo import BASELINE, MODEL_ID, Demo, main

REPETITIONS = 8

VERDICT_LINE = re.compile(
    r"\[(?P<mark>OK|X|\?)\]\s+(?P<status>[A-Z]+)\s+"
    r"effect=(?P<effect>[-+][\d.]+)\s+control=(?P<control>[-+][\d.]+)\s+"
    r"reproducibility=(?P<repro>[\d.]+)\s+validity=(?P<validity>[\d.]+)"
)


class DemoRun:
    """One executed demo, with its transcript and the state it left behind."""

    def __init__(self, text: str, demo: Demo) -> None:
        self.text = text
        self.demo = demo
        self.verdicts = [m.groupdict() for m in VERDICT_LINE.finditer(text)]

    def line_containing(self, needle: str) -> str:
        for line in self.text.splitlines():
            if needle in line:
                return line
        raise AssertionError(f"the demo never printed a line containing {needle!r}")


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> DemoRun:
    var_dir = tmp_path_factory.mktemp("demo")
    demo = Demo(var_dir, REPETITIONS)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            asyncio.run(demo.run())
        yield DemoRun(buffer.getvalue(), demo)
    finally:
        demo.close()


class TestTheStoryItTells:
    """The four beats the whole demo exists to show."""

    def test_it_reaches_a_verdict_for_every_version_and_the_reaudit(
        self, run: DemoRun
    ) -> None:
        assert len(run.verdicts) == 5, (
            "expected four version verdicts plus the post-shift re-audit; "
            f"the transcript contains {len(run.verdicts)}"
        )

    @pytest.mark.parametrize(
        ("version", "status"),
        [
            ("1.0.0", "CONTRADICTED"),  # the explanation does not hold
            ("2.0.0", "SUPPORTED"),  # the same explanation, now true
            ("3.0.0", "INCONCLUSIVE"),  # genuinely ambiguous, not a failure
            ("4.0.0", "CONTRADICTED"),  # a regression the system catches unprompted
        ],
    )
    def test_the_lineage_summary_matches_the_story(
        self, run: DemoRun, version: str, status: str
    ) -> None:
        """The narrative's spine. If this breaks, the recorded demo is wrong."""
        assert re.search(rf"v{re.escape(version)}\s+\[.{{1,2}}\]\s+{status}\b", run.text), (
            f"the demo's lineage summary does not report v{version} as {status}"
        )

    def test_the_post_shift_reaudit_is_inconclusive_not_contradicted(
        self, run: DemoRun
    ) -> None:
        """The single most important honesty claim in the script.

        After the distribution shifts, the probe can no longer move the input far enough to
        test the claim. `INCONCLUSIVE` is the truthful answer; `CONTRADICTED` would be an
        invented finding, which is exactly the failure mode this project exists to prevent.
        """
        assert run.verdicts[-1]["status"] == "INCONCLUSIVE"
        assert float(run.verdicts[-1]["validity"]) < 0.9, (
            "the re-audit should fail the validity gate, which is *why* it is inconclusive"
        )
        assert "CONTRADICTED would be a fabrication" in run.text

    def test_the_verdict_mark_always_agrees_with_the_verdict(self, run: DemoRun) -> None:
        expected = {"SUPPORTED": "OK", "CONTRADICTED": "X", "INCONCLUSIVE": "?"}
        for verdict in run.verdicts:
            assert verdict["mark"] == expected[verdict["status"]]


class TestNothingIsHardcoded:
    """The docstring claims every printed number is computed during the run."""

    def test_printed_effect_sizes_are_the_ledger_s_effect_sizes(self, run: DemoRun) -> None:
        stored = {
            row.verdict_id: run.demo.ledger.get_verdict(row.verdict_id)
            for row in run.demo.runtime.list_investigations()
            if row.verdict_id
        }
        printed = {v["effect"] for v in run.verdicts}
        actual = {f"{v.effect_size:+.4f}" for v in stored.values() if v is not None}
        assert printed <= actual, (
            f"the transcript prints effect sizes not present in the ledger: {printed - actual}"
        )

    def test_the_verdict_statuses_printed_are_the_ones_recorded(self, run: DemoRun) -> None:
        recorded = {
            str(verdict.status)
            for row in run.demo.runtime.list_investigations()
            if row.verdict_id and (verdict := run.demo.ledger.get_verdict(row.verdict_id))
        }
        assert {v["status"] for v in run.verdicts} <= recorded


class TestTheEvidenceSurvives:
    def test_the_hash_chain_is_reported_intact(self, run: DemoRun) -> None:
        assert "Hash chain intact: True" in run.text
        assert run.demo.lineage.verify_chain(run.demo.family()) == []

    def test_integrity_checks_report_nothing_broken(self, run: DemoRun) -> None:
        assert "lineage_broken=[]" in run.text
        assert "verdicts_broken=[]" in run.text

    def test_point_in_time_reconstruction_answers_each_day_distinctly(
        self, run: DemoRun
    ) -> None:
        """Evidence expires rather than being overwritten, so each day has its own answer."""
        answers = re.findall(r"What did we believe on day\s+(\d+)\?\s+(.+)", run.text)
        assert len(answers) == 4
        assert len({a for _, a in answers}) == 4, (
            f"every checkpoint should reconstruct a different belief, got {answers}"
        )

    def test_old_evidence_is_expired_rather_than_deleted(self, run: DemoRun) -> None:
        assert "expired, not deleted" in run.text
        view = run.demo.lineage.view(run.demo.family())
        assert len(view.entries) >= 4, "the append-only ledger should retain every reading"


class TestResilience:
    def test_duplicate_deliveries_write_nothing(self, run: DemoRun) -> None:
        assert "new ledger rows: none" in run.text
        assert run.demo.worker.stats.duplicates_skipped == 3

    def test_a_crash_mid_experiment_does_not_duplicate_science(self, run: DemoRun) -> None:
        line = run.line_containing("evidence rows before")
        before, after = re.findall(r"before (\d+), after\s+(\d+)", line)[0]
        assert before == after, f"the resume produced new evidence rows: {before} -> {after}"

    def test_the_resume_starts_from_where_it_died(self, run: DemoRun) -> None:
        assert "resumed from EXPERIMENT_RUNNING" in run.text

    def test_the_bus_reports_no_failures_or_dead_letters(self, run: DemoRun) -> None:
        snapshot = run.demo.bus.snapshot()
        assert snapshot["failed"] == 0
        assert snapshot["dead_lettered"] == 0
        assert snapshot["duplicates_suppressed"] == 3


class TestGovernance:
    def test_the_governor_escalates_to_a_human(self, run: DemoRun) -> None:
        assert run.demo.runtime.pending_approvals(), (
            "the demo's claim that the Governor requires a human is unbacked"
        )
        assert re.search(r"approvals awaiting a human: [1-9]", run.text)

    def test_debt_is_computed_not_asserted(self, run: DemoRun) -> None:
        assert "No debt snapshot was recorded" not in run.text
        assert run.demo.ledger.latest_debt(MODEL_ID) is not None
        assert "not a scientific quantity" in run.text


class TestItRefusesToOversell:
    def test_the_synthetic_disclaimer_is_printed(self, run: DemoRun) -> None:
        assert "Not a medical device" in run.text or "synthetic" in run.text.lower()

    def test_the_closing_scope_statement_survives(self, run: DemoRun) -> None:
        assert "Not hidden causal truth" in run.text
        assert "Not a clinical system" in run.text

    def test_the_deterministic_boundary_is_stated(self, run: DemoRun) -> None:
        """The claim a judge is most likely to probe: what did the LLM actually decide?"""
        assert "Gemini proposed the test. Deterministic code decided the result." in run.text

    def test_it_publishes_the_formula_for_every_version(self, run: DemoRun) -> None:
        for version in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"):
            assert re.search(rf"v{re.escape(version)}: .*score", run.text)


class TestTheCommandLine:
    """`main()` is what CI and a rehearsing presenter actually invoke."""

    def test_it_runs_end_to_end_and_writes_its_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        var_dir = tmp_path / "cli"
        monkeypatch.setattr(
            sys, "argv",
            ["run_demo", "--var-dir", str(var_dir), "--repetitions", str(REPETITIONS)],
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            main()
        assert "ARIADNE" in out.getvalue()
        assert (var_dir / "demo.sqlite3").exists()

    def test_it_resets_previous_state_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rehearsal must start from the same baseline every time, or the recorded
        numbers drift from the ones a viewer is told to expect."""
        var_dir = tmp_path / "reset"
        var_dir.mkdir()
        stale = var_dir / "stale.txt"
        stale.write_text("left over from a previous run", encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv",
            ["run_demo", "--var-dir", str(var_dir), "--repetitions", str(REPETITIONS)],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            main()
        assert not stale.exists(), "a fresh run must not inherit previous demo state"

    def test_keep_preserves_previous_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        var_dir = tmp_path / "keep"
        var_dir.mkdir()
        stale = var_dir / "stale.txt"
        stale.write_text("deliberately retained", encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv",
            ["run_demo", "--var-dir", str(var_dir), "--repetitions", str(REPETITIONS), "--keep"],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            main()
        assert stale.read_text(encoding="utf-8") == "deliberately retained"


class TestReportingEdges:
    def test_a_failed_investigation_reports_its_error_rather_than_crashing(
        self, tmp_path: Path
    ) -> None:
        """`report()` must survive an investigation that never produced a verdict.

        Printing `state=FAILED (no verdict: ...)` is the useful outcome; a KeyError on the
        verdict mark would take down the demo at the exact moment it had something
        interesting to say.
        """
        var_dir = tmp_path / "edge"
        var_dir.mkdir()
        demo = Demo(var_dir, REPETITIONS)
        try:
            investigation = next(
                iter(demo.runtime.list_investigations()), None
            ) or asyncio.run(demo.deploy("1.0.0", BASELINE))
            broken = investigation.model_copy(
                update={"verdict_id": None, "last_error": "target model unreachable"}
            )
            with contextlib.redirect_stdout(io.StringIO()) as out:
                demo.report(broken)
            assert "no verdict: target model unreachable" in out.getvalue()
        finally:
            demo.close()

    def test_latest_is_none_before_anything_runs(self, tmp_path: Path) -> None:
        var_dir = tmp_path / "empty"
        var_dir.mkdir()
        demo = Demo(var_dir, REPETITIONS)
        try:
            assert demo.latest() is None
            assert demo.family() == ""
        finally:
            demo.close()

    def test_the_verdict_marks_cover_every_verdict_status(self) -> None:
        """A new verdict status would otherwise KeyError mid-demo."""
        assert set(run_demo.VERDICT_MARK) == {str(s) for s in VerdictStatus}
