#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared NER engine used both by the bulk PDF-processing pipeline
(`EnhancedFootnoteNERClassifier`) and by on-demand classification of a
single, user-drawn reference/component span (see app.py's reference/
component add/update endpoints).

The three BERT token-classification models (reference, component, names)
are expensive to load, so instances are cached per (ner_suite_path,
is_nested) for the lifetime of the process instead of being reloaded
per job/request.
"""

import concurrent.futures
import re
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

import torch
from pathlib import Path
from transformers import AutoTokenizer, AutoModelForTokenClassification

try:
    from pipeline.model_pool import InstancePool
    from pipeline.reference_spans import no_chunk_break_before, repair_reference_spans
except ImportError:
    # Handle if run directly
    from model_pool import InstancePool
    from reference_spans import no_chunk_break_before, repair_reference_spans


# Module-level (not tied to a loaded NEREngine instance) so citation-type
# classification — pure regex, no model involved — still works as a fallback
# even when the reference/component BERT models themselves can't be loaded
# (see classify_citation_type_standalone below, used by app.py's
# create_reference/update_reference when NEREngine.get_instance() fails).
# The only component labels that can carry nested FirstName/LastName
# children — the names model is trained on person-name phrases, so running it
# on a Title or a Publisher is meaningless. Kept here rather than in app.py so
# the engine's own classify_reference_components applies the same rule as the
# bulk pipeline (pdf_to_structured_footnotes.py:674) and app.py's edit routes.
NAME_BEARING_COMPONENT_LABELS = frozenset({'Author', 'Editor', 'Translator'})

# The citation-type patterns and the engine-free classifier live in
# pipeline/citation_types.py (free of torch, for the inference run on every
# save); imported here so existing imports keep working.
from pipeline.citation_types import IBIDEM_PATTERNS, OPUS_CITATUM_PATTERNS, classify_citation_type_standalone  # noqa: E402,F401


class NEREngine:
    """Lazily-loaded NER engine: copies per (ner_suite_path, is_nested) in
    NEREngine.pool (model_pool.py); get_instance() gives this thread's copy."""

    def __init__(self, ner_suite_path: str, is_nested: bool):
        self.ner_suite_path = ner_suite_path
        self.is_nested = is_nested
        # Only the suites named in gpu_suites go on the GPU, the others on the
        # CPU; None (what the app and the offline scripts use): every suite on
        # the GPU.
        on_gpu = NEREngine.gpu_suites is None or Path(ner_suite_path).name in NEREngine.gpu_suites
        self.device = torch.device('cuda' if torch.cuda.is_available() and on_gpu else 'cpu')

        self.ibidem_patterns = IBIDEM_PATTERNS
        self.opus_citatum_patterns = OPUS_CITATUM_PATTERNS

        self._load_models()

    gpu_suites = None          # set of suite folder names allowed on the GPU; None = all

    @classmethod
    def get_instance(cls, ner_suite_path: str, is_nested: bool) -> "NEREngine":
        return cls.pool.current((str(ner_suite_path), bool(is_nested)))

    def _load_models(self):
        from pathlib import Path
        import json

        suite_path = Path(self.ner_suite_path)

        ref_path = suite_path / "NER_reference"
        comp_path = suite_path / "NER_component"
        names_path = suite_path / "NER_names"

        if not ref_path.exists():
            raise FileNotFoundError(f"Reference model not found at {ref_path}")
        if not comp_path.exists():
            raise FileNotFoundError(f"Component model not found at {comp_path}")

        print("📦 Loading Reference model...")
        self.ref_tokenizer = AutoTokenizer.from_pretrained(str(ref_path))
        self.ref_model = AutoModelForTokenClassification.from_pretrained(str(ref_path))
        self.ref_model.to(self.device).eval()

        mapping_path = ref_path / 'label_mappings.json'
        if mapping_path.exists():
            with open(mapping_path) as f:
                ref_mappings = json.load(f)
                self.ref_id2label = {int(k): v for k, v in ref_mappings['id2label'].items()}
        else:
            self.ref_id2label = {int(k): v for k, v in self.ref_model.config.id2label.items()}

        print("📦 Loading Component model...")
        self.comp_tokenizer = AutoTokenizer.from_pretrained(str(comp_path))
        self.comp_model = AutoModelForTokenClassification.from_pretrained(str(comp_path))
        self.comp_model.to(self.device).eval()

        mapping_path = comp_path / 'label_mappings.json'
        if mapping_path.exists():
            with open(mapping_path) as f:
                comp_mappings = json.load(f)
                self.comp_id2label = {int(k): v for k, v in comp_mappings['id2label'].items()}
        else:
            self.comp_id2label = {int(k): v for k, v in self.comp_model.config.id2label.items()}

        if names_path.exists():
            print("📦 Loading Names model...")
            self.names_tokenizer = AutoTokenizer.from_pretrained(str(names_path))
            self.names_model = AutoModelForTokenClassification.from_pretrained(str(names_path))
            self.names_model.to(self.device).eval()

            mapping_path = names_path / 'label_mappings.json'
            if mapping_path.exists():
                with open(mapping_path) as f:
                    names_mappings = json.load(f)
                    self.names_id2label = {int(k): v for k, v in names_mappings['id2label'].items()}
            else:
                self.names_id2label = {int(k): v for k, v in self.names_model.config.id2label.items()}
        else:
            print(f"⚠️ Names model not found at {names_path}. Proceeding without it.")
            self.names_model = None

        print(f"✅ NER suite loaded successfully! (Nested mode: {self.is_nested})")

    def classify_citation_type(self, text: str) -> str:
        """Classify citation as ibidem, opus_citatum, or normal"""
        text_lower = text.lower()

        for pattern in self.ibidem_patterns:
            if re.search(pattern, text_lower):
                return 'ibidem'

        for pattern in self.opus_citatum_patterns:
            if re.search(pattern, text_lower):
                return 'opus_citatum'

        return 'normal'

    def split_long_text(self, text: str, max_chars: int = 1000) -> List[Tuple[int, int]]:
        """Split text into chunks by punctuation to avoid cutting tokens"""
        chunks = []

        boundaries = []
        for m in re.finditer(r'(?:[.;]\s+|\n)', text):
            start = m.start()
            if text[start] == '.':
                if start >= 1 and text[start - 1].isupper():
                    if start == 1 or not text[start - 2].isalpha():
                        continue
                # never between a locator word and its number ("s. 45",
                # "op. cit. 12"): the chunks are decoded apart, which cut
                # references in two (pipeline/reference_spans.py)
                if no_chunk_break_before(text, start):
                    continue
            boundaries.append(m.end())

        if not boundaries:
            boundaries = [m.end() for m in re.finditer(r'\s+', text)]

        boundaries.append(len(text))

        current_start = 0
        for b in boundaries:
            if b - current_start > max_chars:
                chunks.append((current_start, b))
                current_start = b
            elif b == boundaries[-1] or (b - current_start > 500):
                chunks.append((current_start, b))
                current_start = b

        if current_start < len(text):
            chunks.append((current_start, len(text)))

        return chunks

    def extract_entities(self, text, model, tokenizer, id2label, conf_threshold=0.8):
        """Extract entities from text, handling long texts by chunking"""
        chunks = self.split_long_text(text)
        all_entities = []

        for start_idx, end_idx in chunks:
            chunk_text = text[start_idx:end_idx]
            if not chunk_text.strip():
                continue

            inputs = tokenizer(chunk_text, return_tensors='pt', truncation=True,
                              max_length=512, padding='max_length',
                              return_offsets_mapping=True)

            offset_mapping = inputs.pop('offset_mapping').squeeze(0).cpu().numpy()

            if 'token_type_ids' in inputs and getattr(model.config, 'model_type', '') in ['distilbert', 'roberta', 'xlm-roberta']:
                inputs.pop('token_type_ids')

            inputs = inputs.to(self.device)

            with torch.no_grad():
                outputs = model(**inputs)
                predictions = torch.argmax(outputs.logits, dim=2)[0]
                confidences = torch.softmax(outputs.logits, dim=2)[0]

            entities = []
            current_entity = None

            for i, (pred, conf_scores) in enumerate(zip(predictions, confidences)):
                label = id2label[pred.item()]
                confidence = conf_scores[pred].item()
                tok_start, tok_end = offset_mapping[i]

                if tok_start == tok_end:  # Special tokens
                    continue

                if label != 'O' and confidence > conf_threshold:
                    if label.startswith('B-'):
                        if current_entity:
                            entities.append(current_entity)

                        current_entity = {
                            'text': chunk_text[tok_start:tok_end],
                            'label': label[2:],
                            'confidence': float(confidence),
                            'start': start_idx + int(tok_start),
                            'end': start_idx + int(tok_end)
                        }
                    elif label.startswith('I-'):
                        if current_entity and current_entity['label'] == label[2:]:
                            current_entity['text'] = chunk_text[current_entity['start'] - start_idx:tok_end]
                            current_entity['end'] = start_idx + int(tok_end)
                            current_entity['confidence'] = max(current_entity['confidence'], float(confidence))
                        else:
                            if current_entity:
                                entities.append(current_entity)

                            current_entity = {
                                'text': chunk_text[tok_start:tok_end],
                                'label': label[2:],
                                'confidence': float(confidence),
                                'start': start_idx + int(tok_start),
                                'end': start_idx + int(tok_end)
                            }
                else:
                    if current_entity:
                        entities.append(current_entity)
                        current_entity = None

            if current_entity:
                entities.append(current_entity)

            all_entities.extend(entities)

        all_entities = [entity for entity in all_entities if entity['confidence'] >= conf_threshold]

        return all_entities

    # -- On-demand entrypoints for manual reference/component span editing --

    def classify_reference_components(self, ref_text: str, ref_start_offset: int, comp_conf: float) -> List[dict]:
        """Run the component model directly on a (manually-selected) reference's
        text and shift offsets to be absolute against the parent footnote's full
        text — mirrors the nested-mode branch of the bulk pipeline's
        classify_footnote (pdf_to_structured_footnotes.py:638-646). Used
        regardless of the suite's is_nested setting: for manually-created
        references we always classify components directly on the reference
        substring rather than replicating full-context mode's whole-footnote
        pass, since only this substring is relevant here."""
        local_components = self.extract_entities(
            ref_text, self.comp_model, self.comp_tokenizer, self.comp_id2label, comp_conf
        )
        components = []
        for comp in local_components:
            comp_start = comp['start'] + ref_start_offset
            component = {
                'id': str(uuid.uuid4()),
                'label': comp['label'],
                'text': comp['text'],
                'confidence': comp['confidence'],
                'start': comp_start,
                'end': comp['end'] + ref_start_offset,
            }
            # Nested names, exactly as the bulk pipeline does them — without
            # this an Author created by any edit path (new reference, redrawn
            # bbox, split/merged footnote) came back with no FirstName/LastName
            # children at all, while the same text processed at upload time got
            # them.
            if comp['label'] in NAME_BEARING_COMPONENT_LABELS:
                names = self.extract_component_names(comp['text'], comp_start, comp_conf)
                if names:
                    component['names'] = names
            components.append(component)
        return components

    def classify_footnote_references(self, text: str, ref_conf: float, comp_conf: float) -> List[dict]:
        """Run the reference model on a full footnote's text, then classify
        each detected reference's components (via classify_reference_components).
        Mirrors the bulk pipeline's classify_footnote orchestration, but assigns
        each reference a uuid4 id (consistent with manually-created references)
        so it can be used for the automatic bbox-add/edit cascade as well as any
        other on-demand full-footnote (re)classification.

        The per-reference component-extraction calls are independent of each
        other, so for 2+ references they run concurrently via a thread pool
        rather than one after another — same reasoning as the parallel split
        halves in app.py's split_footnote: PyTorch releases the GIL during
        the actual tensor math, so this is genuine overlap, not just
        interleaving. This is the main reason merging two footnotes' worth of
        references felt slower than editing either alone — a merged text
        typically has more references than either original piece, so the old
        sequential loop meant more model passes back to back."""
        raw_refs = self.extract_entities(text, self.ref_model, self.ref_tokenizer, self.ref_id2label, ref_conf)
        # a page locator the model left on its own goes back into its
        # reference before the parts are tagged (pipeline/reference_spans.py)
        raw_refs, _ = repair_reference_spans(text, raw_refs)
        if not raw_refs:
            return []

        if len(raw_refs) == 1:
            components_list = [self.classify_reference_components(raw_refs[0]['text'], raw_refs[0]['start'], comp_conf)]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(raw_refs), 8)) as executor:
                futures = [executor.submit(self.classify_reference_components, ref['text'], ref['start'], comp_conf) for ref in raw_refs]
                components_list = [f.result() for f in futures]

        references = []
        for ref, components in zip(raw_refs, components_list):
            references.append({
                'id': str(uuid.uuid4()),
                'text': ref['text'],
                'confidence': ref['confidence'],
                'start': ref['start'],
                'end': ref['end'],
                'components': components,
                'citation_type': self.classify_citation_type(ref['text']),  # pure regex, negligible cost either way
            })
        return references

    def suggest_component_label(self, comp_text: str, comp_conf: float) -> Optional[dict]:
        """Run the component model on an already-selected exact phrase and return
        a single confident {label, confidence} guess, or None if the model
        produces no entity, more than one, or an entity that doesn't cover most
        of the selected text (a partial/ambiguous match isn't a safe suggestion
        to silently apply)."""
        entities = self.extract_entities(
            comp_text, self.comp_model, self.comp_tokenizer, self.comp_id2label, comp_conf
        )
        if len(entities) != 1:
            return None
        entity = entities[0]
        if not comp_text or (entity['end'] - entity['start']) < 0.8 * len(comp_text):
            return None
        return {'label': entity['label'], 'confidence': entity['confidence']}

    def extract_component_names(self, comp_text: str, comp_start_offset: int, conf: float) -> List[dict]:
        """Run the names model over a whole Author/Editor/Translator component
        and return its FirstName/LastName children with offsets shifted to be
        absolute against the parent footnote's text. Same call the bulk
        pipeline makes (pdf_to_structured_footnotes.py:674-685), but assigns
        each name a uuid4 id the way the manual create_name route does, so
        names born this way are addressable by the viewer's edit endpoints.

        Returns [] when the suite has no names model loaded, so every caller
        can treat "no names model" and "no names found" identically."""
        if self.names_model is None:
            return []
        local_names = self.extract_entities(
            comp_text, self.names_model, self.names_tokenizer, self.names_id2label, conf
        )
        return [{
            'id': str(uuid.uuid4()),
            'label': name['label'],
            'text': name['text'],
            'confidence': name['confidence'],
            'start': name['start'] + comp_start_offset,
            'end': name['end'] + comp_start_offset,
        } for name in local_names]

    @staticmethod
    def _entity_for_selection(entities: List[dict], start: int, end: int) -> Optional[dict]:
        """Pick the one entity a selection [start, end) refers to, or None if
        that's ambiguous: nothing overlaps it, it straddles two entities (e.g.
        "Eric Hobsbawm" covering both a FirstName and a LastName — genuinely
        two names, not one), or it sticks out far enough beyond the entity that
        the match can't be trusted."""
        sel_len = end - start
        if sel_len <= 0:
            return None
        overlapping = [ent for ent in entities if min(end, ent['end']) > max(start, ent['start'])]
        if len(overlapping) != 1:
            return None
        entity = overlapping[0]
        overlap = min(end, entity['end']) - max(start, entity['start'])
        if overlap < 0.8 * sel_len:
            return None
        return entity

    def suggest_name_label(self, name_text: str, conf: float, context_text: Optional[str] = None,
                           rel_start: Optional[int] = None, rel_end: Optional[int] = None) -> Optional[dict]:
        """Same idea as suggest_component_label, one level deeper: suggest a
        single confident {label, confidence} for a name span the user selected
        inside an Author/Editor/Translator component, or None to force a manual
        label pick.

        When the parent component's text is supplied (`context_text`, with the
        selection's offsets *relative to it*), the model is run over that whole
        phrase and the entity covering the selection is returned. That is how
        the names model was trained and how the bulk pipeline uses it, and it
        matters a lot here: an initial alone, "E.", scores nothing on its own
        but is confidently FirstName inside "E. Hobsbawm". Falling back to the
        bare phrase keeps the old behaviour for callers with no context."""
        if self.names_model is None:
            return None
        if context_text and rel_start is not None and rel_end is not None:
            entities = self.extract_entities(
                context_text, self.names_model, self.names_tokenizer, self.names_id2label, conf
            )
            entity = self._entity_for_selection(entities, rel_start, rel_end)
            if entity is not None:
                return {'label': entity['label'], 'confidence': entity['confidence']}
            # Deliberately no bare-phrase retry when the context run found the
            # selection ambiguous (two names, or none) — that answer is better
            # informed than anything the fragment alone could produce.
            return None
        entities = self.extract_entities(
            name_text, self.names_model, self.names_tokenizer, self.names_id2label, conf
        )
        if len(entities) != 1:
            return None
        entity = entities[0]
        if not name_text or (entity['end'] - entity['start']) < 0.8 * len(name_text):
            return None
        return {'label': entity['label'], 'confidence': entity['confidence']}


NEREngine.pool = InstancePool(lambda key: NEREngine(*key), 'ner')
