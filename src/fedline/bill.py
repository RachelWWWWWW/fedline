"""Parse a bill's XML into structured amending instructions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .statute import LEVELS, Node, Text, INS, norm

_DIVISIONS = ("division", "title", "subtitle", "part", "subpart", "chapter", "subchapter")
_PARTS = ("subsection", "paragraph", "subparagraph", "clause", "subclause", "item", "subitem")
_LEAD = re.compile(
    r"^(?:Section (?P<section>\w+)(?P<path>(?:\(\w+\))*) of )?(?:the |The )?(?P<name>.+?) "
    r"\((?P<cite>.+)\)(?:, as amended by [^,]+,)? is (?:further )?amended"
    r"(?:, as amended by [^,]+,)?(?P<rest>.*)$"
)
_LEAD_CODE = re.compile(
    r"^Section (?P<section>\w+)(?P<path>(?:\(\w+\))*) of title (?P<title>\d+), United States Code"
    r"(?:, as amended by [^,]+)?, is (?:further )?amended(?P<rest>.*)$"
)


@dataclass
class Instruction:
    """One operation from the bill, with where it applies and its bill citation."""
    cite: str
    raw: str
    target: "Target"
    path: list[tuple[str, str]]
    heading: bool = False
    action: str = ""
    args: dict = field(default_factory=dict)


@dataclass
class Target:
    name: str = ""
    cite: str = ""
    section: str | None = None
    path: list[tuple[str, str]] = field(default_factory=list)
    title: int | None = None
    usc_section: str | None = None
    is_note: bool = False
    public_law: str | None = None


@dataclass
class Provision:
    """Bill text that doesn't amend anything, reproduced in the appendix."""
    cite: str
    header: str
    text: str


@dataclass
class ParseProblem:
    cite: str
    raw: str
    reason: str


@dataclass
class Bill:
    legis_num: str
    title: str
    official_title: str
    instructions: list[Instruction] = field(default_factory=list)
    provisions: list[Provision] = field(default_factory=list)
    problems: list[ParseProblem] = field(default_factory=list)
    plain_text: str = ""

    def titles(self) -> list[int]:
        found = {i.target.title for i in self.instructions if i.target.title}
        return sorted(found)


def _quoted_flat(el: ET.Element) -> str:
    """Flatten bill text the way it prints: quote and term elements carry quotation marks."""
    out = [el.text or ""]
    for child in el:
        tag = child.tag
        if tag in ("quote", "term"):
            out.append("“" + _quoted_flat(child) + "”")
        else:
            out.append(_quoted_flat(child))
        out.append(child.tail or "")
    return "".join(out)


def tokenize(text_el: ET.Element) -> tuple[str, list[str]]:
    """Replace each quoted string with a placeholder, so patterns never match inside quotes."""
    quotes: list[str] = []
    parts = [text_el.text or ""]
    for child in text_el:
        if child.tag == "quote":
            quotes.append(norm(_quoted_flat(child)))
            parts.append(f"⟦{len(quotes) - 1}⟧")
        else:
            parts.append(_quoted_flat(child))
        parts.append(child.tail or "")
    return norm("".join(parts)), quotes


def _enum(el: ET.Element) -> str:
    return norm(el.findtext("enum") or "")


def _cite(parts: list[str]) -> str:
    return "§ " + "".join(parts)


def _parts_of(path: str) -> list[str]:
    return re.findall(r"\((\w+)\)", path)


def _level_for(values: list[str], start: str) -> list[tuple[str, str]]:
    """Pair path values with levels, starting below the given level."""
    index = LEVELS.index(start)
    return [(LEVELS[min(index + 1 + i, len(LEVELS) - 1)], v) for i, v in enumerate(values)]


def _parse_target(match: re.Match) -> Target | None:
    groups = match.groupdict()
    target = Target(section=groups.get("section"), path=_level_for(_parts_of(groups.get("path") or ""), "section"))
    if groups.get("title"):
        target.title = int(groups["title"])
        target.usc_section = groups["section"]
        target.name = f"title {groups['title']}, United States Code"
        target.cite = target.name
        return target
    target.name = norm(groups["name"])
    target.cite = groups["cite"]
    usc = re.search(r"(\d+) U\.S\.C\. (\w+)(?:\([^)]*\))*( note| et seq\.)?", target.cite)
    public_law = re.search(r"Public Law (\d+)[–-](\d+)", target.cite)
    if usc:
        target.title = int(usc.group(1))
        target.usc_section = usc.group(2)
        target.is_note = (usc.group(3) or "").strip() == "note"
    elif public_law:
        target.public_law = f"{public_law.group(1)}-{public_law.group(2)}"
        target.is_note = True
    else:
        return None
    return target


_LEVEL_WORDS = {f"{word}s": word for word in LEVELS} | {word: word for word in LEVELS}
_AS_NOTE = re.compile(r" \(as (?:so )?(?:redesignated|designated|added|amended)[^)]*\)")


def _roman(n: int) -> str:
    out = ""
    for value, letters in ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out, n = out + letters, n - value
    return out


_ROMANS = [_roman(n) for n in range(1, 40)]


def _span(start: str, end: str, kind: str) -> list[str] | None:
    """Expand 'start through end' in the numbering style of its level, or None if unsure."""
    if start.isdigit() and end.isdigit():
        values = [str(n) for n in range(int(start), int(end) + 1)]
    elif kind in ("clause", "subclause"):
        romans = _ROMANS if kind == "clause" else [r.upper() for r in _ROMANS]
        if start not in romans or end not in romans:
            return None
        values = romans[romans.index(start):romans.index(end) + 1]
    elif len(start) == len(end) == 1 and start.isalpha() and start.islower() == end.islower():
        values = [chr(c) for c in range(ord(start), ord(end) + 1)]
    else:
        return None
    return values if len(values) > 1 else None


def _designators(text: str, kind: str) -> list[str] | None:
    """The designations listed in text, expanding ranges. None if a range can't be expanded."""
    pattern = r"\b(\d+\w*)\b" if kind == "section" else r"\((\w+)\)"
    values: list[str] = []
    pending_range = False
    for match in re.finditer(rf"{pattern}|\bthrough\b", text):
        if match.group(0) == "through":
            if not values:
                return None
            pending_range = True
            continue
        if pending_range:
            span = _span(values[-1], match.group(1), kind)
            if span is None:
                return None
            values += span[1:]
            pending_range = False
        else:
            values.append(match.group(1))
    return None if pending_range else values


def _parse_locators(text: str) -> tuple[list[tuple[str, str]], bool, str]:
    """Consume leading 'in subsection (a), in the heading,' phrases."""
    path: list[tuple[str, str]] = []
    heading = False
    text = _AS_NOTE.sub("", text)
    while True:
        match = re.match(r"^in the (?:\w+ )?heading of (\w+) ((?:\(\w+\))+)(?:, |—|$)", text)
        if match:
            path += _path_from(match.group(1), match.group(2))
            heading = True
            text = text[match.end():]
            continue
        match = re.match(r"^in the (?:\w+ )?heading(?:, |—|$)", text)
        if match:
            heading = True
            text = text[match.end():]
            continue
        match = re.match(r"^in section (\w+)(?:, |—|$)", text)
        if match:
            path.append(("section", match.group(1)))
            text = text[match.end():]
            continue
        match = re.match(r"^in (\w+) ((?:\(\w+\))+)(?:, |—|$)", text)
        if match and match.group(1) in LEVELS:
            path += _path_from(match.group(1), match.group(2))
            text = text[match.end():]
            continue
        return path, heading, text


def _path_from(kind: str, values: str) -> list[tuple[str, str]]:
    vals = _parts_of(values)
    return [(kind, vals[0])] + _level_for(vals[1:], kind)


def _parse_action(text: str, quotes: list[str], blocks: list[Node]) -> tuple[str, dict] | None:
    """Recognize an instruction's operation. Returns None when it isn't understood."""
    text = re.sub(r"[;.,]?(?: and| or)?$", "", text).strip()
    q = lambda m, name: quotes[int(m.group(name))]

    match = re.fullmatch(
        r"by striking ⟦(?P<a>\d+)⟧(?P<every> each place (?:it|such term) appears?)?"
        r"(?: \(except in section (?P<ex>\w+)\))? and inserting ⟦(?P<b>\d+)⟧", text)
    if match:
        return "replace", {"old": q(match, "a"), "new": q(match, "b"),
                           "every": bool(match.group("every")), "except": match.group("ex")}

    match = re.fullmatch(r"by inserting ⟦(?P<a>\d+)⟧ before ⟦(?P<b>\d+)⟧", text)
    if match:
        return "insert_before", {"new": q(match, "a"), "anchor": q(match, "b")}

    match = re.fullmatch(r"by inserting ⟦(?P<a>\d+)⟧ after ⟦(?P<b>\d+)⟧", text)
    if match:
        return "insert_after", {"new": q(match, "a"), "anchor": q(match, "b")}

    match = re.fullmatch(r"by inserting ⟦(?P<a>\d+)⟧ before the period at the end", text)
    if match:
        return "insert_before_period", {"new": q(match, "a")}

    text = _AS_NOTE.sub("", text)
    kinds = "|".join(_LEVEL_WORDS)

    match = re.fullmatch(rf"by striking ({kinds}) (.+?) and inserting the following:?", text)
    if match and blocks:
        kind = _LEVEL_WORDS[match.group(1)]
        values = _designators(match.group(2), kind)
        if values:
            return "replace_nodes", {"kind": kind, "values": values, "blocks": blocks}

    match = re.fullmatch(rf"by amending ({kinds}) (.+?) to read as follows:?", text)
    if match and blocks:
        kind = _LEVEL_WORDS[match.group(1)]
        values = _designators(match.group(2), kind)
        if values:
            return "replace_nodes", {"kind": kind, "values": values, "blocks": blocks}

    match = re.fullmatch(rf"by striking ({kinds}) (.+)", text)
    if match:
        kind = _LEVEL_WORDS[match.group(1)]
        values = _designators(match.group(2), kind)
        if values:
            return "delete_nodes", {"kind": kind, "values": values}

    match = re.fullmatch(rf"by redesignating ({kinds}) (.+?) as ({kinds}) (.+?)(?:, respectively)?", text)
    if match:
        kind = _LEVEL_WORDS[match.group(1)]
        old = _designators(match.group(2), kind)
        new = _designators(match.group(4), _LEVEL_WORDS[match.group(3)])
        if old and new:
            return "redesignate", {"kind": kind, "old": old, "new": new}

    match = re.fullmatch(r"to read as follows:?", text)
    if match and len(blocks) == 1:
        return "rewrite", {"blocks": blocks}

    match = re.fullmatch(r"by adding at the end the following:?", text)
    if match and blocks:
        return "add_at_end", {"blocks": blocks}

    match = re.fullmatch(rf"by inserting after ({kinds}) (.+?) the following:?", text)
    if match and blocks:
        kind = _LEVEL_WORDS[match.group(1)]
        values = _designators(match.group(2), kind)
        if values:
            return "insert_after_node", {"kind": kind, "values": values, "blocks": blocks}

    return None


def _new_node(el: ET.Element, source: str) -> Node:
    """Build added statutory text from a quoted block. Every part is marked as inserted."""
    kind = el.tag
    enum = _enum(el)
    value = enum.strip("().")
    label = f"Sec. {enum}" if kind == "section" else enum
    node = Node(kind=kind, value=value, label=Text(label, INS, (source,)))
    header = el.find("header")
    if header is not None:
        node.heading = Text(norm(_quoted_flat(header)), INS, (source,))
    text_el = el.find("text")
    if text_el is not None:
        node.body = Text(norm(_quoted_flat(text_el)), INS, (source,))
    for child in el:
        if child.tag in LEVELS:
            node.children.append(_new_node(child, source))
    continuation = el.find("continuation-text")
    if continuation is not None:
        node.tail = Text(norm(_quoted_flat(continuation)), INS, (source,))
    return node


def _block_nodes(el: ET.Element, source: str) -> list[Node]:
    blocks = []
    for block in el.findall("quoted-block"):
        blocks.extend(_new_node(child, source) for child in block if child.tag in LEVELS)
    return blocks


def _is_lead(text: str) -> re.Match | None:
    return _LEAD_CODE.match(text) or _LEAD.match(text)


def _has_amendment(el: ET.Element) -> bool:
    """True when this provision or one below it amends a law."""
    text_el = el.find("text")
    if text_el is not None and re.search(r"\bis (?:further )?amended\b", tokenize(text_el)[0]):
        return True
    return any(_has_amendment(child) for child in el if child.tag in _PARTS)


def _provision_text(el: ET.Element, depth: int = 0) -> str:
    parts = []
    enum = _enum(el)
    header = norm(el.findtext("header") or "")
    text_el = el.find("text")
    text = norm(_quoted_flat(text_el)) if text_el is not None else ""
    if depth:
        parts.append(" ".join(p for p in (enum, (header + ".—") if header else "", text) if p))
    elif text:
        parts.append(text)
    for child in el:
        if child.tag in _PARTS:
            parts.append(_provision_text(child, depth + 1))
    return "\n".join(parts)


class _Parser:
    def __init__(self, bill: Bill):
        self.bill = bill

    def section(self, el: ET.Element) -> None:
        number = _enum(el).rstrip(".")
        self.provision_level(el, [number])

    def provision_level(self, el: ET.Element, cite: list[str]) -> None:
        """Walk a provision that isn't yet inside an amending lead-in."""
        text_el = el.find("text")
        if text_el is not None:
            text, quotes = tokenize(text_el)
            match = _is_lead(text)
            if match:
                self.amending(el, match, text, quotes, cite)
                return
            if re.search(r"\bis (?:further )?amended\b", text):
                self.bill.problems.append(ParseProblem(_cite(cite), text, "couldn't identify the law being amended"))
                return
        if _has_amendment(el):
            for child in el:
                if child.tag in _PARTS:
                    self.provision_level(child, cite + [_enum(child)])
            return
        self.bill.provisions.append(Provision(_cite(cite), norm(el.findtext("header") or ""), _provision_text(el)))

    def amending(self, el, match, text, quotes, cite) -> None:
        target = _parse_target(match)
        if target is None:
            self.bill.problems.append(ParseProblem(_cite(cite), text, "couldn't resolve the citation for the law being amended"))
            return
        # "is amended—", "is amended, in subsection (a)—" or "is amended, in subsection (a), by …"
        rest = match.group("rest").strip().removeprefix(",").strip()
        located, _, remainder = _parse_locators(rest)
        children = [c for c in el if c.tag in _PARTS]
        if remainder in ("", "—") and children:
            for child in children:
                self.instruction(child, target, located, cite + [_enum(child)])
            return
        self.leaf(el, rest, quotes, target, [], cite, False)

    def instruction(self, el, target, path, cite) -> None:
        text_el = el.find("text")
        if text_el is None:
            self.bill.problems.append(ParseProblem(_cite(cite), "", "instruction has no text"))
            return
        text, quotes = tokenize(text_el)
        located, heading, rest = _parse_locators(text)
        children = [c for c in el if c.tag in _PARTS]
        if not rest and children:
            for child in children:
                self.instruction(child, target, path + located, cite + [_enum(child)])
            return
        self.leaf(el, rest, quotes, target, path + located, cite, heading, raw=text)

    def leaf(self, el, rest, quotes, target, path, cite, heading, raw=None) -> None:
        cite_text = _cite(cite)
        located, more_heading, rest = _parse_locators(rest.strip())
        parsed = _parse_action(rest, quotes, _block_nodes(el, cite_text))
        full = raw or rest
        for i, quote in enumerate(quotes):
            full = full.replace(f"⟦{i}⟧", f"“{quote}”")
        if parsed is None:
            self.bill.problems.append(ParseProblem(cite_text, full, "instruction wording isn't a recognized pattern"))
            return
        action, args = parsed
        # Bills mark text quoted from a heading, even when the wording doesn't say "heading".
        tagged = el.find("text/quote/header-in-text") is not None
        self.bill.instructions.append(Instruction(
            cite=cite_text, raw=full, target=target, path=path + located,
            heading=heading or more_heading or tagged, action=action, args=args))


def parse_bill(xml: bytes) -> Bill:
    root = ET.fromstring(xml)
    dc_title = ""
    for el in root.iter():
        if el.tag.endswith("}title"):
            dc_title = norm(el.text or "")
            break
    body = root.find("legis-body")
    bill = Bill(
        legis_num=norm(root.findtext(".//legis-num") or ""),
        title=dc_title.split(": ", 1)[-1],
        official_title=norm("".join(root.find(".//official-title").itertext()))
        if root.find(".//official-title") is not None else "",
        plain_text=_quoted_flat(body) if body is not None else "",
    )
    if body is not None:
        _walk_divisions(body, _Parser(bill))
    return bill


def _walk_divisions(container: ET.Element, parser: _Parser) -> None:
    """Visit the bill's own sections, including those grouped under titles or subtitles."""
    for child in container:
        if child.tag == "section":
            parser.section(child)
        elif child.tag in _DIVISIONS:
            _walk_divisions(child, parser)
