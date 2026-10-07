from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


#: 总务组：升级时缺失组标记的数据先落到这里，再逐条指派。
GENERAL_GROUP = "general"


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


@dataclass
class Actor:
    user_id: str
    role: str
    group_id: str = GENERAL_GROUP

    @classmethod
    def from_headers(cls, headers):
        user_id = headers.get("X-User-Id", "anonymous")
        role = headers.get("X-Role", "viewer")
        if role not in {item.value for item in Role}:
            raise PermissionDenied("unknown role: " + role)
        group_id = headers.get("X-Group-Id", GENERAL_GROUP) or GENERAL_GROUP
        return cls(user_id=user_id, role=role, group_id=group_id)


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
