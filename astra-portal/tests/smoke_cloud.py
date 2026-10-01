"""Optional real-process loopback smoke check, with an unauthenticated OIDC stub.

Does not test a real identity provider or issue TLS certificates. Needs the
official Caddy executable via --caddy; Gunicorn comes from requirements.lock.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol
import store

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port, path, body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def wait_ready(port, path):
    for _ in range(60):
        try:
            request(port, path)
            return
        except (OSError, urllib.error.URLError):
            time.sleep(.1)
    raise RuntimeError("Local test service did not start.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--caddy", required=True)
    args = parser.parse_args()
    processes = []
    observations = []

    class AuthStub(BaseHTTPRequestHandler):
        def do_GET(self):
            observations.append(dict(self.headers))
            self.send_response(403)
            self.end_headers()
        def log_message(self, *args):
            pass

    auth = ThreadingHTTPServer(("127.0.0.1", 0), AuthStub)
    threading.Thread(target=auth.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="astra-cloud-smoke-") as temp:
            root = Path(temp)
            backend, public = free_port(), free_port()
            operators = root / "operators.txt"
            operators.write_text("operator@example.test\n")
            data = root / "data"
            store.initialize(data)
            panel_id, secret = "astra-smoke0001", secrets.token_hex(32)
            with store.connect(data) as db:
                db.execute("INSERT INTO panels(id,site,name,secret,enrolled_at) VALUES(?,?,?,?,?)", (panel_id, "Synthetic smoke test", "Test", secret, int(time.time())))
            env = {**os.environ, "ASTRA_MODE":"production", "ASTRA_DATA_DIR":str(data), "ASTRA_OPERATORS_FILE":str(operators), "ASTRA_DOMAIN":f"http://127.0.0.1:{public}", "ACME_EMAIL":"test@example.test", "XDG_DATA_HOME":str(root/"xdg-data"), "XDG_CONFIG_HOME":str(root/"xdg-config")}
            gunicorn = Path(sys.executable).parent / "gunicorn"
            processes.append(subprocess.Popen([str(gunicorn), "--bind", f"127.0.0.1:{backend}", "--workers", "2", "--threads", "2", "--umask", "0o077", "app:create_app()"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            wait_ready(backend, "/healthz")
            assert request(backend, "/api/fleet")[0] == 403
            caddyfile = (ROOT/"deploy/Caddyfile").read_text().replace("portal:8000", f"127.0.0.1:{backend}").replace("auth:4180", f"127.0.0.1:{auth.server_port}")
            (root/"Caddyfile").write_text(caddyfile)
            processes.append(subprocess.Popen([args.caddy, "run", "--config", str(root/"Caddyfile"), "--adapter", "caddyfile"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            wait_ready(public, "/")
            forged = {"X-Forwarded-Email":"operator@example.test", "X-Forwarded-User":"forged", "X-Auth-Request-Email":"operator@example.test"}
            assert request(public, "/api/fleet", headers=forged)[0] == 403
            assert not any(key.lower() in {"x-forwarded-email", "x-forwarded-user", "x-auth-request-email"} for key in observations[-1])
            assert request(public, "/api/v1/heartbeat")[0] == 403  # GET still requires login.
            health = {"schema":1, "version":"0.2.44", "uptime_s":60, "temperature_c":48, "memory_used_pct":30, "disk_used_pct":20, "load_1m":.1, "panel_service":"active"}
            body = protocol.encode(health)
            timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
            headers = {"Content-Type":"application/json", "X-Astra-Panel":panel_id, "X-Astra-Time":timestamp, "X-Astra-Nonce":nonce, "X-Astra-Signature":protocol.signature(secret, panel_id, timestamp, nonce, body)}
            assert request(public, "/api/v1/heartbeat", body, headers)[0] == 204
            assert request(public, "/api/v1/heartbeat", body, headers)[0] == 409
            # This is a private-backend identity fixture, not a real OIDC login.
            status, result = request(backend, "/api/fleet", headers={"X-Forwarded-Email":"operator@example.test"})
            assert status == 200
            assert json.loads(result)["panels"][0]["health"] == health
            assert secret.encode() not in result
            print("PASS: two-worker Gunicorn, real Caddy routing, forged identity stripping, signed heartbeat, replay rejection and private inventory read.")
            print("Real OIDC/MFA and public HTTPS still require the new cloud environment.")
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        auth.shutdown()
        auth.server_close()


if __name__ == "__main__":
    main()
