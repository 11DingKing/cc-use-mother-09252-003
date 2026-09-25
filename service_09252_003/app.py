"""应用装配与启动入口。

运行数据默认写入工作目录下的 ``data/``（不进源码目录），可用环境变量
``VISIT_DB`` 覆盖；``VISIT_HOST`` / ``VISIT_PORT`` 控制监听地址。

    python3 -m service_09252_003
"""
from __future__ import annotations

import os
from pathlib import Path

from .api import build_server
from .repository import Database
from .services import VisitExchangeService
from .timeutil import Clock, SystemClock


def create_service(
    db_path: str | None = None,
    clock: Clock | None = None,
) -> tuple[VisitExchangeService, Database]:
    """装配服务；默认使用文件库，测试可传入 ``:memory:`` 与假时钟。"""
    if db_path is None:
        db_path = os.environ.get(
            "VISIT_DB", str(Path.cwd() / "data" / "visit_exchange.db"))
    db = Database(db_path)
    service = VisitExchangeService(db, clock or SystemClock())
    return service, db


def main() -> None:
    host = os.environ.get("VISIT_HOST", "127.0.0.1")
    port = int(os.environ.get("VISIT_PORT", "8080"))
    service, db = create_service()
    server = build_server(service, host=host, port=port, reaper_interval=5.0)
    actual_host, actual_port = server.server_address[:2]
    print(f"跨国师资互访名额编排服务已启动: http://{actual_host}:{actual_port}")
    print(f"数据库: {db.path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.reaper.stop()  # type: ignore[attr-defined]
        server.shutdown()
        db.close()


if __name__ == "__main__":
    main()
