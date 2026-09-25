"""SQLite 持久化。

设计要点：

* 单连接 + 进程级可重入锁，所有写路径经 :meth:`Storage.transaction` 进入，
  事务用 ``BEGIN IMMEDIATE`` 立刻取得写锁，从根上避免并发占位写穿。
* 名额容量用“槽位行”（quota_slots）实现：建名额时物化 ``capacity`` 个槽，
  占位/锁定/出票都是槽状态翻转，天然支持并发占位与超时释放。
* 时间一律存 UTC 微秒整数；列表/字典字段存 JSON 文本。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    org TEXT NOT NULL DEFAULT '',
    candidate_id TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    discipline TEXT NOT NULL,
    home_org TEXT NOT NULL,
    windows_json TEXT NOT NULL,
    visa_status TEXT NOT NULL,
    budget_sources_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quotas (
    id TEXT PRIMARY KEY,
    host_org TEXT NOT NULL,
    discipline TEXT NOT NULL,
    window_json TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    budget_source TEXT NOT NULL,
    budget_total REAL NOT NULL,
    review_ttl_seconds INTEGER NOT NULL,
    lock_ttl_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_slots (
    id TEXT PRIMARY KEY,
    quota_id TEXT NOT NULL REFERENCES quotas(id),
    seq INTEGER NOT NULL,
    state TEXT NOT NULL,
    candidate_id TEXT,
    application_id TEXT,
    hold_reason TEXT,
    hold_expires_at INTEGER,
    locked_at INTEGER,
    ticketed_at INTEGER,
    checkin_at INTEGER,
    closeout_at INTEGER,
    closeout_report TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_quota ON quota_slots(quota_id, state);
CREATE INDEX IF NOT EXISTS idx_slots_candidate ON quota_slots(candidate_id, state);
CREATE INDEX IF NOT EXISTS idx_slots_expiry ON quota_slots(state, hold_expires_at);
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    quota_id TEXT NOT NULL REFERENCES quotas(id),
    slot_id TEXT,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    eval_json TEXT,
    decide_reason TEXT,
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_apps_quota ON applications(quota_id, status);
CREATE INDEX IF NOT EXISTS idx_apps_candidate ON applications(candidate_id, status);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    responsibility TEXT NOT NULL,
    at INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

JSON_COLUMNS = {
    "candidates": ("windows_json", "budget_sources_json"),
    "quotas": ("window_json",),
    "applications": ("eval_json",),
    "events": ("data_json",),
}
JSON_KEY = {
    "windows_json": "windows",
    "budget_sources_json": "budget_sources",
    "window_json": "window",
    "eval_json": "eval",
    "data_json": "data",
}


def _decode(table: str, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for col in JSON_COLUMNS.get(table, ()):
        raw = data.pop(col)
        data[JSON_KEY[col]] = json.loads(raw) if raw is not None else None
    return data


def _decode_all(table: str, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [_decode(table, r) for r in rows]  # type: ignore[misc]


class Storage:
    """对单个 SQLite 库的线程安全访问。"""

    def __init__(self, path: str = ":memory:") -> None:
        # isolation_level=None：自动提交模式，事务完全由 transaction()
        # 的 BEGIN IMMEDIATE/COMMIT/ROLLBACK 显式控制，避免隐式事务泄漏。
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """独占写事务（可重入：嵌套调用复用外层事务）。"""
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # ---- 基础读写 ------------------------------------------------------
    def insert(self, table: str, data: dict[str, Any]) -> None:
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))

    def get(self, table: str, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return _decode(table, row)

    def query(self, table: str, where: str = "", params: tuple = (),
              order_by: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return _decode_all(table, rows)

    def query_one(self, table: str, where: str, params: tuple = (),
                  order_by: str = "") -> dict[str, Any] | None:
        rows = self.query(table, where, params, order_by, limit=1)
        return rows[0] if rows else None

    def update(self, table: str, row_id: str, fields: dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE {table} SET {sets} WHERE id = ?",
                (*fields.values(), row_id))

    def conditional_update(self, table: str, row_id: str,
                           fields: dict[str, Any], where: str,
                           params: tuple = ()) -> bool:
        """乐观并发原语：仅当 where 条件仍成立时更新，返回是否命中。"""
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {table} SET {sets} WHERE id = ? AND {where}",
                (*fields.values(), row_id, *params))
        return cur.rowcount > 0

    # ---- 用户 ----------------------------------------------------------
    def add_user(self, token: str, username: str, role: str,
                 org: str = "", candidate_id: str | None = None) -> None:
        self.insert("users", {
            "token": token, "username": username, "role": role,
            "org": org, "candidate_id": candidate_id,
        })

    def user_by_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        return self.query("users", order_by="username")

    # ---- 候选人 --------------------------------------------------------
    def insert_candidate(self, c: dict[str, Any]) -> None:
        self.insert("candidates", {
            "id": c["id"], "name": c["name"], "discipline": c["discipline"],
            "home_org": c["home_org"],
            "windows_json": json.dumps(c["windows"], ensure_ascii=False),
            "visa_status": c["visa_status"],
            "budget_sources_json": json.dumps(c["budget_sources"], ensure_ascii=False),
            "status": c["status"], "created_at": c["created_at"],
        })

    def update_candidate(self, candidate_id: str, **fields: Any) -> None:
        encoded: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "windows":
                encoded["windows_json"] = json.dumps(value, ensure_ascii=False)
            elif key == "budget_sources":
                encoded["budget_sources_json"] = json.dumps(value, ensure_ascii=False)
            else:
                encoded[key] = value
        self.update("candidates", candidate_id, encoded)

    # ---- 名额与槽位 ----------------------------------------------------
    def insert_quota(self, q: dict[str, Any]) -> None:
        self.insert("quotas", {
            "id": q["id"], "host_org": q["host_org"], "discipline": q["discipline"],
            "window_json": json.dumps(q["window"], ensure_ascii=False),
            "capacity": q["capacity"], "budget_source": q["budget_source"],
            "budget_total": q["budget_total"],
            "review_ttl_seconds": q["review_ttl_seconds"],
            "lock_ttl_seconds": q["lock_ttl_seconds"],
            "status": q["status"], "created_at": q["created_at"],
        })

    def insert_slot(self, s: dict[str, Any]) -> None:
        self.insert("quota_slots", {
            "id": s["id"], "quota_id": s["quota_id"], "seq": s["seq"],
            "state": s["state"], "candidate_id": s.get("candidate_id"),
            "application_id": s.get("application_id"),
            "hold_reason": s.get("hold_reason"),
            "hold_expires_at": s.get("hold_expires_at"),
            "locked_at": s.get("locked_at"), "ticketed_at": s.get("ticketed_at"),
            "checkin_at": s.get("checkin_at"), "closeout_at": s.get("closeout_at"),
            "closeout_report": s.get("closeout_report"),
            "version": s.get("version", 0), "updated_at": s["updated_at"],
        })

    def update_slot_fields(self, slot_id: str, **fields: Any) -> None:
        self.update("quota_slots", slot_id, fields)

    def slot_transition(self, slot_id: str, from_states: tuple[str, ...],
                        **fields: Any) -> bool:
        """仅当槽当前状态属于 from_states 时应用更新（并发占位的核心原语）。"""
        marks = ", ".join("?" for _ in from_states)
        return self.conditional_update(
            "quota_slots", slot_id, fields, f"state IN ({marks})", from_states)

    def list_slots(self, quota_id: str, states: tuple[str, ...] | None = None
                   ) -> list[dict[str, Any]]:
        if states:
            marks = ", ".join("?" for _ in states)
            return self.query("quota_slots",
                              f"quota_id = ? AND state IN ({marks})",
                              (quota_id, *states), order_by="seq")
        return self.query("quota_slots", "quota_id = ?", (quota_id,), order_by="seq")

    def expired_holds(self, now: int) -> list[dict[str, Any]]:
        return self.query(
            "quota_slots",
            "state IN ('held', 'locked') AND hold_expires_at IS NOT NULL "
            "AND hold_expires_at <= ?",
            (now,), order_by="hold_expires_at")

    # ---- 申请 ----------------------------------------------------------
    def insert_application(self, a: dict[str, Any]) -> None:
        self.insert("applications", {
            "id": a["id"], "candidate_id": a["candidate_id"],
            "quota_id": a["quota_id"], "slot_id": a.get("slot_id"),
            "status": a["status"], "note": a.get("note", ""),
            "eval_json": json.dumps(a["eval"], ensure_ascii=False) if a.get("eval") else None,
            "decide_reason": a.get("decide_reason"),
            "created_at": a["created_at"], "decided_at": a.get("decided_at"),
            "version": a.get("version", 0),
        })

    def update_application(self, app_id: str, **fields: Any) -> None:
        encoded: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "eval":
                encoded["eval_json"] = json.dumps(value, ensure_ascii=False) if value else None
            else:
                encoded[key] = value
        self.update("applications", app_id, encoded)

    def open_application(self, candidate_id: str, quota_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "applications",
            "candidate_id = ? AND quota_id = ? AND status IN ('pending', 'approved')",
            (candidate_id, quota_id))

    # ---- 事件（责任记录） ---------------------------------------------
    def append_event(self, e: dict[str, Any]) -> None:
        self.insert("events", {
            "id": e["id"], "type": e["type"], "actor": e["actor"],
            "responsibility": e["responsibility"], "at": e["at"],
            "entity_type": e["entity_type"], "entity_id": e["entity_id"],
            "data_json": json.dumps(e.get("data") or {}, ensure_ascii=False),
        })

    def list_events(self, entity_type: str | None = None,
                    entity_id: str | None = None) -> list[dict[str, Any]]:
        if entity_type and entity_id:
            return self.query("events", "entity_type = ? AND entity_id = ?",
                              (entity_type, entity_id), order_by="at, id")
        if entity_type:
            return self.query("events", "entity_type = ?", (entity_type,),
                              order_by="at, id")
        return self.query("events", order_by="at, id")

    # ---- 幂等 ----------------------------------------------------------
    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["response"] = json.loads(data.pop("response_json"))
        return data

    def put_idempotency(self, key: str, actor: str, request_hash: str,
                        response: dict[str, Any], created_at: int) -> None:
        self.insert("idempotency_keys", {
            "key": key, "actor": actor, "request_hash": request_hash,
            "response_json": json.dumps(response, ensure_ascii=False),
            "created_at": created_at,
        })
