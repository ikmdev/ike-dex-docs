"""Check how faithfully an AsciiDoc file represents the PDF it was converted from.

Usage:

    python check_fidelity.py source.pdf output.adoc
    python check_fidelity.py source.pdf output.adoc --rendered rendered.pdf --diff-dir work/diff
    python check_fidelity.py source.pdf output.adoc --json report.json

The AsciiDoc is parsed with Pandoc (its AsciiDoc reader understands tables with
row and column spans, lists and inline emphasis) and compared with the PDF on:

* Text: every word of the PDF (minus running headers and footers) should appear, in order,
  in the AsciiDoc. A tagged PDF supplies its words in tag order, without untagged text and
  contents lists; an untagged one uses the text layer. Reported per page.
* Tables: the PDF's tag structure (Word and Acrobat export one for accessible
  PDFs) lists each logical table with its rows and cells. Each must be matched
  by AsciiDoc table(s) with the same cell text and the same cells per row. An
  untagged PDF has no tags, so its tables are the ones drawn with ruling lines;
  the check is skipped, with a notice, when it has none.
* Lists and headings: item counts, and whether heading levels were flattened.
* Emphasis: italic and bold words in the PDF versus emphasised words in the
  AsciiDoc.
* Appearance (optional): with ``--rendered``, a PDF built from the AsciiDoc is
  compared page by page with the source. Pagination rarely matches exactly, so
  treat the score as a guide; ``--diff-dir`` writes side-by-side images.

The exit status is 1 when a gate fails, so the check can guard a conversion.
Tag structure carries no cell span attributes in the PDFs seen so far, so a
table's spans are checked indirectly, through its cells per row.
"""

from __future__ import annotations

import argparse
import collections
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional

_TOKEN_RE = re.compile(r"[^\W_]+|[%±≥≤<>=×°+/]")
_ITALIC_RE = re.compile(r"italic|oblique", re.IGNORECASE)
_BOLD_RE = re.compile(r"bold|black|heavy", re.IGNORECASE)


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """NFKC-fold, make soft/odd hyphens ordinary, and lowercase."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("￾", "-").replace("­", "-")
    return text.lower()


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize(text))


def cell_key(text: str) -> str:
    """Whitespace- and punctuation-insensitive form of a table cell."""
    return "".join(_TOKEN_RE.findall(normalize(text)))


def _roman(number: int) -> str:
    out = ""
    for value, numeral in ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while number >= value:
            out += numeral
            number -= value
    return out


def _list_marker(style: str, number: int) -> str:
    if style == "LowerAlpha":
        return chr(ord("a") + (number - 1) % 26)
    if style == "UpperAlpha":
        return chr(ord("A") + (number - 1) % 26)
    if style == "LowerRoman":
        return _roman(number)
    if style == "UpperRoman":
        return _roman(number).upper()
    return str(number)


# --------------------------------------------------------------------------
# AsciiDoc model (via Pandoc's JSON AST)
# --------------------------------------------------------------------------


class AsciiDocModel:
    """Text, headings, lists, tables and emphasis read from an AsciiDoc file."""

    def __init__(self, path: Path) -> None:
        import pypandoc

        ast = json.loads(pypandoc.convert_file(str(path), "json", format="asciidoc"))
        self.words: list[str] = []
        self.heading_levels: list[int] = []
        self.list_items = 0
        self.tables: list[dict[str, Any]] = []
        self.emphasis_words = {"italic": 0, "bold": 0, "underline": 0, "super": 0}
        self.table_ranges: list[tuple[int, int]] = []  # word index ranges that came from tables
        # the document title (= Title) is metadata to Pandoc, not a block
        title = ast.get("meta", {}).get("title", {})
        if title.get("t") == "MetaInlines":
            self._emit(self._text(title["c"]))
        elif title.get("t") == "MetaString":
            self._emit(title["c"])
        self._blocks(ast["blocks"])

    @property
    def words_outside_tables(self) -> list[str]:
        inside = {i for a, b in self.table_ranges for i in range(a, b)}
        return [w for i, w in enumerate(self.words) if i not in inside]

    # -- inlines -----------------------------------------------------------

    def _inlines(self, items: Iterable[dict], sink: list[str]) -> None:
        for item in items:
            kind, content = item["t"], item.get("c")
            if kind == "Str":
                sink.append(content)
            elif kind in ("Space", "SoftBreak", "LineBreak"):
                sink.append(" ")
            elif kind == "Code":
                sink.append(content[1])
            elif kind in ("Emph", "Strong", "Underline"):
                before = len(sink)
                self._inlines(content, sink)
                key = {"Emph": "italic", "Strong": "bold", "Underline": "underline"}[kind]
                self.emphasis_words[key] += len(tokens("".join(sink[before:])))
            elif kind in ("Superscript", "Subscript"):
                self.emphasis_words["super"] += kind == "Superscript"
                self._inlines(content, sink)
            elif kind in ("Strikeout", "SmallCaps"):
                self._inlines(content, sink)
            elif kind in ("Quoted",):
                self._inlines(content[1], sink)
            elif kind in ("Span",):
                self._inlines(content[1], sink)
            elif kind == "Link":
                self._inlines(content[1], sink)
            elif kind in ("Cite",):
                self._inlines(content[1], sink)
            # Image, Note, Math, RawInline: nothing comparable.

    def _text(self, items: Iterable[dict]) -> str:
        sink: list[str] = []
        self._inlines(items, sink)
        return "".join(sink)

    # -- blocks ------------------------------------------------------------

    def _emit(self, text: str) -> None:
        self.words.extend(tokens(text))

    def _blocks(self, blocks: Iterable[dict]) -> None:
        for block in blocks:
            kind, content = block["t"], block.get("c")
            if kind == "Header":
                self.heading_levels.append(content[0])
                heading = self._text(content[2])
                self._emit(heading)
                self.emphasis_words["bold"] += len(tokens(heading))  # headings are set in bold
            elif kind in ("Para", "Plain"):
                self._emit(self._text(content))
            elif kind == "LineBlock":
                for line in content:
                    self._emit(self._text(line))
            elif kind in ("CodeBlock", "RawBlock"):
                self._emit(content[1])
            elif kind == "BlockQuote":
                self._blocks(content)
            elif kind == "Div":
                self._blocks(content[1])
            elif kind == "BulletList":
                for item in content:
                    self.list_items += 1
                    self._blocks(item)
            elif kind == "OrderedList":
                (start, style, _delim), items = content
                number = start
                style = style["t"]
                for item in items:
                    self.list_items += 1
                    self._emit(_list_marker(style, number))
                    number += 1
                    self._blocks(item)
            elif kind == "DefinitionList":
                for term, definitions in content:
                    self.list_items += 1
                    self._emit(self._text(term))
                    for definition in definitions:
                        self._blocks(definition)
            elif kind == "Table":
                self._table(content)
            elif kind == "Figure":
                self._blocks(content[2])
                self._blocks(content[1][1])

    def _table(self, content: list) -> None:
        _attr, caption, colspecs, head, bodies, foot = content
        self._blocks(caption[1])
        table_start = len(self.words)
        rows: list[list[str]] = []
        spans = 0
        raw_rows = list(head[1])
        for body in bodies:
            raw_rows += body[2] + body[3]
        raw_rows += foot[1]
        carried: collections.Counter[int] = collections.Counter()  # columns held by earlier row spans
        for number, row in enumerate(raw_rows):
            cells: list[str] = []
            widths: list[int] = []
            for _cattr, _align, rowspan, colspan, cell_blocks in row[1]:
                spans += rowspan > 1 or colspan > 1
                before = len(self.words)
                self._blocks(cell_blocks)
                cells.append(" ".join(self.words[before:]))
                widths.append(colspan)
                for later in range(number + 1, number + rowspan):
                    carried[later] += colspan
            # Pandoc fills the columns that row spans already cover with empty cells at the end.
            room = len(colspecs) - carried[number]
            while len(cells) > 1 and sum(widths) > room and not cells[-1]:
                cells.pop()
                widths.pop()
            rows.append(cells)
        self.table_ranges.append((table_start, len(self.words)))
        self.tables.append({"rows": rows, "spans": spans})


# --------------------------------------------------------------------------
# PDF model (text layer, tag structure, fonts)
# --------------------------------------------------------------------------


class PdfModel:
    """Per-page words, tagged tables and lists, and italic/bold word counts."""

    def __init__(self, path: Path, ignore: list[str]) -> None:
        import pdfplumber
        import pypdfium2 as pdfium

        self.path = path
        pages = pdfium.PdfDocument(str(path))
        raw_pages = [
            re.split(r"\r?\n", pages[i].get_textpage().get_text_range())
            for i in range(len(pages))
        ]
        pages.close()
        self.page_count = len(raw_pages)
        self.furniture = self._furniture(raw_pages, ignore)
        self.page_words: list[list[str]] = []
        for lines in raw_pages:
            kept = [ln for i, ln in enumerate(lines) if not self._is_furniture(ln, i, len(lines), self.furniture)]
            self.page_words.append(tokens("\n".join(kept)))

        self.plumber = pdfplumber.open(str(path))
        self.italic_words = 0
        self.bold_words = 0
        self.mcid_text: dict[int, dict[int, str]] = {}
        self._bold: list[tuple[int, Optional[int], int, tuple[float, float]]] = []  # page, mcid, words, centre
        self._scan_characters()
        self.tagged = False
        self.image_pages = self._image_pages()
        self.tables: list[dict[str, Any]] = []
        self.list_items = 0
        # Words per page in the tag tree's reading order (None for an untagged PDF).
        self.tag_page_words: Optional[list[list[str]]] = None
        self._scan_structure()
        # For an untagged PDF with ruled tables: words in page order, without tables or running headers.
        self.layout_order_words: Optional[list[list[str]]] = None
        if not self.tagged:
            self._scan_ruled_tables()
            if self.tables:
                self._scan_layout_order()

    # -- running headers and footers -----------------------------------------

    @staticmethod
    def _line_key(line: str) -> str:
        """The line with its digits removed, so "Title 5", "6 Title" and "Title" are one running header."""
        text = " ".join(re.sub(r"\d+", " ", normalize(line)).split())
        return text or "#"

    def close(self) -> None:
        self.plumber.close()

    def _is_furniture(self, line: str, index: int, count: int, furniture: set[str]) -> bool:
        """Is this line running furniture? A line of digits only (a page number) counts only within the
        first two or last three lines of a page, so a table cell "25" is not taken for one."""
        key = self._line_key(line)
        return key in furniture and (key != "#" or index < 2 or index >= count - 3)

    def _furniture(self, pages: list[list[str]], ignore: list[str]) -> set[str]:
        """Lines that repeat on most pages once digits are ignored, plus --ignore matches."""
        seen: collections.Counter[str] = collections.Counter()
        for lines in pages:
            seen.update({
                self._line_key(ln) for i, ln in enumerate(lines)
                if ln.strip() and (self._line_key(ln) != "#" or i < 2 or i >= len(lines) - 3)
            })
        threshold = max(3, int(0.6 * len(pages)))
        furniture = {key for key, count in seen.items() if key and count >= threshold}
        patterns = [re.compile(p, re.IGNORECASE) for p in ignore]
        for lines in pages:
            for line in lines:
                if any(p.search(line) for p in patterns):
                    furniture.add(self._line_key(line))
        return furniture

    # -- characters: fonts and marked-content text --------------------------

    def _scan_characters(self) -> None:
        for number, page in enumerate(self.plumber.pages, 1):
            by_mcid: dict[int, list[str]] = collections.defaultdict(list)
            last: dict[int, dict] = {}
            for char in page.chars:
                mcid = char.get("mcid")
                if mcid is None:
                    continue
                before = last.get(mcid)
                # Some producers (browsers) draw words without a space character: a gap or a new line
                # between two characters of the same marked content is a word break.
                if before is not None and char["text"].strip() and before["text"].strip() and (
                    char["x0"] - before["x1"] > 0.18 * (char.get("size") or 10)
                    or abs(char["top"] - before["top"]) > 0.5 * (char.get("size") or 10)
                ):
                    by_mcid[mcid].append(" ")
                by_mcid[mcid].append(char["text"])
                last[mcid] = char
            self.mcid_text[number] = {m: "".join(c) for m, c in by_mcid.items()}
            for word in page.extract_words(extra_attrs=["fontname", "mcid"]):
                count = len(tokens(word["text"]))
                if _ITALIC_RE.search(word["fontname"]):
                    self.italic_words += count
                elif _BOLD_RE.search(word["fontname"]):
                    self.bold_words += count
                    self._bold.append((
                        number, word.get("mcid"), count,
                        ((word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2),
                    ))

    def _image_pages(self) -> dict[int, float]:
        """Pages with pictures covering >= 3% of the page, mapped to the covered share.

        Text inside a picture (a pasted table, say) is not in the text layer, so
        the text and table checks cannot verify it.
        """
        found = {}
        for number, page in enumerate(self.plumber.pages, 1):
            area = page.width * page.height
            share = sum(i["width"] * i["height"] for i in page.images) / area
            if share >= 0.03:
                found[number] = share
        return found

    # -- tag structure ----------------------------------------------------------

    def _node_text(self, node: dict, page: Optional[int]) -> str:
        """Text under a node, leaving out nested tables (they are listed on their own)."""
        page = node.get("page_number") or page
        parts = [
            self.mcid_text.get(page, {}).get(mcid, "") for mcid in node.get("mcids") or []
        ]
        parts += [
            self._node_text(child, page)
            for child in node.get("children", [])
            if child["type"] != "Table"
        ]
        return " ".join(part for part in parts if part).strip()

    @staticmethod
    def _first_page(node: dict) -> Optional[int]:
        if node.get("page_number"):
            return node["page_number"]
        for child in node.get("children", []):
            page = PdfModel._first_page(child)
            if page:
                return page
        return None

    def _scan_ruled_tables(self) -> None:
        """For an untagged PDF, the tables drawn with ruling lines, read by pdfplumber (a lone box is not
        a table). Unlike tags this is a guess, so it is the same kind of evidence the converter uses."""
        boxes: dict[int, list[tuple[float, float, float, float]]] = collections.defaultdict(list)
        for number, page in enumerate(self.plumber.pages, 1):
            for found in page.find_tables():
                if len(found.cells) < 2:
                    continue
                rows = [[(cell or "") for cell in row] for row in found.extract()]
                if any(len(row) > 1 for row in rows):
                    self.tables.append({"rows": rows, "page": number})
                    boxes[number].append(found.bbox)
        # Bold in a table is header style, not emphasis in the text (an untagged PDF has no TH tags).
        self.bold_words = sum(
            n for page, _, n, (x, y) in self._bold
            if not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes.get(page, []))
        )

    def _scan_layout_order(self) -> None:
        """Words per page in reading order with the ruled tables left out. A table's text sits in a PDF's
        drawing order, which need not be row by row; the table check covers tables instead."""
        pages: list[list[tuple[str, bool]]] = []
        seen: collections.Counter[str] = collections.Counter()
        for page in self.plumber.pages:
            boxes = [f.bbox for f in page.find_tables() if len(f.cells) >= 2]
            rows = []
            for line in page.extract_text_lines(return_chars=False):
                inside = any(
                    b[0] - 1 <= line["x0"] and line["x1"] <= b[2] + 1 and b[1] - 1 <= line["top"] and line["bottom"] <= b[3] + 1
                    for b in boxes
                )
                rows.append((line["text"], inside))
            pages.append(rows)
            seen.update({
                self._line_key(t) for i, (t, _) in enumerate(rows)
                if t.strip() and (self._line_key(t) != "#" or i < 2 or i >= len(rows) - 3)
            })
        threshold = max(3, int(0.6 * len(pages)))
        furniture = {k for k, n in seen.items() if n >= threshold}
        self.layout_order_words = [
            tokens(" ".join(
                t for i, (t, inside) in enumerate(rows)
                if not inside and not self._is_furniture(t, i, len(rows), furniture)
            ))
            for rows in pages
        ]

    def _scan_structure(self) -> None:
        try:
            tree = self.plumber.structure_tree
        except Exception:  # untagged or unreadable structure
            tree = []
        if not tree:
            return
        self.tagged = True

        header_mcids: set[tuple[int, int]] = set()

        def header_cells(node: dict, page: Optional[int]) -> None:
            page = node.get("page_number") or page
            where, previous = page, None
            for mcid in node.get("mcids") or []:
                if previous is not None and mcid < previous:
                    where = (where or 0) + 1
                previous = mcid
                header_mcids.add((where or 0, mcid))
            for child in node.get("children", []):
                header_cells(child, page)

        def visit(node: dict, page: Optional[int]) -> None:
            page = node.get("page_number") or page
            kind = node["type"]
            if kind == "TH":
                header_cells(node, page)
            if kind == "LI":
                self.list_items += 1
            if kind == "Table" and node.get("children"):
                trs = []  # rows sit directly under the table, or under THead, TBody and TFoot
                for part in node["children"]:
                    if part["type"] == "TR":
                        trs.append(part)
                    elif part["type"] in ("THead", "TBody", "TFoot"):
                        trs.extend(r for r in part.get("children", []) if r["type"] == "TR")
                rows = [[self._node_text(cell, page) for cell in row.get("children", [])] for row in trs]
                # A one-column "table" is a layout wrapper (captions, pictures).
                if rows and any(len(row) > 1 for row in rows):
                    self.tables.append({"rows": rows, "page": self._first_page(node) or page})
            for child in node.get("children", []):
                visit(child, page)

        for root in tree:
            visit(root, None)
        # A table header cell is bold by style, not by emphasis in the text.
        self.bold_words = sum(n for page, mcid, n, _ in self._bold if (page, mcid) not in header_mcids)

        pieces: dict[int, list[str]] = collections.defaultdict(list)

        def collect(node: dict, page: Optional[int]) -> None:
            if node["type"] in ("TOC", "TOCI"):  # a contents list is rebuilt from the headings
                return
            page = node.get("page_number") or page
            where, previous = page, None
            for mcid in node.get("mcids") or []:
                if previous is not None and mcid < previous:  # marked content restarts on a new page
                    where = (where or 0) + 1
                previous = mcid
                text = self.mcid_text.get(where or 0, {}).get(mcid, "")
                if text and where:
                    pieces[where].append(text)
            for child in node.get("children", []):
                collect(child, page)

        for root in tree:
            collect(root, None)
        if pieces:
            self.tag_page_words = [
                tokens(" ".join(pieces.get(n, []))) for n in range(1, self.page_count + 1)
            ]


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_text(pdf: PdfModel, adoc: AsciiDocModel) -> dict[str, Any]:
    # A tagged PDF's tag tree gives the expected words in reading order, leaving out untagged text
    # (letterhead, headers, footers) and contents lists. Its drawing order is not used: it can put a
    # table after the paragraphs that follow it on the page.
    adoc_order = adoc.words
    if pdf.tag_page_words is not None:
        ordered = pdf.tag_page_words
        pdf_tokens = None
    elif pdf.layout_order_words is not None:
        # Untagged, with ruled tables: judge order on the text between the tables, in page order.
        ordered, adoc_order, pdf_tokens = pdf.layout_order_words, adoc.words_outside_tables, "pdfium"
    else:
        ordered = pdf.page_words
        pdf_tokens = None
    order_tokens = [(w, page) for page, words in enumerate(ordered, 1) for w in words]
    if pdf_tokens == "pdfium":
        pdf_tokens = [(w, page) for page, words in enumerate(pdf.page_words, 1) for w in words]
    else:
        pdf_tokens = order_tokens
    matcher = difflib.SequenceMatcher(
        None, [w for w, _ in order_tokens], adoc_order, autojunk=False
    )
    in_order = [False] * len(order_tokens)
    for block in matcher.get_matching_blocks():
        for i in range(block.a, block.a + block.size):
            in_order[i] = True

    pdf_counts = collections.Counter(w for w, _ in pdf_tokens)
    adoc_counts = collections.Counter(adoc.words)
    missing = pdf_counts - adoc_counts
    extra = adoc_counts - pdf_counts
    pdf_total, adoc_total = len(pdf_tokens), len(adoc.words)

    per_page = []
    for page in range(1, pdf.page_count + 1):
        flags = [in_order[i] for i, (_, p) in enumerate(order_tokens) if p == page]
        per_page.append(
            {"page": page, "words": len(flags), "in_order": sum(flags)}
        )
    return {
        "pdf_words": pdf_total,
        "adoc_words": adoc_total,
        "content_recall": 1 - sum(missing.values()) / max(pdf_total, 1),
        "content_excess": sum(extra.values()) / max(pdf_total, 1),
        "order_recall": sum(in_order) / max(len(order_tokens), 1),
        "missing": missing.most_common(12),
        "extra": extra.most_common(12),
        "per_page": per_page,
        "image_pages": {str(k): round(v, 2) for k, v in pdf.image_pages.items()},
        "furniture_lines": sorted(pdf.furniture)[:8],
    }


def _overlap(left: list[str], right: list[str]) -> int:
    return sum((collections.Counter(left) & collections.Counter(right)).values())


def check_tables(pdf: PdfModel, adoc: AsciiDocModel) -> dict[str, Any]:
    if not pdf.tagged and not pdf.tables:
        return {"skipped": "PDF has no tag structure and no ruled tables; nothing to compare."}

    # Empty cells are left out: tagged PDFs carry placeholder cells for merged-away cells, and
    # an empty cell says nothing about the text.
    # Rows with a single filled cell are left out of the shape: they are spanning labels, footnotes
    # and lines of a merged cell that the tags list as rows of their own.
    # Cell text is compared as a multiset of characters: that survives lines of one merged cell being
    # joined, and words a PDF text layer splits ("P roduct"). Order is checked by the text check.
    def filled(rows: list[list[str]]) -> tuple[list[str], list[str], list[int]]:
        keys = [[cell_key(c) for c in row] for row in rows]
        counts = [sum(1 for k in row if k) for row in keys]
        letters = [ch for row in rows for c in row for w in tokens(c) for ch in w]
        return [k for row in keys for k in row if k], letters, [n for n in counts if n > 1]

    tagged = []
    for t in pdf.tables:
        keys, letters, shape = filled(t["rows"])
        tagged.append({"keys": keys, "letters": letters, "shape": shape, "page": t.get("page"),
                       "rows": t["rows"]})
    converted = []
    for t in adoc.tables:
        keys, letters, shape = filled(t["rows"])
        converted.append({"keys": keys, "letters": letters, "shape": shape, "spans": t["spans"],
                          "rows": t["rows"]})
    # Each AsciiDoc table belongs to the tagged table holding most of its cells,
    # so a PDF table split across pages may be several AsciiDoc tables.
    assigned: dict[int, list[int]] = collections.defaultdict(list)
    held: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)  # keys placed so far
    orphans = []
    for n, table in enumerate(converted):
        best, best_score = None, 0.0
        for m, ref in enumerate(tagged):
            # Tables that repeat a header (a table continued over several pages) must not all pile onto
            # the first one: a table whose cells are already accounted for is passed over.
            if sum((held[m] & collections.Counter(ref["keys"])).values()) >= 0.85 * max(len(ref["keys"]), 1):
                continue
            shared = _overlap(table["keys"], ref["keys"])
            # The AsciiDoc table may hold rows the tags leave out, so it also counts as a match when it
            # contains nearly all of the tagged table.
            score = max(shared / max(len(table["keys"]), 1), shared / max(len(ref["keys"]), 1)) if shared >= 2 else 0.0
            if score > best_score:
                best, best_score = m, score
        if best is not None and best_score >= 0.6:
            assigned[best].append(n)
            held[best].update(table["keys"])
        else:
            orphans.append(n)

    rows = []
    for m, ref in enumerate(tagged):
        mine = [converted[n] for n in assigned.get(m, [])]
        letters = [ch for t in mine for ch in t["letters"]]
        shape = [s for t in mine for s in t["shape"]]
        # Rows the tags leave out (header cells tagged straight onto the table) may be added, so score
        # how many tagged rows are found in order rather than how similar the two shapes are.
        matched = sum(
            b.size for b in difflib.SequenceMatcher(None, ref["shape"], shape, autojunk=False).get_matching_blocks()
        )
        # A table with no row of two filled cells (a form with a blank value column) has no shape to find.
        shape_ratio = matched / len(ref["shape"]) if ref["shape"] else 1.0
        rows.append(
            {
                "table": m + 1,
                "page": ref["page"],
                "pdf_rows": len(ref["rows"]),
                "pdf_cells": sum(len(r) for r in ref["rows"]),
                "adoc_tables": len(mine),
                "adoc_rows": sum(len(t["rows"]) for t in mine),
                "adoc_cells": sum(len(r) for t in mine for r in t["rows"]),
                "cell_recall": _overlap(ref["letters"], letters) / max(len(ref["letters"]), 1),
                "shape_match": shape_ratio if mine else 0.0,
                "spans": sum(t["spans"] for t in mine),
                "first_cell": ref["rows"][0][0][:30] if ref["rows"] and ref["rows"][0] else "",
            }
        )
    return {"tables": rows, "orphan_adoc_tables": orphans, "adoc_tables": len(converted)}


def check_structure(pdf: PdfModel, adoc: AsciiDocModel) -> dict[str, Any]:
    levels = collections.Counter(adoc.heading_levels)
    return {
        "pdf_list_items": pdf.list_items if pdf.tagged else None,
        "adoc_list_items": adoc.list_items,
        "heading_levels": dict(sorted(levels.items())),
        "headings_flat": len(adoc.heading_levels) > 5 and len(levels) == 1,
        "pdf_italic_words": pdf.italic_words,
        "pdf_bold_words": pdf.bold_words,
        "adoc_italic_words": adoc.emphasis_words["italic"],
        "adoc_bold_words": adoc.emphasis_words["bold"],
        "adoc_underline_words": adoc.emphasis_words["underline"],
        "adoc_superscripts": adoc.emphasis_words["super"],
    }


def check_appearance(
    source: Path, rendered: Path, diff_dir: Optional[Path]
) -> dict[str, Any]:
    import cv2
    import numpy as np
    import pypdfium2 as pdfium

    size = (360, 480)

    def ink(page: Any) -> Any:
        image = page.render(scale=1).to_pil().convert("L").resize(size)
        gray = 1.0 - np.asarray(image, dtype="float32") / 255.0
        return cv2.GaussianBlur(gray, (0, 0), 2.5), image

    def correlation(a: Any, b: Any) -> float:
        a, b = a - a.mean(), b - b.mean()
        denominator = float(np.sqrt((a * a).sum() * (b * b).sum()))
        if denominator:
            return float((a * b).sum() / denominator)
        # A page with no ink at all matches only another blank page.
        return 1.0 if not a.any() and not b.any() else 0.0

    left, right = pdfium.PdfDocument(str(source)), pdfium.PdfDocument(str(rendered))
    scores = []
    for i in range(min(len(left), len(right))):
        (a, a_img), (b, b_img) = ink(left[i]), ink(right[i])
        scores.append(correlation(a, b))
        if diff_dir:
            diff_dir.mkdir(parents=True, exist_ok=True)
            canvas = np.hstack([np.asarray(a_img), np.asarray(b_img)])
            cv2.imwrite(str(diff_dir / f"page-{i + 1:02d}.png"), canvas)
    result = {
        "source_pages": len(left),
        "rendered_pages": len(right),
        "page_scores": scores,
        "mean_score": sum(scores) / max(len(scores), 1),
    }
    left.close()
    right.close()
    return result


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _status(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def report(results: dict[str, Any], args: argparse.Namespace) -> bool:
    ok = True
    text = results["text"]
    gate = text["content_recall"] >= args.min_content and text["order_recall"] >= args.min_order
    ok &= gate
    print(f"[{_status(gate)}] Text: {text['pdf_words']} PDF words, {text['adoc_words']} in AsciiDoc")
    print(
        f"       content recall {text['content_recall']:.1%} (gate {args.min_content:.1%}), "
        f"in-order recall {text['order_recall']:.1%} (gate {args.min_order:.1%}), "
        f"excess words {text['content_excess']:.1%}"
    )
    if text["missing"]:
        print("       most missing:", ", ".join(f"{w}×{n}" for w, n in text["missing"]))
    if text["extra"]:
        print("       most extra:  ", ", ".join(f"{w}×{n}" for w, n in text["extra"]))
    worst = sorted(
        (p for p in text["per_page"] if p["words"]),
        key=lambda p: p["in_order"] / p["words"],
    )[:5]
    print("       weakest pages:", ", ".join(
        f"p{p['page']} {p['in_order'] / p['words']:.0%}" for p in worst))

    if text["image_pages"]:
        pages = ", ".join(f"p{p} ({share:.0%})" for p, share in text["image_pages"].items())
        print(f"[WARN] Pictures cover part of {pages}")
        print("       Text inside pictures is not in the PDF text layer, so it cannot be checked here;")
        print("       compare those pages with the page image by hand; an OCR transcription lists the cells to")
        print("       check in its '// Check' comments, and shows as an AsciiDoc table with no tagged match.")

    tables = results["tables"]
    if "skipped" in tables:
        print(f"[SKIP] Tables: {tables['skipped']}")
    else:
        bad = [
            t for t in tables["tables"]
            if t["cell_recall"] < args.min_cells or t["shape_match"] < args.min_shape
        ]
        gate = not bad
        ok &= gate
        print(
            f"[{_status(gate)}] Tables: {len(tables['tables'])} tagged in PDF, "
            f"{tables['adoc_tables']} in AsciiDoc, {len(bad)} differ"
        )
        if tables["orphan_adoc_tables"]:
            numbers = ", ".join(str(n + 1) for n in tables["orphan_adoc_tables"])
            print(f"       AsciiDoc tables with no tagged match (picture tables or untagged): {numbers}")
        extra = [t for t in tables["tables"] if t["adoc_rows"] > t["pdf_rows"]]
        if extra:
            print("       rows beyond the tags (header cells the tags leave out): "
                  + ", ".join(f"#{t['table']:02d} +{t['adoc_rows'] - t['pdf_rows']}" for t in extra))
        for t in tables["tables"] if args.verbose else bad:
            print(
                f"       #{t['table']:02d} p{t['page']} '{t['first_cell']}': rows {t['pdf_rows']}→{t['adoc_rows']}, "
                f"cells {t['pdf_cells']}→{t['adoc_cells']}, recall {t['cell_recall']:.0%}, "
                f"row shape {t['shape_match']:.0%}, {t['adoc_tables']} AsciiDoc table(s), {t['spans']} spans"
            )

    s = results["structure"]
    warn = []
    if s["headings_flat"]:
        warn.append(f"all {sum(s['heading_levels'].values())} headings are level {next(iter(s['heading_levels']))}")
    headings = sum(s["heading_levels"].values())
    pdf_items = s["pdf_list_items"]
    # The tags use LI for real list items and for numbered headings alike.
    if pdf_items is not None and (
        s["adoc_list_items"] + headings < 0.85 * pdf_items or s["adoc_list_items"] > 1.15 * pdf_items
    ):
        warn.append(
            f"list items {pdf_items} in PDF tags (numbered headings included) vs "
            f"{s['adoc_list_items']} list items + {headings} headings"
        )
    if s["pdf_italic_words"] and s["adoc_italic_words"] < 0.8 * s["pdf_italic_words"]:
        warn.append(f"italic words {s['pdf_italic_words']} in PDF vs {s['adoc_italic_words']}")
    if s["pdf_bold_words"] and s["adoc_bold_words"] < 0.8 * s["pdf_bold_words"]:
        warn.append(f"bold words {s['pdf_bold_words']} in PDF vs {s['adoc_bold_words']}")
    print(f"[{'WARN' if warn else 'PASS'}] Structure and emphasis" + (":" if warn else ""))
    for line in warn:
        print("       " + line)
    print(f"       heading levels {s['heading_levels']}, underlined words {s['adoc_underline_words']}, superscripts {s['adoc_superscripts']}")

    if "appearance" in results:
        a = results["appearance"]
        gate = a["mean_score"] >= args.min_appearance and a["source_pages"] == a["rendered_pages"]
        ok &= gate
        print(
            f"[{_status(gate)}] Appearance: {a['source_pages']} source vs {a['rendered_pages']} rendered pages, "
            f"mean page correlation {a['mean_score']:.2f} (gate {args.min_appearance:.2f})"
        )
    return ok


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check how faithfully an AsciiDoc file represents its source PDF."
    )
    parser.add_argument("pdf", type=Path, help="Source PDF")
    parser.add_argument("adoc", type=Path, help="AsciiDoc converted from the PDF")
    parser.add_argument("--rendered", type=Path, help="PDF rendered from the AsciiDoc, for the appearance check")
    parser.add_argument("--diff-dir", type=Path, help="Write side-by-side page images here (with --rendered)")
    parser.add_argument("--json", type=Path, help="Also write the full results as JSON")
    parser.add_argument("--ignore", action="append", default=[], metavar="REGEX",
                        help="PDF text lines to treat as page furniture (repeatable)")
    parser.add_argument("--min-content", type=float, default=0.995, help="Text content recall gate")
    parser.add_argument("--min-order", type=float, default=0.99, help="Text in-order recall gate")
    parser.add_argument("--min-cells", type=float, default=0.99, help="Per-table cell recall gate")
    parser.add_argument("--min-shape", type=float, default=0.99, help="Per-table row-shape gate")
    parser.add_argument("--min-appearance", type=float, default=0.85, help="Mean page correlation gate")
    parser.add_argument("-v", "--verbose", action="store_true", help="List every table, not only failures")
    args = parser.parse_args(argv)

    try:
        pdf = PdfModel(args.pdf, args.ignore)
        adoc = AsciiDocModel(args.adoc)
        results: dict[str, Any] = {
            "text": check_text(pdf, adoc),
            "tables": check_tables(pdf, adoc),
            "structure": check_structure(pdf, adoc),
        }
        if args.rendered:
            results["appearance"] = check_appearance(args.pdf, args.rendered, args.diff_dir)
    except (OSError, RuntimeError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pdf.close()
    passed = report(results, args)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2, default=list), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
