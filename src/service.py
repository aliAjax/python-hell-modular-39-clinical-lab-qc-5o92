from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    GENERAL_GROUP,
    GROUP_OWNED_KINDS,
    LAB_GROUPS,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ---- 组归属（专业组隔离） ----

    def _group_of(self, entity):
        """返回对象的 (组名, 是否已确认归属)。

        检测项目/质控品批次/患者结果批次直接带组；质控结果通过其质控品批次继承组。
        仪器为共用资源，不按组隔离。
        """
        kind = entity["kind"]
        if kind in GROUP_OWNED_KINDS:
            return entity.get("group_name"), bool(entity.get("group_confirmed", 0))
        if kind == "qc_run":
            lot = self.repository.get_entity(entity["data"].get("qc_lot_id"))
            if lot:
                return lot.get("group_name"), bool(lot.get("group_confirmed", 0))
        return None, True

    def _deny_cross_group(self, actor, entity, action, group_name, confirmed):
        detail = {
            "actor_group": actor.group,
            "entity_group": group_name,
            "entity_id": entity["id"],
            "action": action,
            "confirmed": confirmed,
        }
        if not confirmed:
            self.audit.record(
                entity["id"], actor, "group_unconfirmed", entity["status"], entity["status"], detail
            )
            raise PermissionDenied(
                "entity %s group assignment is unconfirmed; reassign to a group first" % entity["id"]
            )
        self.audit.record(
            entity["id"], actor, "cross_group_denied", entity["status"], entity["status"], detail
        )
        raise PermissionDenied(
            "cross-group operation denied: actor group %s cannot act on entity in group %s"
            % (actor.group, group_name)
        )

    def _check_group_access(self, actor, entity, action):
        group_name, confirmed = self._group_of(entity)
        if group_name is None:
            return  # 共用资源（仪器）或无组对象，不做组隔离
        if not confirmed or group_name != actor.group:
            self._deny_cross_group(actor, entity, action, group_name, confirmed)

    def _assert_reference_in_group(self, actor, entity_id, action):
        if not entity_id:
            raise ValidationError("reference entity is required")
        ref = self.repository.get_entity(entity_id)
        if not ref:
            raise NotFoundError("referenced entity not found: " + entity_id)
        group_name, confirmed = self._group_of(ref)
        if group_name is None:
            return
        if not confirmed or group_name != actor.group:
            self._deny_cross_group(actor, ref, action, group_name, confirmed)

    def _group_for_create(self, actor, kind, payload):
        """创建前校验引用对象与操作者同组，并返回新对象应归属的组。"""
        if kind == "assay":
            return actor.group
        if kind == "qc_lot":
            self._assert_reference_in_group(actor, payload.get("assay_id"), "create")
            return actor.group
        if kind == "result_batch":
            self._assert_reference_in_group(actor, payload.get("assay_id"), "create")
            self._assert_reference_in_group(actor, payload.get("qc_run_id"), "create")
            return actor.group
        if kind == "qc_run":
            self._assert_reference_in_group(actor, payload.get("assay_id"), "create")
            self._assert_reference_in_group(actor, payload.get("qc_lot_id"), "create")
            return None  # qc_run 通过批次继承组，自身不落组
        return None

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        group_name = self._group_for_create(actor, kind, payload)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(
            entity_id, kind, status, payload, actor.user_id,
            group_name=group_name, group_confirmed=1,
        )
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind, "group": group_name})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        self._check_group_access(actor, entity, action)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id, actor=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if actor is not None:
            self._check_group_access(actor, entity, "get")
        return entity

    def list(self, kind=None, status=None, actor=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        entities = self.repository.list_entities(kind=kind, status=status)
        if actor is not None:
            entities = [entity for entity in entities if self._can_read(actor, entity)]
        return entities

    def _can_read(self, actor, entity):
        group_name, confirmed = self._group_of(entity)
        if group_name is None:
            return True
        return confirmed and group_name == actor.group

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ---- 组归属指派与迁移 ----

    def assign_group(self, actor, entity_id, group):
        """把检测项目/质控品批次/患者结果批次指派到指定专业组。

        检测项目换组后，其下批次的组确认失效，需重新确认归属；
        批次重新确认时必须与检测项目当前组一致，杜绝项目归一组、批次在另一组。
        """
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] not in GROUP_OWNED_KINDS:
            raise ValidationError("kind %s cannot be assigned to a group" % entity["kind"])
        if group not in LAB_GROUPS:
            raise ValidationError("unknown group: " + group)
        self.rules._ensure_role(actor, ("supervisor", "admin"))
        if entity["kind"] == "assay":
            updated = self.repository.update_group(entity_id, group, True)
            invalidated = self.repository.invalidate_groups_for_assay(entity_id)
            self.audit.record(
                entity_id, actor, "assign_group", entity["status"], entity["status"],
                {"group": group, "invalidated_batches": invalidated},
            )
            for batch_id in invalidated:
                batch = self.repository.get_entity(batch_id)
                self.audit.record(
                    batch_id, actor, "group_reconfirm_required", batch["status"], batch["status"],
                    {"assay_id": entity_id, "assay_group": group},
                )
            return updated
        # qc_lot / result_batch：必须与其检测项目同组
        assay = self.repository.get_entity(entity["data"].get("assay_id"))
        if not assay:
            raise ValidationError("assay does not exist")
        assay_group, assay_confirmed = self._group_of(assay)
        if not assay_confirmed or assay_group != group:
            raise ValidationError(
                "batch group must match its assay's group (%s)" % (assay_group or "unassigned")
            )
        updated = self.repository.update_group(entity_id, group, True)
        self.audit.record(
            entity_id, actor, "assign_group", entity["status"], entity["status"],
            {"group": group},
        )
        return updated

    def migrate_groups(self, default_group=GENERAL_GROUP):
        """升级：存量数据先落到总务组并标记待指派。幂等，可重复执行。"""
        pending = self.repository.upgrade_group_ownership(default_group)
        return {"default_group": default_group, "pending": pending}

    def pending_group_assignments(self):
        """列出所有待重新确认组归属的对象。"""
        return self.repository.list_pending_group_entities(GROUP_OWNED_KINDS)

    def assign_pending_groups(self, actor, assignments):
        """逐条指派待归属对象；单条失败不影响其余，返回成功/失败明细，便于断点重试。"""
        self.rules._ensure_role(actor, ("supervisor", "admin"))
        pending = {entity["id"]: entity for entity in self.pending_group_assignments()}
        assigned = []
        failed = []
        # 检测项目先于批次指派，保证批次重新确认时项目已在目标组
        for kind in GROUP_OWNED_KINDS:
            for entity_id, group in (assignments or {}).items():
                entity = pending.get(entity_id)
                if not entity or entity["kind"] != kind:
                    continue
                try:
                    self.assign_group(actor, entity_id, group)
                    assigned.append(entity_id)
                except Exception as exc:
                    failed.append({"id": entity_id, "error": str(exc)})
        return {"assigned": assigned, "failed": failed, "remaining": len(pending) - len(assigned)}
