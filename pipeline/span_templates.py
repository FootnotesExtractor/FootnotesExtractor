#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Where a citation part starts and ends — a general principle, plus templates
each book learns from its reviewers.

The principle: a part is the smallest span that still holds all of its
information. Nothing is listed word by word; everything is decided by shape:

  * whitespace and , ; : at either edge are never part of it;
  * a full stop is part of it only when it closes an abbreviation — a letter
    token of at most four letters ("op.cit.", "et al.", "J.") — never after a
    number ("12." -> "12") or a whole word;
  * a short letter token in front of a number ("pp.", "S.", "Bd.", "nr") is a
    prefix and stays outside ("pp. 210" -> "210"), and a part that holds only
    such a prefix moves onto the number that follows it.

Templates: every part is reduced to the decisions it makes about its
neighbours — is the prefix in front of it inside or outside, is the trailing
full stop inside or outside — keyed by label and by the shape of that
neighbour ("aa." for "pp.", "A." for "S.", a full stop after a digit, ...).
What reviewers do in a book (parts they edit or confirm, pages they approve,
weighted by review_signals) is counted per key; where they disagree with the
principle, they win. So if a reviewer marks "pp. 210" whole as Pages, the
template (Pages, prefix "aa.") becomes "inside" for that book and the rest of
its "pp. N" parts follow.

A template is *confirmed* once at least `min_support` reviewer actions agree
with it and none contradicts it; only confirmed templates are applied to
unapproved pages automatically (app.py, apply_span_templates). Everything
else is a suggestion.

Stdlib-only and pure, so the app and the evaluation use the same code.
"""
from __future__ import annotations

import re
from collections import defaultdict

_LETTER = r'[^\W\d_]'
# 1-4 letters closed by a full stop or a space: a prefix-shaped token. The
# separator is required, so "OSWALD" is never read as "OSWA" + "LD".
_PREFIX = re.compile(r'^(' + _LETTER + r'{1,4})(?:(\.)\s*|\s+)')
_PREFIX_ALONE = re.compile(_LETTER + r'{1,4}\.?')
# Number-like content: digits (with a letter suffix, "12a") or upper-case Roman
# numerals of two letters or more ("II", "XIV" - a lone "I" or "M" is as likely
# an initial), in ranges, lists and slashes ("12-15", "3/4", "12, 15").
_NUM_ITEM = r'(?:\d+[a-z]?|[IVXLCDM]{2,})'
_NUMBERISH = re.compile(r'^' + _NUM_ITEM + r'(?:\s*[-–—/,]\s*(?:\d+[a-z]?|[IVXLCDM]+))*$')
_CLOSE_TRIM = ',;:'


def token_shape(token):
    """'pp.' -> 'aa.', 'S.' -> 'A.', 'Bd' -> 'Aa', '12' -> '9'."""
    out = []
    for ch in token:
        if ch.isdigit():
            if not out or out[-1] != '9':
                out.append('9')
        elif ch.isalpha():
            out.append('A' if ch.isupper() else 'a')
        else:
            out.append(ch)
    return ''.join(out)


_ABBREV_USE = re.compile(r'(?<![^\W\d_])(' + _LETTER + r'{1,4})\.(?=\s*[,;:]|\s*\d|\s+(' + _LETTER + r'))')


def book_abbreviations(footnotes):
    """Capitalised short tokens this book itself uses as abbreviations: ones
    whose full stop is followed somewhere in the book by a comma, a number
    or a lower-case word ("Ibid., 12", "Vgl. dazu", "Bd. 2") - which a
    sentence-ending full stop never is. Learned per book, so no list
    of abbreviations is needed and "Veda." at the end of a publisher's name
    stays a word."""
    seen = defaultdict(int)
    for fn in footnotes or []:
        for m in _ABBREV_USE.finditer(fn.get('text') or ''):
            tok, nxt = m.group(1), m.group(2)
            if tok[0].isupper() and (nxt is None or nxt.islower()):
                seen[tok] += 1
    return set(seen)


def _dot_context(text, dot_at, lo, abbrevs=()):
    """What the full stop at `dot_at` closes: 'digit', 'abbrev' or 'word'.

    An abbreviation is a lower-case token of up to four letters ("cit.",
    "al.", "ibid."), a single capital - an initial ("J.") - or a capitalised
    short token the book itself uses as an abbreviation (book_abbreviations:
    "Ibid.", "Vgl."). Any other capitalised word ("Veda.", "Kant.") is a word."""
    i = dot_at
    while i > lo and text[i - 1].isalpha():
        i -= 1
    word = text[i:dot_at]
    if not word:
        return 'digit' if dot_at > lo and text[dot_at - 1].isdigit() else 'other'
    if (len(word) == 1 and word.isupper()) or (len(word) <= 4 and word.islower()) or word in abbrevs:
        return 'abbrev'
    return 'word'


# --------------------------------------------------------------------------
# anatomy of a part: its core, and the neighbours a decision is about
# --------------------------------------------------------------------------

def anatomy(text, start, end, lo=0, hi=None, abbrevs=()):
    """Split a part (start, end) into its core and the neighbours that could
    belong to it, within the reference [lo, hi).

    Returns {'core': (s, e),
             'prefix': None or {'span': (s, e), 'shape', 'inside': bool},
             'trail':  None or {'span': (s, e), 'char', 'context', 'inside': bool}}
    """
    hi = len(text) if hi is None else hi
    s, e = start, end
    while s < e and (text[s].isspace() or text[s] in _CLOSE_TRIM + '.'):
        s += 1
    while e > s and (text[e - 1].isspace() or text[e - 1] in _CLOSE_TRIM):
        e -= 1

    # trailing full stop, inside the part or right after it
    trail = None
    if e > s and text[e - 1] == '.':
        trail = {'span': (e - 1, e), 'char': '.', 'context': _dot_context(text, e - 1, s, abbrevs), 'inside': True}
        e -= 1
        while e > s and text[e - 1].isspace():      # "319 s ." -> "319 s"
            e -= 1
    elif e < hi and text[e:e + 1] == '.':
        trail = {'span': (e, e + 1), 'char': '.', 'context': _dot_context(text, e, s, abbrevs), 'inside': False}

    # prefix inside the part, before number-like content
    prefix = None
    m = _PREFIX.match(text[s:e])
    # a token that is itself number-like ("XL" of a split "XL VI") is no prefix
    alone = (_PREFIX_ALONE.fullmatch(text[s:e]) if s < e and not _NUMBERISH.match(text[s:e]) else None)
    if m and m.end() < e - s and _NUMBERISH.match(text[s + m.end():e]):
        tok = m.group(1) + (m.group(2) or '')
        prefix = {'span': (s, s + len(tok)), 'shape': token_shape(tok), 'inside': True}
        s = s + m.end()
    elif alone:
        # the part is only a prefix: the number after it is what it means
        rest = text[e:hi]
        # the prefix's own full stop may sit just outside the part ("t" + ". 12")
        n = re.match(r'^\.?\s*(' + _NUM_ITEM + r'(?:\s*[-–—/]\s*(?:\d+[a-z]?|[IVXLCDM]+))?)\b', rest)
        tok = text[s:e]
        if n:
            prefix = {'span': (s, s + len(tok)), 'shape': token_shape(tok), 'inside': True, 'only': True}
            s = e + n.start(1)
            e = e + n.end(1)
            trail = None
            if e < hi and text[e:e + 1] == '.':
                trail = {'span': (e, e + 1), 'char': '.', 'context': _dot_context(text, e, s, abbrevs), 'inside': False}
    elif s < e and _NUMBERISH.match(text[s:e]):
        # prefix right before the part, outside it
        before = text[lo:s]
        p = re.search(r'(?:^|[^\w])(' + _LETTER + r'{1,4}\.?)\s*$', before)
        if p:
            tok = p.group(1)
            ps = lo + p.start(1)
            prefix = {'span': (ps, ps + len(tok)), 'shape': token_shape(tok), 'inside': False}
    return {'core': (s, e), 'prefix': prefix, 'trail': trail}


def principle(label, kind, neighbour):
    """The general rule, where no reviewer has said otherwise: True = inside."""
    if kind == 'prefix':
        return False
    return neighbour.get('context') == 'abbrev'


def decisions(label, anat):
    """The (key, inside) decisions a part makes."""
    out = []
    if anat['prefix'] is not None and not anat['prefix'].get('only'):
        out.append(((label, 'prefix', anat['prefix']['shape']), anat['prefix']['inside']))
    if anat['trail'] is not None:
        out.append(((label, 'trail', anat['trail']['char'], anat['trail']['context']), anat['trail']['inside']))
    return out


# --------------------------------------------------------------------------
# learning templates from a book
# --------------------------------------------------------------------------

def _parts(footnotes):
    for fn in footnotes or []:
        text = fn.get('text') or ''
        for ref in fn.get('references') or []:
            lo = ref.get('start') if isinstance(ref.get('start'), int) else 0
            hi = ref.get('end') if isinstance(ref.get('end'), int) else len(text)
            hi = min(max(hi, lo), len(text))
            for comp in ref.get('components') or []:
                if not (isinstance(comp.get('start'), int) and isinstance(comp.get('end'), int)):
                    continue
                if not (0 <= comp['start'] < comp['end'] <= len(text)) or not comp.get('label'):
                    continue
                yield fn, ref, comp, text, lo, max(hi, comp['end'])


def learn(footnotes, reviewer_weight, config):
    """Per-template evidence from what reviewers did in this book.

    `reviewer_weight(fn, comp)` returns (weight, explicit): the weight of this
    part as reviewer evidence (review_signals: edited > confirmed > page
    approved > looked at, 0 for a part no reviewer has vouched for — the
    model's own output teaches nothing here; the principle stands in for it),
    and whether the reviewer acted on this part itself (edited or kept it)
    rather than approving the page it's on.

    Both kinds weigh in on which way a template goes. Only explicit actions
    count as support or contradiction for applying it on its own: an approved
    page also carries whatever the reviewer didn't stop to fix, and measured
    on the corrected books that taught templates the reviewers had been
    correcting elsewhere ("Vol. 84" left on a reviewed page).

    Returns {key: {'inside': bool, 'weight_in', 'weight_out', 'support',
                   'contradicted', 'learned', 'confirmed'}}.
    """
    weights = defaultdict(lambda: [0.0, 0.0])       # key -> [inside, outside]
    counts = defaultdict(lambda: [0, 0])            # explicit actions only
    abbrevs = book_abbreviations(footnotes)
    for fn, ref, comp, text, lo, hi in _parts(footnotes):
        w, explicit = reviewer_weight(fn, comp)
        if w <= 0:
            continue
        for key, inside in decisions(comp['label'], anatomy(text, comp['start'], comp['end'], lo, hi, abbrevs)):
            weights[key][0 if inside else 1] += w
            if explicit:
                counts[key][0 if inside else 1] += 1
    templates = {}
    prior_w = config['principle_weight']
    for key, (w_in, w_out) in weights.items():
        base_in = principle(key[0], key[1], {'context': key[3] if key[1] == 'trail' else None})
        # the principle counts as one more voter, so a lone odd edit can't flip it
        total_in = w_in + (prior_w if base_in else 0)
        total_out = w_out + (0 if base_in else prior_w)
        inside = total_in > total_out
        n_in, n_out = counts[key]
        support, against = (n_in, n_out) if inside else (n_out, n_in)
        templates[key] = {
            'inside': inside,
            'weight_in': round(w_in, 2), 'weight_out': round(w_out, 2),
            'support': support, 'contradicted': against,
            'learned': inside != base_in,
            'confirmed': support >= config['min_support']
                         and against <= config['max_contradiction_share'] * (support + against),
        }
    return templates


# --------------------------------------------------------------------------
# what should change
# --------------------------------------------------------------------------

def target_span(label, anat, templates):
    """The span a part should have under the book's templates (or the
    principle), and which template decided each edge."""
    s, e = anat['core']
    used = []
    for key, inside in decisions(label, anat):
        t = templates.get(key)
        want = t['inside'] if t else principle(key[0], key[1], {'context': key[3] if key[1] == 'trail' else None})
        used.append((key, t))
        if key[1] == 'prefix' and want:
            s = min(s, anat['prefix']['span'][0])
        if key[1] == 'trail' and want:
            e = max(e, anat['trail']['span'][1])
    if anat['prefix'] is not None and anat['prefix'].get('only'):
        key = (label, 'prefix', anat['prefix']['shape'])
        t = templates.get(key)
        used.append((key, t))
        if t and t['inside']:
            s = anat['prefix']['span'][0]
    return (s, e), used


def span_changes(footnotes, templates, skip=None):
    """Every part whose span differs from what the book's templates (or the
    principle) say. `skip(fn, comp)` leaves parts out (reviewer-owned ones).

    Each change: {'footnote_id', 'reference_id', 'component_id', 'page',
    'label', 'from': (s, e), 'to': (s, e), 'text', 'new_text',
    'templates': [key...], 'confirmed': bool, 'learned': bool}.
    """
    out = []
    abbrevs = book_abbreviations(footnotes)
    for fn, ref, comp, text, lo, hi in _parts(footnotes):
        if skip and skip(fn, comp):
            continue
        anat = anatomy(text, comp['start'], comp['end'], lo, hi, abbrevs)
        (ts, te), used = target_span(comp['label'], anat, templates)
        if te <= ts or (ts, te) == (comp['start'], comp['end']):
            continue
        # A part must stay inside its reference (the edit endpoints refuse
        # anything else). Some references end short of their last part — an
        # old NER quirk — and such a part can't be fixed here.
        ref_lo = ref.get('start') if isinstance(ref.get('start'), int) else 0
        ref_hi = ref.get('end') if isinstance(ref.get('end'), int) else len(text)
        if ts < ref_lo or te > ref_hi:
            continue
        others = [c for c in (ref.get('components') or []) if c is not comp
                  and isinstance(c.get('start'), int) and c['start'] < te and ts < c['end']]
        if others:
            continue
        deciding = [t for _k, t in used if t]
        out.append({
            'footnote_id': fn.get('id'), 'reference_id': ref.get('id'), 'component_id': comp.get('id'),
            'page': fn.get('page'), 'label': comp['label'],
            'from': (comp['start'], comp['end']), 'to': (ts, te),
            'text': text[comp['start']:comp['end']], 'new_text': text[ts:te],
            'templates': [list(k) for k, _t in used],
            'learned': any(t['learned'] for t in deciding),
            # automatic only when every template that shaped it is confirmed
            # by reviewers — the principle alone never changes a part by itself
            'confirmed': bool(deciding) and len(deciding) == len(used) and all(t['confirmed'] for t in deciding),
        })
    return out
