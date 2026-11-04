#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic consistency checks for Layout Assist.

The two priors (layout_prior, ner_prior) learn what is normal for a book and
flag what disagrees. These checks are the other half: rules that hold for
any book, written down once. A page range is one Pages part but a page list
is several; an Issue or Volume part holds the number, not "nr" or "Vol.";
"op. cit." is OpusCitatum, never Ibidem. The conventions come from how the
verified books in this app are annotated.

Each check returns items the viewer shows next to the priors' suggestions:

    {'id':      stable across requests, so a dismissal sticks,
     'area':    'labels' (and later 'boxes' / 'text'),
     'rule':    which check fired,
     'page', 'footnote_id', 'reference_id', 'component_id',
     'text':    the part as it reads now, 'label': its label,
     'params':  what the message needs,
     'fix':     the edit that resolves it, in terms of the existing edit
                endpoints - or None when only a person can tell}

Fixes:
    {'type': 'resize', 'start', 'end'}              move/trim the part
    {'type': 'relabel', 'label'}                    change its label
    {'type': 'split', 'parts': [{'start', 'end', 'label'}, ...]}
        the first part replaces this one, the rest are created beside it
    {'type': 'split_reference'}                     cut the reference into one
        per work (reference_split.plan_split); a reference-level item

Pure and stdlib-only, so the app and the offline evaluation run the same
rules.
"""
from __future__ import annotations

import datetime
import re
import unicodedata
from collections import Counter, defaultdict

from pipeline.reference_split import plan_split

# Which rules the viewer shows. Measured on verified books: the
# rules about where a part starts and
# ends almost never fire there, and when they do it's a slip the reviewers
# missed ("s. 716", "nr 1", "163."). The rules that change a label disagreed
# with every verified label they touched - "op. cit." is labelled Ibidem in
# those books, and editor markers vary by language ("zost.") - and the
# structural ones mostly echo the NER model's output format rather than
# something a click can fix. Those stay off until they measure better.
#
# Where a part starts and ends (prefixes like "pp.", a trailing full stop) is
# not decided here: pipeline/span_templates.py handles it with one general
# principle plus templates each book learns from its reviewers, rather than
# lists of prefixes.
ENABLED_LABEL_RULES = frozenset({
    'pages_list', 'marker_in_part', 'year_suffix', 'year_invalid', 'pages_backwards',
    'doi_invalid', 'url_invalid', 'overlap',
})

NUMBER_LABELS = {'Pages', 'Volume', 'Issue', 'PublicationYear', 'PublicationYearSuffix'}
PERSON_LABELS = {'Author', 'Editor', 'Translator'}

_PAGE_ITEM = r'(?:\d+[a-z]?|[ivxlcdm]+)(?:\s*[-–—]\s*(?:\d+[a-z]?|[ivxlcdm]+))?(?:\s*(?:f{1,2}\.|nn\.|n\.))?'
_PAGE_LIST = re.compile(r'^' + _PAGE_ITEM + r'(?:\s*[,;]\s*' + _PAGE_ITEM + r')+$', re.I)
_PAGE_RANGE = re.compile(r'^(\d+)\s*[-–—]\s*(\d+)$')

_YEAR_NOW = datetime.date.today().year
_YEAR_WITH_LETTER = re.compile(r'^(?P<year>\d{4})(?P<suffix>[a-z])$')
_YEAR_OK = re.compile(r'^\[?\(?(\d{4})(?:\s*[-–—/]\s*\d{2,4})?\)?\]?$')
_NO_YEAR = re.compile(r'^(?:b\.?\s*r\.?|n\.?\s*d\.?|s\.?\s*a\.?|o\.?\s*j\.?)$', re.I)
_DOI = re.compile(r'10\.\d{4,9}/\S+')
_URL = re.compile(r'^(?:https?://|www\.)\S+$|^\S+\.[a-z]{2,}(?:/\S*)?$', re.I)

_EDITOR_MARK = re.compile(r'(?<!\w)(?:red\.|pod\s+red\.|ed\.|eds\.|hrsg\.|hg\.|oprac\.|edited\s+by|editor|editors|redakcja)(?!\w)', re.I)
_TRANSLATOR_MARK = re.compile(r'(?<!\w)(?:tłum\.|przeł\.|przekł\.|trans\.|transl\.|tr\.|übers\.|translated\s+by|tłumaczenie|przekład)(?!\w)', re.I)
_OPCIT = re.compile(r'^(?:op\.?\s*cit\.?|dz\.?\s*cyt\.?|dzieło\s+cytowane|o\.?\s*c\.?)$', re.I)
_IBID = re.compile(r'^(?:ibid(?:em)?\.?|ib\.?|tamże|tamze|tamże\.)$', re.I)

_EDGE_ALWAYS = ',;:'            # never belongs at either end of a part
_EDGE_NUMBERS = ',;:.'          # ...nor a full stop, for number-like parts


def _span(text, c):
    return text[c['start']:c['end']]


def _item(rule, fn, ref, comp, text, params=None, fix=None, area='labels'):
    return {
        'id': '%s:%s' % (rule, comp.get('id') if comp else (ref or {}).get('id') or fn.get('id')),
        'area': area,
        'rule': rule,
        'page': fn.get('page'),
        'footnote_id': fn.get('id'),
        'reference_id': (ref or {}).get('id'),
        'component_id': (comp or {}).get('id'),
        'text': text,
        'label': (comp or {}).get('label'),
        'params': params or {},
        'fix': fix,
    }


def _components(ref):
    comps = [c for c in (ref.get('components') or [])
             if isinstance(c.get('start'), int) and isinstance(c.get('end'), int) and c['end'] > c['start']]
    return sorted(comps, key=lambda c: (c['start'], c['end']))


def _human(c):
    return c.get('source') == 'manual' or bool(c.get('edited'))


def _trim(text, start, end, chars):
    """Shrink [start, end) past whitespace and any of `chars` at either edge."""
    while start < end and (text[start].isspace() or text[start] in chars):
        start += 1
    while end > start and (text[end - 1].isspace() or text[end - 1] in chars):
        end -= 1
    return start, end


def _leading_marker(text):
    """The footnote's printed number at its very start, as (number, end offset)."""
    m = re.match(r'^\s*\[?(\d{1,3})\]?(?=[\s.)\]])', text or '')
    return (int(m.group(1)), m.end()) if m else (None, 0)


# --------------------------------------------------------------------------
# per-part rules
# --------------------------------------------------------------------------

def _check_component(fn, ref, comp, comps, idx, text):
    """The first rule that applies to one part, or None — one item per part."""
    label = comp.get('label')
    s, e = comp['start'], comp['end']
    raw = _span(text, comp)
    core_s, core_e = _trim(text, s, e, '')
    core = text[core_s:core_e]

    # Ibidem / op. cit. mix-ups: unambiguous from the text alone.
    if label == 'Ibidem' and _OPCIT.match(core):
        return _item('ibid_opcit', fn, ref, comp, raw, {'to': 'OpusCitatum'},
                     {'type': 'relabel', 'label': 'OpusCitatum'})
    if label == 'OpusCitatum' and _IBID.match(core):
        return _item('ibid_opcit', fn, ref, comp, raw, {'to': 'Ibidem'},
                     {'type': 'relabel', 'label': 'Ibidem'})

    if label == 'Pages':
        stripped_s, stripped_e = _trim(text, s, e, _EDGE_NUMBERS)
        body = text[stripped_s:stripped_e]
        if _PAGE_LIST.match(body):
            parts, pos = [], stripped_s
            for piece in re.split(r'([,;])', body):
                if piece in (',', ';'):
                    pos += 1
                    continue
                ps, pe = _trim(text, pos, pos + len(piece), '')
                if pe > ps:
                    parts.append({'start': ps, 'end': pe, 'label': 'Pages'})
                pos += len(piece)
            if len(parts) > 1:
                return _item('pages_list', fn, ref, comp, raw, {'count': len(parts)},
                             {'type': 'split', 'parts': parts})
        r = _PAGE_RANGE.match(body)
        if r:
            a, b = r.group(1), r.group(2)
            # "123-5" is an abbreviated range, not a backwards one.
            if len(b) >= len(a) and int(b) < int(a):
                return _item('pages_backwards', fn, ref, comp, raw)

    if label == 'PublicationYear':
        m = _YEAR_WITH_LETTER.match(core)
        if m:
            return _item('year_suffix', fn, ref, comp, raw, {'year': m.group('year'), 'suffix': m.group('suffix')},
                         {'type': 'split', 'parts': [
                             {'start': core_s, 'end': core_s + 4, 'label': 'PublicationYear'},
                             {'start': core_s + 4, 'end': core_s + 5, 'label': 'PublicationYearSuffix'}]})
        body_s, body_e = _trim(text, s, e, _EDGE_NUMBERS)
        body = text[body_s:body_e]
        y = _YEAR_OK.match(body)
        if not _NO_YEAR.match(body) and (not y or not 1400 <= int(y.group(1)) <= _YEAR_NOW + 1):
            return _item('year_invalid', fn, ref, comp, raw)

    if label == 'DOI' and not _DOI.search(core):
        return _item('doi_invalid', fn, ref, comp, raw)
    # OCR often breaks a long URL across lines; that's not what this checks.
    if label == 'Webpage' and not _URL.match(re.sub(r'\s+', '', core)):
        return _item('url_invalid', fn, ref, comp, raw)

    # Role markers: an Editor/Translator should have one in its reference;
    # an Author standing right next to one probably is the editor/translator.
    ref_s = ref.get('start') if isinstance(ref.get('start'), int) else 0
    ref_e = ref.get('end') if isinstance(ref.get('end'), int) else len(text)
    ref_text = text[ref_s:ref_e]
    if label == 'Editor' and not _EDITOR_MARK.search(ref_text):
        return _item('role_missing', fn, ref, comp, raw, {'to': 'Author', 'marker': 'red. / ed.'},
                     {'type': 'relabel', 'label': 'Author'})
    if label == 'Translator' and not _TRANSLATOR_MARK.search(ref_text):
        return _item('role_missing', fn, ref, comp, raw, {'to': 'Author', 'marker': 'tłum. / trans.'},
                     {'type': 'relabel', 'label': 'Author'})
    if label == 'Author':
        # "red. Jan Kowalski" / "Jan Kowalski (red.)": the marker within a few
        # characters either side, with no other name in that window it could
        # belong to instead.
        lo, hi = max(ref_s, s - 16), min(ref_e, e + 16)
        near = text[lo:s] + ' ' + text[e:hi]
        m = _EDITOR_MARK.search(near) or _TRANSLATOR_MARK.search(near)
        crowded = any(o is not comp and o.get('label') in PERSON_LABELS and o['start'] < hi and lo < o['end']
                      for o in comps)
        if m and not crowded:
            to = 'Editor' if _EDITOR_MARK.search(m.group(0)) else 'Translator'
            return _item('role_author', fn, ref, comp, raw, {'to': to, 'marker': m.group(0)},
                         {'type': 'relabel', 'label': to})

    # The footnote's own printed number caught at the start of a part that
    # runs on past it: trim the part to start after the number.
    num, num_end = _leading_marker(text)
    if num is not None and s < num_end < e:
        ns, ne = _trim(text, num_end, e, _EDGE_ALWAYS + '.)]')
        if ne > ns:
            return _item('marker_in_part', fn, ref, comp, raw, {'number': num},
                         {'type': 'resize', 'start': ns, 'end': ne})

    return None


def _structure_checks(fn, ref, comps, text):
    items = []
    ref_s = ref.get('start') if isinstance(ref.get('start'), int) else None
    ref_e = ref.get('end') if isinstance(ref.get('end'), int) else None
    if not comps:
        items.append(_item('empty_reference', fn, ref, None, text[ref_s or 0:ref_e or len(text)]))
        return items
    for prev, comp in zip(comps, comps[1:]):
        if comp['start'] < prev['end']:
            items.append(_item('overlap', fn, ref, comp, _span(text, comp), {'other': prev.get('label')}))
    works = plan_split(text, ref)
    if works:
        # Off by default (not in ENABLED_LABEL_RULES): books with the upload
        # option are split automatically; this is for the others.
        items.append(_item('multiple_works', fn, ref, None, text[ref_s or 0:ref_e or len(text)],
                           {'count': len(works), 'preview': [text[w['start']:w['end']] for w in works]},
                           {'type': 'split_reference'}))
    if ref_s is not None and ref_e is not None:
        for comp in comps:
            if comp['start'] < ref_s or comp['end'] > ref_e:
                items.append(_item('outside_reference', fn, ref, comp, _span(text, comp)))
    return items


def _norm(s):
    s = unicodedata.normalize('NFKC', s).lower()
    return re.sub(r'[\W_]+', ' ', s).strip()


def _repeated_label_checks(occurrences):
    """The same text labelled differently across the book.

    Only where a difference can't be legitimate: people can be an author in
    one reference and an editor in another, and a number means different
    things in different places, so person labels and number-like text are
    left out. Needs at least three occurrences and a two-thirds majority;
    a label a person set outranks the model's.
    """
    items = []
    groups = defaultdict(list)
    for occ in occurrences:
        key = _norm(occ['text'])
        if len(key) < 4 or not re.search(r'[^\W\d_]', key) or occ['comp'].get('label') in PERSON_LABELS:
            continue
        groups[key].append(occ)
    for key, occs in groups.items():
        if len(occs) < 3:
            continue
        labels = Counter(o['comp'].get('label') for o in occs)
        if len(labels) < 2:
            continue
        human = Counter(o['comp'].get('label') for o in occs if _human(o['comp']))
        if human:
            majority, n = human.most_common(1)[0]
        else:
            majority, n = labels.most_common(1)[0]
            if n * 3 < len(occs) * 2:
                continue
        for o in occs:
            if o['comp'].get('label') != majority and not _human(o['comp']):
                items.append(_item('repeated_label', o['fn'], o['ref'], o['comp'], o['text'],
                                   {'to': majority, 'count': labels[majority]},
                                   {'type': 'relabel', 'label': majority}))
    return items


# --------------------------------------------------------------------------
# what reviewers decided, per rule
# --------------------------------------------------------------------------

# Fixes a confirmed rule may apply on its own: edits to one part. Span
# boundaries have their own carry-over (span templates), and splitting a
# reference into works is left to a person.
AUTO_FIX_TYPES = frozenset({'resize', 'relabel', 'split'})


def decision_summary(fixed, kept, auto, rereviewed=(), min_support=3, max_contradiction_share=0.0):
    """Per rule, what reviewers decided about its items and what follows.

    fixed / kept / auto: records {'id', 'rule', 'page', ...} of items a
    reviewer fixed, kept as they were ("Fine as is"), or that were fixed
    automatically. A rule is confirmed when enough reviewer fixes agree and
    few enough keeps disagree (review_signals.SPAN_TEMPLATE, the same bar as
    span templates); a confirmed rule fixes its remaining items on its own.
    A rule both fixed and kept is inconsistent: it is not applied on its own,
    and every decision behind it - the automatic fixes too - is listed for
    re-review until someone has looked at it again (rereviewed: record keys).

    Returns {rule: {'fixed', 'kept', 'auto', 'confirmed', 'inconsistent',
    'rereview': [records with 'key' and 'kind' added]}}.
    """
    rereviewed = set(rereviewed)
    out = {}
    for kind, records in (('fixed', fixed), ('kept', kept), ('auto', auto)):
        for rec in records or []:
            r = out.setdefault(rec['rule'], {'fixed': 0, 'kept': 0, 'auto': 0, 'records': []})
            r[kind] += 1
            r['records'].append(dict(rec, kind=kind, key='%s:%s' % (kind, rec['id'])))
    for rule, r in out.items():
        votes = r['fixed'] + r['kept']
        r['confirmed'] = r['fixed'] >= min_support and r['kept'] <= max_contradiction_share * votes
        r['inconsistent'] = r['fixed'] > 0 and r['kept'] > 0
        records = r.pop('records')
        r['rereview'] = ([rec for rec in records if rec['key'] not in rereviewed]
                         if r['inconsistent'] else [])
    return out


def label_checks(footnotes, rules=ENABLED_LABEL_RULES):
    """The citation-part checks over a book's footnotes (result['results']):
    the enabled ones by default, every rule with rules=None (for measuring)."""
    items, occurrences = [], []
    for fn in footnotes or []:
        text = fn.get('text') or ''
        for ref in fn.get('references') or []:
            comps = _components(ref)
            items.extend(_structure_checks(fn, ref, comps, text))
            for idx, comp in enumerate(comps):
                if comp['end'] > len(text):
                    continue
                item = _check_component(fn, ref, comp, comps, idx, text)
                if item:
                    items.append(item)
                occurrences.append({'fn': fn, 'ref': ref, 'comp': comp, 'text': _span(text, comp)})
    flagged = {i['component_id'] for i in items if i['component_id']}
    items.extend(i for i in _repeated_label_checks(occurrences) if i['component_id'] not in flagged)
    return [i for i in items if rules is None or i['rule'] in rules]
