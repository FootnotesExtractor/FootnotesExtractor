#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generates BibLaTeX (.bib) output from a job's structured results — the
"BibTeX Standard"/"BibTeX Expanded" export buttons that existed in the UI
but weren't actually wired to anything before this.

One entry per *reference* (not per footnote — a single footnote can cite
several works, e.g. "See X; see also Y"), built from the normalized fields
pipeline.citation_fields extracts from that reference's component spans.

Standard vs Expanded only differ for ibidem/opus-citatum references (see
citation_fields.extract_reference_fields's docstring) — Standard keeps them
sparse and literal ("Ibid., p. 12"), Expanded resolves them back to the
original work's full citation.
"""

import re
from typing import Dict

from pipeline.citation_fields import CitationFields, extract_reference_fields, iter_references
from pipeline.reference_works import citation_works, where


_BIBTEX_ESCAPE_MAP = {
    '\\': r'\textbackslash{}',
    '{': r'\{',
    '}': r'\}',
    '$': r'\$',
    '&': r'\&',
    '%': r'\%',
    '#': r'\#',
    '_': r'\_',
    '~': r'\textasciitilde{}',
    '^': r'\textasciicircum{}',
}


def _escape(text: str) -> str:
    if not text:
        return ''
    return ''.join(_BIBTEX_ESCAPE_MAP.get(ch, ch) for ch in text)


def _format_people(people) -> str:
    parts = []
    for p in people:
        parts.append(f"{p['last']}, {p['first']}" if p['last'] and p['first'] else (p['last'] or p['first']))
    return ' and '.join(part for part in parts if part)


_KEY_STRIP_RE = re.compile(r'[^0-9A-Za-z]+')


def _make_cite_key(author_text: str, year_text: str, used_keys: Dict[str, int]) -> str:
    base_author = _KEY_STRIP_RE.sub('', author_text.split(',')[0]) or 'Ref'
    base_year = _KEY_STRIP_RE.sub('', year_text) or ''
    base = f"{base_author}{base_year}" or 'Ref'

    count = used_keys.get(base, 0)
    used_keys[base] = count + 1
    if count == 0:
        return base
    # a, b, c, ... aa, ab, ... for the rare case of >26 collisions
    suffix = ''
    n = count
    while True:
        n, rem = divmod(n - 1, 26)
        suffix = chr(97 + rem) + suffix
        if n == 0:
            break
    return f"{base}{suffix}"


def _pages_note(pages) -> str:
    """'PDF page 10', 'PDF pages 10–11' (a footnote running on), 'PDF pages 10, 12'."""
    if len(pages) == 1:
        return f'PDF page {pages[0]}'
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f'PDF pages {pages[0]}–{pages[-1]}'
    return 'PDF pages ' + ', '.join(str(p) for p in pages)


def _fields_to_entry(f: CitationFields, expanded: bool, used_keys: Dict[str, int], provenance: bool = False,
                     cited=None) -> str:
    """cited: with a deduplicated export, every citation of the work
    (reference_works.citation_works), listed in the note instead of the one
    reference's provenance."""
    bibtex_fields: Dict[str, str] = {}

    if f['authors']:
        bibtex_fields['author'] = _format_people(f['authors'])
    if f['editors']:
        bibtex_fields['editor'] = _format_people(f['editors'])
    if f['translators']:
        bibtex_fields['translator'] = _format_people(f['translators'])
    if f['title']:
        bibtex_fields['title'] = f['title']
    if f['booktitle']:
        bibtex_fields['booktitle'] = f['booktitle']
    if f['journal']:
        bibtex_fields['journaltitle'] = f['journal']
    if f['publisher']:
        bibtex_fields['publisher'] = f['publisher']
    if f['location']:
        bibtex_fields['location'] = f['location']
    if f['year']:
        bibtex_fields['date'] = f"{f['year']}{f['year_suffix']}"
    if f['volume']:
        bibtex_fields['volume'] = f['volume']
    if f['issue']:
        bibtex_fields['number'] = f['issue']
    if f['pages']:
        bibtex_fields['pages'] = f['pages']
    if f['doi']:
        bibtex_fields['doi'] = f['doi']
    if f['url']:
        bibtex_fields['url'] = f['url']

    # Standard mode on an ibidem/opus-citatum reference: fields are just the
    # marker (+ maybe Pages) by design, so surface that literally instead of
    # silently emitting an otherwise-empty entry.
    note = []
    if not expanded and f['citation_type'] == 'ibidem':
        note.append('Ibid.')
    elif not expanded and f['citation_type'] == 'opus_citatum':
        note.append('Op. cit.')
    if cited:
        # Where the work is cited, e.g.
        #   note = {Cited 2 times: PDF page 10, footnote 5: "J. Smith, Old Books, s. 5."; PDF page 12, footnote 9: "Ibid., s. 7."}
        note.append(f"Cited {len(cited)} time{'s' if len(cited) != 1 else ''}: "
                    + '; '.join(f'{where(c)}: "{c["text"]}"' for c in cited))
    elif provenance:
        # Where the entry came from: the reference as printed and the PDF
        # page(s) of its footnote, e.g.
        #   note = {Ibid.; Original reference: "Tamże, s. 12."; PDF page 10}
        if f['source_text']:
            note.append(f'Original reference: "{f["source_text"]}"')
        if f['found_on_pages']:
            note.append(_pages_note(f['found_on_pages']))
    if note:
        bibtex_fields['note'] = '; '.join(note)

    if not bibtex_fields:
        # Nothing was extracted at all (e.g. a stray short span with no
        # components) — fall back to the raw reference text so the entry is
        # still traceable back to the source rather than silently omitted.
        bibtex_fields['note'] = f['raw_text']

    key = _make_cite_key(bibtex_fields.get('author', ''), f['year'], used_keys)

    lines = [f"@{f['entry_type']}{{{key},"]
    for name, value in bibtex_fields.items():
        if value:
            lines.append(f"  {name} = {{{_escape(value)}}},")
    lines.append("}")
    return '\n'.join(lines)


def generate_bibtex(result: dict, expanded: bool = False, provenance: bool = False, deduplicate: bool = False) -> str:
    """Builds a full .bib file (one entry per reference) from a job's
    structured result dict (the same shape as api_results()/download JSON).

    provenance: each entry's note also gives the reference as printed and the
    PDF page(s) it was found on - the app's exports. Off for callers that
    compare `note` and look for back-reference markers in it.

    deduplicate: one entry per work instead (pipeline/reference_works.py),
    from its fullest citation, the note listing every citation - page,
    footnote and the reference as printed (the app's exports)."""
    entries = []
    used_keys: Dict[str, int] = {}
    if deduplicate:
        works = citation_works(result)
        for work in works:
            fields = extract_reference_fields(work['ref'], expanded, work['footnote'])
            entries.append(_fields_to_entry(fields, expanded, used_keys, provenance, cited=work['citations']))
        count = f"{len(entries)} work(s) from {sum(len(w['citations']) for w in works)} reference(s), each work once"
    else:
        for footnote, ref in iter_references(result):
            fields = extract_reference_fields(ref, expanded, footnote)
            entries.append(_fields_to_entry(fields, expanded, used_keys, provenance))
        count = f"{len(entries)} reference(s)"

    if not entries:
        return "% No references were extracted from this document.\n"

    header = (
        "% Generated by FootnotesExtractor\n"
        f"% {count}, "
        f"{'ibidem/opus-citatum resolved to the original work' if expanded else 'ibidem/opus-citatum left as-is'}\n"
        + ("% note: every place the work is cited - PDF page, footnote number, the reference as printed\n" if deduplicate
           else "% note: the reference as printed and the PDF page(s) of the footnote it was found in\n" if provenance else '')
        + "\n"
    )
    return header + '\n\n'.join(entries) + '\n'
