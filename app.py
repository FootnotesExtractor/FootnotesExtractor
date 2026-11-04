#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FootnotesExtractor web application

A Flask-based web interface for the Enhanced PDF to Structured Footnotes Pipeline.
Provides an easy-to-use interface for uploading PDFs and downloading structured JSONs.

NEW FEATURES in v2.0:
- Enhanced ibidem and opus citatum detection
- Author-based matching for opus citatum references
- Individual reference citation type classification
- Improved component inference logic
- Better academic citation processing

Features:
- Web-based PDF upload interface
- Real-time processing status updates
- Enhanced citation type classification (ibidem/opus citatum/normal)
- Author-based component inference
- JSON results download
- Processing history
- Error handling and user feedback
- Mobile-responsive design
"""

import os
import sys
import copy
import json
import re
import time
import uuid
import secrets
import logging
import threading
import queue
import concurrent.futures
import difflib

# Run as `python app.py` and this module is `__main__`; api_routes/auth_routes
# then do `from app import processing_status, ...`, which Python treats as a
# *different* module and re-executes this file top to bottom. That produced two
# independent copies of processing_status/processing_results in one process —
# routes defined here saw one, blueprint routes saw the other, and both wrote
# the same history.json. Symptoms: /history and /api/v1/documents disagreeing on
# job counts, API-uploaded jobs missing from history, deleted jobs reappearing
# (the same class of bug the use_reloader=False comment at the bottom describes,
# reached by a different route). Aliasing 'app' to this module makes that import
# resolve to the already-loaded module instead of re-executing the file.
# No-op when something imports us as 'app' — sys.modules['app'] already exists.
sys.modules.setdefault('app', sys.modules[__name__])
import contextlib
import shutil
import statistics
from pathlib import Path
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from flask import Flask, request, render_template, jsonify, send_file, send_from_directory, redirect, url_for, flash, session, g, Response, has_request_context
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

# Import the enhanced pipeline
from pipeline.pdf_to_structured_footnotes import EnhancedPDFToStructuredFootnotesPipeline, split_region_text, OD_MODELS
from pipeline.pdf_to_structured_footnotes import _OPEN_ENDING
from pipeline.model_pool import MODEL_LOAD_LOCK, InstancePool
from pipeline import layout_prior
from pipeline import review_signals
from pipeline import consistency_checks
from pipeline import reference_split
from pipeline import span_templates
from pipeline import ner_prior
from pipeline.ocr_service import OCREngine, open_pdf
from pipeline.ner_service import NEREngine, classify_citation_type_standalone
from pipeline.bibtex_export import generate_bibtex
from pipeline import reference_linker
from pipeline import reference_inference
from pipeline.citation_styles import generate_citation_list, generate_reference_strings

import db
import auth
import features
from features import FEATURES, has_feature, feature_required
import link_counts
from auth_routes import bp as auth_bp
from api_routes import api_bp
from auth import login_required, admin_required, job_access_required, job_owner_or_admin_required, job_editor_required
from translations import t


def _load_or_create_secret_key():
    """A real secret is required now that the session carries login state
    (previously it only held an anonymous activity-log id, so the old
    hardcoded placeholder was low-risk). Prefers an explicit env var;
    otherwise persists a randomly generated key to a local file so sessions
    survive process restarts without requiring the operator to set one."""
    env_key = os.environ.get('FLASK_SECRET_KEY')
    if env_key:
        return env_key
    key_path = Path(__file__).parent / '.secret_key'
    if key_path.exists():
        return key_path.read_text(encoding='utf-8').strip()
    new_key = secrets.token_hex(32)
    # Owner-only from creation — whatever the process umask is, this file
    # forges session cookies for anyone if another local account can read
    # it. os.open with mode=0o600 (not write_text + a separate chmod after)
    # so there's no window where a more permissive default mode is briefly
    # on disk.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(new_key)
    return new_key


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
app.config['MAX_CONTENT_LENGTH'] = 900 * 1024 * 1024  # 900MB max file size

# Session cookie hardening — this app is only ever served over HTTPS (Caddy
# terminates TLS; wsgi.py binds 127.0.0.1, see deploy/DEPLOY.md), so there's
# no legitimate plain-HTTP case SESSION_COOKIE_SECURE would break. 'Lax'
# (not 'Strict') so following a link into the app from elsewhere (e.g. an
# emailed link) still arrives logged in — it only blocks the cookie being
# sent on cross-site POST/fetch, which is what actually matters here.
#
# The one case that assumption doesn't cover is running it locally over plain
# http:// to try something out — the cookie is then never sent back, so every
# login silently bounces to the login page with no error shown. Set
# FOOTNOTES_INSECURE_COOKIES=1 for that, and only that; it must never be set
# on anything reachable from a network, which is why it defaults to secure and
# has to be asked for explicitly rather than inferred from debug/host settings.
_insecure_cookies = os.environ.get('FOOTNOTES_INSECURE_COOKIES') == '1'
app.config['SESSION_COOKIE_SECURE'] = not _insecure_cookies
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if _insecure_cookies:
    print('WARNING: FOOTNOTES_INSECURE_COOKIES=1 — session cookies are not marked '
          'Secure. Local development only; never use this on a served instance.')

# CSRF protection — WTF_CSRF_CHECK_DEFAULT=False turns off flask-wtf's own
# automatic before_request enforcement; auth.enforce_csrf (registered below)
# calls csrf.protect() itself, but only for requests *not* authenticating via
# API key. API keys are an explicit secret the caller must know and send on
# purpose (header, never an ambient cookie a third-party site could ride
# along on) — they were never the thing CSRF protects against, so exempting
# them isn't a hole, it's the same reasoning GitHub/Stripe/etc. use for
# bearer-token API auth. Session-cookie-authenticated requests (the browser
# UI, including its own fetch() calls — see _nav.html's fetch wrapper and
# auto-injected hidden form field) are always checked.
app.config['WTF_CSRF_CHECK_DEFAULT'] = False
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def _handle_csrf_error(e):
    """Mirrors the _wants_json()-based error shape every other auth failure
    in this app already uses (see auth.py's decorators) rather than letting
    flask-wtf's default plain-text 400 through, which the frontend's fetch()
    error handling doesn't expect."""
    if request.path.startswith('/api/'):
        return jsonify({'error': 'CSRF validation failed — refresh the page and try again'}), 400
    flash(t('Your session security token expired — please try again'))
    return redirect(request.referrer or url_for('index'))


# Auth/DB wiring — module-level (not inside `if __name__ == '__main__':`)
# since anything importing `app` and calling `.run()` directly bypasses that
# block entirely.
db.init_db()
db.init_app(app)
app.before_request(auth.load_logged_in_user)
app.before_request(features.load_features)
app.before_request(auth.enforce_csrf)
app.before_request(auth.load_language)
app.before_request(auth.load_registration_open)
app.register_blueprint(auth_bp)
app.register_blueprint(api_bp)


# %b/%p in strftime always render in English regardless of g.lang — Python's
# C-locale default, and relying on the OS having a Polish locale installed
# (locale.setlocale) is fragile across platforms (this app runs on Windows
# dev machines and Linux servers alike). Formatting the Polish case by hand
# sidesteps that entirely, and also switches to the day-month-year order and
# 24-hour clock that's the actual Polish convention rather than just
# translating English month names in English's own m/d/y-with-AM/PM shape.
_MONTHS_ABBR_PL = ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze',
                    'lip', 'sie', 'wrz', 'paź', 'lis', 'gru']


def _human_date(value):
    """Jinja filter — renders any stored timestamp as a human-readable date,
    for display only. Every place a timestamp is actually *stored* (job
    history, the activity log, the DB's added_at/created_at/etc. columns)
    keeps writing full ISO-8601 exactly as before; this only changes how
    templates show it. Accepts either a live datetime object (e.g.
    build_job_rows' job['timestamp']) or an ISO string (e.g. group_members'
    added_at, api_keys' created_at/last_used_at) since both appear in
    templates depending on the field."""
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value  # not a parseable timestamp — show as-is rather than erroring
    try:
        lang = g.lang
    except Exception:
        lang = 'en'
    if lang == 'pl':
        return f"{value.day} {_MONTHS_ABBR_PL[value.month - 1]} {value.year}, {value.strftime('%H:%M')}"
    return value.strftime('%b %d, %Y, %I:%M %p')


app.jinja_env.filters['human_date'] = _human_date
app.jinja_env.globals['t'] = t
# {% if feature('layout_assist') %} - the per-user features (features.py)
app.jinja_env.globals['feature'] = has_feature
app.jinja_env.globals['FEATURES'] = FEATURES
# Single source of truth for label colours, shared by every surface that shows
# a span: the interactive viewer (injected into its JS), the results page, and
# anything added later. It previously lived in two places that had drifted
# completely apart — results.html coloured Author blue where the viewer used
# green, and most of its rules were dead anyway because it keys off
# label.lower() ("PublicationYear" -> component-publicationyear) while only a
# handful of short names had rules.
#
# bg is the chip/highlight fill, fg the text on it. Keys are lowercased labels;
# COMPONENT_COLOR_FALLBACK covers anything the models emit that isn't listed
# (e.g. FootnoteMarker, which is in the model's vocabulary but not the curated
# set) so an unknown label still renders legibly.
COMPONENT_COLORS = {
    'author':                {'bg': '#d1e7dd', 'fg': '#0f5132'},
    'title':                 {'bg': '#cfe2ff', 'fg': '#084298'},
    'journal':               {'bg': '#e0cffc', 'fg': '#3f0071'},
    'booktitle':             {'bg': '#f3e8ff', 'fg': '#6c3483'},
    'publicationyear':       {'bg': '#fff3cd', 'fg': '#664d03'},
    'publicationyearsuffix': {'bg': '#fff3cd', 'fg': '#664d03'},
    'publisher':             {'bg': '#f6c6ea', 'fg': '#6d2177'},
    'publicationplace':      {'bg': '#b8f2e6', 'fg': '#1b4d3e'},
    'volume':                {'bg': '#d1cfe2', 'fg': '#3d2c8d'},
    'issue':                 {'bg': '#e2f0cb', 'fg': '#2d6a4f'},
    'pages':                 {'bg': '#ffd5c8', 'fg': '#8c2f0d'},
    'editor':                {'bg': '#f7d6e0', 'fg': '#7c2f4b'},
    'translator':            {'bg': '#ffe0b2', 'fg': '#e65100'},
    'doi':                   {'bg': '#dcedc8', 'fg': '#558b2f'},
    'webpage':               {'bg': '#e0f7fa', 'fg': '#006064'},
    'ibidem':                {'bg': '#ffe5b4', 'fg': '#a66f00'},
    'opuscitatum':           {'bg': '#ffe5b4', 'fg': '#a66f00'},
    'firstname':             {'bg': '#f8bbd0', 'fg': '#ad1457'},
    'lastname':              {'bg': '#bbdefb', 'fg': '#0d47a1'},
    'reference':             {'bg': '#e3f2fd', 'fg': '#1565c0'},
    'work':                  {'bg': '#e9ecef', 'fg': '#343a40'},
}
COMPONENT_COLOR_FALLBACK = {'bg': '#eee', 'fg': '#000'}


app.jinja_env.globals['LANG_NAMES'] = {'en': 'English', 'pl': 'Polski'}
app.jinja_env.globals['COMPONENT_COLORS'] = COMPONENT_COLORS
app.jinja_env.globals['COMPONENT_COLOR_FALLBACK'] = COMPONENT_COLOR_FALLBACK

# Configuration
UPLOAD_FOLDER = Path(__file__).parent / 'uploads'
# FOOTNOTES_RESULTS_DIR points a test copy of the app elsewhere before
# anything runs: importing this module loads the history and recovers stuck
# jobs (_recover_stuck_jobs), which writes it - patching RESULTS_FOLDER after
# the import is too late and touches the live history.
RESULTS_FOLDER = Path(os.environ.get('FOOTNOTES_RESULTS_DIR') or Path(__file__).parent / 'results')
HISTORY_FILE = RESULTS_FOLDER / 'history.json'
ALLOWED_EXTENSIONS = {'pdf'}

# GROBID configuration – can be overridden via GROBID_URL env var
GROBID_URL = os.environ.get('GROBID_URL', 'http://localhost:8070')


def _resolve_grobid_url(user, requested: str) -> str:
    """grobid_url is a request field, not just a server setting — GrobidClient
    (pipeline/grobid_client.py) makes a real server-side HTTP POST to
    {grobid_url}/api/processFulltextDocument with the uploaded file, and
    surfaces up to 500 chars of a non-200 response back into the job's error
    message. Letting any logged-in user point that at an arbitrary URL is a
    textbook SSRF (internal service probing/fingerprinting via this app,
    regardless of what's actually reachable from outside it) — so only an
    admin's requested override is honored; everyone else silently gets the
    server's own configured GROBID_URL no matter what they send."""
    if requested and user is not None and user['role'] == 'admin':
        return requested
    return GROBID_URL

logger = logging.getLogger(__name__)

# Ensure directories exist. mode=0o700 (not the default 0o777-minus-umask)
# since these hold every user's uploaded PDFs and extracted results —
# other local accounts on the same machine have no business reading them.
# Only takes effect on first creation; exist_ok=True is a no-op on an
# already-existing directory's permissions, so this doesn't fight an
# operator who's deliberately loosened it after the fact.
UPLOAD_FOLDER.mkdir(mode=0o700, exist_ok=True)
RESULTS_FOLDER.mkdir(mode=0o700, exist_ok=True)

# Global variables for tracking processing status
processing_status = {}
processing_results = {}
# Per-job OD proposal sidecars (results/<job_id>_proposals.json), mirroring
# processing_results' lazy-load-from-disk pattern. Kept out of the results dict
# so nothing here can leak into the download/export routes.
job_proposals = {}

# TODO: add an admin panel showing which OD/NER/OCR models are currently
# loaded in memory vs. available on disk.

# Human-in-the-loop capture: recording what reviewers confirm, correct, reject
# and merely look at, and feeding it back into the layout prior. Off unless
# asked for, because it is still being developed and because it writes a
# durable signal that is hard to distinguish from a real one after the fact —
# a short dwell threshold during testing produced 205 "reviewed" pages on a
# single job, none of which anyone actually reviewed.
#
#     FOOTNOTES_HITL=1 python wsgi.py
#
# Decided per job at upload rather than read live, so a job never accumulates
# a signal that was switched on halfway through its review.
HITL_DEFAULT = os.environ.get('FOOTNOTES_HITL') == '1'

# How long a page must stay on screen before we treat the user as having
# looked at it (a passive approval). Per page, from what there is on it to
# check — see review_signals.dwell_floor_ms and _page_dwell_floor. Long enough
# that paging through a book at speed doesn't silently confirm two hundred
# pages nobody actually read — a 0.1s floor during testing produced 205
# "reviewed" pages on a single job.
#
# FOOTNOTES_DWELL_MS pins one fixed floor for every page instead, because
# exercising the signal by hand is unbearable at real speeds:
#     FOOTNOTES_DWELL_MS=100 FOOTNOTES_HITL=1 python wsgi.py
# Anything that short is a testing convenience and makes the implicit
# confirmations worthless as evidence.
#
# TODO: also derive the floor from the specific user's own activity (how fast
# they normally page through and edit), so a fast reader's genuine review and
# a slow reader's skim are each judged against that person's pace.
try:
    LP_DWELL_OVERRIDE_MS = (max(0, int(os.environ['FOOTNOTES_DWELL_MS']))
                            if os.environ.get('FOOTNOTES_DWELL_MS') else None)
except ValueError:
    LP_DWELL_OVERRIDE_MS = None

ACTIVITY_LOG_PATH = RESULTS_FOLDER / 'activity_log.jsonl'
_activity_log_lock = threading.Lock()


def _get_client_id():
    """Attributes edit activity to the authenticated user if logged in;
    falls back to an anonymous per-browser-session cookie otherwise (kept as
    a defensive fallback — every edit route now requires login, so this
    branch shouldn't normally be reached)."""
    if g.get('user') is not None:
        return f"user:{g.user['id']}:{g.user['username']}"
    if 'client_id' not in session:
        session['client_id'] = str(uuid.uuid4())
        session.permanent = True
    return session['client_id']


def _log_activity(action, job_id, _actor_user_id=None, _actor_username=None, _actor_client_id=None, **fields):
    """Append one raw JSONL record to the activity log. Two families of
    action share this one writer/file: user corrections (bbox/reference/
    component/name add/update/delete, text edits, split/merge, job deletion)
    and model predictions (action names prefixed 'prediction_' — see
    _log_prediction below). Best-effort: a logging failure must never break
    the actual edit request.

    Kept deliberately raw/unaggregated (not just counts) — the goal is to
    later mine it for spatial/behavioral features, e.g. if a user's edits
    consistently land in a narrow x1 range, that positional consistency
    could become an input signal for OD/NER predictions on the rest of the
    document. Every correction entry carries both the old and new value
    where the handler has one (not just the new value — see each call site),
    plus explicit user_id/username (in addition to the legacy client_id
    string, kept for continuity) so "which user made this correction, and
    what did they change it from/to" is answerable directly from the log
    without cross-referencing anything else.

    The `_actor_*` params exist for the one call site that runs outside a
    Flask request — the initial pipeline pass, from process_pdf_sync in the
    background worker thread — where g/session aren't available at all,
    so _get_client_id()/g.user must never be touched. Pass them explicitly
    there (resolved from the uploading user at enqueue time); every other
    call site leaves them None and gets the normal g.user-based resolution.
    """
    entry = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'client_id': _actor_client_id if _actor_client_id is not None else _get_client_id(),
        'user_id': _actor_user_id if _actor_user_id is not None else (g.user['id'] if g.get('user') is not None else None),
        'username': _actor_username if _actor_username is not None else (g.user['username'] if g.get('user') is not None else None),
        'job_id': job_id,
        'action': action,
        **fields,
    }
    try:
        with _activity_log_lock:
            with open(ACTIVITY_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.error(f"Failed to write activity log entry: {e}")


def _log_prediction(action, job_id, model, _actor_user_id=None, _actor_username=None, _actor_client_id=None, **fields):
    """Model-prediction counterpart to _log_activity, same file/writer —
    action is one of 'prediction_od'/'prediction_ocr'/
    'prediction_ner_reference'/'prediction_ner_component'/'prediction_ner_name'.
    `model` is a dict resolved fresh at the call site (never cached), since a
    job's NER suite/OCR engine can be switched mid-lifetime (see
    update_job_ner_suite/update_job_ocr_engine) — every reclassification
    must record whichever model was actually in effect for that specific
    call, not the job's original upload-time config. See _log_activity for
    what the `_actor_*` params are for."""
    _log_activity(action, job_id, _actor_user_id=_actor_user_id, _actor_username=_actor_username,
                  _actor_client_id=_actor_client_id, model=model, **fields)

# Several job workers and request threads save the history: one at a time,
# and through a temporary file, so a reader never sees half of it.
_history_lock = threading.Lock()


def save_history():
    """Save processing status to persistent storage"""
    try:
        with _history_lock:
            # Convert datetime objects to ISO strings for JSON serialization
            history_to_save = {}
            for k, v in list(processing_status.items()):
                item = v.copy()
                if isinstance(item.get('start_time'), datetime):
                    item['start_time'] = item['start_time'].isoformat()
                if isinstance(item.get('end_time'), datetime):
                    item['end_time'] = item['end_time'].isoformat()
                history_to_save[k] = item

            tmp = HISTORY_FILE.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(history_to_save, f, indent=2)
            tmp.replace(HISTORY_FILE)
    except Exception as e:
        print(f"Error saving history: {e}")

def load_history():
    """Load processing status from persistent storage"""
    global processing_status
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                loaded_history = json.load(f)
                
            # Convert ISO strings back to datetime objects
            for k, v in loaded_history.items():
                if 'start_time' in v and v['start_time']:
                    v['start_time'] = datetime.fromisoformat(v['start_time'])
                if 'end_time' in v and v['end_time']:
                    v['end_time'] = datetime.fromisoformat(v['end_time'])
            
            processing_status = loaded_history
        except Exception as e:
            print(f"Error loading history: {e}")
            processing_status = {}

# Load history on startup
load_history()

def allowed_file(filename):
    """Check if file has allowed extension"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Job Queue
job_queue = queue.Queue()

# Guards read-modify-write of a job's result dict/file when editing footnotes.
# OCR itself is run outside this lock so one slow re-OCR doesn't block edits on other jobs.
_job_result_lock = threading.Lock()

def worker(index=0):
    """Background worker to process PDFs from the queue, one at a time.
    There are as many workers as the most copies any model may have (Admin ->
    Models -> Instances, see _ensure_workers); a worker whose index is no
    longer needed exits once it is idle.

    Wraps each job in its own try/except so a single job raising an
    unexpected exception (e.g. its processing_status entry got deleted out
    from under it mid-run, racing a job-delete request against the pipeline
    still writing to it) can't silently kill this thread — since it's the
    only consumer of job_queue, that would leave every subsequent upload
    stuck showing "queued" forever until the whole process is restarted."""
    while True:
        if index >= _desired_workers():
            with _workers_lock:
                _workers.pop(index, None)
            return
        if _draining.is_set():            # before a planned restart: queued jobs wait
            time.sleep(2)
            continue
        _models_ready.wait()               # the startup loading, copies included
        with MODEL_LOAD_LOCK:              # and no job starts while models are loading
            pass
        try:
            job = job_queue.get(timeout=5)
        except queue.Empty:
            continue
        if job is None:
            break
        if _draining.is_set():            # arrived while this worker was waiting
            job_queue.put(job)
            job_queue.task_done()
            time.sleep(2)
            continue
        # uploader_id/uploader_username: optional trailing elements (absent
        # for older-shaped queue entries) — who to attribute the initial-pass
        # prediction log entries to, since there's no request/g.user here.
        job_id, pdf_path, options, *actor = job
        uploader_id = actor[0] if len(actor) > 0 else None
        uploader_username = actor[1] if len(actor) > 1 else None
        try:
            process_pdf_sync(job_id, pdf_path, options, uploader_id, uploader_username)
        except Exception as e:
            logger.error(f"Unhandled error processing job {job_id}: {e}")
        job_queue.task_done()

# ---- model copies and job workers (Admin -> Models -> Instances) ----
# MODEL_INSTANCES mirrors db.model_instances, {(model_type, model_key): n};
# models not in it have one copy. Kept in memory because the pools read it
# from worker threads, which have no request (and so no db connection).
MODEL_INSTANCES = {}
MAX_MODEL_INSTANCES = 4
_workers = {}
_workers_lock = threading.Lock()
# Set by POST /api/_internal/drain before a planned restart: workers finish
# what they are processing and take nothing new, so the restart interrupts
# nothing; queued jobs are picked up again after it (_recover_stuck_jobs).
_draining = threading.Event()

# Set once the startup warm-up has loaded every model and copy
# (_trigger_self_warmup); until then the workers leave queued jobs alone, so
# no job runs beside a load (MODEL_LOAD_LOCK, model_pool.py). Without a
# warm-up (scripts, tests) it is set after a minute.
_models_ready = threading.Event()
_warmup_started = threading.Event()
_ready_fallback = threading.Timer(60, lambda: None if _warmup_started.is_set() else _models_ready.set())
_ready_fallback.daemon = True
_ready_fallback.start()



def _instances_for(model_type, model_key):
    return MODEL_INSTANCES.get((model_type, model_key), 1)


def _desired_workers():
    return min(MAX_MODEL_INSTANCES, max([1] + list(MODEL_INSTANCES.values())))


def _ensure_workers():
    """Start workers up to _desired_workers(); surplus ones exit when idle."""
    with _workers_lock:
        for index in range(_desired_workers()):
            if index not in _workers or not _workers[index].is_alive():
                _workers[index] = threading.Thread(target=worker, args=(index,), daemon=True,
                                                   name=f'job-worker-{index}')
                _workers[index].start()


def _od_model_key(path):
    try:
        return Path(path).resolve().relative_to((Path(__file__).parent / 'models' / 'od').resolve()).with_suffix('').as_posix()
    except ValueError:
        return Path(path).stem


OD_MODELS.size_of = lambda path: _instances_for('od_model', _od_model_key(path))
NEREngine.pool.size_of = lambda key: _instances_for('ner_suite', Path(key[0]).name)
OCREngine.pool.size_of = lambda engine: _instances_for('ocr_engine', engine)


def _load_model_instances():
    try:
        with app.app_context():
            MODEL_INSTANCES.clear()
            MODEL_INSTANCES.update(db.list_model_instances())
    except Exception as e:
        logger.error(f"Could not read model instance settings: {e}")


def apply_model_instances():
    """After the admin changed a model's copies: drop surplus copies, start or
    retire workers, and load the new copies of the default models now."""
    for pool in (OD_MODELS, NEREngine.pool, OCREngine.pool):
        pool.trim()
    _ensure_workers()
    threading.Thread(target=_warm_default_copies, daemon=True).start()


def _warm_default_copies():
    try:
        OCREngine.pool.warm('surya')
        OD_MODELS.warm(str(Path(__file__).parent / 'models' / 'od' / f'{DEFAULT_OD_MODEL}.pt'))
        NEREngine.pool.warm((str(Path(__file__).parent / 'models' / 'ner' / DEFAULT_NER_SUITE),
                             'nested' in DEFAULT_NER_SUITE.lower()))
    except Exception as e:
        logger.error(f"Could not load copies of the default models: {e}")


def gpu_licence_status():
    """The vGPU's licence line from nvidia-smi, e.g. 'Licensed (Expiry: ...)'
    or 'Unlicensed (Restricted)'; None without nvidia-smi."""
    import subprocess
    try:
        out = subprocess.run(['nvidia-smi', '-q'], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if 'License Status' in line:
            return line.split(':', 1)[1].strip()
    return None


def unload_model(model_type, model_key):
    """A model switched off in Admin -> Models gives its GPU memory back:
    every loaded copy goes now, or when the job using it is done. Should it
    be needed again (e.g. an edit on an old job made with it), it is loaded
    again then."""
    if model_type == 'ner_suite':
        pool, keys = NEREngine.pool, [k for k in NEREngine.pool.loaded() if Path(k[0]).name == model_key]
    elif model_type == 'od_model':
        pool, keys = OD_MODELS, [k for k in OD_MODELS.loaded() if _od_model_key(k) == model_key]
    elif model_type == 'ocr_engine':
        pool, keys = OCREngine.pool, [k for k in OCREngine.pool.loaded() if k == model_key]
    else:
        return
    for key in keys:
        pool.unload(key)


def model_instances_overview():
    """What Admin -> Models shows next to the Instances setting."""
    loaded = {}
    for (path, _nested), counts in NEREngine.pool.loaded().items():
        loaded[('ner_suite', Path(path).name)] = counts
    for path, counts in OD_MODELS.loaded().items():
        loaded[('od_model', _od_model_key(path))] = counts
    for engine, counts in OCREngine.pool.loaded().items():
        loaded[('ocr_engine', engine)] = counts
    gpu = None
    try:
        import torch
        if torch.cuda.is_available():
            # what dropped copies left in PyTorch's cache goes back first, so
            # the figure shows what the models really hold
            InstancePool.release_memory()
            free, total = torch.cuda.mem_get_info()
            gpu = ((total - free) / 2**30, total / 2**30)
    except Exception:
        pass
    with _workers_lock:
        workers = sum(1 for t in _workers.values() if t.is_alive())
    return {'instances': dict(MODEL_INSTANCES), 'loaded': loaded, 'workers': workers,
            'licence': gpu_licence_status(), 'draining': _draining.is_set(),
            'busy': sum(1 for st in list(processing_status.values()) if st.get('status') == 'processing'),
            'max': MAX_MODEL_INSTANCES, 'gpu': gpu}


_load_model_instances()
_ensure_workers()


def process_pdf_sync(job_id, pdf_path, options, uploader_id=None, uploader_username=None):
    """Process PDF synchronously (called by worker thread) and update status.

    uploader_id/uploader_username: who to attribute the initial-pass
    prediction log entries to (see _log_activity's _actor_* params) — this
    runs in the background worker thread, outside any Flask request, so
    there's no g.user to read like every other logging call site has."""
    try:
        processing_status[job_id].update({
            'status': 'processing',
            'step': 'Initializing pipeline...',
            'progress': 10
        })
        
        # Initialize enhanced pipeline with options
        od_model_path = str(Path(__file__).parent / 'models' / 'od' / f"{options.get('od_model', 'yolo11n_640_production')}.pt")
        ner_suite_path = str(Path(__file__).parent / 'models' / 'ner' / options.get('ner_suite', 'mbert_production'))
        is_nested = 'nested' in options.get('ner_suite', 'mbert_production').lower()

        # This job's own copies of the models it uses, taken in the one order
        # every job takes them (model_pool.py); a job waits here while other
        # jobs hold every allowed copy (Admin -> Models -> Instances).
        processing_status[job_id]['step'] = 'Waiting for a free copy of the models...'
        with contextlib.ExitStack() as leases:
            leases.enter_context(OCREngine.pool.lease(OCREngine.engine_key(options.get('use_surya_ocr', True))))
            leases.enter_context(OD_MODELS.lease(od_model_path))
            ner_copy = leases.enter_context(NEREngine.pool.lease((ner_suite_path, is_nested)))
            # never on the CPU: a job whose NER isn't on the GPU fails
            # rather than running there
            import torch
            if torch.cuda.is_available() and getattr(ner_copy, 'device', None) is not None \
                    and ner_copy.device.type != 'cuda':
                raise RuntimeError(f"NER suite {options.get('ner_suite')} is not on the GPU; not processing on the CPU")
            del ner_copy                 # the lease alone holds it, so a dropped copy can be freed
            processing_status[job_id]['step'] = 'Initializing pipeline...'

            pipeline = EnhancedPDFToStructuredFootnotesPipeline(
                debug=options.get('debug', False),
                cpu_mode=options.get('cpu_mode', False),
                save_images=False,
                save_footnotes=False,
                od_conf=options.get('od_conf', 0.5),
                ref_conf=options.get('ref_conf', 0.7),
                comp_conf=options.get('comp_conf', 0.7),
                od_model_path=od_model_path,
                ner_suite_path=ner_suite_path,
                is_nested=is_nested,
                use_surya_ocr=options.get('use_surya_ocr', True), # Default to true for testing
                split_footnotes=options.get('split_footnotes', True),
                enable_ibidem_handling=options.get('enable_ibidem', True),
                # Older jobs re-processed without the option keep their old behaviour.
                split_multi_work=options.get('split_multi_work', False)
            )
        
            processing_status[job_id]['step'] = 'Processing PDF...'
            processing_status[job_id]['progress'] = 30
        
            # Process the PDF
            result = pipeline.process_pdf(pdf_path, batch_size=options.get('batch_size', 8))

        # Detach the OD proposal payload before anything else touches `result`:
        # it is written to its own sidecar file, never into <job>_results.json,
        # which the download/export routes serve verbatim.
        od_proposals = result.pop('od_proposals', None)

        # Assign ids to references/components/names now (normally deferred,
        # lazy, to _get_job_result's first call for a job — see
        # _backfill_reference_and_component_ids) so the prediction log below
        # can address every item by a stable id from the moment it's first
        # written, rather than ids only appearing the first time someone
        # views the job.
        _backfill_reference_and_component_ids(result)

        # Log every prediction from this initial pass — one line per
        # detected footnote/reference/component/name, model/thresholds
        # resolved from `options` (this call's own config, since this is the
        # one-time upload pass). Later reclassifications resolve the model
        # fresh per-call instead (see _log_reference_predictions/
        # _ner_model_info) since the job's NER suite/OCR engine can be
        # switched after this. Best-effort: must never break processing.
        actor_kwargs = {}
        try:
            # No Flask request/g.user here (background worker thread) — every
            # _log_prediction call below passes the uploader explicitly via
            # _actor_*, resolved at enqueue time (see worker()/_process_upload),
            # instead of the usual g.user-based resolution _log_activity falls
            # back to. client_id mirrors the "user:<id>:<username>" shape
            # _get_client_id() itself would have produced for a logged-in user.
            actor_kwargs = {
                '_actor_user_id': uploader_id,
                '_actor_username': uploader_username,
                '_actor_client_id': f"user:{uploader_id}:{uploader_username}" if uploader_id is not None else None,
            }
            od_model_info = {'od_model': options.get('od_model', 'yolo11n_640_production'), 'od_conf_threshold': options.get('od_conf', 0.5)}
            ocr_model_info = {'ocr_engine': 'surya' if options.get('use_surya_ocr', True) else 'pdfminer'}
            ner_model_info = {
                'ner_suite': options.get('ner_suite', 'mbert_production'),
                'is_nested': is_nested,
                'ref_conf_threshold': options.get('ref_conf', 0.7),
                'comp_conf_threshold': options.get('comp_conf', 0.7),
            }
            # OD/OCR run batched across the whole document (one YOLO call per
            # page-batch, one OCR call per page), so there's no true per-footnote
            # split — every entry below shares the same whole-document total,
            # set by PDFFootnoteExtractor and threaded through pipeline.process_pdf's
            # metadata. NER is genuinely per-footnote (one classify_footnote()
            # call each), so that timing comes off each footnote individually.
            od_inference_time = result.get('metadata', {}).get('od_inference_time_seconds')
            ocr_inference_time = result.get('metadata', {}).get('ocr_inference_time_seconds')
            for footnote in result.get('results', []):
                fid = footnote.get('id')
                ner_inference_time = footnote.get('inference_time_seconds')
                if footnote.get('confidence') is not None:
                    # page/bbox included so a model box can be paired with its
                    # later human correction straight from the log, without
                    # having to join back against the results file.
                    _log_prediction('prediction_od', job_id, od_model_info, footnote_id=fid, confidence=footnote.get('confidence'),
                                     page=footnote.get('page'), bbox=footnote.get('bbox'),
                                     inference_time_seconds=od_inference_time, **actor_kwargs)
                _log_prediction('prediction_ocr', job_id, ocr_model_info, footnote_id=fid, inference_time_seconds=ocr_inference_time, **actor_kwargs)
                for ref in footnote.get('references', []):
                    _log_prediction('prediction_ner_reference', job_id, ner_model_info, footnote_id=fid,
                                     reference_id=ref.get('id'), label=ref.get('citation_type'), confidence=ref.get('confidence'),
                                     inference_time_seconds=ner_inference_time, **actor_kwargs)
                    for comp in ref.get('components', []):
                        if comp.get('inferred'):
                            continue  # copied from a sibling reference, not a fresh prediction — see _log_reference_predictions
                        _log_prediction('prediction_ner_component', job_id, ner_model_info, footnote_id=fid,
                                         reference_id=ref.get('id'), component_id=comp.get('id'),
                                         label=comp.get('label'), confidence=comp.get('confidence'),
                                         inference_time_seconds=ner_inference_time, **actor_kwargs)
        except Exception as e:
            logger.error(f"Failed to log initial-pass predictions for job {job_id}: {e}")

        # GROBID reference extraction (optional)
        if options.get('enable_grobid', False):
            processing_status[job_id]['step'] = 'Running GROBID reference extraction...'
            processing_status[job_id]['progress'] = 80
            try:
                from pipeline.grobid_client import GrobidClient
                grobid = GrobidClient(grobid_url=options.get('grobid_url', GROBID_URL))
                if grobid.is_available():
                    grobid_result = grobid.process_pdf(pdf_path)
                    result['grobid_references'] = grobid_result['grobid_references']
                    result['grobid_metadata'] = grobid_result['grobid_metadata']
                    logger.info(f"GROBID extracted {len(grobid_result['grobid_references'])} references")
                else:
                    logger.warning("GROBID server is not available, skipping reference extraction")
                    result['grobid_references'] = []
                    result['grobid_metadata'] = {'error': 'GROBID server unreachable'}
            except Exception as e:
                logger.error(f"GROBID processing failed: {e}")
                result['grobid_references'] = []
                result['grobid_metadata'] = {'error': str(e)}

        # Metadata enrichment (optional)
        if options.get('enable_enrichment', False):
            processing_status[job_id]['step'] = 'Running External Metadata Enrichment...'
            processing_status[job_id]['progress'] = 85
            try:
                from pipeline.metadata_enrichment import EnrichmentService
                enrichment_service = EnrichmentService()
                
                refs_to_enrich = []
                for fn in result.get('results', []):
                    refs_to_enrich.extend(fn.get('references', []))
                if 'grobid_references' in result:
                    refs_to_enrich.extend(result['grobid_references'])
                
                total_refs = len(refs_to_enrich)
                enrichment_stats = {"google_books": 0, "crossref": 0, "openaire": 0}
                
                for idx, ref in enumerate(refs_to_enrich):
                    components = ref.get('components', [])
                    external_data = enrichment_service.enrich_reference(components)
                    if external_data:
                        ref['external_metadata'] = external_data
                        for source in external_data:
                            enrichment_stats[source] = enrichment_stats.get(source, 0) + 1
                            
                    progress_pct = 85 + int((idx / max(total_refs, 1)) * 5)
                    processing_status[job_id]['progress'] = progress_pct
                    gb_count = enrichment_stats.get("google_books", 0)
                    cr_count = enrichment_stats.get("crossref", 0)
                    oa_count = enrichment_stats.get("openaire", 0)
                    processing_status[job_id]['step'] = f'Enriching Metadata ({idx+1}/{total_refs}) [GB:{gb_count} CR:{cr_count} OA:{oa_count}]'

                result['summary']['enrichment_stats'] = enrichment_stats

            except Exception as e:
                logger.error(f"Metadata enrichment failed: {e}")
        
        processing_status[job_id]['step'] = 'Finalizing results...'
        processing_status[job_id]['progress'] = 90
        
        # Save results - ibid./op. cit. inferred the way every later save does
        # (pipeline/reference_inference.py), so processing and editing agree
        reference_inference.reinfer(result)
        result_filename = f"{job_id}_results.json"
        result_path = RESULTS_FOLDER / result_filename
        
        with open(result_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        # Proposal sidecar for the layout prior. Written here on the worker
        # thread before the status flips to completed, so no reader can observe
        # a job as done-but-sidecar-missing. Only the mutable tail of this file
        # (reviewed_pages/dismissed/auto_applied/revision) is ever rewritten
        # later, and that happens under _job_result_lock.
        if od_proposals is not None:
            try:
                _save_job_proposals(job_id, {
                    'version': 1,
                    'od_model': options.get('od_model', 'yolo11n_640_production'),
                    'floor_conf': options.get('od_floor_conf', 0.05),
                    'decision_conf': options.get('od_conf', 0.5),
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'pages': {str(k): v for k, v in od_proposals.items()},
                    'doc_body_font': _document_body_font(od_proposals),
                    'reviewed_pages': {},
                    'dismissed': [],
                    'auto_applied': [],
                    'revision': 0,
                })
            except Exception as e:
                # A missing sidecar degrades the prior to flags-only; it must
                # never fail the job that produced perfectly good footnotes.
                logger.warning(f"Could not write proposal sidecar for {job_id}: {e}")

        # Update status to completed
        processing_status[job_id].update({
            'status': 'completed',
            'step': 'Processing completed successfully!',
            'progress': 100,
            'end_time': datetime.now(),
            'result_file': result_filename,
            'summary': {
                'total_footnotes': result['metadata']['total_footnotes_processed'],
                'total_references': result['metadata'].get('total_references_detected', 0),
                'total_components': result['metadata'].get('total_components_extracted', 0),
                'ibidem_footnotes': result['metadata'].get('ibidem_footnotes_processed', 0),
                'inferred_components': result['metadata'].get('total_inferred_components', 0),
                'references_split': result['metadata'].get('references_split_multi_work', 0),
                'processing_time': result['metadata']['processing_time_seconds'],
                'grobid_references': result.get('grobid_metadata', {}).get('total_references', 0)
            }
        })
        
        # Store results for quick access
        processing_results[job_id] = result

        # No layout-prior pass here: processing always ends at the detector's
        # raw output. Adding boxes the prior is confident about happens only
        # from the viewer, with Layout Assist on, after a reviewer's own
        # confirmation (see apply_layout_prior) — so whether a book gets that
        # help is decided while reviewing it, not at upload.

        # Save history persistently
        save_history()
        
    except Exception as e:
        # job_id may no longer be in processing_status (e.g. deleted from
        # history while this job was still mid-flight) — nothing to update
        # or persist in that case, and indexing it directly would raise a
        # second, unhandled KeyError that (pre-worker-hardening) used to kill
        # the whole background worker thread.
        if job_id in processing_status:
            processing_status[job_id].update({
                'status': 'error',
                'step': f'Error: {str(e)}',
                'progress': 0,
                'end_time': datetime.now(),
                'error': str(e)
            })
            # Save history persistently
            save_history()


# Curated OD checkpoints to expose in the upload dropdown, each with a short
# human-readable explanation. models/od/ also contains raw pretrained
# YOLOv8/YOLO11 backbones, per-experiment intermediate checkpoints, and full
# training-run directories (experiments_v2/, experiments_v3/, helios_results/,
# etc.) left over from training — none of those are selectable footnote
# detectors, so only these vetted checkpoints are listed here.
#
# 'best'/'best_old' (the original YOLOv12-nano checkpoint, byte-identical
# files) were dropped from the selectable list on request — the files are
# left in place on disk so any already-completed job that recorded
# od_model="best"/"best_old" in its saved options still has something to
# resolve against, it's just not offered for new uploads anymore.
#
# 'yolo12n_640' is byte-identical to the
# now-dropped 'best'/'best_old' (same underlying checkpoint, kept selectable
# under its own name since it's a legitimate strong option on its own merits).
CURATED_OD_MODELS = {
    'v_merged_2026': "YOLOv8-nano",
    'yolo11n_1280': "YOLOv11-nano @ 1280px",
    'yolo12n_640': "YOLOv12-nano @ 640px (same weights as the old 'best'/'best_old')",
    'yolo12s_1280': "YOLOv12-small @ 1280px",
    'yolo11n_640_production': "YOLOv11-nano @ 640px (the earlier default)",
    'yolo11n_validated_v2': "YOLOv11-nano @ 640px (the default)",
}

# What a new upload uses unless it picks otherwise:
# YOLO11-n detection, XLM-R large references, HerBERT large components,
# HerBERT base names, nested. Jobs keep the models recorded in their options;
# the 'yolo11n_640_production'/'mbert_production' fallbacks elsewhere in this
# file are for old jobs that predate those options, so they stay as they are.
DEFAULT_OD_MODEL = 'yolo11n_validated_v2'
DEFAULT_NER_SUITE = 'validated_v2_nested'
app.jinja_env.globals['DEFAULT_OD_MODEL'] = DEFAULT_OD_MODEL
# Every NER suite runs on the GPU. A suite loads only when a job or an edit
# needs it, and switching one off in Admin -> Models gives its memory back
# (unload_model).
NEREngine.gpu_suites = None
app.jinja_env.globals['DEFAULT_NER_SUITE'] = DEFAULT_NER_SUITE

# OCR engines aren't discovered from disk (unlike OD/NER above) — there are
# only ever these two, so they're just a fixed dict. Kept in the same
# {key: description} shape as CURATED_OD_MODELS so get_available_models()
# can build all three model types (od_model/ner_suite/ocr_engine) the same
# way, including the same admin-disable treatment.
OCR_ENGINES = {
    'surya': "Surya OCR (Experimental)",
    'pdfminer': "PDFMiner (Text Layer)",
}

# The three admin-toggleable model "types" — same strings used as the
# model_type column in db.disabled_models, iterated over by the
# /admin/models routes in auth_routes.py.
MODEL_TYPES = ('od_model', 'ner_suite', 'ocr_engine')


def get_available_models():
    """Scan the local models directory and return available models, each
    flagged 'available' or 'disabled' (admin-turned-off via /admin/models —
    see db.is_model_disabled). A 'disabled' entry is still listed, just
    marked so templates can grey it out (index.html already turns any
    non-'available' status into a disabled <option>) — that's a deliberate
    choice over omitting it outright, so users can see a model exists but
    isn't currently offered, rather than it just silently vanishing."""
    base_dir = Path(__file__).parent / 'models'

    def _status(model_type, key):
        return 'disabled' if db.is_model_disabled(model_type, key) else 'available'

    od_models = {}
    od_dir = base_dir / 'od'
    if od_dir.exists():
        for file in od_dir.rglob('*.pt'):
            rel_path = file.relative_to(od_dir)
            key = str(rel_path.with_suffix('')).replace('\\', '/')
            if key not in CURATED_OD_MODELS:
                continue
            explanation = CURATED_OD_MODELS[key]
            od_models[key] = {
                'name': f"OD: {key} ({explanation})" + (" — Default" if key == DEFAULT_OD_MODEL else ""),
                'description': f"Local OD model: {rel_path} — {explanation}",
                'status': _status('od_model', key),
                'is_production': 'production' in key.lower()
            }

    ner_suites = {}
    ner_dir = base_dir / 'ner'
    if ner_dir.exists():
        for d in ner_dir.iterdir():
            if d.is_dir():
                # Verify it has the required subdirectories
                req_dirs = ['NER_reference', 'NER_component', 'NER_names']
                has_all = all((d / req).exists() for req in req_dirs)
                if has_all:
                    key = d.name
                    is_nested = "nested" in key.lower()
                    # A '_production' suffix marks the currently-designated
                    # production suite (see od_model's identical convention
                    # in CURATED_OD_MODELS above) — surfaced in the name/
                    # description here, not just baked into the folder name,
                    # so it's visible in the admin panel and upload form
                    # without needing to know the naming convention.
                    is_production = "production" in key.lower()
                    # Format base name (e.g. bert -> BERT, distilbert -> DistilBERT, xlmroberta -> XLMRoBERTa)
                    base_raw = key.split('_')[0].lower()
                    if key == DEFAULT_NER_SUITE:
                        base_name = 'Validated v2: XLM-R large / HerBERT large / HerBERT base'
                    elif base_raw == 'xlmroberta':
                        base_name = 'XLMRoBERTa'
                    elif base_raw == 'distilbert':
                        base_name = 'DistilBERT'
                    else:
                        base_name = base_raw.upper()

                    mode = "Nested" if is_nested else "Full Context"
                    label = (f"{base_name} ({mode})" + (" — Production" if is_production else "")
                             + (" — Default" if key == DEFAULT_NER_SUITE else ""))

                    ner_suites[key] = {
                        'name': label,
                        'description': f"NER Suite: {key}",
                        'status': _status('ner_suite', key),
                        'is_nested': is_nested,
                        'is_production': is_production
                    }

    ocr_engines = {
        key: {
            'name': description,
            'description': f"OCR engine: {description}",
            'status': _status('ocr_engine', key)
        }
        for key, description in OCR_ENGINES.items()
    }

    return od_models, ner_suites, ocr_engines


def _limit_upload_options(options):
    """Upload options as far as the uploader's features allow (features.py),
    for the form and the API alike: without Advanced Options every model,
    threshold and processing option is the default whatever was sent;
    looking for the book's reference list needs Show References, and
    enrichment needs Metadata Enrichment."""
    if not has_feature('advanced_options'):
        options.update({
            'debug': False, 'cpu_mode': False,
            'od_conf': 0.5, 'ref_conf': 0.7, 'comp_conf': 0.7, 'batch_size': 8,
            'od_model': DEFAULT_OD_MODEL, 'ner_suite': DEFAULT_NER_SUITE, 'use_surya_ocr': True,
            'split_footnotes': True, 'enable_ibidem': True, 'split_multi_work': True,
        })
    if not has_feature('show_references'):
        options['enable_grobid'] = False
    if not has_feature('metadata_enrichment'):
        options['enable_enrichment'] = False
    return options


def _validate_model_selection(options):
    """None if every model the request asked for is currently enabled;
    otherwise a human-readable error naming the first disabled one. Checked
    server-side (not just greyed out in index.html) so a disabled model
    can't be requested by editing the greyed-out <option> back in, or via
    the JSON API, which doesn't render the form at all."""
    ocr_key = 'surya' if options.get('use_surya_ocr') else 'pdfminer'
    for model_type, key in (
        ('od_model', options.get('od_model')),
        ('ner_suite', options.get('ner_suite')),
        ('ocr_engine', ocr_key),
    ):
        if key and db.is_model_disabled(model_type, key):
            return f"The selected {model_type.replace('_', ' ')} ('{key}') has been disabled by an admin — pick another."
    return None

@app.route('/')
@login_required
def index():
    """Main upload page"""
    od_models, ner_suites, ocr_engines = get_available_models()
    user_groups = db.list_groups_for_user(g.user)
    user_collections = db.list_collections_for_user(g.user)
    return render_template('index.html', od_models=od_models, ner_suites=ner_suites, ocr_engines=ocr_engines,
                            user_groups=user_groups, user_collections=user_collections)

@app.route('/set-language', methods=['POST'])
def set_language():
    """Switcher next to Logout (see _nav.html) posts here — plain form post
    (not /api/..., no login required) since it needs to work for anonymous
    visitors on the login/register pages too, see auth.load_language for
    where the choice actually lands (account vs session)."""
    auth.set_language(request.form.get('language', ''))
    next_path = request.form.get('next', '')
    if next_path and auth._safe_next(next_path):
        return redirect(next_path)
    return redirect(url_for('index'))


BOOK_METADATA_FIELDS = ('authors', 'title', 'year', 'publisher', 'license',
                         'isbn', 'issn', 'journal', 'doi')


def _clean_book_metadata(raw):
    """Normalizes a book-metadata dict/form down to just the known fields,
    each trimmed to a non-empty string or None — used both at upload time
    (form/JSON) and by the viewer's edit endpoint (update_book_metadata), so
    the shape stored in processing_status is always the same regardless of
    where it came from. All fields are optional; 'authors' is deliberately
    a single free-text string (e.g. "Jane Doe, John Smith"), not a
    structured list — good enough for manual entry, and keeps this simple
    until the TODO below is picked up.

    TODO: auto-fill these from GROBID's header extraction instead of
    (or as a starting point for) manual entry — grobid_client.py's
    process_pdf already parses <titleStmt>/<title> into document_title
    (see _parse_tei_xml) when GROBID header mode is used; extending it to
    also pull <author>/<date>/<publisher> from the TEI header and wiring
    that into this same dict would let the viewer's metadata panel start
    pre-filled instead of blank. Not done yet — out of scope for now."""
    if not raw:
        return {}
    cleaned = {}
    for field in BOOK_METADATA_FIELDS:
        value = (raw.get(field) or '').strip() if isinstance(raw.get(field), str) else raw.get(field)
        cleaned[field] = value if value else None
    return cleaned


def _process_upload(files, options, group_id, collection_id, user, book_metadata=None):
    """Core upload handling shared by the HTML /upload form route and the
    JSON POST /api/v1/documents route: saves each valid file, initializes its
    status entry, records ownership/collection membership, and enqueues it
    for background processing. Returns the list of created job ids (skipping
    any entry in `files` that isn't a real, allowed-extension upload).

    book_metadata (optional authors/title/year/publisher/license — see
    BOOK_METADATA_FIELDS/_clean_book_metadata) is shared across every file in
    this batch, same as `options` already is — uploading several files at
    once with the same metadata filled in applies it to all of them."""
    cleaned_metadata = _clean_book_metadata(book_metadata)
    job_ids = []
    for file in files:
        if not (file and allowed_file(file.filename)):
            continue

        job_id = str(uuid.uuid4())

        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_filename = f"{timestamp}_{job_id[:8]}_{filename}"
        file_path = UPLOAD_FOLDER / safe_filename
        file.save(file_path)

        processing_status[job_id] = {
            'status': 'queued',
            'step': 'Waiting in queue...',
            'progress': 0,
            'start_time': datetime.now(),
            'original_filename': file.filename,
            'options': options,
            'book_metadata': dict(cleaned_metadata),
        }
        save_history()
        db.set_job_owner(job_id, user['id'], group_id)
        if collection_id is not None:
            db.add_job_to_collection(collection_id, job_id)
        else:
            # No collection explicitly chosen — file it into the uploader's
            # own default "User's Uploads" collection instead of leaving it
            # collection-less. Ownership (set_job_owner above) already makes
            # this job genuinely theirs regardless, but every page a user
            # would actually check for "my uploads" (collections.html, the
            # API's /collections listing) reads from collection membership,
            # not job_owners directly — a job with correct ownership but no
            # collection was invisible there, indistinguishable from "never
            # uploaded" even though it processed fine. The default collection
            # used to only get backfilled once, the first time collections.html
            # loaded (see _ensure_default_collection in auth_routes.py) — real
            # uploads made after that first visit fell through the gap this
            # closes.
            default_collection = db.get_default_collection(user['id'])
            if default_collection is None:
                default_collection_id = db.create_default_collection(user['id'])
            else:
                default_collection_id = default_collection['id']
            db.add_job_to_collection(default_collection_id, job_id)

        # user id/username passed through explicitly — process_pdf_sync runs in
        # the background worker thread, with no Flask request/g.user available
        # to attribute the initial-pass prediction log entries to (see
        # _log_activity's _actor_* params).
        job_queue.put((job_id, str(file_path), options, user['id'], user['username']))
        job_ids.append(job_id)
    return job_ids


@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    """Handle file upload and start processing"""
    if 'files' not in request.files:
        flash(t('No file selected'))
        return redirect(url_for('index'))

    files = request.files.getlist('files')
    if not files or files[0].filename == '':
        flash(t('No file selected'))
        return redirect(url_for('index'))

    # Get processing options from form
    options = {
        'debug': 'debug' in request.form,
        'cpu_mode': 'cpu_mode' in request.form,
        'od_conf': float(request.form.get('od_conf', 0.5)),
        'ref_conf': float(request.form.get('ref_conf', 0.7)),
        'comp_conf': float(request.form.get('comp_conf', 0.7)),
        'batch_size': int(request.form.get('batch_size', 8)),
        'od_model': request.form.get('od_model', DEFAULT_OD_MODEL),
        'ner_suite': request.form.get('ner_suite', DEFAULT_NER_SUITE),
        'enable_grobid': 'enable_grobid' in request.form,
        'enable_enrichment': 'enable_enrichment' in request.form,
        'grobid_url': _resolve_grobid_url(g.user, request.form.get('grobid_url')),
        'use_surya_ocr': request.form.get('ocr_engine', 'surya') == 'surya',
        # Defaults on, so it's read as an explicit value rather than checkbox
        # presence (an unchecked box sends nothing, which would silently flip
        # the default to off).
        'split_footnotes': request.form.get('split_footnotes', 'true').strip().lower()
                           not in ('0', 'false', 'off', 'no'),
        'enable_ibidem': request.form.get('enable_ibidem', 'true').strip().lower()
                         not in ('0', 'false', 'off', 'no'),
        'split_multi_work': request.form.get('split_multi_work', 'true').strip().lower()
                            not in ('0', 'false', 'off', 'no'),
        # Fixed at upload from the development flag; see HITL_DEFAULT.
        'hitl': HITL_DEFAULT,
    }
    _limit_upload_options(options)

    model_error = _validate_model_selection(options)
    if model_error:
        flash(model_error)
        return redirect(url_for('index'))

    # Optional "share with group" choice — defaults to private (owner + admins only).
    group_id_raw = request.form.get('group_id', '').strip()
    group_id = int(group_id_raw) if group_id_raw else None
    if group_id is not None and g.user['role'] != 'admin' and not db.is_group_member(group_id, g.user['id']):
        flash(t('You are not a member of that group'))
        return redirect(url_for('index'))

    # Optional "add to collection" choice, so a freshly-uploaded publication can
    # be filed away immediately instead of requiring a separate trip to it later.
    collection_id_raw = request.form.get('collection_id', '').strip()
    collection_id = int(collection_id_raw) if collection_id_raw else None
    if collection_id is not None and not db.can_access_collection(g.user, collection_id):
        flash(t('Collection not found'))
        return redirect(url_for('index'))

    # Optional book metadata (authors/title/year/publisher/license) — all
    # blank by default; see BOOK_METADATA_FIELDS/_clean_book_metadata.
    book_metadata = {field: request.form.get(field, '') for field in BOOK_METADATA_FIELDS}

    job_ids = _process_upload(files, options, group_id, collection_id, g.user, book_metadata)

    if not job_ids:
        flash(t('Please select valid PDF files'))
        return redirect(url_for('index'))
    elif len(job_ids) == 1:
        return redirect(url_for('status', job_id=job_ids[0]))
    else:
        flash(t('Successfully queued {n} files for processing.', n=len(job_ids)))
        return redirect(url_for('history'))

@app.route('/status/<job_id>')
@login_required
@job_access_required
def status(job_id):
    """Show processing status page"""
    if job_id not in processing_status:
        flash(t('Invalid job ID'))
        return redirect(url_for('index'))
    
    return render_template('status.html', job_id=job_id)

@app.route('/api/status/<job_id>')
@login_required
@job_access_required
def api_status(job_id):
    """API endpoint to get processing status"""
    if job_id not in processing_status:
        return jsonify({'error': 'Invalid job ID'}), 404
    
    status_info = processing_status[job_id].copy()
    
    # Add processing time if available
    if 'start_time' in status_info:
        if status_info['status'] == 'completed' and 'end_time' in status_info:
            duration = (status_info['end_time'] - status_info['start_time']).total_seconds()
            status_info['processing_time'] = round(duration, 2)
        elif status_info['status'] == 'processing':
            duration = (datetime.now() - status_info['start_time']).total_seconds()
            status_info['elapsed_time'] = round(duration, 2)
        
        # Convert datetime objects to strings for JSON serialization
        status_info['start_time'] = status_info['start_time'].isoformat()
        if 'end_time' in status_info:
            status_info['end_time'] = status_info['end_time'].isoformat()
    
    return jsonify(status_info)

@app.route('/download/<job_id>')
@login_required
@job_access_required
def download_results(job_id):
    """Download results — plain JSON by default, or a generated citation
    export via ?format=, each work once with every place it is cited
    (pipeline/reference_works.py). BibTeX 'Standard' keeps ibidem/opus-citatum
    references as extracted (sparse, literal "Ibid." entries); 'Expanded'
    (and apa/chicago/mla, which are always expanded — see citation_styles'
    module docstring) resolve them back to the original work's full Author/
    Title/Journal/... using the same components_inferred the pipeline
    already computes for ibidem/opus-citatum detection."""
    if job_id not in processing_status:
        flash(t('Invalid job ID'))
        return redirect(url_for('index'))

    status_info = processing_status[job_id]
    if status_info['status'] != 'completed':
        flash(t('Processing not completed yet'))
        return redirect(url_for('status', job_id=job_id))

    fmt = request.args.get('format', 'json')

    if fmt in ('bibtex_standard', 'bibtex_expanded'):
        result = _get_job_result(job_id)
        if result is None:
            flash(t('Result file not found'))
            return redirect(url_for('status', job_id=job_id))
        bib_text = generate_bibtex(result, expanded=(fmt == 'bibtex_expanded'), provenance=True, deduplicate=True)
        return Response(
            bib_text,
            mimetype='application/x-bibtex',
            headers={'Content-Disposition': f'attachment; filename="footnotes_{job_id[:8]}.bib"'}
        )

    if fmt in ('apa', 'chicago', 'mla'):
        result = _get_job_result(job_id)
        if result is None:
            flash(t('Result file not found'))
            return redirect(url_for('status', job_id=job_id))
        text = generate_citation_list(result, fmt, deduplicate=True)
        return Response(
            text,
            content_type='text/plain; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="footnotes_{job_id[:8]}_{fmt}.txt"'}
        )

    if fmt == 'reference_strings':
        result = _get_job_result(job_id)
        if result is None:
            flash(t('Result file not found'))
            return redirect(url_for('status', job_id=job_id))
        text = generate_reference_strings(result, deduplicate=True)
        return Response(
            text,
            content_type='text/plain; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="footnotes_{job_id[:8]}_references.txt"'}
        )

    result_file = status_info['result_file']
    result_path = RESULTS_FOLDER / result_file

    if not result_path.exists():
        flash(t('Result file not found'))
        return redirect(url_for('status', job_id=job_id))

    return send_file(result_path, as_attachment=True, download_name=f"footnotes_{job_id[:8]}.json")

@app.route('/api/results/<job_id>')
@login_required
@job_access_required
def api_results(job_id):
    """API endpoint to get processing results as JSON"""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Results not found'}), 404

    return jsonify(result)

@app.route('/api/v1/documents/<job_id>/annotations')
@login_required
@job_access_required
def api_annotations(job_id):
    """API endpoint for the interactive viewer (FastAPI compatibility)"""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Results not found'}), 404

    # Map 'results' to 'footnotes' for compatibility with the interactive viewer
    res = result.copy()
    res['footnotes'] = res.get('results', [])
    # Include GROBID references if available
    res['grobid_references'] = res.get('grobid_references', [])
    res['grobid_metadata'] = res.get('grobid_metadata', {})
    return jsonify(res)


@app.route('/api/v1/documents/<job_id>/verification-status')
@login_required
@job_access_required
def verification_status(job_id):
    """Current human-verification state for this job — who last verified it
    and when, or unverified if no one has. Lives in processing_status (like
    ner-status/ocr-status below), not the results JSON, since it's editorial
    metadata about the job as a whole rather than anything the pipeline
    produced. Fetched once by the interactive viewer on load, and again after
    every toggle (see verify_job)."""
    if job_id not in processing_status:
        return jsonify({'error': 'Job not found'}), 404
    status = processing_status[job_id]
    return jsonify({
        'verified': bool(status.get('verified', False)),
        'verified_by': status.get('verified_by_username'),
        'verified_at': status.get('verified_at'),
    })


@app.route('/api/v1/documents/<job_id>/verify', methods=['POST'])
@login_required
@job_editor_required
def verify_job(job_id):
    """Mark (or, with verified=false, unmark) this job's source book as
    human-verified — a one-shot editorial sign-off on the book as a whole,
    distinct from any individual footnote/reference/component edit. Surfaced
    by the interactive viewer as a prompt on the last page, and as a small
    badge everywhere the job is listed (collections, etc. — see
    build_job_rows/_job_list.html). Logged like every other correction (see
    _log_activity) so who verified it and when is answerable from the
    activity log too, not just the current state stored here. An optional
    "reason" goes into that log entry (e.g. a script withdrawing the
    verification of a book whose PolIndex record changed)."""
    if job_id not in processing_status:
        return jsonify({'error': 'Job not found'}), 404

    payload = request.get_json(silent=True) or {}
    verified = bool(payload.get('verified', True))

    status = processing_status[job_id]
    old_verified = bool(status.get('verified', False))
    old_verified_by = status.get('verified_by_username')
    if verified:
        status['verified'] = True
        status['verified_by_user_id'] = g.user['id']
        status['verified_by_username'] = g.user['username']
        status['verified_at'] = datetime.now(timezone.utc).isoformat()
    else:
        status['verified'] = False
        status.pop('verified_by_user_id', None)
        status.pop('verified_by_username', None)
        status.pop('verified_at', None)
    save_history()

    _log_activity('book_verified' if verified else 'book_unverified', job_id,
                   old_verified=old_verified, old_verified_by=old_verified_by,
                   verified_at=status.get('verified_at'), reason=payload.get('reason'))

    return jsonify({
        'verified': status['verified'],
        'verified_by': status.get('verified_by_username'),
        'verified_at': status.get('verified_at'),
    })


@app.route('/api/v1/documents/<job_id>/book-metadata')
@login_required
@job_access_required
def get_book_metadata(job_id):
    """Current book-level metadata (authors/title/year/publisher/license —
    see BOOK_METADATA_FIELDS) — settable at upload time and/or later via
    update_book_metadata below. Lives in processing_status, same reasoning
    as verification-status above: it's editorial/bibliographic metadata
    about the job as a whole, not anything the pipeline itself produced."""
    if job_id not in processing_status:
        return jsonify({'error': 'Job not found'}), 404
    stored = processing_status[job_id].get('book_metadata') or {}
    return jsonify({field: stored.get(field) for field in BOOK_METADATA_FIELDS})


@app.route('/api/v1/documents/<job_id>/book-metadata', methods=['PUT'])
@login_required
@job_editor_required
def update_book_metadata(job_id):
    """Update any subset of the book-metadata fields — omitted fields keep
    their current value (partial update), an explicit empty string/null
    clears that field. Used by the interactive viewer's expandable metadata
    panel; see BOOK_METADATA_FIELDS/_clean_book_metadata for the field list
    and the GROBID auto-fill TODO."""
    if job_id not in processing_status:
        return jsonify({'error': 'Job not found'}), 404

    payload = request.get_json(silent=True) or {}
    status = processing_status[job_id]
    old_metadata = dict(status.get('book_metadata') or {})
    current = dict(old_metadata)
    for field in BOOK_METADATA_FIELDS:
        if field in payload:
            current[field] = payload[field]
    new_metadata = _clean_book_metadata(current)
    status['book_metadata'] = new_metadata
    save_history()

    _log_activity('book_metadata_update', job_id, old_metadata=old_metadata, new_metadata=new_metadata)

    return jsonify(new_metadata)


@app.route('/api/v1/documents/<job_id>/ner-status')
@login_required
@job_access_required
def ner_status(job_id):
    """Whether this job's configured NER suite is actually loadable right
    now, plus the currently-available suites to switch to if not. Surfaced
    by the interactive viewer as a warning banner + model switcher — the
    situation this exists for is a job's original suite having its files
    removed from disk after the job was processed (merely admin-disabling a
    suite via /admin/models never affects existing jobs' already-configured
    suite, since that only hides it from new uploads — see
    get_available_models). It does, however, block *switching to* a
    different, admin-disabled suite via the endpoint below — each
    alternative here carries its 'available'/'disabled' status so the
    viewer can grey those out the same way index.html already does."""
    options = processing_status.get(job_id, {}).get('options', {})
    current_suite = options.get('ner_suite', 'mbert_production')
    ner_suite_path, is_nested, _ref_conf, _comp_conf = _get_ner_options(job_id)
    available = _ner_suite_available(ner_suite_path, is_nested)

    _od_models, ner_suites, _ocr_engines = get_available_models()
    alternatives = [{'key': key, 'name': suite['name'], 'status': suite['status']} for key, suite in ner_suites.items() if key != current_suite]
    current_name = ner_suites.get(current_suite, {}).get('name', current_suite)

    return jsonify({'ner_suite': current_suite, 'ner_suite_name': current_name, 'available': available, 'alternatives': alternatives})


@app.route('/api/v1/documents/<job_id>/ner-suite', methods=['PUT'])
@login_required
@feature_required('advanced_options')
@job_editor_required
def update_job_ner_suite(job_id):
    """Switch which NER suite future edits on this job use. Doesn't touch
    anything already saved — only what _get_ner_options resolves for the
    next reclassification, split, merge, or reference edit on this job. The
    escape hatch when ner_status reports the job's original suite isn't
    loadable: pick any suite that IS loadable and future edits use that one
    instead. Switching to a suite an admin has disabled via /admin/models is
    rejected (mirrors _validate_model_selection at upload time) — unless
    it's a no-op re-selection of the suite the job is already using, which
    always succeeds regardless of its disabled status."""
    payload = request.get_json(silent=True) or {}
    new_suite = (payload.get('ner_suite') or '').strip()
    if not new_suite:
        return jsonify({'error': 'ner_suite is required'}), 400
    if job_id not in processing_status:
        return jsonify({'error': 'Job not found'}), 404

    current_suite = processing_status[job_id].get('options', {}).get('ner_suite', 'mbert_production')
    if new_suite != current_suite and db.is_model_disabled('ner_suite', new_suite):
        return jsonify({'error': f"'{new_suite}' has been disabled by an admin — pick another."}), 403

    suite_path = str(Path(__file__).parent / 'models' / 'ner' / new_suite)
    is_nested = 'nested' in new_suite.lower()
    if not _ner_suite_available(suite_path, is_nested):
        return jsonify({'error': f"'{new_suite}' isn't loadable either — pick a different suite"}), 422

    processing_status[job_id].setdefault('options', {})['ner_suite'] = new_suite
    save_history()
    _log_activity('ner_suite_change', job_id, ner_suite=new_suite, old_value=current_suite)

    return jsonify({'ner_suite': new_suite}), 200


@app.route('/api/v1/documents/<job_id>/ocr-status')
@login_required
@job_access_required
def ocr_status(job_id):
    """Mirror ner_status for the OCR engine: whether this job's configured
    engine (used to re-OCR a box when it's drawn/resized) is actually
    loadable right now, plus the alternative to switch to if not (each
    carrying its admin 'available'/'disabled' status — see ner_status)."""
    use_surya = _get_use_surya_ocr(job_id)
    current_key = 'surya' if use_surya else 'pdfminer'
    available = _ocr_engine_available(use_surya)

    _od_models, _ner_suites, ocr_engines = get_available_models()
    alternatives = [{'key': key, 'name': engine['name'], 'status': engine['status']} for key, engine in ocr_engines.items() if key != current_key]
    current_name = ocr_engines.get(current_key, {}).get('name', current_key)

    return jsonify({'ocr_engine': current_key, 'ocr_engine_name': current_name, 'available': available, 'alternatives': alternatives})


@app.route('/api/v1/documents/<job_id>/ocr-engine', methods=['PUT'])
@login_required
@feature_required('advanced_options')
@job_editor_required
def update_job_ocr_engine(job_id):
    """Switch which OCR engine future box add/resize edits on this job use.
    Doesn't touch anything already saved — only what _get_use_surya_ocr
    resolves for the next re-OCR on this job. Mirrors update_job_ner_suite,
    including rejecting a switch to an admin-disabled engine (except as a
    no-op re-selection of the one already in use)."""
    payload = request.get_json(silent=True) or {}
    new_engine = (payload.get('ocr_engine') or '').strip()
    if new_engine not in OCR_ENGINES:
        return jsonify({'error': f"ocr_engine must be one of: {', '.join(OCR_ENGINES)}"}), 400
    if job_id not in processing_status:
        return jsonify({'error': 'Job not found'}), 404

    current_key = 'surya' if _get_use_surya_ocr(job_id) else 'pdfminer'
    if new_engine != current_key and db.is_model_disabled('ocr_engine', new_engine):
        return jsonify({'error': f"'{new_engine}' has been disabled by an admin — pick another."}), 403

    use_surya = new_engine == 'surya'
    if not _ocr_engine_available(use_surya):
        return jsonify({'error': f"'{new_engine}' isn't loadable either — pick a different engine"}), 422

    processing_status[job_id].setdefault('options', {})['use_surya_ocr'] = use_surya
    save_history()
    _log_activity('ocr_engine_change', job_id, ocr_engine=new_engine, old_value=current_key)

    return jsonify({'ocr_engine': new_engine}), 200


@app.route('/results/<job_id>')
@login_required
@job_access_required
def view_results(job_id):
    """View results as structured HTML"""
    result = _get_job_result(job_id)
    if result is None:
        flash(t('Results not found'))
        return redirect(url_for('index'))

    return render_template('results.html',
                           job_id=job_id,
                           filename=result['metadata']['input_file'],
                           summary=result['metadata'],
                           results=result['results'],
                           grobid_references=result.get('grobid_references', []),
                           json_data=json.dumps(result, indent=2, ensure_ascii=False))

def _layout_assist_info_context():
    """Template values for _layout_assist_info.html — what Layout Assist looks
    at, what it does, and how much each kind of reviewer signal counts — taken
    from the live configuration (review_signals, layout_prior, ner_prior), so
    the numbers shown are the ones in effect, overrides included."""
    examples = [
        ('No footnotes detected', {}),
        ('One short footnote', {'footnotes': 1, 'ocr_chars': 80, 'references': 1, 'components': 4}),
        ('A typical page', {'footnotes': 3, 'ocr_chars': 600, 'references': 4, 'components': 20}),
        ('A dense page', {'footnotes': 8, 'ocr_chars': 4000, 'references': 20, 'components': 120}),
    ]
    floors = [(label, f, _page_dwell_floor({1: f} if f else {}, 1)) for label, f in examples]
    return {'la_weights': review_signals.WEIGHTS, 'la_dwell': review_signals.DWELL,
            'la_span': review_signals.SPAN_TEMPLATE,
            'la_dwell_own': _acting_review_params()['dwell'],
            'la_dwell_override': LP_DWELL_OVERRIDE_MS, 'la_floors': floors,
            'la_lp': layout_prior.DEFAULTS, 'la_np': ner_prior.DEFAULTS}


@app.route('/layout-assist')
@login_required
@feature_required('layout_assist')
def layout_assist_info():
    """The same explanation as the viewer's "i" pop-up, as a page of its own."""
    return render_template('layout_assist_info.html', **_layout_assist_info_context())


@app.route('/viewer/<job_id>')
@login_required
@job_access_required
def interactive_viewer(job_id):
    """Interactive PDF Viewer"""
    if _get_job_result(job_id) is None:
        flash(t('Results not found'))
        return redirect(url_for('index'))

    # "Back to <collection>" link: only shown when we actually got here from
    # a collection's job list (see _job_list.html's viewing_collection_id ->
    # ?collection_id=<id> on the link, set by collections.html/
    # collection_detail.html) — replaces the old "Browse Publications" panel,
    # which listed every related job/collection unconditionally regardless of
    # how you arrived here. Re-validated against the current user's access
    # (not just "was it in the URL") since the query param is client-supplied.
    back_to_collection = None
    collection_id = request.args.get('collection_id', type=int)
    if collection_id is not None and db.can_access_collection(g.user, collection_id):
        collection = db.get_collection(collection_id)
        if collection is not None:
            back_to_collection = {'id': collection['id'], 'name': collection['name']}

    # Group viewers get the full interactive viewer, read-only: every edit
    # control (add box, split/merge, label-to-apply, NER/OCR switchers,
    # delete icons) is hidden/disabled client-side via can_edit, and every
    # mutation route is independently enforced server-side by
    # job_editor_required regardless — this is a UX nicety, not the real
    # permission boundary.
    can_edit = db.can_edit_job(g.user, job_id)

    return render_template('interactive_pdf_viewer.html', document_id=job_id,
                            back_to_collection=back_to_collection,
                            can_edit=can_edit, **_layout_assist_info_context())

@app.route('/api/v1/documents/<job_id>/pdf')
@app.route('/pdf/<job_id>')
@login_required
@job_access_required
def serve_pdf(job_id):
    """Serve the original PDF file"""
    if job_id not in processing_status:
        return jsonify({'error': 'PDF not found'}), 404
    
    # Try to find the file in uploads
    for file in UPLOAD_FOLDER.glob(f"*{job_id[:8]}*.pdf"):
        return send_file(file)

    return jsonify({'error': 'PDF not found'}), 404


def _backfill_reference_and_component_ids(result):
    """Idempotently assign uuid4 ids to any reference/component/name missing
    one. References/components/names predate this feature and have no id
    field; stable ids are needed for REST addressing (PUT/DELETE by id).
    Returns True if anything changed, so the caller knows whether to persist.
    components_inferred entries (ibidem/opus_citatum) are intentionally left
    alone — manual editing doesn't touch that code path."""
    changed = False
    for footnote in result.get('results', []):
        for ref in footnote.get('references', []):
            if 'id' not in ref:
                ref['id'] = str(uuid.uuid4())
                changed = True
            for comp in ref.get('components', []):
                if 'id' not in comp:
                    comp['id'] = str(uuid.uuid4())
                    changed = True
                for name in comp.get('names', []):
                    if 'id' not in name:
                        name['id'] = str(uuid.uuid4())
                        changed = True
    return changed


def _get_job_result(job_id):
    """Return a job's result dict, lazily loading it from disk if the server
    was restarted since the job completed (processing_results is memory-only
    and isn't rehydrated from history.json on startup)."""
    if job_id in processing_results:
        result = processing_results[job_id]
    else:
        result_path = RESULTS_FOLDER / f"{job_id}_results.json"
        if not result_path.exists():
            return None

        with open(result_path, 'r', encoding='utf-8') as f:
            result = json.load(f)
        processing_results[job_id] = result

    if _backfill_reference_and_component_ids(result):
        with _job_result_lock:
            _save_job_result(job_id, result)

    return result


def _save_job_result(job_id, result):
    """Atomically persist a job's result dict to disk. Every ibid. and op. cit.
    is inferred again first (pipeline/reference_inference.py), so an edit -
    a reference added, removed or retyped, footnotes reordered - is followed
    by the ones that point back past it."""
    reference_inference.reinfer(result)
    result_path = RESULTS_FOLDER / f"{job_id}_results.json"
    tmp_path = result_path.with_suffix('.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    tmp_path.replace(result_path)


def _document_body_font(od_proposals):
    """Median body-text font size across a document's pages, or None.

    Weighted by each page's line count so a near-empty page (a plate, a
    half-title) can't drag the estimate. Pages with no text layer contribute
    nothing rather than a zero.
    """
    sizes = []
    for entry in (od_proposals or {}).values():
        font = entry.get('body_font')
        if font:
            sizes.extend([font] * max(1, entry.get('line_count', 1)))
    return round(statistics.median(sizes), 2) if sizes else None


def _proposals_path(job_id):
    return RESULTS_FOLDER / f"{job_id}_proposals.json"


def _get_job_proposals(job_id):
    """Return a job's OD proposal sidecar, or None if it has none.

    Jobs processed before the sidecar existed simply don't have one; callers
    must degrade to a flags-only prior built from the stored footnotes rather
    than treating its absence as an error.
    """
    if job_id in job_proposals:
        return job_proposals[job_id]

    path = _proposals_path(job_id)
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read proposal sidecar for {job_id}: {e}")
        return None
    job_proposals[job_id] = data
    return data


def _save_job_proposals(job_id, data):
    """Atomically persist a job's proposal sidecar. Caller holds _job_result_lock
    for anything that read-modify-writes it."""
    path = _proposals_path(job_id)
    tmp_path = path.with_suffix('.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp_path.replace(path)
    job_proposals[job_id] = data


def _find_source_pdf(job_id):
    """Locate the originally uploaded PDF for a job, same glob as serve_pdf."""
    for file in UPLOAD_FOLDER.glob(f"*{job_id[:8]}*.pdf"):
        return file
    return None


def _get_use_surya_ocr(job_id):
    return processing_status.get(job_id, {}).get('options', {}).get('use_surya_ocr', True)


def _empty_footnote_summary():
    return {'ref_count': 0, 'total_components': 0, 'refs_with_components': 0}


def _validate_bbox(bbox):
    """Return an error message if bbox is malformed, else None."""
    if not isinstance(bbox, dict):
        return 'bbox must be an object with numeric x1, y1, x2, y2'
    try:
        x1, y1, x2, y2 = float(bbox['x1']), float(bbox['y1']), float(bbox['x2']), float(bbox['y2'])
    except (KeyError, TypeError, ValueError):
        return 'bbox must contain numeric x1, y1, x2, y2'
    if x2 <= x1 or y2 <= y1:
        return 'bbox must have x2 > x1 and y2 > y1'
    return None


# Real label set from models/ner/*/NER_component/config.json (id2label), excluding 'O'.
# Used only to validate a user-*picked* dropdown value; model-suggested labels are trusted as-is.
KNOWN_COMPONENT_LABELS = {
    'Author', 'BookTitle', 'DOI', 'Editor', 'Ibidem', 'Issue', 'Journal',
    'OpusCitatum', 'Pages', 'PublicationPlace', 'PublicationYear',
    'PublicationYearSuffix', 'Publisher', 'Title', 'Translator', 'Volume', 'Webpage'
}

# Name sub-spans are only meaningful nested inside these component types.
COMPONENT_LABELS_WITH_NAMES = {'Author', 'Editor', 'Translator'}
KNOWN_NAME_LABELS = {'FirstName', 'LastName'}


def _get_ner_options(job_id):
    """Mirror _get_use_surya_ocr: read the job's original ner_suite/is_nested
    choice from processing_status, recorded at upload time (app.py upload_file,
    same fields process_pdf_sync uses to construct the pipeline)."""
    options = processing_status.get(job_id, {}).get('options', {})
    ner_suite = options.get('ner_suite', 'mbert_production')
    ner_suite_path = str(Path(__file__).parent / 'models' / 'ner' / ner_suite)
    is_nested = 'nested' in ner_suite.lower()
    ref_conf = options.get('ref_conf', 0.7)
    comp_conf = options.get('comp_conf', 0.7)
    return ner_suite_path, is_nested, ref_conf, comp_conf


def _ner_model_info(job_id):
    """Resolved-fresh (never cached) model info for prediction logging —
    reads processing_status directly so a mid-lifetime ner_suite switch
    (update_job_ner_suite) is reflected in the very next classification's
    log entry, not the job's original upload-time config. Mirrors
    _get_ner_options but returns a dict shaped for logging rather than the
    positional tuple NEREngine.get_instance wants."""
    options = processing_status.get(job_id, {}).get('options', {})
    ner_suite = options.get('ner_suite', 'mbert_production')
    return {
        'ner_suite': ner_suite,
        'is_nested': 'nested' in ner_suite.lower(),
        'ref_conf_threshold': options.get('ref_conf', 0.7),
        'comp_conf_threshold': options.get('comp_conf', 0.7),
    }


def _ocr_model_info(job_id):
    return {'ocr_engine': 'surya' if _get_use_surya_ocr(job_id) else 'pdfminer'}


def _od_model_info(job_id):
    options = processing_status.get(job_id, {}).get('options', {})
    return {'od_model': options.get('od_model', 'yolo11n_640_production'), 'od_conf_threshold': options.get('od_conf', 0.5)}


def _log_reference_predictions(job_id, references, footnote_id, inference_time_seconds=None, **extra):
    """Log one prediction_ner_reference + nested prediction_ner_component
    per item in `references` (the shape _classify_footnote_text/
    classify_footnote_references return) — call right after a genuine model
    classification produces them, never for a manually-typed span. Skips
    inferred components (ibidem/opus citatum carry-forward) since those are
    copied from a sibling reference, not a fresh prediction for this one —
    they're flagged 'inferred': True in the data itself.

    `inference_time_seconds`: how long the single classify call that produced
    ALL of `references` took — the caller times its own
    _classify_footnote_text call and passes the result straight through, so
    every reference/component logged from that one call shares it (same
    reasoning as the initial-pass NER timing: one model call, many items).

    `extra` is passed through to every line — e.g. origin='layout_assist'
    and the _actor_* fields from _lp_auto_apply."""
    if not references:
        return
    model = _ner_model_info(job_id)
    for ref in references:
        _log_prediction('prediction_ner_reference', job_id, model, footnote_id=footnote_id,
                         reference_id=ref.get('id'), label=ref.get('citation_type'), confidence=ref.get('confidence'),
                         inference_time_seconds=inference_time_seconds, **extra)
        for comp in ref.get('components', []):
            if comp.get('inferred'):
                continue
            _log_prediction('prediction_ner_component', job_id, model, footnote_id=footnote_id,
                             reference_id=ref.get('id'), component_id=comp.get('id'),
                             label=comp.get('label'), confidence=comp.get('confidence'),
                             inference_time_seconds=inference_time_seconds, **extra)
            for name in comp.get('names', []):
                _log_prediction('prediction_ner_name', job_id, model, footnote_id=footnote_id,
                                 reference_id=ref.get('id'), component_id=comp.get('id'),
                                 name_id=name.get('id'), label=name.get('label'),
                                 confidence=name.get('confidence'),
                                 inference_time_seconds=inference_time_seconds, **extra)


def _log_component_predictions(job_id, components, footnote_id, reference_id, inference_time_seconds=None):
    """Counterpart to _log_reference_predictions for _classify_reference_span's
    flat component list (used by create_reference/update_reference) — no
    reference-level entry here since citation_type is pure regex
    (classify_citation_type_standalone), not a model prediction."""
    if not components:
        return
    model = _ner_model_info(job_id)
    for comp in components:
        _log_prediction('prediction_ner_component', job_id, model, footnote_id=footnote_id,
                         reference_id=reference_id, component_id=comp.get('id'),
                         label=comp.get('label'), confidence=comp.get('confidence'),
                         inference_time_seconds=inference_time_seconds)
        for name in comp.get('names', []):
            _log_prediction('prediction_ner_name', job_id, model, footnote_id=footnote_id,
                             reference_id=reference_id, component_id=comp.get('id'),
                             name_id=name.get('id'), label=name.get('label'),
                             confidence=name.get('confidence'),
                             inference_time_seconds=inference_time_seconds)


def _ner_suite_available(ner_suite_path, is_nested):
    """True if this suite can be used: it is loaded already, or its three
    models are on disk. It doesn't load a suite just to find out: every
    viewer asks (ner-status), and since every suite goes on the GPU an old
    job's viewer would otherwise put that job's suite there. An
    edit that needs the suite loads it then."""
    if (str(ner_suite_path), bool(is_nested)) in NEREngine.pool.loaded():
        return True
    suite = Path(ner_suite_path)
    return all((suite / part / 'config.json').exists() and any((suite / part).glob('*.safetensors'))
               for part in ('NER_reference', 'NER_component', 'NER_names'))


def _ocr_engine_available(use_surya):
    """Mirror _ner_suite_available for the OCR engine used to re-OCR a box
    when it's drawn/resized (see OCREngine.get_instance in
    pipeline/ocr_service.py). The pdfminer.six path never loads external
    model weights, so this only ever really fails for the Surya engine —
    kept generic so both are checked the same way and the viewer's OCR
    selector doesn't need to special-case either."""
    try:
        OCREngine.get_instance(use_surya)
        return True
    except Exception:
        return False


def _classify_reference_span(job_id, ref_text, start, comp_conf=None):
    """Shared by create_reference/update_reference: classify one reference
    span's citation type + nested components. Returns (citation_type,
    components, needs_classification) — on any classification error (most
    likely the job's NER suite no longer being loadable, e.g. its files were
    removed from disk after the job was processed), falls back to an empty,
    needs_classification=True component list rather than failing the
    request outright, same principle as _classify_footnote_text below.
    citation_type still gets a real answer either way: it's pure regex, not
    dependent on the model actually loading — see
    classify_citation_type_standalone."""
    try:
        ner_suite_path, is_nested, _ref_conf, default_comp_conf = _get_ner_options(job_id)
        engine = NEREngine.get_instance(ner_suite_path, is_nested)
        citation_type = engine.classify_citation_type(ref_text)
        components = engine.classify_reference_components(ref_text, start, comp_conf if comp_conf is not None else default_comp_conf)
        return citation_type, components, False
    except Exception as e:
        logger.error(f"NER classification failed while classifying a reference span for job {job_id}: {e}")
        return classify_citation_type_standalone(ref_text), [], True


def _classify_footnote_text(job_id, text):
    """Run the full reference+component cascade on a footnote's text (used
    after OCR creates/updates a footnote from a manually drawn/resized bbox).
    Returns (references, needs_classification) — on any classification error,
    falls back to an empty, still-needs-classification state rather than
    failing the whole request (the OCR result is still worth keeping)."""
    try:
        ner_suite_path, is_nested, ref_conf, comp_conf = _get_ner_options(job_id)
        engine = NEREngine.get_instance(ner_suite_path, is_nested)
        references = engine.classify_footnote_references(text, ref_conf, comp_conf)
    except Exception as e:
        logger.error(f"NER classification failed while auto-classifying footnote text for job {job_id}: {e}")
        return [], True
    if _split_multi_work_enabled(job_id):
        # The same split the upload pipeline makes, so a box or text correction
        # doesn't bring back "Martines 2009, 2012" as one reference.
        holder = {'text': text, 'references': references}
        reference_split.split_footnote_references(holder, classify=classify_citation_type_standalone)
        references = holder['references']
    return references, False


def _split_multi_work_enabled(job_id):
    """Whether this job cuts references holding several works into one per
    work on every model run: the upload option, also set on existing books by
    admin_split_multi_work."""
    return bool(((processing_status.get(job_id) or {}).get('options') or {}).get('split_multi_work'))


def _recompute_footnote_summary(footnote):
    refs = footnote.get('references', [])
    footnote['summary'] = {
        'ref_count': len(refs),
        'total_components': sum(len(r.get('components', [])) for r in refs),
        'refs_with_components': len([r for r in refs if r.get('components')]),
    }


def _footnote_spans(footnote):
    """Every rectangle a footnote occupies, as [{page, bbox}].

    A footnote continuing over a page break has one per page. Older jobs have
    no spans field at all, so synthesise a single span from page/bbox - which
    keeps every caller on one code path.
    """
    spans = footnote.get('spans')
    if isinstance(spans, list) and spans:
        return spans
    return [{'page': footnote.get('page'), 'bbox': footnote.get('bbox')}]


def _footnote_list_page(footnote):
    """The page whose footnote list this footnote belongs to.

    Normally the page it was detected on. A footnote moved to a neighbouring
    page's list by hand (see move_footnote_to_page) carries list_page instead;
    its page/bbox/spans keep describing where it is printed, since the layout
    prior, OCR and box editing all read them as geometry.
    """
    list_page = footnote.get('list_page')
    return list_page if list_page is not None else footnote.get('page')


# Two boxes within this many points on every edge are one detected region:
# siblings split out of it share its box by design. Same tolerance as the
# viewer's BBOX_GROUP_TOLERANCE, which covers the +3pt bbox inflation.
BBOX_SAME_REGION_TOLERANCE = 4
# Overlaps thinner than this are ignored, so two boxes drawn edge to edge (and
# each inflated by those 3pt) don't count as overlapping.
BBOX_OVERLAP_SLACK = 6


def _same_region(a, b):
    return all(abs(float(a[k]) - float(b[k])) <= BBOX_SAME_REGION_TOLERANCE for k in ('x1', 'y1', 'x2', 'y2'))


def _boxes_overlap(a, b):
    width = min(float(a['x2']), float(b['x2'])) - max(float(a['x1']), float(b['x1']))
    height = min(float(a['y2']), float(b['y2'])) - max(float(a['y1']), float(b['y1']))
    return width > BBOX_OVERLAP_SLACK and height > BBOX_OVERLAP_SLACK


def _overlapping_footnote(result, page, bbox, ignore_ids=(), region=None):
    """The first footnote with a box on `page` overlapping `bbox`, else None.

    Footnote boxes must not overlap. The exception is one detected region:
    footnotes split out of it share its box, so a box matching `bbox` (or
    `region`, the box being resized before the edit, whose siblings still
    carry it) is that same region, not an overlap.
    """
    for footnote in result.get('results', []):
        if footnote.get('id') in ignore_ids:
            continue
        for span in _footnote_spans(footnote):
            other = span.get('bbox')
            if span.get('page') != page or _validate_bbox(other):
                continue
            if _same_region(other, bbox) or (region is not None and _same_region(other, region)):
                continue
            if _boxes_overlap(other, bbox):
                return footnote
    return None


def _region_chunks(text):
    """A box's OCR text cut into footnotes at its markers, the way the
    pipeline cuts a detected region (split_region_text): [{text, marker,
    number}]. A single footnote keeps its text exactly as read, line breaks
    included; only a region that really holds several footnotes is rejoined
    piece by piece."""
    chunks = split_region_text(text)
    if len(chunks) > 1:
        return chunks
    return [dict(chunks[0] if chunks else {'marker': False, 'number': None}, text=text)]


def _region_pieces(text):
    return [chunk['text'] for chunk in _region_chunks(text)]


def _printed_number(text):
    """The footnote number a footnote's text opens with, or None."""
    chunks = split_region_text(text)
    return chunks[0]['number'] if chunks and chunks[0]['marker'] else None


def _own_part(text, number, first):
    """What a box's reading holds of a footnote running over a page break: in
    its first box the piece with its number (the last piece, if none has it),
    in a later one the leading piece without a marker of its own (none: '')."""
    chunks = _region_chunks(text)
    if first:
        return next((c['text'] for c in chunks if number is not None and c['number'] == number), chunks[-1]['text'])
    return chunks[0]['text'] if not chunks[0]['marker'] else ''


def _continued_in_box(result, footnote_id, page, box, chunks, keep):
    """Footnotes running over a page break with a rectangle in `box` on `page`,
    each with the chunk of the box's new reading that is theirs:
    [(footnote, chunk index, index of that rectangle among its spans)]. One
    beginning in the box takes the chunk with its number, one ending in it the
    leading chunk without a marker of its own; chunk `keep` is taken already.
    One with no chunk of its own is not listed."""
    out, taken = [], {keep}
    for f in result.get('results', []):
        spans = _footnote_spans(f)
        if f.get('id') == footnote_id or len(spans) < 2:
            continue
        k = next((i for i, sp in enumerate(spans) if sp.get('page') == page
                  and not _validate_bbox(sp.get('bbox')) and _same_region(sp['bbox'], box)), None)
        if k is None:
            continue
        if k == 0:
            number = _printed_number(f.get('text'))
            at = next((i for i, c in enumerate(chunks)
                       if number is not None and c['number'] == number and i not in taken), None)
        else:
            at = 0 if not chunks[0]['marker'] and 0 not in taken else None
        if at is not None:
            taken.add(at)
            out.append((f, at, k))
    return out


def _overlap_error(page, other):
    return jsonify({'error': f'This box overlaps another footnote box on page {page}. '
                             'Footnote boxes may not overlap: shrink or move it, or delete the other box first.',
                    'conflict_footnote_id': other.get('id')}), 409


def _find_footnote(result, footnote_id):
    return next((f for f in result.get('results', []) if f.get('id') == footnote_id), None)


def _find_reference(footnote, reference_id):
    return next((r for r in footnote.get('references', []) if r.get('id') == reference_id), None)


def _find_component(reference, component_id):
    return next((c for c in reference.get('components', []) if c.get('id') == component_id), None)


def _find_name(component, name_id):
    return next((n for n in component.get('names', []) if n.get('id') == name_id), None)


def _validate_span(text_len, start, end):
    """Return an error message if start/end are malformed, else None."""
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        return 'start and end must be integers'
    if start < 0 or end > text_len or start >= end:
        return f'start/end must satisfy 0 <= start < end <= {text_len}'
    return None


def _spans_overlap(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def _cascade_requested(payload):
    """Whether an edit should re-run the stages that sit downstream of it.

    Holding Shift while finishing an edit in the viewer sends cascade=false,
    which means "apply exactly the thing I changed and leave everything under
    it alone": a bbox moves but keeps its OCR'd text and all its tags, a span's
    bounds move but keep their label and their children. It exists because the
    cascade is destructive by design — re-OCR replaces a footnote's text, which
    invalidates every hand-corrected offset below it, and re-classification
    replaces whole component/name sets — so anyone who has curated a footnote
    needs a way to nudge a boundary without paying for it.

    Only an explicit false disables it, so every existing caller and every
    API client keeps the cascading behaviour it has today."""
    return (payload or {}).get('cascade') is not False


def _offset_remapper(old_text, new_text):
    """A function mapping a character offset in old_text to the equivalent
    offset in new_text, built from the diff between the two.

    Used to carry a footnote's tags across a manual OCR correction. Every
    reference/component/name offset is absolute into the footnote's text, so
    fixing one letter mid-way through would otherwise leave everything after
    it pointing at the wrong characters.

    Offsets inside text the edit left alone map exactly. An offset inside a
    stretch that was replaced or deleted maps forward to where the next
    surviving text begins, which collapses a span whose text is entirely gone
    to zero length — that is how the caller knows to drop it. The mapping is
    non-decreasing, so nesting (a component inside its reference, a name
    inside its component) survives the move without needing to be re-checked."""
    blocks = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False).get_matching_blocks()

    def remap(pos):
        for old_start, new_start, length in blocks:
            if pos < old_start:
                return new_start
            if pos < old_start + length:
                return new_start + (pos - old_start)
        return len(new_text)

    return remap


# How much of a span's text has to survive an edit for the span to be worth
# carrying over. Rewriting a line wholesale still leaves the diff a scatter of
# incidental single-letter matches, and mapping a span onto those produces
# nonsense — an Author reading " e", a Title reading "ntirely" — which is worse
# than no tag at all. Anything that keeps at least half its length is treated
# as the same span, edited; anything gutted below that is treated as gone.
# Growth is not capped: typing *inside* a span is an edit to it, not a reason
# to drop it.
_REMAP_MIN_RETAINED = 0.5


def _remap_span(span, remap, new_text):
    """One reference/component/name moved onto the edited text, or None if the
    edit removed, or gutted, the text it covered."""
    start, end = span.get('start'), span.get('end')
    if start is None or end is None:
        return None
    new_start, new_end = remap(start), remap(end)
    if new_end - new_start < max(1, _REMAP_MIN_RETAINED * (end - start)):
        return None
    moved = dict(span)
    moved['start'], moved['end'], moved['text'] = new_start, new_end, new_text[new_start:new_end]
    return moved


def _remap_annotations(references, old_text, new_text):
    """Carry a footnote's whole tag tree across a text edit rather than
    discarding it — what a non-cascading text correction does instead of
    re-running the model. Fixing a letter of OCR noise shouldn't cost you the
    tagging of the rest of the footnote.

    Spans whose text the edit deleted outright are dropped, and a reference
    that loses its own span takes its components (and their names) with it.
    Everything is copied rather than mutated, so a failure partway through
    leaves the stored data untouched."""
    remap = _offset_remapper(old_text, new_text)
    moved_references = []
    for reference in references or []:
        moved_ref = _remap_span(reference, remap, new_text)
        if moved_ref is None:
            continue
        moved_comps = []
        for comp in reference.get('components', []):
            moved_comp = _remap_span(comp, remap, new_text)
            if moved_comp is None:
                continue
            moved_names = [n for n in (_remap_span(name, remap, new_text)
                                       for name in comp.get('names', [])) if n is not None]
            if moved_names:
                moved_comp['names'] = moved_names
            else:
                moved_comp.pop('names', None)
            moved_comps.append(moved_comp)
        moved_ref['components'] = moved_comps
        moved_references.append(moved_ref)
    return moved_references


def _clip_children(children, start, end, text, nested_key=None):
    """Carry a span's children across a resize to [start, end) instead of
    recomputing them — what a non-cascading edit does with the components
    under a reference, or the names under a component.

    A child that now sticks out past its parent is trimmed to fit rather than
    thrown away: nudging a component's boundary by one character shouldn't
    cost you the name inside it, and a child hanging outside its parent would
    violate the nesting these routes enforce on every other path. Only a child
    with nothing left inside the new bounds is dropped. `nested_key` recurses
    one level further ('names' under a component), since trimming a component
    can leave its own names sticking out in turn.

    Children are copied rather than mutated, so a caller that fails partway
    through leaves the stored data untouched."""
    kept = []
    for child in children or []:
        child_start, child_end = child.get('start'), child.get('end')
        if child_start is None or child_end is None:
            continue
        new_start, new_end = max(child_start, start), min(child_end, end)
        if new_start >= new_end:
            continue
        child = dict(child)
        if (new_start, new_end) != (child_start, child_end):
            child['start'], child['end'], child['text'] = new_start, new_end, text[new_start:new_end]
        if nested_key:
            nested = _clip_children(child.get(nested_key), new_start, new_end, text)
            if nested:
                child[nested_key] = nested
            else:
                child.pop(nested_key, None)
        kept.append(child)
    return kept


def _refresh_component_names(job_id, component, comp_text, start, label):
    """Re-derive a component's nested FirstName/LastName children after it was
    created or resized/relabelled.

    Manual work is never clobbered: a name the user added or edited by hand is
    kept as long as it still falls inside the component's new span, and the
    model's fresh suggestions are merged in around it. Everything else is
    re-derived, because after a resize the old model-produced names describe
    text that may no longer be part of this component. Relabelling away from
    Author/Editor/Translator drops them all — a Title has no names.

    Returns (names, suggested) so the caller can log the model-produced ones;
    a classification failure degrades to keeping just the manual names rather
    than failing the edit, same principle as _classify_reference_span."""
    if label not in COMPONENT_LABELS_WITH_NAMES:
        return [], []

    end = start + len(comp_text)
    kept = [n for n in component.get('names', [])
            if (n.get('source') == 'manual' or n.get('edited'))
            and n.get('start') is not None and n.get('end') is not None
            and n['start'] >= start and n['end'] <= end]

    try:
        ner_suite_path, is_nested, _ref_conf, comp_conf = _get_ner_options(job_id)
        engine = NEREngine.get_instance(ner_suite_path, is_nested)
        fresh = engine.extract_component_names(comp_text, start, comp_conf)
    except Exception as e:
        logger.error(f"NER classification failed while extracting names for job {job_id}: {e}")
        fresh = []

    names = list(kept)
    suggested = []
    for name in fresh:
        if any(_spans_overlap(name['start'], name['end'], n['start'], n['end']) for n in names):
            continue
        names.append(name)
        suggested.append(name)
    names.sort(key=lambda n: n.get('start', 0))
    return names, suggested


def _log_name_predictions(job_id, names, footnote_id, reference_id, component_id, inference_time_seconds=None):
    """Log one prediction_ner_name per model-produced name — the counterpart to
    _log_component_predictions one level deeper, used wherever the names model
    runs over a whole component rather than a hand-picked phrase."""
    if not names:
        return
    model = _ner_model_info(job_id)
    for name in names:
        _log_prediction('prediction_ner_name', job_id, model, footnote_id=footnote_id,
                         reference_id=reference_id, component_id=component_id, name_id=name.get('id'),
                         label=name.get('label'), confidence=name.get('confidence'),
                         inference_time_seconds=inference_time_seconds)


def _insert_sorted_by_start(items, item):
    """Insert `item` into `items` (a reference/component/name list) at the
    position matching its 'start' offset, instead of appending — keeps the
    stored order matching reading order for anything that reads the raw
    JSON directly (exports, the API), the same order the interactive viewer
    already sorts into at render time regardless of storage order. Spans
    can't overlap (enforced by _spans_overlap before this is ever called),
    so comparing start alone is sufficient to place it correctly."""
    idx = next((i for i, existing in enumerate(items) if existing.get('start', 0) > item.get('start', 0)), len(items))
    items.insert(idx, item)


# ---------------------------------------------------------------------------
# Layout consistency prior
#
# Suggestions are DERIVED on every request from the (immutable) proposal
# sidecar plus the job's current footnotes — never stored. That is what keeps
# this feature free of cache invalidation: accept a rescue and it vanishes from
# the next response by itself, because the accepted box is now a real footnote
# that matches its own proposal. Only the review bookkeeping
# (reviewed_pages/dismissed/auto_applied/revision) is persisted.
# ---------------------------------------------------------------------------

def _lp_user_authored(footnote):
    """True if a human drew or reshaped this box.

    An auto-applied box (source='layout_prior', edited=False) is deliberately
    not user-authored until someone actually touches it.
    """
    return footnote.get('source') == 'manual' or footnote.get('edited') is True


def _lp_locked_pages(result, sidecar):
    """Pages the prior must not auto-change.

    Either the user edited something on the page, or they sat on it long enough
    to count as having reviewed it. Both mean: they have seen this page, so
    whatever is on it now is their call and not ours.
    """
    pages = set()
    for fn in result.get('results', []):
        if _lp_user_authored(fn) and fn.get('page') is not None:
            pages.add(int(fn['page']))
    for page in (sidecar or {}).get('reviewed_pages', {}):
        pages.add(int(page))
    return pages


def _lp_bbox_tuple(bbox):
    if not isinstance(bbox, dict):
        return None
    try:
        return (float(bbox['x1']), float(bbox['y1']), float(bbox['x2']), float(bbox['y2']))
    except (KeyError, TypeError, ValueError):
        return None


def _lp_assemble(result, sidecar):
    """Build the inputs the pure prior module needs.

    Returns (mode, regions, page_meta, scorables, rescuables).

    In full mode everything is fitted and scored on the sidecar's exact,
    un-inflated proposal coordinates. In flags-only mode there are no
    proposals, so both fitting and scoring use the stored footnote bboxes —
    those carry a uniform +3pt inflation, which is harmless as long as it is
    on both sides of the comparison, and is exactly why the two modes must not
    be mixed.
    """
    cfg = layout_prior.DEFAULTS
    footnotes = result.get('results', [])
    by_id = {fn.get('id'): fn for fn in footnotes if fn.get('id')}
    pages = (sidecar or {}).get('pages') or {}

    # How much each kind of evidence counts: see pipeline/review_signals.py.
    def weight(signal, features=None):
        return review_signals.weight_for('layout', signal, features)

    # Approved pages and how: 'active' (the reviewer clicked approve) or
    # 'passive' (looked at long enough). Records from before approvals had a
    # mode were all passive.
    page_mode = {int(p): (v or {}).get('mode', 'passive')
                 for p, v in ((sidecar or {}).get('reviewed_pages') or {}).items()}

    def page_weight(page):
        if page in page_mode:
            return weight(review_signals.page_signal(page_mode[page]))
        return weight('model')

    regions, scorables, rescuables, rejects = [], [], [], []

    # Human decisions first, at the highest weight, carrying the font and line
    # count extracted at the moment the decision was made. A correction that
    # contributed only its geometry would be throwing away the sharpest
    # feature a known-good box has.
    confirmed_ids = set()
    for c in (sidecar or {}).get('confirmations', []):
        box = tuple(c['bbox'])
        if c.get('kind') in review_signals.ELEMENT_SIGNAL:
            regions.append({'page': c['page'], 'bbox': box, 'conf': None, 'font': c.get('font'),
                            'weight': weight(review_signals.ELEMENT_SIGNAL[c['kind']], c)})
            if c.get('footnote_id'):
                confirmed_ids.add(c['footnote_id'])
        elif c.get('kind') == 'rejected':
            rejects.append({'page': c['page'], 'bbox': box, 'font': c.get('font')})

    # Older jobs, and any edit made before the feature store existed, still
    # contribute their geometry — just without the typography.
    for fn in footnotes:
        box = _lp_bbox_tuple(fn.get('bbox'))
        if (box and _lp_user_authored(fn) and fn.get('page') is not None
                and fn.get('id') not in confirmed_ids):
            regions.append({'page': int(fn['page']), 'bbox': box, 'conf': fn.get('confidence'),
                            'font': None, 'weight': weight('element_edited')})

    if pages:
        page_meta = {int(k): {'w': v.get('w'), 'h': v.get('h'), 'has_text': v.get('has_text'),
                              'body_font': v.get('body_font'), 'line_count': v.get('line_count')}
                     for k, v in pages.items()}
        for page_str, entry in pages.items():
            page = int(page_str)
            for prop in entry.get('proposals', []):
                box = tuple(prop['bbox'])
                common = {'page': page, 'bbox': box, 'conf': prop.get('conf'),
                          'font': prop.get('font'), 'lines_inside': prop.get('lines_inside')}
                if prop.get('accepted'):
                    live = [fid for fid in prop.get('footnote_ids', []) if fid in by_id]
                    # A detector box a person deleted (it produced footnotes,
                    # none are left) or reshaped (one is user-authored) is not
                    # evidence for the layout any more: the rejection, or the
                    # person's own box above, stands in for it at its higher
                    # weight. Counting the model's version too would keep
                    # reinforcing the very box the reviewer threw out.
                    superseded = ((prop.get('footnote_ids') and not live)
                                  or any(_lp_user_authored(by_id[fid]) for fid in live))
                    if prop.get('conf', 0) >= cfg['tau_trust'] and not superseded:
                        regions.append(dict(common, weight=page_weight(page)))
                    # Flag the footnote(s) this proposal produced, unless a
                    # human has already put their hands on one of them.
                    if (live and not any(_lp_user_authored(by_id[fid]) for fid in live)
                            and not any(fid in confirmed_ids for fid in live)):
                        scorables.append(dict(common, key='f_' + live[0], footnote_id=live[0],
                                              member_keys=['f_' + fid for fid in live]))
                else:
                    rescuables.append(dict(common, key='r_' + prop['id'], proposal_id=prop['id']))
        return 'full', regions, page_meta, scorables, rescuables, rejects

    # flags-only: no sidecar, so the stored footnotes are all there is.
    # Footnotes split out of one detected region all carry its box, so they
    # are grouped back into that region first: one box to fit and one to flag,
    # not one per footnote (which listed the same box several times over).
    groups = []   # [page, box, [footnotes]]
    for fn in footnotes:
        box = _lp_bbox_tuple(fn.get('bbox'))
        if not box or fn.get('page') is None:
            continue
        page = int(fn['page'])
        for grp in groups:
            if grp[0] == page and all(abs(a - b) <= BBOX_SAME_REGION_TOLERANCE for a, b in zip(grp[1], box)):
                grp[2].append(fn)
                break
        else:
            groups.append([page, box, [fn]])
    for page, box, members in groups:
        # A region someone touched or confirmed, through any of its
        # footnotes, is theirs.
        if any(_lp_user_authored(fn) or fn.get('id') in confirmed_ids for fn in members):
            continue
        conf = members[0].get('confidence')
        common = {'page': page, 'bbox': box, 'conf': conf, 'font': None}
        scorables.append(dict(common, key='f_' + members[0]['id'], footnote_id=members[0]['id'],
                              member_keys=['f_' + fn['id'] for fn in members]))
        if conf is not None and conf >= cfg['tau_trust']:
            regions.append(dict(common, weight=page_weight(page)))
    return 'flags_only', regions, {}, scorables, [], rejects


def _lp_box_features(job_id, page, bbox):
    """Read the text-layer features of an arbitrary box: font size and line count.

    A human decision is worth more than a model detection, but only if we
    actually extract something from it. Without this a corrected box
    contributes its geometry and nothing else - and font size is the sharpest
    single discriminator we have between a footnote and body text.

    Returns {} on a scanned page with no text layer, which callers must treat
    as "unknown", never as a zero.
    """
    pdf_path = _find_source_pdf(job_id)
    if pdf_path is None:
        return {}
    try:
        import fitz
        from pipeline.pdf_to_structured_footnotes import PDFFootnoteExtractor
        with open_pdf(str(pdf_path)) as doc:
            if page is None or page < 1 or page > len(doc):
                return {}
            lines = PDFFootnoteExtractor._page_text_lines(doc[page - 1])
            if not lines:
                return {}
            box = (float(bbox['x1']), float(bbox['y1']), float(bbox['x2']), float(bbox['y2']))
            inside = PDFFootnoteExtractor._lines_inside(lines, box)
            return {
                'font': PDFFootnoteExtractor._weighted_median_size(inside),
                'lines_inside': len(inside),
                'page_font': PDFFootnoteExtractor._weighted_median_size(lines),
            }
    except Exception as e:
        logger.debug(f"Layout prior: could not read text features for {job_id} p{page}: {e}")
        return {}


def _lp_record_confirmation(job_id, kind, page, bbox, old_bbox=None, footnote_id=None, extra=None):
    """Persist one human decision, with its extracted features, to the sidecar.

    This is the human-in-the-loop store the prior learns from. Every entry is
    something a person actually decided:

      created         drew a box the detector missed        -> positive
      corrected       reshaped a box                        -> positive, plus a delta
      confirmed       opened a box and accepted it unchanged-> positive
      flag_dismissed  told us a flagged box was fine        -> positive
      rejected        deleted a box, or refused a rescue    -> negative

    Kept in the sidecar rather than on the footnote so that a rejection - the
    most informative signal of all, and the only one with no surviving
    footnote to hang off - is recorded at all.
    """
    if not isinstance(bbox, dict) or page is None:
        return
    box = _lp_bbox_tuple(bbox)
    if box is None:
        return

    entry = {
        'kind': kind,
        'page': int(page),
        'bbox': [round(v, 2) for v in box],
        'at': datetime.now(timezone.utc).isoformat(),
    }
    if footnote_id:
        entry['footnote_id'] = footnote_id
    old = _lp_bbox_tuple(old_bbox) if old_bbox else None
    if old:
        entry['old_bbox'] = [round(v, 2) for v in old]
    try:
        entry['by_user_id'] = g.user['id']
    except Exception:
        pass
    # How long the page had been on screen when this was decided — a feature
    # for weighting signals by activity later (see review_signals.weight_for).
    dwell = request.headers.get('X-Page-Dwell-Ms') if has_request_context() else None
    if dwell and dwell.isdigit():
        entry['page_dwell_ms'] = int(dwell)
    if extra:
        entry.update(extra)
    entry.update(_lp_box_features(job_id, page, bbox))

    try:
        with _job_result_lock:
            sidecar = _lp_ensure_sidecar(job_id)
            sidecar.setdefault('confirmations', []).append(entry)
            # Newest wins if this ever grows unbounded on a heavily edited book.
            if len(sidecar['confirmations']) > 2000:
                sidecar['confirmations'] = sidecar['confirmations'][-2000:]
            sidecar['revision'] = int(sidecar.get('revision', 0)) + 1
            _save_job_proposals(job_id, sidecar)
    except Exception as e:
        # An advisory signal must never break the edit that produced it.
        logger.warning(f"Layout prior: could not record confirmation for {job_id}: {e}")


def _lp_correction_bias(confirmations):
    """Median per-coordinate shift a human applied to the detector's boxes.

    Diagnostic only - deliberately never applied. The prior is fitted on the
    model's own output, so using this to move boxes would take a systematic
    error the humans have been fixing one at a time and propagate it across
    every page they haven't looked at yet. Surfacing it lets someone see
    "this checkpoint clips 6pt off the bottom of every box on this book" and
    decide what to do, which is a judgement call, not an automatic one.
    """
    deltas = {'x1': [], 'y1': [], 'x2': [], 'y2': []}
    for c in confirmations:
        if c.get('kind') != 'corrected' or 'old_bbox' not in c:
            continue
        for i, k in enumerate(('x1', 'y1', 'x2', 'y2')):
            deltas[k].append(c['bbox'][i] - c['old_bbox'][i])
    n = len(deltas['x1'])
    if n < 3:
        return None
    return {'n': n, **{k: round(statistics.median(v), 2) for k, v in deltas.items()}}


def _lp_revision(sidecar, result):
    """Monotonic-ish token guarding lost updates on dismiss.

    Combines the sidecar's own counter with the current footnote count, so a
    concurrent edit that changes what the suggestions mean also invalidates a
    stale dismiss.
    """
    return int((sidecar or {}).get('revision', 0)) * 100000 + len(result.get('results', []))


def _hitl_enabled(job_id):
    """Whether this job captures human-in-the-loop signal.

    Stamped into the job's options at upload from HITL_DEFAULT, so flipping the
    environment variable never retroactively changes what an in-flight review
    is recording. Jobs predating the flag fall back to it.
    """
    opts = (processing_status.get(job_id) or {}).get('options') or {}
    return bool(opts.get('hitl', HITL_DEFAULT))


def _page_features(result):
    """What each page holds for a reviewer to check: {page: {footnotes,
    ocr_chars, references, components}}. Pages with nothing detected are
    absent. Feeds the per-page dwell floor, and is recorded with every page
    approval so the floor can be learned later (review_signals)."""
    pages = {}
    for fn in (result or {}).get('results', []):
        refs = fn.get('references') or []
        for page in {sp.get('page') or fn.get('page') for sp in _footnote_spans(fn)}:
            if page is None:
                continue
            f = pages.setdefault(int(page), {'footnotes': 0, 'ocr_chars': 0,
                                             'references': 0, 'components': 0})
            f['footnotes'] += 1
            f['ocr_chars'] += len(fn.get('text') or '')
            f['references'] += len(refs)
            f['components'] += sum(len(r.get('components') or []) for r in refs)
    return pages


def _page_dwell_floor(features, page):
    """Milliseconds `page` must stay on screen to count as looked at, on the
    page timers of the reviewer at hand (_acting_review_params)."""
    if LP_DWELL_OVERRIDE_MS is not None:
        return LP_DWELL_OVERRIDE_MS
    return review_signals.dwell_floor_ms(features.get(page) or {}, _acting_review_params()['dwell'])


# ---------------------------------------------------------------------------
# Per-reviewer page timers (review_signals, "Per-reviewer page timers"):
# learned from each person's page approvals across books unless set on their
# profile. Rules and their confirmations are never per person - they belong
# to the book.
# ---------------------------------------------------------------------------

_USER_HISTORY_TTL_S = 300
_user_history_cache = {}


def _user_dwell_history(user_id):
    """(time on page, page features) for every page this reviewer approved
    by hand, across all books, and in how many books. Cached a few minutes."""
    hit = _user_history_cache.get(user_id)
    if hit and time.time() - hit[0] < _USER_HISTORY_TTL_S:
        return hit[1]
    approvals, books = [], set()
    for path in RESULTS_FOLDER.glob('*_proposals.json'):
        job_id = path.name[:-len('_proposals.json')]
        sidecar = job_proposals.get(job_id)
        if sidecar is None:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    sidecar = json.load(f)
            except (OSError, ValueError):
                continue
        for rec in (sidecar.get('reviewed_pages') or {}).values():
            if (rec.get('by_user_id') == user_id and rec.get('mode') == 'active'
                    and isinstance(rec.get('dwell_ms'), int) and rec['dwell_ms'] > 0):
                approvals.append((rec['dwell_ms'], rec.get('page_features') or {}))
                books.add(job_id)
    history = {'approvals': approvals, 'books': len(books)}
    _user_history_cache[user_id] = (time.time(), history)
    return history


def _user_review_params(user_id):
    """This reviewer's page timers in effect, with the learned and standard
    ones and what the learned ones come from (for the profile page)."""
    history = _user_dwell_history(user_id)
    learned, basis = review_signals.learn_dwell_params(history['approvals'])
    basis['books'] = history['books']
    settings = db.get_review_settings(user_id)
    standard = review_signals.clamp_dwell_params(review_signals.DWELL)
    if settings['dwell_learned']:
        dwell = learned or standard
    else:
        dwell = review_signals.clamp_dwell_params(settings['dwell_params'] or standard)
    return {'dwell': dwell, 'learned': learned, 'standard': standard, 'basis': basis,
            'own': not settings['dwell_learned'], 'own_params': settings['dwell_params'],
            'bounds': review_signals.DWELL_BOUNDS}


def _acting_review_params():
    """The page timers of whoever is making this request (once per request);
    the standard ones outside a request or for an anonymous one."""
    if has_request_context() and g.get('user') is not None:
        if 'review_params' not in g:
            g.review_params = _user_review_params(g.user['id'])
        return g.review_params
    return {'dwell': review_signals.DWELL}


app.jinja_env.globals['review_params_for'] = _user_review_params


def _save_review_settings(form):
    """Store the page-timer card's state: the learned timers, or the
    reviewer's own (one slider each, kept within review_signals.DWELL_BOUNDS).
    Returns the timers now in effect, or None if a value isn't a number."""
    learned = form.get('dwell_learned') == '1'
    own = None
    if not learned:
        own = {}
        for key in review_signals.DWELL_BOUNDS:
            try:
                own[key] = float(form.get('dwell_' + key))
            except (TypeError, ValueError):
                return None
        own = review_signals.clamp_dwell_params(own)
    db.set_review_settings(g.user['id'], learned, own)
    _log_activity('review_settings_saved', None, dwell_learned=learned, dwell_params=own)
    return _user_review_params(g.user['id'])['dwell']


@app.route('/profile/review-settings', methods=['POST'])
@login_required
def save_review_settings():
    """The page-timer card without JavaScript: a plain form post."""
    if _save_review_settings(request.form) is None:
        flash(t('Please enter a number'))
    else:
        flash(t('Layout Assist settings saved'))
    return redirect(url_for('auth.profile'))


@app.route('/api/v1/profile/review-settings', methods=['POST'])
@login_required
def save_review_settings_api():
    """The page-timer card saves itself on every change (switch, slider,
    buttons) through this."""
    dwell = _save_review_settings(request.form)
    if dwell is None:
        return jsonify({'error': t('Please enter a number')}), 400
    return jsonify({'success': True, 'dwell': dwell})


def _assist_on():
    """Whether this request comes from a viewer with Layout Assist switched
    on (the viewer marks those with an X-Layout-Assist header).

    The switch only decides whether suggestions are shown and applied - the
    endpoints that change a book on Layout Assist's initiative (span
    templates, confirmed check fixes) check it. Learning does not: every
    confirmation, page review and approval is recorded whatever the switch
   , so the priors and each reviewer's page timers keep
    learning while the assistant is off."""
    return (has_request_context() and request.headers.get('X-Layout-Assist') == '1'
            and has_feature('layout_assist'))


def _lp_compute(job_id, result=None, sidecar=None):
    """Derive the full layout-prior response for a job. Pure; no writes.

    Available for every job: whether its suggestions are shown is the
    viewer's Layout Assist switch, not an upload-time option. Jobs uploaded
    with the old enable_layout_prior=false still carry it in their options;
    it's ignored.
    """
    if result is None:
        result = _get_job_result(job_id)
    if result is None:
        return None
    if sidecar is None:
        sidecar = _get_job_proposals(job_id)

    if sidecar and sidecar.get('version') not in (None, layout_prior.VERSION):
        return {'status': 'inactive', 'reason': 'unsupported_sidecar_version',
                'mode': 'none', 'suggestions': {}, 'counts': {}}

    mode, regions, page_meta, scorables, rescuables, rejects = _lp_assemble(result, sidecar)
    prior = layout_prior.build_prior(regions, page_meta, rejects=rejects)
    revision = _lp_revision(sidecar, result)

    hitl = _hitl_enabled(job_id)
    learned = {
        'enabled': hitl,
        'confirmations': sum(1 for c in (sidecar or {}).get('confirmations', [])
                             if c.get('kind') != 'rejected'),
        'rejections': len(rejects),
        'reject_font': prior.get('reject_font'),
        'correction_bias': _lp_correction_bias((sidecar or {}).get('confirmations', [])),
    }

    # Page approvals as the viewer shows them — 'active' (clicked) or
    # 'passive' (looked at) — and the pages the prior will never change,
    # which also include pages where a box was edited. Reported whether or
    # not the prior itself is active for this book.
    locked = _lp_locked_pages(result, sidecar)
    page_approvals = {int(p): (v or {}).get('mode', 'passive')
                      for p, v in ((sidecar or {}).get('reviewed_pages') or {}).items()}
    # Dwell floor per page with footnotes, and for the rest (no detections).
    features = _page_features(result)
    approval = {'page_approvals': page_approvals, 'locked_pages': sorted(locked),
                'dwell_floor_ms': {p: _page_dwell_floor(features, p) for p in features},
                'dwell_floor_empty_ms': _page_dwell_floor({}, 0)}
    approval_counts = {
        'approved_active': sum(1 for m in page_approvals.values() if m == 'active'),
        'approved_passive': sum(1 for m in page_approvals.values() if m != 'active'),
        'locked_pages': len(locked),
    }

    if prior['status'] != 'active':
        return dict(approval, status='inactive', reason=prior.get('reason'), mode=mode,
                    revision=revision, hitl=hitl, suggestions={}, learned=learned,
                    counts=dict(approval_counts, flags=0, rescues=0))

    dismissed = {d['id'] for d in (sidecar or {}).get('dismissed', [])}
    # A proposal already promoted into a real footnote must never be offered
    # again — the sidecar still lists it as un-accepted, so without this it
    # would be re-rescued on every pass.
    applied = {a.get('proposal_id') for a in (sidecar or {}).get('auto_applied', [])}
    rescuables = [r for r in rescuables
                  if r['page'] not in locked
                  and r['key'] not in dismissed
                  and r.get('proposal_id') not in applied]
    # Approved pages are the reviewer's: no flags there either. A region
    # dismissed through any of its footnotes stays dismissed.
    scorables = [s for s in scorables
                 if s['page'] not in locked
                 and not any(k in dismissed for k in s.get('member_keys', [s['key']]))]

    audit = layout_prior.audit_document(prior, scorables, rescuables, rejects=rejects)
    # Every footnote a flagged box holds (footnotes split out of one detected
    # region share it), so the viewer can clear the flag the moment any of
    # them is edited, without waiting for a refetch.
    members = {sc['key']: [k[2:] for k in sc.get('member_keys', [sc['key']])] for sc in scorables}

    by_id = {fn.get('id'): fn for fn in result.get('results', [])}
    suggestions = {}
    for kind, items in (('flag', audit['flags']), ('rescue', audit['rescues'])):
        for item in items:
            entry = {'id': item['key'], 'kind': kind, 'page': item['page'],
                     'conf': item.get('conf'), 'conf_adj': item.get('conf_adj'),
                     'reasons': item['reasons']}
            if kind == 'flag':
                entry['basis'] = item.get('basis')
                fid = item['key'][2:]
                entry['footnote_id'] = fid
                entry['footnote_ids'] = members.get(item['key'], [fid])
                # Report the stored bbox so the overlay lines up with the box
                # the user actually sees, even though scoring used the exact one.
                stored = _lp_bbox_tuple((by_id.get(fid) or {}).get('bbox')) or item['bbox']
                entry['bbox'] = {'x1': stored[0], 'y1': stored[1], 'x2': stored[2], 'y2': stored[3]}
            else:
                b = item['bbox']
                entry['bbox'] = {'x1': b[0], 'y1': b[1], 'x2': b[2], 'y2': b[3]}
                # Landing on a box already there usually means that box is cut
                # short, not that a second footnote is hiding under it.
                entry.update(_lp_extend_target(result, item['page'], entry['bbox']) or {})
            suggestions.setdefault(str(item['page']), []).append(entry)

    classes = {name: {'n': c['n'], 'pages': c['pages'],
                      'x1': {'m': round(c['x1']['m'], 2), 's': round(c['x1']['s'], 2)},
                      'y2': {'m': round(c['y2']['m'], 2), 's': round(c['y2']['s'], 2)},
                      'x2': {'p90': round(c['x2']['p90'], 2) if c['x2']['p90'] is not None else None,
                             's': round(c['x2']['s'], 2)},
                      'font': c['font']}
               for name, c in prior['classes'].items()}

    return {
        **approval,
        'status': 'active',
        'mode': mode,
        'reason': None,
        'version': layout_prior.VERSION,
        'revision': revision,
        'hitl': hitl,
        'parity_split': prior['parity_split'],
        'page_geometry': prior['page_geometry'],
        'classes': classes,
        'suggestions': suggestions,
        # What the prior has learned from people, surfaced so it is auditable
        # rather than a black box. correction_bias is diagnostic only — see
        # _lp_correction_bias for why it is never applied.
        'learned': learned,
        'counts': {'flags': len(audit['flags']), 'rescues': len(audit['rescues']),
                   'pages_with_suggestions': len(suggestions),
                   'reviewed_pages': len((sidecar or {}).get('reviewed_pages', {})),
                   'auto_applied': len((sidecar or {}).get('auto_applied', [])),
                   **approval_counts},
    }


def _lp_extend_target(result, page, bbox):
    """How a rescue that overlaps boxes already on its page can be taken.

    None when it overlaps nothing: a plain rescue, added as a new box. When it
    overlaps exactly one box (or one detected region shared by footnotes split
    out of it), {'extends': ...}: that box grown to cover both, which the
    viewer offers as "Extend this box" — a resize through update_footnote, so
    it is re-read with OCR and NER and undoable like any other. When there is
    no single box to grow, or growing it would run into another box,
    {'blocked': True}: left for a person to redraw by hand.

    Only ever a suggestion. Reshaping stays a human decision (see
    _lp_auto_apply), which is why auto-apply skips these.
    """
    hits = []
    for fn in result.get('results', []):
        for i, span in enumerate(_footnote_spans(fn)):
            other = span.get('bbox')
            if span.get('page') != page or _validate_bbox(other):
                continue
            if _boxes_overlap(other, bbox):
                hits.append((fn, i, other))
    if not hits:
        return None
    region = hits[0][2]
    if any(not _same_region(other, region) for _fn, _i, other in hits):
        return {'blocked': True}
    union = {'x1': min(float(region['x1']), bbox['x1']), 'y1': min(float(region['y1']), bbox['y1']),
             'x2': max(float(region['x2']), bbox['x2']), 'y2': max(float(region['y2']), bbox['y2'])}
    if _overlapping_footnote(result, page, union, ignore_ids={fn.get('id') for fn, _i, _o in hits},
                             region=region) is not None:
        return {'blocked': True}
    fn, span_index, _other = hits[0]
    return {'extends': {'footnote_id': fn.get('id'), 'span_index': span_index,
                        'from_bbox': {k: float(region[k]) for k in ('x1', 'y1', 'x2', 'y2')},
                        'bbox': union}}


def _lp_debug_payload(result, sidecar, prior_payload):
    """Per-page model-vs-human comparison, for development.

    Pairs what the detector alone produced against what came out the other end
    after the prior and any human edits, so the two can be drawn on top of each
    other. Three things are otherwise invisible:

      - the correction delta. original_bbox is written on a footnote's first
        resize and read by nothing else in the app, so the geometry a human
        actually changed has never been visible anywhere.
      - proposals the prior looked at and declined. They exist only in the
        sidecar, and a declined rescue is exactly what you need to see when
        tuning the thresholds.
      - which boxes the prior added on its own.

    Only assembled when ?debug=1 is passed; it roughly doubles the response.
    """
    pages = (sidecar or {}).get('pages') or {}
    rescued_ids, declined = set(), {}
    for items in (prior_payload.get('suggestions') or {}).values():
        for s in items:
            if s['kind'] == 'rescue':
                rescued_ids.add(s['id'][2:])

    applied = {a.get('proposal_id'): a for a in (sidecar or {}).get('auto_applied', [])}
    debug = {}

    # what the detector alone produced
    for page_str, entry in pages.items():
        model = []
        for prop in entry.get('proposals', []):
            b = prop['bbox']
            if prop.get('accepted'):
                state = 'detected'
            elif prop['id'] in applied:
                state = 'auto_applied'
            elif prop['id'] in rescued_ids:
                state = 'suggested'
            else:
                state = 'declined'
            model.append({
                'id': prop['id'],
                'bbox': {'x1': b[0], 'y1': b[1], 'x2': b[2], 'y2': b[3]},
                'conf': prop.get('conf'), 'accepted': bool(prop.get('accepted')),
                'font': prop.get('font'), 'lines_inside': prop.get('lines_inside'),
                'state': state,
            })
        debug[page_str] = {'model': model, 'current': []}

    # what the document holds now, and how far it moved
    for fn in result.get('results', []):
        page = fn.get('page')
        box = _lp_bbox_tuple(fn.get('bbox'))
        if page is None or box is None:
            continue
        key = str(int(page))
        bucket = debug.setdefault(key, {'model': [], 'current': []})
        orig = _lp_bbox_tuple(fn.get('original_bbox'))
        entry = {
            'id': fn.get('id'),
            'bbox': {'x1': box[0], 'y1': box[1], 'x2': box[2], 'y2': box[3]},
            'source': fn.get('source') or 'model',
            'edited': bool(fn.get('edited')),
            'auto_applied': bool(fn.get('auto_applied')),
            'confidence': fn.get('confidence'),
            'conf_adj': fn.get('conf_adj'),
        }
        if orig:
            entry['original_bbox'] = {'x1': orig[0], 'y1': orig[1], 'x2': orig[2], 'y2': orig[3]}
            entry['delta'] = {k: round(box[i] - orig[i], 2)
                              for i, k in enumerate(('x1', 'y1', 'x2', 'y2'))}
        bucket['current'].append(entry)

    # In flags-only mode there are no proposals, so the only record of what the
    # detector originally said is original_bbox on a corrected footnote.
    if not pages:
        for key, bucket in debug.items():
            for cur in bucket['current']:
                if 'original_bbox' not in cur:
                    continue
                # original_bbox is only written on a footnote's FIRST resize,
                # so on a manually drawn box it holds the human's own first
                # attempt, not anything the detector ever said. Labelling that
                # 'model' would invent a prediction that never happened.
                human_origin = cur.get('source') in ('manual', 'layout_prior_accepted')
                bucket['model'].append({
                    'id': 'orig_' + str(cur['id']),
                    'bbox': cur['original_bbox'],
                    'conf': None if human_origin else cur.get('confidence'),
                    'accepted': True,
                    'state': 'first_draw' if human_origin else 'detected',
                })

    return debug


def _lp_ensure_sidecar(job_id):
    """Return this job's sidecar, creating an empty one if it has none.

    Jobs processed before sidecars existed still need somewhere to record
    dismissals and page reviews; an empty 'pages' keeps them in flags-only mode.
    """
    sidecar = _get_job_proposals(job_id)
    if sidecar is None:
        sidecar = {'version': layout_prior.VERSION, 'pages': {}, 'reviewed_pages': {},
                   'confirmations': [], 'dismissed': [], 'auto_applied': [], 'revision': 0,
                   'created_at': datetime.now(timezone.utc).isoformat()}
    sidecar.setdefault('reviewed_pages', {})
    sidecar.setdefault('confirmations', [])
    sidecar.setdefault('dismissed', [])
    sidecar.setdefault('auto_applied', [])
    sidecar.setdefault('revision', 0)
    return sidecar


@app.route('/api/v1/documents/<job_id>/layout-prior')
@login_required
@job_access_required
def layout_prior_status(job_id):
    """Per-page review suggestions from the intra-document layout prior.

    Recomputed from scratch on every call rather than cached — the inputs are a
    few hundred numbers and the arithmetic is trivial, and deriving it means
    there is no stored state to invalidate when a footnote changes.
    """
    payload = _lp_compute(job_id)
    if payload is None:
        return jsonify({'error': 'Job not found'}), 404
    if request.args.get('debug') in ('1', 'true'):
        payload['debug'] = _lp_debug_payload(_get_job_result(job_id),
                                              _get_job_proposals(job_id), payload)
    return jsonify(payload)


@app.route('/api/v1/documents/<job_id>/layout-prior/suggestions/<suggestion_id>/dismiss', methods=['POST'])
@login_required
@job_editor_required
def dismiss_layout_suggestion(job_id, suggestion_id):
    """Dismiss one suggestion. Doubles as a positive signal: a flag someone
    looked at and rejected means that box is right, so it feeds back into the
    prior at the same weight as an explicit correction."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    payload = request.get_json(silent=True) or {}
    kind = payload.get('kind')
    if kind not in ('flag', 'rescue'):
        return jsonify({'error': "kind must be 'flag' or 'rescue'"}), 400

    with _job_result_lock:
        result = _get_job_result(job_id)
        sidecar = _lp_ensure_sidecar(job_id)
        expected = _lp_revision(sidecar, result)
        if payload.get('revision') is not None and int(payload['revision']) != expected:
            return jsonify({'error': 'Suggestions have changed; refetch before dismissing',
                            'revision': expected}), 409
        if not any(d['id'] == suggestion_id for d in sidecar['dismissed']):
            sidecar['dismissed'].append({
                'id': suggestion_id, 'kind': kind,
                'by_user_id': g.user['id'],
                'at': datetime.now(timezone.utc).isoformat(),
            })
            sidecar['revision'] = int(sidecar.get('revision', 0)) + 1
        _save_job_proposals(job_id, sidecar)

    # Dismissing carries real information in both directions: "this flagged box
    # is fine" is a confirmation, "that suggested box is not a footnote" is a
    # rejection. Recorded after the lock is released, since it re-enters it.
    payload_bbox = payload.get('bbox')
    if isinstance(payload_bbox, dict) and payload.get('page'):
        _lp_record_confirmation(job_id,
                                 'flag_dismissed' if kind == 'flag' else 'rejected',
                                 payload.get('page'), payload_bbox)

    _log_activity('layout_suggestion_dismiss', job_id, suggestion_id=suggestion_id, kind=kind)
    return jsonify({'success': True, 'revision': _lp_revision(sidecar, result)})


@app.route('/api/v1/documents/<job_id>/page-reviews', methods=['POST'])
@login_required
@job_editor_required
def record_page_reviews(job_id):
    """Record that the user looked at these pages and left them alone.

    This is the implicit half of the confirmation signal: a page someone dwelt
    on without editing is taken as correct, which both locks it against
    auto-apply and feeds its boxes into the prior. Weighted below an explicit
    correction, because people do skim.

    prior_revision pins what was on screen at the time — a page reviewed under
    an older revision never retro-confirms a box the user never actually saw.
    """
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    pages = (request.get_json(silent=True) or {}).get('pages')
    if not isinstance(pages, list) or not pages:
        return jsonify({'error': 'pages must be a non-empty array'}), 400

    recorded = []
    features = _page_features(result)
    with _job_result_lock:
        sidecar = _lp_ensure_sidecar(job_id)
        for item in pages[:500]:
            if not isinstance(item, dict):
                continue
            page = item.get('page')
            dwell = item.get('dwell_ms')
            if not isinstance(page, int) or page < 1:
                continue
            floor = _page_dwell_floor(features, page)
            if not isinstance(dwell, (int, float)) or dwell < floor:
                continue
            key = str(page)
            if key in sidecar['reviewed_pages']:
                continue
            # Never overwrites: an already active approval stays active.
            sidecar['reviewed_pages'][key] = {
                'mode': 'passive',
                'at': datetime.now(timezone.utc).isoformat(),
                'by_user_id': g.user['id'],
                'dwell_ms': int(dwell),
                'prior_revision': item.get('prior_revision'),
                # features for learned weights and floors later (review_signals)
                'element_actions_on_page': sum(1 for c in sidecar['confirmations']
                                               if c.get('page') == page),
                'dwell_floor_ms': floor,
                'page_features': features.get(page) or {},
            }
            recorded.append(page)
        if recorded:
            sidecar['revision'] = int(sidecar.get('revision', 0)) + 1
            _save_job_proposals(job_id, sidecar)

    if recorded:
        _log_activity('page_reviewed', job_id, pages=recorded)
    return jsonify({'success': True, 'recorded': recorded,
                    'reviewed_pages': len(sidecar['reviewed_pages'])})


@app.route('/api/v1/documents/<job_id>/pages/<int:page>/approval', methods=['POST'])
@login_required
@job_editor_required
def set_page_approval(job_id, page):
    """Active page approval: the reviewer clicked the page's approval icon.

    The deliberate counterpart to the passive review record_page_reviews
    writes after a dwell. Everything on the page counts as checked — its boxes
    and labels get the page_active weight in both priors (review_signals) and
    the page is locked against auto-apply — and whatever Layout Assist still
    had open on it is settled as fine: box flags and label suggestions
    dismissed, a suggested missed footnote dismissed and recorded as rejected
    (the page is right without it). Every record this writes is tagged
    by_page_approval, so withdrawing (approved=false) removes exactly those
    and brings the items back, along with the approval itself.
    """
    if _get_job_result(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404
    payload = request.get_json(silent=True) or {}
    approved = payload.get('approved')
    if not isinstance(approved, bool):
        return jsonify({'error': 'approved (bool) is required'}), 400
    if page < 1:
        return jsonify({'error': 'page must be >= 1'}), 400

    key = str(page)
    now = datetime.now(timezone.utc).isoformat()
    if not approved:
        with _job_result_lock:
            sidecar = _lp_ensure_sidecar(job_id)
            previous = sidecar['reviewed_pages'].pop(key, None)
            sidecar['dismissed'] = [d for d in sidecar['dismissed'] if d.get('by_page_approval') != page]
            sidecar['confirmations'] = [c for c in sidecar['confirmations']
                                        if c.get('by_page_approval') != page]
            checks_state = sidecar.get('checks') or {}
            if checks_state.get('dismissed'):
                checks_state['dismissed'] = [d for d in checks_state['dismissed']
                                             if d.get('by_page_approval') != page]
            ner_state = sidecar.get('ner') or {}
            if ner_state.get('dismissed'):
                ner_state['dismissed'] = [d for d in ner_state['dismissed']
                                          if d.get('by_page_approval') != page]
                ner_state['revision'] = int(ner_state.get('revision', 0)) + 1
            sidecar['revision'] = int(sidecar.get('revision', 0)) + 1
            _save_job_proposals(job_id, sidecar)
        _log_activity('page_unapproved', job_id, page=page,
                      previous_mode=(previous or {}).get('mode'))
        return jsonify({'success': True, 'page': page, 'mode': None})

    lp = _lp_compute(job_id) or {}
    np_ = _np_compute(job_id) or {}
    box_items = (lp.get('suggestions') or {}).get(key, [])
    label_items = [s for items in (np_.get('suggestions') or {}).values() for s in items
                   if s.get('page') == page]
    check_items = [i for i in ((_checks_compute(job_id) or {}).get('items') or []) if i.get('page') == page]
    rejected = []
    with _job_result_lock:
        sidecar = _lp_ensure_sidecar(job_id)
        sidecar['reviewed_pages'][key] = {
            'mode': 'active',
            'at': now,
            'by_user_id': g.user['id'],
            'prior_revision': lp.get('revision'),
            # features for learned weights later (review_signals)
            'dwell_ms': payload.get('dwell_ms') if isinstance(payload.get('dwell_ms'), int) else None,
            'element_actions_on_page': sum(1 for c in sidecar['confirmations'] if c.get('page') == page),
            'open_items': len(box_items) + len(label_items) + len(check_items),
            'page_features': _page_features(_get_job_result(job_id)).get(page) or {},
        }
        known = {d['id'] for d in sidecar['dismissed']}
        for s in box_items:
            if s['id'] in known:
                continue
            sidecar['dismissed'].append({'id': s['id'], 'kind': s['kind'], 'by_user_id': g.user['id'],
                                         'at': now, 'by_page_approval': page})
            if s['kind'] == 'rescue' and not s.get('extends') and not s.get('blocked'):
                rejected.append(s)
        if label_items:
            ner_state = sidecar.setdefault('ner', {})
            ner_state.setdefault('version', ner_prior.VERSION)
            ner_dismissed = ner_state.setdefault('dismissed', [])
            known = {d['id'] for d in ner_dismissed}
            for s in label_items:
                if s['id'] not in known:
                    ner_dismissed.append({'id': s['id'], 'by_user_id': g.user['id'], 'at': now,
                                          'by_page_approval': page})
            ner_state['revision'] = int(ner_state.get('revision', 0)) + 1
        if check_items:
            checks_dismissed = sidecar.setdefault('checks', {}).setdefault('dismissed', [])
            known = {d.get('id') for d in checks_dismissed}
            for i in check_items:
                if i['id'] not in known:
                    checks_dismissed.append({'id': i['id'], 'by_user_id': g.user['id'], 'at': now,
                                             'by_page_approval': page})
        sidecar['revision'] = int(sidecar.get('revision', 0)) + 1
        _save_job_proposals(job_id, sidecar)

    # Outside the lock: _lp_record_confirmation takes it itself.
    for s in rejected:
        _lp_record_confirmation(job_id, 'rejected', page, s['bbox'],
                                extra={'by_page_approval': page})
    settled = len(box_items) + len(label_items) + len(check_items)
    _log_activity('page_approved', job_id, page=page, open_items=settled, dwell_ms=payload.get('dwell_ms'))
    return jsonify({'success': True, 'page': page, 'mode': 'active', 'settled': settled})


@app.route('/api/v1/documents/<job_id>/layout-prior/revert', methods=['POST'])
@login_required
@job_editor_required
def revert_layout_auto_applied(job_id):
    """Remove every box the prior added on its own that no one has since touched.

    The escape hatch for auto-apply: one call puts the document back to what
    the detector alone produced, without discarding any human work.
    """
    removed = []
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404
        sidecar = _lp_ensure_sidecar(job_id)

        keep = []
        for fn in result.get('results', []):
            if fn.get('source') == 'layout_prior' and not _lp_user_authored(fn):
                removed.append({'id': fn.get('id'), 'page': fn.get('page'), 'bbox': fn.get('bbox')})
            else:
                keep.append(fn)
        if removed:
            result['results'] = keep
            _save_job_result(job_id, result)
            removed_ids = {r['id'] for r in removed}
            sidecar['auto_applied'] = [a for a in sidecar['auto_applied']
                                       if a.get('footnote_id') not in removed_ids]
            sidecar['revision'] = int(sidecar.get('revision', 0)) + 1
            _save_job_proposals(job_id, sidecar)

    for item in removed:
        _log_activity('layout_auto_revert', job_id, footnote_id=item['id'],
                       page=item['page'], bbox=item['bbox'])
    return jsonify({'success': True, 'removed': len(removed),
                    'removed_ids': [r['id'] for r in removed]})


def _lp_auto_apply(job_id, actor_kwargs=None):
    """Promote high-scoring sub-threshold proposals into real footnotes.

    Runs only on demand from the viewer (apply_layout_prior), after a
    reviewer's confirmation with Layout Assist on, when their corrections have
    sharpened the prior — never at ingest.

    Adding is the only automatic action. Reshaping or deleting a box is
    deliberately left to a human via flags: the prior is fitted on the model's
    own output, so it can certify a systematic error, and silently reshaping
    every box in a book on the strength of that is not recoverable the way an
    extra box a reviewer can see and delete is.

    Never touches a locked page — one the user edited or dwelt on. Returns the
    footnotes added.
    """
    actor_kwargs = actor_kwargs or {}
    payload = _lp_compute(job_id)
    if not payload or payload.get('status') != 'active':
        return []

    # Only plain rescues. One that lands on an existing box would reshape that
    # box ('extends'/'blocked', see _lp_extend_target) — a person's call.
    rescues = [s for items in payload['suggestions'].values() for s in items
               if s['kind'] == 'rescue' and not s.get('extends') and not s.get('blocked')]
    if not rescues:
        return []

    pdf_path = _find_source_pdf(job_id)
    if pdf_path is None:
        logger.warning(f"Layout prior: no source PDF for {job_id}; skipping auto-apply")
        return []

    sidecar = _lp_ensure_sidecar(job_id)
    pages = sidecar.get('pages') or {}
    added = []

    for sugg in rescues:
        prop_id = sugg['id'][2:]
        page = sugg['page']
        meta = pages.get(str(page), {})
        clamped = layout_prior.clamp_to_page(
            (sugg['bbox']['x1'], sugg['bbox']['y1'], sugg['bbox']['x2'], sugg['bbox']['y2']),
            meta.get('w') or 10 ** 6, meta.get('h') or 10 ** 6)
        if clamped is None:
            continue
        bbox = {'x1': clamped[0], 'y1': clamped[1], 'x2': clamped[2], 'y2': clamped[3]}
        current = _get_job_result(job_id)
        if current is not None and _overlapping_footnote(current, page, bbox) is not None:
            # Would overlap a box already there; footnote boxes may not.
            continue

        try:
            ocr_start = time.perf_counter()
            engine = OCREngine.get_instance(_get_use_surya_ocr(job_id))
            text = engine.extract_text_for_pdf_bbox(str(pdf_path), page, bbox)
            ocr_elapsed = time.perf_counter() - ocr_start
        except Exception as e:
            logger.warning(f"Layout prior: OCR failed for {job_id} p{page}: {e}")
            continue
        if not (text or '').strip():
            # Geometry said footnote, the page says blank. Trust the page.
            continue

        # A box can hold several footnotes: cut it at its markers the way a
        # drawn box is (create_footnote / _region_pieces), one footnote each,
        # all sharing the box.
        pieces = []
        for piece in _region_pieces(text):
            ner_start = time.perf_counter()
            references, needs_classification = _classify_footnote_text(job_id, piece)
            ner_elapsed = time.perf_counter() - ner_start
            footnote = {
                'id': str(uuid.uuid4()),
                'page': page,
                'bbox': dict(bbox),
                'confidence': sugg['conf'],
                'text': piece,
                'references': references,
                'source': 'layout_prior',
                'edited': False,
                'auto_applied': True,
                'conf_adj': sugg['conf_adj'],
                'suggestion_reasons': sugg['reasons'],
                'needs_classification': needs_classification,
            }
            _recompute_footnote_summary(footnote)
            pieces.append((footnote, ner_elapsed))

        with _job_result_lock:
            result = _get_job_result(job_id)
            if result is None:
                break
            result.setdefault('results', []).extend(fn for fn, _ in pieces)
            _save_job_result(job_id, result)
            sidecar = _lp_ensure_sidecar(job_id)
            sidecar['auto_applied'].append({
                'proposal_id': prop_id, 'footnote_id': pieces[0][0]['id'],
                'footnote_ids': [fn['id'] for fn, _ in pieces], 'page': page,
                'conf': sugg['conf'], 'conf_adj': sugg['conf_adj'],
                'at': datetime.now(timezone.utc).isoformat(),
            })
            sidecar['revision'] = int(sidecar.get('revision', 0)) + 1
            _save_job_proposals(job_id, sidecar)

        for footnote, ner_elapsed in pieces:
            added.append(footnote)
            _log_activity('layout_auto_apply', job_id, footnote_id=footnote['id'], page=page,
                           bbox=bbox, confidence=sugg['conf'], conf_adj=sugg['conf_adj'],
                           proposal_id=prop_id, reasons=sugg['reasons'],
                           split_from_box=len(pieces) > 1, **actor_kwargs)
            # The added footnote's own OCR and NER output, logged like a drawn
            # box's (create_footnote) and tagged so it can be told apart from
            # the upload pass. The detector's original proposal stays in the
            # sidecar untouched, and the upload pass's predictions in their
            # own log lines.
            _log_prediction('prediction_ocr', job_id, _ocr_model_info(job_id), footnote_id=footnote['id'],
                             inference_time_seconds=round(ocr_elapsed, 4), origin='layout_assist',
                             **actor_kwargs)
            _log_reference_predictions(job_id, footnote['references'], footnote['id'],
                                       inference_time_seconds=round(ner_elapsed, 4),
                                       origin='layout_assist', **actor_kwargs)

    if added:
        logger.info(f"Layout prior: auto-applied {len(added)} footnote(s) to {job_id}")
    return added


@app.route('/api/v1/documents/<job_id>/layout-prior/apply', methods=['POST'])
@login_required
@job_editor_required
def apply_layout_prior(job_id):
    """Re-run auto-apply now that the user's corrections have sharpened the prior.

    Called by the viewer after each confirmation while Layout Assist is on,
    debounced, so what the reviewer confirms carries over to the pages they
    haven't approved yet. Approved (locked) pages are never touched — see
    _lp_locked_pages. Idempotent — a proposal already applied is recorded in
    the sidecar and never applied twice.
    """
    if _get_job_result(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404
    added = _lp_auto_apply(job_id)
    return jsonify({'success': True, 'added': len(added),
                    'footnotes': added,
                    'layout_prior': _lp_compute(job_id)})


# ---------------------------------------------------------------------------
# NER consistency prior
#
# The layout prior's sibling, for labels instead of boxes. Same discipline:
# derived on every request from the stored document, never cached, never
# written back, and never applied without a human clicking. See
# pipeline/ner_prior.py for the model and for why it keys on features rather
# than on span text.
# ---------------------------------------------------------------------------

def _np_revision(sidecar, result):
    """Concurrency token, counting components rather than footnotes.

    Separate from the layout prior's so an NER schema bump cannot deactivate
    the box prior, and vice versa.
    """
    ner = (sidecar or {}).get('ner') or {}
    n = sum(len(r.get('components') or [])
            for f in result.get('results', []) for r in (f.get('references') or []))
    return int(ner.get('revision', 0)) * 100000 + n


def _np_compute(job_id, result=None, sidecar=None, debug=False):
    """Derive the NER prior response for a job. Pure; writes nothing.

    Like the layout prior, available for every job and shown or hidden by the
    viewer's Layout Assist switch; a stored enable_ner_prior=false from the
    old upload option is ignored.
    """
    if result is None:
        result = _get_job_result(job_id)
    if result is None:
        return None
    if sidecar is None:
        sidecar = _get_job_proposals(job_id)

    ner_state = (sidecar or {}).get('ner') or {}
    if ner_state.get('version') not in (None, ner_prior.VERSION):
        return {'status': 'inactive', 'reason': 'unsupported_ner_version',
                'revision': 0, 'suggestions': {}, 'counts': {'flags': 0}}

    footnotes = result.get('results', [])
    # How much each label counts (pipeline/review_signals.py): one a person
    # set or kept on purpose above one on a page they approved, above one
    # they only looked at, above the model's own. A label settled as part of
    # approving its whole page counts as that page, not as a separate act.
    page_mode = {int(p): (v or {}).get('mode', 'passive')
                 for p, v in ((sidecar or {}).get('reviewed_pages') or {}).items()}
    kept = {d['id'][2:] for d in (ner_state.get('dismissed') or [])
            if d.get('id', '').startswith('c_') and not d.get('by_page_approval')}

    def weight_of(fn, ref, c):
        if c.get('source') == 'manual' or c.get('edited'):
            return review_signals.weight_for('labels', 'element_edited')
        if c.get('id') in kept:
            return review_signals.weight_for('labels', 'element_confirmed')
        page = fn.get('page')
        if page in page_mode:
            return review_signals.weight_for('labels', review_signals.page_signal(page_mode[page]))
        return review_signals.weight_for('labels', 'model')

    observations = ner_prior.collect_observations(footnotes, weight_of=weight_of)
    prior = ner_prior.build_prior(observations)
    revision = _np_revision(sidecar, result)
    hitl = _hitl_enabled(job_id)

    base = {'version': ner_prior.VERSION, 'revision': revision, 'hitl': hitl,
            'n_spans': len(observations)}

    if prior['status'] != 'active':
        return dict(base, status='inactive', reason=prior.get('reason'),
                    needed=prior.get('needed'), suggestions={},
                    counts={'flags': 0, 'footnotes_with_flags': 0})

    dismissed = {d['id'] for d in (ner_state.get('dismissed') or [])}
    # Bound suggestions to labels the picker actually offers: FootnoteMarker is
    # a model-only label, so proposing it would hand the reviewer a correction
    # they have no way to make.
    audit = ner_prior.audit_document(prior, observations, dismissed,
                                      allowed_labels=KNOWN_COMPONENT_LABELS)
    # ...and the same from the other side: a span stored under a label the
    # viewer never shows (FootnoteMarker) isn't something the reviewer can see
    # to judge, so it isn't flagged either. It still counts as evidence above.
    audit['flags'] = [f for f in audit['flags'] if f['stored_label'] in KNOWN_COMPONENT_LABELS]

    suggestions = {}
    for flag in audit['flags']:
        suggestions.setdefault(str(flag.get('footnote_id')), []).append({
            'id': flag['key'],
            'kind': 'label',
            'footnote_id': flag.get('footnote_id'),
            'reference_id': flag.get('reference_id'),
            'component_id': flag.get('component_id'),
            'page': flag.get('page'),
            'text': flag.get('text'),
            'stored_label': flag['stored_label'],
            'suggested_label': flag['suggested_label'],
            'margin': flag['margin'],
            'reasons': flag['reasons'],
        })

    # The interpretability surface: what the prior learned and how much it
    # trusts each piece of evidence. This is what lets someone notice a
    # book-wide systematic error the prior would otherwise happily confirm.
    profile = {
        'labels': prior['labels'],
        'spans_fitted': prior['n'],
        'references': prior['n_refs'],
        'reliability': prior['reliability'],
        'top_cells': _np_top_cells(prior),
    }

    payload = dict(base, status='active', reason=None, profile=profile,
                   suggestions=suggestions,
                   counts={'flags': len(audit['flags']),
                           'footnotes_with_flags': len(suggestions)},
                   learned={'enabled': hitl,
                            'corrections': len(ner_state.get('corrections') or []),
                            'dismissed': len(dismissed)})
    if debug:
        payload['debug'] = {'config': prior['config'],
                            'features': prior['features'],
                            'label_counts': prior['label_counts']}
    return payload


def _np_top_cells(prior, limit=12):
    """The document's most decisive evidence, in the form a person can check.

    Deliberately shows counts, not scores: "'after s.' is Pages in 47 of 47" is
    auditable; a log-odds contribution is not.
    """
    rows = []
    for feature in prior['features']:
        if prior['reliability'].get(feature, 0) <= 0:
            continue
        for cell, counts in prior['tables'][feature].items():
            total = sum(v for v in counts.values() if v > 0)
            if total < 5:
                continue
            label = max(counts, key=lambda y: counts[y])
            n = counts[label]
            if n <= 0:
                continue
            rows.append({'feature': feature,
                         'cell': cell.split('|', 1)[1] if '|' in cell else cell,
                         'label': label, 'n': int(n), 'total': int(total),
                         'purity': round(n / total, 3)})
    rows.sort(key=lambda r: (-r['purity'], -r['total']))
    return rows[:limit]


@app.route('/api/v1/documents/<job_id>/ner-prior')
@login_required
@job_access_required
def ner_prior_status(job_id):
    """Label suggestions from the document's own citation style.

    Recomputed per call like the layout prior — the inputs are a few thousand
    counts and the arithmetic is trivial, and deriving it means there is no
    stored state to invalidate when a label changes.
    """
    payload = _np_compute(job_id, debug=request.args.get('debug') in ('1', 'true'))
    if payload is None:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(payload)


@app.route('/api/v1/documents/<job_id>/ner-prior/suggestions/<suggestion_id>/dismiss', methods=['POST'])
@login_required
@job_editor_required
def dismiss_ner_suggestion(job_id, suggestion_id):
    """Dismiss one label suggestion: the reviewer says the stored label is right.

    Recorded in the sidecar's 'ner' section, which _np_compute already reads
    (dismissed ids are never flagged again). Accepting a suggestion needs no
    endpoint of its own — the viewer relabels through the ordinary component
    update, and the relabelled span then counts as a human decision.
    """
    if _get_job_result(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404
    with _job_result_lock:
        sidecar = _lp_ensure_sidecar(job_id)
        ner_state = sidecar.setdefault('ner', {})
        ner_state.setdefault('version', ner_prior.VERSION)
        dismissed = ner_state.setdefault('dismissed', [])
        if not any(d.get('id') == suggestion_id for d in dismissed):
            dismissed.append({'id': suggestion_id, 'by_user_id': g.user['id'],
                              'at': datetime.now(timezone.utc).isoformat()})
            ner_state['revision'] = int(ner_state.get('revision', 0)) + 1
            _save_job_proposals(job_id, sidecar)
    _log_activity('ner_suggestion_dismiss', job_id, suggestion_id=suggestion_id)
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Consistency checks (pipeline/consistency_checks.py): rules that hold for any
# book, shown by Layout Assist next to the priors' suggestions. Derived on
# every request like the priors; only dismissals are stored (sidecar
# 'checks'). Fixes go through the ordinary edit endpoints from the viewer.
# ---------------------------------------------------------------------------

def _span_templates_for(result, sidecar):
    """This book's span templates (pipeline/span_templates.py), learned from
    what its reviewers did: parts they edited or kept, and parts on pages they
    approved, weighted by review_signals. Returns (templates, page_mode)."""
    page_mode = {int(p): (v or {}).get('mode', 'passive')
                 for p, v in ((sidecar or {}).get('reviewed_pages') or {}).items()}
    kept = {d['id'][2:] for d in ((((sidecar or {}).get('ner') or {}).get('dismissed')) or [])
            if d.get('id', '').startswith('c_') and not d.get('by_page_approval')}

    def reviewer_weight(fn, comp):
        if comp.get('source') == 'manual' or comp.get('edited'):
            return review_signals.weight_for('labels', 'element_edited'), True
        if comp.get('id') in kept:
            return review_signals.weight_for('labels', 'element_confirmed'), True
        page = fn.get('page')
        if page in page_mode:
            return review_signals.weight_for('labels', review_signals.page_signal(page_mode[page])), False
        return 0.0, False

    templates = span_templates.learn(result.get('results', []), reviewer_weight, review_signals.SPAN_TEMPLATE)
    return templates, page_mode


def _span_changes_for(result, sidecar):
    """Parts whose span should change under the book's templates. Never parts
    a person edited, and never on approved pages: what's there is the
    reviewer's evidence, not something to correct."""
    templates, page_mode = _span_templates_for(result, sidecar)
    changes = span_templates.span_changes(
        result.get('results', []), templates,
        skip=lambda fn, c: (c.get('source') == 'manual' or c.get('edited')
                            or fn.get('page') in page_mode))
    return changes, templates


def _describe_templates(templates):
    """The learned ones, for the viewer: what reviewers taught this book."""
    out = []
    for key, t in templates.items():
        if not t['learned'] and not t['confirmed']:
            continue
        out.append({'key': list(key), 'inside': t['inside'], 'support': t['support'],
                    'contradicted': t['contradicted'], 'learned': t['learned'], 'confirmed': t['confirmed']})
    return sorted(out, key=lambda d: (-d['support'], d['key']))


def _check_locations(result):
    """Where each part/reference/footnote id sits, so a stored decision
    (whose item id ends in one of them) can show its page and text."""
    where = {}
    for fn in result.get('results', []):
        text = fn.get('text') or ''
        where[fn.get('id')] = {'page': fn.get('page'), 'footnote_id': fn.get('id')}
        for ref in fn.get('references') or []:
            where[ref.get('id')] = {'page': fn.get('page'), 'footnote_id': fn.get('id')}
            for comp in ref.get('components') or []:
                s, e = comp.get('start'), comp.get('end')
                where[comp.get('id')] = {'page': fn.get('page'), 'footnote_id': fn.get('id'), 'label': comp.get('label'),
                                         'text': text[s:e] if isinstance(s, int) and isinstance(e, int) else None}
    return where


def _check_decisions(result, sidecar):
    """What reviewers decided about each rule's items (consistency_checks.
    decision_summary): fixed by hand, kept ("Fine as is"), fixed
    automatically. Span boundaries are left out - span templates weigh those
    themselves."""
    checks = (sidecar or {}).get('checks') or {}
    where = _check_locations(result)

    def record(d):
        rule, _, target = d.get('id', '').partition(':')
        loc = where.get(target, {})
        return {'id': d['id'], 'rule': d.get('rule') or rule,
                'page': d.get('page', loc.get('page')), 'footnote_id': d.get('footnote_id', loc.get('footnote_id')),
                'text': d.get('text', loc.get('text')), 'label': d.get('label', loc.get('label'))}

    def records(key):
        # A check settled by approving its page is not a decision about the
        # rule ("Fine as is" is): left out, as span templates leave it out.
        return [record(d) for d in checks.get(key) or []
                if not d.get('id', '').startswith('span_boundary:') and not d.get('by_page_approval')]

    return consistency_checks.decision_summary(
        records('fixed'), records('dismissed'), records('auto_applied'), checks.get('rereviewed') or [],
        min_support=review_signals.SPAN_TEMPLATE['min_support'],
        max_contradiction_share=review_signals.SPAN_TEMPLATE['max_contradiction_share'])


def _checks_compute(job_id, result=None, sidecar=None):
    if result is None:
        result = _get_job_result(job_id)
    if result is None:
        return None
    if sidecar is None:
        sidecar = _get_job_proposals(job_id)
    dismissed = {d.get('id') for d in (((sidecar or {}).get('checks') or {}).get('dismissed') or [])}
    items = consistency_checks.label_checks(result.get('results', []))
    changes, templates = _span_changes_for(result, sidecar)
    for ch in changes:
        items.append({
            'id': 'span_boundary:%s' % ch['component_id'], 'area': 'labels', 'rule': 'span_boundary',
            'page': ch['page'], 'footnote_id': ch['footnote_id'], 'reference_id': ch['reference_id'],
            'component_id': ch['component_id'], 'text': ch['text'], 'label': ch['label'],
            'params': {'new_text': ch['new_text'], 'learned': ch['learned'], 'confirmed': ch['confirmed']},
            'fix': {'type': 'resize', 'start': ch['to'][0], 'end': ch['to'][1]},
        })
    items = [i for i in items if i['id'] not in dismissed]
    counts = {}
    for i in items:
        counts[i['rule']] = counts.get(i['rule'], 0) + 1
    rules = _check_decisions(result, sidecar)
    rereview = [rec for r in rules.values() for rec in r['rereview']]
    return {'items': items, 'counts': counts, 'dismissed': len(dismissed),
            'templates': _describe_templates(templates),
            'rules': {rule: {k: v for k, v in r.items() if k != 'rereview'} for rule, r in rules.items()},
            'min_support': review_signals.SPAN_TEMPLATE['min_support'],
            'rereview': sorted(rereview, key=lambda rec: (rec['rule'], rec.get('page') or 0))}


@app.route('/api/v1/documents/<job_id>/span-templates/apply', methods=['POST'])
@login_required
@job_editor_required
def apply_span_templates(job_id):
    """Carry the reviewer's span rules over to pages they haven't approved.

    Called by the viewer after a label edit or a page approval, while Layout
    Assist is on. Applies only changes every template behind which is
    confirmed (review_signals.SPAN_TEMPLATE: enough agreeing reviewer actions,
    none contradicting); never on approved pages, never to a part a person
    edited, never on its own from the principle alone. Each change keeps the
    part's label, clips its names to the new span, is logged, and is kept in
    the sidecar so revert_span_templates can put it back.
    """
    if _get_job_result(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404
    if not _assist_on():
        return jsonify({'success': True, 'changed': [], 'disabled': True})
    now = datetime.now(timezone.utc).isoformat()
    changed_fns, applied = {}, []
    with _job_result_lock:
        result = _get_job_result(job_id)
        sidecar = _lp_ensure_sidecar(job_id)
        changes, _t = _span_changes_for(result, sidecar)
        by_fn = {fn.get('id'): fn for fn in result.get('results', [])}
        for ch in changes:
            if not ch['confirmed']:
                continue
            fn = by_fn.get(ch['footnote_id'])
            ref = next((r for r in (fn or {}).get('references') or [] if r.get('id') == ch['reference_id']), None)
            comp = next((c for c in (ref or {}).get('components') or [] if c.get('id') == ch['component_id']), None)
            if comp is None or (comp['start'], comp['end']) != tuple(ch['from']):
                continue
            text = fn.get('text') or ''
            start, end = ch['to']
            comp['start'], comp['end'] = start, end
            comp['text'] = text[start:end]
            names = _clip_children(comp.get('names'), start, end, text)
            if names:
                comp['names'] = names
            else:
                comp.pop('names', None)
            comp['auto_span'] = {'from': list(ch['from']), 'templates': ch['templates'], 'at': now}
            _recompute_footnote_summary(fn)
            changed_fns[fn['id']] = fn
            applied.append({'component_id': comp['id'], 'footnote_id': fn['id'], 'reference_id': ref.get('id'),
                            'page': fn.get('page'), 'from': list(ch['from']), 'to': [start, end],
                            'templates': ch['templates'], 'at': now})
        if applied:
            _save_job_result(job_id, result)
            sidecar.setdefault('span_auto_applied', []).extend(applied)
            _save_job_proposals(job_id, sidecar)
    for a in applied:
        _log_activity('span_auto_apply', job_id, component_id=a['component_id'], footnote_id=a['footnote_id'],
                      page=a['page'], old_span=a['from'], new_span=a['to'], templates=a['templates'])
    return jsonify({'success': True, 'changed': [a['component_id'] for a in applied],
                    'pages': sorted({a['page'] for a in applied if a['page'] is not None}),
                    'footnotes': list(changed_fns.values())})


@app.route('/api/v1/documents/<job_id>/span-templates/revert', methods=['POST'])
@login_required
@job_editor_required
def revert_span_templates(job_id):
    """Put parts that apply_span_templates changed back as they were — the
    given component ids, or all of them. Skips a part someone has edited since
    or whose span has moved on."""
    ids = set((request.get_json(silent=True) or {}).get('component_ids') or [])
    changed_fns, reverted = {}, []
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404
        sidecar = _lp_ensure_sidecar(job_id)
        by_fn = {fn.get('id'): fn for fn in result.get('results', [])}
        keep = []
        for a in sidecar.get('span_auto_applied', []):
            if ids and a['component_id'] not in ids:
                keep.append(a)
                continue
            fn = by_fn.get(a['footnote_id'])
            ref = next((r for r in (fn or {}).get('references') or [] if r.get('id') == a['reference_id']), None)
            comp = next((c for c in (ref or {}).get('components') or [] if c.get('id') == a['component_id']), None)
            if comp is None or comp.get('edited') or [comp['start'], comp['end']] != a['to']:
                continue
            text = fn.get('text') or ''
            comp['start'], comp['end'] = a['from']
            comp['text'] = text[comp['start']:comp['end']]
            comp.pop('auto_span', None)
            _recompute_footnote_summary(fn)
            changed_fns[fn['id']] = fn
            reverted.append(a)
        if reverted:
            _save_job_result(job_id, result)
            sidecar['span_auto_applied'] = keep
            _save_job_proposals(job_id, sidecar)
    for a in reverted:
        _log_activity('span_auto_revert', job_id, component_id=a['component_id'], footnote_id=a['footnote_id'],
                      page=a['page'], old_span=a['to'], new_span=a['from'])
    return jsonify({'success': True, 'reverted': [a['component_id'] for a in reverted],
                    'footnotes': list(changed_fns.values())})


@app.route('/api/v1/documents/<job_id>/checks')
@login_required
@job_access_required
def document_checks(job_id):
    """Consistency-check items for the whole book (see _checks_compute)."""
    payload = _checks_compute(job_id)
    if payload is None:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(payload)


@app.route('/api/v1/documents/<job_id>/checks/<path:item_id>/dismiss', methods=['POST'])
@login_required
@job_editor_required
def dismiss_check(job_id, item_id):
    """"Fine as is": the reviewer says this check doesn't apply here."""
    if _get_job_result(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404
    with _job_result_lock:
        sidecar = _lp_ensure_sidecar(job_id)
        dismissed = sidecar.setdefault('checks', {}).setdefault('dismissed', [])
        if not any(d.get('id') == item_id for d in dismissed):
            dismissed.append(dict(_check_record_fields(), id=item_id, rule=item_id.split(':', 1)[0],
                                  by_user_id=g.user['id'], at=datetime.now(timezone.utc).isoformat()))
            _save_job_proposals(job_id, sidecar)
    _log_activity('check_dismissed', job_id, item_id=item_id, rule=item_id.split(':', 1)[0])
    return jsonify({'success': True})


@app.route('/api/v1/documents/<job_id>/checks/<path:item_id>/fixed', methods=['POST'])
@login_required
@job_editor_required
def fixed_check(job_id, item_id):
    """The reviewer applied a check's fix (the fix itself went through the
    ordinary edit endpoints). Stored per rule: enough agreeing fixes confirm
    the rule, and apply_check_fixes then fixes its other items on its own."""
    if _get_job_result(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404
    with _job_result_lock:
        sidecar = _lp_ensure_sidecar(job_id)
        fixed = sidecar.setdefault('checks', {}).setdefault('fixed', [])
        if not any(d.get('id') == item_id for d in fixed):
            fixed.append(dict(_check_record_fields(), id=item_id, rule=item_id.split(':', 1)[0],
                              by_user_id=g.user['id'], at=datetime.now(timezone.utc).isoformat()))
            _save_job_proposals(job_id, sidecar)
    _log_activity('check_fixed', job_id, item_id=item_id, rule=item_id.split(':', 1)[0])
    return jsonify({'success': True})


def _check_record_fields():
    """Where a decided item was, as the viewer sends it (page, footnote,
    part, text, label): its part may be gone once the fix is applied."""
    payload = request.get_json(silent=True) or {}
    return {k: payload[k] for k in ('page', 'footnote_id', 'reference_id', 'component_id', 'text', 'label')
            if k in payload}


@app.route('/api/v1/documents/<job_id>/checks/apply', methods=['POST'])
@login_required
@job_editor_required
def apply_check_fixes(job_id):
    """Fix the remaining items of every confirmed rule (_check_decisions).

    Called by the viewer after a check is fixed or a label edited, while
    Layout Assist is on. Like apply_span_templates: never on approved pages,
    never to a part a person made or edited, and only fixes that change one
    part (consistency_checks.AUTO_FIX_TYPES). Parts it creates are marked
    source 'auto_check', and every change is kept in the sidecar so
    revert_check_fixes can put it back and an inconsistent rule can list it
    for re-review.
    """
    if _get_job_result(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404
    if not _assist_on():
        return jsonify({'success': True, 'changed': [], 'disabled': True})
    now = datetime.now(timezone.utc).isoformat()
    changed_fns, applied = {}, []
    with _job_result_lock:
        result = _get_job_result(job_id)
        sidecar = _lp_ensure_sidecar(job_id)
        payload = _checks_compute(job_id, result, sidecar)
        approved = {int(p) for p in (sidecar.get('reviewed_pages') or {})}
        by_fn = {fn.get('id'): fn for fn in result.get('results', [])}
        for item in payload['items']:
            fix, rule = item.get('fix'), item['rule']
            if (rule == 'span_boundary' or not fix or fix.get('type') not in consistency_checks.AUTO_FIX_TYPES
                    or not (payload['rules'].get(rule) or {}).get('confirmed') or item.get('page') in approved):
                continue
            fn = by_fn.get(item['footnote_id'])
            ref = next((r for r in (fn or {}).get('references') or [] if r.get('id') == item['reference_id']), None)
            comp = next((c for c in (ref or {}).get('components') or [] if c.get('id') == item['component_id']), None)
            if comp is None or comp.get('source') == 'manual' or comp.get('edited'):
                continue
            text = fn.get('text') or ''
            before, created = copy.deepcopy(comp), []
            if fix['type'] == 'relabel':
                comp['label'] = fix['label']
                if fix['label'] not in consistency_checks.PERSON_LABELS:
                    comp.pop('names', None)
            else:
                first = fix['parts'][0] if fix['type'] == 'split' else dict(fix, label=comp['label'])
                comp['start'], comp['end'], comp['label'] = first['start'], first['end'], first['label']
                comp['text'] = text[first['start']:first['end']]
                names = _clip_children(comp.get('names'), first['start'], first['end'], text)
                if names:
                    comp['names'] = names
                else:
                    comp.pop('names', None)
                for part in fix.get('parts', [])[1:]:
                    new = {'id': str(uuid.uuid4()), 'label': part['label'], 'text': text[part['start']:part['end']],
                           'confidence': None, 'start': part['start'], 'end': part['end'], 'source': 'auto_check'}
                    _insert_sorted_by_start(ref.setdefault('components', []), new)
                    created.append(new['id'])
            comp['auto_check'] = {'rule': rule, 'at': now}
            _recompute_footnote_summary(fn)
            changed_fns[fn['id']] = fn
            applied.append({'id': item['id'], 'rule': rule, 'page': fn.get('page'), 'footnote_id': fn['id'],
                            'reference_id': ref.get('id'), 'component_id': comp['id'], 'text': item.get('text'),
                            'label': item.get('label'), 'before': before, 'created': created,
                            'after': {'start': comp['start'], 'end': comp['end'], 'label': comp['label']}, 'at': now})
        if applied:
            _save_job_result(job_id, result)
            sidecar.setdefault('checks', {}).setdefault('auto_applied', []).extend(applied)
            _save_job_proposals(job_id, sidecar)
    for a in applied:
        _log_activity('check_auto_apply', job_id, item_id=a['id'], rule=a['rule'], component_id=a['component_id'],
                      footnote_id=a['footnote_id'], page=a['page'], created=a['created'])
    return jsonify({'success': True, 'changed': [a['id'] for a in applied],
                    'rules': sorted({a['rule'] for a in applied}),
                    'pages': sorted({a['page'] for a in applied if a['page'] is not None}),
                    'footnotes': list(changed_fns.values())})


@app.route('/api/v1/documents/<job_id>/checks/revert', methods=['POST'])
@login_required
@job_editor_required
def revert_check_fixes(job_id):
    """Put parts that apply_check_fixes changed back as they were - the given
    item ids, or all of them - and remove the parts it created. Skips a part
    someone has edited since; that record stays."""
    ids = set((request.get_json(silent=True) or {}).get('ids') or [])
    changed_fns, reverted = {}, []
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404
        sidecar = _lp_ensure_sidecar(job_id)
        checks = sidecar.setdefault('checks', {})
        by_fn = {fn.get('id'): fn for fn in result.get('results', [])}
        keep = []
        for a in checks.get('auto_applied', []):
            fn = by_fn.get(a['footnote_id'])
            ref = next((r for r in (fn or {}).get('references') or [] if r.get('id') == a['reference_id']), None)
            comp = next((c for c in (ref or {}).get('components') or [] if c.get('id') == a['component_id']), None)
            if ((ids and a['id'] not in ids) or comp is None or comp.get('edited')
                    or {k: comp.get(k) for k in ('start', 'end', 'label')} != a['after']):
                keep.append(a)
                continue
            comp.clear()
            comp.update(copy.deepcopy(a['before']))
            ref['components'] = [c for c in ref.get('components') or []
                                 if c.get('id') not in a['created'] or c.get('edited') or c.get('source') == 'manual']
            _recompute_footnote_summary(fn)
            changed_fns[fn['id']] = fn
            reverted.append(a)
        if reverted:
            _save_job_result(job_id, result)
            checks['auto_applied'] = keep
            _save_job_proposals(job_id, sidecar)
    for a in reverted:
        _log_activity('check_auto_revert', job_id, item_id=a['id'], rule=a['rule'], component_id=a['component_id'],
                      footnote_id=a['footnote_id'], page=a['page'])
    return jsonify({'success': True, 'reverted': [a['id'] for a in reverted],
                    'footnotes': list(changed_fns.values())})


@app.route('/api/v1/documents/<job_id>/checks/rereviewed', methods=['POST'])
@login_required
@job_editor_required
def rereviewed_checks(job_id):
    """The reviewer looked again at decisions an inconsistent rule listed for
    re-review (keys from the checks payload's 'rereview')."""
    keys = [k for k in (request.get_json(silent=True) or {}).get('keys') or [] if isinstance(k, str)]
    if _get_job_result(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404
    with _job_result_lock:
        sidecar = _lp_ensure_sidecar(job_id)
        done = sidecar.setdefault('checks', {}).setdefault('rereviewed', [])
        new = [k for k in keys if k not in done]
        if new:
            done.extend(new)
            _save_job_proposals(job_id, sidecar)
    for k in new:
        _log_activity('check_rereviewed', job_id, key=k)
    return jsonify({'success': True})


@app.route('/api/v1/documents/<job_id>/footnotes', methods=['POST'])
@login_required
@job_editor_required
def create_footnote(job_id):
    """Create a new footnote from a manually drawn bounding box and OCR it.

    Runs OCR using whichever engine (Surya vs. text-layer) was selected for
    this job at upload time — there is no per-edit engine picker. The OCR'd
    text is then automatically run through the reference/component NER
    cascade (same models as the bulk pipeline), so the new footnote comes in
    fully classified rather than needing manual reference/component tagging.
    """
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    payload = request.get_json(silent=True) or {}
    page = payload.get('page')
    bbox = payload.get('bbox')

    if not isinstance(page, int) or page < 1:
        return jsonify({'error': 'page must be a positive integer'}), 400

    bbox_error = _validate_bbox(bbox)
    if bbox_error:
        return jsonify({'error': bbox_error}), 400

    other = _overlapping_footnote(result, page, bbox)
    if other is not None:
        return _overlap_error(page, other)

    pdf_path = _find_source_pdf(job_id)
    if pdf_path is None:
        return jsonify({'error': 'Source PDF file not found; cannot run OCR'}), 404

    clean_bbox = {'x1': float(bbox['x1']), 'y1': float(bbox['y1']), 'x2': float(bbox['x2']), 'y2': float(bbox['y2'])}

    # Allow-listed so a caller can't invent provenance: 'manual' is someone
    # drawing a box, 'layout_prior_accepted' is someone accepting a rescue the
    # prior suggested. Both are human decisions, so both count as user-authored.
    source = payload.get('source') if payload.get('source') in ('manual', 'layout_prior_accepted') else 'manual'

    try:
        engine = OCREngine.get_instance(_get_use_surya_ocr(job_id))
        ocr_call_start = time.perf_counter()
        text = engine.extract_text_for_pdf_bbox(str(pdf_path), page, clean_bbox)
        ocr_elapsed = time.perf_counter() - ocr_call_start
    except Exception as e:
        logger.error(f"OCR failed while creating footnote for job {job_id}: {e}")
        return jsonify({'error': f'OCR failed: {e}'}), 500

    # A box drawn over several footnotes is cut at their markers, exactly as
    # the pipeline cuts a detected region, into footnotes sharing the box.
    # Shift-accepting a newly drawn box still OCRs (and so still splits) it — a
    # footnote with no text would be nothing at all — but leaves the pieces
    # untagged for manual marking, rather than seeding them with the
    # classifier's guesses.
    cascade = _cascade_requested(payload)
    footnotes = []
    for piece in _region_pieces(text):
        if cascade:
            ner_call_start = time.perf_counter()
            references, needs_classification = _classify_footnote_text(job_id, piece)
            ner_elapsed = time.perf_counter() - ner_call_start
        else:
            references, needs_classification, ner_elapsed = [], True, None
        footnote = {
            'id': str(uuid.uuid4()),
            'page': page,
            'bbox': dict(clean_bbox),
            'confidence': None,
            'text': piece,
            'references': references,
            'source': source,
            'edited': True,
            'needs_classification': needs_classification
        }
        _recompute_footnote_summary(footnote)
        footnotes.append((footnote, ner_elapsed))

    with _job_result_lock:
        result = _get_job_result(job_id)
        result.setdefault('results', []).extend(footnote for footnote, _ in footnotes)
        _save_job_result(job_id, result)

    for footnote, ner_elapsed in footnotes:
        _log_activity('bbox_create', job_id, footnote_id=footnote['id'], page=page, bbox=clean_bbox,
                       text=footnote['text'], confidence=None, source=source,
                       suggestion_id=payload.get('suggestion_id'), split_from_box=len(footnotes) > 1)
        if ner_elapsed is not None:
            _log_reference_predictions(job_id, footnote['references'], footnote['id'],
                                       inference_time_seconds=round(ner_elapsed, 4))
    first = footnotes[0][0]
    _lp_record_confirmation(job_id, 'created', page, clean_bbox, footnote_id=first['id'])
    _log_prediction('prediction_ocr', job_id, _ocr_model_info(job_id), footnote_id=first['id'],
                     inference_time_seconds=round(ocr_elapsed, 4))

    # The first footnote, as before, plus every footnote the box became in
    # `created` (the first included) so the viewer can add them all.
    return jsonify(dict(first, created=[footnote for footnote, _ in footnotes])), 201


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>', methods=['PUT'])
@login_required
@job_editor_required
def update_footnote(job_id, footnote_id):
    """Resize/reshape an existing footnote's bbox and re-run OCR on the new region."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = next((f for f in result.get('results', []) if f.get('id') == footnote_id), None)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    if _validate_bbox(footnote.get('bbox')):
        return jsonify({'error': 'This footnote has no bbox and cannot be edited'}), 422

    payload = request.get_json(silent=True) or {}
    bbox = payload.get('bbox')
    bbox_error = _validate_bbox(bbox)
    if bbox_error:
        return jsonify({'error': bbox_error}), 400

    # Which rectangle of this footnote is being resized. A footnote running over
    # a page break occupies one per page; without this the caller could only
    # ever address the first, and resizing the tail would write the tail's
    # coordinates into the head's rectangle on a different page.
    spans = _footnote_spans(footnote)
    span_index = payload.get('span_index', 0)
    if not isinstance(span_index, int) or isinstance(span_index, bool) or not (0 <= span_index < len(spans)):
        return jsonify({'error': f'span_index must be an integer in 0..{len(spans) - 1}'}), 400

    pdf_path = _find_source_pdf(job_id)
    if pdf_path is None:
        return jsonify({'error': 'Source PDF file not found; cannot run OCR'}), 404

    clean_bbox = {'x1': float(bbox['x1']), 'y1': float(bbox['y1']), 'x2': float(bbox['x2']), 'y2': float(bbox['y2'])}
    page = spans[span_index].get('page') or footnote.get('page')

    old_box = spans[span_index].get('bbox')
    other = _overlapping_footnote(result, page, clean_bbox, ignore_ids={footnote_id},
                                  region=None if _validate_bbox(old_box) else old_box)
    if other is not None:
        return _overlap_error(page, other)

    # The viewer's tick fires this unconditionally, with no dirty check, so
    # clicking a box and immediately accepting it used to re-OCR the identical
    # region and overwrite footnote['references'] wholesale — throwing away any
    # reference/component/name spans the user had hand-corrected. An unchanged
    # box is a confirmation, not an edit: record it and change nothing.
    existing = spans[span_index].get('bbox') or {}
    if all(abs(float(existing.get(k, 1e9)) - clean_bbox[k]) <= 0.5 for k in ('x1', 'y1', 'x2', 'y2')):
        _log_activity('bbox_confirm', job_id, footnote_id=footnote_id, page=page, bbox=clean_bbox)
        # Someone opened this box and accepted it as-is. That is a real
        # positive, and the only one the UI produces without an edit.
        _lp_record_confirmation(job_id, 'confirmed', page, clean_bbox, footnote_id=footnote_id)
        return jsonify(footnote)

    updated_spans = [dict(sp) for sp in spans]
    updated_spans[span_index] = {'page': page, 'bbox': clean_bbox}

    # Shift-accepting a box moves the rectangle and stops there. Both stages
    # below it are skipped, not just the NER one: re-OCR rewrites the
    # footnote's text, and every reference/component/name offset is absolute
    # into that text, so keeping the tags while replacing the text underneath
    # them would leave them all pointing at the wrong characters. Geometry-only
    # is the one version of this edit that can preserve the tags at all.
    cascade = _cascade_requested(payload)
    ocr_elapsed = ner_elapsed = None
    if cascade:
        # Re-OCR every rectangle, not just the edited one, and rejoin in page
        # order: a multi-page footnote's text is the concatenation of its
        # spans, so reading back only the edited span would drop the rest of
        # the footnote.
        try:
            engine = OCREngine.get_instance(_get_use_surya_ocr(job_id))
            ocr_call_start = time.perf_counter()
            parts = [engine.extract_text_for_pdf_bbox(str(pdf_path), sp.get('page'), sp.get('bbox'))
                     for sp in updated_spans if sp.get('page') and sp.get('bbox')]
            text = ' '.join(part.strip() for part in parts if part and part.strip())

            # A re-read box holding several footnotes is cut at their markers
            # like a new one (see create_footnote): this footnote keeps the
            # piece with its own number (the first piece if none has it), the
            # rest become new footnotes sharing the box. Not for a footnote
            # running over a page break, whose text is several boxes joined.
            chunks = (_region_chunks(text) if len(updated_spans) == 1
                      else [{'text': text, 'marker': True, 'number': None}])
            own = _printed_number(footnote.get('text'))
            keep = next((i for i, c in enumerate(chunks) if own is not None and c['number'] == own), 0)

            # A footnote sharing the box that runs over a page break is replaced
            # by its piece as well, keeping its rectangles on the other pages
            # (it was left behind, old box and all). One that
            # begins here keeps the next page only while its piece still reads
            # unfinished: when the old box had cut off its last line, the new
            # box has it, and the "continuation" that cut gave it goes.
            stand_ins = {}
            if len(chunks) > 1 and not _validate_bbox(old_box):
                for f, at, k in _continued_in_box(result, footnote_id, page, old_box, chunks, keep):
                    f_spans = [dict(sp) for sp in _footnote_spans(f)]
                    f_spans[k] = {'page': page, 'bbox': dict(clean_bbox)}
                    if k == 0 and not _OPEN_ENDING.search(chunks[at]['text']):
                        f_spans = f_spans[:1]
                    number = _printed_number(f.get('text'))
                    kept = []
                    for i, sp in enumerate(f_spans):
                        if i == k:
                            kept.append((sp, chunks[at]['text']))
                        elif sp.get('page') and not _validate_bbox(sp.get('bbox')):
                            part = _own_part(engine.extract_text_for_pdf_bbox(str(pdf_path), sp['page'], sp['bbox']),
                                             number, i == 0)
                            if part.strip():
                                kept.append((sp, part))
                    stand_ins[at] = (f['id'], [sp for sp, _ in kept], ' '.join(p.strip() for _, p in kept))
            ocr_elapsed = time.perf_counter() - ocr_call_start
        except Exception as e:
            logger.error(f"OCR failed while updating footnote {footnote_id} for job {job_id}: {e}")
            return jsonify({'error': f'OCR failed: {e}'}), 500

        text = chunks[keep]['text']
        ner_call_start = time.perf_counter()
        references, needs_classification = _classify_footnote_text(job_id, text)
        extra = []
        replaces = {}                       # id of a footnote running over a page break -> its stand-in
        for i, chunk in enumerate(chunks):
            if i == keep:
                continue
            replaced_id, f_spans, piece = stand_ins.get(i, (None, None, chunk['text']))
            piece_refs, piece_needs = _classify_footnote_text(job_id, piece)
            new = {
                'id': str(uuid.uuid4()),
                'page': page,
                'bbox': dict(clean_bbox),
                'confidence': None,
                'text': piece,
                'references': piece_refs,
                'source': 'manual',
                'edited': True,
                'needs_classification': piece_needs,
            }
            if f_spans:
                new.update(spans=f_spans, page=f_spans[0]['page'], bbox=dict(f_spans[0]['bbox']))
                replaces[replaced_id] = new
            _recompute_footnote_summary(new)
            extra.append(new)
        ner_elapsed = time.perf_counter() - ner_call_start
    else:
        text = footnote.get('text')
        extra = []
        keep = 0
        replaces = {}

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnote = next((f for f in result.get('results', []) if f.get('id') == footnote_id), None)
        if footnote is None:
            return jsonify({'error': 'Footnote not found'}), 404

        old_bbox = _footnote_spans(footnote)[span_index].get('bbox')
        old_text = footnote.get('text')
        if 'original_bbox' not in footnote:
            footnote['original_bbox'] = footnote.get('bbox')
        footnote['spans'] = updated_spans
        # page/bbox mirror the first span, so anything reading only those keeps
        # working exactly as before.
        footnote['bbox'] = updated_spans[0]['bbox']
        footnote['page'] = updated_spans[0]['page']
        footnote['edited'] = True
        if cascade:
            footnote['text'] = text
            footnote['needs_classification'] = needs_classification
            footnote['references'] = references
            _recompute_footnote_summary(footnote)
        removed = []
        if extra:
            # The box was re-read and cut up whole, so footnotes that shared
            # its old box are replaced by the new pieces rather than left
            # beside them repeating the same text.
            # A footnote running over a page break gives way to its stand-in in
            # its own place; the other pieces go before or after this footnote
            # as their markers stood in the box.
            footnotes = result['results']
            if not _validate_bbox(old_bbox):
                removed = [f for f in footnotes
                           if f.get('id') != footnote_id
                           and ((len(_footnote_spans(f)) == 1
                                 and f.get('page') == page and not _validate_bbox(f.get('bbox'))
                                 and _same_region(f['bbox'], old_bbox))
                                or f.get('id') in replaces)]
            removed_ids = {f.get('id') for f in removed}
            in_place = {replaces[i]['id'] for i in removed_ids if i in replaces}
            placed = []
            for f in footnotes:
                fid = f.get('id')
                if fid in removed_ids:
                    if fid in replaces:
                        placed.append(replaces[fid])
                    continue
                if fid == footnote_id:
                    placed.extend(n for n in extra[:keep] if n['id'] not in in_place)
                    placed.append(f)
                    placed.extend(n for n in extra[keep:] if n['id'] not in in_place)
                else:
                    placed.append(f)
            result['results'] = placed
        _save_job_result(job_id, result)

    _log_activity('bbox_update', job_id, footnote_id=footnote_id, page=page, bbox=clean_bbox,
                   old_bbox=old_bbox, old_text=old_text, new_text=text, cascaded=cascade,
                   split_into=[f['id'] for f in extra], replaced_ids=[f.get('id') for f in removed])
    # Carries old_bbox too: the (model box -> human box) pair is the single
    # most informative record available, and what _lp_correction_bias reads.
    _lp_record_confirmation(job_id, 'corrected', page, clean_bbox,
                             old_bbox=old_bbox, footnote_id=footnote_id)
    if ocr_elapsed is not None:
        _log_prediction('prediction_ocr', job_id, _ocr_model_info(job_id), footnote_id=footnote_id,
                         inference_time_seconds=round(ocr_elapsed, 4))
    if ner_elapsed is not None:
        _log_reference_predictions(job_id, references, footnote_id, inference_time_seconds=round(ner_elapsed, 4))
        for new in extra:
            _log_reference_predictions(job_id, new['references'], new['id'])

    if extra:
        # The footnote as before, plus what the viewer needs to show and undo
        # the split: the new pieces, and the footnotes they replaced.
        return jsonify(dict(footnote, created=extra, removed=removed)), 200
    return jsonify(footnote), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>', methods=['DELETE'])
@login_required
@job_editor_required
def delete_footnote(job_id, footnote_id):
    """Delete a footnote (e.g. a false-positive OD box, or a rejected edit)."""
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404

        footnotes = result.get('results', [])
        deleted = next((f for f in footnotes if f.get('id') == footnote_id), None)
        if deleted is None:
            return jsonify({'error': 'Footnote not found'}), 404

        result['results'] = [f for f in footnotes if f.get('id') != footnote_id]
        _save_job_result(job_id, result)

    _log_activity('bbox_delete', job_id, footnote_id=footnote_id, page=deleted.get('page'), bbox=deleted.get('bbox'),
                   text=deleted.get('text'), confidence=deleted.get('confidence'))
    # A deletion is the only negative example a human ever gives us, and the
    # prior is otherwise one-class - it knows what a footnote looks like and
    # nothing about what a false positive looks like.
    _lp_record_confirmation(job_id, 'rejected', deleted.get('page'), deleted.get('bbox'),
                             footnote_id=footnote_id)

    return jsonify({'success': True, 'deleted_id': footnote_id})


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/restore', methods=['POST'])
@login_required
@job_editor_required
def restore_footnote(job_id, footnote_id):
    """Upsert a footnote exactly as given (same id, bbox, text, and any
    reference/component/name edits layered on top of it), keyed by the id in
    the request body which must match the URL: replaces it in place if a
    footnote with that id currently exists (undoing/redoing an in-place bbox
    resize), otherwise re-inserts it (undoing a delete, or redoing a create).
    Used by the interactive viewer's undo/redo to reverse a footnote
    create/update/delete without re-running OCR or reclassifying — unlike
    create_footnote/update_footnote, which always OCR the region fresh and
    would silently discard any manual reference edits made since."""
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get('id') != footnote_id:
            return jsonify({'error': "Body must be a footnote object whose 'id' matches the URL"}), 400
        if not isinstance(payload.get('page'), int) or payload['page'] < 1:
            return jsonify({'error': 'Body must include a valid positive integer page'}), 400
        bbox_error = _validate_bbox(payload.get('bbox'))
        if bbox_error:
            return jsonify({'error': bbox_error}), 400

        footnotes = result.setdefault('results', [])
        existing_idx = next((i for i, f in enumerate(footnotes) if f.get('id') == footnote_id), None)
        replaced = footnotes[existing_idx] if existing_idx is not None else None
        if existing_idx is not None:
            footnotes[existing_idx] = payload
        else:
            footnotes.append(payload)
        _save_job_result(job_id, result)

    _log_activity('bbox_restore', job_id, footnote_id=footnote_id, page=payload.get('page'), bbox=payload.get('bbox'),
                   text=payload.get('text'), replaced_text=replaced.get('text') if replaced else None)

    return jsonify(payload), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/text', methods=['PUT'])
@login_required
@job_editor_required
def update_footnote_text(job_id, footnote_id):
    """Manually correct a footnote's OCR'd text in place — unlike
    update_footnote (bbox resize -> fresh OCR), this takes the corrected text
    directly and never touches bbox/page/OCR. By default re-runs the
    reference+component NER cascade on the corrected text (same as every
    other text-changing edit in this app); cascade=false carries the tags the
    footnote already has across the edit instead, re-aligned onto the new text
    (see _remap_annotations). reclassify=false is the older spelling of the
    same flag, kept so existing API callers don't break — the viewer sends
    cascade, the one flag every other edit route now uses.

    Not re-running the model used to mean losing the tags outright, on the
    reasoning that every offset is absolute into the text being replaced. But
    a manual OCR correction is almost always local — a letter, a stray space
    — and throwing away a whole footnote's tagging over one character is the
    opposite of what "don't re-run anything" is asked for. The offsets are
    diffed across instead, and only spans whose text the edit actually
    removed are dropped."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = _find_footnote(result, footnote_id)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    payload = request.get_json(silent=True) or {}
    text = payload.get('text')
    if not isinstance(text, str) or not text.strip():
        return jsonify({'error': 'text must be a non-empty string'}), 400
    text = text.strip()
    reclassify = _cascade_requested(payload) and payload.get('reclassify', True)

    ner_elapsed = None
    if reclassify:
        ner_call_start = time.perf_counter()
        references, needs_classification = _classify_footnote_text(job_id, text)
        ner_elapsed = time.perf_counter() - ner_call_start

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnote = _find_footnote(result, footnote_id)
        if footnote is None:
            return jsonify({'error': 'Footnote not found'}), 404

        old_text = footnote.get('text')
        if not reclassify:
            # Done here rather than above so the diff is against the text as
            # it stands right now, under the same lock that replaces it.
            references = _remap_annotations(footnote.get('references'), old_text or '', text)
            needs_classification = footnote.get('needs_classification', False)
        footnote['text'] = text
        footnote['edited'] = True
        footnote['needs_classification'] = needs_classification
        footnote['references'] = references
        _recompute_footnote_summary(footnote)
        _save_job_result(job_id, result)

    _log_activity('text_edit', job_id, footnote_id=footnote_id, page=footnote.get('page'), bbox=footnote.get('bbox'),
                   old_text=old_text, new_text=text, reclassified=reclassify)
    if reclassify:
        _log_reference_predictions(job_id, references, footnote_id, inference_time_seconds=round(ner_elapsed, 4))

    return jsonify(footnote), 200


@app.route('/api/v1/documents/<job_id>/footnotes/classify-preview', methods=['POST'])
@login_required
@job_editor_required
def classify_footnote_preview(job_id):
    """Run the NER cascade on arbitrary text and return the result — WITHOUT
    touching the job's stored results at all (no lock, no save, no id).
    Exists for the interactive viewer's footnote-edit modal: splitting or
    merging can be done several times locally (working set held in the
    browser, nothing sent to the server) before a single "Save" commits the
    final pieces via the existing create/delete/restore endpoints — this is
    what lets each intermediate step still show a live NER preview without
    writing anything until the user actually saves."""
    payload = request.get_json(silent=True) or {}
    text = payload.get('text', '')
    if not isinstance(text, str) or not text.strip():
        return jsonify({'error': 'text must be a non-empty string'}), 400

    references, needs_classification = _classify_footnote_text(job_id, text.strip())
    return jsonify({'references': references, 'needs_classification': needs_classification}), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/split', methods=['POST'])
@login_required
@job_editor_required
def split_footnote(job_id, footnote_id):
    """Split one footnote into two at a caller-given character offset into
    its text — for undoing the pipeline's automatic segmentation
    (prepare_footnotes_for_processing in pdf_to_structured_footnotes.py)
    getting it wrong the other way: two footnotes merged into one region.
    Both halves keep the original's page/bbox/confidence (a text-level split
    can't re-derive two tighter boxes from one detected region — the same
    tradeoff the pipeline's own auto-split already accepts). By default both
    are freshly reclassified (a split always invalidates old span offsets);
    pass reclassify=false to skip the NER cascade for a faster response and
    leave both halves needs_classification for manual/later tagging instead.
    Fully undoable via the existing restore_footnote endpoint: delete both
    new ids, restore the pre-split snapshot of the original."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = _find_footnote(result, footnote_id)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    payload = request.get_json(silent=True) or {}
    offset = payload.get('offset')
    reclassify = payload.get('reclassify', True)
    text = footnote.get('text', '')
    if not isinstance(offset, int) or not (0 < offset < len(text)):
        return jsonify({'error': f'offset must be an integer strictly between 0 and {len(text)}'}), 400

    first_text = text[:offset].strip()
    second_text = text[offset:].strip()
    if not first_text or not second_text:
        return jsonify({'error': 'Both halves of the split must be non-empty'}), 422

    # The two halves' NER cascades are independent of each other, so run them
    # concurrently rather than back-to-back — this is most of split's latency
    # (each is a full reference+component[+names] BERT pass), and PyTorch's
    # CPU/GPU ops release the GIL during the actual tensor math, so two Python
    # threads calling into the same loaded model genuinely overlap instead of
    # just interleaving. Skipped entirely when reclassify=False (the caller's
    # main lever for cutting this latency to near-zero — see the UI's "Re-run
    # NER" checkbox).
    def _timed_classify(text_piece):
        start = time.perf_counter()
        references, needs_classification = _classify_footnote_text(job_id, text_piece)
        return references, needs_classification, time.perf_counter() - start

    first_ner_elapsed = second_ner_elapsed = None
    if reclassify:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(_timed_classify, first_text)
            second_future = executor.submit(_timed_classify, second_text)
            first_references, first_needs_classification, first_ner_elapsed = first_future.result()
            second_references, second_needs_classification, second_ner_elapsed = second_future.result()
    else:
        first_references, first_needs_classification = [], True
        second_references, second_needs_classification = [], True

    def _build_half(half_text, references, needs_classification):
        half = {
            'id': str(uuid.uuid4()),
            'page': footnote.get('page'),
            'bbox': footnote.get('bbox'),
            'confidence': footnote.get('confidence'),
            'text': half_text,
            'references': references,
            'source': 'manual',
            'edited': True,
            'needs_classification': needs_classification,
        }
        _recompute_footnote_summary(half)
        return half

    first = _build_half(first_text, first_references, first_needs_classification)
    second = _build_half(second_text, second_references, second_needs_classification)

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnotes = result.setdefault('results', [])
        idx = next((i for i, f in enumerate(footnotes) if f.get('id') == footnote_id), None)
        if idx is None:
            return jsonify({'error': 'Footnote not found'}), 404
        footnotes[idx:idx + 1] = [first, second]
        _save_job_result(job_id, result)

    _log_activity('footnote_split', job_id, footnote_id=footnote_id, page=footnote.get('page'), bbox=footnote.get('bbox'),
                   offset=offset, original_text=text, new_ids=[first['id'], second['id']],
                   first_text=first_text, second_text=second_text, reclassified=reclassify)
    if reclassify:
        _log_reference_predictions(job_id, first_references, first['id'], inference_time_seconds=round(first_ner_elapsed, 4))
        _log_reference_predictions(job_id, second_references, second['id'], inference_time_seconds=round(second_ner_elapsed, 4))

    return jsonify({'first': first, 'second': second}), 201


@app.route('/api/v1/documents/<job_id>/footnotes/merge', methods=['POST'])
@login_required
@job_editor_required
def merge_footnotes(job_id):
    """Merge two-or-more footnotes into one — for undoing the pipeline's
    automatic segmentation getting it wrong one way: what should have been a
    single footnote got split across multiple detected regions/markers.
    Merged in reading order (page, then bbox.y1), not submission order.
    bbox is the union of all originals when they share a page (usually
    visually accurate); across pages there's no single sensible rectangle,
    so the earliest footnote's bbox is kept and flagged bbox_approximate so
    the UI doesn't silently draw a box that no longer means what it looks
    like it means. Reclassified by default; pass reclassify=false to skip
    the NER cascade (faster) and leave the merged footnote needs_classification
    instead. Fully undoable via restore_footnote: delete the merged id,
    restore each pre-merge snapshot."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    payload = request.get_json(silent=True) or {}
    ids = payload.get('footnote_ids')
    reclassify = payload.get('reclassify', True)
    if not isinstance(ids, list) or len(ids) < 2:
        return jsonify({'error': 'footnote_ids must be a list of at least 2 ids'}), 400

    footnotes_by_id = {f.get('id'): f for f in result.get('results', [])}
    selected = []
    for fid in ids:
        fn = footnotes_by_id.get(fid)
        if fn is None:
            return jsonify({'error': f'Footnote not found: {fid}'}), 404
        selected.append(fn)

    ordered = sorted(selected, key=lambda f: (f.get('page') or 0, (f.get('bbox') or {}).get('y1') or 0))
    merged_text = '\n'.join(f.get('text', '') for f in ordered).strip()
    if not merged_text:
        return jsonify({'error': 'Nothing to merge — all selected footnotes are empty'}), 422

    pages = {f.get('page') for f in ordered}
    if len(pages) == 1 and all(_validate_bbox(f.get('bbox')) is None for f in ordered):
        boxes = [f['bbox'] for f in ordered]
        bbox = {
            'x1': min(b['x1'] for b in boxes), 'y1': min(b['y1'] for b in boxes),
            'x2': max(b['x2'] for b in boxes), 'y2': max(b['y2'] for b in boxes),
        }
        bbox_approximate = False
        # The union can swallow a box between the merged ones.
        other = _overlapping_footnote(result, ordered[0].get('page'), bbox, ignore_ids=set(ids))
        if other is not None:
            return _overlap_error(ordered[0].get('page'), other)
    else:
        bbox = ordered[0].get('bbox')
        bbox_approximate = True

    ner_elapsed = None
    if reclassify:
        ner_call_start = time.perf_counter()
        references, needs_classification = _classify_footnote_text(job_id, merged_text)
        ner_elapsed = time.perf_counter() - ner_call_start
    else:
        references, needs_classification = [], True
    merged = {
        'id': str(uuid.uuid4()),
        'page': ordered[0].get('page'),
        'bbox': bbox,
        'bbox_approximate': bbox_approximate,
        'confidence': None,
        'text': merged_text,
        'references': references,
        'source': 'manual',
        'edited': True,
        'needs_classification': needs_classification,
    }
    _recompute_footnote_summary(merged)

    merged_ids = set(ids)
    with _job_result_lock:
        result = _get_job_result(job_id)
        footnotes = result.setdefault('results', [])
        idx = next((i for i, f in enumerate(footnotes) if f.get('id') in merged_ids), None)
        if idx is None:
            return jsonify({'error': 'Footnotes not found'}), 404
        result['results'] = [f for f in footnotes if f.get('id') not in merged_ids]
        result['results'].insert(idx, merged)
        _save_job_result(job_id, result)

    _log_activity('footnote_merge', job_id, footnote_id=merged['id'], page=merged.get('page'), bbox=merged.get('bbox'),
                   source_ids=ids, merged_text=merged_text, reclassified=reclassify)
    if reclassify:
        _log_reference_predictions(job_id, references, merged['id'], inference_time_seconds=round(ner_elapsed, 4))

    return jsonify(merged), 201


@app.route('/api/v1/documents/<job_id>/footnotes/reorder', methods=['POST'])
@login_required
@job_editor_required
def reorder_footnotes(job_id):
    """Set the order of one page's footnotes by hand.

    The pipeline emits footnotes in descending detection confidence. On a page
    detected as a single region that is also reading order, which is why
    ordinary bottom-of-page footnotes have always come out right; a page
    holding several separate regions (marginal side notes, a two-page spread)
    instead comes out ranked by how sure the detector was. The viewer sorts
    geometrically to fix the common case, but geometry cannot resolve every
    page — two notes sharing one detected region carry no coordinates to tell
    them apart, and a misread marker sits wherever it was printed. This is the
    manual override for those.

    Applied by reordering result['results'] itself rather than by storing an
    index alongside it, so the order is canonical: every export, download and
    API consumer reads the same list and needs no sorting of its own. Safe
    because nothing addresses a footnote by position — every lookup in this
    file and in the viewer is by id.

    The page's footnotes are written back into the positions they already
    occupied, so footnotes on other pages keep theirs and a page's slice never
    migrates through the list.

    Marks each of them order_manual so the viewer stops re-deriving this page's
    order from geometry and leaves what was set here alone.
    """
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    payload = request.get_json(silent=True) or {}
    page = payload.get('page')
    ids = payload.get('footnote_ids')
    if not isinstance(page, int) or isinstance(page, bool):
        return jsonify({'error': 'page must be an integer'}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'footnote_ids must be a non-empty list'}), 400
    if len(set(ids)) != len(ids):
        return jsonify({'error': 'footnote_ids contains duplicates'}), 400

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnotes = result.setdefault('results', [])
        positions = [i for i, f in enumerate(footnotes) if _footnote_list_page(f) == page]
        on_page = {footnotes[i].get('id') for i in positions}

        # Every footnote on the page, exactly once. A partial list would leave
        # the ones left out with nowhere to go; an id from elsewhere would pull
        # a footnote off its own page.
        if set(ids) != on_page:
            return jsonify({'error': 'footnote_ids must list every footnote on page %d exactly once' % page,
                            'expected': sorted(on_page)}), 400

        by_id = {footnotes[i].get('id'): footnotes[i] for i in positions}
        previous = [footnotes[i].get('id') for i in positions]
        for position, fid in zip(positions, ids):
            footnotes[position] = by_id[fid]
            footnotes[position]['order_manual'] = True
        _save_job_result(job_id, result)

    _log_activity('footnotes_reorder', job_id, page=page, old_order=previous, new_order=ids)

    return jsonify({'page': page, 'footnote_ids': ids}), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/move-page', methods=['POST'])
@login_required
@job_editor_required
def move_footnote_to_page(job_id, footnote_id):
    """Move a footnote into another page's footnote list.

    For a note whose text belongs with a neighbouring page's notes: the first
    one on a page that really ends the previous page's list, or the last one
    that really opens the next page's. Only the list changes: the footnote is
    stored with list_page, and page/bbox/spans still say where it is printed,
    so its box stays drawn where it was detected.

    Takes the target page and that page's full order with this footnote in it
    (the viewer sends it last when moving to the previous page, first when
    moving to the next). The footnote is repositioned in result['results'] so
    the stored list order matches, and the target page is marked order_manual
    like reorder_footnotes does. Moving it back to the page it was detected on
    drops list_page again, which is also how undo works.

    A footnote running over a page break is listed only on the page it begins
    on (its continuation is drawn, not listed), so it moves the same way.
    """
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    payload = request.get_json(silent=True) or {}
    page = payload.get('page')
    ids = payload.get('footnote_ids')
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        return jsonify({'error': 'page must be a positive integer'}), 400
    if not isinstance(ids, list) or footnote_id not in ids:
        return jsonify({'error': 'footnote_ids must be a list including the moved footnote'}), 400
    if len(set(ids)) != len(ids):
        return jsonify({'error': 'footnote_ids contains duplicates'}), 400

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnotes = result.setdefault('results', [])
        index = next((i for i, f in enumerate(footnotes) if f.get('id') == footnote_id), None)
        if index is None:
            return jsonify({'error': 'Footnote not found'}), 404
        footnote = footnotes[index]
        from_page = _footnote_list_page(footnote)

        on_page = {f.get('id') for f in footnotes if _footnote_list_page(f) == page and f.get('id') != footnote_id}
        if set(ids) != on_page | {footnote_id}:
            return jsonify({'error': 'footnote_ids must list every footnote on page %d plus the moved one' % page,
                            'expected': sorted(on_page | {footnote_id})}), 400

        # Take it out, then put it back beside the target page's slice of the
        # list: after it when it goes to the end, before it otherwise. With
        # nothing on the target page it simply returns to where it was.
        footnotes.pop(index)
        positions = [i for i, f in enumerate(footnotes) if _footnote_list_page(f) == page]
        if not positions:
            insert_at = index
        elif ids[-1] == footnote_id:
            insert_at = positions[-1] + 1
        else:
            insert_at = positions[0]
        footnotes.insert(insert_at, footnote)

        if page == footnote.get('page'):
            footnote.pop('list_page', None)
        else:
            footnote['list_page'] = page

        by_id = {f.get('id'): f for f in footnotes if f.get('id') in set(ids)}
        positions = [i for i, f in enumerate(footnotes) if f.get('id') in by_id]
        for position, fid in zip(positions, ids):
            footnotes[position] = by_id[fid]
            footnotes[position]['order_manual'] = True
        _save_job_result(job_id, result)

    _log_activity('footnote_move_page', job_id, footnote_id=footnote_id, page=footnote.get('page'),
                   bbox=footnote.get('bbox'), from_page=from_page, to_page=page, new_order=ids)

    return jsonify({'footnote': footnote, 'page': page, 'footnote_ids': ids}), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references', methods=['POST'])
@login_required
@job_editor_required
def create_reference(job_id, footnote_id):
    """Create a new reference span within a footnote's text.

    Model-assisted: auto-populates components by running the component model
    directly on the selected span's text (mirrors the bbox->OCR cascade).
    """
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = _find_footnote(result, footnote_id)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    text = footnote.get('text') or ''
    payload = request.get_json(silent=True) or {}
    start, end = payload.get('start'), payload.get('end')

    span_error = _validate_span(len(text), start, end)
    if span_error:
        return jsonify({'error': span_error}), 400

    for ref in footnote.get('references', []):
        if _spans_overlap(start, end, ref['start'], ref['end']):
            return jsonify({'error': 'Span overlaps an existing reference'}), 409

    ref_text = text[start:end]
    # citation_type is pure regex, not a model, so it is derived either way —
    # it costs nothing and overwrites nothing. Only the component cascade is
    # skipped, leaving the new reference empty for manual tagging.
    ner_elapsed = None
    if _cascade_requested(payload):
        ner_call_start = time.perf_counter()
        citation_type, components, needs_classification = _classify_reference_span(job_id, ref_text, start)
        ner_elapsed = time.perf_counter() - ner_call_start
    else:
        citation_type, components, needs_classification = classify_citation_type_standalone(ref_text), [], True

    reference = {
        'id': str(uuid.uuid4()),
        'text': ref_text,
        'confidence': None,
        'start': start,
        'end': end,
        'components': components,
        'citation_type': citation_type,
        'source': 'manual',
        'needs_classification': needs_classification,
    }

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnote = _find_footnote(result, footnote_id)
        _insert_sorted_by_start(footnote.setdefault('references', []), reference)
        _recompute_footnote_summary(footnote)
        _save_job_result(job_id, result)

    _log_activity('reference_create', job_id, footnote_id=footnote_id, reference_id=reference['id'],
                   page=footnote.get('page'), bbox=footnote.get('bbox'), start=start, end=end,
                   text=ref_text, citation_type=citation_type, confidence=None, source='manual')
    if ner_elapsed is not None:
        _log_component_predictions(job_id, components, footnote_id, reference['id'], inference_time_seconds=round(ner_elapsed, 4))

    return jsonify({'reference': reference, 'footnote_summary': footnote['summary']}), 201


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>', methods=['PUT'])
@login_required
@job_editor_required
def update_reference(job_id, footnote_id, reference_id):
    """Resize an existing reference's span. Components are discarded and
    recomputed fresh against the new span (wipe-and-recompute, same precedent
    as bbox edits invalidating a footnote's stale references)."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = _find_footnote(result, footnote_id)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    reference = _find_reference(footnote, reference_id)
    if reference is None:
        return jsonify({'error': 'Reference not found'}), 404

    text = footnote.get('text') or ''
    payload = request.get_json(silent=True) or {}
    start, end = payload.get('start'), payload.get('end')

    span_error = _validate_span(len(text), start, end)
    if span_error:
        return jsonify({'error': span_error}), 400

    for ref in footnote.get('references', []):
        if ref.get('id') != reference_id and _spans_overlap(start, end, ref['start'], ref['end']):
            return jsonify({'error': 'Span overlaps an existing reference'}), 409

    ref_text = text[start:end]
    # The wipe-and-recompute this route is built around is exactly what Shift
    # opts out of: the components (and their names) are carried over untouched,
    # minus any the resize left hanging outside the reference's new bounds.
    ner_elapsed = None
    if _cascade_requested(payload):
        ner_call_start = time.perf_counter()
        citation_type, components, needs_classification = _classify_reference_span(job_id, ref_text, start)
        ner_elapsed = time.perf_counter() - ner_call_start
    else:
        citation_type = reference.get('citation_type')
        components = _clip_children(reference.get('components'), start, end, text, nested_key='names')
        needs_classification = reference.get('needs_classification', False)

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnote = _find_footnote(result, footnote_id)
        reference = _find_reference(footnote, reference_id)
        if reference is None:
            return jsonify({'error': 'Reference not found'}), 404

        old_start, old_end = reference.get('start'), reference.get('end')
        old_text, old_citation_type = reference.get('text'), reference.get('citation_type')
        reference['text'] = ref_text
        reference['start'] = start
        reference['end'] = end
        reference['components'] = components
        reference['citation_type'] = citation_type
        reference['needs_classification'] = needs_classification
        reference['edited'] = True
        # Works ("Praca") follow the reference: clipped to its new span, or,
        # when the parts were just predicted afresh in a job that divides
        # multi-work references, worked out again from them.
        reference_split.refresh_after_resize(text, reference,
                                             auto=ner_elapsed is not None and _split_multi_work_enabled(job_id),
                                             classify=classify_citation_type_standalone)
        _recompute_footnote_summary(footnote)
        _save_job_result(job_id, result)

    _log_activity('reference_update', job_id, footnote_id=footnote_id, reference_id=reference_id,
                   page=footnote.get('page'), bbox=footnote.get('bbox'), start=start, end=end,
                   old_start=old_start, old_end=old_end, old_text=old_text, new_text=ref_text,
                   old_citation_type=old_citation_type, new_citation_type=citation_type,
                   cascaded=ner_elapsed is not None)
    if ner_elapsed is not None:
        _log_component_predictions(job_id, components, footnote_id, reference_id, inference_time_seconds=round(ner_elapsed, 4))

    return jsonify({'reference': reference, 'footnote_summary': footnote['summary']}), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>', methods=['DELETE'])
@login_required
@job_editor_required
def delete_reference(job_id, footnote_id, reference_id):
    """Delete a reference (and, by removal, its nested components)."""
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404

        footnote = _find_footnote(result, footnote_id)
        if footnote is None:
            return jsonify({'error': 'Footnote not found'}), 404

        refs = footnote.get('references', [])
        deleted_ref = next((r for r in refs if r.get('id') == reference_id), None)
        if deleted_ref is None:
            return jsonify({'error': 'Reference not found'}), 404

        footnote['references'] = [r for r in refs if r.get('id') != reference_id]
        _recompute_footnote_summary(footnote)
        _save_job_result(job_id, result)

    _log_activity('reference_delete', job_id, footnote_id=footnote_id, reference_id=reference_id,
                   page=footnote.get('page'), bbox=footnote.get('bbox'),
                   start=deleted_ref.get('start'), end=deleted_ref.get('end'),
                   text=deleted_ref.get('text'), citation_type=deleted_ref.get('citation_type'),
                   confidence=deleted_ref.get('confidence'),
                   component_count=len(deleted_ref.get('components', [])))

    return jsonify({'success': True, 'deleted_id': reference_id, 'footnote_summary': footnote['summary']})


def _works_edit(job_id, footnote_id, reference_id, change, action, error=None, **log_fields):
    """Shared shell of every works ("Praca") edit: load the reference under the
    result lock, let `change(footnote, reference, text)` modify it (a falsy
    return means the edit isn't possible there), save, log, and answer with the
    footnote as it is now and as it was, which is what undo/redo restore."""
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404
        footnote = _find_footnote(result, footnote_id)
        if footnote is None:
            return jsonify({'error': 'Footnote not found'}), 404
        reference = _find_reference(footnote, reference_id)
        if reference is None:
            return jsonify({'error': 'Reference not found'}), 404
        previous = copy.deepcopy(footnote)
        outcome = change(footnote, reference, footnote.get('text') or '')
        if outcome is None or outcome is False:
            return jsonify({'error': error or 'That would cut through a part or leave the reference'}), 422
        reference['edited'] = True
        _save_job_result(job_id, result)
    _log_activity(action, job_id, footnote_id=footnote_id, reference_id=reference_id,
                   page=footnote.get('page'), bbox=footnote.get('bbox'),
                   works=[[w['start'], w['end']] for w in reference.get('works') or []], **log_fields)
    return jsonify({'footnote': footnote, 'previous': previous, 'reference': reference}), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/split', methods=['POST'])
@login_required
@job_editor_required
def split_reference(job_id, footnote_id, reference_id):
    """Divide a reference that holds several works into works ("Praca"),
    keeping the reference itself whole (pipeline/reference_split.py). Body {}
    for the detected cut points, or {cuts: [offsets]} to cut where a person
    clicked (cutting the work that holds each cut if it already has works)."""
    payload = request.get_json(silent=True) or {}
    cuts = payload.get('cuts')
    if cuts is not None and (not isinstance(cuts, list) or not cuts
                             or not all(isinstance(c, int) and not isinstance(c, bool) for c in cuts)):
        return jsonify({'error': 'cuts must be a non-empty list of character offsets'}), 400

    def change(footnote, reference, text):
        if cuts is not None:
            return reference_split.split_at(text, reference, cuts, how='manual',
                                            classify=classify_citation_type_standalone)
        works = reference_split.plan_split(text, reference)
        return works and reference_split.set_works(text, reference, works, how='suggested',
                                                   classify=classify_citation_type_standalone)
    return _works_edit(job_id, footnote_id, reference_id, change, 'reference_split',
                       origin='manual' if cuts is not None else 'suggested')


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/works', methods=['POST'])
@login_required
@job_editor_required
def create_work(job_id, footnote_id, reference_id):
    """Mark one work ("Praca") inside a reference, body {start, end}. The rest
    is split off: every remaining stretch that still holds parts becomes a
    work of its own, so nothing drops out of the export."""
    payload = request.get_json(silent=True) or {}
    start, end = payload.get('start'), payload.get('end')
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end)):
        return jsonify({'error': 'start and end must be character offsets'}), 400
    return _works_edit(job_id, footnote_id, reference_id,
                       lambda fn, ref, text: reference_split.mark_work(text, ref, start, end, how='manual',
                                                                       classify=classify_citation_type_standalone),
                       'work_create', start=start, end=end)


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/works/<work_id>', methods=['PUT'])
@login_required
@job_editor_required
def update_work(job_id, footnote_id, reference_id, work_id):
    """Move a work's edges, body {start, end}: inside the reference, clear of
    its neighbours and never through the middle of a part."""
    payload = request.get_json(silent=True) or {}
    start, end = payload.get('start'), payload.get('end')
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end)):
        return jsonify({'error': 'start and end must be character offsets'}), 400
    return _works_edit(job_id, footnote_id, reference_id,
                       lambda fn, ref, text: reference_split.resize_work(text, ref, work_id, start, end),
                       'work_update', work_id=work_id, start=start, end=end)


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/works/merge', methods=['POST'])
@login_required
@job_editor_required
def merge_works(job_id, footnote_id, reference_id):
    """Join neighbouring works, body {ids: [...]}; a reference left with one
    work goes back to being a plain reference."""
    ids = (request.get_json(silent=True) or {}).get('ids')
    if not isinstance(ids, list) or len(set(ids)) < 2:
        return jsonify({'error': 'ids must list at least two works'}), 400
    return _works_edit(job_id, footnote_id, reference_id,
                       lambda fn, ref, text: reference_split.merge_works(text, ref, ids) is not None,
                       'work_merge', work_ids=ids)


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/works/<work_id>/inherited', methods=['PUT'])
@login_required
@job_editor_required
def set_work_inherited(job_id, footnote_id, reference_id, work_id):
    """Set the fields a work carries over from the one before it. Body
    {inherited: [{source_component_id}]} ([] clears them), or
    {from_previous: ['Author', ...]} to take those labels from the previous
    work (its own parts or what it carries itself)."""
    payload = request.get_json(silent=True) or {}
    if 'from_previous' in payload and not isinstance(payload['from_previous'], list):
        return jsonify({'error': 'from_previous must be a list of labels'}), 400
    if 'from_previous' not in payload and not isinstance(payload.get('inherited'), list):
        return jsonify({'error': 'inherited must be a list'}), 400
    return _works_edit(job_id, footnote_id, reference_id,
                       lambda fn, ref, text: reference_split.set_inherited(
                           fn, ref, work_id, entries=payload.get('inherited'),
                           from_previous=payload.get('from_previous')),
                       'work_inherit', error='Nothing of that kind to carry over from the previous work',
                       work_id=work_id)


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/merge', methods=['POST'])
@login_required
@job_editor_required
def merge_reference_spans(job_id, footnote_id):
    """Join neighbouring references of one footnote into one, e.g. one the
    model cut too short. Body {ids: [...]}. Parts are kept; works are dropped
    (the joined reference can be divided again)."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get('ids')
    if not isinstance(ids, list) or len(set(ids)) < 2:
        return jsonify({'error': 'ids must list at least two references'}), 400
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404
        footnote = _find_footnote(result, footnote_id)
        if footnote is None:
            return jsonify({'error': 'Footnote not found'}), 404
        refs = sorted(footnote.get('references') or [], key=lambda r: r.get('start', 0))
        positions = [i for i, r in enumerate(refs) if r.get('id') in set(ids)]
        if len(positions) != len(set(ids)):
            return jsonify({'error': 'Reference not found'}), 404
        if positions != list(range(positions[0], positions[-1] + 1)):
            return jsonify({'error': 'Only neighbouring references can be merged'}), 409
        chosen = [refs[i] for i in positions]
        text = footnote.get('text') or ''
        previous = copy.deepcopy(footnote)
        merged = reference_split.merge_references(text, chosen)
        merged['citation_type'] = classify_citation_type_standalone(merged['text'])
        merged['edited'] = True
        footnote['references'] = refs[:positions[0]] + [merged] + refs[positions[-1] + 1:]
        _recompute_footnote_summary(footnote)
        _save_job_result(job_id, result)

    _log_activity('reference_merge', job_id, footnote_id=footnote_id, reference_id=merged['id'],
                   page=footnote.get('page'), bbox=footnote.get('bbox'), source_ids=[r['id'] for r in chosen],
                   start=merged['start'], end=merged['end'], text=merged['text'])
    return jsonify({'footnote': footnote, 'previous': previous, 'reference': merged}), 200


@app.route('/api/v1/admin/references/split-multi-work', methods=['POST'])
@login_required
@admin_required
def admin_split_multi_work():
    """Divide multi-work references into works across whole collections of
    already processed books. Body {collection_ids: [...], dry_run: true}.
    References cut apart by the first version of this feature (separate
    references sharing a split_group) are turned back into one reference
    holding them as works. A dry run lists everything without writing;
    otherwise each book is changed under the result lock, its job gets the
    split_multi_work option (so later edits divide the same way), and every
    change is logged with origin 'auto_bulk'. Running it twice changes nothing."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get('collection_ids')
    dry_run = payload.get('dry_run', True) is not False
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'collection_ids must be a non-empty list'}), 400
    # {restore_backup: "backup_pre_split_YYYYMMDD_HHMMSS"} first puts each book
    # back as it was in that backup (reports/reference-split/), then divides it.
    backup_dir = None
    if payload.get('restore_backup'):
        name = str(payload['restore_backup'])
        if not re.fullmatch(r'backup_pre_split_\d{8}_\d{6}', name):
            return jsonify({'error': 'restore_backup must name a backup_pre_split_YYYYMMDD_HHMMSS folder'}), 400
        backup_dir = Path(__file__).parent / 'reports' / 'reference-split' / name
        if not backup_dir.is_dir():
            return jsonify({'error': 'No such backup'}), 404
    jobs = []
    for cid in ids:
        for row in db.list_jobs_in_collection(int(cid)):
            if row['job_id'] not in jobs:
                jobs.append(row['job_id'])
    report = {'dry_run': dry_run, 'jobs': len(jobs), 'books_changed': 0, 'restored_from_backup': 0,
              'converted_from_pieces': 0, 'references_split': 0, 'works': 0, 'splits': []}
    for job_id in jobs:
        if (processing_status.get(job_id) or {}).get('status') != 'completed':
            continue
        logged = []
        with _job_result_lock:
            result = _get_job_result(job_id)
            if result is None:
                continue
            changed = False
            backup_file = backup_dir / f'{job_id}_results.json' if backup_dir else None
            if backup_file is not None and backup_file.exists():
                with open(backup_file, encoding='utf-8') as fh:
                    original = json.load(fh)
                report['restored_from_backup'] += 1
                if dry_run:
                    result = original               # plan against the original, write nothing
                else:
                    processing_results[job_id] = result = original
                    changed = True
                    logged.append(('reference_backup_restored', None, None, backup_dir.name))
            for footnote in result.get('results', []):
                text = footnote.get('text') or ''
                refs = footnote.get('references') or []
                if any(r.get('split_group') for r in refs):
                    converted, n = reference_split.groups_to_works(text, copy.deepcopy(refs))
                    report['converted_from_pieces'] += n
                    if n and not dry_run:
                        footnote['references'] = refs = converted
                        changed = True
                        logged.append(('reference_works_migrated', footnote, None, n))
                for ref in refs:
                    works = reference_split.plan_split(text, ref)
                    if not works:
                        continue
                    report['references_split'] += 1
                    report['works'] += len(works)
                    report['splits'].append({'job_id': job_id, 'page': footnote.get('page'),
                                             'footnote_id': footnote.get('id'), 'reference_id': ref.get('id'),
                                             'pieces': [text[w['start']:w['end']] for w in works],
                                             'inherited': [[l for l, _, _ in w['inherited']] for w in works]})
                    if dry_run:
                        continue
                    reference_split.set_works(text, ref, works, how='auto', classify=classify_citation_type_standalone)
                    changed = True
                    logged.append(('reference_split', footnote, ref, None))
                if not dry_run and changed:
                    _recompute_footnote_summary(footnote)
            if changed:
                _save_job_result(job_id, result)
                report['books_changed'] += 1
        if not dry_run:
            processing_status[job_id].setdefault('options', {})['split_multi_work'] = True
            for action, footnote, ref, n in logged:
                if action == 'reference_backup_restored':
                    _log_activity(action, job_id, origin='auto_bulk', backup=n)
                    continue
                if action == 'reference_works_migrated':
                    _log_activity(action, job_id, footnote_id=footnote.get('id'), page=footnote.get('page'),
                                   origin='auto_bulk', groups=n)
                else:
                    _log_activity(action, job_id, footnote_id=footnote.get('id'), reference_id=ref.get('id'),
                                   page=footnote.get('page'), bbox=footnote.get('bbox'), origin='auto_bulk',
                                   works=[[w['start'], w['end']] for w in ref.get('works') or []],
                                   inherited=[[h['label'] for h in w.get('inherited') or []] for w in ref.get('works') or []])
    if not dry_run:
        save_history()
    return jsonify(report), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/components', methods=['POST'])
@login_required
@job_editor_required
def create_component(job_id, footnote_id, reference_id):
    """Create a new component span nested within an existing reference.

    Model-assisted: if no label is supplied, the component model is run on the
    exact selected phrase to suggest one. If it can't confidently suggest a
    label, the caller must resubmit with an explicit label (from the dropdown).
    """
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = _find_footnote(result, footnote_id)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    reference = _find_reference(footnote, reference_id)
    if reference is None:
        return jsonify({'error': 'Reference not found'}), 404

    text = footnote.get('text') or ''
    payload = request.get_json(silent=True) or {}
    start, end = payload.get('start'), payload.get('end')
    label = payload.get('label')

    span_error = _validate_span(len(text), start, end)
    if span_error:
        return jsonify({'error': span_error}), 400

    if start < reference['start'] or end > reference['end']:
        return jsonify({'error': 'Component span must be fully nested within its parent reference'}), 422

    for comp in reference.get('components', []):
        if _spans_overlap(start, end, comp['start'], comp['end']):
            return jsonify({'error': 'Span overlaps an existing component'}), 409

    comp_text = text[start:end]
    confidence = None
    was_suggested = not label
    suggest_elapsed = None

    if label:
        if label not in KNOWN_COMPONENT_LABELS:
            return jsonify({'error': f'Unknown label. Must be one of: {sorted(KNOWN_COMPONENT_LABELS)}'}), 400
    else:
        try:
            ner_suite_path, is_nested, _ref_conf, comp_conf = _get_ner_options(job_id)
            engine = NEREngine.get_instance(ner_suite_path, is_nested)
            suggest_call_start = time.perf_counter()
            suggestion = engine.suggest_component_label(comp_text, comp_conf)
            suggest_elapsed = time.perf_counter() - suggest_call_start
        except Exception as e:
            logger.error(f"NER classification failed while creating component for job {job_id}: {e}")
            return jsonify({'error': f'Classification failed: {e}'}), 500

        if suggestion is None:
            return jsonify({'error': 'No confident label suggestion; please supply a label explicitly.'}), 422
        label = suggestion['label']
        confidence = suggestion['confidence']

    component = {
        'id': str(uuid.uuid4()),
        'label': label,
        'text': comp_text,
        'confidence': confidence,
        'start': start,
        'end': end,
        'source': 'manual',
    }
    # A hand-marked Author gets its FirstName/LastName children straight away,
    # the same as one the bulk pipeline found — unless this edit was told not
    # to cascade, in which case the span is created bare. The label itself is
    # still suggested when none was supplied: that is the thing being created,
    # not a stage below it.
    names, suggested_names = ([], [])
    if _cascade_requested(payload):
        names, suggested_names = _refresh_component_names(job_id, component, comp_text, start, label)
    if names:
        component['names'] = names

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnote = _find_footnote(result, footnote_id)
        reference = _find_reference(footnote, reference_id)
        _insert_sorted_by_start(reference.setdefault('components', []), component)
        _recompute_footnote_summary(footnote)
        _save_job_result(job_id, result)

    _log_activity('component_create', job_id, footnote_id=footnote_id, reference_id=reference_id,
                   component_id=component['id'], page=footnote.get('page'), bbox=footnote.get('bbox'),
                   start=start, end=end, label=label, text=comp_text, confidence=confidence,
                   source='model_suggested' if was_suggested else 'manual')
    if was_suggested:
        _log_prediction('prediction_ner_component', job_id, _ner_model_info(job_id), footnote_id=footnote_id,
                         reference_id=reference_id, component_id=component['id'], label=label, confidence=confidence,
                         inference_time_seconds=round(suggest_elapsed, 4) if suggest_elapsed is not None else None)
    _log_name_predictions(job_id, suggested_names, footnote_id, reference_id, component['id'])

    return jsonify({'component': component, 'footnote_summary': footnote['summary']}), 201


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/components/<component_id>', methods=['PUT'])
@login_required
@job_editor_required
def update_component(job_id, footnote_id, reference_id, component_id):
    """Resize and/or relabel an existing component (partial update)."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = _find_footnote(result, footnote_id)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    reference = _find_reference(footnote, reference_id)
    if reference is None:
        return jsonify({'error': 'Reference not found'}), 404

    component = _find_component(reference, component_id)
    if component is None:
        return jsonify({'error': 'Component not found'}), 404

    text = footnote.get('text') or ''
    payload = request.get_json(silent=True) or {}
    start = payload.get('start', component['start'])
    end = payload.get('end', component['end'])
    label = payload.get('label')
    span_changed = (start != component['start']) or (end != component['end'])

    span_error = _validate_span(len(text), start, end)
    if span_error:
        return jsonify({'error': span_error}), 400

    if start < reference['start'] or end > reference['end']:
        return jsonify({'error': 'Component span must be fully nested within its parent reference'}), 422

    for comp in reference.get('components', []):
        if comp.get('id') != component_id and _spans_overlap(start, end, comp['start'], comp['end']):
            return jsonify({'error': 'Span overlaps an existing component'}), 409

    comp_text = text[start:end]
    cascade = _cascade_requested(payload)
    confidence = component.get('confidence')
    was_resuggested = False
    suggest_elapsed = None

    if label:
        # Resending the label the component already has means "keep this one"
        # (the viewer's drag-to-resize does exactly that, so moving a boundary
        # never silently reclassifies the span) — accepted even when it isn't in
        # the curated set, because the re-suggest branch below can itself assign
        # a model-only label like FootnoteMarker, and rejecting it here would
        # leave such a span permanently unresizable.
        if label != component['label'] and label not in KNOWN_COMPONENT_LABELS:
            return jsonify({'error': f'Unknown label. Must be one of: {sorted(KNOWN_COMPONENT_LABELS)}'}), 400
        confidence = None
    elif span_changed and not cascade:
        # Resized with Shift held: the new text is not re-classified, the
        # label the span already had stands.
        label = component['label']
    elif span_changed:
        # Resizing invalidates the old label's basis; try to re-suggest, but
        # don't fail the resize if the model can't confidently relabel it —
        # keep the existing label pending manual correction.
        try:
            ner_suite_path, is_nested, _ref_conf, comp_conf = _get_ner_options(job_id)
            engine = NEREngine.get_instance(ner_suite_path, is_nested)
            suggest_call_start = time.perf_counter()
            suggestion = engine.suggest_component_label(comp_text, comp_conf)
            suggest_elapsed = time.perf_counter() - suggest_call_start
        except Exception as e:
            logger.error(f"NER classification failed while updating component {component_id} for job {job_id}: {e}")
            suggestion = None
        if suggestion is not None:
            label = suggestion['label']
            confidence = suggestion['confidence']
            was_resuggested = True
        else:
            label = component['label']
    else:
        label = component['label']

    # Resizing or relabelling invalidates the nested names the same way it
    # invalidates the component's own label: the old FirstName/LastName spans
    # may now sit outside the component, or belong to a label that can't carry
    # names at all. Done outside the lock because it runs the model.
    names_changed = span_changed or label != component['label']
    suggested_names = []
    if names_changed and not cascade:
        # Carried over rather than recomputed, minus any the resize left
        # outside the component — same rule as update_reference's components.
        new_names = _clip_children(component.get('names'), start, end, text)
    elif names_changed:
        new_names, suggested_names = _refresh_component_names(job_id, component, comp_text, start, label)

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnote = _find_footnote(result, footnote_id)
        reference = _find_reference(footnote, reference_id)
        component = _find_component(reference, component_id)
        if component is None:
            return jsonify({'error': 'Component not found'}), 404

        old_start, old_end = component.get('start'), component.get('end')
        old_label, old_confidence, old_text = component.get('label'), component.get('confidence'), component.get('text')
        component['start'] = start
        component['end'] = end
        component['text'] = comp_text
        component['label'] = label
        component['confidence'] = confidence
        component['edited'] = True
        if names_changed:
            if new_names:
                component['names'] = new_names
            else:
                component.pop('names', None)
        _recompute_footnote_summary(footnote)
        _save_job_result(job_id, result)

    _log_activity('component_update', job_id, footnote_id=footnote_id, reference_id=reference_id,
                   component_id=component_id, page=footnote.get('page'), bbox=footnote.get('bbox'),
                   start=start, end=end, label=label, confidence=confidence,
                   old_start=old_start, old_end=old_end, old_label=old_label,
                   old_confidence=old_confidence, old_text=old_text, new_text=comp_text)
    if was_resuggested:
        _log_prediction('prediction_ner_component', job_id, _ner_model_info(job_id), footnote_id=footnote_id,
                         reference_id=reference_id, component_id=component_id, label=label, confidence=confidence,
                         inference_time_seconds=round(suggest_elapsed, 4) if suggest_elapsed is not None else None)
    _log_name_predictions(job_id, suggested_names, footnote_id, reference_id, component_id)

    return jsonify({'component': component, 'footnote_summary': footnote['summary']}), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/components/<component_id>', methods=['DELETE'])
@login_required
@job_editor_required
def delete_component(job_id, footnote_id, reference_id, component_id):
    """Delete a component from a reference."""
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404

        footnote = _find_footnote(result, footnote_id)
        if footnote is None:
            return jsonify({'error': 'Footnote not found'}), 404

        reference = _find_reference(footnote, reference_id)
        if reference is None:
            return jsonify({'error': 'Reference not found'}), 404

        comps = reference.get('components', [])
        deleted_comp = next((c for c in comps if c.get('id') == component_id), None)
        if deleted_comp is None:
            return jsonify({'error': 'Component not found'}), 404

        reference['components'] = [c for c in comps if c.get('id') != component_id]
        _recompute_footnote_summary(footnote)
        _save_job_result(job_id, result)

    _log_activity('component_delete', job_id, footnote_id=footnote_id, reference_id=reference_id,
                   component_id=component_id, page=footnote.get('page'), bbox=footnote.get('bbox'),
                   start=deleted_comp.get('start'), end=deleted_comp.get('end'), label=deleted_comp.get('label'),
                   text=deleted_comp.get('text'), confidence=deleted_comp.get('confidence'))

    return jsonify({'success': True, 'deleted_id': component_id, 'footnote_summary': footnote['summary']})


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/components/<component_id>/names', methods=['POST'])
@login_required
@job_editor_required
def create_name(job_id, footnote_id, reference_id, component_id):
    """Create a new name span (FirstName/LastName) nested within an existing
    Author/Editor/Translator component — mirrors create_component one level
    deeper. Model-assisted: if no label is supplied, the names model is run
    on the exact selected phrase to suggest one (falls back to a manual pick
    if the suite has no names model loaded or isn't confident)."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = _find_footnote(result, footnote_id)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    reference = _find_reference(footnote, reference_id)
    if reference is None:
        return jsonify({'error': 'Reference not found'}), 404

    component = _find_component(reference, component_id)
    if component is None:
        return jsonify({'error': 'Component not found'}), 404

    if component['label'] not in COMPONENT_LABELS_WITH_NAMES:
        return jsonify({'error': f"Names can only be added under {sorted(COMPONENT_LABELS_WITH_NAMES)} components"}), 422

    text = footnote.get('text') or ''
    payload = request.get_json(silent=True) or {}
    start, end = payload.get('start'), payload.get('end')
    label = payload.get('label')

    span_error = _validate_span(len(text), start, end)
    if span_error:
        return jsonify({'error': span_error}), 400

    if start < component['start'] or end > component['end']:
        return jsonify({'error': 'Name span must be fully nested within its parent component'}), 422

    for name in component.get('names', []):
        if _spans_overlap(start, end, name['start'], name['end']):
            return jsonify({'error': 'Span overlaps an existing name'}), 409

    name_text = text[start:end]
    confidence = None
    was_suggested = not label
    suggest_elapsed = None

    if label:
        if label not in KNOWN_NAME_LABELS:
            return jsonify({'error': f'Unknown label. Must be one of: {sorted(KNOWN_NAME_LABELS)}'}), 400
    else:
        try:
            ner_suite_path, is_nested, _ref_conf, comp_conf = _get_ner_options(job_id)
            engine = NEREngine.get_instance(ner_suite_path, is_nested)
            suggest_call_start = time.perf_counter()
            # The parent component is passed as context: the names model was
            # trained on whole name phrases, so a bare selection like "E." or
            # a surname on its own often scores nothing in isolation while
            # being unambiguous inside "E. Hobsbawm".
            suggestion = engine.suggest_name_label(
                name_text, comp_conf,
                context_text=text[component['start']:component['end']],
                rel_start=start - component['start'],
                rel_end=end - component['start'])
            suggest_elapsed = time.perf_counter() - suggest_call_start
        except Exception as e:
            logger.error(f"NER classification failed while creating name for job {job_id}: {e}")
            return jsonify({'error': f'Classification failed: {e}'}), 500

        if suggestion is None:
            return jsonify({'error': 'No confident label suggestion; please supply a label explicitly.'}), 422
        label = suggestion['label']
        confidence = suggestion['confidence']

    name = {
        'id': str(uuid.uuid4()),
        'label': label,
        'text': name_text,
        'confidence': confidence,
        'start': start,
        'end': end,
        'source': 'manual',
    }

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnote = _find_footnote(result, footnote_id)
        reference = _find_reference(footnote, reference_id)
        component = _find_component(reference, component_id)
        _insert_sorted_by_start(component.setdefault('names', []), name)
        _save_job_result(job_id, result)

    _log_activity('name_create', job_id, footnote_id=footnote_id, reference_id=reference_id,
                   component_id=component_id, name_id=name['id'], page=footnote.get('page'),
                   bbox=footnote.get('bbox'), start=start, end=end, label=label, text=name_text,
                   confidence=confidence, source='model_suggested' if was_suggested else 'manual')
    if was_suggested:
        _log_prediction('prediction_ner_name', job_id, _ner_model_info(job_id), footnote_id=footnote_id,
                         reference_id=reference_id, component_id=component_id, name_id=name['id'],
                         label=label, confidence=confidence,
                         inference_time_seconds=round(suggest_elapsed, 4) if suggest_elapsed is not None else None)

    return jsonify({'name': name}), 201


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/components/<component_id>/names/<name_id>', methods=['PUT'])
@login_required
@job_editor_required
def update_name(job_id, footnote_id, reference_id, component_id, name_id):
    """Resize and/or relabel an existing name span (partial update)."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Job not found'}), 404

    footnote = _find_footnote(result, footnote_id)
    if footnote is None:
        return jsonify({'error': 'Footnote not found'}), 404

    reference = _find_reference(footnote, reference_id)
    if reference is None:
        return jsonify({'error': 'Reference not found'}), 404

    component = _find_component(reference, component_id)
    if component is None:
        return jsonify({'error': 'Component not found'}), 404

    name = _find_name(component, name_id)
    if name is None:
        return jsonify({'error': 'Name not found'}), 404

    text = footnote.get('text') or ''
    payload = request.get_json(silent=True) or {}
    start = payload.get('start', name['start'])
    end = payload.get('end', name['end'])
    label = payload.get('label')
    span_changed = (start != name['start']) or (end != name['end'])

    span_error = _validate_span(len(text), start, end)
    if span_error:
        return jsonify({'error': span_error}), 400

    if start < component['start'] or end > component['end']:
        return jsonify({'error': 'Name span must be fully nested within its parent component'}), 422

    for other in component.get('names', []):
        if other.get('id') != name_id and _spans_overlap(start, end, other['start'], other['end']):
            return jsonify({'error': 'Span overlaps an existing name'}), 409

    name_text = text[start:end]
    confidence = name.get('confidence')

    was_resuggested = False
    suggest_elapsed = None
    if label:
        # Same "keep the label I already have" allowance as update_component.
        if label != name['label'] and label not in KNOWN_NAME_LABELS:
            return jsonify({'error': f'Unknown label. Must be one of: {sorted(KNOWN_NAME_LABELS)}'}), 400
        confidence = None
    elif span_changed and not _cascade_requested(payload):
        label = name['label']
    elif span_changed:
        try:
            ner_suite_path, is_nested, _ref_conf, comp_conf = _get_ner_options(job_id)
            engine = NEREngine.get_instance(ner_suite_path, is_nested)
            suggest_call_start = time.perf_counter()
            suggestion = engine.suggest_name_label(
                name_text, comp_conf,
                context_text=text[component['start']:component['end']],
                rel_start=start - component['start'],
                rel_end=end - component['start'])
            suggest_elapsed = time.perf_counter() - suggest_call_start
        except Exception as e:
            logger.error(f"NER classification failed while updating name {name_id} for job {job_id}: {e}")
            suggestion = None
        if suggestion is not None:
            label = suggestion['label']
            confidence = suggestion['confidence']
            was_resuggested = True
        else:
            label = name['label']
    else:
        label = name['label']

    with _job_result_lock:
        result = _get_job_result(job_id)
        footnote = _find_footnote(result, footnote_id)
        reference = _find_reference(footnote, reference_id)
        component = _find_component(reference, component_id)
        name = _find_name(component, name_id)
        if name is None:
            return jsonify({'error': 'Name not found'}), 404

        old_start, old_end = name.get('start'), name.get('end')
        old_label, old_confidence, old_text = name.get('label'), name.get('confidence'), name.get('text')
        name['start'] = start
        name['end'] = end
        name['text'] = name_text
        name['label'] = label
        name['confidence'] = confidence
        name['edited'] = True
        _save_job_result(job_id, result)

    _log_activity('name_update', job_id, footnote_id=footnote_id, reference_id=reference_id,
                   component_id=component_id, name_id=name_id, page=footnote.get('page'),
                   bbox=footnote.get('bbox'), start=start, end=end, label=label, confidence=confidence,
                   old_start=old_start, old_end=old_end, old_label=old_label,
                   old_confidence=old_confidence, old_text=old_text, new_text=name_text)
    if was_resuggested:
        _log_prediction('prediction_ner_name', job_id, _ner_model_info(job_id), footnote_id=footnote_id,
                         reference_id=reference_id, component_id=component_id, name_id=name_id,
                         label=label, confidence=confidence,
                         inference_time_seconds=round(suggest_elapsed, 4) if suggest_elapsed is not None else None)

    return jsonify({'name': name}), 200


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/components/<component_id>/names/<name_id>', methods=['DELETE'])
@login_required
@job_editor_required
def delete_name(job_id, footnote_id, reference_id, component_id, name_id):
    """Delete a name span from a component."""
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404

        footnote = _find_footnote(result, footnote_id)
        if footnote is None:
            return jsonify({'error': 'Footnote not found'}), 404

        reference = _find_reference(footnote, reference_id)
        if reference is None:
            return jsonify({'error': 'Reference not found'}), 404

        component = _find_component(reference, component_id)
        if component is None:
            return jsonify({'error': 'Component not found'}), 404

        names = component.get('names', [])
        deleted_name = next((n for n in names if n.get('id') == name_id), None)
        if deleted_name is None:
            return jsonify({'error': 'Name not found'}), 404

        component['names'] = [n for n in names if n.get('id') != name_id]
        _save_job_result(job_id, result)

    _log_activity('name_delete', job_id, footnote_id=footnote_id, reference_id=reference_id,
                   component_id=component_id, name_id=name_id, page=footnote.get('page'),
                   bbox=footnote.get('bbox'), start=deleted_name.get('start'), end=deleted_name.get('end'),
                   label=deleted_name.get('label'), text=deleted_name.get('text'),
                   confidence=deleted_name.get('confidence'))

    return jsonify({'success': True, 'deleted_id': name_id})


# Linked entries per reference list for the job lists (link_counts.py): the
# ones not up to date are worked out here, one at a time, beside the requests.
_link_count_queue = queue.Queue()
_link_count_pending = set()
_link_count_lock = threading.Lock()


def _link_count_thread():
    while True:
        job_id = _link_count_queue.get()
        try:
            link_counts.compute(RESULTS_FOLDER, job_id)
        except Exception as e:
            logger.warning(f"Link counts for {job_id} failed: {e}")
        with _link_count_lock:
            _link_count_pending.discard(job_id)
        time.sleep(0.05)                                 # the requests come first


threading.Thread(target=_link_count_thread, daemon=True, name='link-counts').start()


def _reference_list_links(job_id):
    """How many entries of a job's reference list are linked to footnote
    references (default level), or None while that is being worked out."""
    counts = link_counts.cached(RESULTS_FOLDER, job_id)
    if counts is not None:
        return counts['linked_entries']
    with _link_count_lock:
        if job_id not in _link_count_pending:
            _link_count_pending.add(job_id)
            _link_count_queue.put(job_id)
    return None


def _reference_list_size(job_id):
    """How many entries a job's reference list has, or None without one -
    for the marker in job lists."""
    path = RESULTS_FOLDER / f"{job_id}_reference_list.json"
    if not path.exists():
        return None
    try:
        return len(json.loads(path.read_text(encoding='utf-8')).get('entries') or [])
    except (OSError, ValueError):
        return None


def _review_flag_count(job_id):
    """How many TO REVIEW marks a job has, for the marker in job lists, or
    None without any. Like the viewer, it leaves out a mark whose reference
    is gone (the footnote was classified again) or whose list entry is; the
    results are read only for the few jobs that have marks."""
    try:
        flags = _load_review_flags(job_id)
    except (OSError, ValueError):
        return None
    if not flags:
        return None
    result = _get_job_result(job_id) or {}
    refs = _reference_ids(result)
    entries = None
    if any(f['kind'] == 'link' for f in flags):
        entries = {e['id'] for e in (_load_reference_list(job_id) or {}).get('entries') or []}
    n = sum(1 for f in flags if f['reference_id'] in refs and (f['kind'] != 'link' or f.get('entry_id') in entries))
    return n or None


def build_job_rows(job_ids):
    """The row shape every job-list view (merged uploads/collections page,
    a group's shared-publications list, etc.) renders via _job_list.html —
    extracted from what used to be history()'s only caller so every list
    gets the same rich fields (status/owner/group badges, export links) for
    free. Unlike list_jobs_visible_to (a permission filter), this doesn't
    check access itself — callers are expected to only pass ids the current
    user is already allowed to see."""
    rows = []
    for j_id in job_ids:
        status = processing_status.get(j_id)
        if status is None:
            continue  # stale id (job deleted since) — skip rather than error
        filename = status.get('original_filename')
        if not filename:
            filename = processing_results.get(j_id, {}).get('metadata', {}).get('input_file', 'Unknown')
        entries = _reference_list_size(j_id)

        rows.append({
            'job_id': j_id,
            'filename': filename,
            'timestamp': status.get('start_time'),
            'status': status['status'],
            'summary': status.get('summary'),
            'options': status.get('options', {}),
            'error_message': status.get('error'),
            'owner_info': db.describe_job_owner(j_id),
            'verified': bool(status.get('verified', False)),
            'verified_by': status.get('verified_by_username'),
            'verified_at': status.get('verified_at'),
            'book_metadata': status.get('book_metadata') or {},
            # entries in the job's reference list (Reference List Linker), None without one,
            # and how many are linked to footnote references (None until worked out)
            'reference_list_entries': entries,
            'reference_list_links': _reference_list_links(j_id) if entries else None,
            # TO REVIEW marks in the job (review_flags), None without any
            'review_flags': _review_flag_count(j_id),
        })
    # newest first
    rows.sort(key=lambda x: x['timestamp'] if isinstance(x['timestamp'], datetime) else datetime.fromisoformat(x['timestamp']) if isinstance(x['timestamp'], str) else datetime.min, reverse=True)
    return rows


@app.route('/history')
@login_required
def history():
    """Merged into the uploads/collections page — kept as a redirect (not
    removed) so the 3 existing url_for('history') call sites (this file's
    post-upload redirect, results.html, _nav.html) keep working unchanged."""
    return redirect(url_for('auth.list_collections'))

@app.route('/api/job/<job_id>', methods=['DELETE'])
@login_required
@job_editor_required
def delete_job(job_id):
    """Delete a job from history"""
    filename = processing_status.get(job_id, {}).get('original_filename')
    owner_info = db.describe_job_owner(job_id)
    if job_id in processing_status:
        del processing_status[job_id]
    if job_id in processing_results:
        del processing_results[job_id]
    save_history()
    db.purge_job_references(job_id)
    _log_activity('job_delete', job_id, filename=filename, owner_info=owner_info)
    return jsonify({'success': True})

# ---- Reference List Linker (features.py: reference_linker) ----
# A book's reference list - its own ("internal", found inside the book) or
# one given as BibTeX - linked to the references in its footnotes; the
# matching itself is pipeline/reference_linker.py. Per job the list lives in
# results/<job>_reference_list.json: entries, reviewers' decisions, state.
# Building from the book runs its engine in _linker_thread, one job at a
# time; reading, confirming and rejecting need the feature.

_reference_list_lock = threading.Lock()
_linker_queue = queue.Queue()

# The book's own list ("internal") is switched off for now:
# its engine, GROBID, gives unusable output. Only external lists (BibTeX,
# e.g. PolIndex records) are linked until a better engine replaces it.
INTERNAL_REFERENCE_LISTS = False
app.jinja_env.globals['INTERNAL_REFERENCE_LISTS'] = INTERNAL_REFERENCE_LISTS


def _reference_list_path(job_id):
    return RESULTS_FOLDER / f"{job_id}_reference_list.json"


def _load_reference_list(job_id):
    path = _reference_list_path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def _save_reference_list(job_id, stored):
    path = _reference_list_path(job_id)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(path)


def internal_reference_list(job_id):
    """(entries, engine): the reference list found inside the book itself.

    The engine is GROBID for now and is to be replaced by something better;
    nothing outside this function knows which one it is. Uses the list
    stored with the job when there is one (an upload with Show References),
    otherwise runs the engine on the PDF and stores what it finds with the
    job too, so Show References has it."""
    result = _get_job_result(job_id)
    if result is None:
        raise ValueError('Result file not found')
    parsed = result.get('grobid_references')
    if not parsed:
        from pipeline.grobid_client import GrobidClient
        pdf_path = _find_source_pdf(job_id)
        if pdf_path is None:
            raise ValueError('Source PDF missing')
        engine = GrobidClient(grobid_url=GROBID_URL)
        if not engine.is_available():
            raise RuntimeError('The reference-list engine is not reachable')
        found = engine.process_pdf(str(pdf_path))
        with _job_result_lock:
            result = _get_job_result(job_id)
            result['grobid_references'] = found['grobid_references']
            result['grobid_metadata'] = found['grobid_metadata']
            _save_job_result(job_id, result)
        parsed = found['grobid_references']
    return reference_linker.internal_entries(result, parsed), 'grobid'


def _store_reference_list(job_id, source, engine, entries, user):
    """Replace a job's list, keeping the decisions that still have their entry,
    and reviewers' corrections of an entry that comes back unchanged."""
    with _reference_list_lock:
        old = _load_reference_list(job_id) or {}
        edited = {e['id']: e for e in old.get('entries') or [] if e.get('edited')}
        for e in entries:
            prev = edited.get(e['id'])
            if prev and prev.get('text') == e.get('text'):
                e['fields'], e['edited'] = prev['fields'], prev['edited']
        ids = {e['id'] for e in entries}
        kept = [d for d in old.get('decisions') or [] if d['entry_id'] in ids]
        _save_reference_list(job_id, {
            'source': source, 'engine': engine, 'state': 'ready', 'error': None,
            'built_at': datetime.now().isoformat(timespec='seconds'), 'built_by': user,
            'entries': entries, 'decisions': kept,
        })
    return len(old.get('decisions') or []) - len(kept)


def _set_reference_list_state(job_id, state, user=None, error=None):
    with _reference_list_lock:
        stored = _load_reference_list(job_id) or {'entries': [], 'decisions': []}
        stored.update(state=state, error=error)
        if state == 'running':
            stored.update(source='internal', requested_by=user)
        _save_reference_list(job_id, stored)


def _linker_thread():
    while True:
        job_id, user = _linker_queue.get()
        try:
            entries, engine = internal_reference_list(job_id)
            _store_reference_list(job_id, 'internal', engine, entries, user)
        except Exception as e:
            logger.error(f"Reference list for {job_id} failed: {e}")
            _set_reference_list_state(job_id, 'error', error=str(e))
        _linker_queue.task_done()


threading.Thread(target=_linker_thread, daemon=True, name='reference-linker').start()


def _queue_internal_list(job_id):
    _set_reference_list_state(job_id, 'running', user=g.user['username'])
    _linker_queue.put((job_id, g.user['username']))


@app.route('/api/v1/documents/<job_id>/reference-list', methods=['GET'])
@login_required
@feature_required('reference_linker')
@job_access_required
def get_reference_list(job_id):
    """The job's reference list, its links to the footnote references and
    what stays unlinked (with no list yet: state "none", every footnote
    reference unlinked); ?match=strict|balanced|lenient (default balanced)
    for how strictly they are matched, ?format=csv for the pairs as a table."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Result file not found'}), 404
    # Without a list yet the footnote references are still shown, on their
    # own: all of them come back as unlinked.
    counted_from = link_counts.key(RESULTS_FOLDER, job_id)       # before the files are read
    stored = _load_reference_list(job_id) or {'source': None, 'state': 'none'}
    level = request.args.get('match') or reference_linker.DEFAULT_LEVEL
    if level not in reference_linker.LEVELS:
        return jsonify({'error': f"match must be one of: {', '.join(reference_linker.LEVELS)}"}), 400
    view = reference_linker.build_view(result, stored, level)
    if request.args.get('works') == '1':
        # the footnote references grouped by the work they cite, for the
        # Footnote References column's "one per work"
        view['works'] = reference_linker.distinct_works(view['items'], level)
    if level == reference_linker.DEFAULT_LEVEL:
        link_counts.store(RESULTS_FOLDER, job_id, view['counts'], counted_from)
    if request.args.get('format') == 'csv':
        return Response(reference_linker.pairs_csv(view), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="reference_links_{job_id[:8]}.csv"'})
    return jsonify(view)


@app.route('/api/v1/documents/<job_id>/reference-list', methods=['POST'])
@login_required
@admin_required
def build_reference_list(job_id):
    """Admin: give the job its reference list - {"source": "internal"} (the
    book's own, found by the engine in the background; poll GET for the
    state), {"source": "bibtex", "bibtex": "..."}, or a multipart .bib file
    in the field `bibtex`."""
    if job_id not in processing_status or _get_job_result(job_id) is None:
        return jsonify({'error': 'Document not found'}), 404
    payload = request.get_json(silent=True) or {}
    upload = request.files.get('bibtex')
    source = payload.get('source') or request.form.get('source') or ('bibtex' if upload else '')
    if source == 'internal':
        if not INTERNAL_REFERENCE_LISTS:
            return jsonify({'error': "The book's own reference list is switched off for now; give the list as BibTeX"}), 400
        _queue_internal_list(job_id)
        return jsonify({'state': 'running'}), 202
    if source == 'bibtex':
        text = upload.read().decode('utf-8', errors='replace') if upload else (payload.get('bibtex') or request.form.get('bibtex') or '')
        try:
            entries = reference_linker.bibtex_entries(text)
        except Exception as e:
            return jsonify({'error': f'Could not read the BibTeX: {e}'}), 400
        if not entries:
            return jsonify({'error': 'No BibTeX entries found'}), 400
        dropped = _store_reference_list(job_id, 'bibtex', None, entries, g.user['username'])
        return jsonify({'state': 'ready', 'entries': len(entries), 'dropped_decisions': dropped})
    return jsonify({'error': 'source must be "internal" or "bibtex"'}), 400


@app.route('/api/v1/documents/<job_id>/reference-list/links', methods=['POST'])
@login_required
@feature_required('reference_linker')
@job_editor_required
def decide_reference_link(job_id):
    """A reviewer's decision on one link: {action: confirm|reject|add|clear,
    reference_id, entry_id}. Decisions win over the automatic links."""
    payload = request.get_json(silent=True) or {}
    action, ref_id, entry_id = payload.get('action'), payload.get('reference_id'), payload.get('entry_id')
    status = {'confirm': 'confirmed', 'reject': 'rejected', 'add': 'added', 'clear': None}
    if action not in status or not ref_id or not entry_id:
        return jsonify({'error': 'action (confirm|reject|add|clear), reference_id and entry_id are required'}), 400
    with _reference_list_lock:
        stored = _load_reference_list(job_id)
        if stored is None or entry_id not in {e['id'] for e in stored.get('entries') or []}:
            return jsonify({'error': 'Unknown reference list entry'}), 404
        decisions = [d for d in stored.get('decisions') or [] if (d['reference_id'], d['entry_id']) != (ref_id, entry_id)]
        if action == 'add':                # one link per reference: an added link replaces its others
            decisions = [d for d in decisions if not (d['reference_id'] == ref_id and d['status'] in ('confirmed', 'added'))]
        if status[action]:
            decisions.append({'reference_id': ref_id, 'entry_id': entry_id, 'status': status[action],
                              'user': g.user['username'], 'at': datetime.now().isoformat(timespec='seconds')})
        stored['decisions'] = decisions
        _save_reference_list(job_id, stored)
    _log_activity('reference_link_' + action, job_id, reference_id=ref_id, entry_id=entry_id)
    return jsonify({'success': True})


# The parts of a list entry a reviewer can tag (Label to Apply labels -> fields).
REFERENCE_ENTRY_FIELDS = ('author', 'editor', 'translator', 'title', 'booktitle', 'journal', 'container', 'publisher',
                          'place', 'year', 'volume', 'issue', 'pages', 'doi', 'url')


@app.route('/api/v1/documents/<job_id>/reference-list/entries/<entry_id>', methods=['PATCH'])
@login_required
@feature_required('reference_linker')
@job_editor_required
def correct_reference_list_entry(job_id, entry_id):
    """A reviewer's correction of a list entry's tagged parts:
    {"fields": {"author": "...", "title": "...", ...}} replaces them. Links are
    worked out again on the next read."""
    payload = request.get_json(silent=True) or {}
    fields = payload.get('fields')
    if not isinstance(fields, dict) or any(k not in REFERENCE_ENTRY_FIELDS or not isinstance(v, str) for k, v in fields.items()):
        return jsonify({'error': f"fields must map some of {', '.join(REFERENCE_ENTRY_FIELDS)} to text"}), 400
    clean = {k: re.sub(r'\s+', ' ', v).strip()[:2000] for k, v in fields.items() if v and v.strip()}
    with _reference_list_lock:
        stored = _load_reference_list(job_id)
        entry = next((e for e in (stored or {}).get('entries') or [] if e['id'] == entry_id), None)
        if entry is None:
            return jsonify({'error': 'Unknown reference list entry'}), 404
        old = entry.get('fields') or {}
        entry['fields'] = clean
        entry['edited'] = {'by': g.user['username'], 'at': datetime.now().isoformat(timespec='seconds')}
        _save_reference_list(job_id, stored)
    _log_activity('reference_list_entry_edit', job_id, entry_id=entry_id, old=old, new=clean)
    return jsonify({'success': True, 'entry': entry})


# ---- TO REVIEW marks (features.py: review_flags) ----
# A reviewer marks a footnote reference, or its link to the reference list,
# "TO REVIEW", to come back to it later. Per job the marks
# live in results/<job>_review_flags.json, apart from the results, so no edit
# path has to carry them. A mark whose reference is gone (the footnote was
# classified again) or whose link no longer shows is kept, just not shown.

REVIEW_FLAG_KINDS = ('reference', 'link')
_review_flags_lock = threading.Lock()


def _review_flags_path(job_id):
    return RESULTS_FOLDER / f"{job_id}_review_flags.json"


def _load_review_flags(job_id):
    path = _review_flags_path(job_id)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding='utf-8')).get('flags') or []


def _save_review_flags(job_id, flags):
    path = _review_flags_path(job_id)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps({'flags': flags}, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(path)


def _reference_ids(result):
    """Every footnote reference's id, and every work's of a divided one."""
    ids = set()
    for footnote in result.get('results') or []:
        for ref in footnote.get('references') or []:
            ids.add(ref.get('id'))
            ids.update(w.get('id') for w in ref.get('works') or [])
    ids.discard(None)
    return ids


@app.route('/api/v1/documents/<job_id>/inferred-components')
@login_required
@job_access_required
def get_inferred_components(job_id):
    """What each ibid. and op. cit. of the document resolves to now:
    {"references": {reference_id: {"citation_type", "components_inferred": [...]
    or null, "inferred_from": source reference id or null, "by_hand": bool}}}
    (citation_type as worked out from the text and parts: an "Idem" reference
    is an ibid.).
    Worked out on a copy by the same code that runs on every save, so a
    document not saved since that code changed shows it too; nothing is
    written. The viewer asks on opening and after every edit, since one edit
    can change the source of an ibid. in another footnote."""
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Result file not found'}), 404
    current = reference_inference.reinfer(copy.deepcopy(result))
    return jsonify({'references': {
        ref['id']: _inference_state(ref)
        for footnote in current.get('results') or [] for ref in footnote.get('references') or []
        if ref.get('id') and ref.get('citation_type') in ('ibidem', 'opus_citatum')}})


def _inference_state(ref):
    """What an ibid./op. cit. resolves to, as the viewer keeps it: its type,
    the parts it takes over, its source (chosen by hand or not), the parts
    removed or added by hand and the source's parts it does not take."""
    return {'citation_type': ref.get('citation_type'), 'components_inferred': ref.get('components_inferred'),
            'inferred_from': ref.get('inferred_from'), 'by_hand': bool(ref.get('source_reference_id')),
            'inferred_removed': ref.get('inferred_removed') or [], 'inferred_added': ref.get('inferred_added') or [],
            'inference_left_out': ref.get('inference_left_out') or []}


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/source', methods=['PUT'])
@login_required
@job_editor_required
def set_reference_source(job_id, footnote_id, reference_id):
    """Choose by hand what an ibid. or op. cit. refers to:
    {"source_reference_id": "<a reference's, or a divided one's work's, id>"},
    or null to go back to the automatic rules (pipeline/reference_inference.py).
    Returns what it resolves to now."""
    payload = request.get_json(silent=True) or {}
    if 'source_reference_id' not in payload:
        return jsonify({'error': 'source_reference_id is required (null: automatic)'}), 400
    source_id = payload['source_reference_id']
    if source_id is not None and (not isinstance(source_id, str) or not source_id):
        return jsonify({'error': 'source_reference_id must be a reference id or null'}), 400
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404
        footnote = _find_footnote(result, footnote_id)
        reference = _find_reference(footnote, reference_id) if footnote else None
        if reference is None:
            return jsonify({'error': 'Reference not found'}), 404
        if reference_inference.citation_type(reference) not in ('ibidem', 'opus_citatum'):
            return jsonify({'error': 'Only an ibid. or op. cit. refers back to another reference'}), 400
        if source_id == reference_id:
            return jsonify({'error': 'A reference cannot refer to itself'}), 400
        if source_id is not None and source_id not in _reference_ids(result):
            return jsonify({'error': 'Unknown source reference'}), 404
        previous = reference.get('source_reference_id')
        if source_id is None:
            reference.pop('source_reference_id', None)
        else:
            reference['source_reference_id'] = source_id
        _save_job_result(job_id, result)
    _log_activity('reference_source_set' if source_id else 'reference_source_clear', job_id, footnote_id=footnote_id,
                  reference_id=reference_id, source_reference_id=source_id, previous_source_reference_id=previous)
    return jsonify({'reference_id': reference_id, **_inference_state(reference), 'previous_source_reference_id': previous})


def _parts_list(value):
    """[{label, text}] from a request, or None when it is not one."""
    if not isinstance(value, list):
        return None
    out = []
    for c in value:
        if not isinstance(c, dict) or not isinstance(c.get('label'), str) or not isinstance(c.get('text'), str):
            return None
        out.append({'label': c['label'], 'text': c['text']})
    return out


@app.route('/api/v1/documents/<job_id>/footnotes/<footnote_id>/references/<reference_id>/inferred', methods=['PUT'])
@login_required
@job_editor_required
def set_inferred_parts(job_id, footnote_id, reference_id):
    """Which of its source's parts an ibid./op. cit./idem takes over, by hand
   : {"inferred_removed": [{label, text}], "inferred_added":
    [{label, text}]} - the parts taken out of what the rules carry over, and
    those of the source's other parts put in (pipeline/reference_inference.py).
    Both lists whole; empty ones go back to the rules. Returns what it takes
    now, and the lists before, for undo."""
    payload = request.get_json(silent=True) or {}
    removed, added = _parts_list(payload.get('inferred_removed')), _parts_list(payload.get('inferred_added'))
    if removed is None or added is None:
        return jsonify({'error': 'inferred_removed and inferred_added must be lists of {label, text}'}), 400
    with _job_result_lock:
        result = _get_job_result(job_id)
        if result is None:
            return jsonify({'error': 'Job not found'}), 404
        footnote = _find_footnote(result, footnote_id)
        reference = _find_reference(footnote, reference_id) if footnote else None
        if reference is None:
            return jsonify({'error': 'Reference not found'}), 404
        if reference_inference.citation_type(reference) not in ('ibidem', 'opus_citatum'):
            return jsonify({'error': 'Only an ibid. or op. cit. takes parts over from another reference'}), 400
        previous = {'inferred_removed': reference.get('inferred_removed') or [],
                    'inferred_added': reference.get('inferred_added') or []}
        for key, value in (('inferred_removed', removed), ('inferred_added', added)):
            if value:
                reference[key] = value
            else:
                reference.pop(key, None)
        _save_job_result(job_id, result)
    _log_activity('reference_inferred_parts_set', job_id, footnote_id=footnote_id, reference_id=reference_id,
                  inferred_removed=removed, inferred_added=added, previous=previous)
    return jsonify({'reference_id': reference_id, **_inference_state(reference), 'previous': previous})


@app.route('/api/v1/documents/<job_id>/review-flags', methods=['GET'])
@login_required
@feature_required('review_flags')
@job_access_required
def get_review_flags(job_id):
    """The document's TO REVIEW marks: [{kind, reference_id, entry_id, by, at}]."""
    return jsonify({'flags': _load_review_flags(job_id)})


@app.route('/api/v1/documents/<job_id>/review-flags', methods=['POST'])
@login_required
@feature_required('review_flags')
@job_editor_required
def set_review_flag(job_id):
    """Marks or unmarks one item: {kind: "reference", reference_id} for a
    footnote reference (or a work of a divided one), {kind: "link",
    reference_id, entry_id} for its link to a reference-list entry; plus
    "flagged": true (default) or false. Returns every mark."""
    payload = request.get_json(silent=True) or {}
    kind, ref_id, entry_id = payload.get('kind'), payload.get('reference_id'), payload.get('entry_id')
    flagged = payload.get('flagged', True)
    if kind not in REVIEW_FLAG_KINDS or not isinstance(ref_id, str) or not ref_id \
            or not isinstance(flagged, bool) or (kind == 'link' and not (isinstance(entry_id, str) and entry_id)):
        return jsonify({'error': 'kind (reference|link), reference_id, entry_id for a link, and flagged (true|false) are required'}), 400
    if kind == 'reference':
        entry_id = None
    result = _get_job_result(job_id)
    if result is None:
        return jsonify({'error': 'Result file not found'}), 404
    if ref_id not in _reference_ids(result):
        return jsonify({'error': 'Unknown footnote reference'}), 404
    if kind == 'link' and entry_id not in {e['id'] for e in (_load_reference_list(job_id) or {}).get('entries') or []}:
        return jsonify({'error': 'Unknown reference list entry'}), 404
    with _review_flags_lock:
        flags = [f for f in _load_review_flags(job_id)
                 if (f['kind'], f['reference_id'], f.get('entry_id')) != (kind, ref_id, entry_id)]
        if flagged:
            flags.append({'kind': kind, 'reference_id': ref_id, 'entry_id': entry_id,
                          'by': g.user['username'], 'at': datetime.now().isoformat(timespec='seconds')})
        _save_review_flags(job_id, flags)
    _log_activity('review_flag_' + ('set' if flagged else 'clear'), job_id, kind=kind, reference_id=ref_id, entry_id=entry_id)
    return jsonify({'success': True, 'flags': flags})


@app.route('/api/jobs/bulk-link-reference-lists', methods=['POST'])
@login_required
@admin_required
def bulk_link_reference_lists():
    """Admin: build the internal reference list for several documents; they
    are done one at a time in the background."""
    payload = request.get_json(silent=True) or {}
    job_ids = payload.get('job_ids')
    if not isinstance(job_ids, list) or not job_ids or len(job_ids) > _BULK_MAX_IDS:
        return jsonify({'error': f'job_ids must be a non-empty array of at most {_BULK_MAX_IDS}'}), 400
    if (payload.get('source') or 'internal') != 'internal':
        return jsonify({'error': 'only source "internal" can be run in bulk'}), 400
    if not INTERNAL_REFERENCE_LISTS:
        return jsonify({'error': "The book's own reference list is switched off for now"}), 400
    queued, skipped = [], []
    for job_id in job_ids:
        if processing_status.get(job_id, {}).get('status') == 'completed':
            _queue_internal_list(job_id)
            queued.append(job_id)
        else:
            skipped.append(job_id)
    return jsonify({'queued': queued, 'skipped': skipped})


@app.route('/api/job/<job_id>/reprocess', methods=['POST'])
@login_required
@job_editor_required
def reprocess_job(job_id):
    """Run a job through the pipeline again, in place: same id, PDF, options,
    book metadata, owner and collections, so links to it keep working. Its
    results are replaced, viewer corrections included, so it is meant for
    jobs whose processing went wrong; the old results and proposals are kept in
    results/reprocess_backups/ first."""
    status = processing_status.get(job_id)
    if status is None:
        return jsonify({'error': 'Document not found'}), 404
    if status.get('status') in ('queued', 'processing'):
        return jsonify({'error': f"Document is already {status['status']}"}), 409
    pdf_path = _find_source_pdf(job_id)
    if pdf_path is None:
        return jsonify({'error': 'Source PDF missing; please re-upload this file.'}), 422

    backup_dir = RESULTS_FOLDER / 'reprocess_backups'
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    for name in (f"{job_id}_results.json", f"{job_id}_proposals.json"):
        if (RESULTS_FOLDER / name).exists():
            shutil.copy2(RESULTS_FOLDER / name, backup_dir / f"{name}.{stamp}")

    # The initial-pass prediction log entries go to the original uploader,
    # as when the job was first processed (see _recover_stuck_jobs).
    owner = db.get_job_owner(job_id)
    uploader_id = owner['owner_user_id'] if owner is not None else None
    owner_user = db.get_user_by_id(uploader_id) if uploader_id is not None else None
    status.update({
        'status': 'queued',
        'step': 'Waiting in queue...',
        'progress': 0,
        'start_time': datetime.now(),
        'error': None,
    })
    save_history()
    _log_activity('job_reprocess', job_id, filename=status.get('original_filename'), backup=stamp)
    job_queue.put((job_id, str(pdf_path), status.get('options', {}), uploader_id,
                   owner_user['username'] if owner_user else None))
    return jsonify({'success': True, 'job_id': job_id, 'status': 'queued'})

@app.route('/api/job/<job_id>/share', methods=['POST'])
@login_required
@job_owner_or_admin_required
def share_job(job_id):
    """Share (or unshare) an owned job with one of the current user's groups."""
    owner = db.get_job_owner(job_id)
    if owner is None:
        return jsonify({'error': 'Legacy job has no owner; cannot be shared'}), 422

    payload = request.get_json(silent=True) or {}
    group_id = payload.get('group_id')
    if group_id is not None:
        if not isinstance(group_id, int):
            return jsonify({'error': 'group_id must be an integer or null'}), 400
        if g.user['role'] != 'admin' and not db.is_group_member(group_id, g.user['id']):
            return jsonify({'error': 'You are not a member of that group'}), 403

    db.update_job_group(job_id, group_id)
    return jsonify({'success': True, 'job_id': job_id, 'group_id': group_id})


# ---- Bulk job actions (merged uploads/collections page, group pages) ----
# All three degrade gracefully per-item rather than failing the whole batch
# (same house style as _classify_footnote_text's try/except-and-degrade) —
# a request touching 20 jobs where 2 aren't editable still succeeds for the
# other 18, reporting the 2 skipped rather than 400ing the entire request.
# Not mirrored into api_routes.py: matches the existing precedent that
# single-job mutation endpoints (delete_job, share_job above) already live
# only on `app` and work against API-key auth for free, since every
# decorator/permission check here only ever looks at flask.g.user.

_BULK_MAX_IDS = 500


@app.route('/api/jobs/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_jobs():
    payload = request.get_json(silent=True) or {}
    job_ids = payload.get('job_ids')
    if not isinstance(job_ids, list) or not job_ids:
        return jsonify({'error': 'job_ids must be a non-empty array'}), 400
    if len(job_ids) > _BULK_MAX_IDS:
        return jsonify({'error': f'Too many job_ids (max {_BULK_MAX_IDS})'}), 400

    deleted, skipped = [], []
    for job_id in job_ids:
        if job_id not in processing_status:
            skipped.append({'job_id': job_id, 'reason': 'not_found'})
            continue
        if not db.can_edit_job(g.user, job_id):
            skipped.append({'job_id': job_id, 'reason': 'forbidden'})
            continue
        filename = processing_status.get(job_id, {}).get('original_filename')
        owner_info = db.describe_job_owner(job_id)
        del processing_status[job_id]
        if job_id in processing_results:
            del processing_results[job_id]
        db.purge_job_references(job_id)
        _log_activity('job_delete', job_id, filename=filename, owner_info=owner_info, bulk=True)
        deleted.append(job_id)

    if deleted:
        save_history()
    return jsonify({'success': True, 'deleted': deleted, 'skipped': skipped})


@app.route('/api/jobs/bulk-add-to-collection', methods=['POST'])
@login_required
def bulk_add_jobs_to_collection():
    payload = request.get_json(silent=True) or {}
    job_ids = payload.get('job_ids')
    if not isinstance(job_ids, list) or not job_ids:
        return jsonify({'error': 'job_ids must be a non-empty array'}), 400
    if len(job_ids) > _BULK_MAX_IDS:
        return jsonify({'error': f'Too many job_ids (max {_BULK_MAX_IDS})'}), 400

    new_collection = payload.get('new_collection')
    collection_id = payload.get('collection_id')
    if new_collection:
        name = (new_collection.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'new_collection.name is required'}), 400
        description = (new_collection.get('description') or '').strip() or None
        group_id = new_collection.get('group_id')
        if group_id is not None:
            if not isinstance(group_id, int):
                return jsonify({'error': 'new_collection.group_id must be an integer or null'}), 400
            if g.user['role'] != 'admin' and not db.is_group_member(group_id, g.user['id']):
                return jsonify({'error': 'You are not a member of that group'}), 403
        collection_id = db.create_collection(name, description, g.user['id'], group_id)
    else:
        if not isinstance(collection_id, int):
            return jsonify({'error': 'collection_id (int) or new_collection is required'}), 400
        if not db.can_edit_collection(g.user, collection_id):
            return jsonify({'error': 'You have view-only access to that collection'}), 403

    added, skipped = [], []
    for job_id in job_ids:
        if job_id not in processing_status:
            skipped.append({'job_id': job_id, 'reason': 'not_found'})
            continue
        if not db.can_access_job(g.user, job_id):
            skipped.append({'job_id': job_id, 'reason': 'forbidden'})
            continue
        db.add_job_to_collection(collection_id, job_id)  # INSERT OR IGNORE — re-adding is a harmless no-op
        added.append(job_id)

    return jsonify({'success': True, 'collection_id': collection_id, 'added': added, 'skipped': skipped})


@app.route('/api/jobs/bulk-unshare', methods=['POST'])
@login_required
def bulk_unshare_jobs():
    """Group-page-only: remove jobs from a group's sharing (job_owners.group_id
    -> NULL) without deleting them. Strict owner/admin, no group-editor
    escalation — mirrors share_job's rationale (reassigning/removing a job's
    sharing is higher-blast-radius than editing content already shared)."""
    payload = request.get_json(silent=True) or {}
    job_ids = payload.get('job_ids')
    group_id = payload.get('group_id')
    if not isinstance(job_ids, list) or not job_ids:
        return jsonify({'error': 'job_ids must be a non-empty array'}), 400
    if not isinstance(group_id, int):
        return jsonify({'error': 'group_id (int) is required'}), 400
    if len(job_ids) > _BULK_MAX_IDS:
        return jsonify({'error': f'Too many job_ids (max {_BULK_MAX_IDS})'}), 400

    unshared, skipped = [], []
    for job_id in job_ids:
        owner = db.get_job_owner(job_id)
        if owner is None:
            skipped.append({'job_id': job_id, 'reason': 'not_found'})
            continue
        is_owner_or_admin = g.user['role'] == 'admin' or owner['owner_user_id'] == g.user['id']
        if not is_owner_or_admin:
            skipped.append({'job_id': job_id, 'reason': 'forbidden'})
            continue
        if owner['group_id'] != group_id:
            skipped.append({'job_id': job_id, 'reason': 'not_shared_with_group'})
            continue
        db.update_job_group(job_id, None)
        unshared.append(job_id)

    return jsonify({'success': True, 'unshared': unshared, 'skipped': skipped})


@app.route('/api/jobs/bulk-remove-from-collection', methods=['POST'])
@login_required
def bulk_remove_jobs_from_collection():
    """Collection-detail-page-only: remove jobs from ONE collection's
    membership (collection_jobs) without deleting them or touching any other
    collection they're also in. Mirrors bulk_unshare_jobs's shape but the
    permission check is on the collection (once, like
    bulk_add_jobs_to_collection's destination check) rather than per job —
    removing a job from a collection doesn't require any permission on the
    job itself, same as the existing single-job remove_job_from_collection
    route."""
    payload = request.get_json(silent=True) or {}
    job_ids = payload.get('job_ids')
    collection_id = payload.get('collection_id')
    if not isinstance(job_ids, list) or not job_ids:
        return jsonify({'error': 'job_ids must be a non-empty array'}), 400
    if not isinstance(collection_id, int):
        return jsonify({'error': 'collection_id (int) is required'}), 400
    if len(job_ids) > _BULK_MAX_IDS:
        return jsonify({'error': f'Too many job_ids (max {_BULK_MAX_IDS})'}), 400
    if not db.can_edit_collection(g.user, collection_id):
        return jsonify({'error': 'You have view-only access to that collection'}), 403

    member_ids = {r['job_id'] for r in db.list_jobs_in_collection(collection_id)}
    removed, skipped = [], []
    for job_id in job_ids:
        if job_id not in member_ids:
            skipped.append({'job_id': job_id, 'reason': 'not_in_collection'})
            continue
        db.remove_job_from_collection(collection_id, job_id)
        removed.append(job_id)

    return jsonify({'success': True, 'removed': removed, 'skipped': skipped})


@app.route('/api/jobs/bulk-export-citations')
@login_required
def bulk_export_citations():
    """Combined citation export for several jobs at once — same formats and
    generator functions as download_results' single-job export, concatenated
    into one file with a filename header per job. A plain GET (not POST+JSON
    like the other bulk endpoints above) so the browser just downloads it
    directly from a link/window.location, same as download_results and the
    interactive viewer's own export links — no fetch/blob handling needed
    client-side. job_ids is a repeated query param (?job_ids=a&job_ids=b&...).
    Silently skips any id the current user can't access or that isn't
    completed, same graceful skip-not-fail philosophy as every other bulk
    endpoint."""
    job_ids = request.args.getlist('job_ids')
    if not job_ids:
        return jsonify({'error': 'job_ids is required (repeated query param)'}), 400
    if len(job_ids) > _BULK_MAX_IDS:
        return jsonify({'error': f'Too many job_ids (max {_BULK_MAX_IDS})'}), 400

    fmt = request.args.get('format', 'bibtex_standard')
    generators = {
        'bibtex_standard': lambda result: generate_bibtex(result, expanded=False, provenance=True, deduplicate=True),
        'bibtex_expanded': lambda result: generate_bibtex(result, expanded=True, provenance=True, deduplicate=True),
        'apa': lambda result: generate_citation_list(result, 'apa', deduplicate=True),
        'chicago': lambda result: generate_citation_list(result, 'chicago', deduplicate=True),
        'mla': lambda result: generate_citation_list(result, 'mla', deduplicate=True),
        'reference_strings': lambda result: generate_reference_strings(result, deduplicate=True),
    }
    generator = generators.get(fmt)
    if generator is None:
        return jsonify({'error': f"Unknown format '{fmt}'"}), 400
    is_bibtex = fmt.startswith('bibtex')

    parts = []
    for job_id in job_ids:
        if not db.can_access_job(g.user, job_id):
            continue
        status = processing_status.get(job_id)
        if status is None or status.get('status') != 'completed':
            continue
        result = _get_job_result(job_id)
        if result is None:
            continue
        filename = status.get('original_filename') or job_id
        header_char = '%' if is_bibtex else '#'
        parts.append(f"{header_char} ==== {filename} ====\n{generator(result)}")

    if not parts:
        return jsonify({'error': 'No accessible, completed jobs to export'}), 404

    ext = 'bib' if is_bibtex else 'txt'
    combined = '\n\n'.join(parts)
    headers = {'Content-Disposition': f'attachment; filename="citations_{len(parts)}_jobs_{fmt}.{ext}"'}
    # mimetype= (not content_type=) triggers Werkzeug's own charset handling
    # for text/*, so a charset already embedded in the string here would get
    # a second one appended — bibtex uses the plain mimetype= form (matching
    # download_results' bibtex branch), text formats use content_type=
    # (matching download_results' apa/chicago/mla/reference_strings branches).
    if is_bibtex:
        return Response(combined, mimetype='application/x-bibtex', headers=headers)
    return Response(combined, content_type='text/plain; charset=utf-8', headers=headers)


@app.route('/api/history/clear', methods=['POST'])
@login_required
def clear_history():
    """Clear history. Non-admins only clear jobs they own (not merely
    group-shared ones — a collaborator shouldn't be able to purge someone
    else's job from the shared store). Admins clear everything, matching the
    original single-user behavior."""
    if g.user['role'] == 'admin':
        target_ids = set(processing_status.keys())
    else:
        target_ids = set()
        for jid in processing_status:
            owner = db.get_job_owner(jid)
            if owner is not None and owner['owner_user_id'] == g.user['id']:
                target_ids.add(jid)

    for jid in target_ids:
        processing_status.pop(jid, None)
        processing_results.pop(jid, None)
    save_history()
    return jsonify({'success': True, 'cleared': len(target_ids)})

@app.errorhandler(413)
def too_large(e):
    """Handle file too large error"""
    flash(t('File too large. Maximum size is 900MB.'))
    return redirect(url_for('index'))

@app.route('/favicon.ico')
def favicon():
    """Browsers request /favicon.ico from the site root, not /static — without
    this every tab open produced a 404 (and, while 404.html was missing, a
    logged traceback). Cached hard: it changes about never."""
    resp = send_from_directory(app.static_folder, 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')
    resp.headers['Cache-Control'] = 'public, max-age=604800'
    return resp


@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors"""
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(e):
    """Handle internal server errors"""
    return render_template('500.html'), 500

# False: the app is reachable from the open internet (via a Cloudflare
# Tunnel) once running, and Werkzeug's debug=True enables the interactive
# in-browser debugger — which lets anyone who can trigger an unhandled
# exception execute arbitrary Python on this machine. The existing 404/500
# error handlers above still cover the user-facing side of turning this off.
DEBUG_MODE = False


class _SkipPreload(Exception):
    pass


def _eager_load_default_models():
    """Preload the default OCR engine and NER suite so the first interactive
    edit (add/resize a bbox, add/resize a reference) isn't slowed by a cold
    model load (Surya ~10-25s, a BERT NER suite similarly slow) — previously
    these only loaded lazily on first use via
    OCREngine.get_instance()/NEREngine.get_instance().

    Only the defaults (Surya OCR, mbert_production) are preloaded. Other
    OCR/NER options a job can still explicitly select still lazy-load on
    first use — eagerly loading every NER suite (~8.8GB across all 4) isn't
    worth it until it's clear which ones are worth keeping around.
    """
    try:
        print("🔧 Preloading default OCR engine (Surya)...")
        OCREngine.get_instance(True)
        print("✅ Surya OCR preloaded")
    except Exception as e:
        logger.error(f"Failed to preload default OCR engine: {e}")

    try:
        # not when an admin switched it off: it would only take GPU memory
        # (every suite goes on the GPU); an edit on an old job loads it then
        if db.is_model_disabled('ner_suite', 'mbert_production'):
            raise _SkipPreload('mbert_production is switched off in Admin -> Models')
        # a fresh install has the default suite only; this one is for old jobs
        if not (Path(__file__).parent / 'models' / 'ner' / 'mbert_production').exists():
            raise _SkipPreload('mbert_production is not installed')
        print("🔧 Preloading default NER suite (mbert_production)...")
        default_ner_path = str(Path(__file__).parent / 'models' / 'ner' / 'mbert_production')
        # is_nested is part of NEREngine's singleton cache key, and every request
        # derives it the same way _get_ner_options does — 'nested' in the suite
        # name, which is False for mbert_production. Passing True here warmed an
        # instance under a key nothing ever looks up, so the first edit paid a
        # cold load anyway and left a second full copy of all three BERT models
        # resident (~2.5GB). Derive it identically instead of hardcoding.
        default_is_nested = 'nested' in Path(default_ner_path).name.lower()
        NEREngine.get_instance(default_ner_path, default_is_nested)
        print("✅ mbert_production NER suite preloaded")
    except _SkipPreload as e:
        print(f"⏭️  Not preloading: {e}")
    except Exception as e:
        logger.error(f"Failed to preload default NER suite: {e}")

    # mbert_production stays preloaded above because nearly every existing job
    # uses it; new uploads default to DEFAULT_NER_SUITE, warmed here too.
    try:
        print(f"🔧 Preloading the new-upload NER suite ({DEFAULT_NER_SUITE})...")
        NEREngine.get_instance(str(Path(__file__).parent / 'models' / 'ner' / DEFAULT_NER_SUITE),
                               'nested' in DEFAULT_NER_SUITE.lower())
        print(f"✅ {DEFAULT_NER_SUITE} NER suite preloaded")
    except Exception as e:
        logger.error(f"Failed to preload {DEFAULT_NER_SUITE}: {e}")

    # Further copies of the defaults, when the admin allows more than one
    # (Admin -> Models -> Instances), so parallel jobs don't wait on loading.
    # In this thread, after the rest: models must never load side by side
    # (MODEL_LOAD_LOCK, model_pool.py).
    _warm_default_copies()


@app.route('/api/_internal/drain', methods=['GET', 'POST'])
@admin_required
def internal_drain():
    """Not user-facing - before a planned restart: POST stops the workers
    taking new jobs (cleared by the restart that follows); both report how
    many are still processing."""
    if request.method == 'POST':
        _draining.set()
        logger.info("Draining: workers take no new jobs until the restart")
    processing = sum(1 for st in list(processing_status.values()) if st.get('status') == 'processing')
    return jsonify({'draining': _draining.is_set(), 'processing': processing})


@app.route('/api/_internal/warmup', methods=['GET', 'POST'])
def internal_warmup():
    """Not user-facing — triggers _eager_load_default_models() inside a real
    Flask request-handling thread. Loading these models from an ad-hoc
    threading.Thread started before app.run() was empirically found to hang
    indefinitely on this environment (reproducible with debug/reloader both
    off, ruling out the reloader; a minimal Flask+background-thread repro did
    NOT reproduce it, so the exact trigger inside Werkzeug's dev server is
    still unclear) — but loading via a genuine request thread, the same
    mechanism every OCREngine/NEREngine.get_instance() call already goes
    through successfully on every other endpoint all session, works reliably.
    Idempotent: get_instance()'s cache makes repeat calls a cheap no-op.
    """
    _eager_load_default_models()
    return jsonify({'status': 'ok'})


def _get_lan_ip() -> str:
    """Best-effort local-network IP for the startup banner. Opens a UDP
    "connection" to a public address (nothing is actually sent — UDP has no
    handshake) purely so the OS picks which local interface/IP would be used
    for outbound traffic; falls back to localhost if that's not possible
    (e.g. no network at all)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


def _trigger_self_warmup(port=5002):
    """Poll our own server until it responds, then fire one request to
    /api/_internal/warmup — see that route's docstring for why this indirect
    approach is used instead of loading directly in this background thread.
    port is a parameter (not hardcoded) so wsgi.py can pass through whatever
    port waitress actually ends up bound to."""
    import urllib.request
    _warmup_started.set()
    try:
        base = f'http://localhost:{port}'
        for _ in range(30):
            try:
                urllib.request.urlopen(f'{base}/', timeout=2)
                break
            except Exception:
                time.sleep(1)
        else:
            logger.error("Server never became reachable; skipping model preload")
            return

        try:
            urllib.request.urlopen(f'{base}/api/_internal/warmup', timeout=900)   # several copies to load
        except Exception as e:
            logger.error(f"Self-triggered warmup request failed: {e}")
    finally:
        _models_ready.set()               # the job workers may start now


def _recover_stuck_jobs():
    """Resume anything left mid-flight by a previous process that stopped
    (crashed, or was killed) without finishing. processing_status is
    rehydrated from history.json at import time (see load_history() above),
    but job_queue always starts empty — so a job that was 'queued' (never
    even started) or 'processing' (killed partway through) when the process
    last stopped would otherwise sit showing that status forever, since
    nothing resubmits it to the new queue. 'queued' jobs are safe to
    resubmit outright; 'processing' jobs can't be safely resumed partway
    through a pipeline run, so they're marked as an error instead, matching
    the shape process_pdf_sync's own except handler already produces."""
    requeued = 0
    for job_id, status in list(processing_status.items()):
        if status.get('status') == 'queued':
            pdf_path = _find_source_pdf(job_id)
            if pdf_path is None:
                status.update({
                    'status': 'error',
                    'step': 'Error: source PDF missing after restart',
                    'error': 'Source PDF missing after restart; please re-upload this file.',
                    'end_time': datetime.now(),
                })
                continue
            # Resolve the original uploader for prediction-log attribution
            # (see worker()/process_pdf_sync's uploader_id/uploader_username) —
            # db.get_job_owner needs an app context, which this module-level
            # startup call doesn't have on its own.
            uploader_id = uploader_username = None
            try:
                with app.app_context():
                    owner = db.get_job_owner(job_id)
                    if owner is not None:
                        uploader_id = owner['owner_user_id']
                        owner_user = db.get_user_by_id(uploader_id)
                        uploader_username = owner_user['username'] if owner_user else None
            except Exception as e:
                logger.error(f"Failed to resolve owner for requeued job {job_id}: {e}")
            job_queue.put((job_id, str(pdf_path), status.get('options', {}), uploader_id, uploader_username))
            requeued += 1
        elif status.get('status') == 'processing':
            status.update({
                'status': 'error',
                'step': 'Error: processing was interrupted by a server restart',
                'error': 'Processing was interrupted by a server restart; please re-upload this file.',
                'end_time': datetime.now(),
            })
    if requeued:
        logger.info(f"Resumed {requeued} job(s) left queued by a previous run")
    save_history()


# Module-level (not inside `if __name__ == '__main__':`) so a
# `from app import app; app.run(...)` entry point recovers stuck jobs too,
# matching the auth/DB wiring above. Unconditional: app.py's own entry point
# below now always runs with use_reloader=False (see the comment there for
# why), so there's only ever one process per run — no parent/child
# duplicate-processing risk to guard against.
_recover_stuck_jobs()


if __name__ == '__main__':
    print("🚀 Starting FootnotesExtractor")
    print("📍 Local access: http://localhost:5002")
    print(f"🌐 Network access: http://{_get_lan_ip()}:5002  (reachable from other devices on this network)")
    print("📄 Upload a PDF to extract structured footnotes with enhanced citation processing!")
    print("🔧 New features: Individual reference classification, author-based opus citatum matching")
    print("💡 MLflow server should be available at: http://localhost:5000")

    threading.Thread(target=_trigger_self_warmup, daemon=True).start()

    # Run the app
    #
    # use_reloader=False is deliberate: with the reloader on, this script
    # re-execs itself as a child (WERKZEUG_RUN_MAIN=true) that's supposed to
    # be the only one serving requests, while the outer supervisor process
    # just spawns/monitors it. On this environment that separation didn't
    # hold — both processes ended up independently handling live requests
    # against their own separate processing_status/job_queue (loaded at each
    # process's own import time), causing background-worker duplicate
    # processing and requests randomly seeing stale state depending on which
    # process handled them (e.g. /history and /api/v1/documents disagreeing
    # on job counts, deleted jobs reappearing). Single-process mode avoids
    # the whole class of bug; debug=True is kept for Flask's debug error
    # pages and Jinja template auto-reload, neither of which need the
    # reloader/process-respawn machinery.
    # host='0.0.0.0': bind on every interface, not just loopback, so other
    # devices on the local network can reach this machine's IP on port 5002
    # (not just this machine itself via localhost) — needed now that there
    # are real per-user accounts for more than one person to log into.
    app.run(debug=DEBUG_MODE, host='0.0.0.0', port=5002, threaded=True, use_reloader=False)
