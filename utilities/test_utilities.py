"""Regression tests for the conversion utilities.

    utilities/.venv/bin/python -m unittest discover -s utilities -p "test_*.py"

They need no sample documents: pages, lines and cells are built in memory. Tests that render AsciiDoc
skip themselves when ``asciidoctor`` is not installed.
"""

from __future__ import annotations

import collections
import dataclasses
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_fidelity as cf
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


def make_table(rows: list[list[str]], page: int, top: float = 50.0, header: Any = 1, x=(0, 100, 200, 300)) -> m.Table:
    """A table of text cells with rectangles; ``header`` is a row count (the first rows) or a set of row numbers."""
    header_rows = set(range(header)) if isinstance(header, int) else set(header)
    cells = []
    for r, texts in enumerate(rows):
        row = []
        for c, text in enumerate(texts):
            ch = [m.Ch(t, 10, 15, top + 20 * r, top + 20 * r + 10, 10.0, False, False, None, page) for t in text]
            row.append(m.Cell(header=r in header_rows, chars=ch, rect=(x[c], top + 20 * r, x[c + 1], top + 20 * r + 20), page=page))
        cells.append(row)
    return m.Table(rows=cells, pages={page}, first_page=page)


class Tables(unittest.TestCase):
    def converter(self) -> m.TaggedPdfConverter:
        return object.__new__(m.TaggedPdfConverter)

    def test_repeated_header_is_written_once(self) -> None:
        table = make_table([["Code", "Name"], ["1", "a"], ["Code", "Name"], ["2", "b"]], page=1, header={0, 2})
        text = self.converter()._render_table(table, 612)
        self.assertEqual(text.count("Code"), 1)
        self.assertIn("options=header", text)
        self.assertIn("|2 |b", text)

    def test_header_copies_in_a_row_are_one_header(self) -> None:
        table = make_table([["Code", "Name"], ["Code", "Name"], ["1", "a"]], page=1, header=2)
        self.assertEqual(m._header_row_count(table), 1)
        m._drop_repeated_headers(table)
        self.assertEqual(len(table.rows), 2)

    def test_a_repeated_data_row_is_kept(self) -> None:
        table = make_table([["Code", "Name"], ["1", "a"], ["1", "a"]], page=1)
        self.assertEqual(m._drop_repeated_headers(table), 0)

    def test_table_continued_on_the_next_page_is_one_table(self) -> None:
        first = make_table([["Code", "Name"], ["1", "a"]], page=1, top=700)
        second = make_table([["Code", "Name"], ["2", "b"]], page=2, top=40)
        self.assertTrue(m._continues(first, second, 792))
        first.rows += second.rows
        text = self.converter()._render_table(first, 612)
        self.assertEqual(text.count("Code"), 1)
        self.assertIn("|1 |a", text)
        self.assertIn("|2 |b", text)

    def test_continuation_on_a_page_with_other_margins(self) -> None:
        first = make_table([["Code", "Name"], ["1", "a"]], page=1, top=700, x=(54, 154, 254, 354))
        second = make_table([["Code", "Name"], ["2", "b"]], page=2, top=40, x=(72, 172, 272, 372))
        self.assertTrue(m._continues(first, second, 792))

    def test_next_table_with_other_columns_is_a_new_table(self) -> None:
        first = make_table([["Code", "Name"], ["1", "a"]], page=1, top=700)
        other = make_table([["Code", "Name"], ["2", "b"]], page=2, top=40, x=(0, 50, 200))
        self.assertFalse(m._continues(first, other, 792))

    def test_next_table_mid_page_is_a_new_table(self) -> None:
        first = make_table([["Code", "Name"], ["1", "a"]], page=1, top=100)
        other = make_table([["Size", "Type"], ["2", "b"]], page=2, top=40)
        self.assertFalse(m._continues(first, other, 792))

    def test_one_column_table_with_a_header_is_a_table(self) -> None:
        def row(*kinds: str) -> dict:
            return {"type": "TR", "children": [{"type": k} for k in kinds]}

        self.assertTrue(m._is_data_table([row("TH"), row("TD")]))
        self.assertFalse(m._is_data_table([row("TD")]))  # a lone box
        self.assertFalse(m._is_data_table([row("TD"), row("TD")]))
        self.assertTrue(m._is_data_table([row("TD", "TD")]))

    def test_header_with_a_vertical_span_is_not_the_asciidoc_header_row(self) -> None:
        # a two-row header: "Group" spans both rows, so the first row cannot be the header row on its own
        table = make_table([["Group", "Measures", "Notes"], ["", "A", ""], ["1", "x", "y"]], page=1, header=2)
        for cell, span in zip(table.rows[0], (2, 1, 2)):
            cell.tag_rowspan = span
        del table.rows[1][0], table.rows[1][1]
        table.rows[1][0].tag_colspan = 1
        text = self.converter()._render_table(table, 612)
        self.assertNotIn("options=header", text)
        self.assertIn("options=noheader", text)  # or Asciidoctor makes row 1 a header and shifts row 2
        self.assertIn(".2+h|Group", text)
        self.assertIn("h|A", text)

    @unittest.skipUnless(shutil.which("asciidoctor"), "asciidoctor not installed")
    def test_two_row_header_renders_in_place(self) -> None:
        table = make_table([["Group", "Measures", "Notes"], ["", "A", ""], ["1", "x", "y"]], page=1, header=2)
        for cell, span in zip(table.rows[0], (2, 1, 2)):
            cell.tag_rowspan = span
        del table.rows[1][0], table.rows[1][1]
        html = render_html(self.converter()._render_table(table, 612))
        self.assertNotIn("<thead>", html)
        self.assertEqual(html.count("<th "), 4)  # Group, Measures, Notes, A: row two starts under Measures
        self.assertEqual(html.count("rowspan"), 2)

    def test_tag_spans_place_cells(self) -> None:
        table = make_table([["a", "b", "c"], ["d", "e"]], page=1, header=0)
        table.rows[0][0].tag_rowspan = 2
        table.rows[0][1].tag_colspan = 2
        del table.rows[0][2]
        self.assertTrue(m._grid_from_tags(table))
        self.assertEqual([c.col for c in table.rows[1]], [1, 2])

    def test_rows_that_do_not_line_up_are_refused(self) -> None:
        table = make_table([["a", "b"], ["c"]], page=1, header=0)
        table.rows[0][0].tag_colspan = 2
        self.assertTrue(m._grid_from_tags(table) is False)


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
    def test_table_with_leading_comments_stays_in_the_list_item(self) -> None:
        converter = object.__new__(m.TaggedPdfConverter)
        converter.tables = []
        items = [make_block("1. One", 72, 0)]
        table = m.Block(kind="image", page=1, top=20, bottom=40, x0=90, x1=300)
        table.payload = None
        converter._render_image = lambda block: ["// note one\n// note two\n|===\n|a |b\n|==="]  # type: ignore[method-assign]
        converter.pdf = type("P", (), {"pages": [type("G", (), {"width": 612})()]})()
        after = make_block("Text after the table", 90, 60)
        out = "\n".join(p.removeprefix(converter.LIST) for p in converter._emit(items + [table, after]))
        html = render_html("= T\n\n" + out)
        self.assertNotIn("<p>+", html)
        self.assertEqual(html.count("<ol"), 1)
        self.assertIn("Text after the table", html)

    @unittest.skipUnless(shutil.which("asciidoctor"), "asciidoctor not installed")
    def test_item_after_an_attached_paragraph_stays_a_list_item(self) -> None:
        converter = object.__new__(m.TaggedPdfConverter)
        converter.tables = []
        blocks = [make_block("1. One", 72, 0), make_block("(a) alpha", 90, 20),
                  make_block("Attached paragraph", 108, 40), make_block("(b) beta", 90, 60)]
        text = converter._join(converter._emit(blocks))
        self.assertEqual(render_html(text).count("<li>"), 3)

    def test_list_carries_on_over_a_page_with_mirrored_margins(self) -> None:
        def body(x: float, page: int, top: float) -> m.Block:
            block = m._lines_to_block([make_line(f"body line {i}", x, top + 14 * i, size=12, page=page) for i in range(4)])
            block.page = page
            return block

        def item(text: str, x: float, page: int, top: float) -> m.Block:
            block = make_block(text, x, top)
            block.page = page
            return block

        converter = object.__new__(m.TaggedPdfConverter)
        converter.tables = []
        seq = [body(72, 1, 20), item("1. One", 72, 1, 100), item("2. Two", 72, 1, 120), item("3. Three", 72, 1, 140),
               body(54, 2, 20), item("4. Four", 54, 2, 100), item("5. Five", 54, 2, 120),
               body(72, 3, 20), item("6. Six", 72, 3, 100), body(54, 4, 20), item("7. Seven", 54, 4, 100)]
        converter._align_margins(seq)
        text = converter._join(converter._emit(seq))
        self.assertNotIn("..", text)
        self.assertNotIn("[start", text)

    @unittest.skipUnless(shutil.which("asciidoctor"), "asciidoctor not installed")
    def test_nested_markers_render_as_nested_lists(self) -> None:
        html = render_html("= T\n\n" + self.emit([("1. One", 72), ("(a) alpha", 90), ("(i) roman", 108), ("2. Two", 72)]))
        self.assertEqual(html.count("<ol"), 3)
        self.assertIn("lowerroman", html)


def converter_with(tables: list[m.Table] | None = None) -> m.TaggedPdfConverter:
    converter = object.__new__(m.TaggedPdfConverter)
    converter.tables = tables or []
    converter.owner_kind = {}
    converter._captioned = set()
    converter._callouts = 0
    converter.pdf = type("P", (), {"pages": [type("G", (), {"width": 612, "height": 792})()] * 40})()
    return converter


class Callouts(unittest.TestCase):
    def page_chars(self, label_x: float = 83) -> list[m.Ch]:
        chars: list[m.Ch] = []
        for top, text in ((100, "The first line of the note"), (110, "continues with a second line"), (120, "and ends on a third.")):
            chars += make_line(text, 108, top, size=9).chars
        chars += make_line("Note", label_x, 110, size=9, bold=True).chars
        return chars

    def test_margin_label_leaves_the_text_whole(self) -> None:
        rest, labels = m.take_margin_labels(self.page_chars())
        self.assertEqual([(lb.kind, lb.column) for lb in labels], [("NOTE", 108)])
        lines = [ln for ln in m.group_lines(rest) if not ln.blank]
        self.assertEqual(len(lines), 3)
        self.assertEqual(len(m.flow_blocks(lines, layout=True)), 1)

    def test_two_callouts_a_few_lines_apart_both_keep_their_label(self) -> None:
        chars: list[m.Ch] = []
        for top, text in ((100, "First note text on a line"), (108, "and its second line"),
                          (140, "Second note text on a line"), (148, "and its second line")):
            chars += make_line(text, 108, top, size=8).chars
        chars += make_line("Note", 83, 104, size=9, bold=True).chars
        chars += make_line("Note", 83, 144, size=9, bold=True).chars
        rest, labels = m.take_margin_labels(chars)
        self.assertEqual(len(labels), 2)
        self.assertNotIn("N", "".join(c.text for c in rest if c.x0 < 100))

    def test_label_at_the_text_indent_is_text(self) -> None:
        rest, labels = m.take_margin_labels(make_line("Note this is a sentence", 108, 100, size=9, bold=True).chars)
        self.assertEqual(labels, [])
        rest, labels = m.take_margin_labels(make_line("Note: a sentence", 108, 100, size=9).chars)
        self.assertEqual(labels, [])

    def test_ruled_box_groups_its_paragraphs(self) -> None:
        rest, labels = m.take_margin_labels(self.page_chars())
        inside = m._lines_to_block([make_line("inside the box", 108, 112, size=9)])
        outside = m._lines_to_block([make_line("below the box", 108, 160, size=9)])
        extra = m.assign_callouts([inside, outside], labels, [(95, 108, 558), (135, 108, 558)], 0)
        self.assertEqual(extra, [])
        self.assertEqual(inside.callout, (0, "NOTE"))
        self.assertIsNone(outside.callout)

    def test_label_with_no_text_beside_it_is_kept(self) -> None:
        label = m.MarginLabel("NOTE", make_line("Note", 83, 300, size=9, bold=True), 108)
        extra = m.assign_callouts([], [label], [], 0)
        self.assertEqual(len(extra), 1)

    def test_label_in_the_text_becomes_the_admonition(self) -> None:
        for text, kind in (("Note: keep it dry", "NOTE"), ("Important: Start soon", "IMPORTANT"),
                           ("Note Important: Start soon", "IMPORTANT"), ("WARNING: hot", "WARNING")):
            block = m._lines_to_block([make_line(text, 72, 100)])
            m.callout_in_text(block)
            self.assertEqual(block.callout[1], kind, text)
            self.assertNotIn(":", m.plain(block.units)[:3])
        alone = m._lines_to_block([make_line("Note:", 72, 100)])
        m.callout_in_text(alone)
        self.assertIsNone(alone.callout)
        plain = m._lines_to_block([make_line("Notes on method", 72, 100)])
        m.callout_in_text(plain)
        self.assertIsNone(plain.callout)

    def test_single_paragraph_is_one_line_and_several_are_a_block(self) -> None:
        converter = converter_with()
        one = make_block("Keep it dry.", 72, 0)
        group = converter._group_callouts([self.marked(one, 1, "NOTE")])
        self.assertEqual(converter._render_callout(group[0]), "NOTE: Keep it dry.")
        two = [self.marked(make_block("First.", 72, 0), 2, "IMPORTANT"), self.marked(make_block("- second", 72, 20), 2, "IMPORTANT")]
        group = converter._group_callouts(two)
        self.assertEqual(len(group), 1)
        self.assertEqual(converter._render_callout(group[0]), "[IMPORTANT]\n====\nFirst.\n\n* second\n====")

    def test_title_and_notice_in_two_sizes_are_two_lines(self) -> None:
        chars = make_line("DEVICE TITLE", 10, 100, size=20).chars  # ends at x=130
        chars += make_line("Notice: a note", 160, 106, size=8).chars  # a 30 pt gap: two small ems, half a large one
        self.assertEqual(len([ln for ln in m.group_lines(chars) if not ln.blank]), 2)
        same = make_line("two words", 10, 100, size=10).chars + make_line("here", 70, 100, size=10).chars
        self.assertEqual(len([ln for ln in m.group_lines(same) if not ln.blank]), 1)

    def test_a_label_the_admonition_lacks_is_kept_as_its_caption(self) -> None:
        converter = converter_with()
        block = m._lines_to_block([make_line("Notice: Any discrepancies?", 72, 100)])
        m.callout_in_text(block)
        self.assertEqual((block.callout[1], block.callout_word), ("NOTE", "Notice"))
        group = converter._group_callouts([block])
        self.assertEqual(converter._render_callout(group[0]), '[NOTE,caption="Notice"]\nAny discrepancies?')

    def test_lines_under_a_labelled_callout_join_its_paragraph(self) -> None:
        converter = converter_with()
        first = m._lines_to_block([make_line("Notice: Any question?", 324, 156, size=8)])
        second = m._lines_to_block([make_line("If so, write to us.", 324, 167, size=8)])  # tagged apart
        m.callout_in_text(first)
        other = m._lines_to_block([make_line("A separate paragraph far below", 324, 260, size=8)])
        seq = converter._absorb_continuations([first, second, other])
        self.assertEqual(len(seq), 2)
        self.assertEqual(m.plain(seq[0].units), "Any question? If so, write to us.")
        self.assertIsNone(seq[1].callout)

    @staticmethod
    def marked(block: m.Block, group: int, kind: str) -> m.Block:
        block.callout = (group, kind)
        return block


class Captions(unittest.TestCase):
    def figure(self, top: float) -> m.Block:
        return m.Block(kind="image", page=1, top=top, bottom=top + 100, x0=72, x1=300, payload=(None, (72, top, 300, top + 100), {}))

    def caption(self, text: str, top: float, bold: bool = True) -> m.Block:
        return m._lines_to_block([make_line(text, 72, top, bold=bold)])

    def test_figure_caption_below_the_picture_becomes_its_title(self) -> None:
        converter = converter_with()
        picture = self.figure(100)
        seq = converter._attach_captions([picture, self.caption("Figure 3. A swab being held", 205)])
        self.assertEqual(len(seq), 1)
        self.assertEqual(picture.caption, ("Figure 3. ", "figure-3", "A swab being held"))

    def test_table_caption_above_the_table_becomes_its_title(self) -> None:
        cell = m.Cell(header=False, chars=[], rect=(72, 140, 300, 180), page=1)
        table = m.Table(rows=[[cell]], pages={1}, first_page=1)
        converter = converter_with([table])
        block = m.Block(kind="table", page=1, top=140, bottom=140, payload=0)
        seq = converter._attach_captions([self.caption("Table 2: Results", 120), block])
        self.assertEqual([b.kind for b in seq], ["table"])
        self.assertEqual(block.caption, ("Table 2: ", "table-2", "Results"))

    def test_caption_below_a_table_is_found(self) -> None:
        cell = m.Cell(header=False, chars=[], rect=(72, 100, 300, 180), page=1)
        table = m.Table(rows=[[cell]], pages={1}, first_page=1)
        converter = converter_with([table])
        block = m.Block(kind="table", page=1, top=100, bottom=100, payload=0)
        converter._attach_captions([block, self.caption("Table 4. Data", 186)])
        self.assertIsNotNone(block.caption)

    def test_sentences_and_far_text_are_not_captions(self) -> None:
        converter = converter_with()
        for text, top in (("Table 1 illustrates the data", 205), ("Figure 3. A swab being held", 600)):
            picture = self.figure(100)
            seq = converter._attach_captions([picture, self.caption(text, top)])
            self.assertEqual(len(seq), 2, text)
            self.assertIsNone(picture.caption)

    def test_title_text_and_the_attribute_that_stops_double_numbering(self) -> None:
        converter = converter_with()
        text = converter._titled("// note\n|===\n|a\n|===", ("Table 6. ", "table-6", "Limit"))
        self.assertEqual(text, "// note\n[[table-6]]\n.Table 6. Limit\n|===\n|a\n|===")
        self.assertEqual(converter._captioned, {"table"})


class Links(unittest.TestCase):
    class Page:
        annots = [{"x0": 10, "top": 0, "x1": 60, "bottom": 12, "uri": "https://example.org/a b"},
                  {"x0": 70, "top": 0, "x1": 100, "bottom": 12, "uri": "file:/C:/Users/x/doc.docx#_bookmark2"}]

    def test_web_links_are_marked_and_local_paths_are_not(self) -> None:
        chars = make_line("see here and more", 10, 0, size=10).chars
        m.mark_links(self.Page(), chars)
        marked = "".join(c.text for c in chars if c.link)
        self.assertTrue(marked.startswith("seehere"))
        self.assertTrue(all(c.link == "https://example.org/a b" for c in chars if c.link))
        self.assertFalse(any(c.link and c.x0 > 62 for c in chars))  # nothing under the file: link

    def test_link_is_written_as_a_macro(self) -> None:
        line = make_line("read it", 10, 0, size=10)
        for ch in line.chars:
            ch.link = "https://example.org/x"
        self.assertEqual(m.render_units(m.line_units(line)), "link:https://example.org/x[read it]")
        line.chars[0].link = None
        self.assertEqual(m.render_units(m.line_units(line)), "rlink:https://example.org/x[ead it]")

    def test_link_text_with_a_comma_is_quoted(self) -> None:
        line = make_line("a, b", 10, 0, size=10)
        for ch in line.chars:
            ch.link = "mailto:me@example.org"
        self.assertEqual(m.render_units(m.line_units(line)), "mailto:me@example.org[a&#44; b]")

    def test_link_text_with_a_dot_is_guarded(self) -> None:
        line = make_line("www.example.org", 10, 0, size=10)
        for ch in line.chars:
            ch.link = "https://example.org"
        self.assertEqual(m.render_units(m.line_units(line)), "link:https://example.org[{empty}www.example.org]")


class Reading(unittest.TestCase):
    def test_columns_follow_the_tags(self) -> None:
        def block(top: float, x: float, owner: int) -> m.Block:
            b = m._lines_to_block([make_line("word word", x, top)])
            b.owner = owner
            return b

        # two columns of three, tagged left column first: position order alternates the columns
        items = [block(100, 50, 0), block(100, 300, 3), block(120, 50, 1), block(120, 300, 4), block(140, 50, 2), block(140, 300, 5)]
        self.assertEqual([b.owner for b in m.TaggedPdfConverter._structure_order(items)], [0, 1, 2, 3, 4, 5])

    def test_one_column_is_left_alone(self) -> None:
        def block(top: float, owner: int) -> m.Block:
            b = m._lines_to_block([make_line("word word", 50, top)])
            b.owner = owner
            return b

        items = [block(100, 2), block(120, 0), block(140, 1)]  # out of tag order, but not side by side
        self.assertEqual([b.owner for b in m.TaggedPdfConverter._structure_order(items)], [2, 0, 1])


class Grids(unittest.TestCase):
    def legend(self, rows: list[tuple[str, str]], x: float = 300, top: float = 100) -> list[m.Ch]:
        chars: list[m.Ch] = []
        for r, (number, name) in enumerate(rows):
            chars += make_line(number, x, top + 16 * r, size=9, bold=r == 0).chars
            chars += make_line(name, x + 60, top + 16 * r, size=9, bold=r == 0).chars
        return chars

    def test_numbers_and_names_in_two_columns_are_a_table(self) -> None:
        table = m.text_grid(self.legend([("Number", "Description"), ("1", "Squeeze here"), ("2", "Pipette"), ("3", "Overflow bulb")]), 1)
        self.assertIsNotNone(table)
        self.assertTrue(table.legend)
        self.assertEqual(len(table.rows), 4)
        self.assertEqual([len(r) for r in table.rows], [2, 2, 2, 2])
        self.assertTrue(all(c.header for c in table.rows[0]))
        self.assertEqual("".join(c.text for c in table.rows[3][1].chars), "Overflowbulb")

    def test_running_text_and_short_lists_are_not(self) -> None:
        prose = []
        for r, text in enumerate(("The quick brown fox jumps over", "the lazy dog and keeps running", "until it reaches the far end")):
            prose += make_line(text, 300, 100 + 12 * r, size=9).chars
        self.assertIsNone(m.text_grid(prose, 1))
        self.assertIsNone(m.text_grid(self.legend([("1", "one"), ("2", "two")]), 1))  # two rows
        one_column = []
        for r in range(4):
            one_column += make_line(f"label {r}", 300, 100 + 14 * r, size=9).chars
        self.assertIsNone(m.text_grid(one_column, 1))


class Underlines(unittest.TestCase):
    class Page:
        def __init__(self, segments: list[tuple[float, float, float]]) -> None:
            self.rects = [{"x0": a, "x1": b, "top": y, "bottom": y + 0.7} for y, a, b in segments]
            self.lines: list = []
            self.curves: list = []

    def test_line_under_the_words_underlines_them(self) -> None:
        chars = make_line("Brand Name: GeneXpert", 10, 100, size=10).chars  # 5 pt a character
        # the label runs x 10..65; the underline sits just under the text
        m.mark_underlines(self.Page([(109.5, 10, 65)]), chars)
        marked = "".join(c.text for c in chars if c.underline)
        self.assertEqual(marked, "Brand Name:")

    def test_rule_wider_than_the_text_is_not_an_underline(self) -> None:
        chars = make_line("Heading", 10, 100, size=10).chars
        m.mark_underlines(self.Page([(109.5, 10, 300)]), chars)
        self.assertFalse(any(c.underline for c in chars))

    def test_linked_text_is_left_to_its_link(self) -> None:
        chars = make_line("Terms", 10, 100, size=10).chars
        for c in chars:
            c.link = "https://example.org"
        m.mark_underlines(self.Page([(109.5, 10, 35)]), chars)
        self.assertFalse(any(c.underline for c in chars))

    def test_half_a_word_is_a_whole_word(self) -> None:
        chars = make_line("Nested ANOVA", 10, 100, size=10).chars
        m.mark_underlines(self.Page([(109.5, 20, 70)]), chars)  # starts after the "N"
        self.assertTrue(chars[0].underline)

    def test_underline_and_link_title_are_written(self) -> None:
        line = make_line("label", 10, 0, size=10)
        for c in line.chars:
            c.underline = True
        self.assertEqual(m.render_units(m.line_units(line)), "[.underline]##label##")
        for c in line.chars:
            c.underline, c.link, c.link_title = False, "https://example.org/x", "Hover text"
        self.assertEqual(m.render_units(m.line_units(line)), 'link:https://example.org/x[label,title="Hover text"]')


class ListsOf(unittest.TestCase):
    def test_list_of_tables_is_written_from_the_captions(self) -> None:
        title = make_block("Table of Tables", 72, 0)
        title.listof = "table"
        table = m.Block(kind="table", page=1, top=50, bottom=60, payload=0)
        table.caption = ("Table 1. ", "table-1", "Results")
        figure = m.Block(kind="image", page=1, top=80, bottom=90, payload=(None, (0, 0, 1, 1), {}))
        figure.caption = ("Figure 1. ", "figure-1", "A swab")
        out = m.TaggedPdfConverter._resolve_lists([title, table, figure])
        self.assertEqual([b.kind for b in out], ["para", "listof", "table", "image"])
        self.assertEqual(out[1].payload, [("Table 1. ", "table-1", "Results")])
        converter = converter_with()
        text = converter._emit(out[:2])
        self.assertEqual(text[-1], "* xref:table-1[Table 1. Results]")

    def test_a_list_with_nothing_to_list_is_dropped(self) -> None:
        title = make_block("Table of Figures", 72, 0)
        title.listof = "figure"
        self.assertEqual(m.TaggedPdfConverter._resolve_lists([title]), [])


class Checker(unittest.TestCase):
    def test_blank_pictures_are_found(self) -> None:
        import tempfile
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            clear = Path(tmp) / "clear.png"
            Image.new("RGBA", (8, 8), (10, 20, 30, 0)).save(clear)  # wholly transparent
            flat = Path(tmp) / "flat.png"
            Image.new("RGB", (8, 8), (255, 255, 255)).save(flat)
            real = Path(tmp) / "real.png"
            picture = Image.new("RGB", (8, 8), (255, 255, 255))
            picture.putpixel((3, 3), (0, 0, 0))
            picture.save(real)
            self.assertTrue(cf._blank_image(clear))
            self.assertTrue(cf._blank_image(flat))
            self.assertFalse(cf._blank_image(real))

    def test_link_targets_compare_without_mailto_case_or_trailing_slash(self) -> None:
        self.assertEqual(cf._same_link("mailto:Me@Example.org"), cf._same_link("me@example.org"))
        self.assertEqual(cf._same_link("https://Example.org/a%20b/"), cf._same_link("https://example.org/a b"))

    def test_a_caption_on_an_admonition_is_read_as_its_label(self) -> None:
        one = cf._with_labels('[NOTE,caption="Notice"]\nAny discrepancies?')
        self.assertEqual(one, "[NOTE]\nNotice: Any discrepancies?")
        block = cf._with_labels('[IMPORTANT,caption="Attention"]\n====\nFirst.\n====')
        self.assertEqual(block, "[NOTE]\n====\nAttention: First.\n====")
        self.assertEqual(cf._with_labels("NOTE: plain"), "NOTE: plain")


def cut_chars(words: str, top: float, size: float = 10.0) -> list[dict]:
    """Upright characters of one cell's text, one line per word, from ``top`` down."""
    chars: list[dict] = []
    for line, word in enumerate(words.split()):
        for k, ch in enumerate(word):
            chars.append({"text": ch, "x0": 100 + k * 5.0, "x1": 105 + k * 5.0, "top": top + line * 12.0,
                          "bottom": top + line * 12.0 + size, "size": size, "upright": True})
    return chars


def node(kind: str, children: list[dict] | None = None, marks: list[tuple[int, int]] | None = None) -> dict:
    return {"type": kind, "children": children or [], "mcid_pages": marks or []}


class TableContinuations(unittest.TestCase):
    """A cell cut by a page break: its tail is checked inside its own cell, not as part of the page's order."""

    def test_finds_the_tails_of_cut_cells(self) -> None:
        table = node("Table", [node("TR", [node("TD", marks=[(5, 1)]), node("TD", marks=[(5, 2)])])])
        tail = node("Table", marks=[(6, 3)])  # no rows of its own, on the page after the table
        far = node("Table", marks=[(20, 9)])  # no table runs here
        spanning = node("Table", [node("TR", [node("TD", marks=[(13, 10), (14, 11)])])])
        tree = [node("Document", [table, tail, far, spanning])]
        self.assertEqual(cf._continuation_runs(tree), {(6, 3), (14, 11)})

    def test_a_cell_is_cut_where_the_reading_position_jumps_back_up(self) -> None:
        left = cut_chars("alpha beta gamma", 50)
        right = cut_chars("delta epsilon", 50)
        pieces = cf._split_cells(left + right)
        self.assertEqual([cf.tokens(cf._run_text(p)) for p in pieces], [["alpha", "beta", "gamma"], ["delta", "epsilon"]])
        sideways = [dict(c, upright=False) for c in left + right]
        self.assertEqual(len(cf._split_cells(sideways)), 1)

    def test_neighbouring_tag_cells_join_only_to_exactly_one_converted_cell(self) -> None:
        known = {cf.cell_key("Endogenous and urine additives"), cf.cell_key("Other")}
        rows = [["Endogenous and urine", "additives"], ["Other", "x"]]
        self.assertEqual(cf._join_whole_cells(rows, known)[0], ["Endogenous and urine additives"])
        self.assertEqual(cf._join_whole_cells(rows, known)[1], ["Other", "x"])  # "Other" is a cell already
        self.assertEqual(cf._join_whole_cells([["Endogenous", "additives"]], known), [["Endogenous", "additives"]])

    def _stub(self, adoc_cells: list[list[str]]) -> tuple[Any, Any]:
        """Page 1 holds the first part of cell A and all of cell B; page 2 holds both tails, one after the other."""
        import types

        runs = [(1, 1, "alpha beta"), (1, 2, "gamma delta epsilon"), (2, 3, "theta iota kappa lambda")]
        chars = {1: {1: cut_chars("alpha beta", 50), 2: cut_chars("gamma delta epsilon", 50)},
                 2: {3: cut_chars("theta iota", 50) + cut_chars("kappa lambda", 50)}}
        pdf = types.SimpleNamespace(
            page_count=2, tag_runs=[(p, m, cf.tokens(t)) for p, m, t in runs], mcid_chars=chars,
            tag_page_words=[cf.tokens("alpha beta gamma delta epsilon"), cf.tokens("theta iota kappa lambda")],
            continuations={(2, 3)}, repeated_header_words=[], tag_repeated_words=[], image_pages={},
            furniture=set(), layout_order_words=None)
        words = [w for row in adoc_cells for c in row for w in cf.tokens(c)]
        adoc = types.SimpleNamespace(words=words, tables=[{"rows": adoc_cells, "spans": 0}])
        return pdf, adoc

    def test_a_cut_cell_read_whole_is_in_order(self) -> None:
        pdf, adoc = self._stub([["alpha beta theta iota", "gamma delta epsilon kappa lambda"]])
        result = cf.check_text(pdf, adoc)
        self.assertEqual(result["order_recall"], 1.0)
        self.assertEqual(result["in_cells"], {"runs": 1, "words": 4, "in_order": 4})
        self.assertEqual(result["content_recall"], 1.0)

    def test_words_of_a_tail_out_of_order_inside_their_cell_are_reported(self) -> None:
        pdf, adoc = self._stub([["alpha beta iota theta", "gamma delta epsilon kappa lambda"]])
        self.assertLess(cf.check_text(pdf, adoc)["order_recall"], 1.0)

    def test_a_run_that_does_not_read_as_cells_is_left_to_the_main_check(self) -> None:
        pdf, adoc = self._stub([["alpha beta", "gamma delta epsilon"], ["theta", "iota", "kappa", "lambda"]])
        result = cf.check_text(pdf, adoc)
        self.assertEqual(result["in_cells"]["runs"], 0)
        self.assertEqual(result["order_recall"], 1.0)  # still in order as a whole


class Tables2(unittest.TestCase):
    """Rules found with the second set of documents."""

    @staticmethod
    def cell(text: str, x0: float, top: float, x1: float, bottom: float = 0.0, page: int = 1, header: bool = False) -> m.Cell:
        bottom = bottom or top + 20
        chars = make_line(text, x0 + 2, top + 2, size=8).chars
        for ch in chars:
            ch.page = page
        return m.Cell(header=header, chars=chars, rect=(x0, top, x1, bottom), page=page)

    def test_a_row_repeated_at_the_top_of_the_next_page_is_the_header_whatever_its_style(self) -> None:
        first = m.Table(rows=[[self.cell("Substance", 0, 0, 100), self.cell("Limit", 100, 0, 200)],
                              [self.cell("Glucose", 0, 20, 100), self.cell("250", 100, 20, 200)]], pages={1}, first_page=1)
        second = m.Table(rows=[[self.cell("Substance", 0, 0, 100, page=2), self.cell("Limit", 100, 0, 200, page=2)],
                               [self.cell("Biotin", 0, 20, 100, page=2), self.cell("4250", 100, 20, 200, page=2)]], pages={2}, first_page=2)
        self.assertTrue(m._repeats_first_row(first, second))
        self.assertTrue(m._continues(first, second, 792))
        other = m.Table(rows=[[self.cell("Name", 0, 0, 100, page=2), self.cell("Value", 100, 0, 200, page=2)]], pages={2}, first_page=2)
        self.assertFalse(m._repeats_first_row(first, other))

    def test_a_line_tagged_as_a_row_of_its_own_returns_to_the_cell_it_sits_in(self) -> None:
        table = m.Table(rows=[
            [self.cell("Traceability", 0, 0, 100, 50), self.cell("serum and NIST SRM 914", 100, 0, 200, 50)],
            [self.cell("NIST SRM 967 for", 100, 0, 200, 50)],
        ], pages={1}, first_page=1)
        self.assertEqual(m._fold_rows_in_the_same_box(table), 1)
        self.assertEqual(len(table.rows), 1)
        self.assertIn("NISTSRM967for", m._cell_text(table.rows[0][1]))

    def test_the_rest_of_a_cell_cut_by_a_page_break_joins_the_cell(self) -> None:
        converter = converter_with()
        above = [self.cell("Intended use", 0, 600, 100, 700), self.cell("used in the differential", 100, 600, 200, 700)]
        below = [self.cell("evaluating a patient.", 100, 40, 200, 80, page=2)]
        table = m.Table(rows=[above, below], pages={1, 2}, first_page=1)
        self.assertEqual(converter._join_split_rows(table), 1)
        self.assertEqual(len(table.rows), 1)
        # a next row that starts a new sentence is a row of its own
        table = m.Table(rows=[[self.cell("Intended use", 0, 600, 100, 700), self.cell("is a test.", 100, 600, 200, 700)],
                              [self.cell("Evaluating a patient.", 100, 40, 200, 80, page=2)]], pages={1, 2}, first_page=1)
        self.assertEqual(converter._join_split_rows(table), 0)

    def test_cells_with_no_borders_to_explain_them_go_where_they_start(self) -> None:
        table = m.Table(rows=[
            [self.cell("Virus", 0, 0, 100, 20), self.cell("Strain", 100, 0, 200, 20), self.cell("Result", 200, 0, 300, 20)],
            [self.cell("A/WS/33", 100, 20, 200, 40), self.cell("NEG", 200, 20, 300, 40)],  # the virus cell spans down
        ], pages={1}, first_page=1)
        self.assertTrue(m._columns_by_position(table))
        self.assertEqual([c.colspan for c in table.rows[1]], [1, 1, 1])
        self.assertEqual(m._cell_text(table.rows[1][0]), "")  # a blank where the cell above spans down
        self.assertEqual(m._cell_text(table.rows[1][1]), "A/WS/33")

    def test_text_on_its_side_reads_along_its_own_direction(self) -> None:
        chars = []
        for i, letter in enumerate("Virus"):  # reads from the bottom up: each letter higher on the page
            ch = m.Ch(letter, 50, 100 - 6 * i - 6, 58, 100 - 6 * i, 8.0, False, False, None, 1)
            ch.rot = 90
            chars.append(ch)
        line = [ln for ln in m.group_lines(m.unrotate(chars)) if not ln.blank]
        self.assertEqual(len(line), 1)
        self.assertEqual(m.plain(m.line_units(line[0])), "Virus")

    def test_a_caption_with_only_its_label_in_bold_and_a_second_line_is_one_caption(self) -> None:
        converter = converter_with()
        label = make_line("Table 19", 72, 100, bold=True).chars
        rest = make_line(". Summary of Detections for the Test", 72 + 8 * 5, 100).chars
        caption = m._lines_to_block([m.Line(label + rest, 100, 110, 72, 300, 10.0, 110, None, 1)])
        second = m._lines_to_block([make_line("Test of 2016 and 2022", 72, 112)])
        cell = m.Cell(header=False, chars=[], rect=(72, 130, 300, 200), page=1)
        table = m.Table(rows=[[cell]], pages={1}, first_page=1)
        converter.tables = [table]
        block = m.Block(kind="table", page=1, top=130, bottom=130, payload=0)
        seq = converter._attach_captions([caption, second, block])
        self.assertEqual([b.kind for b in seq], ["table"])
        self.assertEqual(block.caption[0], "Table 19. ")
        self.assertIn("Test of 2016 and 2022", block.caption[2])


class Numbering(unittest.TestCase):
    def test_the_next_number_starts_an_item_even_after_a_web_address(self) -> None:
        lines = [make_line("9. Centers for Disease Control. Biosafety. http://www.cdc.gov/biosafety/", 72, 100, size=10),
                 make_line("10. Clinical and Laboratory Standards Institute.", 72, 112, size=10)]
        blocks = m.flow_blocks(lines)
        self.assertEqual(len(blocks), 2)
        wrapped = [make_line("The set is (1) of the lot and", 72, 100, size=10),
                   make_line("2. other things that follow", 72, 112, size=10)]
        self.assertEqual(len(m.flow_blocks(wrapped)), 1)  # a number that does not follow the opening label's


class HeadersAndFurniture(unittest.TestCase):
    cell = staticmethod(Tables2.cell)

    def test_a_table_of_figures_under_a_row_of_labels_gets_a_header(self) -> None:
        rows = [[self.cell("Sample", 0, 0, 80), self.cell("Mean", 80, 0, 160), self.cell("SD", 160, 0, 240)]]
        for i in range(4):
            rows.append([self.cell(f"Control {i}", 0, 20 * (i + 1), 80), self.cell("1.42", 80, 20 * (i + 1), 160),
                         self.cell("0.015", 160, 20 * (i + 1), 240)])
        table = m.Table(rows=rows, pages={1}, first_page=1)
        self.assertEqual(m._infer_header(table), 1)
        self.assertTrue(all(c.header for c in table.rows[0]))

    def test_names_and_values_are_not_given_a_header(self) -> None:
        rows = [[self.cell("Brand Name", 0, 0, 80), self.cell("GeneXpert", 80, 0, 160)],
                [self.cell("Primary DI Number", 0, 20, 80), self.cell("07332940008581", 80, 20, 160)],
                [self.cell("Issuing Agency", 0, 40, 80), self.cell("GS1", 80, 40, 160)],
                [self.cell("Device Count", 0, 60, 80), self.cell("1", 80, 60, 160)]]
        table = m.Table(rows=rows, pages={1}, first_page=1)
        self.assertEqual(m._infer_header(table), 0)

    def test_a_repeated_header_with_a_cell_missing_is_still_the_header(self) -> None:
        import check_fidelity as cf
        above = [["virus", "strain", "targetconc", "result"], ["sarscov2", "flua", "flub", "rsv"]]
        below = [["virus", "targetconc", "result"], ["sarscov2", "flua", "flub", "rsv"]]  # "Strain" tagged elsewhere
        self.assertTrue(cf._same_header(above, below))
        self.assertFalse(cf._same_header(above, [["name", "value"], ["a", "b"]]))

    def test_text_tagged_into_the_cell_above_but_outside_its_column_is_released(self) -> None:
        converter = converter_with()
        cell = self.cell("100 copies", 310, 100, 400, 130, page=13)
        strays = make_line("Strain", 200, 50, size=8).chars  # the next page's header cell, tagged here
        for ch in strays:
            ch.page = 14
        cell.chars += strays
        converter.tables = [m.Table(rows=[[cell]], pages={13, 14}, first_page=13)]
        out = converter._release_misfiled(14)
        self.assertEqual("".join(c.text for c in out), "Strain")
        self.assertEqual("".join(c.text for c in cell.chars), "100 copies")
        # a continuation in the cell's own column stays
        cell.chars += [dataclasses.replace(c, x0=330, x1=335) for c in make_line("rest", 330, 40, size=8).chars]
        for ch in cell.chars:
            ch.page = ch.page if ch.page == 13 else 14
        converter._release_misfiled(14)
        self.assertIn("rest", "".join(c.text for c in cell.chars))

    def test_a_margin_picture_is_furniture_only_when_it_repeats(self) -> None:
        converter = object.__new__(m.TaggedPdfConverter)
        converter.layout = False
        converter.pdf = type("P", (), {"pages": [object()] * 10})()
        image = {"x0": 47, "top": 21, "x1": 238, "bottom": 58}
        key = m.TaggedPdfConverter._image_key(image)
        converter._image_repeats = collections.Counter({key: 1})
        self.assertFalse(converter._is_furniture_image(image, None))  # the letterhead of page 1 alone
        converter._image_repeats = collections.Counter({key: 9})
        self.assertTrue(converter._is_furniture_image(image, None))  # a running header

    def test_a_missing_title_is_the_centred_heading_that_opens_page_one(self) -> None:
        converter = object.__new__(m.TaggedPdfConverter)
        converter.path = Path("K210452.pdf")
        converter.title = "K210452"
        converter._title_given = False
        heading = m._lines_to_block([make_line("510(k) DECISION SUMMARY", 150, 90, bold=True)])
        heading.kind = "center"
        body = m._lines_to_block([make_line("A paragraph", 72, 140)])
        seq = [heading, body]
        converter._title_from_the_first_page(seq)
        self.assertEqual(converter.title, "510(k) DECISION SUMMARY")
        self.assertEqual(seq, [body])
        converter.title, converter._title_given = "Given title", True
        again = [m._lines_to_block([make_line("Other", 150, 90)])]
        again[0].kind = "center"
        converter._title_from_the_first_page(again)
        self.assertEqual(converter.title, "Given title")


class Sizes(unittest.TestCase):
    def test_small_symbols_are_kept_and_rules_and_bullets_are_not(self) -> None:
        c = object.__new__(m.TaggedPdfConverter)
        c.min_image_size = 16
        self.assertFalse(c._too_small(30, 15))  # an "Rx only" mark
        self.assertFalse(c._too_small(40, 40))
        self.assertTrue(c._too_small(100, 2))  # a rule
        self.assertTrue(c._too_small(60, 12))  # a strip
        self.assertTrue(c._too_small(8, 8))  # a bullet
        self.assertTrue(c._too_small(3, 30))


class Masks(unittest.TestCase):
    def test_inverted_soft_mask_is_read_as_opacity(self) -> None:
        self.assertEqual(m.mask_values(bytes([0, 255]), [1, 0]), bytes([255, 0]))
        self.assertEqual(m.mask_values(bytes([0, 255]), [0, 1]), bytes([0, 255]))
        self.assertEqual(m.mask_values(bytes([0, 255]), []), bytes([0, 255]))


class Marks(unittest.TestCase):
    def test_raised_mark_of_the_same_size_is_a_superscript(self) -> None:
        line = make_line("20a", 10, 100, size=6.3)
        line.chars[2].top -= 3.2
        line.chars[2].bottom -= 3.2
        line.baseline = 100 + 6.3
        self.assertEqual(m.render_units(m.line_units(line)), "20^a^")

    def test_text_that_fills_a_cell_gives_no_alignment_evidence(self) -> None:
        full = m.Cell(header=False, chars=make_line("a text that fills the cell", 4, 0, size=10).chars)
        self.assertEqual(m.TaggedPdfConverter._alignment(full, (0, 0, 134, 12)), "?")
        centred = m.Cell(header=False, chars=make_line("ok", 50, 0, size=10).chars)
        self.assertEqual(m.TaggedPdfConverter._alignment(centred, (0, 0, 111, 12)), "^")
        left = m.Cell(header=False, chars=make_line("ok", 4, 0, size=10).chars)
        self.assertEqual(m.TaggedPdfConverter._alignment(left, (0, 0, 111, 12)), "")

    def test_labels_on_a_picture_become_its_description(self) -> None:
        picture = m.Block(kind="image", page=1, top=100, bottom=200, x0=100, x1=200, payload=(None, (100, 100, 200, 200), {}))
        page = type("P", (), {"width": 612, "height": 792})()
        legend = make_line("Squeeze here", 120, 130, size=8).chars  # inside the picture
        beside = make_line("Overflow bulb", 210, 150, size=8).chars  # level with it, to the right
        prose = make_line("An ordinary sentence of the body text", 72, 400, size=10).chars
        flow, tables = m.TaggedPdfConverter._take_picture_text(legend + beside + prose, [picture], page)
        self.assertEqual(tables, [])  # two lines are labels, not a grid
        self.assertEqual("".join(c.text for c in flow), "An ordinary sentence of the body text")
        self.assertIn("Squeeze here", m.plain(picture.units))
        self.assertIn("Overflow bulb", m.plain(picture.units))



if __name__ == "__main__":
    unittest.main()
