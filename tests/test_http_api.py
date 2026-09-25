"""HTTP 边界端到端测试：真实线程服务器 + http.client。"""
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from service_09252_003.api import build_server
from service_09252_003.app import create_service
from service_09252_003.timeutil import FakeClock
from tests._fixtures import window


class ApiClient:
    def __init__(self, port: int) -> None:
        self.port = port

    def call(self, method: str, path: str, body=None, *, actor_id="a",
             role="admin", school="", name="", idem=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json",
                   "X-Actor-Id": actor_id, "X-Actor-Role": role,
                   "X-Actor-School": school, "X-Actor-Name": name}
        if idem:
            headers["Idempotency-Key"] = idem
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()
        data = json.loads(raw) if raw else {}
        return resp.status, dict(resp.getheaders()), data


class HttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(cls.tmp.name) / "http.db")
        cls.clock = FakeClock("2026-09-01T00:00:00Z")
        cls.service, cls.db = create_service(db_path, cls.clock)
        cls.server = build_server(cls.service, "127.0.0.1", 0,
                                  reaper_interval=None)
        cls.port = cls.server.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.api = ApiClient(cls.port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.close()
        cls.tmp.cleanup()

    def _seed(self) -> None:
        admin = dict(actor_id="admin", role="admin")
        self.api.call("POST", "/admin/quotas", {
            "id": "Q1", "host_school": "MIT", "discipline": "cs",
            "seats": 1, "budget_per_seat": 10000, "funding_source": "CSC",
            **dict(zip(("window_start", "window_end", "tz", "end_tz"),
                       ("2026-10-01T00:00", "2026-10-15T00:00",
                        "America/Los_Angeles", "America/Los_Angeles"))),
        }, **admin)
        avail = window("2026-09-25T00:00", "2026-10-31T00:00", "UTC")
        self.api.call("POST", "/admin/candidates", {
            "id": "C1", "name": "张老师", "sending_school": "THU",
            "disciplines": ["cs"], "visa_ready": True,
            "availability_windows": [avail],
        }, **admin)
        self.api.call("POST", "/admin/candidates", {
            "id": "C2", "name": "李老师", "sending_school": "THU",
            "disciplines": ["cs"], "visa_ready": True,
            "availability_windows": [avail],
        }, **admin)

    def test_full_flow_over_http_with_partial_confirm_and_isolation(self) -> None:
        self._seed()
        # 申请（幂等键）
        s, h, a = self.api.call("POST", "/applications",
                                {"candidate_id": "C1", "quota_id": "Q1",
                                 "requested_budget": 8000},
                                actor_id="C1", role="applicant", idem="app1")
        self.assertEqual(s, 200)
        app1 = a["id"]
        self.api.call("POST", "/applications",
                      {"candidate_id": "C2", "quota_id": "Q1",
                       "requested_budget": 9000},
                      actor_id="C2", role="applicant", idem="app2")
        # 同键重试 -> 回放
        s, h, a2 = self.api.call("POST", "/applications",
                                 {"candidate_id": "C1", "quota_id": "Q1",
                                  "requested_budget": 8000},
                                 actor_id="C1", role="applicant", idem="app1")
        self.assertEqual(h.get("X-Idempotent-Replayed"), "true")
        self.assertEqual(a2["id"], app1)

        # 外校国际处评审被拒
        s, _, _ = self.api.call("POST", f"/applications/{app1}/review",
                                {"decision": "approve"},
                                actor_id="io1", role="io", school="Berkeley")
        self.assertEqual(s, 403)
        # MIT 国际处评审通过
        for app in (app1,):
            s, _, rev = self.api.call("POST", f"/applications/{app}/review",
                                      {"decision": "approve"},
                                      actor_id="io-mit", role="io",
                                      school="MIT", name="Prof-Wang")
            self.assertEqual(s, 200, rev)
        # C2 的申请 id 通过列表获取
        s, _, listing = self.api.call("GET", "/applications?quota_id=Q1",
                                      actor_id="io-mit", role="io",
                                      school="MIT")
        c2_app = next(x["id"] for x in listing["applications"]
                      if x["candidate_id"] == "C2")
        self.api.call("POST", f"/applications/{c2_app}/review",
                      {"decision": "approve"},
                      actor_id="io-mit", role="io", school="MIT")

        # 匹配报告含解释
        s, _, report = self.api.call("GET", "/quotas/Q1/match-report",
                                     actor_id="io-mit", role="io", school="MIT")
        self.assertEqual(s, 200)
        self.assertTrue(all("explanation" in r for r in report["results"]))

        # 生成方案、占位、确认（出票）
        s, _, plan = self.api.call("POST", "/quotas/Q1/plans", {},
                                   actor_id="io-mit", role="io", school="MIT")
        self.assertEqual(s, 200)
        s, _, locked = self.api.call("POST", "/quotas/Q1/lock",
                                     {"hold_ttl_seconds": 3600},
                                     actor_id="io-mit", role="io",
                                     school="MIT", idem="lock1")
        self.assertEqual(s, 200)
        sid = locked["held"][0]["id"]
        s, _, conf = self.api.call("POST", f"/slots/{sid}/confirm",
                                   {"ticketed": True},
                                   actor_id="io-mit", role="io",
                                   school="MIT", idem="conf1")
        self.assertTrue(conf["ticketed"])

        # 签证拒绝：已出票不挪动，仅记责任
        s, _, vr = self.api.call("POST", f"/slots/{sid}/visa-rejection",
                                 {"note": "214b"},
                                 actor_id="io-mit", role="io", school="MIT")
        self.assertEqual(s, 200)
        self.assertTrue(vr["manual_action_required"])

        # 报到 -> 结项
        s, _, _ = self.api.call("POST", f"/slots/{sid}/check-in", {},
                                actor_id="io-mit", role="io", school="MIT")
        self.assertEqual(s, 200)
        s, _, done = self.api.call("POST", f"/slots/{sid}/complete",
                                   {"note": "完成访学"},
                                   actor_id="io-mit", role="io", school="MIT")
        self.assertEqual(done["status"], "completed")

        # 申请人只能看到自己的槽位
        s, _, plan_view = self.api.call("GET", f"/plans/{plan['id']}",
                                        actor_id="C1", role="applicant")
        self.assertEqual(s, 200)
        self.assertTrue(all(sl["candidate_id"] == "C1"
                            for sl in plan_view["slots"]))

        # 鉴权缺失 401
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/health")
        r = conn.getresponse(); r.read(); conn.close()
        self.assertEqual(r.status, 200)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/quotas")
        r = conn.getresponse(); body = json.loads(r.read().decode()); conn.close()
        self.assertEqual(r.status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_restart_releases_due_hold(self) -> None:
        # 独立文件库验证“重启后继续执行超时释放”
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "r.db")
            svc, db = create_service(path, self.clock)
            server = build_server(svc, "127.0.0.1", 0, reaper_interval=None)
            port = server.server_address[1]
            import threading
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            api = ApiClient(port)
            admin = dict(actor_id="admin", role="admin")
            w = window("2026-09-25T00:00", "2026-10-31T00:00", "UTC")
            api.call("POST", "/admin/quotas", {
                "id": "QR", "host_school": "MIT", "discipline": "cs",
                "seats": 1, "budget_per_seat": 10000,
                "funding_source": "CSC",
                "window_start": w["start"], "window_end": w["end"],
                "tz": "UTC", "end_tz": "UTC"}, **admin)
            api.call("POST", "/admin/candidates", {
                "id": "R1", "name": "r1", "sending_school": "THU",
                "disciplines": ["cs"], "visa_ready": True,
                "availability_windows": [w]}, **admin)
            api.call("POST", "/applications",
                     {"candidate_id": "R1", "quota_id": "QR",
                      "requested_budget": 1},
                     actor_id="R1", role="applicant", idem="a")
            app = svc.db.list_applications(quota_id="QR")[0]
            api.call("POST", f"/applications/{app.id}/review",
                     {"decision": "approve"},
                     actor_id="io", role="io", school="MIT")
            api.call("POST", "/quotas/QR/plans", {},
                     actor_id="io", role="io", school="MIT")
            s, _, locked = api.call("POST", "/quotas/QR/lock",
                                    {"hold_ttl_seconds": 60},
                                    actor_id="io", role="io", school="MIT",
                                    idem="l")
            self.assertEqual(s, 200)
            server.shutdown(); db.close()

            # “重启”：新服务指向同一文件；build_server 启动即同步执行一轮回收
            from service_09252_003.timeutil import timedelta
            self.clock.advance(timedelta(seconds=61))
            svc2, db2 = create_service(path, self.clock)
            server2 = build_server(svc2, "127.0.0.1", 0, reaper_interval=None)
            port2 = server2.server_address[1]
            api2 = ApiClient(port2)
            t2 = threading.Thread(target=server2.serve_forever, daemon=True)
            t2.start()
            s, _, roster = api2.call("GET", "/quotas/QR/roster",
                                    actor_id="io", role="io", school="MIT")
            self.assertEqual(s, 200)
            # 原占位已过期释放，没有活跃占用
            self.assertEqual(roster["active_seats"], 0)
            server2.shutdown()
            server2.server_close()
            db2.close()


if __name__ == "__main__":
    unittest.main()
