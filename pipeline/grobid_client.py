#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GROBID Client Module for PDF Reference Extraction

Sends PDFs to a running GROBID server, parses TEI XML responses,
and locates bibliography references on PDF pages using PyMuPDF text search.

Output format matches the existing footnote NER pipeline structure so the
frontend can render reference overlays and component tags identically.
"""

import re
import uuid
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import requests
import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# TEI namespace used by GROBID output
TEI_NS = "http://www.tei-c.org/ns/1.0"


def _ns(tag: str) -> str:
    """Prefix a tag name with the TEI namespace."""
    return f"{{{TEI_NS}}}{tag}"


class GrobidClient:
    """Client for GROBID reference extraction with PDF page location."""

    def __init__(self, grobid_url: str = "http://localhost:8070"):
        self.grobid_url = grobid_url.rstrip("/")
        self.timeout = 120  # seconds – GROBID can be slow on large PDFs

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check whether the GROBID server is reachable."""
        try:
            resp = requests.get(
                f"{self.grobid_url}/api/isalive", timeout=5
            )
            return resp.status_code == 200
        except Exception:
            return False

    def process_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """
        Full pipeline: send PDF to GROBID → parse TEI XML → locate on pages.

        Returns a dict with keys:
          - grobid_references: list of reference dicts (with bbox, components)
          - grobid_metadata: extraction metadata
        """
        start = datetime.now()

        # Step 1 – call GROBID
        tei_xml = self._call_grobid(pdf_path)

        # Step 2 – parse TEI XML into structured references
        parsed_refs, doc_meta = self._parse_tei_xml(tei_xml)

        # Step 3 – locate each reference on the PDF pages
        located_refs = self._locate_references_on_pdf(pdf_path, parsed_refs)

        elapsed = (datetime.now() - start).total_seconds()

        return {
            "grobid_references": located_refs,
            "grobid_metadata": {
                "document_title": doc_meta.get("title", ""),
                "total_references": len(located_refs),
                "grobid_version": doc_meta.get("grobid_version", ""),
                "processing_time_seconds": round(elapsed, 2),
            },
        }

    def parse_tei_file(self, tei_path: str) -> Tuple[List[Dict], Dict]:
        """Parse an existing TEI XML file (useful for tests)."""
        with open(tei_path, "r", encoding="utf-8") as f:
            xml_text = f.read()
        return self._parse_tei_xml(xml_text)

    # ------------------------------------------------------------------
    # GROBID HTTP call
    # ------------------------------------------------------------------

    def _call_grobid(self, pdf_path: str) -> str:
        """Send a PDF to GROBID processFulltextDocument and return TEI XML."""
        url = f"{self.grobid_url}/api/processFulltextDocument"
        with open(pdf_path, "rb") as f:
            resp = requests.post(
                url,
                files={"input": (Path(pdf_path).name, f, "application/pdf")},
                data={
                    "teiCoordinates": "biblStruct",
                    "consolidateCitations": "0",
                },
                timeout=self.timeout,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"GROBID returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        resp.encoding = "utf-8"
        return resp.text

    # ------------------------------------------------------------------
    # TEI XML parsing
    # ------------------------------------------------------------------

    def _parse_tei_xml(self, xml_text: str) -> Tuple[List[Dict], Dict]:
        """
        Parse GROBID TEI XML and extract bibliography entries.

        Returns (list_of_references, document_metadata).
        """
        root = ET.fromstring(xml_text)

        # Document-level metadata
        doc_meta: Dict[str, Any] = {}
        title_el = root.find(f".//{_ns('titleStmt')}/{_ns('title')}")
        if title_el is not None and title_el.text:
            doc_meta["title"] = title_el.text.strip()

        app_el = root.find(f".//{_ns('application')}")
        if app_el is not None:
            doc_meta["grobid_version"] = app_el.get("version", "")

        # Find all <biblStruct> in the back/references section
        references: List[Dict[str, Any]] = []
        back_el = root.find(f".//{_ns('back')}")
        if back_el is not None:
            list_bibl = back_el.find(f".//{_ns('listBibl')}")
            if list_bibl is not None:
                for bibl in list_bibl.findall(_ns("biblStruct")):
                    ref = self._parse_bibl_struct(bibl)
                    if ref:
                        references.append(ref)

        return references, doc_meta

    def _parse_bibl_struct(self, bibl: ET.Element) -> Optional[Dict[str, Any]]:
        """Parse a single <biblStruct> into a structured reference dict."""
        ref_id = bibl.get("{http://www.w3.org/XML/1998/namespace}id", "")

        components: List[Dict[str, Any]] = []
        text_parts: List[str] = []

        # ---- Authors ----
        for author_el in bibl.iter(_ns("author")):
            pers = author_el.find(_ns("persName"))
            if pers is not None:
                forename = ""
                surname = ""
                names_sub: List[Dict[str, Any]] = []
                for fn in pers.findall(_ns("forename")):
                    if fn.text:
                        forename += fn.text.strip() + " "
                        names_sub.append({
                            "label": "FirstName",
                            "text": fn.text.strip(),
                            "confidence": 1.0,
                        })
                sn = pers.find(_ns("surname"))
                if sn is not None and sn.text:
                    surname = sn.text.strip()
                    names_sub.append({
                        "label": "LastName",
                        "text": surname,
                        "confidence": 1.0,
                    })
                full_name = f"{forename.strip()} {surname}".strip()
                if full_name:
                    comp = {
                        "label": "Author",
                        "text": full_name,
                        "confidence": 1.0,
                    }
                    if names_sub:
                        comp["names"] = names_sub
                    components.append(comp)
                    text_parts.append(full_name)

        # ---- Editors ----
        for editor_el in bibl.iter(_ns("editor")):
            editor_text = self._element_text(editor_el)
            if editor_text:
                components.append({
                    "label": "Editor",
                    "text": editor_text,
                    "confidence": 1.0,
                })

        # ---- Titles ----
        for title_el in bibl.iter(_ns("title")):
            title_text = self._element_text(title_el)
            if not title_text:
                continue
            level = title_el.get("level", "")
            title_type = title_el.get("type", "")
            if level == "a":
                # Article / analytic title
                components.append({
                    "label": "Title",
                    "text": title_text,
                    "confidence": 1.0,
                })
                text_parts.append(title_text)
            elif level == "j":
                components.append({
                    "label": "Journal",
                    "text": title_text,
                    "confidence": 1.0,
                })
                text_parts.append(title_text)
            elif level == "m":
                # Monograph / book title
                label = "BookTitle" if any(
                    c["label"] == "Title" for c in components
                ) else "Title"
                components.append({
                    "label": label,
                    "text": title_text,
                    "confidence": 1.0,
                })
                text_parts.append(title_text)

        # ---- Imprint: date, publisher, pubPlace, volume, issue, pages ----
        for imprint in bibl.iter(_ns("imprint")):
            # Date / year
            date_el = imprint.find(_ns("date"))
            if date_el is not None:
                year = date_el.get("when", "")
                if not year and date_el.text:
                    year = date_el.text.strip()
                if year:
                    # Extract just the 4-digit year
                    match = re.search(r"\d{4}", year)
                    if match:
                        year = match.group(0)
                    components.append({
                        "label": "PublicationYear",
                        "text": year,
                        "confidence": 1.0,
                    })
                    text_parts.append(year)

            # Publisher
            pub_el = imprint.find(_ns("publisher"))
            if pub_el is not None and pub_el.text:
                components.append({
                    "label": "Publisher",
                    "text": pub_el.text.strip(),
                    "confidence": 1.0,
                })
                text_parts.append(pub_el.text.strip())

            # Publication place
            place_el = imprint.find(_ns("pubPlace"))
            if place_el is not None and place_el.text:
                components.append({
                    "label": "PublicationPlace",
                    "text": place_el.text.strip(),
                    "confidence": 1.0,
                })
                text_parts.append(place_el.text.strip())

            # Volume
            for scope in imprint.findall(_ns("biblScope")):
                unit = scope.get("unit", "")
                val = scope.text.strip() if scope.text else ""
                if not val:
                    continue
                if unit == "volume":
                    components.append({
                        "label": "Volume",
                        "text": val,
                        "confidence": 1.0,
                    })
                elif unit == "issue":
                    components.append({
                        "label": "Issue",
                        "text": val,
                        "confidence": 1.0,
                    })
                elif unit == "page":
                    from_p = scope.get("from", "")
                    to_p = scope.get("to", "")
                    page_text = f"{from_p}-{to_p}" if from_p and to_p else val
                    components.append({
                        "label": "Pages",
                        "text": page_text,
                        "confidence": 1.0,
                    })

        # ---- DOI ----
        for idno in bibl.iter(_ns("idno")):
            if idno.get("type", "").upper() == "DOI" and idno.text:
                components.append({
                    "label": "DOI",
                    "text": idno.text.strip(),
                    "confidence": 1.0,
                })

        # ---- URL / ptr ----
        for ptr in bibl.iter(_ns("ptr")):
            target = ptr.get("target", "")
            if target:
                components.append({
                    "label": "Webpage",
                    "text": target,
                    "confidence": 1.0,
                })

        # ---- Notes ----
        for note_el in bibl.iter(_ns("note")):
            note_text = self._element_text(note_el)
            if note_text:
                text_parts.append(note_text)

        if not components:
            return None

        # Build a reconstructed reference string
        ref_text = self._reconstruct_reference_text(components)

        # Parse GROBID coordinates if available
        coords_str = bibl.get("coords", "")
        bbox = None
        page = None
        if coords_str:
            parts = coords_str.split(";")
            x0, y0, x1, y1 = float('inf'), float('inf'), float('-inf'), float('-inf')
            for p in parts:
                vals = p.split(",")
                if len(vals) >= 5:
                    try:
                        pg = int(vals[0])
                        if page is None:
                            page = pg
                        x, y, w, h = map(float, vals[1:5])
                        x0 = min(x0, x)
                        y0 = min(y0, y)
                        x1 = max(x1, x + w)
                        y1 = max(y1, y + h)
                    except ValueError:
                        continue
            
            if page is not None and x0 != float('inf'):
                bbox = {
                    "x1": round(x0, 1),
                    "y1": round(y0, 1),
                    "x2": round(x1, 1),
                    "y2": round(y1, 1),
                }

        return {
            "id": ref_id or str(uuid.uuid4()),
            "text": ref_text,
            "components": components,
            "source": "grobid",
            "grobid_page": page,
            "grobid_bbox": bbox,
        }

    def _reconstruct_reference_text(self, components: List[Dict]) -> str:
        """Build a readable reference string from components."""
        parts = []
        authors = [c["text"] for c in components if c["label"] == "Author"]
        if authors:
            parts.append(", ".join(authors))

        title = next(
            (c["text"] for c in components if c["label"] == "Title"), ""
        )
        if title:
            parts.append(title)

        journal = next(
            (c["text"] for c in components if c["label"] == "Journal"), ""
        )
        book = next(
            (c["text"] for c in components if c["label"] == "BookTitle"), ""
        )
        if journal:
            parts.append(journal)
        elif book:
            parts.append(book)

        year = next(
            (c["text"] for c in components if c["label"] == "PublicationYear"),
            "",
        )
        if year:
            parts.append(year)

        publisher = next(
            (c["text"] for c in components if c["label"] == "Publisher"), ""
        )
        if publisher:
            parts.append(publisher)

        place = next(
            (c["text"] for c in components if c["label"] == "PublicationPlace"),
            "",
        )
        if place:
            parts.append(place)

        return ", ".join(parts) if parts else ""

    @staticmethod
    def _element_text(el: ET.Element) -> str:
        """Get all text content of an element, including tails of children."""
        return "".join(el.itertext()).strip()

    # ------------------------------------------------------------------
    # Locating references on PDF pages
    # ------------------------------------------------------------------

    def _locate_references_on_pdf(
        self, pdf_path: str, parsed_refs: List[Dict]
    ) -> List[Dict[str, Any]]:
        """
        For each parsed reference, try to find its location on the PDF.
        Uses GROBID's exact coordinates if available. Fallbacks to PyMuPDF
        text search with progressively shorter search strings.
        """
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        located: List[Dict[str, Any]] = []

        for ref in parsed_refs:
            g_page = ref.get("grobid_page")
            g_bbox = ref.get("grobid_bbox")
            
            if g_page and g_bbox:
                # Use GROBID coordinates directly
                ref_out = {
                    "id": ref["id"],
                    "text": ref["text"],
                    "page": g_page,
                    "bbox": g_bbox,
                    "confidence": 1.0,
                    "components": ref["components"],
                    "source": "grobid",
                }
            else:
                # Fallback to PyMuPDF text search
                best_match = self._find_reference_on_pages(doc, ref, total_pages)
                ref_out = {
                    "id": ref["id"],
                    "text": ref["text"],
                    "page": best_match["page"] if best_match else None,
                    "bbox": best_match["bbox"] if best_match else None,
                    "confidence": best_match["confidence"] if best_match else 0.0,
                    "components": ref["components"],
                    "source": "grobid",
                }
            located.append(ref_out)

        doc.close()
        return located

    def _find_reference_on_pages(
        self, doc: fitz.Document, ref: Dict, total_pages: int
    ) -> Optional[Dict]:
        """
        Try to find a reference on PDF pages using several search strategies.
        Restricts search to the last few pages to avoid highlighting in-text citations.
        Returns the bounding box of the entire text block containing the match.
        """
        # Prioritize the last 30% of pages (minimum 2) where bibliography usually lives
        num_bib_pages = max(2, int(total_pages * 0.3))
        search_pages = list(range(max(0, total_pages - num_bib_pages), total_pages))

        # Build search queries from components
        search_queries = self._build_search_queries(ref)

        for query in search_queries:
            if not query or len(query) < 4:
                continue
            for page_idx in search_pages:
                page = doc[page_idx]
                rects = page.search_for(query, quads=False)
                if rects:
                    # Merge matched string rectangles
                    merged = self._merge_rects(rects)
                    
                    # Find the paragraph block that contains this match
                    blocks = page.get_text("blocks")
                    best_block = None
                    for b in blocks:
                        # b is (x0, y0, x1, y1, text, block_no, block_type)
                        b_rect = fitz.Rect(b[:4])
                        # If the block intersects significantly with our match
                        if b_rect.intersects(merged):
                            best_block = b_rect
                            break
                    
                    # Use the full block if found, otherwise fallback to just the matched string
                    final_rect = best_block if best_block else merged
                    
                    return {
                        "page": page_idx + 1,  # 1-indexed
                        "bbox": {
                            "x1": round(final_rect.x0, 1),
                            "y1": round(final_rect.y0, 1),
                            "x2": round(final_rect.x1, 1),
                            "y2": round(final_rect.y1, 1),
                        },
                        "confidence": 1.0,
                    }

        return None

    def _build_search_queries(self, ref: Dict) -> List[str]:
        """
        Build a prioritized list of search queries for a reference.

        We try increasingly specific queries, preferring author surnames
        combined with year since they are short and distinctive.
        """
        queries = []

        # 1. Author surname + year (most specific, short)
        authors = [c for c in ref["components"] if c["label"] == "Author"]
        year = next(
            (c["text"] for c in ref["components"] if c["label"] == "PublicationYear"),
            "",
        )
        for author in authors:
            # Try to extract just the surname (last word)
            surname = author["text"].split()[-1] if author["text"] else ""
            if surname and year:
                queries.append(f"{surname}, {year}")
                queries.append(f"{surname} {year}")

        # 2. Author surname alone
        for author in authors:
            surname = author["text"].split()[-1] if author["text"] else ""
            if surname and len(surname) > 2:
                # Search for "Surname X." pattern (initial + surname)
                names = author.get("names", [])
                first = next(
                    (n["text"] for n in names if n["label"] == "FirstName"), ""
                )
                if first:
                    queries.append(f"{first[0]}. {surname}")
                    queries.append(f"{surname}")

        # 3. Title fragment (first ~40 chars)
        title = next(
            (c["text"] for c in ref["components"] if c["label"] == "Title"), ""
        )
        if title and len(title) > 10:
            # Use first N chars of title as search
            queries.append(title[:min(50, len(title))])
            queries.append(title[:min(30, len(title))])

        return queries

    @staticmethod
    def _merge_rects(rects: List[fitz.Rect]) -> fitz.Rect:
        """Merge multiple rectangles into one encompassing rect."""
        if not rects:
            return fitz.Rect(0, 0, 0, 0)
        result = rects[0]
        for r in rects[1:]:
            result |= r  # union
        return result
