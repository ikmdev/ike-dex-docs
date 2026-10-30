"""Regression tests for the conversion utilities.

    utilities/.venv/bin/python -m unittest discover -s utilities -p "test_*.py"

They need no sample documents: pages, lines and cells are built in memory. Tests that render AsciiDoc
skip themselves when ``asciidoctor`` is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pdf_to_asciidoc as m
import picture_tables as pt


def make_line(text: str, x: float, top: float, size: float = 10.0, bold: bool = False, page: int = 1) -> m.Line:
    chars, at = [], x
    for ch in text:
        chars.append(m.Ch(ch, at, at + size * 0.5, top, top + size, size, bold, False, None, page))
        at += size * 0.5
    return m.Line(chars, top, top + size, x, at, size, top + size, None, page)


def make_block(text: str, x: float, top: float) -> m.Block:
    block = m._lines_to_block([make_line(text, x, top, size=12)])
    block.owner = int(top)
    m.split_label(block)
    return block


def render_html(adoc: str) -> str:
    done = subprocess.run(["asciidoctor", "-s", "-o", "-", "-"], input=adoc, capture_output=True, text=True)
    return done.stdout


class Labels(unittest.TestCase):
    def test_label_styles(self) -> None:
        cases = {
            "1. x": ("num", 1), "1) x": ("num", 1), "(1) x": ("num", 1), "a. x": ("alpha", 1),
            "(iv) x": ("roman", 4), "II. x": ("ROMAN", 2), "A) x": ("ALPHA", 1),
            "- x": ("bullet", 0), "– x": ("bullet", 0), "• x": ("bullet", 0),
        }
        for text, (kind, value) in cases.items():
            label = m.parse_label(text)
            self.assertEqual((label.kind, label.value), (kind, value), text)

    def test_not_labels(self) -> None:
        for text in ("-5 mg", "(a. x", "Ab. x", "MD. x", "The text", "1.5 mg"):
            self.assertIsNone(m.parse_label(text), text)

    def test_heading_false_positives(self) -> None:
        def classify(text: str):
            block = m.Block(kind="para")
            block.lines = [object()]
            block.units = [(c, (True, False, None)) for c in text]
            return m.Headings().classify(block)

        for text in ("I agree to the terms", "10 mg daily", "MD Anderson Cancer Center", "CD Player"):
            self.assertIsNone(classify(text), text)
        self.assertEqual(classify("10.1 General")[0], "decimal1")
        self.assertEqual(classify("A. Alpha")[0], "ALPHA")

    def test_heading_schemes_nest_in_the_order_they_open(self) -> None:
        stack = m.Headings()
        self.assertEqual([stack.level("ROMAN"), stack.level("ALPHA"), stack.level("ROMAN"), stack.level("ALPHA")], [2, 3, 2, 3])


class Escaping(unittest.TestCase):
    def test_guard_line_start(self) -> None:
        for text in (".5 mg twice daily.", "NOTE: take care", "- not a bullet", "|===", "// comment"):
            self.assertTrue(m.guard_line_start(text).startswith("{empty}"), text)
        self.assertEqual(m.guard_line_start("Plain text."), "Plain text.")

    @unittest.skipUnless(shutil.which("asciidoctor"), "asciidoctor not installed")
    def test_text_survives_asciidoctor(self) -> None:
        for text, expect in (
            ("C++ and C++ code.", "C++ and C++ code."),
            ("snake_case_name and http://x.org/a_b_c", "snake_case_name and http://x.org/a_b_c"),
            ("a * b and 2 ^ 3 and ~5", "a * b and 2 ^ 3 and ~5"),
        ):
            html = render_html(m.guard_line_start(m.escape(text)))
            body = html.replace("<p>", "").replace("</p>", "").strip()
            self.assertIn(expect, body.replace("&#43;", "+"), text)

    @unittest.skipUnless(shutil.which("asciidoctor"), "asciidoctor not installed")
    def test_leading_dot_and_admonition_stay_text(self) -> None:
        for text in (".5 mg twice daily.", "NOTE: not an admonition"):
            html = render_html(m.guard_line_start(m.escape(text)))
            self.assertNotIn("admonitionblock", html)
            self.assertNotIn('class="title"', html)


class Paragraphs(unittest.TestCase):
    def test_generous_line_spacing_stays_one_paragraph(self) -> None:
        # 10 pt type on a 15 pt pitch leaves a 5 pt gap: more than a third of the size, but the usual pitch
        lines = [make_line(f"line {i} of one wrapped paragraph", 72, 100 + 15 * i) for i in range(5)]
        blocks = m.flow_blocks(lines, layout=True, right_edge=400, pitch=m._line_pitch(lines))
        self.assertEqual(len(blocks), 1)

    def test_extra_space_breaks_a_paragraph(self) -> None:
        tops = [100, 115, 130, 145, 175, 190, 205]
        lines = [make_line(f"line {i} of the text", 72, top) for i, top in enumerate(tops)]
        blocks = m.flow_blocks(lines, layout=True, right_edge=400, pitch=m._line_pitch(lines))
        self.assertEqual(len(blocks), 2)


class Cells(unittest.TestCase):
    def converter(self) -> m.TaggedPdfConverter:
        return object.__new__(m.TaggedPdfConverter)

    def cell(self, rows: list[str], header: bool = False) -> m.Cell:
        chars: list[m.Ch] = []
        for i, text in enumerate(rows):
            chars += make_line(text, 10, 10 + 14 * i).chars
        return m.Cell(header=header, chars=chars, rect=None, page=1)

    def test_non_bullet_labels_survive(self) -> None:
        for text in ("A. bold cell alpha", "1. first", "(a) next", "II. roman"):
            rendered, _ = self.converter()._render_cell(self.cell([text]))
            self.assertEqual(rendered.replace("++", ""), text)

    def test_paragraph_then_list_has_a_blank_line(self) -> None:
        rendered, rich = self.converter()._render_cell(self.cell(["Includes alpha:", "• first item", "• second item"]))
        self.assertTrue(rich)
        self.assertEqual(rendered, "Includes alpha:\n\n* first item\n* second item")


class Borders(unittest.TestCase):
    def borders(self, horizontal: list[tuple], vertical: list[tuple]) -> m.PageBorders:
        found = object.__new__(m.PageBorders)
        found.horizontal, found.vertical = horizontal, vertical
        return found

    def test_cells_with_no_border_between_them_merge(self) -> None:
        # two stacked cells in column 1 with no rule between them; column 2 keeps its rule at y=20
        rects = [(0, 0, 50, 20), (0, 20, 50, 40), (50, 0, 100, 20), (50, 20, 100, 40)]
        borders = self.borders(
            horizontal=[(0, 0, 100), (20, 50, 100), (40, 0, 100)],
            vertical=[(0, 0, 40), (50, 0, 40), (100, 0, 40)],
        )
        merged = borders.merge_cells(rects)
        self.assertEqual(len(merged), 3)
        self.assertIn((0, 0, 50, 40), merged)

    def test_unseen_borders_merge_nothing(self) -> None:
        rects = [(0, 0, 50, 20), (0, 20, 50, 40), (50, 0, 100, 20), (50, 20, 100, 40)]
        self.assertEqual(len(self.borders([], []).merge_cells(rects)), 4)


class Voting(unittest.TestCase):
    def test_punctuation_is_plausible(self) -> None:
        text, flagged = pt.vote({"rapidocr": "Ratio: 3", "tesseract": "Ratio: 3", "easyocr": "Ratio 3"}, {"ratio"}, True)
        self.assertEqual(text, "Ratio: 3")
        self.assertFalse(flagged)

    def test_overruled_majority_is_flagged(self) -> None:
        # the majority reading fails the vocabulary check; the lone engine passes it: say so
        text, flagged = pt.vote({"rapidocr": "Dayz", "tesseract": "Dayz", "easyocr": "Days"}, {"days"}, True)
        self.assertEqual(text, "Days")
        self.assertTrue(flagged)

    def test_malformed_percent_does_not_raise(self) -> None:
        self.assertTrue(pt.is_plausible("97.7.% (1/2)", None) in (True, False))

    def test_unread_ink_is_flagged(self) -> None:
        self.assertTrue(pt.vote({"rapidocr": "", "tesseract": "", "easyocr": ""}, None, True)[1])


class Lists(unittest.TestCase):
    def emit(self, items: list[tuple[str, float]]) -> str:
        converter = object.__new__(m.TaggedPdfConverter)
        converter.tables = []
        blocks = [make_block(text, x, 20 * i) for i, (text, x) in enumerate(items)]
        return "\n".join(piece.removeprefix(converter.LIST) for piece in converter._emit(blocks))

    def test_roman_under_letter(self) -> None:
        text = self.emit([("A. one", 72), ("I. sub", 90), ("II. sub", 90), ("B. two", 72)])
        self.assertIn("[upperalpha]", text)
        self.assertIn("[upperroman]", text)

    @unittest.skipUnless(shutil.which("asciidoctor"), "asciidoctor not installed")
    def test_nested_markers_render_as_nested_lists(self) -> None:
        html = render_html("= T\n\n" + self.emit([("1. One", 72), ("(a) alpha", 90), ("(i) roman", 108), ("2. Two", 72)]))
        self.assertEqual(html.count("<ol"), 3)
        self.assertIn("lowerroman", html)


if __name__ == "__main__":
    unittest.main()
