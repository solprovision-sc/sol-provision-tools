"""Read-only SQL query interface for officers. Auth is handled by nginx,
plus a session-level gate below so direct URL access without a login is
still refused if the proxy ever lets a request through."""
from flask import (Blueprint, render_template, request, jsonify, current_app,
                   session, redirect)
import sqlite3
import os
import time

officer_db = Blueprint('officer_db', __name__)

MAX_ROWS = 5000
QUERY_TIMEOUT_S = 10
ALLOWED_PREFIXES = ('SELECT', 'WITH', 'EXPLAIN')


@officer_db.before_request
def _require_login():
    """Mirror of server.require_page_login for this blueprint (defined here
    to avoid a circular import). Anonymous visitors go to the dashboard's
    Discord login; on the dev deployment ranks below 4 are refused."""
    if not session.get('discord_id'):
        if request.method != 'GET' or request.path != '/officer-db':
            return jsonify({'error': 'Not authenticated'}), 401
        return redirect('/')
    if current_app.config.get('IS_DEV'):
        try:
            rank_n = int(session.get('rank') or 0)
        except (TypeError, ValueError):
            rank_n = 0
        if rank_n < 4:
            return ('You are not authorized to access the '
                    'Sol Provision Development area'), 403


def get_readonly_conn():
    """Open dataforge.db in true read-only mode."""
    db_path = (current_app.config.get('DB_PATH')
               or os.environ.get('DATAFORGE_DB', '../../shared/data/dataforge.db'))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def validate_query(sql):
    stripped = sql.strip().rstrip(';').strip()
    if not stripped:
        return False, "Query is empty"
    if ';' in stripped:
        return False, "Multiple statements are not allowed"
    head = stripped.upper().lstrip('(').lstrip()
    if not any(head.startswith(p) for p in ALLOWED_PREFIXES):
        return False, f"Only {', '.join(ALLOWED_PREFIXES)} queries are allowed"
    return True, None


def _timeout_handler_factory(deadline):
    def handler():
        return 1 if time.monotonic() > deadline else 0
    return handler


@officer_db.route('/officer-db', methods=['GET'])
def page():
    tables = []
    try:
        with get_readonly_conn() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            tables = [r['name'] for r in cur.fetchall()]
    except Exception:
        current_app.logger.exception("officer_db: failed to list tables")
    return render_template('officer_db.html', tables=tables)


@officer_db.route('/officer-db/columns', methods=['GET'])
def columns():
    """Return the column list for a single table (used by the query builder)."""
    table = request.args.get('table', '')
    try:
        with get_readonly_conn() as conn:
            # Validate the table name against the real schema with a bound
            # parameter BEFORE interpolating it into PRAGMA (which can't be
            # parameterized). This blocks identifier injection.
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','view') AND name=?",
                (table,)
            )
            if not cur.fetchone():
                return jsonify({'error': 'Unknown table'}), 400
            safe = table.replace('"', '""')
            cur = conn.execute(f'PRAGMA table_info("{safe}")')
            cols = [{'name': r['name'], 'type': r['type'] or ''}
                    for r in cur.fetchall()]
        return jsonify({'table': table, 'columns': cols})
    except Exception:
        current_app.logger.exception("officer_db: failed to list columns")
        return jsonify({'error': 'Internal error'}), 500


@officer_db.route('/officer-db/query', methods=['POST'])
def query():
    payload = request.get_json(silent=True) or {}
    sql = payload.get('sql', '')
    ok, err = validate_query(sql)
    if not ok:
        return jsonify({'error': err}), 400

    try:
        t0 = time.perf_counter()
        with get_readonly_conn() as conn:
            deadline = time.monotonic() + QUERY_TIMEOUT_S
            conn.set_progress_handler(_timeout_handler_factory(deadline), 1000)
            cur = conn.execute(sql)
            rows = cur.fetchmany(MAX_ROWS + 1)
            cols = [d[0] for d in cur.description] if cur.description else []
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        truncated = len(rows) > MAX_ROWS
        if truncated:
            rows = rows[:MAX_ROWS]
        return jsonify({
            'columns': cols,
            'rows': [list(r) for r in rows],
            'row_count': len(rows),
            'truncated': truncated,
            'elapsed_ms': elapsed_ms,
        })
    except sqlite3.OperationalError as e:
        msg = str(e)
        if 'interrupted' in msg.lower():
            return jsonify({'error': f"Query exceeded {QUERY_TIMEOUT_S}s timeout"}), 400
        return jsonify({'error': f"SQLite error: {msg}"}), 400
    except sqlite3.Error as e:
        return jsonify({'error': f"SQLite error: {e}"}), 400
    except Exception:
        current_app.logger.exception("officer_db: query failed")
        return jsonify({'error': "Internal error"}), 500