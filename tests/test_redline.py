"""End-to-end and unit tests, using H.R. 10453 and the Code sections it amends."""

import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from xml.etree import ElementTree as ET

from fedline.bill import _designators, parse_bill
from fedline.cli import redline
from fedline.docx_writer import write_docx
from fedline.bill import Instruction, Target
from fedline.engine import Engine, _NotApplied, _sentences, _word_matches
from fedline.sources import SourceError, parse_bill_url
from fedline.statute import CodeTitle, DEL, INS, Node, Text, credit_origin

FIXTURES = Path(__file__).parent / "fixtures"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def run_bill():
    bill_xml = (FIXTURES / "BILLS-119hr10453ih.xml").read_bytes()
    title = CodeTitle((FIXTURES / "usc47_s1601-1609.xml").read_bytes())
    return redline(bill_xml, {47: title})


def marked(text: Text) -> str:
    out = ""
    for seg in text.segments:
        out += {"orig": seg.text, DEL: f"[-{seg.text}-]", INS: f"[+{seg.text}+]"}[seg.status]
    return out


def find_doc(outcome, title):
    return next(d for d in outcome.documents if d.title == title)


class TextTests(unittest.TestCase):
    def test_replace_keeps_both_versions(self):
        text = Text("one two three")
        text.replace(4, 7, "2", "§ 1")
        self.assertEqual(text.before(), "one two three")
        self.assertEqual(text.current(), "one 2 three")
        self.assertEqual(marked(text), "one [-two-][+2+] three")

    def test_replacing_inserted_text_nets_out_and_keeps_both_citations(self):
        text = Text("(7)")
        text.replace(0, 3, "(8)", "§ 1")
        text.replace(0, 3, "(9)", "§ 2")
        self.assertEqual(marked(text), "[-(7)-][+(9)+]")
        self.assertEqual(text.segments[1].sources, ("§ 1", "§ 2"))

    def test_deleting_all_drops_earlier_insertions(self):
        text = Text("old words")
        text.replace(0, 3, "new", "§ 1")
        text.delete_all("§ 2")
        self.assertEqual(text.current(), "")
        self.assertEqual(text.before(), "old words")
        self.assertTrue(all(s.sources == ("§ 2",) for s in text.segments))


class MatchingTests(unittest.TestCase):
    def test_whole_phrases_only(self):
        phrase = "communications equipment or service"
        self.assertEqual(_word_matches(phrase, "covered communications equipment or services"), [])
        self.assertEqual(_word_matches(phrase, "telecommunications equipment or service"), [])
        self.assertEqual(len(_word_matches(phrase, "any communications equipment or service, if")), 1)

    def test_phrases_starting_with_punctuation_can_follow_a_word(self):
        self.assertEqual(len(_word_matches(", including x", "facilities, including x (other)")), 1)


class FootnoteTests(unittest.TestCase):
    XML = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>30</docNumber></meta><main>'
           '<section identifier="/us/usc/t30/s352"><num value="352">§ 352.</num><content>Under the Act '
           '(sec. 1611),<ref class="footnoteRef" idref="fn1">1</ref><note type="footnote" id="fn1"><num>1</num> '
           'See References in Text note below.</note> all deposits. Then the Secretary acts.</content>'
           '<sourceCredit>(Aug. 7, 1947, ch. 513, § 3.)</sourceCredit></section></main></uscDoc>').encode()

    def test_editorial_footnotes_are_left_out_of_the_text(self):
        doc = CodeTitle(self.XML).document("352")
        self.assertEqual(doc.root.body.current(), "Under the Act (sec. 1611), all deposits. Then the Secretary acts.")
        self.assertEqual(doc.source_text, "§352.UndertheAct(sec.1611),alldeposits.ThentheSecretaryacts.")


class CreditTests(unittest.TestCase):
    def test_public_law(self):
        self.assertEqual(credit_origin("(Pub. L. 104–104, § 3, Feb. 8, 1996, 110 Stat. 56.)"),
                         ("Pub. L. 104-104", "3"))

    def test_titles_are_skipped(self):
        credit = "(Pub. L. 91–190, title I, § 107, as added Pub. L. 118–5, div. C, title III, § 321(b), June 3, 2023)"
        self.assertEqual(credit_origin(credit), ("Pub. L. 91-190", "107"))

    def test_divisions_stay_with_the_law(self):
        credit = "(Pub. L. 116–260, div. Z, title III, § 3102, Dec. 27, 2020, 134 Stat. 2514.)"
        self.assertEqual(credit_origin(credit), ("Pub. L. 116-260, div. Z", "3102"))

    def test_chapter_laws(self):
        credit = "(June 10, 1920, ch. 285, pt. II, § 216, as added Pub. L. 109–58, title XII, § 1221(a), Aug. 8, 2005)"
        self.assertEqual(credit_origin(credit), ("Act of June 10, 1920, ch. 285", "216"))
        self.assertEqual(credit_origin("(Feb. 25, 1920, ch. 85, § 17, 41 Stat. 443.)"),
                         ("Act of Feb. 25, 1920, ch. 85", "17"))

    def test_revised_statutes(self):
        self.assertEqual(credit_origin("(R.S. § 2337; Pub. L. 86–390, Mar. 18, 1960, 74 Stat. 7.)"), ("R.S.", "2337"))


class DesignatorTests(unittest.TestCase):
    def test_ranges_expand_in_each_numbering_style(self):
        self.assertEqual(_designators("(2) through (6)", "paragraph"), ["2", "3", "4", "5", "6"])
        self.assertEqual(_designators("(a) through (c)", "subsection"), ["a", "b", "c"])
        self.assertEqual(_designators("(iii) through (vi)", "clause"), ["iii", "iv", "v", "vi"])
        self.assertEqual(_designators("(II) through (IV)", "subclause"), ["II", "III", "IV"])
        self.assertEqual(_designators("(1), (3) through (5), and (7)", "paragraph"), ["1", "3", "4", "5", "7"])
        self.assertEqual(_designators("5 through 7", "section"), ["5", "6", "7"])

    def test_ranges_it_cannot_expand_are_refused(self):
        self.assertIsNone(_designators("(aa) through (cc)", "item"))
        self.assertIsNone(_designators("(5A) through (7)", "paragraph"))


def parse_sections(sections_xml: str):
    return parse_bill(f"<bill><legis-body>{sections_xml}</legis-body></bill>".encode())


class ParserTests(unittest.TestCase):
    def test_lead_in_locators_apply_to_nested_instructions(self):
        bill = parse_sections(
            "<section><enum>2.</enum><text>Section 5 of the Foo Act (42 U.S.C. 100) is amended, in subsection (a)—"
            "</text><paragraph><enum>(1)</enum><text>by striking <quote>x</quote> and inserting <quote>y</quote>;"
            "</text></paragraph><paragraph><enum>(2)</enum><text>in paragraph (1) (as so designated), by striking "
            "<quote>p</quote> and inserting <quote>q</quote>.</text></paragraph></section>")
        self.assertEqual(bill.problems, [])
        self.assertEqual([i.path for i in bill.instructions],
                         [[("subsection", "a")], [("subsection", "a"), ("paragraph", "1")]])

    def test_lead_in_comma_before_the_action(self):
        bill = parse_sections(
            "<section><enum>2.</enum><text>Section 5 of the Foo Act (42 U.S.C. 100) is amended, in subsection (b), "
            "by striking <quote>x</quote> and inserting <quote>y</quote>.</text></section>")
        self.assertEqual(bill.problems, [])
        self.assertEqual(bill.instructions[0].path, [("subsection", "b")])

    def test_strike_patterns(self):
        def action(text):
            bill = parse_sections(f"<section><enum>2.</enum><text>Section 5 of the Foo Act (42 U.S.C. 100) is "
                                  f"amended {text}</text></section>")
            self.assertEqual(bill.problems, [])
            return bill.instructions[0].action, bill.instructions[0].args

        self.assertEqual(action("by striking <quote>as needed</quote>."),
                         ("replace", {"old": "as needed", "new": "", "every": False, "except": None}))
        self.assertEqual(action("by striking <quote>; and</quote> and inserting a period."),
                         ("replace", {"old": "; and", "new": ".", "every": False, "except": None}))
        self.assertEqual(action("by striking <quote>and</quote> after the semicolon at the end."),
                         ("replace_at_end", {"old": "and", "after": ";", "new": ""}))
        self.assertEqual(action("by striking the period at the end and inserting <quote>; and</quote>."),
                         ("replace_at_end", {"old": ".", "after": None, "new": "; and"}))
        self.assertEqual(action("by striking <quote>A</quote> and all that follows through the period at the end "
                                "and inserting <quote>B.</quote>."),
                         ("replace_span", {"start": "A", "end": None, "end_punct": ".", "new": "B."}))

    def test_within_locators(self):
        bill = parse_sections(
            "<section><enum>2.</enum><text>Section 5 of the Foo Act (42 U.S.C. 100) is amended—</text>"
            "<paragraph><enum>(1)</enum><text>in the matter preceding paragraph (1), in the second sentence—</text>"
            "<subparagraph><enum>(A)</enum><text>by striking <quote>x</quote> and inserting <quote>y</quote>; and"
            "</text></subparagraph></paragraph><paragraph><enum>(2)</enum><text>in subsection (b), in the "
            "undesignated matter following clause (v), by striking the last sentence.</text></paragraph></section>")
        self.assertEqual(bill.problems, [])
        first, second = bill.instructions
        self.assertEqual(first.within, [("preceding", "paragraph", "1"), ("sentence", 2)])
        self.assertEqual((second.path, second.within, second.action, second.args),
                         ([("subsection", "b")], [("following", "clause", "v")], "replace_sentence",
                          {"n": -1, "new": ""}))

    def test_amended_to_read_as_follows(self):
        bill = parse_sections(
            "<section><enum>2.</enum><text>Section 6 of the Foo Act (42 U.S.C. 101) is amended to read as follows:"
            "</text><quoted-block><section><enum>6.</enum><header>New heading</header><text>New text.</text>"
            "</section></quoted-block></section>")
        self.assertEqual(bill.problems, [])
        self.assertEqual(bill.instructions[0].action, "rewrite")


def operate(action: str, body: str, within=(), **args) -> Text:
    """Apply one parsed operation to a section with the given text and return that text."""
    node = Node("section", "1", body=Text(body))
    doc = SimpleNamespace(title="Test § 1", root=node)
    inst = Instruction(cite="§ 9", raw="", target=Target(), path=[], action=action, args=args, within=list(within))
    Engine(SimpleNamespace(), {})._operate(inst, doc, node)
    return node.body


class TextEditTests(unittest.TestCase):
    def test_pure_deletion_takes_one_space(self):
        self.assertEqual(marked(operate("replace", "audits as needed; and", old="as needed", new="", every=False)),
                         "audits[- as needed-]; and")
        self.assertEqual(marked(operate("replace", "shall ensure", old="shall", new="", every=False)),
                         "[-shall -]ensure")

    def test_word_at_the_end(self):
        self.assertEqual(marked(operate("replace_at_end", "law; or", old="or", new="", after=None)), "law;[- or-]")
        self.assertEqual(marked(operate("replace_at_end", "law; and", old="and", new="", after=";")), "law;[- and-]")
        with self.assertRaises(_NotApplied):
            operate("replace_at_end", "a door", old="or", new="", after=None)
        with self.assertRaises(_NotApplied):
            operate("replace_at_end", "law, and", old="and", new="", after=";")

    def test_inserted_punctuation_attaches_to_the_anchor(self):
        self.assertEqual(marked(operate("insert_after", "per site, respectively", anchor="respectively",
                                        new=", except mill sites")), "per site, respectively[+, except mill sites+]")

    def test_punctuation_at_the_end(self):
        self.assertEqual(marked(operate("replace_at_end", "this title.", old=".", new="; and", after=None)),
                         "this title[-.-][+; and+]")

    def test_all_that_follows_through(self):
        self.assertEqual(marked(operate("replace_span", "shall conform to State practice.", start="shall conform",
                                        end=None, end_punct=".", new="shall follow rule 71.1.")),
                         "[-shall conform to State practice.-][+shall follow rule 71.1.+]")
        self.assertEqual(marked(operate("replace_span", "Except as provided, if it finds that, the", start="Except",
                                        end="finds that", end_punct=None, new="Except")),
                         "[-Except as provided, if it finds that-][+Except+], the")


class InlineParagraphTests(unittest.TestCase):
    """The Code prints NEPA § 102(1) and (2) inside 42 U.S.C. § 4332's introduction, with (2)'s subparagraphs
    directly under the section."""

    def setUp(self):
        self.root = Node("section", "4332", label=Text("§ 4332."), body=Text(
            "The Congress directs that: (1) the policies shall be interpreted, and (2) all agencies shall—"),
            children=[Node("subparagraph", "A", label=Text("(A)"), body=Text("utilize an approach;")),
                      Node("subparagraph", "B", label=Text("(B)"), body=Text("identify methods."))])
        self.doc = SimpleNamespace(title="42 U.S.C. § 4332", root=self.root)
        self.engine = Engine(SimpleNamespace(), {})

    def apply(self, path, action, **args):
        inst = Instruction(cite="§ 9", raw="", target=Target(), path=path, action=action, args=args)
        node = self.engine._walk(self.root, path, inst, self.doc)
        return self.engine._operate(inst, self.doc, node)

    def test_subdivisions_of_the_inline_paragraph_are_found(self):
        self.apply([("subsection", "2"), ("subparagraph", "B")], "replace_at_end", old=".", new=";", after=None)
        self.assertEqual(marked(self.root.children[1].body), "identify methods[-.-][+;+]")
        self.assertEqual([f.blocking for f in self.engine.outcome.flags], [False])

    def test_only_the_last_inline_designation_owns_the_subdivisions(self):
        with self.assertRaises(_NotApplied):
            self.apply([("subsection", "1"), ("subparagraph", "A")], "replace", old="an", new="a", every=False)

    def test_text_edits_to_the_inline_paragraph_itself_are_refused(self):
        with self.assertRaises(_NotApplied):
            self.apply([("subsection", "2")], "replace", old="agencies", new="bodies", every=False)


class SentenceTests(unittest.TestCase):
    TEXT = ("The Secretary may act under 30 U.S.C. 22 et seq. and Pub. L. 109–58. The fee is $100. "
            "Mr. Smith shall pay it to the U.S. Treasury.")

    def test_splits_only_at_sentence_ends(self):
        self.assertEqual([self.TEXT[a:b] for a, b in _sentences(self.TEXT)], [
            "The Secretary may act under 30 U.S.C. 22 et seq. and Pub. L. 109–58.",
            "The fee is $100.",
            "Mr. Smith shall pay it to the U.S. Treasury."])

    def test_edits_are_limited_to_the_named_sentence(self):
        text = "The fee is due. The fee is $100."
        self.assertEqual(marked(operate("replace", text, [("sentence", 2)], old="fee", new="charge", every=False)),
                         "The fee is due. The [-fee-][+charge+] is $100.")
        with self.assertRaises(_NotApplied):
            operate("replace", text, [("sentence", 3)], old="fee", new="charge", every=False)

    def test_period_at_the_end_of_a_sentence(self):
        text = "Lands under the Act (36 Stat. 961). Secretary means the Secretary."
        self.assertEqual(marked(operate("replace_span", text, [("sentence", 1)], start="(36 Stat.", end=None,
                                        end_punct=".", new="(16 U.S.C. 552).")),
                         "Lands under the Act [-(36 Stat. 961).-][+(16 U.S.C. 552).+] Secretary means the Secretary.")

    def test_strike_and_insert_after_a_sentence(self):
        text = "One. Two. Three."
        self.assertEqual(marked(operate("replace_sentence", text, n=2, new="")), "One.[- Two.-] Three.")
        self.assertEqual(marked(operate("insert_after_sentence", text, n=1, new="New.")), "One.[+ New.+] Two. Three.")


class MatterTests(unittest.TestCase):
    def setUp(self):
        self.node = Node("paragraph", "1", body=Text("The Secretary shall—"),
                         children=[Node("subparagraph", "A", body=Text("act; and")),
                                   Node("subparagraph", "B", body=Text("report,"))],
                         tail=Text("as the Secretary determines."))
        self.doc = SimpleNamespace(title="Test", root=self.node)

    def apply(self, within, **args):
        inst = Instruction(cite="§ 9", raw="", target=Target(), path=[], action="replace",
                           args={"every": False, **args}, within=within)
        Engine(SimpleNamespace(), {})._operate(inst, self.doc, self.node)

    def test_matter_preceding_and_following(self):
        self.apply([("preceding", "subparagraph", "A")], old="Secretary", new="Administrator")
        self.apply([("following", "subparagraph", "B")], old="Secretary", new="Administrator")
        self.assertEqual(marked(self.node.body), "The [-Secretary-][+Administrator+] shall—")
        self.assertEqual(marked(self.node.tail), "as the [-Secretary-][+Administrator+] determines.")

    def test_matter_must_be_next_to_the_named_subdivision(self):
        with self.assertRaises(_NotApplied):
            self.apply([("preceding", "subparagraph", "B")], old="Secretary", new="Administrator")
        with self.assertRaises(_NotApplied):
            self.apply([("following", "subparagraph", "A")], old="Secretary", new="Administrator")

    def test_sentences_need_a_single_block(self):
        with self.assertRaises(_NotApplied):
            self.apply([("sentence", 1)], old="Secretary", new="Administrator")


class RewriteTests(unittest.TestCase):
    def test_rewrite_keeps_the_designation_and_replaces_the_rest(self):
        old = Node("section", "4336c", label=Text("§ 4336c."), heading=Text("Old"), body=Text("Old text."),
                   children=[Node("subsection", "a", label=Text("(a)"), body=Text("Gone."))])
        new = Node("section", "108", label=Text("Sec. 108.", INS), heading=Text("New", INS),
                   body=Text("New text.", INS))
        old.rewrite(new, "§ 9")
        self.assertEqual(old.label.current(), "§ 4336c.")
        self.assertEqual((old.heading.current(), old.body.current()), ("New", "New text."))
        self.assertTrue(old.children[0].deleted)
        self.assertEqual(old.before_text(), "§ 4336c.OldOld text.(a)Gone.")


class UrlTests(unittest.TestCase):
    def test_parses_bill_urls(self):
        ref = parse_bill_url("https://www.congress.gov/bill/119th-congress/house-bill/10453/text")
        self.assertEqual((ref.congress, ref.bill_type, ref.number, ref.version), (119, "hr", 10453, None))
        self.assertEqual(ref.filename, "HR10453_119")

    def test_parses_a_named_version(self):
        ref = parse_bill_url("https://www.congress.gov/bill/119th-congress/senate-bill/5/text/rs?format=txt")
        self.assertEqual((ref.bill_type, ref.version), ("s", "rs"))

    def test_rejects_other_urls(self):
        with self.assertRaises(SourceError):
            parse_bill_url("https://example.com/bill/119th-congress/house-bill/1")


class Hr10453Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bill, cls.outcome, cls.passed = run_bill()
        cls.s1601 = find_doc(cls.outcome, "47 U.S.C. § 1601").root
        cls.s1608 = find_doc(cls.outcome, "47 U.S.C. § 1608").root

    def child(self, node, value):
        return node.live_child(value)

    def test_all_verification_checks_pass(self):
        self.assertEqual(len(self.passed), 3, self.passed)
        self.assertFalse([f.message for f in self.outcome.flags if "Verification failed" in f.message])

    def test_only_the_bracketed_note_edit_is_not_applied(self):
        blocking = [f for f in self.outcome.flags if f.blocking]
        self.assertEqual([f.cite for f in blocking], ["§ 2(b)(2)(D)"])
        self.assertEqual(self.outcome.total, 35)
        self.assertEqual(self.outcome.applied, 34)

    def test_section_4_is_excluded_from_global_replacements(self):
        self.assertNotIn("47 U.S.C. § 1603", [d.title for d in self.outcome.documents])

    def test_global_replacement_handles_singular_and_plural(self):
        a = self.child(self.s1601, "a")
        self.assertEqual(a.body.current().split("list of ")[1],
                         "covered information and communications technology or services.")
        b = self.child(self.s1601, "b")
        self.assertIn("any information and communications technology or service, if and only if "
                      "such technology or service—", b.body.current())

    def test_headings_are_amended_only_when_the_bill_says_so(self):
        self.assertIn("information and communications technology or services posing", self.s1601.heading.current())
        s1604 = find_doc(self.outcome, "47 U.S.C. § 1604").root
        self.assertEqual(s1604.heading.current(), "Reports on covered communications equipment or services")
        self.assertTrue(any("Reports on covered" in f.message for f in self.outcome.flags))

    def test_stacked_edits_show_the_net_result_with_both_citations(self):
        new = self.child(self.s1608, "6")
        self.assertEqual(new.heading.current(), "Information and communications technology or services")
        self.assertEqual(marked(new.label), "[+(6)+]")
        self.assertEqual(new.label.segments[0].sources, ("§ 2(a)(5)(D)", "§ 3(b)(2)(B)"))

    def test_as_so_redesignated_targets_the_new_number(self):
        covered = self.child(self.s1608, "4")
        self.assertEqual(covered.heading.current(), "Covered information and communications technology or service")
        self.assertEqual(marked(covered.label), "[-(5)-][+(4)+]")

    def test_renumbering_changes_only_the_number(self):
        person = self.child(self.s1608, "7")
        self.assertEqual(person.heading.current(), "Person")
        self.assertEqual(marked(person.label), "[-(8)-][+(7)+]")
        self.assertFalse(person.body.changed())

    def test_replaced_paragraph_is_struck_and_reinserted(self):
        d2 = next(c for c in self.child(self.s1601, "d").children if c.value == "2" and not c.deleted)
        self.assertEqual(d2.heading.current(), "Monitoring of determinations")
        self.assertEqual([c.value for c in d2.children], ["A", "B"])

    def test_duplicate_subsection_is_kept_and_flagged_once(self):
        designated = [c.value for c in self.s1601.children if not c.deleted and c.kind == "subsection"]
        self.assertEqual(designated, ["a", "b", "c", "d", "e", "f", "f", "g"])
        flags = [f for f in self.outcome.flags if "two subsections designated (f)" in f.message]
        self.assertEqual([f.cite for f in flags], ["§ 6"])

    def test_new_act_section_has_no_invented_code_number(self):
        new = find_doc(self.outcome, "New § 10 of Pub. L. 116-124 (no Code section number yet)")
        self.assertEqual(new.basis, "new")
        self.assertTrue(any("Code section numbers are assigned" in f.message for f in self.outcome.flags))

    def test_note_targets_are_redlined_and_labeled(self):
        notes = [d for d in self.outcome.documents if d.basis == "note"]
        self.assertEqual(len(notes), 2)
        sea = next(d for d in notes if "Secure Equipment" in d.title)
        section = sea.root.live_child("2", "section").live_child("a").live_child("3")
        self.assertEqual([c.value for c in section.children if not c.deleted], ["A", "B", "C"])

    def test_non_amending_provisions_go_to_the_appendix(self):
        self.assertEqual([p.cite for p in self.bill.provisions], ["§ 1", "§ 3(c)"])
        self.assertIn("“covered list”", self.bill.provisions[1].text)

    def test_quoted_additions_print_their_quotation_marks(self):
        e = self.child(self.s1601, "e")
        self.assertIn("the term “foreign adversary” has", e.live_child("3").body.current())


class DocxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bill, outcome, passed = run_bill()
        folder = tempfile.mkdtemp()
        cls.path = os.path.join(folder, "out.docx")
        write_docx(cls.path, bill, outcome, "https://www.congress.gov/bill/119th-congress/house-bill/10453/text",
                   "ih", passed)
        with zipfile.ZipFile(cls.path) as package:
            cls.document = ET.fromstring(package.read("word/document.xml"))
            cls.footnotes = ET.fromstring(package.read("word/footnotes.xml"))

    def test_every_footnote_reference_resolves(self):
        refs = [r.get(f"{W}id") for r in self.document.iter(f"{W}footnoteReference")]
        defined = [f.get(f"{W}id") for f in self.footnotes if f.get(f"{W}type") is None]
        self.assertTrue(refs)
        self.assertEqual(refs, defined)

    def test_repeat_citations_refer_back_to_the_first_note(self):
        texts = ["".join(f.itertext()) for f in self.footnotes]
        self.assertTrue(any("See supra note" in t for t in texts))

    def test_redline_formatting(self):
        added = deleted = 0
        for run in self.document.iter(f"{W}r"):
            props = run.find(f"{W}rPr")
            if props is None:
                continue
            color = props.find(f"{W}color")
            if color is not None and color.get(f"{W}val") == "00803C":
                self.assertEqual(props.find(f"{W}u").get(f"{W}val"), "single")
                added += 1
            if props.find(f"{W}strike") is not None:
                self.assertEqual(color.get(f"{W}val"), "C00000")
                deleted += 1
        self.assertTrue(added and deleted)


if __name__ == "__main__":
    unittest.main()
