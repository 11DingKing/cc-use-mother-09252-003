"""测试公共辅助：确定性时钟、顺序 ID、种子用户与数据构造器。"""
from __future__ import annotations

from typing import Any

from service_09252_003 import (
    ExchangeService,
    FakeClock,
    SequentialIdGenerator,
    Storage,
)

# 2027-01-15T08:00:00Z，所有名额/空档窗口都在此之后
T0_US = 1_800_000_000_000_000

REVIEW_TTL = 100  # 秒，测试用小 TTL 便于推进时钟
LOCK_TTL = 50

USERS = [
    {"token": "tok-coord", "username": "coord", "role": "coordinator"},
    {"token": "tok-finance", "username": "fin", "role": "finance"},
    {"token": "tok-homeA", "username": "homeA", "role": "home_admin",
     "org": "派出大学A"},
    {"token": "tok-homeB", "username": "homeB", "role": "home_admin",
     "org": "派出大学B"},
    {"token": "tok-hostX", "username": "hostX", "role": "host_admin",
     "org": "接收大学X"},
    {"token": "tok-hostY", "username": "hostY", "role": "host_admin",
     "org": "接收大学Y"},
]

DEFAULT_WINDOW = {"start": "2027-03-01T00:00:00+00:00",
                  "end": "2027-04-01T00:00:00+00:00"}


def make_service(db_path: str = ":memory:", *, recover: bool = False,
                 clock: FakeClock | None = None,
                 review_ttl: int = REVIEW_TTL,
                 lock_ttl: int = LOCK_TTL) -> tuple[ExchangeService, FakeClock]:
    clk = clock or FakeClock(T0_US)
    service = ExchangeService(
        Storage(db_path), clk, SequentialIdGenerator(),
        review_ttl_seconds=review_ttl, lock_ttl_seconds=lock_ttl,
        recover=recover)
    service.seed_users(USERS)
    return service, clk


def actor(service: ExchangeService, token: str) -> dict[str, Any]:
    return service.authenticate(token)


def coord(service: ExchangeService) -> dict[str, Any]:
    return actor(service, "tok-coord")


def finance(service: ExchangeService) -> dict[str, Any]:
    return actor(service, "tok-finance")


def add_teacher_user(service: ExchangeService, candidate_id: str,
                     username: str = "teacher1") -> dict[str, Any]:
    """登记一个绑定候选人的教师用户，返回其 actor。"""
    service.add_user(coord(service), {
        "token": f"tok-{username}", "username": username,
        "role": "teacher", "candidate_id": candidate_id})
    return actor(service, f"tok-{username}")


def cand_payload(name: str = "教师甲", discipline: str = "计算机",
                 home_org: str = "派出大学A",
                 windows: list[dict[str, Any]] | None = None,
                 visa_status: str = "ok",
                 budget_sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "discipline": discipline,
        "home_org": home_org,
        "windows": windows if windows is not None else [dict(DEFAULT_WINDOW)],
        "visa_status": visa_status,
        "budget_sources": budget_sources if budget_sources is not None
        else ["校级基金"],
    }


def quota_payload(host_org: str = "接收大学X", discipline: str = "计算机",
                  capacity: int = 1, budget_source: str = "校级基金",
                  window: dict[str, Any] | None = None,
                  review_ttl_seconds: int = REVIEW_TTL,
                  lock_ttl_seconds: int = LOCK_TTL) -> dict[str, Any]:
    return {
        "host_org": host_org,
        "discipline": discipline,
        "window": window if window is not None else dict(DEFAULT_WINDOW),
        "capacity": capacity,
        "budget_source": budget_source,
        "budget_total": 100000,
        "review_ttl_seconds": review_ttl_seconds,
        "lock_ttl_seconds": lock_ttl_seconds,
    }


def add_candidate(service: ExchangeService, **kwargs: Any) -> dict[str, Any]:
    return service.create_candidate(coord(service), cand_payload(**kwargs))["candidate"]


def add_quota(service: ExchangeService, **kwargs: Any) -> dict[str, Any]:
    return service.create_quota(coord(service), quota_payload(**kwargs))["quota"]


def apply(service: ExchangeService, candidate_id: str, quota_id: str,
          **kwargs: Any) -> dict[str, Any]:
    return service.apply(coord(service), {
        "candidate_id": candidate_id, "quota_id": quota_id, **kwargs})


def approve_all(service: ExchangeService, quota_id: str) -> None:
    apps = service.list_applications(coord(service), quota_id=quota_id,
                                     status="pending")["applications"]
    for app in apps:
        service.review(coord(service), app["id"], "approve")


def events_of(service: ExchangeService, entity_type: str,
              entity_id: str) -> list[dict[str, Any]]:
    return service.list_events(coord(service), entity_type, entity_id)["events"]


def event_types(service: ExchangeService, entity_type: str,
                entity_id: str) -> list[str]:
    return [e["type"] for e in events_of(service, entity_type, entity_id)]
