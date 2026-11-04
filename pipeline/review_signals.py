#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""How much each kind of reviewer signal counts for the document priors, and
how long a page must stay on screen to count as looked at.

Layout Assist learns a book's footnote layout (layout_prior) and citation
style (ner_prior) from the document itself and from the people reviewing it.
Not every signal is worth the same: a page someone merely looked at says less
than a page they deliberately approved, and both say less than an element
they opened and changed. This is the one table that ranks them:

    model              the detector's / NER model's own output
    page_passive       the page stayed on screen past its dwell floor
                       (dwell_floor_ms below) with nothing left open on it
    page_active        the reviewer clicked the page's approval icon, which
                       marks every element on the page as checked
    element_confirmed  opened one element and accepted it as it was:
                       a box accepted unchanged, "Box is fine", "Keep <label>"
    element_edited     drew, reshaped or relabelled one element (a deletion
                       is the negative counterpart, recorded as a rejection)

Two columns because the priors weigh evidence on different scales; the
defaults keep the values each prior used before this table existed (model 1,
viewed page 2, human action 5 for boxes / 6 for labels).

Every caller goes through weight_for(), and every signal is recorded with the
features that could explain it (dwell time, how many elements the reviewer
touched on the page, what was still open, who) - so the fixed numbers here
can later be replaced by weights learned from that activity without touching
the call sites.

Override without a code change, e.g. while tuning with the replay script:

    FOOTNOTES_REVIEW_WEIGHTS='{"layout": {"page_active": 3.5}}' python wsgi.py

Stdlib only, so the app and the offline evaluation read the same numbers.
"""
from __future__ import annotations

import json
import os
import statistics

SIGNALS = ('model', 'page_passive', 'page_active', 'element_confirmed', 'element_edited')

DEFAULT_WEIGHTS = {
    'layout': {'model': 1.0, 'page_passive': 2.0, 'page_active': 3.0,
               'element_confirmed': 4.0, 'element_edited': 5.0},
    'labels': {'model': 1.0, 'page_passive': 2.0, 'page_active': 4.0,
               'element_confirmed': 5.0, 'element_edited': 6.0},
}

# The layout prior's stored confirmation kinds (see _lp_record_confirmation in
# app.py), as signals. 'rejected' is not here: a rejection is a negative
# example, kept apart from the weighted positives.
ELEMENT_SIGNAL = {
    'created': 'element_edited',
    'corrected': 'element_edited',
    'confirmed': 'element_confirmed',
    'flag_dismissed': 'element_confirmed',
}


def load_weights(env=None):
    """DEFAULT_WEIGHTS with any FOOTNOTES_REVIEW_WEIGHTS override merged in.

    Unknown priors/signals and non-positive or non-numeric values are ignored
    rather than fatal: a typo in an environment variable must not take the
    app down, it just leaves the default in place.
    """
    weights = {prior: dict(table) for prior, table in DEFAULT_WEIGHTS.items()}
    raw = (env if env is not None else os.environ).get('FOOTNOTES_REVIEW_WEIGHTS')
    if not raw:
        return weights
    try:
        override = json.loads(raw)
    except ValueError:
        return weights
    if not isinstance(override, dict):
        return weights
    for prior, table in override.items():
        if prior not in weights or not isinstance(table, dict):
            continue
        for signal, value in table.items():
            if signal in SIGNALS and isinstance(value, (int, float)) and value > 0:
                weights[prior][signal] = float(value)
    return weights


WEIGHTS = load_weights()


def weight_for(prior, signal, features=None):
    """How much one signal counts for `prior` ('layout' or 'labels').

    `features` is what was recorded alongside the signal (dwell_ms,
    element_actions_on_page, open_items, by_user_id, ...). It is unused today.

    TODO: replace the fixed table with weights learned from reviewer activity
    - time on the page, how many elements were touched, how often this
    reviewer's approvals later needed correcting - fed by exactly these
    features, the same way the dwell floor (dwell_floor_ms) is meant to be
    learned.
    """
    return WEIGHTS[prior][signal]


# --------------------------------------------------------------------------
# How long a page must stay on screen to count as looked at (passive approval)
# --------------------------------------------------------------------------
#
# A page with nothing detected needs a glance to see nothing was missed; a
# page full of footnotes, text and tagged references needs longer to have
# actually been read. The floor grows with what there is to check:
#
#     no detections:  empty_ms
#     otherwise:      base_ms + per_footnote_ms * footnotes
#                             + per_100_chars_ms * (OCR characters / 100)
#                             + per_reference_ms * references
#                             + per_component_ms * components
#                     clamped to [min_ms, max_ms]
#
# The clamp is for pages with footnotes only: a page with nothing detected
# takes empty_ms as it is.
#
# Override without a code change:
#
#     FOOTNOTES_DWELL_PARAMS='{"empty_ms": 1500, "per_component_ms": 150}' python wsgi.py
#
# (FOOTNOTES_DWELL_MS in app.py still pins one fixed floor for every page,
# which is only for exercising the signal by hand.)

DWELL_DEFAULTS = {          # standard timers
    'empty_ms': 1000.0,
    'base_ms': 2000.0,
    'per_footnote_ms': 500.0,
    'per_100_chars_ms': 200.0,
    'per_reference_ms': 200.0,
    'per_component_ms': 100.0,
    'min_ms': 1500.0,
    'max_ms': 20000.0,
}


def load_dwell_params(env=None):
    """DWELL_DEFAULTS with any FOOTNOTES_DWELL_PARAMS override merged in;
    unknown keys and negative or non-numeric values are ignored."""
    params = dict(DWELL_DEFAULTS)
    raw = (env if env is not None else os.environ).get('FOOTNOTES_DWELL_PARAMS')
    if not raw:
        return params
    try:
        override = json.loads(raw)
    except ValueError:
        return params
    if isinstance(override, dict):
        for key, value in override.items():
            if key in params and isinstance(value, (int, float)) and value >= 0:
                params[key] = float(value)
    return params


DWELL = load_dwell_params()


def dwell_floor_ms(features, params=None):
    """How long this page must stay on screen to count as looked at.

    `features` describes the page as the reviewer sees it: footnotes,
    ocr_chars (their text, summed), references, components. `params` are the
    reviewer's own timers (learn_dwell_params or set on their profile);
    the standard ones (DWELL) without.
    """
    p = params or DWELL
    footnotes = features.get('footnotes', 0)
    if not footnotes:
        return int(round(p['empty_ms']))
    floor = (p['base_ms']
             + p['per_footnote_ms'] * footnotes
             + p['per_100_chars_ms'] * features.get('ocr_chars', 0) / 100.0
             + p['per_reference_ms'] * features.get('references', 0)
             + p['per_component_ms'] * features.get('components', 0))
    return int(round(min(max(floor, p['min_ms']), p['max_ms'])))


def page_signal(mode):
    """The signal a page approval record stands for; records written before
    approvals had a mode were all passive."""
    return 'page_active' if mode == 'active' else 'page_passive'


# --------------------------------------------------------------------------
# Span templates (span_templates.py): when a learned template is trusted
# --------------------------------------------------------------------------
#
#     min_support              reviewer actions that must agree with a
#                              template before it is applied to unapproved
#                              pages on its own
#     max_contradiction_share  share of reviewer actions allowed to disagree
#                              (0: a single contradicting action holds it back)
#     principle_weight         how much the general principle counts, as one
#                              more voter, against reviewers' evidence - one
#                              edited label's worth (DEFAULT_WEIGHTS), and a tie
#                              goes to the principle, so a lone odd edit can't
#                              flip it
#
#     FOOTNOTES_SPAN_TEMPLATE_PARAMS='{"min_support": 5}' python wsgi.py

SPAN_TEMPLATE_DEFAULTS = {
    'min_support': 3,
    'max_contradiction_share': 0.0,
    'principle_weight': DEFAULT_WEIGHTS['labels']['element_edited'],
}


def load_span_template_params(env=None):
    params = dict(SPAN_TEMPLATE_DEFAULTS)
    raw = (env if env is not None else os.environ).get('FOOTNOTES_SPAN_TEMPLATE_PARAMS')
    if not raw:
        return params
    try:
        override = json.loads(raw)
    except ValueError:
        return params
    if isinstance(override, dict):
        for key, value in override.items():
            if key in params and isinstance(value, (int, float)) and value >= 0:
                params[key] = type(params[key])(value)
    return params


SPAN_TEMPLATE = load_span_template_params()


# --------------------------------------------------------------------------
# Per-reviewer page timers: learned from how each person reviews
# --------------------------------------------------------------------------
#
# The dwell timers above (DWELL) follow the reviewer whose pages they time,
# learned from their own page approvals across books (app.py,
# _user_dwell_history) unless they set them by hand on their profile.
# Rules and their confirmations are never per reviewer: they belong to the
# book (span templates, consistency checks).
#
# Learning compares how long they stayed on each page they approved by hand
# with the standard floor for that page, separately for pages with nothing
# detected and pages with footnotes, since the two are read differently:
#
#     empty_ms                        x  median ratio on empty pages
#     base_ms, per_*, min_ms, max_ms  x  median ratio on pages with footnotes
#
# A group with fewer than min_approvals approvals keeps the standard timers.
# Every timer, learned or set by hand, stays within DWELL_BOUNDS.

DWELL_BOUNDS = {                    # (lowest, highest, slider step), ms
    'empty_ms':          (0, 3000, 100),
    'base_ms':           (0, 5000, 100),
    'per_footnote_ms':   (0, 3000, 100),
    'per_100_chars_ms':  (0, 2000, 100),
    'per_reference_ms':  (0, 2000, 100),
    'per_component_ms':  (0, 1000, 100),
    'min_ms':            (0, 5000, 100),
    'max_ms':            (10000, 60000, 500),
}
DWELL_LEARNING = {'min_approvals': 10}


def clamp_dwell_params(params):
    """Every timer within DWELL_BOUNDS and on its slider step, missing ones
    standard, and the shortest floor no longer than the longest."""
    out = {}
    for key, (lo, hi, step) in DWELL_BOUNDS.items():
        try:
            value = float(params.get(key, DWELL[key]))
        except (TypeError, ValueError):
            value = DWELL[key]
        out[key] = max(lo, min(hi, round(value / step) * step))
    if out['min_ms'] > out['max_ms']:
        out['min_ms'] = out['max_ms']
    return out


def learn_dwell_params(approvals, learning=DWELL_LEARNING):
    """approvals: (time on page in ms, page features) for each page the
    reviewer approved by hand. Returns (timers, basis): the timers learned
    where there is enough to learn from, standard elsewhere, and what they
    were learned from."""
    groups = {'empty': [], 'content': []}
    for ms, features in approvals:
        groups['content' if (features or {}).get('footnotes') else 'empty'].append(ms / dwell_floor_ms(features or {}))
    basis, factor = {}, {}
    for name, ratios in groups.items():
        basis[name] = {'approvals': len(ratios), 'needed': learning['min_approvals']}
        if len(ratios) >= learning['min_approvals']:
            factor[name] = statistics.median(ratios)
            basis[name]['ratio'] = round(factor[name], 2)
    if not factor:
        return None, basis
    empty, content = factor.get('empty', 1.0), factor.get('content', 1.0)
    learned = {key: DWELL[key] * content for key in ('base_ms', 'per_footnote_ms', 'per_100_chars_ms',
                                                     'per_reference_ms', 'per_component_ms', 'min_ms', 'max_ms')}
    learned['empty_ms'] = DWELL['empty_ms'] * empty
    return clamp_dwell_params(learned), basis
