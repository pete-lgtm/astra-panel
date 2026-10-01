import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest import mock

from app import create_app
from agent import health_agent
import manage
import protocol
import store


HEALTH = {"schema": 1, "version": "0.2.44", "uptime_s": 86400,
          "temperature_c": 51.4, "memory_used_pct": 30, "disk_used_pct": 19,
          "load_1m": .24, "panel_service": "active"}


class PortalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1790800000
        self.panel = "astra-test0001"
        self.secret = "ab" * 32
        self.data = str(Path(self.temp.name) / "data")
        self.config = {"MODE": "test", "DATA_DIR": self.data, "CLOCK": lambda: self.now, "TESTING": True}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        with store.connect(self.data) as db:
            db.execute("INSERT INTO panels(id,site,name,secret,enrolled_at) VALUES(?,?,?,?,?)", (self.panel, "Test home", "Entry", self.secret, self.now))

    def signed(self, payload=None, stamp=None, nonce=None, panel=None, secret=None):
        body = protocol.encode(HEALTH if payload is None else payload)
        stamp = str(self.now if stamp is None else stamp)
        nonce = nonce or secrets.token_hex(16)
        panel = panel or self.panel
        return body, {"Content-Type": "application/json", "X-Astra-Panel": panel,
                      "X-Astra-Time": stamp, "X-Astra-Nonce": nonce,
                      "X-Astra-Signature": protocol.signature(secret or self.secret, panel, stamp, nonce, body)}

    def post(self, **options):
        body, headers = self.signed(**options)
        return self.client.post("/api/v1/heartbeat", data=body, headers=headers)

    def test_accepted_health_survives_restart_and_never_returns_secret(self):
        self.assertEqual(self.post().status_code, 204)
        client = create_app(self.config).test_client()
        reply = client.get("/api/fleet")
        self.assertNotIn(self.secret, reply.text)
        p = reply.json["panels"][0]
        self.assertEqual(p["health"], HEALTH)
        self.assertEqual(p["status"], "online")
        self.assertEqual(p["support"], "not_configured")
        self.assertFalse(reply.json["capabilities"]["alarm_control"])

    def test_tampered_body_is_rejected(self):
        body, headers = self.signed()
        self.assertEqual(self.client.post("/api/v1/heartbeat", data=body+b" ", headers=headers).status_code, 403)

    def test_unknown_identity_and_wrong_key_are_rejected(self):
        self.assertEqual(self.post(panel="astra-unknown").status_code, 403)
        self.assertEqual(self.post(secret="cd"*32).status_code, 403)

    def test_replayed_envelope_is_rejected_after_restart(self):
        body, headers = self.signed()
        self.assertEqual(self.client.post("/api/v1/heartbeat", data=body, headers=headers).status_code, 204)
        self.now += 20
        client = create_app(self.config).test_client()
        self.assertEqual(client.post("/api/v1/heartbeat", data=body, headers=headers).status_code, 409)

    def test_stale_and_future_timestamps_are_rejected(self):
        self.assertEqual(self.post(stamp=self.now-121).status_code, 403)
        self.assertEqual(self.post(stamp=self.now+121).status_code, 403)

    def test_new_nonce_is_accepted_but_fast_sender_is_limited(self):
        self.assertEqual(self.post().status_code, 204)
        self.assertEqual(self.post().status_code, 429)
        self.now += 60
        self.assertEqual(self.post().status_code, 204)
        self.assertEqual(len(self.client.get("/api/activity").json["events"]), 1)

    def test_old_signed_measurement_cannot_replace_newer_one(self):
        old = self.now
        self.assertEqual(self.post().status_code, 204)
        self.now += 60
        self.assertEqual(self.post().status_code, 204)
        self.now += 20
        self.assertEqual(self.post(stamp=old).status_code, 409)

    def test_revocation_is_immediate(self):
        self.assertEqual(self.post().status_code, 204)
        with store.connect(self.data) as db:
            db.execute("UPDATE panels SET revoked_at=?,secret='' WHERE id=?", (self.now, self.panel))
        self.now += 60
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.client.get("/api/fleet").json["panels"][0]["status"], "revoked")

    def test_privacy_allowlist_rejects_credentials_and_extra_fields(self):
        for key in ("api_key", "pin", "camera_url", "alarm_state", "command"):
            with self.subTest(key=key):
                self.assertEqual(self.post(payload={**HEALTH, key: "must never be stored"}).status_code, 400)
        with store.connect(self.data) as db:
            self.assertIsNone(db.execute("SELECT health FROM panels").fetchone()[0])

    def test_bad_types_ranges_and_missing_fields_are_rejected(self):
        for key, value in (("temperature_c", 151), ("uptime_s", -1), ("memory_used_pct", True), ("panel_service", []), ("version", "<script>"), ("schema", True)):
            with self.subTest(key=key):
                self.assertEqual(self.post(payload={**HEALTH, key:value}).status_code, 400)
        bad = dict(HEALTH)
        del bad["disk_used_pct"]
        self.assertEqual(self.post(payload=bad).status_code, 400)

    def test_missing_os_measurements_are_explicitly_unknown(self):
        health = {**HEALTH, "temperature_c": None, "memory_used_pct": None, "load_1m": None}
        self.assertEqual(self.post(payload=health).status_code, 204)
        self.assertIsNone(self.client.get("/api/fleet").json["panels"][0]["health"]["temperature_c"])

    def test_pending_and_offline_are_distinct_and_use_server_receipt_time(self):
        self.assertEqual(self.client.get("/api/fleet").json["panels"][0]["status"], "pending")
        self.assertEqual(self.post(stamp=self.now+100).status_code, 204)
        p = self.client.get("/api/fleet").json["panels"][0]
        self.assertEqual(p["last_seen"], self.now)
        self.now += 181
        p = self.client.get("/api/fleet").json["panels"][0]
        self.assertEqual(p["status"], "offline")
        self.assertIn("stopped checking in", p["issues"][0])

    def test_large_requests_are_rejected(self):
        self.assertEqual(self.client.post("/api/v1/heartbeat", data=b"x"*4097, content_type="application/json").status_code, 413)

    def test_malformed_json_after_valid_signature_is_rejected(self):
        body, headers = self.signed()
        body = b'{"broken"'
        headers["X-Astra-Signature"] = protocol.signature(self.secret, self.panel, headers["X-Astra-Time"], headers["X-Astra-Nonce"], body)
        self.assertEqual(self.client.post("/api/v1/heartbeat", data=body, headers=headers).status_code, 400)

    def test_production_requires_allowlist(self):
        with self.assertRaises(RuntimeError):
            create_app({**self.config, "MODE":"production", "OPERATORS_FILE":""})

    def production_client(self):
        path = Path(self.temp.name) / "operators.txt"
        path.write_text("operator@example.test\n")
        return create_app({**self.config, "MODE":"production", "OPERATORS_FILE":str(path)}).test_client()

    def test_browser_routes_require_allowed_proxy_identity(self):
        client = self.production_client()
        for url in ("/", "/api/fleet", "/api/activity", "/assets/portal.js"):
            self.assertEqual(client.get(url).status_code, 403)
            self.assertEqual(client.get(url, headers={"X-Forwarded-Email":"stranger@example.test"}).status_code, 403)
            with client.get(url, headers={"X-Forwarded-Email":"operator@example.test"}) as response:
                self.assertEqual(response.status_code, 200)

    def test_device_key_cannot_read_fleet_or_actuate_alarm(self):
        client = self.production_client()
        _, headers = self.signed()
        self.assertEqual(client.get("/api/fleet", headers=headers).status_code, 403)
        for path in ("/api/arm", "/api/disarm", "/api/command", "/api/reboot", "/api/update", "/api/support"):
            self.assertEqual(self.client.post(path).status_code, 404)
        self.assertEqual(self.client.post("/api/fleet").status_code, 405)

    def test_preview_rejects_lan_clients_and_real_telemetry(self):
        client = create_app({**self.config, "MODE":"preview"}).test_client()
        self.assertEqual(client.get("/", environ_overrides={"REMOTE_ADDR":"192.168.0.40"}).status_code, 403)
        self.assertEqual(client.get("/", headers={"Host":"attacker.example"}).status_code, 403)
        body, headers = self.signed()
        self.assertEqual(client.post("/api/v1/heartbeat", data=body, headers=headers).status_code, 403)
        self.assertTrue(client.get("/api/fleet").json["demo"])

    def test_database_permission_guard(self):
        path = Path(self.data)/"fleet.sqlite3"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            store.connect(self.data)

    def test_security_headers_and_traversal(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        response.close()
        self.assertEqual(self.client.get("/assets/../app.py").status_code, 404)

    def test_enrollment_cli_generates_unique_credentials_without_printing_them(self):
        output = Path(self.temp.name)/"device.json"
        args = ["manage.py", "--data-dir", self.data, "enroll", "--site", "Other home", "--name", "Hall panel", "--origin", "https://fleet.example.test", "--output", str(output)]
        captured = io.StringIO()
        original_umask = os.umask(0o077)
        try:
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(captured):
                manage.main()
        finally:
            os.umask(original_umask)
        enrollment = json.loads(output.read_text())
        self.assertEqual(len(enrollment["secret"]), 64)
        self.assertEqual(enrollment["url"], "https://fleet.example.test/api/v1/heartbeat")
        self.assertNotIn(enrollment["secret"], captured.getvalue())
        self.assertNotEqual(enrollment["panel_id"], self.panel)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(health_agent.load_config(output), enrollment)


class AgentTest(unittest.TestCase):
    def config(self):
        return {"panel_id":"astra-test0001", "secret":"ab"*32, "url":"https://astra.example.test/api/v1/heartbeat"}

    def test_agent_signs_measurements_and_does_not_read_commands(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 204
        opener = mock.Mock()
        opener.open.return_value = response
        health_agent.send(self.config(), HEALTH, opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.config()["url"])
        self.assertEqual(json.loads(request.data), HEALTH)
        self.assertNotIn(self.config()["secret"], str(request.headers))
        self.assertEqual(request.headers["X-astra-signature"], protocol.signature(self.config()["secret"], self.config()["panel_id"], request.headers["X-astra-time"], request.headers["X-astra-nonce"], request.data))
        response.__enter__.return_value.read.assert_not_called()
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 8)

    def test_redirects_are_refused(self):
        with self.assertRaises(Exception):
            health_agent.NoRedirect().redirect_request(mock.Mock(full_url="https://astra.example.test"), None, 302, "", {}, "http://elsewhere.invalid")

    def test_http_and_credential_urls_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"enrollment.json"
            for url in ("http://astra.example.test/api/v1/heartbeat", "https://user:pass@astra.example.test/api/v1/heartbeat", "https://astra.example.test/api/command"):
                path.write_text(json.dumps({**self.config(), "url":url}))
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    health_agent.load_config(path)

    def test_agent_rejects_readable_credential_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"enrollment.json"
            path.write_text(json.dumps(self.config()))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                health_agent.load_config(path)


if __name__ == "__main__":
    unittest.main()
