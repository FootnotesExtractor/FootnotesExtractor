#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared logic for turning one extracted reference's component spans (Author,
Title, Journal, ...) into a normalized set of citation fields — the common
groundwork every export format (BibTeX, APA, Chicago, MLA, ...) builds on,
so each format module only has to worry about its own template/punctuation
rules, not re-walking the component tree.

This is a best-effort transcription of what the OD+OCR+NER pipeline
extracted, not a citation-management-grade formatter — split/merged spans
from imperfect NER extraction (e.g. a journal name broken across two
components by an intervening quote mark) come through as best-effort
concatenation, not corrected.
"""

import re
from typing import Dict, List, Optional, TypedDict

from pipeline.reference_split import resolve_inherited, work_view


def clean_text(text: Optional[str]) -> str:
    """Collapse whitespace/newlines a component span may carry."""
    return re.sub(r'\s+', ' ', (text or '')).strip()


def _components_by_label(components: List[dict]) -> Dict[str, List[dict]]:
    by_label: Dict[str, List[dict]] = {}
    for comp in components or []:
        by_label.setdefault(comp.get('label'), []).append(comp)
    return by_label


def _merged_text(comps: List[dict]) -> str:
    """Same-label components (e.g. a journal name split across two spans by
    an intervening quote mark the NER model treated as a boundary) are
    joined in offset order — a best-effort reassembly, not a corrected one."""
    ordered = sorted(comps, key=lambda c: c.get('start', 0))
    return clean_text(' '.join(clean_text(c.get('text')) for c in ordered))


# Volume/Issue/Pages spans capture the source citation's own label word along
# with the number (e.g. "vol. 54", "s. 124") — stripped here so the field
# holds just the value. Deliberately narrow (a fixed prefix list) rather than
# a general numeric-extraction regex, so it only ever removes a label word it
# recognizes and never mangles a value it doesn't.
_NUMERIC_FIELD_PREFIXES = re.compile(
    r'^(?:vol\.?|tom\.?|t\.?|no\.?|nr\.?|issue|pp\.?|pages?|p\.?|s\.?)\s*',
    re.IGNORECASE,
)


def clean_numeric_field(text: str) -> str:
    """Returns '' (not the original text) when the span turns out to be
    *only* the label word with no actual number after it (e.g. a "vol. 3"
    citation where NER only captured "vol." before the number) — letting
    callers treat that the same as a missing field, rather than emitting a
    volume/issue/pages value that's just the bare label word."""
    stripped = _NUMERIC_FIELD_PREFIXES.sub('', text).strip()
    return stripped.rstrip('.').strip()


class Person(TypedDict):
    first: str  # '' if unavailable
    last: str   # falls back to the component's whole raw text if unsplit
    raw: str    # the component's own text, unsplit


def _person_from_component(component: dict) -> Person:
    names = component.get('names') or []
    first = next((n['text'] for n in names if n.get('label') == 'FirstName'), None)
    last = next((n['text'] for n in names if n.get('label') == 'LastName'), None)
    raw = clean_text(component.get('text'))
    return {
        'first': clean_text(first) if first else '',
        'last': clean_text(last) if last else (raw if not first else ''),
        'raw': raw,
    }


def _people_from_components(comps: List[dict]) -> List[Person]:
    ordered = sorted(comps, key=lambda c: c.get('start', 0))
    return [_person_from_component(c) for c in ordered]


def pick_entry_type(by_label: Dict[str, List[dict]]) -> str:
    """'article' | 'book' | 'incollection' | 'online' | 'misc' — used both to
    pick a BibTeX @type and to pick which template a style formatter uses."""
    if by_label.get('Journal'):
        return 'article'
    if by_label.get('BookTitle'):
        return 'incollection'
    if by_label.get('Publisher'):
        return 'book'
    if by_label.get('Webpage') or by_label.get('DOI'):
        return 'online'
    return 'misc'


class CitationFields(TypedDict):
    citation_type: str          # 'normal' | 'ibidem' | 'opus_citatum'
    entry_type: str             # see pick_entry_type
    authors: List[Person]
    editors: List[Person]
    translators: List[Person]
    title: str
    booktitle: str
    journal: str
    publisher: str
    location: str
    year: str
    year_suffix: str
    volume: str
    issue: str
    pages: str
    doi: str
    url: str
    raw_text: str                # the reference's own extracted text, verbatim
    source_text: str             # the reference as printed: for a work split out
                                 # of a multi-work reference, the whole reference
    found_on_pages: List[int]    # PDF page(s) of the footnote it was found in


def extract_reference_fields(ref: dict, expanded: bool, footnote: Optional[dict] = None) -> CitationFields:
    """Normalizes one reference's component spans into a flat field set.

    `expanded` only changes anything for ibidem/opus-citatum references: with
    it on, `components_inferred` is used when present — the NER pipeline
    already resolves that back to the original work's full Author/Title/
    Journal/... (see pdf_to_structured_footnotes.py) — instead of the bare
    Ibidem/OpusCitatum marker + Pages that `components` holds for these.

    A reference cut out of a span that held several works (see
    pipeline/reference_split.py) also carries `inherited` fields: the author,
    title, ... that the text leaves out of a later work ("2012" in "Martines
    2009, 2012"). Those are the work's own fields elided in print, not an
    inference about another citation, so both modes use them, refreshed from
    their source part when `footnote` is given."""
    citation_type = ref.get('citation_type', 'normal')
    components = ref.get('components') or []
    if expanded and citation_type in ('ibidem', 'opus_citatum') and ref.get('components_inferred'):
        components = ref['components_inferred']
    if ref.get('inherited'):
        inherited = resolve_inherited(footnote, ref) if footnote is not None else ref['inherited']
        own = {c.get('label') for c in components}
        components = list(components) + [h for h in inherited if h.get('label') not in own]

    by_label = _components_by_label(components)

    year = _merged_text(by_label['PublicationYear']) if by_label.get('PublicationYear') else ''
    year_suffix = _merged_text(by_label['PublicationYearSuffix']) if by_label.get('PublicationYearSuffix') else ''

    return {
        'citation_type': citation_type,
        'entry_type': pick_entry_type(by_label),
        'authors': _people_from_components(by_label.get('Author', [])),
        'editors': _people_from_components(by_label.get('Editor', [])),
        'translators': _people_from_components(by_label.get('Translator', [])),
        'title': _merged_text(by_label['Title']) if by_label.get('Title') else '',
        'booktitle': _merged_text(by_label['BookTitle']) if by_label.get('BookTitle') else '',
        'journal': _merged_text(by_label['Journal']) if by_label.get('Journal') else '',
        'publisher': _merged_text(by_label['Publisher']) if by_label.get('Publisher') else '',
        'location': _merged_text(by_label['PublicationPlace']) if by_label.get('PublicationPlace') else '',
        'year': year,
        'year_suffix': year_suffix,
        'volume': clean_numeric_field(_merged_text(by_label['Volume'])) if by_label.get('Volume') else '',
        'issue': clean_numeric_field(_merged_text(by_label['Issue'])) if by_label.get('Issue') else '',
        'pages': clean_numeric_field(_merged_text(by_label['Pages'])) if by_label.get('Pages') else '',
        'doi': _merged_text(by_label['DOI']) if by_label.get('DOI') else '',
        'url': _merged_text(by_label['Webpage']) if by_label.get('Webpage') else '',
        'raw_text': clean_text(ref.get('text')),
        'source_text': clean_text(ref.get('reference_text') or ref.get('text')),
        'found_on_pages': footnote_pages(footnote) if footnote is not None else [],
    }


def footnote_pages(footnote: dict) -> List[int]:
    """The PDF pages a footnote is printed on - several when it runs on to the
    next page (its spans). Which of them holds a given reference isn't
    recorded, so a reference in such a footnote gets all of them."""
    pages = {span.get('page') or footnote.get('page') for span in footnote.get('spans') or []}
    if not pages:
        pages = {footnote.get('page')}
    return sorted(p for p in pages if isinstance(p, int))


def iter_references(result: dict):
    """Yields every reference across every footnote, in document order. A
    reference divided into works ("Martines 2009, 2012") yields one entry per
    work (pipeline/reference_split.work_view)."""
    for footnote in result.get('results', []):
        text = footnote.get('text') or ''
        for ref in sorted(footnote.get('references', []), key=lambda r: r.get('start', 0)):
            if ref.get('works'):
                for work in sorted(ref['works'], key=lambda w: w['start']):
                    view = work_view(text, ref, work)
                    view['reference_text'] = ref.get('text') or text[ref.get('start', 0):ref.get('end', 0)]
                    yield footnote, view
            else:
                yield footnote, ref
