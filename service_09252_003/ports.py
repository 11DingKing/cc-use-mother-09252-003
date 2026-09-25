"""可替换端口：时钟与标识生成器。

生产使用系统时钟与 UUID；测试注入 :class=`FakeClock` 和顺序 ID 生成器，
使“超时释放”“服务重启后恢复”等场景可以确定性复现。
"""
from __future__ import annotations

import itertools
import uuid
from typing import Protocol

from .timeutil import now_us


class Clock(Protocol):
    def now_us(self) -> int: ...


class SystemClock:
    def now_us(self) -> int:
        return now_us()


class FakeClock:
    """测试用时钟：从给定瞬间起步，可手动推进。"""

    def __init__(self, start_us: int | None = None) -> None:
        self._now = start_us if start_us is not None else now_us()

    def now_us(self) -> int:
        return self._now

    def advance(self, seconds: int) -> int:
        self._now += seconds * 1_000_000
        return self._now

    def set(self, instant_us: int) -> None:
        self._now = instant_us


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str: ...


class UuidIdGenerator:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIdGenerator:
    """测试用：``cand_1``、``cand_2``……按前缀独立计数。"""

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count] = {}

    def new_id(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}_{next(counter)}"
