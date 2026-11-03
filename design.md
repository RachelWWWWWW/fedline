# Goal
Take as input the language of a congressional bill and output a redlined version of effects on the underlying statutory language.

# Why
The format of congressional bills show what changes, but they don't show the changes in context, meaning that they don't provide -- and even actively obfuscate -- the effect of the changes. This problem is well known to lawyers, who insist on "redlines" of edits, which show -- in line and in context -- what changed. We want a tool that does the same thing, but instead of contracts, we want to see redlines of laws.

# What and How
- Bills are identified by a URL with a standardized format: E.g. https://www.congress.gov/bill/119th-congress/house-bill/10453/text
- Get bill text as XML through the Congress.gov API or the direct bill XML links on congress.gov or GovInfo: E.g. https://www.govinfo.gov/content/pkg/BILLS-119hr10453ih/xml/BILLS-119hr10453ih.xml
  - Do not scrape the congress.gov bill page. It returned a 403 to a scripted request. The API (requires a key) and direct XML links both worked, so Playwright is not needed for these sources.
- Get the US Code as XML from the House release points: https://uscode.house.gov/download.html

# Design

## Interface
- Minimalist is fine and desired for the MVP. As simple as a terminal command with arguments. Ideally it could be as direct as e.g.
% fedline https://www.congress.gov/bill/119th-congress/house-bill/10453/text
- Remember this has to work natively on windows/powershell. Minimum overhead; no server.
- The first version is for a single user (the author), who runs it and checks the output.

## Function
Given a URL for a Bill:
0. Validate that the URL is accessible. Use the bill version named in the URL; otherwise use the latest version.
1. Step through each section of the bill and identify each amending instruction and its target. Where a bill amends an Act by its own section numbers (e.g. "section 2" of an Act), map the target to the US Code section (e.g. 47 U.S.C. § 1601).
2. Convert each instruction into a structured edit. Code handles common patterns and the LLM handles harder wording. The LLM only parses; it never writes statutory text.
3. Apply the edits in code, in bill order, to the US Code text. Later instructions must see the results of earlier ones (e.g. "as so redesignated", "as amended by the preceding provisions of this Act").
4. Generate the redline from the edits, showing the net result rather than each intermediate step. The text tracks each edit as it's applied instead of running a generic comparison afterward, because that keeps every change tied to its bill citation and can't misalign. The original and amended text can both be read back from the result.
5. Verify the result (see Accuracy and Verification).
6. Copy each affected statutory section into the deliverable doc, prefaced by the name of the bill and hyperlinked URL of the source.
7. For each redline, add a footnote citing to the relevant section number and subsection of the bill (e.g. § 2(a)(1)). Where several bill sections contribute to one net change, cite all of them. When a bill section results in multiple edits throughout a statute (e.g. a change of a word throughout), footnote each instance of the edit, but refer to the first footnote (e.g. "See supra note 4").
8. Emit the deliverable document into a folder on the local machine.

## Baseline
- The US Code is the baseline, including where a bill amends an Act rather than the Code directly. Note that this isn't always the literal text being amended: e.g. Title 47 isn't positive law, and its wording can differ from the Act's (e.g. "this chapter" where an Act would say "this Act").
- Map Act sections to Code sections using each Code section's source credit (e.g. "Pub. L. 116-124, § 2" on 47 U.S.C. § 1601).
- Where an instruction's target wording doesn't match the Code, flag it.
- "Each place it appears" replacements match whole phrases, with exact capitalization, and skip headings. Acts print headings in capitals and bills amend them separately, but the Code prints them in sentence case. Flag any heading or near match left unchanged.
- Print the Code release point on each document (e.g. "current through P.L. 119-136").

## Special cases
- Amendments to text outside the Code's main text (e.g. an Act printed as a Code note, or a provision of a public law): redline the text from the Code notes when it's there and label that redline as based on a note, since notes may be incomplete or out of date. Otherwise, list the instruction with its bill citation in a clearly marked section.
- New provisions with no Code number yet (e.g. a new section of an Act): show as added text labeled with the Act section number. Don't invent a Code citation.
- Drafting problems in the bill (e.g. two different provisions that each add a subsection (f)): apply the text exactly as written and flag the problem. Don't correct it.
- Provisions that don't amend anything (e.g. rules of construction, standalone mandates, effective dates): reproduce verbatim in an appendix and mention them in the opening summary.

## Deliverable format:
- A .docx with fixed formatting, for reading and printing. Not Word tracked changes, since Word's settings control their colors.
- Filename reflects the bill type and number, Congress, and bill version code. E.g. HR10453_119_ih.docx. Bill numbers run per Congress, not per year, and bills already have version codes (e.g. ih for introduced in House).
- The document opens with a summary, which lists every exception and mentions any appendix material.
- Document each affected statutory section as its own set of pages (section break ends page)
- Reproduce the whole Code section by default. The relevant text of the statutory section is reproduced faithfully and then edited
- Each edit is formatted in accordance with the following redlining format conventions: green and underlined for added, red and struckthrough for deleted, purple and doubleunderlined for moved
- Purple doubleunderline is only for text that actually moves to a new location. When a provision is only renumbered (e.g. paragraph (5) becomes paragraph (4)), change only the number, in red and green.
- State the scope limit on the document: the tool doesn't detect other laws whose cross-references are affected by the amendments, so a clean redline isn't the bill's full effect.

## Accuracy and Verification
- The standard is no silent errors: every instruction is either applied and verified or visibly flagged, and all other text matches the source exactly.
- Target: 99.9% of instructions correct or flagged. A flagged instruction is not an error, so the tool has no incentive to guess.
- Test against enacted laws: applying a public law to the Code release point before it should reproduce the release point after it, at least where the codifiers didn't adjust the wording.
- When an instruction can't be applied with confidence, or the bill looks internally inconsistent, always produce the document anyway: leave that text unchanged, flag the problem where it occurs, and list it up front.

## Test case
H.R. 10453 (119th Congress) is the first test case. It covers:
- Stacked edits: § 2(a)(5) adds a paragraph (7) to Act § 9, and § 3(b)(2)(B) then renumbers it as paragraph (6).
- Global replacements with an exception: § 2(a)(1)-(3), "each place it appears (except in section 4)". Find-and-replace isn't enough: "communications equipment or service" also matches inside "telecommunications equipment or services" and inside the plural that § 2(a)(2) amends separately.
- Targets outside the Code's main text: the Secure Equipment Act of 2021 (47 U.S.C. 1601 note) and § 1709 of P.L. 118-159.
- A new section with no Code number: new Act § 10, added by § 8.
- An apparent drafting error: § 5 adds subsections (e) and (f) to Act § 2, and § 6 adds another subsection (f).
- Non-amending text: the rule of construction in § 3(c).

# Assumptions

## Tech Stack
- Python (only because I'm most familiar with it and it has lots of libraries)
- Need an LLM at runtime for parsing harder instructions (Probably Claude to start, but bring our own key later)
- Needs a Congress.gov API key and an Anthropic key

## Constraints
- Must work on Windows or be system agnostic
- Any agentic access of computing resources must not cause disruption (ratelimits, backoff, lightweight protocols)

# Open Questions
- How should floor and committee amendments be handled? The filename convention covers bill versions, but amendments offered to a bill aren't bill versions.

# Next phase
- Save a snapshot on each run so a redline can be reproduced later (e.g. source files, parsed edits, retrieval times, hashes).

# Future work
- Mac or multisystem support
- Web or intra-net visible interface for making available to other users. Hosted on GitHub or similar lightweight hosting is fine.
- Automatically start a new folder for each bill, store the deliverable into the correct folder.
- One click re-run when a bill is updated.
- Support for other government entities: States legislatures, Federal agencies (e.g. FCC and support for CFR edits)
- Redlines of CFR texts would be great such as proposed/final rule appendices in FCC documents and amendatory instructions in Federal Register documents
- User-configurable excerpt length for very long statutory sections
- Detect other laws whose cross-references are affected by the amendments
