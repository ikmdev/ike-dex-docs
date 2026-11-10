"""The structure tree of a PDF with the page of every marked-content id.

pdfplumber lists a structure element's marked-content ids without their pages: a table cell or a paragraph that
runs over a page break has ids from two pages, and a guess from where the numbers restart is wrong whenever a
page's numbering does not start lower than the last one's. The ids carry their page where the tree states it
(in a marked-content reference, or on the element or an ancestor), so this reads it from there.

    tree = paged_structure_tree(pdf)  # like pdf.structure_tree, plus "mcid_pages": [[page, mcid], ...]
"""

from __future__ import annotations

from typing import Any, Optional

from pdfminer.pdftypes import resolve1
from pdfplumber.structure import PDFStructElement, PDFStructTree, StructTreeMissing


class _Tree(PDFStructTree):
    """Keeps each element's raw dictionary, to read its marked-content references again."""

    def __init__(self, doc: Any) -> None:
        self.raw: dict[int, tuple[Any, list[Any]]] = {}
        super().__init__(doc)

    def _make_element(self, obj: Any) -> tuple[Optional[PDFStructElement], list[Any]]:
        element, children = super()._make_element(obj)
        if element is not None:
            self.raw[id(element)] = (obj, children)
        return element, children


def paged_structure_tree(pdf: Any) -> list[dict[str, Any]]:
    """The document's structure tree as dictionaries, or [] when it has none."""
    try:
        tree = _Tree(pdf)
    except StructTreeMissing:
        return []
    pages = {page.page_obj.pageid: page.page_number for page in pdf.pages}

    def page_of(obj: Any, inherited: Optional[int]) -> Optional[int]:
        pg = obj.get("Pg") if isinstance(obj, dict) else None
        return pages.get(getattr(pg, "objid", None), inherited) if pg is not None else inherited

    def convert(element: PDFStructElement, inherited: Optional[int]) -> dict[str, Any]:
        obj, kids = tree.raw.get(id(element), ({}, []))
        here = element.page_number or inherited
        marks: list[list[int]] = []
        for child in kids:
            resolved = resolve1(child)
            if isinstance(resolved, int):
                marks.append([here or 0, resolved])
            elif isinstance(resolved, dict) and "MCID" in resolved:
                marks.append([page_of(resolved, here) or 0, resolved["MCID"]])
        # the same ids pdfplumber lists, in the same order; if its list differs, pages cannot be paired
        if [m[1] for m in marks] != element.mcids:
            marks = []
        out: dict[str, Any] = {"type": element.type}
        for key in ("page_number", "revision", "id", "lang", "title", "alt_text", "actual_text"):
            if getattr(element, key) is not None:
                out[key] = getattr(element, key)
        if element.attributes:
            out["attributes"] = element.attributes
        if element.mcids:
            out["mcids"] = list(element.mcids)
            if marks:
                out["mcid_pages"] = marks
        if element.children:
            out["children"] = [convert(child, here) for child in element.children]
        return out

    return [convert(element, None) for element in tree]
