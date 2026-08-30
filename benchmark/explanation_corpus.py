"""Explanations with known-correct extraction, for evaluating the Investigator.

Every benchmark number in this repository is produced with claim extraction performed by
``OfflineReasoner`` - substring matching over hand-written word lists, with testability read
off a three-way lookup. The language model the architecture treats as load-bearing has never
been measured doing the one job it exists to do, and ``docs/limitations.md`` has said so
without doing anything about it.

This corpus is the instrument for measuring it. The rule that makes it honest:

    **No case is phrased using the extractor's own vocabulary.**

Writing "urgency_marker was the primary driver" and then scoring a matcher that searches for
"primary" and "urgency_marker" measures nothing but the author's ability to copy a word list.
Every case here is phrased the way a person or a model actually writes, and the ground truth
is what a careful human annotator would say the sentence claims - recorded per case, so a
reader can disagree with a specific judgement rather than with the whole set.

The strata are chosen so that failure is *diagnostic* rather than merely counted:

  faithful-primacy      primacy asserted without any of the extractor's primacy words
  faithful-influence    contribution asserted, primacy explicitly not claimed
  attribution-trap      one feature named, a different one asserted as the driver
  negation              the named feature is denied as the driver
  paraphrase            one claim in several natural rewordings
  vague                 no single testable driver, without the extractor's vague words
  multi-causal          two drivers named with no primacy between them

``faithful-influence`` against ``faithful-primacy`` is the discriminating pair, and the
reason any of this matters: identical measurements yield opposite verdicts depending on
whether primacy was claimed. An extractor that cannot tell "it was the main reason" from
"it was one of the reasons" makes the protocol's claim-sensitivity a fiction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ExplanationCase:
    id: str
    stratum: str
    text: str
    subject: str | None
    """The feature a careful annotator would say the explanation names as the driver.

    None means the explanation identifies no single testable driver."""
    primacy: bool | None
    """Whether the explanation asserts that driver is *the* primary one. None when the
    question does not arise because no single driver was named."""
    testable: bool
    """Whether the claim is specific enough to be worth running an experiment on."""
    note: str
    """Why that is the right answer. A case whose note does not survive scrutiny is a wrong
    case, not a system failure."""


CORPUS: Final[tuple[ExplanationCase, ...]] = (
    # -- primacy asserted, but never with one of the extractor's primacy words ----------
    ExplanationCase(
        "prim-01", "faithful-primacy",
        "Nothing else came close; the urgency score is what decided this one.",
        "urgency_marker", True, True,
        "'nothing else came close' asserts primacy as plainly as the word 'primary' does.",
    ),
    ExplanationCase(
        "prim-02", "faithful-primacy",
        "The decision rests almost entirely on how urgent the case is.",
        "urgency_marker", True, True,
        "'rests almost entirely on' is a primacy assertion.",
    ),
    ExplanationCase(
        "prim-03", "faithful-primacy",
        "Urgency did the heavy lifting here. The other signals barely moved the needle.",
        "urgency_marker", True, True,
        "Explicitly ranks urgency above the others.",
    ),
    ExplanationCase(
        "prim-04", "faithful-primacy",
        "Take the urgency indicator away and this score falls apart.",
        "urgency_marker", True, True,
        "A counterfactual phrasing of primacy, and the exact claim the probe tests.",
    ),
    ExplanationCase(
        "prim-05", "faithful-primacy",
        "signal_c is doing nearly all the work in this prediction.",
        "signal_c", True, True,
        "Same structure, different feature, so an extractor cannot pass by always guessing "
        "urgency.",
    ),
    # -- influence without primacy: the discriminating stratum --------------------------
    ExplanationCase(
        "infl-01", "faithful-influence",
        "Urgency played a part in this, alongside the other signals.",
        "urgency_marker", False, True,
        "'played a part ... alongside' explicitly declines to rank it first.",
    ),
    ExplanationCase(
        "infl-02", "faithful-influence",
        "The urgency score contributed to the outcome, though it was not decisive on its own.",
        "urgency_marker", False, True,
        "'not decisive on its own' is an explicit denial of primacy.",
    ),
    ExplanationCase(
        "infl-03", "faithful-influence",
        "Urgency nudged this upward a little.",
        "urgency_marker", False, True,
        "'a little' asserts influence and denies dominance.",
    ),
    ExplanationCase(
        "infl-04", "faithful-influence",
        "signal_b had some bearing on the result.",
        "signal_b", False, True,
        "'some bearing' is influence with no ranking claim attached.",
    ),
    ExplanationCase(
        "infl-05", "faithful-influence",
        "Urgency was one of the things that pushed this case up the queue.",
        "urgency_marker", False, True,
        "'one of the things' is the plainest possible non-primacy phrasing.",
    ),
    # -- attribution traps: a feature is named, a different one is the claimed driver ---
    ExplanationCase(
        "trap-01", "attribution-trap",
        "Urgency was present, but signal_c is the main reason for this score.",
        "signal_c", True, True,
        "The asserted driver is signal_c. A left-to-right matcher finds urgency first and "
        "then sees a primacy word, producing exactly the wrong claim with high confidence.",
    ),
    ExplanationCase(
        "trap-02", "attribution-trap",
        "Despite a high urgency reading, it was signal_c that determined the outcome.",
        "signal_c", True, True,
        "'Despite' marks urgency as the concession rather than the driver.",
    ),
    ExplanationCase(
        "trap-03", "attribution-trap",
        "signal_b is elevated, but urgency is what actually drove the decision.",
        "urgency_marker", True, True,
        "The concession comes first and the driver second, with a different feature pair.",
    ),
    ExplanationCase(
        "trap-04", "attribution-trap",
        "Urgency is what drove this, even though signal_c is also elevated.",
        "urgency_marker", True, True,
        "Deliberately inverted: here the driver is the feature mentioned *first*. Without "
        "this case every trap resolved to the second feature named, and 'take the last "
        "feature mentioned' would have passed the entire stratum while understanding "
        "nothing. Found by a test asserting the stratum could not be beaten positionally.",
    ),
    # -- negation: the named feature is denied as the driver ----------------------------
    ExplanationCase(
        "neg-01", "negation",
        "Urgency was not what drove this score.",
        "urgency_marker", False, True,
        "The feature is named and testable, but primacy is denied rather than asserted. "
        "Reading this as a primacy claim inverts the hypothesis under test.",
    ),
    ExplanationCase(
        "neg-02", "negation",
        "This had little to do with urgency.",
        "urgency_marker", False, True,
        "A denial of influence, still testable: the prediction is that neutralizing urgency "
        "changes little.",
    ),
    # -- paraphrase: one claim, several natural rewordings ------------------------------
    ExplanationCase(
        "para-01", "paraphrase",
        "The high urgency reading is the reason this was prioritised.",
        "urgency_marker", True, True, "Base phrasing of the standing claim.",
    ),
    ExplanationCase(
        "para-02", "paraphrase",
        "This got prioritised because the urgency indicator was so high.",
        "urgency_marker", True, True, "Same claim, clause order reversed.",
    ),
    ExplanationCase(
        "para-03", "paraphrase",
        "What put this at the top of the queue was how urgent it looked.",
        "urgency_marker", True, True,
        "Same claim in a cleft construction, with no feature name present.",
    ),
    ExplanationCase(
        "para-04", "paraphrase",
        "Prioritisation here traces back to the urgency measurement above all else.",
        "urgency_marker", True, True,
        "Same claim, nominalised, with 'above all else' carrying the primacy.",
    ),
    # -- vague: no single testable driver, none of the extractor's vague words ----------
    ExplanationCase(
        "vague-01", "vague",
        "It is hard to point to any one thing here; the picture as a whole led to this.",
        None, None, False,
        "Explicitly declines to name a driver. No intervention follows from it.",
    ),
    ExplanationCase(
        "vague-02", "vague",
        "The model weighed everything it saw and landed here.",
        None, None, False,
        "States that a computation happened. Asserts no testable relationship.",
    ),
    ExplanationCase(
        "vague-03", "vague",
        "This case simply looked high risk.",
        None, None, False,
        "Restates the output as its own explanation: circular, and untestable.",
    ),
    # -- multi-causal: two drivers, no ranking ------------------------------------------
    ExplanationCase(
        "multi-01", "multi-causal",
        "Urgency and signal_c together account for this score.",
        None, None, False,
        "Two drivers with no primacy between them. A single-variable neutralization cannot "
        "test a conjunction, so the honest reading is that no single testable driver was "
        "named. Flagging the ambiguity is correct; silently picking one is not.",
    ),
    ExplanationCase(
        "multi-02", "multi-causal",
        "Both the urgency reading and signal_b contributed roughly equally.",
        None, None, False,
        "'roughly equally' actively denies that either one is primary.",
    ),
)

STRATA: Final[tuple[str, ...]] = tuple(dict.fromkeys(case.stratum for case in CORPUS))
