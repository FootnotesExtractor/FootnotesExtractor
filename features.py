#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Per-user features: the tools an admin can switch on or off for each user
(Admin -> Users). A feature that is off is hidden completely - every element
it owns - so a user only sees what they work with; its endpoints refuse.

Defaults are what everyone had before features existed ("today's view"); a row in db.user_features overrides one for one user.
"""

from functools import wraps

from flask import g, jsonify, request, redirect, url_for, flash

import db
from translations import t

# Order is the column order on Admin -> Users.
FEATURES = {
    'layout_assist': {
        'label': 'Layout Assist',
        'description': 'Layout suggestions, consistency checks and page approvals in the viewer, and the page '
                       'timers on the profile. When off it still learns from the user\'s work, but shows nothing '
                       'and changes nothing on its own.',
        'default': True,
    },
    'show_references': {
        'label': 'Show References',
        'description': 'The reference list found inside a book: the option to look for it at upload, and the '
                       'Reference Page Predictions box and outlines in the viewer.',
        # Off for everyone for now: GROBID, today's engine for
        # it, gives unusable output. Comes back with a better engine.
        'default': False,
    },
    'reference_linker': {
        'label': 'Reference List Linker',
        'description': 'The book\'s reference list (its own or one given as BibTeX) linked to the references in '
                       'its footnotes, with links to confirm or reject in the viewer.',
        'default': False,
    },
    'review_flags': {
        'label': 'To Review',
        'description': 'A small button in the viewer that marks a footnote reference, or its link to the reference '
                       'list, "TO REVIEW", to come back to it later; the marked items are counted and can be stepped '
                       'through.',
        'default': False,
    },
    'metadata_enrichment': {
        'label': 'Metadata Enrichment',
        'description': 'Looking references up in external catalogues (in development).',
        'default': False,
    },
    'advanced_options': {
        'label': 'Advanced Options',
        'description': 'Choosing models, thresholds and processing options at upload and in the viewer. When off, '
                       'uploads - also through the API - use the defaults.',
        'default': True,
    },
}


def features_for(user) -> dict:
    """{feature: on?} for one user: the defaults, overridden by their rows."""
    out = {name: spec['default'] for name, spec in FEATURES.items()}
    if user is not None:
        out.update({k: v for k, v in db.get_user_features(user['id']).items() if k in FEATURES})
    return out


def load_features():
    """Registered as app.before_request, after load_logged_in_user."""
    g.features = features_for(g.get('user'))


def has_feature(name: str) -> bool:
    features = g.get('features')
    if features is None:                  # outside a request
        return FEATURES[name]['default']
    return bool(features.get(name))


def feature_required(name: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not has_feature(name):
                message = t('{feature} is not enabled for your account', feature=t(FEATURES[name]['label']))
                if request.path.startswith('/api/'):
                    return jsonify({'error': message}), 403
                flash(message)
                return redirect(url_for('index'))
            return view(*args, **kwargs)
        return wrapped
    return decorator
