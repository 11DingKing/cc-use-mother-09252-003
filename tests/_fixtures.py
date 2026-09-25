"""测试共用构造助手。"""
from __future__ import annotations

from service_09252_003.models import Role
from service_09252_003.repository import Database
from service_09252_003.services import Actor, VisitExchangeService
from service_09252_003.timeutil import FakeClock


def actor(actor_id: str, role: Role, school: str = "", name: str = "") -> Actor:
    return Actor(actor_id, role, school=school, name=name)


ADMIN = actor("admin-1", Role.ADMIN, name="管理员")


def world(db_path: str = ":memory:", clock: FakeClock | None = None):
    clock = clock or FakeClock("2026-09-01T00:00:00+00:00")
    db = Database(db_path)
    svc = VisitExchangeService(db, clock)
    return svc, db, clock


def io_of(school: str) -> Actor:
    return actor(f"io-{school}", Role.INTERNATIONAL_OFFICE, school=school,
                 name=f"{school}国际处老师")


def window(start: str, end: str, tz: str, end_tz: str | None = None,
           label: str = "") -> dict:
    return {"start": start, "end": end, "tz": tz, "end_tz": end_tz or tz,
            "label": label or f"{tz}窗口"}


def add_candidate(svc, cid: str, *, school="THU", disciplines=("cs",),
                  windows=None, visa_ready=True, name: str | None = None,
                  admin: Actor = ADMIN) -> dict:
    return svc.register_candidate(admin, {
        "id": cid,
        "name": name or f"候选人{cid}",
        "sending_school": school,
        "disciplines": list(disciplines),
        "availability_windows": BIG_AVAIL if windows is None else windows,
        "visa_ready": visa_ready,
    })


def add_quota(svc, qid: str = "Q1", *, host="MIT", discipline="cs", seats=1,
              budget=10000.0, funding="CSC基金", win=None,
              admin: Actor = ADMIN) -> dict:
    win = win or window("2026-10-01T00:00", "2026-10-15T00:00",
                        "America/Los_Angeles")
    return svc.register_quota(admin, {
        "id": qid, "host_school": host, "discipline": discipline,
        "seats": seats, "budget_per_seat": budget,
        "funding_source": funding,
        "window_start": win["start"], "window_end": win["end"],
        "tz": win["tz"], "end_tz": win["end_tz"],
    })


def apply_and_approve(svc, cid: str, qid: str = "Q1", *, budget=8000.0,
                      host: str = "MIT", idem: bool = True) -> str:
    """候选人提交申请并由名额所属国际处通过，返回申请 id。"""
    applicant = actor(cid, Role.APPLICANT)
    res = svc.apply(applicant,
                    {"candidate_id": cid, "quota_id": qid,
                     "requested_budget": budget},
                    f"apply-{cid}-{qid}" if idem else None)
    app_id = res.data["id"]
    svc.review(io_of(host), app_id, {"decision": "approve", "note": "材料合格"})
    return app_id


def make_plan(svc, qid: str = "Q1", host: str = "MIT") -> dict:
    return svc.generate_plan(io_of(host), qid)


def seeded_plan(svc, candidates: list[dict], qid: str = "Q1", *,
                host="MIT", seats=1, **quota_kw) -> dict:
    """一条龙：建档 + 申请/通过 + 生成方案。candidates 元素为
    (cid, disciplines, windows, budget, visa_ready) 或 dict。"""
    add_quota(svc, qid, host=host, seats=seats, **quota_kw)
    for i, spec in enumerate(candidates):
        if isinstance(spec, dict):
            cid = spec.get("id", f"C{i+1}")
            add_candidate(
                svc, cid,
                disciplines=spec.get("disciplines", ("cs",)),
                windows=spec.get("windows"),
                visa_ready=spec.get("visa_ready", True))
            apply_and_approve(svc, cid, qid, host=host,
                              budget=spec.get("budget", 8000.0))
        else:
            cid, disciplines, windows, budget, visa_ready = spec
            add_candidate(svc, cid, disciplines=disciplines, windows=windows,
                          visa_ready=visa_ready)
            apply_and_approve(svc, cid, qid, host=host, budget=budget)
    return make_plan(svc, qid, host)


# 一个覆盖 2026-10 月上旬的“万能空档”，用 UTC 表达
BIG_AVAIL = [window("2026-09-25T00:00", "2026-10-31T00:00", "UTC",
                    label="秋季空档")]
