"""领域错误。

所有应用层错误都继承 :class:`DomainError`，携带稳定的机器可读 ``code``、
HTTP 状态码与可选详情，接口层据此生成统一的错误响应体。
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """业务规则被违反时抛出。"""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: Any = None, code: str | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


class ValidationError(DomainError):
    status = 422
    code = "validation_error"


class AuthError(DomainError):
    status = 401
    code = "unauthorized"


class PermissionError(DomainError):  # noqa: A001 - 领域内有意同名
    status = 403
    code = "forbidden"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class TicketedImmovableError(ConflictError):
    """已出票（及之后状态）的安排不可自动挪动。"""

    code = "ticketed_immovable"


class IdempotencyConflict(ConflictError):
    code = "idempotency_conflict"
