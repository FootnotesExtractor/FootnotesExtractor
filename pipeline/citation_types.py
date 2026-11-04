"""Ibid. / op. cit. / normal from a reference's text - the patterns the NER
engine and the app have always used (formerly in pipeline/ner_service.py,
which imports them from here). Kept free of torch so the inference run on
every save (pipeline/reference_inference.py) and the scripts can use them."""
import re

IBIDEM_PATTERNS = [
    r'\bibidem\b', r'\bibid\.?\b', r'\bib\.?\b',
    r'\beadem\b', r'\bead\.?\b', r'\btamże\b'
]
OPUS_CITATUM_PATTERNS = [
    r'\bop\.?\s*cit\.?\b', r'\bloc\.?\s*cit\.?\b',
    r'\ba\.?\s*a\.?\s*O\.?\b', r'\barzw\.?\b',
    r'\bcyt\.?\s*dz\.?\b', r'\bdz\.?\s*cyt\.?\b'
]


def classify_citation_type_standalone(text: str) -> str:
    """Same logic as NEREngine.classify_citation_type, callable without a
    loaded (or loadable) engine instance."""
    text_lower = (text or '').lower()
    for pattern in IBIDEM_PATTERNS:
        if re.search(pattern, text_lower):
            return 'ibidem'
    for pattern in OPUS_CITATUM_PATTERNS:
        if re.search(pattern, text_lower):
            return 'opus_citatum'
    return 'normal'
