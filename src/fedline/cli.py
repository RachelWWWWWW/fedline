"""Command line: fedline <bill url>"""

from __future__ import annotations

import argparse
import os
import sys

from .bill import Bill, parse_bill
from .docx_writer import write_docx
from .engine import Outcome, apply_bill
from .sources import SourceError, fetch_bill_xml, fetch_code_title, parse_bill_url
from .statute import CodeTitle
from .verify import verify


def redline(bill_xml: bytes, titles: dict[int, CodeTitle]) -> tuple[Bill, Outcome, list[str]]:
    bill = parse_bill(bill_xml)
    outcome = apply_bill(bill, titles)
    passed = verify(bill, outcome)
    return bill, outcome, passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fedline", description="Redline a bill's effect on statutory text.")
    parser.add_argument("url", help="Congress.gov bill URL")
    parser.add_argument("--out", default="output", help="folder for the document (default: output)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        ref = parse_bill_url(args.url)
        print("Fetching bill text...")
        bill_xml, version = fetch_bill_xml(ref)
        wanted = parse_bill(bill_xml).titles()
        titles = {}
        for number in wanted:
            print(f"Fetching Title {number} of the US Code...")
            titles[number] = CodeTitle(fetch_code_title(number))
    except SourceError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    bill, outcome, passed = redline(bill_xml, titles)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{ref.filename}_{version}.docx")
    write_docx(path, bill, outcome, args.url, version, passed)
    blocking = sum(f.blocking for f in outcome.flags)
    print(f"{outcome.applied} of {outcome.total} instructions applied; "
          f"{blocking} not applied or unverified, {len(outcome.flags) - blocking} to review.")
    print(f"Wrote {path}")
    return 0
