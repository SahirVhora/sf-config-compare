import sqlite3
from config import DB_PATH


def get_conn() -> sqlite3.Connection:
    """Return a new SQLite connection with row_factory and foreign keys enabled."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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


def get_all_instances():
    """Return all instances ordered by alias."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM instances ORDER BY alias").fetchall()]


def get_instance(instance_id: int):
    """Return a single instance dict by ID, or None if not found."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
        return dict(row) if row else None


def get_instance_by_alias(alias: str):
    """Return a single instance dict by alias, or None if not found."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM instances WHERE alias = ?", (alias,)).fetchone()
        return dict(row) if row else None


def upsert_instance(data: dict) -> int:
    """Insert or update an instance record and return its ID."""
    cols = ["alias", "base_url", "company_id", "auth_type", "username", "client_id", "token_url"]
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
            cur = conn.execute(f"INSERT INTO instances ({col_names}) VALUES ({placeholders})", values)
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
        conn.execute(f"UPDATE instances SET {col} = datetime('now') WHERE id = ?", (instance_id,))


def get_entities_for_instance(instance_id: int):
    """Return all metadata entities for a given instance."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM metadata_entities WHERE instance_id = ?", (instance_id,)
        ).fetchall()]


def get_fields_for_entities(conn, entity_ids: list) -> dict:
    """Return {entity_id: [fields]} for a list of entity IDs in one query."""
    if not entity_ids:
        return {}
    placeholders = ','.join('?' * len(entity_ids))
    rows = conn.execute(
        f'SELECT * FROM metadata_fields WHERE entity_id IN ({placeholders}) ORDER BY entity_id',
        entity_ids
    ).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row['entity_id'], []).append(dict(row))
    return result


def get_picklists_for_instance(instance_id: int):
    """Return all picklist values for a given instance ordered by picklist and code."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM picklist_values WHERE instance_id = ? ORDER BY picklist_id, external_code",
            (instance_id,)
        ).fetchall()]

