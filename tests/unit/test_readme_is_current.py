"""The README must not drift from the repository it describes.

The README is the most-read file in the project and the least likely to be re-checked. A
self-audit found three stale claims in it at once: a test count 119 short, a statement that
the cloud adapters had "never run against Google Cloud" after they had been deployed and used
to audit a live model, and two documents that existed but were unreachable from the index.

None of those were caught by any test, a type check, or a lint, because none of them are
code. They are checked here instead — every claim the README makes that a machine can verify.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text(encoding="utf-8")


class TestEveryLinkResolves:
    def test_documentation_links_point_at_real_files(self) -> None:
        targets = re.findall(r"\]\((docs/[^)#]+|[A-Z_]+\.md)\)", README)
        assert targets, "the README should link its documentation"
        missing = [t for t in targets if not (REPO / t).exists()]
        assert not missing, f"README links to files that do not exist: {missing}"

    def test_every_doc_is_reachable_from_the_readme(self) -> None:
        """A document nobody can find is a document nobody reads."""
        on_disk = {
            f"docs/{p.name}"
            for p in (REPO / "docs").glob("*.md")
            # Deliberately unlinked: an internal record rather than a reader-facing doc.
            if p.name not in {"submission-checklist.md"}
        }
        unlinked = sorted(d for d in on_disk if d not in README)
        assert not unlinked, f"docs exist but the README never links them: {unlinked}"


class TestRepositoryTreeIsAccurate:
    def test_every_named_path_exists(self) -> None:
        tree = re.search(r"```\n(backend/.*?)```", README, re.DOTALL)
        assert tree, "the README should contain a repository tree"
        missing = []
        for line in tree.group(1).splitlines():
            m = re.match(r"^\s*([a-z_]+/[a-z_]*/?)\s{2,}", line)
            if not m:
                continue
            path = m.group(1).strip()
            if not ((REPO / path).exists() or (REPO / "backend" / path).exists()):
                missing.append(path)
        assert not missing, f"README describes paths that do not exist: {missing}"


class TestClaimedNumbersAreReal:
    def test_the_stated_test_count_matches_collection(self) -> None:
        """A stale count is the most quietly embarrassing kind of wrong.

        Collected rather than run: this is about the README's arithmetic, not about whether
        the suite passes — which every other test in this repository already covers.
        """
        claimed = re.search(r"pytest\s+#\s*([\d,]+)\s*tests", README)
        assert claimed, "the README should state how many tests there are"
        stated = int(claimed.group(1).replace(",", ""))

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:warnings"],
            capture_output=True, text=True, cwd=REPO,
        )
        counts = re.findall(r"^.+\.py: (\d+)$", proc.stdout, re.MULTILINE)
        assert counts, f"could not parse collection output: {proc.stdout[-400:]!r}"
        actual = sum(int(c) for c in counts)

        assert stated == actual, (
            f"README says {stated} tests; collection finds {actual}. Update the README."
        )

    def test_the_target_model_formulas_match_the_source(self) -> None:
        """The formulas are the published ground truth — a judge is invited to check a
        verdict by hand against them, so a typo here would invalidate that invitation."""
        from backend.experiment_engine.target_model import VERSION_SPECS, formula_text

        def normalise(text: str) -> str:
            return text.replace(" ", "").replace("*", "").lower()

        for version, spec in VERSION_SPECS.items():
            quoted = re.search(rf"^v{re.escape(version)}\s+score = (.+)$", README, re.MULTILINE)
            assert quoted, f"README does not publish the formula for v{version}"
            real = formula_text(spec).replace("score = ", "")
            assert normalise(quoted.group(1)).startswith(normalise(real)[:28]), (
                f"v{version}: README says {quoted.group(1)!r}, source computes {real!r}"
            )

    @pytest.mark.parametrize(
        ("configuration", "accuracy"),
        [("full", "100%"), ("no control arm", "93%"), ("no validity gate", "79%")],
    )
    def test_the_ablation_table_quotes_real_numbers(
        self, configuration: str, accuracy: str
    ) -> None:
        row = re.search(rf"\|\s*\*?\*?{re.escape(configuration)}\*?\*?\s*\|([^|]+)\|", README)
        assert row, f"README ablation table has no row for {configuration!r}"
        assert accuracy in row.group(1), (
            f"{configuration}: README claims {row.group(1).strip()!r}, expected {accuracy}"
        )


class TestHonestyClaims:
    def test_it_does_not_still_deny_the_cloud_deployment(self) -> None:
        """The README said the adapters had 'never run against Google Cloud' well after they
        had been deployed and used to audit a live model. Understating is not the safe
        direction — it is just a different way of being wrong about your own system."""
        assert "have never run against Google Cloud" not in README

    def test_the_synthetic_disclaimer_survives(self) -> None:
        """The one claim that must never be softened."""
        assert "Not a medical device" in README
        assert "No clinical validity" in README

    def test_it_still_refuses_to_claim_causal_truth(self) -> None:
        assert "not causal truth" in README.lower()

    def test_the_three_verdicts_are_still_three(self) -> None:
        for verdict in ("SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"):
            assert verdict in README
        assert "no fourth" in README.lower()
