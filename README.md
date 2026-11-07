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

## Using fedline

### 1. Get the bill's link

Find the bill on [Congress.gov](https://www.congress.gov) and open its **Text** tab. To redline a particular version, choose it from the version list; the link then ends with the version's code, such as `/text/is` for "Introduced in Senate". For example:

- `https://www.congress.gov/bill/119th-congress/senate-bill/5653/text/is`: S. 5653 as introduced
- `https://www.congress.gov/bill/119th-congress/house-bill/10453/text`: H.R. 10453, latest version

Without a version, fedline uses the latest text version Congress.gov has. Bills and joint, concurrent and simple resolutions from either chamber are accepted. Links to the bill's XML file (`BILLS-119s5653is.xml`) aren't; use the matching page link instead.

### 2. Run it

From the `fedline` folder, in PowerShell:

```powershell
.venv\Scripts\fedline https://www.congress.gov/bill/119th-congress/senate-bill/5653/text/is
```

fedline downloads the bill, then each title of the US Code the bill amends. Large titles, such as Title 42, take a while. The run ends with a count:

```
190 of 237 instructions applied; 48 not applied or unverified, 3 to review.
Wrote output\S5653_119_is.docx
```

### 3. Open the document

The redline is written to `output\`, named for the bill and version, such as `output\S5653_119_is.docx`. Running the same bill again replaces it. Use `--out` to write somewhere else:

```powershell
.venv\Scripts\fedline <link> --out C:\redlines
```

### 4. Read the redline

The document has four parts:

- **Cover page.** The bill, its link, the bill version, and the Code release point used as the baseline. Also the count, the key (added text is green and underlined, deleted text red and struck through, moved text purple and double-underlined), the verification checks that passed, and numbered lists of the flags under "Not applied or unverified" and "To review".
- **One part per amended provision**, in Code order: a Code section, an Act's text reproduced in a Code note, or a new section the bill adds, which has no Code number yet. Each change has a footnote citing the bill provision that made it. Flags appear in place as `[Flag n, § …: …]`.
- **Instructions not redlined.** Each instruction fedline couldn't apply, quoted from the bill, with the reason.
- **Appendix.** The bill's provisions that don't amend statutory text, such as its short title, definitions and authorizations of appropriations.

The redline shows only text the bill directly changes. It doesn't find other laws whose cross-references are affected.

### 5. Check the results

Start with the flags on the cover page:

- **Not applied or unverified:** the change isn't shown, or it's shown but failed a check. For those not applied, the bill's text is quoted under "Instructions not redlined"; apply them by hand. They include instructions whose wording fedline doesn't recognize, and text the Code words differently from the Act in a way fedline doesn't allow for.
- **To review:** the change was applied, but something is worth checking, such as an apparent drafting problem in the bill, or a heading struck because the new text has none.

When an instruction is ambiguous, such as a phrase that appears twice, fedline flags it rather than choosing.

If the run stops with an error:

- `CONGRESS_API_KEY isn't set`: set the key as described under Setup, then open a new terminal.
- `That doesn't look like a bill URL`: use the bill's page link, as in step 1.
- `Congress.gov has no bill …`: check the Congress, bill type and number in the link.
- `PermissionError` when writing the document: it's open in Word. Close it and run again.

## Tests

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

## Current limits

- Instructions are parsed by pattern matching. Wording the parser doesn't recognize is flagged and listed, not applied. The LLM step for harder wording isn't built yet. Wording not yet recognized includes:
  - "in the first proviso"
  - moving text, other than putting subdivisions in numerical order.
- Moved text is shown only where the bill moves it, in purple and double-underlined. Its old place isn't marked. When subdivisions are put in numerical order, only the fewest needed to explain the new order are marked as moved.
- An instruction that only changes indentation, such as "by indenting clauses (i) through (v) appropriately", is applied without marks, because the redline indents each provision by its level. It's flagged to review.
- When the Code's editors changed the Act's wording, fedline looks for the usual changes: Code section numbers for the Act's ("section 1532(13) of this title" for "section 3(13) of this Act"), "this chapter" for "this Act", "subchapter II of this chapter" for "title II of this Act", a date for "the date of enactment of this section", bracketed citations, and curly apostrophes. A match found that way is applied only if it's the only one, and it's flagged to review. The new text is the bill's, so it may use the Act's numbering inside the Code's text. Other changes, such as reworded sentences, aren't found, and the instruction is flagged, not applied.
- A law cited only by the Statutes at Large, with no Code citation, isn't found. Neither is a chapter's table of sections.
- The Code's editorial footnotes are left out of the statutory text. Bracketed editorial notes printed in the text, such as "[Amended this section.]", are kept.
- The 99.9% accuracy target hasn't been measured. That needs the test against enacted laws, which isn't built yet.
