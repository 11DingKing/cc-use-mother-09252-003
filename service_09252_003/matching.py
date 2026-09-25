"""可解释匹配引擎。

对每个名额（:class:`~service_09252_003.models.Quota`）评估已通过评审的申请：

硬性约束（任一不满足即不可安排，写入拒绝理由）：
  1. 签证材料齐备（``candidate.visa_ready``）——签证周期；
  2. 课程空档与接收时间窗存在交集，且交集不短于最短访问时长——时间窗/日界线；
  3. 候选人专业集合与名额专业匹配——专业匹配；
  4. 申请预算不超过名额每人预算——经费来源。

评分（0~100，给出分项分解，便于解释与审计）：
  - 时间重叠占比 40
  - 专业契合 25（精确 25，同族关键词 15）
  - 预算余量 20（越省越高）
  - 签证就绪 15（硬约束通过即满分，区分同分顺序稳定）

前 ``seats`` 名为主选（进入 held/确认序列），其余按分数构成**有序替补**。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Application, ApplicationStatus, Candidate, Quota

MIN_VISIT_HOURS = 24.0

# 轻量的“同族专业”映射，仅用于评分；硬匹配走精确包含。
_DISCIPLINE_FAMILIES: dict[str, frozenset[str]] = {
    "cs": frozenset({"cs", "computer_science", "software", "ai", "data_science", "it"}),
    "ee": frozenset({"ee", "electronic", "electrical", "telecom", "automation"}),
    "business": frozenset({"business", "management", "finance", "economics", "mba"}),
    "mechanical": frozenset({"mechanical", "materials", "industrial", "aerospace"}),
}


@dataclass
class MatchResult:
    candidate_id: str
    application_id: str
    eligible: bool
    score: float
    breakdown: dict
    reasons: list[str]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "application_id": self.application_id,
            "eligible": self.eligible,
            "score": round(self.score, 3),
            "score_breakdown": self.breakdown,
            "reasons": self.reasons,
            "notes": self.notes,
        }


def _family_of(discipline: str) -> str | None:
    key = discipline.strip().lower()
    for name, members in _DISCIPLINE_FAMILIES.items():
        if key in members:
            return name
    return None


def score_pair(candidate: Candidate, quota: Quota, application: Application) -> MatchResult:
    reasons: list[str] = []
    notes: list[str] = []
    breakdown = {
        "time_overlap": 0.0,
        "discipline": 0.0,
        "budget_headroom": 0.0,
        "visa_ready": 0.0,
    }

    # 1) 签证周期：材料不齐备直接出局
    if not candidate.visa_ready:
        reasons.append("签证材料尚未齐备，无法在签证周期内完成安排")
    else:
        breakdown["visa_ready"] = 15.0

    # 2) 时间窗：课程空档 ∩ 接收窗口（全部按 UTC 绝对区间比较，跨时区/日界线安全）
    best_overlap = 0.0
    best_window = None
    for win in candidate.availability_windows:
        hours = win.overlap(quota.window)
        if hours > best_overlap:
            best_overlap = hours
            best_window = win
    required = min(MIN_VISIT_HOURS, quota.window.duration_hours)
    if best_overlap <= 0:
        reasons.append("课程空档与接收院校时间窗无交集（已按双方时区换算为绝对时间）")
    elif best_overlap < required:
        reasons.append(
            f"空档与时间窗仅重叠 {best_overlap:.1f} 小时，短于最短访问 {required:.1f} 小时"
        )
    else:
        breakdown["time_overlap"] = round(
            40.0 * min(best_overlap, quota.window.duration_hours) / quota.window.duration_hours,
            3,
        )

    # 3) 专业匹配
    cand_disciplines = {d.strip().lower() for d in candidate.disciplines}
    wanted = quota.discipline.strip().lower()
    discipline_hit = wanted in cand_disciplines
    family_hit = False
    if not discipline_hit:
        wanted_family = _family_of(wanted)
        family_hit = wanted_family is not None and any(
            _family_of(d) == wanted_family for d in cand_disciplines
        )
    if discipline_hit:
        breakdown["discipline"] = 25.0
    elif family_hit:
        breakdown["discipline"] = 15.0
        notes.append(f"专业与名额方向 {quota.discipline} 同族但非精确匹配")
    else:
        reasons.append(f"专业不匹配：名额要求 {quota.discipline}")

    # 4) 经费来源/预算
    if application.requested_budget > quota.budget_per_seat + 1e-9:
        reasons.append(
            f"申请预算 {application.requested_budget:.2f} 超过名额每人预算 "
            f"{quota.budget_per_seat:.2f}（经费来源：{quota.funding_source}）"
        )
    else:
        headroom = (quota.budget_per_seat - application.requested_budget) / quota.budget_per_seat
        breakdown["budget_headroom"] = round(20.0 * max(0.0, min(1.0, headroom)), 3)

    eligible = not reasons
    score = sum(breakdown.values()) if eligible else 0.0
    return MatchResult(
        candidate_id=candidate.id,
        application_id=application.id,
        eligible=eligible,
        score=round(score, 3),
        breakdown=breakdown,
        reasons=reasons,
        notes=notes,
    )


def rank_candidates(
    candidates: dict[str, Candidate],
    quotas: dict[str, Quota],
    applications: list[Application],
) -> dict[str, list[MatchResult]]:
    """按名额分组返回排序后的候选结果（含落选解释），同分按候选人 id 稳定排序。"""
    approved = [a for a in applications if a.status == ApplicationStatus.APPROVED]
    out: dict[str, list[MatchResult]] = {}
    for quota_id, quota in quotas.items():
        results: list[MatchResult] = []
        for app in approved:
            if app.quota_id != quota_id:
                continue
            candidate = candidates.get(app.candidate_id)
            if not candidate:
                continue
            results.append(score_pair(candidate, quota, app))
        results.sort(key=lambda r: (not r.eligible, -r.score, r.candidate_id))
        out[quota_id] = results
    return out
