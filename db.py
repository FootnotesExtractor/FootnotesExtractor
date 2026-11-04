#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite persistence layer for user accounts, groups, and job ownership/sharing.

Kept as a pure data-access layer: no Flask request-context assumptions beyond
the standard `flask.g`-based per-request connection, and no dependency on
app.py's in-memory job-status dicts (job-id universes are passed in by the
caller instead, to avoid a circular import).
"""

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Tuple

from flask import g
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = Path(__file__).parent / 'app.db'

# Guards the count-then-insert in create_user() so the "first user becomes
# admin" bootstrap is atomic within this process. Sufficient for the Flask
# dev server (single process); would need a real transaction (e.g. BEGIN
# IMMEDIATE) to be safe across multiple worker processes.
_registration_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    """Per-request connection, stored on flask.g and closed in close_db()."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


def close_db(e=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)


def init_db():
    """Idempotent schema creation. Uses its own connection (no request
    context available at import time — this is called at module load)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    _drop_first_review_settings(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user', 'admin')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            creator_user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL,
            PRIMARY KEY (group_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS job_owners (
            job_id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id),
            group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            owner_user_id INTEGER NOT NULL REFERENCES users(id),
            group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collection_jobs (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (collection_id, job_id)
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            key_prefix TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT
        );

        -- Presence of a row = that model is admin-disabled. model_type is
        -- one of 'od_model' / 'ner_suite' / 'ocr_engine'; model_key matches
        -- whatever key app.py already uses for that model (checkpoint
        -- filename, NER suite directory name, or 'pdfminer'/'surya').
        -- A reviewer's own page timers (profile page, review_signals
        -- DWELL_BOUNDS): learned from their approvals unless they set them
        -- by hand. No row = learned.
        CREATE TABLE IF NOT EXISTS user_review_settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            dwell_learned INTEGER NOT NULL DEFAULT 1 CHECK(dwell_learned IN (0, 1)),
            dwell_params TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS disabled_models (
            model_type TEXT NOT NULL,
            model_key TEXT NOT NULL,
            disabled_at TEXT NOT NULL,
            disabled_by_user_id INTEGER REFERENCES users(id),
            PRIMARY KEY (model_type, model_key)
        );

        -- Per-user features an admin switched away from their default
        -- (features.py, Admin -> Users). No row = the feature's default.
        CREATE TABLE IF NOT EXISTS user_features (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            feature TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            updated_at TEXT NOT NULL,
            updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            PRIMARY KEY (user_id, feature)
        );

        -- How many loaded copies of a model the app may keep, so that many
        -- documents can use it at once (Admin -> Models). No row = 1.
        CREATE TABLE IF NOT EXISTS model_instances (
            model_type TEXT NOT NULL,
            model_key TEXT NOT NULL,
            instances INTEGER NOT NULL CHECK(instances >= 1),
            updated_at TEXT NOT NULL,
            updated_by_user_id INTEGER REFERENCES users(id),
            PRIMARY KEY (model_type, model_key)
        );
    """)

    # Column additions to already-shipped tables — everything above this
    # point is CREATE TABLE IF NOT EXISTS (idempotent for a whole table but
    # a no-op against an existing one, so it never picks up new columns).
    # _ensure_column makes adding a column to an existing DB file safe to
    # run on every startup, same idempotency guarantee as the tables above.
    _migrate_group_members_role_constraint(conn)
    _ensure_column(conn, 'group_members', 'role',
        "role TEXT NOT NULL DEFAULT 'editor' CHECK(role IN ('viewer', 'editor', 'owner'))")
    _ensure_column(conn, 'collections', 'is_default',
        "is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1))")
    # All optional (profile page — see update_profile) — nobody is required to
    # fill these in, existing accounts just get NULL until they do.
    _ensure_column(conn, 'users', 'first_name', "first_name TEXT")
    _ensure_column(conn, 'users', 'last_name', "last_name TEXT")
    _ensure_column(conn, 'users', 'email', "email TEXT")
    # UI language — persists per account (not just a session/cookie) so it
    # follows the user to any browser/device they log in from. 'en' or 'pl'
    # (see translations.py); the switcher next to Logout writes this via
    # auth.set_language. Anonymous/pre-login pages (login/register) fall
    # back to a session-only choice instead — see auth.load_language.
    _ensure_column(conn, 'users', 'language', "language TEXT NOT NULL DEFAULT 'en' CHECK(language IN ('en', 'pl'))")
    # Partial unique index: at most one is_default=1 row per owner. This is
    # also what makes create_default_collection's race-condition handling
    # (two concurrent first-visits) safe — see its docstring.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collections_one_default_per_user
        ON collections(owner_user_id) WHERE is_default = 1
    """)

    conn.commit()
    conn.close()


def _drop_first_review_settings(conn: sqlite3.Connection) -> None:
    """The first user_review_settings also held per-user rule
    confirmations, which belong to the book instead; it lived for an hour
    and held no rows. Dropped so the current shape is created in its place."""
    cols = {row[1] for row in conn.execute('PRAGMA table_info(user_review_settings)')}
    if 'confirm_mode' in cols:
        conn.execute('DROP TABLE user_review_settings')


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, add_column_ddl: str) -> None:
    """ALTER TABLE ... ADD COLUMN is not idempotent on its own (errors if the
    column already exists), so guard it with a PRAGMA table_info check —
    makes it safe to call unconditionally on every init_db() run, the same
    way `CREATE TABLE IF NOT EXISTS` already is for whole tables."""
    cols = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
    if column not in cols:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {add_column_ddl}')


def _migrate_group_members_role_constraint(conn: sqlite3.Connection) -> None:
    """One-time migration for existing databases: group_members.role's CHECK
    constraint originally only allowed 'viewer'/'editor' (see the
    _ensure_column call below) — 'owner' is now a real, settable per-member
    role too (Viewer/Editor/Owner tags in the groups tab, correlating with
    permission level — see set_group_member_role), not just a derived,
    unchangeable "creator" flag. SQLite can't ALTER a CHECK constraint in
    place, so this recreates the table the standard SQLite way (new table ->
    copy -> drop -> rename) — but only when the column already exists with
    the old, narrower constraint; a brand-new database never hits this path
    since the _ensure_column call below already adds the column with the
    correct 3-value constraint from the start."""
    cols = {row[1] for row in conn.execute('PRAGMA table_info(group_members)')}
    if 'role' not in cols:
        return  # nothing to migrate yet — _ensure_column adds it correctly below
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='group_members'"
    ).fetchone()
    if row is not None and "'owner'" in row[0]:
        return  # already migrated
    conn.executescript("""
        CREATE TABLE group_members_new (
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'editor' CHECK(role IN ('viewer', 'editor', 'owner')),
            PRIMARY KEY (group_id, user_id)
        );
        INSERT INTO group_members_new (group_id, user_id, added_at, role)
            SELECT group_id, user_id, added_at, role FROM group_members;
        DROP TABLE group_members;
        ALTER TABLE group_members_new RENAME TO group_members;
    """)
    # Backfill: every existing group's creator becomes its 'owner' — before
    # this migration, "owner" was only ever a derived display value (see
    # list_group_members' old CASE WHEN), computed from groups.creator_user_id
    # and never actually stored as a role; without this, every existing group
    # would come out of the migration with zero real owners, and thus no one
    # (short of a site admin) able to manage it. One-shot: only runs the one
    # time this function actually migrates the table (guarded above), so a
    # creator who's since been deliberately demoted by a real owner doesn't
    # get silently re-promoted on a later restart.
    conn.execute("""
        UPDATE group_members SET role = 'owner'
        WHERE (group_id, user_id) IN (
            SELECT id, creator_user_id FROM groups
        )
    """)


def _now() -> str:
    return datetime.utcnow().isoformat()


# ---- users ----

def create_user(username: str, password: str) -> int:
    """Insert a new user. The very first user ever created becomes admin;
    everyone after that defaults to 'user'. Raises sqlite3.IntegrityError on
    a duplicate (case-insensitive) username."""
    with _registration_lock:
        db = get_db()
        count = db.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
        role = 'admin' if count == 0 else 'user'
        cur = db.execute(
            'INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
            (username, generate_password_hash(password), role, _now())
        )
        db.commit()
        return cur.lastrowid


def get_user_by_id(user_id: Optional[int]) -> Optional[sqlite3.Row]:
    if user_id is None:
        return None
    return get_db().execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    return get_db().execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()


def verify_password(user_row: sqlite3.Row, password: str) -> bool:
    return check_password_hash(user_row['password_hash'], password)


def set_password(user_id: int, password: str) -> None:
    db = get_db()
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (generate_password_hash(password), user_id))
    db.commit()


def update_profile(user_id: int, first_name: Optional[str], last_name: Optional[str], email: Optional[str]) -> None:
    """All three fields are optional — an empty string is stored as NULL
    rather than '' so 'not filled in' has one consistent representation."""
    db = get_db()
    db.execute(
        'UPDATE users SET first_name = ?, last_name = ?, email = ? WHERE id = ?',
        (first_name or None, last_name or None, email or None, user_id)
    )
    db.commit()


def get_review_settings(user_id: int) -> dict:
    """The user's page-timer choice (see user_review_settings): learned, or
    their own timers in ms."""
    row = get_db().execute('SELECT dwell_learned, dwell_params FROM user_review_settings WHERE user_id = ?',
                           (user_id,)).fetchone()
    if row is None:
        return {'dwell_learned': True, 'dwell_params': None}
    return {'dwell_learned': bool(row['dwell_learned']),
            'dwell_params': json.loads(row['dwell_params']) if row['dwell_params'] else None}


def set_review_settings(user_id: int, dwell_learned: bool, dwell_params: Optional[dict]) -> None:
    """Callers keep the timers within bounds (app.save_review_settings)."""
    db = get_db()
    db.execute(
        """INSERT INTO user_review_settings (user_id, dwell_learned, dwell_params, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET dwell_learned = excluded.dwell_learned,
               dwell_params = excluded.dwell_params, updated_at = excluded.updated_at""",
        (user_id, int(bool(dwell_learned)), json.dumps(dwell_params) if dwell_params else None, _now()))
    db.commit()


def update_language(user_id: int, language: str) -> None:
    """Persists the logged-in user's UI language choice (see the 'language'
    column comment above). Callers validate against the CHECK constraint's
    value set before calling this — see auth.set_language."""
    db = get_db()
    db.execute('UPDATE users SET language = ? WHERE id = ?', (language, user_id))
    db.commit()


def list_users() -> list:
    return get_db().execute('SELECT * FROM users ORDER BY id').fetchall()


def count_users() -> int:
    return get_db().execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']


def admin_create_user(username: str, password: str, role: str = 'user') -> int:
    """Create a user with an explicit role, for use by an already-logged-in
    admin via the admin dashboard — unlike create_user(), this never applies
    the first-user-becomes-admin bootstrap (an admin already exists, or this
    couldn't be reached). Raises sqlite3.IntegrityError on a duplicate
    (case-insensitive) username."""
    if role not in ('user', 'admin'):
        raise ValueError(f'Invalid role: {role}')
    db = get_db()
    cur = db.execute(
        'INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
        (username, generate_password_hash(password), role, _now())
    )
    db.commit()
    return cur.lastrowid


def set_user_role(user_id: int, role: str) -> None:
    if role not in ('user', 'admin'):
        raise ValueError(f'Invalid role: {role}')
    db = get_db()
    db.execute('UPDATE users SET role = ? WHERE id = ?', (role, user_id))
    db.commit()


def delete_user(user_id: int) -> None:
    """Raises sqlite3.IntegrityError if the user still owns jobs or created
    groups (no ON DELETE clause on those FKs — intentional, see auth_routes.py)."""
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()


def count_admins() -> int:
    return get_db().execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'").fetchone()['c']


# ---- groups ----

def create_group(name: str, creator_user_id: int) -> int:
    db = get_db()
    cur = db.execute(
        'INSERT INTO groups (name, creator_user_id, created_at) VALUES (?, ?, ?)',
        (name, creator_user_id, _now())
    )
    group_id = cur.lastrowid
    # 'owner' — the creator starts as the group's (first) owner, same
    # permission tier anyone can later promote another member to (see
    # set_group_member_role) or, in principle, be demoted out of by another
    # owner. groups.creator_user_id itself never changes after this — it's
    # just the historical "who made this" fact, no longer consulted for any
    # permission check (see is_group_owner).
    db.execute(
        'INSERT INTO group_members (group_id, user_id, added_at, role) VALUES (?, ?, ?, ?)',
        (group_id, creator_user_id, _now(), 'owner')
    )
    db.commit()
    return group_id


def get_group(group_id: int) -> Optional[sqlite3.Row]:
    return get_db().execute('SELECT * FROM groups WHERE id = ?', (group_id,)).fetchone()


def delete_group(group_id: int) -> None:
    """Deleting a group never fails on foreign keys the way delete_user/
    delete_collection can: group_members cascades (ON DELETE CASCADE), and
    both job_owners.group_id and collections.group_id fall back to NULL
    (ON DELETE SET NULL) — any job/collection shared with this group just
    becomes private again rather than blocking the delete or vanishing."""
    db = get_db()
    db.execute('DELETE FROM groups WHERE id = ?', (group_id,))
    db.commit()


def list_groups_for_user(user: Optional[sqlite3.Row]) -> list:
    """Every group this user belongs to — or every group there is, for an
    admin (mirrors list_jobs_visible_to's admin bypass: full group
    visibility/management, see is_group_owner and the /groups routes in
    auth_routes.py, which already let an admin manage any group's members
    once they're on its page — this is what makes that page reachable via
    the groups list and every group/collection picker in the first place,
    instead of only groups they happen to be a member of)."""
    if user is None:
        return []
    if user['role'] == 'admin':
        return get_db().execute('SELECT * FROM groups ORDER BY name').fetchall()
    return get_db().execute("""
        SELECT g.* FROM groups g
        JOIN group_members gm ON gm.group_id = g.id
        WHERE gm.user_id = ?
        ORDER BY g.name
    """, (user['id'],)).fetchall()


def add_group_member(group_id: int, user_id: int, role: str = 'editor') -> None:
    if role not in ('viewer', 'editor', 'owner'):
        raise ValueError(f"role must be 'viewer', 'editor', or 'owner', got {role!r}")
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO group_members (group_id, user_id, added_at, role) VALUES (?, ?, ?, ?)',
        (group_id, user_id, _now(), role)
    )
    db.commit()


def remove_group_member(group_id: int, user_id: int) -> None:
    db = get_db()
    db.execute('DELETE FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, user_id))
    db.commit()


def list_group_members(group_id: int) -> list:
    """Each row's 'role' is the member's real, stored permission tier —
    'owner', 'editor', or 'viewer' (see set_group_member_role) — which is
    also what group_detail.html displays as the Owner/Editor/Viewer tag and
    what can_edit_job/can_edit_collection/is_group_owner key off. Distinct
    from 'site_role', the member's site-wide user/admin role (unrelated to
    any particular group)."""
    return get_db().execute("""
        SELECT u.id, u.username, u.role AS site_role, gm.added_at, gm.role AS role
        FROM group_members gm
        JOIN users u ON u.id = gm.user_id
        WHERE gm.group_id = ?
        ORDER BY gm.added_at
    """, (group_id,)).fetchall()


def get_group_member_role(group_id: int, user_id: int) -> Optional[str]:
    """The stored permission tier for this membership — 'owner', 'editor',
    'viewer', or None if not a member at all."""
    row = get_db().execute(
        'SELECT role FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, user_id)
    ).fetchone()
    return row['role'] if row else None


def set_group_member_role(group_id: int, user_id: int, role: str) -> None:
    if role not in ('viewer', 'editor', 'owner'):
        raise ValueError(f"role must be 'viewer', 'editor', or 'owner', got {role!r}")
    db = get_db()
    db.execute('UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?',
               (role, group_id, user_id))
    db.commit()


def is_group_member(group_id: int, user_id: int) -> bool:
    row = get_db().execute(
        'SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, user_id)
    ).fetchone()
    return row is not None


def is_group_owner(group_id: int, user_id: int) -> bool:
    """'owner' is now a real, settable per-member role (Viewer/Editor/Owner,
    correlating with permission level — see set_group_member_role), not a
    derived/unchangeable "creator" flag — this is what actually gates
    add/remove-member and role-change rights (can_manage in group_detail and
    its API mirrors), replacing the old is_group_creator check. Any owner
    can promote another member to owner too ("set another owner")."""
    return get_group_member_role(group_id, user_id) == 'owner'


def count_group_owners(group_id: int) -> int:
    """Used to block demoting/removing a group's last remaining owner (see
    set_group_member_role/remove_group_member) — since ownership is now
    freely reassignable rather than a fixed creator, nothing else stops a
    group from accidentally ending up with none at all."""
    return get_db().execute(
        "SELECT COUNT(*) AS c FROM group_members WHERE group_id = ? AND role = 'owner'", (group_id,)
    ).fetchone()['c']


# ---- job ownership ----

def set_job_owner(job_id: str, owner_user_id: int, group_id: Optional[int]) -> None:
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO job_owners (job_id, owner_user_id, group_id, created_at) VALUES (?, ?, ?, ?)',
        (job_id, owner_user_id, group_id, _now())
    )
    db.commit()


def get_job_owner(job_id: str) -> Optional[sqlite3.Row]:
    return get_db().execute('SELECT * FROM job_owners WHERE job_id = ?', (job_id,)).fetchone()


def update_job_group(job_id: str, group_id: Optional[int]) -> None:
    db = get_db()
    db.execute('UPDATE job_owners SET group_id = ? WHERE job_id = ?', (group_id, job_id))
    db.commit()


def describe_job_owner(job_id: str) -> dict:
    """Human-readable ownership/sharing info for display in history.html etc."""
    owner = get_job_owner(job_id)
    if owner is None:
        return {'is_legacy': True, 'owner_username': None, 'group_name': None}
    owner_user = get_user_by_id(owner['owner_user_id'])
    group = get_group(owner['group_id']) if owner['group_id'] else None
    return {
        'is_legacy': False,
        'owner_username': owner_user['username'] if owner_user else None,
        'group_name': group['name'] if group else None,
    }


def _job_visible_via_shared_collection(job_id: str, user_id: int) -> bool:
    """True if job_id is filed into any collection whose sharing group the
    user belongs to — the sole path by which group sharing now works (see
    can_access_job's docstring: only collections are shareable with a group,
    not individual jobs, going forward). Checked in addition to, not instead
    of, the legacy job_owners.group_id path, so jobs shared that way before
    this change keep working."""
    row = get_db().execute("""
        SELECT 1 FROM collection_jobs cj
        JOIN collections c ON c.id = cj.collection_id
        JOIN group_members gm ON gm.group_id = c.group_id
        WHERE cj.job_id = ? AND gm.user_id = ?
        LIMIT 1
    """, (job_id, user_id)).fetchone()
    return row is not None


def can_access_job(user: Optional[sqlite3.Row], job_id: str) -> bool:
    if user is None:
        return False
    if user['role'] == 'admin':
        return True
    owner = get_job_owner(job_id)
    if owner is not None:
        if owner['owner_user_id'] == user['id']:
            return True
        if owner['group_id'] is not None and is_group_member(owner['group_id'], user['id']):
            return True
    # No job_owners row (legacy) doesn't automatically mean admin-only
    # anymore — it can still be visible via a shared collection.
    return _job_visible_via_shared_collection(job_id, user['id'])


def can_edit_job(user: Optional[sqlite3.Row], job_id: str) -> bool:
    """Stricter than can_access_job: admin, the job's owner, or a group
    member whose role for that job's sharing group is 'editor' or 'owner'
    (not 'viewer'). Backs both editing (footnote/reference/bbox mutations,
    NER/OCR suite switching) AND deleting a job shared via a group — group
    editors/owners gaining delete rights over shared jobs is a deliberate
    capability (previously only the job's own owner/admin could delete at
    all). share_job (moving a job to a different group) deliberately does
    NOT use this — it stays on job_owner_or_admin_required, no group-role
    escalation, since reassigning who a job is shared with is
    higher-blast-radius than editing content already shared with your own
    group. Also grants edit access via a shared *collection* (see
    _job_visible_via_shared_collection) — only collections are shareable
    with a group going forward, not individual jobs, so this is the primary
    path for anyone but the job's own owner."""
    if user is None:
        return False
    if user['role'] == 'admin':
        return True
    owner = get_job_owner(job_id)
    if owner is not None:
        if owner['owner_user_id'] == user['id']:
            return True
        if owner['group_id'] is not None and get_group_member_role(owner['group_id'], user['id']) in ('editor', 'owner'):
            return True
    row = get_db().execute("""
        SELECT 1 FROM collection_jobs cj
        JOIN collections c ON c.id = cj.collection_id
        JOIN group_members gm ON gm.group_id = c.group_id
        WHERE cj.job_id = ? AND gm.user_id = ? AND gm.role IN ('editor', 'owner')
        LIMIT 1
    """, (job_id, user['id'])).fetchone()
    return row is not None


def list_jobs_visible_to(user: Optional[sqlite3.Row], all_job_ids: Iterable[str]) -> set:
    all_job_ids = set(all_job_ids)
    if user is None:
        return set()
    if user['role'] == 'admin':
        return all_job_ids
    rows = get_db().execute("""
        SELECT job_id FROM job_owners
        WHERE owner_user_id = ?
           OR group_id IN (SELECT group_id FROM group_members WHERE user_id = ?)
        UNION
        SELECT cj.job_id FROM collection_jobs cj
        JOIN collections c ON c.id = cj.collection_id
        JOIN group_members gm ON gm.group_id = c.group_id
        WHERE gm.user_id = ?
    """, (user['id'], user['id'], user['id'])).fetchall()
    return {r['job_id'] for r in rows} & all_job_ids


# ---- collections ----
# A collection is a named (description optional), user-owned set of job
# references, with an optional group_id for sharing — deliberately mirroring
# the job_owners model above (private by default, shareable by setting a
# group) rather than inventing a second sharing scheme.

def create_collection(name: str, description: Optional[str], owner_user_id: int,
                       group_id: Optional[int]) -> int:
    db = get_db()
    cur = db.execute(
        'INSERT INTO collections (name, description, owner_user_id, group_id, created_at) VALUES (?, ?, ?, ?, ?)',
        (name, description, owner_user_id, group_id, _now())
    )
    db.commit()
    return cur.lastrowid


def get_collection(collection_id: int) -> Optional[sqlite3.Row]:
    return get_db().execute('SELECT * FROM collections WHERE id = ?', (collection_id,)).fetchone()


def list_collections_for_user(user: Optional[sqlite3.Row]) -> list:
    """Collections owned by this user, or shared with a group they belong to
    — or every collection there is, for an admin (mirrors
    list_groups_for_user's admin bypass). Includes group_name (via a LEFT
    JOIN on groups — null for private, unshared collections) so callers like
    collections.html's "Your Collections" list can show which group owns a
    shared collection without a separate lookup."""
    if user is None:
        return []
    if user['role'] == 'admin':
        return get_db().execute("""
            SELECT c.*, g.name AS group_name FROM collections c
            LEFT JOIN groups g ON g.id = c.group_id
            ORDER BY c.name
        """).fetchall()
    return get_db().execute("""
        SELECT DISTINCT c.*, g.name AS group_name FROM collections c
        LEFT JOIN group_members gm ON gm.group_id = c.group_id AND gm.user_id = ?
        LEFT JOIN groups g ON g.id = c.group_id
        WHERE c.owner_user_id = ? OR gm.user_id = ?
        ORDER BY c.name
    """, (user['id'], user['id'], user['id'])).fetchall()


def list_collection_memberships_for_jobs(job_ids, user_id: int) -> dict:
    """Bulk {job_id: [{'id', 'name'}, ...]} of every collection (visible to
    user_id) each job in job_ids already belongs to — one query instead of
    one-per-row, for the "Collections" column on the merged uploads page
    (collections.html's default-collection listing)."""
    job_ids = list(job_ids)
    if not job_ids:
        return {}
    placeholders = ','.join('?' for _ in job_ids)
    rows = get_db().execute(f"""
        SELECT cj.job_id AS job_id, c.id AS collection_id, c.name AS collection_name
        FROM collection_jobs cj
        JOIN collections c ON c.id = cj.collection_id
        LEFT JOIN group_members gm ON gm.group_id = c.group_id AND gm.user_id = ?
        WHERE cj.job_id IN ({placeholders}) AND (c.owner_user_id = ? OR gm.user_id = ?)
        ORDER BY c.name
    """, (user_id, *job_ids, user_id, user_id)).fetchall()
    memberships = {}
    for row in rows:
        memberships.setdefault(row['job_id'], []).append({'id': row['collection_id'], 'name': row['collection_name']})
    return memberships


def update_collection(collection_id: int, name: str, description: Optional[str]) -> None:
    db = get_db()
    db.execute('UPDATE collections SET name = ?, description = ? WHERE id = ?',
               (name, description, collection_id))
    db.commit()


def update_collection_group(collection_id: int, group_id: Optional[int]) -> None:
    db = get_db()
    db.execute('UPDATE collections SET group_id = ? WHERE id = ?', (group_id, collection_id))
    db.commit()


def delete_collection(collection_id: int) -> None:
    db = get_db()
    db.execute('DELETE FROM collections WHERE id = ?', (collection_id,))
    db.commit()


def is_collection_owner(collection_id: int, user_id: int) -> bool:
    collection = get_collection(collection_id)
    return collection is not None and collection['owner_user_id'] == user_id


def can_access_collection(user: Optional[sqlite3.Row], collection_id: int) -> bool:
    """Owner, admin, or any fellow group member — mirrors can_access_job:
    membership in the collection's group grants collaborative read/add-job
    rights, not just visibility (see collection_owner_or_admin_required in
    auth.py for the stricter tier used by rename/reshare/delete)."""
    if user is None:
        return False
    if user['role'] == 'admin':
        return True
    collection = get_collection(collection_id)
    if collection is None:
        return False
    if collection['owner_user_id'] == user['id']:
        return True
    return collection['group_id'] is not None and is_group_member(collection['group_id'], user['id'])


def can_edit_collection(user: Optional[sqlite3.Row], collection_id: int) -> bool:
    """Mirrors can_edit_job for collections: admin, the collection's owner,
    or a group member with 'editor' or 'owner' role for the collection's
    sharing group. Governs add_job_to_collection/remove_job_from_collection
    (previously any collection-accessible user could do these, not just an
    editor)."""
    if user is None:
        return False
    if user['role'] == 'admin':
        return True
    collection = get_collection(collection_id)
    if collection is None:
        return False
    if collection['owner_user_id'] == user['id']:
        return True
    if collection['group_id'] is None:
        return False
    return get_group_member_role(collection['group_id'], user['id']) in ('editor', 'owner')


def add_job_to_collection(collection_id: int, job_id: str) -> None:
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO collection_jobs (collection_id, job_id, added_at) VALUES (?, ?, ?)',
        (collection_id, job_id, _now())
    )
    db.commit()


def remove_job_from_collection(collection_id: int, job_id: str) -> None:
    db = get_db()
    db.execute('DELETE FROM collection_jobs WHERE collection_id = ? AND job_id = ?', (collection_id, job_id))
    db.commit()


def list_jobs_in_collection(collection_id: int) -> list:
    return get_db().execute(
        'SELECT job_id FROM collection_jobs WHERE collection_id = ? ORDER BY added_at', (collection_id,)
    ).fetchall()


def list_collections_shared_with_group(group_id: int) -> list:
    """Every collection shared with this group — group_detail's "Shared
    Collections" section. Only collections are shareable with a group (see
    can_access_job's docstring); a job is only visible to the group by being
    filed into one of these."""
    return get_db().execute(
        'SELECT * FROM collections WHERE group_id = ? ORDER BY name', (group_id,)
    ).fetchall()


def list_jobs_owned_by(owner_user_id: int) -> list:
    return get_db().execute('SELECT job_id FROM job_owners WHERE owner_user_id = ?', (owner_user_id,)).fetchall()


def purge_job_references(job_id: str) -> None:
    """Clean up the SQL-side rows that reference a job after it's deleted
    from processing_status/history.json. Neither job_owners nor
    collection_jobs has a real FK to a jobs table (jobs live only in the
    JSON history, not SQL — see module docstring), so nothing cascades this
    automatically; call explicitly from delete_job and bulk-delete."""
    db = get_db()
    db.execute('DELETE FROM job_owners WHERE job_id = ?', (job_id,))
    db.execute('DELETE FROM collection_jobs WHERE job_id = ?', (job_id,))
    db.commit()


# ---- default "User's Uploads" collection ----
# One auto-created, real collections row per user (owner_user_id, is_default=1
# — identity is this flag, never the name, so renaming it is safe), backfilled
# with their existing owned-but-unfiled jobs the first time they visit the
# merged uploads/collections page. See auth_routes.list_collections.

def get_default_collection(user_id: int) -> Optional[sqlite3.Row]:
    return get_db().execute(
        'SELECT * FROM collections WHERE owner_user_id = ? AND is_default = 1', (user_id,)
    ).fetchone()


def create_default_collection(user_id: int) -> int:
    """Creates this user's default collection. Two concurrent first-visits
    (e.g. two tabs) can race here — the partial unique index
    (idx_collections_one_default_per_user) makes the loser's INSERT raise
    IntegrityError, which is caught here so the loser just returns the
    winner's id instead of the request failing."""
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO collections (name, description, owner_user_id, group_id, created_at, is_default) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("User's Uploads", None, user_id, None, _now())
        )
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        db.rollback()
        existing = get_default_collection(user_id)
        if existing is None:
            raise  # something else caused the IntegrityError — don't mask it
        return existing['id']


def list_job_ids_in_any_collection_owned_by(user_id: int) -> set:
    """Every job_id already filed into one of this user's own collections —
    used by the default-collection backfill to exclude jobs that are already
    organized somewhere, so they aren't duplicated into 'User's Uploads'
    too."""
    rows = get_db().execute("""
        SELECT DISTINCT cj.job_id FROM collection_jobs cj
        JOIN collections c ON c.id = cj.collection_id
        WHERE c.owner_user_id = ?
    """, (user_id,)).fetchall()
    return {r['job_id'] for r in rows}


# ---- API keys ----
# Lets a user authenticate scripts/API clients without a browser session.
# Only a sha256 hash of the raw key is ever stored (the key itself already
# has ~256 bits of entropy from secrets.token_urlsafe, so — unlike a
# user-chosen password — there's no benefit to a slow, salted KDF here, and
# a fast hash keeps every API-authenticated request cheap). The raw key is
# only ever available once, at creation time; key_prefix is stored purely so
# a user can tell their keys apart afterward without ever seeing the value
# again.

def _hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


def create_api_key(user_id: int, name: str) -> Tuple[int, str]:
    """Returns (key_id, raw_key). raw_key is shown to the user exactly once —
    the caller is responsible for displaying it immediately."""
    raw_key = 'fnk_' + secrets.token_urlsafe(32)
    db = get_db()
    cur = db.execute(
        'INSERT INTO api_keys (user_id, name, key_hash, key_prefix, created_at) VALUES (?, ?, ?, ?, ?)',
        (user_id, name, _hash_api_key(raw_key), raw_key[:12], _now())
    )
    db.commit()
    return cur.lastrowid, raw_key


def list_api_keys_for_user(user_id: int) -> list:
    return get_db().execute("""
        SELECT id, name, key_prefix, created_at, last_used_at, revoked_at
        FROM api_keys WHERE user_id = ? ORDER BY created_at DESC
    """, (user_id,)).fetchall()


def revoke_api_key(key_id: int, user_id: int) -> None:
    """Scoped to user_id so one user can't revoke another's key by guessing an id."""
    db = get_db()
    db.execute('UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL',
               (_now(), key_id, user_id))
    db.commit()


def get_user_by_api_key(raw_key: str) -> Optional[sqlite3.Row]:
    """Resolves a raw API key to its owning user, or None if it's missing,
    unknown, or revoked. Touches last_used_at on every successful lookup."""
    if not raw_key:
        return None
    db = get_db()
    row = db.execute(
        'SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL', (_hash_api_key(raw_key),)
    ).fetchone()
    if row is None:
        return None
    db.execute('UPDATE api_keys SET last_used_at = ? WHERE id = ?', (_now(), row['id']))
    db.commit()
    return get_user_by_id(row['user_id'])


# ---- model enable/disable (admin) ----
# model_type is one of 'od_model' / 'ner_suite' / 'ocr_engine'. Presence of a
# row in disabled_models means that model is currently turned off — greyed
# out (as a disabled <option>) in the upload form, and rejected server-side
# if requested anyway (see app.py's _validate_model_selection), so this is a
# real switch, not just a UI hint.

def list_disabled_model_keys(model_type: str) -> set:
    rows = get_db().execute(
        'SELECT model_key FROM disabled_models WHERE model_type = ?', (model_type,)
    ).fetchall()
    return {r['model_key'] for r in rows}


def is_model_disabled(model_type: str, model_key: str) -> bool:
    return get_db().execute(
        'SELECT 1 FROM disabled_models WHERE model_type = ? AND model_key = ?', (model_type, model_key)
    ).fetchone() is not None


def set_model_disabled(model_type: str, model_key: str, disabled: bool, admin_user_id: Optional[int] = None) -> None:
    db = get_db()
    if disabled:
        db.execute(
            'INSERT OR REPLACE INTO disabled_models (model_type, model_key, disabled_at, disabled_by_user_id) '
            'VALUES (?, ?, ?, ?)',
            (model_type, model_key, _now(), admin_user_id)
        )
    else:
        db.execute('DELETE FROM disabled_models WHERE model_type = ? AND model_key = ?', (model_type, model_key))
    db.commit()


# ---- per-user features (admin; see features.py) ----

def get_user_features(user_id: int) -> dict:
    """{feature: bool} for the features set for this user; others keep their default."""
    rows = get_db().execute('SELECT feature, enabled FROM user_features WHERE user_id = ?', (user_id,)).fetchall()
    return {r['feature']: bool(r['enabled']) for r in rows}


def list_user_features() -> dict:
    """{user_id: {feature: bool}} for every user with a feature set."""
    out = {}
    for r in get_db().execute('SELECT user_id, feature, enabled FROM user_features').fetchall():
        out.setdefault(r['user_id'], {})[r['feature']] = bool(r['enabled'])
    return out


def set_user_feature(user_id: int, feature: str, enabled: bool, admin_user_id: Optional[int] = None) -> None:
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO user_features (user_id, feature, enabled, updated_at, updated_by_user_id) '
        'VALUES (?, ?, ?, ?, ?)',
        (user_id, feature, 1 if enabled else 0, _now(), admin_user_id)
    )
    db.commit()


# ---- model instances (admin) ----

def list_model_instances() -> dict:
    """{(model_type, model_key): copies} for every model set above 1."""
    rows = get_db().execute('SELECT model_type, model_key, instances FROM model_instances').fetchall()
    return {(r['model_type'], r['model_key']): r['instances'] for r in rows}


def set_model_instances(model_type: str, model_key: str, instances: int, admin_user_id: Optional[int] = None) -> None:
    db = get_db()
    if instances <= 1:
        db.execute('DELETE FROM model_instances WHERE model_type = ? AND model_key = ?', (model_type, model_key))
    else:
        db.execute(
            'INSERT OR REPLACE INTO model_instances (model_type, model_key, instances, updated_at, updated_by_user_id) '
            'VALUES (?, ?, ?, ?, ?)',
            (model_type, model_key, instances, _now(), admin_user_id)
        )
    db.commit()
