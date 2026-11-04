#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-document NER consistency prior.

The NER cascade classifies every footnote in complete isolation - one string in,
labels out, no memory of the other four hundred footnotes in the same book. But
a book's citation style is fixed: the author comes first, `s.` precedes a page
number, the place precedes the year. That regularity is free evidence the model
never sees, and it is what this module models.

Crucially it keys on FEATURES, not on the text. Measured over 232 documents and
74,990 labelled spans, a bare number is PublicationYear only 79.6% of the time -
but after `p.` it is Pages in 100% of cases, and the shape `x. d+.` ("s. 23.")
is Pages in 100%. An exact-text lexicon reaches 0.951 accuracy over 0.508 of
spans; this scorer reaches 0.969 over 0.756.

Dependency-free (stdlib only - no Flask, torch or transformers) so the web app
and the offline evaluation harness score through exactly one implementation
rather than two that drift.

What it deliberately does NOT do is relabel anything. The tables are fitted
mostly on the model's own output, so a book-wide systematic error would be
learned as the norm and confidently confirmed - the same trap layout_prior.py
documents for box geometry, and sharper here, because a wrong label can be
propagated across a whole document by one click. Every output is a suggestion.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

VERSION = 1

DEFAULTS = {
    # --- activation ------------------------------------------------------
    'n_min': 40,              # labelled spans before the prior is allowed to act.
                              # Below ~40 the per-cell counts are 1-2 and the
                              # leave-one-out purity below is measuring noise.
    'min_refs': 8,            # ...spread over this many distinct references. One
                              # long footnote with 50 components is ONE sample of
                              # a citation style, not fifty.
    'min_support': 3.0,       # weighted support required in the winning cell
    'min_support_footnotes': 2,   # ...drawn from at least this many footnotes
    'min_agreeing_features': 2,   # no single-feature verdicts, ever. This is the
                                  # structural guarantee that this is not a lexicon.
    # ...and at least one of those must actually predict labels in this book.
    # Reliability is already rescaled against the document's majority-class rate,
    # so a feature near 0.5 is barely better than guessing: 'right' (0.563) and
    # 'ntok' (0.618) are tie-breakers between candidates, not evidence for one.
    # Without this bar a span whose discriminating cells are empty gets a verdict
    # from base rates alone - measured: a bare number after "s.", in a book that
    # tags pages as "s. 13.", was accepted as PublicationYear at 7.9 nats carried
    # entirely by "one numeric token" and "followed by end of text".
    'min_decisive_reliability': 0.85,
    # --- weights ---------------------------------------------------------
    'w_model': 1,             # the cascade's own output
    'w_implicit': 2,          # a footnote the user reviewed and left alone
    'w_explicit': 6,          # a span a human drew, relabelled or asserted.
                              # Deliberately above layout_prior's 5: one human
                              # correction must outvote five model spans in its
                              # own cell, or the feature cannot do its job.
    'w_flag_ok': 3,           # a flag the user dismissed ("this label is right")
    'w_reject': -6,           # a span the user deleted: negative evidence.
                              # Without it the prior only ever models what the
                              # model already produces.
    # --- scoring ---------------------------------------------------------
    'alpha': 0.5,             # additive smoothing
    'tau_flag': 5.0,          # margins in nats. Measured accuracy/coverage:
    'tau_suggest': 3.0,       #   tau 3 -> 0.948 / 0.82
    'tau_propagate': 8.0,     #   tau 5 -> 0.969 / 0.76
                              #   tau 8 -> 0.987 / 0.62
    # --- features --------------------------------------------------------
    'ctx_window': 40,         # chars of context scanned for the adjacent token
    'shape_cap': 14,
    'ord_cap': 6,
    'pos_bins': 5,
    'ntok_cap': 5,
    'use_delimiter_features': False,   # segidx / delim_pair. Off until the eval
                                       # harness shows they beat ordsh/possh,
                                       # which already carry much of the same
                                       # information.
    # --- decisions -------------------------------------------------------
    'propagate_max_targets': 25,
}

# Labels that are genuinely interchangeable for the same string, where a
# disagreement is usually a real-world fact rather than an error: one person is
# an author in one reference and an editor in another. Measured: role swaps are
# 20.6% of all high-margin disagreements, and suppressing them cuts the flag
# rate from 2.22% to 1.64% - almost all of it noise.
ROLE_CLASSES = (
    frozenset({'Author', 'Editor', 'Translator'}),
    frozenset({'Title', 'BookTitle'}),
)

# The only features that legitimately discriminate a role: the book's own marker
# (`red.`, `tlum.`, `ed.`, `hrsg.`) sits to the left of the span. Shape and text
# say nothing about whether a person edited or wrote something.
ROLE_MARKER_FEATURES = ('left', 'right', 'lshape')

FEATURES = ('shape', 'left', 'right', 'lshape', 'ordsh', 'possh', 'ntok', 'text')
DELIMITER_FEATURES = ('segidx', 'delim_pair')

_TOKEN = re.compile(r'\S+')


# --------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------

def shape_of(text, cap=14):
    """Character-class encoding with run collapsing: 's. 23.' -> 'x+. d+.'.

    str.isupper()/.isalpha() are Unicode-aware, so Polish diacritics classify
    correctly with no mapping table. Punctuation survives literally, which is
    what makes the shape carry the citation's own grammar.
    """
    out = []
    for ch in text:
        if ch.isdigit():
            out.append('d')
        elif ch.isspace():
            out.append(' ')
        elif ch.isupper():
            out.append('X')
        elif ch.isalpha():
            out.append('x')
        else:
            out.append(ch)
    s = ''.join(out)
    # Digit runs keep their length up to 4; letter runs do not. A 2-digit issue
    # number and a 4-digit year are different things and collapsing both to 'd+'
    # made the prior confidently relabel Issue as PublicationYear on the strength
    # of "shaped 'd+' is PublicationYear in 464 of 468 cases". Surname length
    # carries no comparable meaning, so those still collapse.
    s = re.sub(r'd{5,}', 'd+', s)
    s = re.sub(r'x+', 'x+', s)
    s = re.sub(r'X+', 'X+', s)
    s = re.sub(r' +', ' ', s)
    return s[:cap]


def _left_token(text, start, window):
    """Last whitespace-delimited token before the span.

    Windowed rather than 'last token of the whole prefix' so a span at the head
    of a long footnote doesn't reach back into unrelated prose.
    """
    toks = _TOKEN.findall(text[max(0, start - window):start])
    return toks[-1].lower() if toks else '<BOS>'


def _right_token(text, end, window):
    toks = _TOKEN.findall(text[end:end + window])
    return toks[0].lower() if toks else '<EOS>'


def _is_numeric(text):
    return text.strip().strip('.,;:()[]').replace('-', '').replace('–', '').isdigit()


def span_features(footnote_text, start, end, ordinal, ref_start, ref_end,
                  citation_type='normal', config=None, delimiters=None):
    """Feature cells for one span. Needs only the footnote text and offsets.

    Every key is namespaced by the enclosing reference's citation_type. That is
    not a voter - it partitions the tables. Measured: it cuts the flag rate from
    2.87% to 2.22% and removes the single largest false-flag class
    (Ibidem -> Author, 504 instances) outright, because inside the `ibidem`
    namespace an Ibidem-labelled span is the overwhelming majority.
    """
    cfg = dict(DEFAULTS, **(config or {}))
    span = footnote_text[start:end]
    sh = shape_of(span, cfg['shape_cap'])
    left = _left_token(footnote_text, start, cfg['ctx_window'])
    right = _right_token(footnote_text, end, cfg['ctx_window'])

    # Real data contains components whose end exceeds their parent reference's
    # end, so clamp rather than reject the span.
    ref_len = max(ref_end - ref_start, 1)
    pos = int(cfg['pos_bins'] * (start - ref_start) / ref_len)
    pos = min(max(pos, 0), cfg['pos_bins'] - 1)

    ntok = min(len(_TOKEN.findall(span)), cfg['ntok_cap'])
    numflag = 'num' if _is_numeric(span) else 'alp'
    ns = (citation_type or 'normal') + '|'

    feats = {
        'shape': ns + sh,
        'left': ns + left,
        'right': ns + right,
        'lshape': ns + left + '|' + sh,
        'ordsh': ns + 'o%d|%s' % (min(ordinal, cfg['ord_cap']), sh),
        'possh': ns + 'p%d|%s' % (pos, sh),
        'ntok': ns + 'n%d|%s' % (ntok, numflag),
        'text': ns + span.lower().strip(),
    }

    if cfg['use_delimiter_features'] and delimiters:
        feats['segidx'] = ns + 's%d|%s' % (
            _segment_index(footnote_text, start, end, ref_start, ref_end, delimiters, cfg), sh)
        feats['delim_pair'] = ns + _delim_pair(footnote_text, start, end, ref_start, ref_end)
    return feats


def _segment_index(text, start, end, ref_start, ref_end, delimiters, cfg):
    """Which delimiter-separated segment of the reference this span sits in.

    A delimiter-aware ordinal: unlike plain component order it survives missing
    or unlabelled neighbours, which is exactly the case the gap sweep cares about.
    """
    body = text[ref_start:ref_end]
    pattern = '|'.join(re.escape(d) for d in delimiters if d)
    if not pattern:
        return 0
    mid = (start + end) // 2 - ref_start
    idx = 0
    for m in re.finditer(pattern, body):
        if m.start() >= mid:
            break
        idx += 1
    return min(idx, cfg['ord_cap'])


def _delim_pair(text, start, end, ref_start, ref_end):
    before = text[max(ref_start, start - 6):start].strip() or '∅'
    after = text[end:min(ref_end, end + 6)].strip() or '∅'
    return before[-4:] + '>' + after[:4]


def fit_delimiters(observations):
    """The document's own component separators, most common first.

    Measured over 60 documents: the modal inter-component gap is ', ' in 41 of
    them, with a median modal share of 0.57 - real and document-specific, but
    not a majority of everything, which is why it informs a feature rather than
    acting as a hard tokeniser.
    """
    gaps = Counter()
    by_ref = defaultdict(list)
    for ob in observations:
        if ob.get('reference_id') is not None:
            by_ref[ob['reference_id']].append(ob)
    for spans in by_ref.values():
        spans.sort(key=lambda o: o['start'])
        for a, b in zip(spans, spans[1:]):
            gap = a['footnote_text'][a['end']:b['start']]
            if 0 < len(gap) <= 6:
                gaps[gap] += 1
    return [g for g, _ in gaps.most_common(2)]


# --------------------------------------------------------------------------
# fitting
# --------------------------------------------------------------------------

def build_prior(observations, config=None):
    """Fit the per-document tables.

    observations: [{features, label, weight, footnote_id}]. Negative weights are
    rejections (a span the user deleted) and subtract from their cell.
    """
    cfg = dict(DEFAULTS, **(config or {}))
    obs = [o for o in observations if o.get('label') and o.get('features')]

    positives = [o for o in obs if o.get('weight', cfg['w_model']) > 0]
    if len(positives) < cfg['n_min']:
        return {'status': 'inactive', 'reason': 'insufficient_spans',
                'n': len(positives), 'needed': cfg['n_min'], 'config': cfg}

    refs = {o.get('reference_id') for o in positives if o.get('reference_id') is not None}
    if refs and len(refs) < cfg['min_refs']:
        return {'status': 'inactive', 'reason': 'too_few_references',
                'n_refs': len(refs), 'needed': cfg['min_refs'], 'config': cfg}

    labels = sorted({o['label'] for o in positives})
    if len(labels) < 2:
        return {'status': 'inactive', 'reason': 'single_label', 'config': cfg}

    # cfg['features'] lets the eval harness ablate one voter at a time without
    # the module needing to know anything about ablation.
    feature_names = list(cfg.get('features') or FEATURES)
    if cfg['use_delimiter_features']:
        feature_names += [f for f in DELIMITER_FEATURES if f not in feature_names]

    tables = {f: defaultdict(Counter) for f in feature_names}
    footnotes = {f: defaultdict(lambda: defaultdict(set)) for f in feature_names}
    label_counts = Counter()

    for o in obs:
        w = o.get('weight', cfg['w_model'])
        label_counts[o['label']] += max(w, 0)
        for f in feature_names:
            cell = o['features'].get(f)
            if cell is None:
                continue
            tables[f][cell][o['label']] += w
            if w > 0 and o.get('footnote_id'):
                footnotes[f][cell][o['label']].add(o['footnote_id'])

    reliability = _reliability(positives, tables, label_counts, feature_names, cfg)
    if not any(v > 0 for v in reliability.values()):
        return {'status': 'inactive', 'reason': 'document_too_variable',
                'reliability': reliability, 'config': cfg}

    return {
        'status': 'active',
        'version': VERSION,
        'labels': labels,
        'tables': {f: {c: dict(v) for c, v in cells.items()} for f, cells in tables.items()},
        'footnotes': {f: {c: {y: len(s) for y, s in ys.items()} for c, ys in cells.items()}
                      for f, cells in footnotes.items()},
        'label_counts': dict(label_counts),
        'reliability': reliability,
        'features': feature_names,
        'n': len(positives),
        'n_refs': len(refs),
        'config': cfg,
    }


def _reliability(positives, tables, label_counts, feature_names, cfg):
    """Per-feature leave-one-out purity, inside this document, rescaled so a
    feature no better than the document's own majority class scores zero.

    No hand-tuned constant: a feature that carries no information in THIS book
    contributes nothing to the score, and the number is reported to the user as
    the reason the prior trusts what it trusts.
    """
    total = sum(label_counts.values()) or 1
    pi = max(label_counts.values()) / total
    out = {}
    for f in feature_names:
        hit = seen = 0
        for o in positives:
            cell = o['features'].get(f)
            if cell is None:
                continue
            counts = tables[f].get(cell)
            if not counts:
                continue
            w = o.get('weight', cfg['w_model'])
            adjusted = {y: c - (w if y == o['label'] else 0) for y, c in counts.items()}
            support = sum(v for v in adjusted.values() if v > 0)
            if support < 2:
                continue
            seen += 1
            if max(adjusted, key=lambda y: adjusted[y]) == o['label']:
                hit += 1
        lam = (hit / seen) if seen else 0.0
        out[f] = round(max(0.0, min(1.0, (lam - pi) / (1 - pi))) if pi < 1 else 0.0, 4)
    return out


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def _log_p(count, total, n_labels, alpha):
    return math.log((max(count, 0.0) + alpha) / (max(total, 0.0) + alpha * n_labels))


def score_span(prior, features, restrict=None, exclude_self=None):
    """Return (ranked [(label, score)], margin_in_nats, per_feature_votes).

    The decision statistic is the margin between best and second, not a
    normalised posterior: naive Bayes posteriors are pathologically overconfident
    when features are correlated, and lshape is literally left x shape. The
    margin stays monotone in evidence strength and reads directly as nats.

    exclude_self: {'label':…, 'weight':…} subtracts this span's own contribution,
    for leave-one-out evaluation.
    """
    if prior.get('status') != 'active':
        return [], 0.0, {}

    cfg = prior['config']
    labels = prior['labels']
    alpha = cfg['alpha']
    use = [f for f in prior['features']
           if (restrict is None or f in restrict) and prior['reliability'].get(f, 0) > 0]

    total_labels = sum(prior['label_counts'].values())
    scores = {}
    votes = defaultdict(dict)

    for y in labels:
        s = _log_p(prior['label_counts'].get(y, 0), total_labels, len(labels), alpha)
        for f in use:
            cell = features.get(f)
            if cell is None:
                continue
            counts = dict(prior['tables'][f].get(cell, {}))
            if exclude_self and exclude_self.get('label') in counts:
                counts[exclude_self['label']] -= exclude_self.get('weight', cfg['w_model'])
            tot = sum(v for v in counts.values() if v > 0)
            contribution = prior['reliability'][f] * _log_p(counts.get(y, 0), tot,
                                                            len(labels), alpha)
            s += contribution
            votes[f][y] = contribution
        scores[y] = s

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    margin = (ranked[0][1] - ranked[1][1]) if len(ranked) > 1 else 0.0
    return ranked, margin, dict(votes)


def _cell_support(prior, features, label):
    """Per-feature support for `label` in THIS span's own cells.

    Returns [(feature, weighted_count, distinct_footnotes)] for every feature
    whose cell ranks `label` first.

    The distinction matters. An earlier version took the maximum count across
    features and counted agreeing features separately, so one broadly-supported
    cell could satisfy the support bar while the agreeing features each rested
    on a single observation. A probe for a bare number after "s." was accepted
    at 7.9 nats on evidence reading "in 1 of 1" twice, carried entirely by the
    document's overall count of years. Support has to be substantiated in the
    same cells that produced the verdict.
    """
    out = []
    for f in prior['features']:
        if prior['reliability'].get(f, 0) <= 0:
            continue
        cell = features.get(f)
        if cell is None:
            continue
        counts = prior['tables'][f].get(cell, {})
        if not counts:
            continue
        if max(counts, key=lambda y: counts[y]) == label:
            out.append((f, counts.get(label, 0),
                        prior['footnotes'][f].get(cell, {}).get(label, 0)))
    return out


def accepts(prior, features, label, margin, threshold):
    """Whether a verdict clears every guard, not just the margin.

    Each supporting feature must independently clear both support bars, and
    enough of them must do so - a verdict resting on one substantiated cell
    plus several single-observation cells is refused.
    """
    if prior.get('status') != 'active' or margin < threshold:
        return False
    cfg = prior['config']
    qualifying = [f for f, weighted, fns in _cell_support(prior, features, label)
                  if weighted >= cfg['min_support'] and fns >= cfg['min_support_footnotes']]
    if len(qualifying) < cfg['min_agreeing_features']:
        return False
    return any(prior['reliability'].get(f, 0) >= cfg['min_decisive_reliability']
               for f in qualifying)


def same_role_class(a, b):
    return any(a in cls and b in cls for cls in ROLE_CLASSES)


_SENTINELS = ('<BOS>', '<EOS>')


def has_marker_evidence(features):
    """Is there an actual word before this span, rather than the start of a reference?

    A role flag must rest on the book's own marker - `red.`, `tlum.`, `ed.`,
    `hrsg.` - and nothing else. '<BOS>' means the span opens its reference, which
    is a POSITION signal: first-component-is-usually-Author. Letting that decide
    a role would flag every legitimate editor who happens to be cited first,
    which is precisely the confusion the role classes exist to suppress.
    """
    left = (features.get('left') or '').split('|', 1)[-1]
    return bool(left) and left not in _SENTINELS


# --------------------------------------------------------------------------
# reasons
# --------------------------------------------------------------------------

_READABLE = {
    'left': "after '%s', which is %s in %d of %d cases in this document",
    'lshape': "'%s' after '%s' is %s in %d of %d cases here",
    'shape': "shaped '%s' - %s in %d of %d cases in this document",
    'ordsh': "component %s of its reference, shaped '%s' - %s in %d of %d",
    'possh': "at that position in a reference - %s in %d of %d cases here",
    'ntok': "%d token(s), %s - %s in %d of %d cases here",
    'text': "'%s' is labelled %s in %d of %d places in this document",
    'right': "followed by '%s', which is %s in %d of %d cases here",
    'segidx': "in that delimiter-separated segment - %s in %d of %d",
    'delim_pair': "with that punctuation around it - %s in %d of %d",
}


def explain(prior, features, label, votes, limit=3):
    """Human-readable evidence for `label`, strongest feature first.

    The whole value of the review UI is *why*, not the number - and it is the
    display that lets someone notice a book-wide systematic error the prior
    would otherwise happily confirm.
    """
    if prior.get('status') != 'active':
        return []
    ranked = sorted(((f, v.get(label, 0.0) - max((s for y, s in v.items() if y != label),
                                                 default=0.0))
                     for f, v in votes.items()), key=lambda kv: -kv[1])
    out = []
    for f, strength in ranked:
        if strength <= 0 or len(out) >= limit:
            continue
        cell = features.get(f)
        counts = prior['tables'][f].get(cell, {})
        n = int(max(counts.get(label, 0), 0))
        tot = int(sum(v for v in counts.values() if v > 0))
        if not tot:
            continue
        raw = cell.split('|', 1)[1] if '|' in cell else cell
        if f == 'left':
            out.append(_READABLE[f] % (raw, label, n, tot))
        elif f == 'lshape':
            lft, _, sh = raw.partition('|')
            out.append(_READABLE[f] % (sh, lft, label, n, tot))
        elif f == 'right':
            out.append(_READABLE[f] % (raw, label, n, tot))
        elif f == 'shape':
            out.append(_READABLE[f] % (raw, label, n, tot))
        elif f == 'ordsh':
            ordv, _, sh = raw.partition('|')
            # The ordinal is 0-based; people count a reference's parts from 1.
            out.append(_READABLE[f] % (int(ordv.lstrip('o')) + 1, sh, label, n, tot))
        elif f == 'text':
            out.append(_READABLE[f] % (raw, label, n, tot))
        else:
            out.append("%s in %d of %d comparable spans in this document" % (label, n, tot))
    return out


# --------------------------------------------------------------------------
# document audit
# --------------------------------------------------------------------------

def audit_document(prior, scorables, dismissed=(), allowed_labels=None):
    """Flag stored spans whose label disagrees with the rest of the document.

    scorables: [{key, footnote_id, label, features, human}] - callers must
    exclude human-authored spans (`human=True` skips them here too), because a
    label a person chose is not ours to second-guess.

    allowed_labels bounds what may be SUGGESTED. Some labels exist in the model
    but not in the UI's picker (FootnoteMarker), so proposing one would hand the
    reviewer a correction they cannot make. Such labels stay in the fit as
    competing hypotheses - they just never come out as a suggestion.
    """
    if prior.get('status') != 'active':
        return {'flags': []}

    cfg = prior['config']
    dismissed = set(dismissed or ())
    flags = []

    for span in scorables:
        if span.get('human') or span['key'] in dismissed:
            continue
        ranked, margin, votes = score_span(prior, span['features'],
                                           exclude_self={'label': span['label'],
                                                         'weight': cfg['w_model']})
        if not ranked:
            continue
        best = ranked[0][0]
        if best == span['label']:
            continue
        if allowed_labels is not None and best not in allowed_labels:
            continue
        if not accepts(prior, span['features'], best, margin, cfg['tau_flag']):
            continue

        if same_role_class(span['label'], best):
            # A person really can be an author here and an editor there. Only
            # the book's own marker can settle it, so re-score on the marker
            # features alone; if they still disagree the flag is real.
            if not has_marker_evidence(span['features']):
                continue
            r2, m2, v2 = score_span(prior, span['features'], restrict=ROLE_MARKER_FEATURES,
                                    exclude_self={'label': span['label'],
                                                  'weight': cfg['w_model']})
            if not r2 or r2[0][0] == span['label'] or m2 < cfg['tau_flag']:
                continue
            if allowed_labels is not None and r2[0][0] not in allowed_labels:
                continue
            best, margin, votes = r2[0][0], m2, v2

        flags.append({
            'key': span['key'],
            'footnote_id': span.get('footnote_id'),
            'reference_id': span.get('reference_id'),
            'component_id': span.get('component_id'),
            'page': span.get('page'),
            'text': span.get('text'),
            'stored_label': span['label'],
            'suggested_label': best,
            'margin': round(margin, 2),
            'reasons': explain(prior, span['features'], best, votes),
        })

    flags.sort(key=lambda f: -f['margin'])
    return {'flags': flags}


def suggest_label(prior, features, threshold=None, allowed_labels=None):
    """Best label for an unlabelled selection, or None if the prior abstains.

    allowed_labels bounds the answer to labels the caller can actually apply -
    see audit_document.
    """
    if prior.get('status') != 'active':
        return None
    cfg = prior['config']
    threshold = cfg['tau_suggest'] if threshold is None else threshold
    ranked, margin, votes = score_span(prior, features)
    if allowed_labels is not None:
        ranked = [r for r in ranked if r[0] in allowed_labels]
    if not ranked:
        return None
    best = ranked[0][0]
    if not accepts(prior, features, best, margin, threshold):
        return None
    return {'label': best, 'margin': round(margin, 2),
            'reasons': explain(prior, features, best, votes)}


def propagation_targets(prior, corrected_features, label, candidates):
    """Spans that should arguably carry `label` too, for an explicit "apply to N".

    Matched on the discriminating feature cell, never on the text: correcting one
    'A. Nowicki' to Editor must reach only the occurrences whose marker/position
    also match, not every instance of that person's name.
    """
    if prior.get('status') != 'active':
        return []
    cfg = prior['config']

    usable = [f for f in prior['features'] if prior['reliability'].get(f, 0) > 0]
    if label in ROLE_CLASSES[0]:
        # Role is carried by the marker, not by the name or the shape.
        usable = [f for f in usable if f in ROLE_MARKER_FEATURES]
    if not usable:
        return []
    discriminator = max(usable, key=lambda f: prior['reliability'][f])
    cell = corrected_features.get(discriminator)
    if cell is None:
        return []

    out = []
    for c in candidates:
        if c.get('human') or c.get('label') == label:
            continue
        if c['features'].get(discriminator) != cell:
            continue
        ranked, margin, _v = score_span(prior, c['features'])
        if not ranked or ranked[0][0] != label:
            continue
        if not accepts(prior, c['features'], label, margin, cfg['tau_propagate']):
            continue
        out.append({'key': c['key'], 'footnote_id': c.get('footnote_id'),
                    'component_id': c.get('component_id'), 'page': c.get('page'),
                    'text': c.get('text'), 'current_label': c.get('label'),
                    'margin': round(margin, 2)})
        if len(out) >= cfg['propagate_max_targets']:
            break
    return {'feature': discriminator, 'cell': cell, 'targets': out}


# --------------------------------------------------------------------------
# assembling observations from the stored document shape
# --------------------------------------------------------------------------

def _raw_spans(footnotes):
    """Minimal span records for fitting the document's delimiters, before any
    feature extraction can run."""
    out = []
    for fn in footnotes or []:
        text = fn.get('text') or ''
        for ref in fn.get('references') or []:
            for c in ref.get('components') or []:
                if c.get('start') is None or c.get('end') is None:
                    continue
                out.append({'reference_id': ref.get('id'), 'footnote_text': text,
                            'start': c['start'], 'end': c['end']})
    return out


def collect_observations(footnotes, config=None, weight_of=None):
    """Turn a job's `result['results']` into observations.

    Lives here rather than in the app so the eval harness and the request path
    build identical inputs. Knows the stored JSON shape, nothing about Flask.
    """
    cfg = dict(DEFAULTS, **(config or {}))
    out = []

    # Two passes when the delimiter features are on: the document's own
    # separators have to be fitted from every span before any span's segment
    # index can be computed.
    delimiters = None
    if cfg['use_delimiter_features']:
        delimiters = fit_delimiters(_raw_spans(footnotes))

    for fn in footnotes or []:
        text = fn.get('text') or ''
        fid = fn.get('id')
        for ref in fn.get('references') or []:
            rs = ref.get('start') or 0
            re_ = ref.get('end') if ref.get('end') is not None else len(text)
            ctype = ref.get('citation_type') or 'normal'
            comps = [c for c in (ref.get('components') or [])
                     if c.get('label') and c.get('start') is not None
                     and c.get('end') is not None]
            comps.sort(key=lambda c: c['start'])
            for i, c in enumerate(comps):
                human = bool(c.get('source') == 'manual' or c.get('edited'))
                out.append({
                    'key': 'c_' + str(c.get('id')),
                    'footnote_id': fid,
                    'reference_id': ref.get('id'),
                    'component_id': c.get('id'),
                    'page': fn.get('page'),
                    'label': c['label'],
                    'text': c.get('text'),
                    'start': c['start'],
                    'end': c['end'],
                    'footnote_text': text,
                    'human': human,
                    'weight': (weight_of(fn, ref, c) if weight_of
                               else (cfg['w_explicit'] if human else cfg['w_model'])),
                    'features': span_features(text, c['start'], c['end'], i, rs, re_,
                                              ctype, cfg, delimiters),
                })
    return out
