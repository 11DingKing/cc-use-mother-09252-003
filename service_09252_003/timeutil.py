"""时间与时间窗工具。

系统内部一律使用 UTC“瞬间”（自 Unix 纪元起的微秒整数, ``instant_us``），
彻底回避跨时区与日界线问题。所有展示与匹配都在 UTC 瞬间轴上进行；
时区只在解析用户输入、生成可读说明时使用。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MICROS_PER_SECOND = 1_000_000


class TimeParseError(ValueError):
    """时间或时间窗输入无法解析。"""


def now_us() -> int:
    """系统当前 UTC 瞬间（微秒）。"""
    return int(datetime.now(tz=timezone.utc).timestamp() * MICROS_PER_SECOND)


def parse_instant(value: object) -> int:
    """把整数（微秒）、数字（秒）或 RFC3339 字符串统一为 UTC 微秒瞬间。"""
    if isinstance(value, bool):
        raise TimeParseError("布尔值不是合法时间")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value * MICROS_PER_SECOND)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise TimeParseError("空字符串不是合法时间")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise TimeParseError(f"无法解析时间: {value!r}") from exc
        if dt.tzinfo is None:
            raise TimeParseError(f"时间必须携带时区偏移: {value!r}")
        return int(dt.timestamp() * MICROS_PER_SECOND)
    raise TimeParseError(f"不支持的时间类型: {type(value)!r}")


def format_instant(instant: int) -> str:
    """UTC 微秒瞬间 -> RFC3339（Z 结尾）。"""
    dt = datetime.fromtimestamp(instant / MICROS_PER_SECOND, tz=timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_window(payload: object) -> dict[str, object]:
    """解析时间窗输入。

    接受两种形式：

    * ``{"start": "<RFC3339 带偏移>", "end": "..."}``
    * ``{"start": "2026-03-01T00:00:00", "end": "...", "timezone": "Pacific/Kiritimati"}``
      —— 朴素本地时间按 ``timezone``（IANA 名）解释。

    返回 ``{"start_us", "end_us", "tz", "start_local", "end_local"}``，
    所有比较都使用 ``*_us``。
    """
    if not isinstance(payload, dict):
        raise TimeParseError("时间窗必须是对象")
    tz_name = payload.get("timezone")
    tz: ZoneInfo | None = None
    if tz_name:
        try:
            tz = ZoneInfo(str(tz_name))
        except Exception as exc:  # ZoneInfoError 是 KeyError 子类
            raise TimeParseError(f"未知时区: {tz_name!r}") from exc

    def _one(raw: object, endpoint: str) -> tuple[int, str]:
        if not isinstance(raw, str) or not raw.strip():
            raise TimeParseError(f"时间窗缺少 {endpoint}")
        text = raw.strip()
        if text.endswith(("Z", "z")):
            dt = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            if tz is None:
                raise TimeParseError(
                    f"朴素本地时间 {endpoint} 需要与 timezone 字段一起提供")
            dt = dt.replace(tzinfo=tz)
        return int(dt.timestamp() * MICROS_PER_SECOND), text

    start_us, start_local = _one(payload.get("start"), "start")
    end_us, end_local = _one(payload.get("end"), "end")
    if end_us <= start_us:
        raise TimeParseError("时间窗结束时间必须晚于开始时间")
    return {
        "start_us": start_us,
        "end_us": end_us,
        "tz": tz_name or "UTC",
        "start_local": start_local,
        "end_local": end_local,
    }


def windows_overlap(a: dict[str, object], b: dict[str, object]) -> bool:
    """两个时间窗在 UTC 瞬间轴上是否重叠。"""
    return overlap_seconds(a, b) > 0


def overlap_seconds(a: dict[str, object], b: dict[str, object]) -> int:
    """重叠时长（秒，UTC 瞬间轴）；不重叠返回 0。"""
    lo = max(int(a["start_us"]), int(b["start_us"]))
    hi = min(int(a["end_us"]), int(b["end_us"]))
    return max(0, (hi - lo) // MICROS_PER_SECOND)


def window_seconds(w: dict[str, object]) -> int:
    return (int(w["end_us"]) - int(w["start_us"])) // MICROS_PER_SECOND


def describe_window(w: dict[str, object]) -> str:
    """生成跨时区可读说明，例如 ``2026-03-01T00:00 (Pacific/Kiritimari; UTC 2026-02-28T10:00Z)``。"""
    return (
        f"{w['start_local']}~{w['end_local']} ({w['tz']}; "
        f"UTC {format_instant(int(w['start_us']))} ~ {format_instant(int(w['end_us']))})"
    )


def local_date(instant: int, tz_name: str) -> str:
    """UTC 瞬间在指定时区下的当地日期（用于跨日界线核对报到日）。"""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.fromtimestamp(instant / MICROS_PER_SECOND, tz=tz).date().isoformat()
