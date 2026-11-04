#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Split a reference span that holds several cited works.

The reference model draws one span around "Martines 2009, 2012, 2013" or
"Grilli, Dal 'Tirant' al 'Quijote' (1994a) i Literatura caballeresca (2004)",
but those are three and two works. Exports make one entry per reference, so
the later works would be lost or merged into the first.

A work is anchored by its year. Two or more publication years in one span,
each with its own list separator or its own title between them, mean two or
more works. Years in square brackets are not anchors: "[1615] ... 1999" and
"Theobald [1728] 2010" give an original year next to the edition's, which is
one work. Two years with nothing but spaces or brackets between them
("1980 (1975)") are also one work, and so is the same year written twice
("(1981) ... Historische Zeitschrift 233 (1981)"; "2012a, 2012b" are two). In
the author-year style a year right after the author labels the work that
follows ("Borst A (1981) Das Erdbeben von 1348. Historische Zeitschrift..."),
so after such a year a new work starts only with more years or another
author ("Martines 2009, 2012"; "Smith 2010b pp.2-30, Smith 2015c"). In a bare
list of years only separators and list conjunctions may stand between them;
any other word makes it a note on the same work ("Kraków 2006, druk 2007"). A second Author after a semicolon, once the
current work already has a title or year, starts a new work too
("Smith, Title; Jones, Title" without years).

The reference itself is kept whole - its span and every part - and gets a
`works` list dividing it into one sub-span per cited work ("Praca" in the
viewer); exports make one entry per work. Each work holds the parts that lie
inside it. Whatever the text leaves out of
a later work ("2012" in "Martines 2009, 2012"; author, title and journal for
the next volume in "13, 1936, pp. 10-57; 14, 1937, pp. 107-139") is carried
over from the previous work's head, the parts before its year, as `inherited`
entries. A work never inherits pages or a year.

Pure and stdlib-only: the pipeline, the app and the offline checks run the
same code.
"""
from __future__ import annotations

import copy
import re
import uuid

NAME_LABELS = {'Author', 'Editor', 'Translator'}
POST_ANCHOR = {'Pages', 'PublicationYearSuffix'}
BACKREF = {'Ibidem', 'OpusCitatum'}
# What a work can begin with. A piece starting with an imprint ("Paris:
# Gallimard 1926"), a translator or editor ("tłum. M. Kowalska, ...") or a
# containing volume ("w zbiorze: ...") is more of the same work.
WORK_START = {'Author', 'Title', 'Volume'}
HEAD_LABELS = ('Author', 'Editor', 'Translator', 'Title', 'BookTitle', 'Journal',
               'Publisher', 'PublicationPlace', 'Volume')
# List separators, strongest first. The conjunction list only decides where
# inside the gap between two works the cut falls; whether there are two works
# is decided by the years and titles, never by these words.
_CONJ = r'(?<!\w)(?:i|oraz|a|and|y|e|et|und|en|og|och|ve|v)(?!\w)'
_SEPARATORS = (re.compile(r';'), re.compile(_CONJ, re.I), re.compile(r','))
_STRIP = ' \t\n,;'


def _parts(ref):
    return sorted((c for c in ref.get('components') or []
                   if isinstance(c.get('start'), int) and isinstance(c.get('end'), int)),
                  key=lambda c: (c['start'], c['end']))


def _in_brackets(text, comp):
    inner = text[comp['start']:comp['end']].strip()
    if inner.startswith('[') and inner.endswith(']'):
        return True
    before = text[:comp['start']].rstrip()
    after = text[comp['end']:].lstrip()
    return before.endswith('[') and after.startswith(']')


def _anchors(text, parts):
    return [c for c in parts if c['label'] == 'PublicationYear' and not _in_brackets(text, c)]


def _separator(gap):
    """(start, end) of the strongest separator in gap, or None."""
    for pat in _SEPARATORS:
        found = list(pat.finditer(gap))
        if found:
            m = found[-1]
            return m.start(), m.end()
    return None


def _boundary(text, lo, hi):
    """Where a work ending at or after `lo` and the next one starting at or
    before `hi` divide: (end of the left piece, start of the right one). The
    separator itself belongs to neither."""
    sep = _separator(text[lo:hi])
    if sep:
        left, right = lo + sep[0], lo + sep[1]
    else:
        left = right = hi
    while left > lo and text[left - 1] in _STRIP:
        left -= 1
    while right < hi and text[right] in _STRIP:
        right += 1
    return max(left, lo), min(max(right, left), hi)


def _label_year(text, parts, year):
    """An author-year label: the year right after the author ("Borst A (1981)
    Das Erdbeben..."), naming the work that follows it rather than closing
    the one before. Bracketed original years in between don't count."""
    before = [c for c in parts if c['end'] <= year['start']
              and not (c['label'] == 'PublicationYear' and _in_brackets(text, c))]
    if not before or before[-1]['label'] not in NAME_LABELS:
        return False
    lo = before[-1]['end']
    gap = list(text[lo:year['start']])
    for c in parts:                          # blank out "[1980]" between the author and the year
        if c['label'] == 'PublicationYear' and lo <= c['start'] and c['end'] <= year['start']:
            gap[c['start'] - lo:c['end'] - lo] = ' ' * (c['end'] - c['start'])
    return re.fullmatch(r'[\s()\[\].,:;]*', ''.join(gap)) is not None


def _suffix(text, parts, year):
    """The letter after a year ("a" in "2012a"), tagged or not."""
    nxt = [c for c in parts if c['start'] >= year['end']]
    if nxt and nxt[0]['label'] == 'PublicationYearSuffix':
        return text[nxt[0]['start']:nxt[0]['end']].strip()
    m = re.match(r'[a-z](?![a-z])', text[year['end']:year['end'] + 2])
    return m.group(0) if m else ''


def _year_boundaries(text, parts):
    anchors = _anchors(text, parts)
    out = []
    for a, b in zip(anchors, anchors[1:]):
        between = [c for c in parts if c['start'] >= a['end'] and c['end'] <= b['start']]
        post, head = [], []
        for c in between:
            (post if c['label'] in POST_ANCHOR and not head else head).append(c)
        if (text[a['start']:a['end']].strip() == text[b['start']:b['end']].strip()
                and _suffix(text, parts, a) == _suffix(text, parts, b)):
            continue                        # the same year twice: "(1981) ... Zeitschrift 233 (1981)"
        if head and head[0]['label'] not in WORK_START:
            continue                        # "(1920) Paris: Gallimard 1926": the same work's imprint
        if head and head[0]['label'] not in NAME_LABELS and _label_year(text, parts, a):
            continue                        # "Author (1998) Title. In: ... Berlin 1998": one work
        lo = max([a['end']] + [c['end'] for c in post])
        if lo == a['end'] and re.match(r'[a-z](?![a-z])', text[lo:lo + 2]):
            lo += 1                          # an untagged year letter: "1995b, 2001"
        hi = head[0]['start'] if head else b['start']
        gap = text[lo:hi]
        if not head and not _separator(gap):
            continue                        # "1980 (1975)": one work, two years
        if not head and re.sub(_CONJ, ' ', gap, flags=re.I).strip(' \t\n,;()[]:.') and \
                re.search(r'[^\W\d_]', re.sub(_CONJ, ' ', gap, flags=re.I)):
            continue                        # "Kraków 2006, druk 2007": a note on the same work
        out.append(_boundary(text, lo, hi))
    return out


def _author_boundaries(text, parts, taken):
    """A second Author after a semicolon, once the current work already has a
    title or a year, starts a new work."""
    out = []
    rights = [r for _, r in taken]
    seen = set()
    for i, c in enumerate(parts):
        if i:
            prev_end = parts[i - 1]['end']
            if any(prev_end <= r <= c['start'] for r in rights):
                seen = set()                 # a year boundary already starts a new work here
            elif c['label'] == 'Author' and seen & {'Title', 'PublicationYear'}:
                gap = text[prev_end:c['start']]
                if ';' in gap:
                    cut = prev_end + gap.rindex(';')
                    left, right = cut, cut + 1
                    while left > prev_end and text[left - 1] in _STRIP:
                        left -= 1
                    while right < c['start'] and text[right] in _STRIP:
                        right += 1
                    out.append((left, right))
                    rights.append(right)
                    seen = set()
        seen.add(c['label'])
    return out


def _works(text, ref, parts, boundaries):
    """Pieces between the boundaries, with their parts and inherited fields."""
    boundaries = sorted(set(boundaries))
    starts = [ref['start']] + [r for _, r in boundaries]
    ends = [l for l, _ in boundaries] + [ref['end']]
    works = []
    for s, e in zip(starts, ends):
        works.append({'start': s, 'end': e,
                      'parts': [c for c in parts if s <= c['start'] < max(e, s + 1) and c['end'] <= e]})
    # every part must land in exactly one piece
    placed = sum(len(w['parts']) for w in works)
    if placed != len(parts) or any(w['end'] <= w['start'] for w in works):
        return None
    prev_head = []                           # [(label, text, source part)] of the previous work
    for w in works:
        own = {c['label'] for c in w['parts']}
        inherited = []
        for label in HEAD_LABELS:
            if label in own:
                continue
            inherited.extend(h for h in prev_head if h[0] == label)
        w['inherited'] = inherited
        anchors = _anchors(text, w['parts'])
        first_year = anchors[0]['start'] if anchors else None
        head = [(c['label'], text[c['start']:c['end']], c) for c in w['parts']
                if c['label'] in HEAD_LABELS and (first_year is None or c['start'] < first_year)]
        head_labels = {h[0] for h in head}
        prev_head = head + [h for h in inherited if h[0] not in head_labels]
    return works


def plan_split(text, ref):
    """None, or the works one reference holds:
    [{'start', 'end', 'parts': [component, ...], 'inherited': [(label, text, part), ...]}, ...]
    (offsets in the footnote's text, like every span in a result)."""
    parts = _parts(ref)
    if len(parts) < 2 or any(c['label'] in BACKREF for c in parts):
        return None
    if ref.get('works') or ref.get('split_group'):
        return None                          # already divided into works
    if not (isinstance(ref.get('start'), int) and isinstance(ref.get('end'), int)):
        return None
    boundaries = _year_boundaries(text, parts)
    boundaries += _author_boundaries(text, parts, boundaries)
    if not boundaries:
        return None
    works = _works(text, ref, parts, boundaries)
    return works if works and len(works) > 1 else None


def plan_from_cuts(text, ref, cuts):
    """The works for a person's own cut positions (offsets in the footnote's
    text). None if a cut falls inside a part or outside the reference."""
    parts = _parts(ref)
    boundaries = []
    for cut in sorted(set(cuts)):
        if not (ref['start'] < cut < ref['end']):
            return None
        if any(c['start'] < cut < c['end'] for c in parts):
            return None
        left, right = cut, cut
        while left > ref['start'] and text[left - 1] in _STRIP:
            left -= 1
        while right < ref['end'] and text[right] in _STRIP:
            right += 1
        boundaries.append((left, right))
    works = _works(text, ref, parts, boundaries)
    return works if works and len(works) > 1 else None


# --------------------------------------------------------------------------
# Works ("Praca") inside a reference
#
# A reference that holds several works keeps its span and all its parts; its
# `works` list divides it into sub-spans, one per cited work:
#     {'id', 'start', 'end', 'how': 'auto'|'suggested'|'manual',
#      'citation_type', 'inherited': [{'label', 'text', 'source_component_id'}]}
# A part belongs to the work whose span holds its start. Exports make one
# entry per work (citation_fields.iter_references, via work_view).
# --------------------------------------------------------------------------

def _new_work(start, end, how, inherited=(), classify=None, text=''):
    return {'id': str(uuid.uuid4()), 'start': start, 'end': end, 'how': how,
            'citation_type': classify(text[start:end]) if classify else 'normal',
            'inherited': [{'label': label, 'text': t, 'source_component_id': src.get('id')}
                          for label, t, src in inherited]}


def set_works(text, ref, works, how='auto', classify=None):
    """Give `ref` the works of a plan (plan_split / plan_from_cuts)."""
    # Pipeline-time parts have no ids yet (the app backfills them later); the
    # inherited entries need them to point at their source.
    for w in works:
        for c in w['parts']:
            c.setdefault('id', str(uuid.uuid4()))
    ref['works'] = [_new_work(w['start'], w['end'], how, w['inherited'], classify, text) for w in works]
    return ref['works']


def work_parts(ref, work):
    return [c for c in _parts(ref) if work['start'] <= c['start'] < max(work['end'], work['start'] + 1)]


def _cuts_part(ref, pos):
    return any(c['start'] < pos < c['end'] for c in _parts(ref))


def _trim(text, start, end):
    """A span without the separators and spaces at its edges."""
    while start < end and text[start] in _STRIP:
        start += 1
    while end > start and text[end - 1] in _STRIP:
        end -= 1
    return start, end


def _rebuild(text, ref, intervals, how, classify=None):
    """Works for the given (start, end, old work or None) intervals: kept works
    stay as they were, new ones get their inherited fields from the head of
    the work before them (the same rule plan_split uses)."""
    parts = _parts(ref)
    out = []
    prev_head = []
    for start, end, old in sorted(intervals, key=lambda i: i[0]):
        own = [c for c in parts if start <= c['start'] < max(end, start + 1)]
        if old is not None:
            work = dict(old, start=start, end=end)
            inherited = [(h['label'], h.get('text', ''), {'id': h.get('source_component_id')})
                         for h in old.get('inherited') or []]
        else:
            labels = {c['label'] for c in own}
            inherited = [h for label in HEAD_LABELS if label not in labels for h in prev_head if h[0] == label]
            for c in parts:
                c.setdefault('id', str(uuid.uuid4()))
            work = _new_work(start, end, how, inherited, classify, text)
        out.append(work)
        anchors = _anchors(text, own)
        first_year = anchors[0]['start'] if anchors else None
        head = [(c['label'], text[c['start']:c['end']], c) for c in own
                if c['label'] in HEAD_LABELS and (first_year is None or c['start'] < first_year)]
        head_labels = {h[0] for h in head}
        prev_head = head + [h for h in inherited if h[0] not in head_labels]
    return out


def _current_intervals(ref):
    if ref.get('works'):
        return [(w['start'], w['end'], w) for w in sorted(ref['works'], key=lambda w: w['start'])]
    return [(ref['start'], ref['end'], None)]


def mark_work(text, ref, start, end, how='manual', classify=None):
    """Mark [start, end) as one work, splitting off the rest: each remaining
    stretch of the works it cuts into that still holds parts becomes a work of
    its own. None if the span leaves the reference or cuts through a part."""
    start, end = _trim(text, start, end)
    if not (ref['start'] <= start < end <= ref['end']) or _cuts_part(ref, start) or _cuts_part(ref, end):
        return None
    parts = _parts(ref)
    intervals = []
    for s, e, old in _current_intervals(ref):
        if e <= start or s >= end:
            intervals.append((s, e, old))
            continue
        for a, b in ((s, start), (end, e)):
            a, b = _trim(text, a, b)
            if b > a and any(a <= c['start'] < b for c in parts):
                intervals.append((a, b, None))
    intervals.append((start, end, None))
    works = _rebuild(text, ref, intervals, how, classify)
    ref['works'] = works if len(works) > 1 else []
    if not ref['works']:
        ref.pop('works', None)
    return works


def split_at(text, ref, cuts, how='manual', classify=None):
    """Cut the reference (or the work holding each cut) at the given offsets.
    None if a cut falls outside the reference or inside a part."""
    intervals = _current_intervals(ref)
    for cut in sorted(set(cuts)):
        if not (ref['start'] < cut < ref['end']) or _cuts_part(ref, cut):
            return None
        for i, (s, e, old) in enumerate(intervals):
            if s < cut < e:
                left, right = _trim(text, s, cut), _trim(text, cut, e)
                if left[1] <= left[0] or right[1] <= right[0]:
                    return None
                intervals[i:i + 1] = [(left[0], left[1], old), (right[0], right[1], None)]
                break
        else:
            return None
    works = _rebuild(text, ref, intervals, how, classify)
    ref['works'] = works
    return works


def merge_works(text, ref, ids):
    """Join neighbouring works into one; a reference left with a single work
    goes back to having none. None if the works aren't neighbours."""
    works = sorted(ref.get('works') or [], key=lambda w: w['start'])
    pos = [i for i, w in enumerate(works) if w['id'] in set(ids)]
    if len(pos) < 2 or len(pos) != len(set(ids)) or pos != list(range(pos[0], pos[-1] + 1)):
        return None
    first = works[pos[0]]
    merged = dict(first, end=works[pos[-1]]['end'])
    own = {c['label'] for c in work_parts(ref, merged)}
    merged['inherited'] = [h for h in first.get('inherited') or [] if h['label'] not in own]
    works = works[:pos[0]] + [merged] + works[pos[-1] + 1:]
    if len(works) < 2:
        ref.pop('works', None)
        return []
    ref['works'] = works
    return works


def resize_work(text, ref, work_id, start, end):
    """Move a work's edges, inside the reference and clear of its neighbours
    and of the middle of any part. None if that isn't possible."""
    works = sorted(ref.get('works') or [], key=lambda w: w['start'])
    idx = next((i for i, w in enumerate(works) if w['id'] == work_id), None)
    if idx is None:
        return None
    start, end = _trim(text, start, end)
    lo = works[idx - 1]['end'] if idx > 0 else ref['start']
    hi = works[idx + 1]['start'] if idx + 1 < len(works) else ref['end']
    if not (lo <= start < end <= hi) or _cuts_part(ref, start) or _cuts_part(ref, end):
        return None
    works[idx] = dict(works[idx], start=start, end=end)
    ref['works'] = works
    return works[idx]


def set_inherited(footnote, ref, work_id, entries=None, from_previous=None):
    """Replace a work's carried-over fields. entries: [{'source_component_id'}]
    naming parts of this reference that sit before the work; from_previous:
    labels to take from the work just before it (its own parts or what it
    carries itself). None when that isn't possible."""
    text = footnote.get('text') or ''
    works = sorted(ref.get('works') or [], key=lambda w: w['start'])
    idx = next((i for i, w in enumerate(works) if w['id'] == work_id), None)
    if idx is None:
        return None
    work = works[idx]
    parts = {c.get('id'): c for c in _parts(ref)}
    current = {h.get('source_component_id'): h for h in work.get('inherited') or []}
    out = []
    if from_previous is not None:
        if idx == 0:
            return None
        prev = works[idx - 1]
        sources = [(c['label'], c.get('id'), text[c['start']:c['end']]) for c in work_parts(ref, prev)]
        sources += [(h['label'], h.get('source_component_id'), h.get('text', '')) for h in prev.get('inherited') or []]
        out = list(work.get('inherited') or [])
        have = {h.get('source_component_id') for h in out}
        added = 0
        for label in from_previous:
            for lab, cid, t in sources:
                if lab == label and cid not in have:
                    out.append({'label': lab, 'text': t, 'source_component_id': cid})
                    have.add(cid)
                    added += 1
        if not added:
            return None                      # nothing of that kind to carry over
    else:
        for e in entries or []:
            cid = (e or {}).get('source_component_id')
            if cid in current:
                out.append(current[cid])
            elif cid in parts and parts[cid]['start'] < work['start']:
                c = parts[cid]
                out.append({'label': c['label'], 'text': text[c['start']:c['end']], 'source_component_id': cid})
            else:
                return None
    work['inherited'] = out
    return work


def carry_candidates(footnote, ref, work_id):
    """Labels the work just before `work_id` has (as parts or carried over
    itself) that `work_id` lacks, in HEAD_LABELS order."""
    works = sorted(ref.get('works') or [], key=lambda w: w['start'])
    idx = next((i for i, w in enumerate(works) if w['id'] == work_id), None)
    if not idx:
        return []
    work, prev = works[idx], works[idx - 1]
    have = {c['label'] for c in work_parts(ref, work)} | {h['label'] for h in work.get('inherited') or []}
    offer = {c['label'] for c in work_parts(ref, prev)} | {h['label'] for h in prev.get('inherited') or []}
    return [label for label in HEAD_LABELS if label in offer and label not in have]


def refresh_after_resize(text, ref, auto=False, classify=None):
    """After the reference's own span or parts changed: works are clipped to
    the new span (emptied ones dropped), or worked out afresh when `auto`."""
    if auto:
        ref.pop('works', None)
        works = plan_split(text, ref)
        if works:
            set_works(text, ref, works, how='auto', classify=classify)
        return
    if not ref.get('works'):
        return
    kept = []
    for w in sorted(ref['works'], key=lambda w: w['start']):
        s, e = max(w['start'], ref['start']), min(w['end'], ref['end'])
        if e > s:
            kept.append(dict(w, start=s, end=e))
    if len(kept) > 1:
        ref['works'] = kept
    else:
        ref.pop('works', None)


def work_view(text, ref, work):
    """A work as a stand-alone reference, for exports and the viewer."""
    view = {k: v for k, v in ref.items() if k not in ('works', 'components', 'components_inferred', 'inherited')}
    view.update({'id': work['id'], 'reference_id': ref.get('id'), 'start': work['start'], 'end': work['end'],
                 'text': text[work['start']:work['end']], 'components': work_parts(ref, work),
                 'inherited': work.get('inherited') or [],
                 'citation_type': work.get('citation_type') or ref.get('citation_type', 'normal')})
    return view


def resolve_inherited(footnote, ref):
    """ref's (or a work view's) inherited entries with their text refreshed
    from the source part while it still exists, so an edit to the first
    work's author shows in the second's."""
    by_id = {c.get('id'): c for r in footnote.get('references') or [] for c in r.get('components') or []}
    text = footnote.get('text') or ''
    out = []
    for h in ref.get('inherited') or []:
        src = by_id.get(h['source_component_id']) if h.get('source_component_id') else None
        entry = dict(h)
        if src is not None and isinstance(src.get('start'), int):
            entry['text'] = text[src['start']:src['end']] or h.get('text')
            if src.get('names'):
                entry['names'] = copy.deepcopy(src['names'])
        out.append(entry)
    return out


def split_footnote_references(footnote, classify=None, how='auto'):
    """Give every multi-work reference of one footnote its works, in place;
    returns how many references were split."""
    text = footnote.get('text') or ''
    n = 0
    for ref in footnote.get('references') or []:
        works = plan_split(text, ref)
        if works:
            set_works(text, ref, works, how=how, classify=classify)
            n += 1
    return n


def merge_references(text, refs):
    """One reference covering several adjacent ones."""
    refs = sorted(refs, key=lambda r: r['start'])
    merged = {k: copy.deepcopy(v) for k, v in refs[0].items()
              if k not in ('components', 'inherited', 'split', 'split_group', 'split_index', 'works',
                           'components_inferred', 'text', 'start', 'end')}
    merged['start'], merged['end'] = refs[0]['start'], refs[-1]['end']
    merged['text'] = text[merged['start']:merged['end']]
    merged['components'] = sorted((copy.deepcopy(c) for r in refs for c in r.get('components') or []),
                                  key=lambda c: (c.get('start', 0), c.get('end', 0)))
    return merged


def groups_to_works(text, refs):
    """Turn references cut apart by the first version of the split (pieces
    sharing a split_group) back into one reference holding them as works.
    Returns the new list and how many groups were converted."""
    out, n, i = [], 0, 0
    refs = sorted(refs, key=lambda r: r.get('start', 0))
    while i < len(refs):
        g = refs[i].get('split_group')
        if not g:
            out.append(refs[i]); i += 1
            continue
        j = i
        while j < len(refs) and refs[j].get('split_group') == g:
            j += 1
        pieces = refs[i:j]
        if len(pieces) < 2:
            ref = dict(pieces[0])
            for k in ('split', 'split_group', 'split_index', 'inherited'):
                ref.pop(k, None)
            out.append(ref)
        else:
            merged = merge_references(text, pieces)
            merged['works'] = [{'id': p['id'] if k else str(uuid.uuid4()), 'start': p['start'], 'end': p['end'],
                                'how': p.get('split') or 'auto', 'citation_type': p.get('citation_type', 'normal'),
                                'inherited': [{'label': h['label'], 'text': h.get('text', ''),
                                               'source_component_id': h.get('source_component_id')}
                                              for h in p.get('inherited') or []]}
                               for k, p in enumerate(pieces)]
            out.append(merged)
            n += 1
        i = j
    return out, n
