"""时间与时区端口。

领域内的时间一律使用**带偏移量的感知时间**（aware datetime），存储为带偏移的
ISO-8601 字符串。各时区的“本地日期窗口”在比较前先转换为 UTC 绝对区间，因此天然
支持跨时区与国际日期变更线：奥克兰（UTC+13）的 10 月 1 日与洛杉矶（UTC-7）的
9 月 30 日在 UTC 轴上可能是同一段时间。

时钟通过 :class:`Clock` 注入，测试可冻结/快进，服务重启后用同一时钟重放超时。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_instant(value: str | datetime, tz_name: str | None = None) -> datetime:
    """把输入解析为感知时间。

    - 带偏移的 ISO-8601 字符串：原样保留其绝对时刻；
    - 朴素日期/日期时间 + ``tz_name``：按该 IANA 时区本地化（处理夏令时）。
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if len(text) == 10:  # YYYY-MM-DD
            dt = datetime.combine(date.fromisoformat(text), datetime.min.time())
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        if not tz_name:
            raise ValueError(f"朴素时间 {value!r} 缺少时区信息")
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc)


def local_day_bounds(day: str | date, tz_name: str) -> tuple[datetime, datetime]:
    """返回某时区本地自然日 ``[当天 00:00, 次日 00:00)`` 的 UTC 绝对区间。"""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """稳定的存储/传输格式（秒级，带偏移；UTC 内部以 +00:00 落库）。"""
    if dt.tzinfo is None:
        raise ValueError("禁止序列化朴素时间")
    return dt.astimezone(timezone.utc).isoformat()


def display(dt: datetime, tz_name: str) -> str:
    """按给定时区渲染本地时间，供解释信息与接口输出使用。"""
    return dt.astimezone(ZoneInfo(tz_name)).isoformat()


@dataclass(frozen=True)
class TimeWindow:
    """半开时间区间 ``[start, end)``，两端均为 UTC 感知时间。"""

    start: datetime
    end: datetime
    start_tz: str = "UTC"
    end_tz: str = "UTC"
    label: str = ""

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("时间窗必须带时区")
        if self.end <= self.start:
            raise ValueError(f"非法时间窗: {self.start} >= {self.end}")

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def overlap(self, other: "TimeWindow") -> float:
        """与另一窗口的重叠小时数；不重叠返回 0。"""
        lo = max(self.start, other.start)
        hi = min(self.end, other.end)
        return max(0.0, (hi - lo).total_seconds() / 3600.0)

    def intersect(self, other: "TimeWindow") -> "TimeWindow | None":
        lo = max(self.start, other.start)
        hi = min(self.end, other.end)
        if lo >= hi:
            return None
        return TimeWindow(lo, hi)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "start_utc": iso(self.start),
            "end_utc": iso(self.end),
            "start_local": display(self.start, self.start_tz),
            "end_local": display(self.end, self.end_tz),
            "duration_hours": round(self.duration_hours, 2),
        }


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return now_utc()


class FakeClock:
    """测试时钟：可设定起点并线程安全地快进。"""

    def __init__(self, start: datetime | str | None = None) -> None:
        if start is None:
            current = now_utc()
        elif isinstance(start, str):
            current = parse_instant(start)
        else:
            current = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        self._now = current.astimezone(timezone.utc)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, delta: timedelta | int) -> None:
        if isinstance(delta, int):
            delta = timedelta(seconds=delta)
        with self._lock:
            self._now += delta

    def set(self, value: datetime | str) -> None:
        value = parse_instant(value) if isinstance(value, str) else value
        with self._lock:
            self._now = value.astimezone(timezone.utc)
