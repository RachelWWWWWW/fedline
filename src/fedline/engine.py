"""Apply a bill's instructions to statutory text, flagging anything uncertain."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .bill import Bill, Instruction, Target
from .statute import INS, Act, CodeTitle, Document, Node, Segment, Text, norm


@dataclass
class Flag:
    """A problem to show the reader. Blocking flags mean an edit was not applied."""
    cite: str
    message: str
    blocking: bool = True
    doc: Document | None = None
    node: Node | None = None
    number: int = 0


@dataclass
class Unredlined:
    cite: str
    raw: str
    reason: str


@dataclass
class Outcome:
    documents: list[Document] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    unredlined: list[Unredlined] = field(default_factory=list)
    applied: int = 0
    failed: int = 0
    release_points: dict[int, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.applied + self.failed


def _word_matches(phrase: str, text: str, ignore_case: bool = False) -> list[re.Match]:
    """Matches of phrase that don't start or end partway through a word."""
    flags = re.IGNORECASE if ignore_case else 0
    before = r"(?<!\w)" if phrase[:1].isalnum() else ""
    after = r"(?!\w)" if phrase[-1:].isalnum() else ""
    return list(re.finditer(rf"{before}{re.escape(phrase)}{after}", text, flags))


_UNIT_ABBREVIATIONS = {"part": r"pt\.", "title": "title", "subtitle": "subtitle"}


def _unit(name: str) -> tuple[str, str] | None:
    """The part, title or subtitle a lead-in names, as in "Part II of the Federal Power Act"."""
    match = re.match(r"(Part|Title|Subtitle) (\w+) of ", name)
    return (match.group(1).lower(), match.group(2)) if match else None


def _act_name(name: str) -> str:
    """An Act's name without any part, title or subtitle of it: "Part III of the Federal Power Act" is
    the Federal Power Act."""
    return re.sub(r"^(?:(?:Part|Title|Subtitle) \w+ of )?(?:the )?", "", name)

_SUBDIVISION_ACTIONS =("delete_nodes", "replace_nodes", "redesignate", "insert_after_node", "add_at_end")


def _ends_with_inline(node: Node, value: str) -> bool:
    """Whether node's introductory text ends by introducing an inline ({value}), whose subdivisions follow."""
    text = node.body.current().rstrip()
    inline = re.findall(r"(?:^|\s)\((\w+)\) ", text)
    return bool(inline) and inline[-1] == value and text.endswith("—") and bool(node.children)


# Words that end in a period without ending a sentence. Words with a period inside, like "U.S.C", are
# recognized separately.
_ABBREVIATIONS = {"Stat", "Pub", "No", "Nos", "Sec", "Secs", "sec", "secs", "Inc", "Co", "Corp", "Ltd", "Jr", "Sr",
                  "Dr", "Mr", "Mrs", "Ms", "St", "seq", "etc", "al", "v", "ch", "pt", "div", "subch", "cl", "par",
                  "Jan", "Feb", "Mar", "Apr", "Aug", "Sept", "Oct", "Nov", "Dec"}


def _sentences(text: str) -> list[tuple[int, int]]:
    """Spans of the sentences in text, splitting only where a period is followed by a capitalized word."""
    spans = []
    start = 0
    for match in re.finditer(r"[.?!][”’)]*(\s+)(?=[“‘(]?[A-Z])", text):
        word = re.search(r"[\w.]*$", text[start:match.start()]).group(0)
        if "." in word or word in _ABBREVIATIONS or (len(word) == 1 and word.isupper()):
            continue
        spans.append((start, match.start(1)))
        start = match.end()
    spans.append((start, len(text.rstrip())))
    return spans


def _ordinal(n: int) -> str:
    return {-1: "last"}.get(n) or ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
                                   "ninth", "tenth"][n - 1]


def _swap(text: Text, start: int, end: int, new: str, source: str) -> None:
    """Replace a range. A pure deletion takes one neighboring space with it, so no double space is left."""
    if new:
        text.replace(start, end, new, source)
        return
    current = text.current()
    if start > 0 and current[start - 1] == " " and (end == len(current) or current[end] in " ,;:.)"):
        start -= 1
    elif start == 0 and current[end:end + 1] == " ":
        end += 1
    text.strike(start, end, source)


class Engine:
    def __init__(self, bill: Bill, titles: dict[int, CodeTitle]):
        self.bill = bill
        self.titles = titles
        self.acts: dict[tuple[int, str], Act] = {}
        self.notes: dict[str, Document] = {}
        self.direct: dict[str, Document] = {}
        self.docs: dict[str, Document] = {}
        self.outcome = Outcome(release_points={n: t.release_point for n, t in titles.items()})
        self.global_phrases: list[tuple[str, Document, Node, str]] = []
        self.duplicates: set[tuple[int, tuple[str, str]]] = set()
        self.inline: set[tuple[int, str]] = set()
        self.units: dict[str, tuple[str, str] | None] = {}  # the part or title each new section was added to
        self.act_names: dict[str, tuple] = {}  # Act names found in this bill, to the Acts they cite
        self.undo: dict[str, list] | None = None  # document states to restore if an "in each of" fails

    def run(self) -> Outcome:
        for problem in self.bill.problems:
            self.outcome.failed += 1
            self.outcome.unredlined.append(Unredlined(problem.cite, problem.raw, problem.reason))
            self._flag(problem.cite, f"Not applied: {problem.reason}. See the list of instructions not redlined.")
        for instruction in self.bill.instructions:
            try:
                ok = self._apply(instruction)
            except _NotApplied as err:
                self._flag(instruction.cite, str(err), doc=err.doc, node=err.node)
                ok = False
            if ok:
                self.outcome.applied += 1
            else:
                self.outcome.failed += 1
        self._check_leftovers()
        self.outcome.documents = sorted(
            (d for d in self.docs.values() if d.root.changed()), key=lambda d: d.order)
        return self.outcome

    def _flag(self, cite, message, blocking=True, doc=None, node=None) -> None:
        self.outcome.flags.append(Flag(cite, message, blocking, doc, node))

    def _not_redlined(self, inst: Instruction, reason: str) -> bool:
        self.outcome.unredlined.append(Unredlined(inst.cite, inst.raw, reason))
        self._flag(inst.cite, f"Not applied: {reason}. See the list of instructions not redlined.")
        return False

    # Resolving where an instruction applies

    def _act(self, target: Target) -> Act | None:
        name = _act_name(target.name)
        title = self.titles.get(target.title)
        if title is None or target.usc_section is None:
            # Named without a citation, as in "after section 318 (as added by section 2106)": only an Act
            # already found under that name earlier in the bill.
            return self.acts.get(self.act_names.get(name))
        # Keyed by the enacting law, not the bill's name for it, so "the Federal Power Act" and "Part II of
        # the Federal Power Act" share one set of sections and see each other's edits.
        origin = title.origin(target.usc_section)
        if origin is None:
            return None
        key = (target.title, origin[0])
        if key not in self.acts:
            act = title.act_of(target.usc_section)
            # Use the documents already being edited, if another instruction reached them first.
            act.sections = {n: self.docs.setdefault(doc.key, doc) for n, doc in act.sections.items()}
            self.acts[key] = act
        self.act_names.setdefault(name, key)
        return self.acts[key]

    def _note(self, target: Target) -> Document | None:
        key = f"{target.name}|{target.cite}|{target.section}"
        if key in self.notes:
            return self.notes[key]
        doc = None
        if target.public_law:
            for title in self.titles.values():
                doc = title.note_for_public_law(target.public_law, target.section or "")
                if doc:
                    break
        if doc is None and target.usc_section:
            title = self.titles.get(target.title)
            if title is not None:
                doc = title.note_for_act(target.usc_section, target.name)
        if doc is not None:
            doc = self.docs.setdefault(doc.key, doc)
            self.notes[key] = doc
        return doc

    def _direct(self, target: Target) -> Document | None:
        title = self.titles.get(target.title)
        if title is None:
            return None
        doc = title.document(target.usc_section)
        return self.docs.setdefault(doc.key, doc) if doc else None

    def _apply(self, inst: Instruction) -> bool:
        """Apply an instruction. One aimed at several places ("in each of paragraphs (1) and (2)") applies to
        all of them or, if any fails, to none."""
        steps = [i for i, step in enumerate(inst.path) if step[0] == "each"]
        if not steps:
            return self._apply_one(inst)
        if len(steps) > 1:
            return self._not_redlined(inst, "more than one \"in each of\" in an instruction isn't supported")
        i = steps[0]
        marks = len(self.outcome.flags), len(self.outcome.unredlined), len(self.global_phrases)
        self.undo = {}
        try:
            for alternative in inst.path[i][1]:
                one = replace(inst, path=inst.path[:i] + list(alternative) + inst.path[i + 1:])
                if not self._apply_one(one):
                    raise _NotApplied(self.outcome.unredlined[-1].reason)
            return True
        except _NotApplied as err:
            for state in self.undo.values():
                _restore(state)
            del self.outcome.flags[marks[0]:], self.outcome.unredlined[marks[1]:], self.global_phrases[marks[2]:]
            if not str(err).startswith("Not applied:"):
                return self._not_redlined(inst, f"{err} (in one of the places named; none were changed)")
            raise _NotApplied(f"{err} This instruction names several places; none were changed.", err.doc, err.node)
        finally:
            self.undo = None

    def _apply_one(self, inst: Instruction) -> bool:
        target = inst.target
        if target.is_note:
            return self._apply_note(inst)
        if target.chapter:
            return self._apply_chapter(inst)
        if target.name.startswith("title "):
            doc = self._direct(target)
            if doc is None:
                return self._not_redlined(inst, f"{target.name} § {target.usc_section} wasn't found in the Code")
            return self._apply_in_doc(inst, doc, doc.root, target.path + inst.path)
        return self._apply_act(inst)

    def _apply_note(self, inst: Instruction) -> bool:
        doc = self._note(inst.target)
        if doc is None:
            return self._not_redlined(inst, f"{inst.target.name} wasn't found in the Code notes")
        root = doc.root
        path = list(inst.target.path) + inst.path
        section = root.live_child(inst.target.section or "", "section")
        if section is not None:
            root = section
        elif inst.target.section and any(c.kind == "section" for c in root.children):
            return self._not_redlined(inst, f"section {inst.target.section} wasn't found in the Code note")
        return self._apply_in_doc(inst, doc, root, path)

    def _apply_act(self, inst: Instruction) -> bool:
        act = self._act(inst.target)
        if act is None:
            return self._not_redlined(inst, f"{inst.target.name} couldn't be mapped to Code sections")
        path = list(inst.target.path) + inst.path
        section = inst.target.section
        if path and path[0][0] == "section":
            section = path[0][1]
            path = path[1:]
        if inst.action == "redesignate" and inst.args["kind"] == "section" and section is None:
            return self._redesignate_sections(inst, act)
        if inst.action == "insert_after_node" and inst.args["kind"] == "section" and section is None:
            after = inst.args["values"][-1]
            if after not in act.sections:
                return self._not_redlined(inst, f"Act § {after} wasn't found in the Code")
            return self._insert_section(inst, act, act.sections[after], _unit(inst.target.name))
        if inst.action in ("delete_nodes", "replace_nodes") and inst.args["kind"] == "section" and section is None:
            return self._strike_sections(inst, act)
        if (inst.action == "add_at_end" and section is None and not path
                and all(b.kind == "section" for b in inst.args["blocks"])):
            return self._add_sections_at_end(inst, act)
        if section is None:
            if inst.action == "replace" and inst.args["every"]:
                return self._global_in_act(inst, act)
            return self._not_redlined(inst, "the instruction doesn't name a section of the Act")
        doc = act.sections.get(section)
        if doc is None:
            return self._not_redlined(inst, f"Act § {section} wasn't found in the Code")
        return self._apply_in_doc(inst, doc, doc.root, path)

    def _apply_in_doc(self, inst, doc, root, path) -> bool:
        if self.undo is not None and doc.key not in self.undo:
            self.undo[doc.key] = _snapshot(doc.root)
        node = self._walk(root, path, inst, doc)
        return self._operate(inst, doc, node)

    def _walk(self, node: Node, path, inst, doc) -> Node:
        for index, (kind, value) in enumerate(path):
            child = node.live_child(value, kind) or node.live_child(value)
            if child is None and _ends_with_inline(node, value):
                # The Code sometimes prints a paragraph's designation inside the text that introduces it,
                # as in "(1) … and (2) all agencies shall—", and puts (2)'s subdivisions directly under the
                # section. Only operations on those subdivisions can be placed there safely.
                if index == len(path) - 1 and inst.action not in _SUBDIVISION_ACTIONS:
                    raise _NotApplied(f"Not applied: ({value}) has no text of its own in "
                                      f"{_describe(node, doc)}; the Code runs it into the introductory text.",
                                      doc, node)
                if (id(node), value) not in self.inline:
                    self.inline.add((id(node), value))
                    self._flag(inst.cite, f"The Code prints ({value}) inside the introductory text of "
                                          f"{_describe(node, doc)} and puts its subdivisions directly under it. "
                                          "Applied there.", False, doc, node)
                continue
            if child is None:
                raise _NotApplied(
                    f"Not applied: {kind} ({value}) wasn't found under {_describe(node, doc)}.", doc, node)
            node = child
        return node

    # Operations

    def _operate(self, inst: Instruction, doc: Document, node: Node) -> bool:
        action, args = inst.action, inst.args
        if action == "replace":
            if args["every"]:
                return self._replace_every(inst, doc, node)
            return self._replace_once(inst, doc, node)
        if action in ("insert_before", "insert_after"):
            return self._insert_text(inst, doc, node)
        if action == "insert_before_period":
            return self._insert_before_period(inst, doc, node)
        if action == "replace_at_end":
            return self._replace_at_end(inst, doc, node)
        if action == "replace_span":
            return self._replace_span(inst, doc, node)
        if action in ("replace_sentence", "insert_after_sentence"):
            return self._edit_sentence(inst, doc, node)
        if action == "delete_nodes":
            for child in self._children(inst, doc, node, args["values"], args["kind"]):
                child.delete(inst.cite)
            return True
        if action == "redesignate":
            return self._redesignate(inst, doc, node)
        if action == "replace_nodes":
            targets = self._children(inst, doc, node, args["values"], args["kind"])
            position = node.children.index(targets[-1]) + 1
            for child in targets:
                child.delete(inst.cite)
            node.children[position:position] = args["blocks"]
            self._check_duplicates(inst, doc, node)
            return True
        if action == "replace_with_block":
            return self._replace_with_block(inst, doc, node)
        if action == "replace_opening":
            return self._replace_opening(inst, doc, node)
        if action == "rewrite":
            block = args["blocks"][0]
            # A Code section's root carries its Code number, not the Act's, so only its kind can be compared.
            if block.kind != node.kind or (node is not doc.root and block.value != node.value):
                raise _NotApplied(f"Not applied: the replacement is {block.kind} ({block.value}), but it replaces "
                                  f"{_describe(node, doc)}.", doc, node)
            node.rewrite(block, inst.cite)
            self._check_duplicates(inst, doc, node)
            return True
        if action == "add_at_end":
            node.children.extend(args["blocks"])
            self._check_duplicates(inst, doc, node)
            return True
        if action == "insert_after_node":
            targets = self._children(inst, doc, node, args["values"], args["kind"])
            position = node.children.index(targets[-1]) + 1
            node.children[position:position] = args["blocks"]
            self._check_duplicates(inst, doc, node)
            return True
        raise _NotApplied(f"Not applied: no handler for '{action}'.", doc, node)

    def _children(self, inst, doc, node, values, kind) -> list[Node]:
        found = []
        for value in values:
            child = node.live_child(value, kind)
            if child is None:
                raise _NotApplied(
                    f"Not applied: {kind} ({value}) wasn't found under {_describe(node, doc)}.", doc, node)
            found.append(child)
        return found

    def _scope(self, inst: Instruction, doc: Document, node: Node) -> list[tuple[Text, int, int]]:
        """The text an instruction may edit, as (text, start, end) windows into blocks of text."""
        if inst.heading:
            return [(node.heading, 0, len(node.heading.current()))]
        windows = [(t, 0, len(t.current())) for t in node.texts(headings=False) if t.current()]
        for part in inst.within:
            if part[0] == "sentence":
                windows = [self._sentence(self._sentence_base(windows, part[1], doc, node), part[1], doc, node)]
                continue
            where, kind, value = part
            live = [c for c in node.children if not c.deleted]
            child = node.live_child(value, kind)
            after = live[live.index(child) + 1] if child in live[:-1] else None
            if where == "following" and after is not None and after.kind == "continuation":
                text = after.body  # matter between subdivisions
            else:
                edge = (live[0] if where == "preceding" else live[-1]) if live else None
                if child is None or child is not edge:
                    raise _NotApplied(f"Not applied: the matter {where} {kind} ({value}) wasn't found in "
                                      f"{_describe(node, doc)}.", doc, node)
                text = node.body if where == "preceding" else node.tail
            if not text.current():
                raise _NotApplied(f"Not applied: {_describe(node, doc)} has no text {where} {kind} ({value}).",
                                  doc, node)
            windows = [(text, 0, len(text.current()))]
        return windows

    def _sentence_base(self, windows, n, doc, node) -> tuple[Text, int, int]:
        """The window that sentence n is counted in. With subdivisions, that's the introductory text, which
        comes first, so it's only clear when the count stays within it."""
        if len(windows) == 1:
            return windows[0]
        body = node.body.current()
        if n > 0 and body and len(_sentences(body)) >= n:
            return (node.body, 0, len(body))
        raise _NotApplied(f"Not applied: {_describe(node, doc)} has subdivisions, so it isn't clear which text "
                          "the sentence count starts from.", doc, node)

    def _sentence(self, window, n, doc, node) -> tuple[Text, int, int]:
        text, start, end = window
        spans = [(start + a, start + b) for a, b in _sentences(text.current()[start:end])]
        if n > len(spans) or (n == -1 and len(spans) < 2):
            raise _NotApplied(f"Not applied: {_describe(node, doc)} doesn't have a {_ordinal(n)} sentence "
                              f"(it has {len(spans)}).", doc, node)
        return (text, *spans[n - 1 if n > 0 else -1])

    def _hits(self, phrase, windows, ignore_case=False) -> list[tuple[Text, re.Match]]:
        return [(text, m) for text, start, end in windows for m in _word_matches(phrase, text.current(), ignore_case)
                if start <= m.start() and m.end() <= end]

    def _replace_once(self, inst, doc, node) -> bool:
        old, new = inst.args["old"], inst.args["new"]
        hits = self._hits(old, self._scope(inst, doc, node), inst.heading)
        if not hits:
            raise _NotApplied(f"Not applied: “{old}” wasn't found in {_describe(node, doc)}. "
                              "The Code's wording may differ from the Act's.", doc, node)
        if len(hits) > 1:
            raise _NotApplied(f"Not applied: “{old}” appears {len(hits)} times in {_describe(node, doc)}, "
                              "and the instruction doesn't say which one.", doc, node)
        text, match = hits[0]
        if match.group(0) != old:
            self._flag(inst.cite, f"The text “{match.group(0)}” differs in capitalization from the bill's "
                                  f"“{old}”. Applied, using the bill's capitalization for the new text.",
                       False, doc, node)
        _swap(text, match.start(), match.end(), new, inst.cite)
        return True

    def _replace_every(self, inst, doc, node) -> bool:
        count = self._replace_in_blocks(inst, doc, node)
        if not count:
            raise _NotApplied(f"Not applied: “{inst.args['old']}” wasn't found in {_describe(node, doc)}.", doc, node)
        return True

    def _replace_in_blocks(self, inst, doc, node) -> int:
        """Replace whole-phrase, case-sensitive matches in text but not in headings."""
        old, new = inst.args["old"], inst.args["new"]
        hits = self._hits(old, self._scope(inst, doc, node))
        for text, match in reversed(hits):
            _swap(text, match.start(), match.end(), new, inst.cite)
        count = len(hits)
        if not inst.heading and not inst.within:
            # Checked afterwards for near misses, within the provision the replacement covered.
            self.global_phrases.append((old, doc, node, inst.cite))
        return count

    def _global_in_act(self, inst, act: Act) -> bool:
        excluded = inst.args["except"]
        total = 0
        for number, doc in act.sections.items():
            if number == excluded:
                continue
            total += self._replace_in_blocks(inst, doc, doc.root)
        if not total:
            self._flag(inst.cite, f"“{inst.args['old']}” wasn't found in any section of the Act in the Code.")
            return False
        return True

    def _replace_at_end(self, inst, doc, node) -> bool:
        """'By striking "or" at the end', 'the period at the end and inserting "; and"' and the like."""
        old, new, after = inst.args["old"], inst.args["new"], inst.args["after"]
        if inst.within:
            raise _NotApplied("Not applied: an edit at the end of only part of a provision isn't supported.", doc, node)
        blocks = [t for t in node.texts(headings=False) if t.current()]
        text = blocks[-1] if blocks else None
        current = text.current().rstrip() if text else ""
        if not current.endswith(old) or (old[:1].isalnum() and current[-len(old) - 1:-len(old)].isalnum()):
            raise _NotApplied(f"Not applied: {_describe(node, doc)} doesn't end with “{old}”.", doc, node)
        start = len(current) - len(old)
        if after and not current[:start].rstrip().endswith(after):
            raise _NotApplied(f"Not applied: “{old}” at the end of {_describe(node, doc)} doesn't follow "
                              f"“{after}”.", doc, node)
        _swap(text, start, len(current), new, inst.cite)
        return True

    def _replace_span(self, inst, doc, node) -> bool:
        """'By striking "A" and all that follows through "B"', within one block of text."""
        start, end, new = inst.args["start"], inst.args["end"], inst.args["new"]
        windows = self._scope(inst, doc, node)
        hits = self._hits(start, windows)
        if len(hits) != 1:
            problem = "wasn't found" if not hits else f"appears {len(hits)} times"
            raise _NotApplied(f"Not applied: “{start}” {problem} in {_describe(node, doc)}.", doc, node)
        text, match = hits[0]
        limit = next(e for t, s, e in windows if t is text and s <= match.start() and match.end() <= e)
        current = text.current()
        if end is not None:
            ends = _word_matches(end, current[match.end():limit])
            if not ends:
                raise _NotApplied(f"Not applied: “{end}” wasn't found after “{start}” in the same block of text "
                                  f"in {_describe(node, doc)}.", doc, node)
            stop = match.end() + ends[0].end()
        else:
            # "The period at the end" is the end of the window: a sentence, if the instruction names one.
            punct = inst.args["end_punct"]
            blocks = [t for t in node.texts(headings=False) if t.current()]
            stop = len(current[:limit].rstrip())
            if (not inst.within and text is not blocks[-1]) or not current[:stop].endswith(punct):
                raise _NotApplied(f"Not applied: the text from “{start}” to the “{punct}” at the end of "
                                  f"{_describe(node, doc)} spans more than one block.", doc, node)
        _swap(text, match.start(), stop, new, inst.cite)
        return True

    def _replace_with_block(self, inst, doc, node) -> bool:
        """'By striking "X" and inserting the following:' with new subdivisions. Bills use this to break
        existing text into subdivisions: the struck text is replaced by the new ones, and the text that
        followed it continues as the text of the last of them."""
        old, blocks = inst.args["old"], inst.args["blocks"]
        if not old.startswith("("):
            self._split_into(inst, doc, node, old, blocks, at_start=False)
            return True
        # Struck text that begins with a designation, as in "(e)(1) In carrying", restructures that
        # provision in place, adding headings and new levels under it.
        if len(blocks) != 1:
            raise _NotApplied("Not applied: the struck text starts with a designation, but more than one new "
                              "provision replaces it.", doc, node)
        block = blocks[0]
        target = next((n for n in [node] + [c for c in node.children if not c.deleted]
                       if (n.kind, n.value) == (block.kind, block.value)), None)
        if target is None:
            raise _NotApplied(f"Not applied: the new {block.kind} ({block.value}) doesn't match {_describe(node, doc)} "
                              "or any of its subdivisions.", doc, node)
        self._overlay(inst, doc, target, old, block)
        return True

    def _overlay(self, inst, doc, node, old, block) -> None:
        label = node.label.current().strip()
        if not label or not old.startswith(label):
            raise _NotApplied(f"Not applied: “{old}” doesn't start with the designation of {_describe(node, doc)}.",
                              doc, node)
        rest = old[len(label):].lstrip()
        if block.heading.current() and block.heading.current() != node.heading.current():
            node.heading.delete_all(inst.cite)
            node.heading.segments += block.heading.segments
        live = [c for c in node.children if not c.deleted]
        if block.children:
            first = block.children[0]
            if live and (live[0].kind, live[0].value) == (first.kind, first.value):
                if len(block.children) != 1:
                    raise _NotApplied(f"Not applied: the new subdivisions don't line up with those of "
                                      f"{_describe(node, doc)}.", doc, node)
                return self._overlay(inst, doc, live[0], rest, first)
            return self._split_into(inst, doc, node, rest, block.children, at_start=True)
        if not node.body.current().startswith(rest):
            raise _NotApplied(f"Not applied: the text of {_describe(node, doc)} doesn't start with “{rest}”.",
                              doc, node)
        if block.body.current() != rest:
            _swap(node.body, 0, len(rest), block.body.current(), inst.cite)

    def _replace_opening(self, inst, doc, node) -> bool:
        """'By striking the section designation and all that follows through "All agencies" and inserting the
        following:'. The provision's heading and opening text up to that phrase are replaced by the new
        provision's, which may break the rest of the text into subdivisions."""
        block = inst.args["blocks"][0]
        if node.kind != inst.args["unit"] or block.kind != node.kind or (
                node is not doc.root and block.value != node.value):
            raise _NotApplied(f"Not applied: the new {block.kind} ({block.value}) doesn't replace "
                              f"{_describe(node, doc)}.", doc, node)
        end, n = inst.args["end"], inst.args["n"]
        body = node.body.current()
        start, stop = 0, len(body)
        if n:
            spans = _sentences(body)
            if n > len(spans):
                raise _NotApplied(f"Not applied: {_describe(node, doc)} doesn't have a {_ordinal(n)} sentence.",
                                  doc, node)
            start, stop = spans[n - 1]
        ends = [m for m in _word_matches(end, body) if start <= m.start() and m.end() <= stop]
        if not ends:
            raise _NotApplied(f"Not applied: “{end}” wasn't found in the opening text of {_describe(node, doc)}.",
                              doc, node)
        if block.children and any(c.label.before() for c in node.children if not c.deleted):
            raise _NotApplied(f"Not applied: {_describe(node, doc)} already has subdivisions, and the instruction "
                              "doesn't say whether they move under the new ones.", doc, node)
        # A heading the bill supplies replaces the existing one. Without one, a heading in a title that isn't
        # positive law stays: it's an editors' catchline, not text of the Act that the bill strikes. Elsewhere
        # the heading is enacted text inside the struck span, so it goes.
        title = self.titles.get(doc.order[0]) if doc.basis == "code" else None
        catchline = title is not None and not title.positive_law
        if block.heading.current() and block.heading.current() != node.heading.current():
            node.heading.delete_all(inst.cite)
            node.heading.segments += block.heading.segments
        elif not block.heading.current() and node.heading.current() and not catchline:
            node.heading.delete_all(inst.cite)
            self._flag(inst.cite, f"The heading of {_describe(node, doc)} is struck: it's enacted text within the "
                                  "struck span, and the new text has no heading. Check this one.", False, doc, node)
        node.body.strike(0, ends[0].end(), inst.cite)
        rest = node.body.cut(0)
        node.body.segments += block.body.segments
        if block.children:
            last = block.children[-1]
            while last.children:
                last = last.children[-1]
            last.body.segments += rest.segments
            node.children[0:0] = block.children
        else:
            node.body.segments += rest.segments
        self._check_duplicates(inst, doc, node)
        return True

    def _split_into(self, inst, doc, node, old, blocks, at_start) -> None:
        """Strike old from node's introductory text and put blocks in its place as new subdivisions, moving
        the text that followed old into the last of them. New provisions at node's own level, as in 'in
        paragraph (1), by striking "to promote" and inserting (2) to promote', follow node instead."""
        if any(c.label.before() for c in node.children if not c.deleted):
            raise _NotApplied(f"Not applied: {_describe(node, doc)} already has subdivisions, and the instruction "
                              "doesn't say whether they move under the new ones.", doc, node)
        siblings = all(b.kind == node.kind for b in blocks)
        parent = next((p for p in _walk_nodes(doc.root) if node in p.children), None) if siblings else None
        if any(b.kind == node.kind for b in blocks) and (parent is None or node.children):
            raise _NotApplied(f"Not applied: the new {blocks[0].kind} can't go inside {_describe(node, doc)}.",
                              doc, node)
        text = node.body
        windows = [w for w in self._scope(inst, doc, node) if w[0] is text]
        hits = self._hits(old, windows)
        if at_start:
            hits = [(t, m) for t, m in hits if m.start() == 0]
        if len(hits) != 1:
            problem = "wasn't found" if not hits else f"appears {len(hits)} times"
            raise _NotApplied(f"Not applied: “{old}” {problem} in the text of {_describe(node, doc)}.", doc, node)
        match = hits[0][1]
        text.strike(match.start(), match.end(), inst.cite)
        rest = text.cut(match.start())
        last = blocks[-1]
        while last.children:
            last = last.children[-1]
        last.body.segments += rest.segments
        if parent is not None:
            position = parent.children.index(node) + 1
            parent.children[position:position] = blocks
            self._check_duplicates(inst, doc, parent)
            return
        node.children[0:0] = blocks
        self._check_duplicates(inst, doc, node)

    def _edit_sentence(self, inst, doc, node) -> bool:
        """'By striking the second sentence', 'by inserting after the first sentence the following: "…"'."""
        n = inst.args["n"]
        text, start, end = self._sentence(self._sentence_base(self._scope(inst, doc, node), n, doc, node), n, doc, node)
        new = inst.args["new"]
        if inst.action == "insert_after_sentence":
            text.insert(end, f" {new}", (inst.cite,))
        else:
            _swap(text, start, end, new, inst.cite)
        return True

    def _insert_text(self, inst, doc, node) -> bool:
        anchor, new = inst.args["anchor"], inst.args["new"]
        hits = self._hits(anchor, self._scope(inst, doc, node))
        if len(hits) != 1:
            problem = "wasn't found" if not hits else f"appears {len(hits)} times"
            raise _NotApplied(f"Not applied: the anchor “{anchor}” {problem} in {_describe(node, doc)}.", doc, node)
        text, match = hits[0]
        if inst.action == "insert_before":
            text.insert(match.start(), f"{new} ", (inst.cite,))
        else:
            text.insert(match.end(), new if new[0] in ",;:.)" else f" {new}", (inst.cite,))
        return True

    def _insert_before_period(self, inst, doc, node) -> bool:
        new = inst.args["new"]
        if inst.within:
            raise _NotApplied("Not applied: an edit at the end of only part of a provision isn't supported.", doc, node)
        blocks = [t for t in node.texts(headings=False) if t.current()]
        if not blocks or not blocks[-1].current().endswith("."):
            raise _NotApplied(f"Not applied: {_describe(node, doc)} doesn't end with a period.", doc, node)
        text = blocks[-1]
        if new in text.current():
            raise _NotApplied(f"Not applied: {_describe(node, doc)} already contains “{new}”, possibly as an "
                              "editorial addition by the Code's editors. Check the enacted text.", doc, node)
        spaced = new if new[0] in ",;" else f" {new}"
        text.insert(len(text.current()) - 1, spaced, (inst.cite,))
        return True

    def _redesignate(self, inst, doc, node) -> bool:
        kind, old, new = inst.args["kind"], inst.args["old"], inst.args["new"]
        if len(old) != len(new):
            raise _NotApplied("Not applied: the old and new designations don't line up.", doc, node)
        targets = self._children(inst, doc, node, old, kind)
        for child, value in zip(targets, new):
            child.renumber(value, inst.cite)
        self._check_duplicates(inst, doc, node)
        return True

    def _redesignate_sections(self, inst, act: Act) -> bool:
        old, new = inst.args["old"], inst.args["new"]
        moved = {}
        missing = []
        for before, after in zip(old, new):
            doc = act.sections.pop(before, None)
            if doc is None:
                missing.append(before)
            else:
                moved[after] = doc
        act.sections.update(moved)
        message = f"Act sections {', '.join(old)} become {', '.join(new)}. Code section numbers are assigned by " \
                  "the Code's editors, so no change is shown."
        if missing:
            message += f" Act § {', '.join(missing)} has no section in the Code."
        self._flag(inst.cite, message, False)
        return True

    def _strike_sections(self, inst, act: Act) -> bool:
        """'By striking section 111', or 'by striking section 209 and inserting the following: SEC. 209. …'"""
        docs = []
        for number in inst.args["values"]:
            doc = act.sections.get(number)
            if doc is None:
                return self._not_redlined(inst, f"Act § {number} wasn't found in the Code")
            docs.append(doc)
        for doc in docs:
            doc.root.delete(inst.cite)
        for number in inst.args["values"]:
            del act.sections[number]
        if inst.action == "replace_nodes":
            return self._insert_section(inst, act, docs[-1], _unit(inst.target.name))
        return True

    def _apply_chapter(self, inst) -> bool:
        """'Chapter 3027 of title 54, United States Code, is amended by adding at the end the following:'"""
        target = inst.target
        title = self.titles.get(target.title)
        sections = title.chapter_sections(target.chapter) if title else []
        if inst.action != "add_at_end" or inst.path or not all(b.kind == "section" for b in inst.args["blocks"]):
            return self._not_redlined(inst, "only adding sections at the end of a Code chapter is supported")
        if not sections:
            return self._not_redlined(inst, f"{target.name} wasn't found in the Code")
        last = self._direct(Target(title=target.title, usc_section=sections[-1]))
        for index, block in enumerate(inst.args["blocks"]):
            number = block.value
            block.label = Text(f"§ {number}.", INS, (inst.cite,))  # a Code section, not an Act's "Sec."
            doc = Document(key=f"new:{target.title}usc{number}", title=f"New {target.title} U.S.C. § {number}",
                           root=block, basis="new", order=(last.order[0], last.order[1], 2 + index))
            if title.document(number) is not None or doc.key in self.docs:
                self._flag(inst.cite, f"{target.title} U.S.C. § {number} already exists.", True, doc)
            self.docs[doc.key] = doc
        return True

    def _add_sections_at_end(self, inst, act: Act) -> bool:
        """'Part II of the Federal Power Act is amended by adding at the end the following: SEC. 225. …'"""
        unit = _unit(inst.target.name)
        docs = [d for d in act.sections.values() if unit is None or self._unit_of(d, unit[0]) == unit[1]]
        if not docs:
            return self._not_redlined(inst, f"no section of {inst.target.name} was found in the Code")
        return self._insert_section(inst, act, max(docs, key=lambda d: d.order), unit)

    def _unit_of(self, doc: Document, word: str) -> str | None:
        """Which part, title or subtitle of its Act a section is in, from its source credit."""
        if doc.key in self.units:
            found = self.units[doc.key]
            return found[1] if found and found[0] == word else None
        enactment = doc.credit.split("§")[0]
        match = re.search(rf"\b{_UNIT_ABBREVIATIONS[word]} (\w+),", enactment)
        return match.group(1) if match else None

    def _insert_section(self, inst, act: Act, previous: Document | None, unit) -> bool:
        # A Public Law number identifies the Act exactly; older laws are better known by name.
        name = act.law if act.law.startswith("Pub. L.") else "the " + _act_name(inst.target.name)
        for index, block in enumerate(inst.args["blocks"]):
            number = block.value
            # After the previous section and its notes, and after any new sections already placed after it.
            order = ((previous.order[0], previous.order[1], max(previous.order[2], 1) + 1 + index) if previous
                     else (act.title, (10 ** 9, ""), index))
            doc = Document(
                key=f"new:{act.law}:{number}",
                title=f"New § {number} of {name} (no Code section number yet)",
                root=block,
                basis="new",
                order=order,
            )
            if number in act.sections:
                self._flag(inst.cite, f"Act § {number} already exists when this new section is added.", True, doc)
            act.sections[number] = doc
            self.docs[doc.key] = doc
            self.units[doc.key] = unit
        return True

    def _check_duplicates(self, inst, doc, node) -> None:
        seen: set[tuple[str, str]] = set()
        for child in node.children:
            if child.deleted or child.kind == "continuation":
                continue
            key = (child.kind, child.value)
            if key in seen and (id(node), key) not in self.duplicates:
                self.duplicates.add((id(node), key))
                self._flag(inst.cite, f"The result has two {child.kind}s designated ({child.value}) in "
                                      f"{_describe(node, doc)}. This looks like a drafting problem in the bill. "
                                      "Applied as written, with no correction.", False, doc, child)
            seen.add(key)

    def _check_leftovers(self) -> None:
        """After all edits, report text that a global replacement may have been meant to reach."""
        seen_headings = set()
        spans: dict[int, list[tuple[int, int]]] = {}
        for phrase, doc, scope, cite in self.global_phrases:
            for node in _walk_nodes(scope):
                if node.deleted:
                    continue
                heading = node.heading.current()
                if _word_matches(phrase, heading, True) and id(node) not in seen_headings:
                    seen_headings.add(id(node))
                    self._flag(cite, f"Left unchanged: the heading “{norm(heading)}” contains “{phrase}”. "
                                     "“Each place it appears” replacements skip headings, because the Act prints "
                                     "headings in capitals and the bill amends headings separately. Check this one.",
                               False, doc, node)
                for text in (node.body, node.tail):
                    current = text.current()
                    for match in re.finditer(re.escape(phrase), current, re.IGNORECASE):
                        taken = spans.setdefault(id(text), [])
                        if any(match.start() < end and start < match.end() for start, end in taken):
                            continue
                        taken.append(match.span())
                        begin = max(0, match.start() - 25)
                        self._flag(cite, f"Left unchanged: “…{current[begin:match.end() + 15]}…” contains the "
                                         f"struck phrase “{phrase}” only as part of other wording or in different "
                                         "capitalization. Check whether the bill intends to reach it.",
                                   False, doc, node)


class _NotApplied(Exception):
    def __init__(self, message: str, doc: Document | None = None, node: Node | None = None):
        super().__init__(message)
        self.doc = doc
        self.node = node


def _snapshot(root: Node) -> list:
    """Everything an edit can change in a document's tree, to put back with _restore."""
    copy = lambda text: [Segment(s.text, s.status, s.sources) for s in text.segments]
    return [(n, list(n.children), n.deleted, n.value, [copy(t) for t in (n.label, n.heading, n.body, n.tail)])
            for n in _walk_nodes(root)]


def _restore(state: list) -> None:
    for node, children, deleted, value, texts in state:
        node.children, node.deleted, node.value = children, deleted, value
        for text, segments in zip((node.label, node.heading, node.body, node.tail), texts):
            text.segments = segments


def _walk_nodes(node: Node):
    yield node
    for child in node.children:
        yield from _walk_nodes(child)


def _describe(node: Node, doc: Document) -> str:
    return f"{doc.title}, {node.kind} ({node.value})" if node.value and node is not doc.root else doc.title


def apply_bill(bill: Bill, titles: dict[int, CodeTitle]) -> Outcome:
    return Engine(bill, titles).run()
