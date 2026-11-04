#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enhanced PDF to Structured Footnotes Pipeline - Version 2.0

This script combines PDF footnote extraction, cleaning, and NER classification into a single pipeline.
Takes a PDF file as input and outputs structured JSON with hierarchical citation analysis.

NEW FEATURES in v2.0:
- Enhanced ibidem and opus citatum detection
- Author-based matching for opus citatum references
- Improved component inference logic
- Better citation type classification

Pipeline Steps:
1. PDF → Images conversion (high DPI)
2. YOLO-based footnote detection
3. Text extraction from bounding boxes
4. Basic footnote cleaning and merging
5. Advanced sequential footnote splitting
6. NER classification (Reference detection + Component extraction)
7. ENHANCED: Ibidem vs Opus Citatum classification and handling
8. ENHANCED: Author-based component inference for opus citatum
9. Hierarchical structuring (components nested under references)
10. Statistical analysis (optional debug mode)
11. JSON output with comprehensive metadata

Key Features:
- Automatic detection of ibidem references (ibidem, ibid, ib., eadem, ead., tamże)
- Automatic detection of opus citatum references (op. cit., loc. cit., a.a.O., etc.)
- Author-based matching for opus citatum to find source references
- Intelligent component inference from previous references
- Exclusion of already-present components to avoid duplication
- Special handling for page numbers and fundamental citation elements
- Tracking of inferred components in separate array for transparency

Usage:
    python pdf_to_structured_footnotes_enhanced.py input.pdf
    python pdf_to_structured_footnotes_enhanced.py input.pdf --debug
    python pdf_to_structured_footnotes_enhanced.py input.pdf -o output.json
    python pdf_to_structured_footnotes_enhanced.py input.pdf --debug --stats

Arguments:
    input_pdf           Path to the input PDF file
    -o, --output        Output JSON file path (optional, defaults to structured_footnotes/filename.json)
    --debug             Enable debug mode with detailed logs and intermediate files
    --stats             Show detailed statistics (only in debug mode)
    --cpu               Force CPU processing for YOLO and NER models
    --batch-size        Batch size for processing (default: 16)

Output Format:
    - Structured JSON with hierarchical references and components
    - Metadata including processing statistics and model information
    - Optional detailed statistics in debug mode
    - Each reference contains nested components with confidence scores
    - Inferred components marked with 'inferred': true and stored in 'inferred_components' array
    - Citation type classification (ibidem, opus_citatum, normal)

Requirements:
    - YOLO model: E:/footnotes/experiments/YOLOv8_Finetune_medium/training_run/weights/best.pt
    - NER models: E:/footnotes/experiments/NER_Reference_BERT/ and NER_Component_BERT/
"""

import re
import os
import sys
import json
import time
import argparse
import statistics
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime

# PDF and ML libraries
import fitz  # PyMuPDF
import torch
from ultralytics import YOLO
from PIL import Image
from transformers import AutoTokenizer, AutoModelForTokenClassification

# Import enrichment service
try:
    from pipeline.metadata_enrichment import EnrichmentService
except ImportError:
    # Handle if run directly
    from metadata_enrichment import EnrichmentService

# Import shared OCR engine (also used for on-demand single-bbox re-OCR, see app.py)
try:
    from pipeline.ocr_service import OCREngine, open_pdf
except ImportError:
    # Handle if run directly
    from ocr_service import OCREngine, open_pdf

# Import shared NER engine (also used for on-demand reference/component span editing, see app.py)
try:
    from pipeline.ner_service import NEREngine
    from pipeline.reference_spans import repair_reference_spans
except ImportError:
    # Handle if run directly
    from ner_service import NEREngine
    from reference_spans import repair_reference_spans

try:
    from pipeline.model_pool import InstancePool
except ImportError:
    # Handle if run directly
    from model_pool import InstancePool

# Loaded detection checkpoints, by path: a job leases its own copy (see
# process_pdf_sync), as Ultralytics models are not safe to share between
# threads; everything else gets copy 0.
OD_MODELS = InstancePool(lambda path: YOLO(path), 'od')

# Import PDF processing functions from the existing module
sys.path.append(str(Path(__file__).parent.parent / "4_extract_footnote"))

class PDFFootnoteExtractor:
    """Handles PDF processing and footnote extraction"""
    
    def __init__(self, model_path: str, cpu_mode: bool = False, debug: bool = False, save_images: bool = False, save_footnotes: bool = False, od_conf: float = 0.5, use_surya_ocr: bool = True, od_floor_conf: float = 0.05):
        self.cpu_mode = cpu_mode
        self.debug = debug
        self.save_images = save_images
        self.save_footnotes = save_footnotes
        self.od_conf = od_conf
        self.model_path = Path(model_path)
        self.use_surya_ocr = use_surya_ocr
        self.render_dpi = 150

        # Detection runs at od_floor_conf and is filtered to od_conf in Python
        # afterwards, so the sub-threshold boxes survive for the layout prior
        # (see layout_prior.py) instead of being dropped inside Ultralytics.
        # This does not change which boxes become footnotes: Ultralytics' NMS
        # filters by conf, then sorts descending, then runs greedy IoU
        # suppression — a low-confidence box can never suppress a higher one,
        # so the >=od_conf subset of a floor-conf run is identical to running
        # at od_conf directly.
        self.od_floor_conf = min(od_floor_conf, od_conf)

        # Whole-document totals from the most recent extract_footnotes_from_pdf
        # call — batched by nature (one YOLO call per page-batch, one OCR call
        # per page's boxes), so there's no meaningful per-footnote split here;
        # process_pdf reads these right after the call for prediction logging.
        self.last_od_time_seconds = 0.0
        self.last_ocr_time_seconds = 0.0

        # Populated by extract_footnotes_from_pdf: {page_number: {...}} carrying
        # every proposal down to od_floor_conf plus that page's text-layer
        # features. Read by process_pdf and persisted as the job's sidecar.
        self.last_page_proposals = {}

        if self.use_surya_ocr:
            print(f"🔧 Loading Surya OCR models...")
        self.ocr_engine = OCREngine.get_instance(self.use_surya_ocr)
        if self.use_surya_ocr:
            print(f"✅ Surya OCR loaded")

        if not self.model_path.exists():
            raise FileNotFoundError(f"YOLO model not found at {model_path}")

        print(f"🔧 Loading YOLO model...")
        self.model = OD_MODELS.current(str(self.model_path))

        # Small footnotes (a short single line, or a block near the bottom
        # margin) were getting missed: the inference call passed no explicit
        # imgsz, so Ultralytics silently applied its own default (640)
        # regardless of what resolution this specific checkpoint was actually
        # trained at. For a model trained at 1280 (like yolo11n_1280), forcing
        # it down to 640 at inference shrinks an already-small footnote region
        # to a sliver just a few pixels tall — right at the limit of what the
        # model's finest feature stride can represent. Reading each
        # checkpoint's own training imgsz back out and matching inference to
        # it (rather than hardcoding one value for every model) fixes this
        # without regressing the checkpoints that really were trained at 640
        # (confirmed by A/B testing: forcing 1280 on a 640-trained model
        # measurably hurt its recall instead of helping).
        train_args = (getattr(self.model, 'ckpt', None) or {}).get('train_args', {}) or {}
        self.od_imgsz = train_args.get('imgsz', 640)
        print(f"🔧 OD inference imgsz set to {self.od_imgsz} (matching this checkpoint's own training resolution)")

        if self.cpu_mode:
            self.model.to('cpu')
            print("📱 YOLO model loaded (CPU mode)")
        else:
            if torch.cuda.is_available():
                self.model.to('cuda')
                print(f"🚀 YOLO model loaded (GPU mode - {torch.cuda.get_device_name(0)})")
            else:
                self.model.to('cpu')
                print("📱 YOLO model loaded (CPU mode - GPU not available)")
    
    def pdf_to_images(self, pdf_path: str) -> List[Image.Image]:
        """Convert PDF pages to images"""
        if not Path(pdf_path).exists():
            raise FileNotFoundError(f"PDF file not found at {pdf_path}")
        
        print(f"📄 Converting PDF to images...")
        pdf_document = open_pdf(pdf_path)
        images = []

        for page_num in range(len(pdf_document)):
            page = pdf_document[page_num]
            pixmap = page.get_pixmap(dpi=self.render_dpi)
            image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
            images.append(image)
        
        pdf_document.close()
        print(f"📄 Converted {len(images)} pages to images")
        return images
    
    def convert_bounding_boxes(self, original_size: Tuple[int, int], target_size: Tuple[float, float], 
                             bounding_boxes: List[List[float]], offset: int = 3) -> List[Tuple[int, int, int, int]]:
        """Convert bounding boxes from image coordinates to PDF coordinates"""
        original_width, original_height = original_size
        target_width, target_height = target_size

        scale_x = target_width / original_width
        scale_y = target_height / original_height
        
        converted_bboxes = [
            (
                int(bbox[0] * scale_x) - offset,
                int(bbox[1] * scale_y) - offset,
                int(bbox[2] * scale_x) + offset,
                int(bbox[3] * scale_y) + offset,
            )
            for bbox in bounding_boxes
        ]
        return converted_bboxes

    def convert_bounding_boxes_exact(self, original_size: Tuple[int, int], target_size: Tuple[float, float],
                                     bounding_boxes: List[List[float]]) -> List[Tuple[float, float, float, float]]:
        """Image pixels -> PDF points with no inflation and no rounding.

        convert_bounding_boxes() above pads every edge by 3pt and truncates to
        int, which is fine for a box a human will look at but not for fitting a
        layout prior: the observed per-document spread of the left edge is ~1pt,
        i.e. the same order as that distortion. The prior is fitted on these
        exact coordinates instead; footnote bboxes keep using the padded
        conversion so nothing downstream shifts.
        """
        original_width, original_height = original_size
        target_width, target_height = target_size

        scale_x = target_width / original_width
        scale_y = target_height / original_height

        return [
            (bbox[0] * scale_x, bbox[1] * scale_y, bbox[2] * scale_x, bbox[3] * scale_y)
            for bbox in bounding_boxes
        ]

    @staticmethod
    def _page_text_lines(page) -> List[Dict[str, Any]]:
        """Text-layer lines for one page as [{bbox, size, nchars}] in PDF points.

        Empty for a scanned page with no text layer — callers must treat that as
        "no information", never as evidence against a box.
        """
        lines = []
        try:
            data = page.get_text("dict")
        except Exception:
            return lines
        # get_text reports page coordinates, whose origin is not always the
        # rendered page's top-left (a CropBox set in from the MediaBox moves
        # it); boxes are measured from that top-left, so shift to match.
        ox, oy = page.rect.x0, page.rect.y0

        for block in data.get('blocks', []):
            for line in block.get('lines', []):
                spans = [s for s in line.get('spans', []) if (s.get('text') or '').strip()]
                if not spans:
                    continue
                # Char-weighted so a stray one-character superscript in another
                # size doesn't drag the line's size away from its body text.
                nchars = sum(len(s['text']) for s in spans)
                size = statistics.median([
                    float(s.get('size', 0.0)) for s in spans for _ in range(max(1, len(s['text'])))
                ])
                bbox = line.get('bbox')
                if not bbox or size <= 0:
                    continue
                x0, y0, x1, y1 = (float(v) for v in bbox)
                lines.append({'bbox': (x0 - ox, y0 - oy, x1 - ox, y1 - oy), 'size': round(size, 2), 'nchars': nchars})
        return lines

    @staticmethod
    def _weighted_median_size(lines: List[Dict[str, Any]]) -> Optional[float]:
        """Char-weighted median font size over a set of lines, or None if empty."""
        if not lines:
            return None
        sizes = []
        for ln in lines:
            sizes.extend([ln['size']] * max(1, ln['nchars']))
        return round(statistics.median(sizes), 2)

    @staticmethod
    def _lines_inside(lines: List[Dict[str, Any]], bbox: Tuple[float, float, float, float],
                      min_overlap: float = 0.6) -> List[Dict[str, Any]]:
        """Lines whose vertical extent is mostly inside bbox and which overlap it horizontally."""
        x1, y1, x2, y2 = bbox
        hits = []
        for ln in lines:
            lx1, ly1, lx2, ly2 = ln['bbox']
            lh = ly2 - ly1
            if lh <= 0:
                continue
            v = max(0.0, min(y2, ly2) - max(y1, ly1)) / lh
            h = max(0.0, min(x2, lx2) - max(x1, lx1))
            if v >= min_overlap and h > 0:
                hits.append(ln)
        return hits

    def extract_text_from_bounding_boxes(self, pdf_path: str, bounding_boxes: List[List[float]],
                                       page_number: int, image_size: Tuple[int, int], page_image: Image.Image = None) -> List[str]:
        """Extract text from specific bounding boxes on a PDF page"""
        return self.ocr_engine.extract_text_from_image_bboxes(pdf_path, bounding_boxes, page_number, image_size, page_image)
    
    def basic_footnote_cleaning(self, footnotes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Basic cleaning of extracted footnote text"""
        if not footnotes:
            return []

        cleaned_footnotes = []
        
        for fn in footnotes:
            text_block = fn['text']
            if not text_block.strip():
                continue
                
            block = text_block.strip()
            
            # Process line by line to preserve newlines
            lines = []
            for line in block.split('\n'):
                line = line.strip()
                if not line:
                    continue
                
                # Fix split numbers and ensure space after footnote numbers
                line = re.sub(r'\b(19|20)\s+(\d{2})\b', r'\1\2', line)
                line = re.sub(r'\b(\d)\s+(\d)\s+(\d)\s+(\d)\b', r'\1\2\3\4', line)
                line = re.sub(r'^(\d+)([A-Z])', r'\1 \2', line)
                line = re.sub(r'^(\d+)([a-z])', r'\1 \2', line)
                
                # Clean up spacing within the line
                line = re.sub(r'\s+', ' ', line)
                lines.append(line.strip())
            
            fn_copy = fn.copy()
            fn_copy['text'] = "\n".join(lines)
            cleaned_footnotes.append(fn_copy)
        
        return cleaned_footnotes
    
    def extract_footnotes_from_pdf(self, pdf_path: str, batch_size: int = 16) -> List[Dict[str, Any]]:
        """Complete PDF to footnotes extraction pipeline"""
        # Convert PDF to images
        images = self.pdf_to_images(pdf_path)
        
        # Create output directories if saving is requested
        pdf_name = Path(pdf_path).stem
        debug_dir = None
        footnotes_dir = None
        
        if self.save_images or self.save_footnotes:
            output_base = Path(__file__).parent / 'debug_output' / pdf_name
            if self.save_images:
                debug_dir = output_base / 'debug_images'
                debug_dir.mkdir(parents=True, exist_ok=True)
                print(f"📁 Debug images will be saved to: {debug_dir}")
            if self.save_footnotes:
                footnotes_dir = output_base / 'extracted_footnotes'
                footnotes_dir.mkdir(parents=True, exist_ok=True)
                print(f"📁 Extracted footnotes will be saved to: {footnotes_dir}")
        
        # YOLO detection
        print(f"🔍 Running YOLO footnote detection...")
        all_results = []
        od_time_total = 0.0

        for i in range(0, len(images), batch_size):
            batch_end = min(i + batch_size, len(images))
            batch_images = images[i:batch_end]

            if len(images) > batch_size:
                print(f"Processing batch: images {i+1}-{batch_end}")

            if not self.cpu_mode and torch.cuda.is_available():
                torch.cuda.empty_cache()

            od_batch_start = time.perf_counter()
            # max_det pinned rather than left to the Ultralytics default so an
            # upstream change can't silently truncate the proposal set.
            batch_results = self.model(batch_images, conf=self.od_floor_conf, iou=0.45,
                                       imgsz=self.od_imgsz, augment=False, max_det=100)
            od_time_total += time.perf_counter() - od_batch_start
            all_results.extend(batch_results)

        self.last_od_time_seconds = round(od_time_total, 4)
        print(f"🔍 YOLO detection completed on {len(all_results)} pages")

        # Extract text from detected regions
        print(f"📝 Extracting text from detected footnotes...")
        raw_footnotes = []
        ocr_time_total = 0.0

        # One handle for the whole loop — this used to be opened and closed once
        # per page with detections.
        pdf_document = open_pdf(pdf_path)
        self.last_page_proposals = {}

        for i, result in enumerate(all_results):
            page = pdf_document[i]
            page_size = (page.rect.width, page.rect.height)

            # Everything down to od_floor_conf, then the od_conf subset that
            # actually becomes footnotes. Sorted so proposal ids are stable.
            all_boxes = sorted(
                ((r.xyxy.tolist()[0], float(r.conf.item())) for r in result.boxes),
                key=lambda t: -t[1]
            )
            accepted = [(b, c) for b, c in all_boxes if c >= self.od_conf]
            bounding_boxes = [b for b, _ in accepted]
            confidences = [c for _, c in accepted]

            # Text-layer features, used both for the sidecar and to annotate
            # each proposal. Skipped entirely on pages with no proposals.
            if all_boxes:
                lines = self._page_text_lines(page)
                exact = self.convert_bounding_boxes_exact(images[i].size, page_size,
                                                          [b for b, _ in all_boxes])
                proposals = []
                for idx, ((_, conf), ebox) in enumerate(zip(all_boxes, exact)):
                    inside = self._lines_inside(lines, ebox)
                    proposals.append({
                        'id': f'p{i + 1}_{idx}',
                        'bbox': [round(v, 2) for v in ebox],
                        'conf': round(conf, 4),
                        'font': self._weighted_median_size(inside),
                        'lines_inside': len(inside),
                        'accepted': conf >= self.od_conf,
                        'footnote_ids': [],
                    })
                self.last_page_proposals[i + 1] = {
                    'w': round(page_size[0], 2),
                    'h': round(page_size[1], 2),
                    'has_text': bool(lines),
                    'body_font': self._weighted_median_size(lines),
                    'line_count': len(lines),
                    'proposals': proposals,
                }

            # Save debug image for ALL pages if requested (with or without detections)
            if self.save_images and debug_dir:
                self._save_debug_image(images[i], bounding_boxes, debug_dir, f"page_{i+1:03d}.jpg", confidences)

            if not bounding_boxes:
                if self.debug:
                    print(f"Page {i + 1}: No footnotes detected")
                continue

            if self.debug:
                print(f"Page {i + 1}: {len(bounding_boxes)} footnote regions detected")

            try:
                ocr_call_start = time.perf_counter()
                texts = self.extract_text_from_bounding_boxes(pdf_path, bounding_boxes, i, images[i].size, images[i])
                ocr_time_total += time.perf_counter() - ocr_call_start

                pdf_bboxes = self.convert_bounding_boxes(images[i].size, page_size, bounding_boxes)

                for text, bbox, conf in zip(texts, pdf_bboxes, confidences):
                    raw_footnotes.append({
                        "page": i + 1,
                        "bbox": {"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3]},
                        "confidence": conf,
                        "text": text
                    })

            except Exception as e:
                # Never drop a page's footnotes silently: the job fails
                # with the reason, and can be processed again.
                pdf_document.close()
                raise RuntimeError(f"OCR failed on page {i + 1}: {e}") from e

        pdf_document.close()

        self.last_ocr_time_seconds = round(ocr_time_total, 4)

        if not raw_footnotes:
            print("⚠️ No footnotes were extracted from the PDF.")
            return []
        
        print(f"📝 Successfully extracted text from {len(raw_footnotes)} footnote regions")
        
        # Save all footnotes to a single file if requested
        if self.save_footnotes and footnotes_dir:
            all_footnotes_file = footnotes_dir / "all_footnotes.txt"
            with open(all_footnotes_file, 'w', encoding='utf-8') as f:
                for idx, fn in enumerate(raw_footnotes, 1):
                    f.write(f"=== Footnote {idx} (Page {fn['page']}) ===\n")
                    f.write(fn['text'] + "\n\n")
            print(f"💾 All footnotes saved to: {all_footnotes_file}")
        
        # Basic cleaning
        print(f"🧹 Applying basic footnote cleaning...")
        cleaned_footnotes = self.basic_footnote_cleaning(raw_footnotes)
        
        return cleaned_footnotes
    
    def _save_debug_image(self, image, bounding_boxes, output_dir, filename, confidences=None):
        """Save debug image with bounding boxes drawn (if any) and confidence scores"""
        import cv2
        import numpy as np
        
        # Convert PIL image to OpenCV format
        img_array = np.array(image)
        img_bgr = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
        
        # Draw bounding boxes only if they exist
        if bounding_boxes:
            for i, bbox in enumerate(bounding_boxes):
                x1, y1, x2, y2 = [int(coord) for coord in bbox]
                cv2.rectangle(img_bgr, (x1, y1), (x2, y2), (0, 255, 0), 2)
                
                # Add confidence score if available
                if confidences and i < len(confidences):
                    conf_text = f'Footnote ({confidences[i]:.2f})'
                    cv2.putText(img_bgr, conf_text, (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                else:
                    cv2.putText(img_bgr, 'Footnote', (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # Save image (with or without bounding boxes)
        output_path = output_dir / filename
        cv2.imwrite(str(output_path), img_bgr)
    
    def _save_footnote_texts(self, texts, output_dir, filename):
        """Save footnote texts to file"""
        output_path = output_dir / filename
        with open(output_path, 'w', encoding='utf-8') as f:
            for idx, text in enumerate(texts, 1):
                f.write(f"=== Footnote {idx} ===\n")
                f.write(text + "\n\n")

class EnhancedFootnoteNERClassifier:
    """Enhanced NER classifier with improved ibidem and opus citatum handling"""
    
    def __init__(self, yolo_conf=0.5, ref_conf=0.7, comp_conf=0.7,
                 yolo_model_path="models/yolo/best.pt",
                 ner_suite_path="models/ner/mbert_production",
                 is_nested=False,
                 debug=False, save_images=False, save_footnotes=False,
                 split_footnotes=True, enable_ibidem_handling=True, split_multi_work=False):
        """Initialize the enhanced pipeline with multiple confidence thresholds"""
        self.debug = debug
        self.save_images = save_images
        self.save_footnotes = save_footnotes
        # When False, process_footnotes() skips the marker-based re-segmentation
        # and treats each detected region as a single footnote.
        self.split_footnotes = split_footnotes
        # When False, process_footnotes() skips handle_enhanced_reference_inference
        # entirely — citation_type is still classified (ibidem/opus_citatum/normal,
        # a cheap regex+model call), but no components get copied forward from a
        # matching earlier reference. Off by user request when that inference
        # misattributes components on a given document.
        self.enable_ibidem_handling = enable_ibidem_handling
        # When True, process_footnotes() cuts a reference span that holds
        # several works ("Martines 2009, 2012") into one reference per work
        # (pipeline/reference_split.py). An upload option, on by default.
        self.split_multi_work = split_multi_work
        self.references_split = 0
        self.yolo_conf = yolo_conf
        self.ref_conf = ref_conf
        self.comp_conf = comp_conf
        self.ner_suite_path = ner_suite_path
        self.is_nested = is_nested
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        print(f"🧠 Using device for NER: {self.device}")

        self.ner_engine = NEREngine.get_instance(self.ner_suite_path, self.is_nested)

        # Convenience aliases so existing code below (classify_footnote etc.)
        # that references self.ref_model/self.comp_model/self.names_model/... keeps working unchanged.
        self.ref_model = self.ner_engine.ref_model
        self.ref_tokenizer = self.ner_engine.ref_tokenizer
        self.ref_id2label = self.ner_engine.ref_id2label
        self.comp_model = self.ner_engine.comp_model
        self.comp_tokenizer = self.ner_engine.comp_tokenizer
        self.comp_id2label = self.ner_engine.comp_id2label
        self.names_model = self.ner_engine.names_model
        self.names_tokenizer = getattr(self.ner_engine, 'names_tokenizer', None)
        self.names_id2label = getattr(self.ner_engine, 'names_id2label', None)

        self.ibidem_patterns = self.ner_engine.ibidem_patterns
        self.opus_citatum_patterns = self.ner_engine.opus_citatum_patterns

    def classify_citation_type(self, text: str) -> str:
        """Classify citation as ibidem, opus_citatum, or normal"""
        return self.ner_engine.classify_citation_type(text)

    def split_long_text(self, text: str, max_chars: int = 1000) -> List[Tuple[int, int]]:
        """Split text into chunks by punctuation to avoid cutting tokens"""
        return self.ner_engine.split_long_text(text, max_chars)

    def extract_entities(self, text, model, tokenizer, id2label, conf_threshold=0.8):
        """Extract entities from text, handling long texts by chunking"""
        return self.ner_engine.extract_entities(text, model, tokenizer, id2label, conf_threshold)

    def extract_authors_from_components(self, components: List[Dict[str, Any]]) -> List[str]:
        """Extract author names from components"""
        authors = []
        for comp in components:
            if comp['label'] == 'Author':
                # Clean and normalize author names
                author_text = comp['text'].strip()
                # Remove common prefixes/suffixes
                author_text = re.sub(r'^(von|van|de|del|della|du)\s+', '', author_text, flags=re.IGNORECASE)
                authors.append(author_text.lower())
        return authors
    
    def find_matching_reference_by_author(self, current_authors: List[str], 
                                        reference_chain: List[Tuple[int, int, Dict]]) -> Optional[Dict]:
        """Find the most recent reference with matching author"""
        if not current_authors:
            return None
        
        # Look through reference chain in reverse (most recent first)
        for ref_footnote_idx, ref_reference_idx, ref_data in reversed(reference_chain):
            ref_authors = self.extract_authors_from_components(ref_data['components'])
            
            # Check for author matches (case-insensitive, partial matching)
            for current_author in current_authors:
                for ref_author in ref_authors:
                    # Simple fuzzy matching - check if names overlap significantly
                    current_parts = set(current_author.split())
                    ref_parts = set(ref_author.split())
                    
                    # If there's significant overlap in name parts, consider it a match
                    if len(current_parts & ref_parts) >= min(len(current_parts), len(ref_parts)) * 0.5:
                        if self.debug:
                            print(f"   🎯 Author match found: '{current_author}' ≈ '{ref_author}' in footnote {ref_footnote_idx+1}")
                        return ref_data
        
        return None
    
    def classify_footnote(self, text: str) -> Dict[str, Any]:
        """Classify a single footnote with hierarchical structure and individual reference citation types"""
        # Get references
        references = self.extract_entities(text, self.ref_model, self.ref_tokenizer, self.ref_id2label, self.ref_conf)
        # a page locator the model left on its own goes back into its
        # reference before the parts are tagged (pipeline/reference_spans.py)
        references, _ = repair_reference_spans(text, references)

        # If full context, extract components from the entire footnote text at once
        components_full = []
        if not self.is_nested:
            components_full = self.extract_entities(text, self.comp_model, self.comp_tokenizer, self.comp_id2label, self.comp_conf)
            
        structured_references = []
        total_components_count = 0
        
        for ref in references:
            ref_with_components = {
                'text': ref['text'],
                'confidence': ref['confidence'],
                'start': ref['start'],
                'end': ref['end'],
                'components': []
            }
            
            ref_components = []
            
            if self.is_nested:
                # Nested mode: pass only the reference text to the component model
                local_components = self.extract_entities(ref['text'], self.comp_model, self.comp_tokenizer, self.comp_id2label, self.comp_conf)
                
                # Adjust indices to be global relative to the full text
                for comp in local_components:
                    comp['start'] += ref['start']
                    comp['end'] += ref['start']
                    ref_components.append(comp)
            else:
                # Full context mode: find components that overlap with this reference
                for comp in components_full:
                    if (comp['start'] >= ref['start'] and comp['end'] <= ref['end']) or \
                       (comp['start'] < ref['end'] and comp['end'] > ref['start']):
                        ref_components.append(comp)
            
            for comp in ref_components:
                comp_dict = {
                    'label': comp['label'],
                    'text': comp['text'],
                    'confidence': comp['confidence'],
                    'start': comp['start'],
                    'end': comp['end']
                }
                
                # Extract names specifically for Author, Editor, and Translator
                if self.names_model is not None and comp['label'] in ['Author', 'Editor', 'Translator']:
                    comp_names = self.extract_entities(comp['text'], self.names_model, self.names_tokenizer, self.names_id2label, self.comp_conf)
                    if comp_names:
                        comp_dict['names'] = []
                        for name in comp_names:
                            comp_dict['names'].append({
                                'label': name['label'],
                                'text': name['text'],
                                'confidence': name['confidence'],
                                'start': comp['start'] + name['start'],
                                'end': comp['start'] + name['end']
                            })
                            
                ref_with_components['components'].append(comp_dict)
                total_components_count += 1
            
            # Classify citation type for each individual reference
            ref_citation_type = self.classify_citation_type(ref['text'])
            ref_with_components['citation_type'] = ref_citation_type
            
            structured_references.append(ref_with_components)
        
        return {
            'text': text.strip(),
            'references': structured_references,
            'summary': {
                'ref_count': len(structured_references),
                'total_components': len(components_full) if not self.is_nested else total_components_count,
                'refs_with_components': len([r for r in structured_references if r['components']])
            }
        }
    
    def prepare_footnotes_for_processing(self, footnotes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Split each DETECTED region into footnotes, rejoining page-break continuations.

        Two rules, in this order:

        1. Marker numbering only ever decides where to *cut a region up*. It
           never decides whether a region survives - a detection the OD made
           and the OCR read is a footnote, whatever its number looks like.
        2. A chunk that opens a region with no marker of its own is the tail of
           the footnote before it - its text is appended to that footnote and
           its region's rectangle recorded as another span of it - when that
           footnote ends on the same or the previous page and either reads
           unfinished (no sentence-final punctuation), or the chunk starts in
           lower case, or the region's first marker is the very number expected
           next, so nothing else can have begun before it. Otherwise it is a
           footnote of its own (an unnumbered note, a caption, a marker the OCR
           lost).

        The numbering runs across regions: the last marker seen sets the
        number expected next, which split_region_text uses to choose a
        region's markers.

        Every footnote therefore carries a 'spans' list of {page, bbox} - one
        per rectangle it physically occupies. 'page' and 'bbox' remain the
        first span so nothing downstream has to change, but the geometry of a
        continuation page is no longer thrown away, which is what both the old
        splitter and the manual merge used to do.

        The old implementation ran one accumulator across the whole document,
        flushing only when a marker landed inside a narrow window of the
        expected next number; a line that missed the window was appended to
        the running footnote and its region's page and bbox discarded. One
        stray "1" at a line start reset the counter to 2, after which every
        real marker in a continuously-numbered book missed the window forever.
        On a real 140-page book that swallowed 29 of 70 detected regions into
        a single 22,894-character footnote pinned to page 81.
        """
        prepared = []
        last_number = None

        for fn in footnotes:
            span = {'page': fn.get('page'), 'bbox': fn.get('bbox')}
            expected = last_number + 1 if last_number is not None else None
            chunks = split_region_text(fn.get('text'), expected)
            first_number = next((c['number'] for c in chunks if c['number'] is not None), None)

            for index, chunk in enumerate(chunks):
                text = chunk['text']
                if chunk['number'] is not None:
                    last_number = chunk['number']

                if (index == 0 and not chunk['marker'] and prepared
                        and self._continues_previous(prepared[-1], span)
                        and (_OPEN_ENDING.search(prepared[-1]['text']) or text[:1].islower()
                             or (expected is not None and first_number == expected))):
                    previous = prepared[-1]
                    previous['text'] = (previous['text'] + ' ' + text).strip()
                    if span['bbox'] is not None:
                        previous['spans'].append(dict(span))
                    continue

                prepared.append({
                    'page': span['page'],
                    'bbox': span['bbox'],
                    'confidence': fn.get('confidence'),
                    'text': text,
                    'spans': [dict(span)],
                })

        return prepared

    @staticmethod
    def _continues_previous(previous: Dict[str, Any], span: Dict[str, Any]) -> bool:
        """Is this markerless region the tail of the footnote before it?

        Only when it sits on the same page as that footnote's last rectangle or
        the very next one. Without the adjacency check a markerless region
        anywhere in the book would glue itself onto whatever came before, which
        is how the previous implementation produced a footnote spanning
        fourteen pages.
        """
        page = span.get('page')
        spans = previous.get('spans') or []
        if page is None or not spans:
            return False
        last_page = spans[-1].get('page')
        if last_page is None:
            return False
        return last_page <= page <= last_page + 1

    def process_footnotes(self, footnotes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Process all footnotes and return structured results"""
        import uuid
        # Clean and prepare footnotes
        if self.split_footnotes:
            print(f"📝 Preparing footnotes for processing...")
            prepared_footnotes = self.prepare_footnotes_for_processing(footnotes)
        else:
            # Splitting disabled: one detected region == one footnote, keeping the
            # region's own page/bbox. prepare_footnotes_for_processing() re-segments
            # regions by leading footnote numbers, which assumes numbering is
            # contiguous across the document; when it isn't (e.g. a PDF built from
            # non-adjacent sampled pages), out-of-window numbers are appended to the
            # running footnote instead of starting a new one, collapsing many
            # footnotes into one blob carrying the first region's page/bbox.
            print(f"📝 Footnote splitting disabled — using {len(footnotes)} detected regions as-is")
            prepared_footnotes = [dict(fn, spans=[{'page': fn.get('page'), 'bbox': fn.get('bbox')}])
                                  for fn in footnotes if fn.get('text', '').strip()]
        
        if not prepared_footnotes:
            return []
        
        print(f"🏷️ Classifying {len(prepared_footnotes)} footnotes with enhanced NER models...")
        
        results = []
        for i, fn_dict in enumerate(prepared_footnotes, 1):
            if i % 100 == 0:
                print(f"Progress: {i}/{len(prepared_footnotes)} footnotes processed...")
            
            ner_call_start = time.perf_counter()
            result = self.classify_footnote(fn_dict['text'])
            # One classify_footnote() call produces every reference/component
            # for this footnote together, so this is the finest-grained timing
            # available — shared across all of them when logged, unlike the
            # OD/OCR steps above which only have a whole-document total.
            result['inference_time_seconds'] = round(time.perf_counter() - ner_call_start, 4)
            result['id'] = str(uuid.uuid4())
            result['page'] = fn_dict.get('page')
            result['bbox'] = fn_dict.get('bbox')
            result['confidence'] = fn_dict.get('confidence')
            # Every rectangle this footnote occupies. A footnote continuing over
            # a page break has more than one; page/bbox above stay the first, so
            # anything reading only those is unaffected. Carried through
            # explicitly because this loop rebuilds the footnote from a fixed
            # set of keys rather than copying fn_dict.
            result['spans'] = fn_dict.get('spans') or [{
                'page': fn_dict.get('page'), 'bbox': fn_dict.get('bbox')}]

            results.append(result)
        
        # Several works in one reference span become one reference each, before
        # the ibidem pass, so an "ibid." that follows resolves to the last work.
        self.references_split = 0
        if self.split_multi_work:
            from pipeline.reference_split import split_footnote_references
            self.references_split = sum(split_footnote_references(r, classify=self.classify_citation_type)
                                        for r in results)
            print(f"✂️ Split {self.references_split} references holding several works")

        # Handle ibidem and opus citatum references with enhanced logic
        if self.enable_ibidem_handling:
            print(f"🔄 Processing ibidem and opus citatum references with enhanced logic...")
            results = self.handle_enhanced_reference_inference(results)
        else:
            print(f"⏭️ Ibidem handling disabled — leaving ibidem/opus citatum references without inferred components")

        return results
    
    def handle_enhanced_reference_inference(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Enhanced handling of ibidem and opus citatum references.
        - Ibidem: Copy components from most recent reference
        - Opus citatum: Match by author and copy components from matching reference
        """
        
        # Build a list of all normal references from model predictions
        reference_chain = []  # [(footnote_index, reference_index, reference_data), ...]
        
        for i, current_result in enumerate(results):
            # Add each individual NORMAL reference to the chain
            for ref_idx, ref in enumerate(current_result['references']):
                if ref.get('citation_type') == 'normal':
                    reference_chain.append((i, ref_idx, ref))
                    if self.debug:
                        print(f"📚 Added to reference chain: footnote {i+1}, reference {ref_idx+1} (normal)")
        
        if self.debug:
            print(f"📊 Built reference chain with {len(reference_chain)} normal references")
        
        # Process special citation types
        ibidem_processed = 0
        opus_citatum_processed = 0
        
        for i, current_result in enumerate(results):
            # Process each reference in this footnote
            for ref_idx, current_ref in enumerate(current_result['references']):
                citation_type = current_ref.get('citation_type', 'normal')
                
                if citation_type == 'ibidem':
                    if self.debug:
                        print(f"📍 Processing ibidem in footnote {current_result['id']}, reference {ref_idx+1}: {current_ref['text']}")
                    
                    # Find the most recent normal reference from the reference chain
                    previous_reference = None
                    for ref_footnote_idx, ref_reference_idx, ref_data in reversed(reference_chain):
                        # Must be before current position
                        if ref_footnote_idx < i or (ref_footnote_idx == i and ref_reference_idx < ref_idx):
                            previous_reference = ref_data
                            if self.debug:
                                print(f"   🔗 Linking to most recent reference from footnote {ref_footnote_idx+1}")
                            break
                    
                    if previous_reference:
                        inferred_components = self.infer_components_from_reference(
                            current_ref, previous_reference, "ibidem"
                        )
                        self.apply_inferred_components(current_result, current_ref, inferred_components)
                        ibidem_processed += 1
                        if self.debug:
                            print(f"   ✅ Ibidem: Inferred {len(inferred_components)} components")
                    else:
                        if self.debug:
                            print(f"   ❌ Ibidem: No previous normal reference found")
                
                elif citation_type == 'opus_citatum':
                    if self.debug:
                        print(f"📍 Processing opus citatum in footnote {current_result['id']}, reference {ref_idx+1}: {current_ref['text']}")
                    
                    # Extract authors from current reference components
                    current_authors = self.extract_authors_from_components(current_ref['components'])
                    
                    if current_authors:
                        # Find matching reference by author
                        matching_reference = self.find_matching_reference_by_author(current_authors, reference_chain)
                        
                        if matching_reference:
                            inferred_components = self.infer_components_from_reference(
                                current_ref, matching_reference, "opus_citatum"
                            )
                            self.apply_inferred_components(current_result, current_ref, inferred_components)
                            opus_citatum_processed += 1
                            if self.debug:
                                print(f"   ✅ Opus citatum: Inferred {len(inferred_components)} components by author match")
                        else:
                            if self.debug:
                                print(f"   ❌ Opus citatum: No matching author found in reference chain")
                    else:
                        if self.debug:
                            print(f"   ❌ Opus citatum: No authors detected in current reference")
        
        if self.debug:
            print(f"📈 Enhanced inference complete: {ibidem_processed} ibidem, {opus_citatum_processed} opus citatum processed")
        
        return results
    
    def infer_components_from_reference(self, current_ref: Dict[str, Any], 
                                      source_reference: Dict[str, Any], 
                                      inference_type: str) -> List[Dict[str, Any]]:
        """Infer components from source reference, excluding those already present"""
        current_text = current_ref['text'].lower()
        current_components = current_ref['components']
        
        # Get existing component labels and their text
        existing_component_types = set()
        existing_component_texts = set()
        
        for comp in current_components:
            existing_component_types.add(comp['label'])
            existing_component_texts.add(comp['text'].lower())
        
        # Infer components from source reference
        inferred_components = []
        
        for source_comp in source_reference['components']:
            component_text_lower = source_comp['text'].lower()
            component_label = source_comp['label']
            
            # Skip if this component type already exists in current reference
            if component_label in existing_component_types:
                continue
            
            # Skip if similar text already exists in current reference
            if component_text_lower in existing_component_texts:
                continue
            
            # Skip if the component text appears in the current reference text
            # (means it was not detected by NER but is actually there)
            if component_text_lower in current_text:
                continue
            
            # Component inclusion logic based on inference type and component type
            should_include = False
            
            if inference_type == "ibidem":
                # For ibidem, infer most components except those already present
                if component_label in ['Author', 'Title', 'Journal', 'Publisher', 'PublicationYear', 
                                     'PublicationPlace', 'Volume', 'Issue', 'Pages']:
                    should_include = True
            
            elif inference_type == "opus_citatum":
                # For opus citatum, be more conservative - mainly structural elements
                if component_label in ['Title', 'Journal', 'Publisher', 'PublicationYear', 
                                     'PublicationPlace', 'Volume', 'Issue', 'Pages']:
                    should_include = True
                # Don't infer Author for opus citatum (should already be present)
            
            if should_include:
                inferred_components.append({
                    'label': component_label,
                    'text': source_comp['text'],
                    'confidence': source_comp['confidence'],
                    'inferred': True,
                    'inference_type': inference_type
                })
        
        return inferred_components
    
    def apply_inferred_components(self, result: Dict[str, Any], current_ref: Dict[str, Any], 
                                inferred_components: List[Dict[str, Any]]):
        """Apply inferred components to the result"""
        if not inferred_components:
            return
        
        # Add inferred_components field to track what was inferred
        if 'inferred_components' not in result:
            result['inferred_components'] = []
        result['inferred_components'].extend(inferred_components)
        
        # Create components_inferred with all components
        all_components = []
        # Add existing detected components
        for comp in current_ref['components']:
            all_components.append(comp)
        # Add inferred components
        all_components.extend(inferred_components)
        
        current_ref['components_inferred'] = sorted(all_components, key=lambda x: x['label'])
        
        # Update summary (now based on components_inferred when available)
        result['summary']['total_components'] = sum(
            len(ref.get('components_inferred', ref['components'])) for ref in result['references']
        )
        result['summary']['refs_with_components'] = sum(
            1 for ref in result['references'] 
            if ref.get('components_inferred', ref['components'])
        )
    
    def generate_statistics(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate comprehensive statistics including citation type analysis"""
        if not self.debug:
            return {}
            
        stats = {
            'overview': {},
            'references': {},
            'components': {},
            'quality': {},
            'citation_completeness': {},
            'hierarchical_analysis': {},
            'citation_type_analysis': {},
            'inference_analysis': {}
        }
        
        total_footnotes = len(results)
        total_refs = sum(r['summary']['ref_count'] for r in results)
        total_comps = sum(r['summary']['total_components'] for r in results)
        refs_with_comps = sum(r['summary']['refs_with_components'] for r in results)
        
        # Count citation types from individual references
        citation_types = {'normal': 0, 'ibidem': 0, 'opus_citatum': 0}
        footnotes_with_inference = sum(1 for r in results if 'inferred_components' in r)
        total_inferred_comps = sum(len(r.get('inferred_components', [])) for r in results)
        
        for result in results:
            for ref in result['references']:
                citation_type = ref.get('citation_type', 'normal')
                citation_types[citation_type] = citation_types.get(citation_type, 0) + 1
        
        stats['overview'] = {
            'total_footnotes': total_footnotes,
            'total_references': total_refs,
            'total_components': total_comps,
            'total_inferred_components': total_inferred_comps,
            'avg_refs_per_footnote': total_refs / total_footnotes if total_footnotes > 0 else 0,
            'avg_comps_per_footnote': total_comps / total_footnotes if total_footnotes > 0 else 0,
            'footnotes_with_refs': sum(1 for r in results if r['summary']['ref_count'] > 0),
            'references_with_components': refs_with_comps,
            'component_coverage_rate': refs_with_comps / total_refs * 100 if total_refs > 0 else 0,
            'footnotes_with_inference': footnotes_with_inference,
        }
        
        # Citation type analysis (now based on individual references)
        total_references = sum(citation_types.values())
        stats['citation_type_analysis'] = {
            'normal_citations': citation_types['normal'],
            'ibidem_citations': citation_types['ibidem'],
            'opus_citatum_citations': citation_types['opus_citatum'],
            'normal_percentage': citation_types['normal'] / total_references * 100 if total_references > 0 else 0,
            'ibidem_percentage': citation_types['ibidem'] / total_references * 100 if total_references > 0 else 0,
            'opus_citatum_percentage': citation_types['opus_citatum'] / total_references * 100 if total_references > 0 else 0,
        }
        
        # Component analysis
        all_components = []
        inferred_components = []
        inference_types = {}
        
        for result in results:
            for ref in result['references']:
                # Use components_inferred if available, otherwise use components
                components_to_analyze = ref.get('components_inferred', ref['components'])
                all_components.extend(components_to_analyze)
                
                # Track specifically inferred components
                for comp in components_to_analyze:
                    if comp.get('inferred', False):
                        inferred_components.append(comp)
                        inf_type = comp.get('inference_type', 'unknown')
                        inference_types[inf_type] = inference_types.get(inf_type, 0) + 1
        
        comp_types = {}
        inferred_comp_types = {}
        confidence_scores = []
        
        for comp in all_components:
            comp_type = comp['label']
            confidence_scores.append(comp['confidence'])
            comp_types[comp_type] = comp_types.get(comp_type, 0) + 1
        
        for comp in inferred_components:
            comp_type = comp['label']
            inferred_comp_types[comp_type] = inferred_comp_types.get(comp_type, 0) + 1
        
        sorted_comp_types = dict(sorted(comp_types.items(), key=lambda x: x[1], reverse=True))
        sorted_inferred_comp_types = dict(sorted(inferred_comp_types.items(), key=lambda x: x[1], reverse=True))
        
        stats['components'] = {
            'type_distribution': sorted_comp_types,
            'inferred_type_distribution': sorted_inferred_comp_types,
            'unique_types': len(comp_types),
            'most_common_type': max(comp_types.items(), key=lambda x: x[1])[0] if comp_types else None,
        }
        
        # Inference analysis
        stats['inference_analysis'] = {
            'total_inferred_components': len(inferred_components),
            'inference_type_distribution': inference_types,
            'avg_inferred_per_footnote': len(inferred_components) / footnotes_with_inference if footnotes_with_inference > 0 else 0,
            'most_inferred_component_type': max(inferred_comp_types.items(), key=lambda x: x[1])[0] if inferred_comp_types else None
        }
        
        # Quality statistics
        if confidence_scores:
            stats['quality'] = {
                'avg_confidence': sum(confidence_scores) / len(confidence_scores),
                'median_confidence': sorted(confidence_scores)[len(confidence_scores)//2],
            }
        
        return stats

# A leading number, optionally bracketed ("[4]") or closed with a parenthesis
# ("4)", common in German books), followed by a separator - or nothing, when a
# PDF text layer gives the superscript marker a line of its own.
_MARKER_PATTERN = re.compile(r'^\[?(\d{1,3})[\])]?(?:[\.\s]+|$)(.*)')
# A line holding nothing but a number: a superscript marker, or the tail of the
# line above (a page, "102.").
_LONE_NUMBER = re.compile(r'^\[?(\d{1,3})[\])]?\.?$')
# Footnote numbers reach a few hundred at most, so anything larger is a year, a
# page number or an amount rather than a marker.
_MAX_MARKER = 999
# Markers inside one region are consecutive, so only a small forward jump is a
# plausible next footnote. Bounds over-splitting; can never drop anything.
_MAX_GAP = 20
# "2. 6. 2021" at a line start is a date, not footnote 2.
_DATE_AFTER_MARKER = re.compile(r'^\d{1,2}\.\s*\d{1,4}\b')
# Unnumbered notes: an author's or translator's "*" note, or a figure caption's
# arrow. A region opening with one did not begin with the previous footnote's
# tail; an arrow starts a new note wherever it stands.
_SYMBOL_MARKER = re.compile(r'^(?:\*{1,3}|[†‡§¶•]|[←↑→↓↖↗↘↙])\s*\S')
_ARROW_MARKER = re.compile(r'^[←↑→↓↖↗↘↙]')
_LONE_SYMBOL = re.compile(r'^(?:\*{1,3}|[†‡§¶•←↑→↓↖↗↘↙])$')
# Superscript digits and ^12^ carets, as some OCR engines write a marker.
_SUPERSCRIPT_DIGITS = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹', '0123456789')
_LEADING_SUPERSCRIPT = re.compile(r'^[⁰¹²³⁴⁵⁶⁷⁸⁹]+')
_CARET_MARKER = re.compile(r'^\^(\d{1,3})\^')
# Text that does not end a sentence: a footnote cut off at the page break.
_OPEN_ENDING = re.compile(r'[^.!?»”")\]]\s*$')


def _region_lines(text: Optional[str]) -> List[str]:
    """A region's non-empty lines, stripped, with superscript and caret markers
    turned into plain numbers. A lone number (or lone "*", arrow) is joined to
    the line after it, unless that line opens with a marker itself - then the
    number ends the text above it instead."""
    raw = []
    for line in (text or '').split('\n'):
        line = line.strip()
        if not line:
            continue
        match = _LEADING_SUPERSCRIPT.match(line)
        if match:
            line = match.group(0).translate(_SUPERSCRIPT_DIGITS) + ' ' + line[match.end():].lstrip()
        match = _CARET_MARKER.match(line)
        if match:
            line = match.group(1) + ' ' + line[match.end():].lstrip()
        raw.append(line)
    lines, i = [], 0
    while i < len(raw):
        following = raw[i + 1] if i + 1 < len(raw) else None
        if following is not None and (_LONE_SYMBOL.match(raw[i]) or
                                      (_LONE_NUMBER.match(raw[i]) and not _MARKER_PATTERN.match(following))):
            lines.append(raw[i].rstrip('.') + ' ' + following)
            i += 2
        else:
            lines.append(raw[i])
            i += 1
    return lines


def _choose_markers(candidates: List[tuple], expected: Optional[int]) -> List[tuple]:
    """The markers of a region: the best forward-numbered chain among the lines
    opening with a number, [(line index, number)].

    Scored +1 per marker, -0.2 per number skipped between two markers, and
    -0.5 when the chain does not start at the number expected next (when the
    regions before told us one); ties go to the chain that starts earliest. A
    greedy line-by-line pass let one stray number (a list item, a page at a
    line start) derail every marker after it; the chain just leaves it out.
    """
    if not candidates:
        return []
    score = [0.0] * len(candidates)
    previous = [-1] * len(candidates)
    for j, (_, num) in enumerate(candidates):
        score[j] = 1.0 - (0.5 if expected is not None and num != expected else 0.0)
        for i in range(j):
            step = num - candidates[i][1]
            if 1 <= step <= _MAX_GAP and score[i] + 1.0 - 0.2 * (step - 1) > score[j] + 1e-9:
                score[j], previous[j] = score[i] + 1.0 - 0.2 * (step - 1), i

    def first_line(j):
        while previous[j] != -1:
            j = previous[j]
        return candidates[j][0]

    end = max(range(len(candidates)), key=lambda j: (round(score[j], 6), -first_line(j)))
    chain = []
    while end != -1:
        chain.append(candidates[end])
        end = previous[end]
    return chain[::-1]


def split_region_text(text: Optional[str], expected: Optional[int] = None) -> List[Dict[str, Any]]:
    """Cut one detected region's OCR text into footnotes at its own markers.

    A line opening with a footnote number starts a new footnote when that
    number belongs to the region's forward-numbered chain (_choose_markers);
    a line opening with "*" or a caption arrow starts an unnumbered note;
    everything else continues the one before. expected is the number the
    previous region's last footnote implies comes next, when known.

    Returns [{'text', 'marker', 'number'}], where marker says whether the
    piece opened with a marker of its own - one that did not is the tail of a
    footnote begun earlier - and number is its footnote number (None for an
    unnumbered piece). Lines are joined with spaces.

    Shared by the pipeline (prepare_footnotes_for_processing) and the viewer's
    hand-drawn and resized boxes, so both cut a region the same way.
    """
    lines = _region_lines(text)
    candidates = []
    for index, line in enumerate(lines):
        match = _MARKER_PATTERN.match(line)
        if not match or not 0 < int(match.group(1)) <= _MAX_MARKER:
            continue
        if line[len(match.group(1)):].startswith('.') and _DATE_AFTER_MARKER.match(match.group(2)):
            continue
        candidates.append((index, int(match.group(1))))
    starts = dict(_choose_markers(candidates, expected))

    chunks = []
    for index, line in enumerate(lines):
        unnumbered = bool(_SYMBOL_MARKER.match(line)) and (
            not chunks or bool(_ARROW_MARKER.match(line)) or bool(_SYMBOL_MARKER.match(chunks[-1]['lines'][0])))
        if index in starts or unnumbered or not chunks:
            chunks.append({'lines': [line], 'marker': index in starts or unnumbered, 'number': starts.get(index)})
        else:
            chunks[-1]['lines'].append(line)

    # The leading marker stays in the text: it is the token the FootnoteMarker
    # NER label is trained to tag, and it is the footnote's real printed
    # number, which the UI displays.
    pieces = [{'text': ' '.join(chunk['lines']).strip(), 'marker': chunk['marker'], 'number': chunk['number']}
              for chunk in chunks]
    return [piece for piece in pieces if piece['text']]


class EnhancedPDFToStructuredFootnotesPipeline:
    """Enhanced main pipeline orchestrator with improved citation handling"""
    
    def __init__(self, debug: bool = False, cpu_mode: bool = False, save_images: bool = False, save_footnotes: bool = False,
                 od_conf: float = 0.5, ref_conf: float = 0.8, comp_conf: float = 0.8,
                 od_model_path: Optional[str] = None, ner_suite_path: Optional[str] = "models/ner/mbert_production", is_nested: bool = False, use_surya_ocr: bool = True,
                 split_footnotes: bool = True, enable_ibidem_handling: bool = True, od_floor_conf: float = 0.05,
                 split_multi_work: bool = False):
        self.debug = debug
        self.cpu_mode = cpu_mode
        self.od_conf = od_conf
        self.ref_conf = ref_conf
        self.comp_conf = comp_conf
        self.use_surya_ocr = use_surya_ocr
        self.split_footnotes = split_footnotes
        self.enable_ibidem_handling = enable_ibidem_handling

        # Initialize components
        script_dir = Path(__file__).parent.parent
        if od_model_path is None:
            od_model_path = script_dir / "models" / "od" / "yolo11n_640_production.pt"

        self.pdf_extractor = PDFFootnoteExtractor(str(od_model_path), cpu_mode, debug, save_images, save_footnotes, od_conf, self.use_surya_ocr,
                                                  od_floor_conf=od_floor_conf)

        if ner_suite_path is None:
            ner_suite_path = "models/ner/mbert_production"

        self.ner_classifier = EnhancedFootnoteNERClassifier(
            yolo_conf=od_conf, ref_conf=ref_conf, comp_conf=comp_conf,
            yolo_model_path=str(od_model_path), ner_suite_path=ner_suite_path, is_nested=is_nested,
            debug=debug, save_images=save_images, save_footnotes=save_footnotes,
            split_footnotes=split_footnotes, enable_ibidem_handling=enable_ibidem_handling,
            split_multi_work=split_multi_work
        )
    
    @staticmethod
    def _link_footnotes_to_proposals(structured_results: List[Dict[str, Any]],
                                     od_proposals: Dict[int, Dict[str, Any]]) -> None:
        """Record on each proposal which footnote ids came out of it (in place).

        Matched by best IoU rather than equality because a footnote's stored
        bbox went through the +3pt-inflating conversion while the proposal keeps
        exact coordinates. With split_footnotes on, several footnotes share one
        region and therefore all attach to the same proposal — which is why
        footnote_ids is a list.
        """
        def iou(a, b):
            ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
            iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            inter = ix * iy
            union = ((a[2] - a[0]) * (a[3] - a[1])) + ((b[2] - b[0]) * (b[3] - b[1])) - inter
            return inter / union if union > 0 else 0.0

        for fn in structured_results:
            page_entry = od_proposals.get(fn.get('page'))
            bbox = fn.get('bbox')
            if not page_entry or not bbox or not fn.get('id'):
                continue
            target = (bbox['x1'], bbox['y1'], bbox['x2'], bbox['y2'])
            best, best_iou = None, 0.0
            for prop in page_entry['proposals']:
                if not prop['accepted']:
                    continue
                score = iou(target, prop['bbox'])
                if score > best_iou:
                    best, best_iou = prop, score
            # 0.5 is loose enough for the inflation on a small box, tight enough
            # that an unrelated region on the same page never claims a footnote.
            if best is not None and best_iou >= 0.5:
                best['footnote_ids'].append(fn['id'])

    def process_pdf(self, pdf_path: str, batch_size: int = 16) -> Dict[str, Any]:
        """Complete enhanced pipeline: PDF → Structured JSON"""
        
        print(f"\n{'='*60}")
        print(f"🚀 STARTING ENHANCED PDF TO STRUCTURED FOOTNOTES PIPELINE v2.0")
        print(f"{'='*60}")
        print(f"📄 Input PDF: {Path(pdf_path).name}")
        if self.debug:
            print(f"🔧 Pipeline Configuration:")
            print(f"   • Debug Mode: {self.debug}")
            print(f"   • CPU Mode: {self.cpu_mode}")
            print(f"   • OD Detection: {self.od_conf}")
        print(f"🔧 Enhanced Features:")
        print(f"   • Citation Type Classification (ibidem, opus citatum, normal)")
        print(f"   • Author-based matching for opus citatum")
        print(f"   • Improved component inference logic")
        print()
        
        start_time = datetime.now()
        
        # Step 1: Extract footnotes from PDF
        print(f"\n--- Step 1: PDF Footnote Extraction ---")
        footnotes_text = self.pdf_extractor.extract_footnotes_from_pdf(pdf_path, batch_size)
        # Whole-document totals (see PDFFootnoteExtractor.last_od_time_seconds
        # docstring) — read right after the call that set them.
        od_inference_time = self.pdf_extractor.last_od_time_seconds
        ocr_inference_time = self.pdf_extractor.last_ocr_time_seconds

        if not footnotes_text:
            return {
                'results': [],
                'metadata': {
                    'input_file': str(Path(pdf_path).name),
                    'total_footnotes_processed': 0,
                    'processing_date': datetime.now().isoformat(),
                    'processing_time_seconds': 0,
                    'od_inference_time_seconds': od_inference_time,
                    'ocr_inference_time_seconds': ocr_inference_time,
                    'models_used': ['YOLOv8', 'Enhanced_NER_Reference_BERT', 'Enhanced_NER_Component_BERT'],
                    'debug_mode': self.debug,
                    'structure': 'enhanced_hierarchical_references_with_citation_type_classification',
                    'version': '2.0',
                    'error': 'No footnotes extracted from PDF'
                },
                # Kept even on the empty path: a page can hold sub-threshold
                # proposals while yielding no footnote at all, and those are
                # exactly what the layout prior may later rescue.
                'od_proposals': self.pdf_extractor.last_page_proposals,
            }
        
        # Step 2: Enhanced NER classification and structuring
        print(f"\n--- Step 2: Enhanced NER Classification and Structuring ---")
        structured_results = self.ner_classifier.process_footnotes(footnotes_text)

        od_proposals = self.pdf_extractor.last_page_proposals
        self._link_footnotes_to_proposals(structured_results, od_proposals)
        
        # Step 3: Generate statistics (if debug mode)
        stats = None
        if self.debug:
            print(f"\n--- Step 3: Generating Enhanced Statistics ---")
            stats = self.ner_classifier.generate_statistics(structured_results)
        
        # Calculate processing time
        end_time = datetime.now()
        processing_time = (end_time - start_time).total_seconds()
        
        # Prepare final output with enhanced metadata
        total_refs = sum(r['summary']['ref_count'] for r in structured_results)
        total_comps = sum(r['summary']['total_components'] for r in structured_results)
        refs_with_comps = sum(r['summary']['refs_with_components'] for r in structured_results)
        footnotes_with_inference = sum(1 for r in structured_results if 'inferred_components' in r)
        total_inferred_comps = sum(len(r.get('inferred_components', [])) for r in structured_results)
        
        # Citation type counts from individual references
        citation_type_counts = {'normal': 0, 'ibidem': 0, 'opus_citatum': 0}
        for result in structured_results:
            for ref in result['references']:
                citation_type = ref.get('citation_type', 'normal')
                citation_type_counts[citation_type] = citation_type_counts.get(citation_type, 0) + 1
        
        output = {
            'results': structured_results,
            'metadata': {
                'input_file': str(Path(pdf_path).name),
                'total_footnotes_processed': len(structured_results),
                'total_references_detected': total_refs,
                'total_components_extracted': total_comps,
                'references_with_components': refs_with_comps,
                'footnotes_with_inference': footnotes_with_inference,
                'total_inferred_components': total_inferred_comps,
                'citation_type_distribution': citation_type_counts,
                'references_split_multi_work': self.ner_classifier.references_split,
                'processing_date': datetime.now().isoformat(),
                'processing_time_seconds': round(processing_time, 2),
                # Whole-document totals — YOLO/OCR run batched per page(-batch),
                # not per footnote, so this is the finest granularity available
                # for those two; per-footnote NER timing lives on each footnote
                # itself instead (see EnhancedFootnoteNERClassifier.process_footnotes).
                'od_inference_time_seconds': od_inference_time,
                'ocr_inference_time_seconds': ocr_inference_time,
                'models_used': ['YOLOv8', 'Enhanced_NER_Reference_BERT', 'Enhanced_NER_Component_BERT'],
                'debug_mode': self.debug,
                'structure': 'enhanced_hierarchical_references_with_citation_type_classification_and_author_matching',
                'version': '2.0',
                'success_rates': {
                    'footnote_detection': f"{len(structured_results)} footnotes",
                    'reference_detection': f"{total_refs} references",
                    'component_extraction': f"{total_comps} components",
                    'component_coverage': f"{refs_with_comps}/{total_refs} ({refs_with_comps/total_refs*100:.1f}%)" if total_refs > 0 else "0/0 (0%)",
                    'enhanced_processing': f"{citation_type_counts['ibidem']} ibidem + {citation_type_counts['opus_citatum']} opus citatum with {total_inferred_comps} inferred components"
                }
            },
            # Peeled off by app.py into the job's proposal sidecar — deliberately
            # NOT nested under 'results' or 'metadata', both of which are served
            # verbatim by the download/export routes.
            'od_proposals': od_proposals,
        }
        
        # Add statistics if debug mode
        if stats and self.debug:
            output['statistics'] = stats
        
        # Enhanced summary
        print(f"\n{'='*60}")
        print(f"✅ ENHANCED PIPELINE COMPLETED SUCCESSFULLY")
        print(f"{'='*60}")
        print(f"📊 Results Summary:")
        print(f"   • Footnotes processed: {len(structured_results)}")
        print(f"   • References detected: {total_refs}")
        print(f"   • Components extracted: {total_comps}")
        print(f"   • Component coverage: {refs_with_comps}/{total_refs} ({refs_with_comps/total_refs*100:.1f}%)" if total_refs > 0 else "   • Component coverage: 0/0 (0%)")
        print(f"📈 Citation Type Analysis:")
        total_references_for_display = sum(citation_type_counts.values())
        print(f"   • Normal citations: {citation_type_counts['normal']} ({citation_type_counts['normal']/total_references_for_display*100:.1f}%)" if total_references_for_display > 0 else "   • Normal citations: 0")
        print(f"   • Ibidem citations: {citation_type_counts['ibidem']} ({citation_type_counts['ibidem']/total_references_for_display*100:.1f}%)" if total_references_for_display > 0 else "   • Ibidem citations: 0")
        print(f"   • Opus citatum citations: {citation_type_counts['opus_citatum']} ({citation_type_counts['opus_citatum']/total_references_for_display*100:.1f}%)" if total_references_for_display > 0 else "   • Opus citatum citations: 0")
        print(f"🔧 Enhanced Processing:")
        print(f"   • Footnotes with inference: {footnotes_with_inference}")
        print(f"   • Total inferred components: {total_inferred_comps}")
        print(f"   • Processing time: {processing_time:.2f} seconds")
        
        return output

def main():
    """Main function for the enhanced pipeline"""
    parser = argparse.ArgumentParser(description='Enhanced PDF to Structured Footnotes Pipeline v2.0')
    parser.add_argument('input_pdf', help='Path to the input PDF file')
    parser.add_argument('-o', '--output', help='Output JSON file path (optional)')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode with detailed logs')
    parser.add_argument('--stats', action='store_true', help='Show detailed statistics (requires debug mode)')
    parser.add_argument('--cpu', action='store_true', help='Force CPU processing')
    parser.add_argument('--batch-size', type=int, default=16, help='Batch size for processing (default: 16)')
    parser.add_argument('--save-images', action='store_true', help='Save debug images with bounding boxes')
    parser.add_argument('--save-footnotes', action='store_true', help='Save extracted footnote texts')
    parser.add_argument('--yolo-conf', type=float, default=0.5, help='YOLO detection confidence threshold (default: 0.5)')
    parser.add_argument('--ref-conf', type=float, default=0.8, help='Reference model confidence threshold (default: 0.8)')
    parser.add_argument('--comp-conf', type=float, default=0.8, help='Component model confidence threshold (default: 0.8)')
    parser.add_argument('--enrich', action='store_true', help='Enrich reference metadata with external APIs (Google Books, Crossref, OpenAIRE)')
    parser.add_argument('--google-books-key', type=str, default=None, help='API key for Google Books enrichment')
    parser.add_argument('--crossref-email', type=str, default=None, help='Contact email for the Crossref polite pool (defaults to the CROSSREF_EMAIL environment variable)')
    
    args = parser.parse_args()
    
    # Validate input PDF
    pdf_path = Path(args.input_pdf)
    if not pdf_path.is_file():
        print(f"❌ Error: PDF file not found at {pdf_path}")
        sys.exit(1)
    
    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_dir = Path(__file__).parent / 'structured_footnotes'
        output_dir.mkdir(exist_ok=True)
        output_path = output_dir / f"{pdf_path.stem}_enhanced_structured.json"
    
    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Enable debug mode if stats requested
    if args.stats:
        args.debug = True
    
    try:
        # Initialize and run enhanced pipeline
        pipeline = EnhancedPDFToStructuredFootnotesPipeline(debug=args.debug, cpu_mode=args.cpu, 
                                                   save_images=args.save_images, 
                                                   save_footnotes=args.save_footnotes,
                                                   od_conf=args.yolo_conf,
                                                   ref_conf=args.ref_conf,
                                                   comp_conf=args.comp_conf)
        result = pipeline.process_pdf(str(pdf_path), args.batch_size)
        
        # Show detailed statistics if requested
        if args.debug and args.stats and 'statistics' in result:
            print(f"\n{'='*60}")
            print(f"📊 DETAILED ENHANCED STATISTICS")
            print(f"{'='*60}")
            
            stats = result['statistics']
            if stats:
                overview = stats.get('overview', {})
                components = stats.get('components', {})
                quality = stats.get('quality', {})
                citation_analysis = stats.get('citation_type_analysis', {})
                inference_analysis = stats.get('inference_analysis', {})
                
                print(f"📈 Overview:")
                print(f"   • References per footnote: {overview.get('avg_refs_per_footnote', 0):.2f}")
                print(f"   • Components per footnote: {overview.get('avg_comps_per_footnote', 0):.2f}")
                print(f"   • Component coverage: {overview.get('component_coverage_rate', 0):.1f}%")
                print(f"   • Footnotes with inference: {overview.get('footnotes_with_inference', 0)}")
                
                print(f"\n📚 Citation Type Analysis:")
                print(f"   • Normal: {citation_analysis.get('normal_citations', 0)} ({citation_analysis.get('normal_percentage', 0):.1f}%)")
                print(f"   • Ibidem: {citation_analysis.get('ibidem_citations', 0)} ({citation_analysis.get('ibidem_percentage', 0):.1f}%)")
                print(f"   • Opus citatum: {citation_analysis.get('opus_citatum_citations', 0)} ({citation_analysis.get('opus_citatum_percentage', 0):.1f}%)")
                
                print(f"\n🏷️ Component Types:")
                type_dist = components.get('type_distribution', {})
                for comp_type, count in list(type_dist.items())[:5]:
                    percentage = count / sum(type_dist.values()) * 100 if type_dist else 0
                    print(f"   • {comp_type}: {count} ({percentage:.1f}%)")
                
                print(f"\n🔄 Inference Analysis:")
                print(f"   • Total inferred: {inference_analysis.get('total_inferred_components', 0)}")
                print(f"   • Average per footnote: {inference_analysis.get('avg_inferred_per_footnote', 0):.2f}")
                inference_types = inference_analysis.get('inference_type_distribution', {})
                for inf_type, count in inference_types.items():
                    print(f"   • {inf_type}: {count}")
                
                if quality:
                    print(f"\n⭐ Quality:")
                    print(f"   • Average confidence: {quality.get('avg_confidence', 0):.3f}")
                    print(f"   • Median confidence: {quality.get('median_confidence', 0):.3f}")
        
        # Optional: Enrich references with external metadata
        if args.enrich:
            print(f"\n--- Step 4: External Metadata Enrichment ---")
            enrichment_service = EnrichmentService(
                google_books_api_key=args.google_books_key,
                crossref_email=args.crossref_email
            )
            
            total_enriched = 0
            for result_item in result['results']:
                for ref in result_item.get('references', []):
                    # We pass all references if option selected
                    components = ref.get('components', [])
                    if components:
                        external_data = enrichment_service.enrich_reference(components)
                        if external_data:
                            ref['external_metadata'] = external_data
                            total_enriched += 1
            print(f"   • Enriched {total_enriched} references with external metadata.")

        # Save results
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        
        print(f"\n💾 Enhanced structured results saved to: {output_path}")
        print(f"✅ Enhanced pipeline finished successfully!")
        
    except Exception as e:
        print(f"\n❌ Enhanced pipeline failed with error: {e}")
        if args.debug:
            import traceback
            print(f"\nFull traceback:")
            traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
