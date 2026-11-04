#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Blueprint for account/group/admin routes: register, login, logout, groups
(create/view/manage membership), and the admin user-management panel.
"""

import sqlite3
import threading
import time

from flask import Blueprint, request, render_template, redirect, url_for, flash, session, g

import db
from auth import (login_required, admin_required, _safe_next,
                   collection_access_required, collection_edit_required, collection_owner_or_admin_required)
from translations import t

bp = Blueprint('auth', __name__)

# ---- login brute-force throttling ----
# Keyed by username (not IP): the app sits behind a Cloudflare Tunnel once
# exposed to the open internet, and the tunnel process proxies from
# localhost, so request.remote_addr is useless for this (every request
# looks like it's from 127.0.0.1) — X-Forwarded-For would carry the real
# client IP but is trivially spoofable by anyone who reaches the app
# directly (e.g. on the LAN), so it's not trustworthy as a rate-limit key
# either. Per-username throttling instead protects the thing that actually
# matters — a given account can't be brute-forced — regardless of where
# requests appear to originate.
_LOGIN_MAX_ATTEMPTS = 8
_LOGIN_WINDOW_SECONDS = 15 * 60
_login_lock = threading.Lock()
_login_failures = {}  # username.lower() -> [failure timestamps within the window]


def _login_throttled(username: str) -> bool:
    key = username.lower()
    now = time.time()
    with _login_lock:
        attempts = [t for t in _login_failures.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
        _login_failures[key] = attempts
        return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def _record_login_failure(username: str) -> None:
    key = username.lower()
    with _login_lock:
        _login_failures.setdefault(key, []).append(time.time())


def _clear_login_failures(username: str) -> None:
    key = username.lower()
    with _login_lock:
        _login_failures.pop(key, None)


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if g.get('user') is not None:
        return redirect(url_for('index'))

    # Self-registration only ever works to bootstrap the very first (admin)
    # account. Once any user exists, new accounts are admin-created only.
    if not g.get('registration_open'):
        flash(t('Self-registration is disabled. Ask an administrator to create your account.'))
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')

        if not username or not password:
            flash(t('Username and password are required'))
        elif password != confirm:
            flash(t('Passwords do not match'))
        elif len(password) < 8:
            flash(t('Password must be at least 8 characters'))
        else:
            try:
                user_id = db.create_user(username, password)
            except sqlite3.IntegrityError:
                flash(t('That username is already taken'))
            else:
                session.clear()
                session['user_id'] = user_id
                session.permanent = True
                flash(t('Account created'))
                return redirect(url_for('index'))

    return render_template('register.html')


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if g.get('user') is not None:
        return redirect(url_for('index'))

    next_path = request.values.get('next')

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if username and _login_throttled(username):
            flash(t('Too many failed attempts for this account. Try again in a few minutes.'))
        else:
            user = db.get_user_by_username(username)

            if user is None or not db.verify_password(user, password):
                if username:
                    _record_login_failure(username)
                flash(t('Invalid username or password'))
            else:
                _clear_login_failures(username)
                session.clear()
                session['user_id'] = user['id']
                session.permanent = True
                dest = request.form.get('next')
                if dest and _safe_next(dest):
                    return redirect(dest)
                return redirect(url_for('index'))

    return render_template('login.html', next=next_path)


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/groups')
@login_required
def list_groups():
    groups = db.list_groups_for_user(g.user)
    return render_template('groups.html', groups=groups)


@bp.route('/groups/create', methods=['POST'])
@login_required
def create_group():
    name = request.form.get('name', '').strip()
    if not name:
        flash(t('Group name is required'))
    else:
        group_id = db.create_group(name, g.user['id'])
        return redirect(url_for('auth.group_detail', group_id=group_id))
    return redirect(url_for('auth.list_groups'))


@bp.route('/groups/<int:group_id>')
@login_required
def group_detail(group_id):
    group = db.get_group(group_id)
    if group is None:
        flash(t('Group not found'))
        return redirect(url_for('auth.list_groups'))

    is_member = db.is_group_member(group_id, g.user['id'])
    is_admin = g.user['role'] == 'admin'
    if not is_member and not is_admin:
        flash(t('You are not a member of that group'))
        return redirect(url_for('auth.list_groups'))

    can_manage = is_admin or db.is_group_owner(group_id, g.user['id'])
    members = db.list_group_members(group_id)
    # Only collections are shareable with a group (not individual jobs) —
    # see db.can_access_job's docstring. Each collection's own page already
    # has full add/remove/manage tooling; this is just the index of what's
    # shared here.
    shared_collections = db.list_collections_shared_with_group(group_id)

    return render_template('group_detail.html', group=group, members=members,
                            can_manage=can_manage, shared_collections=shared_collections)


@bp.route('/groups/<int:group_id>/members', methods=['POST'])
@login_required
def add_group_member(group_id):
    group = db.get_group(group_id)
    if group is None:
        flash(t('Group not found'))
        return redirect(url_for('auth.list_groups'))

    can_manage = g.user['role'] == 'admin' or db.is_group_owner(group_id, g.user['id'])
    if not can_manage:
        flash(t('Only a group owner or an admin can add members'))
        return redirect(url_for('auth.group_detail', group_id=group_id))

    username = request.form.get('username', '').strip()
    role = request.form.get('role', 'editor').strip()
    if role not in ('viewer', 'editor', 'owner'):
        role = 'editor'
    user = db.get_user_by_username(username)
    if user is None:
        flash(t('No user named "{username}"', username=username))
    else:
        db.add_group_member(group_id, user['id'], role)
        flash(t('Added {username} to the group as {role}', username=user["username"], role=t(role.capitalize())))

    return redirect(url_for('auth.group_detail', group_id=group_id))


@bp.route('/groups/<int:group_id>/members/<int:user_id>/role', methods=['POST'])
@login_required
def set_group_member_role(group_id, user_id):
    group = db.get_group(group_id)
    if group is None:
        flash(t('Group not found'))
        return redirect(url_for('auth.list_groups'))

    can_manage = g.user['role'] == 'admin' or db.is_group_owner(group_id, g.user['id'])
    if not can_manage:
        flash(t('Only a group owner or an admin can change member roles'))
        return redirect(url_for('auth.group_detail', group_id=group_id))

    role = request.form.get('role', '').strip()
    if role not in ('viewer', 'editor', 'owner'):
        flash(t('Invalid role'))
    elif role != 'owner' and db.get_group_member_role(group_id, user_id) == 'owner' and db.count_group_owners(group_id) <= 1:
        flash(t("Can't demote the group's only owner — promote another member to owner first"))
    else:
        db.set_group_member_role(group_id, user_id, role)
        flash(t('Role updated'))

    return redirect(url_for('auth.group_detail', group_id=group_id))


@bp.route('/groups/<int:group_id>/members/<int:user_id>/remove', methods=['POST'])
@login_required
def remove_group_member(group_id, user_id):
    group = db.get_group(group_id)
    if group is None:
        flash(t('Group not found'))
        return redirect(url_for('auth.list_groups'))

    can_manage = g.user['role'] == 'admin' or db.is_group_owner(group_id, g.user['id'])
    if not can_manage:
        flash(t('Only a group owner or an admin can remove members'))
        return redirect(url_for('auth.group_detail', group_id=group_id))

    if db.get_group_member_role(group_id, user_id) == 'owner' and db.count_group_owners(group_id) <= 1:
        flash(t("Can't remove the group's only owner — promote another member to owner first"))
        return redirect(url_for('auth.group_detail', group_id=group_id))

    db.remove_group_member(group_id, user_id)
    flash(t('Member removed'))
    return redirect(url_for('auth.group_detail', group_id=group_id))


@bp.route('/groups/<int:group_id>/delete', methods=['POST'])
@login_required
def delete_group(group_id):
    group = db.get_group(group_id)
    if group is None:
        flash(t('Group not found'))
        return redirect(url_for('auth.list_groups'))

    can_manage = g.user['role'] == 'admin' or db.is_group_owner(group_id, g.user['id'])
    if not can_manage:
        flash(t('Only a group owner or an admin can delete this group'))
        return redirect(url_for('auth.group_detail', group_id=group_id))

    db.delete_group(group_id)
    flash(t('Group deleted'))
    return redirect(url_for('auth.list_groups'))


def _ensure_default_collection(user):
    """Every user gets one auto-created, real collections row (is_default=1)
    called "User's Uploads" — created and backfilled the first time this
    page loads for them, not at registration/upload time, so it's zero extra
    work for every request that isn't a first visit. Backfill only ever
    happens once: after creation, get_default_collection short-circuits on
    the indexed is_default lookup, so a job removed from it later is never
    silently re-added, and a job uploaded after the first visit is not
    retroactively filed either — see the plan's documented edge cases."""
    default = db.get_default_collection(user['id'])
    if default is not None:
        return default

    from app import processing_status  # deferred: see group_detail's identical import for why

    collection_id = db.create_default_collection(user['id'])
    owned_ids = {r['job_id'] for r in db.list_jobs_owned_by(user['id'])}
    already_filed = db.list_job_ids_in_any_collection_owned_by(user['id'])
    for job_id in owned_ids - already_filed:
        if job_id in processing_status:
            db.add_job_to_collection(collection_id, job_id)
    return db.get_collection(collection_id)


@bp.route('/collections')
@login_required
def list_collections():
    from app import build_job_rows

    default_collection = _ensure_default_collection(g.user)
    default_job_ids = [r['job_id'] for r in db.list_jobs_in_collection(default_collection['id'])]
    default_jobs = build_job_rows(default_job_ids)

    # "Collections" column on this listing: which OTHER collections each job
    # is also filed into — the default collection itself is excluded, since
    # every row here is trivially "in User's Uploads" already.
    memberships = db.list_collection_memberships_for_jobs(default_job_ids, g.user['id'])
    for job in default_jobs:
        job['collections'] = [c for c in memberships.get(job['job_id'], []) if c['id'] != default_collection['id']]

    all_collections = db.list_collections_for_user(g.user)
    other_collections = [c for c in all_collections if not c['is_default']]
    user_groups = db.list_groups_for_user(g.user)
    collection_counts = {c['id']: _collection_counts(c['id']) for c in other_collections}

    return render_template('collections.html',
                            default_collection=default_collection, default_jobs=default_jobs,
                            other_collections=other_collections, all_collections=all_collections,
                            user_groups=user_groups, collection_counts=collection_counts)


def _collection_counts(collection_id):
    """(publications, verified) in a collection, for the line under its name
   ; ids of deleted jobs are not counted."""
    from app import processing_status
    known = [processing_status[r['job_id']] for r in db.list_jobs_in_collection(collection_id)
             if r['job_id'] in processing_status]
    return len(known), sum(1 for st in known if st.get('verified'))


@bp.route('/collections/create', methods=['POST'])
@login_required
def create_collection():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip() or None
    if not name:
        flash(t('Collection name is required'))
        return redirect(url_for('auth.list_collections'))

    group_id = request.form.get('group_id') or None
    if group_id is not None:
        try:
            group_id = int(group_id)
        except ValueError:
            flash(t('Invalid group'))
            return redirect(url_for('auth.list_collections'))
        if g.user['role'] != 'admin' and not db.is_group_member(group_id, g.user['id']):
            flash(t('You are not a member of that group'))
            return redirect(url_for('auth.list_collections'))

    collection_id = db.create_collection(name, description, g.user['id'], group_id)
    return redirect(url_for('auth.collection_detail', collection_id=collection_id))


@bp.route('/collections/<int:collection_id>')
@collection_access_required
def collection_detail(collection_id):
    # Deferred import: build_job_rows lives in app.py, which imports this
    # blueprint at module load time — importing it back at module scope here
    # would be circular. Safe at request time since both modules are fully
    # loaded by then (see the identical pattern in group_detail above).
    from app import build_job_rows

    collection = db.get_collection(collection_id)
    can_manage = g.user['role'] == 'admin' or db.is_collection_owner(collection_id, g.user['id'])
    can_edit = db.can_edit_collection(g.user, collection_id)

    member_job_ids = {r['job_id'] for r in db.list_jobs_in_collection(collection_id)}
    jobs = build_job_rows(member_job_ids)

    user_groups = db.list_groups_for_user(g.user)
    # For the bulk "add to collection" destination picker — includes this
    # collection itself (selecting it is a harmless no-op, same as
    # re-checking a checkbox that's already checked) so it can be the
    # picker's default, rather than excluding it and defaulting to whatever
    # else happens to sort first.
    other_collections = db.list_collections_for_user(g.user)

    return render_template('collection_detail.html', collection=collection, can_manage=can_manage,
                            can_edit=can_edit, jobs=jobs,
                            user_groups=user_groups, other_collections=other_collections)


@bp.route('/collections/<int:collection_id>/edit', methods=['POST'])
@collection_owner_or_admin_required
def edit_collection(collection_id):
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip() or None
    if not name:
        flash(t('Collection name is required'))
    else:
        db.update_collection(collection_id, name, description)
        flash(t('Collection updated'))
    # The inline edit form on collections.html (the only way to reach the
    # default collection's name/description — it has no detail-page link)
    # sends return_to=list so the user lands back where they edited from.
    if request.form.get('return_to') == 'list':
        return redirect(url_for('auth.list_collections'))
    return redirect(url_for('auth.collection_detail', collection_id=collection_id))


@bp.route('/collections/<int:collection_id>/share', methods=['POST'])
@collection_owner_or_admin_required
def share_collection(collection_id):
    group_id = request.form.get('group_id') or None
    if group_id is not None:
        try:
            group_id = int(group_id)
        except ValueError:
            flash(t('Invalid group'))
            return redirect(url_for('auth.collection_detail', collection_id=collection_id))
        if g.user['role'] != 'admin' and not db.is_group_member(group_id, g.user['id']):
            flash(t('You are not a member of that group'))
            return redirect(url_for('auth.collection_detail', collection_id=collection_id))

    db.update_collection_group(collection_id, group_id)
    flash(t('Sharing updated'))
    return redirect(url_for('auth.collection_detail', collection_id=collection_id))


@bp.route('/collections/<int:collection_id>/delete', methods=['POST'])
@collection_owner_or_admin_required
def delete_collection(collection_id):
    collection = db.get_collection(collection_id)
    if collection is not None and collection['is_default']:
        flash(t("The default Uploads collection can't be deleted"))
        return redirect(url_for('auth.collection_detail', collection_id=collection_id))
    db.delete_collection(collection_id)
    flash(t('Collection deleted'))
    return redirect(url_for('auth.list_collections'))


@bp.route('/collections/<int:collection_id>/jobs/add', methods=['POST'])
@collection_edit_required
def add_job_to_collection(collection_id):
    from app import processing_status

    job_id = request.form.get('job_id', '').strip()
    if not job_id or not db.can_access_job(g.user, job_id) or job_id not in processing_status:
        flash(t('Job not found'))
    else:
        db.add_job_to_collection(collection_id, job_id)
        flash(t('Job added to collection'))
    return redirect(url_for('auth.collection_detail', collection_id=collection_id))


@bp.route('/collections/<int:collection_id>/jobs/<job_id>/remove', methods=['POST'])
@collection_edit_required
def remove_job_from_collection(collection_id, job_id):
    db.remove_job_from_collection(collection_id, job_id)
    flash(t('Job removed from collection'))
    return redirect(url_for('auth.collection_detail', collection_id=collection_id))


@bp.route('/profile')
@login_required
def profile():
    keys = db.list_api_keys_for_user(g.user['id'])
    return render_template('profile.html', keys=keys)


@bp.route('/profile/update', methods=['POST'])
@login_required
def update_profile():
    db.update_profile(
        g.user['id'],
        request.form.get('first_name', '').strip(),
        request.form.get('last_name', '').strip(),
        request.form.get('email', '').strip(),
    )
    flash(t('Profile updated'))
    return redirect(url_for('auth.profile'))


@bp.route('/api-keys')
@login_required
def list_api_keys():
    """Kept as a redirect — API keys now live on the profile page, this just
    keeps any existing bookmarks/links working."""
    return redirect(url_for('auth.profile'))


@bp.route('/account/password', methods=['POST'])
@login_required
def change_password():
    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm_new_password', '')

    if not db.verify_password(g.user, current):
        flash(t('Current password is incorrect'))
    elif new != confirm:
        flash(t('New passwords do not match'))
    elif len(new) < 8:
        flash(t('New password must be at least 8 characters'))
    else:
        db.set_password(g.user['id'], new)
        flash(t('Password changed'))

    return redirect(url_for('auth.profile'))


@bp.route('/api-keys/create', methods=['POST'])
@login_required
def create_api_key():
    name = request.form.get('name', '').strip()
    if not name:
        flash(t('Key name is required'))
        return redirect(url_for('auth.profile'))

    _key_id, raw_key = db.create_api_key(g.user['id'], name)
    # Shown exactly once: reused via the same flash mechanism as ordinary
    # messages (category 'api-key' so the template can render it distinctly,
    # in a copy-this-now box, instead of as a plain dismissible alert).
    flash(raw_key, 'api-key')
    return redirect(url_for('auth.profile'))


@bp.route('/api-keys/<int:key_id>/revoke', methods=['POST'])
@login_required
def revoke_api_key(key_id):
    db.revoke_api_key(key_id, g.user['id'])
    flash(t('API key revoked'))
    return redirect(url_for('auth.profile'))


@bp.route('/admin/users')
@admin_required
def admin_users():
    from features import FEATURES
    users = db.list_users()
    overrides = db.list_user_features()
    user_features = {u['id']: {name: overrides.get(u['id'], {}).get(name, spec['default'])
                               for name, spec in FEATURES.items()} for u in users}
    return render_template('admin_users.html', users=users, user_features=user_features)


@bp.route('/admin/users/<int:user_id>/features', methods=['POST'])
@admin_required
def admin_set_user_feature(user_id):
    """One per-user feature on or off (features.py). The switch posts '1'
    when checked and the hidden fallback '0' otherwise, as on Admin ->
    Models."""
    from features import FEATURES
    feature = request.form.get('feature', '')
    if feature not in FEATURES or db.get_user_by_id(user_id) is None:
        flash(t('Invalid feature'))
        return redirect(url_for('auth.admin_users'))
    enabled = request.form.get('enabled') == '1'
    db.set_user_feature(user_id, feature, enabled, admin_user_id=g.user['id'])
    user = db.get_user_by_id(user_id)
    flash(t('{feature} for {name}: {state}', feature=t(FEATURES[feature]['label']), name=user['username'],
            state=t('on') if enabled else t('off')))
    return redirect(url_for('auth.admin_users'))


@bp.route('/admin/users/create', methods=['POST'])
@admin_required
def admin_create_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    confirm = request.form.get('confirm', '')
    role = request.form.get('role', 'user')

    if role not in ('user', 'admin'):
        flash(t('Invalid role'))
    elif not username or not password:
        flash(t('Username and password are required'))
    elif password != confirm:
        flash(t('Passwords do not match'))
    elif len(password) < 8:
        flash(t('Password must be at least 8 characters'))
    else:
        try:
            db.admin_create_user(username, password, role)
        except sqlite3.IntegrityError:
            flash(t('That username is already taken'))
        else:
            flash(t('Account "{username}" created', username=username))

    return redirect(url_for('auth.admin_users'))


@bp.route('/admin/users/<int:user_id>/role', methods=['POST'])
@admin_required
def admin_set_role(user_id):
    role = request.form.get('role')
    if role not in ('user', 'admin'):
        flash(t('Invalid role'))
        return redirect(url_for('auth.admin_users'))

    if role == 'user' and user_id == g.user['id'] and db.count_admins() <= 1:
        flash(t('You are the last admin — promote someone else before demoting yourself'))
        return redirect(url_for('auth.admin_users'))

    target = db.get_user_by_id(user_id)
    if target is not None and target['role'] == 'admin' and role == 'user' and db.count_admins() <= 1:
        flash(t('Cannot demote the last remaining admin'))
        return redirect(url_for('auth.admin_users'))

    db.set_user_role(user_id, role)
    flash(t('Role updated'))
    return redirect(url_for('auth.admin_users'))


@bp.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    if user_id == g.user['id']:
        flash(t('You cannot delete your own account'))
        return redirect(url_for('auth.admin_users'))

    target = db.get_user_by_id(user_id)
    if target is not None and target['role'] == 'admin' and db.count_admins() <= 1:
        flash(t('Cannot delete the last remaining admin'))
        return redirect(url_for('auth.admin_users'))

    try:
        db.delete_user(user_id)
        flash(t('User deleted'))
    except sqlite3.IntegrityError:
        flash(t('Cannot delete this user — they still own jobs or created groups'))

    return redirect(url_for('auth.admin_users'))


@bp.route('/admin/models')
@admin_required
def admin_models():
    # Deferred import: get_available_models/MODEL_TYPES live in app.py,
    # which imports this blueprint at module load time — a top-level import
    # here would be circular. Same pattern as group_detail's processing_status
    # import above.
    from app import get_available_models, model_instances_overview
    od_models, ner_suites, ocr_engines = get_available_models()
    return render_template('admin_models.html', od_models=od_models, ner_suites=ner_suites, ocr_engines=ocr_engines,
                           pool=model_instances_overview())


@bp.route('/admin/models/toggle', methods=['POST'])
@admin_required
def admin_toggle_model():
    from app import MODEL_TYPES

    model_type = request.form.get('model_type', '')
    model_key = request.form.get('model_key', '').strip()
    # The switch in admin_models.html represents "enabled" (the natural on
    # = active reading for a toggle next to a model's name) — '1' if
    # checked, '0' from a fallback hidden field if not, so this always has
    # a value either way. Storage is the inverse (disabled_models is a
    # deny-list, see db.py), so it's negated right here at the boundary.
    enabled = request.form.get('enabled') == '1'

    if model_type not in MODEL_TYPES or not model_key:
        flash(t('Invalid model'))
        return redirect(url_for('auth.admin_models'))

    db.set_model_disabled(model_type, model_key, disabled=not enabled, admin_user_id=g.user['id'])
    if not enabled:                      # give its GPU memory back
        import app as app_module
        app_module.unload_model(model_type, model_key)
    flash(f"{model_key} " + (t('enabled') if enabled else t('disabled')))
    return redirect(url_for('auth.admin_models'))


@bp.route('/admin/models/instances', methods=['POST'])
@admin_required
def admin_model_instances():
    """How many loaded copies of a model the app keeps, so that many documents
    can use it at once; jobs run in parallel up to the highest setting."""
    import app as app_module

    model_type = request.form.get('model_type', '')
    model_key = request.form.get('model_key', '').strip()
    try:
        instances = int(request.form.get('instances', ''))
    except ValueError:
        instances = 0
    if model_type not in app_module.MODEL_TYPES or not model_key or not 1 <= instances <= app_module.MAX_MODEL_INSTANCES:
        flash(t('Invalid model'))
        return redirect(url_for('auth.admin_models'))

    db.set_model_instances(model_type, model_key, instances, admin_user_id=g.user['id'])
    app_module.MODEL_INSTANCES.clear()
    app_module.MODEL_INSTANCES.update(db.list_model_instances())
    app_module.apply_model_instances()
    flash(t('{key}: {n} instance(s)').format(key=model_key, n=instances))
    return redirect(url_for('auth.admin_models'))
