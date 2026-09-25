"""领域枚举与常量。"""
from __future__ import annotations


class Role:
    COORDINATOR = "coordinator"  # 院校国际处
    TEACHER = "teacher"          # 候选人本人
    HOME_ADMIN = "home_admin"    # 派出院校管理员
    HOST_ADMIN = "host_admin"    # 接收院校管理员
    FINANCE = "finance"          # 经费管理

    ALL = (COORDINATOR, TEACHER, HOME_ADMIN, HOST_ADMIN, FINANCE)


class VisaStatus:
    OK = "ok"
    PENDING = "pending"
    REJECTED = "rejected"
    ALL = (OK, PENDING, REJECTED)


class CandidateStatus:
    ACTIVE = "active"
    FROZEN = "frozen"  # 经费冻结
    ALL = (ACTIVE, FROZEN)


class QuotaStatus:
    OPEN = "open"
    CLOSED = "closed"
    ALL = (OPEN, CLOSED)


class SlotState:
    AVAILABLE = "available"      # 空缺席位
    HELD = "held"                # 评审占位（有时限）
    LOCKED = "locked"            # 已锁定待出票（有时限）
    TICKETED = "ticketed"        # 已出票，不可自动挪动
    CHECKED_IN = "checked_in"    # 已报到
    COMPLETED = "completed"      # 已结项
    # 终态/不可逆链：ticketed -> checked_in -> completed
    IMMOVABLE = (TICKETED, CHECKED_IN, COMPLETED)
    OCCUPIED = (HELD, LOCKED) + IMMOVABLE


class AppStatus:
    PENDING = "pending"          # 评审中（可能占席，也可能在替补队列中）
    APPROVED = "approved"        # 评审通过
    REJECTED = "rejected"
    EXPIRED = "expired"          # 占位/锁定超时
    CANCELLED = "cancelled"      # 主动取消或连带取消
    OPEN_STATES = (PENDING, APPROVED)
    DECIDED = (REJECTED, EXPIRED, CANCELLED)


class HoldReason:
    REVIEW = "review"
    LOCK = "lock"


class Responsibility:
    """责任归属，写入审计事件，用于事后追责。"""
    CANDIDATE = "candidate"
    VISA_AUTHORITY = "visa_authority"
    FINANCE = "finance"
    HOME_ORG = "home_org"
    HOST_ORG = "host_org"
    COORDINATOR = "coordinator"
    TIMEOUT = "timeout"
    SYSTEM = "system"
    ALL = (CANDIDATE, VISA_AUTHORITY, FINANCE, HOME_ORG, HOST_ORG,
           COORDINATOR, TIMEOUT, SYSTEM)


class EventType:
    CANDIDATE_CREATED = "candidate_created"
    QUOTA_CREATED = "quota_created"
    QUOTA_CLOSED = "quota_closed"
    PROPOSAL_VIEWED = "proposal_viewed"
    APPLICATION_CREATED = "application_created"
    APPLICATION_WAITLISTED = "application_waitlisted"
    APPLICATION_DECIDED = "application_decided"
    SLOT_HELD = "slot_held"
    SLOT_LOCKED = "slot_locked"
    SLOT_TICKETED = "slot_ticketed"
    SLOT_CHECKED_IN = "slot_checked_in"
    SLOT_CLOSED_OUT = "slot_closed_out"
    SLOT_CANCELLED = "slot_cancelled"
    HOLD_EXPIRED = "hold_expired"
    SUBSTITUTION_PROMOTED = "substitution_promoted"
    SUBSTITUTION_SKIPPED = "substitution_skipped"
    VISA_REJECTED = "visa_rejected"
    FUNDING_FROZEN = "funding_frozen"
    TICKETED_PROTECTED = "ticketed_protected"


# 匹配评分权重（可解释）
SCORE_DISCIPLINE = 40
SCORE_WINDOW_MAX = 30
SCORE_BUDGET = 20
SCORE_VISA_OK = 10
SCORE_VISA_PENDING = 5

DEFAULT_REVIEW_TTL_SECONDS = 72 * 3600
DEFAULT_LOCK_TTL_SECONDS = 24 * 3600
