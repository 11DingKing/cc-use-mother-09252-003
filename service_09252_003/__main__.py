"""命令行入口：``python3 -m service_09252_003``。

运行数据（SQLite 库）默认放在用户数据目录，可用 ``--db`` 或环境变量
``EXCHANGE_DB_PATH`` 覆盖，绝不写入源码目录。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .api import make_server
from .ports import SystemClock, UuidIdGenerator
from .reaper import Reaper
from .service import ExchangeService
from .storage import Storage

DEFAULT_DB = str(
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    / "service_09252_003" / "exchange.db")

DEMO_USERS = [
    {"token": "token-coordinator", "username": "coordinator",
     "role": "coordinator"},
    {"token": "token-finance", "username": "finance", "role": "finance"},
    {"token": "token-home-admin", "username": "home_admin",
     "role": "home_admin", "org": "派出大学A"},
    {"token": "token-host-admin", "username": "host_admin",
     "role": "host_admin", "org": "接收大学B"},
]


def build_service(db_path: str, *, recover: bool = True) -> ExchangeService:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(db_path)
    return ExchangeService(storage, SystemClock(), UuidIdGenerator(),
                           recover=recover)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨国师资互访名额编排服务")
    parser.add_argument("--db", default=os.environ.get("EXCHANGE_DB_PATH", DEFAULT_DB),
                        help="SQLite 数据库路径（默认用户数据目录）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reaper-interval", type=float, default=1.0,
                        help="超时释放扫描间隔（秒）")
    args = parser.parse_args(argv)

    service = build_service(args.db)
    seeded = service.seed_users(DEMO_USERS)
    if seeded:
        print(f"已初始化 {seeded} 个演示用户（token 见 DEMO_USERS）")
    reaper = Reaper(service, interval_seconds=args.reaper_interval)
    reaper.start()
    server = make_server(service, args.host, args.port)
    print(f"监听 http://{args.host}:{args.port}，数据库 {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reaper.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
