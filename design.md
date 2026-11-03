# Goal
Take as input the language of a congressional bill and output a redlined version of effects on the underlying statutory language.

# Why
The format of congressional bills show what changes, but they don't show the changes in context, meaning that they don't provide -- and even actively obfuscate -- the effect of the changes. This problem is well known to lawyers, who insist on "redlines" of edits, which show -- in line and in context -- what changed. We want a tool that does the same thing, but instead of contracts, we want to see redlines of laws.

# What and How
- Bills are presented in XML, TXT, PDF, at a URL with a standardized format: E.g. https://www.congress.gov/bill/119th-congress/house-bill/10453/text
- US Code is presented in standardized format, typically HTML: https://uscode.house.gov/browse.html or https://www.govinfo.gov/app/collection/uscode (choose whichever of these or another source is most authoritative)

# Design

## Interface
- Minimalist is fine and desired for the MVP. As simple as a terminal command with arguments. Ideally it could be as direct as e.g.
% fedline https://www.congress.gov/bill/119th-congress/house-bill/10453/text
 - But if we need to start the program first before accepting arguments (making it a two step command), that's fine.
Okay if it runs in a browser window. Remember this has to work natively on windows/powershell. Minimum overhead; we don't want to set up a server and hit localhost first if we don't have to.

## Function
Given a URL for a Bill:
0. Validate that the URL is accessible
1. Step through each section of the bill, finding the authoritative source for the full text of each cited statutory section
2. Copy the relevant portion of the statutory text into the deliverable doc (see open question below regarding “relevant portion”), prefaced by the name of the bill and hyperlinked URL of the source
3. Apply the transformations as noted in the bill (this likely requires LLM to parse), using redline notation
4. For each redline, add a footnote citing to the relevant section number and subsection of the bill (e.g. § 2(a)(1)). When a bill section results in multiple edits throughout a statute (e.g. a change of a word throughout), footnote each instance of the edit, but refer to the first footnote (e.g. “See supra note 4”).
5. Emit the deliverable document into a folder on the local machine.

## Deliverable format:
- Filename reflects the bill number, year, and version number. E.g. HR10453_2026_1 (unclear how amendments are serialized; propose a format).
- Document each affected statutory section as its own set of pages (section break ends page)
- The relevant text of the statutory section is reproduced faithfully and then edited
- Each edit is formatted in accordance with the following redlining format conventions: green and underlined for added, red and struckthrough for deleted, purple and doubleunderlined for moved)
- Undecided on whether the document should be .docx, .html, more than one of these, or something else. Probably a .docx to start off with. 

# Assumptions

## Tech Stack
- Python? (only because I'm most familiar with it and it has lots of libraries)
- Need an LLM at runtime for the text evaluation and generation (Probably Claude to start, but bring our own key later)

## Constraints
- Must work on Windows or be system agnostic
- Any agentic access of computing resources must not cause disruption (ratelimits, backoff, lightweight protocols)
- Needs to be highly accurate (99.9%); incorporate validation step to avoid hallucination and spot type 1 and type 2 errors.

# Open Questions
Will the various sources of truth for the bills and laws allow browsing by an AI agent? If not, what’s an easy and non-invasive solve for this? Is Playwright a good choice?
Do we want to pull down a snapshot as an immutable, local source of truth each time we do a redline?
How much of a statute is “the relevant section”? Statutes can be massive; should this be user configurable, with a default being the first through last sections that have changes? 
Version comparison seems non-trivival and easy to mess up the presentation of. What’s the best way to ensure accuracy and take advantage of existing work/best practices? Is it better to do the changes as a one step process, generating the redline directly on the existing statutory/regulatory text, or is it better to transform first and then compare a before and after? 


# Future work
- Mac or multisystem support
- Web or intra-net visible interface for making available to other users. Hosted on GitHub or similar lightweight hosting is fine.
- Automatically start a new folder for each bill, store the deliverable into the correct folder.
- One click re-run when a bill is updated.
- Support for other government entities: States legislatures, Federal agencies (e.g. FCC and support for CFR edits)
- Redlines of CFR texts would be great such as proposed/final rule appendices in FCC documents and amendatory instructions in Federal Register documents

