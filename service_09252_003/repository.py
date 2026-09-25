"""SQLite 持久化适配器。

设计要点：

- 开启 WAL；所有写事务使用 ``BEGIN IMMEDIATE``，配合 ``busy_timeout`` 把并发写
  串行化，应用层在单事务内完成“读状态—改状态—写流水”，杜绝并发占位超卖。
- 幂等键独立建表：``done`` 直接回放结果，``pending``（进程崩溃残留）返回 409。
- 所有状态变化写 ``events`` 流水，责任记录可审计；超时释放与补位同样落事件。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .models import (
    Application,
    ApplicationStatus,
    Candidate,
    Plan,
    Quota,
    Slot,
    SlotStatus,
)
from .timeutil import TimeWindow, iso, parse_instant

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sending_school TEXT NOT NULL,
    disciplines TEXT NOT NULL,
    availability_windows TEXT NOT NULL,
    passport_no TEXT NOT NULL DEFAULT '',
    visa_ready INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quotas (
    id TEXT PRIMARY KEY,
    host_school TEXT NOT NULL,
    discipline TEXT NOT NULL,
    window TEXT NOT NULL,
    seats INTEGER NOT NULL,
    budget_per_seat REAL NOT NULL,
    funding_source TEXT NOT NULL,
    funding_frozen INTEGER NOT NULL DEFAULT 0,
    ticketed_seats INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    quota_id TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_budget REAL NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    reviewer TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    UNIQUE(candidate_id, quota_id)
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    quota_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    quota_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    status TEXT NOT NULL,
    score REAL NOT NULL,
    score_breakdown TEXT NOT NULL,
    explanation TEXT NOT NULL,
    held_until TEXT,
    confirmed_at TEXT,
    ticketed INTEGER NOT NULL DEFAULT 0,
    checked_in_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT '',
    responsible_party TEXT NOT NULL DEFAULT '',
    replaced_slot_id TEXT,
    promoted_from_slot_id TEXT,
    idempotency_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_slots_quota_status ON slots(quota_id, status);
CREATE INDEX IF NOT EXISTS idx_slots_plan ON slots(plan_id);
CREATE INDEX IF NOT EXISTS idx_slots_held ON slots(status, held_until);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    slot_id TEXT,
    plan_id TEXT,
    quota_id TEXT,
    candidate_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_slot ON events(slot_id);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,            -- pending / done / error
    response TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _window_to_json(w: TimeWindow) -> str:
    return json.dumps(w.to_dict(), ensure_ascii=False)


def _window_from_json(text: str) -> TimeWindow:
    d = json.loads(text)
    return TimeWindow(
        start=parse_instant(d["start_utc"]),
        end=parse_instant(d["end_utc"]),
        start_tz=d.get("start_tz", "UTC"),
        end_tz=d.get("end_tz", "UTC"),
        label=d.get("label", ""),
    )


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = 10000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """立即拿写锁的串行化事务；同线程嵌套时复用外层事务。"""
        conn = self._conn
        self._lock.acquire()
        outer = self._tx_depth == 0
        try:
            if outer:
                conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            yield conn
            self._tx_depth -= 1
            if outer:
                conn.execute("COMMIT")
        except BaseException:
            self._tx_depth = max(0, self._tx_depth - 1)
            if outer:
                conn.execute("ROLLBACK")
            raise
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --------------------------------------------------------------- candidates
    def upsert_candidate(self, c: Candidate) -> None:
        self._conn.execute(
            """INSERT INTO candidates(id, name, sending_school, disciplines,
                   availability_windows, passport_no, visa_ready)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, sending_school=excluded.sending_school,
                 disciplines=excluded.disciplines,
                 availability_windows=excluded.availability_windows,
                 passport_no=excluded.passport_no,
                 visa_ready=excluded.visa_ready""",
            (
                c.id,
                c.name,
                c.sending_school,
                json.dumps(c.disciplines, ensure_ascii=False),
                json.dumps([w.to_dict() for w in c.availability_windows], ensure_ascii=False),
                c.passport_no,
                1 if c.visa_ready else 0,
            ),
        )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        row = self._conn.execute(
            "SELECT * FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        return self._row_to_candidate(row) if row else None

    def list_candidates(self) -> list[Candidate]:
        rows = self._conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()
        return [self._row_to_candidate(r) for r in rows]

    @staticmethod
    def _row_to_candidate(row: sqlite3.Row) -> Candidate:
        windows = [
            TimeWindow(
                start=parse_instant(d["start_utc"]),
                end=parse_instant(d["end_utc"]),
                start_tz=d.get("start_tz", "UTC"),
                end_tz=d.get("end_tz", "UTC"),
                label=d.get("label", ""),
            )
            for d in json.loads(row["availability_windows"])
        ]
        return Candidate(
            id=row["id"],
            name=row["name"],
            sending_school=row["sending_school"],
            disciplines=json.loads(row["disciplines"]),
            availability_windows=windows,
            passport_no=row["passport_no"],
            visa_ready=bool(row["visa_ready"]),
        )

    # ------------------------------------------------------------------ quotas
    def upsert_quota(self, q: Quota) -> None:
        self._conn.execute(
            """INSERT INTO quotas(id, host_school, discipline, window, seats,
                   budget_per_seat, funding_source, funding_frozen, ticketed_seats, version)
               VALUES (?,?,?,?,?,?,?,0,0,0)
               ON CONFLICT(id) DO UPDATE SET
                 host_school=excluded.host_school, discipline=excluded.discipline,
                 window=excluded.window, seats=excluded.seats,
                 budget_per_seat=excluded.budget_per_seat,
                 funding_source=excluded.funding_source""",
            (
                q.id,
                q.host_school,
                q.discipline,
                _window_to_json(q.window),
                q.seats,
                q.budget_per_seat,
                q.funding_source,
            ),
        )

    def get_quota(self, quota_id: str) -> Quota | None:
        row = self._conn.execute("SELECT * FROM quotas WHERE id=?", (quota_id,)).fetchone()
        return self._row_to_quota(row) if row else None

    def list_quotas(self) -> list[Quota]:
        rows = self._conn.execute("SELECT * FROM quotas ORDER BY id").fetchall()
        return [self._row_to_quota(r) for r in rows]

    def set_funding_flag(self, quota_id: str, frozen: bool) -> None:
        self._conn.execute(
            "UPDATE quotas SET funding_frozen=?, version=version+1 WHERE id=?",
            (1 if frozen else 0, quota_id),
        )

    def adjust_ticketed(self, quota_id: str, delta: int) -> None:
        self._conn.execute(
            "UPDATE quotas SET ticketed_seats = ticketed_seats + ? WHERE id=?",
            (delta, quota_id),
        )

    @staticmethod
    def _row_to_quota(row: sqlite3.Row) -> Quota:
        return Quota(
            id=row["id"],
            host_school=row["host_school"],
            discipline=row["discipline"],
            window=_window_from_json(row["window"]),
            seats=row["seats"],
            budget_per_seat=row["budget_per_seat"],
            funding_source=row["funding_source"],
            funding_frozen=bool(row["funding_frozen"]),
            ticketed_seats=row["ticketed_seats"],
        )

    # ------------------------------------------------------------- applications
    def insert_application(self, a: Application) -> None:
        self._conn.execute(
            """INSERT INTO applications(id, candidate_id, quota_id, status,
                   requested_budget, submitted_at, updated_at, version, reviewer, review_note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                a.id, a.candidate_id, a.quota_id, a.status.value, a.requested_budget,
                iso(a.submitted_at), iso(a.updated_at), a.version, a.reviewer, a.review_note,
            ),
        )

    def get_application(self, app_id: str) -> Application | None:
        row = self._conn.execute(
            "SELECT * FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        return self._row_to_application(row) if row else None

    def find_application(self, candidate_id: str, quota_id: str) -> Application | None:
        row = self._conn.execute(
            "SELECT * FROM applications WHERE candidate_id=? AND quota_id=?",
            (candidate_id, quota_id),
        ).fetchone()
        return self._row_to_application(row) if row else None

    def list_applications(
        self, quota_id: str | None = None, candidate_id: str | None = None,
        status: ApplicationStatus | None = None,
    ) -> list[Application]:
        sql = "SELECT * FROM applications WHERE 1=1"
        args: list[Any] = []
        if quota_id:
            sql += " AND quota_id=?"
            args.append(quota_id)
        if candidate_id:
            sql += " AND candidate_id=?"
            args.append(candidate_id)
        if status:
            sql += " AND status=?"
            args.append(status.value)
        sql += " ORDER BY submitted_at, id"
        return [self._row_to_application(r) for r in self._conn.execute(sql, args).fetchall()]

    def update_application(self, a: Application) -> None:
        cur = self._conn.execute(
            "UPDATE applications SET status=?, updated_at=?, version=version+1, "
            "reviewer=?, review_note=? WHERE id=? AND version=?",
            (
                a.status.value, iso(a.updated_at), a.reviewer, a.review_note,
                a.id, a.version,
            ),
        )
        if cur.rowcount == 0:
            raise sqlite3.IntegrityError("application version conflict")
        a.version += 1

    @staticmethod
    def _row_to_application(row: sqlite3.Row) -> Application:
        return Application(
            id=row["id"],
            candidate_id=row["candidate_id"],
            quota_id=row["quota_id"],
            status=ApplicationStatus(row["status"]),
            requested_budget=row["requested_budget"],
            submitted_at=parse_instant(row["submitted_at"]),
            updated_at=parse_instant(row["updated_at"]),
            version=row["version"],
            reviewer=row["reviewer"],
            review_note=row["review_note"],
        )

    # ------------------------------------------------------------------- plans
    def insert_plan(self, p: Plan) -> None:
        self._conn.execute(
            "INSERT INTO plans(id, quota_id, created_at, created_by, status) VALUES (?,?,?,?,?)",
            (p.id, p.quota_id, iso(p.created_at), p.created_by, p.status),
        )

    def close_plan(self, plan_id: str) -> None:
        self._conn.execute("UPDATE plans SET status='closed' WHERE id=?", (plan_id,))

    def get_plan(self, plan_id: str) -> Plan | None:
        prow = self._conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not prow:
            return None
        slots = self.list_slots(plan_id=plan_id)
        return Plan(
            id=prow["id"], quota_id=prow["quota_id"],
            created_at=parse_instant(prow["created_at"]),
            created_by=prow["created_by"], status=prow["status"], slots=slots,
        )

    def list_plans(self, quota_id: str | None = None) -> list[Plan]:
        if quota_id:
            rows = self._conn.execute(
                "SELECT id FROM plans WHERE quota_id=? ORDER BY created_at", (quota_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT id FROM plans ORDER BY created_at").fetchall()
        plans = []
        for r in rows:
            plan = self.get_plan(r["id"])
            if plan:
                plans.append(plan)
        return plans

    def latest_active_plan(self, quota_id: str) -> Plan | None:
        row = self._conn.execute(
            "SELECT id FROM plans WHERE quota_id=? AND status='active' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (quota_id,),
        ).fetchone()
        return self.get_plan(row["id"]) if row else None

    # ------------------------------------------------------------------- slots
    def insert_slot(self, s: Slot) -> None:
        self._conn.execute(
            """INSERT INTO slots(id, plan_id, quota_id, candidate_id, rank, status,
                   score, score_breakdown, explanation, held_until, confirmed_at, ticketed,
                   checked_in_at, completed_at, cancelled_at, cancel_reason,
                   responsible_party, replaced_slot_id, promoted_from_slot_id, idempotency_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s.id, s.plan_id, s.quota_id, s.candidate_id, s.rank, s.status.value,
                s.score, json.dumps(s.score_breakdown, ensure_ascii=False),
                json.dumps(s.explanation, ensure_ascii=False),
                iso(s.held_until) if s.held_until else None,
                iso(s.confirmed_at) if s.confirmed_at else None,
                1 if s.ticketed else 0,
                iso(s.checked_in_at) if s.checked_in_at else None,
                iso(s.completed_at) if s.completed_at else None,
                iso(s.cancelled_at) if s.cancelled_at else None,
                s.cancel_reason, s.responsible_party,
                s.replaced_slot_id, s.promoted_from_slot_id, s.idempotency_key,
            ),
        )

    def update_slot(self, s: Slot) -> None:
        cur = self._conn.execute(
            """UPDATE slots SET rank=?, status=?, score=?, score_breakdown=?,
                   explanation=?, held_until=?, confirmed_at=?, ticketed=?,
                   checked_in_at=?, completed_at=?, cancelled_at=?, cancel_reason=?,
                   responsible_party=?, replaced_slot_id=?, promoted_from_slot_id=?,
                   idempotency_key=?
               WHERE id=?""",
            (
                s.rank, s.status.value, s.score,
                json.dumps(s.score_breakdown, ensure_ascii=False),
                json.dumps(s.explanation, ensure_ascii=False),
                iso(s.held_until) if s.held_until else None,
                iso(s.confirmed_at) if s.confirmed_at else None,
                1 if s.ticketed else 0,
                iso(s.checked_in_at) if s.checked_in_at else None,
                iso(s.completed_at) if s.completed_at else None,
                iso(s.cancelled_at) if s.cancelled_at else None,
                s.cancel_reason, s.responsible_party,
                s.replaced_slot_id, s.promoted_from_slot_id, s.idempotency_key,
                s.id,
            ),
        )
        if cur.rowcount == 0:
            raise sqlite3.IntegrityError("slot missing")

    def get_slot(self, slot_id: str) -> Slot | None:
        row = self._conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        return self._row_to_slot(row) if row else None

    def list_slots(
        self,
        plan_id: str | None = None,
        quota_id: str | None = None,
        status: SlotStatus | None = None,
        statuses: tuple[SlotStatus, ...] | None = None,
    ) -> list[Slot]:
        sql = "SELECT * FROM slots WHERE 1=1"
        args: list[Any] = []
        if plan_id:
            sql += " AND plan_id=?"
            args.append(plan_id)
        if quota_id:
            sql += " AND quota_id=?"
            args.append(quota_id)
        if status:
            sql += " AND status=?"
            args.append(status.value)
        if statuses:
            marks = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({marks})"
            args.extend(s.value for s in statuses)
        sql += " ORDER BY rank, id"
        return [self._row_to_slot(r) for r in self._conn.execute(sql, args).fetchall()]

    @staticmethod
    def _row_to_slot(row: sqlite3.Row) -> Slot:
        def opt(value: str | None) -> datetime | None:
            return parse_instant(value) if value else None

        return Slot(
            id=row["id"], plan_id=row["plan_id"], quota_id=row["quota_id"],
            candidate_id=row["candidate_id"], rank=row["rank"],
            status=SlotStatus(row["status"]), score=row["score"],
            score_breakdown=json.loads(row["score_breakdown"]),
            explanation=json.loads(row["explanation"]),
            held_until=opt(row["held_until"]),
            confirmed_at=opt(row["confirmed_at"]),
            ticketed=bool(row["ticketed"]),
            checked_in_at=opt(row["checked_in_at"]),
            completed_at=opt(row["completed_at"]),
            cancelled_at=opt(row["cancelled_at"]),
            cancel_reason=row["cancel_reason"],
            responsible_party=row["responsible_party"],
            replaced_slot_id=row["replaced_slot_id"],
            promoted_from_slot_id=row["promoted_from_slot_id"],
            idempotency_key=row["idempotency_key"],
        )

    # ------------------------------------------------------------------ events
    def append_event(
        self, at: datetime, actor: str, event_type: str, *,
        slot_id: str | None = None, plan_id: str | None = None,
        quota_id: str | None = None, candidate_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO events(at, actor, type, slot_id, plan_id, quota_id,
                   candidate_id, payload) VALUES (?,?,?,?,?,?,?,?)""",
            (iso(at), actor, event_type, slot_id, plan_id, quota_id, candidate_id,
             json.dumps(payload or {}, ensure_ascii=False)),
        )

    def list_events(self, slot_id: str | None = None, quota_id: str | None = None,
                    event_type: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list[Any] = []
        if slot_id:
            sql += " AND slot_id=?"
            args.append(slot_id)
        if quota_id:
            sql += " AND quota_id=?"
            args.append(quota_id)
        if event_type:
            sql += " AND type=?"
            args.append(event_type)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, args).fetchall()
        return [
            {
                "id": r["id"], "at": r["at"], "actor": r["actor"], "type": r["type"],
                "slot_id": r["slot_id"], "plan_id": r["plan_id"],
                "quota_id": r["quota_id"], "candidate_id": r["candidate_id"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------- idempotency
    def idem_get(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM idempotency WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        return {
            "key": row["key"], "scope": row["scope"],
            "fingerprint": row["fingerprint"], "status": row["status"],
            "response": json.loads(row["response"]) if row["response"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def idem_put_pending(self, key: str, scope: str, fingerprint: str, at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO idempotency(key, scope, fingerprint, status, response, "
            "created_at, updated_at) VALUES (?,?,?,'pending',NULL,?,?)",
            (key, scope, fingerprint, iso(at), iso(at)),
        )

    def idem_finish(self, key: str, status: str, response: dict, at: datetime) -> None:
        self._conn.execute(
            "UPDATE idempotency SET status=?, response=?, updated_at=? WHERE key=?",
            (status, json.dumps(response, ensure_ascii=False), iso(at), key),
        )

    def idem_delete_pending(self, key: str) -> None:
        self._conn.execute(
            "DELETE FROM idempotency WHERE key=? AND status='pending'", (key,)
        )

    def idem_reset_all_pending(self) -> int:
        """进程启动恢复：清除上次崩溃残留的 in-flight 幂等记录。

        所有写操作都在单事务内提交，崩溃时不可能留下部分生效的业务写入，
        因此 pending 记录可安全清除，客户端可用同一键安全重试。
        """
        cur = self._conn.execute(
            "DELETE FROM idempotency WHERE status='pending'")
        return cur.rowcount
