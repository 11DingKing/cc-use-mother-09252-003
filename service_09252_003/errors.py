"""领域错误与错误码。

所有跨层抛出的业务异常都带稳定的机器可读 ``code``，HTTP 边界据此映射状态码。
"""
from __future__ import annotations


class DomainError(Exception):
    """全部领域异常的基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class AuthError(DomainError):
    code = "unauthorized"
    http_status = 401


class PermissionError(DomainError):  # noqa: A001 - 领域内有意遮蔽内置名
    code = "forbidden"
    http_status = 403


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class StateConflictError(DomainError):
    code = "state_conflict"
    http_status = 409


class CapacityConflictError(DomainError):
    code = "capacity_conflict"
    http_status = 409


class IdempotencyConflictError(DomainError):
    code = "idempotency_in_progress"
    http_status = 409
