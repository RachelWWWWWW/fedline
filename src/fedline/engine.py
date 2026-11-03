"""Apply a bill's instructions to statutory text, flagging anything uncertain."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .bill import Bill, Instruction, Target
from .statute import Act, CodeTitle, Document, Node, Text, norm


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
    flags = re.IGNORECASE if ignore_case else 0
    return list(re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, flags))


class Engine:
    def __init__(self, bill: Bill, titles: dict[int, CodeTitle]):
        self.bill = bill
        self.titles = titles
        self.acts: dict[tuple[int, str], Act] = {}
        self.notes: dict[str, Document] = {}
        self.direct: dict[str, Document] = {}
        self.docs: dict[str, Document] = {}
        self.outcome = Outcome(release_points={n: t.release_point for n, t in titles.items()})
        self.global_phrases: list[tuple[str, Document, str]] = []
        self.duplicates: set[tuple[int, tuple[str, str]]] = set()

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
        title = self.titles.get(target.title)
        if title is None or target.usc_section is None:
            return None
        key = (target.title, target.name)
        if key not in self.acts:
            act = title.act_of(target.usc_section)
            if act is None:
                return None
            self.acts[key] = act
            for doc in act.sections.values():
                self.docs.setdefault(doc.key, doc)
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
        else:
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
        target = inst.target
        if target.is_note:
            return self._apply_note(inst)
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
            return self._insert_section(inst, act)
        if section is None:
            if inst.action == "replace" and inst.args["every"]:
                return self._global_in_act(inst, act)
            return self._not_redlined(inst, "the instruction doesn't name a section of the Act")
        doc = act.sections.get(section)
        if doc is None:
            return self._not_redlined(inst, f"Act § {section} wasn't found in the Code")
        return self._apply_in_doc(inst, doc, doc.root, path)

    def _apply_in_doc(self, inst, doc, root, path) -> bool:
        node = self._walk(root, path, inst, doc)
        return self._operate(inst, doc, node)

    def _walk(self, node: Node, path, inst, doc) -> Node:
        for kind, value in path:
            child = node.live_child(value, kind) or node.live_child(value)
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

    def _scope(self, inst: Instruction, node: Node) -> list[Text]:
        if inst.heading:
            return [node.heading]
        return [t for t in node.texts(headings=False) if t.current()]

    def _replace_once(self, inst, doc, node) -> bool:
        old, new = inst.args["old"], inst.args["new"]
        hits = []
        ignore_case = inst.heading
        for text in self._scope(inst, node):
            hits.extend((text, m) for m in _word_matches(old, text.current(), ignore_case))
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
        text.replace(match.start(), match.end(), new, inst.cite)
        return True

    def _replace_every(self, inst, doc, node) -> bool:
        count = self._replace_in_blocks(inst, doc, node)
        if not count:
            raise _NotApplied(f"Not applied: “{inst.args['old']}” wasn't found in {_describe(node, doc)}.", doc, node)
        return True

    def _replace_in_blocks(self, inst, doc, node) -> int:
        """Replace whole-phrase, case-sensitive matches in text but not in headings."""
        old, new = inst.args["old"], inst.args["new"]
        count = 0
        for text in self._scope(inst, node):
            for match in reversed(_word_matches(old, text.current())):
                text.replace(match.start(), match.end(), new, inst.cite)
                count += 1
        if not inst.heading:
            self.global_phrases.append((old, doc, inst.cite))
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

    def _insert_text(self, inst, doc, node) -> bool:
        anchor, new = inst.args["anchor"], inst.args["new"]
        hits = []
        for text in self._scope(inst, node):
            hits.extend((text, m) for m in _word_matches(anchor, text.current()))
        if len(hits) != 1:
            problem = "wasn't found" if not hits else f"appears {len(hits)} times"
            raise _NotApplied(f"Not applied: the anchor “{anchor}” {problem} in {_describe(node, doc)}.", doc, node)
        text, match = hits[0]
        if inst.action == "insert_before":
            text.insert(match.start(), f"{new} ", (inst.cite,))
        else:
            text.insert(match.end(), f" {new}", (inst.cite,))
        return True

    def _insert_before_period(self, inst, doc, node) -> bool:
        new = inst.args["new"]
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

    def _insert_section(self, inst, act: Act) -> bool:
        after = inst.args["values"][-1]
        previous = act.sections.get(after)
        for index, block in enumerate(inst.args["blocks"]):
            number = block.value
            order = (previous.order[0], previous.order[1], 2 + index) if previous else (act.title, (10 ** 9, ""), index)
            doc = Document(
                key=f"new:{act.public_law}:{number}",
                title=f"New § {number} of Pub. L. {act.public_law} (no Code section number yet)",
                root=block,
                basis="new",
                order=order,
            )
            if number in act.sections:
                self._flag(inst.cite, f"Act § {number} already exists when this new section is added.", True, doc)
            act.sections[number] = doc
            self.docs[doc.key] = doc
        return True

    def _check_duplicates(self, inst, doc, node) -> None:
        seen: set[tuple[str, str]] = set()
        for child in node.children:
            if child.deleted:
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
        for phrase, doc, cite in self.global_phrases:
            for node in _walk_nodes(doc.root):
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


def _walk_nodes(node: Node):
    yield node
    for child in node.children:
        yield from _walk_nodes(child)


def _describe(node: Node, doc: Document) -> str:
    return f"{doc.title}, {node.kind} ({node.value})" if node.value and node is not doc.root else doc.title


def apply_bill(bill: Bill, titles: dict[int, CodeTitle]) -> Outcome:
    return Engine(bill, titles).run()
