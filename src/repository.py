import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)
            self._ensure_group_columns(connection)

    def _ensure_group_columns(self, connection):
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(entities)").fetchall()
        }
        if "group_name" not in columns:
            connection.execute("ALTER TABLE entities ADD COLUMN group_name TEXT")
        if "group_confirmed" not in columns:
            connection.execute(
                "ALTER TABLE entities ADD COLUMN group_confirmed INTEGER NOT NULL DEFAULT 1"
            )

    def upgrade_group_ownership(self, default_group):
        """升级：为缺少组标记的存量数据补列并先落到总务组，标记为待指派。

        幂等，可重复执行；已落组的记录不会被覆盖。仅检测项目、质控品批次、
        患者结果批次需要组归属；质控结果通过批次继承，仪器为共用资源。
        """
        group_owned = ("assay", "qc_lot", "result_batch")
        placeholders = ",".join("?" for _ in group_owned)
        with self._connect() as connection:
            self._ensure_group_columns(connection)
            pending = connection.execute(
                "SELECT COUNT(*) FROM entities WHERE group_name IS NULL "
                "AND kind IN (%s)" % placeholders,
                group_owned,
            ).fetchone()[0]
            connection.execute(
                "UPDATE entities SET group_name = ?, group_confirmed = 0 "
                "WHERE group_name IS NULL AND kind IN (%s)" % placeholders,
                (default_group,) + group_owned,
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                ("group_ownership", utcnow()),
            )
        return pending

    def list_pending_group_entities(self, kinds):
        placeholders = ",".join("?" for _ in kinds)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities WHERE group_confirmed = 0 AND kind IN (%s) "
                "ORDER BY created_at, id" % placeholders,
                tuple(kinds),
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def update_group(self, entity_id, group_name, group_confirmed):
        now = utcnow()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            connection.execute(
                "UPDATE entities SET group_name = ?, group_confirmed = ?, "
                "version = version + 1, updated_at = ? WHERE id = ?",
                (group_name, 1 if group_confirmed else 0, now, entity_id),
            )
        return self.get_entity(entity_id)

    def invalidate_groups_for_assay(self, assay_id):
        """检测项目组归属变更后，把相关批次的组确认置为失效（保留原组待重新确认）。"""
        affected = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, data FROM entities "
                "WHERE kind IN ('qc_lot', 'result_batch') AND group_confirmed = 1"
            ).fetchall()
            for row in rows:
                data = json.loads(row["data"])
                if data.get("assay_id") == assay_id:
                    connection.execute(
                        "UPDATE entities SET group_confirmed = 0, version = version + 1, "
                        "updated_at = ? WHERE id = ?",
                        (utcnow(), row["id"]),
                    )
                    affected.append(row["id"])
        return affected

    @staticmethod
    def _entity_from_row(row):
        keys = row.keys()
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "group_name": row["group_name"] if "group_name" in keys else None,
            "group_confirmed": int(row["group_confirmed"]) if "group_confirmed" in keys else 1,
        }

    def create_entity(self, entity_id, kind, status, data, actor_id, group_name=None, group_confirmed=1):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at, "
                "group_name, group_confirmed) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now, group_name,
                 1 if group_confirmed else 0),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
