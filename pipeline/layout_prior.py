#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Intra-document layout consistency prior for footnote detection.

Within one book the page layout barely changes, so a detected box that
disagrees with the rest of the document is suspect, and a sub-threshold
proposal that agrees with it is probably real. This module turns that into a
score adjustment.

Deliberately dependency-free (stdlib only - no Flask, no torch, no
ultralytics) so the web app and the offline evaluation harness score through
exactly one implementation rather than two that drift.

Two modes:

  full        the job has a proposal sidecar (see PDFFootnoteExtractor's
              last_page_proposals). The prior is fitted on exact, un-inflated
              proposal coordinates, and both flags and rescues are available.

  flags_only  no sidecar - every job processed before sidecars existed. The
              prior is fitted on the stored footnote bboxes instead. Those
              carry convert_bounding_boxes()' uniform +3pt inflation, but
              uniformly, so a prior fitted and scored on them is internally
              consistent. No sub-threshold proposals exist, so no rescues.

What the prior deliberately does NOT do is move a box. It is fitted on the
model's own output, so it will happily certify a systematic error (the +3pt
inflation itself, say, or a checkpoint that always clips the last line);
snapping boxes to it would propagate that across a whole book. It only scores.
"""
from __future__ import annotations

import math
from collections import defaultdict

VERSION = 1

DEFAULTS = {
    # --- fitting ---------------------------------------------------------
    'tau_trust': 0.85,        # a detection must beat this to inform the prior
    'n_min': 8,               # distinct regions required before the prior activates
    'min_pages': 4,           # ...spread over at least this many pages
    's_min': 2.0,             # floor on every scale, in PDF points. Essential:
                              # measured MAD(x1) is 0.0pt at the 10th percentile,
                              # and an unfloored scale turns a 3pt deviation
                              # into 50 sigma.
    # s_max keeps scoring numerically sane; max_s_* decide whether the document
    # is consistent enough to model at all. They are compared against the RAW
    # MAD, not the clamped scale - gating on the clamped value would be dead
    # code, since s_max is deliberately the tighter of the two.
    's_max': 12.0,
    'max_s_x1': 15.0,         # measured max MAD(x1) over 113 real documents is 16.5pt,
    'max_s_y2': 15.0,         # so this deactivates roughly the worst decile
    'modal_bucket_frac': 0.70,
    # --- weights ---------------------------------------------------------
    'w_model': 1,             # the detector's own confident box
    'w_implicit': 2,          # a page the user viewed and left alone
    'w_explicit': 5,          # a box the user drew or resized, or a flag dismissed
    # --- scoring ---------------------------------------------------------
    'sigma_log_font': 0.08,
    'delta_max': 2.0,         # logit clamp downwards; the primary safety rail
    # Evidence for, not just against. The geometry/font terms are both
    # log-likelihoods and so never positive, which on its own would mean the
    # prior could only ever push a box down - and no sub-threshold proposal
    # could ever be rescued, however perfectly it matched the document. This is
    # the credit a box earns for fitting: a flawless match lands at +delta_boost,
    # a poor one is dragged back below zero by the penalty terms. At 2.0 a 0.25
    # proposal tops out near 0.71, so nothing weak is promoted on geometry alone.
    'delta_boost': 2.0,
    'y2_band_frac': 0.35,     # how far above the bottom margin is unremarkable
    'height_slack': 1.5,      # multiple of p95 height before an excess penalty
    'line_height_ratio': 1.9, # generous points-per-line multiplier vs font size
    # --- decisions -------------------------------------------------------
    'flag_below': 0.5,
    # A separate, confidence-independent flag path. The delta clamp means a box
    # above conf ~0.88 can never be pushed under flag_below however wrong its
    # geometry is - so without this the "model is confidently wrong" population,
    # exactly the one its own score cannot triage, would be unflaggable.
    # -12.5 is a single coordinate off by 5 sigma.
    'flag_geom_below': -12.5,
    'rescue_min_conf': 0.25,
    'rescue_min_conf_adj': 0.5,
    'rescue_max_per_page': 2,
}


# --------------------------------------------------------------------------
# weighted robust statistics
# --------------------------------------------------------------------------

def _wpercentile(pairs, q):
    """Weighted percentile of [(value, weight)] at q in [0,1]. None if empty."""
    pairs = [(v, w) for v, w in pairs if w > 0 and v is not None]
    if not pairs:
        return None
    pairs.sort(key=lambda t: t[0])
    total = sum(w for _, w in pairs)
    target = total * q
    cum = 0.0
    for v, w in pairs:
        cum += w
        if cum >= target:
            return v
    return pairs[-1][0]


def _wmedian(pairs):
    return _wpercentile(pairs, 0.5)


def _wmad(pairs, centre):
    """Weighted median absolute deviation, scaled comparable to a stdev."""
    if centre is None:
        return None
    mad = _wmedian([(abs(v - centre), w) for v, w in pairs if v is not None])
    return None if mad is None else 1.4826 * mad


def _scale(pairs, centre, cfg):
    """MAD-based scale, clamped. The clamp is what keeps a document of
    pixel-identical boxes from flagging a 3pt deviation at 50 sigma."""
    mad = _wmad(pairs, centre)
    if mad is None:
        return cfg['s_min']
    return min(max(mad, cfg['s_min']), cfg['s_max'])


def _sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _logit(p, eps=1e-6):
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


# --------------------------------------------------------------------------
# fitting
# --------------------------------------------------------------------------

def _dedupe(regions):
    """Drop regions sharing a page and geometry.

    Mandatory, not tidiness: split_footnotes=True makes N footnotes copy one
    detected region's bbox verbatim, so without this a single six-way split
    would carry six times the weight of every other page in the document.
    """
    seen, out = set(), []
    for r in regions:
        key = (r['page'],) + tuple(round(v, 2) for v in r['bbox'])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _modal_page_bucket(regions, page_meta):
    """Most common rounded page size, and the regions sitting on it.

    A book with plates or a differently-sized appendix otherwise poisons the
    bottom-margin estimate, which is the tightest signal available.
    """
    counts = defaultdict(int)
    for r in regions:
        meta = page_meta.get(r['page']) or {}
        counts[(round(meta.get('w') or 0), round(meta.get('h') or 0))] += 1
    if not counts:
        return None, regions, 1.0
    modal = max(counts.items(), key=lambda kv: kv[1])[0]

    def bucket(r):
        meta = page_meta.get(r['page']) or {}
        return (round(meta.get('w') or 0), round(meta.get('h') or 0))

    kept = [r for r in regions if bucket(r) == modal]
    return modal, kept, len(kept) / len(regions)


def _fit_class(regions, cfg):
    """Fit one parity class. None if it has nothing usable."""
    if not regions:
        return None

    def w(r):
        return r.get('weight', cfg['w_model'])

    x1 = [(r['bbox'][0], w(r)) for r in regions]
    m_x1 = _wmedian(x1)
    s_x1 = _scale(x1, m_x1, cfg)
    raw_x1 = _wmad(x1, m_x1)

    # The bottom margin is a property of the lowest region on a page: a
    # footnote stacked above another one legitimately ends higher, and folding
    # those in would smear a margin otherwise stable to within half a point.
    bottom_per_page = {}
    for r in regions:
        cur = bottom_per_page.get(r['page'])
        if cur is None or r['bbox'][3] > cur['bbox'][3]:
            bottom_per_page[r['page']] = r
    y2 = [(r['bbox'][3], w(r)) for r in bottom_per_page.values()]
    m_y2 = _wmedian(y2)
    s_y2 = _scale(y2, m_y2, cfg)
    raw_y2 = _wmad(y2, m_y2)

    # The right edge is right-censored: a one-line footnote ends where its text
    # ends, so only an over-long box is evidence of anything. Anchor at p90,
    # score one-sided, and fit the scale on the upper half alone.
    x2_pairs = [(r['bbox'][2], w(r)) for r in regions]
    r_x2 = _wpercentile(x2_pairs, 0.90)
    med_x2 = _wmedian(x2_pairs)
    upper = [(v, ww) for v, ww in x2_pairs if med_x2 is None or v >= med_x2]
    s_x2 = _scale(upper, r_x2, cfg)

    heights = [(r['bbox'][3] - r['bbox'][1], w(r)) for r in regions]
    p95_h = _wpercentile(heights, 0.95)

    fonts = [(r.get('font'), w(r)) for r in regions if r.get('font')]
    total_w = sum(w(r) for r in regions) or 1
    coverage = sum(ww for _, ww in fonts) / total_w

    return {
        'n': len(regions),
        'pages': len({r['page'] for r in regions}),
        # Each coordinate carries its own usable flag. Books differ in which
        # parts of their layout are actually regular: one real book here has a
        # left column stable to 2pt but a bottom margin varying by 114pt,
        # because its footnotes float under the body text instead of being
        # bottom-anchored. Abstaining on the bad coordinate keeps the good one
        # working; killing the whole prior would throw the signal away with it.
        'x1': {'m': m_x1, 's': s_x1, 'mad': raw_x1,
               'usable': (raw_x1 or 0) <= cfg['max_s_x1']},
        'y2': {'m': m_y2, 's': s_y2, 'mad': raw_y2,
               'usable': (raw_y2 or 0) <= cfg['max_s_y2']},
        'x2': {'p90': r_x2, 's': s_x2},
        'height': {'p95': p95_h},
        'font': {'note': _wmedian(fonts), 'coverage': round(coverage, 3)},
    }


def build_prior(regions, page_meta, config=None, rejects=None):
    """Fit the prior.

    regions   [{page, bbox: (x1,y1,x2,y2), conf, font, weight}] - already
              filtered to trusted detections plus user confirmations.
    page_meta {page: {w, h, has_text, body_font, line_count}}
    rejects   [{page, bbox, font}] - boxes a human deleted or refused. Without
              these the prior is one-class: it models what a footnote looks
              like and knows nothing about what a false positive looks like in
              this particular document.
    """
    cfg = dict(DEFAULTS, **(config or {}))
    page_meta = {int(k): v for k, v in (page_meta or {}).items()}

    regions = _dedupe(list(regions or []))
    if len(regions) < cfg['n_min']:
        return {'status': 'inactive', 'reason': 'insufficient_samples',
                'n': len(regions), 'config': cfg}

    modal, kept, frac = _modal_page_bucket(regions, page_meta)
    if frac < cfg['modal_bucket_frac']:
        return {'status': 'inactive', 'reason': 'mixed_page_sizes',
                'modal_fraction': round(frac, 3), 'config': cfg}
    regions = kept

    if len({r['page'] for r in regions}) < cfg['min_pages']:
        return {'status': 'inactive', 'reason': 'too_few_pages', 'config': cfg}

    odd = [r for r in regions if r['page'] % 2 == 1]
    even = [r for r in regions if r['page'] % 2 == 0]

    # Mirrored recto/verso margins are common enough in real books (measured:
    # 21 of 60 documents differ by more than 6pt in median left edge) that one
    # pooled prior would smear the strongest signal available. Only split when
    # each side can stand on its own.
    half = max(cfg['n_min'] // 2, 4)
    parity_split = len(odd) >= half and len(even) >= half
    if parity_split:
        classes = {'odd': _fit_class(odd, cfg), 'even': _fit_class(even, cfg)}
    else:
        classes = {'combined': _fit_class(regions, cfg)}

    for name, cls in classes.items():
        if cls is None:
            return {'status': 'inactive', 'reason': 'empty_class:' + name, 'config': cfg}
        # The left edge is the primary signal - size-independent, and the one
        # that works for the small footnotes this targets. Without it there is
        # not enough left to be worth scoring.
        if not cls['x1']['usable']:
            return {'status': 'inactive', 'reason': 'layout_too_variable',
                    'class': name,
                    'mad_x1': round(cls['x1']['mad'] or 0, 2),
                    'mad_y2': round(cls['y2']['mad'] or 0, 2),
                    'config': cfg}

    body_fonts = [(m.get('body_font'), m.get('line_count') or 1)
                  for m in page_meta.values() if m.get('body_font')]

    rejects = list(rejects or [])
    reject_fonts = [(r.get('font'), 1) for r in rejects if r.get('font')]

    return {
        'status': 'active',
        'rejects': rejects,
        # Two rejections is the minimum that could show a pattern rather than
        # one person's stray click.
        'reject_font': _wmedian(reject_fonts) if len(reject_fonts) >= 2 else None,
        'version': VERSION,
        'parity_split': parity_split,
        'classes': classes,
        'page_geometry': {'width': modal[0] if modal else None,
                          'height': modal[1] if modal else None,
                          'modal_fraction': round(frac, 3)},
        'body_font': _wmedian(body_fonts),
        'page_meta': page_meta,
        'config': cfg,
    }


def _class_for(prior, page):
    classes = prior['classes']
    if 'combined' in classes:
        return classes['combined']
    return classes['odd'] if page % 2 == 1 else classes['even']


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score_proposal(prior, proposal):
    """Return (conf_adj, reasons).

    reasons are human-readable: the point of the review UI is *why* a box was
    flagged, not the number.
    """
    conf_adj, _pen, _ev, reasons = score_terms(prior, proposal)
    return conf_adj, reasons


def score_terms(prior, proposal):
    """(conf_adj, conf_penalised, evidence, reasons).

    conf_adj        confidence after both the boost and the penalties - what a
                    rescue decision uses, and what the UI shows.
    conf_penalised  penalties only, no boost. Flagging uses this so that a box
                    fitting the prior well can't have its own poor confidence
                    papered over.
    evidence        l_geom + l_font, the raw log-likelihood. Confidence-free, so
                    the audit can flag a confidently-wrong box on geometry alone.
    """
    if prior.get('status') != 'active':
        conf = proposal.get('conf')
        return conf, conf, 0.0, []

    cfg = prior['config']
    cls = _class_for(prior, proposal['page'])
    x1, y1, x2, y2 = proposal['bbox']
    meta = prior['page_meta'].get(proposal['page'], {})
    page_h = meta.get('h') or prior['page_geometry']['height'] or 800.0
    reasons, terms = [], []

    # --- left column edge: the strongest signal, and size-independent, which
    # is what makes it work for the small footnotes this is aimed at ---------
    z = abs(x1 - cls['x1']['m']) / cls['x1']['s']
    terms.append(z)
    if z > 3:
        side = 'left' if x1 < cls['x1']['m'] else 'right'
        reasons.append('left edge %.0fpt to the %s of the document column (%.0f sigma)'
                       % (abs(x1 - cls['x1']['m']), side, z))

    # --- bottom margin, asymmetric, and only when this book actually has one.
    # Extending *below* where footnotes ever end is always wrong. Ending above
    # it is normal for a stacked footnote, so that side gets a wide band. -----
    if cls['y2']['usable']:
        z_low = max(0.0, y2 - cls['y2']['m']) / cls['y2']['s']
        terms.append(z_low)
        if z_low > 3:
            reasons.append('extends %.0fpt below the footnote margin (%.0f sigma)'
                           % (y2 - cls['y2']['m'], z_low))

        band = max(cfg['y2_band_frac'] * page_h, 3 * cls['y2']['s'])
        z_high = max(0.0, (cls['y2']['m'] - y2) - band) / max(0.15 * page_h, 1.0)
        terms.append(z_high)
        if z_high > 1:
            reasons.append('sits %.0fpt above the footnote area - mid-page for this layout'
                           % (cls['y2']['m'] - y2))

    # --- right edge, one-sided: never penalise a box for being short ---------
    if cls['x2']['p90'] is not None:
        z_x2 = max(0.0, x2 - cls['x2']['p90']) / cls['x2']['s']
        terms.append(z_x2)
        if z_x2 > 3:
            reasons.append('right edge %.0fpt past the text column (%.0f sigma)'
                           % (x2 - cls['x2']['p90'], z_x2))

    # --- excess height. Prefer the line-count estimate when the page has a
    # text layer; it catches a box that swallowed whitespace or ran up into
    # body text, which the document-wide p95 is too blunt to see. ------------
    h = y2 - y1
    expected = None
    lines = proposal.get('lines_inside')
    font = proposal.get('font') or cls['font']['note']
    if lines and font:
        expected = lines * font * cfg['line_height_ratio']
    elif cls['height']['p95']:
        expected = cls['height']['p95'] * cfg['height_slack']
    if expected and expected > 0:
        z_h = max(0.0, h - expected) / max(0.5 * expected, 1.0)
        terms.append(z_h)
        if z_h > 1.5:
            reasons.append('%.0fpt tall for %s line(s) of text - about %.0fpt expected'
                           % (h, lines if lines else 'its', expected))

    l_geom = -0.5 * sum(t * t for t in terms)

    # --- font size. Abstains on a page with no text layer: absence of text is
    # not evidence against a box, and most scanned uploads have none. --------
    l_font = 0.0
    f_box, f_note = proposal.get('font'), cls['font']['note']
    if f_box and f_note and meta.get('has_text'):
        ratio = math.log(f_box / f_note) / cfg['sigma_log_font']
        l_font = -0.5 * ratio * ratio
        if abs(ratio) > 2:
            body = prior.get('body_font')
            if body and abs(f_box - body) < abs(f_box - f_note):
                reasons.append('font %.1fpt matches body text, not the %.1fpt footnote size'
                               % (f_box, f_note))
            else:
                reasons.append('font %.1fpt vs %.1fpt elsewhere in this document'
                               % (f_box, f_note))

    # --- learned from rejections. Meaningful once the people working on this
    # document have deleted a few boxes AND those share a typography distinct
    # from the footnotes, which is the usual case: a detector grabs running
    # heads or body paragraphs, not random rectangles.
    reject_font = prior.get('reject_font')
    if f_box and f_note and reject_font and meta.get('has_text'):
        if abs(math.log(reject_font / f_note)) > 0.1:
            d_note = abs(math.log(f_box / f_note))
            d_rej = abs(math.log(f_box / reject_font))
            if d_rej < d_note:
                l_font += -0.5 * ((d_note - d_rej) / cfg['sigma_log_font']) ** 2
                reasons.append('font %.1fpt matches boxes rejected in this document, '
                               'not the %.1fpt footnote size' % (f_box, f_note))

    evidence = l_geom + l_font
    conf = proposal.get('conf')
    if conf is None:
        return None, None, evidence, reasons

    boosted = max(-cfg['delta_max'], min(cfg['delta_boost'], cfg['delta_boost'] + evidence))
    penalised = max(-cfg['delta_max'], min(0.0, evidence))
    base = _logit(conf)
    return _sigmoid(base + boosted), _sigmoid(base + penalised), evidence, reasons


def positive_reasons(prior, proposal):
    """Short affirmative notes for a rescue - why we think it is real."""
    if prior.get('status') != 'active':
        return []
    cls = _class_for(prior, proposal['page'])
    x1, _, _, y2 = proposal['bbox']
    out = []
    dz = abs(x1 - cls['x1']['m']) / cls['x1']['s']
    if dz <= 1.5:
        out.append('left edge matches the document column (%.1f sigma)' % dz)
    if cls['y2']['usable']:
        dy = abs(y2 - cls['y2']['m'])
        if dy <= max(3.0, 2 * cls['y2']['s']):
            out.append('bottom edge within %.0fpt of the page pattern' % dy)
    f_box, f_note = proposal.get('font'), cls['font']['note']
    if f_box and f_note and abs(math.log(f_box / f_note)) < 0.12:
        out.append('font %.1fpt matches the footnote size' % f_box)
    if proposal.get('lines_inside'):
        out.append('%d line(s) of text inside' % proposal['lines_inside'])
    return out


# --------------------------------------------------------------------------
# document audit
# --------------------------------------------------------------------------

def audit_document(prior, scorables, rescuables, rejects=None):
    """Turn scored boxes into review suggestions.

    scorables   already-stored boxes to check, as
                [{key, page, bbox, conf, font, lines_inside}]. Callers must
                exclude anything the user authored - a box someone drew or
                resized is not ours to second-guess.
    rescuables  sub-threshold proposals eligible for promotion, same shape.
    """
    if prior.get('status') != 'active':
        return {'flags': [], 'rescues': []}

    cfg = prior['config']
    flags = []
    for box in scorables:
        _conf_adj, penalised, evidence, reasons = score_terms(prior, box)
        if not reasons:
            continue
        by_conf = penalised is not None and penalised < cfg['flag_below']
        by_geom = evidence < cfg['flag_geom_below']
        if not (by_conf or by_geom):
            continue
        flags.append({'key': box['key'], 'page': box['page'], 'bbox': box['bbox'],
                      'conf': box.get('conf'),
                      'conf_adj': round(penalised, 4) if penalised is not None else None,
                      'basis': 'confidence' if by_conf else 'geometry',
                      'reasons': reasons})

    by_page = defaultdict(list)
    for box in rescuables:
        conf = box.get('conf')
        if conf is None or conf < cfg['rescue_min_conf']:
            continue
        conf_adj, _pen, _ev, _r = score_terms(prior, box)
        if conf_adj is None or conf_adj < cfg['rescue_min_conf_adj']:
            continue
        # A page with a text layer but no text inside the box is a blank
        # region, whatever the geometry says.
        meta = prior['page_meta'].get(box['page'], {})
        if meta.get('has_text') and not box.get('lines_inside'):
            continue
        # Never re-suggest something a human already threw away on this page.
        # No score is allowed to overrule that.
        if _overlaps_reject(box, rejects if rejects is not None else prior.get('rejects')):
            continue
        by_page[box['page']].append(
            {'key': box['key'], 'page': box['page'], 'bbox': box['bbox'],
             'conf': conf, 'conf_adj': round(conf_adj, 4),
             'reasons': positive_reasons(prior, box)})

    rescues = []
    for page, items in by_page.items():
        items.sort(key=lambda d: -d['conf_adj'])
        rescues.extend(items[:cfg['rescue_max_per_page']])

    flags.sort(key=lambda d: (d['page'], d['conf_adj']))
    rescues.sort(key=lambda d: (d['page'], -d['conf_adj']))
    return {'flags': flags, 'rescues': rescues}


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _overlaps_reject(box, rejects, thresh=0.5):
    for r in (rejects or []):
        if r.get('page') == box['page'] and _iou(tuple(box['bbox']), tuple(r['bbox'])) >= thresh:
            return True
    return False


def clamp_to_page(bbox, page_w, page_h):
    """Clip a bbox to the page rect.

    _validate_bbox in app.py checks only that the box is non-degenerate, not
    that it is on the page - the activity log contains a real stored box at
    x1=-359. Anything the prior proposes goes through here first.
    """
    x1, y1, x2, y2 = bbox
    x1 = max(0.0, min(x1, page_w))
    x2 = max(0.0, min(x2, page_w))
    y1 = max(0.0, min(y1, page_h))
    y2 = max(0.0, min(y2, page_h))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)
