#!/usr/bin/env python3
"""
Provisioner Portal — Sol Provision org site.

A SEPARATE Flask app from the tools app (app/server.py). They share exactly two
things and talk to each other never:

  1. shared/brand/  — the design token layer, served at /brand/
  2. mee6_snapshots.db — the Discord member roster, opened HERE READ-ONLY

Auth mirrors the tools app: the browser authenticates to Firebase (Discord as an
OIDC provider), posts the resulting ID token here, and this app verifies it and
matches identities['oidc.discord'] against the member roster. The two apps each
verify independently against the same Firebase project — there is no call
between them.

NOTE: the portal deliberately keeps its OWN session (distinct cookie name, no
SESSION_COOKIE_DOMAIN). Cross-subdomain SSO with the tools app is a later,
deliberate change — turning it on requires editing the tools app's session
config, which would invalidate every live tools session on deploy.
"""

import os
import sqlite3
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_from_directory, session)

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / 'shared' / 'brand'

app = Flask(__name__, template_folder='templates', static_folder='static')

# ══════════════════════════════════════════════════════════════════════
# ENVIRONMENT
# ══════════════════════════════════════════════════════════════════════
# Explicit env var wins. The tools app infers this from its install path and
# got bitten once when prod matched a dev-looking path (see the comment above
# `is_dev` in app/server.py) — so here the systemd unit states it outright and
# path sniffing is only a fallback for when nobody set it.
_env = os.environ.get('PORTAL_ENV', '').strip().lower()
if _env not in ('local', 'dev', 'prod'):
    if os.name == 'nt':
        _env = 'local'
    elif str(Path(__file__).resolve()).startswith('/var/www/sol-provision-tools-dev'):
        _env = 'dev'
    else:
        _env = 'prod'

IS_LOCAL = _env == 'local'
IS_DEV = _env == 'dev'
ENV = _env

# The dev portal is restricted to ranks 4+, matching the tools dev deployment,
# so in-progress work isn't visible to the whole org.
DEV_MIN_RANK = 4
DEV_AREA_DENIED = 'You are not authorized to access the Sol Provision Development area'

# ══════════════════════════════════════════════════════════════════════
# SESSION
# ══════════════════════════════════════════════════════════════════════
app.secret_key = os.environ.get('PORTAL_SECRET_KEY', 'dev-secret-key-change-in-prod')
app.config['SESSION_COOKIE_SECURE'] = not IS_LOCAL
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
# Distinct per environment. If cross-subdomain SSO is ever enabled, a cookie
# scoped to .solprovision.com is sent to dev AND prod; identical names would
# make each environment invalidate the other's cookie on every hop.
app.config['SESSION_COOKIE_NAME'] = 'sp_portal_session' + ('' if ENV == 'prod' else f'_{ENV}')

# ══════════════════════════════════════════════════════════════════════
# FIREBASE
# ══════════════════════════════════════════════════════════════════════
# ONE Firebase project — sp-ledger — for every environment.
#
# This looks like it should be split dev/prod, and the tools app appears to
# split it, but that appearance is misleading and cost us an investigation:
# app/server.py picks a different `databaseURL` per environment
# (sp-ledger-dev-default-rtdb vs sp-ledger-default-rtdb), which is the REALTIME
# DATABASE, not the auth project. The auth project comes from the
# service-account JSON, and the files at both the dev and prod paths on the VPS
# are for `sp-ledger` (verified 2026-08-14). That is also why the Firebase
# console shows a single Web API key.
#
# So: dev and prod authenticate against the same project and differ only in
# which Realtime Database they read — which the portal doesn't use at all.
# Both are still env-overridable if that ever changes, and the startup guard
# below refuses to run on a project/credential mismatch.
#
# The web API key is not a secret: it identifies the project to Firebase's
# public endpoints, and access is governed by Firebase rules plus the
# authorized-domains list.
FIREBASE_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID', 'sp-ledger')
FIREBASE_API_KEY = os.environ.get(
    'FIREBASE_API_KEY', 'AIzaSyDsVV5hNkPyk8QMW0zWxM7TwN3XkeFs82E')
FIREBASE_AUTH_DOMAIN = os.environ.get(
    'FIREBASE_AUTH_DOMAIN', f'{FIREBASE_PROJECT_ID}.firebaseapp.com')

FIREBASE_READY = False       # Admin SDK can verify tokens
firebase_auth = None


def _init_firebase():
    """Initialise the Admin SDK if a credential is available.

    Kept tolerant on purpose: local development should be able to boot and
    render pages without a service-account file present. When it isn't
    initialised, /api/auth/verify returns 503 rather than pretending to work.
    """
    global FIREBASE_READY, firebase_auth

    cred_path = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if not cred_path:
        # The tools app in this same checkout already holds the credential for
        # THIS environment's project — dev checkout has sp-ledger-dev's, prod
        # checkout has sp-ledger's — so the fallback lands on the right one.
        candidate = REPO_ROOT / 'app' / 'firebase-service-account.json'
        cred_path = str(candidate) if candidate.exists() else None

    if not cred_path or not Path(cred_path).exists():
        app.logger.warning(
            'Firebase service account not found — auth verification disabled. '
            'Set FIREBASE_SERVICE_ACCOUNT to enable it.')
        return

    # Fail loudly on project drift rather than at login time. A credential for
    # one project cannot verify tokens minted by another, and the resulting
    # error surfaces to members as an opaque "verification failed".
    try:
        import json
        cred_project = json.loads(Path(cred_path).read_text(encoding='utf-8')).get('project_id')
        if cred_project and cred_project != FIREBASE_PROJECT_ID:
            app.logger.error(
                'Firebase project mismatch: service account is for %r but the '
                'client is configured for %r. Tokens minted by the client will '
                'NOT verify. Fix FIREBASE_PROJECT_ID or the credential file.',
                cred_project, FIREBASE_PROJECT_ID)
            return
    except Exception as exc:
        app.logger.warning('Could not read project_id from credential: %s', exc)

    try:
        import firebase_admin
        from firebase_admin import auth as _auth
        from firebase_admin import credentials
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(cred_path))
        firebase_auth = _auth
        FIREBASE_READY = True
        app.logger.info('Firebase initialised (env=%s project=%s)', ENV, FIREBASE_PROJECT_ID)
    except Exception as exc:  # pragma: no cover - startup diagnostics
        app.logger.error('Firebase init failed: %s', exc)


_init_firebase()

if FIREBASE_READY and not FIREBASE_API_KEY:
    app.logger.error(
        'FIREBASE_API_KEY is not set for env=%s (project=%s) — the browser SDK '
        'cannot initialise, so sign-in is disabled even though this server '
        'could verify tokens.', ENV, FIREBASE_PROJECT_ID)

# Sign-in is only offered when BOTH halves are wired: this server can verify
# tokens, and the page has enough config to mint one.
AUTH_ENABLED = FIREBASE_READY and bool(FIREBASE_API_KEY)

# ══════════════════════════════════════════════════════════════════════
# MEMBER ROSTER (READ-ONLY)
# ══════════════════════════════════════════════════════════════════════
def _members_db_path():
    path = os.environ.get('MEE6_DB')
    if path:
        return path
    if os.name == 'nt':
        return str(REPO_ROOT / 'mee6_snapshots.db')
    return '/var/www/sparqy/data/mee6_snapshots.db'


def get_members_db():
    """Open the Discord member roster READ-ONLY.

    Enforced at the connection level with `mode=ro`, not by convention: on the
    VPS the dev deployment is pointed at the same live databases as prod, so a
    stray write from portal-dev would hit real member data. The tools app owns
    writes to this file (it stamps last_login); the portal only ever reads.
    """
    uri = Path(_members_db_path()).resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rank_int(rank):
    """Coerce a rank to int; treat missing/unparseable as 0 so a drifting
    column can never accidentally grant access."""
    try:
        return int(rank) if rank is not None else 0
    except (TypeError, ValueError):
        return 0


def lookup_member(discord_id):
    """Most recent roster row for a Discord ID, or None."""
    conn = get_members_db()
    try:
        return conn.execute(
            '''SELECT user_id, username, display_name, rank, division, roles, join_date
               FROM discord_members
               WHERE user_id = ?
               ORDER BY snapshot_date DESC
               LIMIT 1''',
            (str(discord_id),),
        ).fetchone()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# AUTH GATES
# ══════════════════════════════════════════════════════════════════════
def require_org_member(f):
    """API gate: session present AND still on the roster."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        discord_id = session.get('discord_id')
        if not discord_id:
            return jsonify({'error': 'Not authenticated'}), 401
        if not lookup_member(discord_id):
            session.clear()
            return jsonify({'error': 'No longer an org member'}), 403
        return f(*args, **kwargs)
    return wrapper


def require_page_login(f):
    """Page gate: anonymous visitors bounce to the landing page, which hosts
    the login overlay. On dev, members below DEV_MIN_RANK are refused."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('discord_id'):
            return redirect('/')
        if IS_DEV and rank_int(session.get('rank')) < DEV_MIN_RANK:
            return DEV_AREA_DENIED, 403
        return f(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════════════
# SHARED BRAND LAYER
# ══════════════════════════════════════════════════════════════════════
@app.route('/brand/<path:filename>')
def brand_static(filename):
    """Serve the shared brand bundle (brand.css + the woff2 faces).

    Same contract as the tools app: nginx may shortcut this with its own
    /brand/ location in production, but this route keeps the app self-contained
    and makes local development work with no web-server config.
    """
    return send_from_directory(BRAND_DIR, filename)


@app.context_processor
def inject_globals():
    def asset_v(rel_path):
        """mtime cache-busting stamp. 'brand/…' resolves against BRAND_DIR —
        without that a token change would ship behind a stale ?v=."""
        if rel_path.startswith('brand/'):
            base, rel = BRAND_DIR, rel_path[len('brand/'):]
        else:
            base, rel = app.static_folder, rel_path
        try:
            return int(os.path.getmtime(os.path.join(base, rel)))
        except OSError:
            return ''

    return {
        'asset_v': asset_v,
        'env': ENV,
        'firebase_config': {
            'apiKey': FIREBASE_API_KEY,
            'authDomain': FIREBASE_AUTH_DOMAIN,
            'projectId': FIREBASE_PROJECT_ID,
        },
        'auth_enabled': AUTH_ENABLED,
    }


# ══════════════════════════════════════════════════════════════════════
# AUTH API
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/auth/verify', methods=['POST'])
def verify_auth():
    """Verify a Firebase ID token and establish a portal session.

    Mirrors the tools app's flow, with two deliberate differences: this app
    never writes to the roster (no last_login stamp — the tools app owns that),
    and the Firebase project is taken from config shared with the template.
    """
    if not FIREBASE_READY:
        return jsonify({
            'error': 'Auth unavailable',
            'message': 'Authentication is not configured on this server.',
        }), 503

    id_token = (request.get_json(silent=True) or {}).get('idToken')
    if not id_token:
        return jsonify({'error': 'No token provided'}), 401

    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except Exception as exc:
        app.logger.warning('Token verification failed: %s', exc)
        return jsonify({'error': 'Invalid token'}), 401

    identities = decoded.get('firebase', {}).get('identities', {})
    discord_ids = identities.get('oidc.discord', [])
    if not discord_ids:
        app.logger.warning('Token carried no Discord identity (uid=%s)', decoded.get('uid'))
        return jsonify({
            'error': 'Discord ID not found in token',
            'message': 'Authentication token is missing Discord information.',
        }), 401

    discord_id = discord_ids[0]
    member = lookup_member(discord_id)
    if not member:
        return jsonify({
            'error': 'Not a Sol Provision member',
            'message': 'You must be a member of the Sol Provision Discord server.',
        }), 403

    if IS_DEV and rank_int(member['rank']) < DEV_MIN_RANK:
        return jsonify({'error': 'Not authorized', 'message': DEV_AREA_DENIED}), 403

    session['discord_id'] = discord_id
    session['username'] = member['username']
    session['callsign'] = member['display_name']
    session['rank'] = member['rank']
    session['division'] = member['division']
    session.permanent = True

    return jsonify({
        'discord_id': discord_id,
        'username': member['username'],
        'callsign': member['display_name'],
        'rank': member['rank'],
        'division': member['division'],
        'join_date': member['join_date'],
        'verified': True,
    })


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/me', methods=['GET'])
def current_user():
    discord_id = session.get('discord_id')
    if not discord_id:
        return jsonify({'error': 'Not authenticated'}), 401
    return jsonify({
        'discord_id': discord_id,
        'username': session.get('username'),
        'callsign': session.get('callsign'),
        'rank': session.get('rank'),
        'division': session.get('division'),
    })


@app.route('/api/health')
def health():
    """Cheap liveness probe that also reports whether auth is wired up."""
    roster_ok = True
    try:
        get_members_db().close()
    except Exception:
        roster_ok = False
    return jsonify({
        'ok': True,
        'env': ENV,
        'firebase_project': FIREBASE_PROJECT_ID,
        'token_verification': FIREBASE_READY,   # Admin SDK loaded with a matching credential
        'client_config': bool(FIREBASE_API_KEY),  # page has enough config to sign in
        'auth_enabled': AUTH_ENABLED,           # both halves wired
        'roster_readable': roster_ok,
    })


# ══════════════════════════════════════════════════════════════════════
# PAGES
# ══════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    """Landing page. Hosts the login overlay; content behind it stays blurred
    until a session exists, matching the tools app's pattern."""
    return render_template('index.html')


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Provisioner Portal')
    parser.add_argument('--port', type=int, default=5002)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    print(f'Provisioner Portal  |  env={ENV}  |  firebase={FIREBASE_PROJECT_ID}  '
          f'|  auth={"on" if AUTH_ENABLED else "OFF"}  |  http://localhost:{args.port}')
    app.run(debug=args.debug, port=args.port, host='0.0.0.0')
