"""A document's footnote references as a deduplicated list - each work once,
with every place it is cited (exports deduplicate, but say
where each reference was found: the page, the footnote's number and the
reference as printed).

The works are the Reference List Linker's (reference_linker.distinct_works,
default level): an ibid. or op. cit. is the
work it was resolved to; any other reference is an earlier work again when
the linker's rule matches it with that work's first citation and the two name
no different people. A reference with neither a title nor an author is a
work of its own. The document is read in the viewer's order
(reference_inference.document_order).

Each work is represented by its fullest citation: of its full references
(not ibid./op. cit.), the one with the most tagged parts, the first if
several; with none, its first citation.
"""
from typing import List

from pipeline.citation_fields import extract_reference_fields, footnote_pages, iter_references
from pipeline.reference_inference import document_order
from pipeline.reference_linker import DEFAULT_LEVEL, distinct_works, footnote_items, printed_number


def citation_works(result: dict, level: str = DEFAULT_LEVEL) -> List[dict]:
    """[{'footnote', 'ref': the work's representative citation, 'citations':
    [{'footnote', 'ref', 'pages', 'footnote_number', 'text'}]}], in the order
    the works are first cited."""
    ordered = {'results': document_order(result.get('results') or [])}
    position = {id(f): n for n, f in enumerate(ordered['results'], 1)}
    work_of = {rid: n for n, ids in enumerate(distinct_works(footnote_items(ordered), level)) for rid in ids}
    works, by_key = [], {}
    for footnote, ref in iter_references(ordered):
        n = work_of.get(ref.get('id'))
        key = n if n is not None else ('alone', len(works))
        if key not in by_key:
            by_key[key] = {'citations': []}
            works.append(by_key[key])
        fields = extract_reference_fields(ref, False, footnote)
        by_key[key]['citations'].append({
            'footnote': footnote, 'ref': ref, 'pages': footnote_pages(footnote),
            'footnote_number': printed_number(footnote) or position.get(id(footnote)),
            'text': fields['source_text'] or fields['raw_text']})
    for work in works:
        full = [c for c in work['citations'] if (c['ref'].get('citation_type') or 'normal') == 'normal']
        main = max(full, key=lambda c: len(c['ref'].get('components') or []), default=work['citations'][0])
        work['footnote'], work['ref'] = main['footnote'], main['ref']
    return works


def _pages(pages) -> str:
    if not pages:
        return 'PDF page ?'
    if len(pages) == 1:
        return f'PDF page {pages[0]}'
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f'PDF pages {pages[0]}–{pages[-1]}'
    return 'PDF pages ' + ', '.join(str(p) for p in pages)


def where(citation: dict) -> str:
    """'PDF page 10, footnote 5'."""
    return f"{_pages(citation['pages'])}, footnote {citation['footnote_number'] or '?'}"
