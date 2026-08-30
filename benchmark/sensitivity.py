"""How much does the benchmark's result depend on the exact thresholds chosen?

The honest problem this answers: Ariadne's thresholds - a 0.10 minimum effect, 0.80
reproducibility, 0.90 validity - were **not pre-registered**. The repository was built in a
single pass, so there is no commit history that could establish they were fixed before any
result was seen, and no reviewer should take a claim that they were on trust.

The remedy for an un-pre-registered threshold is not an assurance. It is a sensitivity
analysis: show the result as a function of the parameter, so a reader can see for themselves
whether the conclusion balances on the specific value or sits on a plateau.

    python -m benchmark.sensitivity

Two sweeps, because they answer different questions:

  `uniform`   forces every case onto one threshold, overriding the per-case overrides that
              exist to construct particular experimental conditions. This is the strict
              reading - what the suite scores with a single global parameter.
  `as-designed` keeps each case's own overrides and moves only the default.

The uniform sweep is the one that matters for the tuning question, and it is reported first
even though it scores lower, because a sensitivity analysis that quietly picks the flattering
variant is not a sensitivity analysis.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
from pathlib import Path
from typing import Any

from benchmark.cases import CASES
from benchmark.run_benchmark import run_case

THRESHOLDS: tuple[float, ...] = (0.04, 0.06, 0.08, 0.09, 0.10, 0.11, 0.12, 0.15, 0.20, 0.25)
REPRODUCIBILITY: tuple[float, ...] = (0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 1.00)


def _score(overrides: dict[str, Any], *, uniform: bool, configuration: str = "full") -> int:
    """Correct cases under a parameter setting. Errors count as incorrect, never skipped."""
    correct = 0
    for case in CASES:
        if uniform:
            # Strip the case's own value for any swept key, so the sweep really is uniform
            # rather than silently honouring a per-case exception.
            merged = {k: v for k, v in case.plan_overrides.items() if k not in overrides}
            merged.update(overrides)
        else:
            # As designed: a case that declares its own threshold keeps it. Those overrides
            # construct specific experimental conditions rather than rescue results, so the
            # sweep moves the default underneath them.
            merged = {**overrides, **case.plan_overrides}
        patched = dataclasses.replace(case, plan_overrides=merged)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_case(patched, configuration)
            correct += bool(result.correct)
        except Exception:  # noqa: BLE001 - an erroring case is a failing case here
            pass
    return correct


def sweep(parameter: str, values: tuple[float, ...], *, uniform: bool) -> dict[str, int]:
    return {f"{value:g}": _score({parameter: value}, uniform=uniform) for value in values}


def run() -> dict[str, Any]:
    return {
        "cases": len(CASES),
        "min_effect_threshold": {
            "uniform": sweep("min_effect_threshold", THRESHOLDS, uniform=True),
            "as_designed": sweep("min_effect_threshold", THRESHOLDS, uniform=False),
        },
        "reproducibility_threshold": {
            "uniform": sweep("reproducibility_threshold", REPRODUCIBILITY, uniform=True),
        },
    }


def render(report: dict[str, Any]) -> str:
    n = report["cases"]
    out = [
        "# Threshold sensitivity",
        "",
        "Ariadne's thresholds were **not pre-registered** - see `PREREGISTRATION.md`. This is",
        "the evidence offered instead: the result as a function of the parameter.",
        "",
        f"Correct verdicts out of {n}, configuration `full`.",
        "",
        "## Minimum effect threshold",
        "",
        "| threshold | uniform (per-case overrides stripped) | as designed |",
        "|---|---|---|",
    ]
    uni = report["min_effect_threshold"]["uniform"]
    des = report["min_effect_threshold"]["as_designed"]
    for key in uni:
        mark = " **<- default**" if key == "0.1" else ""
        out.append(f"| {key}{mark} | {uni[key]}/{n} | {des[key]}/{n} |")
    out += [
        "",
        "## Reproducibility threshold",
        "",
        "| threshold | uniform |",
        "|---|---|",
    ]
    for key, value in report["reproducibility_threshold"]["uniform"].items():
        mark = " **<- default**" if key == "0.8" else ""
        out.append(f"| {key}{mark} | {value}/{n} |")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="directory for sensitivity.json/.md")
    args = parser.parse_args()

    report = run()
    text = render(report)
    print(text)
    if args.out:
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sensitivity.json").write_text(json.dumps(report, indent=2), "utf-8")
        (directory / "sensitivity.md").write_text(text, "utf-8")


if __name__ == "__main__":
    main()
