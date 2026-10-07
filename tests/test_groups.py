import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class GroupOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "groups.db"),
            RuleEngine(),
        )
        self.biochem_supervisor = Actor("bio-sup", "supervisor", "biochem")
        self.biochem_analyst = Actor("bio-analyst", "analyst", "biochem")
        self.immuno_analyst = Actor("immuno-analyst", "analyst", "immunology")
        self.immuno_supervisor = Actor("immuno-sup", "supervisor", "immunology")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_biochem_run(self, value=5.0):
        assay = self.service.create(
            self.biochem_supervisor,
            "assay",
            {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )
        lot = self.service.create(
            self.biochem_supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        self.service.transition(self.biochem_supervisor, lot["id"], "activate", {"activated_by": "a"})
        instrument = self.service.create(
            self.biochem_supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        run = self.service.create(
            self.biochem_supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": "2026-09-27T08:00:00Z",
            },
        )
        run = self.service.transition(self.biochem_supervisor, run["id"], "evaluate", {"evaluated_by": "a"})
        return assay, lot, instrument, run

    def _make_batch(self, assay, instrument, run):
        return self.service.create(
            self.biochem_supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:05:00Z",
                "patient_count": 12,
            },
        )

    def test_same_group_can_release(self):
        assay, lot, instrument, run = self._setup_biochem_run()
        batch = self._make_batch(assay, instrument, run)
        released = self.service.transition(
            self.biochem_analyst, batch["id"], "release", {"reviewer_id": "r"}
        )
        self.assertEqual(released["status"], "released")

    def test_cross_group_release_denied_and_audited(self):
        assay, lot, instrument, run = self._setup_biochem_run()
        batch = self._make_batch(assay, instrument, run)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.immuno_analyst, batch["id"], "release", {"reviewer_id": "r"}
            )
        entries = self.service.audit_log(entity_id=batch["id"])
        denied = [e for e in entries if e["action"] == "cross_group_denied"]
        self.assertTrue(denied)
        self.assertEqual(denied[0]["actor_id"], "immuno-analyst")
        self.assertEqual(denied[0]["actor_role"], "analyst")
        self.assertEqual(denied[0]["detail"]["actor_group"], "immunology")
        self.assertEqual(denied[0]["detail"]["entity_group"], "biochem")

    def test_cross_group_create_denied(self):
        assay, lot, instrument, run = self._setup_biochem_run()
        # 免疫组分析员引用生化组的检测项目建批次 -> 越权
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.immuno_analyst,
                "result_batch",
                {
                    "assay_id": assay["id"],
                    "instrument_id": instrument["id"],
                    "qc_run_id": run["id"],
                    "run_at": "2026-09-27T09:00:00Z",
                    "patient_count": 1,
                },
            )
        # 免疫组主管引用生化组项目建质控品批次 -> 越权
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.immuno_supervisor,
                "qc_lot",
                {"assay_id": assay["id"], "lot_no": "LOT-X", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
            )

    def test_assay_group_change_invalidates_batches(self):
        assay, lot, instrument, run = self._setup_biochem_run()
        batch = self._make_batch(assay, instrument, run)
        # 检测项目从生化转到免疫
        self.service.assign_group(self.immuno_supervisor, assay["id"], "immunology")
        lot_after = self.service.get(lot["id"])
        batch_after = self.service.get(batch["id"])
        self.assertEqual(lot_after["group_name"], "biochem")
        self.assertEqual(lot_after["group_confirmed"], 0)
        self.assertEqual(batch_after["group_confirmed"], 0)
        # 批次未重新确认前，生化组和免疫组都不能放行
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.biochem_analyst, batch["id"], "release", {"reviewer_id": "r"})
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.immuno_analyst, batch["id"], "release", {"reviewer_id": "r"})
        # 批次重新确认到免疫组（与项目一致）后可放行
        self.service.assign_group(self.immuno_supervisor, batch["id"], "immunology")
        released = self.service.transition(
            self.immuno_analyst, batch["id"], "release", {"reviewer_id": "r"}
        )
        self.assertEqual(released["status"], "released")

    def test_batch_cannot_be_assigned_to_other_group_than_assay(self):
        assay, lot, instrument, run = self._setup_biochem_run()
        batch = self._make_batch(assay, instrument, run)
        with self.assertRaises(ValidationError):
            self.service.assign_group(self.immuno_supervisor, batch["id"], "immunology")

    def test_assign_group_requires_supervisor(self):
        assay, lot, instrument, run = self._setup_biochem_run()
        with self.assertRaises(PermissionDenied):
            self.service.assign_group(self.biochem_analyst, assay["id"], "immunology")


class GroupMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "migration.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("sup", "supervisor")  # 无 X-Group，默认总务组

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy_data(self):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "a"})
        return assay, lot

    def _mark_legacy(self, *entity_ids):
        # 模拟升级前的存量数据：缺少组标记（group_name 为 NULL）
        with self.service.repository._connect() as connection:
            for entity_id in entity_ids:
                connection.execute(
                    "UPDATE entities SET group_name = NULL WHERE id = ?", (entity_id,)
                )

    def test_migration_lands_in_general_then_assigns_one_by_one(self):
        assay, lot = self._legacy_data()
        self._mark_legacy(assay["id"], lot["id"])
        # 升级前：存量数据缺组标记
        self.assertIsNone(self.service.get(assay["id"])["group_name"])
        result = self.service.migrate_groups()
        self.assertEqual(result["default_group"], "general")
        self.assertGreaterEqual(result["pending"], 2)
        # 升级后先落到总务组，待指派
        for entity in self.service.pending_group_assignments():
            self.assertEqual(entity["group_name"], "general")
            self.assertEqual(entity["group_confirmed"], 0)
        # 逐条指派：检测项目 -> 生化，质控品批次 -> 生化
        summary = self.service.assign_pending_groups(
            self.supervisor,
            {assay["id"]: "biochem", lot["id"]: "biochem"},
        )
        self.assertIn(assay["id"], summary["assigned"])
        self.assertIn(lot["id"], summary["assigned"])
        self.assertEqual(summary["failed"], [])
        self.assertEqual(self.service.pending_group_assignments(), [])
        # 重复执行迁移幂等，不覆盖已指派
        again = self.service.migrate_groups()
        self.assertEqual(again["pending"], 0)
        self.assertEqual(self.service.get(assay["id"])["group_name"], "biochem")

    def test_failed_assignment_can_be_retried(self):
        assay, lot = self._legacy_data()
        self._mark_legacy(assay["id"], lot["id"])
        self.service.migrate_groups()
        # 故意先指派批次（此时项目还在总务组）-> 失败
        first = self.service.assign_pending_groups(
            self.supervisor,
            {lot["id"]: "biochem"},
        )
        self.assertEqual(first["assigned"], [])
        self.assertEqual(len(first["failed"]), 1)
        self.assertEqual(first["remaining"], 2)
        # 补上项目指派后重试 -> 成功
        retry = self.service.assign_pending_groups(
            self.supervisor,
            {assay["id"]: "biochem", lot["id"]: "biochem"},
        )
        self.assertIn(assay["id"], retry["assigned"])
        self.assertIn(lot["id"], retry["assigned"])
        self.assertEqual(retry["failed"], [])
        self.assertEqual(self.service.pending_group_assignments(), [])


if __name__ == "__main__":
    unittest.main()
