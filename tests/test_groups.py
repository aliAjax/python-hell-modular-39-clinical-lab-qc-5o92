import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class GroupScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "groups.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.chem = Actor("chem-supervisor", "supervisor", "chemistry")
        self.immuno_analyst = Actor("immuno-analyst", "analyst", "immuno")
        self.chem_analyst = Actor("chem-analyst", "analyst", "chemistry")
        self.general_coord = Actor("general-coordinator", "coordinator", "general")
        self.service.create(self.chem, "lab_group",
                            {"id": "chemistry", "name": "生化组"})
        self.service.create(self.chem, "lab_group",
                            {"id": "immuno", "name": "免疫组"})

    def tearDown(self):
        self.tmp.cleanup()

    def _base(self, actor=None):
        actor = actor or self.chem
        assay = self.service.create(
            actor,
            "assay",
            {
                "name": "Glucose",
                "unit": "mmol/L",
                "allowed_low": 3.9,
                "allowed_high": 6.1,
            },
        )
        lot = self.service.create(
            actor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0,
             "sd": 0.1, "expires_at": "2099-01-01"},
        )
        lot = self.service.transition(actor, lot["id"], "activate", {"activated_by": "c1"})
        instrument = self.service.create(
            actor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        run = self.service.create(
            actor,
            "qc_run",
            {"assay_id": assay["id"], "qc_lot_id": lot["id"],
             "instrument_id": instrument["id"], "value": 5.02,
             "run_at": "2026-09-27T08:00:00Z"},
        )
        run = self.service.transition(actor, run["id"], "evaluate", {"evaluated_by": "c1"})
        batch = self.service.create(
            actor,
            "result_batch",
            {"assay_id": assay["id"], "instrument_id": instrument["id"],
             "qc_run_id": run["id"], "run_at": "2026-09-27T08:05:00Z",
             "patient_count": 12},
        )
        return assay, lot, run, batch

    def test_new_entities_carry_their_group(self):
        assay, lot, run, batch = self._base()
        self.assertEqual(assay["data"]["group_id"], "chemistry")
        self.assertTrue(assay["data"]["group_confirmed"])
        self.assertEqual(lot["data"]["group_id"], "chemistry")
        self.assertEqual(batch["data"]["group_id"], "chemistry")
        self.assertTrue(lot["data"]["group_confirmed"])
        self.assertTrue(batch["data"]["group_confirmed"])

    def test_cross_group_release_is_denied_and_recorded(self):
        assay, lot, run, batch = self._base()
        # 免疫组分析员尝试放行生化组患者结果批次
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.immuno_analyst, batch["id"], "release", {"reviewer_id": "i-9"})
        violations = self.service.list_violations()
        self.assertEqual(len(violations), 1)
        violation = violations[0]
        self.assertEqual(violation["actor_id"], "immuno-analyst")
        self.assertEqual(violation["actor_group"], "immuno")
        self.assertEqual(violation["target_group"], "chemistry")
        self.assertEqual(violation["entity_id"], batch["id"])
        self.assertEqual(violation["action"], "release")
        # 批次没有被放行
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")

    def test_analyst_can_release_within_own_group(self):
        assay, lot, run, batch = self._base()
        released = self.service.transition(
            self.chem_analyst, batch["id"], "release", {"reviewer_id": "chem-analyst"})
        self.assertEqual(released["status"], "released")
        self.assertEqual(self.service.list_violations(), [])

    def test_cross_group_create_is_denied(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.immuno_analyst,
                "assay",
                {"name": "Cross", "unit": "u", "allowed_low": 0, "allowed_high": 1,
                 "group_id": "chemistry"},
            )
        violations = self.service.list_violations()
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["action"], "create")

    def test_lot_cannot_land_in_another_group_than_assay(self):
        assay, _, _, _ = self._base()
        # 即使显式指定，批次组也必须等于项目组
        lot = self.service.create(
            self.chem,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-X", "target": 5.0,
             "sd": 0.1, "expires_at": "2099-01-01", "group_id": "immuno"},
        )
        self.assertEqual(lot["data"]["group_id"], "chemistry")

    def test_assay_reassignment_cascades_and_freezes_batches(self):
        assay, lot, run, batch = self._base()
        immuno_supervisor = Actor("immuno-supervisor", "supervisor", "immuno")
        # 项目改归免疫组（当前组——生化组组长划出）
        moved = self.service.assign_group(self.chem, assay["id"], "immuno")
        self.assertEqual(moved["data"]["group_id"], "immuno")
        self.assertTrue(moved["data"]["group_confirmed"])
        # 相关批次同事务重指到新组但归属待确认
        for child_id in (lot["id"], batch["id"]):
            child = self.service.get(child_id)
            self.assertEqual(child["data"]["group_id"], "immuno")
            self.assertFalse(child["data"]["group_confirmed"])
        # 任何时刻项目和批次都不会分属两组
        self.assertEqual(
            self.service.get(lot["id"])["data"]["group_id"],
            self.service.get(assay["id"])["data"]["group_id"],
        )
        # 未重新确认前操作被冻结（即使是新组成员，也是归属未确认而非越权）
        with self.assertRaises(ConflictError):
            self.service.transition(
                immuno_supervisor, batch["id"], "intercept", {"reason": "hold"})
        # 旧组成员来操作：归属未确认，同样冻结（旧组范围已失效）
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.chem, batch["id"], "intercept", {"reason": "hold"})
        # 新组逐条重新确认后恢复
        self.service.assign_group(immuno_supervisor, lot["id"], "immuno")
        self.service.assign_group(immuno_supervisor, batch["id"], "immuno")
        intercepted = self.service.transition(
            immuno_supervisor, batch["id"], "intercept", {"reason": "hold"})
        self.assertEqual(intercepted["status"], "intercepted")

    def test_batch_assignment_must_follow_assay_group(self):
        assay, lot, run, batch = self._base()
        # 先把项目改组到免疫组
        immuno_supervisor = Actor("immuno-supervisor", "supervisor", "immuno")
        self.service.assign_group(self.chem, assay["id"], "immuno")
        # 批次不能被重新确认到生化组
        with self.assertRaises(ValidationError):
            self.service.assign_group(immuno_supervisor, batch["id"], "chemistry")

    def test_cross_group_assignment_is_recorded(self):
        assay, lot, run, batch = self._base()
        with self.assertRaises(PermissionDenied):
            self.service.assign_group(self.immuno_analyst, assay["id"], "immuno")
        self.assertEqual(len(self.service.list_violations()), 1)


class MigrationTest(unittest.TestCase):
    """模拟旧版数据库（实体无组标记）的升级过程。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "legacy.db"
        self._build_legacy_database()
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _build_legacy_database(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL, data TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL,
                from_status TEXT, to_status TEXT NOT NULL, detail TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE idempotency (
                actor_id TEXT NOT NULL, idem_key TEXT NOT NULL, entity_id TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(actor_id, idem_key));
        """)
        legacy = [
            ("a1", "assay", "active", {"name": "Glucose"}),
            ("lot1", "qc_lot", "active", {"assay_id": "a1", "lot_no": "L1"}),
            ("batch1", "result_batch", "waiting", {"assay_id": "a1"}),
            ("inst1", "instrument", "ready", {"serial": "S1"}),
        ]
        for entity_id, kind, status, data in legacy:
            connection.execute(
                "INSERT INTO entities VALUES (?, ?, ?, 1, ?, 'legacy', 't0', 't0')",
                (entity_id, kind, status, json.dumps(data, sort_keys=True)),
            )
        connection.commit()
        connection.close()

    def test_legacy_data_lands_in_general_group_unconfirmed(self):
        self.assertIsNotNone(self.service.get("general"))
        for entity_id in ("a1", "lot1", "batch1"):
            entity = self.service.get(entity_id)
            self.assertEqual(entity["data"]["group_id"], "general")
            self.assertFalse(entity["data"]["group_confirmed"])
        # 仪器不按组归属
        self.assertNotIn("group_id", self.service.get("inst1")["data"])
        # 落位前一切操作被冻结（冻结判定先于角色/组判定）
        with self.assertRaises(ConflictError):
            self.service.transition(
                Actor("general-supervisor", "supervisor", "general"),
                "a1", "suspend", {"reason": "waiting for group"})
        pending = self.service.list_assignments(status="pending")
        self.assertEqual({item["entity_id"] for item in pending},
                         {"a1", "lot1", "batch1"})
        # 升级是幂等的
        DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.assertEqual(len(self.service.list_assignments(status="pending")), 3)

    def test_assignment_failure_does_not_stop_the_queue_and_retries_resume(self):
        coordinator = Actor("general-coordinator", "coordinator", "general")
        self.service.create(
            Actor("admin", "admin", "general"), "lab_group",
            {"id": "chemistry", "name": "生化组"})
        # 第一轮：只指派项目，未点名的批次留在队列里等下一轮
        report = self.service.process_pending_assignments(
            coordinator, targets={"a1": "chemistry"})
        self.assertEqual(report["processed"], [{"entity_id": "a1", "group_id": "chemistry"}])
        self.assertEqual(report["failed"], [])
        pending = {item["entity_id"] for item in
                   self.service.list_assignments(status="pending")}
        self.assertEqual(pending, {"lot1", "batch1"})
        # 第二轮（无显式目标）：批次跟随项目自动落到生化组
        report = self.service.process_pending_assignments(coordinator)
        processed = {item["entity_id"]: item["group_id"] for item in report["processed"]}
        self.assertEqual(processed, {"lot1": "chemistry", "batch1": "chemistry"})
        for entity_id in ("lot1", "batch1"):
            entity = self.service.get(entity_id)
            self.assertTrue(entity["data"]["group_confirmed"])
            self.assertEqual(entity["data"]["group_id"], "chemistry")
        self.assertEqual(self.service.list_assignments(status="pending"), [])

    def test_failed_assignment_is_recorded_and_retried(self):
        coordinator = Actor("general-coordinator", "coordinator", "general")
        self.service.create(
            Actor("admin", "admin", "general"), "lab_group",
            {"id": "chemistry", "name": "生化组"})

        # 用桩模拟第一次指派时的瞬时故障（如乐观锁冲突）。
        original = self.service.repository.set_entity_group
        state = {"fail": True}

        def flaky_set(entity_id, group_id, confirmed):
            if state["fail"] and entity_id == "a1":
                state["fail"] = False
                raise ConflictError("transient version conflict")
            return original(entity_id, group_id, confirmed)

        self.service.repository.set_entity_group = flaky_set
        report = self.service.process_pending_assignments(
            coordinator, targets={"a1": "chemistry"})
        self.service.repository.set_entity_group = original
        self.assertEqual(report["processed"], [])
        self.assertEqual(report["failed"][0]["entity_id"], "a1")
        self.assertIn("transient version conflict",
                      report["failed"][0]["error"])
        failed = self.service.list_assignments(status="failed")
        self.assertEqual(len(failed), 1)
        # 目标组在失败记录里保留，重试时才能续跑。
        self.assertEqual(failed[0]["target_group"], "chemistry")
        # 批次不会在项目落位前被处理
        pending = {item["entity_id"] for item in
                   self.service.list_assignments(status="pending")}
        self.assertEqual(pending, {"lot1", "batch1"})
        # 故障消除后续跑：失败条目带着原目标重新入队并成功，
        # 批次在同一轮跟随项目自动落位，剩余条目接着跑完
        report = self.service.retry_pending_assignments(coordinator)
        processed = {item["entity_id"] for item in report["processed"]}
        self.assertEqual(processed, {"a1", "lot1", "batch1"})
        self.assertEqual(report["failed"], [])
        self.assertEqual(self.service.list_assignments(status="failed"), [])
        self.assertEqual(self.service.list_assignments(status="pending"), [])


if __name__ == "__main__":
    unittest.main()
