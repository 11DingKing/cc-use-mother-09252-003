"""HTTP 接口端到端测试：认证、幂等键、错误格式与完整流程。"""
import http.client
import json
import threading
import unittest

from service_09252_003.api import make_server

from helpers import cand_payload, make_service, quota_payload


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service, cls.clock = make_service()
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def call(self, method: str, path: str, body: dict | None = None,
             token: str | None = "tok-coord",
             idem: str | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Token"] = token
        if idem:
            headers["Idempotency-Key"] = idem
        conn.request(method, path,
                     body=json.dumps(body) if body is not None else None,
                     headers=headers)
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, payload

    def test_health_is_public(self) -> None:
        status, body = self.call("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_auth_required(self) -> None:
        status, body = self.call("GET", "/candidates", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        status, _ = self.call("GET", "/candidates", token="bad-token")
        self.assertEqual(status, 401)

    def test_unknown_route_and_validation_error_shape(self) -> None:
        status, body = self.call("GET", "/no-such-path")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        status, body = self.call("POST", "/candidates", {"name": "缺字段"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_full_flow_over_http_with_idempotency(self) -> None:
        status, cand_body = self.call("POST", "/candidates", cand_payload(),
                                      idem="http-cand-1")
        self.assertEqual(status, 200)
        candidate_id = cand_body["candidate"]["id"]

        status, quota_body = self.call("POST", "/quotas", quota_payload(),
                                       idem="http-quota-1")
        self.assertEqual(status, 200)
        quota_id = quota_body["quota"]["id"]
        # 时间字段附带可读 ISO 形式
        self.assertIn("created_at_iso", quota_body["quota"])

        apply_payload = {"candidate_id": candidate_id, "quota_id": quota_id}
        status, applied = self.call("POST", "/applications", apply_payload,
                                    idem="http-apply-1")
        self.assertEqual(status, 200)
        app_id = applied["application"]["id"]
        self.assertEqual(applied["slot"]["state"], "held")

        # 相同幂等键重放：同一响应，无副作用
        status, replay = self.call("POST", "/applications", apply_payload,
                                   idem="http-apply-1")
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["application"]["id"], app_id)
        # 同键不同请求体 → 409
        status, conflict = self.call(
            "POST", "/applications",
            {"candidate_id": candidate_id, "quota_id": quota_id, "note": "变了"},
            idem="http-apply-1")
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

        status, _ = self.call("POST", f"/applications/{app_id}/review",
                              {"decision": "approve"})
        self.assertEqual(status, 200)
        status, locked = self.call("POST", f"/quotas/{quota_id}/batch-lock", {})
        self.assertEqual(status, 200)
        self.assertEqual(locked["locked_count"], 1)
        status, ticketed = self.call("POST", f"/applications/{app_id}/ticket", {})
        self.assertEqual(status, 200)
        self.assertEqual(ticketed["slot"]["state"], "ticketed")
        status, checked = self.call("POST", f"/applications/{app_id}/checkin",
                                    {"at": "2027-03-05T10:00:00Z"})
        self.assertEqual(status, 200)
        self.assertEqual(checked["slot"]["state"], "checked_in")
        status, closed = self.call("POST", f"/applications/{app_id}/closeout",
                                   {"report": "完成"})
        self.assertEqual(status, 200)
        self.assertEqual(closed["slot"]["state"], "completed")

        # 事件（责任记录）可查询
        status, events = self.call(
            "GET", f"/events?entity_type=application&entity_id={app_id}")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["type"] == "application_decided"
                            for e in events["events"]))

    def test_permission_isolation_over_http(self) -> None:
        status, cand_body = self.call("POST", "/candidates",
                                      cand_payload(name="隔离对象"))
        other_candidate = cand_body["candidate"]["id"]
        # 教师角色无权创建名额
        self.service.add_user(
            self.service.authenticate("tok-coord"),
            {"token": "tok-t1", "username": "t1", "role": "teacher",
             "candidate_id": other_candidate})
        status, body = self.call("POST", "/quotas", quota_payload(),
                                 token="tok-t1")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
