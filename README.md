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

The document is written to `output\`, for example `output\HR10453_119_ih.docx`. Use `--out` to choose another folder.

## Tests

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

## Current limits

- Instructions are parsed by pattern matching. Wording the parser doesn't recognize is flagged and listed, not applied. The LLM step for harder wording isn't built yet.
- Instructions that move text aren't recognized yet, so nothing is shown in the "moved" format.
- The 99.9% accuracy target hasn't been measured. That needs the test against enacted laws, which isn't built yet.
