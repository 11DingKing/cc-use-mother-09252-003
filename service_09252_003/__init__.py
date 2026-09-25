"""跨国师资互访名额编排的服务端包入口。"""
from .ports import FakeClock, SequentialIdGenerator, SystemClock, UuidIdGenerator
from .service import ExchangeService
from .storage import Storage

PROJECT_CODE = "service_09252_003"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "跨国师资互访名额编排"}


def make_service(db_path: str = ":memory:", *, clock=None, ids=None,
                 recover: bool = True, **kwargs) -> ExchangeService:
    """便捷工厂：构建挂在指定数据库上的服务实例。"""
    return ExchangeService(
        Storage(db_path),
        clock or SystemClock(),
        ids or UuidIdGenerator(),
        recover=recover,
        **kwargs,
    )


__all__ = [
    "PROJECT_CODE",
    "project_info",
    "make_service",
    "ExchangeService",
    "Storage",
    "SystemClock",
    "FakeClock",
    "UuidIdGenerator",
    "SequentialIdGenerator",
]
