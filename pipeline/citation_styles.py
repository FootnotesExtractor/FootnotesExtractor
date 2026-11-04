#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
APA 7th / Chicago 17th (Notes-Bibliography) / MLA 9th plain-text citation
list generation, built on the normalized fields pipeline.citation_fields
extracts from each reference's component spans.

These always resolve ibidem/opus-citatum references back to the original
work's full citation (i.e. always "expanded") — a formal reference list/
bibliography in any of these styles never contains a literal "Ibid." entry,
unlike a sequential footnote stream where that's the whole point. If you
want ibidem left as-is, use the BibTeX Standard export instead.

Plain text can't render italics, so journal/book titles are wrapped in
*asterisks* (Markdown-style, trivially strippable) rather than silently
losing that distinction — every one of these styles relies on it to tell a
journal article's title from the journal's own name.

Best-effort, not citation-manager-grade: fields that weren't extracted are
omitted (not guessed), and this doesn't correct upstream NER/OCR mistakes.
"""

from typing import List

from pipeline.citation_fields import CitationFields, Person, extract_reference_fields, iter_references
from pipeline.reference_works import citation_works, where


def _italic(text: str) -> str:
    return f"*{text}*" if text else text


def _ensure_period(text: str) -> str:
    """Appends '.' unless the text already ends with sentence-ending
    punctuation (most commonly an initial like "K." — avoids the "K.."
    double-period that a blind `+ '.'` produces). A trailing comma is
    stripped first — templates often tack ", " onto a field in case
    something follows it (volume, pages, ...), and when that "something"
    turns out to be empty the comma would otherwise dangle right before the
    period (", .")."""
    text = text.strip().rstrip(',').strip()
    if not text or text[-1] in '.!?':
        return text
    return text + '.'


def _join_nonempty(parts: List[str], sep: str = ' ') -> str:
    return sep.join(p for p in parts if p)


# ---- author-list formatting, one style per citation style ----

def _apa_initials(first: str) -> str:
    if not first:
        return ''
    # Names that already look like initials (e.g. "K.", "J. M.") are left
    # alone; a full first name is reduced to its leading letters instead.
    tokens = first.replace('-', ' ').split()
    return ' '.join(t if t.endswith('.') else f"{t[0].upper()}." for t in tokens)


def _apa_person(p: Person) -> str:
    initials = _apa_initials(p['first'])
    if p['last'] and initials:
        return f"{p['last']}, {initials}"
    return p['last'] or p['raw']


def _format_authors_apa(people: List[Person]) -> str:
    if not people:
        return ''
    names = [_apa_person(p) for p in people]
    if len(names) == 1:
        return names[0]
    if len(names) <= 20:
        return ', '.join(names[:-1]) + f", & {names[-1]}"
    # APA 7 rule for 21+ authors: first 19, ellipsis, last author.
    return ', '.join(names[:19]) + f", ... {names[-1]}"


def _chicago_person(p: Person, inverted: bool) -> str:
    if p['last'] and p['first']:
        return f"{p['last']}, {p['first']}" if inverted else f"{p['first']} {p['last']}"
    return p['last'] or p['raw']


def _format_authors_chicago(people: List[Person]) -> str:
    if not people:
        return ''
    if len(people) == 1:
        return _chicago_person(people[0], inverted=True)
    if len(people) <= 3:
        rest = [_chicago_person(p, inverted=False) for p in people[1:]]
        if len(rest) == 1:
            return f"{_chicago_person(people[0], inverted=True)}, and {rest[0]}"
        return f"{_chicago_person(people[0], inverted=True)}, " + ', '.join(rest[:-1]) + f", and {rest[-1]}"
    return _chicago_person(people[0], inverted=True) + ', et al.'


def _mla_person(p: Person, inverted: bool) -> str:
    if p['last'] and p['first']:
        return f"{p['last']}, {p['first']}" if inverted else f"{p['first']} {p['last']}"
    return p['last'] or p['raw']


def _format_authors_mla(people: List[Person]) -> str:
    if not people:
        return ''
    if len(people) == 1:
        return _mla_person(people[0], inverted=True)
    if len(people) == 2:
        return _mla_person(people[0], inverted=True) + ', and ' + _mla_person(people[1], inverted=False)
    return _mla_person(people[0], inverted=True) + ', et al.'


def _lead_authors(f: CitationFields, formatter) -> str:
    authors = formatter(f['authors']) or formatter(f['editors'])
    return _ensure_period(authors) if authors else ''


# ---- APA 7 ----
# {Authors}. ({Year}). {Title}. *{Journal}*, {Volume}({Issue}), {Pages}.

def _apa_entry(f: CitationFields) -> str:
    lead = _lead_authors(f, _format_authors_apa)
    year_part = f"({f['year']}{f['year_suffix']})." if f['year'] else ''

    if f['entry_type'] == 'article':
        vol_issue = f"{f['volume']}({f['issue']})" if (f['volume'] and f['issue']) else f['volume']
        journal_bits = _join_nonempty([_italic(f['journal']) + ',' if f['journal'] else '',
                                        (vol_issue + ',') if (vol_issue and f['pages']) else vol_issue,
                                        f['pages']])
        body = _join_nonempty([_ensure_period(f['title']) if f['title'] else '', _ensure_period(journal_bits)])
    elif f['entry_type'] == 'incollection':
        editors = f"In {_format_authors_apa(f['editors'])} (Ed.), " if f['editors'] else ''
        pages = f" (pp. {f['pages']})" if f['pages'] else ''
        chapter = f"{editors}{_italic(f['booktitle'])}{pages}".strip()
        body = _join_nonempty([_ensure_period(f['title']) if f['title'] else '',
                                _ensure_period(chapter) if chapter else '',
                                _ensure_period(f['publisher']) if f['publisher'] else ''])
    elif f['entry_type'] == 'book':
        body = _join_nonempty([_ensure_period(_italic(f['title'])) if f['title'] else '',
                                _ensure_period(f['publisher']) if f['publisher'] else ''])
    elif f['entry_type'] == 'online':
        body = _join_nonempty([_ensure_period(f['title']) if f['title'] else '', f['url']])
    else:
        body = _ensure_period(f['title']) if f['title'] else ''

    result = _join_nonempty([lead, year_part, body])
    return result or f['raw_text']


# ---- Chicago 17 (Notes-Bibliography, bibliography-list form) ----
# {Author}. "{Title}." *{Journal}* {Volume}, no. {Issue} ({Year}): {Pages}.

def _chicago_entry(f: CitationFields) -> str:
    lead = _lead_authors(f, _format_authors_chicago)
    quoted_title = f'"{f["title"]}."' if f['title'] else ''

    if f['entry_type'] == 'article':
        vol_issue = _join_nonempty([f['volume'], f"no. {f['issue']}" if f['issue'] else ''], sep=', ')
        paren_year = f"({f['year']}{f['year_suffix']})" if f['year'] else ''
        journal_bits = _join_nonempty([vol_issue, paren_year])
        if f['pages']:
            journal_bits = f"{journal_bits}: {f['pages']}" if journal_bits else f['pages']
        journal_line = _ensure_period(_join_nonempty([_italic(f['journal']), journal_bits]))
        body = _join_nonempty([quoted_title, journal_line])
    elif f['entry_type'] == 'incollection':
        editors = f"edited by {_format_authors_chicago(f['editors'])}, " if f['editors'] else ''
        place_pub = _join_nonempty([f['location'], f['publisher']], sep=': ')
        tail = _ensure_period(_join_nonempty([place_pub, f['year']], sep=', '))
        chapter_line = _ensure_period(f"In {_italic(f['booktitle'])}, {editors}{f['pages']}".strip().rstrip(','))
        body = _join_nonempty([quoted_title, chapter_line if f['booktitle'] else '', tail])
    elif f['entry_type'] == 'book':
        place_pub_year = _ensure_period(_join_nonempty([_join_nonempty([f['location'], f['publisher']], sep=': '), f['year']], sep=', '))
        body = _join_nonempty([_ensure_period(_italic(f['title'])) if f['title'] else '', place_pub_year])
    elif f['entry_type'] == 'online':
        body = _join_nonempty([quoted_title, _ensure_period(f['url']) if f['url'] else ''])
    else:
        body = quoted_title

    result = _join_nonempty([lead, body])
    return result or f['raw_text']


# ---- MLA 9 ----
# {Author}. "{Title}." *{Journal}*, vol. {Volume}, no. {Issue}, {Year}, pp. {Pages}.

def _mla_entry(f: CitationFields) -> str:
    lead = _lead_authors(f, _format_authors_mla)
    quoted_title = f'"{f["title"]}."' if f['title'] else ''

    if f['entry_type'] == 'article':
        tail = _join_nonempty([
            f"vol. {f['volume']}," if f['volume'] else '',
            f"no. {f['issue']}," if f['issue'] else '',
            f"{f['year']}," if f['year'] else '',
            f"pp. {f['pages']}" if f['pages'] else '',
        ])
        journal_line = _ensure_period(_join_nonempty([_italic(f['journal']) + ',' if f['journal'] else '', tail]))
        body = _join_nonempty([quoted_title, journal_line])
    elif f['entry_type'] == 'incollection':
        editors = f"edited by {_format_authors_mla(f['editors'])}," if f['editors'] else ''
        tail = _join_nonempty([editors, f"{f['publisher']}," if f['publisher'] else '',
                                f"{f['year']}," if f['year'] else '', f"pp. {f['pages']}" if f['pages'] else ''])
        chapter_line = _ensure_period(_join_nonempty([_italic(f['booktitle']) + ',' if f['booktitle'] else '', tail]))
        body = _join_nonempty([quoted_title, chapter_line])
    elif f['entry_type'] == 'book':
        tail = _ensure_period(_join_nonempty([f"{f['publisher']}," if f['publisher'] else '', f['year']]))
        body = _join_nonempty([_ensure_period(_italic(f['title'])) if f['title'] else '', tail])
    elif f['entry_type'] == 'online':
        tail = _ensure_period(_join_nonempty([f"{f['year']}," if f['year'] else '', f['url']]))
        body = _join_nonempty([quoted_title, tail])
    else:
        body = quoted_title

    result = _join_nonempty([lead, body])
    return result or f['raw_text']


_STYLE_FORMATTERS = {
    'apa': (_apa_entry, 'APA 7th Edition'),
    'chicago': (_chicago_entry, 'Chicago 17th Edition (Notes-Bibliography)'),
    'mla': (_mla_entry, 'MLA 9th Edition'),
}


def _citations(work) -> str:
    """Under a deduplicated entry: each place the work is cited, one per line."""
    return ''.join(f"\n   – {where(c)}: {c['text']}" for c in work['citations'])


def generate_citation_list(result: dict, style: str, deduplicate: bool = False) -> str:
    """style: 'apa' | 'chicago' | 'mla'. Always resolves ibidem/opus-citatum
    (see module docstring). One numbered entry per reference, in document
    order; deduplicate: one per work (pipeline/reference_works.py), with each
    place it is cited listed under it."""
    formatter, label = _STYLE_FORMATTERS[style]

    if deduplicate:
        works = citation_works(result)
        entries = [formatter(extract_reference_fields(w['ref'], expanded=True, footnote=w['footnote'])) + _citations(w)
                   for w in works]
        count = f"{len(entries)} work(s) from {sum(len(w['citations']) for w in works)} reference(s), each work once"
    else:
        entries = [formatter(extract_reference_fields(ref, expanded=True, footnote=fn)) for fn, ref in iter_references(result)]
        count = f"{len(entries)} reference(s)"

    if not entries:
        return "No references were extracted from this document.\n"

    header = f"{label} — {count}, ibidem/opus-citatum resolved to the original work\n\n"
    body = '\n\n'.join(f"{i}. {entry}" for i, entry in enumerate(entries, start=1))
    return header + body + '\n'


def generate_reference_strings(result: dict, deduplicate: bool = False) -> str:
    """Plain listing of each reference's own extracted text, verbatim — no
    reformatting, just what the pipeline actually read off the page
    (including a literal "Ibid."/"Op. cit." if that's what was printed).
    deduplicate: each work once, by its fullest citation, with every place
    it is cited listed under it."""
    if deduplicate:
        works = citation_works(result)
        entries = [(w['ref'].get('text') or '').strip() + _citations(w) for w in works]
        count = f"{len(entries)} work(s) from {sum(len(w['citations']) for w in works)} reference string(s), each work once"
        sep = '\n\n'
    else:
        entries = [ref.get('text', '').strip() for _fn, ref in iter_references(result)]
        entries = [e for e in entries if e]
        count = f"{len(entries)} reference string(s)"
        sep = '\n'

    if not entries:
        return "No references were extracted from this document.\n"

    header = f"{count}, as extracted (unresolved, unformatted)\n\n"
    body = sep.join(f"{i}. {entry}" for i, entry in enumerate(entries, start=1))
    return header + body + '\n'
