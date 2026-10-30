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

**Tagged** engine: the tag tree says what is a paragraph, list item, table cell, heading (`H1`-`H6`)
or contents entry (dropped, `:toc:` added).

**Layout** engine: with no tags, it decides from the page.

- **Paragraphs** break when the line distance exceeds the page's measured line pitch by a third, or
  at a change of type size or boldness, a dedent, or a short last line after a full stop. A paragraph
  over a page break is joined when it stops near the foot of the page without one.
- **Tables** are those drawn with ruling lines. A wholly bold cell is a header; a lone box is text.
  A table continuing on the next page becomes two.
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
- **Side-by-side columns** (a web form's label and value pairs) split at gaps over three ems and read
  in visual row order. Tags may list the left column first, so the checker's order figure can drop.
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
- **Tables**: from the tags, or for an untagged PDF from its ruling lines (the same kind of guess the
  layout engine makes, so look at the page too). Each table must be matched by AsciiDoc table(s) with
  its text and every row of two or more filled cells in order. Extra rows are reported, not failed.
  Spans are checked only through cells per row.
- **Pictures**: pages with a picture are flagged; its text is not in the text layer. OCR tables show
  as AsciiDoc tables with no tagged match.
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
