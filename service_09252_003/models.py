"""领域模型：候选人、接收院校（含名额）、申请与匹配方案。

状态机概览::

    Application(申请):  submitted -> under_review -> approved
                        submitted/under_review -> rejected
                        approved -> withdrawn（申请人主动撤回）

    Slot(名额项):       open -> held -> confirmed -> checked_in -> completed
                        held/open -> waitlisted（落选入替补）
                        held -> open（占位超时/拒绝确认，释放）
                        confirmed -> cancelled（签证拒绝/经费冻结/主动取消，触发补位）
                        waitlisted -> held（被补位成功）/ expired
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .timeutil import TimeWindow, iso


class Role(str, Enum):
    APPLICANT = "applicant"          # 候选人本人（仅可见/操作自己的申请）
    INTERNATIONAL_OFFICE = "io"      # 院校国际处：评审、锁定、补位、报到
    FINANCE = "finance"              # 财务：经费冻结/解冻
    ADMIN = "admin"                  # 管理员：建档、结项、责任记录查看
    SYSTEM = "system"                # # 系统主体（超时回收器），不接受外部请求伪造


class ApplicationStatus(str, Enum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class SlotStatus(str, Enum):
    OPEN = "open"                    # 尚未安排候选人
    HELD = "held"                    # 已占位，等待确认（有锁定期限）
    WAITLISTED = "waitlisted"        # 在某方案的替补序列中
    CONFIRMED = "confirmed"          # 已确认/已出票，不可自动挪动
    CHECKED_IN = "checked_in"        # 已报到
    COMPLETED = "completed"          # 已结项
    CANCELLED = "cancelled"          # 已取消（记录取消原因与责任方）
    EXPIRED = "expired"              # 占位超时或替补失效


class CancelReason(str, Enum):
    VISA_REJECTED = "visa_rejected"
    FUNDING_FROZEN = "funding_frozen"
    APPLICANT_CANCEL = "applicant_cancel"
    HOST_CANCEL = "host_cancel"
    HOLD_EXPIRED = "hold_expired"
    DECLINED = "declined"            # 收到占位但拒绝/未在期限内确认


class ResponsibleParty(str, Enum):
    APPLICANT = "applicant"
    SENDING_SCHOOL = "sending_school"
    HOST_SCHOOL = "host_school"
    FUNDING_BODY = "funding_body"
    CONSULATE = "consulate"
    SYSTEM = "system"


@dataclass
class Candidate:
    id: str
    name: str
    sending_school: str
    disciplines: list[str]                     # 可授课/研究方向
    availability_windows: list[TimeWindow]     # 课程空档（可多个，UTC 绝对区间）
    passport_no: str = ""
    visa_ready: bool = False                   # 签证材料是否齐备

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "sending_school": self.sending_school,
            "disciplines": list(self.disciplines),
            "passport_no": self.passport_no,
            "visa_ready": self.visa_ready,
            "availability_windows": [w.to_dict() for w in self.availability_windows],
        }


@dataclass
class Quota:
    """接收院校在一个时间窗内的名额。"""

    id: str
    host_school: str
    discipline: str
    window: TimeWindow
    seats: int
    budget_per_seat: float
    funding_source: str
    funding_frozen: bool = False
    ticketed_seats: int = 0                     # 已出票（confirmed 后）的座位数

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "host_school": self.host_school,
            "discipline": self.discipline,
            "window": self.window.to_dict(),
            "seats": self.seats,
            "budget_per_seat": self.budget_per_seat,
            "funding_source": self.funding_source,
            "funding_frozen": self.funding_frozen,
            "ticketed_seats": self.ticketed_seats,
        }


@dataclass
class Application:
    id: str
    candidate_id: str
    quota_id: str
    status: ApplicationStatus
    requested_budget: float
    submitted_at: datetime
    updated_at: datetime
    version: int = 0
    reviewer: str = ""
    review_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "quota_id": self.quota_id,
            "status": self.status.value,
            "requested_budget": self.requested_budget,
            "submitted_at": iso(self.submitted_at),
            "updated_at": iso(self.updated_at),
            "version": self.version,
            "reviewer": self.reviewer,
            "review_note": self.review_note,
        }


@dataclass
class Slot:
    """一次具体的“名额-候选人”编排项（方案行）。"""

    id: str
    plan_id: str
    quota_id: str
    candidate_id: str
    rank: int                                  # 匹配排序位次（1 起）
    status: SlotStatus
    score: float
    score_breakdown: dict
    explanation: list[str]
    held_until: datetime | None = None
    confirmed_at: datetime | None = None
    ticketed: bool = False
    checked_in_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str = ""
    responsible_party: str = ""
    replaced_slot_id: str | None = None        # 本槽位由哪个取消槽位补位而来
    promoted_from_slot_id: str | None = None   # 替补来源槽位（审计链）
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "plan_id": self.plan_id,
            "quota_id": self.quota_id,
            "candidate_id": self.candidate_id,
            "rank": self.rank,
            "status": self.status.value,
            "score": round(self.score, 3),
            "score_breakdown": self.score_breakdown,
            "explanation": list(self.explanation),
            "held_until": iso(self.held_until) if self.held_until else None,
            "confirmed_at": iso(self.confirmed_at) if self.confirmed_at else None,
            "ticketed": self.ticketed,
            "checked_in_at": iso(self.checked_in_at) if self.checked_in_at else None,
            "completed_at": iso(self.completed_at) if self.completed_at else None,
            "cancelled_at": iso(self.cancelled_at) if self.cancelled_at else None,
            "cancel_reason": self.cancel_reason,
            "responsible_party": self.responsible_party,
            "replaced_slot_id": self.replaced_slot_id,
            "promoted_from_slot_id": self.promoted_from_slot_id,
        }
        return out


@dataclass
class Plan:
    """一轮匹配方案：每个名额含主选与替补序列，可分批确认。"""

    id: str
    quota_id: str
    created_at: datetime
    created_by: str
    status: str = "active"                     # active / closed
    slots: list[Slot] = field(default_factory=list)

    def to_dict(self, include_slots: bool = True) -> dict[str, Any]:
        out = {
            "id": self.id,
            "quota_id": self.quota_id,
            "created_at": iso(self.created_at),
            "created_by": self.created_by,
            "status": self.status,
        }
        if include_slots:
            out["slots"] = [s.to_dict() for s in sorted(self.slots, key=lambda s: s.rank)]
        return out


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
