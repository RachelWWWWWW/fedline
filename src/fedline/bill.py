"""Parse a bill's XML into structured amending instructions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from xml.etree import ElementTree as ET

from .statute import LEVELS, Node, Text, INS, norm

_DIVISIONS = ("division", "title", "subtitle", "part", "subpart", "chapter", "subchapter")
_PARTS = ("subsection", "paragraph", "subparagraph", "clause", "subclause", "item", "subitem")
# "Paragraph (4) of section 3101 of", "Section 216(b) of": where in the law the lead-in points.
_WHERE = (r"(?:(?P<small>Subsection|Paragraph|Subparagraph|Clause|Subclause) (?P<small_path>(?:\(\w+\))+) of )?"
          r"(?:[Ss]ection (?P<section>\w+)(?P<path>(?:\(\w+\))*) of )?")
# A citation in parentheses, which may itself contain parentheses, as in "(42 U.S.C. 4332(2)(C))". A
# parenthetical starting "as amended" or "as added" isn't a citation.
_CITE = r"\((?!as )(?P<cite>(?:[^()]|\([^()]*\))+)\)"
_AS_AMENDED = r"(?: \(as (?:amended|added|redesignated)(?:[^()]|\([^()]*\))*\)|, as amended by [^,]+,)?"
_LEAD = re.compile(
    rf"^{_WHERE}(?:the |The )?(?P<name>.+?) {_CITE}{_AS_AMENDED},? is (?:further )?amended{_AS_AMENDED}"
    r"(?P<rest>.*)$"
)
_LEAD_CODE = re.compile(
    r"^Section (?P<section>\w+)(?P<path>(?:\(\w+\))*) of title (?P<title>\d+), United States Code"
    r"(?:, as amended by [^,]+)?, is (?:further )?amended(?P<rest>.*)$"
)
_LEAD_CHAPTER = re.compile(
    r"^Chapter (?P<chapter>\w+) of title (?P<title>\d+), United States Code"
    r"(?:, as amended by [^,]+)?, is (?:further )?amended(?P<rest>.*)$"
)
# An Act named without a citation, as in "The Federal Power Act is amended by inserting after section 216
# (16 U.S.C. 824p) the following:". The citation, if any, comes later in the instruction.
_LEAD_NAMED = re.compile(
    rf"^{_WHERE}(?:the |The )?"
    r"(?P<name>(?:(?:Title|Part|Subtitle) \w+ of (?:the )?)?[A-Z][^()]*? Act(?: of \d{4})?)"
    rf"{_AS_AMENDED},? is (?:further )?amended(?P<rest>.*)$"
)
_USC_CITE = re.compile(r" \((\d+ U\.S\.C\. (?:[^()]|\([^()]*\))*)\)")


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
    # Where in the provision: ("preceding" | "following", kind, value) for the matter before or after
    # a subdivision, ("sentence", n) for the nth sentence, with -1 for the last.
    within: list[tuple] = field(default_factory=list)


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
    chapter: str | None = None


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
    if groups.get("small"):
        if not target.section:
            return None
        small = groups["small"].lower()
        target.path += _path_from(small, groups["small_path"])
    if groups.get("chapter"):
        target.title = int(groups["title"])
        target.chapter = groups["chapter"]
        target.name = f"chapter {groups['chapter']} of title {groups['title']}, United States Code"
        target.cite = target.name
        return target
    if groups.get("title"):
        target.title = int(groups["title"])
        target.usc_section = groups["section"]
        target.name = f"title {groups['title']}, United States Code"
        target.cite = target.name
        return target
    target.name = norm(groups["name"])
    if "cite" not in groups:
        return target  # named only; the caller looks for a citation later in the instruction
    target.cite = groups["cite"]
    usc = re.search(r"(\d+) U\.S\.C\. (\w+)(?:\([^)]*\))*( note| et seq\.)?", target.cite)
    public_law = re.search(r"Public Law (\d+)[–-](\d+)", target.cite)
    if usc:
        target.title = int(usc.group(1))
        target.usc_section = usc.group(2)
        target.is_note = (usc.group(3) or "").strip() == "note"
        # "(42 U.S.C. 3211 note; Public Law 118–272)": the Public Law finds the note exactly, even when the
        # note's text doesn't give the Act's name.
        if target.is_note and public_law:
            target.public_law = f"{public_law.group(1)}-{public_law.group(2)}"
    elif public_law:
        target.public_law = f"{public_law.group(1)}-{public_law.group(2)}"
        target.is_note = True
    elif re.fullmatch(r"\d+ Stat\. \d+(?:, (?:chapter|ch\.) \w+)?", target.cite):
        # "The Act of February 5, 1948 (62 Stat. 17, chapter 45)": no Code citation here. Each instruction
        # under it may give one, as in "in section 2 (62 Stat. 18, chapter 45; 25 U.S.C. 324)".
        return target
    else:
        return None
    return target


_LEVEL_WORDS = {f"{word}s": word for word in LEVELS} | {word: word for word in LEVELS}
_KINDS = "|".join(_LEVEL_WORDS)
_AS_NOTE = re.compile(r" \(as (?:so )?(?:redesignated|designated|added|amended)(?:[^()]|\([^()]*\))*\)")


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


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
             "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}
_ORDINAL = "|".join(_ORDINALS)


# What ends a locator: a comma or dash, the end of the text, or, as bills sometimes omit the comma, the
# operation itself ("in the matter preceding subparagraph (A) by striking").
_SEP = r"(?:, |—|$| (?=by ))"


def _parse_locators(text: str) -> tuple[list[tuple[str, str]], list[tuple], bool, str]:
    """Consume leading 'in subsection (a), in the heading, in the first sentence,' phrases."""
    path: list[tuple[str, str]] = []
    within: list[tuple] = []
    heading = False
    text = _AS_NOTE.sub("", text)
    while True:
        match = re.match(rf"^in the ({_ORDINAL}) (sentence|proviso){_SEP}", text)
        if match:
            within.append((match.group(2), _ORDINALS[match.group(1)]))
            text = text[match.end():]
            continue
        match = re.match(rf"^in the (?:undesignated )?matter (preceding|following) (\w+) \((\w+)\){_SEP}", text)
        if match and match.group(2) in LEVELS:
            within.append(match.groups())
            text = text[match.end():]
            continue
        match = re.match(rf"^in the (?:\w+ )?heading of (\w+) ((?:\(\w+\))+){_SEP}", text)
        if match:
            path += _path_from(match.group(1), match.group(2))
            heading = True
            text = text[match.end():]
            continue
        match = re.match(rf"^in the (?:\w+ )?heading{_SEP}", text)
        if match:
            heading = True
            text = text[match.end():]
            continue
        match = re.match(
            rf"^in each of ({_KINDS}) ((?:\(\w+\))+(?:(?:,? and |, | through )(?:\(\w+\))+)*){_SEP}", text)
        if match:
            alternatives = _each_paths(_LEVEL_WORDS[match.group(1)], match.group(2))
            if alternatives is None:
                return path, within, heading, text
            path.append(("each", alternatives))
            text = text[match.end():]
            continue
        match = re.match(rf"^in section (\w+){_SEP}", text)
        if match:
            path.append(("section", match.group(1)))
            text = text[match.end():]
            continue
        match = re.match(rf"^in (\w+) ((?:\(\w+\))+){_SEP}", text)
        if match and match.group(1) in LEVELS:
            path += _path_from(match.group(1), match.group(2))
            text = text[match.end():]
            continue
        return path, within, heading, text


_SECTION_CITED = re.compile(rf"^in (?:section (?P<section>\w+)|the (?P<first>first) section)(?P<path>(?:\(\w+\))*) "
                            rf"{_CITE}(?:, |—|$)")


def _locate_section(target: Target, text: str) -> tuple[Target, str]:
    """Handle "in section 1412(b)(2) (50 U.S.C. 98b note)," under a lead-in that names the Act without a
    section, as in "The … Act (Public Law 118–159) is amended—". Each instruction then gets its own section,
    and its own Code citation when the Act was cited only by Public Law or the Statutes at Large, so the right
    title is searched. "In the first section (62 Stat. 17, chapter 45; 25 U.S.C. 323)" is section 1."""
    match = _SECTION_CITED.match(text)
    if not match or target.section or target.chapter or target.name.startswith("title "):
        return target, text
    located = replace(target, section="1" if match.group("first") else match.group("section"),
                      path=target.path + _level_for(_parts_of(match.group("path")), "section"))
    # The Code citation may follow a Statutes at Large one: "62 Stat. 18, chapter 45; 25 U.S.C. 324".
    usc = re.fullmatch(r"(?:[^;]*; )?(\d+) U\.S\.C\. (\w+)(?:\([^)]*\))*( note)?", match.group("cite"))
    if usc and target.title is None:
        located.title, located.usc_section = int(usc.group(1)), usc.group(2)
        located.is_note = located.is_note or bool(usc.group(3))
    return located, text[match.end():]


def _each_paths(kind: str, text: str) -> tuple | None:
    """The places named by "in each of paragraphs (3), (4)(B), and (6) through (8)", as relative paths."""
    paths: list[tuple] = []
    for item in re.split(r",? and |, ", text):
        if " through " in item:
            values = _designators(item, kind)
            if values is None:
                return None
            paths += [((kind, value),) for value in values]
        else:
            paths.append(tuple(_path_from(kind, item)))
    return tuple(paths) if len(paths) > 1 else None


def _path_from(kind: str, values: str) -> list[tuple[str, str]]:
    vals = _parts_of(values)
    return [(kind, vals[0])] + _level_for(vals[1:], kind)


_PUNCTUATION = {"period": ".", "semicolon": ";", "comma": ",", "colon": ":"}


def _parse_action(text: str, quotes: list[str], blocks: list[Node], lead: str = "") -> tuple[str, dict] | None:
    """Recognize an instruction's operation. Returns None when it isn't understood.

    lead is run-in text that the new subdivisions follow, as in 'inserting the following: “, except
    that—” (A) …'. Only operations that place it accept it."""
    parsed = _parse_operation(text, quotes, blocks, lead)
    if parsed and lead and "lead" not in parsed[1]:
        return None
    return parsed


def _parse_operation(text: str, quotes: list[str], blocks: list[Node], lead: str) -> tuple[str, dict] | None:
    text = re.sub(r"[;.,]?(?: and| or)?$", "", text).strip()
    q = lambda m, name: quotes[int(m.group(name))]

    punct = "|".join(_PUNCTUATION)
    new = rf"(?:⟦(?P<b>\d+)⟧|a (?P<pb>{punct}))"
    new_text = lambda m: q(m, "b") if m.group("b") else _PUNCTUATION.get(m.group("pb") or "", "")

    match = re.fullmatch(
        r"by striking ⟦(?P<a>\d+)⟧(?P<every> each place (?:it|such term) appears?)?"
        rf"(?: \(except in section (?P<ex>\w+)\))?(?: and inserting {new})?", text)
    if match:
        return "replace", {"old": q(match, "a"), "new": new_text(match),
                           "every": bool(match.group("every")), "except": match.group("ex")}

    match = re.fullmatch(r"by striking ⟦(?P<a>\d+)⟧ and inserting the following:?(?: ⟦(?P<b>\d+)⟧)?", text)
    if match and match.group("b") and not blocks:
        return "replace", {"old": q(match, "a"), "new": q(match, "b"), "every": False, "except": None}
    if match and blocks and not match.group("b"):
        return "replace_with_block", {"old": q(match, "a"), "blocks": blocks, "lead": lead}

    match = re.fullmatch(
        rf"by striking (?:⟦(?P<a>\d+)⟧|the (?P<pa>{punct})) at the end and inserting the following:?", text)
    if match and blocks:
        return "replace_at_end_with_block", {
            "old": q(match, "a") if match.group("a") else _PUNCTUATION[match.group("pa")],
            "blocks": blocks, "lead": lead}

    match = re.fullmatch(r"by adding at the end the following:? ⟦(?P<b>\d+)⟧", text)
    if match and not blocks:
        return "add_text_at_end", {"new": q(match, "b")}

    match = re.fullmatch(
        rf"by striking (?:⟦(?P<a>\d+)⟧|the (?P<pa>{punct}))(?: after the (?P<after>{punct}))? at the end"
        rf"(?: and inserting {new})?", text)
    if match:
        return "replace_at_end", {
            "old": q(match, "a") if match.group("a") else _PUNCTUATION[match.group("pa")],
            "after": _PUNCTUATION.get(match.group("after") or ""),
            "new": new_text(match)}

    match = re.fullmatch(
        rf"by striking ⟦(?P<a>\d+)⟧ and all that follows through (?:⟦(?P<c>\d+)⟧|the (?P<pc>{punct}) at the end)"
        rf"(?: and inserting {new})?", text)
    if match:
        return "replace_span", {
            "start": q(match, "a"), "end": q(match, "c") if match.group("c") else None,
            "end_punct": _PUNCTUATION.get(match.group("pc") or ""),
            "new": new_text(match)}

    match = re.fullmatch(r"by inserting ⟦(?P<a>\d+)⟧ after the (?P<unit>\w+) designation", text)
    if match and match.group("unit") in LEVELS:
        return "insert_at_start", {"new": q(match, "a"), "unit": match.group("unit")}

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

    # Formatting only: "by indenting clauses (i) through (v) appropriately", "by moving the margins of
    # subparagraphs (A) and (B) 2 ems to the right".
    match = re.fullmatch(
        rf"by (?:indenting ({kinds}) (.+?)(?: appropriately)?|moving the margins? of ({kinds}) (.+?) "
        r"\d+ ems? to the (?:right|left))", text)
    if match:
        kind = _LEVEL_WORDS[match.group(1) or match.group(3)]
        values = _designators(match.group(2) or match.group(4), kind)
        if values:
            return "indent", {"kind": kind, "values": values}

    # Only designations, so trailing words such as "and moving the paragraphs so as to appear in numerical
    # order" are read rather than swallowed.
    designations = r"(?:\(\w+\)|\d+\w*)(?:(?:,? and |, | through )(?:\(\w+\)|\d+\w*))*"
    match = re.fullmatch(
        rf"by redesignating ({kinds}) ({designations}) as ({kinds}) ({designations})(?:,? respectively)?"
        rf"(?:,? and (?:indenting appropriately|(?P<move>(?:indenting and )?moving the (?:{kinds}) so as to "
        r"appear in numerical order)))?", text)
    if match:
        kind = _LEVEL_WORDS[match.group(1)]
        old = _designators(match.group(2), kind)
        new_kind = _LEVEL_WORDS[match.group(3)]
        new = _designators(match.group(4), new_kind)
        if old and new:
            return "redesignate", {"kind": kind, "old": old, "new": new, "new_kind": new_kind,
                                   "reorder": bool(match.group("move"))}

    match = re.fullmatch(rf"by moving the ({kinds}) so as to appear in numerical order", text)
    if match:
        return "reorder", {"kind": _LEVEL_WORDS[match.group(1)]}

    match = re.fullmatch(
        rf"by striking the ({_ORDINAL}) sentence(?: and inserting the following:? ⟦(?P<b>\d+)⟧)?", text)
    if match:
        return "replace_sentence", {"n": _ORDINALS[match.group(1)], "new": q(match, "b") if match.group("b") else ""}

    match = re.fullmatch(rf"by inserting after the ({_ORDINAL}) sentence the following:? ⟦(?P<b>\d+)⟧", text)
    if match:
        return "insert_after_sentence", {"n": _ORDINALS[match.group(1)], "new": q(match, "b")}

    match = re.fullmatch(
        r"by striking the (?P<unit>section|subsection|paragraph|subparagraph) (?:heading and )?designation and all "
        rf"that follows through ⟦(?P<c>\d+)⟧(?: in the (?P<n>{_ORDINAL}) sentence)? and inserting the following:?", text)
    if match and len(blocks) == 1:
        return "replace_opening", {"unit": match.group("unit"), "end": q(match, "c"),
                                   "n": _ORDINALS.get(match.group("n") or ""), "blocks": blocks}

    match = re.fullmatch(r"to read as follows:?", text)
    if match and len(blocks) == 1:
        return "rewrite", {"blocks": blocks}

    match = re.fullmatch(r"by adding at the end the following:?", text)
    if match and blocks:
        return "add_at_end", {"blocks": blocks}

    match = re.fullmatch(rf"by inserting (after|before) ({kinds}) (.+?) the following:?", text)
    if match and blocks:
        kind = _LEVEL_WORDS[match.group(2)]
        values = _designators(match.group(3), kind)
        if values:
            return f"insert_{match.group(1)}_node", {"kind": kind, "values": values, "blocks": blocks}

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


def _lead_text(el: ET.Element, rest: str, blocks: list[Node]) -> tuple[str, str]:
    """Run-in text that new subdivisions follow. Bills give it either as a quotation the instruction opens
    and the quoted block closes, as in 'inserting the following: “; and' then (B), or as text at the top of
    the quoted block. Returns the instruction without it, and the text."""
    if not blocks:
        return rest, ""
    match = re.search(r"( the following:?) “([^”⟦⟧]*)$", rest)
    if match:
        return rest[:match.end(1)], norm(match.group(2))
    block = el.find("quoted-block")
    first = next(iter(block), None) if block is not None else None
    if first is not None and first.tag == "text":
        return rest, norm(_quoted_flat(first))
    return rest, ""


def _is_lead(text: str) -> re.Match | None:
    return _LEAD_CODE.match(text) or _LEAD_CHAPTER.match(text) or _LEAD.match(text) or _LEAD_NAMED.match(text)


def _cite_from(target: Target, rest: str) -> str:
    """For an Act named without a citation, take the first Code citation in the instruction, as in
    "by inserting after section 2 (42 U.S.C. 4321)", and drop the citations so they aren't read as
    designations."""
    if target.cite or target.title:
        return rest
    usc = _USC_CITE.search(rest)
    if usc:
        found = re.match(r"(\d+) U\.S\.C\. (\w+)", usc.group(1))
        target.cite, target.title, target.usc_section = usc.group(1), int(found.group(1)), found.group(2)
    return _USC_CITE.sub("", rest)


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
        rest = _cite_from(target, match.group("rest").strip().removeprefix(",").strip())
        located, within, _, remainder = _parse_locators(rest)
        children = [c for c in el if c.tag in _PARTS]
        if remainder in ("", "—") and children:
            for child in children:
                self.instruction(child, target, located, within, cite + [_enum(child)])
            return
        self.leaf(el, rest, quotes, target, [], [], cite, False)

    def instruction(self, el, target, path, within, cite) -> None:
        text_el = el.find("text")
        if text_el is None:
            self.bill.problems.append(ParseProblem(_cite(cite), "", "instruction has no text"))
            return
        text, quotes = tokenize(text_el)
        target, rest = _locate_section(target, text)
        located, more_within, heading, rest = _parse_locators(rest)
        children = [c for c in el if c.tag in _PARTS]
        if not rest and children:
            for child in children:
                self.instruction(child, target, path + located, within + more_within, cite + [_enum(child)])
            return
        self.leaf(el, rest, quotes, target, path + located, within + more_within, cite, heading, raw=text)

    def leaf(self, el, rest, quotes, target, path, within, cite, heading, raw=None) -> None:
        cite_text = _cite(cite)
        located, more_within, more_heading, rest = _parse_locators(rest.strip())
        blocks = _block_nodes(el, cite_text)
        rest, lead = _lead_text(el, rest, blocks)
        parsed = _parse_action(rest, quotes, blocks, lead)
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
            heading=heading or more_heading or tagged, action=action, args=args, within=within + more_within))


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
