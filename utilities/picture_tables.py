"""Read a table that a PDF stores as a picture, and turn it into an AsciiDoc table.

A picture has no text layer, so the table is rebuilt in three steps:

1. Structure: the ruling lines are found with OpenCV. A cell merges with its neighbour wherever the
   line between them is missing, which gives column and row spans. Pictures without a clear grid are
   left alone.
2. Text: every cell is split into text lines, and each line is read by three independent OCR engines
   (RapidOCR, Tesseract, EasyOCR). They share the line images, so they can only disagree about the
   reading, not about where the text is.
3. Vote: the most common reading wins. Readings that fail a check are set aside first: a percentage
   such as ``97.7% (173/177)`` must match its fraction, and every word must occur in the document's own
   text. A cell the engines could not settle (no majority, or ink that nothing read) is listed for
   review instead of being trusted.

Header cells are recognised from their gray shading. Bold and italic are not read from a picture, and
neither is anything but English.

Usage as a library:

    from picture_tables import PictureTableReader

    reader = PictureTableReader.create()          # None unless two engines load
    table = reader.read("table.png", vocabulary={"mismatch", "insertion"})
    blocks = table.to_asciidoc(escape=lambda text: text, width_percent=100)
"""

from __future__ import annotations

import collections
import dataclasses
import os
import re
import tempfile
import urllib.request
import warnings
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata"
# Engines in order of trust: the first wins when readings tie and no check can tell them apart.
ENGINE_ORDER = ("rapidocr", "tesseract", "easyocr")

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*\((\d+)/(\d+)\)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z+*\-]*")
# Characters a reading may contain: letters, digits and ordinary table punctuation. Anything else (box-drawing,
# CJK, stray glyphs such as 六) marks OCR noise.
_ALLOWED_RE = re.compile(r"[^\w\s%().,/+*<>=~:;'\"?!&@#$±°≥≤≈×÷·–—‒−®©™µ\[\]{}|\\’‘“”-]")


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def load_image(path: Path) -> Any:
    """The picture as a BGR array, with transparent areas flattened onto white."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as source:
        rgba = source.convert("RGBA")
    flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    flat.alpha_composite(rgba)
    return np.asarray(flat.convert("RGB"))[:, :, ::-1].copy()


def _cluster(values: Iterable[float], tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(g) / len(g) for g in groups]


def _ink(crop: Any) -> Any:
    """Dark pixels that are text: not light borders or gray shading, and not red spell-check marks."""
    import cv2

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    blue, green, red = crop[:, :, 0], crop[:, :, 1], crop[:, :, 2]
    return (gray < 140) & ~((red > 150) & (green < 100) & (blue < 100))


# --------------------------------------------------------------------------
# Grid
# --------------------------------------------------------------------------


@dataclasses.dataclass
class GridCell:
    r0: int  # first row band
    r1: int  # last row band
    c0: int  # first column edge
    c1: int  # right column edge (so the column span is c1 - c0)
    box: tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels


@dataclasses.dataclass
class Grid:
    bands: list[tuple[float, float]]
    edges: list[float]
    cells: list[GridCell]


def detect_grid(img: Any) -> Optional[Grid]:
    """Find the ruled grid of a table picture, or None when it does not look like a ruled table."""
    import cv2

    height, width = img.shape[:2]
    if min(height, width) < 100:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    if float(((hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 60)).mean()) > 0.15:
        return None  # a photograph or a coloured diagram, not a table
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 15, 40), 1))
    )
    rows = _cluster((y for y in range(height) if horizontal[y].sum() / 255 > 0.10 * width), 5)

    bands: list[tuple[float, float, list[float]]] = []
    for top, bottom in zip(rows, rows[1:]):
        band_height = int(bottom - top)
        if band_height < 12:
            continue
        strip = binary[int(top) + 3:int(bottom) - 2]
        vertical = cv2.morphologyEx(
            strip, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(int(0.6 * band_height), 8))),
        )
        xs = [x for x in range(width) if vertical[:, x].sum() / 255 >= 0.7 * strip.shape[0]]
        bands.append((top, bottom, _cluster(xs, 5)))

    edges = _cluster((x for *_, xs in bands for x in xs), 6)
    if len(bands) < 2 or len(edges) < 3:
        return None
    left, right, top, bottom = edges[0], edges[-1], bands[0][0], bands[-1][1]
    if (right - left) * (bottom - top) < 0.5 * width * height:
        return None

    def span(y: float) -> float:  # share of the grid's width that a horizontal rule covers
        strip = horizontal[max(int(y) - 3, 0):int(y) + 4, int(left):int(right)]
        return float((strip.max(axis=0) > 0).mean()) if strip.size else 0.0

    # A table is closed by a rule along its top and bottom and a line down each side.
    if span(top) < 0.7 or span(bottom) < 0.7:
        return None
    framed = sum(1 for _, _, xs in bands if xs and abs(xs[0] - left) <= 8 and abs(xs[-1] - right) <= 8)
    if framed < 0.7 * len(bands):
        return None

    def edge_index(x: float) -> int:
        return min(range(len(edges)), key=lambda i: abs(edges[i] - x))

    def covered(y: float, x0: float, x1: float) -> float:
        strip = horizontal[max(int(y) - 3, 0):int(y) + 4, int(x0) + 3:int(x1) - 3]
        return float((strip.max(axis=0) > 0).mean()) if strip.size else 0.0

    cells: list[dict] = []
    open_cells: dict[tuple[int, int], dict] = {}
    for index, (top_y, bottom_y, xs) in enumerate(bands):
        ids = [edge_index(x) for x in xs]
        for c0, c1 in zip(ids, ids[1:]):
            if c1 <= c0:
                continue
            cell = open_cells.get((c0, c1))
            if cell is not None and cell["open"]:
                cell["r1"] = index
            else:
                cell = {"r0": index, "r1": index, "c0": c0, "c1": c1, "open": False}
                cells.append(cell)
                open_cells[(c0, c1)] = cell
            # no rule under the cell: it carries on into the next band
            cell["open"] = covered(bottom_y, edges[c0], edges[c1]) < 0.6
        for cell in open_cells.values():
            if cell["r1"] < index:
                cell["open"] = False

    result = []
    for cell in cells:
        box = (int(edges[cell["c0"]]), int(bands[cell["r0"]][0]), int(edges[cell["c1"]]), int(bands[cell["r1"]][1]))
        result.append(GridCell(cell["r0"], cell["r1"], cell["c0"], cell["c1"], box))
    if len(result) < 4:
        return None
    return Grid([(a, b) for a, b, _ in bands], edges, result)


def _cell_lines(img: Any, box: tuple[int, int, int, int], margin: int = 5) -> list[Any]:
    """Cut a region into text-line images by the gaps in its ink, scaled up for the OCR engines."""
    import cv2
    import numpy as np

    x0, y0, x1, y1 = box
    crop = img[max(y0 + margin, 0):y1 - margin, max(x0 + margin, 0):x1 - margin]
    if crop.size == 0:
        return []
    ink = _ink(crop)
    present = ink.sum(axis=1) > 0
    runs: list[list[int]] = []
    start = None
    for i, flag in enumerate(list(present) + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append([start, i])
            start = None
    merged: list[list[int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= 3:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    lines = []
    for a, b in merged:
        if b - a < 4:  # a speck
            continue
        columns = np.where(ink[a:b].sum(axis=0) > 0)[0]
        piece = crop[max(a - 3, 0):b + 3, max(columns[0] - 3, 0):columns[-1] + 4]
        piece = cv2.resize(piece, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        lines.append(cv2.copyMakeBorder(piece, 14, 14, 14, 14, cv2.BORDER_CONSTANT, value=(255, 255, 255)))
    return lines


def _shading(img: Any, box: tuple[int, int, int, int]) -> str:
    """'header' for the plain gray fill of a header cell, 'tint' for any other colour, '' for none."""
    import cv2
    import numpy as np

    x0, y0, x1, y1 = box
    crop = img[y0 + 4:y1 - 4, x0 + 4:x1 - 4]
    if crop.size == 0:
        return ""
    background = crop[~_ink(crop)]
    if background.size == 0:
        return ""
    hsv = cv2.cvtColor(np.median(background, axis=0).astype("uint8").reshape(1, 1, 3), cv2.COLOR_BGR2HSV)[0, 0]
    saturation, value = int(hsv[1]), int(hsv[2])
    if saturation < 25 and 150 <= value <= 238:
        return "header"
    if value < 245 or saturation >= 25:
        return "tint"
    return ""


def _alignment(img: Any, box: tuple[int, int, int, int]) -> str:
    import numpy as np

    x0, y0, x1, y1 = box
    crop = img[y0 + 4:y1 - 4, x0 + 4:x1 - 4]
    if crop.size == 0:
        return ""
    ink = _ink(crop)
    columns = np.where(ink.sum(axis=0) > 0)[0]
    if columns.size == 0:
        return ""
    left, right = int(columns[0]), ink.shape[1] - 1 - int(columns[-1])
    if left >= 8 and right >= 8 and abs(left - right) <= max(6, 0.05 * ink.shape[1]):
        return "^"
    if right <= 10 and left >= 20:
        return ">"
    return ""


# --------------------------------------------------------------------------
# OCR engines
# --------------------------------------------------------------------------


def tessdata_dir() -> Path:
    """Folder holding eng.traineddata, downloaded on first use."""
    for candidate in filter(None, (os.environ.get("TESSDATA_PREFIX"),)):
        if (Path(candidate) / "eng.traineddata").is_file():
            return Path(candidate)
    folder = Path.home() / ".cache" / "ike-dex-docs" / "tessdata"
    target = folder / "eng.traineddata"
    if not target.is_file():
        import ssl

        import certifi

        folder.mkdir(parents=True, exist_ok=True)
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(TESSDATA_URL, context=context, timeout=120) as response:
            data = response.read()
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
            handle.write(data)
        os.replace(handle.name, target)
    return folder


class _Engine:
    name = ""

    def line(self, image: Any) -> str:
        raise NotImplementedError


class _RapidOCR(_Engine):
    name = "rapidocr"

    def __init__(self) -> None:
        import importlib.util

        from rapidocr import EngineType, RapidOCR

        # onnxruntime is RapidOCR's own default; PyTorch (a Docling dependency) serves when it is absent.
        kind = EngineType.ONNXRUNTIME if importlib.util.find_spec("onnxruntime") else EngineType.TORCH
        self._engine = RapidOCR(params={
            "Det.engine_type": kind, "Cls.engine_type": kind, "Rec.engine_type": kind,
            "Global.log_level": "critical",
        })

    def line(self, image: Any) -> str:
        result = self._engine(image, use_det=False, use_cls=False, use_rec=True)
        return result.txts[0] if result.txts else ""


class _Tesseract(_Engine):
    name = "tesseract"

    def __init__(self) -> None:
        import tesserocr

        self._lib = tesserocr
        self._api = tesserocr.PyTessBaseAPI(
            path=str(tessdata_dir()), lang="eng", psm=tesserocr.PSM.SINGLE_LINE
        )

    def line(self, image: Any) -> str:
        import cv2
        from PIL import Image

        picture = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
        self._api.SetPageSegMode(self._lib.PSM.SINGLE_LINE)
        self._api.SetImage(picture)
        text = self._api.GetUTF8Text().strip()
        if not text:  # a lone glyph such as "0" reads better as a single character
            self._api.SetPageSegMode(self._lib.PSM.SINGLE_CHAR)
            self._api.SetImage(picture)
            text = self._api.GetUTF8Text().strip()
        return text


class _EasyOCR(_Engine):
    name = "easyocr"

    def __init__(self) -> None:
        import easyocr

        self._reader = easyocr.Reader(["en"], gpu=False, verbose=False)

    def line(self, image: Any) -> str:
        import cv2

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape
        found = self._reader.recognize(gray, horizontal_list=[[0, width, 0, height]], free_list=[])
        return found[0][1] if found else ""


# --------------------------------------------------------------------------
# Voting
# --------------------------------------------------------------------------


def _normal(text: str) -> str:
    return "".join(text.split()).lower()


def is_plausible(text: str, vocabulary: Optional[set[str]]) -> bool:
    """A reading that passes the checks: arithmetic for ``p% (a/b)``, known words, ordinary symbols."""
    text = text.strip()
    if not text:
        return True
    match = _PERCENT_RE.fullmatch(text)
    if match:
        places = len(match[1].split(".")[-1]) if "." in match[1] else 0
        return abs(round(100 * int(match[2]) / max(int(match[3]), 1), places) - float(match[1])) <= 0.051
    if _ALLOWED_RE.search(text):
        return False
    if vocabulary:
        return all(w.lower() in vocabulary or w.lower().strip("*+-") in vocabulary for w in _WORD_RE.findall(text))
    return True


def vote(readings: dict[str, str], vocabulary: Optional[set[str]], ink: bool) -> tuple[str, bool]:
    """(winning text, whether it needs review) for the readings of one cell or line."""
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for engine in ENGINE_ORDER:
        if engine in readings:
            groups[_normal(readings[engine])].append(engine)
    def rank(item: tuple[str, list[str]]) -> tuple[int, int]:
        return len(item[1]), -min(ENGINE_ORDER.index(e) for e in item[1])

    plausible = {k: v for k, v in groups.items() if is_plausible(readings[v[0]], vocabulary)}
    pool = plausible or groups
    best = max(pool.items(), key=rank)
    text = readings[best[1][0]].strip()
    split = len(pool) > 1 and len(best[1]) * 2 <= len(readings)
    # The checks set aside a reading the engines mostly agreed on: say so rather than trust the checks.
    overruled = len(groups) > len(pool) and len(max(groups.values(), key=len)) > len(best[1])
    unread = ink and not text
    return text, split or overruled or unread


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclasses.dataclass
class OcrCell:
    grid: GridCell
    text: str
    header: bool
    align: str
    flagged: bool
    readings: dict[str, str]


@dataclasses.dataclass
class PictureTable:
    cells: list[OcrCell]
    edges: list[float]
    rows: int
    width: int  # picture width in pixels
    above: list[str]  # text in the picture outside the grid
    below: list[str]
    engines: list[str]

    @property
    def flagged(self) -> list[OcrCell]:
        return [c for c in self.cells if c.flagged]

    def review_lines(self) -> list[str]:
        """One comment line for each cell the engines could not settle."""
        lines = []
        for cell in sorted(self.flagged, key=lambda c: (c.grid.r0, c.grid.c0)):
            seen = "; ".join(f"{engine}: {text!r}" for engine, text in cell.readings.items())
            lines.append(f"row {cell.grid.r0 + 1}, column {cell.grid.c0 + 1}: kept {cell.text!r} ({seen})")
        return lines

    def to_asciidoc(self, escape: Callable[[str], str], width_percent: int = 100) -> Optional[str]:
        """The grid as an AsciiDoc table, or None when its cells do not tile the grid."""
        ncols = len(self.edges) - 1
        occupied: dict[tuple[int, int], bool] = {}
        placed: dict[int, list[OcrCell]] = collections.defaultdict(list)
        for cell in sorted(self.cells, key=lambda c: (c.grid.r0, c.grid.c0)):
            for r in range(cell.grid.r0, cell.grid.r1 + 1):
                for c in range(cell.grid.c0, cell.grid.c1):
                    if occupied.get((r, c)):
                        return None
                    occupied[(r, c)] = True
            placed[cell.grid.r0].append(cell)

        blank = GridCell(0, 0, 0, 0, (0, 0, 0, 0))
        for r in range(self.rows):
            for c in range(ncols):
                if not occupied.get((r, c)):  # a slot the detector missed: keep the table valid
                    placed[r].append(OcrCell(dataclasses.replace(blank, r0=r, r1=r, c0=c, c1=c + 1), "", False, "", False, {}))
            placed[r].sort(key=lambda c: c.grid.c0)

        votes: list[collections.Counter] = [collections.Counter() for _ in range(ncols)]
        for cell in self.cells:
            if cell.grid.c1 - cell.grid.c0 == 1 and cell.text:
                votes[cell.grid.c0][cell.align] += 1
        column = [v.most_common(1)[0][0] if v else "" for v in votes]
        widths = [self.edges[i + 1] - self.edges[i] for i in range(ncols)]
        total = sum(widths)
        spec = ",".join(f"{column[i] or '<'}{max(round(100 * w / total), 1)}" for i, w in enumerate(widths))
        first_row = placed[0]
        header_option = bool(first_row) and all(c.header for c in first_row)
        attrs = [f'cols="{spec}"', f"width={width_percent}%"] + (["options=header"] if header_option else [])

        out = [f"[{','.join(attrs)}]", "|==="]
        for r in range(self.rows):
            parts = []
            for cell in placed[r]:
                span = cell.grid.c1 - cell.grid.c0
                prefix = (str(span) if span > 1 else "") + (f".{cell.grid.r1 - cell.grid.r0 + 1}" if cell.grid.r1 > cell.grid.r0 else "")
                prefix += "+" if prefix else ""
                if span > 1 or cell.align != column[cell.grid.c0]:
                    prefix += cell.align
                if cell.header and not (header_option and r == 0):
                    prefix += "h"
                parts.append(f"{prefix}|{escape(cell.text).replace('|', chr(92) + '|')}")
            out.append(" ".join(parts))
            out.append("")
        out[-1] = "|==="
        return "\n".join(out)


class PictureTableReader:
    """Reads ruled table pictures with every OCR engine that loads (at least two are needed)."""

    def __init__(self, engines: Sequence[_Engine]) -> None:
        self.engines = list(engines)
        self.errors: list[str] = []  # pictures that could not be read, with the reason

    @classmethod
    def create(cls, log: Callable[[str], None] = lambda message: None) -> Optional["PictureTableReader"]:
        """A reader, or None (after saying why through ``log``) when fewer than two engines load."""
        import certifi

        # EasyOCR fetches its models with urllib, which needs a certificate bundle on some Python builds.
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        engines: list[_Engine] = []
        missing_package = False
        for factory in (_RapidOCR, _Tesseract, _EasyOCR):
            try:
                with warnings.catch_warnings():  # the engines' libraries are noisy
                    warnings.simplefilter("ignore")
                    engines.append(factory())
            except Exception as exc:  # a missing package, model download or library
                missing_package = missing_package or isinstance(exc, ImportError)
                log(f"OCR engine {factory.name} is unavailable: {type(exc).__name__}: {exc}")
        if len(engines) < 2:
            log("OCR needs at least two engines to cross-check each other, so pictures are left as they are.")
            if missing_package:
                log("Install the OCR packages with: pip install -r utilities/requirements-ocr.txt "
                    "(or pass --no-ocr to skip this step).")
            return None
        return cls(engines)

    def _read(self, lines: list[Any], vocabulary: Optional[set[str]]) -> tuple[str, dict[str, str], bool]:
        readings = {}
        for engine in self.engines:
            parts = []
            for image in lines:
                try:
                    parts.append(engine.line(image))
                except Exception:
                    parts.append("")
            readings[engine.name] = " ".join(p for p in parts if p).strip()
        text, flagged = vote(readings, vocabulary, ink=bool(lines))
        return text, readings, flagged

    def read(self, path: Path, vocabulary: Optional[set[str]] = None) -> Optional[PictureTable]:
        """The table in a picture, or None when it has no clear ruled grid or cannot be read."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                return self._read_picture(Path(path), vocabulary)
            except Exception as exc:  # an odd picture must cost one table, not the whole conversion
                self.errors.append(f"{Path(path).name}: {type(exc).__name__}: {exc}")
                return None

    def _read_picture(self, path: Path, vocabulary: Optional[set[str]]) -> Optional[PictureTable]:
        img = load_image(path)
        grid = detect_grid(img)
        if grid is None:
            return None
        cells = []
        for cell in grid.cells:
            text, readings, flagged = self._read(_cell_lines(img, cell.box), vocabulary)
            shade = _shading(img, cell.box)
            cells.append(OcrCell(cell, text, shade == "header", _alignment(img, cell.box), flagged, readings))

        height, width = img.shape[:2]
        top, bottom = int(grid.bands[0][0]), int(grid.bands[-1][1])
        outside: dict[str, list[str]] = {"above": [], "below": []}
        for side, box in (("above", (0, 0, width, top - 2)), ("below", (0, bottom + 2, width, height))):
            if box[3] - box[1] < 14:
                continue
            for line in _cell_lines(img, box, margin=2):
                text, _, _ = self._read([line], vocabulary)
                if text:
                    outside[side].append(text)
        return PictureTable(
            cells, grid.edges, len(grid.bands), width, outside["above"], outside["below"],
            [e.name for e in self.engines],
        )
