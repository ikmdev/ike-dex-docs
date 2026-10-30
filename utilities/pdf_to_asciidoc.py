"""Convert a tagged PDF to AsciiDoc from its structure tree, fonts and geometry.

Tagged PDFs (Word and Acrobat exports are, as a rule) say which text belongs to which
paragraph, list item and table cell. This module reads that structure instead of guessing
it from a Markdown export, so the AsciiDoc keeps:

* tables with the PDF's rows and cells, plus column and row spans, which the tags omit
  and which are recovered from the cell borders drawn on the page;
* nested lists, numbered and bulleted, with continuation paragraphs;
* heading levels worked out from the numbering (I, A, 1.2, ...) and bold type;
* inline bold, italic, superscript and subscript from the fonts and baselines;
* centred paragraphs, and pictures written at their stored resolution (the image data in
  the PDF, not a render of the page).

Untagged PDFs (print-to-PDF and FOP output, say) are read in *layout* mode: the same conversion, but
paragraphs come from line spacing and type, tables from the ruling lines, headings from numbering and
type size, and repeated headers and footers are found by comparing pages. Scans have no text to read and
go to Docling.

Usage as a library:

    from pdf_to_asciidoc import TaggedPdfConverter

    if TaggedPdfConverter.usable("report.pdf"):
        adoc = TaggedPdfConverter("report.pdf", title="Report", images_dir="out/images").convert()
"""

from __future__ import annotations

import collections
import dataclasses
import re
import statistics
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

_ITALIC_RE = re.compile(r"italic|oblique", re.IGNORECASE)
_BOLD_RE = re.compile(r"bold|black|heavy|semibold|demi", re.IGNORECASE)

# Text that AsciiDoc would read as markup is wrapped in an inline passthrough.
_RISKY_RE = re.compile(r"[*_#`^~\[\]{}<>\\]|\.\.\.|\(C\)|\(R\)|\(TM\)|--|->|=>")
# A list label at the start of a line: a bullet or dash, or an identifier closed by "." or ")",
# or wrapped in parentheses: "1.", "a)", "(iv)", "II.", "-".
_LABEL_RE = re.compile(
    r"^(?:(?P<bullet>[•·▪◦●○■□‣⁃\uf0b7\uf0a7\uf0d8\uf076]\s*|[-–—](?=\s))"
    r"|(?P<open>\()?(?P<id>\d{1,3}|[a-zA-Z]|[ivxlc]{2,5}|[IVXLC]{2,5})(?P<close>[.)])(?=\s))"
)
# A heading label: "I", "A.", "1.2", "(a)", "iv)"; lower-case labels must carry a delimiter.
_HEADING_RE = re.compile(
    r"^(?P<open>\()?(?P<id>[IVXLC]{1,5}|[ivxlc]{1,5}|[A-Za-z]|\d{1,2}(?:\.\d{1,2})*)(?P<close>[.)])?\s+(?P<title>\S.*)$"
)
_ROMAN_RE = re.compile(r"^(?=[ivxlcdm])m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.IGNORECASE)

# Structure tags whose meaning is carried down to the paragraphs inside them.
_SEMANTIC_TAG_RE = re.compile(r"H[1-6]?|TOC|TOCI|Caption")

# A gap wider than this many ems between two characters of a line starts a new column.
_COLUMN_GAP = 3.0

# Tolerances in PDF points.
_LINE_OVERLAP = 0.4
_EDGE_TOL = 2.5
_INDENT_TOL = 4.0


# --------------------------------------------------------------------------
# Characters, lines and inline text
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Ch:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool
    italic: bool
    owner: Optional[int]
    page: int


@dataclasses.dataclass
class Line:
    chars: list[Ch]
    top: float
    bottom: float
    x0: float
    x1: float
    size: float
    baseline: float
    owner: Optional[int]
    page: int

    @property
    def blank(self) -> bool:
        return not any(c.text.strip() for c in self.chars)


def _normalize_char(text: str) -> str:
    if text in ("￾", "‑", "‐"):
        return "-"
    if text == " ":
        return " "
    return text


def make_char(raw: dict, owner: Optional[int], page: int) -> Ch:
    font = raw.get("fontname") or ""
    return Ch(
        text=_normalize_char(raw["text"]),
        x0=raw["x0"], x1=raw["x1"], top=raw["top"], bottom=raw["bottom"],
        size=raw.get("size") or (raw["bottom"] - raw["top"]),
        bold=bool(_BOLD_RE.search(font)), italic=bool(_ITALIC_RE.search(font)),
        owner=owner, page=page,
    )


def _split_columns(members: list[Ch]) -> list[list[Ch]]:
    """Split a line of characters where a wide gap separates side-by-side columns (a form's label and
    value pairs, say), so the columns do not run together into one line of text."""
    groups: list[list[Ch]] = [[]]
    previous: Optional[Ch] = None
    size = max((c.size for c in members if c.text.strip()), default=10)
    for ch in members:
        if previous is not None and ch.text.strip() and previous.text.strip() \
                and ch.x0 - previous.x1 > _COLUMN_GAP * size:
            groups.append([])
        groups[-1].append(ch)
        if ch.text.strip():
            previous = ch
    return groups


def group_lines(chars: Iterable[Ch]) -> list[Line]:
    """Cluster characters into text lines by vertical overlap, so raised marks stay in their line."""
    lines: list[list[Ch]] = []
    extents: list[list[float]] = []
    for ch in sorted(chars, key=lambda c: (c.page, c.top, c.x0)):
        height = max(ch.bottom - ch.top, 0.1)
        best, best_share = None, 0.0
        for i in range(len(lines) - 1, max(len(lines) - 6, -1), -1):
            if lines[i][0].page != ch.page:
                continue
            top, bottom = extents[i]
            overlap = min(bottom, ch.bottom) - max(top, ch.top)
            share = overlap / min(height, max(bottom - top, 0.1))
            if share > best_share:
                best, best_share = i, share
        if best is not None and best_share >= _LINE_OVERLAP:
            lines[best].append(ch)
            extents[best][0] = min(extents[best][0], ch.top)
            extents[best][1] = max(extents[best][1], ch.bottom)
        else:
            lines.append([ch])
            extents.append([ch.top, ch.bottom])

    result = []
    for members, (top, bottom) in zip(lines, extents):
        members.sort(key=lambda c: c.x0)
        for group in _split_columns(members):
            visible = [c for c in group if c.text.strip()] or group
            size = max(c.size for c in visible)
            main = [c for c in visible if c.size >= 0.9 * size] or visible
            owners = collections.Counter(c.owner for c in visible if c.owner is not None)
            result.append(
                Line(
                    chars=group, top=top, bottom=bottom,
                    x0=min(c.x0 for c in visible), x1=max(c.x1 for c in visible),
                    size=size, baseline=statistics.median(c.bottom for c in main),
                    owner=owners.most_common(1)[0][0] if owners else None,
                    page=group[0].page,
                )
            )
    result.sort(key=lambda ln: (ln.page, ln.top, ln.x0))
    return result


Style = tuple  # (bold, italic, script) with script in (None, "sup", "sub")
Unit = tuple   # (text, style or None for undetermined whitespace)


def line_units(line: Line) -> list[Unit]:
    """(text, style) for each character of a line, with spaces inserted where the layout has gaps."""
    units: list[Unit] = []
    previous: Optional[Ch] = None
    for ch in line.chars:
        if previous is not None and ch.text.strip() and previous.text.strip():
            if ch.x0 - previous.x1 > 0.18 * line.size:
                units.append((" ", None))
        if not ch.text.strip():
            units.append((" ", None))
        else:
            script = None
            if 0.5 * line.size <= ch.size < 0.8 * line.size:
                if ch.bottom < line.baseline - 0.12 * line.size:
                    script = "sup"
                elif ch.bottom > line.baseline + 0.12 * line.size:
                    script = "sub"
            units.append((ch.text, (ch.bold, ch.italic, script)))
        previous = ch
    return units


def join_units(lines: Sequence[Line]) -> list[Unit]:
    """Join line units into one paragraph: a space between lines, none after a hyphen."""
    out: list[Unit] = []
    for line in lines:
        units = line_units(line)
        while units and units[0][0] == " ":
            units.pop(0)
        while units and units[-1][0] == " ":
            units.pop()
        if not units:
            continue
        if out:
            glue = (
                out[-1][0] == "-" and out[-1][1] is not None
                and units[0][0].isalnum()
                and (len(out) < 2 or out[-2][0].isalnum())
            )
            if not glue:
                out.append((" ", None))
        out.extend(units)
    return _collapse_spaces(out)


def _collapse_spaces(units: list[Unit]) -> list[Unit]:
    out: list[Unit] = []
    for unit in units:
        if unit[0] == " " and out and out[-1][0] == " ":
            continue
        out.append(unit)
    while out and out[0][0] == " ":
        out.pop(0)
    while out and out[-1][0] == " ":
        out.pop()
    return out


def plain(units: Sequence[Unit]) -> str:
    return "".join(t for t, _ in units)


def bold_share(units: Sequence[Unit]) -> float:
    letters = [s for t, s in units if s is not None]
    return sum(1 for s in letters if s[0]) / len(letters) if letters else 0.0


def escape(text: str) -> str:
    """Text as AsciiDoc that reads back the same: markup characters are passed through. A word that holds
    an underscore is passed through whole (several ``++_++`` in one word do not parse), and a text with
    more than one ``+`` writes each as {plus}, which cannot pair into a passthrough."""
    plus = text.count("+") > 1 or text.endswith("+")
    pieces = []
    for token in re.split(r"(\s+)", text):
        if "_" in token and "++" not in token and not token.isspace():
            pieces.append(f"++{token}++")
        else:
            if plus:
                token = token.replace("+", "\x05")  # kept apart so the markup below cannot touch it
            pieces.append(_RISKY_RE.sub(lambda m: f"++{m.group(0)}++", token).replace("\x05", "{plus}"))
    return "".join(pieces)


_LINE_START_RE = re.compile(r"^(?:\.\S|(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION):|[-*+=>]\s|//|\|)")


def guard_line_start(text: str) -> str:
    """A paragraph that begins like a block title (``.5 mg``), an admonition (``NOTE:``), a list item
    (``- ``) or a comment would be read as that block; ``{empty}`` in front makes it text."""
    return "{empty}" + text if _LINE_START_RE.match(text) else text


def render_units(units: Sequence[Unit], *, bold: bool = True) -> str:
    """AsciiDoc inline markup for styled characters. Set bold=False to drop bold (headings)."""
    resolved: list[tuple[str, Optional[Style]]] = []
    for i, (text, style) in enumerate(units):
        if style is None:
            before = next((s for t, s in reversed(units[:i]) if s is not None), None)
            after = next((s for t, s in units[i + 1:] if s is not None), None)
            style = before if before is not None and before == after else None
        resolved.append((text, style))

    runs: list[tuple[str, Optional[Style]]] = []
    for text, style in resolved:
        if runs and runs[-1][1] == style:
            runs[-1] = (runs[-1][0] + text, style)
        else:
            runs.append((text, style))

    pieces: list[str] = []
    for index, (text, style) in enumerate(runs):
        if style is None or not text.strip():
            pieces.append(escape(text))
            continue
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        body = escape(text.strip())
        is_bold, is_italic, script = style
        if script and (len(text.strip()) > 8 or " " in text.strip()):
            script = None  # ^...^ and ~...~ cannot hold spaces, and a long run is small text, not a mark
        if script == "sup":
            body = f"^{body}^"
        elif script == "sub":
            body = f"~{body}~"
        else:
            before = (pieces[-1][-1:] if pieces else "") if not lead else lead
            after_text = runs[index + 1][0][:1] if index + 1 < len(runs) and not trail else trail
            unconstrained = before.isalnum() or after_text.isalnum()
            if is_italic:
                body = f"__{body}__" if unconstrained else f"_{body}_"
            if is_bold and bold:
                body = f"**{body}**" if unconstrained else f"*{body}*"
        pieces.append(f"{lead}{body}{trail}")
    return "".join(pieces).strip().replace("\x01", "&#46;").replace("\x02", "&#41;").replace("\x04", "&#45;")


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Block:
    kind: str  # para | heading | center | table | image
    lines: list[Line] = dataclasses.field(default_factory=list)
    units: list[Unit] = dataclasses.field(default_factory=list)
    owner: Optional[int] = None
    page: int = 0
    top: float = 0.0
    bottom: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    size: float = 0.0
    # list item (para blocks only)
    label: Optional[str] = None
    label_kind: str = ""
    label_value: int = 0
    label_x: float = 0.0
    content_x: float = 0.0
    # heading
    level: int = 0
    # table / image payload
    payload: Any = None


def _letter_value(label: str) -> int:
    return ord(label.lower()) - ord("a") + 1


def _roman_value(label: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total, previous = 0, 0
    for char in reversed(label.lower()):
        value = values[char]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total


def _lines_to_block(lines: list[Line]) -> Block:
    return Block(
        kind="para", lines=list(lines), units=join_units(lines), owner=lines[0].owner,
        page=lines[0].page, top=lines[0].top, bottom=lines[-1].bottom,
        x0=min(ln.x0 for ln in lines), x1=max(ln.x1 for ln in lines),
        size=max(ln.size for ln in lines),
    )


def _line_text(line: Line) -> str:
    return plain(line_units(line)).strip()


def _bold_line(line: Line) -> bool:
    visible = [c for c in line.chars if c.text.strip()]
    return bool(visible) and sum(1 for c in visible if c.bold) >= 0.9 * len(visible)


def _line_pitch(lines: list[Line]) -> float:
    """The usual distance between the tops of consecutive body lines on a page (0 when it cannot be told).
    Producers space lines differently, so a paragraph break is judged against this, not a fixed gap. Most
    consecutive pairs sit inside a paragraph, so the median of the plausible distances is the line pitch."""
    pitches = [
        round((b.top - a.top) * 2) / 2
        for a, b in zip(lines, lines[1:])
        if not a.blank and not b.blank and abs(a.size - b.size) <= 0.6 and abs(a.x0 - b.x0) <= 2.5 * a.size
        and 0.9 * a.size < b.top - a.top < 2.0 * a.size
    ]
    return statistics.median(pitches) if len(pitches) >= 3 else 0.0


def _layout_break(previous: Line, line: Line, first: Line, right_edge: float, pitch: float = 0.0) -> bool:
    """Does an untagged line start a new paragraph? Spacing, size, boldness, a dedent, or a short last
    line after a finished sentence say so."""
    if pitch:
        if line.top - previous.top > 1.35 * pitch + 0.5:
            return True
    elif line.top - previous.bottom > 0.35 * line.size:
        return True
    if abs(line.size - previous.size) > 0.6 or _bold_line(previous) != _bold_line(line):
        return True
    if line.x0 < first.x0 - 2 * line.size:
        return True
    ended = _line_text(previous).endswith((".", ":", ";", "!", "?"))
    return bool(right_edge) and ended and previous.x1 < right_edge - 4 * previous.size


def flow_blocks(lines: list[Line], layout: bool = False, right_edge: float = 0.0, pitch: float = 0.0) -> list[Block]:
    """Group a page's lines into paragraphs: a new one starts when the tagged owner changes. In layout
    mode (untagged PDFs) spacing, type and indentation decide instead."""
    blocks: list[Block] = []
    current: list[Line] = []
    for line in lines:
        if line.blank:
            if current:
                blocks.append(_lines_to_block(current))
                current = []
            continue
        starts_item = False
        if current and abs(line.x0 - current[0].x0) <= _INDENT_TOL:
            # A line inside a tagged paragraph starts a new item when it carries a label. Anything but a
            # bullet glyph must also follow a finished sentence: "(1) SARS" can be a wrapped enumeration.
            label = parse_label(plain(line_units(line)).lstrip())
            ended = plain(line_units(current[-1])).rstrip().endswith((".", ":", ";", "!", "?"))
            starts_item = label is not None and (
                ended or (label.kind == "bullet" and label.raw not in "-–—")
            )
        if layout and current:
            breaks = starts_item or _layout_break(current[-1], line, current[0], right_edge, pitch)
        else:
            breaks = bool(current) and (
                line.owner != current[-1].owner or starts_item
                or line.owner is None and line.top - current[-1].bottom > 0.9 * line.size
            )
        if breaks:
            blocks.append(_lines_to_block(current))
            current = []
        current.append(line)
    if current:
        blocks.append(_lines_to_block(current))
    return blocks


@dataclasses.dataclass
class Label:
    kind: str  # bullet | num | alpha | ALPHA | roman | ROMAN
    value: int
    raw: str  # the label as written, delimiter included, without the following space


def parse_label(text: str) -> Optional[Label]:
    """The list label a line starts with, or None. A lone i, v, x (or I, V, X) is read as a letter
    here; the list that holds it decides whether it is a roman numeral."""
    match = _LABEL_RE.match(text)
    if not match:
        return None
    raw = match.group(0).rstrip()
    if match.group("bullet"):
        return Label("bullet", 0, raw)
    ident = match.group("id")
    if match.group("open") and match.group("close") != ")":
        return None  # "(a." is not a label
    if ident.isdigit():
        return Label("num", int(ident), raw)
    if len(ident) == 1:
        return Label("ALPHA" if ident.isupper() else "alpha", _letter_value(ident), raw)
    if not _ROMAN_RE.match(ident):
        return None
    return Label("ROMAN" if ident.isupper() else "roman", _roman_value(ident), raw)


def split_label(block: Block) -> None:
    """Detect a leading list label and record it on the block, removing it from the text."""
    label = parse_label(plain(block.units))
    first = next((ln for ln in block.lines if not ln.blank), None)
    if label is None or first is None:
        return
    visible = [c for c in first.chars if c.text.strip()]
    # x where the text after the label starts: the first character past the label glyphs
    after = visible[len(label.raw):]
    block.label, block.label_kind, block.label_value = label.raw, label.kind, label.value
    block.label_x = visible[0].x0
    block.content_x = after[0].x0 if after else block.label_x + 18
    cut, seen = 0, 0
    for cut, (t, _) in enumerate(block.units):
        seen += len(t)
        if seen >= len(label.raw):
            cut += 1
            break
    rest = block.units[cut:]
    while rest and rest[0][0] == " ":
        rest = rest[1:]
    block.units = rest


class Headings:
    """Heading level from the numbering scheme.

    Schemes (I, A, 1, 1.2, i, a, ...) nest in the order they open: a scheme that is not open yet goes
    one level below the innermost open one, and one that is open again returns to its own level, so
    "I, A, II, A" and "A, I, B, I" both come out right.
    """

    def __init__(self) -> None:
        self.stack: list[tuple[str, int]] = []
        self.last_letter: dict[bool, int] = {True: 0, False: 0}  # by upper case

    def classify(self, block: Block) -> Optional[tuple[str, str]]:
        """(scheme, text) when the block is a numbered, wholly bold heading."""
        if bold_share(block.units) < 0.9 or len(block.lines) > 2:
            return None
        text = plain(block.units)
        match = _HEADING_RE.match(text)
        if not match:
            return None
        ident, delimited = match.group("id"), bool(match.group("close"))
        if match.group("title")[0].islower():
            return None  # "I agree to ...", "10 mg daily": a sentence, not a heading
        if bool(match.group("open")) and match.group("close") != ")":
            return None
        if ident[0].isdigit():
            dots = ident.count(".")
            return (f"decimal{dots}" if dots else "number"), text
        upper = ident.isupper()
        if not upper and not delimited:
            return None  # a lower-case label needs "." or ")" to be a label
        if len(ident) == 1 and self.last_letter[upper] and _letter_value(ident) == self.last_letter[upper] + 1:
            scheme = "ALPHA" if upper else "alpha"  # continues the letters
        elif _ROMAN_RE.match(ident) and (len(ident) > 1 or ident in "IVXivx"):
            scheme = "ROMAN" if upper else "roman"
        elif len(ident) == 1:
            scheme = "ALPHA" if upper else "alpha"
        else:
            return None
        self.last_letter[upper] = _letter_value(ident) if scheme in ("ALPHA", "alpha") else 0
        return scheme, text

    def level(self, scheme: str) -> int:
        for i, (name, level) in enumerate(self.stack):
            if name == scheme:
                del self.stack[i + 1:]
                return level
        level = min(self.stack[-1][1] + 1 if self.stack else 2, 6)
        self.stack.append((scheme, level))
        return level


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Cell:
    header: bool
    chars: list[Ch]
    rect: Optional[tuple[float, float, float, float]] = None  # x0, top, x1, bottom
    col: int = 0
    colspan: int = 1
    rowspan: int = 1
    page: int = 0


@dataclasses.dataclass
class Table:
    rows: list[list[Cell]]
    pages: set[int]
    first_page: int
    top: float = 0.0
    emitted: bool = False


def _cluster(values: Iterable[float], tolerance: float = _EDGE_TOL) -> list[float]:
    edges: list[list[float]] = []
    for value in sorted(values):
        if edges and value - edges[-1][-1] <= tolerance:
            edges[-1].append(value)
        else:
            edges.append([value])
    return [sum(group) / len(group) for group in edges]


def _nearest(edges: Sequence[float], value: float) -> int:
    return min(range(len(edges)), key=lambda i: abs(edges[i] - value))


class PageBorders:
    """Thin rectangles, closed paths and lines on a page: the cell borders of a table."""

    def __init__(self, page: Any) -> None:
        self.vertical: list[tuple[float, float, float]] = []
        self.horizontal: list[tuple[float, float, float]] = []
        # Producers draw a border as a thin filled rectangle, a closed path (pdfplumber calls a
        # rectangle written with five points a "curve") or a stroked line; all have a thin bounding box.
        for rect in [*page.rects, *page.curves, *page.lines]:
            width, height = rect["x1"] - rect["x0"], rect["bottom"] - rect["top"]
            if width <= 1.8 and height >= 3:
                self.vertical.append(((rect["x0"] + rect["x1"]) / 2, rect["top"], rect["bottom"]))
            elif height <= 1.8 and width >= 3:
                self.horizontal.append(((rect["top"] + rect["bottom"]) / 2, rect["x0"], rect["x1"]))

    @staticmethod
    def _covered(segments: list[tuple[float, float, float]], position: float, low: float, high: float) -> float:
        """Share of [low, high] that borders at ``position`` cover."""
        if high <= low:
            return 1.0
        spans = sorted(
            (max(a, low), min(b, high)) for at, a, b in segments
            if abs(at - position) <= 4.0 and b > low and a < high
        )
        total, end = 0.0, low
        for a, b in spans:
            a = max(a, end)
            if b > a:
                total += b - a
                end = b
        return total / (high - low)

    def horizontal_cover(self, y: float, x0: float, x1: float) -> float:
        return self._covered(self.horizontal, y, x0, x1)

    def vertical_cover(self, x: float, y0: float, y1: float) -> float:
        return self._covered(self.vertical, x, y0, y1)

    def merge_cells(self, rects: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
        """Join cells stacked one above the other with no border between them: a table finder splits a
        merged cell (a row span) wherever a border in the next column runs through it. Only stacked cells
        are joined: a missing vertical border is more often a border drawn as something this does not see
        (a header row ruled with fills) than a column span, and the finder handles column spans itself.
        Nothing is joined unless most shared edges are seen, as a safeguard against unseen borders."""
        cells = [list(r) for r in rects]

        def neighbours() -> list[tuple[int, int, bool, float]]:
            found = []
            for i, a in enumerate(cells):
                for j, b in enumerate(cells):
                    if i == j:
                        continue
                    if abs(a[0] - b[0]) <= 2 and abs(a[2] - b[2]) <= 2 and abs(a[3] - b[1]) <= 2:
                        found.append((i, j, True, self.horizontal_cover(a[3], a[0], a[2])))
                    elif abs(a[1] - b[1]) <= 2 and abs(a[3] - b[3]) <= 2 and abs(a[2] - b[0]) <= 2:
                        found.append((i, j, False, self.vertical_cover(a[2], a[1], a[3])))  # counted, never joined
            return found

        pairs = neighbours()
        if not pairs or sum(1 for *_, c in pairs if c >= 0.5) < 0.6 * len(pairs):
            return [tuple(r) for r in cells]  # type: ignore[misc]
        while True:
            open_pairs = [p for p in neighbours() if p[2] and p[3] < 0.5]
            if not open_pairs:
                break
            i, j, _, _ = open_pairs[0]
            a, b = cells[i], cells[j]
            cells[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
            del cells[j]
        return [tuple(r) for r in cells]  # type: ignore[misc]

    def around(self, chars: Sequence[Ch]) -> Optional[tuple[float, float, float, float]]:
        visible = [c for c in chars if c.text.strip()]
        if not visible:
            return None
        x0, x1 = min(c.x0 for c in visible), max(c.x1 for c in visible)
        top, bottom = min(c.top for c in visible), max(c.bottom for c in visible)
        cx, cy = (x0 + x1) / 2, (top + bottom) / 2
        left = [x for x, a, b in self.vertical if x <= x0 + 1.0 and a - 1 <= cy <= b + 1]
        right = [x for x, a, b in self.vertical if x >= x1 - 1.0 and a - 1 <= cy <= b + 1]
        above = [y for y, a, b in self.horizontal if y <= top + 1.0 and a - 1 <= cx <= b + 1]
        below = [y for y, a, b in self.horizontal if y >= bottom - 1.0 and a - 1 <= cx <= b + 1]
        if not (left and right):
            return None
        # a row cut by a page break has no border on that side of the page
        return (max(left), max(above) if above else top - 2, min(right),
                min(below) if below else bottom + 2)


def _assign_grid(table: Table) -> bool:
    """Work out column and row spans from cell rectangles. Returns False when they do not fit."""
    # The tags sometimes list the lines of one vertically merged cell as several rows; the cells
    # then share one rectangle on the page. Fold them into the first.
    seen: dict[tuple, Cell] = {}
    for row in table.rows:
        for cell in list(row):
            if not cell.rect:
                continue
            key = (cell.page,) + tuple(round(v) for v in cell.rect)
            if key in seen:
                seen[key].chars += cell.chars
                row.remove(cell)
            else:
                seen[key] = cell
    table.rows[:] = [row for row in table.rows if row]

    known = [c for row in table.rows for c in row if c.rect]
    if not known:
        return False
    x_edges = _cluster(x for c in known for x in (c.rect[0], c.rect[2]))
    ncols = len(x_edges) - 1
    if ncols < 1:
        return False

    # Top edge of each row, per page, so a cell's row span is the number of rows it covers.
    tops: dict[int, list[float]] = collections.defaultdict(list)
    for row in table.rows:
        rects = [c.rect for c in row if c.rect]
        if rects:
            tops[row[0].page].append(min(r[1] for r in rects))

    for row in table.rows:
        for cell in row:
            if cell.rect:
                c0, c1 = _nearest(x_edges, cell.rect[0]), _nearest(x_edges, cell.rect[2])
                cell.col, cell.colspan = c0, max(c1 - c0, 1)
                covered = sum(
                    1 for top in tops[cell.page]
                    if cell.rect[1] - _EDGE_TOL <= top < cell.rect[3] - _EDGE_TOL
                )
                cell.rowspan = max(covered, 1)

    # Place cells row by row, as AsciiDoc will, to confirm the spans agree with the column starts.
    occupied: set[tuple[int, int]] = set()
    for r, row in enumerate(table.rows):
        cursor = 0
        kept: list[Cell] = []
        for i, cell in enumerate(row):
            while (r, cursor) in occupied:
                cursor += 1
            if cell.rect is None:
                # A blank cell with no border is a real empty cell only when there is room before the
                # next positioned cell; otherwise it is a placeholder (a merged-away cell) and is dropped.
                following = next((c for c in row[i + 1:] if c.rect), None)
                limit = following.col if following else ncols
                room = sum(1 for col in range(cursor, limit) if (r, col) not in occupied)
                if room < 1:
                    continue
                blanks = sum(1 for c in row[i:] if c.rect is None and row.index(c) < row.index(following)) \
                    if following else sum(1 for c in row[i:] if c.rect is None)
                cell.col, cell.colspan = cursor, (room if blanks == 1 else 1)
            elif cell.col < cursor:
                return False
            elif cell.col > cursor:
                # a continuation row after a page break leaves the earlier columns empty
                for gap in range(cursor, cell.col):
                    if (r, gap) not in occupied:
                        kept.append(Cell(header=False, chars=[], col=gap, page=cell.page))
                        occupied.add((r, gap))
                cursor = cell.col
            kept.append(cell)
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied.add((r + dr, cell.col + dc))
            cursor = cell.col + cell.colspan
        # tags can leave out trailing cells; AsciiDoc needs every row to reach the last column
        for gap in range(cursor, ncols):
            if (r, gap) not in occupied:
                kept.append(Cell(header=False, chars=[], col=gap, page=row[0].page if row else 0))
                occupied.add((r, gap))
        row[:] = kept
        if cursor > ncols:
            return False
    return True


def _pad_rows(table: Table) -> int:
    """Fallback when borders do not explain the layout: stretch short rows to the widest row."""
    width = max(sum(c.colspan for c in row) for row in table.rows)
    for row in table.rows:
        for cell in row:
            cell.colspan = cell.rowspan = 1
        short = width - len(row)
        if short > 0 and row:
            row[-1].colspan = 1 + short
    return width


# --------------------------------------------------------------------------
# Converter
# --------------------------------------------------------------------------


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "document"


def _table_rows(table: dict) -> list[dict]:
    """The TR nodes of a Table, whether they sit directly under it or under THead, TBody and TFoot."""
    rows: list[dict] = []
    for child in table.get("children", []):
        if child["type"] == "TR":
            rows.append(child)
        elif child["type"] in ("THead", "TBody", "TFoot"):
            rows.extend(r for r in child.get("children", []) if r["type"] == "TR")
    return rows


def _first_page(node: dict) -> Optional[int]:
    if node.get("page_number"):
        return node["page_number"]
    for child in node.get("children", []):
        page = _first_page(child)
        if page:
            return page
    return None


class TaggedPdfConverter:
    """Convert one tagged PDF to an AsciiDoc string (see the module docstring)."""

    LIST = "\x03"  # marks a list item, so consecutive items are written without blank lines

    def __init__(
        self,
        path: Union[str, Path],
        title: str = "",
        images_dir: Union[str, Path, None] = None,
        image_ref_prefix: Optional[str] = None,
        ignore: Sequence[str] = (),
        min_image_size: float = 16,
        ocr: bool = True,
        layout: bool = False,
    ) -> None:
        import pdfplumber

        self.path = Path(path)
        self.title = title or self.path.stem.replace("_", " ")
        self.images_dir = Path(images_dir) if images_dir else None
        self.image_prefix = image_ref_prefix or (self.images_dir.name if self.images_dir else "images")
        self.ignore = [re.compile(p, re.IGNORECASE) for p in ignore]
        self.min_image_size = min_image_size
        self.slug = _slugify(self.path.stem)
        self.pdf = pdfplumber.open(str(self.path))
        self.images_written = 0
        self._image_repeats: collections.Counter[tuple] = collections.Counter(
            self._image_key(image) for page in self.pdf.pages for image in page.images
        )
        self.rendered_images: list[str] = []  # pictures saved as a page render, not as stored
        self._pdfium: Any = None
        self.ocr = ocr
        self.layout = layout  # read an untagged PDF from its layout instead of its tags
        self.notes: list[str] = []  # what the OCR step did, for the caller to show
        self._reader: Any = None
        self._reader_tried = False
        self._vocabulary_words: Optional[set[str]] = None
        self.pictures_kept = 0  # pictures left as pictures

    # -- availability ------------------------------------------------------------

    @classmethod
    def usable(cls, path: Union[str, Path]) -> bool:
        """True when the PDF carries a structure tree with marked content on its pages."""
        import pdfplumber

        try:
            with pdfplumber.open(str(path)) as pdf:
                if not pdf.structure_tree:
                    return False
                return any(
                    c.get("mcid") is not None for page in pdf.pages[:3] for c in page.chars
                )
        except Exception:
            return False

    @classmethod
    def layout_usable(cls, path: Union[str, Path]) -> bool:
        """True when the PDF has a text layer to read (a scan does not, and needs OCR)."""
        import pdfplumber

        try:
            with pdfplumber.open(str(path)) as pdf:
                pages = pdf.pages[:5]
                return bool(pages) and sum(len(p.chars) for p in pages) >= 100 * len(pages)
        except Exception:
            return False

    # -- structure tree --------------------------------------------------------

    def _index_structure(self) -> None:
        """Map (page, mcid) to its tagged paragraph, and find the real tables."""
        self.owner: dict[tuple[int, int], int] = {}
        self.cell_of: dict[tuple[int, int], tuple[int, int, int]] = {}
        self.tables: list[Table] = []
        # Paragraph ids of marked content tagged straight onto a Table node: captions, and header
        # cells that the tags do not wrap in a TR.
        self.table_owned: set[int] = set()
        # Tag type (H1..H6, TOC, TOCI, Caption) of the paragraphs that sit under such a tag.
        self.owner_kind: dict[int, str] = {}
        if self.layout:  # no tags to read: tables and paragraphs come from the page layout
            return
        counter = iter(range(10**9))

        def mcid_pages(node: dict, page: int) -> list[tuple[int, int]]:
            result, previous = [], None
            for mcid in node.get("mcids") or []:
                if previous is not None and mcid < previous:
                    page += 1
                result.append((page, mcid))
                previous = mcid
            return result

        def leaves(node: dict, page: Optional[int], ctx: Optional[str] = None) -> list[tuple[int, int, int]]:
            page = node.get("page_number") or page
            if _SEMANTIC_TAG_RE.fullmatch(node["type"]):
                ctx = node["type"]
            found = []
            if node.get("mcids") and page:
                owner = next(counter)
                if ctx:
                    self.owner_kind[owner] = ctx
                for key in mcid_pages(node, page):
                    self.owner[key] = owner
                    found.append((key[0], key[1], owner))
            for child in node.get("children", []):
                if child["type"] != "Table":
                    found.extend(leaves(child, page, ctx))
            return found

        def visit(node: dict, page: Optional[int], ctx: Optional[str] = None) -> None:
            page = node.get("page_number") or page
            if _SEMANTIC_TAG_RE.fullmatch(node["type"]):
                ctx = node["type"]
            if node["type"] == "Table" and node.get("children"):
                rows = _table_rows(node)
                if rows and any(len(r.get("children", [])) > 1 for r in rows):
                    index = len(self.tables)
                    table_rows: list[list[Cell]] = []
                    for r, row in enumerate(rows):
                        cells = []
                        for c, cell in enumerate(row.get("children", [])):
                            keys = leaves(cell, page)
                            for p, mcid, _ in keys:
                                self.cell_of[(p, mcid)] = (index, r, c)
                            cells.append(
                                Cell(header=cell["type"] == "TH", chars=[],
                                     page=(keys[0][0] if keys else (_first_page(cell) or page or 0)))
                            )
                        table_rows.append(cells)
                    self.tables.append(
                        Table(rows=table_rows, pages=set(), first_page=_first_page(node) or page or 0)
                    )
                    return
            for child in node.get("children", []):
                visit(child, page, ctx)
            if node.get("mcids") and page:
                found = leaves(node, page, ctx)
                if node["type"] == "Table":
                    self.table_owned.update(owner for _, _, owner in found)

        for root in self.pdf.structure_tree:
            visit(root, None)

    # -- page content ------------------------------------------------------------

    def _skip(self, line_text: str) -> bool:
        return any(p.search(line_text) for p in self.ignore)

    def _page_chars(self, page: Any, number: int) -> tuple[list[Ch], list[Ch]]:
        """(flow characters, table-owned characters) of a page.

        Characters inside tagged table cells are filed under their cell; untagged characters are
        page furniture and dropped.
        """
        flow, table_owned = [], []
        for raw in page.chars:
            mcid = raw.get("mcid")
            if mcid is None:
                continue
            owner = self.owner.get((number, mcid))
            ch = make_char(raw, owner, number)
            key = self.cell_of.get((number, mcid))
            if key is not None:
                t, r, c = key
                self.tables[t].rows[r][c].chars.append(ch)
                self.tables[t].pages.add(number)
            elif owner in self.table_owned:
                table_owned.append(ch)
            else:
                flow.append(ch)
        return flow, table_owned

    def _adopt_orphans(self, chars: list[Ch], number: int, borders: "PageBorders") -> list[Ch]:
        """File table-owned characters that sit inside the page's cell borders as cells of the
        nearest table; return the rest (captions), which stay in the text flow."""
        by_rect: dict[tuple[float, ...], list[Ch]] = collections.defaultdict(list)
        rest = []
        for ch in chars:
            rect = borders.around([ch]) if ch.text.strip() else None
            if rect is None:
                rest.append(ch)
            else:
                by_rect[tuple(round(v, 1) for v in rect)].append(ch)
        if not by_rect:
            return rest

        def span(table: Table, rect: tuple[float, ...]) -> float:
            """Vertical distance from the orphan row to the table's text on this page."""
            seen = [c for row in table.rows for cell in row if cell.page == number
                    for c in cell.chars if c.text.strip()]
            if not seen:
                return 1e9
            return max(min(c.top for c in seen) - rect[3], rect[1] - max(c.bottom for c in seen), 0.0)

        rows: dict[float, list[Cell]] = {}
        for rect, members in by_rect.items():
            members.sort(key=lambda c: (c.top, c.x0))
            cell = Cell(header=all(c.bold for c in members if c.text.strip()),
                        chars=members, rect=rect, page=number)  # type: ignore[arg-type]
            key = next((k for k in rows if abs(k - rect[1]) <= _EDGE_TOL), rect[1])
            rows.setdefault(key, []).append(cell)

        for top, cells in sorted(rows.items()):
            cells.sort(key=lambda c: c.rect[0])
            candidates = [t for t in self.tables if number in t.pages]
            if not candidates:
                for cell in cells:
                    rest.extend(cell.chars)
                continue
            table = min(candidates, key=lambda t: span(t, cells[0].rect))

            def row_key(row: list[Cell]) -> tuple[int, float]:
                tops = [c.top for cell in row for c in cell.chars if c.text.strip()]
                return (row[0].page if row else 0, min(tops)) if tops else (0, -1.0)

            where = len(table.rows)
            for i, row in enumerate(table.rows):
                key = row_key(row)
                if key[1] >= 0 and key > (number, top):
                    where = i
                    break
            table.rows.insert(where, cells)
        return rest

    @staticmethod
    def _image_key(image: dict) -> tuple:
        return tuple(round(image[k]) for k in ("x0", "top", "x1", "bottom"))

    def _is_furniture_image(self, image: dict, page: Any) -> bool:
        """A running header or footer picture, or letterhead: tagged as an artifact and either in the
        page margin or repeated on several pages. A picture of content can be tagged as an artifact
        too (a pasted table), so artifacts elsewhere on the page are kept."""
        if not self.layout and image.get("mcid") is not None:
            return False
        in_margin = image["bottom"] <= 0.12 * page.height or image["top"] >= 0.88 * page.height
        return in_margin or self._image_repeats[self._image_key(image)] >= 3

    def _page_images(self, page: Any, number: int) -> list[Block]:
        found = []
        for image in page.images:
            width, height = image["x1"] - image["x0"], image["bottom"] - image["top"]
            if min(width, height) < self.min_image_size or self._is_furniture_image(image, page):
                continue
            found.append(
                Block(kind="image", page=number, top=image["top"], bottom=image["bottom"],
                      x0=image["x0"], x1=image["x1"],
                      payload=(page, (image["x0"], image["top"], image["x1"], image["bottom"]),
                               image))
            )
        return found

    def _native_image(self, number: int, bbox: tuple[float, float, float, float], image: dict,
                      stem: Path) -> Optional[Path]:
        """Write the picture's own pixels (JPEG and JPEG 2000 bytes untouched, anything else decoded
        losslessly to PNG). None when that would not show the picture as placed: the page rotates or
        mirrors it, or it has a stencil or a soft mask with transparency."""
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_raw
        from pdfminer.pdftypes import resolve1

        attrs = image["stream"].attrs
        if resolve1(attrs.get("ImageMask")) or "Mask" in attrs:
            return None
        alpha = None
        if "SMask" in attrs:
            soft = resolve1(attrs["SMask"])
            data = soft.get_data()
            if data and min(data) < 255:  # a transparent picture: carry its mask over as alpha
                width, height = resolve1(soft.attrs.get("Width")), resolve1(soft.attrs.get("Height"))
                if not width or not height or len(data) != width * height:
                    return None  # a mask pdfminer did not decode to plain bytes
                from PIL import Image

                alpha = Image.frombytes("L", (width, height), bytes(data))

        if self._pdfium is None:
            self._pdfium = pdfium.PdfDocument(str(self.path))
        page = self._pdfium[number - 1]
        height = page.get_height()
        for obj in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]):
            left, bottom, right, top = obj.get_bounds()
            edges = (left, height - top, right, height - bottom)
            if any(abs(a - b) > 2.0 for a, b in zip(edges, bbox)):
                continue
            matrix = obj.get_matrix()
            upright = (matrix.a > 0 and matrix.d > 0
                       and abs(matrix.b) < 0.01 * matrix.a and abs(matrix.c) < 0.01 * matrix.d)
            if not upright:
                return None
            if alpha is not None:
                picture = obj.get_bitmap(render=False).to_pil().convert("RGB")
                if alpha.size != picture.size:
                    alpha = alpha.resize(picture.size)
                picture.putalpha(alpha)
                target = stem.with_name(stem.name + ".png")
                picture.save(target)
                return target
            for stale in stem.parent.glob(stem.name + ".*"):  # an earlier run's file with another extension
                stale.unlink()
            obj.extract(stem)
            written = sorted(stem.parent.glob(stem.name + ".*"))
            return written[0] if written else None
        return None

    def _write_image(self, number: int, page: Any, bbox: tuple[float, float, float, float],
                     image: dict) -> Optional[str]:
        """Save one picture to the images directory; return its reference."""
        if self.images_dir is None:
            return None
        self.images_written += 1
        stem = f"{self.slug}-image-{self.images_written:02d}"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        try:
            saved = self._native_image(number, bbox, image, self.images_dir / stem)
        except Exception:  # an odd image object: fall back to the page render
            saved = None
        if saved is None:
            # A render of the page area at the picture's own resolution (never below 200 dpi).
            native_width = (image.get("srcsize") or (0, 0))[0]
            dpi = max(200, round(native_width / max((bbox[2] - bbox[0]) / 72, 0.01)))
            x0, top, x1, bottom = bbox
            crop = page.crop((max(x0, 0), max(top, 0), min(x1, page.width), min(bottom, page.height)))
            saved = self.images_dir / f"{stem}.png"
            crop.to_image(resolution=dpi).original.save(saved)
            self.rendered_images.append(saved.name)
        return f"{self.image_prefix}/{saved.name}"

    # -- assembling blocks -------------------------------------------------------

    def _cover_title(self, seq: list[Block]) -> None:
        """The document title of an untagged PDF: the one largest line of text on page 1, when it is much
        larger than the body text. It leaves the body and becomes the AsciiDoc title."""
        first = [b for b in seq if b.kind in ("para", "center") and b.page == 1]
        if not first:
            return
        largest = max(first, key=lambda b: b.size)
        text = plain(largest.units).replace("\n", " ").strip()
        same = [b for b in first if round(b.size) == round(largest.size)]
        if len(same) == 1 and largest.size >= 1.5 * self._body_size and len(largest.lines) <= 2 and 0 < len(text) <= 120:
            self.title = text
            seq.remove(largest)

    def _layout_furniture(self) -> set[tuple[str, int]]:
        """Lines that repeat at the same height on at least half the pages (running headers and footers,
        page numbers), by text with the digits blanked."""
        seen: collections.Counter[tuple[str, int]] = collections.Counter()
        for number, page in enumerate(self.pdf.pages, 1):
            keys = set()
            for line in group_lines(make_char(raw, None, number) for raw in page.chars):
                text = _line_text(line)
                if text:
                    keys.add((re.sub(r"\d+", "#", text.lower()), round(line.top / 4)))
            seen.update(keys)
        threshold = max(2, -(-len(self.pdf.pages) // 2))  # half the pages, rounded up, never fewer than two
        return {key for key, count in seen.items() if count >= threshold}

    def _layout_tables(self, page: Any, number: int, chars: list[Ch]) -> tuple[list[Table], list[Ch]]:
        """Tables drawn with ruling lines, found by pdfplumber, with the characters inside their cells.
        Returns the tables and the characters left for the text flow."""
        tables: list[Table] = []
        taken: set[int] = set()
        borders = PageBorders(page)
        for found in page.find_tables():
            cells = list(found.cells)
            if len(cells) < 2:  # a lone box is a callout, not a table
                continue
            cells = borders.merge_cells([tuple(c) for c in cells])
            members: dict[tuple, Cell] = {}
            for rect in cells:
                members[tuple(rect)] = Cell(header=False, chars=[], rect=tuple(rect), page=number)
            ordered = sorted(members.values(), key=lambda c: (c.rect[1], c.rect[0]))
            for ch in chars:
                if id(ch) in taken:
                    continue
                cx, cy = (ch.x0 + ch.x1) / 2, (ch.top + ch.bottom) / 2
                inside = [c for c in ordered if c.rect[0] <= cx < c.rect[2] and c.rect[1] <= cy < c.rect[3]]
                if inside:
                    min(inside, key=lambda c: (c.rect[2] - c.rect[0]) * (c.rect[3] - c.rect[1])).chars.append(ch)
                    taken.add(id(ch))
            rows: list[list[Cell]] = []
            for cell in ordered:
                if rows and abs(rows[-1][0].rect[1] - cell.rect[1]) <= _EDGE_TOL:
                    rows[-1].append(cell)
                else:
                    rows.append([cell])
            for row in rows:
                row.sort(key=lambda c: c.rect[0])
                for cell in row:
                    visible = [c for c in cell.chars if c.text.strip()]
                    cell.header = bool(visible) and all(c.bold for c in visible)
            table = Table(rows=rows, pages={number}, first_page=number)
            table.top = found.bbox[1]
            tables.append(table)
        return tables, [c for c in chars if id(c) not in taken]

    def _sequence_layout(self) -> list[Block]:
        furniture = self._layout_furniture()
        seq: list[Block] = []
        for number, page in enumerate(self.pdf.pages, 1):
            chars = [make_char(raw, None, number) for raw in page.chars]
            dropped = {
                id(c) for line in group_lines(chars)
                if (re.sub(r"\d+", "#", _line_text(line).lower()), round(line.top / 4)) in furniture
                for c in line.chars
            }
            chars = [c for c in chars if id(c) not in dropped]
            tables, flow = self._layout_tables(page, number, chars)
            lines = [ln for ln in group_lines(flow) if not self._skip(plain(line_units(ln)))]
            wide = [ln.x1 for ln in lines if len(_line_text(ln)) >= 50]
            blocks = flow_blocks(lines, layout=True, right_edge=max(wide, default=0.0), pitch=_line_pitch(lines))
            items = blocks + self._page_images(page, number)
            for table in tables:
                self.tables.append(table)
                items.append(Block(kind="table", page=number, top=table.top, bottom=table.top,
                                   payload=len(self.tables) - 1))
            items.sort(key=lambda b: b.top)
            seq.extend(items)
        return self._merge_pages_layout(seq)

    def _merge_pages_layout(self, seq: list[Block]) -> list[Block]:
        """An untagged paragraph that runs over a page break: it stops near the foot of a page without
        a full stop, or the next page starts in lower case."""
        merged: list[Block] = []
        for block in seq:
            previous = merged[-1] if merged else None
            if (block.kind == "para" and previous is not None and previous.kind == "para"
                    and block.page == previous.page + 1
                    and abs(block.size - previous.size) <= 0.6
                    and parse_label(plain(block.units)) is None
                    and bold_share(block.units) < 0.9):
                height = self.pdf.pages[previous.page - 1].height
                unfinished = not plain(previous.units).rstrip().endswith((".", ":", ";", "!", "?"))
                if previous.bottom > 0.8 * height and block.top < 0.2 * height and (
                    unfinished or plain(block.units)[:1].islower()
                ):
                    previous.lines += block.lines
                    previous.units = join_units(previous.lines)
                    previous.bottom, previous.page = block.bottom, block.page
                    continue
            merged.append(block)
        return merged

    def _sequence(self) -> list[Block]:
        self._index_structure()
        if self.layout:
            return self._sequence_layout()
        pages = []
        for number, page in enumerate(self.pdf.pages, 1):
            flow, owned = self._page_chars(page, number)
            pages.append((number, page, flow, owned))

        seq: list[Block] = []
        for number, page, flow, owned in pages:
            borders = PageBorders(page)
            flow += self._adopt_orphans(owned, number, borders)
            for table in self.tables:
                for row in table.rows:
                    for cell in row:
                        if cell.page == number and cell.chars and cell.rect is None:
                            cell.rect = borders.around(cell.chars)

            lines = [ln for ln in group_lines(flow) if not self._skip(plain(line_units(ln)))]
            blocks = flow_blocks(lines)
            items = blocks + self._page_images(page, number)
            for t, table in enumerate(self.tables):
                if number in table.pages:
                    tops = [c.top for row in table.rows for cell in row if cell.page == number
                            for c in cell.chars if c.text.strip()]
                    if tops:
                        items.append(Block(kind="table", page=number, top=min(tops),
                                           bottom=max(tops), payload=t))
            items.sort(key=lambda b: b.top)
            seq.extend(items)
        return self._merge_pages(seq)

    @staticmethod
    def _merge_pages(seq: list[Block]) -> list[Block]:
        """A tagged paragraph that runs over a page break is one paragraph."""
        merged: list[Block] = []
        for block in seq:
            previous = merged[-1] if merged else None
            if (block.kind == "para" and previous is not None and previous.kind == "para"
                    and block.owner is not None and block.owner == previous.owner
                    and block.page == previous.page + 1):
                previous.lines += block.lines
                previous.units = join_units(previous.lines)
                previous.bottom, previous.page = block.bottom, block.page
                continue
            merged.append(block)
        return merged

    # -- classification ----------------------------------------------------------

    def _classify(self, seq: list[Block]) -> list[Block]:
        headings = Headings()
        page_mid = {n: p.width / 2 for n, p in enumerate(self.pdf.pages, 1)}
        left_margin = min((b.x0 for b in seq if b.kind == "para" and len(b.lines) > 2), default=72.0)
        sizes: collections.Counter[int] = collections.Counter()
        for b in seq:
            if b.kind == "para":
                sizes[round(b.size)] += len(b.units)
        body_size = sizes.most_common(1)[0][0] if sizes else 12
        self._body_size = body_size
        out: list[Block] = []
        for block in seq:
            if block.kind != "para":
                out.append(block)
                continue
            tag = self.owner_kind.get(block.owner) if block.owner is not None else None
            if tag in ("TOC", "TOCI"):
                self.has_toc = True
                continue
            if tag and tag.startswith("H"):
                block.kind = "heading"
                block.level = 1 + int(tag[1:] or 1)
                block.payload = "tag"
                out.append(block)
                continue
            found = headings.classify(block)
            if found:
                kind, text = found
                block.kind, block.level = "heading", headings.level(kind)
                out.append(block)
                continue
            centred = all(abs((ln.x0 + ln.x1) / 2 - page_mid[block.page]) <= 8 for ln in block.lines)
            if (self.layout and not centred and bold_share(block.units) >= 0.9 and len(block.lines) <= 2
                    and len(plain(block.units)) <= 120 and block.size >= 1.1 * body_size):
                # An untagged heading: bold and larger than the text. Its level follows its size.
                block.kind, block.payload = "heading", "size"
                out.append(block)
                continue
            if centred and block.x0 - left_margin > 30:
                block.kind = "center"
                out.append(block)
                continue
            label = parse_label(plain(block.units))
            if label and label.kind != "bullet" and block.size < 0.85 * body_size:
                # Footnotes and table notes keep their label as text instead of joining the list outline.
                # The delimiter is written as a character reference so AsciiDoc does not see a list marker.
                if not label.raw.startswith("("):
                    for i, (t, style) in enumerate(block.units[:6]):
                        if t in ".)":
                            block.units[i] = ("\x01" if t == "." else "\x02", style)
                            break
            elif label and label.raw in "-–—" and block.size < 0.85 * body_size:
                block.units[0] = ("\x04" if label.raw == "-" else label.raw, block.units[0][1])
            else:
                split_label(block)
            out.append(block)
        # centred single lines that follow one another are one title block
        joined: list[Block] = []
        for block in out:
            previous = joined[-1] if joined else None
            if (block.kind == "center" and previous is not None and previous.kind == "center"
                    and block.page == previous.page and block.top - previous.bottom < 1.6 * block.size):
                previous.lines += block.lines
                previous.bottom = block.bottom
                previous.units += [("\n", None)] + block.units
                continue
            joined.append(block)
        return joined

    # -- rendering ---------------------------------------------------------------

    def _render_cell(self, cell: Cell) -> tuple[str, bool]:
        """(text, needs AsciiDoc cell style) for one table cell."""
        lines = [ln for ln in group_lines(cell.chars) if not ln.blank]
        paragraphs: list[list[Line]] = []
        for line in lines:
            previous = paragraphs[-1][-1] if paragraphs else None
            starts_item = (parse_label(plain(line_units(line)).lstrip()) or Label("", 0, "")).kind == "bullet"
            if previous is None or starts_item or line.top - previous.bottom > 0.9 * line.size:
                paragraphs.append([line])
            else:
                paragraphs[-1].append(line)
        parts: list[tuple[str, bool]] = []  # (text, is a bullet)
        for group in paragraphs:
            block = _lines_to_block(group)
            label = parse_label(plain(block.units))
            if label is not None and label.kind == "bullet":
                split_label(block)  # the bullet becomes a list marker; any other label stays as text
                parts.append((f"* {render_units(block.units, bold=not cell.header)}", True))
            else:
                parts.append((render_units(block.units, bold=not cell.header), False))
        rich = any(is_bullet for _, is_bullet in parts)
        text = ""
        for i, (piece, is_bullet) in enumerate(parts):
            if i:  # bullets follow each other directly; anything else needs a blank line
                text += "\n" if is_bullet and parts[i - 1][1] else "\n\n"
            text += piece
        return text.replace("|", "\\|"), rich

    @staticmethod
    def _alignment(cell: Cell, rect: Optional[tuple[float, float, float, float]]) -> str:
        lines = [ln for ln in group_lines(cell.chars) if not ln.blank]
        if not lines or rect is None:
            return ""
        left, right = rect[0], rect[2]
        middle = (left + right) / 2
        slack = [(ln.x0 - left, right - ln.x1) for ln in lines]
        if all(a >= 4 and b >= 4 for a, b in slack) and all(
            abs((ln.x0 + ln.x1) / 2 - middle) <= 3 for ln in lines
        ):
            return "^"
        if all(b <= 4 and a >= 10 for a, b in slack):
            return ">"
        return ""

    def _render_table(self, table: Table, page_width: float) -> str:
        if not _assign_grid(table):
            _pad_rows(table)
            note = "// Cell spans could not be recovered from the page borders; row layout may differ.\n"
        else:
            note = ""
        known = [c for row in table.rows for c in row if c.rect]
        x_edges = _cluster(x for c in known for x in (c.rect[0], c.rect[2])) if known else []
        ncols = max(len(x_edges) - 1, max(sum(c.colspan for c in row) for row in table.rows))

        aligns: list[collections.Counter] = [collections.Counter() for _ in range(ncols)]
        rendered: list[list[tuple[Cell, str, bool, str]]] = []
        for row in table.rows:
            cells = []
            for cell in row:
                text, rich = self._render_cell(cell)
                align = self._alignment(cell, cell.rect)
                if cell.colspan == 1 and cell.col < ncols and cell.chars:
                    aligns[cell.col][align] += 1
                cells.append((cell, text, rich, align))
            rendered.append(cells)
        column_align = [c.most_common(1)[0][0] if c else "" for c in aligns]

        if len(x_edges) == ncols + 1:
            widths = [x_edges[i + 1] - x_edges[i] for i in range(ncols)]
            total = sum(widths)
            cols = [f"{column_align[i] or '<'}{max(round(100 * w / total), 1)}" for i, w in enumerate(widths)]
            span = (x_edges[-1] - x_edges[0]) / (page_width - 144)
            attrs = [f'cols="{",".join(cols)}"', f"width={min(round(span * 100), 100)}%"]
        else:
            attrs = [f'cols="{ncols}*"']
        first = table.rows[0]
        if first and all(c.header for c in first):
            attrs.append("options=header")
        lines = [f"[{','.join(attrs)}]", "|==="]
        for r, cells in enumerate(rendered):
            header_row = r == 0 and "options=header" in attrs
            parts = []
            for cell, text, rich, align in cells:
                prefix = ""
                if cell.colspan > 1:
                    prefix += str(cell.colspan)
                if cell.rowspan > 1:
                    prefix += f".{cell.rowspan}"
                if prefix:
                    prefix += "+"
                column = column_align[cell.col] if cell.col < ncols else ""
                if cell.colspan > 1 or align != column:
                    prefix += align
                if rich:
                    prefix += "a"  # a cell takes one style letter, and a list needs the AsciiDoc one
                elif cell.header and not header_row:
                    prefix += "h"
                parts.append(f"{prefix}|{text}")
            lines.append("\n".join(parts) if any("\n" in p for p in parts) else " ".join(parts))
            lines.append("")
        lines[-1:] = ["|==="]
        return note + "\n".join(lines)

    def _vocabulary(self) -> set[str]:
        """Words of the document's own text, used to reject OCR readings that are not words."""
        if self._vocabulary_words is None:
            words: set[str] = set()
            for page in self.pdf.pages:
                for word in re.findall(r"[A-Za-z][A-Za-z+*\-]*", page.extract_text() or ""):
                    words.add(word.lower())
                    words.add(word.lower().strip("*+-"))
            self._vocabulary_words = words
        return self._vocabulary_words

    def _transcribe(self, block: Block, ref: str, width_percent: int) -> Optional[list[str]]:
        """AsciiDoc blocks for a picture that is a ruled table, read by OCR; None for any other picture."""
        if not self.ocr or self.images_dir is None:
            return None
        if self._reader is None and not self._reader_tried:
            from picture_tables import PictureTableReader

            self._reader_tried = True
            self._reader = PictureTableReader.create(log=self.notes.append)
        if self._reader is None:
            return None
        path = self.images_dir / Path(ref).name
        table = self._reader.read(path, self._vocabulary())
        while self._reader.errors:
            self.notes.append(f"OCR could not read {self._reader.errors.pop()}; the picture is kept")
        text = table.to_asciidoc(escape, width_percent) if table else None
        if text is None:
            return None
        flagged = table.review_lines()
        self.notes.append(
            f"page {block.page}: transcribed {ref} as a table of {len(table.cells)} cells; "
            + (f"{len(flagged)} to check against the picture" if flagged else "no cell needs review")
        )
        comment = [f"// OCR transcription of {ref} (picture on page {block.page}); engines: {', '.join(table.engines)}.",
                   "// The picture is not in the text layer, so the checker cannot verify this table: compare it with the picture.",
                   f"// image::{ref}[Picture on page {block.page}]"]
        comment += [f"// Check {line}" for line in flagged]
        parts = []
        if table.above:
            parts.append(escape(" ".join(table.above)))
        parts.append("\n".join(comment) + "\n" + text)
        if table.below:
            parts.append(escape(" ".join(table.below)))
        return parts

    def _render_image(self, block: Block) -> list[str]:
        """AsciiDoc blocks for a picture: an OCR table when it is a ruled table, else the picture."""
        page, bbox, image = block.payload
        ref = self._write_image(block.page, page, bbox, image)
        width = round((bbox[2] - bbox[0]) * 96 / 72)
        note = ("// REVIEW: picture in the PDF. Text inside it is not in the text layer, so it cannot be\n"
                "// checked automatically; transcribe it as an AsciiDoc table or verify the image.\n")
        if ref is None:
            return [note + "// (picture omitted: no images directory)"]
        percent = min(round(100 * (bbox[2] - bbox[0]) / max(page.width - 144, 1)), 100)
        transcription = self._transcribe(block, ref, percent)
        if transcription is not None:
            return transcription
        self.pictures_kept += 1
        return [f"{note}image::{ref}[Picture on page {block.page},width={width}]"]

    # -- lists -------------------------------------------------------------------

    @staticmethod
    def _marker(levels: list[dict], depth: int) -> str:
        level = levels[depth]
        same = sum(1 for lv in levels[: depth + 1] if lv["ordered"] == level["ordered"])
        return ("." if level["ordered"] else "*") * same

    def _emit(self, seq: list[Block]) -> list[str]:
        out: list[str] = []
        levels: list[dict] = []
        chunk: Optional[list[str]] = None  # current list item: item line then continuation parts
        item_open = False  # nothing attached to the current item yet

        def flush() -> None:
            nonlocal chunk, item_open
            if chunk is not None:
                out.append(self.LIST + "\n".join(chunk))
                chunk = None
            item_open = False

        def attach(text: str) -> None:
            nonlocal chunk, item_open
            if chunk is None:
                chunk = [text]
            elif item_open and chunk[-1].rstrip().endswith(":") and not text.startswith(("[", "|", "image:", "//")):
                # a label line followed by its value: keep them together with a hard line break
                chunk[-1] += f" +\n{text}"
            else:
                chunk.extend(["+", text])
            item_open = False

        for i, block in enumerate(seq):
            if block.kind == "heading":
                flush()
                levels.clear()
                out.append(f"{'=' * block.level} {render_units(block.units, bold=False)}")
                continue
            if block.kind == "para" and block.label:
                flush()
                self._push_level(levels, block)
                depth = len(levels) - 1
                level = levels[depth]
                lines = []
                if level["fresh"]:
                    if level["style"]:
                        lines.append(f"[{level['style']}]")
                    if level["ordered"] and level["first"] != 1:
                        lines.append(f"[start={level['first']}]")
                    level["fresh"] = False
                text = render_units(block.units)
                lines.append(f"{self._marker(levels, depth)} {text}")
                chunk, item_open = lines, True
                continue

            if block.kind == "para":
                text = guard_line_start(render_units(block.units))
                if levels and self._belongs_to_list(seq, i, levels):
                    attach(text)
                    continue
                flush()
                levels.clear()
                out.append(text)
                continue

            if block.kind == "center":
                flush()
                levels.clear()
                parts = [render_units(u) for u in self._split_center(block)]
                out.append("[.text-center]\n" + " +\n".join(parts))
                continue

            if levels and self._left_of_list(block, levels):
                flush()
                levels.clear()
            if block.kind == "table":
                table = self.tables[block.payload]
                if table.emitted:
                    continue
                table.emitted = True
                text = self._render_table(table, self.pdf.pages[block.page - 1].width)
            else:  # image
                for part in self._render_image(block):
                    if levels:
                        attach(part)
                    else:
                        flush()
                        out.append(part)
                continue
            if levels:
                attach(text)
            else:
                flush()
                out.append(text)
        flush()
        return out

    @staticmethod
    def _split_center(block: Block) -> list[list[Unit]]:
        lines: list[list[Unit]] = [[]]
        for unit in block.units:
            if unit[0] == "\n":
                lines.append([])
            else:
                lines[-1].append(unit)
        return lines

    def _push_level(self, levels: list[dict], block: Block) -> None:
        kind = block.label_kind
        ordered = kind != "bullet"
        # A lone i, v, x (or I, V, X) is a letter when it continues an alphabetic list, else a roman numeral.
        ident = block.label.strip("().")
        if kind in ("alpha", "ALPHA") and ident.lower() in ("i", "v", "x"):
            parent = next((lv for lv in reversed(levels) if lv["kind"] == kind), None)
            if not (parent and parent["last"] + 1 == block.label_value):
                kind = "roman" if kind == "alpha" else "ROMAN"
                block.label_value = _roman_value(ident)

        while levels and block.label_x < levels[-1]["label_x"] - _INDENT_TOL:
            levels.pop()
        if levels:
            top = levels[-1]
            same_x = abs(block.label_x - top["label_x"]) <= _INDENT_TOL
            if same_x and top["kind"] == kind:
                top["last"] = block.label_value
                return
            if same_x:
                lower = next((i for i, lv in enumerate(levels) if lv["kind"] == kind
                              and abs(lv["label_x"] - block.label_x) <= _INDENT_TOL), None)
                if lower is not None:
                    del levels[lower + 1:]
                    levels[lower]["last"] = block.label_value
                    return
        style = {"alpha": "loweralpha", "ALPHA": "upperalpha", "roman": "lowerroman",
                 "ROMAN": "upperroman"}.get(kind, "")
        levels.append({
            "kind": kind, "ordered": ordered, "label_x": block.label_x, "content_x": block.content_x,
            "last": block.label_value, "first": block.label_value if ordered else 0,
            "style": style, "fresh": True,
        })

    def _left_of_list(self, block: Block, levels: list[dict]) -> bool:
        """A table or picture that starts left of the list's own labels is not part of the list."""
        if block.kind == "table":
            table = self.tables[block.payload]
            xs = [c.rect[0] for row in table.rows for c in row if c.rect]
            xs = xs or [ch.x0 for row in table.rows for c in row for ch in c.chars if ch.text.strip()]
            x0 = min(xs, default=block.x0)
        else:
            x0 = block.x0
        return x0 < levels[0]["label_x"] - _INDENT_TOL

    def _belongs_to_list(self, seq: list[Block], index: int, levels: list[dict]) -> bool:
        """Is this paragraph part of the open list item, rather than text after the list?"""
        block = seq[index]
        deeper = [i for i, lv in enumerate(levels) if block.x0 >= lv["content_x"] - _INDENT_TOL]
        if deeper:
            del levels[deeper[-1] + 1:]
            return True
        # Left of the list's own labels it is text after the list. At the labels' indent it still belongs
        # when the list carries on after it.
        if block.x0 < levels[0]["label_x"] - _INDENT_TOL:
            return False
        for later in seq[index + 1:]:
            if later.kind == "heading":
                return False
            if later.kind == "para" and later.label:
                # Only a numbered list carries on across a paragraph, and only with the next number: bullets
                # have no such thread, so a plain line at the bullets' indent is text after the list.
                for i, lv in enumerate(levels):
                    if (abs(later.label_x - lv["label_x"]) <= _INDENT_TOL and lv["ordered"]
                            and later.label_value == lv["last"] + 1):
                        del levels[i + 1:]
                        return True
                return False
        return False

    # -- entry point -------------------------------------------------------------

    def close(self) -> None:
        """Release the PDF files; ``convert`` does this itself."""
        self.pdf.close()
        if self._pdfium is not None:
            self._pdfium.close()
            self._pdfium = None

    def convert(self) -> str:
        try:
            return self._convert()
        finally:
            self.close()

    def _convert(self) -> str:
        self.has_toc = False
        seq = self._classify(self._sequence())
        if self.layout:
            self._cover_title(seq)
        # A single H1 ahead of every other heading is the document title.
        tagged = [b for b in seq if b.kind == "heading" and b.payload == "tag"]
        if tagged and tagged[0].level == 2 and sum(1 for b in tagged if b.level == 2) == 1 \
                and seq.index(tagged[0]) < next((i for i, b in enumerate(seq) if b.kind == "heading"
                                                  and b is not tagged[0]), len(seq)):
            self.title = plain(tagged[0].units)
            seq.remove(tagged[0])
            tagged = tagged[1:]
        # Tag levels become consecutive section levels, so a document that starts at H3 gets "==" and
        # never skips a level (Asciidoctor warns about "section title out of sequence").
        ranks = {level: 2 + i for i, level in enumerate(sorted({b.level for b in tagged}))}
        for b in tagged:
            b.level = min(ranks[b.level], 6)
        # Headings found by type size: the largest is the highest level, below any numbered scheme.
        numbered = [b for b in seq if b.kind == "heading" and b.payload != "size"]
        if numbered:
            # Bold large text ahead of the first numbered heading is front matter (a cover, a notice), not a
            # section; later ones take the level of the numbered heading closest to them in size.
            first = seq.index(numbered[0])
            for b in seq[:first]:
                if b.kind == "heading" and b.payload == "size":
                    b.kind = "para"
            level_of: dict[int, int] = {}
            for b in numbered:
                level_of.setdefault(round(b.size), b.level)
            for b in seq:
                if b.kind == "heading" and b.payload == "size":
                    nearest = min(level_of, key=lambda size: abs(size - round(b.size)))
                    b.level = level_of[nearest]
        else:
            sized = sorted({round(b.size) for b in seq if b.kind == "heading"}, reverse=True)
            for b in seq:
                if b.kind == "heading":
                    b.level = min(2 + sized.index(round(b.size)), 6)
        previous = 1  # the document title
        for b in seq:
            if b.kind == "heading":  # a section may go one level deeper than the one before, never more
                b.level = min(b.level, previous + 1)
                previous = b.level
        for table in self.tables:
            if not any(c.chars for row in table.rows for c in row):
                table.emitted = True
        body = self._emit(seq)
        text = ""
        for i, piece in enumerate(body):
            # List items follow each other directly; a list attribute line such as [lowerroman] needs a
            # blank line before it, or it would be read as part of the paragraph above.
            tight = piece.startswith(self.LIST) and body[i - 1].startswith(self.LIST) and not piece.startswith(self.LIST + "[")
            glue = "" if i == 0 else ("\n" if tight else "\n\n")
            text += glue + piece.removeprefix(self.LIST)
        header = f"= {self.title}\n" + (":toc:\n" if self.has_toc else "")
        if self.pictures_kept:
            reason = (
                "OCR is off" if not self.ocr
                else "OCR is unavailable" if self._reader is None
                else "none is a ruled table OCR can read"
            )
            self.notes.append(f"{self.pictures_kept} picture(s) left as pictures: they have no text layer and {reason}")
        return f"{header}\n{text}\n"
