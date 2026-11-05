#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON API blueprint providing full parity with the browser UI, for scripts
authenticating with an API key (see auth.load_logged_in_user) instead of a
session cookie: document upload/listing, and groups/collections management.

Per-job footnote/reference/component/name CRUD and the single-job
status/results endpoints already live directly on `app` under
/api/v1/documents/<job_id>/... and /api/status,/api/results — those aren't
duplicated here, they already work with an API key with zero changes, since
every decorator in auth.py only ever looks at flask.g.user.

Deferred imports of app.py internals (processing_status, _process_upload,
etc.) happen inside each view function rather than at module scope, since
app.py imports this module to register the blueprint — a top-level import
here would be circular. Same pattern already used by auth_routes.py's
group_detail/collection_detail views.
"""

from flask import Blueprint, request, jsonify, g

import db
from auth import (login_required, collection_access_required, collection_edit_required, collection_owner_or_admin_required)

api_bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')


def _job_summary(job_id, status, processing_results):
    filename = status.get('original_filename')
    if not filename:
        filename = processing_results.get(job_id, {}).get('metadata', {}).get('input_file', 'Unknown')
    return {
        'job_id': job_id,
        'filename': filename,
        'status': status.get('status'),
        'step': status.get('step'),
        'progress': status.get('progress'),
        'summary': status.get('summary'),
        'error': status.get('error'),
        'owner_info': db.describe_job_owner(job_id),
    }


# ---- documents (upload / list) ----

@api_bp.route('/documents', methods=['POST'])
@login_required
def api_upload_documents():
    """Multipart upload — same processing options as the HTML form. Accepts
    either a single 'file' field or a repeated 'files' field (matching the
    browser form's field name), so a simple `curl -F file=@paper.pdf` works."""
    from app import (_process_upload, _upload_limit_error, _validate_model_selection, _limit_upload_options, _resolve_grobid_url, allowed_file, BOOK_METADATA_FIELDS,
                     DEFAULT_OD_MODEL, DEFAULT_NER_SUITE)

    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        files = [f for f in request.files.getlist('file') if f and f.filename]
    if not files:
        return jsonify({'error': 'No file provided (multipart field "file" or "files")'}), 400
    if not all(allowed_file(f.filename) for f in files):
        return jsonify({'error': 'Only PDF files are accepted'}), 400
    limit_error = _upload_limit_error(files)
    if limit_error:
        return jsonify({'error': limit_error}), 413

    form = request.form

    def _flag(name):
        return form.get(name, 'false').strip().lower() in ('1', 'true', 'yes', 'on')

    def _flag_default_true(name):
        return form.get(name, 'true').strip().lower() not in ('0', 'false', 'no', 'off')

    options = {
        'debug': _flag('debug'),
        'cpu_mode': _flag('cpu_mode'),
        'od_conf': float(form.get('od_conf', 0.5)),
        'ref_conf': float(form.get('ref_conf', 0.7)),
        'comp_conf': float(form.get('comp_conf', 0.7)),
        'batch_size': int(form.get('batch_size', 8)),
        'od_model': form.get('od_model', DEFAULT_OD_MODEL),
        'ner_suite': form.get('ner_suite', DEFAULT_NER_SUITE),
        'enable_grobid': _flag('enable_grobid'),
        'enable_enrichment': _flag('enable_enrichment'),
        'grobid_url': _resolve_grobid_url(g.user, form.get('grobid_url')),
        'use_surya_ocr': form.get('ocr_engine', 'surya') == 'surya',
        'split_footnotes': _flag_default_true('split_footnotes'),
        'enable_ibidem': _flag_default_true('enable_ibidem'),
        'split_multi_work': _flag_default_true('split_multi_work'),
    }
    _limit_upload_options(options)

    model_error = _validate_model_selection(options)
    if model_error:
        return jsonify({'error': model_error}), 400

    group_id_raw = (form.get('group_id') or '').strip()
    try:
        group_id = int(group_id_raw) if group_id_raw else None
    except ValueError:
        return jsonify({'error': 'group_id must be an integer'}), 400
    if group_id is not None and g.user['role'] != 'admin' and not db.is_group_member(group_id, g.user['id']):
        return jsonify({'error': 'You are not a member of that group'}), 403

    collection_id_raw = (form.get('collection_id') or '').strip()
    try:
        collection_id = int(collection_id_raw) if collection_id_raw else None
    except ValueError:
        return jsonify({'error': 'collection_id must be an integer'}), 400
    if collection_id is not None and not db.can_access_collection(g.user, collection_id):
        return jsonify({'error': 'Collection not found'}), 404

    # Optional book metadata (authors/title/year/publisher/license) — all
    # optional, shared across every file in this upload batch. See
    # BOOK_METADATA_FIELDS/_clean_book_metadata in app.py.
    book_metadata = {field: form.get(field, '') for field in BOOK_METADATA_FIELDS}

    job_ids = _process_upload(files, options, group_id, collection_id, g.user, book_metadata)
    if not job_ids:
        return jsonify({'error': 'No valid PDF files provided'}), 400
    return jsonify({'job_ids': job_ids, 'job_id': job_ids[0]}), 201


@api_bp.route('/documents', methods=['GET'])
@login_required
def api_list_documents():
    """Jobs visible to the authenticated user — owned by them, shared via a
    group they belong to, or every job if they're an admin. Mirrors
    history()'s visibility logic, as JSON."""
    from app import processing_status, processing_results

    visible_ids = db.list_jobs_visible_to(g.user, processing_status.keys())
    documents = [
        _job_summary(job_id, status, processing_results)
        for job_id, status in processing_status.items()
        if job_id in visible_ids
    ]
    return jsonify({'documents': documents})


@api_bp.route('/documents/<job_id>', methods=['GET'])
@login_required
def api_get_document(job_id):
    """Single job's status/summary — a JSON-API-shaped alias of the existing
    /api/status/<job_id>, kept under the /api/v1 prefix for discoverability."""
    from app import processing_status, processing_results

    if not db.can_access_job(g.user, job_id) or job_id not in processing_status:
        return jsonify({'error': 'Document not found'}), 404
    return jsonify(_job_summary(job_id, processing_status[job_id], processing_results))


# ---- groups ----

@api_bp.route('/groups', methods=['GET'])
@login_required
def api_list_groups():
    groups = db.list_groups_for_user(g.user)
    return jsonify({'groups': [dict(row) for row in groups]})


@api_bp.route('/groups', methods=['POST'])
@login_required
def api_create_group():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    group_id = db.create_group(name, g.user['id'])
    return jsonify(dict(db.get_group(group_id))), 201


@api_bp.route('/groups/<int:group_id>', methods=['GET'])
@login_required
def api_get_group(group_id):
    group = db.get_group(group_id)
    if group is None or not (g.user['role'] == 'admin' or db.is_group_member(group_id, g.user['id'])):
        return jsonify({'error': 'Group not found'}), 404
    members = db.list_group_members(group_id)
    return jsonify({'group': dict(group), 'members': [dict(m) for m in members]})


@api_bp.route('/groups/<int:group_id>/members', methods=['POST'])
@login_required
def api_add_group_member(group_id):
    group = db.get_group(group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    can_manage = g.user['role'] == 'admin' or db.is_group_owner(group_id, g.user['id'])
    if not can_manage:
        return jsonify({'error': 'Only a group owner or an admin can add members'}), 403

    payload = request.get_json(silent=True) or request.form
    username = (payload.get('username') or '').strip()
    role = (payload.get('role') or 'editor').strip()
    if role not in ('viewer', 'editor', 'owner'):
        role = 'editor'
    user = db.get_user_by_username(username)
    if user is None:
        return jsonify({'error': f'No user named "{username}"'}), 404
    db.add_group_member(group_id, user['id'], role)
    return jsonify({'success': True})


@api_bp.route('/groups/<int:group_id>/members/<int:user_id>', methods=['DELETE'])
@login_required
def api_remove_group_member(group_id, user_id):
    group = db.get_group(group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    can_manage = g.user['role'] == 'admin' or db.is_group_owner(group_id, g.user['id'])
    if not can_manage:
        return jsonify({'error': 'Only a group owner or an admin can remove members'}), 403
    if db.get_group_member_role(group_id, user_id) == 'owner' and db.count_group_owners(group_id) <= 1:
        return jsonify({'error': "Can't remove the group's only owner — promote another member to owner first"}), 422
    db.remove_group_member(group_id, user_id)
    return jsonify({'success': True})


@api_bp.route('/groups/<int:group_id>/members/<int:user_id>', methods=['PATCH'])
@login_required
def api_set_group_member_role(group_id, user_id):
    group = db.get_group(group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    can_manage = g.user['role'] == 'admin' or db.is_group_owner(group_id, g.user['id'])
    if not can_manage:
        return jsonify({'error': 'Only a group owner or an admin can change member roles'}), 403

    payload = request.get_json(silent=True) or {}
    role = (payload.get('role') or '').strip()
    if role not in ('viewer', 'editor', 'owner'):
        return jsonify({'error': "role must be 'viewer', 'editor', or 'owner'"}), 400
    if role != 'owner' and db.get_group_member_role(group_id, user_id) == 'owner' and db.count_group_owners(group_id) <= 1:
        return jsonify({'error': "Can't demote the group's only owner — promote another member to owner first"}), 422
    db.set_group_member_role(group_id, user_id, role)
    return jsonify({'success': True})


@api_bp.route('/groups/<int:group_id>', methods=['DELETE'])
@login_required
def api_delete_group(group_id):
    group = db.get_group(group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    can_manage = g.user['role'] == 'admin' or db.is_group_owner(group_id, g.user['id'])
    if not can_manage:
        return jsonify({'error': 'Only a group owner or an admin can delete this group'}), 403
    db.delete_group(group_id)
    return jsonify({'success': True})


# ---- collections ----

@api_bp.route('/collections', methods=['GET'])
@login_required
def api_list_collections():
    collections = db.list_collections_for_user(g.user)
    return jsonify({'collections': [dict(c) for c in collections]})


@api_bp.route('/collections', methods=['POST'])
@login_required
def api_create_collection():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    description = (payload.get('description') or '').strip() or None

    group_id = payload.get('group_id')
    if group_id is not None and group_id != '':
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'group_id must be an integer'}), 400
        if g.user['role'] != 'admin' and not db.is_group_member(group_id, g.user['id']):
            return jsonify({'error': 'You are not a member of that group'}), 403
    else:
        group_id = None

    collection_id = db.create_collection(name, description, g.user['id'], group_id)
    return jsonify(dict(db.get_collection(collection_id))), 201


@api_bp.route('/collections/<int:collection_id>', methods=['GET'])
@collection_access_required
def api_get_collection(collection_id):
    collection = db.get_collection(collection_id)
    job_ids = [r['job_id'] for r in db.list_jobs_in_collection(collection_id)]
    return jsonify({'collection': dict(collection), 'job_ids': job_ids})


@api_bp.route('/collections/<int:collection_id>', methods=['PATCH'])
@collection_owner_or_admin_required
def api_update_collection(collection_id):
    collection = db.get_collection(collection_id)
    payload = request.get_json(silent=True) or request.form
    name = (payload.get('name', collection['name']) or '').strip()
    if not name:
        return jsonify({'error': 'name cannot be empty'}), 400
    description = payload.get('description', collection['description'])
    db.update_collection(collection_id, name, description)
    return jsonify(dict(db.get_collection(collection_id)))


@api_bp.route('/collections/<int:collection_id>/share', methods=['POST'])
@collection_owner_or_admin_required
def api_share_collection(collection_id):
    payload = request.get_json(silent=True) or request.form
    group_id = payload.get('group_id')
    if group_id is not None and group_id != '':
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'group_id must be an integer'}), 400
        if g.user['role'] != 'admin' and not db.is_group_member(group_id, g.user['id']):
            return jsonify({'error': 'You are not a member of that group'}), 403
    else:
        group_id = None
    db.update_collection_group(collection_id, group_id)
    return jsonify({'success': True, 'group_id': group_id})


@api_bp.route('/collections/<int:collection_id>', methods=['DELETE'])
@collection_owner_or_admin_required
def api_delete_collection(collection_id):
    collection = db.get_collection(collection_id)
    if collection is not None and collection['is_default']:
        return jsonify({'error': "The default Uploads collection can't be deleted"}), 400
    db.delete_collection(collection_id)
    return jsonify({'success': True})


@api_bp.route('/collections/<int:collection_id>/jobs', methods=['POST'])
@collection_edit_required
def api_add_job_to_collection(collection_id):
    payload = request.get_json(silent=True) or request.form
    job_id = (payload.get('job_id') or '').strip()
    if not job_id or not db.can_access_job(g.user, job_id):
        return jsonify({'error': 'Job not found'}), 404
    db.add_job_to_collection(collection_id, job_id)
    return jsonify({'success': True})


@api_bp.route('/collections/<int:collection_id>/jobs/<job_id>', methods=['DELETE'])
@collection_edit_required
def api_remove_job_from_collection(collection_id, job_id):
    db.remove_job_from_collection(collection_id, job_id)
    return jsonify({'success': True})
