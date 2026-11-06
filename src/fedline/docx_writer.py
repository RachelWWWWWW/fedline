"""Write the redline as a .docx with fixed formatting and real footnotes.

The file is built directly as Office Open XML, because the formatting is simple
and the common Python Word libraries don't support footnotes.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from .bill import Bill
from .engine import Flag, Outcome
from .statute import DEL, INS, ORIG, Document, Node, Text

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
GREEN, RED = "00803C", "C00000"
_INDENT = {"section": 0, "subsection": 0, "paragraph": 1, "subparagraph": 2,
           "clause": 3, "subclause": 4, "item": 5, "subitem": 6}
SCOPE_NOTE = ("This redline shows only the text that the bill directly changes. It doesn't detect other laws "
              "whose cross-references are affected, so a clean redline isn't the bill's full effect.")


def _run(text: str, props: str = "") -> str:
    if not text:
        return ""
    pieces = text.split("\n")
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{escape(p)}</w:t>' for p in pieces)
    return f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}{body}</w:r>"


def _status_props(status: str) -> str:
    if status == INS:
        return f'<w:color w:val="{GREEN}"/><w:u w:val="single"/>'
    if status == DEL:
        return f'<w:strike/><w:color w:val="{RED}"/>'
    return ""


def _para(content: str, style: str = "", indent: int = 0, keep: bool = False) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if keep:
        props += "<w:keepNext/>"
    if indent:
        props += f'<w:ind w:left="{indent * 360}"/>'
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{content}</w:p>"


@dataclass
class _Piece:
    text: str
    status: str
    sources: tuple[str, ...] = ()
    bold: bool = False


@dataclass
class _Line:
    """A paragraph of redlined statutory text, or a flag callout when flag is set."""
    pieces: list[_Piece] = field(default_factory=list)
    indent: int = 0
    flag: Flag | None = None


class Writer:
    def __init__(self, bill: Bill, outcome: Outcome, source_url: str, version: str):
        self.bill, self.outcome, self.source_url, self.version = bill, outcome, source_url, version
        self.footnotes: list[str] = []
        self.first_note: dict[tuple[str, ...], int] = {}
        self.links: list[str] = []

    def _link(self, url: str, text: str) -> str:
        if url not in self.links:
            self.links.append(url)
        rid = f"rIdLink{self.links.index(url)}"
        return f'<w:hyperlink r:id="{rid}"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>' \
               f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:hyperlink>'

    def _footnote(self, sources: tuple[str, ...]) -> str:
        number = len(self.footnotes) + 1
        if sources in self.first_note:
            body = _run("See supra", "<w:i/>") + _run(f" note {self.first_note[sources]}.")
        else:
            self.first_note[sources] = number
            cites = [s.removeprefix("§ ") for s in sources]
            mark = "§§" if len(cites) > 1 else "§"
            body = _run(f"{self.bill.legis_num} {mark} {', '.join(cites)}.")
        self.footnotes.append(
            f'<w:footnote w:id="{number}"><w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>'
            f'<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/></w:r>'
            f'{_run(" ")}{body}</w:p></w:footnote>')
        return f'<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>' \
               f'<w:footnoteReference w:id="{number}"/></w:r>'

    # Turning statutory text into lines

    def _pieces(self, text: Text, bold: bool = False) -> list[_Piece]:
        return [_Piece(s.text, s.status, s.sources, bold) for s in text.segments if s.text]

    def _lines(self, node: Node, flags: dict[int, list[Flag]], lines: list[_Line], root: bool = False) -> None:
        pieces: list[_Piece] = []
        for part, bold in ((node.label, True), (node.heading, True), (node.body, False)):
            new = self._pieces(part, bold)
            if new:
                if pieces:
                    pieces.append(_Piece(" ", ORIG, (), False))
                pieces.extend(new)
        if pieces:
            lines.append(_Line(pieces, 0 if root else _INDENT.get(node.kind, 0)))
        lines.extend(_Line(flag=f) for f in flags.get(id(node), []))
        for child in node.children:
            if child.kind == "continuation":
                # Text between subdivisions lines up with the text that introduced them.
                text = self._pieces(child.body)
                if text:
                    lines.append(_Line(text, 0 if root else _INDENT.get(node.kind, 0)))
                continue
            self._lines(child, flags, lines)
        tail = self._pieces(node.tail)
        if tail:
            lines.append(_Line(tail, 0 if root else _INDENT.get(node.kind, 0)))

    def _render_lines(self, lines: list[_Line]) -> str:
        """Render lines, with one footnote per run of adjacent changes from the same provisions."""
        notes = self._note_placement(lines)
        out = []
        for line in lines:
            if line.flag is not None:
                out.append(self._flag_para(line.flag))
                continue
            content = ""
            for piece in line.pieces:
                props = ("<w:b/>" if piece.bold else "") + _status_props(piece.status)
                content += _run(piece.text, props)
                if id(piece) in notes:
                    content += self._footnote(notes[id(piece)])
            out.append(_para(content, indent=line.indent))
        return "".join(out)

    def _note_placement(self, lines: list[_Line]) -> dict[int, tuple[str, ...]]:
        """Map the piece that ends each change to the provisions it cites.

        Adjacent changes sharing a provision count as one change, such as a deletion and
        its replacement, or all the parts of a new subsection.
        """
        notes: dict[int, tuple[str, ...]] = {}
        last: _Piece | None = None
        cited: list[str] = []

        def close() -> None:
            nonlocal last, cited
            if last is not None:
                notes[id(last)] = tuple(dict.fromkeys(cited))
            last, cited = None, []

        for line in lines:
            for piece in line.pieces:
                if piece.status == ORIG:
                    if piece.text.strip():
                        close()
                    continue
                if last is not None and not set(piece.sources) & set(last.sources):
                    close()
                last = piece
                cited.extend(piece.sources)
        close()
        return notes

    def _flag_para(self, flag: Flag) -> str:
        return _para(_run(f"[Flag {flag.number}, {flag.cite}: {flag.message}]",
                          '<w:highlight w:val="yellow"/><w:sz w:val="18"/>'))

    # Document parts

    def _summary(self, passed: list[str]) -> str:
        o, b = self.outcome, self.bill
        blocking = [f for f in o.flags if f.blocking]
        notes = [f for f in o.flags if not f.blocking]
        out = [_para(_run(f"Redline of {b.legis_num}: {b.title}"), "Title")]
        out.append(_para(self._link(self.source_url, self.source_url)))
        out.append(_para(_run(f"Bill version: {self.version}. Baseline: US Code, current through "
                              f"{self._release()}.")))
        out.append(_para(_run(f"{o.applied} of {o.total} instructions applied. {len(blocking)} not applied or "
                              f"unverified, {len(notes)} to review.", "<w:b/>")))
        out.append(_para(_run("Key: ") + _run("added text", _status_props(INS)) + _run("; ") +
                         _run("deleted text", _status_props(DEL)) + _run(". Unchanged text is black.")))
        out.append(_para(_run(SCOPE_NOTE)))
        if b.provisions:
            cites = ", ".join(p.cite.removeprefix("§ ") for p in b.provisions)
            out.append(_para(_run(f"This bill also has provisions that don't amend statutory text (§§ {cites}). "
                                  "They are in the appendix.")))
        if any(d.basis == "note" for d in o.documents):
            out.append(_para(_run("Redlines marked as based on a Code note use the text printed in the Code's "
                                  "notes, which may be incomplete or out of date.")))
        out.append(_para(_run("Verification"), "Heading2"))
        out.extend(_para(_run(p), "ListBullet") for p in passed)
        if blocking:
            out.append(_para(_run("Not applied or unverified"), "Heading2"))
            out.extend(self._exception(f) for f in blocking)
        if notes:
            out.append(_para(_run("To review"), "Heading2"))
            out.extend(self._exception(f) for f in notes)
        return "".join(out)

    def _exception(self, flag: Flag) -> str:
        return _para(_run(f"Flag {flag.number} ({flag.cite}): {flag.message}"), "ListBullet")

    def _release(self) -> str:
        points = sorted(set(self.outcome.release_points.values()))
        return ", ".join(f"P.L. {p}" for p in points) if points else "an unknown release point"

    def _document(self, doc: Document, flags: dict[int, list[Flag]]) -> str:
        out = [_para(_run(f"{self.bill.legis_num}: {self.bill.title}"), keep=True)]
        out.append(_para(self._link(self.source_url, self.source_url), keep=True))
        out.append(_para(_run(doc.title), "Heading1", keep=True))
        if doc.basis == "note":
            out.append(_para(_run("Based on a Code note, which may be incomplete or out of date. " + doc.intro),
                             "Note"))
        elif doc.basis == "new":
            out.append(_para(_run("New section added by this bill. The Code's editors assign its Code number "
                                  "if it is enacted."), "Note"))
        else:
            out.append(_para(_run(f"Source: Office of the Law Revision Counsel, release point "
                                  f"{self._release().removeprefix('P.L. ')}."), "Note"))
        out.extend(self._flag_para(f) for f in self.outcome.flags
                   if f.doc is doc and (f.node is None))
        lines: list[_Line] = []
        self._lines(doc.root, flags, lines, root=True)
        out.append(self._render_lines(lines))
        if doc.credit:
            out.append(_para(_run(doc.credit), "Note"))
        return "".join(out)

    def _unredlined(self) -> str:
        out = [_para(_run("Instructions not redlined"), "Heading1")]
        out.append(_para(_run("The tool couldn't apply these instructions to statutory text. Each is reproduced "
                              "from the bill.")))
        for item in self.outcome.unredlined:
            out.append(_para(_run(f"{item.cite}: ", "<w:b/>") + _run(item.raw)))
            out.append(_para(_run(f"Reason: {item.reason}."), "Note"))
        return "".join(out)

    def _appendix(self) -> str:
        out = [_para(_run("Appendix: provisions that don't amend statutory text"), "Heading1")]
        for provision in self.bill.provisions:
            out.append(_para(_run(f"{provision.cite}. {provision.header}", "<w:b/>"), keep=True))
            out.extend(_para(_run(line)) for line in provision.text.split("\n") if line)
        return "".join(out)

    def body(self, passed: list[str]) -> str:
        o = self.outcome
        for number, flag in enumerate(sorted(o.flags, key=lambda f: not f.blocking), 1):
            flag.number = number
        flags: dict[int, list[Flag]] = {}
        for flag in o.flags:
            if flag.node is not None:
                flags.setdefault(id(flag.node), []).append(flag)
        blocks = [self._summary(passed)]
        blocks.extend(self._document(doc, flags) for doc in o.documents)
        if o.unredlined:
            blocks.append(self._unredlined())
        if self.bill.provisions:
            blocks.append(self._appendix())
        return _SECTION_BREAK.join(blocks) + _PAGE_SETUP


_PAGE_SETUP = '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>' \
              '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" ' \
              'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
_SECTION_BREAK = f'<w:p><w:pPr><w:sectPr><w:type w:val="nextPage"/>{_PAGE_SETUP[len("<w:sectPr>"):]}</w:pPr></w:p>'

_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {W_NS}>
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman"/><w:sz w:val="22"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="200"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Note"><w:name w:val="Note"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="595959"/><w:sz w:val="18"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="FootnoteText"><w:name w:val="footnote text"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0"/></w:pPr><w:rPr><w:sz w:val="18"/></w:rPr></w:style>
<w:style w:type="character" w:styleId="FootnoteReference"><w:name w:val="footnote reference"/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr></w:style>
<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>
</w:styles>"""

_SETTINGS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings {W_NS}><w:footnotePr><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr></w:settings>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def write_docx(path: str, bill: Bill, outcome: Outcome, source_url: str, version: str, passed: list[str]) -> None:
    writer = Writer(bill, outcome, source_url, version)
    body = writer.body(passed)
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {W_NS}><w:body>{body}</w:body></w:document>'
    separators = ('<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
                  '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>')
    footnotes = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:footnotes {W_NS}>{separators}{"".join(writer.footnotes)}</w:footnotes>'
    rels = [
        f'<Relationship Id="rIdStyles" Type="{_REL}/styles" Target="styles.xml"/>',
        f'<Relationship Id="rIdSettings" Type="{_REL}/settings" Target="settings.xml"/>',
        f'<Relationship Id="rIdFootnotes" Type="{_REL}/footnotes" Target="footnotes.xml"/>',
    ]
    for i, url in enumerate(writer.links):
        rels.append(f'<Relationship Id="rIdLink{i}" Type="{_REL}/hyperlink" Target="{escape(url)}" TargetMode="External"/>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(rels) + "</Relationships>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", _CONTENT_TYPES)
        package.writestr("_rels/.rels", _ROOT_RELS)
        package.writestr("word/document.xml", document)
        package.writestr("word/styles.xml", _STYLES)
        package.writestr("word/settings.xml", _SETTINGS)
        package.writestr("word/footnotes.xml", footnotes)
        package.writestr("word/_rels/document.xml.rels", doc_rels)
