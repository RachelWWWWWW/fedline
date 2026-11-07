"""Independent checks on a finished redline. Failures become blocking flags."""

from __future__ import annotations

import re

from .bill import Bill
from .engine import Flag, Outcome
from .statute import INS, Node


def _bare(text: str) -> str:
    return re.sub(r"[\s“”]+", "", text)


def _inserted(node: Node):
    for text in node.texts():
        for seg in text.segments:
            if seg.status == INS:
                yield seg


def verify(bill: Bill, outcome: Outcome) -> list[str]:
    """Check text fidelity and instruction accounting. Returns the checks that passed."""
    passed = []
    bill_text = _bare(bill.plain_text)

    faithful = True
    for doc in outcome.documents:
        if doc.basis == "new":
            continue
        if _bare(doc.root.before_text()) != _bare(doc.source_text):
            faithful = False
            outcome.flags.append(Flag(doc.title, "Verification failed: the original text reconstructed from "
                                                 "this redline doesn't match the source.", True, doc))
    if faithful:
        passed.append("Original text in the redline matches the source XML in every section.")

    sourced = True
    for doc in outcome.documents:
        for seg in _inserted(doc.root):
            if _bare(seg.text) not in bill_text:
                sourced = False
                outcome.flags.append(Flag(doc.title, f"Verification failed: inserted text “{seg.text}” isn't in "
                                                     "the bill.", True, doc))
    if sourced:
        passed.append("All inserted text appears verbatim in the bill.")

    expected = len(bill.instructions) + len(bill.problems)
    if outcome.total == expected:
        passed.append(f"All {expected} instructions are accounted for: {outcome.applied} applied, "
                      f"{outcome.failed} not applied and flagged.")
    else:
        outcome.flags.append(Flag("Verification", f"Verification failed: {expected} instructions were found, but "
                                                  f"{outcome.total} were processed."))
    return passed
