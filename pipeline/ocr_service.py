#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared OCR engine used both by the bulk PDF-processing pipeline
(`PDFFootnoteExtractor`) and by on-demand re-OCR of a single, user-edited
bounding box (see app.py's footnote add/edit endpoints).

Two engines: Surya (real OCR — rasterizes the region and reads it as an
image, so it works even on scanned/image-only PDFs) and pdfminer.six
(extracts the region's already-embedded PDF text layer directly — fast, no
model weights, but only works when the PDF actually has one). Surya's model
weights are expensive to load, so instances are cached per engine type for
the lifetime of the process instead of being reloaded per job/request.
"""

import threading

import torch
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF — only used for page rendering/bounds on the Surya path
from PIL import Image

try:
    from pipeline.model_pool import InstancePool
    from pipeline.line_hyphens import join_line_break_hyphens
except ImportError:
    # Handle if run directly
    from model_pool import InstancePool
    from line_hyphens import join_line_break_hyphens

# Must match PDFFootnoteExtractor.pdf_to_images' render resolution so that
# image-pixel bounding boxes and PDF-point bounding boxes convert consistently.
RENDER_DPI = 150
POINTS_TO_PIXELS = RENDER_DPI / 72.0


def _inherited_box(doc: "fitz.Document", page: "fitz.Page", key: str) -> Optional["fitz.Rect"]:
    """A page's raw MediaBox/CropBox in PDF space, following /Parent
    inheritance, normalised so x0 < x1 and y0 < y1. None if absent."""
    xref = page.xref
    while xref:
        kind, value = doc.xref_get_key(xref, key)
        if kind == 'array':
            x0, y0, x1, y1 = (float(n) for n in value.strip('[]').split())
            return fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        kind, parent = doc.xref_get_key(xref, 'Parent')
        xref = int(parent.split()[0]) if kind == 'xref' else 0
    return None


def open_pdf(pdf_path: str) -> "fitz.Document":
    """Open a PDF with every page framed the way the viewer's pdf.js frames it.

    Every bbox in this app is in the viewer's page space: points from the
    top-left of the page pdf.js draws. pdf.js draws CropBox ∩ MediaBox.
    PyMuPDF instead renders a CropBox reaching outside the MediaBox whole —
    invalid per the spec, but some publishers' PDFs have it — so its pixmap
    gained a strip the viewer doesn't show, and every box detected or OCR'd
    on it came out shifted by that strip's width (28pt on some books).
    Clipping the CropBox here, in memory only, puts both in the same frame.
    Pages whose CropBox already lies inside the MediaBox are left alone.
    """
    doc = fitz.open(pdf_path)
    for page in doc:
        media = _inherited_box(doc, page, 'MediaBox')
        crop = _inherited_box(doc, page, 'CropBox')
        if media is None or crop is None:
            continue
        view = crop & media
        if view.is_empty:
            view = media  # what pdf.js falls back to as well
        if view != crop:
            doc.xref_set_key(page.xref, 'CropBox', '[%g %g %g %g]' % tuple(view))
    return doc


class OCREngine:
    """Lazily-loaded OCR engine: copies per engine type in OCREngine.pool
    (model_pool.py); get_instance() gives this thread's copy."""

    def __init__(self, engine: str):
        self.engine = engine
        self.use_surya = (engine == 'surya')
        # A copy is shared by every thread that isn't leasing one: document
        # processing and the viewer's box OCR (waitress serves requests on
        # several threads). The Surya models are not safe to run concurrently —
        # the recognition model keeps a cache sized to the batch in flight, so
        # two overlapping calls fail with "The size of tensor a (28) must match
        # the size of tensor b (19)", each seeing the other's batch. Calls on
        # one copy take turns; separate copies run side by side.
        self._surya_lock = threading.Lock()

        if self.use_surya:
            from surya.model.recognition.model import load_model as load_rec_model
            from surya.model.recognition.processor import load_processor as load_rec_processor
            from surya.model.detection.model import load_model as load_det_model
            from surya.model.detection.model import load_processor as load_det_processor

            self.surya_rec_model = load_rec_model()
            self.surya_rec_processor = load_rec_processor()
            self.surya_det_model = load_det_model()
            self.surya_det_processor = load_det_processor()
            # One dtype throughout, whatever happened while loading (see
            # MODEL_LOAD_LOCK in model_pool.py): a model with float32 and float16
            # layers fails every call.
            from surya.settings import settings as surya_settings
            for name in ('surya_rec_model', 'surya_det_model'):
                model = getattr(self, name)
                mixed = {p.dtype for p in model.parameters()}
                if len(mixed) > 1:
                    print(f"⚠️ {name} loaded with mixed dtypes {mixed}; converting to {surya_settings.MODEL_DTYPE}")
                setattr(self, name, model.to(dtype=surya_settings.MODEL_DTYPE))
        elif engine != 'pdfminer':
            raise ValueError(f"Unknown OCR engine: {engine!r}")
        # pdfminer.six has nothing to preload — it parses each PDF lazily,
        # per call, in _pdfminer_page_lines below.

    @classmethod
    def get_instance(cls, engine) -> "OCREngine":
        # Back-compat: older callers (and any job's saved options predating
        # this engine becoming a named string) pass a bool — True meant
        # Surya, False meant "the other one", which used to be a PyMuPDF
        # text-layer fallback mislabeled "Tesseract" in the UI even though
        # it never ran Tesseract. pdfminer.six now fills that same "extract
        # the embedded text layer" role for real, so False maps onto it and
        # nothing already-processed silently breaks.
        return cls.pool.current(cls.engine_key(engine))

    @staticmethod
    def engine_key(engine) -> str:
        if isinstance(engine, bool):
            return 'surya' if engine else 'pdfminer'
        return engine

    # -- Surya: line-grouping/joining heuristic (shared by both entrypoints) --

    def _run_surya_ocr(self, cropped_imgs: List[Image.Image]) -> List[str]:
        from surya.ocr import run_ocr

        if not cropped_imgs:
            return []

        langs = [["pl", "en"]] * len(cropped_imgs)

        # Using run_ocr since footnotes have multiple lines of text
        def ocr(imgs, lng):
            return run_ocr(imgs, lng, self.surya_det_model, self.surya_det_processor,
                           self.surya_rec_model, self.surya_rec_processor)

        with self._surya_lock:
            try:
                results = ocr(cropped_imgs, langs)
            except torch.cuda.OutOfMemoryError:
                # The GPU is shared with the NER models and other requests: free
                # what PyTorch has cached and try again, then one region at a
                # time. A page whose OCR fails is never dropped silently - the
                # caller fails the job instead.
                torch.cuda.empty_cache()
                try:
                    results = ocr(cropped_imgs, langs)
                except torch.cuda.OutOfMemoryError:
                    torch.cuda.empty_cache()
                    results = []
                    for img, lng in zip(cropped_imgs, langs):
                        results += ocr([img], [lng])
                        torch.cuda.empty_cache()

        final_texts = []
        for res in results:
            boxes = []
            for line in res.text_lines:
                text = line.text.strip()
                if not text:
                    continue

                xs = [pt[0] for pt in line.polygon]
                ys = [pt[1] for pt in line.polygon]
                boxes.append({
                    "text": text,
                    "min_x": min(xs), "max_x": max(xs),
                    "min_y": min(ys), "max_y": max(ys)
                })

            # Sort boxes primarily by top y-coordinate
            boxes.sort(key=lambda b: b['min_y'])

            lines_grouped = []
            for box in boxes:
                added = False
                box_height = box['max_y'] - box['min_y']

                for line_group in lines_grouped:
                    group_min_y = min(b['min_y'] for b in line_group)
                    group_max_y = max(b['max_y'] for b in line_group)
                    group_height = group_max_y - group_min_y

                    intersection = max(0, min(box['max_y'], group_max_y) - max(box['min_y'], group_min_y))

                    # Group if there's significant vertical overlap (>20% of the smaller box's height)
                    if intersection > 0.2 * min(box_height, group_height):
                        line_group.append(box)
                        added = True
                        break

                if not added:
                    lines_grouped.append([box])

            # Sort left-to-right within groups and join
            joined_lines = []
            for line_group in lines_grouped:
                line_group.sort(key=lambda b: b['min_x'])
                joined_lines.append(" ".join(b['text'] for b in line_group))

            text = join_line_break_hyphens("\n".join(joined_lines))
            final_texts.append(text.strip())

        return final_texts

    # -- pdfminer.six text-layer extraction (shared by both entrypoints) --
    #
    # pdfminer's coordinate space has its origin at the page's bottom-left
    # with y increasing upward — the opposite of the top-left/y-down space
    # every bbox in this app is stored and worked with in (matching
    # PyMuPDF's fitz.Rect convention, used elsewhere in this module and by
    # the frontend overlay editor). _pdfminer_page_lines flips each line's
    # bbox to that top-left/y-down convention once, up front, so every
    # caller can compare pdfminer's output directly against app-space rects
    # without redoing the flip per bbox.

    @staticmethod
    def _pdfminer_page_lines(pdf_path: str, page_index: int) -> Tuple[List[Tuple[Tuple[float, float, float, float], str]], float, float]:
        """Parse one page's text layer once via pdfminer.six and return
        (lines, page_width, page_height), where each line is
        ((x1, y1, x2, y2), text) in top-left/y-down PDF-point space. Parsing
        is per-page (via extract_pages' page_numbers filter) so a bulk call
        covering many boxes on the same page only pays for this once."""
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer, LTTextLine

        lines: List[Tuple[Tuple[float, float, float, float], str]] = []
        page_width = page_height = 0.0
        for page_layout in extract_pages(pdf_path, page_numbers=[page_index]):
            page_width, page_height = page_layout.width, page_layout.height
            for element in page_layout:
                if not isinstance(element, LTTextContainer):
                    continue
                for line in element:
                    if not isinstance(line, LTTextLine):
                        continue
                    x1, y0, x2, y1 = line.bbox  # pdfminer: bottom-left origin, y up
                    top = page_height - y1
                    bottom = page_height - y0
                    lines.append(((x1, top, x2, bottom), line.get_text()))
        return lines, page_width, page_height

    @staticmethod
    def _text_in_rect(lines: List[Tuple[Tuple[float, float, float, float], str]], rect: Tuple[float, float, float, float]) -> str:
        """Join every pre-flipped (top-left/y-down) line from
        _pdfminer_page_lines that overlaps rect, in reading order."""
        x1, y1, x2, y2 = rect
        selected = [
            (lx1, ly1, text) for (lx1, ly1, lx2, ly2), text in lines
            if lx2 >= x1 and lx1 <= x2 and ly2 >= y1 and ly1 <= y2
        ]
        selected.sort(key=lambda t: (t[1], t[0]))  # top-to-bottom, then left-to-right
        text = "".join(t[2] for t in selected)  # line.get_text() already ends each line in "\n"
        return join_line_break_hyphens(text).strip()

    @staticmethod
    def convert_bounding_boxes(original_size: Tuple[int, int], target_size: Tuple[float, float],
                                bounding_boxes: List[List[float]], offset: int = 3) -> List[Tuple[int, int, int, int]]:
        """Convert bounding boxes from image coordinates to PDF coordinates."""
        original_width, original_height = original_size
        target_width, target_height = target_size

        scale_x = target_width / original_width
        scale_y = target_height / original_height

        return [
            (
                int(bbox[0] * scale_x) - offset,
                int(bbox[1] * scale_y) - offset,
                int(bbox[2] * scale_x) + offset,
                int(bbox[3] * scale_y) + offset,
            )
            for bbox in bounding_boxes
        ]

    # -- Bulk entrypoint: same signature/behavior as the original
    #    PDFFootnoteExtractor.extract_text_from_bounding_boxes --

    def extract_text_from_image_bboxes(self, pdf_path: str, bounding_boxes: List[List[float]],
                                        page_number: int, image_size: Tuple[int, int],
                                        page_image: Optional[Image.Image] = None) -> List[str]:
        """Extract text from specific image-space bounding boxes on a PDF page."""
        if self.use_surya and page_image is not None:
            pad = 5
            cropped_imgs = []
            for bbox in bounding_boxes:
                # OD-detected boxes are always within the page image, but a manually
                # drawn box can extend past the page edge — clamp so the crop rect
                # never inverts (x1<x2, y1<y2), regardless of how far out-of-bounds
                # the input box is.
                x1 = max(0, min(int(bbox[0]) - pad, page_image.width - 1))
                y1 = max(0, min(int(bbox[1]) - pad, page_image.height - 1))
                x2 = max(x1 + 1, min(int(bbox[2]) + pad, page_image.width))
                y2 = max(y1 + 1, min(int(bbox[3]) + pad, page_image.height))
                cropped_imgs.append(page_image.crop((x1, y1, x2, y2)))

            return self._run_surya_ocr(cropped_imgs)
        else:
            lines, page_width, page_height = self._pdfminer_page_lines(pdf_path, page_number)
            if page_width == 0.0 and page_height == 0.0:
                raise ValueError("Invalid page number.")
            pdf_bboxes = self.convert_bounding_boxes(image_size, (page_width, page_height), bounding_boxes)
            return [self._text_in_rect(lines, bbox) for bbox in pdf_bboxes]

    # -- New entrypoint: on-demand OCR for a single region already in PDF-point
    #    coordinates (the space bounding boxes are stored in throughout the
    #    results JSON, and the space the frontend overlay editor works in). --

    def extract_text_for_pdf_bbox(self, pdf_path: str, page_number_1based: int, pdf_bbox: Dict[str, float]) -> str:
        page_index = page_number_1based - 1
        pdf_document = open_pdf(pdf_path)
        try:
            if page_index < 0 or page_index >= len(pdf_document):
                raise ValueError(f"Invalid page number: {page_number_1based}")

            page = pdf_document[page_index]

            if self.use_surya:
                pixmap = page.get_pixmap(dpi=RENDER_DPI)
                page_image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
                image_bbox = [
                    pdf_bbox["x1"] * POINTS_TO_PIXELS,
                    pdf_bbox["y1"] * POINTS_TO_PIXELS,
                    pdf_bbox["x2"] * POINTS_TO_PIXELS,
                    pdf_bbox["y2"] * POINTS_TO_PIXELS,
                ]
                texts = self.extract_text_from_image_bboxes(
                    pdf_path, [image_bbox], page_index,
                    (page_image.width, page_image.height), page_image=page_image
                )
                return texts[0] if texts else ""
            else:
                lines, _page_width, _page_height = self._pdfminer_page_lines(pdf_path, page_index)
                rect = (pdf_bbox["x1"], pdf_bbox["y1"], pdf_bbox["x2"], pdf_bbox["y2"])
                return self._text_in_rect(lines, rect)
        finally:
            pdf_document.close()


OCREngine.pool = InstancePool(OCREngine, 'ocr')
