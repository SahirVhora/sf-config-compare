import json
import sqlite3
import threading
from contextlib import contextmanager

from config import DB_PATH

# Thread-local connection pool to avoid "SQLite objects created in a thread can only be used
# in that same thread" errors when Flask's reloader spawns threads.
_local = threading.local()
_pool: dict[int, sqlite3.Connection] = {}
_pool_lock = threading.Lock()


def _init_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")  # ~64 MB page cache
    conn.execute("PRAGMA temp_store = MEMORY")


def get_conn() -> sqlite3.Connection:
    """Return a new SQLite connection with row_factory and foreign keys enabled.

    In production (workers=1, threaded requests), each request gets its own
    connection. WAL mode + NORMAL synchronous gives the best balance of
    durability and performance for a read-heavy workload.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _init_pragmas(conn)
    return conn


@contextmanager
def get_pool_conn() -> sqlite3.Connection:
    """Context manager that returns a pooled or new connection.

    Useful for long-running operations (metadata pulls, comparisons) that
    issue many queries and benefit from connection reuse.
    """
    tid = threading.current_thread().ident
    with _pool_lock:
        conn = _pool.get(tid)
        if conn is None:
            conn = get_conn()
            _pool[tid] = conn
    try:
        yield conn
    finally:
        # NOTE: We intentionally do NOT close here; the connection is
        # reused across requests on the same thread. A separate cleanup
        # task (see _close_pool_conn) should be run on app shutdown.
        pass


def _close_pool_conn() -> None:
    """Close the thread-local pooled connection. Call on app shutdown."""
    tid = threading.current_thread().ident
    with _pool_lock:
        conn = _pool.pop(tid, None)
        if conn is not None:
            conn.close()


@contextmanager
def transaction() -> sqlite3.Connection:
    """Context manager that wraps a block in an explicit SQLite transaction.

    Commits on success, rolls back on exception. Also disables autocommit
    so that multiple INSERTs/UPDATEs inside the block are batched.
    """
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations incrementally."""
    # Migration 1: pull_history table for drift tracking (Phase 2)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pull_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id INTEGER NOT NULL,
            pull_type TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT,
            entities_count INTEGER,
            fields_count INTEGER,
            picklists_count INTEGER,
            values_count INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pull_history_instance ON pull_history(instance_id, pull_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pull_history_started ON pull_history(started_at)"
    )

    # Migration 2: entity_snapshots for deep historical diffs (Phase 2)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER NOT NULL,
            entity_name TEXT NOT NULL,
            entity_label TEXT,
            element_name TEXT,
            fields_json TEXT NOT NULL,
            FOREIGN KEY (history_id) REFERENCES pull_history(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_snapshots_history ON entity_snapshots(history_id)"
    )

    # Migration 3: picklist_snapshots for historical picklist diffs (Phase 2)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS picklist_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER NOT NULL,
            picklist_id TEXT NOT NULL,
            external_code TEXT,
            option_id TEXT,
            label_en TEXT,
            status TEXT,
            all_labels TEXT,
            FOREIGN KEY (history_id) REFERENCES pull_history(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_picklist_snapshots_history ON picklist_snapshots(history_id)"
    )

    # Migration 4: scheduled_checks for automated drift detection (Phase 4)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            instance_a_id INTEGER NOT NULL,
            instance_b_id INTEGER NOT NULL,
            cron_expression TEXT NOT NULL DEFAULT '0 0 * * *',
            enabled INTEGER NOT NULL DEFAULT 1,
            webhook_url TEXT,
            webhook_type TEXT DEFAULT 'slack',
            notify_on TEXT DEFAULT 'any_change',
            last_run_at TEXT,
            last_run_status TEXT,
            last_run_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (instance_a_id) REFERENCES instances(id) ON DELETE CASCADE,
            FOREIGN KEY (instance_b_id) REFERENCES instances(id) ON DELETE CASCADE
        )
    """)

    # Migration 5: drift_results for storing scheduled check outputs (Phase 4)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drift_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_id INTEGER NOT NULL,
            run_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'pending',
            summary_json TEXT,
            entity_diff_count INTEGER,
            field_diff_count INTEGER,
            picklist_issue_count INTEGER,
            report_id TEXT,
            notification_sent INTEGER DEFAULT 0,
            FOREIGN KEY (check_id) REFERENCES scheduled_checks(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drift_results_check ON drift_results(check_id)"
    )


def init_db():
    """Initialise the database schema, creating tables and indexes if absent."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias TEXT NOT NULL UNIQUE,
                base_url TEXT NOT NULL,
                company_id TEXT NOT NULL,
                auth_type TEXT NOT NULL DEFAULT 'basic',
                username TEXT,
                client_id TEXT,
                token_url TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_metadata_pull TEXT,
                last_picklist_pull TEXT
            );

            CREATE TABLE IF NOT EXISTS metadata_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                entity_name TEXT NOT NULL,
                entity_label TEXT,
                element_name TEXT,
                pull_timestamp TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS metadata_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                field_id TEXT NOT NULL,
                field_label TEXT,
                field_type TEXT,
                required TEXT,
                visibility TEXT,
                max_length TEXT,
                picklist_id TEXT,
                is_custom INTEGER DEFAULT 0,
                raw_attributes TEXT,
                FOREIGN KEY (entity_id) REFERENCES metadata_entities(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS picklist_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                picklist_id TEXT NOT NULL,
                option_id TEXT,
                external_code TEXT,
                parent_picklist_id TEXT,
                status TEXT,
                label_en TEXT,
                all_labels TEXT,
                pull_timestamp TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_picklist_instance
                ON picklist_values(instance_id, picklist_id);

            CREATE TABLE IF NOT EXISTS pull_jobs (
                id TEXT PRIMARY KEY,
                instance_id INTEGER NOT NULL,
                pull_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT,
                error TEXT,
                FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE
            );

        """)
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_entities_instance ON metadata_entities(instance_id);
            CREATE INDEX IF NOT EXISTS idx_entities_name ON metadata_entities(entity_name);
            CREATE INDEX IF NOT EXISTS idx_fields_entity ON metadata_fields(entity_id);
            CREATE INDEX IF NOT EXISTS idx_fields_entity_field ON metadata_fields(entity_id, field_id);
            CREATE INDEX IF NOT EXISTS idx_picklist_items_picklist ON picklist_values(picklist_id);
        """)
        _run_migrations(conn)


def get_all_instances():
    """Return all instances ordered by alias."""
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM instances ORDER BY alias").fetchall()
        ]


def get_instance(instance_id: int):
    """Return a single instance dict by ID, or None if not found."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        return dict(row) if row else None


def get_instance_by_alias(alias: str):
    """Return a single instance dict by alias, or None if not found."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE alias = ?", (alias,)
        ).fetchone()
        return dict(row) if row else None


def upsert_instance(data: dict) -> int:
    """Insert or update an instance record and return its ID."""
    cols = [
        "alias",
        "base_url",
        "company_id",
        "auth_type",
        "username",
        "client_id",
        "token_url",
    ]
    with get_conn() as conn:
        if data.get("id"):
            set_clause = ", ".join(f"{c} = ?" for c in cols)
            values = [data.get(c) for c in cols] + [data["id"]]
            conn.execute(f"UPDATE instances SET {set_clause} WHERE id = ?", values)
            return data["id"]
        else:
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(cols)
            values = [data.get(c) for c in cols]
            cur = conn.execute(
                f"INSERT INTO instances ({col_names}) VALUES ({placeholders})", values
            )
            return cur.lastrowid


def delete_instance(instance_id: int):
    """Delete an instance and its cascaded child records by ID."""
    with get_conn() as conn:
        conn.execute("DELETE FROM instances WHERE id = ?", (instance_id,))


def update_pull_timestamp(instance_id: int, pull_type: str):
    """Update the last metadata or picklist pull timestamp for an instance."""
    if pull_type == "metadata":
        col = "last_metadata_pull"
    else:
        col = "last_picklist_pull"
    with get_conn() as conn:
        conn.execute(
            f"UPDATE instances SET {col} = datetime('now') WHERE id = ?", (instance_id,)
        )


def get_entities_for_instance(instance_id: int):
    """Return all metadata entities for a given instance."""
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM metadata_entities WHERE instance_id = ?", (instance_id,)
            ).fetchall()
        ]


def get_fields_for_entities(conn, entity_ids: list) -> dict:
    """Return {entity_id: [fields]} for a list of entity IDs in one query."""
    if not entity_ids:
        return {}
    placeholders = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"SELECT * FROM metadata_fields WHERE entity_id IN ({placeholders}) ORDER BY entity_id",
        entity_ids,
    ).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row["entity_id"], []).append(dict(row))
    return result


# ── Pull history (Phase 2) ────────────────────────────────────────────────


def record_pull_history(
    instance_id: int,
    pull_type: str,
    status: str,
    entities_count: int | None = None,
    fields_count: int | None = None,
    picklists_count: int | None = None,
    values_count: int | None = None,
    error: str | None = None,
    history_id: int | None = None,
) -> int:
    """Record or update a pull history entry. Returns the history_id."""
    with get_conn() as conn:
        if history_id:
            conn.execute(
                """UPDATE pull_history
                   SET status = ?, finished_at = datetime('now'),
                       entities_count = ?, fields_count = ?,
                       picklists_count = ?, values_count = ?, error = ?
                   WHERE id = ?""",
                (
                    status,
                    entities_count,
                    fields_count,
                    picklists_count,
                    values_count,
                    error,
                    history_id,
                ),
            )
            return history_id
        cur = conn.execute(
            """INSERT INTO pull_history
               (instance_id, pull_type, status, entities_count, fields_count,
                picklists_count, values_count, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                instance_id,
                pull_type,
                status,
                entities_count,
                fields_count,
                picklists_count,
                values_count,
                error,
            ),
        )
        return cur.lastrowid


def get_pull_history(
    instance_id: int | None = None, pull_type: str | None = None, limit: int = 100
):
    """Return pull history records, optionally filtered by instance and type."""
    with get_conn() as conn:
        if instance_id and pull_type:
            rows = conn.execute(
                "SELECT * FROM pull_history WHERE instance_id = ? AND pull_type = ? ORDER BY started_at DESC LIMIT ?",
                (instance_id, pull_type, limit),
            ).fetchall()
        elif instance_id:
            rows = conn.execute(
                "SELECT * FROM pull_history WHERE instance_id = ? ORDER BY started_at DESC LIMIT ?",
                (instance_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pull_history ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def get_pull_history_by_id(history_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pull_history WHERE id = ?", (history_id,)
        ).fetchone()
        return dict(row) if row else None


def save_entity_snapshots(history_id: int, entities: list[dict]):
    """Store entity snapshots for a given pull history record."""
    with get_conn() as conn:
        for ent in entities:
            conn.execute(
                """INSERT INTO entity_snapshots
                   (history_id, entity_name, entity_label, element_name, fields_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    history_id,
                    ent["entity_name"],
                    ent.get("entity_label", ""),
                    ent.get("element_name", ""),
                    json.dumps(ent.get("fields", [])),
                ),
            )


def save_picklist_snapshots(history_id: int, picklist_values: list[dict]):
    """Store picklist value snapshots for a given pull history record."""
    with get_conn() as conn:
        for pv in picklist_values:
            conn.execute(
                """INSERT INTO picklist_snapshots
                   (history_id, picklist_id, external_code, option_id, label_en, status, all_labels)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    history_id,
                    pv["picklist_id"],
                    pv.get("external_code"),
                    pv.get("option_id"),
                    pv.get("label_en"),
                    pv.get("status"),
                    pv.get("all_labels"),
                ),
            )


def get_entity_snapshots(history_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM entity_snapshots WHERE history_id = ?", (history_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_picklist_snapshots(history_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM picklist_snapshots WHERE history_id = ?", (history_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Scheduled checks (Phase 4) ─────────────────────────────────────────────


def create_scheduled_check(data: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO scheduled_checks
               (name, instance_a_id, instance_b_id, cron_expression, enabled,
                webhook_url, webhook_type, notify_on)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["name"],
                data["instance_a_id"],
                data["instance_b_id"],
                data.get("cron_expression", "0 0 * * *"),
                int(data.get("enabled", True)),
                data.get("webhook_url"),
                data.get("webhook_type", "slack"),
                data.get("notify_on", "any_change"),
            ),
        )
        return cur.lastrowid


def get_scheduled_checks() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_checks ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_scheduled_check(check_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM scheduled_checks WHERE id = ?", (check_id,)
        ).fetchone()
        return dict(row) if row else None


def update_scheduled_check(check_id: int, data: dict):
    with get_conn() as conn:
        conn.execute(
            """UPDATE scheduled_checks
               SET name = ?, cron_expression = ?, enabled = ?,
                   webhook_url = ?, webhook_type = ?, notify_on = ?
               WHERE id = ?""",
            (
                data["name"],
                data.get("cron_expression", "0 0 * * *"),
                int(data.get("enabled", True)),
                data.get("webhook_url"),
                data.get("webhook_type", "slack"),
                data.get("notify_on", "any_change"),
                check_id,
            ),
        )


def delete_scheduled_check(check_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM scheduled_checks WHERE id = ?", (check_id,))


def update_check_last_run(check_id: int, status: str, error: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE scheduled_checks
               SET last_run_at = datetime('now'), last_run_status = ?, last_run_error = ?
               WHERE id = ?""",
            (status, error, check_id),
        )


def record_drift_result(
    check_id: int,
    status: str,
    summary_json: str | None = None,
    entity_diff_count: int = 1,
    field_diff_count: int = 1,
    picklist_issue_count: int = 1,
    report_id: str | None = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO drift_results
               (check_id, status, summary_json, entity_diff_count, field_diff_count,
                picklist_issue_count, report_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                check_id,
                status,
                summary_json,
                entity_diff_count,
                field_diff_count,
                picklist_issue_count,
                report_id,
            ),
        )
        return cur.lastrowid


def get_drift_results(check_id: int | None = None, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        if check_id:
            rows = conn.execute(
                "SELECT * FROM drift_results WHERE check_id = ? ORDER BY run_at DESC LIMIT ?",
                (check_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM drift_results ORDER BY run_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# ── Existing picklist functions ────────────────────────────────────────────


def get_picklists_for_instance(instance_id: int):
    """Return all picklist values for a given instance ordered by picklist and code."""
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM picklist_values WHERE instance_id = ? ORDER BY picklist_id, external_code",
                (instance_id,),
            ).fetchall()
        ]
