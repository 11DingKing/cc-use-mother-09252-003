"""超时释放回收器。

后台守护线程按固定间隔调用 :meth:`ExchangeService.release_expired`，
把到期的占位/锁定席位释放为空缺并触发有序补位。服务启动时
``ExchangeService(recover=True)`` 已先执行一次，保证重启后继续释放。
"""
from __future__ import annotations

import logging
import threading

from .service import ExchangeService

log = logging.getLogger("service_09252_003.reaper")


class Reaper:
    def __init__(self, service: ExchangeService, interval_seconds: float = 1.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须为正数")
        self._service = service
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="hold-reaper",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                result = self._service.release_expired(actor="reaper")
                if result["released"]:
                    log.info("释放超时席位 %s，补位 %s",
                             result["released"], result["promotions"])
            except Exception:  # pragma: no cover - 防御性
                log.exception("回收器执行失败，下个周期重试")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
