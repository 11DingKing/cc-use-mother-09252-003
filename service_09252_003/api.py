"""HTTP JSON 接口（仅标准库实现）。

路由、认证（``X-Token``）、幂等键（``Idempotency-Key``）与统一错误响应。
所有时间字段在响应中同时给出 UTC 微秒整数与 RFC3339 可读形式。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import AuthError, DomainError, ValidationError
from .service import ExchangeService
from .timeutil import TimeParseError, format_instant, parse_instant


def _maybe_instant(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return parse_instant(value)
    except TimeParseError as exc:
        raise ValidationError(f"时间参数无效: {exc}") from exc

INSTANT_FIELDS = (
    "created_at", "decided_at", "hold_expires_at", "locked_at", "ticketed_at",
    "checkin_at", "closeout_at", "updated_at", "at", "start_us", "end_us",
)


def _enrich(value: Any) -> Any:
    """递归地为时间字段附加 ``*_iso`` 可读形式。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            out[key] = _enrich(val)
            if key in INSTANT_FIELDS and isinstance(val, int):
                out[key.removesuffix("_us") + "_iso"] = format_instant(val)
        return out
    if isinstance(value, list):
        return [_enrich(v) for v in value]
    return value


class _Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str,
            handler: Callable[..., Any]) -> None:
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        self._routes.append((method, re.compile(f"^{regex}$"), handler))

    def match(self, method: str, path: str
              ) -> tuple[Callable[..., Any], dict[str, str]] | None:
        for m, regex, handler in self._routes:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                return handler, match.groupdict()
        return None


def build_router(service: ExchangeService) -> _Router:
    router = _Router()

    def actor_of(ctx: dict[str, Any]) -> dict[str, Any]:
        return ctx["actor"]

    # ---- 无认证 ----
    router.add("GET", "/health", lambda ctx: {
        "status": "ok", "now": format_instant(service.clock.now_us())})

    # ---- 用户 ----
    router.add("POST", "/users", lambda ctx: {
        "user": service.add_user(actor_of(ctx), ctx["body"])})
    router.add("GET", "/users", lambda ctx: {
        "users": service.list_users(actor_of(ctx))})

    # ---- 候选人 ----
    router.add("POST", "/candidates", lambda ctx: service.create_candidate(
        actor_of(ctx), ctx["body"], ctx["idem"]))
    router.add("GET", "/candidates", lambda ctx: service.list_candidates(actor_of(ctx)))
    router.add("GET", "/candidates/{cid}", lambda ctx: service.get_candidate(
        actor_of(ctx), ctx["cid"]))
    router.add("POST", "/candidates/{cid}/visa-rejected", lambda ctx:
               service.visa_rejected(actor_of(ctx), ctx["cid"],
                                     str(ctx["body"].get("reason") or "")))
    router.add("POST", "/candidates/{cid}/funding-frozen", lambda ctx:
               service.funding_frozen(actor_of(ctx), ctx["cid"],
                                      str(ctx["body"].get("reason") or "")))

    # ---- 名额 ----
    router.add("POST", "/quotas", lambda ctx: service.create_quota(
        actor_of(ctx), ctx["body"], ctx["idem"]))
    router.add("GET", "/quotas", lambda ctx: service.list_quotas(actor_of(ctx)))
    router.add("GET", "/quotas/{qid}", lambda ctx: service.get_quota(
        actor_of(ctx), ctx["qid"]))
    router.add("POST", "/quotas/{qid}/close", lambda ctx: service.close_quota(
        actor_of(ctx), ctx["qid"], str(ctx["body"].get("reason") or "")))
    router.add("GET", "/quotas/{qid}/proposal", lambda ctx: service.proposal(
        actor_of(ctx), ctx["qid"]))
    router.add("POST", "/quotas/{qid}/batch-lock", lambda ctx: service.batch_lock(
        actor_of(ctx), ctx["qid"], ctx["body"].get("ttl_seconds"), ctx["idem"]))

    # ---- 申请 ----
    router.add("POST", "/applications", lambda ctx: service.apply(
        actor_of(ctx), ctx["body"], ctx["idem"]))
    router.add("GET", "/applications", lambda ctx: service.list_applications(
        actor_of(ctx), quota_id=ctx["query"].get("quota_id"),
        status=ctx["query"].get("status"),
        candidate_id=ctx["query"].get("candidate_id")))
    router.add("GET", "/applications/{aid}", lambda ctx: service.get_application(
        actor_of(ctx), ctx["aid"]))
    router.add("POST", "/applications/{aid}/review", lambda ctx: service.review(
        actor_of(ctx), ctx["aid"], str(ctx["body"].get("decision") or ""),
        str(ctx["body"].get("reason") or ""), ctx["idem"]))
    router.add("POST", "/applications/{aid}/ticket", lambda ctx: service.ticket(
        actor_of(ctx), ctx["aid"], ctx["idem"]))
    router.add("POST", "/applications/{aid}/checkin", lambda ctx: service.checkin(
        actor_of(ctx), ctx["aid"], _maybe_instant(ctx["body"].get("at")),
        ctx["idem"]))
    router.add("POST", "/applications/{aid}/closeout", lambda ctx: service.closeout(
        actor_of(ctx), ctx["aid"], str(ctx["body"].get("report") or ""), ctx["idem"]))
    router.add("POST", "/applications/{aid}/cancel", lambda ctx: service.cancel(
        actor_of(ctx), ctx["aid"], str(ctx["body"].get("reason") or ""),
        str(ctx["body"].get("responsibility") or "coordinator"), ctx["idem"]))

    # ---- 席位 / 事件 ----
    router.add("GET", "/slots/{sid}", lambda ctx: service.get_slot(
        actor_of(ctx), ctx["sid"]))
    router.add("GET", "/events", lambda ctx: service.list_events(
        actor_of(ctx), entity_type=ctx["query"].get("entity_type"),
        entity_id=ctx["query"].get("entity_id")))
    return router


def make_handler(service: ExchangeService) -> type[BaseHTTPRequestHandler]:
    router = build_router(service)
    public_paths = {("GET", "/health")}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FacultyExchange/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默
            return

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                path, _, raw_query = self.path.partition("?")
                query = dict(
                    pair.split("=", 1) for pair in raw_query.split("&") if "=" in pair)
                body: dict[str, Any] = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    raw = self.rfile.read(length)
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        raise ValidationError(f"请求体不是合法 JSON: {exc}") from exc
                    if not isinstance(parsed, dict):
                        raise ValidationError("请求体必须是 JSON 对象")
                    body = parsed
                matched = router.match(method, path)
                if matched is None:
                    raise DomainError("接口不存在", code="not_found", status=404)
                handler, path_params = matched
                if (method, path) in public_paths:
                    actor: dict[str, Any] = {"username": "anonymous", "role": "none"}
                else:
                    actor = service.authenticate(self.headers.get("X-Token"))
                ctx = {
                    "actor": actor,
                    "body": body,
                    "query": query,
                    "idem": self.headers.get("Idempotency-Key"),
                    **path_params,
                }
                result = handler(ctx)
                self._send(200, _enrich(result))
            except DomainError as exc:
                self._send(exc.status, {"error": exc.to_body()})
            except BrokenPipeError:
                raise
            except Exception as exc:  # pragma: no cover - 兜底
                self._send(500, {"error": {"code": "internal_error",
                                           "message": f"{type(exc).__name__}: {exc}"}})

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def make_server(service: ExchangeService, host: str = "127.0.0.1",
                port: int = 8080) -> ThreadingHTTPServer:
    """构建 HTTP 服务（调用方负责 serve_forever / shutdown）。"""
    return ThreadingHTTPServer((host, port), make_handler(service))
