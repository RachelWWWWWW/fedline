"""Statutory text as a tree of nodes whose text tracks every edit made to it.

Each piece of text is a list of segments marked original, deleted or inserted,
along with the bill provisions responsible. The text before and after the bill
can both be read back from the same segments, and every change keeps its
citation for the footnotes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

ORIG, DEL, INS = "orig", "del", "ins"
LEVELS = ("section", "subsection", "paragraph", "subparagraph", "clause", "subclause", "item", "subitem",
          "subsubitem")
# Elements the Code uses for subdivisions. "level" is a generic one, used where the usual names don't fit.
_SUBDIVISIONS = LEVELS + ("level",)
# Text printed between or after subdivisions. Notes here are bracketed editorial text, like "[Amended this
# section.]", printed in place in the Code; footnotes are left out separately.
_RUNNING_TEXT = ("continuation", "proviso", "note")


def norm(text: str) -> str:
    return " ".join(text.split())


@dataclass
class Segment:
    text: str
    status: str = ORIG
    sources: tuple[str, ...] = ()


class Text:
    def __init__(self, text: str = "", status: str = ORIG, sources: tuple[str, ...] = ()):
        self.segments = [Segment(text, status, sources)] if text else []

    def current(self) -> str:
        return "".join(s.text for s in self.segments if s.status != DEL)

    def before(self) -> str:
        return "".join(s.text for s in self.segments if s.status != INS)

    def changed(self) -> bool:
        return any(s.status != ORIG for s in self.segments)

    def _split_at(self, pos: int) -> int:
        """Return the segment index at current-text offset pos, splitting if needed."""
        offset = 0
        for i, seg in enumerate(self.segments):
            if seg.status == DEL:
                continue
            if pos == offset:
                return i
            if pos < offset + len(seg.text):
                cut = pos - offset
                self.segments[i:i + 1] = [
                    Segment(seg.text[:cut], seg.status, seg.sources),
                    Segment(seg.text[cut:], seg.status, seg.sources),
                ]
                return i + 1
            offset += len(seg.text)
        return len(self.segments)

    def strike(self, start: int, end: int, source: str) -> tuple[str, ...]:
        """Delete a current-text range. Returns the sources of inserted text it removed."""
        first = self._split_at(start)
        last = self._split_at(end)
        first = self._split_at(start)
        carried: list[str] = []
        kept: list[Segment] = []
        for seg in self.segments[first:last]:
            if seg.status == ORIG:
                kept.append(Segment(seg.text, DEL, (source,)))
            elif seg.status == INS:
                carried.extend(seg.sources)
            else:
                kept.append(seg)
        self.segments[first:last] = kept
        return tuple(carried)

    def insert(self, pos: int, text: str, sources: tuple[str, ...]) -> None:
        """Insert at a current-text offset, after any deletions at that spot."""
        i = self._split_at(pos)
        self.segments.insert(i, Segment(text, INS, tuple(dict.fromkeys(sources))))

    def replace(self, start: int, end: int, text: str, source: str) -> None:
        carried = self.strike(start, end, source)
        self.insert(start, text, carried + (source,))

    def cut(self, pos: int) -> "Text":
        """Split off and return the text from a current-text offset to the end."""
        i = self._split_at(pos)
        rest = Text()
        rest.segments, self.segments = self.segments[i:], self.segments[:i]
        return rest

    def delete_all(self, source: str) -> None:
        """Delete everything. Earlier edits to this text are moot, so only this source is cited."""
        self.strike(0, len(self.current()), source)
        for seg in self.segments:
            seg.sources = (source,)


@dataclass
class Node:
    kind: str
    value: str = ""
    label: Text = field(default_factory=Text)
    heading: Text = field(default_factory=Text)
    body: Text = field(default_factory=Text)
    children: list["Node"] = field(default_factory=list)
    tail: Text = field(default_factory=Text)
    deleted: bool = False

    def texts(self, headings: bool = True):
        """Yield this subtree's text blocks in reading order."""
        if headings:
            yield self.heading
        yield self.body
        for child in self.children:
            yield from child.texts(headings)
        yield self.tail

    def live_child(self, value: str, kind: str | None = None) -> "Node | None":
        for child in self.children:
            if child.kind == "continuation":
                continue
            if not child.deleted and child.value == value and (kind is None or child.kind == kind):
                return child
        return None

    def delete(self, source: str) -> None:
        self.deleted = True
        self.label.delete_all(source)
        self.heading.delete_all(source)
        self.body.delete_all(source)
        self.tail.delete_all(source)
        for child in self.children:
            child.delete(source)

    def rewrite(self, new: "Node", source: str) -> None:
        """Amend to read as new: strike everything but the designation and insert new's text in its place."""
        for old, added in ((self.heading, new.heading), (self.body, new.body), (self.tail, new.tail)):
            old.delete_all(source)
            old.segments += added.segments
        for child in self.children:
            child.delete(source)
        self.children += new.children

    def renumber(self, value: str, source: str) -> None:
        current = self.label.current()
        match = re.search(rf"\({re.escape(self.value)}\)|\b{re.escape(self.value)}\b", current)
        new = f"({value})" if match and match.group(0).startswith("(") else value
        if match:
            self.label.replace(match.start(), match.end(), new, source)
        self.value = value

    def changed(self) -> bool:
        return (self.label.changed() or self.heading.changed() or self.body.changed()
                or self.tail.changed() or any(c.changed() for c in self.children))

    def before_text(self) -> str:
        parts = [self.label.before(), self.heading.before(), self.body.before()]
        parts += [c.before_text() for c in self.children if c.before_text()]
        parts.append(self.tail.before())
        return "".join(parts)


@dataclass
class Document:
    """One statutory unit that gets its own set of pages."""
    key: str
    title: str
    root: Node
    basis: str  # "code", "note" or "new"
    source_text: str = ""  # text read independently of the tree, for verification
    credit: str = ""
    intro: str = ""
    order: tuple = ()


# The start of a source credit: the enacting law, then any title or part, then the section.
# Divisions are kept with the law because omnibus laws restart section numbers in each division.
_CREDIT = re.compile(
    r"\(?(?:Pub\. L\. (?P<pl>\d+[–-]\d+)(?:, (?P<div>div\. \w+))?"
    r"|(?P<date>[A-Z][a-z]+\.? \d+, \d{4}), (?P<ch>ch\. \w+)"
    r"|R\.S\.)"
    r"(?:, (?:title|subtitle|pt\.|subpart) [\w-]+)*,? § (?P<section>\w+)\b"
)


def credit_origin(credit: str) -> tuple[str, str] | None:
    """Read the enacting law and its section number from a source credit."""
    match = _CREDIT.match(credit)
    if not match:
        return None
    if match.group("pl"):
        law = f"Pub. L. {match.group('pl').replace('–', '-')}"
        if match.group("div"):
            law += f", {match.group('div')}"
    elif match.group("date"):
        law = f"Act of {match.group('date')}, {match.group('ch')}"
    else:
        law = "R.S."
    return law, match.group("section")


@dataclass
class Act:
    law: str
    sections: dict[str, Document] = field(default_factory=dict)
    title: int = 0


def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _is_footnote(el: ET.Element) -> bool:
    """A footnote the Code's editors added, or the reference mark pointing to one."""
    tag = _local(el)
    return ((tag == "ref" and "footnoteRef" in (el.get("class") or "").split())
            or (tag == "note" and el.get("type") == "footnote"))


def _itertext(el: ET.Element):
    """Like el.itertext(), but leaving out editorial footnotes, which aren't part of the statute."""
    if el.text:
        yield el.text
    for child in el:
        if not _is_footnote(child):
            yield from _itertext(child)
        if child.tail:
            yield child.tail


def _flat(el: ET.Element) -> str:
    return norm("".join(_itertext(el)))


def _strip_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def parse_uslm_node(el: ET.Element) -> Node:
    """Convert a USLM level element (section, subsection, ...) into a Node."""
    node = Node(kind=_local(el))
    body_parts: list[str] = []
    tail_parts: list[str] = []
    levels = [i for i, child in enumerate(el) if _local(child) in _SUBDIVISIONS]
    for i, child in enumerate(el):
        tag = _local(child)
        if _is_footnote(child):
            continue
        if tag in _RUNNING_TEXT or (tag in ("chapeau", "content", "p") and levels and i > levels[0]):
            # Text keeps its place in reading order: before the first subdivision it's introductory text,
            # between subdivisions (as in 16 U.S.C. 1536(b)(4), or a second chapeau as in 460u-3) it's an
            # unlabeled node, and after the last it follows them.
            if not levels or i < levels[0]:
                body_parts.append(_flat(child))
            elif i < levels[-1]:
                node.children.append(Node(kind="continuation", body=Text(_flat(child))))
            else:
                tail_parts.append(_flat(child))
        elif tag == "num":
            node.value = child.get("value", "")
            node.label = Text(_flat(child))
        elif tag == "heading":
            node.heading = Text(_flat(child))
        elif tag in _SUBDIVISIONS:
            node.children.append(parse_uslm_node(child))
        elif tag in ("chapeau", "content", "p"):
            paragraphs = list(child)
            if tag == "content" and len(paragraphs) > 1 and all(_local(p) == "p" for p in paragraphs):
                # Keep any text before or between the paragraphs, as in 15 U.S.C. 12(a).
                body_parts.append(norm(child.text or ""))
                for p in paragraphs:
                    body_parts += [_flat(p), norm(p.tail or "")]
            else:
                body_parts.append(_flat(child))
    node.body = Text("\n".join(p for p in body_parts if p))
    node.tail = Text("\n".join(tail_parts))
    return node


def _body_source_text(el: ET.Element) -> str:
    """Read a section's text straight from the XML, skipping notes, credits and footnotes."""
    parts = []
    for child in el:
        if _local(child) in ("notes", "sourceCredit"):
            continue
        parts.append("".join(_itertext(child)))
    return _strip_ws("".join(parts))


class CodeTitle:
    """A title of the US Code, loaded from a House release point's XML."""

    def __init__(self, xml: bytes):
        self.root = ET.fromstring(xml)
        self.ns = _local_ns(self.root)
        self.number = 0
        self.release_point = ""
        for el in self.root.iter():
            tag = _local(el)
            if tag == "docNumber" and not self.number and (el.text or "").strip().isdigit():
                self.number = int(el.text.strip())
            elif tag == "docPublicationName" and not self.release_point:
                self.release_point = (el.text or "").split("@")[-1].strip()
            if self.number and self.release_point:
                break
        self.sections: dict[str, ET.Element] = {}
        for el in self.root.iter(f"{{{self.ns}}}section"):
            identifier = el.get("identifier") or ""
            match = re.fullmatch(r"/us/usc/t\d+[a-zA-Z]?/s([^/]+)", identifier)
            if match:
                self.sections.setdefault(match.group(1), el)

    def chapter_sections(self, chapter: str) -> list[str]:
        """The numbers of a chapter's sections, in order."""
        for el in self.root.iter(f"{{{self.ns}}}chapter"):
            num = el.find(f"{{{self.ns}}}num")
            if num is not None and num.get("value") == chapter:
                return [n for s in el.iter(f"{{{self.ns}}}section")
                        for n in re.findall(r"^/us/usc/t\d+[a-zA-Z]?/s([^/]+)$", s.get("identifier") or "")]
        return []

    def document(self, number: str) -> Document | None:
        el = self.sections.get(number)
        if el is None:
            return None
        credit = el.find(f"{{{self.ns}}}sourceCredit")
        return Document(
            key=f"{self.number}usc{number}",
            title=f"{self.number} U.S.C. § {number}",
            root=parse_uslm_node(el),
            basis="code",
            source_text=_body_source_text(el),
            credit=_flat(credit) if credit is not None else "",
            order=(self.number, _sort_key(number), 0),
        )

    def act_of(self, number: str) -> Act | None:
        """Map an Act's section numbers to Code sections using each section's source credit."""
        origin = self.origin(number)
        if origin is None:
            return None
        law, _ = origin
        act = Act(law=law, title=self.number)
        ambiguous = set()
        for candidate in self.sections:
            found = self.origin(candidate)
            if found and found[0] == law:
                doc = self.document(candidate)
                if doc is None:
                    continue
                if found[1] in act.sections:
                    # Two Code sections credit the same Act section, so neither can be chosen safely.
                    ambiguous.add(found[1])
                act.sections.setdefault(found[1], doc)
        for section in ambiguous:
            del act.sections[section]
        return act

    def origin(self, number: str) -> tuple[str, str] | None:
        """The law a Code section was enacted by, and its section number in that law."""
        el = self.sections.get(number)
        credit = el.find(f"{{{self.ns}}}sourceCredit") if el is not None else None
        return credit_origin(_flat(credit)) if credit is not None else None

    def note_for_act(self, number: str, act_name: str) -> Document | None:
        """Find an Act reproduced in the notes of a Code section, by its name."""
        el = self.sections.get(number)
        if el is None:
            return None
        for note in el.iter(f"{{{self.ns}}}note"):
            quoted = note.find(f".//{{{self.ns}}}quotedContent")
            if quoted is not None and act_name in _flat(quoted):
                return self._note_document(number, note, quoted, act_name)
        return None

    def note_for_public_law(self, public_law: str, section: str) -> Document | None:
        """Find a public law section reproduced in the notes of any section of this title."""
        congress, law = public_law.split("-")
        pattern = rf"/us/pl/{congress}/{law}(/.*)?/s{re.escape(section)}$"
        for number, el in self.sections.items():
            for note in el.iter(f"{{{self.ns}}}note"):
                quoted = note.find(f".//{{{self.ns}}}quotedContent")
                if quoted is not None and re.fullmatch(pattern, quoted.get("origin") or ""):
                    name = f"Pub. L. {public_law}, § {section}"
                    return self._note_document(number, note, quoted, name)
        return None

    def _note_document(self, number: str, note: ET.Element, quoted: ET.Element, name: str) -> Document:
        root = Node(kind="note")
        for child in quoted:
            if _local(child) in LEVELS:
                root.children.append(parse_uslm_node(child))
        heading = note.find(f"{{{self.ns}}}heading")
        parent = note.find(f"{{{self.ns}}}p")
        intro = ""
        if parent is not None:
            intro = norm((parent.text or "") + "".join(
                ("" if _is_footnote(c) else "".join(_itertext(c))) + (c.tail or "") for c in parent if c is not quoted))
        return Document(
            key=f"{self.number}usc{number}note:{name}",
            title=f"{self.number} U.S.C. § {number} note ({name})",
            root=root,
            basis="note",
            source_text=_strip_ws("".join(
                "".join(_itertext(c)) for c in quoted if _local(c) in LEVELS)),
            intro=(_flat(heading) + ". " if heading is not None else "") + intro,
            order=(self.number, _sort_key(number), 1),
        )


def _local_ns(root: ET.Element) -> str:
    return root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""


def _sort_key(number: str) -> tuple:
    match = re.match(r"(\d+)(.*)", number)
    return (int(match.group(1)), match.group(2)) if match else (0, number)
