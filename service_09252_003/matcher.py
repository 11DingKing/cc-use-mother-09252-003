"""可解释匹配引擎。

对每个 (候选人, 名额) 组合评估四类硬约束：

1. 专业匹配（discipline，忽略大小写的精确匹配）
2. 课程空档重叠（候选人任一时间窗与名额时间窗在 UTC 瞬间轴上重叠）
3. 经费来源（候选人可用经费来源包含名额经费来源）
4. 签证状态（ok / pending 可匹配，rejected 不可）

评估结果包含逐项 ``checks``、人类可读的 ``reasons``（中文说明，含 UTC 与
当地时间的对照）与加权 ``score``，供方案解释与替补排序使用。
"""
from __future__ import annotations

from typing import Any

from .models import (
    SCORE_BUDGET,
    SCORE_DISCIPLINE,
    SCORE_VISA_OK,
    SCORE_VISA_PENDING,
    SCORE_WINDOW_MAX,
    VisaStatus,
)
from .timeutil import describe_window, overlap_seconds, window_seconds

ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"


def evaluate(candidate: dict[str, Any], quota: dict[str, Any]) -> dict[str, Any]:
    """评估单个候选人对单个名额的适配度，返回可解释结果。"""
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    score = 0.0

    # 1. 专业匹配
    cand_disc = str(candidate["discipline"]).strip().lower()
    quota_disc = str(quota["discipline"]).strip().lower()
    if cand_disc == quota_disc:
        checks.append({"code": "discipline", "ok": True,
                       "detail": f"{candidate['discipline']} == {quota['discipline']}"})
        score += SCORE_DISCIPLINE
    else:
        checks.append({"code": "discipline", "ok": False,
                       "detail": f"{candidate['discipline']} != {quota['discipline']}"})
        reasons.append(f"专业不匹配：候选人 {candidate['discipline']}，"
                       f"名额要求 {quota['discipline']}")

    # 2. 课程空档重叠（UTC 瞬间轴）
    quota_window = quota["window"]
    best_overlap = 0
    best_window: dict[str, Any] | None = None
    for w in candidate["windows"]:
        ov = overlap_seconds(w, quota_window)
        if ov > best_overlap:
            best_overlap = ov
            best_window = w
    if best_overlap > 0 and best_window is not None:
        days = best_overlap / 86400
        window_score = SCORE_WINDOW_MAX * min(1.0, best_overlap / window_seconds(quota_window))
        checks.append({"code": "window_overlap", "ok": True,
                       "detail": f"重叠 {days:.2f} 天",
                       "overlap_seconds": best_overlap})
        reasons.append(
            f"课程空档重叠 {days:.1f} 天：候选人空档 {describe_window(best_window)}；"
            f"名额窗口 {describe_window(quota_window)}")
        score += window_score
    else:
        checks.append({"code": "window_overlap", "ok": False,
                       "detail": "无重叠"})
        cand_desc = "、".join(describe_window(w) for w in candidate["windows"]) or "（无空档）"
        reasons.append(
            f"课程空档不重叠：候选人空档 {cand_desc}；"
            f"名额窗口 {describe_window(quota_window)}")

    # 3. 经费来源
    if quota["budget_source"] in candidate["budget_sources"]:
        checks.append({"code": "budget_source", "ok": True,
                       "detail": quota["budget_source"]})
        score += SCORE_BUDGET
    else:
        checks.append({"code": "budget_source", "ok": False,
                       "detail": f"需要 {quota['budget_source']}，"
                                 f"候选人仅有 {candidate['budget_sources']}"})
        reasons.append(
            f"经费来源不符：名额使用 {quota['budget_source']}，"
            f"候选人可用来源为 {', '.join(candidate['budget_sources']) or '（无）'}")

    # 4. 签证状态
    visa = candidate["visa_status"]
    if visa == VisaStatus.OK:
        checks.append({"code": "visa", "ok": True, "detail": "签证有效"})
        score += SCORE_VISA_OK
    elif visa == VisaStatus.PENDING:
        checks.append({"code": "visa", "ok": True, "detail": "签证办理中（降权）"})
        reasons.append("签证仍在办理中，匹配成功但评分降权")
        score += SCORE_VISA_PENDING
    else:
        checks.append({"code": "visa", "ok": False, "detail": "签证已被拒绝"})
        reasons.append("签证已被拒绝，不可匹配")

    eligible = all(c["ok"] for c in checks)
    return {
        "candidate_id": candidate["id"],
        "quota_id": quota["id"],
        "status": ELIGIBLE if eligible else INELIGIBLE,
        "score": round(score, 3),
        "checks": checks,
        "reasons": reasons,
    }


def generate_proposal(candidates: list[dict[str, Any]],
                      quota: dict[str, Any],
                      free_slots: int) -> dict[str, Any]:
    """为一个名额生成可解释方案：每个候选人给出评估，合格者按分数排序推荐。

    不修改任何状态，纯函数，便于“先解释、后确认”。
    """
    evaluations = [evaluate(c, quota) for c in candidates]
    eligible = sorted(
        (e for e in evaluations if e["status"] == ELIGIBLE),
        key=lambda e: (-e["score"], e["candidate_id"]),
    )
    ineligible = [e for e in evaluations if e["status"] == INELIGIBLE]
    recommended = [e["candidate_id"] for e in eligible[:max(0, free_slots)]]
    return {
        "quota_id": quota["id"],
        "host_org": quota["host_org"],
        "discipline": quota["discipline"],
        "window": quota["window"],
        "free_slots": free_slots,
        "recommended": recommended,
        "evaluations": evaluations,
        "summary": (
            f"名额 {quota['id']}（{quota['host_org']} / {quota['discipline']}）"
            f"空余 {free_slots} 席；{len(eligible)} 人合格，"
            f"{len(ineligible)} 人不合格；推荐 {recommended or '（无）'}"
        ),
    }
