#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web-layer auth concerns: loading the current user into flask.g, and the
route-protection decorators. Kept separate from db.py so the persistence
layer stays importable/testable without a request context beyond flask.g.
"""

from functools import wraps

from flask import g, session, request, redirect, url_for, flash, jsonify

import db


def _extract_api_key() -> str:
    """X-API-Key header, or Authorization: Bearer <key> — whichever is
    present. Shared by load_logged_in_user (resolves g.user from it) and
    enforce_csrf (exempts requests that carry one — see its docstring)."""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            api_key = auth_header[len('Bearer '):].strip()
    return api_key


def load_logged_in_user():
    """Registered as app.before_request in app.py. Resolves g.user from
    either the session cookie (browser) or an API key (Authorization: Bearer
    <key>, or X-API-Key header) — whichever is present, session taking
    priority. Every route-protection decorator below (and job/collection
    access checks) only ever looks at g.user, so a script authenticating
    with an API key is indistinguishable from a logged-in browser tab to
    the rest of the app — no other route needed to change to support it."""
    user_id = session.get('user_id')
    if user_id is not None:
        g.user = db.get_user_by_id(user_id)
        return

    api_key = _extract_api_key()
    g.user = db.get_user_by_api_key(api_key) if api_key else None


def enforce_csrf():
    """Registered as app.before_request in app.py, after load_logged_in_user.
    Called manually rather than left to flask-wtf's own automatic hook
    (app.py sets WTF_CSRF_CHECK_DEFAULT=False) specifically so it can skip
    requests authenticating via API key — see app.py's CSRFProtect wiring for
    why that's a deliberate exemption (explicit secret the caller sends on
    purpose), not a gap. csrf.protect() itself already no-ops for safe
    methods (GET/HEAD/OPTIONS), so this runs unconditionally otherwise,
    covering session-cookie-authenticated form posts and fetch() calls
    alike."""
    if _extract_api_key():
        return
    from app import csrf  # deferred: same circular-import reason as app.py's own imports of this blueprint
    csrf.protect()


def load_language():
    """Registered as app.before_request, after load_logged_in_user (needs
    g.user resolved first to know whether to prefer the account's saved
    language). Logged-in users' choice lives on the account (db.update_language)
    so it follows them to any browser/device; anonymous visitors (and the
    login/register pages themselves, before there's an account to attach to)
    get a session-only choice that defaults to English. Templates read
    g.lang directly for the switcher's active state; every other string goes
    through translations.t(), which also reads g.lang."""
    if g.get('user') is not None:
        g.lang = g.user['language'] or 'en'
    else:
        g.lang = session.get('lang', 'en')


def set_language(language: str) -> None:
    """Called by the POST /set-language route. Silently ignores anything
    other than the two supported codes rather than erroring, since a stale
    dropdown option is the only realistic way to send something else."""
    if language not in ('en', 'pl'):
        return
    if g.get('user') is not None:
        db.update_language(g.user['id'], language)
    else:
        session['lang'] = language
    g.lang = language


def load_registration_open():
    """Registered as app.before_request in app.py, alongside
    load_logged_in_user. Self-registration (/register) only ever works to
    bootstrap the very first (admin) account — once any user exists, every
    subsequent account is created by an admin via the dashboard. Templates
    read g.registration_open to decide whether to show the Register link at
    all, rather than just letting it 404/redirect if clicked."""
    g.registration_open = db.count_users() == 0


def _wants_json() -> bool:
    # Path-prefix based rather than Accept-header sniffing: this app mixes
    # plain HTML routes and /api/... JSON routes, and the JSON routes are hit
    # via fetch() from inline <script> blocks that don't reliably send an
    # Accept: application/json header.
    return request.path.startswith('/api/')


def _safe_next(target: str) -> bool:
    """Open-redirect guard: only allow same-site, non-protocol-relative paths."""
    return target.startswith('/') and not target.startswith('//')


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            if _wants_json():
                return jsonify({'error': 'Login required'}), 401
            next_path = request.full_path if request.query_string else request.path
            return redirect(url_for('auth.login', next=next_path if _safe_next(next_path) else None))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            if _wants_json():
                return jsonify({'error': 'Login required'}), 401
            return redirect(url_for('auth.login'))
        if g.user['role'] != 'admin':
            if _wants_json():
                return jsonify({'error': 'Admin access required'}), 403
            flash('Admin access required')
            return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapped


def job_access_required(view):
    """Owner, admin, or any fellow group member — used for read/edit routes.
    Reads job_id from kwargs since Flask dispatches views with keyword args
    (edit routes also carry footnote_id/reference_id/component_id)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            if _wants_json():
                return jsonify({'error': 'Login required'}), 401
            return redirect(url_for('auth.login', next=request.path if _safe_next(request.path) else None))
        job_id = kwargs.get('job_id')
        if not db.can_access_job(g.user, job_id):
            if _wants_json():
                return jsonify({'error': 'Not found'}), 404
            flash('Job not found')
            return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapped


def job_owner_or_admin_required(view):
    """Stricter than job_access_required: owner or admin only, excludes plain
    group members — for destructive/administrative job-level actions
    (delete, reshare)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            if _wants_json():
                return jsonify({'error': 'Login required'}), 401
            return redirect(url_for('auth.login', next=request.path if _safe_next(request.path) else None))
        job_id = kwargs.get('job_id')
        if g.user['role'] != 'admin':
            owner = db.get_job_owner(job_id)
            if owner is None or owner['owner_user_id'] != g.user['id']:
                if _wants_json():
                    return jsonify({'error': 'Only the job owner or an admin can do this'}), 403
                flash('Only the job owner or an admin can do this')
                return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapped


def job_editor_required(view):
    """Between job_access_required (view: owner/admin/any group member) and
    job_owner_or_admin_required (owner/admin only): owner, admin, or a group
    member whose role for that job's sharing group is 'editor' (see
    db.can_edit_job). Used for every route that mutates a job's content
    (footnote/reference/bbox CRUD, split/merge, NER/OCR suite switching) and
    for deleting a job shared via a group — group editors gaining delete
    rights is a deliberate new capability, see db.can_edit_job's docstring.
    share_job (reassigning which group a job is shared with) deliberately
    stays on job_owner_or_admin_required instead, no group-editor escalation."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            if _wants_json():
                return jsonify({'error': 'Login required'}), 401
            return redirect(url_for('auth.login', next=request.path if _safe_next(request.path) else None))
        job_id = kwargs.get('job_id')
        if not db.can_edit_job(g.user, job_id):
            if _wants_json():
                return jsonify({'error': 'You have view-only access to this job'}), 403
            flash('You have view-only access to this job')
            return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapped


def collection_access_required(view):
    """Owner, admin, or any fellow group member — mirrors job_access_required.
    Grants read access only; add/remove-job and other mutations require
    collection_edit_required (see below) since a group member's role may be
    'viewer'."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            if _wants_json():
                return jsonify({'error': 'Login required'}), 401
            return redirect(url_for('auth.login', next=request.path if _safe_next(request.path) else None))
        collection_id = kwargs.get('collection_id')
        if not db.can_access_collection(g.user, collection_id):
            if _wants_json():
                return jsonify({'error': 'Not found'}), 404
            flash('Collection not found')
            return redirect(url_for('auth.list_collections'))
        return view(*args, **kwargs)
    return wrapped


def collection_edit_required(view):
    """Mirrors job_editor_required for collections: owner, admin, or a group
    member with 'editor' role for the collection's sharing group (see
    db.can_edit_collection). Used for add_job_to_collection/
    remove_job_from_collection — previously any collection-accessible user
    could do these regardless of role."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            if _wants_json():
                return jsonify({'error': 'Login required'}), 401
            return redirect(url_for('auth.login', next=request.path if _safe_next(request.path) else None))
        collection_id = kwargs.get('collection_id')
        if not db.can_edit_collection(g.user, collection_id):
            if _wants_json():
                return jsonify({'error': 'You have view-only access to this collection'}), 403
            flash('You have view-only access to this collection')
            return redirect(url_for('auth.list_collections'))
        return view(*args, **kwargs)
    return wrapped


def collection_owner_or_admin_required(view):
    """Stricter than collection_access_required: owner or admin only, for
    rename/reshare/delete — mirrors job_owner_or_admin_required."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            if _wants_json():
                return jsonify({'error': 'Login required'}), 401
            return redirect(url_for('auth.login', next=request.path if _safe_next(request.path) else None))
        collection_id = kwargs.get('collection_id')
        if g.user['role'] != 'admin' and not db.is_collection_owner(collection_id, g.user['id']):
            if _wants_json():
                return jsonify({'error': 'Only the collection owner or an admin can do this'}), 403
            flash('Only the collection owner or an admin can do this')
            return redirect(url_for('auth.list_collections'))
        return view(*args, **kwargs)
    return wrapped
