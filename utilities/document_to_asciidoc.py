"""Convert PDF or Word (.docx) documents to AsciiDoc.

Format-agnostic: this module knows nothing about what the document contains.

Usage as a library:

    from document_to_asciidoc import DocumentToAsciiDocConverter

    converter = DocumentToAsciiDocConverter()
    adoc = converter.convert_file("report.docx", images_dir="out/images")
    adoc = converter.convert_bytes(pdf_bytes, source_name="report.pdf")

Usage from the command line:

    python document_to_asciidoc.py input.pdf [-o output.adoc]

Besides the raw conversion, the output is tidied so it is usable as AsciiDoc:

* Pictures are written to an images directory and referenced with ``image::``.
* A Word/PDF table of contents (a list of titles and page numbers) is removed
  and replaced by AsciiDoc's own ``:toc:`` attribute.
* The document gets a ``= Title`` header, and heading levels are normalized so
  top-level sections are ``==``.
* Pandoc's ``++#++``-style passthrough escapes are replaced by plain characters
  wherever that is safe.
"""

from __future__ import annotations

import argparse
import html
import io
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional, Union
from urllib.parse import unquote

SUPPORTED_EXTENSIONS = (".pdf", ".docx")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")

_TOC_TITLE_RE = re.compile(r"^(table of contents|contents|toc)$", re.IGNORECASE)
_TOC_ENTRY_RE = re.compile(
    r"^.+?(?:\s*\.{2,}\s*|\s+)(?:\d+|[ivxlcdm]+)\s*$"
    r"|^(?:table|list) of (?:figures|tables|illustrations|exhibits)\s*$",
    re.IGNORECASE,
)

# Pandoc wraps characters it considers risky as ++c++ (inline passthrough).
_PASSTHROUGH_RE = re.compile(r"\+\+([#\[\]*<]+)\+\+")
_LONE_INLINE_IMAGE_RE = re.compile(r"^image:(?!:)(\S+)\[([^\]]*)\]$", re.MULTILINE)


class UnsupportedFormatError(ValueError):
    """Raised when the input is neither a PDF nor a .docx file."""


def detect_extension(data: bytes, source_name: Optional[str] = None) -> str:
    """
    Return ".pdf" or ".docx" for the given bytes.

    The file's content wins over its name, so a mislabelled file is still
    handled correctly. The name is only used to tell Word apart from other
    zip-based formats (.xlsx, .pptx), which share the same magic bytes.
    """
    if data.lstrip()[:5] == b"%PDF-":
        return ".pdf"

    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return ".docx"
        except zipfile.BadZipFile:
            pass

    suffix = Path(source_name).suffix.lower() if source_name else ""
    raise UnsupportedFormatError(
        f"Unsupported input{f' ({suffix})' if suffix else ''}; "
        f"expected one of: {', '.join(SUPPORTED_EXTENSIONS)}"
    )


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "document"


def _docx_title(data: bytes) -> Optional[str]:
    """Return the Title property stored in a .docx file, if it is meaningful."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            core = archive.read("docProps/core.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile):
        return None

    match = re.search(r"<dc:title>(.*?)</dc:title>", core, re.DOTALL)
    title = html.unescape(match.group(1)).strip() if match else ""
    if not title or title.lower().startswith(("microsoft word", "untitled")):
        return None
    return title


def _heading_lines(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) for each Markdown heading outside code fences."""
    headings = []
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            match = _HEADING_RE.match(line)
            if match:
                headings.append((index, len(match.group(1)), match.group(2)))
    return headings


def _strip_toc(markdown: str) -> tuple[str, bool]:
    """
    Remove a table-of-contents section from Markdown.

    A section is only removed when it is made up of "title  page-number" lines
    (or "Table of Figures"-style list headings), so real content named
    "Contents" is left alone. Returns (markdown, whether a TOC was removed).
    """
    lines = markdown.split("\n")
    headings = _heading_lines(lines)

    for position, (start, _level, text) in enumerate(headings):
        if not _TOC_TITLE_RE.match(text):
            continue

        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        body = [
            (i, line)
            for i, line in enumerate(lines[start + 1 : end], start + 1)
            if line.strip() and not _IMAGE_RE.search(line)
        ]
        entries = [i for i, line in body if _TOC_ENTRY_RE.match(line.strip())]
        if not body or len(entries) < 0.8 * len(body):
            continue

        drop = set(entries) | {start}
        kept = [line for i, line in enumerate(lines) if i not in drop]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)), True

    return markdown, False


def _normalize_headings(markdown: str, fallback_title: str) -> tuple[str, str]:
    """
    Make top-level sections `#` and work out the document title.

    A single leading top-level heading is treated as the title and removed
    from the body; otherwise `fallback_title` is used. Returns (markdown, title).
    """
    lines = markdown.split("\n")
    headings = _heading_lines(lines)
    if not headings:
        return markdown, fallback_title

    min_level = min(level for _, level, _ in headings)
    top = [h for h in headings if h[1] == min_level]
    title = fallback_title
    title_index = None

    if min_level == 1 and len(top) == 1 and top[0] is headings[0]:
        title_index, _, title = top[0]
        headings = headings[1:]
        min_level = min((level for _, level, _ in headings), default=1)

    shift = min_level - 1
    for index, level, text in headings:
        lines[index] = f"{'#' * (level - shift)} {text}"

    return "\n".join(line for i, line in enumerate(lines) if i != title_index), title


def _clean_escapes(asciidoc: str) -> str:
    """
    Replace Pandoc's ++c++ passthroughs for # [ ] * < with plain characters.

    `*` and `#` pair up into bold/highlight, so they are only unescaped when
    every such character in their block is part of the run being replaced;
    otherwise each gets a backslash escape. `[` is kept escaped at the start
    of a line, where it would be read as a block attribute list, and `<` is
    kept escaped next to another angle bracket (`<<xref>>`).
    """

    def clean_block(block: str) -> str:
        def replace(match: re.Match[str]) -> str:
            run = match.group(1)
            before = block[match.start() - 1 : match.start()]
            after = block[match.end() : match.end() + 1]

            if "<" in run:
                if run == "<" and "<" not in (before, after) and ">" not in (before, after):
                    return "<"
                return match.group(0)
            if run[0] == "[" and before in ("", "\n"):
                return match.group(0)

            return "".join(
                (char if block.count(char) == run.count(char) else f"\\{char}")
                if char in "*#"
                else char
                for char in run
            )

        return _PASSTHROUGH_RE.sub(replace, block)

    return "\n\n".join(clean_block(block) for block in asciidoc.split("\n\n"))


class DocumentToAsciiDocConverter:
    """
    Convert PDF or Word (.docx) bytes or files to an AsciiDoc string.

    Pipeline:
    1) Docling parses the document into a structured representation and
       writes its pictures to disk.
    2) The Markdown export is tidied (TOC removed, headings normalized).
    3) Pandoc converts the Markdown to AsciiDoc, which is then cleaned up.

    extract_images: write pictures out when an images directory is given.
    min_image_size: skip pictures smaller than this many pixels on either side
        (decorative rules and bullets).
    toc: True always adds `:toc:`, False never does, None (default) adds it
        only when a Word/PDF table of contents was found and removed.
    """

    def __init__(
        self,
        extract_images: bool = True,
        min_image_size: int = 16,
        toc: Optional[bool] = None,
    ) -> None:
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise RuntimeError(
                "Docling is required. Install it with: pip install docling"
            ) from exc

        self._extract_images = extract_images
        self._min_image_size = min_image_size
        self._toc = toc

        pdf_options = PdfPipelineOptions(
            generate_picture_images=extract_images, images_scale=2.0
        )
        self._converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)}
        )

    def convert_file(
        self,
        path: Union[str, Path],
        images_dir: Union[str, Path, None] = None,
        image_ref_prefix: Optional[str] = None,
    ) -> str:
        """Convert a .pdf or .docx file on disk to an AsciiDoc string."""
        path = Path(path)
        return self.convert_bytes(
            path.read_bytes(),
            source_name=path.name,
            images_dir=images_dir,
            image_ref_prefix=image_ref_prefix,
        )

    def convert_bytes(
        self,
        data: bytes,
        source_name: Optional[str] = None,
        images_dir: Union[str, Path, None] = None,
        image_ref_prefix: Optional[str] = None,
    ) -> str:
        """
        Convert raw PDF or .docx bytes to an AsciiDoc string.

        source_name is optional; it provides the fallback document title and
        image file names. The format itself is detected from the bytes.

        Pictures are written to images_dir and referenced as
        `image::<image_ref_prefix>/<file>[]`. The prefix defaults to the
        directory's name, so the .adoc file should be saved next to that
        directory. Without images_dir, pictures are left out.
        """
        if not data:
            raise ValueError("data must not be empty")

        extension = detect_extension(data, source_name)
        stem = Path(source_name).stem if source_name else "document"

        from docling.datamodel.base_models import DocumentStream
        from docling_core.types.doc import ImageRefMode

        # Docling selects its parser from the stream name's extension, so the
        # name must carry the detected extension rather than the caller's.
        stream = DocumentStream(name=f"{stem}{extension}", stream=io.BytesIO(data))
        document = self._converter.convert(stream, raises_on_error=True).document

        write_images = self._extract_images and images_dir is not None
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            markdown_path = tmp_path / "document.md"
            document.save_as_markdown(
                markdown_path,
                artifacts_dir=tmp_path / "artifacts",
                image_mode=(
                    ImageRefMode.REFERENCED if write_images else ImageRefMode.PLACEHOLDER
                ),
            )
            markdown = markdown_path.read_text(encoding="utf-8")
            if write_images:
                images_dir = Path(images_dir)
                markdown = self._relocate_images(
                    markdown,
                    tmp_path,
                    images_dir,
                    image_ref_prefix or images_dir.name,
                    _slugify(stem),
                )

        markdown, had_toc = _strip_toc(markdown)
        fallback_title = (
            (_docx_title(data) if extension == ".docx" else None)
            or stem.replace("_", " ")
        )
        markdown, title = _normalize_headings(markdown, fallback_title)

        add_toc = had_toc if self._toc is None else self._toc
        return self._markdown_to_asciidoc(markdown, title, add_toc)

    def _relocate_images(
        self,
        markdown: str,
        source_root: Path,
        images_dir: Path,
        ref_prefix: str,
        slug: str,
    ) -> str:
        """Move Docling's image files into images_dir and point the Markdown at them."""
        from PIL import Image

        images_dir.mkdir(parents=True, exist_ok=True)
        # Docling's file reference -> (image number, new reference), or None if skipped.
        moved: dict[str, Optional[tuple[int, str]]] = {}
        kept = 0

        def relocate(match: re.Match[str]) -> str:
            nonlocal kept
            reference = match.group(1)
            source = source_root / unquote(reference)
            if not source.is_file():
                return match.group(0)

            if reference not in moved:
                with Image.open(source) as image:
                    too_small = min(image.size) < self._min_image_size
                if too_small:
                    moved[reference] = None
                else:
                    kept += 1
                    name = f"{slug}-image-{kept:02d}{source.suffix}"
                    shutil.copyfile(source, images_dir / name)
                    moved[reference] = (kept, f"{ref_prefix}/{name}")

            placed = moved[reference]
            return f"![Image {placed[0]}]({placed[1]})" if placed else ""

        return _IMAGE_RE.sub(relocate, markdown)

    @staticmethod
    def _markdown_to_asciidoc(markdown: str, title: str, toc: bool) -> str:
        """Convert Markdown to AsciiDoc using Pandoc via pypandoc."""
        if not markdown.strip():
            return ""

        try:
            import pypandoc
        except ImportError as exc:
            raise RuntimeError(
                "pypandoc is required for AsciiDoc output. Install it with: pip install pypandoc"
            ) from exc

        args = ["--standalone", "-M", f"title={title}"]
        if toc:
            args.append("--toc")

        try:
            asciidoc = pypandoc.convert_text(
                markdown,
                to="asciidoc",
                # Without implicit_figures an image alone in a paragraph does
                # not turn its alt text into a duplicate caption.
                format="markdown-implicit_figures",
                extra_args=args,
            )
        except OSError as exc:
            raise RuntimeError(
                "Pandoc binary is required for Markdown->AsciiDoc conversion. "
                "Install pandoc and ensure it is on PATH."
            ) from exc

        # Pandoc emits a paragraph-sized image as an inline macro, with the alt
        # text wrapped across lines; make it a block macro on a single line.
        asciidoc = _LONE_INLINE_IMAGE_RE.sub(
            lambda m: f"image::{m.group(1)}[{' '.join(m.group(2).split())}]", asciidoc
        )
        return _clean_escapes(asciidoc)


def convert_to_asciidoc(
    path: Union[str, Path], images_dir: Union[str, Path, None] = None
) -> str:
    """Convenience wrapper: convert one file to AsciiDoc."""
    return DocumentToAsciiDocConverter().convert_file(path, images_dir=images_dir)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a PDF or Word (.docx) document to AsciiDoc."
    )
    parser.add_argument("input", type=Path, help="Path to a .pdf or .docx file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output .adoc path (default: input path with .adoc extension)",
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        help="Where to write pictures (default: 'images' next to the output file)",
    )
    parser.add_argument(
        "--no-images", action="store_true", help="Leave pictures out of the output"
    )
    args = parser.parse_args(argv)

    output = args.output or args.input.with_suffix(".adoc")
    images_dir = None if args.no_images else (args.images_dir or output.parent / "images")

    try:
        converter = DocumentToAsciiDocConverter(extract_images=not args.no_images)
        asciidoc = converter.convert_file(
            args.input,
            images_dir=images_dir,
            # Reference images relative to the .adoc, wherever they were written.
            image_ref_prefix=(
                Path(os.path.relpath(images_dir, output.parent)).as_posix()
                if images_dir
                else None
            ),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(asciidoc, encoding="utf-8")
    print(f"Wrote {output} ({len(asciidoc)} characters)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
