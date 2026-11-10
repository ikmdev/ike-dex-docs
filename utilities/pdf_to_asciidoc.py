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

from structure_pages import paged_structure_tree

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

# Words that mark a callout, with the AsciiDoc admonition each one becomes.
_ADMONITIONS = {
    "note": "NOTE", "notes": "NOTE", "notice": "NOTE", "tip": "TIP", "hint": "TIP", "important": "IMPORTANT",
    "attention": "IMPORTANT", "warning": "WARNING", "danger": "WARNING", "caution": "CAUTION",
}
# A paragraph that opens with the label and a colon: "Note: ...", "Note Important: ...".
_CALLOUT_RE = re.compile(
    r"^(?:(?:notes?|tip|hint)\s+)?(?P<word>notes?|notice|tip|hint|important|attention|warning|danger|caution)\s*:\s*",
    re.IGNORECASE,
)

# A caption: "Table 6. Title", "Figure 2: Title", "Fig. 3 - Title".
_CAPTION_RE = re.compile(
    r"^(?P<label>(?P<word>Table|Figure|Fig\.|Chart|Exhibit|Diagram)\s+(?P<number>[A-Z]?\d+(?:[.-]\d+)*[A-Za-z]?)"
    r"\s*[.:\u2013\u2014-])\s*(?P<title>\S.*)$",
    re.DOTALL,
)

# The title over a list of contents ("Table of Contents"), and over a list of tables or figures.
_CONTENTS_TITLE_RE = re.compile(r"^(?:table of|list of)\s+contents\s*$|^contents\s*$", re.IGNORECASE)
_LIST_TITLE_RE = re.compile(r"^(?:table of|list of)\s+(?P<what>tables|figures|illustrations)\s*$", re.IGNORECASE)

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
    link: Optional[str] = None  # the address of the link annotation over the character
    link_title: str = ""  # the link annotation's own text (a tooltip), where it has one
    underline: bool = False  # a thin line is drawn right under it
    rot: int = 0  # 90 when the text runs up the page, -90 when it runs down


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
    matrix = raw.get("matrix") or (1, 0, 0, 1, 0, 0)
    rot = (90 if matrix[1] > 0 else -90) if abs(matrix[1]) > abs(matrix[0]) else 0
    return Ch(
        text=_normalize_char(raw["text"]),
        x0=raw["x0"], x1=raw["x1"], top=raw["top"], bottom=raw["bottom"],
        size=raw.get("size") or (raw["bottom"] - raw["top"]),
        bold=bool(_BOLD_RE.search(font)), italic=bool(_ITALIC_RE.search(font)),
        owner=owner, page=page, rot=rot,
    )


def unrotate(chars: list[Ch]) -> list[Ch]:
    """Text that runs up or down the page (a row label turned on its side) as if it ran along a line: the
    characters are placed by their position along the text, so lines are made of them as of any other."""
    if not any(c.rot for c in chars):
        return chars
    out = []
    for c in chars:
        if c.rot == 90:  # reads from the bottom up, the tops of the glyphs towards the left
            out.append(dataclasses.replace(c, x0=-c.bottom, x1=-c.top, top=c.x0, bottom=c.x1, rot=0))
        elif c.rot == -90:  # reads from the top down, the tops of the glyphs towards the right
            out.append(dataclasses.replace(c, x0=c.top, x1=c.bottom, top=-c.x1, bottom=-c.x0, rot=0))
        else:
            out.append(c)
    return out


_LINK_SCHEMES = ("http://", "https://", "mailto:", "ftp://")


def _annotation_text(annotation: dict) -> str:
    """The text a link annotation carries for a viewer's tooltip (its /Contents), or ""."""
    raw = (annotation.get("data") or {}).get("Contents")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-16" if raw[:2] in (b"\xfe\xff", b"\xff\xfe") else "latin-1", errors="replace")
    return " ".join(str(raw).split()) if raw else ""


def mark_links(page: Any, chars: Iterable[Ch]) -> None:
    """Characters under a link annotation to a web or mail address carry it. Links to a file path or a
    bookmark of the author's own machine (a Word document's internal references) go nowhere and are left
    as plain text."""
    areas = [(a["x0"], a["top"], a["x1"], a["bottom"], a["uri"], _annotation_text(a)) for a in (page.annots or [])
             if isinstance(a.get("uri"), str) and a["uri"].lower().startswith(_LINK_SCHEMES)]
    if not areas:
        return
    for ch in chars:
        if not ch.text.strip():
            continue
        cx, cy = (ch.x0 + ch.x1) / 2, (ch.top + ch.bottom) / 2
        for x0, top, x1, bottom, uri, title in areas:
            if x0 - 1 <= cx <= x1 + 1 and top - 1 <= cy <= bottom + 1:
                ch.link, ch.link_title = uri, title
                break


def mask_values(data: bytes, decode: list) -> bytes:
    """Soft-mask bytes as opacity. A mask with /Decode [1 0] is stored inverted; read as stored, a picture
    that is wholly opaque comes out wholly transparent."""
    if len(decode) >= 2 and decode[0] > decode[1]:
        return bytes(255 - b for b in data)
    return bytes(data)


def mark_underlines(page: Any, chars: Iterable[Ch]) -> None:
    """Characters with a thin line drawn right under them are underlined. A rule is not an underline: it
    runs beyond its text (a cell border, a rule under a heading), where an underline starts and ends with
    the words it marks. A linked character is left alone; its link already shows."""
    segments = []
    for kind in ("rects", "lines", "curves"):
        for item in getattr(page, kind):
            height, width = item["bottom"] - item["top"], item["x1"] - item["x0"]
            if height <= 1.5 and width >= 6:
                segments.append(((item["top"] + item["bottom"]) / 2, item["x0"], item["x1"]))
    if not segments:
        return
    visible = [c for c in chars if c.text.strip()]
    for y, x0, x1 in segments:
        under = [c for c in visible if x0 - 1.5 <= (c.x0 + c.x1) / 2 <= x1 + 1.5
                 and c.bottom - 0.35 * c.size <= y <= c.bottom + 0.3 * c.size]
        if not under:
            continue
        top = min(under, key=lambda c: c.top).top
        row = sorted((c for c in under if abs(c.top - top) <= 0.5 * c.size), key=lambda c: c.x0)
        first, last = row[0], row[-1]
        if not any(c.text.isalnum() for c in row):
            continue  # a full stop or an asterisk the line happens to run under
        if abs(first.x0 - x0) > 3 or not -2.5 <= x1 - last.x1 <= 4 or (x1 - x0) > 1.15 * (last.x1 - first.x0) + 4:
            continue
        covered = [c for c in chars if first.x0 - 0.1 <= c.x0 and c.x1 <= last.x1 + 0.1
                   and abs(c.top - first.top) <= 0.5 * c.size and not c.link]
        if not any(c.text.isalnum() for c in covered):
            continue  # all that is left, once the link's own text is set aside, is a full stop
        for c in covered:
            c.underline = True
    # a line drawn in pieces can stop in the middle of a word, and no one underlines half a word
    rows: dict[int, list[Ch]] = collections.defaultdict(list)
    for c in chars:
        if c.text.strip():
            rows[round(c.top / 2)].append(c)
    for row in rows.values():
        row.sort(key=lambda c: c.x0)
        for _ in range(2):
            for a, b in zip(row, row[1:]):
                if b.x0 - a.x1 < 0.2 * a.size and not a.link and not b.link and a.underline != b.underline:
                    a.underline = b.underline = True


def run_mcid(raw: dict, previous: Optional[dict]) -> Optional[int]:
    """The marked-content id of a character. A producer can write a ligature ("ﬁ") and the letters after it
    outside the marked content, so an untagged character that follows a tagged one on the same line,
    close behind it, belongs to the same run."""
    mcid = raw.get("mcid")
    if mcid is not None or previous is None or previous.get("mcid") is None:
        return mcid
    size = raw.get("size") or (raw["bottom"] - raw["top"])
    if abs(raw["top"] - previous["top"]) <= 0.5 * size and -0.5 * size <= raw["x0"] - previous["x1"] <= 0.5 * size:
        return previous["mcid"]
    return None


def _split_columns(members: list[Ch]) -> list[list[Ch]]:
    """Split a line of characters where a wide gap separates side-by-side columns (a form's label and
    value pairs, say), so the columns do not run together into one line of text."""
    groups: list[list[Ch]] = [[]]
    previous: Optional[Ch] = None
    size = max((c.size for c in members if c.text.strip()), default=10)
    for ch in members:
        if previous is not None and ch.text.strip() and previous.text.strip():
            # Where the type size changes a lot (a title and a notice set beside it), the gap is judged in
            # ems of the smaller type: the larger one would make a clear gap look narrow.
            small, large = sorted((previous.size, ch.size))
            em = small if large >= 1.5 * small and small >= 0.4 * size else size
            if ch.x0 - previous.x1 > _COLUMN_GAP * em:
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
            result.append(_line_of(group, top, bottom))
    result.sort(key=lambda ln: (ln.page, ln.top, ln.x0))
    return result


def _line_of(group: list[Ch], top: float, bottom: float) -> Line:
    visible = [c for c in group if c.text.strip()] or group
    size = max(c.size for c in visible)
    main = [c for c in visible if c.size >= 0.9 * size] or visible
    owners = collections.Counter(c.owner for c in visible if c.owner is not None)
    return Line(
        chars=group, top=top, bottom=bottom,
        x0=min(c.x0 for c in visible), x1=max(c.x1 for c in visible),
        size=size, baseline=statistics.median(c.bottom for c in main),
        owner=owners.most_common(1)[0][0] if owners else None,
        page=group[0].page,
    )


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
            elif ch.size >= 0.8 * line.size:
                # Some producers raise a mark without shrinking it, so only the offset says it is one. A
                # glyph in a line of its own size has to sit well off the baseline to count.
                if ch.bottom < line.baseline - 0.3 * line.size:
                    script = "sup"
                elif ch.bottom > line.baseline + 0.3 * line.size:
                    script = "sub"
            units.append((ch.text, (ch.bold, ch.italic, script, ch.link, ch.underline, ch.link_title)))
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
        is_bold, is_italic, script, *rest = style
        link = rest[0] if rest else None
        underline = rest[1] if len(rest) > 1 else False
        title = rest[2] if len(rest) > 2 else ""
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
        if link:
            # A comma or equals sign would start another attribute (quotes stay in the text of a link macro, so
            # they are written as character references); {empty} keeps a dot from reading as a role in Pandoc.
            body = body.replace(",", "&#44;").replace("=", "&#61;")
            if "." in body:
                body = "{empty}" + body
            # link: works after any character; a bare address is only taken as a link after a space
            target = link if link.lower().startswith("mailto:") else "link:" + link
            tip = f',title="{title.replace(chr(34), chr(39)).replace(chr(93), chr(41))}"' if title else ""
            scheme = re.search(r"https?://$", pieces[-1]) if pieces else None
            if scheme:  # the address was set in two runs; the link carries its own scheme
                pieces[-1] = pieces[-1][: scheme.start()]
            body = f"{target.replace(' ', '%20')}[{body.replace(']', chr(92) + ']')}{tip}]"
        elif underline:
            body = f"[.underline]##{body}##"
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
    # table / image / callout payload
    payload: Any = None
    # callout: (group id, admonition) of the box this paragraph sits in, and whether its own text said so
    callout: Optional[tuple[int, str]] = None
    callout_inline: bool = False
    callout_word: str = ""  # the label as the document wrote it ("Notice"), for a label the admonition lacks
    # table / image: the caption written for it (label, anchor id, title)
    caption: Optional[tuple[str, str, str]] = None
    # a title over a list of tables or figures: which ("table" or "figure"); the entries are rebuilt from captions
    listof: str = ""


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
            opened = parse_label(plain(line_units(current[0])).lstrip())
            # the next number in a numbered list starts an item whatever the line above ended with (a web
            # address, say); only a stray label inside a wrapped line needs the full stop before it
            follows = (label is not None and opened is not None and label.kind == opened.kind
                       and label.kind in ("num", "alpha", "ALPHA") and label.value == opened.value + 1)
            starts_item = label is not None and (
                ended or follows or (label.kind == "bullet" and label.raw not in "-–—")
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
class MarginLabel:
    kind: str  # the admonition
    line: Line  # the label alone
    column: float  # where the text beside it starts
    word: str = ""  # as written


def _lead_word(line: Line) -> tuple[str, list[Ch]]:
    """The first word of a line, as the text and the characters that spell it."""
    chars: list[Ch] = []
    for ch in line.chars:
        if not ch.text.strip():
            if chars:
                break
            continue
        if chars and ch.x0 - chars[-1].x1 > 0.3 * line.size:
            break
        chars.append(ch)
    return "".join(c.text for c in chars), chars


def _text_x0(line: Line) -> float:
    """Where a line's text starts: past a margin label at its head, which hangs out of the text column and
    would otherwise stand for the column's edge (two callouts a few lines apart would hide each other)."""
    word, spelled = _lead_word(line)
    if word and not word.endswith(":") and word.lower() in _ADMONITIONS and all(c.bold for c in spelled):
        rest = [c.x0 for c in line.chars if c.x0 >= spelled[-1].x1 - 0.1 and c.text.strip() and c not in spelled]
        if rest:
            return min(rest)
    return line.x0


def take_margin_labels(chars: list[Ch]) -> tuple[list[Ch], list[MarginLabel]]:
    """Callouts in technical documents carry their label ("Note", "Important") in the margin beside the
    box, so the label shares a line with the middle of the text and splits it (and, being taller, joins the
    lines above and below it into one). A bold label word with text beginning just right of it, on its own
    line or among the lines above or below, is such a label: its characters are taken out and returned
    apart. A label at the text's own indent, or with a colon after it, is part of the paragraph."""
    lines = group_lines(chars)
    labels: list[MarginLabel] = []
    removed: set[int] = set()
    for line in lines:
        word, spelled = _lead_word(line)
        kind = _ADMONITIONS.get(word.lower()) if not word.endswith(":") else None
        if kind is None or sum(1 for c in spelled if c.bold) < 0.9 * len(spelled):
            continue
        right = spelled[-1].x1
        top, bottom = min(c.top for c in spelled), max(c.bottom for c in spelled)
        neighbours = [_text_x0(o) for o in lines if o is not line and o.page == line.page and not o.blank
                      and o.bottom > top - 4 * line.size and o.top < bottom + 4 * line.size]
        rest = [c for c in line.chars if c.x0 >= right - 0.1 and c not in spelled and c.text.strip()]
        if rest:
            column = min(c.x0 for c in rest)
            # a label that opens the text's own indent is a word of the paragraph; one that hangs out
            # into the margin, with the text clear of it, is the callout's label
            beside = 0.35 * line.size <= column - right <= 4 * line.size \
                and bool(neighbours) and spelled[0].x0 < min(neighbours) - 0.5 * line.size
        else:
            near = [x for x in neighbours if x >= right - 0.1 and x - right <= 4 * line.size]
            column, beside = (min(near), True) if near else (0.0, False)
        if beside:
            labels.append(MarginLabel(kind, _line_of(spelled, top, bottom), column, word))
            removed.update(id(c) for c in spelled)
    return [c for c in chars if id(c) not in removed], labels


def assign_callouts(blocks: list[Block], labels: list[MarginLabel], horizontal: Sequence[tuple[float, float, float]],
                    first_id: int) -> list[Block]:
    """Mark the paragraphs a margin label belongs to. The box is what lies between the rules above and
    below the label, when the page draws them; else the paragraph beside the label. A label with nothing
    beside it stays in the text as a paragraph, so no text is lost."""
    extra: list[Block] = []
    for number, label in enumerate(labels):
        line = label.line
        centre = (line.top + line.bottom) / 2
        above = [y for y, a, b in horizontal if y <= line.top + 0.3 * line.size and line.top - y <= 24 * line.size
                 and a <= label.column + 6 and b >= label.column + 12 * line.size]
        below = [y for y, a, b in horizontal if y >= line.bottom - 0.3 * line.size and y - line.bottom <= 24 * line.size
                 and a <= label.column + 6 and b >= label.column + 12 * line.size]
        members: list[Block] = []
        if above and below:
            top, bottom = max(above), min(below)
            members = [b for b in blocks if b.kind == "para" and b.page == line.page and b.callout is None
                       and b.x0 >= label.column - 6 and top - 1 <= (b.top + b.bottom) / 2 <= bottom + 1]
        if not members:
            near = [b for b in blocks if b.kind == "para" and b.page == line.page and b.callout is None
                    and b.x0 >= label.column - 6
                    and b.top - 0.5 * line.size <= centre <= b.bottom + 0.5 * line.size]
            members = near[:1]
        if not members:
            extra.append(_lines_to_block([line]))
            continue
        for member in members:
            member.callout = (first_id + number, label.kind)
            member.callout_word = label.word
    return extra


def callout_in_text(block: Block) -> None:
    """A paragraph that opens with "Note:" (or Important, Warning, ...) is a callout: the label becomes the
    admonition and leaves the text."""
    match = _CALLOUT_RE.match(plain(block.units))
    if not match:
        return
    seen = 0
    for cut, (text, _) in enumerate(block.units):
        seen += len(text)
        if seen >= match.end():
            break
    rest = block.units[cut + 1:]
    if not rest:
        return  # the label stands alone: leave it as text rather than write an empty callout
    block.units = rest
    block.callout = (block.callout[0] if block.callout else -1, _ADMONITIONS[match.group("word").lower()])
    block.callout_inline = True
    block.callout_word = match.group("word")


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
    tag_colspan: int = 1  # spans stated by the tags (a Chrome export has them; Word and Prince do not)
    tag_rowspan: int = 1
    pictures: list = dataclasses.field(default_factory=list)  # picture blocks drawn inside the cell


@dataclasses.dataclass
class Table:
    rows: list[list[Cell]]
    pages: set[int]
    first_page: int
    top: float = 0.0
    emitted: bool = False
    legend: bool = False  # found beside a picture from its text alone, no ruling lines


def text_grid(chars: list[Ch], page: int) -> Optional[Table]:
    """A table drawn with no lines, read from the text alone: words fall in columns that are separated by
    gutters no row crosses, and in rows that sit one under another. It takes three rows and two columns, and
    most rows must reach into two columns, which running text never does: its words are a space apart
    everywhere, so it is one column. Returns None for anything else."""
    visible = [c for c in chars if c.text.strip()]
    if len(visible) < 6:
        return None
    size = statistics.median(c.size for c in visible)
    # columns: the x ranges that hold text, apart where the gap is wider than a word space can be
    spans = sorted((c.x0, c.x1) for c in visible)
    columns: list[list[float]] = [[spans[0][0], spans[0][1]]]
    for x0, x1 in spans[1:]:
        if x0 - columns[-1][1] > 1.0 * size:
            columns.append([x0, x1])
        else:
            columns[-1][1] = max(columns[-1][1], x1)
    if len(columns) < 2:
        return None
    # rows: lines whose vertical middles are close are one row
    lines = sorted((ln for ln in group_lines(visible) if not ln.blank), key=lambda ln: (ln.top + ln.bottom) / 2)
    rows: list[list[Line]] = []
    for line in lines:
        middle = (line.top + line.bottom) / 2
        last = rows[-1][0] if rows else None
        if last is not None and abs(middle - (last.top + last.bottom) / 2) <= 0.5 * size:
            rows[-1].append(line)
        else:
            rows.append([line])
    if len(rows) < 3:
        return None

    def column_of(ch: Ch) -> int:
        middle = (ch.x0 + ch.x1) / 2
        return next(i for i, (a, b) in enumerate(columns) if a - 0.1 <= middle <= b + 0.1)

    cells: list[list[list[Ch]]] = []
    for row in rows:
        entry: list[list[Ch]] = [[] for _ in columns]
        for line in row:
            for ch in line.chars:
                if ch.text.strip():
                    entry[column_of(ch)].append(ch)
        cells.append(entry)
    reaching = sum(1 for entry in cells if sum(1 for cell in entry if cell) >= 2)
    if reaching < 0.7 * len(rows) or any(sum(1 for entry in cells if entry[i]) < 0.5 * len(rows) for i in range(len(columns))):
        return None
    edges = [columns[0][0] - 6] + [(columns[i][1] + columns[i + 1][0]) / 2 for i in range(len(columns) - 1)] + [columns[-1][1] + 6]
    tops = [min(c.top for cell in entry for c in cell) - 1 for entry in cells]
    bottoms = [t for t in tops[1:]] + [max(c.bottom for cell in cells[-1] for c in cell) + 1]
    table_rows: list[list[Cell]] = []
    for r, entry in enumerate(cells):
        table_rows.append([
            Cell(header=r == 0 and bool(cell) and all(c.bold for c in cell), chars=cell,
                 rect=(edges[i], tops[r], edges[i + 1], bottoms[r]), col=i, page=page)
            for i, cell in enumerate(entry)
        ])
    first = table_rows[0]
    for cell in first:  # a header row is all bold or none is
        cell.header = all(c.header for c in first)
    table = Table(rows=table_rows, pages={page}, first_page=page, legend=True)
    table.top = tops[0]
    return table


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


def vector_regions(page: Any) -> list[tuple[float, float, float, float]]:
    """Areas of a page drawn with curves: logos, symbols, icons and diagrams that are paths, not images.
    Curves (and the filled shapes beside them) are grouped where they lie within a few points of each
    other. Borders and rules are thin and ignored; a group of rectangles alone is a table or a text
    box, not a picture, and a group needs several curves to be one."""
    objects = []
    for kind in ("curves", "rects"):
        for item in getattr(page, kind):
            width, height = item["x1"] - item["x0"], item["bottom"] - item["top"]
            if min(width, height) <= 2.0 or width * height > 0.5 * page.width * page.height:
                continue
            objects.append((kind, item["x0"], item["top"], item["x1"], item["bottom"]))
    if len(objects) > 600:
        objects = [o for o in objects if o[0] == "curves"]
    groups: list[list[tuple]] = []
    for item in objects:
        merged = [item]
        for group in groups[:]:
            if any(not (item[3] + 6 < m[1] or m[3] + 6 < item[1] or item[4] + 6 < m[2] or m[4] + 6 < item[2])
                   for m in group):
                merged += group
                groups.remove(group)
        groups.append(merged)
    found = []
    for group in groups:
        curves = sum(1 for m in group if m[0] == "curves")
        x0, top = max(min(m[1] for m in group), 0.0), max(min(m[2] for m in group), 0.0)
        x1, bottom = min(max(m[3] for m in group), page.width), min(max(m[4] for m in group), page.height)
        if curves >= 2 and 2 * curves >= len(group) and len(group) <= 80 and x1 - x0 >= 10 and bottom - top >= 10:
            found.append((x0, top, x1, bottom))
    return found


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


def _columns_by_position(table: Table) -> bool:
    """Fallback when the borders do not explain the spans: each cell goes in the column it starts in, found from
    where cells (their boxes, or their text) start across the table. A row without a first cell (a cell above
    spans down into it) gets an empty one, and a gap in a row is a cell stretched over it. False when the cells
    do not fall into a few clear columns."""
    def left(cell: Cell) -> Optional[float]:
        if cell.rect:
            return cell.rect[0]
        visible = [c.x0 for c in cell.chars if c.text.strip()]
        return min(visible) if visible else None

    starts = [x for row in table.rows for c in row for x in [left(c)] if x is not None]
    if not starts:
        return False
    edges = _cluster(starts, 4.0)
    if len(edges) < 2 or len(edges) > 40:
        return False
    for row in table.rows:
        columns = []
        for cell in row:
            x = left(cell)
            columns.append(None if x is None else _nearest(edges, x))
        if any(c is None for c in columns) or columns != sorted(columns) or len(set(columns)) != len(columns):
            return False  # a cell with no place, or two in one column: the layout is not a grid of starts
    for row in table.rows:
        columns = [_nearest(edges, left(c)) for c in row]  # type: ignore[arg-type]
        placed: list[Cell] = []
        if columns[0] > 0:
            blank = Cell(header=row[0].header, chars=[], page=row[0].page, colspan=columns[0])
            placed.append(blank)
        for k, cell in enumerate(row):
            cell.rowspan = 1
            following = columns[k + 1] if k + 1 < len(row) else len(edges)
            cell.colspan = max(following - columns[k], 1)
            placed.append(cell)
        row[:] = placed
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


def _grid_from_tags(table: Table) -> bool:
    """Place cells by the spans the tags state. False when none is stated or the rows do not line up."""
    if not any(c.tag_colspan > 1 or c.tag_rowspan > 1 for row in table.rows for c in row):
        return False
    occupied: set[tuple[int, int]] = set()
    widths = []
    for r, row in enumerate(table.rows):
        cursor = 0
        for cell in row:
            while (r, cursor) in occupied:
                cursor += 1
            cell.col, cell.colspan, cell.rowspan = cursor, cell.tag_colspan, cell.tag_rowspan
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied.add((r + dr, cursor + dc))
            cursor += cell.colspan
        while (r, cursor) in occupied:
            cursor += 1
        widths.append(cursor)
    ncols = max(widths, default=0)
    last = len(table.rows)
    return (ncols > 0 and all(w == ncols for w in widths)
            and all(r < last for r, _ in occupied))  # a row span must not run past the last row


def _cell_text(cell: Cell) -> str:
    return re.sub(r"\s+", "", "".join(c.text for c in cell.chars))


def _header_row_count(table: Table) -> int:
    """Rows of the table's header: the leading rows made only of header cells. When that run is the same
    rows twice over (a part joined to the table above with its repeated header), one copy is the header."""
    count = 0
    for row in table.rows:
        if row and all(c.header and c.chars for c in row):
            count += 1
        else:
            break
    texts = [[_cell_text(c) for c in row] for row in table.rows[:count]]
    for size in range(1, count // 2 + 1):
        if count % size == 0 and texts == texts[:size] * (count // size):
            return size
    return count


def _drop_repeated_headers(table: Table) -> int:
    """Remove header rows that repeat the table's own header further down: a PDF repeats it at the top of
    every page, and the rendered AsciiDoc does the same, so it is written once. Returns rows dropped."""
    count = _header_row_count(table)
    if not count:
        return 0
    key = [[_cell_text(c) for c in row] for row in table.rows[:count]]
    dropped, i = 0, count
    while i + count <= len(table.rows):
        block = table.rows[i:i + count]
        if all(row and all(c.header for c in row) for row in block) and [[_cell_text(c) for c in r] for r in block] == key:
            del table.rows[i:i + count]
            dropped += count
        else:
            i += 1
    return dropped


def _infer_header(table: Table) -> int:
    """Header rows of a table of numbers that nothing marks as a header (no header tag, no bold, no repeat): the
    rows at the top with not a figure in them, over rows that are mostly figures, where the first of those rows
    is nearly all figures. A table of names and values has half its cells as figures at most, so is left. Marks the
    cells and returns the rows."""
    rows = [r for r in table.rows if r]
    if len(rows) < 3 or any(c.header for c in rows[0]):
        return 0

    def figures(row: list[Cell]) -> tuple[int, int]:
        texts = [_cell_text(c) for c in row if c.chars]
        return sum(1 for t in texts if re.search(r"\d", t)), len(texts)

    count = 0
    while count < min(3, len(rows) - 2) and figures(rows[count])[0] == 0 and figures(rows[count])[1] > 0:
        count += 1
    if not count:
        return 0
    body = rows[count:]
    with_figures = sum(1 for r in body if figures(r)[0])
    first_figs, first_cells = figures(body[0])
    if with_figures < 0.6 * len(body) or not first_cells or first_figs < 0.6 * first_cells:
        return 0
    for row in rows[:count]:
        for cell in row:
            cell.header = True
    return count


def _repeats_first_row(first: Table, second: Table) -> bool:
    """Does ``second`` open with the row ``first`` opens with? A row set again at the top of the next page is a
    header, however it is styled (the tags of some producers call it a plain row, in regular type)."""
    a, b = [_cell_text(c) for c in first.rows[0]], [_cell_text(c) for c in second.rows[0]]
    return a == b and sum(1 for t in a if t) >= 2


def group_lines_text(cell: Cell) -> list[Unit]:
    """The text of a cell as units, in reading order."""
    return join_units([ln for ln in group_lines(unrotate(cell.chars)) if not ln.blank])


def _fold_rows_in_the_same_box(table: Table) -> int:
    """Some producers tag the first line of a cell as a row of its own. Its box on the page is the very box of a
    cell in the row beside it, so the line is that cell's text. Returns the rows folded away."""
    folded, i = 0, 0
    while i < len(table.rows):
        row = table.rows[i]
        boxed = bool(row) and all(c.rect for c in row)
        home = None
        if boxed:
            for j in (i + 1, i - 1):
                if 0 <= j < len(table.rows) and table.rows[j] is not row:
                    others = table.rows[j]
                    matches = [next((o for o in others if o.rect and o.page == c.page
                                     and all(abs(a - b) <= _EDGE_TOL for a, b in zip(o.rect, c.rect))), None)
                               for c in row]
                    if all(matches) and len(row) < len(others):
                        home = matches
                        break
        if home:
            for cell, target in zip(row, home):
                target.chars = cell.chars + target.chars
            del table.rows[i]
            folded += 1
        else:
            i += 1
    return folded


def _x_edges(table: Table) -> list[float]:
    return _cluster(x for row in table.rows for c in row if c.rect for x in (c.rect[0], c.rect[2]))


def _continues(first: Table, second: Table, height: float) -> bool:
    """Is ``second`` the rest of ``first`` on the next page? It has the same columns, and either starts with the
    header ``first`` starts with, or picks up where ``first`` stopped, at the foot of one page and the top of the next."""
    if second.first_page != max(first.pages) + 1 or not first.rows or not second.rows:
        return False
    a, b = _x_edges(first), _x_edges(second)
    # compared from each table's left edge: facing pages have mirrored margins
    if not a or len(a) != len(b) or any(abs((x - a[0]) - (y - b[0])) > 3.0 for x, y in zip(a, b)):
        return False
    count = _header_row_count(first)
    if count and [[_cell_text(c) for c in r] for r in first.rows[:count]] == [[_cell_text(c) for c in r] for r in second.rows[:count]]:
        return True
    if _repeats_first_row(first, second):
        return True
    ends = [c.rect[3] for row in first.rows for c in row if c.rect and c.page == max(first.pages)]
    starts = [c.rect[1] for row in second.rows for c in row if c.rect and c.page == second.first_page]
    return bool(ends and starts) and max(ends) > 0.75 * height and min(starts) < 0.25 * height


# --------------------------------------------------------------------------
# Converter
# --------------------------------------------------------------------------


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "document"


def _span(value: Any) -> int:
    return value if isinstance(value, int) and value > 1 else 1


def _is_data_table(rows: list[dict]) -> bool:
    """A tagged Table that holds data. A table of single cells is a layout box, unless it has header cells
    and more than one row: a one-column table with its header on top (a browser export has many)."""
    if any(len(r.get("children", [])) > 1 for r in rows):
        return True
    return len(rows) >= 2 and any(c["type"] == "TH" for r in rows for c in r.get("children", []))


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
        self._title_given = bool(title)
        self.title = title or self.path.stem.replace("_", " ")
        self.images_dir = Path(images_dir) if images_dir else None
        self.image_prefix = image_ref_prefix or (self.images_dir.name if self.images_dir else "images")
        self.ignore = [re.compile(p, re.IGNORECASE) for p in ignore]
        self.min_image_size = min_image_size
        self.slug = _slugify(self.path.stem)
        self.pdf = pdfplumber.open(str(self.path))
        if not title:
            self.title = self._metadata_title() or self.title
        self.images_written = 0
        self._image_repeats: collections.Counter[tuple] = collections.Counter(
            self._image_key(image) for page in self.pdf.pages for image in page.images
        )
        self._vector_regions: dict[int, list[tuple[float, float, float, float]]] = {
            number: vector_regions(page) for number, page in enumerate(self.pdf.pages, 1)
        }
        # a drawing in the same place on several pages is the running header or footer
        self._vector_repeats: collections.Counter[tuple] = collections.Counter(
            tuple(round(v) for v in box) for boxes in self._vector_regions.values() for box in boxes
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
        self._callouts = 0  # callout boxes found so far, for their group ids
        self._captioned: set[str] = set()  # kinds of caption written as block titles

    def _metadata_title(self) -> str:
        """The PDF's own /Title when it is a title and not a leftover (a file name, an editor's default)."""
        value = str((self.pdf.metadata or {}).get("Title") or "").strip()
        if (not value or value.lower().startswith(("microsoft word", "untitled", "document"))
                or value.lower().endswith((".pdf", ".docx", ".doc", ".indd"))
                or re.sub(r"[\W_]+", "", value).lower() == re.sub(r"[\W_]+", "", self.path.stem).lower()):
            return ""  # empty, an editor's default, or the file's own name
        return re.sub(r"\s+", " ", value)

    # -- availability ------------------------------------------------------------

    @classmethod
    def usable(cls, path: Union[str, Path]) -> bool:
        """True when the PDF carries a structure tree with marked content on its pages."""
        import pdfplumber

        try:
            with pdfplumber.open(str(path)) as pdf:
                if not paged_structure_tree(pdf):
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
            if node.get("mcid_pages"):  # the tree states each id's page
                return [(p or page, m) for p, m in node["mcid_pages"]]
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
                if rows and _is_data_table(rows):
                    index = len(self.tables)
                    table_rows: list[list[Cell]] = []
                    for r, row in enumerate(rows):
                        cells = []
                        for c, cell in enumerate(row.get("children", [])):
                            keys = leaves(cell, page)
                            for p, mcid, _ in keys:
                                self.cell_of[(p, mcid)] = (index, r, c)
                            attrs = cell.get("attributes") or {}
                            cells.append(
                                Cell(header=cell["type"] == "TH", chars=[],
                                     page=(keys[0][0] if keys else (_first_page(cell) or page or 0)),
                                     tag_colspan=_span(attrs.get("ColSpan")),
                                     tag_rowspan=_span(attrs.get("RowSpan")))
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

        for root in paged_structure_tree(self.pdf):
            visit(root, None)

    # -- page content ------------------------------------------------------------

    def _skip(self, line_text: str) -> bool:
        return any(p.search(line_text) for p in self.ignore)

    def _page_chars(self, page: Any, number: int) -> tuple[list[Ch], list[Ch]]:
        """(flow characters, table-owned characters) of a page.

        Characters inside tagged table cells are filed under their cell; untagged characters are
        page furniture and dropped.
        """
        flow, table_owned, made = [], [], []
        previous: Optional[dict] = None
        for raw in page.chars:
            mcid = run_mcid(raw, previous)
            previous = dict(raw, mcid=mcid) if mcid is not None else None
            if mcid is None:
                continue
            owner = self.owner.get((number, mcid))
            ch = make_char(raw, owner, number)
            made.append(ch)
            key = self.cell_of.get((number, mcid))
            if key is not None:
                t, r, c = key
                self.tables[t].rows[r][c].chars.append(ch)
                self.tables[t].pages.add(number)
            elif owner in self.table_owned:
                table_owned.append(ch)
            else:
                flow.append(ch)
        mark_links(page, made)
        mark_underlines(page, made)
        return flow, table_owned

    def _release_misfiled(self, number: int) -> list[Ch]:
        """A producer can tag the text of a header it repeats on the next page as part of a cell above (the
        last row of the page before). Text on a later page than its cell that lies outside the cell's column
        is not that cell's continuation: it is released, to be filed by the page's borders."""
        released: list[Ch] = []
        for table in self.tables:
            for row in table.rows:
                for cell in row:
                    if cell.page >= number or cell.rect is None:
                        continue
                    outside = {id(ch) for ch in cell.chars
                               if ch.page == number and not cell.rect[0] - 3 <= (ch.x0 + ch.x1) / 2 <= cell.rect[2] + 3}
                    if outside:
                        released += [ch for ch in cell.chars if id(ch) in outside]
                        cell.chars = [ch for ch in cell.chars if id(ch) not in outside]
        return released

    def _release_megacells(self, number: int, borders: "PageBorders") -> list[Ch]:
        """A tagged "cell" that holds the text of a whole grid (a producer that tags a table as one cell and one
        run of text) is not a cell: the page's borders show cells, and the text of four or more of them in one
        is released, to be filed by the borders like any text the tags leave out of the table."""
        released: list[Ch] = []
        for table in self.tables:
            for row in table.rows:
                for cell in row:
                    if cell.page != number or cell.rect is not None:
                        continue
                    boxes = {tuple(round(v) for v in box) for ch in cell.chars if ch.text.strip()
                             for box in [borders.around([ch])] if box is not None}
                    if len(boxes) >= 4:
                        released += cell.chars
                        cell.chars = []
            table.rows = [r for r in table.rows
                          if any(c.chars for c in r) or all(c.page != number for c in r)]
        return released

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
            if not candidates:  # a table with pages before and after has this one in between
                candidates = [t for t in self.tables if min(t.pages, default=number) < number < max(t.pages, default=number)]
            if not candidates:
                for cell in cells:
                    rest.extend(cell.chars)
                continue
            table = min(candidates, key=lambda t: span(t, cells[0].rect))
            # Text the tags leave out of a cell it sits in (some producers tag the first line of a cell apart)
            # belongs to that cell: the page's borders give it the same box.
            homeless = []
            for cell in cells:
                home = next((c for row in table.rows for c in row if c.page == number and c.rect and cell.rect
                             and all(abs(a - b) <= _EDGE_TOL for a, b in zip(c.rect, cell.rect))), None)
                if home is None:
                    homeless.append(cell)
                else:
                    home.chars = cell.chars + home.chars
            cells = homeless
            if not cells:
                continue

            def row_key(row: list[Cell]) -> tuple[int, float]:
                tops = [c.top for cell in row for c in cell.chars if c.text.strip()]
                return (row[0].page if row else 0, min(tops)) if tops else (0, -1.0)

            # cells whose row is already there (a header row with one cell missing) go into that row
            sibling = next((row for row in table.rows if row and row[0].page == number
                            and any(c.rect and abs(c.rect[1] - cells[0].rect[1]) <= _EDGE_TOL for c in row)), None)
            if sibling is not None:
                sibling.extend(cells)
                sibling.sort(key=lambda c: c.rect[0] if c.rect else 1e9)
                table.pages.add(number)
                continue
            where = len(table.rows)
            for i, row in enumerate(table.rows):
                key = row_key(row)
                if key[1] >= 0 and key > (number, top):
                    where = i
                    break
            table.rows.insert(where, cells)
            table.pages.add(number)
        return rest

    @staticmethod
    def _image_key(image: dict) -> tuple:
        return tuple(round(image[k]) for k in ("x0", "top", "x1", "bottom"))

    def _is_furniture_image(self, image: dict, page: Any) -> bool:
        """A running header or footer picture: the same picture in the same place on at least half the pages
        (never fewer than two). That it sits in the margin is not enough: the letterhead on page 1 only is
        part of the document, not of its page furniture. Tagged content pictures are always kept."""
        if not self.layout and image.get("mcid") is not None:
            return False
        needed = max(2, -(-len(self.pdf.pages) // 2))
        return self._image_repeats[self._image_key(image)] >= needed

    @staticmethod
    def _has_prose(page: Any, box: tuple[float, float, float, float]) -> bool:
        """Does a line of running text (six words or more) lie inside the area? Labels on a diagram are
        shorter."""
        text = page.crop(box, strict=False).extract_text() or ""
        return any(len(line.split()) >= 6 for line in text.split("\n"))

    def _too_small(self, width: float, height: float) -> bool:
        """A picture too small to be content: a bullet, a spacer, or a rule (a hairline wider than it is tall).
        A short side under the minimum is not enough: a 30 x 15 pt "Rx only" mark is a symbol."""
        short, long = min(width, height), max(width, height)
        if short >= self.min_image_size:
            return False
        return short < 4 or width * height < 150 or long > 4 * short

    def _page_images(self, page: Any, number: int) -> list[Block]:
        found = []
        for image in page.images:
            width, height = image["x1"] - image["x0"], image["bottom"] - image["top"]
            if self._too_small(width, height) or self._is_furniture_image(image, page):
                continue
            found.append(
                Block(kind="image", page=number, top=image["top"], bottom=image["bottom"],
                      x0=image["x0"], x1=image["x1"],
                      payload=(page, (image["x0"], image["top"], image["x1"], image["bottom"]),
                               image))
            )
        covered = [b.payload[1] for b in found]
        for box in self._vector_regions.get(number, []):
            if self._vector_repeats[tuple(round(v) for v in box)] >= 3:
                continue
            if any(c[0] - 2 <= box[0] and c[1] - 2 <= box[1] and c[2] + 2 >= box[2] and c[3] + 2 >= box[3] for c in covered):
                continue  # drawn over a picture already kept
            if self._has_prose(page, box):
                continue  # a bar or box with sentences in it (an icon beside a banner), not a drawing
            x0, top, x1, bottom = box
            found.append(Block(kind="image", page=number, top=top, bottom=bottom, x0=x0, x1=x1,
                               payload=(page, (x0, top, x1, bottom), {})))
        return found

    @staticmethod
    def _take_picture_text(flow: list[Ch], images: list[Block], page: Any, number: int = 0) -> tuple[list[Ch], list[Table]]:
        """Labels drawn on or beside a diagram (a legend, call-out numbers, part names) are part of the
        picture, not body text: they would otherwise land in the flow around it and between it and its
        caption. Short text wholly inside a picture, or short lines level with a tall picture and just
        beside it, are taken out of the flow and kept as the picture's description. Text beside it that is
        laid out as a grid (a legend of numbers and names) is a table, and is returned as one."""
        legends: list[Table] = []
        for image in images:
            x0, top, x1, bottom = image.payload[1]
            if (x1 - x0) * (bottom - top) > 0.5 * page.width * page.height:
                continue
            inside = [c for c in flow if c.x0 >= x0 - 1 and c.x1 <= x1 + 1 and c.top >= top - 1 and c.bottom <= bottom + 1]
            lines = [ln for ln in group_lines(inside) if not ln.blank]
            words = sum(len(_line_text(ln).split()) for ln in lines)
            if lines and words <= 80 and max(len(_line_text(ln).split()) for ln in lines) <= 14:
                taken = {id(c) for c in inside}  # prose over a background picture is text, and is left
                flow = [c for c in flow if id(c) not in taken]
                image.units = join_units(lines)
            if bottom - top < 70:
                continue
            reach = 250
            beside = [c for c in flow if top <= (c.top + c.bottom) / 2 <= bottom
                      and (x1 - 1 <= c.x0 <= x1 + reach or x0 - reach <= c.x1 <= x0 + 1)]
            grid = text_grid(beside, number)
            if grid is not None:
                taken = {id(c) for c in beside}
                flow = [c for c in flow if id(c) not in taken]
                grid.top = image.top + 0.5  # read after the picture, not before it
                legends.append(grid)
                continue
            near = [ln for ln in group_lines(beside) if not ln.blank]
            if near and sum(len(_line_text(ln).split()) for ln in near) <= 30 \
                    and max(len(_line_text(ln).split()) for ln in near) <= 8:
                taken = {id(c) for c in beside}
                flow = [c for c in flow if id(c) not in taken]
                image.units = _collapse_spaces(image.units + ([(" ", None)] if image.units else []) + join_units(near))
        return flow, legends

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
            data = mask_values(data, [resolve1(v) for v in (resolve1(soft.attrs.get("Decode")) or [])])
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

    def _title_from_the_first_page(self, seq: list[Block]) -> None:
        """When the PDF names no title (its own is empty, a default, or the file's name), the title is the
        centred heading that opens page 1: it leaves the body and becomes the AsciiDoc title."""
        if self.title != self.path.stem.replace("_", " ") or self._title_given:
            return
        for block in seq:
            if block.page != 1 or block.kind == "image":
                continue
            if block.kind != "center":
                return  # something other than a centred heading comes first
            text = " ".join(plain(u).strip() for u in self._split_center(block)).strip()
            if 0 < len(text) <= 160 and len(block.lines) <= 4:
                self.title = text
                seq.remove(block)
            return

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

    def _layout_tables(self, page: Any, number: int, chars: list[Ch], min_filled: int = 0) -> tuple[list[Table], list[Ch]]:
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
            claimed: set[int] = set()
            for ch in chars:
                if id(ch) in taken:
                    continue
                cx, cy = (ch.x0 + ch.x1) / 2, (ch.top + ch.bottom) / 2
                inside = [c for c in ordered if c.rect[0] <= cx < c.rect[2] and c.rect[1] <= cy < c.rect[3]]
                if inside:
                    min(inside, key=lambda c: (c.rect[2] - c.rect[0]) * (c.rect[3] - c.rect[1])).chars.append(ch)
                    claimed.add(id(ch))
            if sum(1 for c in ordered if any(ch.text.strip() for ch in c.chars)) < min_filled:
                continue  # a box or two round some text, not a table
            taken |= claimed
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
            mark_links(page, chars)
            mark_underlines(page, chars)
            dropped = {
                id(c) for line in group_lines(chars)
                if (re.sub(r"\d+", "#", _line_text(line).lower()), round(line.top / 4)) in furniture
                for c in line.chars
            }
            chars = [c for c in chars if id(c) not in dropped]
            tables, flow = self._layout_tables(page, number, chars)
            images = self._page_images(page, number)
            flow, legends = self._take_picture_text(flow, images, page, number)
            tables += legends
            flow, labels = take_margin_labels(flow)
            lines = [ln for ln in group_lines(flow) if not self._skip(plain(line_units(ln)))]
            wide = [ln.x1 for ln in lines if len(_line_text(ln)) >= 50]
            blocks = flow_blocks(lines, layout=True, right_edge=max(wide, default=0.0), pitch=_line_pitch(lines))
            blocks += assign_callouts(blocks, labels, PageBorders(page).horizontal, self._callouts)
            self._callouts += len(labels)
            items = blocks + images
            for table in tables:
                self.tables.append(table)
                items.append(Block(kind="table", page=number, top=table.top, bottom=table.top,
                                   payload=len(self.tables) - 1))
            items.sort(key=lambda b: b.top)
            seq.extend(items)
        return self._merge_tables(self._merge_pages_layout(seq))

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
            owned = owned + self._release_misfiled(number) + self._release_megacells(number, borders)
            for table in self.tables:  # the boxes of the cells the tags did place, so orphans can join their rows
                for row in table.rows:
                    for cell in row:
                        if cell.page == number and cell.chars and cell.rect is None:
                            cell.rect = borders.around([ch for ch in cell.chars if ch.page == number] or cell.chars)
            flow += self._adopt_orphans(owned, number, borders)
            # a grid the tags do not make a table of (all its text in one cell, or none of it tagged as cells)
            found, flow = self._layout_tables(page, number, flow, min_filled=4)
            self.tables.extend(found)
            images = self._page_images(page, number)
            flow, legends = self._take_picture_text(flow, images, page, number)
            self.tables.extend(legends)
            for table in self.tables:
                for row in table.rows:
                    for cell in row:
                        if cell.page == number and cell.chars and cell.rect is None:
                            cell.rect = borders.around([ch for ch in cell.chars if ch.page == number] or cell.chars)

            flow, labels = take_margin_labels(flow)
            lines = [ln for ln in group_lines(flow) if not self._skip(plain(line_units(ln)))]
            blocks = flow_blocks(lines)
            blocks += assign_callouts(blocks, labels, borders.horizontal, self._callouts)
            self._callouts += len(labels)
            items = blocks + images
            for t, table in enumerate(self.tables):
                if number in table.pages:
                    tops = [c.top for row in table.rows for cell in row if cell.page == number
                            for c in cell.chars if c.text.strip() and c.page == number]
                    if tops:
                        items.append(Block(kind="table", page=number, top=table.top if table.legend else min(tops),
                                           bottom=max(tops), payload=t))
            items.sort(key=lambda b: b.top)
            seq.extend(self._structure_order(items))
        return self._merge_tables(self._merge_pages(seq))

    @staticmethod
    def _structure_order(items: list[Block]) -> list[Block]:
        """Paragraphs set side by side (the two columns of a record, say) come out row by row when ordered by
        position, so a pair is read across and the sentences of a column are split up. The tags give the
        order the author meant: where a run of paragraphs is in columns and not in tag order, it follows the
        tags."""
        def in_columns(run: list[Block]) -> bool:
            return any(a.x1 + 20 <= b.x0 and a.top < b.bottom and b.top < a.bottom for a in run for b in run)

        out: list[Block] = []
        run: list[Block] = []

        def flush() -> None:
            owners = [b.owner for b in run]
            if len(run) > 1 and owners != sorted(owners) and in_columns(run):
                run.sort(key=lambda b: b.owner)
            out.extend(run)
            run.clear()

        for block in items:
            if block.kind == "para" and block.owner is not None:
                run.append(block)
            else:
                flush()
                out.append(block)
        flush()
        return out

    def _merge_tables(self, seq: list[Block]) -> list[Block]:
        """A table that carries on over a page break is one table: the part on the next page, with the
        header the PDF repeats there, joins it. The repeated header is dropped when the table is written."""
        merged: list[Block] = []
        for block in seq:
            previous = merged[-1] if merged else None
            if (block.kind == "table" and previous is not None and previous.kind == "table"
                    and block.payload != previous.payload):
                first, second = self.tables[previous.payload], self.tables[block.payload]
                if _continues(first, second, self.pdf.pages[second.first_page - 1].height):
                    if _repeats_first_row(first, second):  # it is the header, so both copies are one
                        for cell in first.rows[0] + second.rows[0]:
                            cell.header = True
                    shift = _x_edges(first)[0] - _x_edges(second)[0]
                    for row in second.rows:  # into line with the first part, so the columns are one set
                        for cell in row:
                            if cell.rect:
                                cell.rect = (cell.rect[0] + shift, cell.rect[1], cell.rect[2] + shift, cell.rect[3])
                    first.rows += second.rows
                    first.pages |= second.pages
                    second.emitted = True
                    continue
            merged.append(block)
        return merged

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
        # Facing pages mirror their margins, so "indented" is judged against the margin of the page itself.
        page_margin: dict[int, float] = {}
        for b in seq:
            if b.kind == "para" and len(b.lines) >= 2:
                page_margin[b.page] = min(page_margin.get(b.page, b.x0), b.x0)
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
                own = _LIST_TITLE_RE.match(plain(block.units).strip())
                if own:  # a producer may tag the title of a list of tables as one of its entries
                    block.listof = "table" if own.group("what").lower() == "tables" else "figure"
                    out.append(block)
                    continue
                # The entries are dropped (the contents are rebuilt from the headings), and so is the title
                # that stood over them, or it would head an empty list.
                if out and out[-1].kind in ("heading", "para") and len(out[-1].lines) <= 2:
                    title = plain(out[-1].units).strip()
                    listed = _LIST_TITLE_RE.match(title)
                    if _CONTENTS_TITLE_RE.match(title):
                        out.pop()
                    elif listed:  # kept: the list is written again from the captions
                        out[-1].listof = "table" if listed.group("what").lower() == "tables" else "figure"
                continue
            if tag and tag.startswith("H"):
                block.kind = "heading"
                block.level = 1 + int(tag[1:] or 1)
                block.payload = "tag"
                out.append(block)
                continue
            found = headings.classify(block) if block.callout is None else None
            if found:
                kind, text = found
                block.kind, block.level = "heading", headings.level(kind)
                out.append(block)
                continue
            centred = all(abs((ln.x0 + ln.x1) / 2 - page_mid[block.page]) <= 8 for ln in block.lines)
            if (self.layout and not centred and block.callout is None and bold_share(block.units) >= 0.9 and len(block.lines) <= 2
                    and len(plain(block.units)) <= 120 and block.size >= 1.1 * body_size):
                # An untagged heading: bold and larger than the text. Its level follows its size.
                block.kind, block.payload = "heading", "size"
                out.append(block)
                continue
            if centred and block.callout is None and block.x0 - page_margin.get(block.page, left_margin) > 30 and not parse_label(plain(block.units)):
                block.kind = "center"
                out.append(block)
                continue
            callout_in_text(block)
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
        return self._group_callouts(self._absorb_continuations(joined))

    def _absorb_continuations(self, seq: list[Block]) -> list[Block]:
        """A callout opened by its own label ("Notice: ...") goes on over the lines straight under it, in
        the same column and type size, whatever the tags make of them: Chrome tags each line of a wrapped
        sentence apart, so the second line would otherwise be a paragraph on its own. They join its text."""
        out: list[Block] = []
        taken: set[int] = set()
        for i, block in enumerate(seq):
            if id(block) in taken:
                continue
            out.append(block)
            if block.kind != "para" or not block.callout_inline or block.label:
                continue
            last = block
            for later in seq[i + 1:]:
                if (later.kind != "para" or later.callout is not None or later.label or later.page != last.page
                        or abs(later.x0 - block.x0) > 2 or abs(later.size - block.size) > 0.6
                        or not 0 <= later.top - last.lines[-1].baseline <= 0.9 * later.size
                        or bold_share(later.units) >= 0.9):
                    break
                block.lines += later.lines
                block.units = _collapse_spaces(block.units + [(" ", None)] + later.units)  # keeps the label cut off
                block.bottom, block.x1 = later.bottom, max(block.x1, later.x1)
                taken.add(id(later))
                last = later
        return out

    @staticmethod
    def _group_callouts(seq: list[Block]) -> list[Block]:
        """The paragraphs of one callout box become one block that holds them. Where the text says
        "Important:" itself, that is the kind, whatever the margin label beside it said."""
        out: list[Block] = []
        for block in seq:
            if block.kind != "para" or block.callout is None:
                out.append(block)
                continue
            group, kind = block.callout
            previous = out[-1] if out else None
            if previous is not None and previous.kind == "admon" and group >= 0 and previous.payload[2] == group:
                previous.payload[1].append(block)
                previous.bottom, previous.x0 = block.bottom, min(previous.x0, block.x0)
                previous.x1 = max(previous.x1, block.x1)
                if block.callout_inline:
                    previous.payload[0], previous.payload[3] = kind, block.callout_word
                continue
            out.append(Block(kind="admon", page=block.page, top=block.top, bottom=block.bottom, x0=block.x0,
                             x1=block.x1, size=block.size, payload=[kind, [block], group, block.callout_word]))
        return out

    # -- rendering ---------------------------------------------------------------

    def _render_cell(self, cell: Cell) -> tuple[str, bool]:
        """(text, needs AsciiDoc cell style) for one table cell."""
        lines = [ln for ln in group_lines(unrotate(cell.chars)) if not ln.blank]
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
        for picture in cell.pictures:
            parts.append((self._inline_picture(picture), False))
        rich = any(is_bullet for _, is_bullet in parts)
        text = ""
        for i, (piece, is_bullet) in enumerate(parts):
            if i:  # bullets follow each other directly; anything else needs a blank line
                text += "\n" if is_bullet and parts[i - 1][1] else "\n\n"
            text += piece
        return text.replace("|", "\\|"), rich

    @staticmethod
    def _alignment(cell: Cell, rect: Optional[tuple[float, float, float, float]]) -> str:
        """"^" centred, ">" right, "" left, or "?" when the text fills its cell and says nothing about
        alignment (a line with under 8 pt to spare on both sides fits any of them)."""
        lines = [ln for ln in group_lines(cell.chars) if not ln.blank]
        if not lines or rect is None:
            return ""
        left, right = rect[0], rect[2]
        middle = (left + right) / 2
        slack = [(ln, ln.x0 - left, right - ln.x1) for ln in lines]
        evidence = [(ln, a, b) for ln, a, b in slack if a >= 8 or b >= 8]
        if not evidence:
            return "?"
        if all(a >= 8 and b >= 8 and abs((ln.x0 + ln.x1) / 2 - middle) <= 3 for ln, a, b in evidence):
            return "^"
        if all(b <= 4 and a >= 10 for ln, a, b in evidence):
            return ">"
        return ""

    def _join_split_rows(self, table: Table) -> int:
        """A row that a page break cuts in two: the next page starts with the rest of one or more of its cells,
        in their own columns, and the text carries on (it stops short of a full stop, and the rest starts in
        lower case). The rest joins the cells it continues. Returns the rows joined."""
        joined, i = 0, 1
        while i < len(table.rows):
            above, below = table.rows[i - 1], table.rows[i]
            if not (above and below and all(c.rect for c in above + below) and below[0].page == above[0].page + 1
                    and len(below) < len(above)):
                i += 1
                continue
            pairs = []
            for cell in below:
                mid = (cell.rect[0] + cell.rect[2]) / 2
                twin = next((a for a in above if a.rect[0] - 2 <= mid <= a.rect[2] + 2), None)
                pairs.append((twin, cell))
            carries = [t for t, c in pairs if t is not None and plain(group_lines_text(t)).rstrip()[-1:] not in ".?!:;)"
                       and plain(group_lines_text(c)).lstrip()[:1].islower()]
            if all(t is not None for t, _ in pairs) and carries:
                for twin, cell in pairs:
                    twin.chars = twin.chars + cell.chars
                del table.rows[i]
                joined += 1
            else:
                i += 1
        return joined

    def _render_table(self, table: Table, page_width: float) -> str:
        _drop_repeated_headers(table)
        _fold_rows_in_the_same_box(table)
        self._join_split_rows(table)
        note = ""
        stated = any(c.tag_colspan > 1 or c.tag_rowspan > 1 for row in table.rows for c in row)
        saved = [[dataclasses.replace(c, chars=list(c.chars)) for c in row] for row in table.rows]
        if stated and _grid_from_tags(table):
            pass  # the tags say how the cells span
        else:
            if stated:
                table.rows[:] = [[dataclasses.replace(c, chars=list(c.chars)) for c in row] for row in saved]
            if not _assign_grid(table):
                table.rows[:] = saved
                if not _grid_from_tags(table):
                    if _columns_by_position(table):
                        note = "// Cell spans were placed by where cells start; compare with the page.\n"
                    else:
                        _pad_rows(table)
                        note = "// Cell spans could not be recovered from the page borders; row layout may differ.\n"
        _infer_header(table)
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
                if cell.colspan == 1 and cell.col < ncols and cell.chars and align != "?":
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
        # Only the first row can be the AsciiDoc header, and a span cannot cross out of it: a header with a
        # cell spanning down (a two-row header) keeps its header cells as row headers instead.
        if first and all(c.header for c in first) and all(c.rowspan == 1 for c in first):
            attrs.append("options=header")
        else:
            # Without this Asciidoctor takes the first row, which our blank lines set apart, as a header row
            attrs.append("options=noheader")
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
                if align == "?":
                    align = column if cell.colspan == 1 else ""
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

    def _inline_picture(self, block: Block) -> str:
        """A picture inside a table cell, as an inline image."""
        page, bbox, image = block.payload
        ref = self._write_image(block.page, page, bbox, image)
        if ref is None:
            return ""
        alt = f"Picture on page {block.page}"
        if block.units:
            alt += ". Text in the picture: " + plain(block.units)
        alt = alt.replace('"', "'").replace("]", ")").replace("\\", "/").replace("|", "/")
        self.pictures_kept += 1
        return f'image:{ref}["{alt}",width={round((bbox[2] - bbox[0]) * 96 / 72)}]'

    def _pictures_into_cells(self, seq: list[Block]) -> list[Block]:
        """A picture drawn inside a table cell (a symbol in a legend, say) belongs to the cell. The text it
        carries (a "CC" in the symbol) moves from the cell to the picture's description."""
        out: list[Block] = []
        for block in seq:
            if block.kind != "image":
                out.append(block)
                continue
            x0, top, x1, bottom = block.payload[1]
            cx, cy = (x0 + x1) / 2, (top + bottom) / 2
            home = next((c for t in self.tables if block.page in t.pages and any(ch.chars for r in t.rows for ch in r)
                         for row in t.rows for c in row
                         if c.page == block.page and c.rect and c.rect[0] <= cx <= c.rect[2]
                         and c.rect[1] <= cy <= c.rect[3]), None)
            if home is None:
                out.append(block)
                continue
            inside = [c for c in home.chars if x0 - 1 <= (c.x0 + c.x1) / 2 <= x1 + 1 and top - 1 <= (c.top + c.bottom) / 2 <= bottom + 1]
            if inside:
                home.chars = [c for c in home.chars if c not in inside]
                block.units = join_units([ln for ln in group_lines(inside) if not ln.blank])
            home.pictures.append(block)
        return out

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
        alt = f"Picture on page {block.page}"
        if block.units:
            alt += ". Text in the picture: " + plain(block.units)
        alt = alt.replace('"', "'").replace("]", ")").replace("\\", "/")
        return [f'{note}image::{ref}["{alt}",width={width}]']

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
                # Comment lines (an OCR table's notes) stay above the "+": after it, a comment ends the list
                # item's attachment and the "+" that follows the block is printed as text.
                lines = text.split("\n")
                top = 0
                while top < len(lines) - 1 and lines[top].startswith("//"):
                    top += 1
                chunk.extend(lines[:top] + ["+", "\n".join(lines[top:])])
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

            if block.kind == "listof":
                flush()
                levels.clear()
                out.append("\n".join(f"* xref:{slug}[{(label + title).replace(']', chr(92) + ']')}]"
                                    for label, slug, title in block.payload))
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
                text = self._titled(self._render_table(table, self.pdf.pages[block.page - 1].width), block.caption)
            elif block.kind == "admon":
                text = self._render_callout(block)
            else:  # image
                parts = self._render_image(block)
                at = next((k for k, part in enumerate(parts) if "|===" in part), len(parts) - 1)  # not the notes
                parts[at] = self._titled(parts[at], block.caption)
                for part in parts:
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

    def _table_end(self, block: Block) -> tuple[int, float]:
        """The last page a table is on and where it ends there."""
        table = self.tables[block.payload]
        page = max(table.pages)
        bottoms = [c.rect[3] if c.rect else max((ch.bottom for ch in c.chars), default=0.0)
                   for row in table.rows for c in row if c.page == page]
        return page, max(bottoms, default=block.bottom)

    def _attach_captions(self, seq: list[Block]) -> list[Block]:
        """A caption ("Table 6. ...", "Figure 2: ...") next to its table or picture becomes the block title
        of that table or picture, wherever the producer set it: above for tables, below for pictures, most
        often. The title keeps the label ("Table 6. ...") so the numbers stay those of the document, and the
        block gets an anchor for cross references."""
        def reach(block: Block, side: str) -> Optional[tuple[int, float]]:
            """(page, edge) of a table or picture on the given side facing the caption."""
            if side == "after":  # the block sits after the caption: its top edge
                return (block.page, block.top)
            if block.kind == "table":
                return self._table_end(block)
            return (block.page, block.bottom)

        out: list[Block] = []
        taken: set[int] = set()
        consumed: set[int] = set()  # later lines of a caption set in several blocks
        for i, block in enumerate(seq):
            if id(block) in consumed:
                continue
            if block.kind not in ("para", "center") or block.callout is not None or len(block.lines) > 3:
                out.append(block)
                continue
            text = plain(block.units).strip()
            match = _CAPTION_RE.match(text)
            label_units = [st for t, st in block.units[:len(match.group("label").rstrip(".:\u2013\u2014- ")) if match else 0] if st]
            styled = bold_share(block.units) >= 0.9 or all(s is not None and s[1] for _, s in block.units if s) \
                or block.kind == "center" or self.owner_kind.get(block.owner) == "Caption" \
                or (bool(label_units) and (all(st[0] for st in label_units) or all(st[1] for st in label_units)
                                           or all(len(st) > 4 and st[4] for st in label_units)))  # only "Table 1" is set apart
            if not match or not styled:
                out.append(block)
                continue
            first = match.group("word") == "Table"
            # a caption can run to a second line that the tags make a paragraph of its own
            extra: list[Block] = []
            j = i + 1
            while j < len(seq) and seq[j].kind == "para" and seq[j].callout is None and not seq[j].label \
                    and abs(seq[j].x0 - block.x0) <= 2 and abs(seq[j].size - block.size) <= 0.6 \
                    and len(seq[j].lines) <= 2 and not _CAPTION_RE.match(plain(seq[j].units).strip()) \
                    and 0 <= seq[j].top - (extra[-1] if extra else block).lines[-1].baseline <= 0.9 * seq[j].size:
                extra.append(seq[j])
                j += 1
            if extra and not (j < len(seq) and seq[j].kind in ("table", "image")):
                extra, j = [], i + 1  # not followed by a table or picture after all: they are paragraphs
            last = extra[-1] if extra else block
            def clear(j: int, step: int) -> int:
                """Past a legend table: it belongs to the picture, which the caption is for."""
                while 0 <= j < len(seq) and not first and seq[j].kind == "table" and self.tables[seq[j].payload].legend:
                    j += step
                return j

            options = [("after", j), ("before", i - 1)] if first else [("before", clear(i - 1, -1)), ("after", clear(j, 1))]
            partner = None
            for side, j in options:
                if not 0 <= j < len(seq) or j in taken or seq[j].kind not in ("table", "image") \
                        or seq[j].caption is not None:
                    continue
                page, edge = reach(seq[j], side)
                near = (edge - last.bottom) if side == "after" else (block.top - edge)
                height = self.pdf.pages[block.page - 1].height
                # a caption at the foot of a page, its table at the top of the next
                over = side == "after" and page == block.page + 1 and last.bottom > 0.8 * height and edge < 0.2 * height
                if (page == block.page and -4 <= near <= 48) or over:
                    partner = (side, j)
                    break
            if partner is None:
                out.append(block)
                continue
            seen, cut = 0, 0
            for cut, (unit, _) in enumerate(block.units, 1):
                seen += len(unit)
                if seen >= match.start("title"):
                    break
            # A caption set wholly in bold or italic is the title's own style; italic inside it is emphasis
            whole_italic = all(st[1] for _, st in block.units if st)
            title = [(t, (False, bool(st[1]) and not whole_italic, st[2]) if st else None)
                     for t, st in block.units[cut:]]
            for more in extra:  # the caption's later lines
                title += [(" ", None)] + [(t, (False, bool(st[1]) and not whole_italic, st[2]) if st else None)
                                          for t, st in more.units]
                consumed.add(id(more))
            label = f"{match.group('label')} "
            slug = f"{'figure' if match.group('word') in ('Figure', 'Fig.') else match.group('word').lower()}-" \
                   + re.sub(r"[^a-z0-9]+", "-", match.group("number").lower()).strip("-")
            seq[partner[1]].caption = (label, slug, render_units(title, bold=False))
            taken.add(partner[1])
        return out

    @staticmethod
    def _resolve_lists(seq: list[Block]) -> list[Block]:
        """A "Table of Tables" or "Table of Figures" is written again from the captions of the document, as
        cross references, so it stays right whatever the page numbers do. With no caption of its kind to
        list, the title is dropped with its entries."""
        out: list[Block] = []
        for block in seq:
            if not block.listof:
                out.append(block)
                continue
            entries = [b for b in seq if b.caption and b.caption[1].startswith(block.listof + "-")]
            if not entries:
                continue
            out.append(block)
            out.append(Block(kind="listof", page=block.page, top=block.bottom + 0.1, bottom=block.bottom + 0.1,
                             payload=[b.caption for b in entries]))
        return out

    def _titled(self, text: str, caption: Optional[tuple[str, str, str]]) -> str:
        """The block text with its title lines in front of the block itself (after any comment lines). The
        label is part of the title text, so the document turns off Asciidoctor's own numbering."""
        if caption is None:
            return text
        label, slug, title = caption
        self._captioned.add("figure" if slug.startswith("figure-") else "table" if slug.startswith("table-") else "")
        lines = text.split("\n")
        top = 0
        while top < len(lines) - 1 and lines[top].startswith("//"):
            top += 1
        head = [f"[[{slug}]]", f".{label}{title}"]
        return "\n".join(lines[:top] + head + lines[top:])

    def _render_callout(self, block: Block) -> str:
        """A callout as an admonition: one line for a single paragraph, a delimited block for several
        paragraphs or a list. A label the admonition has no name for ("Notice") is kept as its caption."""
        kind, members, _, word = block.payload
        body = self._join(self._emit(members))
        caption = word.capitalize() if word and word.lower() not in ("note", "tip", "important", "warning", "caution") else ""
        style = f'{kind},caption="{caption}"' if caption else kind
        if len(members) == 1 and not members[0].label and "\n" not in body:
            return f"[{style}]\n{body}" if caption else f"{kind}: {body}"
        return f"[{style}]\n====\n{body}\n===="

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
        return block.x0 < levels[0]["label_x"] - _INDENT_TOL  # a table block's x0 is its left edge, set by _align_margins

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

    def _align_margins(self, seq: list[Block]) -> None:
        """Indents are compared across pages, and facing pages have mirrored margins (a list item at 54 pt on
        one page is at 72 pt on the next). When odd and even pages keep to two different left margins, shift
        the blocks of the even pages onto the odd pages' margin."""
        for block in seq:
            if block.kind == "table":
                table = self.tables[block.payload]
                xs = [c.rect[0] for row in table.rows for c in row if c.rect]
                xs = xs or [ch.x0 for row in table.rows for c in row for ch in c.chars if ch.text.strip()]
                block.x0 = min(xs, default=block.x0)
        margins: dict[int, list[float]] = collections.defaultdict(list)  # page parity -> the leftmost text of each page
        for page in sorted({b.page for b in seq}):
            # headings, tables and paragraphs of two lines or more: a one-line callout label sits in the margin
            edges = [b.x0 for b in seq if b.page == page
                     and (b.kind in ("heading", "table") or (b.kind == "para" and len(b.lines) >= 2))]
            if edges:
                margins[page % 2].append(min(edges))
        if len(margins) < 2 or min(len(v) for v in margins.values()) < 2:
            return
        middle = {parity: statistics.median(values) for parity, values in margins.items()}
        steady = all(
            sum(1 for v in values if abs(v - middle[parity]) <= 3) >= 0.6 * len(values)
            for parity, values in margins.items()
        )
        if not steady or abs(middle[0] - middle[1]) < 6:
            return
        shift_even = middle[0] - middle[1]
        held = [m for b in seq if b.kind == "admon" for m in b.payload[1]]  # the paragraphs inside callouts
        for block in [*seq, *held]:
            if block.page % 2:
                continue
            block.x0 -= shift_even
            if block.kind == "table":
                continue
            block.x1 -= shift_even
            if block.label:
                block.label_x -= shift_even
                block.content_x -= shift_even

    def _join(self, body: list[str]) -> str:
        """The written blocks as one text: blank lines between blocks, none between the items of a list."""
        text = ""
        for i, piece in enumerate(body):
            # List items follow each other directly; a list attribute line such as [lowerroman] needs a
            # blank line before it, or it would be read as part of the paragraph above.
            # A list item that follows an item with attached blocks needs a blank line: after an attached
            # paragraph, the next marker would be read as more text of that paragraph and end the list.
            tight = (piece.startswith(self.LIST) and body[i - 1].startswith(self.LIST)
                     and not piece.startswith(self.LIST + "[") and "\n+\n" not in body[i - 1])
            glue = "" if i == 0 else ("\n" if tight else "\n\n")
            text += glue + piece.removeprefix(self.LIST)
        return text

    def _convert(self) -> str:
        self.has_toc = False
        seq = self._pictures_into_cells(self._resolve_lists(self._attach_captions(self._classify(self._sequence()))))
        self._align_margins(seq)
        if self.layout:
            self._cover_title(seq)
        # A single H1 ahead of every other heading, and ahead of any text, is the document title. With text
        # before it (a page banner, say) it is a heading where it stands, and the title comes from the PDF's
        # own metadata: lifting it to the top would pull it away from what is set beside it.
        tagged = [b for b in seq if b.kind == "heading" and b.payload == "tag"]
        if tagged and tagged[0].level == 2 and sum(1 for b in tagged if b.level == 2) == 1 \
                and seq.index(tagged[0]) < next((i for i, b in enumerate(seq) if b.kind == "heading"
                                                  and b is not tagged[0]), len(seq)) \
                and not any(b.kind in ("para", "admon", "table", "center") for b in seq[:seq.index(tagged[0])]):
            self.title = plain(tagged[0].units)
            seq.remove(tagged[0])
            tagged = tagged[1:]
        else:
            self._title_from_the_first_page(seq)
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
        text = self._join(body)
        header = f"= {self.title}\n" + (":toc:\n" if self.has_toc else "")
        header += "".join(f":{kind}-caption!:\n" for kind in ("figure", "table") if kind in self._captioned)
        if self.pictures_kept:
            reason = (
                "OCR is off" if not self.ocr
                else "OCR is unavailable" if self._reader is None
                else "none is a ruled table OCR can read"
            )
            self.notes.append(f"{self.pictures_kept} picture(s) left as pictures: they have no text layer and {reason}")
        return f"{header}\n{text}\n"
