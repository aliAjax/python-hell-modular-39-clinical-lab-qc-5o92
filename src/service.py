from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    GENERAL_GROUP,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import RuleEngine

#: 可以执行组指派/重新确认的角色（含总务组协调员）。
ASSIGN_ROLES = ("coordinator", "supervisor", "admin")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # -- 专业组 ---------------------------------------------------------------

    def _group_exists(self, group_id):
        if not group_id:
            return False
        return self.repository.get_entity(group_id) is not None

    def list_groups(self):
        return self.repository.list_entities(kind="lab_group")

    def list_violations(self, entity_id=None):
        return self.repository.list_group_violations(entity_id=entity_id)

    def list_assignments(self, status=None):
        return self.repository.list_group_assignments(status=status)

    def _deny_cross_group(self, actor, entity, action, reason):
        """跨组操作：直接拒绝，并记下是谁。"""
        self.repository.append_group_violation(
            entity_id=entity.get("id") if isinstance(entity, dict) else entity,
            kind=entity.get("kind") if isinstance(entity, dict) else "unknown",
            action=action,
            actor=actor,
            target_group=(
                entity.get("data", {}).get("group_id")
                if isinstance(entity, dict)
                else None
            ),
            reason=reason,
        )
        raise PermissionDenied(reason)

    def _assert_assigned(self, entity):
        """归属未确认（等待重新指派）时冻结一切操作。"""
        if entity["data"].get("group_confirmed") is False:
            raise ConflictError(
                "%s %s 的专业组归属待重新确认，操作已冻结" % (entity["kind"], entity["id"])
            )

    def _assert_group_scope(self, actor, entity, action):
        self._assert_assigned(entity)
        target_group = entity["data"].get("group_id") or GENERAL_GROUP
        if actor.group_id != target_group:
            self._deny_cross_group(
                actor,
                entity,
                action,
                "跨组操作被拒绝：用户属于 %s，对象归属 %s" % (actor.group_id, target_group),
            )

    def _assay_for(self, assay_id):
        assay = self.repository.get_entity(assay_id) if assay_id else None
        if not assay:
            raise ValidationError("assay does not exist")
        return assay

    def _guard_create(self, actor, kind, payload):
        """创建前的组归属校验。"""
        if kind == "assay":
            target_group = payload.get("group_id") or actor.group_id
            payload["group_id"] = target_group
            payload["group_confirmed"] = True
            if actor.group_id != target_group:
                self._deny_cross_group(
                    actor,
                    {"id": payload.get("id") or "(new)", "kind": kind,
                     "data": {"group_id": target_group}},
                    "create",
                    "跨组创建被拒绝：用户属于 %s，目标组为 %s"
                    % (actor.group_id, target_group),
                )
        elif kind in ("qc_lot", "result_batch"):
            assay = self._assay_for(payload.get("assay_id"))
            self._assert_assigned(assay)
            # 质控品批次/患者结果批次的组必须与其检测项目一致，
            # 不能出现项目归一组、批次落在另一组；请求里自带的 group_id 无效。
            if actor.group_id != assay["data"]["group_id"]:
                self._deny_cross_group(
                    actor,
                    {"id": payload.get("id") or "(new)", "kind": kind,
                     "data": {"group_id": assay["data"]["group_id"]}},
                    "create",
                    "跨组创建被拒绝：检测项目归属 %s" % assay["data"]["group_id"],
                )
            payload["group_id"] = assay["data"]["group_id"]
            payload["group_confirmed"] = True
        if kind == "qc_run":
            assay = self._assay_for(payload.get("assay_id"))
            self._assert_group_scope(actor, assay, "create")

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self._guard_create(actor, kind, payload)
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(
            entity_id, actor, "create", None, status,
            {"kind": kind, "group_id": payload.get("group_id")},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] in self.rules.GROUP_SCOPED_KINDS or entity["kind"] == "qc_run":
            owner = entity
            if entity["kind"] == "qc_run":
                owner = self._assay_for(entity["data"].get("assay_id"))
            self._assert_group_scope(actor, owner, action)
        else:
            owner = None
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
            {"patch": patch, "group_id": owner["data"].get("group_id") if owner else None},
        )
        return updated

    # -- 组指派 / 改组 --------------------------------------------------------

    def assign_group(self, actor, entity_id, target_group):
        """逐条指派专业组归属，或对改组后待确认的批次重新确认。

        - 未确认的检测项目：指派到任意已存在的组（总务组协调员可代派）。
        - 已确认的检测项目改组：仅本组 supervisor/admin 可执行，
          相关批次在同一事务内重指到新组并进入待重新确认状态。
        - 质控品批次/患者结果批次：只能重新确认到其检测项目所在组。
        """
        if not self._group_exists(target_group):
            raise ValidationError("lab group does not exist: " + str(target_group))
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] not in self.rules.GROUP_SCOPED_KINDS:
            raise ValidationError("%s 不按专业组归属" % entity["kind"])

        confirmed = entity["data"].get("group_confirmed") is not False
        current_group = entity["data"].get("group_id") or GENERAL_GROUP

        if entity["kind"] == "assay":
            if confirmed and current_group != target_group:
                # 已确认项目改组只能由当前组 supervisor/admin 执行；
                # 其它组的尝试一律记越权并拒绝（先于角色判定）。
                if actor.group_id != current_group:
                    self._deny_cross_group(
                        actor, entity, "assign_group",
                        "跨组改组被拒绝：项目归属 %s" % current_group,
                    )
                if actor.role not in ("supervisor", "admin"):
                    raise PermissionDenied("只有组长/管理员可以调整检测项目的组归属")
                cascaded = self.repository.reassign_assay_group(entity, target_group)
                updated = self.repository.get_entity(entity_id)
                self.audit.record(
                    entity_id, actor, "assign_group", entity["status"], entity["status"],
                    {"from_group": current_group, "to_group": target_group,
                     "cascaded": cascaded},
                )
                for child_id in cascaded:
                    child = self.repository.get_entity(child_id)
                    self.audit.record(
                        child_id, actor, "group_scope_reset", child["status"],
                        child["status"],
                        {"from_group": current_group, "to_group": target_group,
                         "reason": "assay group changed; reconfirmation required"},
                    )
                return updated
            # 首次指派（含总务组待落位项目）：先查越权再查角色。
            if actor.group_id not in (GENERAL_GROUP, target_group):
                self._deny_cross_group(
                    actor, entity, "assign_group",
                    "跨组指派被拒绝：用户属于 %s，目标组为 %s"
                    % (actor.group_id, target_group),
                )
            if actor.role not in ASSIGN_ROLES:
                raise PermissionDenied("role %s 不允许指派专业组归属" % actor.role)
            updated = self.repository.set_entity_group(entity_id, target_group, True)
            self.repository.remove_group_assignment(entity_id)
            self.audit.record(
                entity_id, actor, "assign_group", entity["status"], entity["status"],
                {"from_group": current_group, "to_group": target_group, "initial": not confirmed},
            )
            return updated

        # 质控品批次 / 患者结果批次：归属跟随检测项目。
        assay = self._assay_for(entity["data"].get("assay_id"))
        assay_group = assay["data"].get("group_id") or GENERAL_GROUP
        if target_group != assay_group:
            raise ValidationError(
                "批次必须归属其检测项目所在组（%s），不能单独指派到 %s"
                % (assay_group, target_group)
            )
        if actor.group_id not in (GENERAL_GROUP, target_group):
            self._deny_cross_group(
                actor, entity, "assign_group",
                "跨组指派被拒绝：用户属于 %s，目标组为 %s"
                % (actor.group_id, target_group),
            )
        if actor.role not in ASSIGN_ROLES:
            raise PermissionDenied("role %s 不允许指派专业组归属" % actor.role)
        if assay["data"].get("group_confirmed") is False:
            raise ConflictError("检测项目尚未完成组归属确认，请先指派检测项目后再重试")
        updated = self.repository.set_entity_group(entity_id, target_group, True)
        self.repository.remove_group_assignment(entity_id)
        self.audit.record(
            entity_id, actor, "assign_group", entity["status"], entity["status"],
            {"group_id": target_group, "assay_id": assay["id"]},
        )
        return updated

    def process_pending_assignments(self, actor, targets=None):
        """逐条处理待指派队列。单条失败不中断，记录错误后继续，可重试续跑。

        批次的目标组取其检测项目当前所在组；项目尚未落位的批次本轮跳过，
        下一次重试时再处理。
        """
        if actor.role not in ASSIGN_ROLES or actor.group_id != GENERAL_GROUP:
            raise PermissionDenied("只有总务组协调员/组长/管理员可以批量指派归属")
        wanted = dict(targets or {})
        pending = self.repository.list_group_assignments(status="pending")
        result = {"processed": [], "failed": [], "skipped": []}
        for item in pending:
            entity_id = item["entity_id"]
            # 显式指派只处理点到的条目；没点到的留待后续重试，不自动落位。
            if wanted and entity_id not in wanted:
                continue
            target_group = wanted.get(entity_id, item["target_group"])
            entity = self.repository.get_entity(entity_id)
            if not entity:
                self.repository.mark_group_assignment(
                    entity_id, "failed", "entity no longer exists")
                result["failed"].append({"entity_id": entity_id,
                                         "error": "entity no longer exists"})
                continue
            if not target_group:
                if entity["kind"] == "assay":
                    # 项目没有显式目标组：本轮无法落位，等待后续指派。
                    result["skipped"].append(
                        {"entity_id": entity_id, "reason": "no target group"})
                    continue
                assay = self.repository.get_entity(entity["data"].get("assay_id"))
                if assay and assay["data"].get("group_confirmed") is not False:
                    target_group = assay["data"]["group_id"]
                if not target_group:
                    result["skipped"].append({"entity_id": entity_id,
                                              "reason": "waiting for assay assignment"})
                    continue
            try:
                if not self._group_exists(target_group):
                    raise ValidationError("lab group does not exist: " + str(target_group))
                if entity["kind"] == "assay":
                    updated = self.repository.set_entity_group(entity_id, target_group, True)
                    self.repository.remove_group_assignment(entity_id)
                else:
                    assay = self._assay_for(entity["data"].get("assay_id"))
                    if assay["data"].get("group_confirmed") is False:
                        result["skipped"].append(
                            {"entity_id": entity_id, "reason": "assay unconfirmed"})
                        continue
                    if target_group != assay["data"]["group_id"]:
                        raise ValidationError(
                            "target group %s does not match assay group %s"
                            % (target_group, assay["data"]["group_id"])
                        )
                    updated = self.repository.set_entity_group(entity_id, target_group, True)
                    self.repository.remove_group_assignment(entity_id)
                self.audit.record(
                    entity_id, actor, "assign_group", entity["status"], entity["status"],
                    {"group_id": target_group, "batch": True},
                )
                result["processed"].append({"entity_id": entity_id,
                                            "group_id": updated["data"]["group_id"]})
            except Exception as exc:  # 指派失败：记错误，接着处理没处理完的
                message = "%s: %s" % (type(exc).__name__, exc)
                self.repository.mark_group_assignment(
                    entity_id, "failed", message, target_group=target_group)
                result["failed"].append({"entity_id": entity_id, "error": message})
        return result

    def retry_pending_assignments(self, actor):
        """把上一轮失败的条目重新置为待处理，然后继续逐条重试。"""
        if actor.role not in ASSIGN_ROLES or actor.group_id != GENERAL_GROUP:
            raise PermissionDenied("只有总务组协调员/组长/管理员可以重试指派")
        for item in self.repository.list_group_assignments(status="failed"):
            self.repository.enqueue_group_assignment(
                item["entity_id"], item["kind"], item["target_group"])
        return self.process_pending_assignments(actor)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
