"""HTTP 接口边界（仅依赖标准库）。

鉴权：每个请求通过请求头传递身份，服务端不做用户体系，便于对接与测试::

    X-Actor-Id:    身份标识（申请人即候选人 id）
    X-Actor-Role:  applicant / io / finance / admin / system
    X-Actor-School: 院校隔离范围（io 只能操作本校名额）
    X-Actor-Name:  展示名（可选）

幂等：写请求可带 ``Idempotency-Key`` 头；回放时响应头带
``X-Idempotent-Replayed: true``。

并发：所有写命令在服务层进入串行化事务，HTTP 层可安全使用线程服务器。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import AuthError, DomainError, PermissionError
from .models import Role
from .services import Actor, VisitExchangeService

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replayed"


def actor_from_headers(headers) -> Actor:
    actor_id = headers.get("X-Actor-Id")
    role_value = headers.get("X-Actor-Role")
    if not actor_id or not role_value:
        raise AuthError("缺少身份头 X-Actor-Id / X-Actor-Role")
    try:
        role = Role(role_value)
    except ValueError as exc:
        raise AuthError(f"未知角色 {role_value!r}") from exc
    if role == Role.SYSTEM:
        raise PermissionError("system 主体不可用于外部请求")
    return Actor(
        id=actor_id, role=role,
        school=headers.get("X-Actor-School", ""),
        name=headers.get("X-Actor-Name", ""),
    )


# 路由表：(方法, 路径模式) -> (处理函数名, 是否需要幂等键支持)
# 路径段以 {name} 表示参数。
def _match(pattern: str, path: str) -> dict[str, str] | None:
    pp = pattern.strip("/").split("/")
    qp = path.strip("/").split("/")
    if len(pp) != len(qp):
        return None
    params: dict[str, str] = {}
    for seg, val in zip(pp, qp):
        if seg.startswith("{") and seg.endswith("}"):
            params[seg[1:-1]] = val
        elif seg != val:
            return None
    return params


ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/admin/candidates"): "h_register_candidate",
    ("POST", "/admin/quotas"): "h_register_quota",
    ("GET", "/quotas"): "h_list_quotas",
    ("GET", "/candidates"): "h_list_candidates",

    ("POST", "/applications"): "h_apply",
    ("GET", "/applications"): "h_list_applications",
    ("GET", "/applications/{id}"): "h_get_application",
    ("POST", "/applications/{id}/withdraw"): "h_withdraw",
    ("POST", "/applications/{id}/review"): "h_review",

    ("GET", "/quotas/{id}/match-report"): "h_match_report",
    ("POST", "/quotas/{id}/plans"): "h_generate_plan",
    ("GET", "/quotas/{id}/roster"): "h_roster",
    ("POST", "/quotas/{id}/lock"): "h_lock",
    ("POST", "/quotas/{id}/backfill"): "h_backfill",
    ("POST", "/quotas/{id}/funding"): "h_funding",
    ("GET", "/plans/{id}"): "h_get_plan",

    ("POST", "/confirmations/batch"): "h_confirm_batch",
    ("GET", "/slots/{id}"): "h_get_slot",
    ("POST", "/slots/{id}/confirm"): "h_confirm",
    ("POST", "/slots/{id}/cancel"): "h_cancel",
    ("POST", "/slots/{id}/visa-rejection"): "h_visa_rejection",
    ("POST", "/slots/{id}/check-in"): "h_check_in",
    ("POST", "/slots/{id}/complete"): "h_complete",

    ("GET", "/events"): "h_events",
    ("POST", "/system/expire-holds"): "h_expire",
    ("GET", "/health"): "h_health",
}


class ExchangeHandler(BaseHTTPRequestHandler):
    server_version = "VisitExchange/1.0"

    # ------------------------------------------------------------ http plumbing
    def _send(self, status: int, payload: Any, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        if getattr(self.server, "access_log", False):
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            for (m, pattern), handler_name in ROUTES.items():
                if m != method:
                    continue
                params = _match(pattern, path)
                if params is None:
                    continue
                handler: Callable[..., Any] = getattr(self, handler_name)
                handler(params, query)
                return
            self._send(404, {"error": "not_found", "message": f"无此路由: {method} {path}"})
        except DomainError as exc:
            self._send(exc.http_status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 边界兜底，防止连接挂死
            self._send(500, {"error": "internal_error", "message": str(exc)})

    # -------------------------------------------------------------- properties
    @property
    def service(self) -> VisitExchangeService:
        return self.server.service  # type: ignore[attr-defined]

    def _actor(self) -> Actor:
        return actor_from_headers(self.headers)

    def _idem_key(self) -> str | None:
        return self.headers.get(IDEMPOTENCY_HEADER)

    def _result(self, result) -> None:
        headers = {REPLAY_HEADER: "true"} if result.replayed else None
        self._send(200, result.data, headers)

    # ----------------------------------------------------------------- catalog
    def h_health(self, params: dict, query: dict) -> None:
        self._send(200, {"status": "ok"})

    def h_register_candidate(self, params: dict, query: dict) -> None:
        actor = self._actor()
        self._send(200, self.service.register_candidate(actor, self._read_json()))

    def h_register_quota(self, params: dict, query: dict) -> None:
        actor = self._actor()
        self._send(200, self.service.register_quota(actor, self._read_json()))

    def h_list_candidates(self, params: dict, query: dict) -> None:
        actor = self._actor()
        if actor.role not in (Role.ADMIN, Role.INTERNATIONAL_OFFICE):
            raise PermissionError("无权浏览候选人名册")
        self._send(200, {"candidates": [c.to_dict()
                                        for c in self.service.db.list_candidates()]})

    def h_list_quotas(self, params: dict, query: dict) -> None:
        actor = self._actor()
        quotas = self.service.db.list_quotas()
        if actor.role == Role.INTERNATIONAL_OFFICE:
            quotas = [q for q in quotas if q.host_school == actor.school]
        elif actor.role == Role.APPLICANT:
            raise PermissionError("申请人无权浏览全部名额")
        self._send(200, {"quotas": [q.to_dict() for q in quotas]})

    # ------------------------------------------------------------ applications
    def h_apply(self, params: dict, query: dict) -> None:
        result = self.service.apply(self._actor(), self._read_json(), self._idem_key())
        self._result(result)

    def h_list_applications(self, params: dict, query: dict) -> None:
        actor = self._actor()
        quota_id = query.get("quota_id", [None])[0]
        self._send(200, self.service.list_applications(actor, quota_id))

    def h_get_application(self, params: dict, query: dict) -> None:
        self._send(200, self.service.get_application(self._actor(), params["id"]))

    def h_withdraw(self, params: dict, query: dict) -> None:
        self._send(200, self.service.withdraw(self._actor(), params["id"]))

    def h_review(self, params: dict, query: dict) -> None:
        self._send(200, self.service.review(
            self._actor(), params["id"], self._read_json()))

    # ------------------------------------------------------------------- plans
    def h_match_report(self, params: dict, query: dict) -> None:
        self._send(200, self.service.match_report(self._actor(), params["id"]))

    def h_generate_plan(self, params: dict, query: dict) -> None:
        actor = self._actor()
        self._send(200, self.service.generate_plan(actor, params["id"]))

    def h_get_plan(self, params: dict, query: dict) -> None:
        self._send(200, self.service.get_plan(self._actor(), params["id"]))

    def h_roster(self, params: dict, query: dict) -> None:
        self._send(200, self.service.roster(self._actor(), params["id"]))

    # -------------------------------------------------------------------- lock
    def h_lock(self, params: dict, query: dict) -> None:
        result = self.service.lock_slots(
            self._actor(), params["id"], self._read_json(), self._idem_key())
        self._result(result)

    # -------------------------------------------------------------- confirm etc
    def h_confirm(self, params: dict, query: dict) -> None:
        result = self.service.confirm_slot(
            self._actor(), params["id"], self._read_json(), self._idem_key())
        self._result(result)

    def h_confirm_batch(self, params: dict, query: dict) -> None:
        result = self.service.confirm_batch(
            self._actor(), self._read_json(), self._idem_key())
        self._result(result)

    def h_cancel(self, params: dict, query: dict) -> None:
        self._send(200, self.service.cancel_slot(
            self._actor(), params["id"], self._read_json()))

    def h_visa_rejection(self, params: dict, query: dict) -> None:
        self._send(200, self.service.report_visa_rejection(
            self._actor(), params["id"], self._read_json()))

    def h_backfill(self, params: dict, query: dict) -> None:
        actor = self._actor()
        self._send(200, self.service.backfill(actor, params["id"]))

    def h_funding(self, params: dict, query: dict) -> None:
        result = self.service.set_funding_frozen(
            self._actor(), params["id"], self._read_json(), self._idem_key())
        self._result(result)

    def h_check_in(self, params: dict, query: dict) -> None:
        self._send(200, self.service.check_in(
            self._actor(), params["id"], self._read_json()))

    def h_complete(self, params: dict, query: dict) -> None:
        self._send(200, self.service.complete(
            self._actor(), params["id"], self._read_json()))

    def h_get_slot(self, params: dict, query: dict) -> None:
        self._send(200, self.service.get_slot(self._actor(), params["id"]))

    # ------------------------------------------------------------------ events
    def h_events(self, params: dict, query: dict) -> None:
        actor = self._actor()
        quota_id = query.get("quota_id", [None])[0]
        slot_id = query.get("slot_id", [None])[0]
        self._send(200, self.service.list_events(actor, quota_id, slot_id))

    def h_expire(self, params: dict, query: dict) -> None:
        actor = self._actor()
        if actor.role != Role.ADMIN:
            raise PermissionError("仅管理员可手动驱动超时回收")
        self._send(200, self.service.expire_holds())


class HoldReaper(threading.Thread):
    """后台定时执行占位超时释放；守护线程，服务停止即退出。

    服务重启后的首轮回收由 :func:`build_server` 同步执行，不依赖定时器，
    保证崩溃期间到期的占位在恢复时立即释放并触发替补。
    """

    def __init__(self, service: VisitExchangeService, interval_seconds: float) -> None:
        super().__init__(daemon=True, name="hold-reaper")
        self.service = service
        self.interval = interval_seconds
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            self._safe_reap()

    def _safe_reap(self) -> None:
        try:
            self.service.expire_holds()
        except Exception:  # noqa: BLE001 - 后台线程不得因单次异常死亡
            pass


def build_server(
    service: VisitExchangeService,
    host: str = "127.0.0.1",
    port: int = 0,
    reaper_interval: float | None = 5.0,
    startup_reap: bool = True,
) -> ThreadingHTTPServer:
    # 重启恢复：在开始接客前同步跑一轮超时释放，崩溃期间到期的占位立即处理
    if startup_reap:
        service.expire_holds()
    # 清除上次崩溃可能残留的 pending 幂等记录（业务写入均为单事务原子提交）
    with service.db.transaction():
        service.db.idem_reset_all_pending()
    server = ThreadingHTTPServer((host, port), ExchangeHandler)
    server.service = service  # type: ignore[attr-defined]
    server.access_log = False  # type: ignore[attr-defined]
    if reaper_interval is not None:
        reaper = HoldReaper(service, interval_seconds=reaper_interval)
        server.reaper = reaper  # type: ignore[attr-defined]
        reaper.start()
    return server
