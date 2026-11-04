#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reference List Linker: a book's reference list - its own ("internal", found
inside the book) or one given as BibTeX - linked to the references in its
footnotes.

The list is stored per job in results/<job>_reference_list.json (the app
writes it, see app.py): the entries and the reviewers' decisions. Links are
not stored - they are worked out from the job's current footnotes each time
(build_view), so an edited footnote, or a new matcher, takes effect at once;
the decisions (confirm / reject / add) are applied on top and always win.

Entries share one shape whatever their source:
    {id, source, text, fields: {author, editor, title, year, journal, booktitle, publisher, place, ...},
     page?, bbox?, bibtex_key?}
"""

import difflib
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from pipeline.citation_fields import extract_reference_fields, footnote_pages, iter_references


# ---- matching ------------------------------------------------------------
#
# The matching method is expected to change. Everything
# else goes through match() and MATCHER_VERSION: a new method replaces
# match() and bumps the version; links are recomputed on the next read, and
# the reviewers' decisions, stored apart, stay as they are.

MATCHER_VERSION = 4
TITLE_THRESHOLD = 0.8
LEVELS = ('strict', 'balanced', 'lenient')
DEFAULT_LEVEL = 'balanced'
PARTIAL_THRESHOLD = 0.9          # balanced: the shorter title inside the longer one
TEXT_THRESHOLD = 0.85            # lenient: the footnote's title inside the entry's whole text
MIN_PARTIAL = 15                 # characters (and 3 words) a title needs for either


def _fold(s: Optional[str]) -> str:
    s = unicodedata.normalize('NFKD', s or '')
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in s if not unicodedata.combining(c)).lower()).strip()


def _surname(author: Optional[str]) -> str:
    first = re.split(r'\s+and\s+|;', author or '')[0].strip()
    part = first.split(',')[0] if ',' in first else (first.split()[-1] if first.split() else '')
    return _fold(part)


def _surnames(author: Optional[str]) -> set:
    """Every author's folded surname ("Burchell, G and Miller, P" -> {burchell, miller})."""
    out = set()
    for name in re.split(r'\s+and\s+|;', author or ''):
        name = name.strip()
        part = name.split(',')[0] if ',' in name else (name.split()[-1] if name.split() else '')
        if _fold(part):
            out.add(_fold(part))
    return out


def _people(author: Optional[str]) -> List[List[str]]:
    """Each author's name as folded parts: "Furuyama Masao and M. Kowalski"
    -> [['furuyama', 'masao'], ['m', 'kowalski']]."""
    return [p for p in (_fold(n).split() for n in re.split(r'\s+and\s+|;', author or '')) if p]


def _same_person(p: List[str], q: List[str]) -> bool:
    """Two spellings of one name, in either order and with initials
   : "Furuyama Masao" = "M. Furuyama" = "Furuyama, M.".
    They share a full name part (3+ letters), and what is left does not
    disagree - its first parts start with the same letter - so "Kowalski Jan"
    and "Kowalski Adam" stay apart."""
    shared = {t for t in p if len(t) >= 3} & {t for t in q if len(t) >= 3}
    if not shared:
        return False
    rest_p, rest_q = [t for t in p if t not in shared], [t for t in q if t not in shared]
    return not rest_p or not rest_q or rest_p[0][0] == rest_q[0][0]


def _year(fields: dict) -> str:
    m = re.search(r'\d{4}', fields.get('year') or '')
    return m.group(0) if m else ''


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _partial_ratio(a: str, b: str) -> float:
    """How well the shorter string matches its best-aligned stretch of the
    longer one (fuzzywuzzy's partial_ratio, with difflib): 1.0 when one title
    sits word for word inside the other."""
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if not short:
        return 0.0
    best = 0.0
    for i, j, _size in difflib.SequenceMatcher(None, short, long_, autojunk=False).get_matching_blocks():
        start = max(0, j - i)
        best = max(best, difflib.SequenceMatcher(None, short, long_[start:start + len(short)]).ratio())
        if best > 0.995:
            break
    return best


def _long_enough(folded: str) -> bool:
    return len(folded) >= MIN_PARTIAL and len(folded.split()) >= 3


def _words(folded: str) -> set:
    return {w for w in folded.split() if len(w) >= 4}


class _Prep:
    """A side's fields folded once, not on every comparison."""
    __slots__ = ('title', 'words', 'surname', 'surnames', 'people', 'name_parts', 'year', 'text', 'text_words')

    def __init__(self, d: dict, with_text: bool = False):
        f = d['fields']
        self.title = _fold(f.get('title'))
        self.words = _words(self.title)
        self.surname = _surname(f.get('author') or f.get('editor'))   # an edited volume by its editor
        names = ' and '.join(x for x in (f.get('author'), f.get('editor')) if x)
        self.surnames = _surnames(names)
        self.people = _people(names)
        self.name_parts = {t for person in self.people for t in person if len(t) >= 3}
        self.year = _year(f)
        self.text = _fold(d.get('text')) if with_text else ''
        self.text_words = _words(self.text) if with_text else set()


def _mostly_inside(words: set, other: set) -> bool:
    """Cheap gate before a partial alignment: most of the shorter side's words
    occur in the other."""
    return bool(words) and len(words & other) >= 0.6 * len(words)


def match(item: dict, entry: dict, _sm: Optional[difflib.SequenceMatcher] = None,
          level: str = 'strict') -> Optional[Tuple[float, str]]:
    """(score, rule) when a footnote reference and a list entry are the same
    work, else None. Three levels, from strict to lenient:

      strict    titles at least
                0.8 alike - or, when a title is missing, as in author-year
                citations, the first author's surname and the year agreeing
                (rules 'title', 'author+year')
      balanced  also the shorter title almost word for word inside the longer
                one - a subtitle, a cut-off parse, two works run together in
                one record ('title-inside', >= 0.9); and surname + year
                agreeing where the titles are at least half alike
                ('author+year+title')
      lenient   also surname + year alone ('author+year'), and the footnote's
                title found in the entry's whole text ('text', >= 0.85)

    At balanced and lenient the author may be any of either side's authors
    (editors included), not only the first - an edited volume, authors in
    another order; such rules read 'co-author+year...'.
    Names are compared as people there, in either order and with initials
    ("Furuyama Masao" = "M. Furuyama"). Strict keeps the first-author
    surname rule."""
    return _match(_Prep(item), _Prep(entry, with_text=level == 'lenient'), _sm, level)


def _match(a: '_Prep', b: '_Prep', sm, level, partial=True):
    ta, tb = a.title, b.title
    same_year = bool(a.year and a.year == b.year)
    first_author = bool(a.surname and b.surname
                        and (a.surname == b.surname or _sim(a.surname, b.surname) >= TITLE_THRESHOLD))
    if not first_author and level != 'strict' and a.people and b.people:
        first_author = _same_person(a.people[0], b.people[0])
    co_author = level != 'strict' and not first_author and (
        bool(a.surnames & b.surnames) or any(_same_person(p, q) for p in a.people for q in b.people))
    author_year = same_year and (first_author or co_author)
    who = 'co-author' if co_author else 'author'
    if len(ta) >= 5 and len(tb) >= 5:
        if sm is None:
            sm = difflib.SequenceMatcher(None, '', ta)
        sm.set_seq1(tb)
        score = None
        # the cheap upper bounds first; ratio() only for what could pass
        if sm.real_quick_ratio() >= TITLE_THRESHOLD and sm.quick_ratio() >= TITLE_THRESHOLD:
            score = sm.ratio()
            if score >= TITLE_THRESHOLD:
                return score, 'title'
        if level == 'strict':
            return None
        short, long_ = (a, b) if len(ta) <= len(tb) else (b, a)
        if partial and _long_enough(short.title) and _mostly_inside(short.words, long_.words):
            p = _partial_ratio(ta, tb)
            if p >= PARTIAL_THRESHOLD:
                return p, 'title-inside'
        if author_year:
            score = sm.ratio() if score is None else score
            if score >= 0.5:
                return score, f'{who}+year+title'
            if level == 'lenient':
                return 0.7, f'{who}+year'
    elif author_year:
        if co_author:
            return 0.9, 'co-author+year'
        if a.surname == b.surname:
            return 1.0, 'author+year'
        sim = _sim(a.surname, b.surname)
        return (sim if sim >= TITLE_THRESHOLD else 0.95), 'author+year'   # 0.95: the same person, written otherwise
    if level == 'lenient' and _long_enough(ta) and _mostly_inside(a.words, b.text_words):
        p = _partial_ratio(ta, b.text)
        if p >= TEXT_THRESHOLD:
            return p, 'text'
    return None


class _Index:
    """Candidate entries for an item, so a 300-entry list against 300
    references doesn't compare every pair: entries sharing a title word (at
    the lenient level also a word of the entry's text), or the year and the
    surname's first letters."""

    MAX_TITLE_CANDIDATES = 40

    def __init__(self, entries, level='strict'):
        self.entries = entries
        self.prep = [_Prep(e, with_text=level == 'lenient') for e in entries]
        self.by_word, self.by_year = {}, {}
        for i, p in enumerate(self.prep):
            for w in p.words | p.text_words:
                self.by_word.setdefault(w, set()).add(i)
            if p.year:
                self.by_year.setdefault(p.year, set()).add(i)

    def candidates(self, item: '_Prep'):
        """Indexes of the entries sharing the most words first (at most
        MAX_TITLE_CANDIDATES), then those of the same year whose author's
        surname starts alike."""
        shared = {}
        for w in item.words:
            for i in self.by_word.get(w, ()):
                shared[i] = shared.get(i, 0) + 1
        found = sorted(shared, key=lambda i: (-shared[i], i))[:self.MAX_TITLE_CANDIDATES]
        if item.year and (item.surnames or item.name_parts):
            # a surname starting alike (typos), or a whole name part in common
            # (names in another order: "Furuyama Masao" / "M. Furuyama")
            starts = {s[:2] for s in item.surnames}
            found += [i for i in sorted(self.by_year.get(item.year, ())) if i not in shared
                      and (any(s[:2] in starts for s in self.prep[i].surnames)
                           or item.name_parts & self.prep[i].name_parts)]
        return found


def auto_links(items: List[dict], entries: List[dict], level: str = DEFAULT_LEVEL) -> List[dict]:
    """Each footnote reference linked to its best-matching entry, if any."""
    index = _Index(entries, level)
    links = []
    for item in items:
        a = _Prep(item)
        best = None
        sm = difflib.SequenceMatcher(None, '', a.title)
        for rank, i in enumerate(index.candidates(a)):
            # the costly title-inside alignment only for the entries sharing the
            # most words (candidates come in that order)
            m = _match(a, index.prep[i], sm, level, partial=rank < 10)
            if m and (best is None or m[0] > best[0]):
                best = (m[0], m[1], entries[i]['id'])
                if best[0] >= 0.999:            # can't do better
                    break
        if best:
            links.append({'reference_id': item['reference_id'], 'entry_id': best[2], 'score': round(best[0], 3),
                          'rule': best[1], 'matcher_version': MATCHER_VERSION, 'status': 'auto'})
    return links


def _no_other_people(a: '_Prep', b: '_Prep') -> bool:
    """Whether two references name a person in common, or one names no one."""
    return not (a.people and b.people) or bool(a.surnames & b.surnames) \
        or any(_same_person(p, q) for p in a.people for q in b.people)


def distinct_works(items: List[dict], level: str = DEFAULT_LEVEL) -> List[List[str]]:
    """The footnote references grouped by the work they cite, so that a work
    cited five times counts once. In order:

      ibid., op. cit.  the work of the reference it was resolved to
                       (inferred_from, pipeline/reference_inference)
      any other        an earlier work again when it matches that work's
                       first reference by the rule it would be linked to a
                       list entry with (_match at `level`; the best-matching
                       work if several) and the two name no different people
                       ("Herbarz polski" by Niesiecki is not Boniecki's);
                       otherwise a work of its own

    Only the references with a title or an author (the linkable ones). Each
    work as the reference ids of its citations."""
    works, firsts, work_of = [], [], {}
    for item in items:
        if not (item['fields'].get('title') or item['fields'].get('author')):
            continue
        n = work_of.get(item.get('inferred_from'))
        if n is None:
            a = _Prep(item)
            sm = difflib.SequenceMatcher(None, '', a.title)
            best = None
            for k, b in enumerate(firsts):
                m = _no_other_people(a, b) and _match(a, b, sm, level)
                if m and (best is None or m[0] > best[0]):
                    best = (m[0], k)
            n = best[1] if best else None
        if n is None:
            n = len(works)
            works.append([])
            firsts.append(_Prep(item, with_text=level == 'lenient'))
        works[n].append(item['reference_id'])
        work_of[item['reference_id']] = n
    return works


# ---- the footnote side ---------------------------------------------------

def _all_authors(people) -> str:
    """'Last, First and Last, First ...' - the first author first, as in BibTeX."""
    names = []
    for p in people or []:
        if p.get('last') or p.get('first'):
            names.append(f"{p['last']}, {p['first']}".strip(', ') if p.get('last') else p['first'])
    return ' and '.join(names)


_PRINTED_NUMBER = re.compile(r'^\s*\[?([0-9]{1,3})\]?(?=[\s.)\]])')


def printed_number(footnote: dict) -> Optional[int]:
    """The footnote's number as printed at the start of its text ("2 E.
    Śnieżyńska-Stolot, …"), the same rule as consistency_checks.
    _leading_marker, or None. Shown as "fn N" rather than the footnote's
    position in the document, which is off wherever an extra note (an
    author's "*", an editorial note) comes first or numbering restarts
   ."""
    m = _PRINTED_NUMBER.match(footnote.get('text') or '')
    return int(m.group(1)) if m else None


def footnote_items(result: dict) -> List[dict]:
    """Every reference in the footnotes as an item to link: works split out
    of a multi-work reference separately, ibid./op. cit. through the work
    they refer back to (expanded fields)."""
    numbers = {id(f): n for n, f in enumerate(result.get('results') or [], 1)}
    items = []
    for footnote, ref in iter_references(result):
        if not ref.get('id'):
            continue
        f = extract_reference_fields(ref, True, footnote)
        items.append({
            'reference_id': ref['id'],
            'footnote_id': footnote.get('id'),
            'footnote_number': printed_number(footnote) or numbers.get(id(footnote)),
            'footnote_position': numbers.get(id(footnote)),
            'pages': footnote_pages(footnote),
            'text': f['source_text'] or f['raw_text'],
            'citation_type': f['citation_type'],
            'inferred_from': ref.get('inferred_from'),        # an ibid./op. cit.'s source (distinct_works)
            'fields': {'author': _all_authors((f['authors'] or []) + (f['editors'] or [])), 'title': f['title'],
                       'year': f['year']},
        })
    return items


# ---- the list side: internal (the book's own list) ------------------------

_COMPONENT_FIELDS = {'Author': 'author', 'Editor': 'author', 'Title': 'title', 'PublicationYear': 'year',
                     'Date': 'year', 'Year': 'year', 'Journal': 'journal', 'BookTitle': 'booktitle',
                     'Publisher': 'publisher'}


def _share_inside(a: dict, b: dict) -> float:
    """Share of box a's area inside box b (the viewer's bboxShareInside)."""
    w = min(a['x2'], b['x2']) - max(a['x1'], b['x1'])
    h = min(a['y2'], b['y2']) - max(a['y1'], b['y1'])
    area = (a['x2'] - a['x1']) * (a['y2'] - a['y1'])
    return w * h / area if w > 0 and h > 0 and area > 0 else 0.0


def internal_entries(result: dict, parsed_list: List[dict]) -> List[dict]:
    """Entries from the reference list an engine found inside the book
    (today GROBID's `grobid_references`: text, page, bbox, components). Entries
    lying in one of our footnote boxes are footnotes the engine took for a
    bibliography, and are left out - as in the viewer (referenceListOnPage)."""
    boxes = {}
    for f in result.get('results') or []:
        for span in f.get('spans') or [{'page': f.get('page'), 'bbox': f.get('bbox')}]:
            page, bbox = span.get('page') or f.get('page'), span.get('bbox') or f.get('bbox')
            if bbox:
                boxes.setdefault(page, []).append(bbox)
    entries = []
    for n, ref in enumerate(parsed_list or [], 1):
        if ref.get('bbox') and any(_share_inside(ref['bbox'], b) >= 0.5 for b in boxes.get(ref.get('page'), [])):
            continue
        fields = {}
        for c in ref.get('components') or []:
            name, text = _COMPONENT_FIELDS.get(c.get('label')), (c.get('text') or '').strip()
            if not name or not text:
                continue
            if name == 'author' and fields.get('author'):
                fields['author'] += ' and ' + text
            else:
                fields.setdefault(name, text)
        entries.append({'id': f"i{n}", 'source': 'internal', 'text': ref.get('text') or '', 'fields': fields,
                        'page': ref.get('page'), 'bbox': ref.get('bbox')})
    return entries


# ---- the list side: BibTeX -------------------------------------------------

def _unbrace(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').replace('{', '').replace('}', '')).strip()


def bibtex_entries(text: str) -> List[dict]:
    """Entries from BibTeX text (a file or text pasted in the viewer). TeX
    accents become Unicode; braces are dropped. `text` is the entry's note
    or howpublished when it has one (our own exports put the reference as
    printed there), else it is put together from the fields."""
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    from bibtexparser.customization import convert_to_unicode

    parser = BibTexParser(common_strings=True)
    parser.customization = convert_to_unicode
    parser.ignore_nonstandard_types = False
    db = bibtexparser.loads(text or '', parser=parser)
    entries, seen = [], {}
    for e in db.entries:
        key = e.get('ID') or 'entry'
        seen[key] = seen.get(key, 0) + 1
        eid = f"b:{key}" if seen[key] == 1 else f"b:{key}:{seen[key]}"
        year = e.get('year') or (e.get('date') or '')[:4]
        # every part a list can carry; people joined with " and " as in BibTeX
        fields = {'author': _unbrace(e.get('author')), 'editor': _unbrace(e.get('editor')),
                  'translator': _unbrace(e.get('translator')), 'title': _unbrace(e.get('title')),
                  'year': _unbrace(year), 'booktitle': _unbrace(e.get('booktitle')),
                  'journal': _unbrace(e.get('journal') or e.get('journaltitle')),
                  'publisher': _unbrace(e.get('publisher')),
                  'place': _unbrace(e.get('address') or e.get('location')),
                  'volume': _unbrace(e.get('volume')), 'issue': _unbrace(e.get('number') or e.get('issue')),
                  'pages': _unbrace(e.get('pages')), 'doi': _unbrace(e.get('doi')), 'url': _unbrace(e.get('url'))}
        fields = {k: v for k, v in fields.items() if v}
        shown = _unbrace(e.get('note') or e.get('howpublished'))
        if not shown:
            shown = ', '.join(v for v in (fields.get('author') or fields.get('editor'), fields.get('title'),
                                          fields.get('journal') or fields.get('booktitle'),
                                          fields.get('publisher'), fields.get('year')) if v)
        entries.append({'id': eid, 'source': 'bibtex', 'bibtex_key': key, 'text': shown, 'fields': fields})
    return entries


# ---- putting it together ----------------------------------------------------

def apply_decisions(links: List[dict], decisions: List[dict], entry_ids: set, reference_ids: set) -> List[dict]:
    """Reviewers' decisions over the automatic links: a rejected pair goes, a
    confirmed one is marked, an added one is created. A reference keeps at
    most one link; a confirmed or added link replaces its automatic one."""
    by_pair = {(d['reference_id'], d['entry_id']): d for d in decisions}
    forced = {d['reference_id'] for d in decisions if d['status'] in ('confirmed', 'added')}
    out = []
    for link in links:
        d = by_pair.get((link['reference_id'], link['entry_id']))
        if d and d['status'] == 'rejected':
            continue
        if d and d['status'] == 'confirmed':
            out.append({**link, 'status': 'confirmed', 'by': d.get('user'), 'at': d.get('at')})
        elif link['reference_id'] not in forced:
            out.append(link)
    have = {(l['reference_id'], l['entry_id']) for l in out}
    for d in decisions:
        pair = (d['reference_id'], d['entry_id'])
        if d['status'] in ('confirmed', 'added') and pair not in have \
                and d['entry_id'] in entry_ids and d['reference_id'] in reference_ids:
            out.append({'reference_id': d['reference_id'], 'entry_id': d['entry_id'], 'score': None,
                        'rule': 'reviewer', 'matcher_version': None,
                        'status': 'added' if d['status'] == 'added' else 'confirmed',
                        'by': d.get('user'), 'at': d.get('at')})
            have.add(pair)
    return out


def build_view(result: dict, stored: dict, level: str = DEFAULT_LEVEL) -> dict:
    """What the viewer and the API show: entries, items, links, and what
    stays unlinked on either side, with automatic links at `level`."""
    entries = stored.get('entries') or []
    items = footnote_items(result)
    links = apply_decisions(auto_links(items, entries, level), stored.get('decisions') or [],
                            {e['id'] for e in entries}, {i['reference_id'] for i in items})
    linked_refs = {l['reference_id'] for l in links}
    linked_entries = {l['entry_id'] for l in links}
    linkable = [i for i in items if i['fields'].get('title') or i['fields'].get('author')]
    return {
        'source': stored.get('source'), 'engine': stored.get('engine'), 'state': stored.get('state', 'ready'),
        'error': stored.get('error'), 'built_at': stored.get('built_at'), 'built_by': stored.get('built_by'),
        'matcher_version': MATCHER_VERSION, 'match_level': level,
        'entries': entries, 'items': items, 'links': links,
        'unlinked_references': [i['reference_id'] for i in linkable if i['reference_id'] not in linked_refs],
        'unlinked_entries': [e['id'] for e in entries if e['id'] not in linked_entries],
        'counts': {'entries': len(entries), 'references': len(items), 'linkable_references': len(linkable),
                   'linked_references': len(linked_refs), 'linked_entries': len(linked_entries)},
    }


def pairs_csv(view: dict) -> str:
    """Entry ↔ footnote reference pairs, plus the unlinked on both sides."""
    import csv
    import io
    items = {i['reference_id']: i for i in view['items']}
    entries = {e['id']: e for e in view['entries']}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['entry_id', 'entry_text', 'entry_page', 'reference_id', 'footnote', 'footnote_position', 'footnote_pages',
                'reference_text', 'status', 'rule', 'score'])
    for l in view['links']:
        e, i = entries.get(l['entry_id'], {}), items.get(l['reference_id'], {})
        w.writerow([l['entry_id'], e.get('text', ''), e.get('page') or '', l['reference_id'], i.get('footnote_number'),
                    i.get('footnote_position'), ' '.join(map(str, i.get('pages') or [])), i.get('text', ''),
                    l['status'], l['rule'], l['score']])
    for rid in view['unlinked_references']:
        i = items[rid]
        w.writerow(['', '', '', rid, i.get('footnote_number'), i.get('footnote_position'),
                    ' '.join(map(str, i.get('pages') or [])), i.get('text', ''), 'unlinked', '', ''])
    for eid in view['unlinked_entries']:
        e = entries[eid]
        w.writerow([eid, e.get('text', ''), e.get('page') or '', '', '', '', '', '', 'unlinked', '', ''])
    return out.getvalue()
