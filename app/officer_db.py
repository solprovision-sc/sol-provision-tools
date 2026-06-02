"""Read-only SQL query interface for officers. Auth is handled by nginx."""
from flask import Blueprint, render_template, request, jsonify, current_app
import sqlite3
import time

officer_db = Blueprint('officer_db', __name__)

MAX_ROWS = 5000
QUERY_TIMEOUT_S = 10
ALLOWED_PREFIXES = ('SELECT', 'WITH', 'EXPLAIN')


def get_readonly_conn():
    """Open dataforge.db in true read-only mode."""
    db_path = current_app.config['DB_PATH']  # adjust key if yours differs
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