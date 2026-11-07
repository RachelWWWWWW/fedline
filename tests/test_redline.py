"""End-to-end and unit tests, using H.R. 10453 and the Code sections it amends."""

import os
import re
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
from fedline.engine import Engine, _NotApplied, _code_wording, _next_free, _sentences, _word_matches, apply_bill
from fedline.sources import SourceError, parse_bill_url
from fedline.verify import verify
from fedline.statute import CodeTitle, DEL, INS, MOVED, Node, Text, _strip_ws, credit_origin

FIXTURES = Path(__file__).parent / "fixtures"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def run_bill():
    bill_xml = (FIXTURES / "BILLS-119hr10453ih.xml").read_bytes()
    title = CodeTitle((FIXTURES / "usc47_s1601-1609.xml").read_bytes())
    return redline(bill_xml, {47: title})


def marked(text: Text) -> str:
    out = ""
    for seg in text.segments:
        out += {"orig": seg.text, DEL: f"[-{seg.text}-]", INS: f"[+{seg.text}+]", MOVED: f"[~{seg.text}~]"}[seg.status]
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


class ContinuationTests(unittest.TestCase):
    """16 U.S.C. 1536(b)(4) has text between subparagraph (C) and clauses (i) and (ii)."""
    XML = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>16</docNumber></meta><main>'
           '<section identifier="/us/usc/t16/s1536"><num value="1536">§ 1536.</num><chapeau>If the Secretary '
           'concludes that—</chapeau><subparagraph><num value="C">(C)</num><content>the taking is authorized;'
           '</content></subparagraph><continuation>the Secretary shall provide a statement that—</continuation>'
           '<clause><num value="i">(i)</num><content>specifies the impact, and</content></clause>'
           '<clause><num value="ii">(ii)</num><content>sets forth terms.</content></clause>'
           '<continuation>The statement is final.</continuation>'
           '<sourceCredit>(Pub. L. 93–205, § 7.)</sourceCredit></section></main></uscDoc>').encode()

    def setUp(self):
        self.doc = CodeTitle(self.XML).document("1536")

    def test_text_between_subdivisions_keeps_its_place(self):
        root = self.doc.root
        self.assertEqual([c.kind for c in root.children], ["subparagraph", "continuation", "clause", "clause"])
        self.assertEqual(root.tail.current(), "The statement is final.")
        self.assertEqual(_strip_ws(root.before_text()), self.doc.source_text)

    def test_matter_following_a_subdivision_can_be_the_text_between(self):
        inst = Instruction(cite="§ 9", raw="", target=Target(), path=[], action="replace",
                           args={"old": "Secretary", "new": "Director", "every": False},
                           within=[("following", "subparagraph", "C")])
        Engine(SimpleNamespace(), {})._operate(inst, self.doc, self.doc.root)
        self.assertEqual(marked(self.doc.root.children[1].body),
                         "the [-Secretary-][+Director+] shall provide a statement that—")


class UslmShapeTests(unittest.TestCase):
    """Shapes from the Code that each used to lose or misplace text."""

    def section(self, inner):
        xml = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>15</docNumber></meta><main>'
               f'<section identifier="/us/usc/t15/s1"><num value="1">§ 1.</num>{inner}'
               '<sourceCredit>(Pub. L. 1–1, § 1.)</sourceCredit></section></main></uscDoc>').encode()
        doc = CodeTitle(xml).document("1")
        self.assertEqual(_strip_ws(doc.root.before_text()), doc.source_text)
        return doc.root

    def test_content_text_before_its_paragraphs(self):  # 15 U.S.C. 12(a)
        root = self.section('<content>"Antitrust laws" includes the Sherman Act.<p>"Commerce" means trade.</p>'
                            '<p>"Person" includes corporations.</p></content>')
        self.assertEqual(root.body.current().split("\n"),
                         ['"Antitrust laws" includes the Sherman Act.', '"Commerce" means trade.',
                          '"Person" includes corporations.'])

    def test_generic_levels_and_subsubitems(self):  # 30 U.S.C. 524, 825
        root = self.section('<chapeau>Every claim—</chapeau><level><num value="a">(a)</num><content>leased; or</content>'
                            '</level><subsubitem><num value="aaa">(aaa)</num><content>covered.</content></subsubitem>')
        self.assertEqual([(c.kind, c.value) for c in root.children], [("level", "a"), ("subsubitem", "aaa")])

    def test_provisos_notes_and_later_chapeaus_keep_their_place(self):  # 15 U.S.C. 46, 16 U.S.C. 410hh, 460u-3
        root = self.section('<chapeau>The term means—</chapeau><paragraph><num value="1">(1)</num><content>a house.'
                            '</content></paragraph><chapeau>It also includes land that—</chapeau><subparagraph>'
                            '<num value="A">(A)</num><content>is owned.</content></subparagraph><note topic="x">'
                            '<p>[Amended this section.]</p></note><paragraph><num value="2">(2)</num><content>a barn.'
                            '</content></paragraph><proviso>Provided, That this applies.</proviso>')
        self.assertEqual(root.body.current(), "The term means—")
        self.assertEqual([(c.kind, c.body.current()) for c in root.children if c.kind == "continuation"],
                         [("continuation", "It also includes land that—"), ("continuation", "[Amended this section.]")])
        self.assertEqual(root.tail.current(), "Provided, That this applies.")


class ActNameTests(unittest.TestCase):
    """A bill can name one Act in different ways, like "the Federal Power Act" and "Part II of the Federal
    Power Act". Every name must reach the same sections."""

    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>16</docNumber></meta><main>'
            '<section identifier="/us/usc/t16/s824"><num value="824">§ 824.</num><content>The rate is fair.'
            '</content><sourceCredit>(June 10, 1920, ch. 285, pt. II, § 201.)</sourceCredit></section>'
            '<section identifier="/us/usc/t16/s824a"><num value="824a">§ 824a.</num><content>The Commission may act.'
            '</content><sourceCredit>(June 10, 1920, ch. 285, pt. II, § 202.)</sourceCredit></section>'
            '</main></uscDoc>').encode()

    def test_edits_through_different_names_reach_the_same_sections(self):
        bill = parse_sections(
            "<section><enum>2.</enum><text>Section 201 of the Federal Power Act (16 U.S.C. 824) is amended by "
            "striking <quote>fair</quote> and inserting <quote>just</quote>.</text></section>"
            "<section><enum>3.</enum><text>Part II of the Federal Power Act (16 U.S.C. 824 et seq.) is amended, "
            "in section 201, by striking <quote>rate</quote> and inserting <quote>charge</quote>.</text></section>"
            "<section><enum>4.</enum><text>Part II of the Federal Power Act (16 U.S.C. 824 et seq.) is amended by "
            "adding at the end the following:</text><quoted-block><section><enum>203.</enum><header>New</header>"
            "<text>Text.</text></section></quoted-block></section>")
        outcome = apply_bill(bill, {16: CodeTitle(self.CODE)})
        self.assertEqual(outcome.applied, 3)
        self.assertEqual(marked(find_doc(outcome, "16 U.S.C. § 824").root.body),
                         "The [-rate-][+charge+] is [-fair-][+just+].")
        new = find_doc(outcome, "New § 203 of the Federal Power Act (no Code section number yet)")
        self.assertGreater(new.order, find_doc(outcome, "16 U.S.C. § 824").order)


class RedesignateTests(unittest.TestCase):
    """S. 5653 § 2222(a)(1)(F): "by redesignating subsections (a) through (g) as paragraphs (8), (4), …,
    respectively, and indenting and moving the paragraphs so as to appear in numerical order"."""
    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>30</docNumber></meta><main>'
            '<section identifier="/us/usc/t30/s1001"><num value="1001">§ 1001.</num><chapeau>In this chapter:'
            '</chapeau><subsection><num value="a">(a)</num><content>“Secretary” means the Secretary.</content>'
            '</subsection><subsection><num value="b">(b)</num><content>“lease” means a lease.</content>'
            '</subsection><subsection><num value="c">(c)</num><content>“byproduct” means a mineral.</content>'
            '</subsection><sourceCredit>(Pub. L. 91–581, § 2, Dec. 24, 1970.)</sourceCredit></section>'
            '</main></uscDoc>').encode()

    def run_bill(self, *instructions):
        items = "".join(f"<paragraph><enum>({n})</enum>{text}</paragraph>" for n, text in enumerate(instructions, 1))
        bill = parse_sections("<section><enum>2.</enum><text>Section 2 of the Geothermal Steam Act of 1970 "
                              f"(30 U.S.C. 1001) is amended—</text>{items}</section>")
        outcome = apply_bill(bill, {30: CodeTitle(self.CODE)})
        docs = [d for d in outcome.documents if d.title == "30 U.S.C. § 1001"]
        return bill, outcome, docs[0].root if docs else None

    def test_redesignated_subdivisions_take_the_new_kind(self):
        bill, outcome, root = self.run_bill(
            "<text>by redesignating subsections (a) through (c) as paragraphs (1) through (3), respectively, and "
            "indenting appropriately;</text>",
            "<text>by inserting after paragraph (2) the following:</text><quoted-block><paragraph><enum>(2A)</enum>"
            "<text>“mineral” means a mineral.</text></paragraph></quoted-block>")
        self.assertEqual((outcome.applied, outcome.flags), (2, []))
        self.assertEqual([(c.kind, c.value) for c in root.children],
                         [("paragraph", "1"), ("paragraph", "2"), ("paragraph", "2A"), ("paragraph", "3")])

    def test_moving_into_numerical_order_marks_the_fewest_provisions_moved(self):
        bill, outcome, root = self.run_bill(
            "<text>by redesignating subsections (a) through (c) as paragraphs (3), (1), and (2), respectively, and "
            "indenting and moving the paragraphs so as to appear in numerical order.</text>")
        self.assertTrue(bill.instructions[0].args["reorder"])
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        self.assertEqual([c.value for c in root.children], ["1", "2", "3"])
        # (1) and (2) were already in order; only (3), the former (a), moved.
        self.assertEqual([any(seg.status == MOVED for seg in c.body.segments) for c in root.children],
                         [False, False, True])
        self.assertEqual(marked(root.children[2].label), "[-(a)-][+(3)+]")
        self.assertEqual(root.before_text(), "§ 1001.In this chapter:(a)“Secretary” means the Secretary.(b)“lease” means a "
                                                     "lease.(c)“byproduct” means a mineral.")
        self.assertIn("Original text in the redline matches the source XML in every section.", verify(bill, outcome))

    def test_moved_text_is_purple_and_double_underlined(self):
        bill, outcome, _ = self.run_bill(
            "<text>by redesignating subsections (a) and (b) as paragraphs (2) and (1), respectively, and moving the "
            "paragraphs so as to appear in numerical order.</text>")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.docx")
            write_docx(path, bill, outcome, "https://example.gov", "ih", verify(bill, outcome))
            document = zipfile.ZipFile(path).read("word/document.xml").decode()
        moved = '<w:rPr><w:color w:val="7030A0"/><w:u w:val="double"/></w:rPr><w:t xml:space="preserve">'
        # Swapping two, the first stays and the second, (1), the former (b), is marked as moved before it.
        self.assertIn(moved + "“lease” means a lease.</w:t>", document)
        self.assertIn(moved + "moved text</w:t>", document)  # the key
        # Its old place, after (a) and before (c), with the old designation, as a subsection, purple and
        # double-struck.
        was = '<w:color w:val="7030A0"/></w:rPr><w:t xml:space="preserve">'
        old_place = ('<w:p><w:r><w:rPr><w:b/><w:dstrike/>' + was + '(b)</w:t></w:r>'
                     '<w:r><w:t xml:space="preserve"> </w:t></w:r><w:r><w:rPr><w:dstrike/>' + was +
                     '“lease” means a lease.</w:t></w:r>')
        self.assertIn(old_place, document)
        paragraphs = [re.sub(r"<[^>]+>", "", p) for p in re.findall(r"<w:p>.*?</w:p>", document)]
        start = paragraphs.index("§ 1001. In this chapter:")
        self.assertEqual(paragraphs[start + 1:start + 5], [
            "(b)(1) “lease” means a lease.", "(a)(2) “Secretary” means the Secretary.", "(b) “lease” means a lease.",
            "(c) “byproduct” means a mineral."])
        self.assertIn('<w:dstrike/><w:color w:val="7030A0"/></w:rPr><w:t xml:space="preserve">where it was', document)

    def test_moving_subdivisions_already_in_order_needs_no_flag(self):
        _, outcome, _ = self.run_bill(
            "<text>by redesignating subsections (a) through (c) as paragraphs (1) through (3), respectively, and "
            "indenting and moving the paragraphs so as to appear in numerical order.</text>")
        self.assertEqual((outcome.applied, outcome.flags), (1, []))

    def test_the_old_place_reads_as_it_was_when_the_bill_also_split_the_text(self):
        # As in 42 U.S.C. 4336(b)(2): the bill splits the text into a new subparagraph (A), then moves the
        # provision. Where it was, the text ran on in one line, so the old place shows it that way.
        bill, outcome, root = self.run_bill(
            "<text>in subsection (a), by striking <quote>“Secretary” means</quote> and inserting the following:"
            "</text><quoted-block><subparagraph><enum>(A)</enum><header>In general</header><text>The term "
            "<term>Secretary</term> means</text></subparagraph></quoted-block>",
            "<text>by redesignating subsections (a) through (c) as paragraphs (3), (1), and (2), respectively, and "
            "moving the paragraphs so as to appear in numerical order.</text>")
        self.assertEqual((outcome.applied, outcome.flags), (2, []))
        moved = root.children[2]
        self.assertEqual((moved.value, moved.moved_by), ("3", "§ 2(2)"))
        self.assertEqual(marked(moved.children[0].body), "[+The term “Secretary” means+][~ the Secretary.~]")
        passed = verify(bill, outcome)
        self.assertIn("Original text in the redline matches the source XML in every section.", passed)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.docx")
            write_docx(path, bill, outcome, "https://example.gov", "ih", passed)
            document = zipfile.ZipFile(path).read("word/document.xml").decode()
        paragraphs = [re.sub(r"<[^>]+>", "", p) for p in re.findall(r"<w:p>.*?</w:p>", document)]
        start = paragraphs.index("§ 1001. In this chapter:")
        self.assertEqual(paragraphs[start + 1:start + 6], [
            "(a) “Secretary” means the Secretary.",  # the old place, in one line
            "(b)(1) “lease” means a lease.",
            "(c)(2) “byproduct” means a mineral.",
            "(a)(3) “Secretary” means",
            "(A) In general The term “Secretary” means the Secretary."])
        old_place = next(p for p in re.findall(r"<w:p>.*?</w:p>", document)
                         if re.sub(r"<[^>]+>", "", p) == paragraphs[start + 1])
        texts = re.findall(r"<w:r>(.*?)</w:r>", old_place)
        self.assertTrue(all("<w:dstrike/>" in t for t in texts if re.sub(r"<[^>]+>", "", t).strip()))

    def test_a_separate_instruction_to_reorder(self):
        _, outcome, root = self.run_bill(
            "<text>by redesignating subsections (a) and (b) as subsections (b) and (a), respectively;</text>",
            "<text>by moving the subsections so as to appear in numerical order.</text>")
        self.assertEqual((outcome.applied, outcome.failed), (2, 0))
        self.assertEqual([c.value for c in root.children], ["a", "b", "c"])

    def test_other_words_after_the_designations_arent_swallowed(self):
        bill, _, _ = self.run_bill("<text>by redesignating subsections (a) through (c) as paragraphs (1) through (3), "
                                   "respectively, and striking the definitions.</text>")
        self.assertEqual([p.reason for p in bill.problems], ["instruction wording isn't a recognized pattern"])


class MoreShapesTests(unittest.TestCase):
    """S. 5653 §§ 1205(1), 1302(2)(D)(ii), 2232 and 2222(a)(1)(G)(i)."""
    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>16</docNumber></meta><main>'
            '<section identifier="/us/usc/t16/s1536"><num value="1536">§ 1536.</num>'
            '<subsection><num value="b">(b)</num><paragraph><num value="1">(1)</num>'
            '<subparagraph><num value="A">(A)</num><content>Consultation shall end.</content></subparagraph>'
            '<subparagraph><num value="B">(B)</num><chapeau>The Secretary may not agree—</chapeau>'
            '<clause><num value="i">(i)</num><content>first;</content></clause>'
            '<clause><num value="ii">(ii)</num><content>second,</content></clause>'
            '<continuation>unless the applicant consents. The Secretary may extend the period.</continuation>'
            '</subparagraph></paragraph></subsection>'
            '<subsection><num value="e">(e)</num><content>Licenses within any reservation shall protect the use '
            'of such reservation: Provided, That the Secretary shall deem the use of such reservation necessary: '
            'Provided further, That no license shall affect the use of such reservation.</content></subsection>'
            '<subsection><num value="f">(f)</num><chapeau>In this section:</chapeau>'
            '<paragraph><num value="1">(1)</num><content>“Secretary” means the Secretary.</content></paragraph>'
            '<paragraph><num value="2">(2)</num><content>“lease” means a lease.</content></paragraph></subsection>'
            '<subsection><num value="n">(n)</num><content>Any person, as defined by section 3(1), may seek review in '
            'the court for (1) any circuit where the action occurs, or (2) the District of Columbia, within 90 days.'
            '</content></subsection>'
            '<subsection><num value="t">(t)</num><heading>Navigable waters</heading><content>Nothing in this '
            'section shall preclude a State.</content></subsection>'
            '<sourceCredit>(Pub. L. 93–205, § 7, Dec. 28, 1973.)</sourceCredit></section></main></uscDoc>').encode()

    def run_bill(self, *instructions):
        items = "".join(f"<paragraph><enum>({n})</enum>{text}</paragraph>" for n, text in enumerate(instructions, 1))
        bill = parse_sections("<section><enum>2.</enum><text>Section 7 of the Endangered Species Act of 1973 "
                              f"(16 U.S.C. 1536) is amended—</text>{items}</section>")
        self.assertEqual(bill.problems, [])
        outcome = apply_bill(bill, {16: CodeTitle(self.CODE)})
        self.assertIn("Original text in the redline matches the source XML in every section.", verify(bill, outcome))
        docs = [d for d in outcome.documents if d.title == "16 U.S.C. § 1536"]
        return outcome, docs[0].root if docs else None

    def test_matter_after_the_last_subdivision_becomes_a_new_provision_after_it(self):
        outcome, root = self.run_bill(
            "<text>in subsection (b)(1)(B), in the undesignated matter following clause (ii), by striking "
            "<quote>The Secretary may extend</quote> and inserting the following:</text><quoted-block><subparagraph>"
            "<enum>(C)</enum><text>The Secretary may extend</text></subparagraph></quoted-block>")
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        paragraph = root.live_child("b").live_child("1")
        self.assertEqual([c.value for c in paragraph.children], ["A", "B", "C"])
        subparagraph_b, subparagraph_c = paragraph.children[1:]
        self.assertEqual([c.value for c in subparagraph_b.children], ["i", "ii"])  # the clauses stay
        self.assertEqual(marked(subparagraph_b.tail), "unless the applicant consents. [-The Secretary may extend-]")
        self.assertEqual(marked(subparagraph_c.body), "[+The Secretary may extend+] the period.")

    def test_matter_after_subdivisions_cant_become_a_deeper_provision(self):
        outcome, _ = self.run_bill(
            "<text>in subsection (b)(1)(B), in the undesignated matter following clause (ii), by striking "
            "<quote>The Secretary may extend</quote> and inserting the following:</text><quoted-block><clause>"
            "<enum>(iii)</enum><text>The Secretary may extend</text></clause></quoted-block>")
        self.assertEqual(outcome.applied, 0)
        self.assertIn("only the matter after the last subdivision", outcome.flags[0].message)

    def test_an_edit_in_a_proviso_stays_in_it(self):
        # "of such reservation" appears in the text before the provisos, in the first and in the second.
        outcome, root = self.run_bill("<text>in subsection (e), in the first proviso, by inserting <quote>and are "
                                      "reasonable</quote> after <quote>of such reservation</quote>.</text>")
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        self.assertEqual(marked(root.live_child("e").body),
                         "Licenses within any reservation shall protect the use of such reservation: Provided, That "
                         "the Secretary shall deem the use of such reservation[+ and are reasonable+] necessary: "
                         "Provided further, That no license shall affect the use of such reservation.")

    def test_a_missing_proviso_isnt_guessed(self):
        outcome, _ = self.run_bill("<text>in subsection (e), in the third proviso, by inserting <quote>X</quote> "
                                   "after <quote>of such reservation</quote>.</text>")
        self.assertEqual(outcome.applied, 0)
        self.assertIn("doesn't have a third proviso (it has 2)", outcome.flags[0].message)

    def test_a_struck_designation_gives_way_to_the_new_one(self):
        outcome, root = self.run_bill(
            "<text>in subsection (t), by striking <quote>(t) Nothing in this section</quote> and inserting the "
            "following:</text><quoted-block><subsection><enum>(u)</enum><header>Savings provision</header><text>"
            "Nothing in this section</text></subsection></quoted-block>")
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        subsection = root.live_child("u")
        self.assertEqual((marked(subsection.label), marked(subsection.heading), marked(subsection.body)),
                         ("[-(t)-][+(u)+]", "[-Navigable waters-][+Savings provision+]",
                          "Nothing in this section shall preclude a State."))
        self.assertIsNone(root.live_child("t"))

    def test_designations_run_into_the_text_are_redesignated_there(self):
        # 16 U.S.C. 1536(n) runs "(1) any circuit …, or (2) …" into its text. The "(1)" in "section 3(1)" is a
        # citation, not a designation.
        outcome, root = self.run_bill("<text>in subsection (n), by redesignating paragraphs (1) and (2) as "
                                      "subparagraphs (A) and (B), respectively.</text>")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(marked(root.live_child("n").body),
                         "Any person, as defined by section 3(1), may seek review in the court for [-(1)-][+(A)+] any "
                         "circuit where the action occurs, or [-(2)-][+(B)+] the District of Columbia, within 90 days.")
        self.assertEqual([f.blocking for f in outcome.flags], [False])
        self.assertIn("runs paragraphs (1) and (2) into the text of", outcome.flags[0].message)

    def test_run_in_designations_out_of_order_or_missing_arent_guessed(self):
        for values in ("(2) and (1) as subparagraphs (A) and (B)", "(1) and (3) as subparagraphs (A) and (B)"):
            outcome, _ = self.run_bill(f"<text>in subsection (n), by redesignating paragraphs {values}, "
                                       "respectively.</text>")
            self.assertEqual(outcome.applied, 0, values)
            self.assertIn("wasn't found", outcome.flags[0].message)

    def test_text_inserted_after_a_designation(self):
        outcome, root = self.run_bill("<text>in subsection (f), in each of paragraphs (1) and (2), by inserting "
                                      "<quote>The term</quote> after the paragraph designation.</text>")
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        self.assertEqual([marked(c.body) for c in root.live_child("f").children],
                         ["[+The term +]“Secretary” means the Secretary.", "[+The term +]“lease” means a lease."])

    def test_text_after_a_designation_must_name_the_right_level(self):
        outcome, _ = self.run_bill("<text>in subsection (f)(1), by inserting <quote>The term</quote> after the "
                                   "subsection designation.</text>")
        self.assertEqual(outcome.applied, 0)
        self.assertIn("isn't a subsection with text after its designation", outcome.flags[0].message)


class StatutesAtLargeTests(unittest.TestCase):
    """S. 5653 § 2212: "The Act of February 5, 1948 (62 Stat. 17, chapter 45), is amended— (1) in the first
    section (62 Stat. 17, chapter 45; 25 U.S.C. 323), …"."""
    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>25</docNumber></meta><main>'
            '<section identifier="/us/usc/t25/s323"><num value="323">§ 323.</num><heading>Rights-of-way across any '
            'Indian lands</heading><content>The Secretary of the Interior be, and he is empowered to grant '
            'rights-of-way.</content><sourceCredit>(Feb. 5, 1948, ch. 45, § 1, 62 Stat. 17.)</sourceCredit></section>'
            '<section identifier="/us/usc/t25/s324"><num value="324">§ 324.</num><content>No grant over lands of a '
            'tribe organized under the Act of June 18, 1934 (48 Stat. 984), as amended [25 U.S.C. 5101 et seq.], '
            'shall be made without consent.</content><sourceCredit>(Feb. 5, 1948, ch. 45, § 2, 62 Stat. 18.)'
            '</sourceCredit></section></main></uscDoc>').encode()

    def run_bill(self, *instructions):
        items = "".join(f"<paragraph><enum>({n})</enum>{text}</paragraph>" for n, text in enumerate(instructions, 1))
        bill = parse_sections("<section><enum>2.</enum><text>The Act of February 5, 1948 (62 Stat. 17, chapter 45), "
                              f"is amended—</text>{items}</section>")
        self.assertEqual(bill.problems, [])
        outcome = apply_bill(bill, {25: CodeTitle(self.CODE)})
        self.assertIn("Original text in the redline matches the source XML in every section.", verify(bill, outcome))
        return bill, outcome

    def test_each_instruction_gives_the_code_citation(self):
        bill, outcome = self.run_bill(
            "<text>in the first section (62 Stat. 17, chapter 45; 25 U.S.C. 323), by striking <quote>That the "
            "Secretary of the Interior be, and he is hereby, empowered to</quote> and inserting the following:"
            "</text><quoted-block><section><enum>1.</enum><header>Rights-of-way across Indian land</header>"
            "<subsection><enum /><text>The Secretary of the Interior may</text></subsection></section>"
            "</quoted-block>",
            "<text>in section 2 (62 Stat. 18, chapter 45; 25 U.S.C. 324), by striking <quote>organized under the Act "
            "of June 18, 1934 (48 Stat. 984), as amended,</quote>.</text>",
            "<text>by adding at the end the following:</text><quoted-block><section><enum>8.</enum><header>Tribal "
            "grants</header><text>A tribe may grant a right-of-way.</text></section></quoted-block>")
        self.assertEqual(bill.titles(), [25])
        self.assertEqual([(i.target.section, i.target.usc_section) for i in bill.instructions],
                         [("1", "323"), ("2", "324"), (None, None)])
        self.assertEqual((outcome.applied, outcome.failed), (3, 0))
        first = find_doc(outcome, "25 U.S.C. § 323").root
        self.assertEqual(marked(first.heading),
                         "[-Rights-of-way across any Indian lands-][+Rights-of-way across Indian land+]")
        self.assertEqual(marked(first.body), "[-The Secretary of the Interior be, and he is empowered to-][+The "
                                             "Secretary of the Interior may+] grant rights-of-way.")
        self.assertEqual(marked(find_doc(outcome, "25 U.S.C. § 324").root.body),
                         "No grant over lands of a tribe[- organized under the Act of June 18, 1934 (48 Stat. 984), as "
                         "amended [25 U.S.C. 5101 et seq.],-] shall be made without consent.")
        find_doc(outcome, "New § 8 of the Act of February 5, 1948 (no Code section number yet)")
        self.assertEqual([f.blocking for f in outcome.flags], [False, False])  # the two reworded matches

    def test_without_a_code_citation_the_act_isnt_found(self):
        _, outcome = self.run_bill("<text>in section 2, by striking <quote>without consent</quote>.</text>")
        self.assertEqual(outcome.applied, 0)
        self.assertIn("couldn't be mapped to Code sections", outcome.flags[0].message)

    def test_the_enacting_that_is_optional_only_at_the_start(self):
        pattern = _code_wording("That the Secretary", {})
        self.assertRegex("The Secretary", pattern)
        self.assertRegex("That the Secretary", pattern)
        self.assertIsNone(_code_wording("provided That the Secretary", {}))


class CodeWordingTests(unittest.TestCase):
    """The Code's editors reword an Act's text: its own section numbers for the Act's, "this chapter" for
    "this Act", dates for "the date of enactment", curly apostrophes. A phrase quoted from the Act is matched
    against those forms when it isn't found as is."""
    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>16</docNumber></meta><main>'
            '<section identifier="/us/usc/t16/s1532"><num value="1532">§ 1532.</num><content>Definitions.</content>'
            '<sourceCredit>(Pub. L. 93–205, § 3, Dec. 28, 1973.)</sourceCredit></section>'
            '<section identifier="/us/usc/t16/s1536"><num value="1536">§ 1536.</num><content>Any person, as defined '
            'by section 1532(13) of this title, may obtain review under sections 1532 and 1533 of this title. '
            'The agency’s rules are due 18 months after December 28, 1973. The agency’s staff shall assist.'
            '</content><sourceCredit>(Pub. L. 93–205, § 7, Dec. 28, 1973.)</sourceCredit></section>'
            '<section identifier="/us/usc/t16/s1533"><num value="1533">§ 1533.</num><content>Listing.</content>'
            '<sourceCredit>(Pub. L. 93–205, § 4, Dec. 28, 1973.)</sourceCredit></section></main></uscDoc>').encode()

    def run_bill(self, *instructions):
        items = "".join(f"<paragraph><enum>({n})</enum><text>{text}</text></paragraph>"
                        for n, text in enumerate(instructions, 1))
        bill = parse_sections("<section><enum>2.</enum><text>Section 7 of the Endangered Species Act of 1973 "
                              f"(16 U.S.C. 1536) is amended—</text>{items}</section>")
        self.assertEqual(bill.problems, [])
        outcome = apply_bill(bill, {16: CodeTitle(self.CODE)})
        docs = [d for d in outcome.documents if d.title == "16 U.S.C. § 1536"]
        return outcome, marked(docs[0].root.body) if docs else None

    def test_patterns_allow_the_codes_forms_and_leave_plain_phrases_alone(self):
        self.assertIsNone(_code_wording("may obtain review", {"3": "1532"}))
        pattern = _code_wording("section 3(13) of this Act,", {"3": "1532"})
        self.assertRegex("section 1532(13) of this title,", pattern)
        self.assertRegex("section 3(13) of this Act,", pattern)
        self.assertRegex("subchapter II of this chapter", _code_wording("title II of this Act", {}))
        self.assertRegex("1342 of this title, and (G)", _code_wording("402, and (G)", {"402": "1342"}))
        # At the end of a phrase, "of this title" isn't taken, so striking the phrase leaves it in place.
        self.assertNotRegex("and 824v of this title", _code_wording("and 222", {"222": "824v"}) + "$")
        self.assertRegex("the Federal Power Act [16 U.S.C. 824p(a)]",
                         _code_wording("the Federal Power Act (16 U.S.C. 824p(a))", {}))
        self.assertRegex("section 824p(a) of title 16",
                         _code_wording("section 216(a) of the Federal Power Act (16 U.S.C. 824p(a))", {}))

    def test_the_codes_wording_is_matched_and_noted_for_review(self):
        outcome, body = self.run_bill(
            "by striking <quote>Any person, as defined by section 3(13) of this Act,</quote> and inserting "
            "<quote>Any person</quote>;",
            "by striking <quote>sections 3 and 4</quote> and inserting <quote>sections 3, 4, and 5</quote>;",
            "by striking <quote>18 months after the date of enactment of this section</quote> and inserting "
            "<quote>1 year after the date of enactment of the Recovery Act</quote>.")
        self.assertEqual((outcome.applied, outcome.failed), (3, 0))
        self.assertEqual(body, "[-Any person, as defined by section 1532(13) of this title,-][+Any person+] may "
                               "obtain review under [-sections 1532 and 1533-][+sections 3, 4, and 5+] of this "
                               "title. The agency’s rules are due [-18 months after December 28, 1973-][+1 year "
                               "after the date of enactment of the Recovery Act+]. The agency’s staff shall assist.")
        self.assertEqual([f.blocking for f in outcome.flags], [False, False, False])
        self.assertIn("The Code prints “sections 1532 and 1533” where the bill quotes the Act's “sections 3 and 4”",
                      outcome.flags[1].message)

    def test_a_reworded_match_that_isnt_unique_isnt_applied(self):
        outcome, body = self.run_bill("by striking <quote>agency's</quote> and inserting <quote>agency</quote>.")
        self.assertEqual((outcome.applied, outcome.failed), (0, 1))
        self.assertIsNone(body)
        self.assertIn("wasn't found", outcome.flags[0].message)

    def test_a_review_note_is_dropped_when_the_instruction_then_fails(self):
        outcome, _ = self.run_bill("by striking <quote>section 3(13) of this Act</quote> and all that follows "
                                   "through <quote>nowhere</quote> and inserting <quote>X</quote>.")
        self.assertEqual(outcome.applied, 0)
        self.assertEqual([f.blocking for f in outcome.flags], [True])


class LeadInTests(unittest.TestCase):
    def target(self, lead):
        bill = parse_sections(f"<section><enum>2.</enum><text>{lead} by striking <quote>a</quote> and inserting "
                              "<quote>b</quote>.</text></section>")
        self.assertEqual(bill.problems, [])
        return bill.instructions[0].target

    def test_citation_attached_to_a_section(self):
        bill = parse_sections("<section><enum>2.</enum><text>The Federal Power Act is amended by inserting after "
                              "section 216 (16 U.S.C. 824p) the following:</text><quoted-block><section><enum>216A."
                              "</enum><header>New</header><text>Text.</text></section></quoted-block></section>")
        inst = bill.instructions[0]
        self.assertEqual((inst.target.name, inst.target.title, inst.target.usc_section), ("Federal Power Act", 16, "824p"))
        self.assertEqual((inst.action, inst.args["values"]), ("insert_after_node", ["216"]))

    def test_smaller_unit_before_the_section(self):
        target = self.target("Paragraph (4) of section 3101 of the Energy Act of 2020 (43 U.S.C. 3001) is amended")
        self.assertEqual((target.section, target.path, target.usc_section), ("3101", [("paragraph", "4")], "3001"))

    def test_as_amended_notes_with_parentheses(self):
        target = self.target("Section 4 of the Geothermal Steam Act of 1970 (30 U.S.C. 1003) (as amended by section "
                             "2223(b)) is amended")
        self.assertEqual((target.name, target.cite), ("Geothermal Steam Act of 1970", "30 U.S.C. 1003"))
        target = self.target("Title I of the National Environmental Policy Act of 1969 (as amended by section "
                             "1102(b)(1)) is amended, in section 110 (42 U.S.C. 4336d),")
        self.assertEqual((target.name, target.usc_section),
                         ("Title I of the National Environmental Policy Act of 1969", "4336d"))

    def test_code_chapter(self):
        target = self.target("Chapter 3027 of title 54, United States Code, is amended")
        self.assertEqual((target.title, target.chapter), (54, "3027"))


class WholeSectionTests(unittest.TestCase):
    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>42</docNumber></meta><main>'
            '<chapter><num value="55">CHAPTER 55</num>'
            '<section identifier="/us/usc/t42/s4346"><num value="4346">§ 4346.</num><content>Old 208.</content>'
            '<sourceCredit>(Pub. L. 91–190, title II, § 208.)</sourceCredit></section>'
            '<section identifier="/us/usc/t42/s4347"><num value="4347">§ 4347.</num><content>Old 209.</content>'
            '<sourceCredit>(Pub. L. 91–190, title II, § 209.)</sourceCredit></section></chapter></main></uscDoc>').encode()

    def run_bill(self, sections_xml):
        return apply_bill(parse_sections(sections_xml), {42: CodeTitle(self.CODE)})

    def test_striking_and_replacing_a_whole_section(self):
        outcome = self.run_bill(
            "<section><enum>2.</enum><text>Title II of the National Environmental Policy Act of 1969 is amended by "
            "striking section 209 (42 U.S.C. 4347) and inserting the following:</text><quoted-block><section>"
            "<enum>209.</enum><header>eNEPA</header><text>New 209.</text></section></quoted-block></section>")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(marked(find_doc(outcome, "42 U.S.C. § 4347").root.body), "[-Old 209.-]")
        titles = [d.title for d in outcome.documents]
        self.assertEqual(titles, ["42 U.S.C. § 4347", "New § 209 of Pub. L. 91-190 (no Code section number yet)"])

    def test_adding_a_section_to_a_code_chapter(self):
        outcome = self.run_bill(
            "<section><enum>2.</enum><text>Chapter 55 of title 42, United States Code, is amended by adding at the "
            "end the following:</text><quoted-block><section><enum>4348.</enum><header>New</header><text>Text."
            "</text></section></quoted-block></section>")
        new = find_doc(outcome, "New 42 U.S.C. § 4348")
        self.assertEqual(new.root.label.current(), "§ 4348.")
        self.assertGreater(new.order, CodeTitle(self.CODE).document("4347").order)

    def test_a_new_section_whose_number_is_taken_gets_the_next_free_one(self):
        # As in S. 5653 § 2234, which adds a § 37 to the Federal Power Act when it already has one. Here § 209
        # exists, so the new 209 becomes 210, and the new 210 after it becomes 211.
        outcome = self.run_bill(
            "<section><enum>2.</enum><text>Title II of the National Environmental Policy Act of 1969 (42 U.S.C. 4346 "
            "et seq.) is amended by adding at the end the following:</text><quoted-block><section><enum>209.</enum>"
            "<header>First</header>"
            "<text>One.</text></section><section><enum>210.</enum><header>Second</header><text>Two.</text>"
            "</section></quoted-block></section>")
        self.assertEqual(outcome.applied, 1)
        first = find_doc(outcome, "New § 210 of Pub. L. 91-190 (no Code section number yet)")
        second = find_doc(outcome, "New § 211 of Pub. L. 91-190 (no Code section number yet)")
        self.assertEqual((marked(first.root.label), marked(first.root.heading)), ("[+Sec. 210.+]", "[+First+]"))
        self.assertEqual(marked(second.root.label), "[+Sec. 211.+]")
        self.assertEqual([f.blocking for f in outcome.flags], [False, False])
        self.assertEqual(outcome.flags[0].message,
                         "Act § 209 already exists (42 U.S.C. § 4347), so the new section is added as § 210, the next "
                         "number not in use. Check the numbering, and any references to the new section as § 209.")
        self.assertIn("Act § 210 already exists", outcome.flags[1].message)

    def test_a_new_code_section_whose_number_is_taken_gets_the_next_free_one(self):
        outcome = self.run_bill(
            "<section><enum>2.</enum><text>Chapter 55 of title 42, United States Code, is amended by adding at the "
            "end the following:</text><quoted-block><section><enum>4347.</enum><header>New</header><text>Text."
            "</text></section></quoted-block></section>")
        new = find_doc(outcome, "New 42 U.S.C. § 4348")
        self.assertEqual(new.root.label.current(), "§ 4348.")
        self.assertEqual([f.blocking for f in outcome.flags], [False])
        self.assertIn("42 U.S.C. § 4347 already exists, so the new section is added as § 4348", outcome.flags[0].message)

    def test_a_taken_number_with_no_clear_next_one_stays_flagged(self):
        self.assertIsNone(_next_free("216A", lambda n: n == "216A"))
        self.assertEqual(_next_free("37", lambda n: n in ("37", "38")), "39")
        self.assertEqual(_next_free("37", lambda n: False), "37")


class EachOfTests(unittest.TestCase):
    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>16</docNumber></meta><main>'
            '<section identifier="/us/usc/t16/s824p"><num value="824p">§ 824p.</num>'
            '<paragraph><num value="1">(1)</num><content>The Secretary may act within 5 years.</content></paragraph>'
            '<paragraph><num value="2">(2)</num><content>The Secretary shall report within 5 years.</content>'
            '</paragraph><paragraph><num value="3">(3)</num><content>The Secretary shall publish it.</content>'
            '</paragraph><sourceCredit>(June 10, 1920, ch. 285, pt. II, § 216.)</sourceCredit></section>'
            '</main></uscDoc>').encode()

    def run_bill(self, instruction):
        bill = parse_sections(f"<section><enum>2.</enum><text>Section 216 of the Federal Power Act (16 U.S.C. 824p) "
                              f"is amended, {instruction}</text></section>")
        self.assertEqual(bill.problems, [])
        outcome = apply_bill(bill, {16: CodeTitle(self.CODE)})
        doc = find_doc(outcome, "16 U.S.C. § 824p") if outcome.documents else None
        return outcome, [marked(c.body) for c in doc.root.children] if doc else None

    def test_parses_lists_ranges_and_deeper_places(self):
        bill = parse_sections("<section><enum>2.</enum><text>Section 216 of the Federal Power Act (16 U.S.C. 824p) "
                              "is amended, in each of paragraphs (1), (4)(B), and (6) through (8), by striking "
                              "<quote>a</quote>.</text></section>")
        self.assertEqual(bill.instructions[0].path, [("each", (
            (("paragraph", "1"),), (("paragraph", "4"), ("subparagraph", "B")),
            (("paragraph", "6"),), (("paragraph", "7"),), (("paragraph", "8"),)))])

    def test_applies_in_every_place_named(self):
        outcome, texts = self.run_bill("in each of paragraphs (1) and (2), by striking <quote>5 years</quote> and "
                                       "inserting <quote>10 years</quote>.")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(texts, ["The Secretary may act within [-5 years-][+10 years+].",
                                 "The Secretary shall report within [-5 years-][+10 years+].",
                                 "The Secretary shall publish it."])

    def test_a_failure_in_one_place_changes_none(self):
        outcome, texts = self.run_bill("in each of paragraphs (1) and (3), by striking <quote>5 years</quote> and "
                                       "inserting <quote>10 years</quote>.")
        self.assertEqual((outcome.applied, outcome.failed), (0, 1))
        self.assertIsNone(texts)  # nothing changed, so the section isn't in the redline
        self.assertIn("none were changed", outcome.flags[-1].message)

    def test_indenting_is_applied_without_marks_and_flagged_for_review(self):
        for wording in ("by indenting paragraphs (1) through (3) appropriately.",
                        "by moving the margins of paragraphs (1) through (3) 2 ems to the right."):
            outcome, texts = self.run_bill(wording)
            self.assertEqual((outcome.applied, outcome.failed), (1, 0))
            self.assertIsNone(texts)  # no words changed
            self.assertEqual([f.blocking for f in outcome.flags], [False])
            self.assertIn("Indentation only: the instruction moves paragraphs (1) through (3)",
                          outcome.flags[0].message)

    def test_indenting_a_missing_subdivision_isnt_applied(self):
        outcome, _ = self.run_bill("by indenting paragraphs (1) through (4) appropriately.")
        self.assertEqual((outcome.applied, outcome.failed), (0, 1))
        self.assertIn("paragraph (4) wasn't found", outcome.flags[0].message)

    def test_near_misses_are_checked_only_where_the_replacement_reached(self):
        outcome, texts = self.run_bill("in each of paragraphs (1) and (2), by striking <quote>Secretary</quote> "
                                       "each place it appears and inserting <quote>Commission</quote>.")
        self.assertEqual(texts[2], "The Secretary shall publish it.")
        self.assertEqual([f.message for f in outcome.flags], [])


class NoteLookupTests(unittest.TestCase):
    """Pub. L. 118–272 § 2232 is reproduced in a note to 42 U.S.C. 3211 under a heading, "Modernization of
    Environmental Reviews", that doesn't give the Act's name."""
    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>42</docNumber></meta><main>'
            '<section identifier="/us/usc/t42/s3211"><num value="3211">§ 3211.</num><content>Text.</content>'
            '<sourceCredit>(Pub. L. 89–136, title VI, § 601.)</sourceCredit><notes><note topic="miscellaneous">'
            '<heading>Modernization of Environmental Reviews</heading><p>Pub. L. 118–272, div. B, title II, '
            '§ 2232, provided that:<quotedContent origin="/us/pl/118/272/dB/tII/s2232"><subsection>'
            '<num value="a">“(a)</num><heading> In General.—</heading><content>The Secretary shall report on '
            'exclusions (as defined in section 111 of the National Environmental Policy Act of 1969 '
            '(42 U.S.C. 4336e)).</content></subsection></quotedContent></p></note></notes></section>'
            '</main></uscDoc>').encode()

    def run_bill(self, cite):
        bill = parse_sections(
            f"<section><enum>2.</enum><text>Section 2232(a) of the Economic Development Reauthorization Act of 2024 "
            f"({cite}) is amended by striking <quote>section 111 of the National Environmental Policy Act of 1969 "
            "(42 U.S.C. 4336e)</quote> and inserting <quote>section 3 of the National Environmental Policy Act of "
            "1969</quote>.</text></section>")
        self.assertEqual(bill.problems, [])
        return bill.instructions[0].target, apply_bill(bill, {42: CodeTitle(self.CODE)})

    def test_public_law_in_a_note_citation_finds_the_note(self):
        target, outcome = self.run_bill("42 U.S.C. 3211 note; Public Law 118–272")
        self.assertEqual((target.is_note, target.usc_section, target.public_law), (True, "3211", "118-272"))
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        doc = find_doc(outcome, "42 U.S.C. § 3211 note (Pub. L. 118-272, § 2232)")
        self.assertIn("[-section 111 of the National Environmental Policy Act of 1969 (42 U.S.C. 4336e)-]"
                      "[+section 3 of the National Environmental Policy Act of 1969+]", marked(doc.root.children[0].body))

    def test_without_the_public_law_an_unnamed_note_is_still_flagged(self):
        target, outcome = self.run_bill("42 U.S.C. 3211 note")
        self.assertIsNone(target.public_law)
        self.assertEqual(outcome.applied, 0)
        self.assertIn("wasn't found in the Code notes", outcome.flags[0].message)

    def test_an_act_cited_by_public_law_takes_its_section_and_code_cite_from_each_instruction(self):
        # "The … Act (Public Law 118–159) is amended— (A) in section 1412(b)(2) (50 U.S.C. 98b note), by …"
        code = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>50</docNumber></meta><main>'
                '<section identifier="/us/usc/t50/s98b"><num value="98b">§ 98b.</num><content>Text.</content>'
                '<sourceCredit>(Pub. L. 79–520, § 3.)</sourceCredit><notes><note topic="miscellaneous">'
                '<heading>Stockpile</heading><p>Pub. L. 118–159, div. A, title XIV, § 1412, provided that:'
                '<quotedContent origin="/us/pl/118/159/dA/tXIV/s1412"><subsection><num value="b">“(b)</num>'
                '<heading> Review.—</heading><paragraph><num value="2">(2)</num><content>Exclusions under section 111 '
                'of the National Environmental Policy Act of 1969 (42 U.S.C. 4336e) apply.</content></paragraph>'
                '</subsection></quotedContent></p></note></notes></section></main></uscDoc>').encode()
        bill = parse_sections(
            "<section><enum>2.</enum><text>The Servicemember Quality of Life Improvement and National Defense "
            "Authorization Act for Fiscal Year 2025 (Public Law 118–159) is amended—</text><paragraph>"
            "<enum>(1)</enum><text>in section 1412(b)(2) (50 U.S.C. 98b note), by striking <quote>section 111 of the "
            "National Environmental Policy Act of 1969 (42 U.S.C. 4336e)</quote> and inserting <quote>section 3 of "
            "the National Environmental Policy Act of 1969</quote>.</text></paragraph></section>")
        self.assertEqual(bill.problems, [])
        target = bill.instructions[0].target
        self.assertEqual((target.section, target.path, target.title, target.usc_section, target.is_note),
                         ("1412", [("subsection", "b"), ("paragraph", "2")], 50, "98b", True))
        self.assertEqual(bill.titles(), [50])
        outcome = apply_bill(bill, {50: CodeTitle(code)})
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        doc = find_doc(outcome, "50 U.S.C. § 98b note (Pub. L. 118-159, § 1412)")
        self.assertIn("[+section 3 of the National Environmental Policy Act of 1969+]",
                      marked(doc.root.children[0].children[0].body))

    # The Code reproduces only subsection (c) of Pub. L. 118–159 § 3511, in a note to 46 U.S.C. 50505.
    PART_CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>46</docNumber></meta><main>'
                 '<section identifier="/us/usc/t46/s50505"><num value="50505">§ 50505.</num><content>Text.</content>'
                 '<sourceCredit>(Pub. L. 109–304, § 8.)</sourceCredit><notes><note topic="miscellaneous">'
                 '<heading>Categorical Exclusions</heading><p>Pub. L. 118–159, div. C, title XXXV, § 3511(c), provided '
                 'that:<quotedContent origin="/us/pl/118/159/dC/tXXXV/s3511/c"><paragraph><num value="3">“(3)</num>'
                 '<heading> Definitions.—</heading><subparagraph><num value="A">“(A)</num><content> The term '
                 '“categorical exclusion” has the meaning given in section 111 of the National Environmental Policy '
                 'Act of 1969 (42 U.S.C. 4336e).</content></subparagraph></paragraph></quotedContent></p></note>'
                 '</notes></section></main></uscDoc>').encode()

    def run_part(self, locator):
        bill = parse_sections(
            "<section><enum>2.</enum><text>The Servicemember Quality of Life Improvement and National Defense "
            "Authorization Act for Fiscal Year 2025 (Public Law 118–159) is amended—</text><paragraph>"
            f"<enum>(1)</enum><text>in section {locator} (46 U.S.C. 50505 note), by striking <quote>section 111 of "
            "the National Environmental Policy Act of 1969 (42 U.S.C. 4336e)</quote> and inserting <quote>section 3 "
            "of the National Environmental Policy Act of 1969</quote>.</text></paragraph></section>")
        self.assertEqual(bill.problems, [])
        return apply_bill(bill, {46: CodeTitle(self.PART_CODE)})

    def test_a_note_of_part_of_a_section_is_found_from_the_path(self):
        outcome = self.run_part("3511(c)(3)(A)")
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        doc = find_doc(outcome, "46 U.S.C. § 50505 note (Pub. L. 118-159, § 3511(c))")
        self.assertIn("[+section 3 of the National Environmental Policy Act of 1969+]",
                      marked(doc.root.children[0].children[0].body))

    def test_a_note_of_another_part_of_the_section_isnt_used(self):
        outcome = self.run_part("3511(d)(3)(A)")
        self.assertEqual(outcome.applied, 0)
        self.assertIn("wasn't found in the Code notes", outcome.flags[0].message)


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

    def test_with_subdivisions_sentences_count_only_within_the_introductory_text(self):
        self.apply([("sentence", 1)], old="Secretary", new="Administrator")
        self.assertEqual(marked(self.node.body), "The [-Secretary-][+Administrator+] shall—")
        with self.assertRaises(_NotApplied):
            self.apply([("sentence", 2)], old="Secretary", new="Administrator")


def new_node(kind, value, heading="", body="", children=()):
    return Node(kind, value, label=Text(f"({value})", INS), heading=Text(heading, INS), body=Text(body, INS),
                children=list(children))


class BlockTests(unittest.TestCase):
    def apply(self, node, old, blocks, within=()):
        doc = SimpleNamespace(title="Test", root=node)
        inst = Instruction(cite="§ 9", raw="", target=Target(), path=[], action="replace_with_block",
                           args={"old": old, "blocks": blocks}, within=list(within))
        Engine(SimpleNamespace(), {})._operate(inst, doc, node)

    def test_splitting_text_into_new_subdivisions_keeps_the_original_text(self):
        node = Node("subsection", "b", label=Text("(b)"), body=Text("The fee is due. The location fee is $5."))
        self.apply(node, "The location fee", [new_node("paragraph", "2", "Fee", "The location fee")], [("sentence", 2)])
        self.apply(node, "The fee", [new_node("paragraph", "1", "In general", "The fee")], [("sentence", 1)])
        self.assertEqual(marked(node.body), "[-The fee-]")
        first, second = node.children
        self.assertEqual(marked(first.body), "[+The fee+] is due. [-The location fee-]")
        self.assertEqual(marked(second.body), "[+The location fee+] is $5.")
        self.assertEqual(node.before_text(), "(b)The fee is due. The location fee is $5.")

    def test_subdivisions_split_off_earlier_move_under_the_new_one_that_precedes_them(self):
        # 54 U.S.C. 306108: S. 5653 § 2301(c)(1) makes the second sentence paragraph (2), then (c)(2) makes the
        # first sentence subsection (b) with paragraph (1). (2) belongs in (b), after (1).
        node = Node("section", "306108", label=Text("§ 306108."),
                    body=Text("The head of any Federal agency having direct jurisdiction shall act. "
                              "The head of the Federal agency shall afford comment."))
        self.apply(node, "The head of the Federal agency",
                   [new_node("paragraph", "2", "Opportunity to comment", "The head of any agency")], [("sentence", 2)])
        self.apply(node, "The head of any Federal agency having direct", [new_node(
            "subsection", "b", "Adverse effect", children=[new_node("paragraph", "1", "In general", "The head")])],
            [("sentence", 1)])
        (subsection,) = node.children
        self.assertEqual([(c.kind, c.value) for c in subsection.children], [("paragraph", "1"), ("paragraph", "2")])
        self.assertEqual(marked(subsection.children[0].body), "[+The head+] jurisdiction shall act. "
                                                              "[-The head of the Federal agency-]")
        self.assertEqual(marked(subsection.children[1].body), "[+The head of any agency+] shall afford comment.")
        self.assertEqual(node.before_text(), "§ 306108.The head of any Federal agency having direct jurisdiction "
                                             "shall act. The head of the Federal agency shall afford comment.")

    def test_existing_subdivisions_block_a_split(self):
        node = Node("paragraph", "1", label=Text("(1)"), body=Text("The Secretary shall—"),
                    children=[Node("subparagraph", "A", label=Text("(A)"), body=Text("act."))])
        with self.assertRaises(_NotApplied):
            self.apply(node, "The Secretary", [new_node("subparagraph", "A", "In general", "The Secretary")])

    def test_restructuring_in_place_follows_designations_down(self):
        node = Node("subsection", "e", label=Text("(e)"), heading=Text("General permits"), children=[
            Node("paragraph", "1", label=Text("(1)"), body=Text("In carrying out his functions, the Secretary"))])
        block = new_node("subsection", "e", "General permits", children=[
            new_node("paragraph", "1", "Permits authorized", "In carrying")])
        self.apply(node, "(e)(1) In carrying", [block])
        self.assertEqual(marked(node.heading), "General permits")
        paragraph = node.children[0]
        self.assertEqual(marked(paragraph.heading), "[+Permits authorized+]")
        self.assertEqual(marked(paragraph.body), "In carrying out his functions, the Secretary")

    def test_restructuring_in_place_adds_a_level(self):
        node = Node("subsection", "c", label=Text("(c)"), heading=Text("Denial"), body=Text("The Administrator may act."))
        block = new_node("subsection", "c", "Specification", children=[
            new_node("paragraph", "1", "In general", "The Administrator")])
        self.apply(node, "(c) The Administrator", [block])
        self.assertEqual(marked(node.heading), "[-Denial-][+Specification+]")
        self.assertEqual(marked(node.body), "[-The Administrator-]")
        self.assertEqual(marked(node.children[0].body), "[+The Administrator+] may act.")

    def test_mismatched_designation_is_refused(self):
        node = Node("subsection", "t", label=Text("(t)"), body=Text("Nothing in this section"))
        with self.assertRaises(_NotApplied):
            self.apply(node, "(t) Nothing in this section", [new_node("subsection", "u", "Savings", "Nothing")])


class FollowingTests(unittest.TestCase):
    """'… and inserting the following:' with run-in text before new subdivisions, text added at the end, and
    new subdivisions inserted before others: the shapes in S. 5653 §§ 2101(f)(2), 2233(a), 2252(c)(3) and
    2301(c)(3)."""
    CODE = ('<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0"><meta><docNumber>16</docNumber></meta><main>'
            '<section identifier="/us/usc/t16/s824p"><num value="824p">§ 824p.</num>'
            '<subsection><num value="a">(a)</num><content>The Department shall act as the lead agency.</content>'
            '</subsection><subsection><num value="b">(b)</num><content>Except under section 388(d) of the Energy '
            'Policy Act of 2005, the Secretary shall issue a lease on a competitive basis.</content></subsection>'
            '<subsection><num value="c">(c)</num><content>The fee is due, as defined in section 2(f), in full.'
            '</content></subsection><subsection><num value="e">(e)</num><content>Final.</content></subsection>'
            '<sourceCredit>(June 10, 1920, ch. 285, pt. II, § 216.)</sourceCredit></section></main></uscDoc>').encode()

    def run_bill(self, *instructions):
        items = "".join(f"<paragraph><enum>({n})</enum>{text}</paragraph>" for n, text in enumerate(instructions, 1))
        bill = parse_sections("<section><enum>2.</enum><text>Section 216 of the Federal Power Act (16 U.S.C. 824p) "
                              f"is amended—</text>{items}</section>")
        self.assertEqual(bill.problems, [])
        outcome = apply_bill(bill, {16: CodeTitle(self.CODE)})
        return outcome, find_doc(outcome, "16 U.S.C. § 824p").root

    def test_end_punctuation_gives_way_to_run_in_text_and_new_subdivisions(self):
        outcome, root = self.run_bill(
            "<text>in subsection (a), by striking the period at the end and inserting the following:</text>"
            "<quoted-block><text>, except that—</text><paragraph><enum>(1)</enum><text>the Commission shall act; "
            "and</text></paragraph><paragraph><enum>(2)</enum><text>the Secretary shall act offshore.</text>"
            "</paragraph><after-quoted-block>;</after-quoted-block></quoted-block>")
        self.assertEqual((outcome.applied, outcome.flags), (1, []))
        subsection = root.children[0]
        self.assertEqual(marked(subsection.body), "The Department shall act as the lead agency[-.-][+, except that—+]")
        self.assertEqual([c.value for c in subsection.children], ["1", "2"])

    def test_run_in_text_opened_in_the_instruction_and_a_new_subdivision_after_the_one_named(self):
        outcome, root = self.run_bill(
            "<text>in subsection (b), by striking <quote>, the Secretary shall issue a lease</quote> and inserting "
            "the following: “(16 U.S.C. 824p note), the Secretary—</text><quoted-block><paragraph><enum>(1)</enum>"
            "<text>shall issue a lease</text></paragraph><after-quoted-block>; and</after-quoted-block>"
            "</quoted-block>",
            "<text>in subsection (b)(1) (as so designated), by striking the period at the end and inserting the "
            "following: “; and</text><quoted-block><paragraph><enum>(2)</enum><text>may issue a permit.</text>"
            "</paragraph><after-quoted-block>.</after-quoted-block></quoted-block>")
        self.assertEqual((outcome.applied, outcome.flags), (2, []))
        subsection = root.children[1]
        self.assertEqual(marked(subsection.body), "Except under section 388(d) of the Energy Policy Act of 2005"
                         "[-, the Secretary shall issue a lease-][+ (16 U.S.C. 824p note), the Secretary—+]")
        first, second = subsection.children
        self.assertEqual(marked(first.body), "[+shall issue a lease+] on a competitive basis[-.-][+; and+]")
        self.assertEqual((second.value, marked(second.body)), ("2", "[+may issue a permit.+]"))

    def test_text_added_at_the_end_is_a_new_sentence(self):
        outcome, root = self.run_bill("<text>in subsection (e), by adding at the end the following: <quote>The "
                                      "Commission shall publish it.</quote>.</text>")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(marked(root.children[3].body), "Final.[+ The Commission shall publish it.+]")

    def test_new_subdivisions_inserted_before_one_named(self):
        outcome, root = self.run_bill(
            "<text>by inserting before subsection (e) the following:</text><quoted-block><subsection>"
            "<enum>(d)</enum><text>Reports are due.</text></subsection><after-quoted-block>;</after-quoted-block>"
            "</quoted-block>")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual([c.value for c in root.children], ["a", "b", "c", "d", "e"])

    def test_a_locator_without_a_comma(self):
        outcome, root = self.run_bill("<text>in subsection (c) by striking <quote>, as defined in section 2(f),"
                                      "</quote>.</text>")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(marked(root.children[2].body), "The fee is due[-, as defined in section 2(f),-] in full.")

    def test_run_in_text_with_an_operation_that_cant_place_it_isnt_recognized(self):
        bill = parse_sections(
            "<section><enum>2.</enum><text>Section 216 of the Federal Power Act (16 U.S.C. 824p) is amended by "
            "adding at the end the following:</text><quoted-block><text>Also—</text><subsection><enum>(f)</enum>"
            "<text>more.</text></subsection></quoted-block></section>")
        self.assertEqual([p.reason for p in bill.problems], ["instruction wording isn't a recognized pattern"])


class OpeningTests(unittest.TestCase):
    def apply(self, node, end, block, n=None, unit="section", positive_law=False):
        doc = SimpleNamespace(title="Test", root=node, basis="code", order=(30, (226, ""), 0))
        inst = Instruction(cite="§ 9", raw="", target=Target(), path=[], action="replace_opening",
                           args={"unit": unit, "end": end, "n": n, "blocks": [block]})
        engine = Engine(SimpleNamespace(), {30: SimpleNamespace(positive_law=positive_law, release_point="")})
        engine._operate(inst, doc, node)
        return engine.outcome.flags

    def test_designation_span_restructures_the_opening(self):  # NEPA § 2, 42 U.S.C. 4321
        node = Node("section", "4321", label=Text("§ 4321."), heading=Text("Declaration of purpose"),
                    body=Text("The purposes of this chapter are: To declare a policy; to promote efforts."))
        block = Node("section", "2", label=Text("Sec. 2.", INS), heading=Text("Purposes", INS), children=[
            new_node("subsection", "a", "Purposes", "The purposes of this Act are—", [
                new_node("paragraph", "1", "", "to declare")])])
        self.apply(node, "are: To declare", block)
        self.assertEqual(marked(node.label), "§ 4321.")
        self.assertEqual(marked(node.heading), "[-Declaration of purpose-][+Purposes+]")
        self.assertEqual(marked(node.body), "[-The purposes of this chapter are: To declare-]")
        paragraph = node.children[0].children[0]
        self.assertEqual(marked(paragraph.body), "[+to declare+] a policy; to promote efforts.")
        self.assertEqual(node.before_text(), "§ 4321.Declaration of purposeThe purposes of this chapter are: To "
                                             "declare a policy; to promote efforts.")

    def test_code_catchline_stays_when_the_bill_gives_no_heading(self):  # 30 U.S.C. 226(g)
        node = Node("subsection", "g", label=Text("(g)"), heading=Text("Regulation of activities"),
                    body=Text("The Secretary of the Interior, or the Secretary of Agriculture, shall act. Then more."))
        block = new_node("subsection", "g", children=[new_node("paragraph", "1", "", "The Secretary of the Interior, or")])
        self.apply(node, "Secretary of the Interior, or", block, n=1, unit="subsection")
        self.assertEqual(marked(node.heading), "Regulation of activities")
        self.assertEqual(marked(node.children[0].body),
                         "[+The Secretary of the Interior, or+] the Secretary of Agriculture, shall act. Then more.")

    def test_heading_in_a_positive_law_title_is_struck_and_noted(self):  # e.g. 54 U.S.C.
        node = Node("subsection", "a", label=Text("(a)"), heading=Text("In general"), body=Text("Property may be listed."))
        block = new_node("subsection", "a", children=[new_node("paragraph", "1", "", "Property")])
        flags = self.apply(node, "Property", block, unit="subsection", positive_law=True)
        self.assertEqual(marked(node.heading), "[-In general-]")
        self.assertEqual([(f.blocking, "heading" in f.message) for f in flags], [(False, True)])

    def test_end_phrase_must_be_in_the_named_sentence(self):
        node = Node("subsection", "g", label=Text("(g)"), body=Text("First sentence. The Secretary acts."))
        with self.assertRaises(_NotApplied):
            self.apply(node, "The Secretary", new_node("subsection", "g", body="New"), n=1, unit="subsection")

    def test_new_provision_at_the_same_level_follows_the_original(self):  # NEPA § 1101(2)(A)
        parent = Node("subsection", "a", label=Text("(a)"), children=[
            Node("paragraph", "1", label=Text("(1)", INS), body=Text("to declare a policy; to promote efforts."))])
        doc = SimpleNamespace(title="Test", root=parent)
        inst = Instruction(cite="§ 9", raw="", target=Target(), path=[("paragraph", "1")], action="replace_with_block",
                           args={"old": "to promote", "blocks": [new_node("paragraph", "2", "", "to promote")]})
        engine = Engine(SimpleNamespace(), {})
        engine._operate(inst, doc, engine._walk(parent, inst.path, inst, doc))
        first, second = parent.children
        self.assertEqual(marked(first.body), "to declare a policy; [-to promote-]")
        self.assertEqual((second.value, marked(second.body)), ("2", "[+to promote+] efforts."))


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

    def test_the_cover_page_links_to_the_appendix(self):
        links = [h for h in self.document.iter(f"{W}hyperlink") if h.get(f"{W}anchor")]
        self.assertEqual(["".join(h.itertext()) for h in links], ["appendix"])
        marks = {b.get(f"{W}name"): b for b in self.document.iter(f"{W}bookmarkStart")}
        self.assertIn(links[0].get(f"{W}anchor"), marks)
        heading = next(p for p in self.document.iter(f"{W}p") if marks[links[0].get(f"{W}anchor")] in list(p))
        self.assertTrue("".join(heading.itertext()).startswith("Appendix: provisions that don't amend"))

    def test_no_appendix_no_link(self):
        bill = parse_sections("<section><enum>2.</enum><text>Section 201 of the Federal Power Act (16 U.S.C. 824) "
                              "is amended by striking <quote>fair</quote> and inserting <quote>just</quote>."
                              "</text></section>")
        outcome = apply_bill(bill, {16: CodeTitle(ActNameTests.CODE)})
        self.assertEqual(bill.provisions, [])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.docx")
            write_docx(path, bill, outcome, "https://example.gov", "ih", verify(bill, outcome))
            document = zipfile.ZipFile(path).read("word/document.xml").decode()
        self.assertNotIn("w:anchor=", document)
        self.assertNotIn("bookmarkStart", document)

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
