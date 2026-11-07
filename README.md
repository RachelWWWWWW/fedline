# fedline

Redlines the effect of a congressional bill on the underlying statutory text. See `design.md` for the design.

## Setup (PowerShell)

Requires Python 3.12 or later and a free [Congress.gov API key](https://api.congress.gov/sign-up/).

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
[Environment]::SetEnvironmentVariable('CONGRESS_API_KEY', '<your key>', 'User')
```

Open a new terminal after setting the key.

## Use

```powershell
.venv\Scripts\fedline https://www.congress.gov/bill/119th-congress/house-bill/10453/text
```

Pass the bill's page on Congress.gov, optionally with a version, such as `.../senate-bill/5653/text/is`. Links to the bill's XML file (`BILLS-119s5653is.xml`) aren't accepted; use the matching page link instead.

The document is written to `output\`, for example `output\HR10453_119_ih.docx`. Use `--out` to choose another folder.

The run ends with a count, such as `179 of 236 instructions applied; 58 not applied or unverified, 5 to review.` Instructions that weren't applied, or whose result failed a check, are flagged in the document, and those not applied are also listed at the end. Items to review were applied, with a note on something worth checking, such as an apparent drafting problem in the bill.

## Tests

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

## Current limits

- Instructions are parsed by pattern matching. Wording the parser doesn't recognize is flagged and listed, not applied. The LLM step for harder wording isn't built yet. Wording not yet recognized includes:
  - "in the first proviso"
  - indenting, and moving text. Nothing is shown in the "moved" format yet.
- When the Code's editors changed the Act's wording, for example printing "subchapter II of this chapter" for the Act's "title II of this Act", the bill's quoted text isn't found in the Code. The instruction is flagged, not applied.
- A law cited only by the Statutes at Large, with no Code citation, isn't found. Neither is a chapter's table of sections.
- The Code's editorial footnotes are left out of the statutory text. Bracketed editorial notes printed in the text, such as "[Amended this section.]", are kept.
- The 99.9% accuracy target hasn't been measured. That needs the test against enacted laws, which isn't built yet.
