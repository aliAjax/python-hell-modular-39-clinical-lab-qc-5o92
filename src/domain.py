from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class DomainError(Exception):
    """Base class for domain failures."""


class ValidationError(DomainError):
    """Input does not satisfy a domain rule."""


class PermissionDenied(DomainError):
    """Actor is not allowed to perform the action."""


class NotFoundError(DomainError):
    """Requested record does not exist."""


class ConflictError(DomainError):
    """A uniqueness or version constraint was violated."""


class InvalidTransition(DomainError):
    """The requested state transition is not valid."""


class Role(str, Enum):
    viewer = "viewer"
    reporter = "reporter"
    operator = "operator"
    analyst = "analyst"
    coordinator = "coordinator"
    supervisor = "supervisor"
    auditor = "auditor"
    admin = "admin"


# 专业组（检验科各专业组共用一套系统，权限按组隔离）
GENERAL_GROUP = "general"  # 总务组：历史数据升级时的兜底归属
LAB_GROUPS = (
    "biochem",       # 生化
    "immunology",    # 免疫
    "microbiology",  # 微生物
    "hematology",    # 血液
    "molecular",     # 分子
    GENERAL_GROUP,
)
# 需要按组归属的对象；质控结果(qc_run)通过其质控品批次继承组
GROUP_OWNED_KINDS = ("assay", "qc_lot", "result_batch")


@dataclass
class Actor:
    user_id: str
    role: str
    group: str = GENERAL_GROUP

    @classmethod
    def from_headers(cls, headers):
        user_id = headers.get("X-User-Id", "anonymous")
        role = headers.get("X-Role", "viewer")
        if role not in {item.value for item in Role}:
            raise PermissionDenied("unknown role: " + role)
        group = headers.get("X-Group", GENERAL_GROUP) or GENERAL_GROUP
        if group not in LAB_GROUPS:
            raise PermissionDenied("unknown group: " + group)
        return cls(user_id=user_id, role=role, group=group)


@dataclass
class Entity:
    id: str
    kind: str
    status: str
    version: int
    data: Dict[str, Any]
    created_by: str
    created_at: str
    updated_at: str
