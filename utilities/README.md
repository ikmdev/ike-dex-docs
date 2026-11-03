# utilities

Python helpers for the ingestion workflow. One shared virtual environment
(`utilities/.venv`, git-ignored) serves every script here.

## Setup

Install [Pandoc](https://pandoc.org/installing.html), a system dependency of
`document_to_asciidoc.py` that must be on your `PATH`, then create the environment:

```bash
./utilities/setup.sh            # creates .venv; installs requirements.txt and requirements-ocr.txt
./utilities/setup.sh --no-ocr   # skips the OCR packages (about 160 MB)
```

The environment was built with Python 3.14 (`PYTHON=python3.14 ./utilities/setup.sh`).
`docling` pulls in PyTorch and may lag the newest Python release; if installation fails,
choose an older interpreter and delete `utilities/.venv` first. Without bash (Windows), run
`python -m pip install -r utilities/requirements.txt` (and `-r utilities/requirements-ocr.txt`
for OCR) inside the environment.

`requirements.txt` holds the direct dependencies and `requirements-ocr.txt` the optional OCR
ones. `requirements.lock` is the `pip freeze` of the working environment; regenerate it after
changing dependencies: `utilities/.venv/bin/python -m pip freeze > utilities/requirements.lock`.

## Scripts

| Script | Purpose |
| --- | --- |
| `document_to_asciidoc.py` | Convert a `.pdf` or `.docx` to AsciiDoc. Tagged PDFs are read from their tags, untagged ones from their layout; scans and Word files go through Docling. |
| `pdf_to_asciidoc.py` | The tagged and layout engines: tables with spans and alignment, nested lists, heading levels, inline styles, pictures at stored resolution. |
| `picture_tables.py` | OCR for tables a PDF stores as pictures (RapidOCR, Tesseract and EasyOCR vote). Used by both engines unless `--no-ocr`. |
| `check_fidelity.py` | Compare a converted `.adoc` with its source PDF. Exits 1 when a gate fails. |

```bash
utilities/.venv/bin/python utilities/document_to_asciidoc.py input.pdf -o work/raw/input.adoc
utilities/.venv/bin/python utilities/check_fidelity.py input.pdf work/raw/input.adoc
utilities/.venv/bin/python utilities/check_fidelity.py input.pdf work/raw/input.adoc \
    --rendered rendered.pdf --diff-dir work/diff     # also compare appearance
```

`work/` is git-ignored scratch space; pictures go to `work/raw/images/`. The output is raw
conversion only: continue with `semantic-linebreak` and decomposition as described in the
repository README.

## Engines

`--engine auto` (the default) picks a reader for each PDF:

| Engine | Used when | Reads |
| --- | --- | --- |
| `tagged` | the PDF has a structure tree (Word and Acrobat exports do) | paragraphs, lists, tables and headings from the tags |
| `layout` | it has a text layer but no tags (FOP, print-to-PDF) | the same, guessed from the page layout |
| `docling` | it is a scan, a `.docx`, or you ask for it | Docling, Markdown, Pandoc: slow, flatter output |

Force one with `--engine tagged|layout|docling`; a forced engine that cannot read the file stops
with an error. The summary line names the engine used. Tagged and layout take seconds.

Both engines take bold, italic, superscript and subscript from fonts and baselines, and recover
table spans, column widths and alignment from the drawn cell borders (tags carry no spans).
Running headers, footers and page numbers are dropped (`--ignore REGEX` adds more).

**Tables over a page break** are one AsciiDoc table: a part with the same columns that repeats the
header (or starts at the top of the page where the last ended) joins the one above, and a header row
repeated inside a table is written once, as the first row with `options=header`. The rendered PDF
repeats that header on every page again. A header with a cell spanning down (a two-row header) cannot be
the AsciiDoc header row, so its cells become row headers (`h`). Where the tags state spans (a Chrome
export does) they are used when the page borders do not explain the cells.

**Callouts** (both engines) become admonitions. A bold "Note", "Important", "Warning", "Caution" or "Tip"
set in the margin beside a box (taken out before lines are grouped, since it would join the lines above and
below it) marks the paragraphs between the rules above and below it, or else the paragraph beside it;
a paragraph that opens with "Note:" does too. One paragraph is written `NOTE: text`; several, or a
list, a `[NOTE]` block between `====` lines.

**Captions** ("Table 6. ...", "Figure 2: ...", in bold, italic or a `Caption` tag, within a few
points of a table or picture) become the block title (`.Table 6. Title`) with an anchor
(`[[table-6]]`). A table looks for its caption above first, a picture below. The label stays in the title
text, so the header gets `:table-caption!:` / `:figure-caption!:` and Asciidoctor does not number again.

**Links**: a link annotation to a web or mail address is written as a link macro, with the annotation's own
text as its `title` (a PDF viewer's tooltip) when it has one. Links to a file path or a bookmark of the
author's machine (a Word document's internal references) are left as text. **Underlines**: a thin line right
under a run of words that starts and ends with them (a rule or cell border runs beyond its text) is
`[.underline]##text##`, whole words only; linked text is left to its link. Hover text is only carried where the
PDF has it: a Chrome or Word export drops HTML `title` attributes, so GUDID's underlined labels have none.
Asciidoctor shows a link `title` in HTML and Prince writes it into the PDF link annotation.

**Title and notice** on one line: a gap is judged in ems of the smaller type where the size changes by half
or more, so a large title and a small notice beside it are two lines, and "Notice:" is a callout.
**Lists of tables and figures**: the entries are rebuilt from the captions as cross references
(`xref:table-1[...]`) under the original title, with no page numbers; with no caption of that kind to list
they are dropped with the contents.

**Text on pictures**: labels wholly inside a picture leave the text flow and go into the picture's
description (`Text in the picture: ...`), so they neither run into the body text nor sit between a picture
and its caption. Text level with a tall picture and just beside it is told apart by its layout: words in two
or more columns and three or more rows, with a gutter no row crosses and most rows reaching into two columns
(running text never does), are a table, written after the picture; a few loose labels go into the description.
The text layer is what separates this from a picture of a table: its characters are real text, which a scan's
are not (those go to OCR). Areas drawn with curves (logos, symbols, icons) are written as pictures too; one inside a table
cell goes in the cell as an inline image. A drawing in the same place on three pages is furniture.

**Reading order** (tagged): paragraphs set in columns that position order would read row by row (the two
columns of a record) follow the tags instead.

**Tagged** engine: the tag tree says what is a paragraph, list item, table cell, heading (`H1`-`H6`)
or contents entry (dropped, `:toc:` added).

**Layout** engine: with no tags, it decides from the page.

- **Paragraphs** break when the line distance exceeds the page's measured line pitch by a third, or
  at a change of type size or boldness, a dedent, or a short last line after a full stop. A paragraph
  over a page break is joined when it stops near the foot of the page without one.
- **Tables** are those drawn with ruling lines. A wholly bold cell is a header; a lone box is text.
- **Headings** come from numbering and, if unnumbered, from bold text larger than the body. Large
  text ahead of the first numbered heading (a cover) stays text, except the one largest line on
  page 1, which becomes the document title.
- **Running furniture** is a line that repeats at the same height on half the pages (at least two),
  so a one-page document keeps its header.

**Lists and headings** (both engines): labels `1.`, `1)`, `(1)`, `a.`, `a)`, `(a)`, `A.`, `i.`, `II)`,
`(iv)`, bullet glyphs and dashes. A lone `i`, `v`, `x` is a letter when it continues the alphabet,
else a roman numeral. Lists nest by indentation, and a different label type at the same indent
opens a sub-list. Heading schemes nest in the order they open and return to their own level when
they recur; levels never skip. AsciiDoc writes its own markers, so `a)` comes out as `a.` and all
bullet glyphs become plain bullets.

**Pictures** are written to `images/` as stored in the PDF (JPEG bytes unchanged, other formats as
lossless PNG, soft masks kept as alpha), with a `// REVIEW` comment. A picture the page rotates, mirrors
or stencils is saved as a render of its page area (at least 200 dpi). Logos in the page margin or
repeated on several pages are dropped.

## OCR for picture tables

A PDF may hold a table as a picture, with no text underneath. With OCR on (the default; `--no-ocr`
turns it off), the tagged and layout engines rebuild it as AsciiDoc:

1. **Structure**: OpenCV finds the ruling lines, and a cell merges with its neighbour where the line
   between them is missing (spans). A picture without a clear grid stays a picture.
2. **Text**: each cell is cut into lines, and RapidOCR, Tesseract and EasyOCR read the same line images.
3. **Vote**: the most common reading wins once implausible ones are set aside (`97.7% (173/177)` must
   match its fraction; words must occur in the document's own text). Cells with no majority, or with
   ink nothing read, are listed in `// Check ...` comments above the table; the picture stays in a
   commented-out `image::` line.

Header cells come from gray shading, and text outside the grid becomes a paragraph. Two engines
suffice; with fewer, OCR is skipped with a note.

- **Downloads**: nothing is fetched at install time or with `--no-ocr` (which never imports the OCR
  code). The first OCR run downloads about 110 MB: Tesseract English data (~15 MB, in
  `~/.cache/ike-dex-docs/tessdata`) and EasyOCR (~93 MB, in `~/.EasyOCR`). RapidOCR's models ship in
  its wheel, which Docling installs anyway.
- **Pros**: real, searchable tables with spans and headers; three engines cross-check each other; local.
- **Cons**: needs `easyocr` and `tesserocr` (`requirements-ocr.txt`, ~160 MB; `setup.sh --no-ocr` skips
  them); adds seconds per table; ruled tables and English only; bold and italic are lost; flagged cells
  need checking against the picture, and `check_fidelity.py` cannot verify OCR text.

On K230440's five picture tables (475 cells) the vote matched a reference read from arithmetic and
vocabulary on every cell. That is one document: compare any table that matters with its picture.

## Known limitations

Always review output against the source (`check_fidelity.py`, then the page images).

- **Pictures with text** that are not ruled tables (scans, charts), or any picture under `--no-ocr`,
  stay pictures, flagged for transcription.
- **Side-by-side columns** split at gaps over three ems; a tagged PDF reads them in tag order, an
  untagged one in visual row order.
- **Contents**: the entries and the title over them are dropped (`:toc:` is added).
- **Borderless tables** are found from their text only beside a picture; elsewhere the layout engine needs
  ruling lines.
- **Pictures of charts and diagrams** stay pictures with the text on them in the description.
- **Layout guesses** fail on tables without ruling lines, unlabelled lists and headings in body type.
- **Unsupported**: cell shading, borders and fonts; a block that follows a nested list attaches to its
  last bullet.
- **Tables without drawn borders** fall back to the tag rows, padded to the widest, with a
  `// Cell spans could not be recovered` comment.
- **Docling** flattens heading levels, drops inline formatting, splits tables across pages and handles
  vector shapes poorly. Pictures Pillow cannot read (`.emf`, `.wmf`) are skipped with a warning.

## Checking a conversion

`check_fidelity.py` parses the AsciiDoc with Pandoc and compares it with the PDF:

- **Text**: content and order of the PDF's words, with the weakest pages listed. A tagged PDF supplies
  words in tag order, without untagged text or contents entries. An untagged one uses the text layer
  minus repeating headers and footers; with ruled tables, order is judged on the text between them,
  since drawing order inside a table need not run row by row.
- **Tables**: from the tags (a header repeated on each page counts once), or for an untagged PDF from its
  ruling lines (the same kind of guess the layout engine makes, so look at the page too). Each table must be matched by AsciiDoc table(s) with
  its text and every row of two or more filled cells in order. Extra rows are reported, not failed.
  Spans are checked only through cells per row.
- **Pictures**: pages with a picture are flagged; its text is not in the text layer. OCR tables show
  as AsciiDoc tables with no tagged match.
- **Links, pictures, captions, callouts**: each is counted in the PDF with code of the checker's own, not the
  converter's, so a shared mistake cannot hide. Link annotations to web or mail addresses against the
  AsciiDoc's links (distinct targets; a missing one fails); pictures and curve drawings against image
  macros plus OCR tables, with a blank picture file (wholly transparent or one flat colour) failing and a
  shortfall a warning; "Table 6." lines against block titles; bold Note/Important labels against
  admonitions. They were added after the checker passed a converter that dropped links, left a diagram
  invisible, and lost two callouts.
- **Structure**: flat heading levels, list item counts, and italic or bold words against the fonts
  (table headers count as bold by style).
- **Appearance** (`--rendered`): per-page similarity of a PDF rendered from the AsciiDoc. It means
  something only with the source's page size and fonts, so read it as a guide; `--diff-dir` writes
  side-by-side images.

Gates are adjustable (`--min-content`, `--min-order`, `--min-cells`, `--min-shape`, `--min-appearance`).

## Tests

```bash
utilities/.venv/bin/python -m unittest discover -s utilities -p "test_*.py"
```

`test_utilities.py` builds pages, lines and cells in memory, so it needs no sample documents
(escaping tests use Asciidoctor and skip without it). Add a case for every defect you fix.

## Adding a utility

1. Put the script in this directory.
2. Add its dependencies to `requirements.txt`.
3. Re-run `./utilities/setup.sh` and add a row to the table above.
