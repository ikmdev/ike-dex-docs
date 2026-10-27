# utilities

Python helpers for the ingestion workflow. One shared virtual environment
(`utilities/.venv`, git-ignored) serves every script here.

## Setup

Install [Pandoc](https://pandoc.org/installing.html), a system dependency of
`document_to_asciidoc.py` that must be on your `PATH`, then create the environment:

```bash
./utilities/setup.sh         # creates .venv and installs requirements.txt
```

The environment was built with Python 3.14 (`PYTHON=python3.14 ./utilities/setup.sh`).
`docling` pulls in PyTorch and may lag the newest Python release; if installation fails,
choose an older interpreter and delete `utilities/.venv` first.

`requirements.txt` holds the direct dependencies; `requirements.lock` is the `pip freeze`
of the working environment. Regenerate it after changing dependencies:
`utilities/.venv/bin/python -m pip freeze > utilities/requirements.lock`.

## Scripts

| Script | Purpose |
| --- | --- |
| `document_to_asciidoc.py` | Convert a `.pdf` or `.docx` to AsciiDoc (Docling, then Pandoc). Writes pictures to an `images/` directory, drops the source's table of contents, and normalizes headings. |

```bash
utilities/.venv/bin/python utilities/document_to_asciidoc.py input.docx -o work/raw/input.adoc
```

`work/` is git-ignored scratch space; pictures go to `work/raw/images/`.
The output is raw conversion only. Continue with `semantic-linebreak` and
decomposition as described in the repository README.

### Known limitations

Always review the output against the source (render the original with `soffice --headless
--convert-to pdf` to compare):

- Vector shapes, text boxes and positioned cover art in Word are flattened. Docling may emit a
  picture of a banner with its text baked in alongside the same text as ordinary paragraphs;
  keep one and drop the other.
- `.emf`/`.wmf` pictures cannot be read by Pillow and are skipped with a warning.
- Table captions and "Figure n." lines come through as plain paragraphs; convert them to
  `.Title` block titles. Cells with several paragraphs may merge into one line.
- A source table of contents is removed and replaced by `:toc:`; running headers and footers are lost.

## Adding a utility

1. Put the script in this directory.
2. Add its dependencies to `requirements.txt`.
3. Re-run `./utilities/setup.sh` and add a row to the table above.
