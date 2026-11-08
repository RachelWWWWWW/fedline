"""Independent checks on a finished redline. Failures become blocking flags."""

from __future__ import annotations

import re

from .bill import Bill
from .engine import Flag, Outcome
from .statute import INS, Node


def _bare(text: str) -> str:
    return re.sub(r"[\s“”]+", "", text)


def _inserted(node: Node, skip=()):
    """Inserted segments, leaving out the headings of the nodes in skip."""
    texts = [node.body, node.tail] + ([] if id(node) in skip else [node.heading])
    for text in texts:
        for seg in text.segments:
            if seg.status == INS:
                yield seg
    for child in node.children:
        yield from _inserted(child, skip)


def _defines(node: Node, heading: str) -> bool:
    """Whether a heading taken from a provision's text is the term, or terms, the provision quotes."""
    terms = re.findall(r"“([^“”]+)”", node.body.current())
    return heading.casefold() in {t.casefold() for t in terms} or heading.casefold() in {
        "; ".join(terms[:n]).casefold() for n in range(2, len(terms) + 1)}


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
    # A heading the bill describes rather than quotes, as "the term defined in that paragraph", is taken from
    # the provision's text, so it's checked against that text instead of the bill's.
    described = {id(node): (node, heading) for node, heading in outcome.term_headings
                 if node.heading.current() == heading}
    for node, heading in described.values():
        if not _defines(node, heading):
            sourced = False
            outcome.flags.append(Flag("Verification", f"Verification failed: the heading “{heading}” isn't a term "
                                                      "its provision defines.", True, None, node))
    for doc in outcome.documents:
        for seg in _inserted(doc.root, described):
            if _bare(seg.text) not in bill_text:
                sourced = False
                outcome.flags.append(Flag(doc.title, f"Verification failed: inserted text “{seg.text}” isn't in "
                                                     "the bill.", True, doc))
    if sourced:
        passed.append("All inserted text appears verbatim in the bill." if not described else
                      "All inserted text appears verbatim in the bill, except headings the bill describes "
                      "rather than quotes, which match the terms their provisions define.")

    expected = len(bill.instructions) + len(bill.problems)
    if outcome.total == expected:
        passed.append(f"All {expected} instructions are accounted for: {outcome.applied} applied, "
                      f"{outcome.failed} not applied and flagged.")
    else:
        outcome.flags.append(Flag("Verification", f"Verification failed: {expected} instructions were found, but "
                                                  f"{outcome.total} were processed."))
    return passed
