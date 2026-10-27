# utilities

Python helpers for the ingestion workflow. One shared virtual environment
(`utilities/.venv`, git-ignored) serves every script here.

## Setup

```bash
brew install pandoc          # system dependency of document_to_asciidoc.py
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
utilities/.venv/bin/python utilities/document_to_asciidoc.py input.docx -o work/input.adoc
```

The output is raw conversion only. Continue with `semantic-linebreak` and
decomposition as described in the repository README.

## Adding a utility

1. Put the script in this directory.
2. Add its dependencies to `requirements.txt`.
3. Re-run `./utilities/setup.sh` and add a row to the table above.
