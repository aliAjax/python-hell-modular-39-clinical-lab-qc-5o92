import json
import sqlite3
from datetime import datetime, timezone

from .domain import GENERAL_GROUP, ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: 需要按专业组归属的对象类型。
GROUP_SCOPED_KINDS = ("assay", "qc_lot", "result_batch")

_UNSET = object()


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
                    to_status TEXT,
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
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_violations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT,
                    kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    actor_group TEXT NOT NULL,
                    target_group TEXT,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_violations_actor
                    ON group_violations(actor_id, id);
                CREATE TABLE IF NOT EXISTS group_assignment_queue (
                    entity_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target_group TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_assign_queue_status
                    ON group_assignment_queue(status, updated_at);
            """)
        self._migrate()

    def _migrate(self):
        """把已有库升级到当前 schema 版本。

        v1：增加专业组归属。缺组标记的检测项目/质控品批次/患者结果批次
        先落到总务组且标记为未确认，同时进入指派队列等待逐条指派。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else 0

            if version < 1:
                now = utcnow()
                # 旧库 audit_log.to_status 为 NOT NULL，归属重置没有目标状态，放宽它。
                audit_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(audit_log)").fetchall()
                }
                if audit_columns:
                    notnull = connection.execute(
                        "SELECT \"notnull\" FROM pragma_table_info('audit_log') WHERE name = 'to_status'"
                    ).fetchone()
                    if notnull and int(notnull[0]):
                        connection.executescript("""
                            CREATE TABLE audit_log_new (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                entity_id TEXT NOT NULL,
                                actor_id TEXT NOT NULL,
                                actor_role TEXT NOT NULL,
                                action TEXT NOT NULL,
                                from_status TEXT,
                                to_status TEXT,
                                detail TEXT NOT NULL,
                                created_at TEXT NOT NULL
                            );
                            INSERT INTO audit_log_new
                                SELECT id, entity_id, actor_id, actor_role, action,
                                       from_status, to_status, detail, created_at
                                FROM audit_log;
                            DROP TABLE audit_log;
                            ALTER TABLE audit_log_new RENAME TO audit_log;
                            CREATE INDEX IF NOT EXISTS idx_audit_entity
                                ON audit_log(entity_id, id);
                        """)
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                    "VALUES (?, 'lab_group', 'active', 1, ?, 'system', ?, ?) "
                    "ON CONFLICT(id) DO NOTHING",
                    (
                        GENERAL_GROUP,
                        json.dumps(
                            {"name": "总务组", "system": True},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
                # 已有数据缺组标记：补落到总务组，归属待指派。
                rows = connection.execute(
                    "SELECT id, kind, version, data FROM entities "
                    "WHERE kind IN ('assay', 'qc_lot', 'result_batch')"
                ).fetchall()
                for item in rows:
                    data = json.loads(item["data"])
                    if data.get("group_id"):
                        continue
                    data["group_id"] = GENERAL_GROUP
                    data["group_confirmed"] = False
                    connection.execute(
                        "UPDATE entities SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
                        (json.dumps(data, ensure_ascii=False, sort_keys=True), now, item["id"]),
                    )
                    connection.execute(
                        "INSERT INTO group_assignment_queue"
                        "(entity_id, kind, target_group, status, attempts, last_error, updated_at) "
                        "VALUES (?, ?, NULL, 'pending', 0, NULL, ?) "
                        "ON CONFLICT(entity_id) DO NOTHING",
                        (item["id"], item["kind"], now),
                    )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = '1'"
                )

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
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

    # -- 专业组归属 -----------------------------------------------------------

    def reassign_assay_group(self, assay, target_group):
        """项目改组：项目指向新组并重新确认，相关批次同事务重指且待确认。

        原组范围在批次层面立即失效；批次的 group_id 同步指向新组，
        因此任何时刻都不会出现“项目在一组、批次在另一组”。
        返回受级联影响的实体 id 列表。
        """
        now = utcnow()
        cascaded = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM entities WHERE id = ?", (assay["id"],)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + assay["id"])
            data = json.loads(row["data"])
            data["group_id"] = target_group
            data["group_confirmed"] = True
            connection.execute(
                "UPDATE entities SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False, sort_keys=True), now, assay["id"]),
            )
            lot_rows = connection.execute(
                "SELECT id, data FROM entities WHERE kind = 'qc_lot' "
                "AND json_extract(data, '$.assay_id') = ?",
                (assay["id"],),
            ).fetchall()
            batch_rows = connection.execute(
                "SELECT id, data FROM entities WHERE kind = 'result_batch' "
                "AND json_extract(data, '$.assay_id') = ?",
                (assay["id"],),
            ).fetchall()
            for item in list(lot_rows) + list(batch_rows):
                item_data = json.loads(item["data"])
                item_data["group_id"] = target_group
                item_data["group_confirmed"] = False
                connection.execute(
                    "UPDATE entities SET data = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(item_data, ensure_ascii=False, sort_keys=True), now, item["id"]),
                )
                # 新组必须逐条重新确认归属；失败的重试会接着这里跑。
                connection.execute(
                    "INSERT INTO group_assignment_queue"
                    "(entity_id, kind, target_group, status, attempts, last_error, updated_at) "
                    "VALUES (?, ?, ?, 'pending', 0, NULL, ?) "
                    "ON CONFLICT(entity_id) DO UPDATE SET target_group = excluded.target_group, "
                    "status = 'pending', last_error = NULL, updated_at = excluded.updated_at",
                    (item["id"], "qc_lot" if item in lot_rows else "result_batch",
                     target_group, now),
                )
                cascaded.append(item["id"])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return cascaded

    def set_entity_group(self, entity_id, group_id, confirmed):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            data = json.loads(row["data"])
            data["group_id"] = group_id
            data["group_confirmed"] = bool(confirmed)
            connection.execute(
                "UPDATE entities SET data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False, sort_keys=True), now, entity_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_group_violation(self, entity_id, kind, action, actor, target_group, reason):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO group_violations"
                "(entity_id, kind, action, actor_id, actor_role, actor_group, target_group, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    kind,
                    action,
                    actor.user_id,
                    actor.role,
                    actor.group_id,
                    target_group,
                    reason,
                    utcnow(),
                ),
            )

    def list_group_violations(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM group_violations WHERE entity_id = ? ORDER BY id",
                    (entity_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM group_violations ORDER BY id"
                ).fetchall()
        return [self._violation_from_row(row) for row in rows]

    @staticmethod
    def _violation_from_row(row):
        return {
            "id": row["id"],
            "entity_id": row["entity_id"],
            "kind": row["kind"],
            "action": row["action"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "actor_group": row["actor_group"],
            "target_group": row["target_group"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }

    # -- 组指派队列（失败可续跑） ---------------------------------------------

    def enqueue_group_assignment(self, entity_id, kind, target_group=None):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO group_assignment_queue"
                "(entity_id, kind, target_group, status, attempts, last_error, updated_at) "
                "VALUES (?, ?, ?, 'pending', 0, NULL, ?) "
                "ON CONFLICT(entity_id) DO UPDATE SET target_group = excluded.target_group, "
                "status = 'pending', last_error = NULL, updated_at = excluded.updated_at",
                (entity_id, kind, target_group, now),
            )

    def list_group_assignments(self, status=None):
        # 检测项目排在前面，批量处理时批次才能在同一轮推导出其目标组。
        order = "CASE kind WHEN 'assay' THEN 0 ELSE 1 END, updated_at, entity_id"
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM group_assignment_queue WHERE status = ? ORDER BY " + order,
                    (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM group_assignment_queue ORDER BY " + order
                ).fetchall()
        return [
            {
                "entity_id": row["entity_id"],
                "kind": row["kind"],
                "target_group": row["target_group"],
                "status": row["status"],
                "attempts": int(row["attempts"]),
                "last_error": row["last_error"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def mark_group_assignment(self, entity_id, status, error=None, target_group=_UNSET):
        with self._connect() as connection:
            if target_group is _UNSET:
                connection.execute(
                    "UPDATE group_assignment_queue SET status = ?, attempts = attempts + 1, "
                    "last_error = ?, updated_at = ? WHERE entity_id = ?",
                    (status, error, utcnow(), entity_id),
                )
            else:
                connection.execute(
                    "UPDATE group_assignment_queue SET status = ?, attempts = attempts + 1, "
                    "last_error = ?, target_group = ?, updated_at = ? WHERE entity_id = ?",
                    (status, error, target_group, utcnow(), entity_id),
                )

    def remove_group_assignment(self, entity_id):
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM group_assignment_queue WHERE entity_id = ?", (entity_id,)
            )

    # -- 审计 ----------------------------------------------------------------

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
